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

"""Functional tests for loaded_at_field source freshness."""

import pytest
from dbt.tests.util import run_dbt


FRESHNESS_SEED_CSV = """id,loaded_at
1,2099-01-01 00:00:00
"""

FRESHNESS_SOURCE_YML = """
version: 2
sources:
  - name: raw
    schema: "{{ target.schema }}"
    tables:
      - name: freshness_events
        loaded_at_field: loaded_at
        freshness:
          warn_after:
            count: 1
            period: day
          error_after:
            count: 2
            period: day
"""


class TestDorisLoadedAtFieldFreshness:
    @pytest.fixture(scope="class")
    def seeds(self):
        return {"freshness_events.csv": FRESHNESS_SEED_CSV}

    @pytest.fixture(scope="class")
    def models(self):
        return {"sources.yml": FRESHNESS_SOURCE_YML}

    @pytest.fixture(scope="class")
    def project_config_update(self):
        return {
            "seeds": {
                "test": {
                    "freshness_events": {
                        "column_types": {
                            "id": "int",
                            "loaded_at": "datetime",
                        }
                    }
                }
            }
        }

    def test_loaded_at_field_freshness_passes(self, project):
        seed_results = run_dbt(["seed"])
        assert len(seed_results) == 1

        freshness_results = run_dbt(["source", "freshness"])
        assert len(freshness_results) == 1
        assert str(freshness_results[0].status) == "pass"
