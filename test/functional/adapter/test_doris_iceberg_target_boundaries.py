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


class TestDorisIcebergUnsupportedTargets:
    @pytest.fixture(scope="class")
    def models(self):
        return {"iceberg_mv_boundary.sql": """
{{ config(materialized=var('kind','table')) }}
select cast(101 as int) id
"""}

    @pytest.fixture(scope="class")
    def tests(self):
        return {"iceberg_audit.sql": """
{{ config(store_failures=true, store_failures_as=var('audit_kind','table'),
          alias='iceberg_audit_rows', severity='warn') }}
select cast(101 as int) id
"""}

    @pytest.fixture(scope="class")
    def macros(self):
        return {"audit_schema.sql": """
{% macro generate_schema_name(custom_schema_name, node) %}
{{ return(target.schema) }}
{% endmacro %}
"""}

    def test_external_mv_rejection_preserves_existing_table(self, project):
        name = "iceberg_mv_boundary"
        run_dbt(["run", "--select", name])
        failure, sql = _run_and_capture_sql(
            name, ["run", "--select", name, "--vars", "{kind: materialized_view}"], expect_pass=False
        )
        assert "Materialized View in external Catalog" in failure.results[0].message
        assert not any("drop table" in query or "rename" in query for query in sql)
        relation = relation_from_name(project.adapter, name)
        assert project.run_sql(f"select id from {relation}", fetch="all") == [(101,)]

    def test_external_audit_view_rejection_preserves_old_audit_rows(self, project):
        run_dbt(["test", "--select", "iceberg_audit"])
        relation = relation_from_name(project.adapter, "iceberg_audit_rows")
        assert project.run_sql(f"select id from {relation}", fetch="all") == [(101,)]
        failure = run_dbt(["test", "--select", "iceberg_audit", "--vars", "{audit_kind: view}"], expect_pass=False)
        assert "View in external Catalog" in failure.results[0].message
        assert project.run_sql(f"select id from {relation}", fetch="all") == [(101,)]
