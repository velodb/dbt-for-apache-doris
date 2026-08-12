---
name: release-skill
description: Safely prepare, test, merge, tag, publish, verify, and clean up a dbt-for-apache-doris release through GitHub Actions and PyPI Trusted Publishing. Use when asked to release or publish a specific adapter version, prepare a release branch or PR, validate release artifacts against a Doris cluster, configure the GitHub pypi Environment or PyPI Pending Trusted Publisher, push a release tag, monitor publishing, recover a failed release, or report release progress.
---

# Release dbt-for-apache-doris

Release an explicitly requested version without bypassing validation or publishing from an unmerged commit. Treat PyPI uploads and pushed tags as irreversible release boundaries.

## Load the procedure

Read [references/release-checklist.md](references/release-checklist.md) completely before taking release actions. Use `scripts/verify_release.py` at the source, pre-tag, and published gates.

## Enforce the safety contract

- Require an exact target version. Never substitute a version from an old document or example.
- Default to requesting confirmation before each state-changing step. Group related read-only checks without confirmation.
- Honor a user's explicit, bounded authorization such as “through the end of testing, execute without asking.” Let that authorization expire at its stated boundary, then resume confirmations.
- Announce the exact effect before pushing a tag: it triggers the production PyPI workflow.
- Never request, print, store, or pass a PyPI password or long-lived token. Use GitHub OIDC Trusted Publishing.
- Never edit the tracked Doris credential template with private credentials. Use an external mode-600 config or an ephemeral config.
- Never run Functional tests against a shared or production Doris cluster. Confirm destructive-test authorization once before the Functional phase unless already granted.
- Preserve unrelated worktree changes. Do not reset, force-push, overwrite tags, delete branches, or drop a stash without explicit authorization.
- Stop at the first failed gate. Diagnose and fix the cause; do not weaken validation.

## Resume from observed state

Inspect Git, GitHub Actions, PR, tag, and PyPI state before acting. Continue from the first incomplete gate instead of repeating completed work. A successful public PyPI record is the source of truth for publication; a successful workflow alone is insufficient.

## Follow the release gates

1. **Identify** — Record target version, repository, current branch, HEAD, worktree state, remote `main`, existing tags, PyPI name availability, and applicable instructions.
2. **Prepare** — Synchronize from `origin/main`, create `release/<version>`, update the canonical version, and apply any one-time packaging or workflow bootstrap changes.
3. **Test** — Run lint, Unit tests, strict build checks, exact artifact/metadata checks, clean wheel and sdist installs, then the full Functional suite on a dedicated Doris cluster. Re-run affected and full gates after fixes.
4. **Review and merge** — Review the entire diff, commit, push the release branch, create a PR to `main`, wait for required CI, and have the PR merged.
5. **Provision publishing** — Ensure the GitHub Environment is named `pypi`. If the PyPI project does not yet exist, have the user add the exact Pending Trusted Publisher tuple before tagging.
6. **Tag and publish** — Synchronize local `main`, run the pre-tag verifier, create annotated `v<version>` on the merge commit, verify it locally, request a separate confirmation, and push only that tag.
7. **Verify publicly** — Monitor both build and publish jobs. Validate the PyPI JSON record, filenames, metadata, hashes, and a fresh public-index download.
8. **Clean up** — Report success first. Offer release-branch and backup-stash cleanup as separate, explicitly confirmed operations.

## Handle version sources correctly

Treat `dbt/adapters/doris/__version__.py` as the canonical package version. `setup.py` must load it rather than duplicate it, and the adapter's `dbt_project.yml` must not duplicate it. Distinguish deliberate documentation/examples from version sources:

- Update a README install pin if the documentation promises the current release.
- Do not rewrite the Quickstart dbt project’s independent `version: 1.0.0`.
- Do not rewrite references to the unrelated PyPI distribution `dbt-doris==1.0.0`.
- Preserve internal `dbt-doris:` database markers and upstream paths unless a separate compatibility change requires migration.

## Report progress with evidence

At each gate, report concrete evidence: branch and SHA, test counts, artifact filenames, workflow run URL, PyPI URL, and any remaining blocker. Never call a release complete until the public package can be retrieved and its metadata matches the requested version.
