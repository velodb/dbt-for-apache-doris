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


class TestDorisIcebergStaticPartitions:
    @pytest.fixture(scope="class")
    def models(self):
        return {"iceberg_static_scope.sql": """
{{ config(materialized='incremental', incremental_strategy='insert_overwrite',
          partition_type='LIST', partition_by=['dt'], overwrite_partitions=var('scope', none)) }}
{% if var('batch', 1) == 1 %}
select cast(1 as int) id, cast('old A' as string) value, cast('A' as string) dt
union all select cast(2 as int), cast('retained B' as string), cast('B' as string)
{% else %}
select cast(3 as int) id, cast('new A' as string) value, cast('A' as string) dt
where {{ 'false' if var('empty', false) else 'true' }}
{% endif %}
"""}

    def test_static_scope_replaces_and_clears_only_selected_partition(self, project):
        name = "iceberg_static_scope"
        run_dbt(["run", "--select", name])
        relation = relation_from_name(project.adapter, name)
        _, statements = _run_and_capture_sql(
            name, ["run", "--select", name, "--vars", "{batch: 2, scope: {dt: A}}"]
        )
        assert project.run_sql(f"select * from {relation} order by id", fetch="all") == [
            (2, "retained B", "B"), (3, "new A", "A"),
        ]
        assert any("partition(" in sql.replace(" ", "") and "'a'" in sql for sql in statements)
        run_dbt(["run", "--select", name, "--vars", "{batch: 2, empty: true, scope: {dt: A}}"])
        assert project.run_sql(f"select * from {relation} order by id", fetch="all") == [
            (2, "retained B", "B"),
        ]

    def test_rows_outside_static_scope_fail_before_target_write(self, project):
        name = "iceberg_static_scope"
        run_dbt(["run", "--select", name, "--full-refresh"])
        relation = relation_from_name(project.adapter, name)
        failure, statements = _run_and_capture_sql(
            name, ["run", "--select", name, "--vars", "{batch: 1, scope: {dt: A}}"], expect_pass=False
        )
        assert "outside overwrite_partitions" in failure.results[0].message
        assert not any("insert overwrite" in sql for sql in statements)
        assert project.run_sql(f"select * from {relation} order by id", fetch="all") == [
            (1, "old A", "A"), (2, "retained B", "B"),
        ]
