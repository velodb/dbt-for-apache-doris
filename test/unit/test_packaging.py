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

"""Source packaging checks for the supported dbt runtime."""

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def read_setup_py():
    return (PROJECT_ROOT / "setup.py").read_text()


def test_setup_pins_dbt_core_1_12():
    setup_py = read_setup_py()
    version = re.search(r'dbt_core_version\s*=\s*"([^"]+)"', setup_py)

    assert version
    assert version.group(1) == "1.12.0"
    assert '"dbt-core~={}".format(dbt_core_version)' in setup_py


def test_setup_requires_python_3_10():
    assert 'python_requires=">=3.10"' in read_setup_py()


def test_docs_directory_is_not_published_in_git():
    gitignore_lines = (PROJECT_ROOT / ".gitignore").read_text().splitlines()

    assert "/docs/" in gitignore_lines


def test_docs_are_excluded_from_distributions():
    manifest = (PROJECT_ROOT / "MANIFEST.in").read_text()

    assert "\nprune docs\n" in manifest


def test_functional_runner_is_included_in_source_distributions():
    manifest = (PROJECT_ROOT / "MANIFEST.in").read_text()

    assert "recursive-include scripts *.py" in manifest
    assert "include test/doris_test.env.example" in manifest


def test_runtime_and_development_connector_floors_match():
    setup_py = read_setup_py()
    development_requirements = (PROJECT_ROOT / "dev-requirements.txt").read_text()

    requirement = "mysql-connector-python>=8.0.33"
    assert f'"{requirement}"' in setup_py
    assert requirement in development_requirements
