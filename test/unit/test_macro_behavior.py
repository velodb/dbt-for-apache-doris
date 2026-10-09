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

"""Regression tests for macro behaviour, rendered without a Doris cluster.

Each test here pins a bug that shipped in a released adapter. The functional
suite covers the same ground end to end but needs a cluster, so it cannot run on
a pull request; these can.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from .macro_harness import (
    CapturedCompilerError,
    FakeAdapter,
    FakeColumn,
    FakeConfig,
    FakeRelation,
    MacroRunner,
)

TABLE_MACROS = ("materializations/table/create_table_as.sql", "adapters/relation.sql")

CREATE_TABLE_MACROS = ("doris__create_table_as", "doris__create_unique_table_as")


def unit_testing_runner(model=None):
    return MacroRunner(
        "adapters/unit_testing.sql",
        context={
            "model": model or {"resource_type": "model", "name": "upstream"},
            "safe_cast": lambda value, data_type: f"cast({value} as {data_type})",
            "dbt": {
                "string_literal": lambda value: f"'{value}'",
                "escape_single_quotes": lambda value: value.replace("'", "''"),
            },
        },
    )


def statement_count(sql):
    """Number of SQL statements in a rendered string.

    A trailing semicolon is one statement, not two.
    """
    return len([part for part in sql.split(";") if part.strip()])


def table_runner(config=None, model=None, columns_sql="`id` int"):
    return MacroRunner(
        *TABLE_MACROS,
        context={
            "adapter": FakeAdapter(),
            "config": FakeConfig(config or {"unique_key": ["id"]}),
            "model": model or {},
            # Provided by dbt-core when a contract is enforced.
            "get_assert_columns_equivalent": lambda sql: "",
            "get_table_columns_and_constraints": lambda: columns_sql,
        },
    )


def test_current_timestamp_is_utc():
    runner = MacroRunner("adapters/freshness.sql")

    assert runner.sql("doris__current_timestamp") == "utc_timestamp()"


class TestUnitTestingFixtures:
    def test_string_fixture_widens_bounded_varchar(self):
        formatted = unit_testing_runner().render(
            "format_row",
            {"name": "longer_string_value"},
            {"name": "varchar(5)"},
        )

        assert formatted == {"name": "cast('longer_string_value' as varchar)"}

    def test_non_string_fixture_preserves_bounded_varchar(self):
        formatted = unit_testing_runner().render(
            "format_row",
            {"name": 123},
            {"name": "varchar(5)"},
        )

        assert formatted == {"name": "cast(123 as varchar(5))"}

    def test_invalid_fixture_column_raises_compiler_error(self):
        with pytest.raises(CapturedCompilerError, match="Invalid column name"):
            unit_testing_runner().render(
                "format_row",
                {"missing": "value"},
                {"name": "varchar(5)"},
            )


class TestGrants:
    def runner(self):
        return MacroRunner("adapters/grants.sql")

    def test_show_grants_uses_doris_table_privileges(self):
        sql = self.runner().sql(
            "doris__get_grant_candidates_sql",
            FakeRelation(schema="analytics", identifier="orders"),
        )

        assert "from `internal`.information_schema.table_privileges" in sql
        assert "table_schema = 'analytics'" in sql
        assert "table_name = 'orders'" in sql
        assert "as grantee" in sql
        assert "as privilege_type" in sql

    @pytest.mark.parametrize(
        "privilege,doris_privilege",
        [
            ("select", "SELECT_PRIV"),
            ("insert", "LOAD_PRIV"),
            ("alter", "ALTER_PRIV"),
            ("create", "CREATE_PRIV"),
            ("drop", "DROP_PRIV"),
            ("show_view", "SHOW_VIEW_PRIV"),
        ],
    )
    def test_grant_maps_dbt_privileges(self, privilege, doris_privilege):
        sql = self.runner().sql(
            "doris__get_grant_sql",
            FakeRelation(),
            privilege,
            ["analyst"],
        )

        assert sql == (
            f"grant {doris_privilege} on `dbt_test`.`my_model` "
            "to 'analyst'@'%'"
        )

    def test_revoke_supports_an_explicit_user_host(self):
        sql = self.runner().sql(
            "doris__get_revoke_sql",
            FakeRelation(),
            "select",
            ["analyst@10.%"],
        )

        assert sql.endswith("from 'analyst'@'10.%'")

    @pytest.mark.parametrize(
        "macro,args,message",
        [
            (
                "doris__grant_privilege",
                ("execute",),
                "Unsupported Doris grant privilege",
            ),
            (
                "doris__grant_user_identity",
                ("role:analyst",),
                "role grants cannot be reconciled",
            ),
        ],
    )
    def test_unsupported_grants_fail_before_dcl(self, macro, args, message):
        with pytest.raises(CapturedCompilerError, match=message):
            self.runner().render(macro, *args)

    def test_each_dcl_statement_runs_separately(self):
        runner = self.runner()

        runner.render(
            "doris__call_dcl_statements",
            ["grant SELECT_PRIV on db.table to user1", "revoke LOAD_PRIV on db.table from user2"],
        )

        assert [statement.name for statement in runner.statements] == [
            "grant_1",
            "grant_2",
        ]
        assert len(runner.statements) == 2


class TestCreateTableEngine:
    """Let Doris infer omitted engines and keep OLAP DDL out of lake tables."""

    @pytest.mark.parametrize("macro", CREATE_TABLE_MACROS)
    @pytest.mark.parametrize("engine", ["iceberg", "ICEBERG", "Iceberg"])
    def test_explicit_iceberg_engine_is_emitted(self, macro, engine):
        sql = table_runner(config={"engine": engine, "unique_key": ["id"]}).sql(
            macro,
            False,
            FakeRelation(database="iceberg_catalog"),
            "select cast(1 as int) as id",
        )

        assert f"ENGINE = {engine}" in sql

    @pytest.mark.parametrize("macro", CREATE_TABLE_MACROS)
    @pytest.mark.parametrize("database", [None, "internal", "iceberg_catalog"])
    def test_omitted_engine_is_inferred_by_doris(self, macro, database):
        sql = table_runner(config={"unique_key": ["id"]}).sql(
            macro,
            False,
            FakeRelation(database=database),
            "select cast(1 as int) as id",
        )

        assert "ENGINE" not in sql.upper()

    @pytest.mark.parametrize("engine", [None, "iceberg", "ICEBERG"])
    def test_external_unique_ctas_has_no_olap_key_or_default_property(self, engine):
        config = {"unique_key": ["id"]}
        if engine is not None:
            config["engine"] = engine
        sql = table_runner(config=config).sql(
            "doris__create_unique_table_as",
            False,
            FakeRelation(database="iceberg_catalog"),
            "select cast(1 as int) as id",
        )

        assert "UNIQUE KEY" not in sql.upper()
        assert "enable_unique_key_merge_on_write" not in sql

    @pytest.mark.parametrize("database", [None, "internal", "INTERNAL"])
    @pytest.mark.parametrize("engine", [None, "OLAP", "olap"])
    def test_internal_unique_ctas_keeps_olap_layout(self, database, engine):
        config = {"unique_key": ["id"], "distributed_by": ["id"]}
        if engine is not None:
            config["engine"] = engine
        sql = table_runner(config=config).sql(
            "doris__create_unique_table_as",
            False,
            FakeRelation(database=database),
            "select cast(1 as int) as id",
        )

        assert "UNIQUE KEY ( `id` )" in sql
        assert "DISTRIBUTED BY HASH ( `id` )" in sql
        assert '"enable_unique_key_merge_on_write" = "true"' in sql

    @pytest.mark.parametrize("macro", CREATE_TABLE_MACROS)
    @pytest.mark.parametrize("engine", ["OLAP", "olap"])
    def test_explicit_olap_engine_in_external_catalog_is_not_rewritten(
        self, macro, engine
    ):
        sql = table_runner(config={"engine": engine, "unique_key": ["id"]}).sql(
            macro,
            False,
            FakeRelation(database="iceberg_catalog"),
            "select cast(1 as int) as id",
        )

        assert f"ENGINE = {engine}" in sql

    @pytest.mark.parametrize("macro", CREATE_TABLE_MACROS)
    @pytest.mark.parametrize("engine", [None, "iceberg"])
    def test_explicit_external_distribution_is_not_silently_dropped(
        self, macro, engine
    ):
        config = {"unique_key": ["id"], "distributed_by": ["id"], "buckets": 8}
        if engine is not None:
            config["engine"] = engine
        sql = table_runner(config=config).sql(
            macro,
            False,
            FakeRelation(database="iceberg_catalog"),
            "select cast(1 as int) as id",
        )

        assert "DISTRIBUTED BY HASH ( `id` ) BUCKETS 8" in sql

    @pytest.mark.parametrize("macro", CREATE_TABLE_MACROS)
    def test_explicit_external_duplicate_key_is_not_silently_dropped(self, macro):
        sql = table_runner(
            config={
                "engine": "iceberg",
                "unique_key": ["id"],
                "duplicate_key": ["id"],
            }
        ).sql(
            macro,
            False,
            FakeRelation(database="iceberg_catalog"),
            "select cast(1 as int) as id",
        )

        assert "DUPLICATE KEY ( `id` )" in sql
        assert "UNIQUE KEY" not in sql.upper()

    @pytest.mark.parametrize(
        "macro",
        [
            "doris__create_incremental_staging_table",
            "doris__create_view_snapshot_table",
        ],
    )
    @pytest.mark.parametrize("engine", [None, "iceberg", "ICEBERG"])
    def test_external_physical_helpers_do_not_add_olap_defaults(self, macro, engine):
        config = {"unique_key": ["id"]}
        if engine is not None:
            config["engine"] = engine
        source = (
            "select cast(1 as int) as id"
            if macro == "doris__create_incremental_staging_table"
            else FakeRelation(database="iceberg_catalog", identifier="source_view")
        )
        sql = table_runner(config=config).sql(
            macro,
            FakeRelation(database="iceberg_catalog", identifier="target__dbt_tmp"),
            source,
        )

        assert "DISTRIBUTED BY" not in sql.upper()
        assert "enable_duplicate_without_keys_by_default" not in sql
        if engine is None:
            assert "ENGINE" not in sql.upper()
        else:
            assert f"ENGINE = {engine}" in sql

    @pytest.mark.parametrize("engine", [None, "iceberg"])
    @pytest.mark.parametrize("unique", [False, True])
    def test_external_documented_table_and_source_do_not_add_olap_defaults(
        self, engine, unique
    ):
        class DocumentedAdapter(FakeAdapter):
            @staticmethod
            def get_columns_in_relation(relation):
                return [SimpleNamespace(name="id", data_type="INT")]

        config = {"unique_key": ["id"], "persist_docs": {"columns": True}}
        if engine is not None:
            config["engine"] = engine
        runner = table_runner(
            config=config,
            model={"columns": {"id": {"description": "Identifier"}}},
        )
        runner.context["adapter"] = DocumentedAdapter()
        runner.render(
            "doris__create_documented_table_as",
            False,
            FakeRelation(database="iceberg_catalog"),
            "select cast(1 as int) as id",
            unique=unique,
        )
        creates = [
            statement.sql
            for statement in runner.statements
            if statement.name
            in {"create_documented_table_source", "create_documented_table"}
        ]

        assert len(creates) == 2
        for sql in creates:
            assert "UNIQUE KEY" not in sql.upper()
            assert "DISTRIBUTED BY" not in sql.upper()
            assert "enable_unique_key_merge_on_write" not in sql
            assert "enable_duplicate_without_keys_by_default" not in sql
            if engine is None:
                assert "ENGINE" not in sql.upper()
            else:
                assert f"ENGINE = {engine}" in sql
        assert "COMMENT 'Identifier'" in creates[1]


class TestExchangeRelation:
    """External replacement preserves the old table; OLAP keeps its atomic swap."""

    @staticmethod
    def runner(engine=None):
        config = {} if engine is None else {"engine": engine}
        return MacroRunner(
            "adapters/relation.sql",
            context={
                "config": FakeConfig(config),
                "make_backup_relation": lambda relation, relation_type: relation.incorporate(
                    path={"identifier": relation.identifier + "__dbt_backup"},
                    type=relation_type,
                ),
            },
        )

    @staticmethod
    def statements(runner):
        return [
            (statement.name, " ".join(statement.sql.split()))
            for statement in runner.statements
        ]

    @pytest.mark.parametrize("engine", [None, "iceberg", "ICEBERG"])
    @pytest.mark.parametrize("drop_target", [False, True])
    def test_external_replacement_preserves_target_until_publish(
        self, engine, drop_target
    ):
        runner = self.runner(engine)
        target = FakeRelation(database="iceberg_catalog", identifier="orders")
        source = FakeRelation(database="iceberg_catalog", identifier="orders__dbt_tmp")

        runner.render("exchange_relation", target, source, drop_target)

        expected = [
            (
                "rename_relation",
                "alter table `iceberg_catalog`.`dbt_test`.`orders` rename `orders__dbt_backup`",
            ),
            (
                "rename_relation",
                "alter table `iceberg_catalog`.`dbt_test`.`orders__dbt_tmp` rename `orders`",
            ),
        ]
        if drop_target:
            expected.append(
                (
                    "drop_relation",
                    "drop table if exists `iceberg_catalog`.`dbt_test`.`orders__dbt_backup`",
                )
            )
        else:
            expected.append(
                (
                    "rename_relation",
                    "alter table `iceberg_catalog`.`dbt_test`.`orders__dbt_backup` "
                    "rename `orders__dbt_tmp`",
                )
            )
        assert self.statements(runner) == expected

    @pytest.mark.parametrize("drop_destination", [None, False, True])
    def test_rename_destination_drop_is_optional(self, drop_destination):
        runner = self.runner()
        target = FakeRelation(database="iceberg_catalog", identifier="orders")
        source = FakeRelation(database="iceberg_catalog", identifier="orders__dbt_tmp")
        options = (
            {} if drop_destination is None else {"drop_destination": drop_destination}
        )

        runner.render("doris__rename_relation", source, target, **options)

        expected = []
        if drop_destination is not False:
            expected.append(
                (
                    "drop_relation",
                    "drop table if exists `iceberg_catalog`.`dbt_test`.`orders`",
                )
            )
        expected.append(
            (
                "rename_relation",
                "alter table `iceberg_catalog`.`dbt_test`.`orders__dbt_tmp` rename `orders`",
            )
        )
        assert self.statements(runner) == expected

    @pytest.mark.parametrize("database", [None, "internal", "INTERNAL"])
    @pytest.mark.parametrize("engine", [None, "OLAP", "olap"])
    @pytest.mark.parametrize("drop_target", [False, True])
    def test_olap_keeps_atomic_replace(self, database, engine, drop_target):
        runner = self.runner(engine)
        target = FakeRelation(database=database, identifier="orders")
        source = FakeRelation(database=database, identifier="orders__dbt_tmp")

        runner.render("exchange_relation", target, source, drop_target)

        assert self.statements(runner) == [
            (
                "exchange_relation",
                f"ALTER TABLE {target} REPLACE WITH TABLE `orders__dbt_tmp` "
                f"PROPERTIES('swap' = '{not drop_target}');",
            )
        ]

    @pytest.mark.parametrize(
        "source",
        [
            FakeRelation(database="other_catalog", identifier="orders__dbt_tmp"),
            FakeRelation(
                database="iceberg_catalog",
                schema="other_schema",
                identifier="orders__dbt_tmp",
            ),
        ],
    )
    def test_external_cross_namespace_is_rejected_before_writes(self, source):
        runner = self.runner()
        target = FakeRelation(database="iceberg_catalog", identifier="orders")

        with pytest.raises(CapturedCompilerError, match="(?i)catalog|schema"):
            runner.render("exchange_relation", target, source, True)

        assert runner.statements == []

    def test_external_self_replacement_is_rejected_before_writes(self):
        runner = self.runner()
        target = FakeRelation(database="iceberg_catalog", identifier="orders")
        source = FakeRelation(database="iceberg_catalog", identifier="orders")

        with pytest.raises(CapturedCompilerError, match="(?i)distinct|different"):
            runner.render("exchange_relation", target, source, True)

        assert runner.statements == []

    def test_external_backup_name_collision_is_rejected_before_writes(self):
        runner = self.runner()
        target = FakeRelation(database="iceberg_catalog", identifier="orders")
        source = FakeRelation(
            database="iceberg_catalog", identifier="orders__dbt_backup"
        )

        with pytest.raises(CapturedCompilerError, match="(?i)backup"):
            runner.render("exchange_relation", target, source, True)

        assert runner.statements == []

    @pytest.mark.parametrize("drop_target", [False, True])
    def test_failed_publish_does_not_drop_backup(self, drop_target):
        runner = self.runner()
        target = FakeRelation(database="iceberg_catalog", identifier="orders")
        source = FakeRelation(database="iceberg_catalog", identifier="orders__dbt_tmp")
        capture = runner.context["statement"]

        def fail_publish(name=None, caller=None, **kwargs):
            rendered_sql = caller()
            capture(name, caller=lambda: rendered_sql, **kwargs)
            if "orders__dbt_tmp` rename" in rendered_sql:
                raise RuntimeError("publish failed")
            return ""

        runner.context["statement"] = fail_publish
        with pytest.raises(RuntimeError, match="publish failed"):
            runner.render("exchange_relation", target, source, drop_target)

        assert [statement.name for statement in runner.statements] == [
            "rename_relation",
            "rename_relation",
        ]
        assert "`orders` rename `orders__dbt_backup`" in runner.statements[0].sql

    @pytest.mark.parametrize("target_name", ["select", "order details", "order`daily"])
    def test_external_rename_quotes_target_identifier(self, target_name):
        runner = self.runner()
        target = FakeRelation(database="iceberg_catalog", identifier=target_name)
        source = FakeRelation(database="iceberg_catalog", identifier="orders__dbt_tmp")

        runner.render("exchange_relation", target, source, True)

        expected_identifier = "`" + target_name.replace("`", "``") + "`"
        assert runner.statements[1].sql.endswith("rename " + expected_identifier)

    @pytest.mark.parametrize("engine", [None, "OLAP", "iceberg"])
    @pytest.mark.parametrize("view_position", ["target", "source"])
    def test_views_are_rejected_before_writes(self, engine, view_position):
        runner = self.runner(engine)
        target = FakeRelation(
            database="iceberg_catalog",
            identifier="orders",
            relation_type="view" if view_position == "target" else "table",
        )
        source = FakeRelation(
            database="iceberg_catalog",
            identifier="orders__dbt_tmp",
            relation_type="view" if view_position == "source" else "table",
        )

        with pytest.raises(CapturedCompilerError, match="Views"):
            runner.render("exchange_relation", target, source, True)

        assert runner.statements == []


class TestSeedEngine:
    @staticmethod
    def runner(database, config):
        class SeedAdapter(FakeAdapter):
            @staticmethod
            def convert_type(agate_table, column_index):
                return "INT"

            @staticmethod
            def quote_seed_column(column_name, quote_columns):
                return f"`{column_name}`"

        return MacroRunner(
            "materializations/seed/helpers.sql",
            "adapters/relation.sql",
            context={
                "adapter": SeedAdapter(),
                "config": FakeConfig(config),
                "this": FakeRelation(database=database),
            },
        )

    @pytest.mark.parametrize("engine", [None, "iceberg", "ICEBERG"])
    def test_external_seed_does_not_add_engine_or_distribution_defaults(self, engine):
        config = {} if engine is None else {"engine": engine}
        sql = self.runner("iceberg_catalog", config).sql(
            "doris__create_csv_table",
            {"config": {}},
            SimpleNamespace(column_names=["id"]),
        )

        assert "DISTRIBUTED BY" not in sql.upper()
        if engine is None:
            assert "ENGINE" not in sql.upper()
        else:
            assert f"ENGINE = {engine}" in sql

    @pytest.mark.parametrize("database", [None, "internal", "INTERNAL"])
    @pytest.mark.parametrize("engine", [None, "OLAP", "olap"])
    def test_internal_seed_keeps_default_hash_distribution(self, database, engine):
        config = {} if engine is None else {"engine": engine}
        sql = self.runner(database, config).sql(
            "doris__create_csv_table",
            {"config": {}},
            SimpleNamespace(column_names=["id"]),
        )

        assert "DISTRIBUTED BY HASH ( `id` ) BUCKETS 10" in sql
        if engine is None:
            assert "ENGINE" not in sql.upper()
        else:
            assert f"ENGINE = {engine}" in sql

    @pytest.mark.parametrize(
        "target_catalog,stage_catalog,expects_distribution",
        [
            ("internal", "iceberg_catalog", False),
            ("iceberg_catalog", "internal", True),
        ],
    )
    def test_seed_table_creation_uses_explicit_relation(
        self, target_catalog, stage_catalog, expects_distribution
    ):
        runner = self.runner(target_catalog, {})
        stage = FakeRelation(database=stage_catalog, identifier="seed__dbt_tmp")

        sql = runner.sql(
            "doris__create_csv_table",
            {"config": {}},
            SimpleNamespace(column_names=["id"]),
            stage,
        )

        assert sql.startswith(f"create table {stage.render()} (")
        assert "my_model" not in sql
        assert ("DISTRIBUTED BY HASH" in sql) is expects_distribution
        assert len(runner.statements) == 1
        assert " ".join(runner.statements[0].sql.split()) == sql

    def test_seed_table_creation_preserves_two_argument_call(self):
        runner = self.runner("iceberg_catalog", {"engine": "iceberg"})

        sql = runner.sql(
            "doris__create_csv_table",
            {"config": {"column_types": {"id": "BIGINT"}}},
            SimpleNamespace(column_names=["id"]),
        )

        assert sql.startswith(f"create table {runner.context['this'].render()} (")
        assert "`id` BIGINT" in sql
        assert "ENGINE = iceberg" in sql

    def test_empty_seed_uses_existing_types_by_normalized_column_name(self):
        runner = self.runner("iceberg_catalog", {})
        inferred_columns = []

        def infer_type(table, index):
            inferred_columns.append(table.column_names[index])
            return "BOOLEAN"

        runner.context["adapter"].convert_type = infer_type
        stage = FakeRelation(database="iceberg_catalog", identifier="seed__dbt_tmp")

        sql = runner.sql(
            "doris__create_csv_table",
            {"config": {}},
            SimpleNamespace(column_names=["ID", "Label", "new_flag"], rows=[]),
            stage,
            inferred_column_types={"id": "BIGINT", "label": "STRING"},
        )

        assert "`ID` BIGINT" in sql
        assert "`Label` STRING" in sql
        assert "`new_flag` BOOLEAN" in sql
        assert inferred_columns == ["new_flag"]

    def test_explicit_seed_types_override_empty_seed_fallback(self):
        runner = self.runner("iceberg_catalog", {})

        def unexpected_inference(table, index):
            raise AssertionError("An existing empty seed column must retain its type")

        runner.context["adapter"].convert_type = unexpected_inference

        sql = runner.sql(
            "doris__create_csv_table",
            {"config": {"column_types": {"id": "INT", "Label": "VARCHAR(40)"}}},
            SimpleNamespace(column_names=["id", "Label"], rows=[]),
            FakeRelation(database="iceberg_catalog", identifier="seed__dbt_tmp"),
            inferred_column_types={"id": "BIGINT", "label": "STRING"},
        )

        assert "`id` INT" in sql
        assert "`Label` VARCHAR(40)" in sql


class TestSeedCsvLoading:
    @staticmethod
    def runner(batch_size=2, binding_char="%s"):
        queries = []

        class SeedAdapter(FakeAdapter):
            @staticmethod
            def quote_seed_column(column_name, quote_columns):
                if quote_columns is False:
                    return column_name
                return "`" + column_name.replace("`", "``") + "`"

            @staticmethod
            def add_query(sql, bindings, abridge_sql_log):
                queries.append((" ".join(sql.split()), bindings, abridge_sql_log))

        adapter = SeedAdapter()
        runner = MacroRunner(
            "materializations/seed/helpers.sql",
            context={
                "adapter": adapter,
                "this": FakeRelation(database="iceberg_catalog", identifier="seed"),
                "get_batch_size": lambda: batch_size,
                "get_binding_char": lambda: binding_char,
                "get_seed_column_quoted_csv": lambda model, names: ", ".join(
                    adapter.quote_seed_column(
                        name, model["config"].get("quote_columns")
                    )
                    for name in names
                ),
            },
        )
        return runner, queries

    def test_batches_bind_all_values_and_only_write_to_stage(self):
        runner, queries = self.runner(batch_size=2)
        stage = FakeRelation(database="iceberg_catalog", identifier="seed__dbt_tmp")
        table = SimpleNamespace(
            column_names=["id", "label", "nullable"],
            rows=[
                (1, "O'Reilly", None),
                (2, "path\\value", ""),
                (3, "snow\u2603", False),
            ],
        )

        first_batch_sql = runner.sql(
            "doris__load_csv_rows_into_relation", {"config": {}}, table, stage
        )

        assert len(queries) == 2
        assert [query[1] for query in queries] == [
            [1, "O'Reilly", None, 2, "path\\value", ""],
            [3, "snow\u2603", False],
        ]
        assert [query[0].count("%s") for query in queries] == [6, 3]
        assert all(query[2] is True for query in queries)
        assert all(query[0].startswith(f"insert into {stage.render()} (") for query in queries)
        assert all("`seed`" not in query[0] for query in queries)
        assert all("O'Reilly" not in query[0] for query in queries)
        assert all("path\\value" not in query[0] for query in queries)
        assert first_batch_sql == queries[0][0]

    def test_empty_csv_returns_empty_sql_without_queries(self):
        runner, queries = self.runner()

        sql = runner.render(
            "doris__load_csv_rows_into_relation",
            {"config": {}},
            SimpleNamespace(column_names=["id", "label"], rows=[]),
            FakeRelation(database="iceberg_catalog", identifier="seed__dbt_tmp"),
        )

        assert sql == ""
        assert queries == []

    def test_csv_column_names_are_quoted_without_touching_bound_data(self):
        runner, queries = self.runner()

        runner.render(
            "doris__load_csv_rows_into_relation",
            {"config": {}},
            SimpleNamespace(
                column_names=["select", "order details", "tick`name"],
                rows=[("a'quoted", None, "value")],
            ),
            FakeRelation(database="iceberg_catalog", identifier="seed__dbt_tmp"),
        )

        assert "(`select`, `order details`, `tick``name`)" in queries[0][0]
        assert queries[0][1] == ["a'quoted", None, "value"]

    def test_quote_columns_false_and_binding_char_are_respected(self):
        runner, queries = self.runner(binding_char="?")

        runner.render(
            "doris__load_csv_rows_into_relation",
            {"config": {"quote_columns": False}},
            SimpleNamespace(column_names=["id", "label"], rows=[(1, "one")]),
            FakeRelation(database="iceberg_catalog", identifier="seed__dbt_tmp"),
        )

        assert "(id,label) values (?,?)" in queries[0][0].replace(", ", ",")
        assert queries[0][1] == [1, "one"]

    def test_later_batch_failure_propagates_without_target_write(self):
        runner, queries = self.runner(batch_size=1)
        capture = runner.context["adapter"].add_query

        def fail_second_batch(sql, bindings, abridge_sql_log):
            capture(sql, bindings, abridge_sql_log)
            if len(queries) == 2:
                raise RuntimeError("CSV batch rejected")

        runner.context["adapter"].add_query = fail_second_batch
        stage = FakeRelation(database="iceberg_catalog", identifier="seed__dbt_tmp")
        with pytest.raises(RuntimeError, match="CSV batch rejected"):
            runner.render(
                "doris__load_csv_rows_into_relation",
                {"config": {}},
                SimpleNamespace(column_names=["id"], rows=[(1,), (2,), (3,)]),
                stage,
            )

        assert len(queries) == 2
        assert all(query[0].startswith(f"insert into {stage.render()} (") for query in queries)


class TestSingleStatementDDL:
    """dbt sends one statement per `execute()`; the connector cannot take two.

    Two semicolon-separated statements in one call leave unconsumed result sets
    on the connection, and the *next* statement fails with
    `2014 (HY000) Commands out of sync`. The visible failure lands on whatever
    ran afterwards -- usually temp-table cleanup, which then leaks a
    `__dbt_tmp` table.
    """

    @pytest.mark.parametrize("macro", CREATE_TABLE_MACROS)
    @pytest.mark.parametrize("temporary", [True, False])
    def test_create_table_as_emits_one_statement(self, macro, temporary):
        runner = table_runner()
        sql = runner.sql(macro, temporary, FakeRelation(), "select 1 as id")
        assert statement_count(sql) == 1, f"{macro} emitted more than one statement: {sql}"

    @pytest.mark.parametrize("macro", CREATE_TABLE_MACROS)
    def test_create_table_as_does_not_drop(self, macro):
        """The drop belongs to the caller.

        `create_table_as` used to prepend `drop table if exists` for temporary
        relations, which is what made it a two-statement macro.
        """
        runner = table_runner()
        sql = runner.sql(macro, True, FakeRelation(), "select 1 as id")
        assert "drop" not in sql.lower(), f"{macro} still drops the relation: {sql}"
        assert runner.statements == [], (
            f"{macro} must return SQL, not execute statements of its own: " f"{runner.statements}"
        )

    @pytest.mark.parametrize("macro", CREATE_TABLE_MACROS)
    def test_create_table_as_keeps_external_catalog(self, macro):
        runner = table_runner()
        relation = FakeRelation(database="hive_catalog", schema="ods")

        sql = runner.sql(macro, False, relation, "select 1 as id")

        assert "create table `hive_catalog`.`ods`.`my_model`" in sql

    def test_unique_table_defaults_to_merge_on_write(self):
        sql = table_runner().sql(
            "doris__create_unique_table_as",
            False,
            FakeRelation(),
            "select 1 as id",
        )
        assert '"enable_unique_key_merge_on_write" = "true"' in sql

    def test_key_and_distribution_columns_are_quoted(self):
        sql = table_runner(
            config={
                "unique_key": ["order"],
                "distributed_by": ["order"],
            }
        ).sql(
            "doris__create_unique_table_as",
            False,
            FakeRelation(),
            "select 1 as `order`",
        )

        assert "UNIQUE KEY ( `order` )" in sql
        assert "DISTRIBUTED BY HASH ( `order` )" in sql

    def test_explicit_merge_on_read_property_overrides_default(self):
        properties = {
            "replication_num": "1",
            "enable_unique_key_merge_on_write": "false",
        }
        sql = table_runner(config={"unique_key": ["id"], "properties": properties}).sql(
            "doris__create_unique_table_as",
            False,
            FakeRelation(),
            "select 1 as id",
        )
        assert '"enable_unique_key_merge_on_write" = "false"' in sql
        assert '"enable_unique_key_merge_on_write" = "true"' not in sql
        assert properties == {
            "replication_num": "1",
            "enable_unique_key_merge_on_write": "false",
        }


class TestMetadataMacros:
    def test_get_columns_escapes_identifier_literal(self):
        class Result:
            table = []

        runner = MacroRunner(
            "adapters/columns.sql",
            context={
                "adapter": FakeAdapter(),
                "api": type("Api", (), {"Column": object}),
                "load_result": lambda name: Result(),
            },
        )
        runner.render(
            "doris__get_columns_in_relation",
            FakeRelation(
                database="hive_catalog",
                schema="ods\\archive",
                identifier="orders'2026\\daily",
            ),
        )

        assert len(runner.statements) == 1
        sql = runner.statements[0].sql
        assert "table_name = 'orders\\'2026\\\\daily'" in sql

    def test_incremental_staging_preserves_replication_allocation(self):
        sql = table_runner(
            config={
                "distributed_by": ["id"],
                "properties": {
                    "replication_allocation": "tag.location.default: 1",
                }
            }
        ).sql(
            "doris__create_incremental_staging_table",
            FakeRelation(identifier="target__dbt_tmp"),
            "select 1 as id",
        )

        assert '"enable_duplicate_without_keys_by_default" = "true"' in sql
        assert '"replication_allocation" = "tag.location.default: 1"' in sql
        assert '"replication_num"' not in sql
        assert "distributed by random buckets auto" in sql.lower()
        assert "distributed by hash" not in sql.lower()

    def test_incremental_staging_prefers_top_level_replication_num(self):
        sql = table_runner(
            config={
                "replication_num": "1",
                "properties": {
                    "replication_num": "2",
                    "replication_allocation": "tag.location.default: 3",
                },
            }
        ).sql(
            "doris__create_incremental_staging_table",
            FakeRelation(identifier="target__dbt_tmp"),
            "select 1 as id",
        )

        assert '"enable_duplicate_without_keys_by_default" = "true"' in sql
        assert '"replication_num" = "1"' in sql
        assert '"replication_allocation"' not in sql

    def test_incremental_staging_is_keyless_for_non_keyable_first_column(self):
        sql = table_runner(
            config={
                "duplicate_key": ["id"],
                "distributed_by": ["id"],
                "replication_num": "1",
            }
        ).sql(
            "doris__create_incremental_staging_table",
            FakeRelation(identifier="target__dbt_tmp"),
            "select cast(1.5 as double) as measure, 1 as id",
        )

        assert "distributed by random buckets auto" in sql.lower()
        assert "distributed by hash" not in sql.lower()
        assert "duplicate key" not in sql.lower()
        assert '"enable_duplicate_without_keys_by_default" = "true"' in sql
        assert sql.count("select cast(1.5 as double) as measure, 1 as id") == 1


class TestContractProjection:
    """`columns:` in schema.yml is documentation unless the contract is enforced.

    The DDL used to always project the declared column list, so a model with a
    partially documented schema silently dropped every undeclared column from
    the target table -- data loss reported as a successful run.
    """

    @pytest.mark.parametrize("macro", CREATE_TABLE_MACROS)
    def test_unenforced_contract_passes_sql_through(self, macro):
        runner = table_runner(config={"unique_key": ["id"]})
        sql = runner.sql(macro, False, FakeRelation(), "select 1 as id, 2 as undocumented")
        assert sql.rstrip(";").endswith("as select 1 as id, 2 as undocumented"), sql
        assert "_table_colume_type_name" not in sql, (
            "the declared column list must not be projected without an enforced "
            f"contract: {sql}"
        )

    @pytest.mark.parametrize("macro", CREATE_TABLE_MACROS)
    def test_enforced_contract_projects_declared_columns(self, macro):
        class Contract:
            enforced = True

        runner = table_runner(
            config={"unique_key": ["id"], "contract": Contract()},
            columns_sql="`id` int",
        )
        sql = runner.sql(macro, False, FakeRelation(), "select 1 as id")
        assert (
            "_table_colume_type_name" in sql
        ), f"an enforced contract should cast to the declared types: {sql}"


class TestExternalViewTargets:
    @pytest.mark.parametrize("database", ["iceberg_catalog", "other_external_catalog"])
    @pytest.mark.parametrize("engine", [None, "iceberg", "OLAP"])
    def test_create_view_rejects_external_catalog_before_contract_validation(
        self, database, engine
    ):
        def unexpected_contract_validation(sql):
            raise AssertionError("External Views must fail before validating their query")

        runner = MacroRunner(
            "materializations/view/create_view_as.sql",
            context={
                "config": FakeConfig({
                    "engine": engine,
                    "contract": SimpleNamespace(enforced=True),
                    "sql_header": "set enable_insert_strict=true;",
                }),
                "get_assert_columns_equivalent": unexpected_contract_validation,
            },
        )

        with pytest.raises(CapturedCompilerError, match="View in external Catalog"):
            runner.render(
                "doris__create_view_as",
                FakeRelation(database=database, relation_type="view"),
                "select 1 as id",
            )
        assert runner.statements == []

    @pytest.mark.parametrize("database", [None, "internal", "INTERNAL"])
    @pytest.mark.parametrize("engine", [None, "iceberg"])
    def test_internal_view_target_is_independent_of_table_engine(self, database, engine):
        runner = MacroRunner(
            "materializations/view/create_view_as.sql",
            context={"config": FakeConfig({"engine": engine})},
        )
        relation = FakeRelation(database=database, relation_type="view")

        sql = runner.sql("doris__create_view_as", relation, "select 1 as id")

        assert f"create or replace view {relation}" in sql
        assert "ENGINE" not in sql

    @pytest.mark.parametrize("engine", [None, "iceberg", "OLAP"])
    def test_materialization_rejects_external_catalog_before_hooks(self, engine):
        def unexpected_hook(*args, **kwargs):
            raise AssertionError("External View rejection must happen before hooks")

        target = FakeRelation(database="iceberg_catalog", relation_type="view")
        runner = MacroRunner(
            "materializations/view/view.sql",
            "materializations/view/create_view_as.sql",
            context={
                "this": target,
                "adapter": FakeAdapter(),
                "config": FakeConfig({"engine": engine}),
                "load_cached_relation": lambda relation: relation.incorporate(type="table"),
                "run_hooks": unexpected_hook,
                "pre_hooks": [],
            },
        )

        with pytest.raises(CapturedCompilerError, match="View in external Catalog"):
            runner.render("materialization_view_doris")
        assert runner.statements == []


class TestViewContractValidation:
    def test_enforced_contract_runs_preflight_before_create(self):
        class Contract:
            enforced = True

        validated = []
        runner = MacroRunner(
            "materializations/view/create_view_as.sql",
            context={
                "config": FakeConfig({"contract": Contract()}),
                "get_assert_columns_equivalent": validated.append,
            },
        )

        sql = runner.sql(
            "doris__create_view_as",
            FakeRelation(relation_type="view"),
            "select 1 as id",
        )

        assert validated == ["select 1 as id"]
        assert "create or replace view" in sql.lower()

    def test_unenforced_view_skips_contract_preflight(self):
        validated = []
        runner = MacroRunner(
            "materializations/view/create_view_as.sql",
            context={
                "config": FakeConfig(),
                "get_assert_columns_equivalent": validated.append,
            },
        )

        runner.sql(
            "doris__create_view_as",
            FakeRelation(relation_type="view"),
            "select 1 as id",
        )

        assert validated == []


@pytest.mark.parametrize(
    "database,engine,is_olap",
    [
        ("iceberg_catalog", None, False),
        ("iceberg_catalog", "iceberg", False),
        ("iceberg_catalog", "ICEBERG", False),
        (None, None, True),
        ("internal", None, True),
        ("internal", "OLAP", True),
    ],
)
class TestIncrementalSchemaChange:
    """Iceberg ALTER is synchronous; OLAP retains schema-job ordering."""

    def runner(self, database, engine, ddl_error=None):
        events = []

        class SchemaAdapter(FakeAdapter):
            @staticmethod
            def get_latest_schema_change_job_id(relation):
                events.append(("latest_job", relation))
                return "42"

            @staticmethod
            def wait_for_schema_change(relation, previous_job_id):
                events.append(("wait", relation, previous_job_id))

        config = {} if engine is None else {"engine": engine}
        runner = MacroRunner(
            *TABLE_MACROS,
            "adapters/columns.sql",
            context={"adapter": SchemaAdapter(), "config": FakeConfig(config)},
        )
        original_statement = runner.context["statement"]

        def statement(name=None, fetch_result=False, auto_begin=True, caller=None):
            sql = caller() if caller is not None else ""
            events.append(("ddl", " ".join(sql.split())))
            if ddl_error is not None:
                raise ddl_error
            return original_statement(
                name,
                fetch_result=fetch_result,
                auto_begin=auto_begin,
                caller=lambda: sql,
            )

        def add_remove_columns(relation, add_columns, remove_columns):
            events.append(("add_remove", relation, add_columns, remove_columns))
            if ddl_error is not None:
                raise ddl_error

        runner.context.update(
            {
                "statement": statement,
                "alter_relation_add_remove_columns": add_remove_columns,
                "alter_column_type": lambda *args: runner.context[
                    "doris__alter_column_type"
                ](*args),
            }
        )
        return runner, events, FakeRelation(database=database)

    @staticmethod
    def changes(add_columns=None, remove_columns=None, new_types=None):
        return {
            "source_not_in_target": add_columns or [],
            "target_not_in_source": remove_columns or [],
            "new_target_types": new_types or [],
        }

    @staticmethod
    def expected_events(relation, ddl_event, is_olap):
        if is_olap:
            return [
                ("latest_job", relation),
                ddl_event,
                ("wait", relation, "42"),
            ]
        return [ddl_event]

    def test_append_new_columns_uses_engine_specific_completion(
        self, database, engine, is_olap
    ):
        runner, events, relation = self.runner(database, engine)
        added = [FakeColumn("extra")]
        removed = [FakeColumn("old_value")]

        runner.render(
            "doris__sync_column_schemas",
            "append_new_columns",
            relation,
            self.changes(added, removed),
        )

        assert events == self.expected_events(
            relation, ("add_remove", relation, added, None), is_olap
        )

    @pytest.mark.parametrize(
        "added_names,removed_names",
        [(["extra"], []), ([], ["old_value"]), (["extra"], ["old_value"])],
    )
    def test_sync_all_columns_adds_and_removes_before_waiting(
        self, database, engine, is_olap, added_names, removed_names
    ):
        runner, events, relation = self.runner(database, engine)
        added = [FakeColumn(name) for name in added_names]
        removed = [FakeColumn(name) for name in removed_names]

        runner.render(
            "doris__sync_column_schemas",
            "sync_all_columns",
            relation,
            self.changes(added, removed),
        )

        assert events == self.expected_events(
            relation, ("add_remove", relation, added, removed), is_olap
        )

    def test_sync_all_columns_changes_types_through_real_alter_macro(
        self, database, engine, is_olap
    ):
        runner, events, relation = self.runner(database, engine)

        runner.render(
            "doris__sync_column_schemas",
            "sync_all_columns",
            relation,
            self.changes(new_types=[{"column_name": "id", "new_type": "BIGINT"}]),
        )

        sql = f"alter table {relation} modify column `id` BIGINT"
        assert [" ".join(statement.sql.split()) for statement in runner.statements] == [sql]
        assert events == self.expected_events(relation, ("ddl", sql), is_olap)

    def test_direct_type_change_uses_engine_specific_completion(
        self, database, engine, is_olap
    ):
        runner, events, relation = self.runner(database, engine)

        runner.render("doris__alter_column_type", relation, "value", "varchar(64)")

        sql = f"alter table {relation} modify column `value` varchar(64)"
        assert events == self.expected_events(relation, ("ddl", sql), is_olap)

    @pytest.mark.parametrize("mode", ["append_new_columns", "sync_all_columns"])
    def test_unchanged_schema_has_no_ddl_or_job_queries(
        self, database, engine, is_olap, mode
    ):
        runner, events, relation = self.runner(database, engine)

        runner.render("doris__sync_column_schemas", mode, relation, self.changes())

        assert events == []
        assert runner.statements == []

    def test_alter_failure_is_propagated_without_waiting(
        self, database, engine, is_olap
    ):
        error = RuntimeError("Unsupported type conversion")
        runner, events, relation = self.runner(database, engine, ddl_error=error)

        with pytest.raises(RuntimeError, match="Unsupported type conversion"):
            runner.render("doris__alter_column_type", relation, "id", "STRING")

        expected = [("latest_job", relation)] if is_olap else []
        expected.append(("ddl", f"alter table {relation} modify column `id` STRING"))
        assert events == expected


class TestPersistDocs:
    """Column comments come from dbt as {column_name: column_info_dict}.

    Interpolating the value directly wrote the whole dict repr into the comment,
    and its embedded quotes broke the statement outright:
    `1105 ... mismatched input 'name' expecting {<EOF>, ';'}`.
    """

    def runner(self):
        return MacroRunner("adapters/columns.sql", "adapters/relation.sql")

    def test_column_comment_uses_description_only(self):
        runner = self.runner()
        runner.render(
            "doris__alter_column_comment",
            FakeRelation(),
            {"id": {"name": "id", "description": "the user id", "data_type": "int"}},
        )
        assert len(runner.statements) == 1
        sql = runner.statements[0].sql
        assert sql == (
            'alter table `dbt_test`.`my_model` modify column `id` '
            'comment "the user id"'
        ), sql
        # Doris rejects a type in MODIFY COLUMN for key and distribution columns.
        assert "int" not in sql, f"the column type must be omitted: {sql}"

    def test_column_comment_escapes_quotes(self):
        runner = self.runner()
        runner.render(
            "doris__alter_column_comment",
            FakeRelation(),
            {"id": {"description": "it's a c:\\path"}},
        )
        sql = runner.statements[0].sql
        assert 'comment "it\'s a c:\\path"' in sql

    def test_columns_without_a_description_are_skipped(self):
        runner = self.runner()
        runner.render(
            "doris__alter_column_comment",
            FakeRelation(),
            {"documented": {"description": "yes"}, "bare": {"description": ""}},
        )
        assert len(runner.statements) == 1, (
            "a column with no description needs no ALTER: " f"{runner.statements}"
        )
        assert "`documented`" in runner.statements[0].sql

    def test_views_are_skipped(self):
        """Doris has no MODIFY COLUMN/COMMENT for views."""
        runner = self.runner()
        view = FakeRelation(relation_type="view")
        runner.render("doris__alter_column_comment", view, {"id": {"description": "x"}})
        assert runner.statements == []

        runner = self.runner()
        runner.render("doris__alter_relation_comment", view, "a comment")
        assert runner.statements == []

    def test_relation_comment_escapes_quotes(self):
        runner = self.runner()
        runner.render("doris__alter_relation_comment", FakeRelation(), "it's fine")
        assert 'comment "it\'s fine"' in runner.statements[0].sql

    def test_complex_comment_update_requires_a_full_refresh(self):
        runner = self.runner()
        with pytest.raises(CapturedCompilerError, match="--full-refresh"):
            runner.render(
                "doris__alter_relation_comment",
                FakeRelation(),
                "it's \"documented\"",
            )


INCREMENTAL_MACROS = (
    "materializations/incremental/incremental.sql",
    "materializations/incremental/help.sql",
    "materializations/incremental/strategies.sql",
    "adapters/relation.sql",
    "adapters/metadata.sql",
)


def microbatch_model(
    batch_id="20260804",
    start=None,
    end=None,
):
    return {
        "unique_id": "model.my_project.my_model",
        "name": "my_model",
        "batch": {
            "id": batch_id,
            "event_time_start": start
            or datetime(2026, 8, 4, tzinfo=timezone.utc),
            "event_time_end": end
            or datetime(2026, 8, 5, tzinfo=timezone.utc),
        },
    }


def microbatch_config(**updates):
    values = {
        "incremental_strategy": "microbatch",
        "event_time": "event_time",
        "batch_size": "day",
        "partition_by": ["event_time"],
        "partition_type": "RANGE",
        "properties": {
            "dynamic_partition.enable": "true",
            "dynamic_partition.time_unit": "DAY",
            "dynamic_partition.time_zone": "UTC",
            "dynamic_partition.prefix": "p",
            "dynamic_partition.start": "-10",
            "dynamic_partition.end": "1",
            "dynamic_partition.create_history_partition": "true",
        },
    }
    values.update(updates)
    return values


def incremental_args(**updates):
    values = {
        "target_relation": FakeRelation(identifier="target"),
        "temp_relation": FakeRelation(identifier="target__dbt_tmp"),
        "unique_key": ["id"],
        "dest_columns": [FakeColumn("id"), FakeColumn("value")],
        "incremental_predicates": None,
        "source_sql": "select 1 as id, 'new' as value",
        "temp_relation_exists": False,
        "overwrite_partitions": None,
    }
    values.update(updates)
    return values


class TestIncrementalStrategyValidation:
    """The public strategy names map cleanly to Doris table semantics."""

    def runner(self, config, model=None):
        return MacroRunner(
            *INCREMENTAL_MACROS,
            context={
                "adapter": FakeAdapter(),
                "config": FakeConfig(config),
                "model": model
                or {
                    "unique_id": "model.my_project.my_model",
                    "name": "my_model",
                },
            },
        )

    def validate(self, config, model=None):
        return self.runner(config, model=model).render(
            "dbt_doris_validate_get_incremental_strategy", FakeConfig(config)
        )

    @pytest.mark.parametrize("config", [{}, {"unique_key": ["id"]}])
    def test_default_strategy_keeps_public_default_name(self, config):
        assert self.validate(config) == "default"

    @pytest.mark.parametrize(
        ("unique_key", "expected"),
        [(None, "append"), (["id"], "merge")],
    )
    def test_default_routes_by_unique_key(self, unique_key, expected):
        runner = self.runner({})
        assert (
            runner.render(
                "doris__effective_incremental_strategy",
                "default",
                unique_key,
            )
            == expected
        )

    def test_append_needs_no_unique_key(self):
        assert self.validate({"incremental_strategy": "append"}) == "append"

    def test_merge_requires_unique_key(self):
        with pytest.raises(CapturedCompilerError) as excinfo:
            self.validate({"incremental_strategy": "merge"})
        message = str(excinfo.value)
        assert "requires a 'unique_key'" in message
        assert "model.my_project.my_model" in message
        assert "unique_key=" in message

    @pytest.mark.parametrize("unique_key", ["id", ["tenant_id", "id"]])
    def test_merge_accepts_single_and_composite_keys(self, unique_key):
        assert (
            self.validate({"incremental_strategy": "merge", "unique_key": unique_key}) == "merge"
        )

    def test_insert_overwrite_needs_no_unique_key(self):
        assert self.validate({"incremental_strategy": "insert_overwrite"}) == "insert_overwrite"

    def test_microbatch_accepts_aligned_dynamic_range_partition(self):
        config = microbatch_config()

        assert self.validate(config, microbatch_model()) == "microbatch"

    def test_microbatch_accepts_adapter_managed_static_range_partitions(self):
        config = microbatch_config(properties={"replication_num": "1"})

        assert self.validate(config, microbatch_model()) == "microbatch"

    @pytest.mark.parametrize(
        ("updates", "expected"),
        [
            ({"event_time": None}, "event_time"),
            ({"partition_by": None}, "partition_by"),
            ({"partition_by": ["other_time"]}, "same column"),
            ({"partition_type": "LIST"}, "range"),
            (
                {
                    "properties": {
                        "dynamic_partition.enable": "true",
                        "dynamic_partition.time_unit": "HOUR",
                        "dynamic_partition.time_zone": "UTC",
                        "dynamic_partition.prefix": "p",
                        "dynamic_partition.start": "-10",
                        "dynamic_partition.end": "1",
                        "dynamic_partition.create_history_partition": "true",
                    }
                },
                "batch_size",
            ),
            (
                {
                    "properties": {
                        "dynamic_partition.enable": "true",
                        "dynamic_partition.time_unit": "DAY",
                        "dynamic_partition.time_zone": "Asia/Shanghai",
                        "dynamic_partition.prefix": "p",
                        "dynamic_partition.start": "-10",
                        "dynamic_partition.end": "1",
                        "dynamic_partition.create_history_partition": "true",
                    }
                },
                "utc",
            ),
            (
                {
                    "properties": {
                        "dynamic_partition.enable": "true",
                        "dynamic_partition.time_unit": "DAY",
                        "dynamic_partition.time_zone": "UTC",
                        "dynamic_partition.prefix": "p",
                        "dynamic_partition.start": "-10",
                        "dynamic_partition.end": "1",
                        "dynamic_partition.create_history_partition": "false",
                    }
                },
                "create_history_partition",
            ),
            (
                {
                    "properties": {
                        "dynamic_partition.enable": "true",
                        "dynamic_partition.time_unit": "DAY",
                        "dynamic_partition.time_zone": "UTC",
                        "dynamic_partition.start": "-10",
                        "dynamic_partition.end": "1",
                        "dynamic_partition.create_history_partition": "true",
                    }
                },
                "dynamic_partition.prefix",
            ),
            ({"unique_key": "id"}, "unique_key"),
            ({"overwrite_partitions": "*"}, "adapter resolves"),
            ({"partition_by_init": ["PARTITION p1 VALUES LESS THAN (MAXVALUE)"]}, "model.batch"),
        ],
    )
    def test_microbatch_rejects_unsafe_partition_configs(
        self,
        updates,
        expected,
    ):
        config = microbatch_config(**updates)

        with pytest.raises(CapturedCompilerError) as excinfo:
            self.validate(config, microbatch_model())

        assert expected in str(excinfo.value).lower()

    def test_microbatch_requires_core_batch_context(self):
        with pytest.raises(CapturedCompilerError) as excinfo:
            self.validate(microbatch_config())

        assert "model.batch" in str(excinfo.value)

    def test_insert_overwrite_rejects_legacy_unique_key_combination(self):
        with pytest.raises(CapturedCompilerError) as excinfo:
            self.validate(
                {
                    "incremental_strategy": "insert_overwrite",
                    "unique_key": "id",
                }
            )
        message = str(excinfo.value)
        assert "could silently" in message
        assert "strategy='merge'" in message
        assert "remove 'unique_key'" in message

    @pytest.mark.parametrize("strategy", ["delete+insert", "delete_insert"])
    def test_delete_insert_is_explicitly_rejected(self, strategy):
        with pytest.raises(CapturedCompilerError) as excinfo:
            self.validate({"incremental_strategy": strategy, "unique_key": ["id"]})
        message = str(excinfo.value)
        assert "not supported" in message
        assert "Use 'merge'" in message

    @pytest.mark.parametrize(
        "properties",
        [
            {"enable_unique_key_merge_on_write": "false"},
            {"function_column.sequence_col": "updated_at"},
        ],
    )
    def test_merge_accepts_mor_and_sequence_properties(self, properties):
        assert (
            self.validate(
                {
                    "incremental_strategy": "merge",
                    "unique_key": "id",
                    "properties": properties,
                }
            )
            == "merge"
        )

    def test_merge_rejects_sequence_type_hidden_column_mode(self):
        with pytest.raises(CapturedCompilerError) as excinfo:
            self.validate(
                {
                    "incremental_strategy": "merge",
                    "unique_key": "id",
                    "properties": {"FUNCTION_COLUMN.SEQUENCE_TYPE": "BIGINT"},
                }
            )
        message = str(excinfo.value)
        assert "__DORIS_SEQUENCE_COL__" in message
        assert "function_column.sequence_col" in message

    def test_existing_merge_target_accepts_matching_visible_sequence_mapping(self):
        runner = self.runner(
            {
                "incremental_strategy": "merge",
                "unique_key": "id",
                "properties": {
                    "function_column.sequence_col": "Sequence_ID",
                },
            }
        )

        create_table = '''CREATE TABLE `target` (`id` int, `sequence_id` bigint)
            UNIQUE KEY(`id`)
            PROPERTIES (
            "FUNCTION_COLUMN.SEQUENCE_COL" = "sequence_id",
            "replication_num" = "1"
            )'''.replace("\n", "\r\n")

        runner.render(
            "doris__validate_incremental_sequence_mapping",
            create_table,
            FakeRelation(identifier="target"),
        )

    def test_sequence_property_text_in_comment_is_not_physical_mapping(self):
        runner = self.runner(
            {
                "incremental_strategy": "merge",
                "unique_key": "id",
            }
        )

        runner.render(
            "doris__validate_incremental_sequence_mapping",
            '''CREATE TABLE target (`id` int COMMENT
            '"function_column.sequence_type" = "bigint"')
            UNIQUE KEY(`id`)
            PROPERTIES (
            "replication_num" = "1"
            )''',
            FakeRelation(identifier="target"),
        )

    def test_keyless_duplicate_property_identifies_append_target(self):
        runner = self.runner(
            {
                "incremental_strategy": "append",
            }
        )

        table_model = runner.render(
            "doris__table_model_from_create_table",
            '''CREATE TABLE target (`measure` double)
            DISTRIBUTED BY RANDOM BUCKETS AUTO
            PROPERTIES (
            "enable_duplicate_without_keys_by_default" = "true",
            "replication_num" = "1"
            )''',
        )

        assert table_model == "duplicate"

    @pytest.mark.parametrize("spoofed_key", ["UNIQUE KEY(", "AGGREGATE KEY("])
    def test_table_model_ignores_key_clause_text_in_comments(self, spoofed_key):
        table_model = self.runner({}).render(
            "doris__table_model_from_create_table",
            f'''CREATE TABLE `target` (`id` int COMMENT "example {spoofed_key}")
            DUPLICATE KEY(`id`)
            COMMENT "documented {spoofed_key}"
            DISTRIBUTED BY HASH(`id`) BUCKETS 1
            PROPERTIES (
            "replication_num" = "1"
            )''',
        )

        assert table_model == "duplicate"

    @pytest.mark.parametrize(
        ("properties", "physical_property", "expected"),
        [
            (
                {},
                '"function_column.sequence_col" = "sequence_id"',
                "does not configure 'function_column.sequence_col'",
            ),
            (
                {"function_column.sequence_col": "sequence_id"},
                '"function_column.sequence_col" = "tenant_id"',
                "sequence mapping column 'tenant_id'",
            ),
            (
                {"function_column.sequence_col": "sequence_id"},
                '"replication_num" = "1"',
                "no visible sequence mapping",
            ),
            (
                {},
                '"function_column.sequence_type" = "bigint"',
                "__doris_sequence_col__",
            ),
        ],
    )
    def test_existing_merge_target_rejects_physical_sequence_mismatch(
        self,
        properties,
        physical_property,
        expected,
    ):
        runner = self.runner(
            {
                "incremental_strategy": "merge",
                "unique_key": "id",
                "properties": properties,
            }
        )

        with pytest.raises(CapturedCompilerError) as excinfo:
            runner.render(
                "doris__validate_incremental_sequence_mapping",
                "CREATE TABLE target UNIQUE KEY(id) PROPERTIES (\n"
                f"{physical_property}\n)",
                FakeRelation(identifier="target"),
            )

        assert expected in str(excinfo.value).lower()

    def test_insert_overwrite_rejects_hidden_physical_sequence(self):
        runner = self.runner(
            {
                "incremental_strategy": "insert_overwrite",
            }
        )

        with pytest.raises(CapturedCompilerError) as excinfo:
            runner.render(
                "doris__validate_incremental_sequence_mapping",
                "CREATE TABLE target UNIQUE KEY(id) PROPERTIES (\n"
                '"function_column.sequence_type" = "bigint"\n)',
                FakeRelation(identifier="target"),
                "insert_overwrite",
            )

        message = str(excinfo.value).lower()
        assert "incremental strategy 'insert_overwrite'" in message
        assert "__doris_sequence_col__" in message
        assert "without hidden sequence state" in message
        assert "strategy 'merge'" in message

    @pytest.mark.parametrize(
        "config",
        [
            {
                "incremental_strategy": "append",
                "properties": {"function_column.sequence_col": "updated_at"},
            },
            {
                "incremental_strategy": "merge",
                "unique_key": "id",
                "properties": {"function_column.sequence_col": "updated-at"},
            },
        ],
    )
    def test_sequence_column_requires_merge_and_a_plain_column_name(self, config):
        with pytest.raises(CapturedCompilerError):
            self.validate(config)

    def test_merge_rejects_duplicate_configured_key_columns(self):
        with pytest.raises(CapturedCompilerError) as excinfo:
            self.validate(
                {
                    "incremental_strategy": "merge",
                    "unique_key": ["id", "ID"],
                }
            )
        assert "Duplicate unique_key column" in str(excinfo.value)

    @pytest.mark.parametrize(
        "strategy",
        ["append", "merge", "insert_overwrite", "microbatch"],
    )
    def test_predicates_are_rejected_for_builtins(self, strategy):
        config = {
            "incremental_strategy": strategy,
            "incremental_predicates": ["DBT_INTERNAL_DEST.id > 0"],
        }
        if strategy == "merge":
            config["unique_key"] = "id"
        if strategy == "microbatch":
            config.update(microbatch_config())
        with pytest.raises(CapturedCompilerError) as excinfo:
            self.validate(
                config,
                microbatch_model() if strategy == "microbatch" else None,
            )
        assert "native MERGE INTO" in str(excinfo.value)

    @pytest.mark.parametrize("option", ["merge_update_columns", "merge_exclude_columns"])
    def test_partial_merge_configs_are_rejected(self, option):
        with pytest.raises(CapturedCompilerError) as excinfo:
            self.validate(
                {
                    "incremental_strategy": "merge",
                    "unique_key": "id",
                    option: ["value"],
                }
            )
        assert "native MERGE INTO" in str(excinfo.value)

    def test_overwrite_partition_config_is_validated(self):
        with pytest.raises(CapturedCompilerError):
            self.validate(
                {
                    "incremental_strategy": "append",
                    "overwrite_partitions": ["p1"],
                }
            )

        assert (
            self.validate(
                {
                    "incremental_strategy": "insert_overwrite",
                    "partition_by": "event_date",
                    "overwrite_partitions": "*",
                }
            )
            == "insert_overwrite"
        )

    @pytest.mark.parametrize(
        "partitions",
        [[], ["*", "p1"], ["unsafe-name"]],
    )
    def test_invalid_overwrite_partitions_are_rejected(self, partitions):
        with pytest.raises(CapturedCompilerError):
            self.validate(
                {
                    "incremental_strategy": "insert_overwrite",
                    "partition_by": "event_date",
                    "overwrite_partitions": partitions,
                }
            )

    def test_empty_strategy_falls_back_to_the_default(self):
        """An unset config arrives as '' or none; both mean "use the default"."""
        assert self.validate({"incremental_strategy": "", "unique_key": ["id"]}) == "default"


class TestIncrementalStrategySql:
    def runner(self, config=None, model=None):
        return MacroRunner(
            *INCREMENTAL_MACROS,
            context={
                "adapter": FakeAdapter(),
                "config": FakeConfig(config),
                "model": model
                or {
                    "unique_id": "model.my_project.my_model",
                    "name": "my_model",
                },
            },
        )

    @pytest.mark.parametrize(
        "macro",
        ["doris__get_incremental_append_sql", "doris__get_incremental_merge_sql"],
    )
    def test_direct_insert_is_one_statement_and_reads_source_once(self, macro):
        sql = self.runner().sql(macro, incremental_args())
        assert statement_count(sql) == 1
        assert sql.count("select 1 as id") == 1
        assert "__dbt_tmp" not in sql
        assert "insert into `dbt_test`.`target` (`id`, `value`)" in sql

    def test_merge_validates_duplicate_source_keys_in_the_insert(self):
        sql = self.runner().sql(
            "doris__get_incremental_merge_sql",
            incremental_args(),
        )
        assert "count(*) over" in sql
        assert "json_parse(if(" in sql
        assert "'DBT_INTERNAL_DUPLICATE_KEYS'" in sql
        assert "select DBT_INTERNAL_VALIDATION_MARKER" not in sql

    def test_merge_validation_column_cannot_collide_with_model_columns(self):
        columns = [
            FakeColumn("id"),
            FakeColumn("DBT_INTERNAL_UNIQUE_KEY_VALIDATION_0"),
            FakeColumn("DBT_INTERNAL_UNIQUE_KEY_VALIDATION_1"),
        ]
        sql = self.runner().sql(
            "doris__get_incremental_merge_sql",
            incremental_args(dest_columns=columns),
        )

        assert "as `DBT_INTERNAL_UNIQUE_KEY_VALIDATION_2`" in sql
        assert (
            "DBT_INTERNAL_SOURCE.`DBT_INTERNAL_UNIQUE_KEY_VALIDATION_2` > 1"
            in sql
        )

    def test_initial_merge_projects_unique_keys_before_value_columns(self):
        columns = [
            FakeColumn("value"),
            FakeColumn("tenant_id"),
            FakeColumn("id"),
        ]
        ordered = self.runner().render(
            "doris__unique_key_first_columns",
            columns,
            ["tenant_id", "id"],
        )
        assert [column.name for column in ordered] == [
            "tenant_id",
            "id",
            "value",
        ]

    @pytest.mark.parametrize(
        ("partitions", "expected"),
        [
            (None, "insert overwrite table `dbt_test`.`target` (`id`, `value`)"),
            ("*", "partition(*) (`id`, `value`)"),
            (["p1", "p2"], "partition(`p1`, `p2`) (`id`, `value`)"),
        ],
    )
    def test_native_insert_overwrite(self, partitions, expected):
        sql = self.runner().sql(
            "doris__get_incremental_insert_overwrite_sql",
            incremental_args(overwrite_partitions=partitions),
        )
        assert statement_count(sql) == 1
        assert expected in sql
        assert "__dbt_tmp" not in sql

    def test_microbatch_overwrites_the_resolved_static_partition(self):
        sql = self.runner(
            microbatch_config(),
            model=microbatch_model(),
        ).sql(
            "doris__get_incremental_microbatch_sql",
            incremental_args(
                unique_key=None,
                microbatch_partition="actual_partition_name",
            ),
        )

        assert statement_count(sql) == 1
        assert "partition(`actual_partition_name`)" in sql
        assert "partition(*)" not in sql
        assert "__dbt_tmp" not in sql

    @pytest.mark.parametrize(
        ("batch_size", "batch_id", "start", "end", "partition_name"),
        [
            (
                "hour",
                "20260804T12",
                datetime(2026, 8, 4, 12, tzinfo=timezone.utc),
                datetime(2026, 8, 4, 13, tzinfo=timezone.utc),
                "dbt_mb_2026080412",
            ),
            (
                "day",
                "20260804",
                datetime(2026, 8, 4, tzinfo=timezone.utc),
                datetime(2026, 8, 5, tzinfo=timezone.utc),
                "dbt_mb_20260804",
            ),
            (
                "month",
                "202608",
                datetime(2026, 8, 1, tzinfo=timezone.utc),
                datetime(2026, 9, 1, tzinfo=timezone.utc),
                "dbt_mb_202608",
            ),
            (
                "year",
                "2026",
                datetime(2026, 1, 1, tzinfo=timezone.utc),
                datetime(2027, 1, 1, tzinfo=timezone.utc),
                "dbt_mb_2026",
            ),
        ],
    )
    def test_microbatch_initial_partition_uses_the_core_batch_range(
        self,
        batch_size,
        batch_id,
        start,
        end,
        partition_name,
    ):
        config = microbatch_config(
            batch_size=batch_size,
            properties={"replication_num": "1"},
        )
        model = microbatch_model(batch_id, start, end)

        assert (
            self.runner(config, model=model).render(
                "dbt_doris_validate_get_incremental_strategy",
                FakeConfig(config),
            )
            == "microbatch"
        )
        clause = self.runner(
            config,
            model=model,
        ).render("doris__microbatch_partition_by_clause")

        assert "partition by range (`event_time`)" in clause.lower()
        assert f"`{partition_name}`" in clause
        assert start.strftime("%Y-%m-%d %H:%M:%S") in clause
        assert end.strftime("%Y-%m-%d %H:%M:%S") in clause

    @pytest.mark.parametrize(
        ("batch_id", "start", "end", "description"),
        [
            (
                "20260804",
                datetime(2026, 8, 4, tzinfo=timezone.utc),
                datetime(2026, 8, 5, tzinfo=timezone.utc),
                "[('2026-08-04'), ('2026-08-05'))",
            ),
            (
                "20260804T12",
                datetime(2026, 8, 4, 12, tzinfo=timezone.utc),
                datetime(2026, 8, 4, 13, tzinfo=timezone.utc),
                (
                    "[('2026-08-04 12:00:00'), "
                    "('2026-08-04 13:00:00'))"
                ),
            ),
        ],
    )
    def test_microbatch_resolves_any_exact_range_partition_name(
        self,
        batch_id,
        start,
        end,
        description,
    ):
        rows = [
            [
                "arbitrary_physical_name",
                "RANGE",
                "event_time",
                description,
            ]
        ]
        model = microbatch_model(batch_id, start, end)

        partition = self.runner(
            microbatch_config(),
            model=model,
        ).render(
            "doris__microbatch_partition_from_rows",
            rows,
            FakeRelation(identifier="target"),
        )

        assert partition == "arbitrary_physical_name"

    def test_microbatch_rejects_a_coarser_physical_partition(self):
        rows = [
            [
                "p202608",
                "RANGE",
                "event_time",
                "[('2026-08-01'), ('2026-09-01'))",
            ]
        ]

        with pytest.raises(CapturedCompilerError) as excinfo:
            self.runner(
                microbatch_config(),
                model=microbatch_model(),
            ).render(
                "doris__microbatch_partition_from_rows",
                rows,
                FakeRelation(identifier="target"),
            )

        message = str(excinfo.value).lower()
        assert "exact range partition" in message
        assert "2026-08-04 00:00:00" in message

    def test_static_microbatch_can_create_a_missing_non_overlapping_partition(self):
        rows = [
            [
                "old_batch",
                "RANGE",
                "event_time",
                "[('2026-08-03'), ('2026-08-04'))",
            ]
        ]

        partition = self.runner(
            microbatch_config(properties={"replication_num": "1"}),
            model=microbatch_model(),
        ).render(
            "doris__microbatch_partition_from_rows",
            rows,
            FakeRelation(identifier="target"),
            True,
        )

        assert partition is None

    def test_dynamic_microbatch_rejects_a_missing_exact_partition(self):
        rows = [
            [
                "old_batch",
                "RANGE",
                "event_time",
                "[('2026-08-03'), ('2026-08-04'))",
            ]
        ]

        with pytest.raises(CapturedCompilerError) as excinfo:
            self.runner(
                microbatch_config(),
                model=microbatch_model(),
            ).render(
                "doris__microbatch_partition_from_rows",
                rows,
                FakeRelation(identifier="target"),
            )

        assert "dynamic partition is enabled" in str(excinfo.value).lower()
        assert "no exact range partition" in str(excinfo.value).lower()

    def test_microbatch_rejects_static_dynamic_target_drift(self):
        create_table = """CREATE TABLE target DUPLICATE KEY(id)
        PARTITION BY RANGE(event_time)
        (PARTITION p1 VALUES [('2026-08-04'), ('2026-08-05')))
        PROPERTIES (
        "dynamic_partition.enable" = "true",
        "dynamic_partition.time_unit" = "DAY",
        "dynamic_partition.time_zone" = "UTC",
        "dynamic_partition.prefix" = "p",
        "dynamic_partition.start" = "-10",
        "dynamic_partition.end" = "1",
        "dynamic_partition.create_history_partition" = "true"
        )"""

        with pytest.raises(CapturedCompilerError) as excinfo:
            self.runner(
                microbatch_config(properties={"replication_num": "1"}),
                model=microbatch_model(),
            ).render(
                "doris__validate_microbatch_target_properties",
                create_table,
                FakeRelation(identifier="target"),
            )

        assert "static partitions" in str(excinfo.value)

    def test_microbatch_accepts_matching_physical_dynamic_properties(self):
        create_table = """CREATE TABLE target DUPLICATE KEY(id)
        PARTITION BY RANGE(event_time)
        (PARTITION p1 VALUES [('2026-08-04'), ('2026-08-05')))
        PROPERTIES (
        "dynamic_partition.enable" = "true",
        "dynamic_partition.time_unit" = "DAY",
        "dynamic_partition.time_zone" = "UTC",
        "dynamic_partition.prefix" = "p",
        "dynamic_partition.start" = "-10",
        "dynamic_partition.end" = "1",
        "dynamic_partition.create_history_partition" = "true"
        )"""

        self.runner(
            microbatch_config(),
            model=microbatch_model(),
        ).render(
            "doris__validate_microbatch_target_properties",
            create_table,
            FakeRelation(identifier="target"),
        )

    @pytest.mark.parametrize(
        "macro",
        [
            "doris__get_incremental_append_sql",
            "doris__get_incremental_merge_sql",
            "doris__get_incremental_insert_overwrite_sql",
        ],
    )
    def test_standard_five_key_contract_reads_temp_relation(self, macro):
        args = incremental_args()
        for key in ("source_sql", "temp_relation_exists", "overwrite_partitions"):
            args.pop(key)
        sql = self.runner().sql(macro, args)
        assert "`dbt_test`.`target__dbt_tmp`" in sql

    @pytest.mark.parametrize(
        ("unique_key", "expected"),
        [(None, "select DBT_INTERNAL_SOURCE.`id`"), (["id"], "DBT_INTERNAL_DUPLICATE_KEYS")],
    )
    def test_default_sql_routes_by_unique_key(self, unique_key, expected):
        sql = self.runner().sql(
            "doris__get_incremental_default_sql",
            incremental_args(unique_key=unique_key),
        )
        assert expected in sql

    def test_unique_key_type_change_requires_full_refresh(self):
        with pytest.raises(CapturedCompilerError) as excinfo:
            self.runner().render(
                "doris__validate_unique_key_schema_changes",
                {"new_target_types": [{"column_name": "id", "new_type": "bigint"}]},
                ["id"],
            )
        assert "--full-refresh" in str(excinfo.value)

    def test_sequence_mapping_type_change_requires_full_refresh(self):
        runner = self.runner(
            {
                "properties": {
                    "function_column.sequence_col": "sequence_value",
                }
            }
        )
        with pytest.raises(CapturedCompilerError) as excinfo:
            runner.render(
                "doris__validate_unique_key_schema_changes",
                {
                    "new_target_types": [
                        {
                            "column_name": "sequence_value",
                            "new_type": "varchar(40)",
                        }
                    ]
                },
                ["id"],
            )
        assert "Sequence mapping" in str(excinfo.value)
        assert "--full-refresh" in str(excinfo.value)
