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

"""Compatibility checks for dbt Core 1.12 adapter class interfaces."""

import inspect

import pytest

from dbt.adapters.capability import Capability
from dbt.adapters.doris.impl import DorisAdapter
from dbt.adapters.doris.relation import DorisRelation
from dbt_common.clients.agate_helper import table_from_rows
from dbt_common.exceptions import DbtRuntimeError


def test_class_level_adapter_methods_remain_classmethods():
    for method_name in ("quote", "_catalog_filter_table"):
        descriptor = inspect.getattr_static(DorisAdapter, method_name)
        assert isinstance(descriptor, classmethod)


def test_quote_can_be_called_by_dbt_at_class_level():
    assert DorisAdapter.quote("order") == "`order`"


def test_incremental_strategy_allowlist_excludes_delete_insert():
    adapter = object.__new__(DorisAdapter)
    assert adapter.valid_incremental_strategies() == [
        "append",
        "merge",
        "insert_overwrite",
        "microbatch",
    ]


def test_microbatch_batches_remain_sequential():
    assert not DorisAdapter.supports(Capability.MicrobatchConcurrency)


def test_quoted_contract_column_renders_without_an_adapter_instance():
    columns = DorisAdapter.render_raw_columns_constraints(
        {
            "order": {
                "name": "order",
                "quote": True,
                "data_type": "bigint",
                "description": "order id",
            }
        }
    )

    assert len(columns) == 1
    assert columns[0].get_col_name() == "order"
    assert columns[0].get_table_column_constraint() == (
        "cast(`order` as bigint) as `order`"
    )


def test_catalog_preserves_empty_doris_database_for_manifest_matching():
    column_names = [
        "table_database",
        "table_schema",
        "table_name",
        "table_type",
        "table_comment",
        "table_owner",
        "column_name",
        "column_index",
        "column_type",
        "column_comment",
    ]
    table = table_from_rows(
        [["", "analytics", "orders", "table", "orders docs", None, "id", 1, "int", "id docs"]],
        column_names,
        text_only_columns=[name for name in column_names if name != "column_index"],
    )

    filtered = DorisAdapter._catalog_filter_table(
        table,
        frozenset({("", "analytics")}),
    )

    assert len(filtered.rows) == 1
    assert filtered.rows[0]["table_database"] == ""


def test_schema_change_waits_until_the_new_job_finishes(monkeypatch):
    adapter = object.__new__(DorisAdapter)
    relation = DorisRelation.create(schema="analytics", identifier="events")
    jobs = iter(
        [
            {"job_id": "2", "state": "RUNNING", "message": ""},
            {"job_id": "2", "state": "FINISHED", "message": ""},
        ]
    )
    monkeypatch.setattr(adapter, "_latest_schema_change_job", lambda _: next(jobs))
    sleeps = []
    monkeypatch.setattr(
        "dbt.adapters.doris.impl.time.sleep",
        lambda seconds: sleeps.append(seconds),
    )

    adapter.wait_for_schema_change(relation, previous_job_id="1")

    assert sleeps == [0.2]


def test_schema_change_cancelled_job_is_reported(monkeypatch):
    adapter = object.__new__(DorisAdapter)
    relation = DorisRelation.create(schema="analytics", identifier="events")
    monkeypatch.setattr(
        adapter,
        "_latest_schema_change_job",
        lambda _: {
            "job_id": "2",
            "state": "CANCELLED",
            "message": "invalid type conversion",
        },
    )

    with pytest.raises(DbtRuntimeError, match="invalid type conversion"):
        adapter.wait_for_schema_change(relation, previous_job_id="1")


def test_latest_schema_change_job_uses_newest_table_job(monkeypatch):
    adapter = object.__new__(DorisAdapter)
    relation = DorisRelation.create(schema="analytics", identifier="events")
    captured = {}

    class Result:
        rows = []

    def execute(sql, auto_begin, fetch):
        captured["sql"] = sql
        return None, Result()

    monkeypatch.setattr(adapter, "execute", execute)

    assert adapter._latest_schema_change_job(relation) is None
    assert "where TableName = 'events'" in captured["sql"]
    assert "order by JobId desc limit 1" in captured["sql"]
