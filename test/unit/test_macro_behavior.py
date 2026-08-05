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

import pytest

from .macro_harness import (
    CapturedCompilerError,
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


class TestIncrementalStrategyValidation:
    """`insert_overwrite` without a unique_key used to silently append.

    The materialization dispatched on `if not unique_key or strategy ==
    'append'`, so a missing key routed an insert_overwrite model into the append
    branch: it built a DUPLICATE KEY table and appended every row on every run,
    duplicating the data while dbt reported success.
    """

    def runner(self, config):
        return MacroRunner(
            "materializations/incremental/incremental.sql",
            context={
                "config": FakeConfig(config),
                "model": {"unique_id": "model.my_project.my_model", "name": "my_model"},
            },
        )

    def validate(self, config):
        return self.runner(config).render(
            "dbt_doris_validate_get_incremental_strategy", FakeConfig(config)
        )

    def test_default_strategy_is_insert_overwrite(self):
        assert self.validate({"unique_key": ["id"]}) == "insert_overwrite"

    def test_append_needs_no_unique_key(self):
        assert self.validate({"incremental_strategy": "append"}) == "append"

    def test_insert_overwrite_requires_unique_key(self):
        with pytest.raises(CapturedCompilerError) as excinfo:
            self.validate({})
        message = str(excinfo.value)
        assert "requires a 'unique_key'" in message
        assert "model.my_project.my_model" in message
        # The message has to name the way out, both of them.
        assert "unique_key=" in message
        assert "incremental_strategy='append'" in message

    @pytest.mark.parametrize("strategy", ["delete+insert", "merge", "overwrite"])
    def test_unknown_strategy_is_rejected(self, strategy):
        with pytest.raises(CapturedCompilerError) as excinfo:
            self.validate({"incremental_strategy": strategy, "unique_key": ["id"]})
        assert "Invalid incremental strategy" in str(excinfo.value)

    def test_empty_strategy_falls_back_to_the_default(self):
        """An unset config arrives as '' or none; both mean "use the default"."""
        assert self.validate({"incremental_strategy": "", "unique_key": ["id"]}) == (
            "insert_overwrite"
        )
