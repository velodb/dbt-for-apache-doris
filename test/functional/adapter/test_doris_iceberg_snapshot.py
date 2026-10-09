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

SNAPSHOT = """
{% snapshot NAME %}
{{ config(target_schema=target.schema, unique_key='id', strategy='check',
          check_cols=['value'], invalidate_hard_deletes=True, properties={'format-version':'2'} CONFIG) }}
{% if var('fail_source', false) %}
select missing_snapshot_column from numbers('number'='1')
{% elif var('batch', 1) == 1 %}
select cast(1 as int) id, cast('old' as string) value
union all select cast(2 as int), cast('retained' as string)
{% elif var('batch', 1) == 2 %}
select cast(1 as int) id, cast('updated' as string) value
union all select cast(3 as int), cast('new' as string)
{% else %}
select cast(1 as int) id, cast('old' as string) value where false
{% endif %}
{% endsnapshot %}
"""


class TestDorisIcebergSnapshot:
    @pytest.fixture(scope="class")
    def snapshots(self):
        result = {}
        for suffix in ("history", "partition", "recovery", "new_column", "backup_conflict"):
            name = "iceberg_snapshot_" + suffix
            partition = ", partition_type='LIST', partition_by=['id']" if suffix == "partition" else ""
            result[name + ".sql"] = SNAPSHOT.replace("NAME", name).replace(" CONFIG", partition)
        result["iceberg_snapshot_new_column.sql"] = result["iceberg_snapshot_new_column.sql"].replace(
            "{% if var('fail_source', false) %}",
            "{% if var('new_column', false) %}\n"
            "select cast(1 as int) id, cast('new schema' as string) value, cast(42 as int) extra\n"
            "{% elif var('fail_source', false) %}",
        )
        return result

    @staticmethod
    def rows(project, name):
        relation = relation_from_name(project.adapter, name)
        return project.run_sql(f"select id,value,dbt_valid_to is null from {relation} order by id,value",
                               fetch="all")

    @pytest.mark.parametrize("name", ["iceberg_snapshot_history", "iceberg_snapshot_partition"])
    def test_repeated_snapshot_preserves_history_and_layout(self, project, name):
        run_dbt(["snapshot", "--select", name])
        for _ in range(2):
            _, statements = _run_and_capture_sql(
                name, ["snapshot", "--select", name, "--vars", "{batch: 2}"]
            )
            assert not any(" like " in sql and "create table" in sql for sql in statements)
            assert self.rows(project, name) == [
                (1, "old", 0), (1, "updated", 1), (2, "retained", 0), (3, "new", 1),
            ]
        run_dbt(["snapshot", "--select", name, "--vars", "{batch: 3}"])
        assert self.rows(project, name) == [
            (1, "old", 0), (1, "updated", 0), (2, "retained", 0), (3, "new", 0),
        ]
        relation = relation_from_name(project.adapter, name)
        if name.endswith("partition"):
            assert "PARTITION BY LIST" in project.run_sql(
                f"show create table {relation}", fetch="one"
            )[1]

    def test_snapshot_new_column_does_not_poll_olap_jobs(self, project):
        name = "iceberg_snapshot_new_column"
        run_dbt(["snapshot", "--select", name])
        _, statements = _run_and_capture_sql(
            name, ["snapshot", "--select", name, "--vars", "{new_column: true}"]
        )
        assert not any("show alter table column" in sql for sql in statements)
        relation = relation_from_name(project.adapter, name)
        current = project.run_sql(f"select id,value,extra from {relation} "
                                  "where dbt_valid_to is null order by id", fetch="all")
        assert current == [(1, "new schema", 42)]

    def test_snapshot_restore_backup_before_failed_retry(self, project):
        name = "iceberg_snapshot_recovery"
        run_dbt(["snapshot", "--select", name])
        relation = relation_from_name(project.adapter, name)
        project.run_sql(f"alter table {relation} rename `{name}__dbt_backup`")
        failure = run_dbt(["snapshot", "--select", name, "--vars", "{fail_source: true}"],
                          expect_pass=False)
        assert "missing_snapshot_column" in failure.results[0].message
        assert self.rows(project, name) == [(1, "old", 1), (2, "retained", 1)]
        run_dbt(["snapshot", "--select", name, "--vars", "{batch: 2}"])
        assert self.rows(project, name) == [
            (1, "old", 0), (1, "updated", 1), (2, "retained", 0), (3, "new", 1),
        ]

    def test_snapshot_existing_backup_is_rejected_without_deleting_either_copy(self, project):
        name = "iceberg_snapshot_backup_conflict"
        run_dbt(["snapshot", "--select", name])
        relation = relation_from_name(project.adapter, name)
        backup = relation.incorporate(path={"identifier": name + "__dbt_backup"})
        project.run_sql(f"create table {backup} as select * from {relation}")
        before = self.rows(project, name)
        failure, statements = _run_and_capture_sql(
            name, ["snapshot", "--select", name, "--vars", "{batch: 2}"], expect_pass=False
        )
        assert "Inspect the target and backup" in failure.results[0].message
        assert not any("drop table" in sql or "rename" in sql for sql in statements)
        assert self.rows(project, name) == before
        assert self.rows(project, name + "__dbt_backup") == before
