# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements. See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership. The ASF licenses this file
# to you under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance
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

from test.functional.adapter.test_doris_iceberg_metadata import dbt_project_yml  # noqa: F401
from test.functional.adapter.test_doris_iceberg_table import ICEBERG_CATALOG
from test.functional.adapter.test_doris_incremental import _run_and_capture_sql


class TestDorisUnsupportedConstraints:
    @pytest.fixture(scope="class", params=[None, pytest.param(
        ICEBERG_CATALOG, marks=pytest.mark.skipif(not ICEBERG_CATALOG, reason="Writable Iceberg Catalog required")
    )])
    def dbt_profile_target(self, dbt_profile_target, request):
        if request.param is not None:
            return {**dbt_profile_target, "database": request.param}
        return dbt_profile_target

    @pytest.fixture(scope="class")
    def models(self):
        return {"required_model.sql": """
{{ config(materialized=var('kind','table'),contract={'enforced':true},
          incremental_strategy='append',on_schema_change='append_new_columns') }}
{% if not target.database or target.database|lower == 'internal' %}{{ config(replication_num=1) }}{% endif %}
{% if var('bad',false) %}
{{ config(pre_hook="insert into " ~ this ~ " values(999,'hook')",
          sql_header="insert into " ~ this ~ " values(888,'header')") }}
select cast(null as int) id,cast('bad NULL' as string) value
{% else %}
select cast(1 as int) id,cast('old' as string) value
{% endif %}
"""}

    @pytest.mark.parametrize("kind", ["table", "incremental"])
    @pytest.mark.parametrize("location", ["column", "model"])
    def test_constraints_rejected_before_hooks_ddl_or_dml(self, project, kind, location):
        name = "required_model"
        model = {"name": name, "columns": [{"name": "id", "data_type": "int"},
                                           {"name": "value", "data_type": "string"}]}
        write_file(yaml.safe_dump({"version": 2, "models": [model]}), project.project_root, "models", "schema.yml")
        run_dbt(["run", "--select", name, "--full-refresh", "--vars", "{kind: " + kind + "}"])
        relation = relation_from_name(project.adapter, name)
        ddl = project.run_sql(f"show create table {relation}", fetch="one")[1]
        if location == "column":
            model["columns"][0]["constraints"] = [{"type": "not_null"}]
        else:
            model["constraints"] = [{"type": "primary_key", "columns": ["id"]}]
        write_file(yaml.safe_dump({"version": 2, "models": [model]}), project.project_root, "models", "schema.yml")
        failure, statements = _run_and_capture_sql(
            name, ["run", "--select", name, "--vars", "{kind: " + kind + ", bad: true}"], expect_pass=False
        )
        assert "does not enforce database constraints" in failure.results[0].message
        assert not any(sql.startswith(("insert", "create", "drop", "alter", "merge")) for sql in statements)
        assert project.run_sql(f"select * from {relation}", fetch="all") == [(1, "old")]
        assert project.run_sql(f"show create table {relation}", fetch="one")[1] == ddl
