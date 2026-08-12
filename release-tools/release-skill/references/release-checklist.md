# dbt-for-apache-doris release checklist

## Contents

1. Release identity and confirmation model
2. Baseline and release branch
3. Version and packaging preparation
4. Synchronizing a moving main branch
5. Test gates
6. Review, commit, PR, and merge
7. Trusted Publishing setup
8. Tag and publish
9. Public verification
10. Cleanup
11. Failure recovery

## 1. Release identity and confirmation model

Set the exact target from the user's request, for example `1.1.0`; never copy `1.0.0` from an older release guide. Derive:

```text
VERSION=<exact requested version>
TAG=v<VERSION>
BRANCH=release/<VERSION>
DIST_NAME=dbt-for-apache-doris
REPOSITORY=velodb/dbt-for-apache-doris
WORKFLOW=release.yml
ENVIRONMENT=pypi
```

Before each mutation, state what will change and ask for confirmation. Read-only discovery, validation, and monitoring may be grouped. If the user grants bounded autonomy, record the boundary explicitly and resume confirmations immediately after it.

Never combine these into one confirmation:

- creating the local tag;
- pushing the tag to GitHub.

The second action starts the production release.

## 2. Baseline and release branch

Inspect without changing state:

```bash
git status --branch --short
git remote -v
git fetch origin main --tags
git log -3 --oneline --decorate origin/main
git tag --list "v${VERSION}"
git ls-remote --tags origin "refs/tags/v${VERSION}" "refs/tags/v${VERSION}^{}"
gh auth status
```

Require a recoverable, understood worktree. Preserve unrelated changes. Start the release from the latest desired `origin/main`:

```bash
git switch main
git merge --ff-only origin/main
git switch -c "release/${VERSION}"
```

If the branch already exists, inspect its upstream and commits rather than recreating it.

## 3. Version and packaging preparation

### Routine version release

Change the canonical version only in:

```text
dbt/adapters/doris/__version__.py
```

Confirm that:

- `setup.py` uses `run_path` to load that version;
- `dbt/include/doris/dbt_project.yml` has no duplicate package version;
- the distribution name remains `dbt-for-apache-doris`;
- the README current-release installation pin is updated when present.

Scan version-like text and classify every match before editing:

```bash
rg -n --hidden \
  --glob '!.git/**' --glob '!.venv/**' --glob '!build/**' \
  --glob '!dist/**' --glob '!*.egg-info/**' \
  'dbt-doris|dbt_for_apache_doris|dbt-for-apache-doris|[0-9]+\.[0-9]+\.[0-9]+' .
```

Keep these intentional matches:

- Quickstart `dbt_project.yml` example version;
- warning about the unrelated `dbt-doris==1.0.0` package;
- internal `dbt-doris:` metadata markers and user-facing compatibility errors;
- upstream `extension/dbt-doris` paths.

### One-time publishing bootstrap

Only add or change these if missing or incorrect:

- package name and single version source in `setup.py`;
- `.github/workflows/release.yml`;
- package-name and version-source Unit tests;
- README installation and package-name warning;
- cleanup of old/new egg-info directories.

The release workflow must:

- trigger on pushed `v*` tags;
- verify `v<canonical version>` exactly;
- lint, run Unit tests, build wheel and sdist, and run strict Twine checks;
- verify exact filenames and Name/Version metadata;
- install wheel and sdist in clean virtual environments;
- upload the verified artifacts once and publish those same artifacts in a separate job;
- use GitHub Environment `pypi` and job-level `id-token: write`;
- publish with `pypa/gh-action-pypi-publish@release/v1` and no stored PyPI credential.

Run the source gate:

```bash
python release-tools/release-skill/scripts/verify_release.py \
  --repo . --version "$VERSION" --phase source
```

## 4. Synchronizing a moving main branch

Re-fetch `origin/main` before final testing and before opening the PR. If it advanced:

1. Show the new commits and explain whether they belong in the release.
2. Preserve release and unrelated work before integration.
3. If release work is uncommitted, create a clearly named backup stash only after confirmation, fast-forward/merge as appropriate, and reapply it.
4. If release commits exist, use the repository's normal PR update policy; do not silently rebase shared commits.
5. Resolve conflicts deliberately and re-run all affected tests.
6. Keep the backup stash until public publication succeeds.

Never use `git reset --hard` or `git checkout --` to synchronize.

## 5. Test gates

Use a dedicated virtual environment and install the complete development requirements. Confirm the actual versions used:

```bash
python -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r dev-requirements.txt
.venv/bin/python -m pip install -e .
.venv/bin/python -m pip check
.venv/bin/dbt --version
```

### Lint and Unit tests

```bash
.venv/bin/python -m flake8 dbt scripts test
.venv/bin/python -m pytest -q test/unit
```

Record the exact pass count and warnings. Fix the cause of failures. Do not exclude failing tests merely to advance the release.

### Build and inspect distributions

Clean stale outputs using the repository target, then build:

```bash
make clean PYTHON=.venv/bin/python
.venv/bin/python -m build
.venv/bin/python -m twine check --strict dist/*
python release-tools/release-skill/scripts/verify_release.py \
  --repo . --version "$VERSION" --phase source --check-dist
```

Require exactly:

```text
dist/dbt_for_apache_doris-<VERSION>-py3-none-any.whl
dist/dbt_for_apache_doris-<VERSION>.tar.gz
```

Install each artifact into a different clean temporary virtual environment. Run `pip check`, `dbt --version`, import `dbt.adapters.doris`, and read installed distribution metadata.

### Functional tests on Doris

Functional tests require a real, dedicated Doris cluster. Collect:

- host and MySQL port;
- username and password;
- test schema namespace;
- replication number.

Warn that the suite creates and drops databases, relations, users, and grants and must not run concurrently. Do not write private credentials into `test/doris_test.env`. Use an external config with mode 600 or an ephemeral file.

Run preflight first:

```bash
.venv/bin/python scripts/run_doris_functional_tests.py \
  --config /secure/path/doris-release.env --preflight-only
```

Then run the full suite serially:

```bash
.venv/bin/python scripts/run_doris_functional_tests.py \
  --config /secure/path/doris-release.env -- -q
```

If a test fails:

1. Separate environment/collection failures from adapter behavior failures.
2. Reproduce the smallest failing set.
3. Make the smallest justified fix.
4. Re-run the failing set.
5. Re-run the full Functional suite.
6. Re-run lint and Unit tests if code or tests changed.
7. Finish with `--preflight-only` to verify cluster cleanup.

Record FE/BE versions, adapter commit, dbt Core version, Functional pass count, and cleanup result.

## 6. Review, commit, PR, and merge

Review all changes and ignored/untracked outputs:

```bash
git diff --check
git diff --stat
git diff
git status --short
git ls-files 'build/**' 'dist/**' '*.egg-info/**' '.venv/**' '.pytest_cache/**'
```

Stage only reviewed release files. Commit and push the branch after separate confirmations:

```bash
git commit -m "release: prepare dbt-for-apache-doris ${VERSION}"
git push --set-upstream origin "release/${VERSION}"
```

Create a PR to `main` and include test evidence. Monitor all required checks. After the user or repository automation merges it, verify the merge through GitHub and synchronize local `main` with `--ff-only`.

Do not tag the release-branch commit. Tag the verified merge commit on `main`.

## 7. Trusted Publishing setup

Check the GitHub Environment:

```bash
gh api repos/velodb/dbt-for-apache-doris/environments/pypi
```

If it returns 404, create it only after confirmation:

```bash
gh api --method PUT \
  repos/velodb/dbt-for-apache-doris/environments/pypi \
  --input /dev/null
```

Check `https://pypi.org/pypi/dbt-for-apache-doris/json`.

- If the project exists, verify its normal Trusted Publisher.
- If it returns 404, have the logged-in PyPI user add a Pending Trusted Publisher at `https://pypi.org/manage/account/publishing/` with exactly:

```text
PyPI Project Name: dbt-for-apache-doris
Owner: velodb
Repository: dbt-for-apache-doris
Workflow name: release.yml
Environment name: pypi
```

The workflow field is the filename only, not `.github/workflows/release.yml`. Never ask for PyPI credentials. Wait for the user to confirm the publisher was added. Recheck that the public project name remains unclaimed immediately before tagging.

## 8. Tag and publish

Require all of the following:

- release PR merged into `main`;
- local `main` clean and equal to the remote `main` SHA;
- merge-commit CI successful;
- package version and tag consistent;
- neither local nor remote `v<VERSION>` exists;
- `pypi` Environment exists;
- Trusted Publisher or Pending Trusted Publisher is configured;
- PyPI project/version does not already conflict.

Run:

```bash
python release-tools/release-skill/scripts/verify_release.py \
  --repo . --version "$VERSION" --phase pre-tag
```

Create an annotated local tag after confirmation:

```bash
git tag -a "v${VERSION}" -m "dbt-for-apache-doris ${VERSION}"
git show --no-patch "v${VERSION}"
git rev-list -n 1 "v${VERSION}"
git ls-remote --tags origin "refs/tags/v${VERSION}"
```

Explain that the next command triggers production publishing and request a new confirmation. Then push only the tag:

```bash
git push origin "refs/tags/v${VERSION}"
```

Find and monitor the exact tag-triggered run:

```bash
gh run list --workflow release.yml --limit 5 \
  --json databaseId,headBranch,headSha,status,conclusion,url,createdAt
gh run watch <RUN_ID> --exit-status
gh run view <RUN_ID> --json status,conclusion,url,headSha,headBranch,jobs
```

Require both `Build and verify distributions` and `Publish to PyPI` to succeed.

## 9. Public verification

Run only after the workflow reports success:

```bash
python release-tools/release-skill/scripts/verify_release.py \
  --repo . --version "$VERSION" --phase published
```

Also download the exact public package in a clean environment or temporary directory:

```bash
python -m pip download --no-deps --only-binary=:all: \
  "dbt-for-apache-doris==${VERSION}"
```

Verify:

- PyPI reports Name `dbt-for-apache-doris` and Version `<VERSION>`;
- wheel and sdist filenames are exact;
- the downloaded SHA-256 matches PyPI JSON;
- `Requires-Python` and dependencies are expected;
- the release is not yanked;
- the tag peels to the intended `main` commit.

Only then report the release complete and provide the PyPI and workflow URLs plus the installation command.

## 10. Cleanup

Treat cleanup as optional post-release work and request separate confirmation. Before deleting anything, compare it with merged `main`.

Possible cleanup:

- remove the local and remote release branch;
- inspect and drop the named backup stash if all its changes are present in `main`;
- remove temporary verification directories created by the agent.

Do not delete the tag or published artifacts. Do not create a GitHub Release object unless the user or repository policy requests one; the PyPI workflow does not imply one.

## 11. Failure recovery

### Build or test job fails before publish

Inspect logs, fix through a new PR to `main`, and repeat validation. After a release tag has been pushed, prefer a new patch version and tag; never force-move a public release tag.

### OIDC publish fails and no files reached PyPI

Correct the GitHub Environment or exact Trusted Publisher tuple, then re-run the failed GitHub job for the same immutable commit and artifacts. Do not add a long-lived API token as a shortcut.

### Only one artifact reached PyPI

Treat the version as partially published and immutable. Diagnose, bump to a new patch version, rebuild both artifacts, and release the new version. Do not silently skip existing files.

### PyPI name was claimed before first publish

Stop. Do not upload under a different name without user authorization. Choose a new distribution name, update packaging/docs/publisher configuration, and repeat all gates.

### `main` advances before tagging

Show the commits. If they must be included, test the new `main` state and tag that exact verified commit. If they must not be included, stop for a release-policy decision rather than tagging an arbitrary older commit.
