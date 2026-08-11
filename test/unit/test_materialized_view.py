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

import agate
import dbt.exceptions
import pytest

from dbt.adapters.contracts.relation import RelationType
from dbt.adapters.doris.impl import (
    DorisAdapter,
    _validate_doris_materialized_view_version,
)
from dbt.adapters.doris.relation import DorisRelation

from .macro_harness import CapturedCompilerError, FakeConfig, FakeRelation, MacroRunner

MATERIALIZED_VIEW_MACROS = (
    "materializations/materialized_view/materialized_view.sql",
    "materializations/table/create_table_as.sql",
)


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
    fail_grant_preflight=False,
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
            self.timeline = []

        def drop_relation(self, relation):
            self.events.append(("drop", relation))

        def rename_relation(self, from_relation, to_relation):
            self.events.append(("rename", from_relation, to_relation))

        @staticmethod
        def cache_added(relation):
            return None

        def commit(self):
            self.events.append(("commit",))

        def materialized_view_adapter_response(
            self,
            action,
            relation,
            refresh_task=None,
        ):
            return DorisAdapter.materialized_view_adapter_response(
                self,
                action,
                relation,
                refresh_task,
            )

        def validate_materialized_view_version(self, frontends_table):
            assert frontends_table.rows[0]["Version"].startswith("doris-")
            self.timeline.append(("version", frontends_table.rows[0]["Version"]))

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
        adapter.timeline.append(("query", normalized_sql))
        if normalized_sql == "show frontends":
            return SimpleNamespace(
                rows=[{"Version": "doris-4.1.2-test"}],
            )
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
        if (
            normalized_sql.startswith("create table")
            and "__dbt_backup" in normalized_sql
            and " as select * from " in normalized_sql
        ):
            return None
        raise AssertionError(f"Unexpected run_query SQL: {sql}")

    def run_hooks(hooks, inside_transaction):
        adapter.hook_events.append((hooks[0], inside_transaction))
        adapter.timeline.append(("hook", hooks[0], inside_transaction))
        if fail_inside_post_hook and hooks[0] == "post" and inside_transaction:
            raise CapturedCompilerError("inside post-hook failed")
        return ""

    def preflight_grants(relation, grant_config):
        adapter.timeline.append(("grants_preflight", grant_config))
        if fail_grant_preflight:
            raise CapturedCompilerError("grant principal does not exist")
        return ""

    runner = MacroRunner(
        *MATERIALIZED_VIEW_MACROS,
        "adapters/relation.sql",
        context={
            "adapter": adapter,
            "apply_grants": lambda *args, **kwargs: "",
            "config": FakeConfig(config),
            "doris__preflight_grants": preflight_grants,
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
            "store_result": lambda name, response, agate_table=None: raw_results.append(
                {
                    "name": name,
                    "response": response,
                    "message": str(response),
                    "code": response.code,
                    "rows_affected": response.rows_affected,
                }
            ),
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
        ("materialized_view", True, False, {}, "refresh"),
        (
            "materialized_view",
            True,
            False,
            {"refresh_trigger": " MANUAL "},
            "refresh",
        ),
        (
            "materialized_view",
            True,
            False,
            {"refresh_trigger": "schedule"},
            "skip",
        ),
        (
            "materialized_view",
            True,
            False,
            {"refresh_trigger": "commit"},
            "skip",
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


@pytest.mark.parametrize(
    ("refresh_method", "expected_method"),
    [
        ("auto", "auto"),
        ("complete", "complete"),
        (" COMPLETE ", "complete"),
    ],
)
def test_manual_refresh_sql_uses_the_configured_refresh_method(
    refresh_method,
    expected_method,
):
    runner = materialized_view_runner(
        config={
            "refresh_method": refresh_method,
            "refresh_trigger": "manual",
        }
    )
    relation = FakeRelation(relation_type="materialized_view")

    assert runner.sql(
        "doris__get_refresh_materialized_view_sql",
        relation,
    ) == (
        "refresh materialized view "
        f"`dbt_test`.`my_model` {expected_method}"
    )


def test_core_materialized_view_dispatch_helpers_use_doris_ddl():
    runner = materialized_view_runner(
        config={
            "refresh_method": "complete",
            "refresh_trigger": "manual",
        }
    )
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
    runner = materialized_view_runner(
        config={"persist_docs": {"relation": True}},
        model={"description": "Daily sales"},
    )

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


def test_relation_description_is_persisted_only_when_relation_docs_are_enabled():
    model = {"description": "Daily sales"}
    disabled = materialized_view_runner(model=model)
    enabled = materialized_view_runner(
        config={"persist_docs": {"relation": True}},
        model=model,
    )

    disabled_sql = disabled.sql(
        "doris__get_create_materialized_view_as_sql",
        FakeRelation(relation_type="materialized_view"),
        "select 1 as id",
    )
    enabled_sql = enabled.sql(
        "doris__get_create_materialized_view_as_sql",
        FakeRelation(relation_type="materialized_view"),
        "select 1 as id",
    )

    assert "Daily sales" not in disabled_sql
    assert "Daily sales dbt-doris:deployment-pending=" in enabled_sql
    assert disabled.render(
        "doris__materialized_view_definition_hash",
        "select 1 as id",
    ) != enabled.render(
        "doris__materialized_view_definition_hash",
        "select 1 as id",
    )


def test_column_descriptions_are_rendered_in_create_materialized_view():
    schema_probes = []
    runner = materialized_view_runner(
        config={"persist_docs": {"columns": True}},
        model={
            "name": "daily_sales",
            "columns": {
                "order_date": {"description": "Business date"},
                "sales": {"description": "It's total"},
            },
        },
    )
    runner.context.update(
        {
            "execute": True,
            "adapter": SimpleNamespace(
                get_column_schema_from_query=lambda sql: (
                    schema_probes.append(sql)
                    or [
                        SimpleNamespace(name="order_date"),
                        SimpleNamespace(name="sales"),
                        SimpleNamespace(name="undocumented"),
                    ]
                )
            ),
        }
    )

    sql = runner.sql(
        "doris__get_create_materialized_view_as_sql",
        FakeRelation(relation_type="materialized_view"),
        "select order_date, sales, 1 as undocumented from orders",
    )

    assert (
        "(`order_date` comment 'Business date', "
        "`sales` comment 'It\\'s total', `undocumented`)"
    ) in sql
    assert len(schema_probes) == 1
    normalized_probe = " ".join(schema_probes[0].lower().split())
    assert normalized_probe.startswith("select * from ( select order_date")
    assert "where false limit 0" in normalized_probe


def test_column_description_changes_the_hash_only_when_column_docs_are_enabled():
    first_model = {"columns": {"id": {"description": "First"}}}
    second_model = {"columns": {"id": {"description": "Second"}}}

    disabled_first = materialized_view_runner(model=first_model).render(
        "doris__materialized_view_definition_hash",
        "select 1 as id",
    )
    disabled_second = materialized_view_runner(model=second_model).render(
        "doris__materialized_view_definition_hash",
        "select 1 as id",
    )
    enabled_first = materialized_view_runner(
        config={"persist_docs": {"columns": True}},
        model=first_model,
    ).render(
        "doris__materialized_view_definition_hash",
        "select 1 as id",
    )
    enabled_second = materialized_view_runner(
        config={"persist_docs": {"columns": True}},
        model=second_model,
    ).render(
        "doris__materialized_view_definition_hash",
        "select 1 as id",
    )

    assert disabled_first == disabled_second
    assert enabled_first != enabled_second


def test_column_doc_quote_semantics_are_part_of_the_definition_hash():
    unquoted = materialized_view_runner(
        config={"persist_docs": {"columns": True}},
        model={
            "columns": {
                "SALES": {
                    "description": "Gross sales",
                    "quote": False,
                }
            }
        },
    ).render(
        "doris__materialized_view_definition_hash",
        "select 1 as sales",
    )
    quoted = materialized_view_runner(
        config={"persist_docs": {"columns": True}},
        model={
            "columns": {
                "SALES": {
                    "description": "Gross sales",
                    "quote": True,
                }
            }
        },
    ).render(
        "doris__materialized_view_definition_hash",
        "select 1 as sales",
    )

    assert unquoted != quoted


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
    assert not any(
        statement.sql.startswith("refresh materialized view")
        for statement in runner.statements
    )
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
    assert raw_results[0]["response"].task_id == "1"
    assert raw_results[0]["response"].task_status == "SUCCESS"
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
    assert raw_results[-1]["code"] == "CREATE MATERIALIZED VIEW"
    assert "refresh task" not in raw_results[-1]["message"]


def test_unchanged_manual_materialized_view_refreshes_and_waits():
    config = {
        "refresh_method": "complete",
        "refresh_trigger": "manual",
    }
    definition_hash = materialized_view_runner(config=config).render(
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
        config=config,
        previous_task_ids=["50"],
        refresh_task_rows=[
            ("49", "SUCCESS", None, "query-49"),
        ],
    )

    runner.render("materialization_materialized_view_doris")

    assert [statement.name for statement in runner.statements] == [
        "main",
        "drop_relation",
    ]
    assert runner.statements[0].sql == (
        "refresh materialized view `dbt_test`.`my_model` complete"
    )
    assert adapter.events == [("commit",)]
    assert adapter.hook_events == [
        ("pre", False),
        ("pre", True),
        ("post", True),
        ("post", False),
    ]
    assert any(
        "select TaskId from tasks" in query
        for query in runner.run_queries
    )
    assert any("select TaskId, Status" in query for query in runner.run_queries)
    assert raw_results[0]["code"] == "REFRESH MATERIALIZED VIEW"
    assert raw_results[0]["response"].task_id == "49"
    assert raw_results[0]["response"].task_status == "SUCCESS"
    assert raw_results[0]["response"].query_id == "query-49"


@pytest.mark.parametrize("refresh_trigger", ["schedule", "commit"])
def test_unchanged_database_triggered_materialized_view_is_a_no_op(
    refresh_trigger,
):
    config = {
        "refresh_method": "auto",
        "refresh_trigger": refresh_trigger,
    }
    if refresh_trigger == "schedule":
        config["refresh_schedule"] = {
            "interval": 1,
            "unit": "day",
        }
    definition_hash = materialized_view_runner(config=config).render(
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
        config=config,
    )

    runner.render("materialization_materialized_view_doris")

    assert [statement.name for statement in runner.statements] == [
        "drop_relation",
    ]
    assert adapter.events == []
    assert adapter.hook_events == [("pre", False), ("post", False)]
    assert not any("tasks('type'='mv')" in query for query in runner.run_queries)
    assert raw_results[0]["code"] == "skip"


def test_outside_pre_hook_runs_before_show_create_definition_inspection():
    existing = FakeRelation(relation_type="materialized_view")
    runner, adapter, _ = materialization_runner(
        existing_relation=existing,
        show_create_sql=(
            "create materialized view my_model "
            "comment 'dbt-doris:definition-hash=stale' as select 0"
        ),
        config={"build_mode": "deferred"},
    )

    runner.render("materialization_materialized_view_doris")

    outside_pre_hook = adapter.timeline.index(("hook", "pre", False))
    show_create = next(
        index
        for index, event in enumerate(adapter.timeline)
        if event[0] == "query"
        and event[1].startswith("show create materialized view")
    )
    assert outside_pre_hook < show_create


def test_invalid_grants_fail_before_definition_inspection_or_target_ddl():
    existing = FakeRelation(relation_type="materialized_view")
    runner, adapter, _ = materialization_runner(
        existing_relation=existing,
        config={
            "build_mode": "deferred",
            "grants": {"select": ["missing_user"]},
        },
        fail_grant_preflight=True,
    )

    with pytest.raises(
        CapturedCompilerError,
        match="grant principal does not exist",
    ):
        runner.render("materialization_materialized_view_doris")

    assert ("hook", "pre", False) in adapter.timeline
    assert ("version", "doris-4.1.2-test") in adapter.timeline
    assert (
        "grants_preflight",
        {"select": ["missing_user"]},
    ) in adapter.timeline
    assert not any(
        event[0] == "query"
        and event[1].startswith("show create materialized view")
        for event in adapter.timeline
    )
    assert runner.statements == []
    assert adapter.events == []


def test_materialization_definition_change_builds_then_atomically_swaps():
    existing = FakeRelation(relation_type="materialized_view")
    runner, adapter, raw_results = materialization_runner(
        existing_relation=existing,
        show_create_sql=(
            "create materialized view my_model "
            "comment 'dbt-doris:definition-hash=stale' as select 0"
        ),
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
    refresh_queries = [
        query
        for query in runner.run_queries
        if "select TaskId, Status" in query
    ]
    assert refresh_queries
    assert all(">" not in query for query in refresh_queries)
    assert sum(
        query.lower().startswith("select sleep(")
        for query in runner.run_queries
    ) == 2
    assert adapter.events == [("commit",)]
    assert raw_results[-1]["code"] == "REPLACE MATERIALIZED VIEW"
    assert "refresh task 40 SUCCESS" in raw_results[-1]["message"]
    assert "query query-40" in raw_results[-1]["message"]
    assert raw_results[-1]["response"].task_id == "40"
    assert raw_results[-1]["response"].task_status == "SUCCESS"
    assert raw_results[-1]["response"].query_id == "query-40"


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


def test_manual_refresh_can_submit_without_waiting_for_the_task():
    config = {
        "refresh_method": "auto",
        "refresh_trigger": "manual",
        "wait_for_refresh": False,
    }
    definition_hash = materialized_view_runner(config=config).render(
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
        config=config,
        refresh_task_rows=[],
    )

    runner.render("materialization_materialized_view_doris")

    assert [statement.name for statement in runner.statements] == [
        "main",
        "drop_relation",
    ]
    assert runner.statements[0].sql == (
        "refresh materialized view `dbt_test`.`my_model` auto"
    )
    assert adapter.events == [("commit",)]
    assert not any("tasks('type'='mv')" in query for query in runner.run_queries)
    assert raw_results[0]["code"] == "REFRESH MATERIALIZED VIEW"
    assert raw_results[0]["response"].task_id is None


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


def test_pending_replace_rolls_back_preserved_old_mv_before_retrying():
    existing = FakeRelation(relation_type="materialized_view")
    preserved_old_mv = FakeRelation(
        identifier="my_model__dbt_tmp",
        relation_type="materialized_view",
    )
    runner, adapter, _ = materialization_runner(
        existing_relation=existing,
        preexisting_intermediate_relation=preserved_old_mv,
        show_create_sql=(
            "create materialized view my_model "
            "comment 'dbt-doris:deployment-pending=old' as select 0"
        ),
        config={
            "build_mode": "deferred",
            "on_configuration_change": "continue",
        },
    )

    runner.render("materialization_materialized_view_doris")

    assert [statement.name for statement in runner.statements] == [
        "rollback_materialized_view",
        "create_materialized_view_intermediate",
        "main",
        "mark_materialized_view_deployment_complete",
        "drop_relation",
    ]
    assert "alter materialized view `dbt_test`.`my_model`" in (
        runner.statements[0].sql.lower()
    )
    assert "replace with materialized view `my_model__dbt_tmp`" in (
        " ".join(runner.statements[0].sql.lower().split())
    )
    assert adapter.events[0] == ("drop", preserved_old_mv)
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
    assert raw_results[-1]["code"] == "REPLACE MATERIALIZED VIEW"


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
    if existing_type == "view":
        assert [event[0] for event in adapter.events] == [
            "drop",
            "rename",
            "commit",
            "drop",
        ]
        assert adapter.events[0][1] == existing
        assert adapter.events[1][1].identifier == "my_model__dbt_tmp"
        assert adapter.events[1][2].identifier == "my_model"
        assert any(
            "create table `dbt_test`.`my_model__dbt_backup`" in query.lower()
            and "as select * from `dbt_test`.`my_model`" in query.lower()
            for query in runner.run_queries
        )
    else:
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


def test_table_materialization_preserves_existing_mv_until_table_is_ready():
    events = []
    existing = FakeRelation(relation_type="materialized_view")

    class Adapter:
        @staticmethod
        def quote(identifier):
            return f"`{identifier}`"

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
        if relation.identifier.endswith(("__dbt_tmp", "__dbt_backup")):
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
            "make_backup_relation": lambda relation, relation_type: (
                relation.incorporate(
                    path={"identifier": relation.identifier + "__dbt_backup"},
                    type=relation_type,
                )
            ),
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

    assert [event[0] for event in events] == [
        "rename",
        "rename",
        "commit",
        "drop",
        "drop",
    ]
    assert events[0][1] is existing
    assert events[0][2].identifier == "my_model__dbt_backup"
    assert events[0][2].type == "materialized_view"
    assert events[1][1].identifier == "my_model__dbt_tmp"
    assert events[1][2].identifier == "my_model"


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
            "persist_docs": {"relation": True},
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


def test_top_level_replication_num_is_rendered_as_a_materialized_view_property():
    runner = materialized_view_runner(config={"replication_num": "1"})

    sql = runner.sql(
        "doris__get_create_materialized_view_as_sql",
        FakeRelation(relation_type="materialized_view"),
        "select 1 as id",
    )

    assert 'properties ("replication_num" = "1")' in sql


def test_top_level_replication_num_overrides_and_merges_with_properties():
    runner = materialized_view_runner(
        config={
            "replication_num": "1",
            "properties": {
                "replication_num": "3",
                "workload_group": "dbt_mv",
            },
        }
    )

    sql = runner.sql(
        "doris__get_create_materialized_view_as_sql",
        FakeRelation(relation_type="materialized_view"),
        "select 1 as id",
    )

    assert (
        'properties ("replication_num" = "1", "workload_group" = "dbt_mv")'
        in sql
    )
    assert sql.count('"replication_num"') == 1


def test_top_level_replication_num_changes_the_materialized_view_definition_hash():
    replication_one = materialized_view_runner(
        config={
            "replication_num": "1",
            "properties": {"workload_group": "dbt_mv"},
        }
    ).render(
        "doris__materialized_view_definition_hash",
        "select 1 as id",
    )
    replication_two = materialized_view_runner(
        config={
            "replication_num": "2",
            "properties": {"workload_group": "dbt_mv"},
        }
    ).render(
        "doris__materialized_view_definition_hash",
        "select 1 as id",
    )

    assert replication_one != replication_two


def test_replication_num_integer_and_trimmed_string_are_canonical():
    integer_runner = materialized_view_runner(config={"replication_num": 1})
    string_runner = materialized_view_runner(config={"replication_num": " 1 "})

    integer_sql = integer_runner.sql(
        "doris__get_create_materialized_view_as_sql",
        FakeRelation(relation_type="materialized_view"),
        "select 1 as id",
    )
    string_sql = string_runner.sql(
        "doris__get_create_materialized_view_as_sql",
        FakeRelation(relation_type="materialized_view"),
        "select 1 as id",
    )

    assert integer_sql == string_sql
    assert '"replication_num" = "1"' in integer_sql


def test_equivalent_identifier_and_bucket_configs_have_one_definition_hash():
    scalar = materialized_view_runner(
        config={
            "duplicate_key": " id ",
            "distribution_type": "HASH",
            "distributed_by": " id ",
            "buckets": "AUTO",
        }
    )
    list_form = materialized_view_runner(
        config={
            "duplicate_key": ["id"],
            "distribution_type": "hash",
            "distributed_by": ["id"],
            "buckets": "auto",
        }
    )

    scalar_hash = scalar.render(
        "doris__materialized_view_definition_hash",
        "select 1 as id",
    )
    list_hash = list_form.render(
        "doris__materialized_view_definition_hash",
        "select 1 as id",
    )
    scalar_sql = scalar.sql(
        "doris__get_create_materialized_view_as_sql",
        FakeRelation(relation_type="materialized_view"),
        "select 1 as id",
    )
    list_sql = list_form.sql(
        "doris__get_create_materialized_view_as_sql",
        FakeRelation(relation_type="materialized_view"),
        "select 1 as id",
    )

    assert scalar_hash == list_hash
    assert scalar_sql == list_sql


def test_single_partition_list_matches_string_in_sql_and_definition_hash():
    string_runner = materialized_view_runner(config={"partition_by": "order_date"})
    list_runner = materialized_view_runner(config={"partition_by": ["order_date"]})
    relation = FakeRelation(relation_type="materialized_view")

    string_hash = string_runner.render(
        "doris__materialized_view_definition_hash",
        "select order_date from orders",
    )
    list_hash = list_runner.render(
        "doris__materialized_view_definition_hash",
        "select order_date from orders",
    )
    string_sql = string_runner.sql(
        "doris__get_create_materialized_view_as_sql",
        relation,
        "select order_date from orders",
    )
    list_sql = list_runner.sql(
        "doris__get_create_materialized_view_as_sql",
        relation,
        "select order_date from orders",
    )

    assert list_hash == string_hash
    assert list_sql == string_sql
    assert "partition by (order_date)" in list_sql


@pytest.mark.parametrize(
    "partition_by",
    [[], [""], [1], ["order_date", "region"]],
)
def test_partition_list_requires_exactly_one_non_empty_string(partition_by):
    runner = materialized_view_runner(config={"partition_by": partition_by})

    with pytest.raises(
        CapturedCompilerError,
        match="partition_by.*exactly one.*non-empty string",
    ):
        runner.sql(
            "doris__get_create_materialized_view_as_sql",
            FakeRelation(relation_type="materialized_view"),
            "select order_date, region from orders",
        )


@pytest.mark.parametrize(
    "partition_by",
    [
        "`order-date`",
        "分区列",
        "date_trunc(order_date, 'day')",
        "analytics.date_trunc(order_date, 'day')",
        "analytics . date_trunc(order_date, 'day')",
        "`analytics` . `date_trunc`(order_date, 'day')",
    ],
)
def test_partition_by_accepts_identifier_and_function_call_shapes(partition_by):
    sql = materialized_view_runner(config={"partition_by": partition_by}).sql(
        "doris__get_create_materialized_view_as_sql",
        FakeRelation(relation_type="materialized_view"),
        "select order_date from orders",
    )

    assert f"partition by ({partition_by})" in sql


@pytest.mark.parametrize(
    "partition_by",
    [
        "order_date, region",
        "date_trunc(order_date, 'day'), region",
        "(order_date, region)",
        "order_date + 1",
        "123",
        "date_trunc(order_date, 'day'",
        "`unterminated",
    ],
)
def test_partition_by_rejects_non_identifier_or_function_call(partition_by):
    runner = materialized_view_runner(config={"partition_by": partition_by})
    with pytest.raises(
        CapturedCompilerError,
        match="partition_by.*identifier.*function call",
    ):
        runner.sql(
            "doris__get_create_materialized_view_as_sql",
            FakeRelation(relation_type="materialized_view"),
            "select order_date, region from orders",
        )


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


def test_create_schedule_rejects_test_only_seconds():
    runner = materialized_view_runner(
        config={
            "build_mode": "deferred",
            "refresh_trigger": "schedule",
            "refresh_schedule": {"interval": 30, "unit": "second"},
        }
    )

    with pytest.raises(
        CapturedCompilerError,
        match="refresh_schedule.unit.*test-only",
    ):
        runner.sql(
            "doris__get_create_materialized_view_as_sql",
            FakeRelation(relation_type="materialized_view"),
            "select 1 as id",
        )


def test_create_sql_escapes_comments_properties_and_identifiers():
    runner = materialized_view_runner(
        config={
            "duplicate_key": ["odd`key"],
            "properties": {'quoted"key': 'c:\\path\\"value'},
            "persist_docs": {"relation": True},
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
            "refresh_schedule.unit.*minute.*hour.*day.*week",
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
        (
            {"partition_by": "order_date); drop table orders; --"},
            "partition_by.*unsafe",
        ),
        ({"properties": ["replication_num", "1"]}, "properties.*dictionary"),
        ({"properties": {"": "1"}}, "property name.*non-empty string"),
        (
            {"properties": {"replication_num": {"count": 1}}},
            "property.*replication_num.*string, number, or boolean",
        ),
        ({"replication_num": 0}, "replication_num.*positive integer"),
        ({"replication_num": "many"}, "replication_num.*positive integer"),
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


def test_grants_are_valid_materialized_view_config():
    runner = materialized_view_runner(
        config={"grants": {"select": ["analyst"]}}
    )

    sql = runner.sql(
        "doris__get_create_materialized_view_as_sql",
        FakeRelation(relation_type="materialized_view"),
        "select 1",
    )

    assert sql.startswith("create materialized view")


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


@pytest.mark.parametrize(
    "version",
    [
        "doris-0.0.0-2f26860a79c",
        "doris-2.1.5-release",
        "doris-2.1.10-release",
        "doris-3.0.1-release",
        "doris-3.1.0-release",
        "doris-4.1.2-rc01-build",
    ],
)
def test_materialized_view_version_contract_accepts_configured_gate_versions(
    version,
):
    table = agate.Table(
        [(version, "Yes")],
        ["Version", "CurrentConnected"],
    )

    _validate_doris_materialized_view_version(table)


@pytest.mark.parametrize(
    "version",
    [
        "doris-0.0.0",
        "doris-0.0.0-abcdef",
        "doris-0.0.0-not-a-git-sha",
        "doris-0.0.0-2f26860a79c-dirty",
        "doris-2.1.4-release",
        "doris-3.0.0-release",
    ],
)
def test_materialized_view_version_contract_rejects_versions_outside_gate(
    version,
):
    table = agate.Table(
        [(version, "Yes")],
        ["Version", "CurrentConnected"],
    )

    with pytest.raises(
        dbt.exceptions.DbtRuntimeError,
        match="does not pass.*version gate",
    ):
        _validate_doris_materialized_view_version(table)


def test_materialized_view_version_contract_uses_the_connected_frontend():
    table = agate.Table(
        [
            ("doris-3.0.0-release", "No"),
            ("doris-4.1.2-release", "Yes"),
        ],
        ["Version", "CurrentConnected"],
    )

    _validate_doris_materialized_view_version(table)


def test_materialized_view_version_contract_also_validates_master_frontend():
    table = agate.Table(
        [
            ("doris-4.1.2-release", "Yes", "No"),
            ("doris-3.0.0-release", "No", "Yes"),
        ],
        ["Version", "CurrentConnected", "IsMaster"],
    )

    with pytest.raises(
        dbt.exceptions.DbtRuntimeError,
        match="Doris FE version doris-3.0.0.*does not pass",
    ):
        _validate_doris_materialized_view_version(table)


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
