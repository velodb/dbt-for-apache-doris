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

"""Functional test for a user-authored dbt unit test on Doris."""

import pytest
from dbt.tests.util import run_dbt


UNIT_TEST_MODEL_SQL = """
select tested_column
from {{ ref('unit_test_upstream') }}
"""

UNIT_TEST_UPSTREAM_SQL = """
select 1 as tested_column
"""

UNIT_TEST_YML = """
version: 2
unit_tests:
  - name: test_unit_test_model
    model: unit_test_model
    given:
      - input: ref('unit_test_upstream')
        rows:
          - {tested_column: 1}
          - {tested_column: 2}
    expect:
      rows:
        - {tested_column: 1}
        - {tested_column: 2}
"""


class TestDorisUserUnitTest:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "unit_test_model.sql": UNIT_TEST_MODEL_SQL,
            "unit_test_upstream.sql": UNIT_TEST_UPSTREAM_SQL,
            "unit_tests.yml": UNIT_TEST_YML,
        }

    def test_user_unit_test_runs(self, project):
        build_results = run_dbt(["run"])
        assert len(build_results) == 2

        project.run_sql(
            f"alter database `{project.test_schema}` set properties "
            '("replication_allocation" = "tag.location.default: 1")'
        )

        unit_test_results = run_dbt(
            ["test", "--select", "test_type:unit"]
        )
        assert len(unit_test_results) == 1
        assert unit_test_results[0].status == "pass"
