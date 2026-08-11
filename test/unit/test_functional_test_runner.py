#!/usr/bin/env python
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

from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import run_doris_functional_tests as runner

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def settings(**overrides):
    return runner.resolve_settings(overrides)


def version_evidence(
    backend_count=1,
    frontend_version="doris-4.1.3-rc01-fe",
    backend_version=None,
):
    backend_version = backend_version or frontend_version
    return {
        "doris_frontends": [
            {
                "alive": "true",
                "host": "127.0.0.1",
                "version": frontend_version,
            }
        ],
        "doris_backends": [
            {
                "alive": "true",
                "host": f"127.0.0.{index + 2}",
                "version": backend_version,
            }
            for index in range(backend_count)
        ],
    }


class FakeCursor:
    def __init__(self, fixed_database_exists=False):
        self.fixed_database_exists = fixed_database_exists
        self.executed = []
        self.current_sql = None
        self.closed = False

    def execute(self, sql, parameters=None):
        self.current_sql = sql
        self.executed.append((sql, parameters))

    def fetchone(self):
        if "information_schema.schemata" in self.current_sql:
            if self.fixed_database_exists:
                return (runner.FIXED_CROSS_DATABASE,)
            return None
        return (1,)

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, fixed_database_exists=False):
        self.cursor_instance = FakeCursor(fixed_database_exists)
        self.closed = False

    def cursor(self):
        return self.cursor_instance

    def close(self):
        self.closed = True


def test_example_configuration_is_parseable():
    values = runner.load_config_file(PROJECT_ROOT / "test/doris_test.env.example")

    assert values["DORIS_TEST_HOST"] == "127.0.0.1"
    assert values["DORIS_TEST_PASSWORD"] == ""
    assert "DORIS_TEST_EXPECTED_VERSION" not in values
    assert "DORIS_TEST_SUITE" not in values


def test_config_parser_handles_export_quotes_hashes_and_equals(tmp_path):
    config = tmp_path / "doris.env"
    config.write_text(
        "# comment\n"
        "export DORIS_TEST_PASSWORD='pa=ss # still secret'\n"
        "DORIS_TEST_HOST=fe.example.test\n"
    )

    assert runner.load_config_file(config) == {
        "DORIS_TEST_PASSWORD": "pa=ss # still secret",
        "DORIS_TEST_HOST": "fe.example.test",
    }


@pytest.mark.parametrize(
    "contents, message",
    [
        ("NOT_A_DORIS_KEY=value\n", "unknown configuration key"),
        (
            "DORIS_TEST_HOST=one\nDORIS_TEST_HOST=two\n",
            "duplicate configuration key",
        ),
        ("DORIS_TEST_HOST\n", "expected KEY=VALUE"),
        ("DORIS_TEST_HOST='unterminated\n", "invalid quoted value"),
        (
            "DORIS_TEST_EXPECTED_VERSION=4.1.3\n",
            "unknown configuration key",
        ),
        (
            "DORIS_TEST_SUITE=core\n",
            "unknown configuration key",
        ),
    ],
)
def test_config_parser_rejects_invalid_input(tmp_path, contents, message):
    config = tmp_path / "bad.env"
    config.write_text(contents)

    with pytest.raises(runner.RunnerError, match=message):
        runner.load_config_file(config)


def test_missing_config_has_copy_instruction(tmp_path):
    with pytest.raises(runner.RunnerError, match="doris_test.env.example"):
        runner.load_config_file(tmp_path / "missing.env")


def test_config_parser_rejects_non_utf8_input(tmp_path):
    config = tmp_path / "non-utf8.env"
    config.write_bytes(b"DORIS_TEST_HOST=\xff\n")

    with pytest.raises(runner.RunnerError, match="valid UTF-8"):
        runner.load_config_file(config)


@pytest.mark.parametrize(
    "key, value, message",
    [
        ("DORIS_TEST_HOST", "", "HOST must not be empty"),
        ("DORIS_TEST_USER", "", "USER must not be empty"),
        ("DORIS_TEST_PORT", "0", "between 1 and 65535"),
        ("DORIS_TEST_PORT", "not-a-port", "must be an integer"),
        ("DORIS_TEST_REPLICATION_NUM", "0", "at least 1"),
        ("DORIS_TEST_SCHEMA", "mysql", "system schema"),
        ("DORIS_TEST_SCHEMA", "bad-schema", "letters, digits, and underscores"),
    ],
)
def test_settings_validation(key, value, message):
    with pytest.raises(runner.RunnerError, match=message):
        settings(**{key: value})


def test_pytest_environment_removes_ambient_runner_configuration():
    environment = settings().pytest_environment(
        {
            "PATH": "/bin",
            "DBT_LOG_FORMAT": "json",
            "DORIS_TEST_EXPECTED_VERSION": "stale",
            "DORIS_TEST_OTHER": "stale",
            "PYTEST_ADDOPTS": "-n auto",
            "PYTEST_PLUGINS": "xdist.plugin",
        }
    )

    assert environment["PATH"] == "/bin"
    assert environment["DBT_LOG_FORMAT"] == "json"
    assert "DORIS_TEST_EXPECTED_VERSION" not in environment
    assert "DORIS_TEST_OTHER" not in environment
    assert "PYTEST_ADDOPTS" not in environment
    assert "PYTEST_PLUGINS" not in environment


def test_version_reader_can_load_when_started_outside_repository(monkeypatch):
    project_root = str(runner.PROJECT_ROOT)
    monkeypatch.setattr(
        runner.sys,
        "path",
        [path for path in runner.sys.path if path != project_root],
    )

    read_versions = runner._load_version_reader()

    assert runner.sys.path[0] == project_root
    assert callable(read_versions)


def test_preflight_checks_connection_versions_replication_and_fixed_database():
    connection = FakeConnection()
    connect_calls = []

    def connect(**kwargs):
        connect_calls.append(kwargs)
        return connection

    summary = runner.preflight_connection(
        settings(),
        connect=connect,
        read_versions=lambda _: version_evidence(
            backend_count=2,
            frontend_version="doris-0.0.0-source123",
            backend_version="doris-2.1.4-release",
        ),
    )

    assert connect_calls == [
        {
            "host": "127.0.0.1",
            "port": 9030,
            "user": "root",
            "password": "",
            "connection_timeout": 10,
        }
    ]
    assert summary.frontend_count == 1
    assert summary.backend_count == 2
    assert summary.versions == (
        "doris-0.0.0-source123",
        "doris-2.1.4-release",
    )
    assert connection.cursor_instance.executed == [
        ("select 1", None),
        (
            "select schema_name from information_schema.schemata "
            "where schema_name = %s",
            (runner.FIXED_CROSS_DATABASE,),
        ),
    ]
    assert connection.cursor_instance.closed is True
    assert connection.closed is True


def test_preflight_rejects_existing_fixed_database_and_closes_connection():
    connection = FakeConnection(fixed_database_exists=True)

    with pytest.raises(runner.RunnerError, match="already exists"):
        runner.preflight_connection(
            settings(),
            connect=lambda **_: connection,
            read_versions=lambda _: version_evidence(),
        )

    assert connection.closed is True


def test_preflight_rejects_replication_above_live_backend_count():
    connection = FakeConnection()

    with pytest.raises(runner.RunnerError, match="exceeds.*live backends"):
        runner.preflight_connection(
            settings(DORIS_TEST_REPLICATION_NUM="2"),
            connect=lambda **_: connection,
            read_versions=lambda _: version_evidence(),
        )

    assert connection.closed is True


@pytest.mark.parametrize(
    "parallel_arguments",
    [
        ["-n", "2"],
        ["-n2"],
        ["-nauto"],
        ["-nlogical"],
        ["--numprocesses=2"],
        ["--tx=2*popen"],
        ["--px", "popen"],
        ["--dist=load"],
        ["-d"],
        ["-f"],
        ["-p", "xdist.plugin"],
        ["-pxdist.plugin"],
    ],
)
def test_pytest_command_rejects_parallel_execution(parallel_arguments):
    with pytest.raises(runner.RunnerError, match="Parallel pytest"):
        runner.build_pytest_command(parallel_arguments)


def test_pytest_command_does_not_exclude_grant_tests_by_default():
    command = runner.build_pytest_command([])

    assert command == [
        runner.sys.executable,
        "-m",
        "pytest",
        "-p",
        "no:xdist",
        "-p",
        "no:xdist.looponfail",
        runner.FUNCTIONAL_TEST_PATH,
    ]


def test_run_uses_repo_root_isolated_environment_and_returns_pytest_code(
    monkeypatch,
):
    monkeypatch.setenv("DORIS_TEST_HOST", "ambient-host-must-not-leak")
    monkeypatch.setenv("DORIS_TEST_EXPECTED_VERSION", "stale-gate-must-not-leak")
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=7)

    test_settings = settings(DORIS_TEST_PASSWORD="sentinel-secret")
    return_code = runner.run_functional_tests(test_settings, ["-q"], run=run)

    assert return_code == 7
    command, kwargs = calls[0]
    assert command[-1] == "-q"
    assert "sentinel-secret" not in " ".join(command)
    assert kwargs["cwd"] == runner.PROJECT_ROOT
    assert kwargs["check"] is False
    assert "shell" not in kwargs
    assert kwargs["env"]["DORIS_TEST_HOST"] == "127.0.0.1"
    assert kwargs["env"]["DORIS_TEST_PASSWORD"] == "sentinel-secret"
    assert "DORIS_TEST_EXPECTED_VERSION" not in kwargs["env"]


def test_main_preflight_only_does_not_run_tests(
    tmp_path,
    monkeypatch,
    capsys,
):
    config = tmp_path / "preflight.env"
    config.write_text("DORIS_TEST_PASSWORD=sentinel-secret\n")
    monkeypatch.setattr(
        runner,
        "preflight_connection",
        lambda _: runner.ClusterSummary(1, 1, ("doris-4.1.3-release",)),
    )
    monkeypatch.setattr(
        runner,
        "run_functional_tests",
        lambda *_: pytest.fail("pytest must not run for --preflight-only"),
    )

    return_code = runner.main(["--config", str(config), "--preflight-only"])

    captured = capsys.readouterr()
    assert return_code == 0
    assert "preflight passed" in captured.out
    assert "sentinel-secret" not in captured.out
    assert "sentinel-secret" not in captured.err


def test_main_runs_after_configuration_and_preflight(tmp_path, monkeypatch, capsys):
    config = tmp_path / "run.env"
    config.write_text("DORIS_TEST_HOST=fe.test\n")
    calls = []

    def preflight(test_settings):
        calls.append(("preflight", test_settings.host))
        return runner.ClusterSummary(1, 1, ("doris-4.1.3-release",))

    def run_tests(test_settings, pytest_args):
        calls.append(("pytest", test_settings.host, pytest_args))
        return 0

    monkeypatch.setattr(runner, "preflight_connection", preflight)
    monkeypatch.setattr(runner, "run_functional_tests", run_tests)

    assert runner.main(["--config", str(config)]) == 0
    assert calls == [("preflight", "fe.test"), ("pytest", "fe.test", [])]
    output = capsys.readouterr().out
    assert "tests are destructive" in output
    assert "creates and drops temporary users" in output


def test_main_redacts_preflight_errors_and_does_not_run_tests(
    tmp_path,
    monkeypatch,
    capsys,
):
    config = tmp_path / "failed-preflight.env"
    config.write_text("DORIS_TEST_PASSWORD=sentinel-secret\n")
    monkeypatch.setattr(
        runner,
        "preflight_connection",
        lambda _: (_ for _ in ()).throw(
            runner.RunnerError("connection failed: sentinel-secret")
        ),
    )
    monkeypatch.setattr(
        runner,
        "run_functional_tests",
        lambda *_: pytest.fail("pytest must not run after a failed preflight"),
    )

    assert runner.main(["--config", str(config)]) == 2
    captured = capsys.readouterr()
    assert "<redacted>" in captured.err
    assert "sentinel-secret" not in captured.out
    assert "sentinel-secret" not in captured.err


def test_main_returns_interrupted_status(monkeypatch, tmp_path, capsys):
    config = tmp_path / "interrupt.env"
    config.write_text("")
    monkeypatch.setattr(
        runner,
        "preflight_connection",
        lambda _: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    assert runner.main(["--config", str(config)]) == 130
    assert "Interrupted" in capsys.readouterr().err
