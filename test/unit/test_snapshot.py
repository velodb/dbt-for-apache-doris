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

"""Failure-safety and type-compatibility guards for Snapshot macros."""

from types import SimpleNamespace

import pytest

from .macro_harness import FakeRelation, MacroRunner, read_macro_file


SNAPSHOT_MACROS = ("materializations/snapshot/snapshot.sql",)
STRATEGY_MACROS = ("materializations/snapshot/strategies.sql",)


def column(data_type, char_size=None, precision=None, scale=None):
    return SimpleNamespace(
        expanded_data_type=data_type,
        char_size=char_size,
        numeric_precision=precision,
        numeric_scale=scale,
    )


@pytest.mark.parametrize(
    ("source", "target", "expected"),
    [
        (column("tinyint"), column("bigint"), True),
        (column("bigint"), column("int"), False),
        (column("varchar(20)", 20), column("varchar(40)", 40), True),
        (column("varchar(40)", 40), column("varchar(20)", 20), False),
        (
            column("character varying(256)", 256),
            column("varchar(20)", 20),
            True,
        ),
        (column("date"), column("datetime(6)"), True),
        (column("datetime(6)"), column("datetime"), False),
        (
            column("decimal(10,2)", precision=10, scale=2),
            column("decimal(18,4)", precision=18, scale=4),
            True,
        ),
        (
            column("decimal(18,4)", precision=18, scale=4),
            column("decimal(10,2)", precision=10, scale=2),
            False,
        ),
        (column("int"), column("varchar(20)", 20), False),
    ],
)
def test_snapshot_type_compatibility_only_allows_safe_writes(
    source, target, expected
):
    runner = MacroRunner(*SNAPSHOT_MACROS)

    assert runner.render("doris__snapshot_type_can_write", source, target) is expected


def test_snapshot_helper_relations_are_tables_with_reserved_names():
    runner = MacroRunner(*SNAPSHOT_MACROS)
    target = FakeRelation(identifier="customer_history")

    upsert = runner.render("doris__snapshot_upsert_relation", target)
    initial = runner.render("doris__snapshot_initial_relation", target)

    assert upsert.identifier == "customer_history__snapshot_upsert"
    assert upsert.type == "table"
    assert initial.identifier == "customer_history__snapshot_initial"
    assert initial.type == "table"


def test_snapshot_merge_never_drops_existing_target_before_exchange():
    source = read_macro_file(SNAPSHOT_MACROS[0])

    assert "exchange_relation(target, upsert, true)" in source
    assert "drop table if exists {{ target }}" not in source
    assert "alter table {{ upsert }} rename" not in source


def test_check_strategy_hash_adds_nonce_only_for_adapter_clock():
    runner = MacroRunner(*STRATEGY_MACROS)

    check_hash = runner.sql(
        "doris__snapshot_hash_arguments",
        ["customer_id", "current_timestamp()"],
    )
    timestamp_hash = runner.sql(
        "doris__snapshot_hash_arguments",
        ["customer_id", "updated_at"],
    )

    assert "uuid()" in check_hash
    assert "uuid()" not in timestamp_hash
