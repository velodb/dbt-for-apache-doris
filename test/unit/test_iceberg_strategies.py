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
from dbt.adapters.doris.impl import DorisAdapter, _iceberg_partition_clause
from dbt_common.clients.agate_helper import table_from_rows
from dbt_common.exceptions import DbtRuntimeError

from .macro_harness import CapturedCompilerError, FakeColumn, FakeConfig, FakeRelation, MacroRunner


def runner():
    return MacroRunner(
        "adapters/metadata.sql", "adapters/relation.sql",
        "materializations/incremental/help.sql", "materializations/incremental/strategies.sql",
        context={"execute": True, "model": {"database": "lake", "unique_id": "model.test.iceberg"},
                 "adapter": SimpleNamespace(quote=DorisAdapter.quote, get_catalog_type=lambda name: "iceberg"),
                 "config": FakeConfig()},
    )


def test_native_merge_uses_null_safe_keys_and_both_actions():
    args = {"target_relation": FakeRelation(database="lake"), "temp_relation": FakeRelation(database="lake"),
            "unique_key": ["id", "part"], "dest_columns": [FakeColumn(x) for x in ["id", "part", "value"]]}
    sql = runner().sql("doris__get_incremental_merge_sql", args)
    assert sql.startswith("merge into")
    assert sql.count("<=>") == 2
    assert "when matched then update set" in sql
    assert "when not matched then insert" in sql
    assert "DBT_INTERNAL_DUPLICATE_KEYS" in sql


def test_catalog_type_is_cached_per_model_not_parsed_config():
    r = runner()
    calls = []

    def lookup(name):
        calls.append(name)
        return "iceberg"
    r.context["adapter"].get_catalog_type = lookup
    target = FakeRelation(database="lake")
    assert r.render("doris__is_iceberg_catalog", target)
    assert r.render("doris__is_iceberg_catalog", target)
    assert calls == ["lake"]
    assert r.context["config"].values == {}


@pytest.mark.parametrize("ddl,expected", [
    ("CREATE TABLE t(id int) ENGINE=ICEBERG_EXTERNAL_TABLE", ""),
    ('CREATE TABLE t(id int COMMENT "PARTITION BY LIST(fake)") PARTITION BY LIST (`id`) ()',
     "PARTITION BY LIST (`id`) ()"),
    ('CREATE TABLE t(id int) PARTITION BY LIST(day(`ts`), bucket(16,`id`)) ()',
     "PARTITION BY LIST(day(`ts`), bucket(16,`id`)) ()"),
    ('CREATE TABLE t(`paren)` int) PARTITION BY LIST (`paren)`) ()',
     "PARTITION BY LIST (`paren)`) ()"),
])
def test_clone_partition_parser_preserves_transforms_and_ignores_literals(ddl, expected):
    assert _iceberg_partition_clause(ddl) == expected


def test_invalid_partition_clause_fails_without_losing_layout():
    with pytest.raises(DbtRuntimeError, match="Unterminated"):
        _iceberg_partition_clause("CREATE TABLE t(id int) PARTITION BY LIST(day(ts)")


@pytest.mark.parametrize("value,literal", [(None, "null"), (True, "true"), (7, "7"), ("O'Reilly", "'O\\'Reilly'")])
def test_static_partition_values_are_literals(value, literal):
    assert runner().render("doris__iceberg_partition_values", {"dt": value}) == {"dt": literal}


@pytest.mark.parametrize("scope", [[], ["p1"], {}, {"dt": []}, {"dt": float("nan")}])
def test_invalid_static_partition_scope_fails_before_sql(scope):
    with pytest.raises(CapturedCompilerError):
        runner().render("doris__iceberg_partition_values", scope)


def test_catalog_type_lookup_uses_visible_catalog_name():
    adapter = object.__new__(DorisAdapter)
    adapter.execute = Mock(return_value=(None, table_from_rows([["lake", "iceberg"]], ["CatalogName", "Type"])))
    assert adapter.get_catalog_type("lake") == "iceberg"


def test_null_partition_replacement_inserts_partition_columns_and_uses_null_safe_scope():
    columns = [SimpleNamespace(name=name, data_type="int" if name == "id" else "string")
               for name in ["id", "dt", "region", "value"]]
    args = {"target_relation": FakeRelation(database="lake"), "temp_relation": FakeRelation(database="lake"),
            "dest_columns": columns, "overwrite_partitions": {"dt": None, "region": "A"}}
    sql = runner().sql("doris__get_incremental_insert_overwrite_sql", args)
    assert sql.startswith("merge into")
    assert "DBT_INTERNAL_DEST.`dt` <=> null" in sql
    assert "DBT_INTERNAL_DEST.`region` <=> 'A'" in sql
    assert "insert (`id`, `dt`, `region`, `value`)" in sql
    assert "partition (" not in sql


def test_whole_replacement_has_null_and_non_null_delete_buckets_with_collision_free_marker():
    columns = [SimpleNamespace(name=name, data_type="int")
               for name in ["id", "DBT_INTERNAL_BATCH_OPERATION_0"]]
    args = {"target_relation": FakeRelation(database="lake"), "temp_relation": FakeRelation(database="lake"),
            "dest_columns": columns}
    sql = runner().sql("doris__get_iceberg_replace_sql", args)
    assert "DBT_INTERNAL_DEST.`id` is not null" in sql
    assert "else 'N'" in sql
    assert "'N' as `DBT_INTERNAL_BATCH_OPERATION_1`" in sql
    assert "'D' as `DBT_INTERNAL_BATCH_OPERATION_1`" in sql
    assert "when matched then delete" in sql
    assert "when not matched and DBT_INTERNAL_SOURCE.`DBT_INTERNAL_BATCH_OPERATION_1`='I'" in sql
