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
from dbt.tests.util import patch_microbatch_end_time, relation_from_name, run_dbt, write_file

from test.functional.adapter.test_doris_iceberg_metadata import (  # noqa: F401
    dbt_profile_target, dbt_project_yml,
)
from test.functional.adapter.test_doris_iceberg_table import ICEBERG_CATALOG
from test.functional.adapter.test_doris_incremental import _run_and_capture_sql

pytestmark = pytest.mark.skipif(not ICEBERG_CATALOG, reason="Writable Iceberg Catalog required")


class TestDorisIcebergMicrobatch:
    @pytest.fixture(scope="class")
    def models(self):
        models = {"iceberg_micro_input.sql": """
{{ config(materialized='table', event_time='event_time', properties={'format-version':'2'}) }}
select cast(1 as int) id, cast('2025-01-01 12:00:00' as datetime) event_time, cast('first' as string) value
union all select cast(2 as int), cast('2025-01-02 12:00:00' as datetime), cast('second' as string)
union all select cast(3 as int), cast('2025-01-03 12:00:00' as datetime), cast('third' as string)
"""}
        for mode, partition in (("plain", ""), ("partitioned", ", partition_type='LIST', partition_by=['day(event_time)']")):
            models[f"iceberg_micro_{mode}.sql"] = (
                "{{ config(materialized='incremental', incremental_strategy='microbatch', "
                "event_time='event_time', begin='2025-01-01T00:00:00+00:00', batch_size='day', "
                "lookback=3, properties={'format-version':'2'}" + partition + ") }}\n"
                "select id,event_time,value from {{ ref('iceberg_micro_input') }}"
            )
        return models

    @staticmethod
    def rows(project, relation):
        return project.run_sql(f"select id,value from {relation} order by id", fetch="all")

    @staticmethod
    def assert_cleanup(project, relation):
        names = {row[0] for row in project.run_sql(
            f"show tables from `{relation.database}`.`{relation.schema}`", fetch="all"
        )}
        for suffix in ("__dbt_tmp", "__dbt_backup", "__dbt_intermediate"):
            assert relation.identifier + suffix not in names

    @pytest.mark.parametrize("mode", ["plain", "partitioned"])
    def test_iceberg_microbatch_replaces_window_including_empty_window(self, project, mode):
        name = f"iceberg_micro_{mode}"
        with patch_microbatch_end_time("2025-01-04 00:00:00"):
            run_dbt(["run", "--select", "+" + name])
        relation = relation_from_name(project.adapter, name)
        source = relation_from_name(project.adapter, "iceberg_micro_input")
        assert project.run_sql(f"select id,value from {relation} order by id", fetch="all") == [
            (1, "first"), (2, "second"), (3, "third"),
        ]
        project.run_sql(f"delete from {source} where id=3")
        project.run_sql(f"update {source} set value='changed' where id=2")
        for _ in range(2):
            with patch_microbatch_end_time("2025-01-04 00:00:00"):
                _, statements = _run_and_capture_sql(name, ["run", "--select", name])
            assert project.run_sql(f"select id,value from {relation} order by id", fetch="all") == [
                (1, "first"), (2, "changed"),
            ]
            assert any("merge into" in sql for sql in statements)
            assert not any("add partition" in sql for sql in statements)
        with patch_microbatch_end_time("2025-01-04 00:00:00"):
            _, statements = _run_and_capture_sql(name, ["run", "--select", name, "--full-refresh"])
        assert self.rows(project, relation) == [(1, "first"), (2, "changed")]
        assert any("merge into" in sql for sql in statements)
        self.assert_cleanup(project, relation)

    @pytest.mark.parametrize("invalid_time", ["null", "cast('2025-01-05' as datetime)"])
    def test_invalid_window_source_fails_before_target_dml(self, project, models, capsys, invalid_time):
        name = "iceberg_micro_plain"
        with patch_microbatch_end_time("2025-01-02 00:00:00"):
            run_dbt(["run", "--select", "+" + name, "--full-refresh"])
        relation = relation_from_name(project.adapter, name)
        before = self.rows(project, relation)
        try:
            write_file(models[name + ".sql"] + f"\nunion all select 999, {invalid_time}, 'invalid'",
                       project.project_root, "models", name + ".sql")
            with patch_microbatch_end_time("2025-01-02 00:00:00"):
                failure, statements = _run_and_capture_sql(name, ["run", "--select", name], expect_pass=False)
            assert failure.results[0].batch_results.failed
            assert "Iceberg microbatch source contains rows outside model.batch" in capsys.readouterr().out
            assert not any("merge into" in sql or "insert overwrite" in sql for sql in statements)
            assert self.rows(project, relation) == before
            self.assert_cleanup(project, relation)
        finally:
            write_file(models[name + ".sql"], project.project_root, "models", name + ".sql")

    def test_microbatch_restore_backup_retains_untouched_windows_through_failed_retry(self, project, models):
        name = "iceberg_micro_plain"
        with patch_microbatch_end_time("2025-01-04 00:00:00"):
            run_dbt(["run", "--select", "+" + name, "--full-refresh"])
        relation = relation_from_name(project.adapter, name)
        backup_name = name + "__dbt_backup"
        project.run_sql(f"alter table {relation} rename `{backup_name}`")
        try:
            write_file(models[name + ".sql"] + " where missing_microbatch_column=1",
                       project.project_root, "models", name + ".sql")
            with patch_microbatch_end_time("2025-01-02 00:00:00"):
                run_dbt(["run", "--select", name], expect_pass=False)
            assert self.rows(project, relation) == [(1, "first"), (2, "second"), (3, "third")]
            write_file(models[name + ".sql"], project.project_root, "models", name + ".sql")
            with patch_microbatch_end_time("2025-01-02 00:00:00"):
                run_dbt(["run", "--select", name])
            assert self.rows(project, relation) == [(1, "first"), (2, "second"), (3, "third")]
            self.assert_cleanup(project, relation)
        finally:
            write_file(models[name + ".sql"], project.project_root, "models", name + ".sql")
