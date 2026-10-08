#!/usr/bin/env python
# encoding: utf-8

# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements. See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership. The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License. You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied. See the License for the
# specific language governing permissions and limitations
# under the License.

"""CREATE, replacement and incremental coverage for an Iceberg catalog.

Set DBT_DORIS_ICEBERG_CATALOG to an existing writable Iceberg catalog and use
the normal DORIS_TEST_* connection settings. These tests create isolated test
namespaces, but do not set up a REST service or object storage.
"""

import os
import re

import pytest
import yaml
from dbt.tests.util import relation_from_name, run_dbt, write_file

from test.functional.adapter.test_doris_incremental import _run_and_capture_sql

ICEBERG_CATALOG = os.getenv("DBT_DORIS_ICEBERG_CATALOG")
pytestmark = pytest.mark.skipif(
    not ICEBERG_CATALOG,
    reason="DBT_DORIS_ICEBERG_CATALOG must name a writable Iceberg catalog",
)

DATA_SQL = """
select cast(1 as int) as id, cast('first' as string) as value
union all
select cast(2 as int) as id, cast('second' as string) as value
"""
EXPECTED_ROWS = [(1, "first"), (2, "second")]
CREATE_CONFIGS = {
    "iceberg_table_default": "materialized='table'",
    "iceberg_table_explicit": "materialized='table', engine='iceberg'",
    "iceberg_merge_initial": (
        "materialized='incremental', incremental_strategy='merge', unique_key=['id']"
    ),
    "iceberg_table_documented": ("materialized='table', persist_docs={'columns': true}"),
    "iceberg_merge_documented": (
        "materialized='incremental', incremental_strategy='merge', unique_key=['id'], "
        "engine='iceberg', persist_docs={'columns': true}"
    ),
}


@pytest.fixture(scope="class")
def dbt_profile_target(dbt_profile_target):
    return {**dbt_profile_target, "database": ICEBERG_CATALOG}


@pytest.fixture(scope="class")
def dbt_project_yml(project_root, project_config_update):
    # The suite's default fixture injects OLAP replication properties. External
    # CREATE must receive only this project's explicit resource settings.
    project_config = {
        "name": "test",
        "profile": "test",
        "flags": {"send_anonymous_usage_stats": False},
        # list_schemas currently interpolates a dbt-quoted catalog as a string.
        "quoting": {"database": False},
    }
    project_config.update(project_config_update)
    write_file(yaml.safe_dump(project_config), project_root, "dbt_project.yml")
    return project_config


class TestDorisIcebergFirstCreate:
    @pytest.fixture(scope="class")
    def models(self):
        models = {
            f"{name}.sql": "{{ config(" + config + ") }}\n" + DATA_SQL
            for name, config in CREATE_CONFIGS.items()
        }
        models["iceberg_explicit_olap.sql"] = (
            "{{ config(materialized='table', engine='OLAP') }}\n" + DATA_SQL
        )
        models["schema.yml"] = yaml.safe_dump(
            {
                "version": 2,
                "models": [
                    {
                        "name": name,
                        "columns": [
                            {"name": "id", "description": "Issue 1 identifier"},
                            {"name": "value", "description": "Issue 1 value"},
                        ],
                    }
                    for name in ("iceberg_table_documented", "iceberg_merge_documented")
                ],
            }
        )
        return models

    @pytest.fixture(scope="class")
    def seeds(self):
        return {"iceberg_seed_default.csv": "id,value\n1,first\n2,second\n"}

    @pytest.fixture(scope="class")
    def project_config_update(self):
        return {"seeds": {"+column_types": {"id": "INT", "value": "STRING"}}}

    @staticmethod
    def assert_iceberg_rows(project, name):
        relation = relation_from_name(project.adapter, name)
        ddl = project.run_sql(f"show create table {relation}", fetch="one")[1]
        assert "ICEBERG_EXTERNAL_TABLE" in ddl.upper()
        assert "UNIQUE KEY" not in ddl.upper()
        assert "enable_unique_key_merge_on_write" not in ddl.lower()
        assert (
            project.run_sql(f"select id, value from {relation} order by id", fetch="all")
            == EXPECTED_ROWS
        )
        return ddl

    @pytest.mark.parametrize("name", CREATE_CONFIGS)
    def test_model_first_create(self, project, name):
        # Each model name runs once. Reruns would test unsupported lifecycle
        # operations rather than the CREATE boundary guarded by this regression.
        results = run_dbt(["run", "--select", name])
        assert len(results) == 1
        ddl = self.assert_iceberg_rows(project, name)
        if name.endswith("_documented"):
            assert "Issue 1 identifier" in ddl
            assert "Issue 1 value" in ddl

    def test_seed_first_create_without_engine(self, project):
        results = run_dbt(["seed", "--select", "iceberg_seed_default"])
        assert len(results) == 1
        self.assert_iceberg_rows(project, "iceberg_seed_default")

    def test_explicit_olap_is_rejected(self, project):
        failure = run_dbt(["run", "--select", "iceberg_explicit_olap"], expect_pass=False)
        assert len(failure.results) == 1
        error = failure.results[0].message.lower()
        assert "olap" in error
        assert "catalog" in error

        relation = relation_from_name(project.adapter, "iceberg_explicit_olap")
        tables = project.run_sql(
            f"show tables from `{relation.database}`.`{relation.schema}`", fetch="all"
        )
        names = {row[0] for row in tables}
        assert relation.identifier not in names
        assert relation.identifier + "__dbt_tmp" not in names


REPLACEMENT_SQL = """
{% if var('fail_build', false) %}
select missing_issue2_column from numbers("number" = "1")
{% elif var('batch', 1) == 1 %}
select cast(1 as int) as id, cast('first' as string) as value
union all
select cast(2 as int) as id, cast('second' as string) as value
{% elif var('batch', 1) == 2 %}
select cast(2 as int) as id, cast('updated' as string) as value
union all
select cast(3 as int) as id, cast('new' as string) as value
{% else %}
select cast(4 as int) as id, cast('replacement' as string) as value
{% endif %}
"""
REPLACEMENT_CONFIGS = {
    "iceberg_replace_default": "materialized='table'",
    "iceberg_replace_explicit": "materialized='table', engine='iceberg'",
    "iceberg_replace_quoted": "materialized='table', alias='select'",
    "iceberg_refresh_default": (
        "materialized='incremental', incremental_strategy='insert_overwrite'"
    ),
    "iceberg_refresh_explicit": (
        "materialized='incremental', incremental_strategy='insert_overwrite', "
        "engine='iceberg'"
    ),
    "iceberg_failed_replace_default": "materialized='table'",
    "iceberg_failed_replace_explicit": "materialized='table', engine='iceberg'",
    "iceberg_interrupted_table": "materialized='table'",
    "iceberg_interrupted_refresh": (
        "materialized='incremental', incremental_strategy='insert_overwrite', "
        "engine='iceberg'"
    ),
}


class TestDorisIcebergReplacement:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            f"{name}.sql": "{{ config(" + config + ") }}\n" + REPLACEMENT_SQL
            for name, config in REPLACEMENT_CONFIGS.items()
        }

    @staticmethod
    def assert_target(project, name, expected_rows):
        relation = relation_from_name(project.adapter, name)
        ddl = project.run_sql(f"show create table {relation}", fetch="one")[1]
        assert "ICEBERG_EXTERNAL_TABLE" in ddl.upper()
        assert (
            project.run_sql(
                f"select id, value from {relation} order by id", fetch="all"
            )
            == expected_rows
        )
        tables = project.run_sql(
            f"show tables from `{relation.database}`.`{relation.schema}`", fetch="all"
        )
        names = {row[0] for row in tables}
        assert relation.identifier in names
        assert relation.identifier + "__dbt_tmp" not in names
        assert relation.identifier + "__dbt_backup" not in names

    @pytest.mark.parametrize(
        "name", ["iceberg_replace_default", "iceberg_replace_explicit"]
    )
    def test_table_rerun_and_full_refresh_replace_all_data(self, project, name):
        run_dbt(["run", "--select", name])
        self.assert_target(project, name, EXPECTED_ROWS)

        run_dbt(["run", "--select", name, "--vars", "{batch: 2}"])
        self.assert_target(project, name, [(2, "updated"), (3, "new")])

        run_dbt(["run", "--select", name, "--full-refresh", "--vars", "{batch: 3}"])
        self.assert_target(project, name, [(4, "replacement")])

    def test_reserved_target_name_can_be_replaced(self, project):
        run_dbt(["run", "--select", "iceberg_replace_quoted"])
        self.assert_target(project, "select", EXPECTED_ROWS)

        run_dbt(["run", "--select", "iceberg_replace_quoted", "--vars", "{batch: 2}"])
        self.assert_target(project, "select", [(2, "updated"), (3, "new")])

    @pytest.mark.parametrize(
        "name", ["iceberg_refresh_default", "iceberg_refresh_explicit"]
    )
    def test_incremental_full_refresh_replaces_all_data(self, project, name):
        run_dbt(["run", "--select", name])
        self.assert_target(project, name, EXPECTED_ROWS)

        # Ordinary incremental runs exercise a separate strategy/temporary-view
        # path. This regression covers the shared full-refresh replacement path.
        run_dbt(["run", "--select", name, "--full-refresh", "--vars", "{batch: 2}"])
        self.assert_target(project, name, [(2, "updated"), (3, "new")])

        run_dbt(["run", "--select", name, "--full-refresh", "--vars", "{batch: 3}"])
        self.assert_target(project, name, [(4, "replacement")])

    @pytest.mark.parametrize(
        "name",
        ["iceberg_failed_replace_default", "iceberg_failed_replace_explicit"],
    )
    def test_failed_build_keeps_existing_target(self, project, name):
        run_dbt(["run", "--select", name])
        self.assert_target(project, name, EXPECTED_ROWS)

        failure = run_dbt(
            ["run", "--select", name, "--vars", "{fail_build: true}"],
            expect_pass=False,
        )
        assert len(failure.results) == 1
        assert "missing_issue2_column" in failure.results[0].message
        self.assert_target(project, name, EXPECTED_ROWS)

        run_dbt(["run", "--select", name, "--vars", "{batch: 2}"])
        self.assert_target(project, name, [(2, "updated"), (3, "new")])

    @pytest.mark.parametrize(
        "name,refresh_args",
        [
            ("iceberg_interrupted_table", []),
            ("iceberg_interrupted_refresh", ["--full-refresh"]),
        ],
    )
    def test_interrupted_publish_preserves_backup_through_failed_retry(
        self, project, name, refresh_args
    ):
        run_dbt(["run", "--select", name])
        self.assert_target(project, name, EXPECTED_ROWS)

        # Reproduce the state after target -> backup succeeds and publishing
        # the replacement fails, without changing production adapter macros.
        relation = relation_from_name(project.adapter, name)
        project.run_sql(
            f"alter table {relation} rename `{relation.identifier}__dbt_backup`"
        )

        run_args = ["run", "--select", name] + refresh_args
        failure = run_dbt(
            run_args + ["--vars", "{fail_build: true}"], expect_pass=False
        )
        assert len(failure.results) == 1
        assert "missing_issue2_column" in failure.results[0].message
        if refresh_args:
            # Incremental recovery retains the backup while rebuilding a
            # missing target. A failed rebuild must leave that data available.
            backup = relation.incorporate(
                path={"identifier": relation.identifier + "__dbt_backup"}
            )
            assert (
                project.run_sql(
                    f"select id, value from {backup} order by id", fetch="all"
                )
                == EXPECTED_ROWS
            )
            tables = project.run_sql(
                f"show tables from `{relation.database}`.`{relation.schema}`",
                fetch="all",
            )
            names = {row[0] for row in tables}
            assert relation.identifier not in names
            assert backup.identifier in names
        else:
            self.assert_target(project, name, EXPECTED_ROWS)

        run_dbt(run_args + ["--vars", "{batch: 2}"])
        self.assert_target(project, name, [(2, "updated"), (3, "new")])


OVERWRITE_SQL = """
{% if var('empty_batch', false) %}
select cast(null as int) as id, cast(null as string) as value where false
{% else %}
select id, value
{% if var('extra_column', false) %}
  , cast({{ var('extra_value', 99) }} as int) as extra
{% endif %}
from (
""" + REPLACEMENT_SQL.replace("missing_issue2_column", "missing_issue3_column") + """
) as issue3_batch
{% endif %}
"""
OVERWRITE_CONFIGS = {
    "iceberg_overwrite_default": "",
    "iceberg_overwrite_explicit": ", engine='iceberg'",
    "iceberg_overwrite_fail": ", on_schema_change='fail'",
    "iceberg_overwrite_ignore": ", on_schema_change='ignore'",
    "iceberg_overwrite_failed_default": "",
    "iceberg_overwrite_failed_explicit": ", engine='iceberg'",
    "iceberg_overwrite_append_columns": ", on_schema_change='append_new_columns'",
    "iceberg_overwrite_sync_columns": ", on_schema_change='sync_all_columns'",
    "iceberg_overwrite_empty": "",
    "iceberg_overwrite_invalid_key": "",
    "iceberg_overwrite_invalid_partition": (
        ", overwrite_partitions=var('overwrite_partitions', none)"
        ", partition_by=var('partition_by', none)"
    ),
}


class TestDorisIcebergIncrementalOverwrite:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            f"{name}.sql": (
                "{{ config(materialized='incremental', "
                "incremental_strategy='insert_overwrite', "
                "unique_key=var('overwrite_key', none)"
                + config
                + ") }}\n"
                + OVERWRITE_SQL
            )
            for name, config in OVERWRITE_CONFIGS.items()
        }

    @staticmethod
    def assert_target(project, name, expected_rows, columns=("id", "value")):
        relation = relation_from_name(project.adapter, name)
        ddl = project.run_sql(f"show create table {relation}", fetch="one")[1]
        assert "ICEBERG_EXTERNAL_TABLE" in ddl.upper()
        assert "UNIQUE KEY" not in ddl.upper()
        actual_columns = project.run_sql(f"describe {relation}", fetch="all")
        assert [column[0] for column in actual_columns] == list(columns)
        assert project.run_sql(
            f"select {', '.join(columns)} from {relation} order by id", fetch="all"
        ) == expected_rows
        names = {
            row[0]
            for row in project.run_sql(
                f"show tables from `{relation.database}`.`{relation.schema}`",
                fetch="all",
            )
        }
        assert relation.identifier in names
        assert relation.identifier + "__dbt_tmp" not in names
        assert relation.identifier + "__dbt_backup" not in names

    @staticmethod
    def assert_physical_overwrite(statements, name):
        stages = [
            statement
            for statement in statements
            if "create table" in statement
            and name + "__dbt_tmp" in statement
            and " as " in statement
        ]
        assert len(stages) == 1
        assert "unique key" not in stages[0]
        assert "distributed by" not in stages[0]
        assert not any("create or replace view" in sql for sql in statements)
        dml = [
            statement
            for statement in statements
            if re.search(r"\binsert\s+(?:into|overwrite\s+table)\s+", statement)
        ]
        assert len(dml) == 1
        assert "insert overwrite table" in dml[0]
        assert name + "__dbt_tmp" in dml[0]
        assert "unique key" not in dml[0]

    @pytest.mark.parametrize(
        "name", ["iceberg_overwrite_default", "iceberg_overwrite_explicit"]
    )
    def test_repeated_incremental_runs_replace_all_rows(self, project, name):
        run_dbt(["run", "--select", name])
        self.assert_target(project, name, EXPECTED_ROWS)

        for _ in range(2):
            results, statements = _run_and_capture_sql(
                name, ["run", "--select", name, "--vars", "{batch: 2}"]
            )
            assert len(results) == 1
            self.assert_physical_overwrite(statements, name)
            self.assert_target(project, name, [(2, "updated"), (3, "new")])

        # Full refresh publishes a renamed Iceberg table. A subsequent frozen
        # batch must safely reuse dbt's staging name and overwrite that target.
        run_dbt(["run", "--select", name, "--full-refresh", "--vars", "{batch: 3}"])
        self.assert_target(project, name, [(4, "replacement")])

        _, statements = _run_and_capture_sql(
            name, ["run", "--select", name, "--vars", "{batch: 2}"]
        )
        self.assert_physical_overwrite(statements, name)
        self.assert_target(project, name, [(2, "updated"), (3, "new")])

    def test_fail_policy_with_unchanged_schema_can_overwrite(self, project):
        name = "iceberg_overwrite_fail"
        run_dbt(["run", "--select", name])
        self.assert_target(project, name, EXPECTED_ROWS)

        _, statements = _run_and_capture_sql(
            name, ["run", "--select", name, "--vars", "{batch: 2}"]
        )
        self.assert_physical_overwrite(statements, name)
        self.assert_target(project, name, [(2, "updated"), (3, "new")])

    def test_fail_policy_rejects_extra_column_before_target_dml(self, project):
        name = "iceberg_overwrite_fail"
        run_dbt(["run", "--select", name, "--full-refresh"])
        self.assert_target(project, name, EXPECTED_ROWS)

        failure, statements = _run_and_capture_sql(
            name,
            ["run", "--select", name, "--vars", "{batch: 2, extra_column: true}"],
            expect_pass=False,
        )
        assert len(failure.results) == 1
        assert "schema" in failure.results[0].message.lower()
        assert not any("insert overwrite table" in sql for sql in statements)
        self.assert_target(project, name, EXPECTED_ROWS)

    def test_ignore_policy_does_not_add_extra_source_column(self, project):
        name = "iceberg_overwrite_ignore"
        run_dbt(["run", "--select", name])
        self.assert_target(project, name, EXPECTED_ROWS)

        _, statements = _run_and_capture_sql(
            name,
            ["run", "--select", name, "--vars", "{batch: 2, extra_column: true}"],
        )
        self.assert_physical_overwrite(statements, name)
        self.assert_target(project, name, [(2, "updated"), (3, "new")])

    def test_append_policy_adds_column_and_writes_new_values(self, project):
        name = "iceberg_overwrite_append_columns"
        run_dbt(["run", "--select", name])
        self.assert_target(project, name, EXPECTED_ROWS)

        _, statements = _run_and_capture_sql(
            name,
            ["run", "--select", name, "--vars", "{batch: 2, extra_column: true}"],
        )
        self.assert_physical_overwrite(statements, name)
        self.assert_target(
            project, name, [(2, "updated", 99), (3, "new", 99)],
            columns=("id", "value", "extra"),
        )

        run_dbt([
            "run", "--select", name, "--vars",
            "{batch: 3, extra_column: true, extra_value: 123}",
        ])
        self.assert_target(
            project, name, [(4, "replacement", 123)],
            columns=("id", "value", "extra"),
        )

    def test_sync_policy_adds_and_removes_columns_with_complete_data(self, project):
        name = "iceberg_overwrite_sync_columns"
        run_dbt([
            "run", "--select", name, "--vars", "{extra_column: true}",
        ])
        self.assert_target(
            project, name, [(1, "first", 99), (2, "second", 99)],
            columns=("id", "value", "extra"),
        )

        _, statements = _run_and_capture_sql(
            name, ["run", "--select", name, "--vars", "{batch: 2}"]
        )
        self.assert_physical_overwrite(statements, name)
        self.assert_target(project, name, [(2, "updated"), (3, "new")])

        run_dbt([
            "run", "--select", name, "--vars",
            "{batch: 3, extra_column: true, extra_value: 123}",
        ])
        self.assert_target(
            project, name, [(4, "replacement", 123)],
            columns=("id", "value", "extra"),
        )

    def test_empty_batch_has_native_iceberg_overwrite_semantics(self, project):
        name = "iceberg_overwrite_empty"
        run_dbt(["run", "--select", name])
        self.assert_target(project, name, EXPECTED_ROWS)

        _, statements = _run_and_capture_sql(
            name, ["run", "--select", name, "--vars", "{empty_batch: true}"]
        )
        self.assert_physical_overwrite(statements, name)
        self.assert_target(project, name, [])

    @pytest.mark.parametrize(
        "name,vars_argument,error_fragment",
        [
            ("iceberg_overwrite_invalid_key", "{overwrite_key: [id]}", "unique_key"),
            (
                "iceberg_overwrite_invalid_partition",
                "{overwrite_partitions: [p1], partition_by: [id]}",
                "overwrite_partitions",
            ),
        ],
    )
    def test_olap_configs_are_rejected_before_staging_or_target_writes(
        self, project, name, vars_argument, error_fragment
    ):
        run_dbt(["run", "--select", name])
        self.assert_target(project, name, EXPECTED_ROWS)

        failure, statements = _run_and_capture_sql(
            name,
            ["run", "--select", name, "--vars", vars_argument],
            expect_pass=False,
        )
        assert len(failure.results) == 1
        assert error_fragment in failure.results[0].message
        assert not any("insert overwrite table" in sql for sql in statements)
        assert not any("create table" in sql for sql in statements)
        assert not any("create or replace view" in sql for sql in statements)
        self.assert_target(project, name, EXPECTED_ROWS)

    @pytest.mark.parametrize(
        "name",
        ["iceberg_overwrite_failed_default", "iceberg_overwrite_failed_explicit"],
    )
    def test_failed_source_keeps_target_and_retry_cleans_stage(self, project, name):
        run_dbt(["run", "--select", name])
        self.assert_target(project, name, EXPECTED_ROWS)

        # An interrupted prior invocation may leave a frozen batch. A failed
        # new source must clean that helper without writing to the target.
        relation = relation_from_name(project.adapter, name)
        stage = relation.incorporate(
            path={"identifier": relation.identifier + "__dbt_tmp"}
        )
        project.run_sql(f"create table {stage} as select * from {relation}")
        failure, statements = _run_and_capture_sql(
            name,
            ["run", "--select", name, "--vars", "{fail_build: true}"],
            expect_pass=False,
        )
        assert len(failure.results) == 1
        assert "missing_issue3_column" in failure.results[0].message
        assert not any("insert overwrite table" in sql for sql in statements)
        self.assert_target(project, name, EXPECTED_ROWS)

        _, statements = _run_and_capture_sql(
            name, ["run", "--select", name, "--vars", "{batch: 2}"]
        )
        self.assert_physical_overwrite(statements, name)
        self.assert_target(project, name, [(2, "updated"), (3, "new")])
