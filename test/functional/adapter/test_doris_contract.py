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

"""Functional tests for Doris model contracts."""

import pytest
from dbt.tests.util import relation_from_name, run_dbt, run_dbt_and_capture


CONTRACT_MODEL_SQL = """
{{ config(
    materialized='table',
    contract={'enforced': true},
    duplicate_key=['id'],
    distributed_by=['id'],
    properties={'replication_num': '1'}
) }}

select cast(1 as int) as id, cast('alice' as varchar(20)) as name
"""

CONTRACT_SCHEMA_YML = """
version: 2
models:
  - name: contract_model
    config:
      contract:
        enforced: true
    columns:
      - name: id
        data_type: int
      - name: name
        data_type: varchar(20)
"""

CONTRACT_MISMATCH_SQL = """
{{ config(
    materialized='table',
    contract={'enforced': true},
    duplicate_key=['id'],
    distributed_by=['id'],
    properties={'replication_num': '1'}
) }}

select
    cast(1 as int) as id,
    cast('alice' as varchar(20)) as name,
    cast('not_declared' as varchar(20)) as unexpected
"""

CONTRACT_MISMATCH_SCHEMA_YML = """
version: 2
models:
  - name: contract_mismatch
    config:
      contract:
        enforced: true
    columns:
      - name: id
        data_type: int
      - name: name
        data_type: varchar(20)
"""


class TestDorisModelContract:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "contract_model.sql": CONTRACT_MODEL_SQL,
            "schema.yml": CONTRACT_SCHEMA_YML,
        }

    def test_enforced_contract_builds_declared_column_set(self, project):
        results = run_dbt(["run"])
        assert len(results) == 1

        relation = relation_from_name(project.adapter, "contract_model")
        columns = project.run_sql(
            f"describe {relation}",
            fetch="all",
        )
        assert [column[0] for column in columns] == ["id", "name"]

        row = project.run_sql(
            f"select id, name from {relation}",
            fetch="one",
        )
        assert row == (1, "alice")


class TestDorisModelContractMismatch:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "contract_mismatch.sql": CONTRACT_MISMATCH_SQL,
            "schema.yml": CONTRACT_MISMATCH_SCHEMA_YML,
        }

    def test_enforced_contract_rejects_undeclared_columns(self, project):
        results, output = run_dbt_and_capture(["run"], expect_pass=False)

        assert len(results) == 1
        assert results[0].status == "error"
        assert "unexpected" in output
        assert "missing in contract" in output.lower()
