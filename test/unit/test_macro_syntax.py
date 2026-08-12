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

"""Static checks over the shipped macros. No cluster, no dbt project."""

import re
from collections import defaultdict

import pytest
from dbt_common.clients.jinja import get_environment

from .macro_harness import macro_files, read_macro_file, top_level_blocks


@pytest.mark.parametrize("rel_path", macro_files())
def test_macro_file_parses(rel_path):
    """Every macro file compiles with dbt's Jinja environment.

    A syntax error here fails at runtime inside a dbt run, in a traceback that
    points at the rendered template rather than the file. Catching it statically
    is the whole reason this module can run without a cluster.
    """
    env = get_environment(None, capture_macros=False)
    env.from_string(read_macro_file(rel_path))


@pytest.mark.parametrize("rel_path", macro_files())
def test_macro_file_has_license_header(rel_path):
    """Apache RAT gates the release; every file needs the ASF header."""
    assert "Licensed to the Apache Software Foundation" in read_macro_file(rel_path)


def test_macro_names_are_unique():
    """No two files may define the same macro.

    dbt loads all of them into one namespace, and the last one parsed silently
    wins -- there is no warning about the shadowed definition.
    """
    seen = defaultdict(list)
    for rel_path in macro_files():
        for block in top_level_blocks(rel_path):
            seen[block.block_name].append(rel_path)
    duplicates = {name: paths for name, paths in seen.items() if len(paths) > 1}
    assert duplicates == {}, f"macros defined more than once: {duplicates}"


def test_materializations_are_adapter_scoped():
    """Materializations must be registered for the doris adapter, not `default`.

    A `default` materialization is global: installed alongside another adapter it
    would take over that adapter's models too.
    """
    pattern = re.compile(r"{%-?\s*materialization\s+(\w+)\s*,\s*([^%]*?)-?%}")
    found = []
    for rel_path in macro_files():
        for name, args in pattern.findall(read_macro_file(rel_path)):
            found.append((rel_path, name, args.strip()))

    assert found, "expected the adapter to ship materializations"
    for rel_path, name, args in found:
        assert "adapter=" in args, (
            f"materialization {name!r} in {rel_path} is registered as {args!r}; "
            "it must be adapter-scoped, e.g. materialization "
            f"{name}, adapter='doris'"
        )
