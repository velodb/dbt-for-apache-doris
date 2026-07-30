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

"""Public SQL behavior for Doris async materialized-view models."""

import hashlib
from types import SimpleNamespace

import pytest

from dbt.adapters.contracts.relation import RelationType
from dbt.adapters.doris.impl import DorisAdapter
from dbt.adapters.doris.relation import DorisRelation

from .macro_harness import CapturedCompilerError, FakeConfig, FakeRelation, MacroRunner

MATERIALIZED_VIEW_MACROS = ("materializations/materialized_view/materialized_view.sql",)


def materialized_view_runner(config=None, model=None):
    return MacroRunner(
        *MATERIALIZED_VIEW_MACROS,
        context={
            "config": FakeConfig(config),
            "model": model or {},
            "local_md5": lambda value: hashlib.md5(value.encode()).hexdigest(),
        },
    )


def materialization_runner(
    existing_relation=None,
    preexisting_intermediate_relation=None,
    preexisting_backup_relation=None,
    show_create_sql="",
    config=None,
    full_refresh=False,
    previous_task_ids=None,
    refresh_task_rows=None,
    fail_inside_post_hook=False,
):
    raw_results = []
    run_queries = []
    refresh_task_rows = list(
        refresh_task_rows
        if refresh_task_rows is not None
        else [("1", "SUCCESS", None, None)]
    )

    class Adapter:
        def __init__(self):
            self.events = []
            self.hook_events = []

        def drop_relation(self, relation):
            self.events.append(("drop", relation))

        def rename_relation(self, from_relation, to_relation):
            self.events.append(("rename", from_relation, to_relation))

        def commit(self):
            self.events.append(("commit",))

    adapter = Adapter()
    target = FakeRelation(relation_type=None)

    def load_cached_relation(relation):
        if relation.identifier.endswith("__dbt_tmp"):
            return preexisting_intermediate_relation
        if relation.identifier.endswith("__dbt_backup"):
            return preexisting_backup_relation
        return existing_relation

    def run_query(sql):
        run_queries.append(sql)
        normalized_sql = " ".join(sql.lower().split())
        if normalized_sql.startswith("show create materialized view"):
            return SimpleNamespace(rows=[["my_model", show_create_sql]])
        if normalized_sql.startswith("select taskid from tasks"):
            return SimpleNamespace(
                rows=[[task_id] for task_id in (previous_task_ids or [])]
            )
        if normalized_sql.startswith("select taskid, status"):
            if not refresh_task_rows:
                return SimpleNamespace(rows=[])
            rows = (
                refresh_task_rows.pop(0)
                if len(refresh_task_rows) > 1
                else refresh_task_rows[0]
            )
            if rows is None:
                return SimpleNamespace(rows=[])
            if rows and isinstance(rows[0], (list, tuple)):
                return SimpleNamespace(rows=rows)
            return SimpleNamespace(rows=[rows])
        if normalized_sql.startswith("select sleep("):
            return SimpleNamespace(rows=[[0]])
        raise AssertionError(f"Unexpected run_query SQL: {sql}")

    def run_hooks(hooks, inside_transaction):
        adapter.hook_events.append((hooks[0], inside_transaction))
        if fail_inside_post_hook and hooks[0] == "post" and inside_transaction:
            raise CapturedCompilerError("inside post-hook failed")
        return ""

    runner = MacroRunner(
        *MATERIALIZED_VIEW_MACROS,
        "adapters/relation.sql",
        context={
            "adapter": adapter,
            "apply_grants": lambda *args, **kwargs: "",
            "config": FakeConfig(config),
            "execute": True,
            "load_cached_relation": load_cached_relation,
            "local_md5": lambda value: hashlib.md5(value.encode()).hexdigest(),
            "make_intermediate_relation": lambda relation: relation.incorporate(
                path={"identifier": relation.identifier + "__dbt_tmp"}
            ),
            "model": {},
            "post_hooks": ["post"],
            "pre_hooks": ["pre"],
            "run_hooks": run_hooks,
            "run_query": run_query,
            "should_full_refresh": lambda: full_refresh,
            "should_revoke": lambda *args, **kwargs: False,
            "store_raw_result": lambda **kwargs: raw_results.append(kwargs),
            "this": target,
        },
    )
    runner.run_queries = run_queries
    return runner, adapter, raw_results


def test_create_manual_materialized_view_uses_safe_defaults():
    runner = materialized_view_runner()

    sql = runner.sql(
        "doris__get_create_materialized_view_as_sql",
        FakeRelation(relation_type="materialized_view"),
        "select order_date, sum(amount) as sales from `dbt_test`.`orders` group by order_date",
    )

    assert sql.startswith(
        "create materialized view `dbt_test`.`my_model` "
        "build immediate refresh auto on manual"
    )
    assert "distributed by random buckets auto" in sql
    assert "comment 'dbt-doris:deployment-pending=" in sql
    assert sql.endswith(
        "as select order_date, sum(amount) as sales "
        "from `dbt_test`.`orders` group by order_date"
    )


def test_definition_hash_changes_with_model_sql_or_ddl_config():
    base = materialized_view_runner().sql(
        "doris__materialized_view_definition_hash",
        "select 1 as id",
    )
    same = materialized_view_runner().sql(
        "doris__materialized_view_definition_hash",
        "select 1 as id",
    )
    changed_sql = materialized_view_runner().sql(
        "doris__materialized_view_definition_hash",
        "select 2 as id",
    )
    changed_config = materialized_view_runner(
        config={"refresh_method": "complete"}
    ).sql(
        "doris__materialized_view_definition_hash",
        "select 1 as id",
    )

    assert base == same
    assert base != changed_sql
    assert base != changed_config


@pytest.mark.parametrize(
    (
        "existing_type",
        "definition_matches",
        "full_refresh",
        "config",
        "expected",
    ),
    [
        (None, False, False, {}, "create"),
        ("table", False, False, {}, "replace_type"),
        ("view", False, False, {}, "replace_type"),
        ("materialized_view", True, False, {}, "skip"),
        (
            "materialized_view",
            True,
            False,
            {"refresh_on_run": True},
            "refresh",
        ),
        ("materialized_view", False, False, {}, "replace"),
        (
            "materialized_view",
            False,
            False,
            {"on_configuration_change": "continue"},
            "continue",
        ),
        (
            "materialized_view",
            True,
            True,
            {"on_configuration_change": "continue"},
            "replace",
        ),
        (
            "materialized_view",
            "pending",
            False,
            {"on_configuration_change": "continue"},
            "replace",
        ),
        (
            "materialized_view",
            "pending",
            False,
            {"on_configuration_change": "fail"},
            "replace",
        ),
    ],
)
def test_materialized_view_action_is_idempotent_and_honors_change_policy(
    existing_type,
    definition_matches,
    full_refresh,
    config,
    expected,
):
    runner = materialized_view_runner(config=config)
    existing_relation = (
        FakeRelation(relation_type=existing_type) if existing_type else None
    )

    action = runner.render(
        "doris__materialized_view_action",
        existing_relation,
        definition_matches,
        full_refresh,
    )

    assert action == expected


def test_materialized_view_change_policy_can_fail_the_run():
    runner = materialized_view_runner(config={"on_configuration_change": "fail"})

    with pytest.raises(CapturedCompilerError, match="on_configuration_change.*fail"):
        runner.render(
            "doris__materialized_view_action",
            FakeRelation(relation_type="materialized_view"),
            False,
            False,
        )


def test_definition_match_reads_the_hash_from_show_create():
    expected_hash = materialized_view_runner().render(
        "doris__materialized_view_definition_hash",
        "select 1 as id",
    )
    runner = materialized_view_runner()
    runner.context.update(
        {
            "execute": True,
            "run_query": lambda sql: SimpleNamespace(
                rows=[
                    [
                        "my_model",
                        "create materialized view my_model "
                        f"comment 'dbt-doris:definition-hash={expected_hash}' "
                        "as select 1 as id",
                    ]
                ]
            ),
        }
    )

    assert runner.render(
        "doris__materialized_view_definition_matches",
        FakeRelation(relation_type="materialized_view"),
        "select 1 as id",
    )
    assert not runner.render(
        "doris__materialized_view_definition_matches",
        FakeRelation(relation_type="materialized_view"),
        "select 2 as id",
    )

    runner.context["run_query"] = lambda sql: SimpleNamespace(
        rows=[
            [
                "my_model",
                "create materialized view my_model "
                f"comment 'dbt-doris:deployment-pending={expected_hash}' "
                "as select 1 as id",
            ]
        ]
    )
    assert not runner.render(
        "doris__materialized_view_definition_matches",
        FakeRelation(relation_type="materialized_view"),
        "select 1 as id",
    )
    assert runner.render(
        "doris__materialized_view_definition_state",
        FakeRelation(relation_type="materialized_view"),
        "select 1 as id",
    ) == "pending"


def test_refresh_sql_uses_the_configured_doris_refresh_method():
    runner = materialized_view_runner(config={"refresh_method": "complete"})

    sql = runner.sql(
        "doris__get_refresh_materialized_view_sql",
        FakeRelation(relation_type="materialized_view"),
    )

    assert sql == "refresh materialized view `dbt_test`.`my_model` complete"


def test_core_materialized_view_dispatch_helpers_use_doris_sql():
    runner = materialized_view_runner(config={"refresh_method": "complete"})
    relation = FakeRelation(relation_type="materialized_view")

    assert runner.sql("doris__refresh_materialized_view", relation) == (
        "refresh materialized view `dbt_test`.`my_model` complete"
    )
    assert runner.sql("doris__drop_materialized_view", relation) == (
        "drop materialized view if exists `dbt_test`.`my_model`"
    )
    assert runner.sql(
        "doris__get_rename_materialized_view_sql",
        relation,
        "renamed`model",
    ) == (
        "alter materialized view `dbt_test`.`my_model` "
        "rename `renamed``model`"
    )


def test_replace_sql_uses_doris_atomic_materialized_view_swap():
    runner = materialized_view_runner()

    sql = runner.sql(
        "doris__get_swap_materialized_view_sql",
        FakeRelation(relation_type="materialized_view"),
        FakeRelation(
            identifier="my_model__dbt_tmp",
            relation_type="materialized_view",
        ),
    )

    assert sql == (
        "alter materialized view `dbt_test`.`my_model` "
        "replace with materialized view `my_model__dbt_tmp` "
        'properties("swap" = "true")'
    )


def test_deployment_complete_sql_replaces_the_pending_hash_marker():
    runner = materialized_view_runner(model={"description": "Daily sales"})

    sql = runner.sql(
        "doris__get_mark_materialized_view_deployment_complete_sql",
        FakeRelation(relation_type="materialized_view"),
        "select 1 as id",
    )

    assert sql.startswith(
        "alter table `dbt_test`.`my_model` modify comment "
        "'Daily sales dbt-doris:definition-hash="
    )
    assert "deployment-pending" not in sql


def test_materialization_first_immediate_run_builds_before_exposing_the_target():
    runner, adapter, raw_results = materialization_runner()

    result = runner.render("materialization_materialized_view_doris")

    assert result["relations"][0].type == "materialized_view"
    assert [statement.name for statement in runner.statements] == [
        "create_materialized_view_intermediate",
        "mark_materialized_view_deployment_complete",
        "drop_relation",
    ]
    assert runner.statements[0].sql.startswith(
        "create materialized view `dbt_test`.`my_model__dbt_tmp`"
    )
    assert "build immediate" in runner.statements[0].sql
    assert "deployment-pending=" in runner.statements[0].sql
    assert "definition-hash=" in runner.statements[1].sql
    assert [event[0] for event in adapter.events] == ["rename", "commit"]
    assert adapter.hook_events == [
        ("pre", False),
        ("pre", True),
        ("post", True),
        ("post", False),
    ]
    assert raw_results[0]["code"] == "CREATE MATERIALIZED VIEW"
    assert any(
        "select TaskId from tasks" in query
        for query in runner.run_queries
    )
    assert any("select TaskId, Status" in query for query in runner.run_queries)


def test_materialization_first_deferred_run_creates_the_target_without_waiting():
    runner, adapter, raw_results = materialization_runner(
        config={"build_mode": "deferred"}
    )

    runner.render("materialization_materialized_view_doris")

    assert [statement.name for statement in runner.statements] == [
        "main",
        "mark_materialized_view_deployment_complete",
        "drop_relation",
    ]
    assert runner.statements[0].sql.startswith(
        "create materialized view `dbt_test`.`my_model`"
    )
    assert "build deferred" in runner.statements[0].sql
    assert "definition-hash=" in runner.statements[1].sql
    assert not any("tasks('type'='mv')" in query for query in runner.run_queries)
    assert adapter.events == [("commit",)]
    assert raw_results == []


def test_materialization_same_definition_is_a_true_no_op():
    definition_hash = materialized_view_runner().render(
        "doris__materialized_view_definition_hash",
        "",
    )
    existing = FakeRelation(relation_type="materialized_view")
    runner, adapter, raw_results = materialization_runner(
        existing_relation=existing,
        show_create_sql=(
            "create materialized view my_model "
            f"comment 'dbt-doris:definition-hash={definition_hash}' as"
        ),
    )

    runner.render("materialization_materialized_view_doris")

    assert [statement.name for statement in runner.statements] == ["drop_relation"]
    assert adapter.events == []
    assert adapter.hook_events == [("pre", False), ("post", False)]
    assert raw_results[0]["code"] == "skip"


def test_materialization_definition_change_builds_then_atomically_swaps():
    existing = FakeRelation(relation_type="materialized_view")
    runner, adapter, raw_results = materialization_runner(
        existing_relation=existing,
        show_create_sql=(
            "create materialized view my_model "
            "comment 'dbt-doris:definition-hash=stale' as select 0"
        ),
    )

    runner.render("materialization_materialized_view_doris")

    assert [statement.name for statement in runner.statements] == [
        "create_materialized_view_intermediate",
        "main",
        "mark_materialized_view_deployment_complete",
        "drop_relation",
    ]
    assert "build immediate" in runner.statements[0].sql.lower()
    assert "replace with materialized view `my_model__dbt_tmp`" in (
        " ".join(runner.statements[1].sql.split())
    )
    assert 'properties("swap" = "true")' in runner.statements[1].sql
    assert adapter.events == [("commit",)]
    assert raw_results == []


def test_materialization_refresh_on_run_recognizes_a_lower_new_task_id():
    definition_hash = materialized_view_runner(
        config={"refresh_on_run": True}
    ).render(
        "doris__materialized_view_definition_hash",
        "",
    )
    existing = FakeRelation(relation_type="materialized_view")
    runner, adapter, raw_results = materialization_runner(
        existing_relation=existing,
        show_create_sql=(
            "create materialized view my_model "
            f"comment 'dbt-doris:definition-hash={definition_hash}' as"
        ),
        config={"refresh_on_run": True},
        previous_task_ids=["50", "41"],
        refresh_task_rows=[
            [
                ("50", "SUCCESS", None, "query-50"),
                ("41", "SUCCESS", None, "query-41"),
            ],
            [
                ("40", "RUNNING", None, "query-40"),
                ("50", "SUCCESS", None, "query-50"),
            ],
            [
                ("40", "SUCCESS", None, "query-40"),
                ("50", "SUCCESS", None, "query-50"),
            ],
        ],
    )

    runner.render("materialization_materialized_view_doris")

    assert [statement.name for statement in runner.statements] == [
        "main",
        "drop_relation",
    ]
    assert runner.statements[0].sql.endswith(" auto")
    refresh_queries = [
        query
        for query in runner.run_queries
        if "select TaskId, Status" in query
    ]
    assert refresh_queries
    assert all(">" not in query for query in refresh_queries)
    assert sum(query.lower().startswith("select sleep(") for query in runner.run_queries) == 2
    assert adapter.events == [("commit",)]
    assert raw_results == []


def test_materialization_does_not_swap_when_immediate_build_fails():
    existing = FakeRelation(relation_type="materialized_view")
    runner, adapter, _ = materialization_runner(
        existing_relation=existing,
        show_create_sql=(
            "create materialized view my_model "
            "comment 'dbt-doris:definition-hash=stale' as select 0"
        ),
        refresh_task_rows=[
            ("42", "FAILED", "base table was dropped", "query-42")
        ],
    )

    with pytest.raises(
        CapturedCompilerError,
        match="refresh failed.*task 42.*base table was dropped",
    ):
        runner.render("materialization_materialized_view_doris")

    assert [statement.name for statement in runner.statements] == [
        "create_materialized_view_intermediate",
    ]
    assert adapter.events == []


def test_wait_for_refresh_can_be_explicitly_disabled():
    runner, adapter, _ = materialization_runner(
        config={"wait_for_refresh": False},
        refresh_task_rows=[],
    )

    runner.render("materialization_materialized_view_doris")

    assert [event[0] for event in adapter.events] == ["rename", "commit"]
    assert not any("tasks('type'='mv')" in query for query in runner.run_queries)


def test_wait_for_refresh_reports_when_the_new_task_never_appears():
    runner, adapter, _ = materialization_runner(
        config={
            "refresh_wait_timeout": 1,
            "refresh_poll_interval": 1,
        },
        refresh_task_rows=[],
    )

    with pytest.raises(
        CapturedCompilerError,
        match="Timed out.*new Doris materialized view refresh task",
    ):
        runner.render("materialization_materialized_view_doris")

    assert adapter.events == []


def test_materialization_removes_a_stale_intermediate_before_recovery():
    stale_intermediate = FakeRelation(
        identifier="my_model__dbt_tmp",
        relation_type="materialized_view",
    )
    runner, adapter, _ = materialization_runner(
        preexisting_intermediate_relation=stale_intermediate,
        config={"build_mode": "deferred"},
    )

    runner.render("materialization_materialized_view_doris")

    assert adapter.events[0] == ("drop", stale_intermediate)
    assert adapter.events[-1] == ("commit",)


def test_materialization_restores_a_backup_before_retrying_a_type_switch():
    backup = FakeRelation(
        identifier="my_model__dbt_backup",
        relation_type="table",
    )
    runner, adapter, _ = materialization_runner(
        preexisting_backup_relation=backup,
        config={"build_mode": "deferred"},
    )

    runner.render("materialization_materialized_view_doris")

    assert adapter.events[0][0] == "rename"
    assert adapter.events[0][1] == backup
    assert adapter.events[0][2].identifier == "my_model"
    assert adapter.events[0][2].type == "table"
    assert [event[0] for event in adapter.events[1:]] == [
        "rename",
        "rename",
        "commit",
        "drop",
    ]


def test_failed_inside_post_hook_leaves_the_deployment_marker_pending():
    existing = FakeRelation(relation_type="materialized_view")
    runner, adapter, _ = materialization_runner(
        existing_relation=existing,
        show_create_sql=(
            "create materialized view my_model "
            "comment 'dbt-doris:definition-hash=stale' as select 0"
        ),
        config={"build_mode": "deferred"},
        fail_inside_post_hook=True,
    )

    with pytest.raises(CapturedCompilerError, match="post-hook failed"):
        runner.render("materialization_materialized_view_doris")

    assert [statement.name for statement in runner.statements] == [
        "create_materialized_view_intermediate",
        "main",
    ]
    assert "deployment-pending=" in runner.statements[0].sql
    assert ("commit",) not in adapter.events


@pytest.mark.parametrize("change_policy", ["continue", "fail"])
def test_pending_deployment_forces_recovery_before_change_policy(change_policy):
    existing = FakeRelation(relation_type="materialized_view")
    runner, adapter, _ = materialization_runner(
        existing_relation=existing,
        show_create_sql=(
            "create materialized view my_model "
            "comment 'dbt-doris:deployment-pending=old' as select 0"
        ),
        config={
            "build_mode": "deferred",
            "on_configuration_change": change_policy,
        },
    )

    runner.render("materialization_materialized_view_doris")

    assert [statement.name for statement in runner.statements] == [
        "create_materialized_view_intermediate",
        "main",
        "mark_materialized_view_deployment_complete",
        "drop_relation",
    ]
    assert "replace with materialized view" in runner.statements[1].sql.lower()
    assert adapter.events == [("commit",)]


def test_pending_type_switch_backup_is_retained_until_recovery_completes():
    existing = FakeRelation(relation_type="materialized_view")
    backup = FakeRelation(
        identifier="my_model__dbt_backup",
        relation_type="table",
    )
    runner, adapter, _ = materialization_runner(
        existing_relation=existing,
        preexisting_backup_relation=backup,
        show_create_sql=(
            "create materialized view my_model "
            "comment 'dbt-doris:deployment-pending=old' as select 0"
        ),
        config={
            "build_mode": "deferred",
            "on_configuration_change": "fail",
        },
    )

    runner.render("materialization_materialized_view_doris")

    assert adapter.events == [("commit",), ("drop", backup)]


def test_materialization_full_refresh_replaces_even_with_continue_policy():
    existing = FakeRelation(relation_type="materialized_view")
    runner, adapter, raw_results = materialization_runner(
        existing_relation=existing,
        show_create_sql="create materialized view my_model as select 1",
        config={
            "build_mode": "deferred",
            "on_configuration_change": "continue",
        },
        full_refresh=True,
    )

    runner.render("materialization_materialized_view_doris")

    assert [statement.name for statement in runner.statements] == [
        "create_materialized_view_intermediate",
        "main",
        "mark_materialized_view_deployment_complete",
        "drop_relation",
    ]
    assert "build deferred" in runner.statements[0].sql.lower()
    assert "replace with materialized view" in runner.statements[1].sql.lower()
    assert adapter.events == [("commit",)]
    assert raw_results == []


@pytest.mark.parametrize("existing_type", ["table", "view"])
def test_materialization_replaces_a_different_relation_type(existing_type):
    existing = FakeRelation(relation_type=existing_type)
    runner, adapter, raw_results = materialization_runner(
        existing_relation=existing,
        config={"build_mode": "deferred"},
    )

    runner.render("materialization_materialized_view_doris")

    assert [statement.name for statement in runner.statements] == [
        "create_materialized_view_intermediate",
        "mark_materialized_view_deployment_complete",
        "drop_relation",
    ]
    assert [event[0] for event in adapter.events] == [
        "rename",
        "rename",
        "commit",
        "drop",
    ]
    assert adapter.events[0][1] == existing
    assert adapter.events[0][2].identifier == "my_model__dbt_backup"
    assert adapter.events[1][1].identifier == "my_model__dbt_tmp"
    assert adapter.events[1][2].identifier == "my_model"
    assert raw_results[0]["code"] == "CREATE MATERIALIZED VIEW"


def test_table_materialization_does_not_exchange_with_an_existing_mv():
    events = []
    existing = FakeRelation(relation_type="materialized_view")

    class Adapter:
        @staticmethod
        def drop_relation(relation):
            events.append(("drop", relation))

        @staticmethod
        def rename_relation(from_relation, to_relation):
            events.append(("rename", from_relation, to_relation))

        @staticmethod
        def commit():
            events.append(("commit",))

    def load_cached_relation(relation):
        if relation.identifier.endswith("__dbt_tmp"):
            return None
        return existing

    runner = MacroRunner(
        "materializations/table/table.sql",
        "materializations/table/create_table_as.sql",
        "adapters/relation.sql",
        context={
            "adapter": Adapter(),
            "apply_grants": lambda *args, **kwargs: "",
            "config": FakeConfig(
                {
                    "unique_key": ["id"],
                    "distributed_by": ["id"],
                    "properties": {"replication_num": "1"},
                }
            ),
            "exchange_relation": lambda *args, **kwargs: events.append(("exchange",)),
            "get_assert_columns_equivalent": lambda sql: "",
            "get_table_columns_and_constraints": lambda: "`id` int",
            "load_cached_relation": load_cached_relation,
            "make_intermediate_relation": lambda relation: relation.incorporate(
                path={"identifier": relation.identifier + "__dbt_tmp"}
            ),
            "model": {},
            "persist_docs": lambda *args, **kwargs: "",
            "post_hooks": [],
            "pre_hooks": [],
            "run_hooks": lambda hooks, inside_transaction: "",
            "should_revoke": lambda *args, **kwargs: False,
            "this": FakeRelation(relation_type=None),
        },
    )

    runner.render("materialization_table_doris")

    assert [event[0] for event in events] == ["drop", "rename", "commit"]


def test_view_materialization_drops_an_existing_mv_through_the_adapter():
    events = []
    existing = FakeRelation(relation_type="materialized_view")

    class Adapter:
        @staticmethod
        def drop_relation(relation):
            events.append(("drop", relation))

        @staticmethod
        def commit():
            events.append(("commit",))

    runner = MacroRunner(
        "materializations/view/view.sql",
        "materializations/view/create_view_as.sql",
        "adapters/relation.sql",
        context={
            "adapter": Adapter(),
            "apply_grants": lambda *args, **kwargs: "",
            "config": FakeConfig(),
            "load_cached_relation": lambda relation: existing,
            "model": {},
            "persist_docs": lambda *args, **kwargs: "",
            "post_hooks": [],
            "pre_hooks": [],
            "run_hooks": lambda hooks, inside_transaction: "",
            "should_revoke": lambda *args, **kwargs: False,
            "this": FakeRelation(relation_type=None),
        },
    )

    runner.render("materialization_view_doris")

    assert [event[0] for event in events] == ["drop", "commit"]
    assert events[0][1] == existing


def test_create_scheduled_materialized_view_renders_doris_options_in_order():
    runner = materialized_view_runner(
        config={
            "build_mode": "deferred",
            "refresh_method": "complete",
            "refresh_trigger": "schedule",
            "refresh_schedule": {
                "interval": 1,
                "unit": "day",
                "start_time": "2099-08-01 02:00:00",
            },
            "duplicate_key": ["order_date", "customer_id"],
            "partition_by": "date_trunc(order_date, 'day')",
            "distribution_type": "hash",
            "distributed_by": ["customer_id"],
            "buckets": 8,
            "properties": {
                "replication_num": "1",
                "workload_group": "dbt_mv",
            },
        },
        model={"description": "Daily customer sales"},
    )

    sql = runner.sql(
        "doris__get_create_materialized_view_as_sql",
        FakeRelation(relation_type="materialized_view"),
        "select order_date, customer_id, sum(amount) as sales from orders "
        "group by order_date, customer_id",
    )

    expected_clauses = [
        "build deferred",
        "refresh complete on schedule every 1 day starts '2099-08-01 02:00:00'",
        "duplicate key (`order_date`, `customer_id`)",
        "comment 'Daily customer sales dbt-doris:deployment-pending=",
        "partition by (date_trunc(order_date, 'day'))",
        "distributed by hash (`customer_id`) buckets 8",
        'properties ("replication_num" = "1", "workload_group" = "dbt_mv")',
        "as select order_date",
    ]
    positions = [sql.index(clause) for clause in expected_clauses]
    assert positions == sorted(positions), sql


def test_create_on_commit_materialized_view_renders_doris_trigger():
    runner = materialized_view_runner(
        config={
            "build_mode": "deferred",
            "refresh_trigger": "commit",
        }
    )

    sql = runner.sql(
        "doris__get_create_materialized_view_as_sql",
        FakeRelation(relation_type="materialized_view"),
        "select 1 as id",
    )

    assert "build deferred refresh auto on commit" in sql


def test_create_schedule_supports_seconds():
    runner = materialized_view_runner(
        config={
            "build_mode": "deferred",
            "refresh_trigger": "schedule",
            "refresh_schedule": {"interval": 30, "unit": "second"},
        }
    )

    sql = runner.sql(
        "doris__get_create_materialized_view_as_sql",
        FakeRelation(relation_type="materialized_view"),
        "select 1 as id",
    )

    assert "refresh auto on schedule every 30 second" in sql


def test_create_sql_escapes_comments_properties_and_identifiers():
    runner = materialized_view_runner(
        config={
            "duplicate_key": ["odd`key"],
            "properties": {'quoted"key': 'c:\\path\\"value'},
        },
        model={"description": "it's c:\\data"},
    )

    sql = runner.sql(
        "doris__get_create_materialized_view_as_sql",
        FakeRelation(relation_type="materialized_view"),
        "select 1 as `odd``key`",
    )

    assert "duplicate key (`odd``key`)" in sql
    assert "comment 'it\\'s c:\\\\data dbt-doris:deployment-pending=" in sql
    assert '"quoted\\"key" = "c:\\\\path\\\\\\"value"' in sql


def test_invalid_build_mode_fails_before_sql_execution():
    runner = materialized_view_runner(config={"build_mode": "sometimes"})

    with pytest.raises(CapturedCompilerError, match="build_mode.*immediate.*deferred"):
        runner.sql(
            "doris__get_create_materialized_view_as_sql",
            FakeRelation(relation_type="materialized_view"),
            "select 1",
        )

    assert runner.statements == []


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"refresh_method": "delta"}, "refresh_method.*auto.*complete"),
        ({"refresh_trigger": "cron"}, "refresh_trigger.*manual.*schedule.*commit"),
        (
            {"refresh_trigger": "schedule"},
            "refresh_schedule.*required.*schedule",
        ),
        (
            {
                "refresh_trigger": "schedule",
                "refresh_schedule": {"interval": 0, "unit": "month"},
            },
            "refresh_schedule.interval.*positive",
        ),
        (
            {
                "refresh_trigger": "schedule",
                "refresh_schedule": {"interval": 1.5, "unit": "day"},
            },
            "refresh_schedule.interval.*positive integer",
        ),
        (
            {
                "refresh_trigger": "schedule",
                "refresh_schedule": {"interval": 1, "unit": "month"},
            },
            "refresh_schedule.unit.*second.*minute.*hour.*day.*week",
        ),
        (
            {
                "refresh_trigger": "schedule",
                "refresh_schedule": {
                    "interval": 1,
                    "unit": "day",
                    "start_time": 20990801,
                },
            },
            "refresh_schedule.start_time.*string",
        ),
        (
            {
                "refresh_trigger": "manual",
                "refresh_schedule": {"interval": 1, "unit": "day"},
            },
            "refresh_schedule.*only valid.*schedule",
        ),
    ],
)
def test_invalid_refresh_config_fails_before_sql_execution(config, message):
    runner = materialized_view_runner(config=config)

    with pytest.raises(CapturedCompilerError, match=message):
        runner.sql(
            "doris__get_create_materialized_view_as_sql",
            FakeRelation(relation_type="materialized_view"),
            "select 1",
        )

    assert runner.statements == []


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"distribution_type": "range"}, "distribution_type.*hash.*random"),
        (
            {"distribution_type": "hash"},
            "distributed_by.*required.*hash",
        ),
        (
            {
                "distribution_type": "random",
                "distributed_by": ["customer_id"],
            },
            "distributed_by.*only valid.*hash",
        ),
        ({"buckets": 0}, "buckets.*positive"),
        ({"buckets": "many"}, "buckets.*positive.*auto"),
    ],
)
def test_invalid_distribution_config_fails_before_sql_execution(config, message):
    runner = materialized_view_runner(config=config)

    with pytest.raises(CapturedCompilerError, match=message):
        runner.sql(
            "doris__get_create_materialized_view_as_sql",
            FakeRelation(relation_type="materialized_view"),
            "select 1",
        )

    assert runner.statements == []


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"duplicate_key": 1}, "duplicate_key.*string.*list"),
        (
            {
                "distribution_type": "hash",
                "distributed_by": [1],
            },
            "distributed_by.*non-empty string",
        ),
        ({"partition_by": ["order_date"]}, "partition_by.*string"),
        (
            {"partition_by": "order_date); drop table orders; --"},
            "partition_by.*unsafe",
        ),
        ({"properties": ["replication_num", "1"]}, "properties.*dictionary"),
        ({"refresh_on_run": "yes"}, "refresh_on_run.*true.*false"),
        ({"wait_for_refresh": "yes"}, "wait_for_refresh.*true.*false"),
        ({"refresh_wait_timeout": 0}, "refresh_wait_timeout.*positive integer"),
        ({"refresh_poll_interval": 0}, "refresh_poll_interval.*positive integer"),
        (
            {
                "refresh_wait_timeout": 1,
                "refresh_poll_interval": 2,
            },
            "refresh_poll_interval.*greater.*refresh_wait_timeout",
        ),
        ({"grants": {"select": ["analyst"]}}, "grants.*not supported"),
    ],
)
def test_invalid_ddl_config_fails_before_sql_execution(config, message):
    runner = materialized_view_runner(config=config)

    with pytest.raises(CapturedCompilerError, match=message):
        runner.sql(
            "doris__get_create_materialized_view_as_sql",
            FakeRelation(relation_type="materialized_view"),
            "select 1",
        )


def test_relation_listing_queries_async_materialized_view_metadata():
    runner = MacroRunner(
        "adapters/metadata.sql",
        context={
            "load_result": lambda name: SimpleNamespace(table=[]),
        },
    )

    runner.render("doris__list_relations_without_caching", FakeRelation())

    assert len(runner.statements) == 1
    sql = " ".join(runner.statements[0].sql.lower().split())
    assert "mv_infos(" in sql
    assert '"database" = "dbt_test"' in sql
    assert "'materialized_view'" in sql


def test_catalog_reports_async_materialized_views_as_materialized_views():
    runner = MacroRunner(
        "adapters/metadata.sql",
        context={
            "load_result": lambda name: SimpleNamespace(table=[]),
        },
    )

    runner.render(
        "doris__get_catalog",
        FakeRelation(schema="information_schema"),
        ["sales", "finance"],
    )

    assert len(runner.statements) == 1
    sql = " ".join(runner.statements[0].sql.lower().split())
    assert sql.count("mv_infos(") == 2
    assert '"database" = "sales"' in sql
    assert '"database" = "finance"' in sql
    assert "'materialized_view'" in sql


def test_relation_listing_returns_empty_when_schema_does_not_exist():
    class Adapter:
        Relation = DorisRelation

        @staticmethod
        def check_schema_exists(database, schema):
            return False

        @staticmethod
        def execute_macro(name, kwargs):
            raise AssertionError("metadata macro must not query a missing schema")

    relations = DorisAdapter.list_relations_without_caching(
        Adapter(),
        DorisRelation.create(schema="missing", identifier="schema"),
    )

    assert relations == []


def test_adapter_maps_async_materialized_views_to_the_dbt_relation_type():
    class Adapter:
        Relation = DorisRelation

        @staticmethod
        def check_schema_exists(database, schema):
            return True

        @staticmethod
        def execute_macro(name, kwargs):
            return [
                (None, "orders", "analytics", "table"),
                (None, "daily_sales", "analytics", "materialized_view"),
                (None, "active_customers", "analytics", "view"),
            ]

    relations = DorisAdapter.list_relations_without_caching(
        Adapter(),
        DorisRelation.create(schema="analytics", identifier="schema"),
    )

    assert [relation.type for relation in relations] == [
        RelationType.Table,
        RelationType.MaterializedView,
        RelationType.View,
    ]


def test_drop_async_materialized_view_uses_doris_two_word_relation_type():
    runner = MacroRunner("adapters/relation.sql")

    runner.render(
        "doris__drop_relation",
        FakeRelation(relation_type="materialized_view"),
    )

    assert [statement.sql for statement in runner.statements] == [
        "drop materialized view if exists `dbt_test`.`my_model`"
    ]


def test_rename_async_materialized_view_uses_doris_ddl():
    runner = MacroRunner("adapters/relation.sql")

    runner.render(
        "doris__rename_relation",
        FakeRelation(identifier="my_model__dbt_tmp", relation_type="materialized_view"),
        FakeRelation(relation_type="materialized_view"),
    )

    assert [statement.sql for statement in runner.statements] == [
        "drop materialized view if exists `dbt_test`.`my_model`",
        "alter materialized view `dbt_test`.`my_model__dbt_tmp` rename `my_model`",
    ]
