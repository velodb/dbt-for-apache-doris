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
from dbt.tests.util import relation_from_name, run_dbt


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


class TestDorisPartitionReplace:
    @pytest.fixture(scope="class")
    def models(self):
        return {"partition_replace.sql": PARTITION_REPLACE_SQL}

    def test_rerun_replaces_only_selected_partitions(self, project):
        first_run = run_dbt(["run"])
        assert len(first_run) == 1

        relation = relation_from_name(project.adapter, "partition_replace")
        rows = project.run_sql(
            f"select part_id, value from {relation} order by part_id",
            fetch="all",
        )
        assert rows == [
            (1, "old_partition_1"),
            (2, "unchanged_partition_2"),
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

        temporary_relations = project.run_sql(
            "select table_name from information_schema.tables "
            f"where table_schema = '{relation.schema}' "
            "and table_name like 'partition_replace__dbt_tmp%'",
            fetch="all",
        )
        assert temporary_relations == []
