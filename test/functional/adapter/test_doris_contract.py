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
from dbt.tests.adapter.constraints.test_constraints import (
    BaseIncrementalConstraintsColumnsEqual,
    BaseTableConstraintsColumnsEqual,
    BaseViewConstraintsColumnsEqual,
)
from dbt.tests.util import (
    relation_from_name,
    run_dbt,
    run_dbt_and_capture,
    write_file,
)


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

QUOTED_CONTRACT_SQL = """
{{ config(materialized='table', contract={'enforced': true}) }}

select cast(1 as bigint) as `order`
"""

QUOTED_CONTRACT_SCHEMA_YML = """
version: 2
models:
  - name: quoted_contract
    config:
      contract:
        enforced: true
    columns:
      - name: order
        quote: true
        data_type: bigint
"""

SAFE_VIEW_SQL = """
{{ config(materialized='view', contract={'enforced': true}) }}

select cast(1 as int) as id
"""

BROKEN_SAFE_VIEW_SQL = """
{{ config(materialized='view', contract={'enforced': true}) }}

select cast(2 as int) as id, cast('unexpected' as text) as extra_column
"""

SAFE_VIEW_SCHEMA_YML = """
version: 2
models:
  - name: safe_contract_view
    config:
      contract:
        enforced: true
    columns:
      - name: id
        data_type: int
"""

OFFICIAL_CONTRACT_SCHEMA_YML = """
version: 2
models:
  - name: my_model_wrong_order
    config:
      contract:
        enforced: true
    columns:
      - name: id
        data_type: int
      - name: color
        data_type: varchar(20)
      - name: date_day
        data_type: date
  - name: my_model_wrong_name
    config:
      contract:
        enforced: true
    columns:
      - name: id
        data_type: int
      - name: color
        data_type: varchar(20)
      - name: date_day
        data_type: date
"""


def contract_model_sql(materialized, id_column="id"):
    incremental_strategy = (
        ", incremental_strategy='append', on_schema_change='append_new_columns'"
        if materialized == "incremental"
        else ""
    )
    return f"""
{{{{ config(materialized='{materialized}'{incremental_strategy}) }}}}

select
    cast('blue' as varchar(20)) as color,
    cast(1 as int) as {id_column},
    cast('2019-01-01' as date) as date_day
"""


class DorisContractTypes:
    """Doris type cases for dbt-tests-adapter's contract acceptance tests."""

    @pytest.fixture
    def string_type(self):
        return "text"

    @pytest.fixture
    def int_type(self):
        return "int"

    @pytest.fixture
    def data_types(self):
        return [
            ["cast(1 as int)", "int", "INT"],
            ["cast(1 as bigint)", "bigint", "BIGINT"],
            ["cast('one' as text)", "text", "TEXT"],
            ["cast(1.25 as decimal(10, 2))", "decimal(10, 2)", "DECIMAL"],
            ["cast(true as boolean)", "boolean", "TINYINT"],
            ["cast('2019-01-01' as date)", "date", "DATE"],
        ]


class DorisContractModels:
    materialized = "table"

    @pytest.fixture(scope="class")
    def models(self):
        return {
            "my_model_wrong_order.sql": contract_model_sql(self.materialized),
            "my_model_wrong_name.sql": contract_model_sql(self.materialized, "error"),
            "constraints_schema.yml": OFFICIAL_CONTRACT_SCHEMA_YML,
        }


class TestDorisTableContractCompatibility(
    DorisContractTypes,
    DorisContractModels,
    BaseTableConstraintsColumnsEqual,
):
    pass


class TestDorisViewContractCompatibility(
    DorisContractTypes,
    DorisContractModels,
    BaseViewConstraintsColumnsEqual,
):
    materialized = "view"


class TestDorisIncrementalContractCompatibility(
    DorisContractTypes,
    DorisContractModels,
    BaseIncrementalConstraintsColumnsEqual,
):
    materialized = "incremental"


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


class TestDorisQuotedModelContract:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "quoted_contract.sql": QUOTED_CONTRACT_SQL,
            "schema.yml": QUOTED_CONTRACT_SCHEMA_YML,
        }

    def test_reserved_column_name_builds_with_quote_true(self, project):
        results = run_dbt(["run"])
        assert len(results) == 1

        relation = relation_from_name(project.adapter, "quoted_contract")
        row = project.run_sql(f"select `order` from {relation}", fetch="one")
        assert row == (1,)


class TestDorisViewContractFailureSafety:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "safe_contract_view.sql": SAFE_VIEW_SQL,
            "schema.yml": SAFE_VIEW_SCHEMA_YML,
        }

    def test_failed_contract_keeps_existing_view(self, project):
        assert len(run_dbt(["run"])) == 1
        relation = relation_from_name(project.adapter, "safe_contract_view")
        assert project.run_sql(f"select id from {relation}", fetch="one") == (1,)

        write_file(BROKEN_SAFE_VIEW_SQL, "models", "safe_contract_view.sql")
        results = run_dbt(["run"], expect_pass=False)
        assert len(results) == 1
        assert results[0].status == "error"

        assert project.run_sql(f"select id from {relation}", fetch="one") == (1,)
