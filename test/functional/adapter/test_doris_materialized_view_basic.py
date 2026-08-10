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

"""dbt-tests-adapter contract coverage for Doris materialized views."""

import time
from typing import Optional, Tuple

import pytest
from dbt.adapters.base.relation import BaseRelation
from dbt.tests.adapter.materialized_view.basic import MaterializedViewBasic
from dbt.tests.util import get_connection, get_model_file, run_dbt, set_model_file


def _latest_refresh_task(project, relation, previous_task_ids):
    tasks = project.run_sql(
        "select TaskId, Status, ErrorMsg "
        "from tasks('type'='mv') "
        f"where MvDatabaseName = '{relation.schema}' "
        f"and MvName = '{relation.identifier}' "
        "order by CreateTime desc, TaskId desc",
        fetch="all",
    )
    return next(
        (
            task
            for task in tasks
            if str(task[0]) not in previous_task_ids
        ),
        None,
    )


def _refresh_task_ids(project, relation):
    rows = project.run_sql(
        "select TaskId "
        "from tasks('type'='mv') "
        f"where MvDatabaseName = '{relation.schema}' "
        f"and MvName = '{relation.identifier}'",
        fetch="all",
    )
    return {str(row[0]) for row in rows}


class TestDorisMaterializedViewBasic(MaterializedViewBasic):
    """Run dbt's common materialized-view lifecycle contract against Doris."""

    @staticmethod
    def insert_record(
        project,
        table: BaseRelation,
        record: Tuple[int, int],
    ):
        project.run_sql(f"insert into {table} values ({record[0]}, {record[1]})")

    @staticmethod
    def refresh_materialized_view(
        project,
        materialized_view: BaseRelation,
    ):
        previous_task_ids = _refresh_task_ids(project, materialized_view)

        project.run_sql(f"refresh materialized view {materialized_view} complete")

        deadline = time.monotonic() + 120
        latest_task = None
        while time.monotonic() < deadline:
            latest_task = _latest_refresh_task(
                project,
                materialized_view,
                previous_task_ids,
            )
            if latest_task:
                status = str(latest_task[1]).upper()
                if status == "SUCCESS":
                    return
                if status in {"FAILED", "CANCELED", "CANCELLED"}:
                    raise AssertionError(
                        "Doris materialized view refresh failed "
                        f"(task {latest_task[0]}, status {status}): "
                        f"{latest_task[2] or 'no error message was returned'}"
                    )
            time.sleep(0.5)

        raise AssertionError(
            "Timed out waiting for Doris materialized view refresh "
            f"for {materialized_view}; latest task was {latest_task!r}"
        )

    @staticmethod
    def query_row_count(project, relation: BaseRelation) -> int:
        result = project.run_sql(f"select count(*) from {relation}", fetch="one")
        return int(result[0])

    @staticmethod
    def query_relation_type(
        project,
        relation: BaseRelation,
    ) -> Optional[str]:
        adapter = project.adapter
        schema_relation = adapter.Relation.create(schema=relation.schema)
        with get_connection(adapter):
            relations = adapter.list_relations_without_caching(schema_relation)
        actual_relation = next(
            (
                item
                for item in relations
                if item.identifier == relation.identifier
            ),
            None,
        )
        if actual_relation is None:
            return None
        return actual_relation.type.value

    @pytest.fixture(scope="function", autouse=True)
    def setup(self, project, my_materialized_view):
        run_dbt(["seed"])
        run_dbt(
            [
                "run",
                "--models",
                my_materialized_view.identifier,
                "--full-refresh",
            ]
        )
        initial_model = get_model_file(project, my_materialized_view)

        yield

        set_model_file(project, my_materialized_view, initial_model)
        project.drop_test_schema()
        project.create_test_schema()
