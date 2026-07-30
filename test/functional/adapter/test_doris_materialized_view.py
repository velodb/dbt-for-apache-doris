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
    "sum(if(assert_true(order_id < 0, 'dbt async failure'), amount, 0)) "
    "as sales",
)

DAILY_SALES_REFRESH_ON_RUN_SQL = DAILY_SALES_IMMEDIATE_SQL.replace(
    "refresh_trigger='manual',",
    "refresh_trigger='manual',\n    refresh_on_run=true,",
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

        second_run = run_dbt(["run", "--select", "daily_sales"])
        assert len(second_run) == 1
        assert mv_info(project, relation)[0] == first_info[0]

        previous_task_ids = materialized_view_task_ids(
            project,
            relation,
        )
        project.run_sql(f"refresh materialized view {relation} complete")
        wait_for_new_refresh(project, relation, previous_task_ids)
        rows = project.run_sql(
            f"select order_date, sales from {relation} order by order_date",
            fetch="all",
        )
        assert rows == [
            (rows[0][0], 300),
            (rows[1][0], 50),
        ]


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
        assert "dbt async failure" in failed_task[1]

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


class TestDorisMaterializedViewRefreshOnRun:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "base_orders.sql": BASE_ORDERS_SQL,
            "daily_sales_refresh.sql": DAILY_SALES_REFRESH_ON_RUN_SQL,
        }

    def test_refresh_on_run_waits_until_new_data_is_queryable(self, project):
        run_dbt(["run"])
        relation = relation_from_name(project.adapter, "daily_sales_refresh")
        initial_rows = project.run_sql(
            f"select order_date, sales from {relation} order by order_date",
            fetch="all",
        )
        assert [row[1] for row in initial_rows] == [300, 50]

        project.run_sql(
            "insert into base_orders values "
            "(4, cast('2026-07-03' as date), 75)"
        )
        run_dbt(["run", "--select", "daily_sales_refresh"])

        refreshed_rows = project.run_sql(
            f"select order_date, sales from {relation} order by order_date",
            fetch="all",
        )
        assert [row[1] for row in refreshed_rows] == [300, 50, 75]
        latest_task = project.run_sql(
            "select Status from tasks('type'='mv') "
            f"where MvDatabaseName = '{relation.schema}' "
            f"and MvName = '{relation.identifier}' "
            "order by CreateTime desc, TaskId desc limit 1",
            fetch="one",
        )
        assert latest_task == ("SUCCESS",)


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


class TestDorisMaterializedViewTypeSwitch:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "base_orders.sql": BASE_ORDERS_SQL,
            "switchable.sql": SWITCHABLE_TABLE_SQL,
        }

    def test_table_materialized_view_view_and_table_switches(self, project):
        run_dbt(["run"])
        relation = relation_from_name(project.adapter, "switchable")
        assert relation_type(project, relation) == RelationType.Table

        set_model_file(project, relation, SWITCHABLE_MV_SQL)
        run_dbt(["run", "--select", "switchable"])
        relation = relation_from_name(project.adapter, "switchable")
        assert relation_type(project, relation) == RelationType.MaterializedView

        set_model_file(project, relation, SWITCHABLE_VIEW_SQL)
        run_dbt(["run", "--select", "switchable"])
        relation = relation_from_name(project.adapter, "switchable")
        assert relation_type(project, relation) == RelationType.View

        set_model_file(project, relation, SWITCHABLE_TABLE_SQL)
        run_dbt(["run", "--select", "switchable"])
        relation = relation_from_name(project.adapter, "switchable")
        assert relation_type(project, relation) == RelationType.Table
