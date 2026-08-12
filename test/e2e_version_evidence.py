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

"""Machine-readable version evidence for live Doris functional tests."""

import hashlib
import json
import platform
import re
import secrets
import subprocess
from pathlib import Path

from dbt.adapters.doris.__version__ import version as adapter_version
from dbt.version import __version__ as dbt_core_version


EVIDENCE_PREFIX = "DORIS_E2E_VERSION_EVIDENCE="
_SCHEMA_PREFIX_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
_SCHEMA_PREFIX_RANDOM_WIDTH = 5
SCHEMA_PREFIX_RANDOM_SPACE = (
    len(_SCHEMA_PREFIX_ALPHABET) ** _SCHEMA_PREFIX_RANDOM_WIDTH
)

_FRONTEND_FIELDS = (
    ("name", "Name"),
    ("host", "Host"),
    ("role", "Role"),
    ("is_master", "IsMaster"),
    ("current_connected", "CurrentConnected"),
    ("alive", "Alive"),
    ("version", "Version"),
)
_BACKEND_FIELDS = (
    ("backend_id", "BackendId"),
    ("host", "Host"),
    ("node_role", "NodeRole"),
    ("alive", "Alive"),
    ("version", "Version"),
)


def _schema_prefix_nonce(nonce):
    if (
        not isinstance(nonce, int)
        or isinstance(nonce, bool)
        or not 0 <= nonce < SCHEMA_PREFIX_RANDOM_SPACE
    ):
        raise ValueError(
            "schema prefix nonce must be an integer in "
            f"[0, {SCHEMA_PREFIX_RANDOM_SPACE})."
        )

    characters = []
    for _ in range(_SCHEMA_PREFIX_RANDOM_WIDTH):
        nonce, remainder = divmod(nonce, len(_SCHEMA_PREFIX_ALPHABET))
        characters.append(_SCHEMA_PREFIX_ALPHABET[remainder])
    return "".join(reversed(characters))


def short_schema_prefix(configured_schema, nonce):
    """Build a human-recognizable prefix that leaves room for test module names."""
    normalized = re.sub(
        r"[^a-z0-9]+",
        "_",
        str(configured_schema).casefold(),
    ).strip("_")
    namespace = (normalized.replace("_", "") or "test")[:2]
    digest = hashlib.sha256(str(configured_schema).encode("utf-8")).hexdigest()[:3]
    return f"d_{namespace}_{digest}_{_schema_prefix_nonce(nonce)}"


def random_schema_prefix(configured_schema):
    """Build a short test prefix with a process-safe random nonce."""
    nonce = secrets.SystemRandom().randrange(SCHEMA_PREFIX_RANDOM_SPACE)
    return short_schema_prefix(configured_schema, nonce)


def adapter_git_state(repository_root):
    """Return the checked-out commit and whether its worktree has changes."""

    def git(*arguments):
        result = subprocess.run(
            ("git", *arguments),
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    try:
        git_root = Path(git("rev-parse", "--show-toplevel")).resolve()
        repository_root = Path(repository_root).resolve()
        if git_root != repository_root:
            return {"sha": "unavailable", "dirty": None}
        return {
            "sha": git("rev-parse", "HEAD"),
            "dirty": bool(
                git("status", "--porcelain", "--untracked-files=normal")
            ),
        }
    except (OSError, subprocess.CalledProcessError):
        return {"sha": "unavailable", "dirty": None}


def _node_evidence(rows, fields, node_type):
    evidence = []
    for row in rows:
        casefolded_row = {str(name).casefold(): value for name, value in row.items()}
        raw_version = casefolded_row.get("version")
        if raw_version is None or not str(raw_version).strip():
            raise RuntimeError(
                f"SHOW {node_type.upper()} did not return a non-empty Version."
            )
        node = {
            output_name: str(casefolded_row[source_name.casefold()])
            for output_name, source_name in fields
            if source_name.casefold() in casefolded_row
        }
        evidence.append(node)

    if not evidence:
        raise RuntimeError(f"SHOW {node_type.upper()} returned no Doris nodes.")
    return sorted(
        evidence,
        key=lambda node: (
            node.get("host", ""),
            node.get("name", node.get("backend_id", "")),
            node["version"],
        ),
    )


def doris_cluster_versions(connection):
    """Read exact FE and BE build strings from the connected Doris cluster."""
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute("show frontends")
        frontends = _node_evidence(
            cursor.fetchall(),
            _FRONTEND_FIELDS,
            "frontends",
        )
        cursor.execute("show backends")
        backends = _node_evidence(
            cursor.fetchall(),
            _BACKEND_FIELDS,
            "backends",
        )
    finally:
        cursor.close()
    return {"doris_frontends": frontends, "doris_backends": backends}


def version_evidence(connection, repository_root, endpoint):
    """Collect the complete runtime and server identity for one E2E session."""
    evidence = {
        "adapter_git": adapter_git_state(repository_root),
        "adapter_version": adapter_version,
        "dbt_core_version": str(dbt_core_version),
        "doris_endpoint": endpoint,
        "python_version": platform.python_version(),
    }
    evidence.update(doris_cluster_versions(connection))
    return evidence


def format_version_evidence(evidence):
    """Render one grep-friendly JSON line for CI logs and release records."""
    return EVIDENCE_PREFIX + json.dumps(
        evidence,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
