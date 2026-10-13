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

"""
Tests for Doris view materialization: create, replace (CREATE OR REPLACE VIEW),
and view over different sources.
"""

import pytest
from dbt.tests.util import run_dbt, relation_from_name


SOURCE_TABLE_SQL = """
{{ config(
    materialized='table',
    distributed_by=['id'],
    properties={'replication_num': '1'}
) }}

select 1 as id, 'alice' as name, 100 as score
union all
select 2 as id, 'bob' as name, 200 as score
"""

VIEW_BASIC_SQL = """
{{ config(materialized='view') }}

select id, name, score from {{ ref('source_table') }}
"""

VIEW_FILTERED_SQL = """
{{ config(materialized='view') }}

select id, name, score from {{ ref('source_table') }} where score >= 200
"""


class TestDorisViewCreate:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "source_table.sql": SOURCE_TABLE_SQL,
            "view_basic.sql": VIEW_BASIC_SQL,
        }

    def test_view_create(self, project):
        results = run_dbt(["run"])
        assert len(results) == 2

        relation = relation_from_name(project.adapter, "view_basic")
        result = project.run_sql(f"select count(*) from {relation}", fetch="one")
        assert result[0] == 2


class TestDorisViewReplace:
    """Test re-running view uses CREATE OR REPLACE VIEW successfully."""

    @pytest.fixture(scope="class")
    def models(self):
        return {
            "source_table.sql": SOURCE_TABLE_SQL,
            "view_basic.sql": VIEW_BASIC_SQL,
        }

    def test_view_replace(self, project):
        # First run
        results = run_dbt(["run"])
        assert len(results) == 2

        # Second run - should succeed via CREATE OR REPLACE VIEW
        results = run_dbt(["run"])
        assert len(results) == 2

        relation = relation_from_name(project.adapter, "view_basic")
        result = project.run_sql(f"select count(*) from {relation}", fetch="one")
        assert result[0] == 2


class TestDorisViewFiltered:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "source_table.sql": SOURCE_TABLE_SQL,
            "view_filtered.sql": VIEW_FILTERED_SQL,
        }

    def test_view_filtered(self, project):
        results = run_dbt(["run"])
        assert len(results) == 2

        relation = relation_from_name(project.adapter, "view_filtered")
        result = project.run_sql(f"select count(*) from {relation}", fetch="one")
        assert result[0] == 1

        result = project.run_sql(f"select name from {relation}", fetch="one")
        assert result[0] == "bob"


class TestDorisViewTableTransition:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "view_table_transition.sql": """
{{ config(
    materialized=var('target_materialization', 'table'),
    properties={'replication_num': '1'}
) }}
select cast(1 as int) as id, cast('original' as string) as value
union all
select cast(2 as int) as id, cast('retained' as string) as value
"""
        }

    def test_internal_table_can_still_be_replaced_by_view(self, project):
        name = "view_table_transition"
        run_dbt(["run", "--select", name])
        args = ["run", "--select", name, "--vars", "{target_materialization: view}"]
        run_dbt(list(args))
        run_dbt(list(args))
        relation = relation_from_name(project.adapter, name)
        definition = project.run_sql(f"show create view {relation}", fetch="one")[1]
        assert "CREATE VIEW" in definition.upper()
        assert project.run_sql(
            f"select id, value from {relation} order by id", fetch="all"
        ) == [(1, "original"), (2, "retained")]
