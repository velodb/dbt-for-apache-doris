# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements. See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership. The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License. You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied. See the License for the
# specific language governing permissions and limitations
# under the License.

import pytest
from dbt.tests.util import relation_from_name, run_dbt

from test.functional.adapter.test_doris_iceberg_metadata import (  # noqa: F401
    dbt_profile_target, dbt_project_yml,
)
from test.functional.adapter.test_doris_iceberg_table import ICEBERG_CATALOG
from test.functional.adapter.test_doris_incremental import _run_and_capture_sql

pytestmark = pytest.mark.skipif(not ICEBERG_CATALOG, reason="Writable Iceberg Catalog required")

SOURCE = """
{% if var('batch', 1) == 1 %}
select cast(1 as int) id, cast('old' as string) value
union all select cast(2 as int), cast('retained' as string)
{% elif var('batch', 1) == 3 %}
select cast(1 as int) id, cast('duplicate1' as string) value
union all select cast(1 as int), cast('duplicate2' as string)
{% else %}
select cast(1 as int) id, cast('updated' as string) value
union all select cast(3 as int), cast('new' as string)
{% endif %}
"""


class TestDorisIcebergNativeStrategies:
    @pytest.fixture(scope="class")
    def models(self):
        result = {}
        for mode in ("default", "explicit"):
            append = ", incremental_strategy='append'" if mode == "explicit" else ""
            merge = ", incremental_strategy='merge'" if mode == "explicit" else ""
            result[f"iceberg_append_{mode}.sql"] = (
                "{{ config(materialized='incremental'" + append + ") }}\n" + SOURCE
            )
            result[f"iceberg_merge_{mode}.sql"] = (
                "{{ config(materialized='incremental', unique_key=['id'], "
                "properties={'format-version': '2'}" + merge + ") }}\n" + SOURCE
            )
        result["iceberg_merge_nullable.sql"] = (
            "{{ config(materialized='incremental', incremental_strategy='merge', "
            "unique_key=['id'], properties={'format-version': '2'}) }}\n"
            + SOURCE.replace("cast(1 as int)", "cast(null as int)")
        )
        result["iceberg_merge_schema.sql"] = (
            "{{ config(materialized='incremental', incremental_strategy='merge', "
            "unique_key=['id'], on_schema_change='sync_all_columns', "
            "properties={'format-version': '2'}) }}\n"
            "select cast(1 as {{ var('id_type', 'int') }}) as id, cast('value' as string) as value"
        )
        result["iceberg_merge_composite.sql"] = """
{{ config(materialized='incremental', incremental_strategy='merge',
          unique_key=['id','part'], on_schema_change='sync_all_columns',
          properties={'format-version':'2'}) }}
{% if var('batch', 1) == 1 %}
select cast(1 as int) id, cast('A' as string) part, cast('old A' as string) value
union all select cast(1 as int), cast('B' as string), cast('retained B' as string)
union all select cast(2 as int), cast(null as string), cast('old NULL' as string)
{% elif var('batch', 1) == 2 %}
select cast(1 as int) id, cast('A' as string) part, cast('new A' as string) value
union all select cast(1 as int), cast('C' as string), cast('new C' as string)
union all select cast(2 as int), cast(null as string), cast('new NULL' as string)
{% else %}
select cast(1 as int) id, cast('A' as string) part, cast('duplicate' as string) value, cast(42 as int) extra
union all select cast(1 as int), cast('A' as string), cast('duplicate' as string), cast(43 as int)
{% endif %}
"""
        result["iceberg_merge_v1.sql"] = (
            "{{ config(materialized='incremental', incremental_strategy='merge', "
            "unique_key=['id'], properties={'format-version': var('format', '1')}) }}\n" + SOURCE
        )
        return result

    @staticmethod
    def rows(project, name):
        relation = relation_from_name(project.adapter, name)
        return project.run_sql(f"select id,value from {relation} order by id,value", fetch="all")

    @staticmethod
    def assert_cleanup(project, name):
        relation = relation_from_name(project.adapter, name)
        names = {row[0] for row in project.run_sql(
            f"show tables from `{relation.database}`.`{relation.schema}`", fetch="all"
        )}
        assert name + "__dbt_tmp" not in names
        assert name + "__dbt_backup" not in names

    @pytest.mark.parametrize("mode", ["default", "explicit"])
    def test_append_first_and_repeated_batches_use_native_insert(self, project, mode):
        name = f"iceberg_append_{mode}"
        run_dbt(["run", "--select", name])
        _, statements = _run_and_capture_sql(
            name, ["run", "--select", name, "--vars", "{batch: 2}"]
        )
        assert self.rows(project, name) == [(1, "old"), (1, "updated"), (2, "retained"), (3, "new")]
        assert any("insert into" in sql for sql in statements)
        run_dbt(["run", "--select", name, "--vars", "{batch: 2}"])
        assert len(self.rows(project, name)) == 6
        self.assert_cleanup(project, name)

    @pytest.mark.parametrize("mode", ["default", "explicit"])
    def test_merge_upserts_idempotently_and_rejects_duplicate_batch(self, project, mode):
        name = f"iceberg_merge_{mode}"
        run_dbt(["run", "--select", name])
        for _ in range(2):
            _, statements = _run_and_capture_sql(
                name, ["run", "--select", name, "--vars", "{batch: 2}"]
            )
            assert any("merge into" in sql for sql in statements)
            assert self.rows(project, name) == [(1, "updated"), (2, "retained"), (3, "new")]
        failure = run_dbt(["run", "--select", name, "--vars", "{batch: 3}"], expect_pass=False)
        assert "duplicate unique_key" in failure.results[0].message
        assert self.rows(project, name) == [(1, "updated"), (2, "retained"), (3, "new")]
        run_dbt(["run", "--select", name, "--vars", "{batch: 2}"])
        self.assert_cleanup(project, name)

    def test_nullable_merge_key_matches_existing_null_once(self, project):
        name = "iceberg_merge_nullable"
        run_dbt(["run", "--select", name])
        for _ in range(2):
            run_dbt(["run", "--select", name, "--vars", "{batch: 2}"])
            assert self.rows(project, name) == [(None, "updated"), (2, "retained"), (3, "new")]
        self.assert_cleanup(project, name)

    def test_logical_key_type_can_promote_without_olap_schema_jobs(self, project):
        name = "iceberg_merge_schema"
        run_dbt(["run", "--select", name])
        _, statements = _run_and_capture_sql(
            name, ["run", "--select", name, "--vars", "{id_type: bigint}"]
        )
        assert not any("show alter table column" in sql for sql in statements)
        relation = relation_from_name(project.adapter, name)
        assert project.run_sql(f"describe {relation}", fetch="all")[0][1].lower() == "bigint"
        assert self.rows(project, name) == [(1, "value")]
        self.assert_cleanup(project, name)

    def test_composite_nullable_keys_and_duplicate_batch_before_schema_changes(self, project):
        name = "iceberg_merge_composite"
        run_dbt(["run", "--select", name])
        relation = relation_from_name(project.adapter, name)
        query = f"select id,part,value from {relation} order by id,part"
        expected = [(1, "A", "new A"), (1, "B", "retained B"), (1, "C", "new C"), (2, None, "new NULL")]
        for _ in range(2):
            run_dbt(["run", "--select", name, "--vars", "{batch: 2}"])
            assert project.run_sql(query, fetch="all") == expected
        schema = project.run_sql(f"describe {relation}", fetch="all")
        failure, statements = _run_and_capture_sql(
            name, ["run", "--select", name, "--vars", "{batch: 3}"], expect_pass=False
        )
        assert "duplicate unique_key" in failure.results[0].message
        assert not any("alter table" in sql or "merge into" in sql for sql in statements)
        assert project.run_sql(f"describe {relation}", fetch="all") == schema
        assert project.run_sql(query, fetch="all") == expected
        self.assert_cleanup(project, name)

    def test_v1_merge_reports_server_prerequisite_and_preserves_old_rows(self, project):
        name = "iceberg_merge_v1"
        run_dbt(["run", "--select", name])
        relation = relation_from_name(project.adapter, name)
        schema = project.run_sql(f"describe {relation}", fetch="all")
        failure = run_dbt(["run", "--select", name, "--vars", "{batch: 2}"], expect_pass=False)
        assert "format version 2 or higher" in failure.results[0].message
        assert self.rows(project, name) == [(1, "old"), (2, "retained")]
        assert project.run_sql(f"describe {relation}", fetch="all") == schema
        run_dbt(["run", "--select", name, "--full-refresh", "--vars", "{batch: 2, format: '2'}"])
        assert self.rows(project, name) == [(1, "updated"), (3, "new")]
        run_dbt(["run", "--select", name, "--vars", "{batch: 2, format: '2'}"])
        assert self.rows(project, name) == [(1, "updated"), (3, "new")]
        self.assert_cleanup(project, name)
