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

import pytest

from .macro_harness import (
    CapturedCompilerError,
    FakeAdapter,
    FakeColumn,
    FakeConfig,
    FakeRelation,
    FakeRow,
    MacroRunner,
)

TABLE_MACROS = ("materializations/table/create_table_as.sql", "adapters/relation.sql")

CREATE_TABLE_MACROS = ("doris__create_table_as", "doris__create_unique_table_as")


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


class TestGrants:
    def runner(self):
        return MacroRunner("adapters/grants.sql")

    def test_show_grants_uses_doris_table_privileges(self):
        sql = self.runner().sql(
            "doris__get_show_grant_sql",
            FakeRelation(schema="analytics", identifier="orders"),
        )

        assert "from information_schema.table_privileges" in sql
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

    def test_replace_partitions_runs_one_statement_per_partition(self):
        """Each ALTER ... REPLACE PARTITION is its own statement.

        These were emitted as one semicolon-separated blob. The replaces
        succeeded, so the error surfaced later and left the temp table behind.
        """
        runner = MacroRunner(
            "materializations/partition/replace.sql",
            "materializations/partition/helpers.sql",
        )
        partitions = [FakeRow({"dt": "20260101"}), FakeRow({"dt": "20260102"})]
        replaced = runner.render("doris__replace_partitions", FakeRelation(), partitions)

        assert replaced == 2, "should report how many partitions were replaced"
        assert len(runner.statements) == 2
        for statement in runner.statements:
            assert (
                statement_count(statement.sql) == 1
            ), f"partition replace must be one statement: {statement.sql}"
            assert "replace partition" in statement.sql
        # Distinct statement names, or dbt's result store overwrites entries.
        names = [statement.name for statement in runner.statements]
        assert len(set(names)) == len(names), f"statement names collide: {names}"

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


class TestPersistDocs:
    """Column comments come from dbt as {column_name: column_info_dict}.

    Interpolating the value directly wrote the whole dict repr into the comment,
    and its embedded quotes broke the statement outright:
    `1105 ... mismatched input 'name' expecting {<EOF>, ';'}`.
    """

    def runner(self):
        return MacroRunner("adapters/columns.sql")

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
