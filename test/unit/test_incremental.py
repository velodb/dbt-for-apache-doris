#!/usr/bin/env python
# encoding: utf-8

# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements. See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership. The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License. You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied. See the License for the
# specific language governing permissions and limitations
# under the License.

"""Adapter-side coverage needed by incremental schema handling."""

import pytest

from dbt.adapters.doris.column import DorisColumn
from dbt.adapters.doris.impl import DorisAdapter
from dbt.adapters.doris.relation import DorisRelation
from dbt.exceptions import DbtRuntimeError

from .macro_harness import (
    CapturedCompilerError,
    FakeConfig,
    FakeRelation,
    MacroRunner,
)


@pytest.mark.parametrize(
    ("raw_type", "expected"),
    [
        ("VARCHAR(20)", "varchar(20)"),
        ("DECIMAL(18, 4)", "decimal(18,4)"),
        ("CHAR(7)", "CHAR(7)"),
        ("DATETIMEV2(6)", "DATETIMEV2(6)"),
        ("ARRAY<VARCHAR(20)>", "ARRAY<VARCHAR(20)>"),
    ],
)
def test_doris_column_preserves_parameterized_types(raw_type, expected):
    column = DorisColumn.from_description("value", raw_type)
    assert column.data_type == expected


def test_doris_column_widens_with_valid_varchar_syntax():
    target = DorisColumn.from_description("value", "varchar(10)")
    source = DorisColumn.from_description("value", "varchar(40)")

    assert target.can_expand_to(source)
    assert DorisColumn.string_type(source.string_size()) == "varchar(40)"


def test_view_snapshot_ctas_does_not_inherit_new_model_layout():
    sql = MacroRunner(
        "adapters/relation.sql",
        "materializations/table/create_table_as.sql",
        context={
            "adapter": object.__new__(DorisAdapter),
            "config": FakeConfig(
                {
                    "distributed_by": ["missing_from_old_view"],
                    "unique_key": ["missing_from_old_view"],
                    "partition_by": ["missing_from_old_view"],
                    "properties": {"replication_num": "1"},
                }
            ),
        },
    ).render(
        "doris__create_view_snapshot_table",
        FakeRelation(identifier="backup", relation_type="table"),
        FakeRelation(identifier="source", relation_type="view"),
    )

    assert "create table `dbt_test`.`backup`" in sql
    assert '"replication_num" = "1"' in sql
    assert '"enable_duplicate_without_keys_by_default" = "true"' in sql
    assert "as select * from `dbt_test`.`source`;" in sql
    assert "missing_from_old_view" not in sql
    assert "distributed by random buckets auto" in sql.lower()
    assert "distributed by hash" not in sql.lower()


def test_view_snapshot_drops_source_only_after_ctas_succeeds():
    events = []

    class SnapshotAdapter:
        @staticmethod
        def quote(identifier):
            return f"`{identifier}`"

        @staticmethod
        def cache_added(relation):
            events.append(("cache_added", relation.identifier))

        @staticmethod
        def drop_relation(relation):
            events.append(("drop", relation.identifier))

    def run_query(sql):
        events.append(("query", " ".join(sql.split())))

    runner = MacroRunner(
        "adapters/relation.sql",
        "materializations/table/create_table_as.sql",
        context={
            "adapter": SnapshotAdapter(),
            "config": FakeConfig({"properties": {"replication_num": "1"}}),
            "load_cached_relation": lambda relation: None,
            "run_query": run_query,
        },
    )
    runner.render(
        "doris__snapshot_view_to_table",
        FakeRelation(identifier="source", relation_type="view"),
        FakeRelation(identifier="backup", relation_type="table"),
    )

    assert events[0][0] == "query"
    assert "as select * from `dbt_test`.`source`" in events[0][1]
    assert events[1:] == [
        ("cache_added", "backup"),
        ("drop", "source"),
    ]


def test_view_data_snapshot_keeps_source_online():
    events = []

    class SnapshotAdapter:
        @staticmethod
        def quote(identifier):
            return f"`{identifier}`"

        @staticmethod
        def cache_added(relation):
            events.append(("cache_added", relation.identifier))

        @staticmethod
        def drop_relation(relation):
            events.append(("drop", relation.identifier))

    def run_query(sql):
        events.append(("query", " ".join(sql.split())))

    runner = MacroRunner(
        "adapters/relation.sql",
        "materializations/table/create_table_as.sql",
        context={
            "adapter": SnapshotAdapter(),
            "config": FakeConfig({"properties": {"replication_num": "1"}}),
            "load_cached_relation": lambda relation: None,
            "run_query": run_query,
        },
    )
    runner.render(
        "doris__snapshot_view_data_to_table",
        FakeRelation(identifier="source", relation_type="view"),
        FakeRelation(identifier="backup", relation_type="table"),
    )

    assert events[0][0] == "query"
    assert "as select * from `dbt_test`.`source`" in events[0][1]
    assert events[1:] == [("cache_added", "backup")]


def test_view_snapshot_ctas_failure_keeps_source_view():
    dropped = []

    class SnapshotAdapter:
        @staticmethod
        def quote(identifier):
            return f"`{identifier}`"

        @staticmethod
        def cache_added(relation):
            raise AssertionError("failed CTAS must not update the cache")

        @staticmethod
        def drop_relation(relation):
            dropped.append(relation)

    def fail_ctas(sql):
        raise RuntimeError("snapshot failed")

    runner = MacroRunner(
        "adapters/relation.sql",
        "materializations/table/create_table_as.sql",
        context={
            "adapter": SnapshotAdapter(),
            "load_cached_relation": lambda relation: None,
            "run_query": fail_ctas,
        },
    )
    with pytest.raises(RuntimeError, match="snapshot failed"):
        runner.render(
            "doris__snapshot_view_to_table",
            FakeRelation(identifier="source", relation_type="view"),
            FakeRelation(identifier="backup", relation_type="table"),
        )

    assert dropped == []


def test_view_snapshot_rejects_same_source_and_destination_without_side_effects():
    runner = MacroRunner(
        "adapters/relation.sql",
        "materializations/table/create_table_as.sql",
        context={"adapter": object.__new__(DorisAdapter)},
    )

    with pytest.raises(CapturedCompilerError, match="must be different"):
        runner.render(
            "doris__snapshot_view_to_table",
            FakeRelation(identifier="same", relation_type="view"),
            FakeRelation(identifier="same", relation_type="table"),
        )

    assert runner.statements == []


def test_view_snapshot_rejects_existing_destination_without_dropping_it():
    dropped = []
    existing = FakeRelation(identifier="backup", relation_type="table")

    class SnapshotAdapter:
        @staticmethod
        def drop_relation(relation):
            dropped.append(relation)

    runner = MacroRunner(
        "adapters/relation.sql",
        "materializations/table/create_table_as.sql",
        context={
            "adapter": SnapshotAdapter(),
            "load_cached_relation": lambda relation: existing,
        },
    )

    with pytest.raises(CapturedCompilerError, match="must not already exist"):
        runner.render(
            "doris__snapshot_view_to_table",
            FakeRelation(identifier="source", relation_type="view"),
            FakeRelation(identifier="backup", relation_type="table"),
        )

    assert dropped == []
    assert runner.statements == []


def test_rename_view_is_rejected_before_any_statement():
    runner = MacroRunner(
        "adapters/relation.sql",
        context={"adapter": object.__new__(DorisAdapter)},
    )

    with pytest.raises(CapturedCompilerError, match="cannot safely rename"):
        runner.render(
            "doris__rename_relation",
            FakeRelation(relation_type="view"),
            FakeRelation(identifier="backup", relation_type="view"),
        )

    assert runner.statements == []


def test_adapter_rejects_view_rename_before_mutating_cache(monkeypatch):
    adapter = object.__new__(DorisAdapter)
    side_effects = []
    monkeypatch.setattr(
        adapter,
        "cache_renamed",
        lambda *args, **kwargs: side_effects.append("cache"),
    )
    monkeypatch.setattr(
        adapter,
        "execute_macro",
        lambda *args, **kwargs: side_effects.append("macro"),
    )

    with pytest.raises(DbtRuntimeError, match="cannot safely rename a View"):
        adapter.rename_relation(
            FakeRelation(relation_type="view"),
            FakeRelation(identifier="backup", relation_type="view"),
        )

    assert side_effects == []


def test_exchange_views_is_rejected_before_any_statement():
    runner = MacroRunner(
        "adapters/relation.sql",
        context={"adapter": object.__new__(DorisAdapter)},
    )
    with pytest.raises(CapturedCompilerError, match="cannot safely exchange"):
        runner.render(
            "exchange_relation",
            FakeRelation(identifier="first", relation_type="view"),
            FakeRelation(identifier="second", relation_type="view"),
        )

    assert runner.statements == []


def test_schema_change_comparison_is_case_insensitive_for_doris_columns():
    source_relation = DorisRelation.create(
        schema="analytics",
        identifier="source",
    )
    target_relation = DorisRelation.create(
        schema="analytics",
        identifier="target",
    )

    class SchemaAdapter:
        @staticmethod
        def get_columns_in_relation(relation):
            if relation.identifier == "source":
                return [
                    DorisColumn.from_description("ID", "INT"),
                    DorisColumn.from_description("VALUE", "VARCHAR(20)"),
                ]
            return [
                DorisColumn.from_description("id", "INT"),
                DorisColumn.from_description("value", "VARCHAR(20)"),
            ]

    changes = MacroRunner(
        "adapters/columns.sql",
        context={"adapter": SchemaAdapter()},
    ).render(
        "doris__check_for_schema_changes",
        source_relation,
        target_relation,
    )

    assert changes["schema_changed"] is False
    assert changes["source_not_in_target"] == []
    assert changes["target_not_in_source"] == []
    assert changes["new_target_types"] == []


def test_string_widening_matches_doris_columns_case_insensitively(monkeypatch):
    adapter = object.__new__(DorisAdapter)
    source_relation = DorisRelation.create(
        schema="analytics",
        identifier="source",
    )
    target_relation = DorisRelation.create(
        schema="analytics",
        identifier="target",
    )

    def columns(relation):
        if relation.identifier == "source":
            return [DorisColumn.from_description("VALUE", "VARCHAR(40)")]
        return [DorisColumn.from_description("value", "VARCHAR(5)")]

    alterations = []
    monkeypatch.setattr(adapter, "get_columns_in_relation", columns)
    monkeypatch.setattr(
        adapter,
        "alter_column_type",
        lambda relation, column_name, new_type: alterations.append(
            (relation, column_name, new_type)
        ),
    )

    adapter.expand_column_types(source_relation, target_relation)

    assert alterations == [(target_relation, "value", "varchar(40)")]


def test_schema_change_waits_for_finished_job(monkeypatch):
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


def test_schema_change_waits_for_new_job_to_appear(monkeypatch):
    adapter = object.__new__(DorisAdapter)
    relation = DorisRelation.create(schema="analytics", identifier="events")
    jobs = iter(
        [
            {"job_id": "1", "state": "FINISHED", "message": ""},
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


def test_latest_schema_change_job_orders_by_job_id(monkeypatch):
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
    assert "order by JobId desc limit 1" in captured["sql"]


def test_cancelled_schema_change_is_reported(monkeypatch):
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

    with pytest.raises(DbtRuntimeError) as excinfo:
        adapter.wait_for_schema_change(relation, previous_job_id="1")

    assert "invalid type conversion" in str(excinfo.value)


@pytest.mark.parametrize(
    ("job", "expected_message"),
    [
        pytest.param(
            {"job_id": "2", "state": "RUNNING", "message": ""},
            (
                "Timed out after 1 seconds waiting for Doris schema change "
                "job 2 on `analytics`.`events` (state: RUNNING)"
            ),
            id="running-job",
        ),
        pytest.param(
            {"job_id": "1", "state": "FINISHED", "message": ""},
            (
                "Timed out after 1 seconds waiting for a new Doris schema "
                "change job on `analytics`.`events`"
            ),
            id="previous-job-still-visible",
        ),
        pytest.param(
            None,
            (
                "Timed out after 1 seconds waiting for a new Doris schema "
                "change job on `analytics`.`events`"
            ),
            id="new-job-not-visible",
        ),
    ],
)
def test_schema_change_timeout_is_reported(
    monkeypatch,
    job,
    expected_message,
):
    adapter = object.__new__(DorisAdapter)
    relation = DorisRelation.create(schema="analytics", identifier="events")
    monkeypatch.setattr(
        adapter,
        "_latest_schema_change_job",
        lambda _: job,
    )
    ticks = iter([100.0, 101.0])
    monkeypatch.setattr(
        "dbt.adapters.doris.impl.time.monotonic",
        lambda: next(ticks),
    )
    monkeypatch.setattr(
        "dbt.adapters.doris.impl.time.sleep",
        lambda _: pytest.fail("schema-change timeout should not sleep"),
    )

    with pytest.raises(DbtRuntimeError) as excinfo:
        adapter.wait_for_schema_change(
            relation,
            previous_job_id="1",
            timeout_seconds=1,
        )

    assert expected_message in str(excinfo.value)
