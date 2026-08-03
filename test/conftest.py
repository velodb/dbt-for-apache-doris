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

import os
from pathlib import Path

import mysql.connector
import pytest
import yaml
from dbt.tests.util import write_file

from test.e2e_version_evidence import (
    enforce_expected_doris_version,
    format_version_evidence,
    random_schema_prefix,
    version_evidence,
)

# Import the functional fixtures as a plugin
# Note: fixtures with session scope need to be local

pytest_plugins = ["dbt.tests.fixtures.project"]

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FUNCTIONAL_TEST_ROOT = PROJECT_ROOT / "test" / "functional"


def _doris_test_connection_settings():
    return {
        "host": os.getenv("DORIS_TEST_HOST", "127.0.0.1"),
        "port": int(os.getenv("DORIS_TEST_PORT", 9030)),
        "username": os.getenv("DORIS_TEST_USER", "root"),
        "password": os.getenv("DORIS_TEST_PASSWORD", ""),
    }


def _session_has_functional_tests(items):
    return any(
        FUNCTIONAL_TEST_ROOT in Path(str(item.path)).resolve().parents
        for item in items
    )


def _emit_version_evidence(request, evidence):
    evidence_line = format_version_evidence(evidence)
    terminal_reporter = request.config.pluginmanager.get_plugin("terminalreporter")
    if terminal_reporter is None:
        print(evidence_line, flush=True)
    else:
        terminal_reporter.write_line(evidence_line)


@pytest.fixture(scope="session", autouse=True)
def doris_e2e_version_evidence(request):
    """Print exact client, adapter, FE, and BE versions before live tests."""
    if not _session_has_functional_tests(request.session.items):
        return

    settings = _doris_test_connection_settings()
    connection = mysql.connector.connect(
        host=settings["host"],
        port=settings["port"],
        user=settings["username"],
        password=settings["password"],
        connection_timeout=10,
    )
    try:
        evidence = version_evidence(
            connection,
            PROJECT_ROOT,
            f"{settings['host']}:{settings['port']}",
        )
    finally:
        connection.close()

    expected_version = os.environ.get("DORIS_TEST_EXPECTED_VERSION")
    try:
        evidence["doris_version_gate"] = enforce_expected_doris_version(
            evidence,
            expected_version,
        )
    except RuntimeError as error:
        evidence["doris_version_gate"] = {
            "error": str(error),
            "expected_release": expected_version,
            "reported_build": None,
            "status": "failed",
        }
        _emit_version_evidence(request, evidence)
        raise

    _emit_version_evidence(request, evidence)


@pytest.fixture(scope="class")
def prefix():
    """Keep every temporary test database under the requested Doris namespace.

    dbt-tests-adapter otherwise replaces ``DORIS_TEST_SCHEMA`` with a generic
    random name. Retaining a short random suffix keeps classes isolated while
    making every database created by the suite easy to identify and constrain.
    """
    schema_prefix = os.getenv("DORIS_TEST_SCHEMA", "dbt_test")
    return random_schema_prefix(schema_prefix)


# The profile dictionary, used to write out profiles.yml
@pytest.fixture(scope="class")
def dbt_profile_target():
    settings = _doris_test_connection_settings()
    return {
        "type": "doris",
        "threads": 1,
        "host": settings["host"],
        "port": settings["port"],
        "username": settings["username"],
        "password": settings["password"],
        "schema": os.getenv("DORIS_TEST_SCHEMA", "dbt_test"),
    }


@pytest.fixture(scope="class")
def doris_test_replication_num():
    """Replica count for tables built by the test suite.

    Doris defaults to 3 replicas, which fails outright on a single-BE cluster:

        replication num should be less than the number of available backends.
        replication num is 3, available backend num is 1

    Development and CI clusters are usually single-BE, so default to 1 replica and
    let DORIS_TEST_REPLICATION_NUM raise it for multi-BE runs.
    """
    return os.getenv("DORIS_TEST_REPLICATION_NUM", "1")


@pytest.fixture(scope="class")
def dbt_project_yml(project_root, project_config_update, doris_test_replication_num):
    """Write dbt_project.yml with a Doris replica count applied to every resource.

    This overrides the dbt-tests-adapter fixture of the same name, rather than
    `project_config_update`, on purpose. Inherited test classes such as
    BaseEphemeral and BaseGenericTests define `project_config_update` themselves,
    so anything set there is replaced instead of merged. `dbt_project_yml` is the
    point where that dictionary is combined with the defaults, so injecting here
    covers inherited tests as well as local ones.

    Resource-level config still wins, so a test that needs a different replica
    count can set `+properties` on its own models.
    """
    project_config = {
        "name": "test",
        "profile": "test",
        "flags": {"send_anonymous_usage_stats": False},
    }
    if project_config_update:
        if isinstance(project_config_update, dict):
            project_config.update(project_config_update)
        elif isinstance(project_config_update, str):
            project_config.update(yaml.safe_load(project_config_update))

    properties = {"replication_num": doris_test_replication_num}
    for resource_type in ("models", "seeds", "snapshots"):
        resource_config = project_config.setdefault(resource_type, {})
        existing = resource_config.get("+properties") or {}
        # Keep whatever the test asked for; only supply what is missing.
        resource_config["+properties"] = {**properties, **existing}

    write_file(yaml.safe_dump(project_config), project_root, "dbt_project.yml")
    return project_config
