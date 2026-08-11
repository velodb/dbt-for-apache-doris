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

import json
from types import SimpleNamespace

import pytest

from test import conftest as test_conftest
from test import e2e_version_evidence as version_evidence_module
from test.e2e_version_evidence import (
    EVIDENCE_PREFIX,
    SCHEMA_PREFIX_RANDOM_SPACE,
    adapter_git_state,
    doris_cluster_versions,
    format_version_evidence,
    random_schema_prefix,
    short_schema_prefix,
)


class FakeCursor:
    def __init__(self, results):
        self.results = results
        self.executed = []
        self.closed = False

    def execute(self, sql):
        self.executed.append(sql)

    def fetchall(self):
        return self.results[self.executed[-1]]

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, results):
        self.cursor_instance = FakeCursor(results)
        self.cursor_options = []

    def cursor(self, **kwargs):
        self.cursor_options.append(kwargs)
        return self.cursor_instance


class FakeEvidenceConnection:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeTerminalReporter:
    def __init__(self):
        self.lines = []

    def write_line(self, line):
        self.lines.append(line)


def test_short_schema_prefix_is_stable_readable_and_bounded():
    configured_schema = "dbt_incremental_release_2_1_10_with_a_very_long_suffix"

    prefix = short_schema_prefix(configured_schema, 42)

    assert prefix == short_schema_prefix(configured_schema, 42)
    assert prefix.startswith("d_db_")
    assert prefix.endswith("_00016")
    assert len(prefix) <= 14
    longest_known_name = (
        f"{prefix}_test_doris_materialized_view_complete_mv_custom"
    )
    assert len(longest_known_name) <= 62
    assert 64 - len(longest_known_name) >= 2


def test_short_schema_prefix_nonce_boundaries_are_unique():
    assert SCHEMA_PREFIX_RANDOM_SPACE == 36**5
    assert SCHEMA_PREFIX_RANDOM_SPACE > 1_000_000
    assert short_schema_prefix("dbt_test", 0).endswith("_00000")
    assert short_schema_prefix(
        "dbt_test",
        SCHEMA_PREFIX_RANDOM_SPACE - 1,
    ).endswith("_zzzzz")

    sample = {
        short_schema_prefix("dbt_test", nonce)
        for nonce in range(10_000)
    }
    assert len(sample) == 10_000


@pytest.mark.parametrize("nonce", [-1, SCHEMA_PREFIX_RANDOM_SPACE])
def test_short_schema_prefix_rejects_out_of_range_nonce(nonce):
    with pytest.raises(ValueError, match="schema prefix nonce"):
        short_schema_prefix("dbt_test", nonce)


def test_random_schema_prefix_uses_system_random(monkeypatch):
    bounds = []

    class FakeSystemRandom:
        @staticmethod
        def randrange(stop):
            bounds.append(stop)
            return stop - 1

    monkeypatch.setattr(
        version_evidence_module.secrets,
        "SystemRandom",
        FakeSystemRandom,
    )

    assert random_schema_prefix("dbt_test").endswith("_zzzzz")
    assert bounds == [SCHEMA_PREFIX_RANDOM_SPACE]


def test_adapter_git_state_is_unavailable_outside_a_checkout(tmp_path):
    assert adapter_git_state(tmp_path) == {
        "sha": "unavailable",
        "dirty": None,
    }


def test_doris_cluster_versions_records_every_exact_fe_and_be_build():
    connection = FakeConnection(
        {
            "show frontends": [
                {
                    "Name": "fe-1",
                    "Host": "10.0.0.1",
                    "Alive": "true",
                    "Version": "doris-2.1.10-release-abc123",
                    "CurrentConnected": "Yes",
                    "IsMaster": "true",
                },
                {
                    "Name": "fe-2",
                    "Host": "10.0.0.2",
                    "Alive": "true",
                    "Version": "doris-2.1.10-release-def456",
                    "CurrentConnected": "No",
                    "IsMaster": "false",
                },
            ],
            "show backends": [
                {
                    "BackendId": 7,
                    "Host": "10.0.0.3",
                    "Alive": "true",
                    "Version": "doris-2.1.10-release-be789",
                }
            ],
        }
    )

    evidence = doris_cluster_versions(connection)

    assert connection.cursor_options == [{"dictionary": True}]
    assert connection.cursor_instance.executed == ["show frontends", "show backends"]
    assert connection.cursor_instance.closed is True
    assert [node["version"] for node in evidence["doris_frontends"]] == [
        "doris-2.1.10-release-abc123",
        "doris-2.1.10-release-def456",
    ]
    assert evidence["doris_backends"] == [
        {
            "alive": "true",
            "backend_id": "7",
            "host": "10.0.0.3",
            "version": "doris-2.1.10-release-be789",
        }
    ]


@pytest.mark.parametrize("node_type", ["frontends", "backends"])
def test_doris_cluster_versions_rejects_sql_null_version(node_type):
    results = {
        "show frontends": [
            {
                "Name": "fe-1",
                "Host": "10.0.0.1",
                "Alive": "true",
                "Version": "doris-3.0.8-rc01-fe123",
            }
        ],
        "show backends": [
            {
                "BackendId": 7,
                "Host": "10.0.0.2",
                "Alive": "true",
                "Version": "doris-3.0.8-rc01-be456",
            }
        ],
    }
    results[f"show {node_type}"][0]["Version"] = None
    connection = FakeConnection(results)

    with pytest.raises(RuntimeError, match="non-empty Version"):
        doris_cluster_versions(connection)

    assert connection.cursor_instance.closed is True


def _functional_session_request(terminal_reporter):
    pluginmanager = SimpleNamespace(
        get_plugin=lambda name: terminal_reporter if name == "terminalreporter" else None
    )
    return SimpleNamespace(
        config=SimpleNamespace(pluginmanager=pluginmanager),
        session=SimpleNamespace(items=[]),
    )


def _mixed_release_evidence():
    return {
        "doris_frontends": [
            {"alive": "true", "version": "doris-3.0.8-rc01"}
        ],
        "doris_backends": [
            {"alive": "true", "version": "doris-3.1.4-rc02"}
        ],
    }


def test_functional_session_emits_observed_versions_without_a_gate(
    monkeypatch,
):
    evidence = _mixed_release_evidence()
    connection = FakeEvidenceConnection()
    terminal_reporter = FakeTerminalReporter()
    request = _functional_session_request(terminal_reporter)
    monkeypatch.setattr(test_conftest, "_session_has_functional_tests", lambda _: True)
    monkeypatch.setattr(
        test_conftest.mysql.connector,
        "connect",
        lambda **kwargs: connection,
    )
    monkeypatch.setattr(
        test_conftest,
        "version_evidence",
        lambda connection, repository_root, endpoint: evidence,
    )
    test_conftest.doris_e2e_version_evidence.__wrapped__(request)

    assert connection.closed is True
    assert len(terminal_reporter.lines) == 1
    assert terminal_reporter.lines[0].startswith(EVIDENCE_PREFIX)
    emitted = json.loads(
        terminal_reporter.lines[0].removeprefix(EVIDENCE_PREFIX)
    )
    assert emitted["doris_frontends"] == evidence["doris_frontends"]
    assert emitted["doris_backends"] == evidence["doris_backends"]
    assert "doris_version_gate" not in emitted


def test_format_version_evidence_is_one_deterministic_json_line():
    evidence = {
        "python_version": "3.12.13",
        "doris_frontends": [{"version": "doris-4.1.2-rc01-build"}],
    }

    rendered = format_version_evidence(evidence)

    assert rendered.count("\n") == 0
    assert rendered.startswith(EVIDENCE_PREFIX)
    assert json.loads(rendered.removeprefix(EVIDENCE_PREFIX)) == evidence
