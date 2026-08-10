# Upstream and migration provenance

This repository started as a standalone snapshot of
`extension/dbt-doris` from an Apache Doris working tree.

- Upstream repository: <https://github.com/apache/doris>
- Upstream component: `extension/dbt-doris`
- Source working-tree commit: `567c2a5f56baa0651591adfc3ba78553b6af919a`
- Snapshot date: 2026-07-30

The initial standalone snapshot also includes the adapter changes that were
present but not yet committed in that working tree, including the dbt Core
1.12 compatibility work, asynchronous materialized-view implementation, and
tests. Detailed migration and design notes remain local to the development
workspace and are intentionally excluded from this public repository.

The standalone mainline history reachable from the imported source commit was
subsequently migrated into this repository:

- Standalone source repository: <https://github.com/xylaaaaa/dbt-doris-adapter>
- Imported source commit: `142cba1e4d3b84946a78689aa3e5d3761eededc9`
- Imported source tree: `0902e6050da27ddfa9306b41c820c231f27b7194`
- Target import merge commit: `3bf02681ea3c447f1ec211f5cad2c17a4e1e63c6`
- Migration date: 2026-08-10

The import uses an unrelated-history merge so both the target repository's
initial commit and the history reachable from the imported source commit remain
reachable.
GitHub-specific metadata such as source pull-request discussions, reviews, and
Actions runs remains in the standalone source repository.
Unqualified pull-request numbers in imported commit messages also refer to that
standalone source repository.

The target worktree intentionally omits the standalone `docs/` directory. Those
documents remain available in the standalone source repository and imported
history, but they are not part of the current target tree or Python package.

Generated caches, test logs, local dbt runtime state, credentials, Doris
monorepo-only files, and unrelated Doris components were not imported.

This repository has its own maintenance and release lifecycle.
