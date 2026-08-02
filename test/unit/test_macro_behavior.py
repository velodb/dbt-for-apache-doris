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
            "alter table `dbt_test`.`my_model` modify column `id` " "comment 'the user id'"
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
        assert "\\'" in sql, f"single quotes must be escaped: {sql}"
        assert "\\\\path" in sql, f"backslashes must be escaped: {sql}"

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
        assert "\\'" in runner.statements[0].sql
