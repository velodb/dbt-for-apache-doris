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
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

"""End-to-end Source Freshness coverage against Doris."""

import pytest
from dbt.tests.adapter.utils.test_current_timestamp import (
    BaseCurrentTimestampNaive,
)
from dbt.tests.util import relation_from_name, run_dbt


FRESHNESS_SEED_CSV = """id,loaded_at,is_valid
1,2099-01-01 00:00:00,1
2,2099-01-01 00:00:00,0
"""

LOADED_AT_FIELD_SOURCE_YML = """
version: 2
sources:
  - name: raw
    schema: "{{ target.schema }}"
    tables:
      - name: freshness_events
        config:
          loaded_at_field: loaded_at
          freshness:
            warn_after:
              count: 1
              period: hour
            error_after:
              count: 2
              period: hour
"""

FILTERED_SOURCE_YML = """
version: 2
sources:
  - name: raw
    schema: "{{ target.schema }}"
    tables:
      - name: freshness_events
        config:
          loaded_at_field: loaded_at
          freshness:
            filter: is_valid = 1
            warn_after:
              count: 1
              period: hour
            error_after:
              count: 2
              period: hour
"""

LOADED_AT_QUERY_SOURCE_YML = """
version: 2
sources:
  - name: raw
    schema: "{{ target.schema }}"
    tables:
      - name: freshness_events
        config:
          loaded_at_query: |
            select max(loaded_at) from {{ this }} where is_valid = 1
          freshness:
            warn_after:
              count: 1
              period: hour
            error_after:
              count: 2
              period: hour
"""


class DorisFreshnessFixtures:
    source_yml = LOADED_AT_FIELD_SOURCE_YML

    @pytest.fixture(scope="class")
    def seeds(self):
        return {"freshness_events.csv": FRESHNESS_SEED_CSV}

    @pytest.fixture(scope="class")
    def models(self):
        return {"sources.yml": self.source_yml}

    @pytest.fixture(scope="class")
    def project_config_update(self):
        return {
            "seeds": {
                "+column_types": {
                    "id": "int",
                    "loaded_at": "datetime",
                    "is_valid": "boolean",
                }
            }
        }

    @staticmethod
    def seed_relation(project):
        assert len(run_dbt(["seed", "--full-refresh"])) == 1
        return relation_from_name(project.adapter, "freshness_events")

    @classmethod
    def replace_rows(cls, project, valid_age_minutes, invalid_age_minutes=None):
        relation = cls.seed_relation(project)
        if invalid_age_minutes is None:
            invalid_age_minutes = valid_age_minutes
        project.run_sql(f"truncate table {relation}")
        project.run_sql(
            f"insert into {relation} "
            "select 1, "
            f"utc_timestamp() - interval {valid_age_minutes} minute, true "
            "union all select 2, "
            f"utc_timestamp() - interval {invalid_age_minutes} minute, false"
        )
        return relation


class TestDorisCurrentTimestamp(BaseCurrentTimestampNaive):
    """dbt requires its current_timestamp macro to return UTC."""


class TestDorisLoadedAtFieldFreshness(DorisFreshnessFixtures):
    @pytest.mark.parametrize(
        "age_minutes,expected_status,expect_pass",
        [(30, "pass", True), (90, "warn", True), (180, "error", False)],
    )
    def test_pass_warn_and_error_thresholds(
        self,
        project,
        age_minutes,
        expected_status,
        expect_pass,
    ):
        self.replace_rows(project, age_minutes)

        results = run_dbt(["source", "freshness"], expect_pass=expect_pass)

        assert len(results) == 1
        assert str(results[0].status) == expected_status
        assert abs(results[0].age - (age_minutes * 60)) < 15


class TestDorisFilteredFreshness(DorisFreshnessFixtures):
    source_yml = FILTERED_SOURCE_YML

    def test_filter_excludes_newer_invalid_rows(self, project):
        self.replace_rows(project, valid_age_minutes=90, invalid_age_minutes=5)

        results = run_dbt(["source", "freshness"])

        assert len(results) == 1
        assert str(results[0].status) == "warn"
        assert abs(results[0].age - (90 * 60)) < 15


class TestDorisLoadedAtQueryFreshness(DorisFreshnessFixtures):
    source_yml = LOADED_AT_QUERY_SOURCE_YML

    def test_loaded_at_query_is_rendered_and_collected(self, project):
        self.replace_rows(project, valid_age_minutes=90, invalid_age_minutes=5)

        results = run_dbt(["source", "freshness"])

        assert len(results) == 1
        assert str(results[0].status) == "warn"
        assert abs(results[0].age - (90 * 60)) < 15
