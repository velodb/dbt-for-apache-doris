#!/usr/bin/env python
# encoding: utf-8

# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

"""Functional tests for the Doris partition materialization."""

import pytest
from dbt.tests.util import relation_from_name, run_dbt, set_model_file


PARTITION_REPLACE_SQL = """
{{ config(
    materialized='partition',
    duplicate_key=['part_id'],
    partition_by=['part_id'],
    partition_type='RANGE',
    partition_by_init=[
        'PARTITION p1 VALUES LESS THAN ("2")',
        'PARTITION p2 VALUES LESS THAN ("3")'
    ],
    distributed_by=['part_id'],
    properties={'replication_num': '1'}
) }}

{% if is_incremental() %}
select 1 as part_id, 'new_partition_1' as value
{% else %}
select 1 as part_id, 'old_partition_1' as value
union all
select 2 as part_id, 'unchanged_partition_2' as value
{% endif %}
"""


PARTITION_FAILURE_SQL = """
{{ config(
    materialized='partition',
    duplicate_key=['part_id'],
    partition_by=['part_id'],
    partition_type='RANGE',
    partition_by_init=[
        'PARTITION p1 VALUES LESS THAN ("2")',
        'PARTITION p2 VALUES LESS THAN ("3")'
    ],
    distributed_by=['part_id'],
    properties={'replication_num': '1'}
) }}

select * from dbt_partition_intentional_missing_relation
"""


def _partition_helper_relations(project, relation):
    return project.run_sql(
        "select table_name from information_schema.tables "
        f"where table_schema = '{relation.schema}' "
        f"and table_name like '{relation.identifier}__dbt_%' "
        "order by table_name",
        fetch="all",
    )


class TestDorisPartitionReplace:
    @pytest.fixture(scope="class")
    def models(self):
        return {"partition_replace.sql": PARTITION_REPLACE_SQL}

    def test_rerun_replaces_only_selected_partitions(self, project):
        relation = relation_from_name(project.adapter, "partition_replace")
        project.run_sql(
            f"create view {relation} as "
            "select 9 as part_id, 'old_view' as value"
        )

        first_run = run_dbt(["run"])
        assert len(first_run) == 1

        rows = project.run_sql(
            f"select part_id, value from {relation} order by part_id",
            fetch="all",
        )
        assert rows == [
            (1, "old_partition_1"),
            (2, "unchanged_partition_2"),
        ]

        temp_name = f"{relation.identifier}__dbt_tmp"
        backup_name = f"{relation.identifier}__dbt_backup"
        project.run_sql(
            f"create view `{relation.schema}`.`{temp_name}` as "
            "select -1 as sentinel"
        )
        project.run_sql(
            f"create table `{relation.schema}`.`{backup_name}` "
            "(`sentinel` int) duplicate key(`sentinel`) "
            "distributed by hash(`sentinel`) buckets 1 "
            'properties ("replication_num" = "1")'
        )
        project.run_sql(
            f"insert into `{relation.schema}`.`{backup_name}` values (-2)"
        )
        assert _partition_helper_relations(project, relation) == [
            (backup_name,),
            (temp_name,),
        ]

        second_run = run_dbt(["run"])
        assert len(second_run) == 1

        rows = project.run_sql(
            f"select part_id, value from {relation} order by part_id",
            fetch="all",
        )
        assert rows == [
            (1, "new_partition_1"),
            (2, "unchanged_partition_2"),
        ]
        assert _partition_helper_relations(project, relation) == []

        project.run_sql(
            f"alter table {relation} rename `{backup_name}`"
        )
        project.run_sql(
            f"create view `{relation.schema}`.`{temp_name}` as "
            "select -3 as sentinel"
        )
        assert _partition_helper_relations(project, relation) == [
            (backup_name,),
            (temp_name,),
        ]
        set_model_file(project, relation, PARTITION_FAILURE_SQL)
        failure = run_dbt(["run"], expect_pass=False)
        assert len(failure.results) == 1
        assert project.run_sql(
            "select count(*) from information_schema.tables "
            f"where table_schema = '{relation.schema}' "
            f"and table_name = '{relation.identifier}'",
            fetch="one",
        )[0] == 0
        assert project.run_sql(
            f"select part_id, value from `{relation.schema}`.`{backup_name}` "
            "order by part_id",
            fetch="all",
        ) == [
            (1, "new_partition_1"),
            (2, "unchanged_partition_2"),
        ]
        assert project.run_sql(
            "select table_type from information_schema.tables "
            f"where table_schema = '{relation.schema}' "
            f"and table_name = '{backup_name}'",
            fetch="one",
        )[0] == "BASE TABLE"
        assert _partition_helper_relations(project, relation) == [
            (backup_name,),
        ]

        # A second retry must still compile as a full build. Publishing the
        # backup at the canonical name before the failed run would make
        # is_incremental() true here and incorrectly keep new_partition_1.
        set_model_file(project, relation, PARTITION_REPLACE_SQL)
        retry = run_dbt(["run"])
        assert len(retry) == 1
        rows = project.run_sql(
            f"select part_id, value from {relation} order by part_id",
            fetch="all",
        )
        assert rows == [
            (1, "old_partition_1"),
            (2, "unchanged_partition_2"),
        ]

        assert _partition_helper_relations(project, relation) == []
