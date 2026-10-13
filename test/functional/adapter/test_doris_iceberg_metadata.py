#!/usr/bin/env python
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

from test.functional.adapter.test_doris_grants import DorisGrantUsers
from test.functional.adapter.test_doris_iceberg_table import ICEBERG_CATALOG
from test.functional.adapter.test_doris_incremental import _run_and_capture_sql

pytestmark = pytest.mark.skipif(
    not ICEBERG_CATALOG,
    reason="DBT_DORIS_ICEBERG_CATALOG must name a writable Iceberg catalog",
)


@pytest.fixture(scope="class")
def dbt_profile_target(dbt_profile_target):
    return {**dbt_profile_target, "database": ICEBERG_CATALOG}


@pytest.fixture(scope="class")
def dbt_project_yml(project_root, project_config_update):
    project = {"name": "test", "profile": "test",
               "flags": {"send_anonymous_usage_stats": False}}
    project.update(project_config_update)
    write_file(yaml.safe_dump(project), project_root, "dbt_project.yml")
    return project


class TestDorisIcebergCatalogQuoting:
    @pytest.fixture(scope="class")
    def models(self):
        return {"iceberg_catalog_quoting.sql": (
            "{{ config(materialized='table') }}\nselect cast(101 as int) as id"
        )}

    def test_default_catalog_quoting_and_repeated_discovery(self, project):
        name = "iceberg_catalog_quoting"
        for _ in range(2):
            run_dbt(["run", "--select", name])
            relation = relation_from_name(project.adapter, name)
            assert project.run_sql(f"select id from {relation}", fetch="all") == [(101,)]


class TestDorisIcebergColumnDocs:
    @pytest.fixture(scope="class")
    def project_config_update(self):
        # The quoting regression is isolated above for the before-fix run.
        return {"quoting": {"database": False}}

    @pytest.fixture(scope="class")
    def models(self):
        return {
            "iceberg_column_docs.sql": """
{{ config(materialized='incremental', incremental_strategy='insert_overwrite',
          persist_docs={'columns': true}) }}
select cast(101 as int) as id, cast('alpha' as string) as value
""",
            "schema.yml": yaml.safe_dump({"version": 2, "models": [{
                "name": "iceberg_column_docs", "columns": [
                    {"name": "id", "description": "Old identifier comment"},
                    {"name": "value", "description": "Old value comment"},
                ],
            }]}),
        }

    def test_column_comments_change_without_column_attribute_changes(self, project):
        name = "iceberg_column_docs"
        run_dbt(["run", "--select", name])
        relation = relation_from_name(project.adapter, name)
        query = ("select column_name,column_type,is_nullable,column_default,column_comment "
                 f"from `{relation.database}`.information_schema.columns "
                 f"where table_schema='{relation.schema}' and table_name='{relation.identifier}' "
                 "order by ordinal_position")
        before = project.run_sql(query, fetch="all")
        schema = {"version": 2, "models": [{"name": name, "columns": [
            {"name": "id", "description": "New identifier comment"},
            {"name": "value", "description": "New value comment"},
        ]}]}
        write_file(yaml.safe_dump(schema), project.project_root, "models", "schema.yml")
        _, statements = _run_and_capture_sql(name, ["run", "--select", name])
        after = project.run_sql(query, fetch="all")
        assert [row[:-1] for row in before] == [row[:-1] for row in after]
        assert [row[-1] for row in after] == ["New identifier comment", "New value comment"]
        assert project.run_sql(f"select id,value from {relation}", fetch="all") == [(101, "alpha")]
        assert any("modify column" in sql for sql in statements)
        _, repeated = _run_and_capture_sql(name, ["run", "--select", name])
        assert not any("modify column" in sql and "comment" in sql for sql in repeated)


class TestDorisIcebergCatalogGrants(DorisGrantUsers):
    @pytest.fixture(scope="class")
    def project_config_update(self):
        return {"quoting": {"database": False}}

    @pytest.fixture(scope="class")
    def models(self):
        return {"iceberg_catalog_grants.sql": """
{{ config(materialized='table', grants={'select': [env_var('DBT_TEST_USER_1')]}) }}
select cast(101 as int) as id
"""}

    def test_grant_rotation_only_changes_the_selected_catalog(self, project, get_test_users):
        name = "iceberg_catalog_grants"
        relation = relation_from_name(project.adapter, name)
        internal = relation.incorporate(path={"database": "internal"})
        existing = {row[0] for row in project.run_sql("show databases from internal", fetch="all")}
        assert relation.schema not in existing
        project.run_sql(f"create database `internal`.`{relation.schema}`")
        try:
            project.run_sql(f"create table {internal} (id int) distributed by random buckets 1 "
                            "properties('replication_num'='1')")
            user1, user2 = get_test_users[:2]
            project.run_sql(f"grant SELECT_PRIV on {internal} to '{user1}'@'%'")
            run_dbt(["run", "--select", name])
            first = project.run_sql(f"show grants for '{user1}'@'%'", fetch="all")
            assert f"{ICEBERG_CATALOG}.{relation.schema}.{name}: Select_priv" in str(first)
            assert f"internal.{relation.schema}.{name}: Select_priv" in str(first)
            model = """
{{ config(materialized='table', grants={'select': [env_var('DBT_TEST_USER_2')]}) }}
select cast(101 as int) as id
"""
            write_file(model, project.project_root, "models", name + ".sql")
            _, statements = _run_and_capture_sql(name, ["run", "--select", name])
            first_after = project.run_sql(f"show grants for '{user1}'@'%'", fetch="all")
            second = project.run_sql(f"show grants for '{user2}'@'%'", fetch="all")
            assert f"{ICEBERG_CATALOG}.{relation.schema}.{name}: Select_priv" not in str(first_after)
            assert f"internal.{relation.schema}.{name}: Select_priv" in str(first_after)
            assert f"{ICEBERG_CATALOG}.{relation.schema}.{name}: Select_priv" in str(second)
            assert any("revoke select_priv" in sql and ICEBERG_CATALOG in sql for sql in statements)
            assert project.run_sql(f"select id from {relation}", fetch="all") == [(101,)]
        finally:
            project.run_sql(f"drop database if exists `internal`.`{relation.schema}`")
