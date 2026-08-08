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

"""Doris relation namespace behavior."""

from datetime import datetime, timedelta, timezone

import pytest

from dbt.adapters.base.relation import EventTimeFilter
from dbt.adapters.cache import RelationsCache
from dbt.adapters.contracts.relation import RelationType
from dbt.adapters.doris.relation import DorisRelation


@pytest.mark.parametrize(
    ("database", "schema", "expected_schema"),
    [
        ("", "analytics", "analytics"),
        (None, "analytics", "analytics"),
        ("analytics", "analytics", "analytics"),
        ("finance", "analytics", "finance"),
    ],
)
def test_relation_normalizes_doris_database_to_one_cache_namespace(
    database,
    schema,
    expected_schema,
):
    relation = DorisRelation.create(
        database=database,
        schema=schema,
        identifier="orders",
    )

    assert relation.database is None
    assert relation.schema == expected_schema


def test_materialized_view_to_view_replacement_updates_one_cache_key():
    cache = RelationsCache()
    materialized_view = DorisRelation.create(
        database="",
        schema="analytics",
        identifier="daily_sales",
        type=RelationType.MaterializedView,
    )
    view = materialized_view.incorporate(type=RelationType.View)

    cache.add(materialized_view)
    assert cache.get_relations(None, "analytics") == [materialized_view]

    cache.drop(materialized_view)
    cache.add(view)

    cached_relations = cache.get_relations(None, "analytics")
    assert len(cached_relations) == 1
    assert cached_relations[0].type == RelationType.View


def test_event_time_filter_renders_utc_as_naive_doris_datetime():
    relation = DorisRelation.create(
        schema="analytics",
        identifier="events",
        event_time_filter=EventTimeFilter(
            field_name="event_time",
            start=datetime(
                2026,
                8,
                4,
                8,
                0,
                tzinfo=timezone(timedelta(hours=8)),
            ),
            end=datetime(
                2026,
                8,
                5,
                8,
                0,
                tzinfo=timezone(timedelta(hours=8)),
            ),
        ),
    )

    rendered = str(relation)

    assert "event_time >= '2026-08-04 00:00:00'" in rendered
    assert "event_time < '2026-08-05 00:00:00'" in rendered
    assert "+00:00" not in rendered
