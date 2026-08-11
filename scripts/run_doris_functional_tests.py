#!/usr/bin/env python3
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

"""Run the dbt Adapter Functional suite against an explicit Doris cluster."""

import argparse
import os
import re
import shlex
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "test" / "doris_test.env"
FUNCTIONAL_TEST_PATH = "test/functional"
FIXED_CROSS_DATABASE = "cross_db_test"

_CONFIG_KEYS = {
    "DORIS_TEST_HOST",
    "DORIS_TEST_PORT",
    "DORIS_TEST_USER",
    "DORIS_TEST_PASSWORD",
    "DORIS_TEST_SCHEMA",
    "DORIS_TEST_REPLICATION_NUM",
}
_DEFAULT_VALUES = {
    "DORIS_TEST_HOST": "127.0.0.1",
    "DORIS_TEST_PORT": "9030",
    "DORIS_TEST_USER": "root",
    "DORIS_TEST_PASSWORD": "",
    "DORIS_TEST_SCHEMA": "dbt_test",
    "DORIS_TEST_REPLICATION_NUM": "1",
}
_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
_SCHEMA_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_SYSTEM_SCHEMAS = {
    "information_schema",
    "mysql",
    "performance_schema",
    "sys",
}


class RunnerError(RuntimeError):
    """A user-actionable runner error."""


@dataclass(frozen=True)
class DorisTestSettings:
    host: str
    port: int
    username: str
    password: str
    schema: str
    replication_num: int

    def pytest_environment(self, base_environment=None):
        """Return an isolated environment for the pytest subprocess."""
        source = os.environ if base_environment is None else base_environment
        environment = {
            key: value
            for key, value in source.items()
            if not key.startswith("DORIS_TEST_")
            and key not in {"PYTEST_ADDOPTS", "PYTEST_PLUGINS"}
        }
        environment.update(
            {
                "DORIS_TEST_HOST": self.host,
                "DORIS_TEST_PORT": str(self.port),
                "DORIS_TEST_USER": self.username,
                "DORIS_TEST_PASSWORD": self.password,
                "DORIS_TEST_SCHEMA": self.schema,
                "DORIS_TEST_REPLICATION_NUM": str(self.replication_num),
            }
        )
        return environment


@dataclass(frozen=True)
class ClusterSummary:
    frontend_count: int
    backend_count: int
    versions: tuple[str, ...]


def _parse_value(raw_value, path, line_number):
    if not raw_value:
        return ""
    if raw_value[0] not in {"'", '"'}:
        return raw_value
    try:
        values = shlex.split(raw_value, comments=False, posix=True)
    except ValueError as error:
        raise RunnerError(
            f"{path}:{line_number}: invalid quoted value: {error}."
        ) from error
    if len(values) != 1:
        raise RunnerError(
            f"{path}:{line_number}: a quoted value must contain exactly one value."
        )
    return values[0]


def load_config_file(path):
    """Read a strict, non-executable KEY=VALUE configuration file."""
    path = Path(path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as error:
        raise RunnerError(
            f"Configuration file not found: {path}. Copy "
            "test/doris_test.env.example to test/doris_test.env first."
        ) from error
    except UnicodeDecodeError as error:
        raise RunnerError(f"Configuration file must be valid UTF-8: {path}.") from error
    except OSError as error:
        raise RunnerError(f"Cannot read configuration file {path}: {error}.") from error

    values = {}
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            raise RunnerError(
                f"{path}:{line_number}: expected KEY=VALUE, got a line without '='."
            )
        key, raw_value = line.split("=", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if _KEY_PATTERN.fullmatch(key) is None:
            raise RunnerError(
                f"{path}:{line_number}: invalid configuration key {key!r}."
            )
        if key not in _CONFIG_KEYS:
            raise RunnerError(f"{path}:{line_number}: unknown configuration key {key}.")
        if key in values:
            raise RunnerError(
                f"{path}:{line_number}: duplicate configuration key {key}."
            )
        values[key] = _parse_value(raw_value, path, line_number)
    return values


def _parse_integer(values, key, minimum, maximum=None):
    raw_value = values[key]
    try:
        value = int(raw_value)
    except ValueError as error:
        raise RunnerError(f"{key} must be an integer, got {raw_value!r}.") from error
    if value < minimum or (maximum is not None and value > maximum):
        if maximum is None:
            expected = f"at least {minimum}"
        else:
            expected = f"between {minimum} and {maximum}"
        raise RunnerError(f"{key} must be {expected}, got {value}.")
    return value


def resolve_settings(file_values):
    """Apply defaults and validate every supported setting."""
    values = {**_DEFAULT_VALUES, **file_values}
    host = values["DORIS_TEST_HOST"].strip()
    username = values["DORIS_TEST_USER"].strip()
    schema = values["DORIS_TEST_SCHEMA"].strip()
    if not host:
        raise RunnerError("DORIS_TEST_HOST must not be empty.")
    if not username:
        raise RunnerError("DORIS_TEST_USER must not be empty.")
    if _SCHEMA_PATTERN.fullmatch(schema) is None:
        raise RunnerError(
            "DORIS_TEST_SCHEMA must start with a letter and contain only letters, "
            "digits, and underscores."
        )
    if schema.casefold() in _SYSTEM_SCHEMAS:
        raise RunnerError(f"DORIS_TEST_SCHEMA must not be a system schema: {schema}.")

    port = _parse_integer(values, "DORIS_TEST_PORT", 1, 65535)
    replication_num = _parse_integer(
        values,
        "DORIS_TEST_REPLICATION_NUM",
        1,
    )

    return DorisTestSettings(
        host=host,
        port=port,
        username=username,
        password=values["DORIS_TEST_PASSWORD"],
        schema=schema,
        replication_num=replication_num,
    )


def _is_live(node, node_type):
    raw_alive = node.get("alive")
    alive = "" if raw_alive is None else str(raw_alive).strip().casefold()
    if alive not in {"true", "false"}:
        raise RunnerError(
            f"SHOW {node_type} returned an invalid Alive value: {raw_alive!r}."
        )
    return alive == "true"


def _fixed_cross_database_exists(connection):
    cursor = connection.cursor()
    try:
        cursor.execute("select 1")
        cursor.fetchone()
        cursor.execute(
            "select schema_name from information_schema.schemata "
            "where schema_name = %s",
            (FIXED_CROSS_DATABASE,),
        )
        return cursor.fetchone() is not None
    finally:
        cursor.close()


def _load_version_reader():
    project_root = str(PROJECT_ROOT)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    from test.e2e_version_evidence import doris_cluster_versions

    return doris_cluster_versions


def inspect_cluster(connection, settings, read_versions=None):
    """Validate non-destructive cluster prerequisites for the suite."""
    if read_versions is None:
        read_versions = _load_version_reader()

    try:
        evidence = read_versions(connection)
    except RuntimeError as error:
        raise RunnerError(str(error)) from error

    live_frontends = [
        node for node in evidence["doris_frontends"] if _is_live(node, "FRONTENDS")
    ]
    live_backends = [
        node for node in evidence["doris_backends"] if _is_live(node, "BACKENDS")
    ]
    if not live_frontends:
        raise RunnerError("Doris preflight found no live frontend.")
    if not live_backends:
        raise RunnerError("Doris preflight found no live backend.")
    if settings.replication_num > len(live_backends):
        raise RunnerError(
            "DORIS_TEST_REPLICATION_NUM exceeds the number of live backends: "
            f"{settings.replication_num} requested, {len(live_backends)} live."
        )
    if _fixed_cross_database_exists(connection):
        raise RunnerError(
            f"Database {FIXED_CROSS_DATABASE!r} already exists. The Functional "
            "suite creates and drops that fixed database, so the runner refuses "
            "to continue. Use a dedicated test cluster and remove or rename the "
            "existing database yourself only if it is safe."
        )

    versions = tuple(
        sorted({node["version"] for node in (*live_frontends, *live_backends)})
    )
    return ClusterSummary(
        frontend_count=len(live_frontends),
        backend_count=len(live_backends),
        versions=versions,
    )


def preflight_connection(settings, connect=None, read_versions=None):
    """Connect to Doris, inspect the cluster, and always close the connection."""
    connection_errors = ()
    if connect is None:
        try:
            import mysql.connector
        except ModuleNotFoundError as error:
            raise RunnerError(
                "mysql-connector-python is not installed. Run "
                "'python -m pip install -r dev-requirements.txt' first."
            ) from error
        connect = mysql.connector.connect
        connection_errors = (mysql.connector.Error,)

    try:
        connection = connect(
            host=settings.host,
            port=settings.port,
            user=settings.username,
            password=settings.password,
            connection_timeout=10,
        )
        try:
            return inspect_cluster(
                connection,
                settings,
                read_versions=read_versions,
            )
        finally:
            connection.close()
    except connection_errors as error:
        raise RunnerError(
            f"Cannot connect to or inspect Doris at {settings.host}:{settings.port}: "
            f"{error}."
        ) from error


def _reject_parallel_pytest(pytest_args):
    parallel_flags = {
        "-d",
        "-f",
        "-n",
        "--dist",
        "--looponfail",
        "--numprocesses",
        "--px",
        "--tx",
    }
    for index, argument in enumerate(pytest_args):
        loads_xdist_plugin = (
            argument == "-p"
            and index + 1 < len(pytest_args)
            and "xdist" in pytest_args[index + 1].casefold()
        ) or (argument.startswith("-p") and "xdist" in argument.casefold())
        if (
            argument in parallel_flags
            or argument.startswith("-n=")
            or re.fullmatch(r"-n(?:auto|logical|[0-9]+)", argument) is not None
            or any(
                argument.startswith(f"{flag}=")
                for flag in {"--dist", "--numprocesses", "--px", "--tx"}
            )
            or loads_xdist_plugin
        ):
            raise RunnerError(
                "Parallel pytest execution is not supported. The suite currently "
                f"uses the fixed database {FIXED_CROSS_DATABASE!r}."
            )


def build_pytest_command(pytest_args):
    _reject_parallel_pytest(pytest_args)
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "no:xdist",
        "-p",
        "no:xdist.looponfail",
        FUNCTIONAL_TEST_PATH,
    ]
    command.extend(pytest_args)
    return command


def run_functional_tests(settings, pytest_args, run=subprocess.run):
    command = build_pytest_command(pytest_args)
    completed = run(
        command,
        cwd=PROJECT_ROOT,
        env=settings.pytest_environment(),
        check=False,
    )
    return completed.returncode


def _warn_config_permissions(path, settings):
    if os.name != "posix" or not settings.password:
        return
    try:
        mode = stat.S_IMODE(Path(path).stat().st_mode)
    except OSError:
        return
    if mode & 0o077:
        print(
            "WARNING: the configuration contains a password and is readable by "
            f"other users (mode {mode:04o}). Consider: chmod 600 {path}",
            file=sys.stderr,
        )


def _print_settings(settings):
    print("Doris Functional test configuration:")
    print(f"  endpoint: {settings.host}:{settings.port}")
    print(f"  user: {settings.username}")
    print(f"  password: {'set (hidden)' if settings.password else 'empty'}")
    print(f"  test schema namespace: {settings.schema}")
    print(f"  replication number: {settings.replication_num}")


def _print_cluster_summary(summary):
    print("Doris cluster preflight passed:")
    print(f"  live frontends: {summary.frontend_count}")
    print(f"  live backends: {summary.backend_count}")
    print(f"  reported versions: {', '.join(summary.versions)}")


def _redact(message, secret):
    if not secret:
        return message
    return message.replace(secret, "<redacted>")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Validate a Doris test cluster and run the dbt Adapter Functional suite."
        )
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"KEY=VALUE configuration file (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="validate configuration and cluster state without running pytest",
    )
    parser.add_argument(
        "pytest_args",
        nargs=argparse.REMAINDER,
        help="pytest arguments after '--', for example: -- -k snapshot -vv",
    )
    arguments = parser.parse_args(argv)
    if arguments.pytest_args[:1] == ["--"]:
        arguments.pytest_args = arguments.pytest_args[1:]
    return arguments


def main(argv=None):
    password = ""
    try:
        arguments = parse_args(argv)
        file_values = load_config_file(arguments.config)
        settings = resolve_settings(file_values)
        password = settings.password
        _reject_parallel_pytest(arguments.pytest_args)

        _warn_config_permissions(arguments.config, settings)
        _print_settings(settings)
        summary = preflight_connection(settings)
        _print_cluster_summary(summary)
        if arguments.preflight_only:
            return 0

        print(
            "WARNING: tests are destructive and must not run concurrently or "
            "against a production/shared cluster."
        )
        print(
            "WARNING: the suite creates and drops temporary users and executes "
            "GRANT/REVOKE; the configured account must have those privileges."
        )
        return run_functional_tests(settings, arguments.pytest_args)
    except RunnerError as error:
        print(f"ERROR: {_redact(str(error), password)}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
