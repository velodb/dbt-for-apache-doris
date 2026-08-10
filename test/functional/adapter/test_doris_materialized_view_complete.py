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

"""Completion coverage for Doris asynchronous materialized views."""

import json

import pytest
from dbt.tests.util import (
    read_file,
    relation_from_name,
    run_dbt,
    write_file,
)


DOCS_BASE_SQL = """
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

DOCS_MV_SQL = """
{{ config(
    materialized='materialized_view',
    build_mode='immediate',
    refresh_method='complete',
    refresh_trigger='manual',
    duplicate_key=['order_date'],
    distributed_by=['order_date'],
    buckets=1,
    persist_docs={'relation': true, 'columns': true},
    properties={'replication_num': '1'}
) }}

select order_date, sum(amount) as sales
from {{ ref('docs_base_orders') }}
group by order_date
"""

DOCS_DISABLED_MV_SQL = DOCS_MV_SQL.replace(
    "persist_docs={'relation': true, 'columns': true}",
    "persist_docs={'relation': false, 'columns': false}",
)

DOCS_SCHEMA_YML = """
version: 2

models:
  - name: mv_docs_enabled
    description: "Documented daily sales"
    columns:
      - name: order_date
        description: "Business order date"
      - name: sales
        description: "Gross sales"

  - name: mv_docs_disabled
    description: "This relation description must not be persisted"
    columns:
      - name: order_date
        description: "This column description must not be persisted"
      - name: sales
        description: "This column description must not be persisted either"
"""

DOCS_SCHEMA_CHANGED_YML = DOCS_SCHEMA_YML.replace(
    'description: "Gross sales"',
    'description: "Gross sales after returns"',
)

SOURCE_ALIAS_YML = """
version: 2

sources:
  - name: mv_source
    schema: "{{ target.schema }}"
    tables:
      - name: source_orders
"""

SOURCE_REF_TABLE_SQL = """
{{ config(
    materialized='table',
    duplicate_key=['order_date', 'order_id'],
    distributed_by=['order_id'],
    buckets=1,
    properties={'replication_num': '1'}
) }}

select cast('2026-07-01' as date) as order_date, 3 as order_id, 50 as amount
"""

SOURCE_ALIAS_MV_SQL = """
{{ config(
    materialized='materialized_view',
    alias='mv_source_alias',
    schema='mv_custom',
    build_mode='immediate',
    refresh_method='complete',
    refresh_trigger='manual',
    duplicate_key=['order_date'],
    distributed_by=['order_date'],
    buckets=1,
    properties={'replication_num': '1'}
) }}

select order_date, sum(amount) as sales
from (
    select order_date, amount
    from {{ source('mv_source', 'source_orders') }}
    union all
    select order_date, amount
    from {{ ref('ref_orders') }}
) orders
group by order_date
"""


def _materialized_view_id(project, relation):
    row = project.run_sql(
        'select Id from mv_infos("database"='
        f'"{relation.schema}") where Name = \'{relation.identifier}\'',
        fetch="one",
    )
    return str(row[0])


def _column_comments(project, relation):
    rows = project.run_sql(
        f"show full columns from {relation}",
        fetch="all",
    )
    return {str(row[0]): str(row[8] or "") for row in rows}


class TestDorisMaterializedViewPersistDocs:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "docs_base_orders.sql": DOCS_BASE_SQL,
            "mv_docs_enabled.sql": DOCS_MV_SQL,
            "mv_docs_disabled.sql": DOCS_DISABLED_MV_SQL,
            "schema.yml": DOCS_SCHEMA_YML,
        }

    def test_relation_and_column_docs_follow_persist_docs_and_rebuild(
        self,
        project,
    ):
        results = run_dbt(["run"])
        assert len(results) == 3

        enabled = relation_from_name(project.adapter, "mv_docs_enabled")
        disabled = relation_from_name(project.adapter, "mv_docs_disabled")

        enabled_ddl = project.run_sql(
            f"show create materialized view {enabled}",
            fetch="one",
        )[1]
        disabled_ddl = project.run_sql(
            f"show create materialized view {disabled}",
            fetch="one",
        )[1]

        assert "Documented daily sales" in enabled_ddl
        assert "dbt-doris:definition-hash=" in enabled_ddl
        assert "This relation description must not be persisted" not in disabled_ddl
        assert "dbt-doris:definition-hash=" in disabled_ddl

        assert _column_comments(project, enabled) == {
            "order_date": "Business order date",
            "sales": "Gross sales",
        }
        assert _column_comments(project, disabled) == {
            "order_date": "",
            "sales": "",
        }

        original_id = _materialized_view_id(project, enabled)
        schema_path = (project.project_root, "models", "schema.yml")
        original_schema = read_file(*schema_path)
        try:
            write_file(DOCS_SCHEMA_CHANGED_YML, *schema_path)
            changed_results = run_dbt(["run", "--select", "mv_docs_enabled"])
        finally:
            write_file(original_schema, *schema_path)

        assert len(changed_results) == 1
        assert _materialized_view_id(project, enabled) != original_id
        assert _column_comments(project, enabled)["sales"] == (
            "Gross sales after returns"
        )


class TestDorisMaterializedViewSourceAliasAndSchema:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "ref_orders.sql": SOURCE_REF_TABLE_SQL,
            "mv_source_model.sql": SOURCE_ALIAS_MV_SQL,
            "sources.yml": SOURCE_ALIAS_YML,
        }

    @pytest.fixture(scope="class", autouse=True)
    def setup_source_and_custom_schema(self, project):
        custom_schema = f"{project.test_schema}_mv_custom"
        project.create_test_schema(custom_schema)
        try:
            project.run_sql(
                f"create table `{project.test_schema}`.`source_orders` "
                "(order_date date, order_id int, amount int) "
                "duplicate key(order_date, order_id) "
                "distributed by hash(order_id) buckets 1 "
                "properties('replication_num' = '1')"
            )
            project.run_sql(
                f"insert into `{project.test_schema}`.`source_orders` values "
                "('2026-07-01', 1, 100), "
                "('2026-07-01', 2, 200)"
            )
            yield
        finally:
            project.run_sql(
                f"drop database if exists `{custom_schema}` force"
            )
            project.adapter.cache.drop_schema(None, custom_schema)

    def test_source_alias_and_custom_schema(self, project):
        results = run_dbt(["run"])
        assert len(results) == 2

        node = next(
            result.node
            for result in results
            if result.node.name == "mv_source_model"
        )
        assert node.alias == "mv_source_alias"
        assert node.schema == f"{project.test_schema}_mv_custom"

        relation = project.adapter.Relation.create(
            schema=node.schema,
            identifier=node.alias,
            type="materialized_view",
        )
        rows = project.run_sql(
            f"select order_date, sales from {relation}",
            fetch="all",
        )
        assert len(rows) == 1
        assert rows[0][1] == 350

        ddl = project.run_sql(
            f"show create materialized view {relation}",
            fetch="one",
        )[1]
        assert f"`{project.test_schema}`.`source_orders`" in ddl
        assert f"`{project.test_schema}`.`ref_orders`" in ddl

        listed = run_dbt(
            [
                "ls",
                "--select",
                "+mv_source_model",
                "--output",
                "json",
                "--output-keys",
                "unique_id",
                "alias",
                "depends_on",
                "config.materialized",
            ]
        )
        listed_by_id = {
            entry["unique_id"]: entry
            for entry in (json.loads(line) for line in listed)
        }
        assert {
            "model.test.ref_orders",
            "model.test.mv_source_model",
            "source.test.mv_source.source_orders",
        }.issubset(listed_by_id)

        mv_listing = listed_by_id["model.test.mv_source_model"]
        assert mv_listing["alias"] == "mv_source_alias"
        assert mv_listing["config.materialized"] == "materialized_view"
        assert set(mv_listing["depends_on"]["nodes"]) == {
            "model.test.ref_orders",
            "source.test.mv_source.source_orders",
        }

        run_dbt(["docs", "generate"])
        manifest = json.loads(
            read_file(project.project_root, "target", "manifest.json")
        )
        manifest_node = manifest["nodes"]["model.test.mv_source_model"]
        assert manifest_node["alias"] == "mv_source_alias"
        assert manifest_node["schema"] == (
            f"{project.test_schema}_mv_custom"
        )
        assert set(manifest_node["depends_on"]["nodes"]) == {
            "model.test.ref_orders",
            "source.test.mv_source.source_orders",
        }
