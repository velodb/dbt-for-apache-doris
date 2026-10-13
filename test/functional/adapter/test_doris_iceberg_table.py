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


class TestDorisIcebergViewRejection:
    @pytest.fixture(scope="class")
    def models(self):
        names = [
            f"iceberg_view_switch_{engine}_{mode}"
            for engine in ("default", "iceberg", "olap")
            for mode in ("ordinary", "refresh")
        ]
        switch_config = """
{{ config(
    materialized=var('view_materialization', 'table'),
    engine=var('view_engine', none)
) }}
{% if var('view_materialization', 'table') == 'view' %}
{{ config(
    pre_hook='insert into ' ~ this ~ " (id, value) values (99, 'view hook ran')",
    sql_header='set enable_insert_strict=true;'
) }}
{% endif %}
"""
        models = {name + ".sql": switch_config + DATA_SQL for name in names}
        models["iceberg_view_initial.sql"] = """
{{ config(
    materialized='view',
    pre_hook='select 42 as external_view_hook',
    sql_header='set enable_insert_strict=true;'
) }}
""" + DATA_SQL
        models["iceberg_view_source.sql"] = "{{ config(materialized='table') }}\n" + DATA_SQL
        models["internal_iceberg_view.sql"] = """
{{ config(materialized='view', database='internal') }}
select * from {{ ref('iceberg_view_source') }}
"""
        return models

    @staticmethod
    def assert_no_view_side_effects(statements):
        assert not any(
            sql_fragment in sql
            for sql in statements
            for sql_fragment in (
                "drop table", "drop view", "insert into",
                "create or replace view", "set enable_insert_strict",
                "external_view_hook",
            )
        )

    @pytest.mark.parametrize("engine", ["default", "iceberg", "olap"])
    @pytest.mark.parametrize("mode", ["ordinary", "refresh"])
    def test_table_to_view_rejection_preserves_original_before_hooks(
        self, project, engine, mode
    ):
        name = f"iceberg_view_switch_{engine}_{mode}"
        run_dbt(["run", "--select", name])
        relation = relation_from_name(project.adapter, name)
        columns_before = project.run_sql(f"describe {relation}", fetch="all")
        ddl_before = project.run_sql(f"show create table {relation}", fetch="one")
        assert project.run_sql(
            f"select id, value from {relation} order by id", fetch="all"
        ) == EXPECTED_ROWS

        variables = {"view_materialization": "view"}
        if engine != "default":
            variables["view_engine"] = "OLAP" if engine == "olap" else "iceberg"
        args = ["run", "--select", name, "--vars", yaml.safe_dump(variables)]
        if mode == "refresh":
            args.append("--full-refresh")
        for _ in range(2):
            failure, statements = _run_and_capture_sql(name, list(args), expect_pass=False)
            message = failure.results[0].message
            assert "Compilation Error" in message
            assert "View in external Catalog" in message
            assert relation.database in message
            self.assert_no_view_side_effects(statements)
            assert project.run_sql(f"describe {relation}", fetch="all") == columns_before
            assert project.run_sql(f"show create table {relation}", fetch="one") == ddl_before
            assert project.run_sql(
                f"select id, value from {relation} order by id", fetch="all"
            ) == EXPECTED_ROWS

        run_dbt(["run", "--select", name])
        TestDorisIcebergReplacement.assert_target(project, name, EXPECTED_ROWS)

    def test_initial_external_view_rejection_happens_before_hooks(self, project):
        name = "iceberg_view_initial"
        failure, statements = _run_and_capture_sql(
            name, ["run", "--select", name], expect_pass=False
        )
        assert "View in external Catalog" in failure.results[0].message
        self.assert_no_view_side_effects(statements)
        relation = relation_from_name(project.adapter, name)
        names = {
            row[0] for row in project.run_sql(
                f"show tables from `{relation.database}`.`{relation.schema}`", fetch="all"
            )
        }
        assert relation.identifier not in names

    def test_internal_view_can_still_query_iceberg_source(self, project):
        source = relation_from_name(project.adapter, "iceberg_view_source")
        internal_databases = {
            row[0] for row in project.run_sql("show databases from internal", fetch="all")
        }
        assert source.schema not in internal_databases
        view = source.incorporate(
            path={"database": "internal", "identifier": "internal_iceberg_view"},
            type="view",
        )
        try:
            results = run_dbt(["run", "--select", "+internal_iceberg_view"])
            assert len(results) == 2
            assert project.run_sql(
                f"select id, value from {view} order by id", fetch="all"
            ) == EXPECTED_ROWS
            assert "ICEBERG_EXTERNAL_TABLE" in project.run_sql(
                f"show create table {source}", fetch="one"
            )[1].upper()
        finally:
            project.run_sql(f"drop database if exists `internal`.`{source.schema}`")


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


class TestDorisIcebergSeed:
    @pytest.fixture(scope="class")
    def seeds(self):
        return {
            "iceberg_seed_reload_default.csv": "id,value\n1,first\n2,second\n",
            "iceberg_seed_reload_explicit.csv": "id,value\n1,first\n2,second\n",
            "iceberg_seed_empty_default.csv": "id,value\n1,first\n2,second\n",
            "iceberg_seed_empty_explicit.csv": "id,value\n1,first\n2,second\n",
            "iceberg_seed_schema.csv": "id,value\n1,first\n2,second\n",
            "iceberg_seed_failed_load.csv": "id,value\n1,first\n2,second\n",
            "iceberg_seed_failed_refresh.csv": "id,value\n1,first\n2,second\n",
            "iceberg_seed_quoted.csv": 'select,value note\n1,"O\'Reilly, value"\n2,\n',
            "iceberg_seed_batches.csv": "id,value\n1,one\n2,two\n3,three\n4,four\n5,five\n",
            "iceberg_seed_recovery_missing.csv": "id,value\n1,first\n2,second\n",
            "iceberg_seed_recovery_conflict.csv": "id,value\n1,first\n2,second\n",
        }

    @pytest.fixture(scope="class")
    def macros(self):
        # Exercise the production batching path with three small INSERTs.
        return {
            "seed_batch_size.sql": (
                "{% macro get_batch_size() %}{{ return(2) }}{% endmacro %}"
            )
        }

    @pytest.fixture(scope="class")
    def project_config_update(self):
        return {
            "seeds": {
                "+column_types": {"id": "INT", "value": "STRING"},
                "test": {
                    "iceberg_seed_reload_explicit": {"engine": "iceberg"},
                    "iceberg_seed_empty_explicit": {"engine": "iceberg"},
                    "iceberg_seed_schema": {"column_types": {"extra": "INT"}},
                    "iceberg_seed_failed_load": {
                        "pre-hook": "set enable_strict_cast=true"
                    },
                    "iceberg_seed_failed_refresh": {
                        "pre-hook": "set enable_strict_cast=true"
                    },
                    "iceberg_seed_recovery_missing": {
                        "pre-hook": "set enable_strict_cast=true"
                    },
                    "iceberg_seed_quoted": {
                        "quote_columns": True,
                        "column_types": {"select": "INT", "value note": "STRING"},
                    },
                },
            }
        }

    @staticmethod
    def assert_target(
        project, name, expected_rows, columns=("id", "value"), allow_stage=False
    ):
        relation = relation_from_name(project.adapter, name)
        ddl = project.run_sql(f"show create table {relation}", fetch="one")[1]
        assert "ICEBERG_EXTERNAL_TABLE" in ddl.upper()
        actual_columns = project.run_sql(f"describe {relation}", fetch="all")
        assert [column[0] for column in actual_columns] == list(columns)
        columns_sql = ", ".join(project.adapter.quote(column) for column in columns)
        assert project.run_sql(
            f"select {columns_sql} from {relation} order by 1", fetch="all"
        ) == expected_rows
        names = {
            row[0]
            for row in project.run_sql(
                f"show tables from `{relation.database}`.`{relation.schema}`",
                fetch="all",
            )
        }
        assert relation.identifier in names
        if not allow_stage:
            assert relation.identifier + "__dbt_tmp" not in names
        assert relation.identifier + "__dbt_backup" not in names

    @staticmethod
    def assert_one_overwrite(statements, name):
        overwrites = [
            sql for sql in statements if "insert overwrite table" in sql
        ]
        assert len(overwrites) == 1
        assert name + "__dbt_tmp" in overwrites[0]
        assert not any("truncate table" in sql for sql in statements)

    @staticmethod
    def write_csv(project, name, csv):
        write_file(csv, project.project_root, "seeds", name + ".csv")

    @pytest.mark.parametrize(
        "name", ["iceberg_seed_reload_default", "iceberg_seed_reload_explicit"]
    )
    def test_repeated_seed_replaces_all_rows(self, project, name):
        run_dbt(["seed", "--select", name])
        self.assert_target(project, name, EXPECTED_ROWS)

        _, statements = _run_and_capture_sql(name, ["seed", "--select", name])
        self.assert_one_overwrite(statements, name)
        self.assert_target(project, name, EXPECTED_ROWS)

        self.write_csv(project, name, "id,value\n1,ONE\n3,three\n")
        _, statements = _run_and_capture_sql(name, ["seed", "--select", name])
        self.assert_one_overwrite(statements, name)
        self.assert_target(project, name, [(1, "ONE"), (3, "three")])

    @pytest.mark.parametrize(
        "name", ["iceberg_seed_empty_default", "iceberg_seed_empty_explicit"]
    )
    def test_header_only_seed_clears_all_rows(self, project, name):
        run_dbt(["seed", "--select", name])
        self.assert_target(project, name, EXPECTED_ROWS)

        self.write_csv(project, name, "id,value\n")
        _, statements = _run_and_capture_sql(name, ["seed", "--select", name])
        self.assert_one_overwrite(statements, name)
        self.assert_target(project, name, [])

        run_dbt(["seed", "--select", name])
        self.assert_target(project, name, [])

    def test_changed_schema_requires_full_refresh_without_changing_target(self, project):
        name = "iceberg_seed_schema"
        run_dbt(["seed", "--select", name])
        self.assert_target(project, name, EXPECTED_ROWS)

        self.write_csv(project, name, "id,value,extra\n1,ONE,99\n3,three,123\n")
        failure, statements = _run_and_capture_sql(
            name, ["seed", "--select", name], expect_pass=False
        )
        assert len(failure.results) == 1
        message = failure.results[0].message.lower()
        assert "schema" in message
        assert "full-refresh" in message
        assert not any("insert overwrite table" in sql for sql in statements)
        self.assert_target(project, name, EXPECTED_ROWS, allow_stage=True)

        run_dbt(["seed", "--select", name, "--full-refresh"])
        self.assert_target(
            project, name, [(1, "ONE", 99), (3, "three", 123)],
            columns=("id", "value", "extra"),
        )

        run_dbt(["seed", "--select", name])
        self.assert_target(
            project, name, [(1, "ONE", 99), (3, "three", 123)],
            columns=("id", "value", "extra"),
        )

    @pytest.mark.parametrize(
        "name,refresh_args",
        [
            ("iceberg_seed_failed_load", []),
            ("iceberg_seed_failed_refresh", ["--full-refresh"]),
        ],
    )
    def test_failed_binding_load_preserves_target_and_can_retry(
        self, project, name, refresh_args
    ):
        run_dbt(["seed", "--select", name])
        self.assert_target(project, name, EXPECTED_ROWS)

        # The first two rows load successfully into the stage. A later binding
        # batch fails, so publishing earlier batches would corrupt the target.
        self.write_csv(project, name, "id,value\n3,three\n4,four\nbad,invalid\n")
        failure, statements = _run_and_capture_sql(
            name, ["seed", "--select", name] + refresh_args, expect_pass=False
        )
        assert len(failure.results) == 1
        assert "bad can't cast to INT in strict mode" in failure.results[0].message
        assert not any("insert overwrite table" in sql for sql in statements)
        self.assert_target(project, name, EXPECTED_ROWS, allow_stage=True)

        self.write_csv(project, name, "id,value\n1,ONE\n3,three\n")
        run_dbt(["seed", "--select", name] + refresh_args)
        self.assert_target(project, name, [(1, "ONE"), (3, "three")])

    def test_reserved_quoted_columns_and_null_bindings(self, project):
        name = "iceberg_seed_quoted"
        expected_rows = [(1, "O'Reilly, value"), (2, None)]
        columns = ("select", "value note")
        run_dbt(["seed", "--select", name])
        self.assert_target(project, name, expected_rows, columns=columns)

        _, statements = _run_and_capture_sql(name, ["seed", "--select", name])
        self.assert_one_overwrite(statements, name)
        self.assert_target(project, name, expected_rows, columns=columns)

    def test_multiple_binding_batches_are_published_with_one_overwrite(self, project):
        name = "iceberg_seed_batches"
        expected_rows = [(1, "one"), (2, "two"), (3, "three"), (4, "four"), (5, "five")]
        run_dbt(["seed", "--select", name])
        self.assert_target(project, name, expected_rows)

        _, statements = _run_and_capture_sql(name, ["seed", "--select", name])
        self.assert_one_overwrite(statements, name)
        stage_inserts = [
            sql for sql in statements
            if "insert into" in sql and name + "__dbt_tmp" in sql
        ]
        assert len(stage_inserts) == 3
        self.assert_target(project, name, expected_rows)

    def test_seed_recovery_restores_missing_target_before_failed_load(self, project):
        name = "iceberg_seed_recovery_missing"
        run_dbt(["seed", "--select", name])
        self.assert_target(project, name, EXPECTED_ROWS)
        relation = relation_from_name(project.adapter, name)
        project.run_sql(
            f"alter table {relation} rename `{relation.identifier}__dbt_backup`"
        )

        self.write_csv(project, name, "id,value\n3,three\n4,four\nbad,invalid\n")
        failure, statements = _run_and_capture_sql(
            name, ["seed", "--select", name], expect_pass=False
        )
        assert "bad can't cast to INT in strict mode" in failure.results[0].message
        assert not any("insert overwrite table" in sql for sql in statements)
        self.assert_target(project, name, EXPECTED_ROWS, allow_stage=True)

        self.write_csv(project, name, "id,value\n3,three\n4,four\n")
        run_dbt(["seed", "--select", name])
        self.assert_target(project, name, [(3, "three"), (4, "four")])

    def test_seed_recovery_conflict_preserves_target_and_backup(self, project):
        name = "iceberg_seed_recovery_conflict"
        run_dbt(["seed", "--select", name])
        self.assert_target(project, name, EXPECTED_ROWS)
        relation = relation_from_name(project.adapter, name)
        backup = relation.incorporate(
            path={"identifier": relation.identifier + "__dbt_backup"}
        )
        project.run_sql(
            f"create table {backup} as "
            "select cast(9 as int) as id, cast('saved backup' as string) as value"
        )

        self.write_csv(project, name, "id,value\n3,three\n4,four\n")
        failure, statements = _run_and_capture_sql(
            name, ["seed", "--select", name], expect_pass=False
        )
        message = failure.results[0].message.lower()
        assert "recovery backup" in message
        assert "already exists" in message
        assert not any(
            "insert into" in sql
            or "insert overwrite table" in sql
            or "create table" in sql
            or "drop table" in sql
            for sql in statements
        )
        assert project.run_sql(
            f"select id, value from {relation} order by id", fetch="all"
        ) == EXPECTED_ROWS
        assert project.run_sql(
            f"select id, value from {backup} order by id", fetch="all"
        ) == [(9, "saved backup")]

        # This backup belongs exclusively to the fixture. Resolve the conflict
        # explicitly, then prove the unchanged CSV can publish normally.
        project.run_sql(f"drop table {backup}")
        run_dbt(["seed", "--select", name])
        self.assert_target(project, name, [(3, "three"), (4, "four")])


class TestDorisIcebergSeedInferredTypes:
    @pytest.fixture(scope="class")
    def seeds(self):
        # Non-boolean numbers and text establish distinct inferred types.
        names = ["iceberg_seed_empty_inferred", "iceberg_seed_explicit_type_change"]
        names.extend(
            f"iceberg_seed_{case}_{engine}_inferred"
            for case in ("nulls", "digits", "refresh")
            for engine in ("default", "explicit")
        )
        return {name + ".csv": "id,value\n101,alpha\n202,beta\n" for name in names}

    @pytest.fixture(scope="class")
    def project_config_update(self):
        # Inference cases have no column_types, including global overrides.
        configs = {
            f"iceberg_seed_{case}_explicit_inferred": {"engine": "iceberg"}
            for case in ("nulls", "digits", "refresh")
        }
        configs["iceberg_seed_explicit_type_change"] = {
            "engine": "iceberg",
            "+column_types": {
                "id": "BIGINT",
                "value": "{{ var('seed_value_type', 'STRING') }}",
            },
        }
        return {"seeds": {"test": configs}}

    @pytest.mark.parametrize("engine", ["default", "explicit"])
    @pytest.mark.parametrize(
        "case,csv,expected_rows",
        [
            ("nulls", "id,value\n101,\n202,\n", [(101, None), (202, None)]),
            (
                "digits",
                "id,value\n101,123\n202,456\n",
                [(101, "123"), (202, "456")],
            ),
        ],
    )
    def test_nonempty_reload_keeps_inferred_target_types(
        self, project, engine, case, csv, expected_rows
    ):
        name = f"iceberg_seed_{case}_{engine}_inferred"
        results = run_dbt(["seed", "--select", name])
        assert results[0].node.config.column_types == {}
        TestDorisIcebergSeed.assert_target(project, name, [(101, "alpha"), (202, "beta")])
        relation = relation_from_name(project.adapter, name)
        columns_before = project.run_sql(f"describe {relation}", fetch="all")

        TestDorisIcebergSeed.write_csv(project, name, csv)
        for _ in range(2):
            _, statements = _run_and_capture_sql(name, ["seed", "--select", name])
            TestDorisIcebergSeed.assert_one_overwrite(statements, name)
            assert not any(" rename " in sql for sql in statements)
            assert project.run_sql(f"describe {relation}", fetch="all") == columns_before
            TestDorisIcebergSeed.assert_target(project, name, expected_rows)

    @pytest.mark.parametrize("engine", ["default", "explicit"])
    def test_full_refresh_reinfers_nonempty_csv_types(self, project, engine):
        name = f"iceberg_seed_refresh_{engine}_inferred"
        results = run_dbt(["seed", "--select", name])
        assert results[0].node.config.column_types == {}
        TestDorisIcebergSeed.assert_target(project, name, [(101, "alpha"), (202, "beta")])
        relation = relation_from_name(project.adapter, name)
        columns_before = project.run_sql(f"describe {relation}", fetch="all")

        TestDorisIcebergSeed.write_csv(project, name, "id,value\n101,123\n202,456\n")
        results = run_dbt(["seed", "--select", name, "--full-refresh"])
        assert results[0].node.config.column_types == {}
        columns_after = project.run_sql(f"describe {relation}", fetch="all")
        assert columns_after[1][1].upper() == "BIGINT"
        assert columns_after[1][1] != columns_before[1][1]
        TestDorisIcebergSeed.assert_target(project, name, [(101, 123), (202, 456)])

        _, statements = _run_and_capture_sql(name, ["seed", "--select", name])
        TestDorisIcebergSeed.assert_one_overwrite(statements, name)
        assert project.run_sql(f"describe {relation}", fetch="all") == columns_after
        TestDorisIcebergSeed.assert_target(project, name, [(101, 123), (202, 456)])

    def test_explicit_column_type_change_still_requires_full_refresh(self, project):
        name = "iceberg_seed_explicit_type_change"
        results = run_dbt(["seed", "--select", name])
        assert results[0].node.config.column_types == {"id": "BIGINT", "value": "STRING"}
        TestDorisIcebergSeed.assert_target(project, name, [(101, "alpha"), (202, "beta")])
        relation = relation_from_name(project.adapter, name)
        columns_before = project.run_sql(f"describe {relation}", fetch="all")
        TestDorisIcebergSeed.write_csv(project, name, "id,value\n101,123\n202,456\n")
        type_args = ["--vars", "{seed_value_type: BIGINT}"]

        failure, statements = _run_and_capture_sql(
            name, ["seed", "--select", name] + type_args, expect_pass=False
        )
        assert failure.results[0].node.config.column_types["value"] == "BIGINT"
        assert "schema changed" in failure.results[0].message.lower()
        assert "full-refresh" in failure.results[0].message.lower()
        assert not any("insert overwrite table" in sql for sql in statements)
        assert project.run_sql(f"describe {relation}", fetch="all") == columns_before
        TestDorisIcebergSeed.assert_target(project, name, [(101, "alpha"), (202, "beta")])

        run_dbt(["seed", "--select", name, "--full-refresh"] + type_args)
        columns_after = project.run_sql(f"describe {relation}", fetch="all")
        assert columns_after[1][1].upper() == "BIGINT"
        TestDorisIcebergSeed.assert_target(project, name, [(101, 123), (202, 456)])

        _, statements = _run_and_capture_sql(name, ["seed", "--select", name] + type_args)
        TestDorisIcebergSeed.assert_one_overwrite(statements, name)
        assert project.run_sql(f"describe {relation}", fetch="all") == columns_after
        TestDorisIcebergSeed.assert_target(project, name, [(101, 123), (202, 456)])

    def test_header_only_seed_keeps_inferred_schema_and_clears_rows(self, project):
        name = "iceberg_seed_empty_inferred"
        results = run_dbt(["seed", "--select", name])
        assert results[0].node.config.column_types == {}
        relation = relation_from_name(project.adapter, name)
        assert project.run_sql(
            f"select id, value from {relation} order by id", fetch="all"
        ) == [(101, "alpha"), (202, "beta")]
        columns_before = project.run_sql(f"describe {relation}", fetch="all")
        print("ICEBERG_SEED_INFERRED_SCHEMA_BEFORE=" + repr(columns_before))
        assert [column[0] for column in columns_before] == ["id", "value"]
        assert columns_before[0][1].upper() != "BOOLEAN"
        assert columns_before[1][1].upper() != "BOOLEAN"

        write_file("id,value\n", project.project_root, "seeds", name + ".csv")
        _, statements = _run_and_capture_sql(name, ["seed", "--select", name])
        TestDorisIcebergSeed.assert_one_overwrite(statements, name)
        assert project.run_sql(f"describe {relation}", fetch="all") == columns_before
        TestDorisIcebergSeed.assert_target(project, name, [])

        run_dbt(["seed", "--select", name])
        assert project.run_sql(f"describe {relation}", fetch="all") == columns_before
        TestDorisIcebergSeed.assert_target(project, name, [])


class TestDorisIcebergSeedInvalidEngine:
    @pytest.fixture(scope="class")
    def seeds(self):
        return {"iceberg_seed_invalid_engine.csv": "id,value\n1,first\n2,second\n"}

    @pytest.fixture(scope="class")
    def project_config_update(self):
        return {
            "seeds": {
                "+engine": "{{ var('seed_engine', 'iceberg') }}",
                "+column_types": {"id": "INT", "value": "STRING"},
            }
        }

    def test_invalid_olap_engine_full_refresh_preserves_existing_iceberg(self, project):
        name = "iceberg_seed_invalid_engine"
        run_dbt(["seed", "--select", name])
        TestDorisIcebergSeed.assert_target(project, name, EXPECTED_ROWS)

        failure = run_dbt(
            ["seed", "--select", name, "--full-refresh", "--vars", "{seed_engine: OLAP}"],
            expect_pass=False,
        )
        assert failure.results[0].node.config.get("engine").upper() == "OLAP"
        message = failure.results[0].message.lower()
        assert "olap" in message
        assert "catalog" in message
        TestDorisIcebergSeed.assert_target(project, name, EXPECTED_ROWS)

        run_dbt(["seed", "--select", name])
        TestDorisIcebergSeed.assert_target(project, name, EXPECTED_ROWS)
