# Upstream provenance

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

Generated caches, test logs, local dbt runtime state, credentials, Doris
monorepo-only files, and unrelated Doris components were not imported.

The standalone repository has its own maintenance and release lifecycle.
