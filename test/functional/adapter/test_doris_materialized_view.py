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
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

"""End-to-end coverage for Doris asynchronous materialized views."""

import re
import time

import pytest
from dbt.adapters.contracts.relation import RelationType
from dbt.tests.util import get_connection, relation_from_name, run_dbt, set_model_file

BASE_ORDERS_SQL = """
{{ config(
    materialized='table',
    duplicate_key=['order_id'],
    distributed_by=['order_id'],
    buckets=1,
    properties={'replication_num': '1'}
) }}

select 1 as order_id, cast('2026-07-01' as date) as order_date, 100 as amount
union all
select 2 as order_id, cast('2026-07-01' as date) as order_date, 200 as amount
union all
select 3 as order_id, cast('2026-07-02' as date) as order_date, 50 as amount
"""

DAILY_SALES_MV_SQL = """
{{ config(
    materialized='materialized_view',
    build_mode='deferred',
    refresh_method='complete',
    refresh_trigger='manual',
    duplicate_key=['order_date'],
    distribution_type='hash',
    distributed_by=['order_date'],
    buckets=1,
    properties={'replication_num': '1'}
) }}

select order_date, sum(amount) as sales
from {{ ref('base_orders') }}
group by order_date
"""

DAILY_SALES_MV_CHANGED_SQL = DAILY_SALES_MV_SQL.replace(
    "sum(amount) as sales",
    "sum(amount) + 1 as sales",
)

DAILY_SALES_IMMEDIATE_SQL = DAILY_SALES_MV_SQL.replace(
    "build_mode='deferred'",
    "build_mode='immediate'",
)

DAILY_SALES_IMMEDIATE_BUCKETS_2_SQL = DAILY_SALES_IMMEDIATE_SQL.replace(
    "buckets=1",
    "buckets=2",
)

DAILY_SALES_IMMEDIATE_CHANGED_SQL = (
    DAILY_SALES_IMMEDIATE_BUCKETS_2_SQL.replace(
        "sum(amount) as sales",
        "sum(amount) + 1 as sales",
    )
)

DAILY_SALES_ASYNC_FAILURE_SQL = DAILY_SALES_IMMEDIATE_CHANGED_SQL.replace(
    "sum(amount) + 1 as sales",
    "sum(if(json_parse(if(order_id < 0, '{}', 'DBT_ASYNC_FAILURE')) "
    "is not null, amount, 0)) as sales",
)

DAILY_SALES_ON_COMMIT_SQL = DAILY_SALES_MV_SQL.replace(
    "refresh_method='complete'",
    "refresh_method='auto'",
).replace(
    "refresh_trigger='manual'",
    "refresh_trigger='commit'",
)

DAILY_SALES_FAILING_POST_HOOK_SQL = DAILY_SALES_MV_SQL.replace(
    "properties={'replication_num': '1'}",
    "properties={'replication_num': '1'},\n"
    "    post_hook='select * from __dbt_missing_post_hook_table__'",
)

DAILY_SALES_CHANGED_FAILING_POST_HOOK_SQL = (
    DAILY_SALES_MV_CHANGED_SQL.replace(
        "properties={'replication_num': '1'}",
        "properties={'replication_num': '1'},\n"
        "    post_hook='select * from __dbt_missing_post_hook_table__'",
    )
)

MV_TOP_LEVEL_REPLICATION_SQL = """
{{ config(
    materialized='materialized_view',
    build_mode='deferred',
    refresh_method='complete',
    refresh_trigger='manual',
    distributed_by=['order_date'],
    buckets=1,
    properties={'replication_num': '3'},
    replication_num='1'
) }}

select order_date, sum(amount) as sales
from {{ ref('base_orders') }}
group by order_date
"""

PARTITIONED_ORDERS_SQL = """
{{ config(
    materialized='table',
    duplicate_key=['order_date', 'order_id'],
    partition_by=['order_date'],
    partition_type='RANGE',
    partition_by_init=[
        "PARTITION p202607 VALUES LESS THAN ('2026-08-01')",
        "PARTITION pmax VALUES LESS THAN ('9999-12-31')"
    ],
    distributed_by=['order_id'],
    buckets=1,
    properties={'replication_num': '1'}
) }}

select cast('2026-07-01' as date) as order_date, 1 as order_id, 100 as amount
union all
select cast('2026-07-02' as date) as order_date, 2 as order_id, 50 as amount
"""

MV_SINGLE_PARTITION_LIST_SQL = """
{{ config(
    materialized='materialized_view',
    build_mode='deferred',
    refresh_method='complete',
    refresh_trigger='manual',
    partition_by=['order_date'],
    distributed_by=['order_date'],
    buckets=1,
    properties={'replication_num': '1'}
) }}

select order_date, sum(amount) as sales
from {{ ref('partitioned_orders') }}
group by order_date
"""


def with_change_policy(sql, policy):
    return sql.replace(
        "materialized='materialized_view',",
        "materialized='materialized_view',\n"
        f"    on_configuration_change='{policy}',",
    )


DAILY_SALES_SCHEDULED_SQL = """
{{ config(
    materialized='materialized_view',
    build_mode='deferred',
    refresh_method='auto',
    refresh_trigger='schedule',
    refresh_schedule={
        'interval': 1,
        'unit': 'day',
        'start_time': '2099-08-01 02:00:00'
    },
    buckets=1,
    properties={'replication_num': '1'}
) }}

select order_date, sum(amount) as sales
from {{ ref('base_orders') }}
group by order_date
"""

SWITCHABLE_TABLE_SQL = """
{{ config(
    materialized='table',
    duplicate_key=['order_date'],
    distributed_by=['order_date'],
    buckets=1,
    properties={'replication_num': '1'}
) }}

select order_date, sum(amount) as sales
from {{ ref('base_orders') }}
group by order_date
"""

SWITCHABLE_MV_SQL = DAILY_SALES_MV_SQL.replace(
    "daily_sales",
    "switchable",
)

SWITCHABLE_VIEW_SQL = """
{{ config(materialized='view') }}

select order_date, sum(amount) as sales
from {{ ref('base_orders') }}
group by order_date
"""


TYPE_SWITCH_RENAME_FAILURE_MACRO = """
{% macro doris__rename_relation(from_relation, to_relation) %}
    {% if (
        var('fail_intermediate_rename', false)
        and '__dbt_tmp' in from_relation.identifier
    ) %}
        {% do exceptions.raise_compiler_error(
            'intentional intermediate rename failure'
        ) %}
    {% endif %}
    {% call statement('drop_relation') %}
        drop {{
            'materialized view'
            if to_relation.type == 'materialized_view'
            else to_relation.type
        }} if exists {{ to_relation }}
    {% endcall %}
    {% call statement('rename_relation') %}
        {% if to_relation.type == 'materialized_view' %}
        alter materialized view {{ from_relation }}
            rename `{{ to_relation.table | replace("`", "``") }}`
        {% else %}
        alter table {{ from_relation }} rename {{ to_relation.table }}
        {% endif %}
    {% endcall %}
{% endmacro %}
"""


def mv_info(project, relation, columns="Id, Name, State, RefreshState, QuerySql"):
    return project.run_sql(
        f'select {columns} from mv_infos("database"="{relation.schema}") '
        f"where Name = '{relation.identifier}'",
        fetch="one",
    )


def relation_type(project, relation):
    adapter = project.adapter
    schema_relation = adapter.Relation.create(schema=relation.schema)
    with get_connection(adapter):
        relations = adapter.list_relations_without_caching(schema_relation)
    return next(
        item.type for item in relations if item.identifier == relation.identifier
    )


def helper_relations(project, relation):
    return project.run_sql(
        "select table_name from information_schema.tables "
        f"where table_schema = '{relation.schema}' "
        f"and table_name like '{relation.identifier}__dbt_%' "
        "order by table_name",
        fetch="all",
    )


def materialized_view_task_ids(project, relation):
    rows = project.run_sql(
        "select TaskId from tasks('type'='mv') "
        f"where MvDatabaseName = '{relation.schema}' "
        f"and MvName = '{relation.identifier}'",
        fetch="all",
    )
    return {str(row[0]) for row in rows}


def wait_for_new_refresh(project, relation, previous_task_ids, timeout=60):
    deadline = time.monotonic() + timeout
    latest = None
    while time.monotonic() < deadline:
        tasks = project.run_sql(
            "select TaskId, Status from tasks('type'='mv') "
            f"where MvDatabaseName = '{relation.schema}' "
            f"and MvName = '{relation.identifier}' "
            "order by CreateTime desc, TaskId desc",
            fetch="all",
        )
        latest = next(
            (
                task
                for task in tasks
                if str(task[0]) not in previous_task_ids
            ),
            None,
        )
        if latest and latest[1] not in ("PENDING", "RUNNING", "NULL", None):
            break
        time.sleep(0.5)
    assert latest is not None
    assert latest[1] == "SUCCESS", latest


class TestDorisMaterializedViewLifecycle:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "base_orders.sql": BASE_ORDERS_SQL,
            "daily_sales.sql": DAILY_SALES_MV_SQL,
        }

    def test_create_repeat_and_manual_refresh(self, project):
        first_run = run_dbt(["run"])
        assert len(first_run) == 2

        relation = relation_from_name(project.adapter, "daily_sales")
        assert relation_type(project, relation) == RelationType.MaterializedView
        first_info = mv_info(project, relation)
        assert first_info[1:4] == ("daily_sales", "INIT", "INIT")
        assert re.search(
            r"\bsum\s*\([^)]*\bamount\b[^)]*\)",
            first_info[4],
            re.IGNORECASE,
        )

        create_sql = project.run_sql(
            f"show create materialized view {relation}",
            fetch="one",
        )[1]
        assert "BUILD DEFERRED" in create_sql
        assert "REFRESH COMPLETE ON MANUAL" in create_sql
        assert "dbt-doris:definition-hash=" in create_sql
        assert materialized_view_task_ids(project, relation) == set()

        previous_task_ids = materialized_view_task_ids(project, relation)
        second_run = run_dbt(["run", "--select", "daily_sales"])
        assert len(second_run) == 1
        assert mv_info(project, relation)[0] == first_info[0]
        second_response = second_run[0].adapter_response
        assert second_response["code"] == "REFRESH MATERIALIZED VIEW"
        assert second_response["task_status"] == "SUCCESS"
        assert second_response["task_id"] not in previous_task_ids
        rows = project.run_sql(
            f"select order_date, sales from {relation} order by order_date",
            fetch="all",
        )
        assert rows == [
            (rows[0][0], 300),
            (rows[1][0], 50),
        ]

        previous_task_ids = materialized_view_task_ids(project, relation)
        project.run_sql(
            "insert into base_orders values "
            "(4, cast('2026-07-03' as date), 75)"
        )
        third_run = run_dbt(["run", "--select", "daily_sales"])
        assert len(third_run) == 1
        third_response = third_run[0].adapter_response
        assert third_response["code"] == "REFRESH MATERIALIZED VIEW"
        assert third_response["task_status"] == "SUCCESS"
        assert third_response["task_id"] not in previous_task_ids
        refreshed_rows = project.run_sql(
            f"select order_date, sales from {relation} order by order_date",
            fetch="all",
        )
        assert [row[1] for row in refreshed_rows] == [300, 50, 75]


class TestDorisMaterializedViewChanges:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "base_orders.sql": BASE_ORDERS_SQL,
            "daily_sales.sql": DAILY_SALES_IMMEDIATE_SQL,
        }

    def test_config_sql_full_refresh_and_failure_are_atomic(self, project):
        run_dbt(["run"])
        relation = relation_from_name(project.adapter, "daily_sales")
        first_id = mv_info(project, relation)[0]
        initial_rows = project.run_sql(
            f"select order_date, sales from {relation} order by order_date",
            fetch="all",
        )
        assert [row[1] for row in initial_rows] == [300, 50]

        set_model_file(project, relation, DAILY_SALES_IMMEDIATE_BUCKETS_2_SQL)
        config_change = run_dbt(["run", "--select", "daily_sales"])
        assert len(config_change) == 1
        config_info = mv_info(project, relation)
        assert config_info[0] != first_id
        config_create_sql = project.run_sql(
            f"show create materialized view {relation}",
            fetch="one",
        )[1]
        assert "BUILD IMMEDIATE" in config_create_sql
        assert "BUCKETS 2" in config_create_sql

        set_model_file(project, relation, DAILY_SALES_IMMEDIATE_CHANGED_SQL)
        changed_run = run_dbt(["run", "--select", "daily_sales"])
        assert len(changed_run) == 1
        changed_info = mv_info(project, relation)
        assert changed_info[0] != config_info[0]
        assert changed_info[4] != config_info[4]
        changed_rows = project.run_sql(
            f"select order_date, sales from {relation} order by order_date",
            fetch="all",
        )
        assert [row[1] for row in changed_rows] == [301, 51]

        full_refresh = run_dbt(["run", "--select", "daily_sales", "--full-refresh"])
        assert len(full_refresh) == 1
        assert mv_info(project, relation)[0] != changed_info[0]
        full_refresh_rows = project.run_sql(
            f"select order_date, sales from {relation} order by order_date",
            fetch="all",
        )
        assert [row[1] for row in full_refresh_rows] == [301, 51]

        stable_info = mv_info(project, relation)
        set_model_file(project, relation, DAILY_SALES_ASYNC_FAILURE_SQL)
        run_dbt(["run", "--select", "daily_sales"], expect_pass=False)
        failed_info = mv_info(project, relation)
        assert failed_info[0] == stable_info[0]
        assert failed_info[4] == stable_info[4]
        failed_rows = project.run_sql(
            f"select order_date, sales from {relation} order by order_date",
            fetch="all",
        )
        assert failed_rows == full_refresh_rows
        failed_task = project.run_sql(
            "select Status, ErrorMsg from tasks('type'='mv') "
            f"where MvDatabaseName = '{relation.schema}' "
            "and MvName like 'daily_sales__dbt_tmp%' "
            "order by CreateTime desc, TaskId desc limit 1",
            fetch="one",
        )
        assert failed_task[0] == "FAILED"
        failure_message = failed_task[1].casefold()
        assert "json" in failure_message
        assert "parse" in failure_message

        set_model_file(project, relation, DAILY_SALES_IMMEDIATE_CHANGED_SQL)
        recovery = run_dbt(["run", "--select", "daily_sales"])
        assert len(recovery) == 1

        temporary_relations = project.run_sql(
            "select Name from mv_infos("
            f'"database"="{relation.schema}") '
            "where Name like 'daily_sales__dbt_tmp%'",
            fetch="all",
        )
        assert temporary_relations == []


class TestDorisMaterializedViewOnCommit:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "base_orders.sql": BASE_ORDERS_SQL,
            "daily_sales_commit.sql": DAILY_SALES_ON_COMMIT_SQL,
        }

    def test_commit_trigger_refreshes_the_deferred_materialized_view(self, project):
        run_dbt(["run"])
        relation = relation_from_name(project.adapter, "daily_sales_commit")
        create_sql = project.run_sql(
            f"show create materialized view {relation}",
            fetch="one",
        )[1]
        assert "BUILD DEFERRED" in create_sql
        assert "REFRESH AUTO ON COMMIT" in create_sql

        previous_task_ids = materialized_view_task_ids(
            project,
            relation,
        )
        unchanged_run = run_dbt(
            ["run", "--select", "daily_sales_commit"]
        )
        assert len(unchanged_run) == 1
        assert unchanged_run[0].adapter_response["code"] == "skip"
        assert materialized_view_task_ids(
            project,
            relation,
        ) == previous_task_ids

        project.run_sql(
            "insert into base_orders values "
            "(4, cast('2026-07-03' as date), 75)"
        )
        wait_for_new_refresh(project, relation, previous_task_ids)

        rows = project.run_sql(
            f"select order_date, sales from {relation} order by order_date",
            fetch="all",
        )
        assert [row[1] for row in rows] == [300, 50, 75]


class TestDorisMaterializedViewContinue:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "base_orders.sql": BASE_ORDERS_SQL,
            "daily_sales.sql": DAILY_SALES_MV_SQL,
        }

    def test_continue_keeps_the_existing_definition(self, project):
        run_dbt(["run"])
        relation = relation_from_name(project.adapter, "daily_sales")
        initial_info = mv_info(project, relation)

        set_model_file(
            project,
            relation,
            with_change_policy(DAILY_SALES_MV_CHANGED_SQL, "continue"),
        )
        result = run_dbt(["run", "--select", "daily_sales"])
        assert len(result) == 1

        continued_info = mv_info(project, relation)
        assert continued_info[0] == initial_info[0]
        assert continued_info[4] == initial_info[4]


class TestDorisMaterializedViewFail:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "base_orders.sql": BASE_ORDERS_SQL,
            "daily_sales.sql": DAILY_SALES_MV_SQL,
        }

    def test_fail_rejects_the_change_and_keeps_the_existing_definition(self, project):
        run_dbt(["run"])
        relation = relation_from_name(project.adapter, "daily_sales")
        initial_info = mv_info(project, relation)

        set_model_file(
            project,
            relation,
            with_change_policy(DAILY_SALES_MV_CHANGED_SQL, "fail"),
        )
        result = run_dbt(
            ["run", "--select", "daily_sales"],
            expect_pass=False,
        )
        assert len(result) == 1

        failed_info = mv_info(project, relation)
        assert failed_info[0] == initial_info[0]
        assert failed_info[4] == initial_info[4]


class TestDorisMaterializedViewPendingRecovery:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "base_orders.sql": BASE_ORDERS_SQL,
            "daily_sales.sql": DAILY_SALES_FAILING_POST_HOOK_SQL,
        }

    def test_pending_deployment_recovers_even_with_continue_policy(self, project):
        result = run_dbt(["run"], expect_pass=False)
        assert len(result) == 2

        relation = relation_from_name(project.adapter, "daily_sales")
        pending_create_sql = project.run_sql(
            f"show create materialized view {relation}",
            fetch="one",
        )[1]
        assert "dbt-doris:deployment-pending=" in pending_create_sql

        set_model_file(
            project,
            relation,
            with_change_policy(DAILY_SALES_MV_SQL, "continue"),
        )
        recovery = run_dbt(["run", "--select", "daily_sales"])
        assert len(recovery) == 1

        completed_create_sql = project.run_sql(
            f"show create materialized view {relation}",
            fetch="one",
        )[1]
        assert "dbt-doris:definition-hash=" in completed_create_sql
        assert "dbt-doris:deployment-pending=" not in completed_create_sql


class TestDorisMaterializedViewReplaceRollback:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "base_orders.sql": BASE_ORDERS_SQL,
            "daily_sales.sql": DAILY_SALES_MV_SQL,
        }

    def test_failed_post_hook_preserves_and_restores_previous_mv(self, project):
        run_dbt(["run"])
        relation = relation_from_name(project.adapter, "daily_sales")
        original_info = mv_info(project, relation)

        set_model_file(
            project,
            relation,
            DAILY_SALES_CHANGED_FAILING_POST_HOOK_SQL,
        )
        failed_replace = run_dbt(
            ["run", "--select", "daily_sales"],
            expect_pass=False,
        )
        assert len(failed_replace) == 1

        pending_ddl = project.run_sql(
            f"show create materialized view {relation}",
            fetch="one",
        )[1]
        assert "dbt-doris:deployment-pending=" in pending_ddl
        preserved_old_mv = project.run_sql(
            "select Id, QuerySql from mv_infos("
            f'"database"="{relation.schema}") '
            "where Name = 'daily_sales__dbt_tmp'",
            fetch="one",
        )
        assert preserved_old_mv[0] == original_info[0]
        assert preserved_old_mv[1] == original_info[4]

        set_model_file(project, relation, DAILY_SALES_ASYNC_FAILURE_SQL)
        failed_retry = run_dbt(
            ["run", "--select", "daily_sales"],
            expect_pass=False,
        )
        assert len(failed_retry) == 1

        restored_info = mv_info(project, relation)
        assert restored_info[0] == original_info[0]
        assert restored_info[4] == original_info[4]
        restored_ddl = project.run_sql(
            f"show create materialized view {relation}",
            fetch="one",
        )[1]
        assert "dbt-doris:definition-hash=" in restored_ddl
        assert "dbt-doris:deployment-pending=" not in restored_ddl

        set_model_file(project, relation, DAILY_SALES_MV_CHANGED_SQL)
        completed_retry = run_dbt(["run", "--select", "daily_sales"])
        assert len(completed_retry) == 1
        assert mv_info(project, relation)[0] != original_info[0]
        temporary_relations = project.run_sql(
            "select Name from mv_infos("
            f'"database"="{relation.schema}") '
            "where Name like 'daily_sales__dbt_tmp%'",
            fetch="all",
        )
        assert temporary_relations == []


class TestDorisMaterializedViewSchedule:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "base_orders.sql": BASE_ORDERS_SQL,
            "daily_sales_scheduled.sql": DAILY_SALES_SCHEDULED_SQL,
        }

    def test_schedule_config_is_present_in_doris_ddl(self, project):
        run_dbt(["run"])
        relation = relation_from_name(project.adapter, "daily_sales_scheduled")
        create_sql = project.run_sql(
            f"show create materialized view {relation}",
            fetch="one",
        )[1]

        assert "REFRESH AUTO ON SCHEDULE EVERY 1 DAY" in create_sql
        assert 'STARTS "2099-08-01 02:00:00"' in create_sql

        previous_task_ids = materialized_view_task_ids(project, relation)
        unchanged_run = run_dbt(
            ["run", "--select", "daily_sales_scheduled"]
        )
        assert len(unchanged_run) == 1
        assert unchanged_run[0].adapter_response["code"] == "skip"
        assert materialized_view_task_ids(
            project,
            relation,
        ) == previous_task_ids


class TestDorisMvConfig:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "base_orders.sql": BASE_ORDERS_SQL,
            "mv_replication.sql": MV_TOP_LEVEL_REPLICATION_SQL,
            "partitioned_orders.sql": PARTITIONED_ORDERS_SQL,
            "mv_partition.sql": MV_SINGLE_PARTITION_LIST_SQL,
        }

    def test_top_level_replication_num_is_present_in_doris_ddl(self, project):
        result = run_dbt(["run", "--select", "+mv_replication"])
        assert len(result) == 2

        relation = relation_from_name(project.adapter, "mv_replication")
        create_sql = project.run_sql(
            f"show create materialized view {relation}",
            fetch="one",
        )[1]

        assert re.search(
            (
                r"""["']replication_allocation["']\s*=\s*"""
                r"""["']tag\.location\.default:\s*1["']"""
            ),
            create_sql,
            re.IGNORECASE,
        )

    def test_single_partition_list_is_present_in_doris_ddl(self, project):
        result = run_dbt(["run", "--select", "+mv_partition"])
        assert len(result) == 2

        relation = relation_from_name(project.adapter, "mv_partition")
        create_sql = project.run_sql(
            f"show create materialized view {relation}",
            fetch="one",
        )[1]

        assert re.search(
            r"partition\s+by\s*\(\s*`?order_date`?\s*\)",
            create_sql,
            re.IGNORECASE,
        )


class TestDorisMaterializedViewTypeSwitch:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "base_orders.sql": BASE_ORDERS_SQL,
            "switchable.sql": SWITCHABLE_TABLE_SQL,
        }

    @pytest.fixture(scope="class")
    def macros(self):
        return {
            "type_switch_rename_failure.sql": (
                TYPE_SWITCH_RENAME_FAILURE_MACRO
            ),
        }

    def test_table_materialized_view_view_and_table_switches(self, project):
        run_dbt(["run"])
        relation = relation_from_name(project.adapter, "switchable")
        assert relation_type(project, relation) == RelationType.Table
        assert project.run_sql(
            f"select count(*) from {relation}",
            fetch="one",
        )[0] == 2
        assert helper_relations(project, relation) == []

        set_model_file(project, relation, SWITCHABLE_MV_SQL)
        run_dbt(["run", "--select", "switchable"])
        relation = relation_from_name(project.adapter, "switchable")
        assert relation_type(project, relation) == RelationType.MaterializedView
        assert helper_relations(project, relation) == []

        set_model_file(project, relation, SWITCHABLE_VIEW_SQL)
        run_dbt(["run", "--select", "switchable"])
        relation = relation_from_name(project.adapter, "switchable")
        assert relation_type(project, relation) == RelationType.View
        assert project.run_sql(
            f"select count(*) from {relation}",
            fetch="one",
        )[0] == 2
        assert helper_relations(project, relation) == []

        set_model_file(project, relation, SWITCHABLE_MV_SQL)
        run_dbt(["run", "--select", "switchable"])
        relation = relation_from_name(project.adapter, "switchable")
        assert relation_type(project, relation) == RelationType.MaterializedView

        # Direct MV -> Table replacement first preserves the old MV under the
        # backup name. If the following Table rename fails, a retry restores
        # that MV and can complete the type switch without data loss.
        set_model_file(project, relation, SWITCHABLE_TABLE_SQL)
        failure = run_dbt(
            [
                "run",
                "--select",
                "switchable",
                "--vars",
                "{fail_intermediate_rename: true}",
            ],
            expect_pass=False,
        )
        assert len(failure.results) == 1
        backup_relation = relation.incorporate(
            path={"identifier": f"{relation.identifier}__dbt_backup"},
            type=RelationType.MaterializedView,
        )
        assert relation_type(
            project,
            backup_relation,
        ) == RelationType.MaterializedView

        run_dbt(["run", "--select", "switchable"])
        relation = relation_from_name(project.adapter, "switchable")
        assert relation_type(project, relation) == RelationType.Table
        assert project.run_sql(
            f"select count(*) from {relation}",
            fetch="one",
        )[0] == 2
        assert helper_relations(project, relation) == []

        set_model_file(project, relation, SWITCHABLE_VIEW_SQL)
        run_dbt(["run", "--select", "switchable"])
        relation = relation_from_name(project.adapter, "switchable")
        assert relation_type(project, relation) == RelationType.View
        assert project.run_sql(
            f"select count(*) from {relation}",
            fetch="one",
        )[0] == 2
        assert helper_relations(project, relation) == []

        set_model_file(project, relation, SWITCHABLE_TABLE_SQL)
        run_dbt(["run", "--select", "switchable"])
        relation = relation_from_name(project.adapter, "switchable")
        assert relation_type(project, relation) == RelationType.Table
        assert project.run_sql(
            f"select count(*) from {relation}",
            fetch="one",
        )[0] == 2
        assert helper_relations(project, relation) == []
