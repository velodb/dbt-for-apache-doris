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

from dbt.adapters.doris.impl import DorisAdapter


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
    ]


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
    assert columns[0].get_col_name() == "`order`"
