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

import pytest

import os
import random

import yaml
from dbt.tests.util import write_file

# Import the functional fixtures as a plugin
# Note: fixtures with session scope need to be local

pytest_plugins = ["dbt.tests.fixtures.project"]


@pytest.fixture(scope="class")
def prefix():
    """Keep every temporary test database under the requested Doris namespace.

    dbt-tests-adapter otherwise replaces ``DORIS_TEST_SCHEMA`` with a generic
    random name. Retaining a short random suffix keeps classes isolated while
    making every database created by the suite easy to identify and constrain.
    """
    schema_prefix = os.getenv("DORIS_TEST_SCHEMA", "dbt_test")
    return f"{schema_prefix}_{random.randint(0, 9999):04}"


# The profile dictionary, used to write out profiles.yml
@pytest.fixture(scope="class")
def dbt_profile_target():
    return {
        "type": "doris",
        "threads": 1,
        "host": os.getenv("DORIS_TEST_HOST", "127.0.0.1"),
        "port": int(os.getenv("DORIS_TEST_PORT", 9030)),
        "username": os.getenv("DORIS_TEST_USER", "root"),
        "password": os.getenv("DORIS_TEST_PASSWORD", ""),
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
