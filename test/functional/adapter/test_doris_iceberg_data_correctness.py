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
import yaml
from dbt.tests.util import relation_from_name, run_dbt, write_file

from test.functional.adapter.test_doris_iceberg_metadata import (  # noqa: F401
    dbt_profile_target, dbt_project_yml,
)
from test.functional.adapter.test_doris_iceberg_table import ICEBERG_CATALOG
from test.functional.adapter.test_doris_incremental import _run_and_capture_sql

pytestmark = pytest.mark.skipif(not ICEBERG_CATALOG, reason="Writable Iceberg Catalog required")

INITIAL_CSV = "id,dt,value\n1,2025-01-01 12:00:00,old A\n,2025-01-02 12:00:00,old B\n"
NEW_CSV = "id,dt,value\n3,2025-01-01 13:00:00,new A\n"


class TestDorisIcebergPartitionedSeedReplacement:
    @pytest.fixture(scope="class")
    def macros(self):
        return {"seed_batches.sql": "{% macro get_batch_size() %}{{ return(2) }}{% endmacro %}"}

    @pytest.fixture(scope="class")
    def seeds(self):
        return {name + ".csv": INITIAL_CSV for name in ("seed_identity", "seed_transform", "seed_v1")}

    @pytest.fixture(scope="class")
    def project_config_update(self):
        return {"seeds": {
            "+column_types": {"id": "int", "dt": "datetime", "value": "string"},
            "+partition_type": "LIST", "+properties": {"format-version": "2"},
            "+pre-hook": "set enable_strict_cast=true",
            "test": {
                "seed_identity": {"+partition_by": ["dt"]},
                "seed_transform": {"+partition_by": ["day(dt)"]},
                "seed_v1": {"+partition_by": ["dt"], "+properties": {"format-version": "1"}},
            },
        }}

    @pytest.mark.parametrize("name", ["seed_identity", "seed_transform"])
    def test_partitioned_seed_replaces_complete_csv_and_empty_input(self, project, name):
        run_dbt(["seed", "--select", name])
        relation = relation_from_name(project.adapter, name)
        ddl = project.run_sql(f"show create table {relation}", fetch="one")[1]
        assert "PARTITION BY LIST" in ddl
        write_file(NEW_CSV, project.project_root, "seeds", name + ".csv")
        for _ in range(2):
            _, statements = _run_and_capture_sql(name, ["seed", "--select", name])
            assert project.run_sql(f"select id,value from {relation} order by id", fetch="all") == [(3, "new A")]
            assert any("merge into" in sql for sql in statements)
            assert not any("alter table" in sql and "rename" in sql for sql in statements)
            assert project.run_sql(f"show create table {relation}", fetch="one")[1] == ddl
        write_file("id,dt,value\n", project.project_root, "seeds", name + ".csv")
        run_dbt(["seed", "--select", name])
        assert project.run_sql(f"select * from {relation}", fetch="all") == []
        write_file(NEW_CSV, project.project_root, "seeds", name + ".csv")
        run_dbt(["seed", "--select", name])
        assert project.run_sql(f"select id,value from {relation}", fetch="all") == [(3, "new A")]

    def test_existing_partition_layout_is_detected_when_config_omits_it(self, project):
        name = "seed_identity"
        write_file(INITIAL_CSV, project.project_root, "seeds", name + ".csv")
        run_dbt(["seed", "--select", name, "--full-refresh"])
        relation = relation_from_name(project.adapter, name)
        ddl = project.run_sql(f"show create table {relation}", fetch="one")[1]
        config_path = project.project_root / "dbt_project.yml"
        original = config_path.read()
        config = yaml.safe_load(original)
        config["seeds"]["test"][name].pop("+partition_by")
        try:
            write_file(yaml.safe_dump(config), project.project_root, "dbt_project.yml")
            write_file(NEW_CSV, project.project_root, "seeds", name + ".csv")
            _, statements = _run_and_capture_sql(name, ["seed", "--select", name])
            assert any("merge into" in sql for sql in statements)
            assert project.run_sql(f"select id,value from {relation}", fetch="all") == [(3, "new A")]
            assert project.run_sql(f"show create table {relation}", fetch="one")[1] == ddl
        finally:
            write_file(original, project.project_root, "dbt_project.yml")

    def test_failed_later_csv_batch_keeps_target_and_retry_publishes_once(self, project):
        name = "seed_identity"
        write_file(INITIAL_CSV, project.project_root, "seeds", name + ".csv")
        run_dbt(["seed", "--select", name, "--full-refresh"])
        relation = relation_from_name(project.adapter, name)
        before = project.run_sql(f"select * from {relation} order by value", fetch="all")
        csv = ("id,dt,value\n3,2025-01-01 13:00:00,new A\n4,2025-01-01 14:00:00,new B\n"
               "invalid,2025-01-01 15:00:00,bad\n")
        write_file(csv, project.project_root, "seeds", name + ".csv")
        _, statements = _run_and_capture_sql(name, ["seed", "--select", name], expect_pass=False)
        assert not any("merge into" in sql for sql in statements)
        assert sum("insert into" in sql and name + "__dbt_tmp" in sql for sql in statements) == 2
        assert project.run_sql(f"select * from {relation} order by value", fetch="all") == before
        write_file(NEW_CSV, project.project_root, "seeds", name + ".csv")
        _, statements = _run_and_capture_sql(name, ["seed", "--select", name])
        assert sum("merge into" in sql for sql in statements) == 1
        assert project.run_sql(f"select id,value from {relation}", fetch="all") == [(3, "new A")]

    def test_v1_partitioned_reload_fails_without_changing_target_and_full_refresh_succeeds(self, project):
        name = "seed_v1"
        run_dbt(["seed", "--select", name])
        relation = relation_from_name(project.adapter, name)
        before = project.run_sql(f"select * from {relation} order by value", fetch="all")
        write_file(NEW_CSV, project.project_root, "seeds", name + ".csv")
        failure = run_dbt(["seed", "--select", name], expect_pass=False)
        assert "format version 2 or higher" in failure.results[0].message
        assert project.run_sql(f"select * from {relation} order by value", fetch="all") == before
        run_dbt(["seed", "--select", name, "--full-refresh"])
        assert project.run_sql(f"select id,value from {relation}", fetch="all") == [(3, "new A")]


class TestDorisIcebergNullPartitionReplacement:
    @pytest.fixture(scope="class")
    def models(self):
        return {"null_scope.sql": """
{{ config(materialized='incremental',incremental_strategy='insert_overwrite',
          partition_type='LIST',partition_by=['dt','region'],
          overwrite_partitions=var('scope',none),properties={'format-version':var('format','2')}) }}
{% if var('batch',1)==1 %}
select cast(1 as int) id,cast(null as string) dt,cast('A' as string) region,cast('old NULL A' as string) value
union all select cast(2 as int),cast('other' as string),cast('A' as string),cast('outside' as string)
union all select cast(5 as int),cast(null as string),cast('B' as string),cast('old NULL B' as string)
{% else %}
select cast(3 as int) id,cast(null as string) dt,cast('A' as string) region,cast('new NULL A' as string) value
where {{ 'false' if var('empty',false) else 'true' }}
{% if not var('only_a',false) %}
union all select cast(4 as int),cast(null as string),cast('B' as string),cast('new NULL B' as string)
where {{ 'false' if var('empty',false) else 'true' }}
{% endif %}
{% endif %}
"""}

    @pytest.mark.parametrize("only_a", [False, True])
    def test_null_static_scope_replaces_old_rows_and_clears_empty_scope(self, project, only_a):
        name = "null_scope"
        run_dbt(["run", "--select", name, "--full-refresh"])
        relation = relation_from_name(project.adapter, name)
        scope = {"dt": None, **({"region": "A"} if only_a else {})}
        values = {"batch": 2, "scope": scope, "only_a": only_a}
        expected = [(2, "outside"), (3, "new NULL A"), (5, "old NULL B")] if only_a else [
            (2, "outside"), (3, "new NULL A"), (4, "new NULL B"),
        ]
        for _ in range(2):
            _, statements = _run_and_capture_sql(
                name, ["run", "--select", name, "--vars", yaml.safe_dump(values)]
            )
            assert project.run_sql(f"select id,value from {relation} order by id", fetch="all") == expected
            assert any("merge into" in sql for sql in statements)
            assert not any("insert overwrite" in sql for sql in statements)
        values["empty"] = True
        run_dbt(["run", "--select", name, "--vars", yaml.safe_dump(values)])
        expected = [(2, "outside"), (5, "old NULL B")] if only_a else [(2, "outside")]
        assert project.run_sql(f"select id,value from {relation} order by id", fetch="all") == expected

    def test_v1_null_scope_failure_preserves_target_before_v2_rebuild(self, project):
        name = "null_scope"
        run_dbt(["run", "--select", name, "--full-refresh", "--vars", "{format: '1'}"])
        relation = relation_from_name(project.adapter, name)
        before = project.run_sql(f"select * from {relation} order by id", fetch="all")
        values = {"format": "1", "batch": 2, "scope": {"dt": None}}
        failure = run_dbt(["run", "--select", name, "--vars", yaml.safe_dump(values)], expect_pass=False)
        assert "format version 2 or higher" in failure.results[0].message
        assert project.run_sql(f"select * from {relation} order by id", fetch="all") == before
        run_dbt(["run", "--select", name, "--full-refresh", "--vars", "{format: '2'}"])
        values["format"] = "2"
        run_dbt(["run", "--select", name, "--vars", yaml.safe_dump(values)])
        assert project.run_sql(f"select id from {relation} order by id", fetch="all") == [(2,), (3,), (4,)]
