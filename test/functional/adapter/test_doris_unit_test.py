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

"""Functional coverage for user-authored dbt unit tests on Doris."""

import pytest
from dbt.tests.adapter.unit_testing.test_case_insensitivity import (
    BaseUnitTestCaseInsensivity,
)
from dbt.tests.adapter.unit_testing.test_invalid_input import (
    BaseUnitTestInvalidInput,
)
from dbt.tests.adapter.unit_testing.test_quoted_reserved_word_column_names import (
    BaseUnitTestQuotedReservedWordColumnNames,
)
from dbt.tests.adapter.unit_testing.test_types import (
    BaseUnitTestingTypes,
    BaseUnitTestingVarcharFixtureNoTruncation,
)
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


class DorisUnitTesting:
    """Apply the configured replica count to dbt's temporary unit-test tables."""

    @pytest.fixture(autouse=True)
    def configure_unit_test_replication(
        self,
        project,
        doris_test_replication_num,
    ):
        project.run_sql(
            f"alter database `{project.test_schema}` set properties "
            f'("replication_allocation" = "tag.location.default: '
            f'{doris_test_replication_num}")'
        )


class TestDorisUserUnitTest(DorisUnitTesting):
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

        unit_test_results = run_dbt(
            ["test", "--select", "test_type:unit"]
        )
        assert len(unit_test_results) == 1
        assert unit_test_results[0].status == "pass"


class TestDorisUnitTestCaseInsensitivity(
    DorisUnitTesting,
    BaseUnitTestCaseInsensivity,
):
    pass


class TestDorisUnitTestInvalidInput(
    DorisUnitTesting,
    BaseUnitTestInvalidInput,
):
    pass


class TestDorisUnitTestQuotedReservedWordColumnNames(
    DorisUnitTesting,
    BaseUnitTestQuotedReservedWordColumnNames,
):
    pass


class TestDorisUnitTestingTypes(
    DorisUnitTesting,
    BaseUnitTestingTypes,
):
    @pytest.fixture
    def data_types(self):
        # The upstream fixture uses PostgreSQL-only TIMESTAMPTZ and :: casts.
        # Exercise the same scalar paths with expressions accepted by Doris.
        return [
            ["1", "1"],
            ["'1'", "1"],
            ["2.5", "2.5"],
            ["'string value'", "string value"],
            ["true", "true"],
            ["DATE '2020-01-02'", "2020-01-02"],
            [
                "TIMESTAMP '2013-11-03 00:00:00-0'",
                "2013-11-03 00:00:00-0",
            ],
            ["cast('7.77' as decimal(10, 2))", "7.77"],
        ]


class TestDorisUnitTestingVarcharFixtureNoTruncation(
    DorisUnitTesting,
    BaseUnitTestingVarcharFixtureNoTruncation,
):
    pass
