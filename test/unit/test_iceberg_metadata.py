# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements. See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership. The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License. You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied. See the License for the
# specific language governing permissions and limitations
# under the License.

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from dbt.adapters.doris.impl import DorisAdapter, _table_grants_for_relation
from dbt.adapters.doris.relation import DorisRelation
from dbt_common.clients.agate_helper import table_from_rows
from dbt_common.exceptions import DbtRuntimeError

from .macro_harness import FakeConfig, MacroRunner


@pytest.mark.parametrize("database,expected", [
    (None, "internal"), ("lake", "lake"), ("`lake`", "lake"),
    ("`lake``quoted`", "lake`quoted"),
])
def test_schema_discovery_decodes_core_catalog_quotes_once(database, expected):
    runner = MacroRunner("adapters/metadata.sql", context={
        "adapter": SimpleNamespace(quote=DorisAdapter.quote),
        "load_result": lambda name: SimpleNamespace(table=[]),
    })
    runner.render("doris__list_schemas", database)
    sql = " ".join(runner.statements[0].sql.split())
    assert f"from {DorisAdapter.quote(expected)}.information_schema.schemata" in sql
    assert f"upper('{expected}')" in sql


@pytest.mark.parametrize("macro", ["doris__get_grant_sql", "doris__get_revoke_sql"])
@pytest.mark.parametrize("catalog", [None, "internal", "lake"])
def test_dcl_preserves_real_relation_catalog(macro, catalog):
    relation = DorisRelation.create(database=catalog, schema="analytics", identifier="orders")
    runner = MacroRunner("adapters/grants.sql")
    sql = runner.sql(macro, relation, "select", ["reader"])
    assert f"on {relation}" in sql


def test_native_grants_do_not_mix_same_named_tables_in_other_catalogs():
    relation = DorisRelation.create(database="lake", schema="analytics", identifier="orders")
    table = table_from_rows([[
        "internal.analytics.orders: Select_priv; lake.analytics.orders: Load_priv,Alter_priv; "
        "lake.analytics.other: Drop_priv",
    ]], ["TablePrivs"])
    assert _table_grants_for_relation(table, relation) == {"insert", "alter"}
    internal = relation.incorporate(path={"database": None})
    assert _table_grants_for_relation(table, internal) == {"select"}


def test_grants_api_only_reconciles_principals_on_exact_target():
    relation = DorisRelation.create(database="lake", schema="analytics", identifier="orders")
    candidates = table_from_rows([
        ["reader", "select"], ["reader", "insert"], ["shadow", "select"],
    ], ["grantee", "privilege_type"])
    reader = table_from_rows([["lake.analytics.orders: Select_priv,Load_priv"]], ["TablePrivs"])
    shadow = table_from_rows([["internal.analytics.orders: Select_priv"]], ["TablePrivs"])
    adapter = object.__new__(DorisAdapter)
    adapter.execute_macro = Mock(side_effect=["candidates", "'reader'@'%'", "'shadow'@'%'"])
    adapter.execute = Mock(side_effect=[(None, candidates), (None, reader), (None, shadow)])
    assert adapter.get_relation_grants(relation) == {"insert": ["reader"], "select": ["reader"]}
    assert adapter.execute.call_count == 3


def test_missing_native_privilege_columns_fail_instead_of_reconciling_empty():
    relation = DorisRelation.create(schema="analytics", identifier="orders")
    with pytest.raises(DbtRuntimeError, match="cannot reconcile"):
        _table_grants_for_relation(table_from_rows([], ["unexpected"]), relation)


def test_show_grant_sql_keeps_core_result_contract():
    relation = DorisRelation.create(database="lake", schema="analytics", identifier="orders")
    runner = MacroRunner("adapters/grants.sql", context={
        "execute": True,
        "adapter": SimpleNamespace(get_relation_grants=lambda relation: {"select": ["reader"]}),
        "config": FakeConfig(),
    })
    sql = runner.sql("doris__get_show_grant_sql", relation)
    assert "select 'reader' as grantee" in sql
    assert "'select' as privilege_type" in sql
