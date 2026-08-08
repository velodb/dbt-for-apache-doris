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

"""Compatibility checks for dbt Core 1.12 connection interfaces."""

import inspect
from types import SimpleNamespace
from unittest.mock import Mock

import mysql.connector
import pytest
from dbt.adapters.sql import SQLConnectionManager
from dbt.exceptions import DbtRuntimeError

from dbt.adapters.doris.connections import DorisConnectionManager


def test_add_query_matches_dbt_core_parameters():
    doris_parameters = inspect.signature(DorisConnectionManager.add_query).parameters
    core_parameters = inspect.signature(SQLConnectionManager.add_query).parameters

    assert tuple(doris_parameters) == tuple(core_parameters)
    for name, core_parameter in core_parameters.items():
        assert doris_parameters[name].default == core_parameter.default


def test_add_query_forwards_dbt_core_retry_options(monkeypatch):
    expected_connection = object()
    cursor = SimpleNamespace(with_rows=True)
    calls = []

    def core_add_query(
        self,
        sql,
        auto_begin=True,
        bindings=None,
        abridge_sql_log=False,
        retryable_exceptions=(),
        retry_limit=1,
    ):
        calls.append(
            {
                "sql": sql,
                "auto_begin": auto_begin,
                "bindings": bindings,
                "abridge_sql_log": abridge_sql_log,
                "retryable_exceptions": retryable_exceptions,
                "retry_limit": retry_limit,
            }
        )
        return expected_connection, cursor

    monkeypatch.setattr(SQLConnectionManager, "add_query", core_add_query)
    manager = object.__new__(DorisConnectionManager)

    result = manager.add_query(
        "select 1",
        auto_begin=False,
        bindings={"id": 1},
        abridge_sql_log=True,
        retryable_exceptions=(RuntimeError,),
        retry_limit=3,
    )

    assert result == (expected_connection, cursor)
    assert calls == [
        {
            "sql": "select 1",
            "auto_begin": False,
            "bindings": {"id": 1},
            "abridge_sql_log": True,
            "retryable_exceptions": (RuntimeError,),
            "retry_limit": 3,
        }
    ]


def test_add_query_drains_every_extra_non_select_result(monkeypatch):
    cursor = SimpleNamespace(
        with_rows=False,
        nextset=Mock(side_effect=[True, True, False]),
    )
    monkeypatch.setattr(
        SQLConnectionManager,
        "add_query",
        lambda self, *args, **kwargs: (object(), cursor),
    )
    manager = object.__new__(DorisConnectionManager)

    manager.add_query("alter table example add column value int")

    assert cursor.nextset.call_count == 3


def test_add_query_propagates_an_error_from_a_later_result(monkeypatch):
    cursor = SimpleNamespace(
        with_rows=False,
        nextset=Mock(
            side_effect=mysql.connector.DatabaseError(
                "later statement failed"
            )
        ),
    )
    monkeypatch.setattr(
        SQLConnectionManager,
        "add_query",
        lambda self, *args, **kwargs: (object(), cursor),
    )
    manager = object.__new__(DorisConnectionManager)

    with pytest.raises(DbtRuntimeError, match="later statement failed"):
        manager.add_query("set ok = true; set invalid = true")


def test_add_query_preserves_select_result(monkeypatch):
    cursor = SimpleNamespace(with_rows=True, nextset=Mock())
    monkeypatch.setattr(
        SQLConnectionManager,
        "add_query",
        lambda self, *args, **kwargs: (object(), cursor),
    )
    manager = object.__new__(DorisConnectionManager)

    manager.add_query("select 1")

    cursor.nextset.assert_not_called()
