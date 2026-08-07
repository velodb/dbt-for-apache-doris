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

"""Unit coverage for Doris adapter-specific model configuration."""

from dataclasses import fields

import pytest
from mashumaro.exceptions import InvalidFieldValue

from dbt.adapters.doris.impl import DorisConfig
from dbt.contracts.graph.model_config import NodeConfig


MATERIALIZED_VIEW_CONFIG_FIELDS = {
    "build_mode",
    "refresh_method",
    "refresh_trigger",
    "refresh_schedule",
    "distribution_type",
    "wait_for_refresh",
    "refresh_wait_timeout",
    "refresh_poll_interval",
}


def test_doris_config_registers_materialized_view_fields_with_dbt():
    registered_fields = {field.name for field in fields(DorisConfig)}

    assert MATERIALIZED_VIEW_CONFIG_FIELDS <= registered_fields
    assert {"replication_num", "properties", "partition_by"} <= registered_fields
    assert {"refresh_on_run", "refresh_partitions"}.isdisjoint(
        registered_fields
    )


def test_doris_config_materialized_view_defaults_match_macros():
    config = DorisConfig.from_dict({})

    assert config.build_mode == "immediate"
    assert config.refresh_method == "auto"
    assert config.refresh_trigger == "manual"
    assert config.refresh_schedule is None
    assert config.distribution_type is None
    assert config.wait_for_refresh is True
    assert config.refresh_wait_timeout == 300
    assert config.refresh_poll_interval == 1


def test_doris_config_accepts_complete_materialized_view_configuration():
    values = {
        "duplicate_key": ["order_date"],
        "partition_by": "order_date",
        "distributed_by": ["order_date"],
        "buckets": "auto",
        "properties": {"replication_num": "3"},
        "replication_num": 1,
        "build_mode": "deferred",
        "refresh_method": "complete",
        "refresh_trigger": "schedule",
        "refresh_schedule": {
            "interval": 1,
            "unit": "day",
            "start_time": "2099-08-01 02:00:00",
        },
        "distribution_type": "hash",
        "wait_for_refresh": False,
        "refresh_wait_timeout": 600,
        "refresh_poll_interval": 2,
    }
    config = NodeConfig.from_dict({}).update_from(values, DorisConfig)

    assert config.get("partition_by") == "order_date"
    assert config.get("buckets") == "auto"
    assert config.get("replication_num") == 1
    assert config.get("refresh_schedule") == {
        "interval": 1,
        "unit": "day",
        "start_time": "2099-08-01 02:00:00",
    }
    assert config.get("wait_for_refresh") is False


def test_doris_config_refresh_schedule_must_be_a_mapping():
    with pytest.raises(InvalidFieldValue, match="refresh_schedule"):
        DorisConfig.from_dict({"refresh_schedule": ["every", "day"]})
