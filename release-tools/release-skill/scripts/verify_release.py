#!/usr/bin/env python3
"""Read-only release gates for dbt-for-apache-doris."""

import argparse
import json
import re
import subprocess
import tarfile
import urllib.error
import urllib.request
import zipfile
from email.parser import BytesParser
from pathlib import Path
from runpy import run_path

DIST_NAME = "dbt-for-apache-doris"
NORMALIZED_NAME = "dbt_for_apache_doris"
CANONICAL_VERSION = Path("dbt/adapters/doris/__version__.py")
DBT_PROJECT = Path("dbt/include/doris/dbt_project.yml")
RELEASE_WORKFLOW = Path(".github/workflows/release.yml")


class Gate:
    def __init__(self):
        self.failures = []
        self.warnings = []

    def check(self, condition, message):
        if condition:
            print(f"PASS: {message}")
        else:
            print(f"FAIL: {message}")
            self.failures.append(message)

    def warn(self, condition, message):
        if not condition:
            print(f"WARN: {message}")
            self.warnings.append(message)


def command(repo, *args, check=True):
    completed = subprocess.run(
        args,
        cwd=repo,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"{' '.join(args)} failed: {detail}")
    return completed


def validate_version(raw):
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[A-Za-z0-9.-]+)?", raw) is None:
        raise argparse.ArgumentTypeError(f"invalid release version: {raw!r}")
    return raw


def read_metadata_from_wheel(path):
    with zipfile.ZipFile(path) as archive:
        candidates = [
            name
            for name in archive.namelist()
            if name.endswith(".dist-info/METADATA")
        ]
        if len(candidates) != 1:
            raise RuntimeError(f"{path.name}: expected one METADATA file")
        return BytesParser().parsebytes(archive.read(candidates[0]))


def read_metadata_from_sdist(path):
    with tarfile.open(path) as archive:
        candidates = [
            member
            for member in archive.getmembers()
            if member.name.count("/") == 1
            and member.name.endswith("/PKG-INFO")
        ]
        if len(candidates) != 1:
            raise RuntimeError(f"{path.name}: expected one PKG-INFO file")
        extracted = archive.extractfile(candidates[0])
        if extracted is None:
            raise RuntimeError(f"{path.name}: cannot read PKG-INFO")
        return BytesParser().parsebytes(extracted.read())


def check_source(repo, version, gate):
    version_file = repo / CANONICAL_VERSION
    gate.check(version_file.is_file(), f"canonical version file exists: {CANONICAL_VERSION}")
    if not version_file.is_file():
        return

    canonical = run_path(str(version_file)).get("version")
    gate.check(canonical == version, f"canonical version is {version}")

    setup_text = (repo / "setup.py").read_text(encoding="utf-8")
    gate.check(
        re.search(r'package_name\s*=\s*["\']dbt-for-apache-doris["\']', setup_text)
        is not None,
        f"setup.py distribution name is {DIST_NAME}",
    )
    gate.check(
        "run_path(str(version_file))[\"version\"]" in setup_text
        or "run_path(str(version_file))['version']" in setup_text,
        "setup.py loads the canonical version",
    )

    project_text = (repo / DBT_PROJECT).read_text(encoding="utf-8")
    duplicate_version = re.search(r"(?m)^version\s*:", project_text)
    gate.check(duplicate_version is None, "adapter dbt_project.yml has no duplicate version")

    workflow = repo / RELEASE_WORKFLOW
    gate.check(workflow.is_file(), f"release workflow exists: {RELEASE_WORKFLOW}")
    if workflow.is_file():
        workflow_text = workflow.read_text(encoding="utf-8")
        requirements = {
            "tag trigger v*": re.search(r'(?m)^\s*-\s*["\']v\*["\']\s*$', workflow_text),
            "pypi environment": re.search(
                r"(?m)^\s+name:\s*pypi\s*$", workflow_text
            ),
            "OIDC id-token permission": re.search(
                r"(?m)^\s+id-token:\s*write\s*$", workflow_text
            ),
            "PyPA publish action": "pypa/gh-action-pypi-publish@release/v1"
            in workflow_text,
        }
        for label, present in requirements.items():
            gate.check(bool(present), f"release workflow has {label}")

    readme = (repo / "README.md").read_text(encoding="utf-8")
    install_pin = f'{DIST_NAME}=={version}'
    gate.warn(install_pin in readme, f"README does not contain current install pin {install_pin}")


def check_dist(repo, version, gate):
    dist = repo / "dist"
    expected = {
        f"{NORMALIZED_NAME}-{version}-py3-none-any.whl",
        f"{NORMALIZED_NAME}-{version}.tar.gz",
    }
    actual = {path.name for path in dist.iterdir()} if dist.is_dir() else set()
    gate.check(actual == expected, f"dist contains exactly {sorted(expected)}")
    if actual != expected:
        return

    readers = {
        f"{NORMALIZED_NAME}-{version}-py3-none-any.whl": read_metadata_from_wheel,
        f"{NORMALIZED_NAME}-{version}.tar.gz": read_metadata_from_sdist,
    }
    for filename, reader in readers.items():
        metadata = reader(dist / filename)
        gate.check(metadata["Name"] == DIST_NAME, f"{filename} Name metadata is correct")
        gate.check(metadata["Version"] == version, f"{filename} Version metadata is correct")


def remote_refs(repo, version):
    result = command(
        repo,
        "git",
        "ls-remote",
        "--tags",
        "origin",
        f"refs/tags/v{version}",
        f"refs/tags/v{version}^{{}}",
    )
    refs = {}
    for line in result.stdout.splitlines():
        sha, ref = line.split(maxsplit=1)
        refs[ref] = sha
    return refs


def check_pre_tag(repo, version, gate):
    branch = command(repo, "git", "branch", "--show-current").stdout.strip()
    head = command(repo, "git", "rev-parse", "HEAD").stdout.strip()
    status = command(repo, "git", "status", "--porcelain").stdout.strip()
    remote_main = command(repo, "git", "ls-remote", "origin", "refs/heads/main").stdout
    remote_main_sha = remote_main.split()[0] if remote_main.strip() else ""
    local_tag = command(
        repo, "git", "rev-parse", "--verify", f"refs/tags/v{version}", check=False
    )
    refs = remote_refs(repo, version)

    gate.check(branch == "main", "current branch is main")
    gate.check(not status, "worktree is clean")
    gate.check(bool(remote_main_sha) and head == remote_main_sha, "HEAD equals remote main")
    gate.check(local_tag.returncode != 0, f"local tag v{version} does not exist")
    gate.check(not refs, f"remote tag v{version} does not exist")


def fetch_pypi(version):
    url = f"https://pypi.org/pypi/{DIST_NAME}/{version}/json"
    request = urllib.request.Request(url, headers={"User-Agent": "release-skill/1"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return url, json.load(response)


def check_published(repo, version, gate):
    refs = remote_refs(repo, version)
    direct = refs.get(f"refs/tags/v{version}")
    peeled = refs.get(f"refs/tags/v{version}^{{}}", direct)
    gate.check(bool(direct), f"remote tag v{version} exists")
    gate.check(
        bool(peeled) and re.fullmatch(r"[0-9a-f]{40}", peeled) is not None,
        "remote tag resolves to a commit",
    )
    if peeled:
        tagged_version = command(
            repo,
            "git",
            "show",
            f"{peeled}:{CANONICAL_VERSION.as_posix()}",
            check=False,
        )
        match = re.search(
            r'(?m)^version\s*=\s*["\']([^"\']+)["\']',
            tagged_version.stdout,
        )
        gate.check(
            tagged_version.returncode == 0
            and match is not None
            and match.group(1) == version,
            f"remote tag contains canonical version {version}",
        )

    try:
        url, payload = fetch_pypi(version)
    except urllib.error.HTTPError as error:
        gate.check(False, f"PyPI version JSON is available (HTTP {error.code})")
        return
    except (urllib.error.URLError, TimeoutError) as error:
        gate.check(False, f"PyPI version JSON is reachable ({error})")
        return

    info = payload["info"]
    gate.check(info["name"] == DIST_NAME, f"PyPI Name is {DIST_NAME}")
    gate.check(info["version"] == version, f"PyPI Version is {version}")
    gate.check(not info.get("yanked", False), "PyPI release is not yanked")

    expected = {
        f"{NORMALIZED_NAME}-{version}-py3-none-any.whl",
        f"{NORMALIZED_NAME}-{version}.tar.gz",
    }
    files = {entry["filename"] for entry in payload["urls"]}
    gate.check(files == expected, f"PyPI contains exactly {sorted(expected)}")
    for entry in payload["urls"]:
        digest = entry.get("digests", {}).get("sha256", "")
        gate.check(bool(re.fullmatch(r"[0-9a-f]{64}", digest)), f"{entry['filename']} has SHA-256")
    print(f"INFO: PyPI JSON: {url}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--version", type=validate_version, required=True)
    parser.add_argument(
        "--phase", choices=("source", "pre-tag", "published"), required=True
    )
    parser.add_argument("--check-dist", action="store_true")
    args = parser.parse_args(argv)

    repo = args.repo.resolve()
    gate = Gate()
    try:
        is_repo = command(repo, "git", "rev-parse", "--is-inside-work-tree").stdout
        gate.check(is_repo.strip() == "true", f"repository is a Git worktree: {repo}")
        check_source(repo, args.version, gate)
        if args.check_dist:
            check_dist(repo, args.version, gate)
        if args.phase == "pre-tag":
            check_pre_tag(repo, args.version, gate)
        elif args.phase == "published":
            check_published(repo, args.version, gate)
    except (OSError, RuntimeError, KeyError, ValueError) as error:
        gate.check(False, str(error))

    print(
        f"SUMMARY: phase={args.phase} failures={len(gate.failures)} "
        f"warnings={len(gate.warnings)}"
    )
    return 1 if gate.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
