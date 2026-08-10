# dbt-for-apache-doris

A dbt adapter for VeloDB and Apache Doris.

This repository preserves the standalone development history migrated from
[`xylaaaaa/dbt-doris-adapter`](https://github.com/xylaaaaa/dbt-doris-adapter)
and is derived from the
[`extension/dbt-doris`](https://github.com/apache/doris/tree/master/extension/dbt-doris)
component of Apache Doris. It is not an official Apache Software Foundation or
dbt Labs release.

## Compatibility and verification

| Component | Development or test baseline |
| --- | --- |
| dbt Core | 1.12.x; Microbatch requires 1.12.x; the historical release matrix ran on 1.12.0 |
| Apache Doris | 2.1.5+ expected runtime minimum; exact historical release evidence is listed below and in the Incremental guide |
| Historical Doris release E2E matrix | 2.1.11, 3.0.8, 3.1.4, 4.0.7, and 4.1.3 all passed on the recorded CTAS-snapshot, durable-marker, and pre-model-ordering baseline |
| Historical mixed-cluster Functional run | FE `doris-4.1.2-rc01-4536b29f712`; BE `doris-0.0.0-0a5ad292e3f`; 87 passed, but not official-release compatibility evidence |
| Doris Async MV gate unit tests | Mocked version strings for 2.1.5, 2.1.10, 3.0.1, 3.1.0, and 4.1.2 |
| Python | 3.10 or newer; final matrix ran on 3.12.13 |
| Database protocol | Doris MySQL protocol |

The recorded database E2E evidence below covers Apache Doris release
artifacts. VeloDB-specific release compatibility evidence has not yet been
recorded in this repository.

The historical release-candidate E2E matrix is deliberately pinned to exact
public artifacts:

| Doris release | Exact FE/BE Version | Complete Functional | Focused Incremental | Focused Async MV | State |
| --- | --- | --- | --- | --- | --- |
| 2.1.11 | `doris-2.1.11-rc01-97b77e6cda` | 98 passed, 106 warnings, 290.51s | 36 passed, 27 warnings, 45.20s | 21 passed, 121.07s | Passed |
| 3.0.8 | `doris-3.0.8-rc01-09b0cc49a6` | 98 passed, 106 warnings, 143.87s | 36 passed, 27 warnings, 52.49s | 21 passed, 118.73s | Passed |
| 3.1.4 | `doris-3.1.4-rc02-7f5ba43de6` | 98 passed, 106 warnings, 150.81s | 36 passed, 27 warnings, 43.94s | 21 passed, 105.66s | Passed |
| 4.0.7 | `doris-4.0.7-rc02-35854e7e92a` | 98 passed, 106 warnings, 138.82s | 36 passed, 27 warnings, 39.69s | 21 passed, 105.94s | Passed |
| 4.1.3 | `doris-4.1.3-rc02-7126cf65d96` | 98 passed, 106 warnings, 135.13s | 36 passed, 27 warnings, 39.48s | 21 passed, 109.19s | Passed |

The current implementation baseline `7a362c8` has a separate clean Doris 4.1.3
record: all 22 current Async MV Functional items and all 124 directly related
Unit/Adapter items passed. The other four releases have not yet rerun the added
MV Grants case, so the table remains historical rather than being relabeled as a
current 22-case matrix.

Here, `Passed` means that the exact release completed the historical 98-test
Functional suite, the 36-test focused Incremental suite, the 21-test focused
Async MV suite, and the version and cleanup evidence checks. The Async MV suite
ran the adapter lifecycle tests plus dbt Core's Materialized View basic
contract, with no skipped cases.

Each row's Functional and Incremental evidence requires SHA-512 verification of
the downloaded artifact, the same complete Version string matching the expected
release on every live FE and BE, the exact JDK identity, the adapter Git SHA, all
98 Functional tests, the 36-test focused Incremental suite, and verified
test-schema and process cleanup. Those results bind clean commit
`7f6d9701140188f347e9f68a25ef9013551e4e48`; the focused Async MV column was
run separately on clean commit `f5e30c64ef7eb8320cf359c3d96cf62b595faf00`.
The final runs recorded identical FE/BE versions with `Alive=true` and zero
remaining test databases or helper relations. The gate rejects the `0.0.0`
development placeholder. Every per-version JSON record contains a
`doris_version_gate` object whose `expected_release` matches the matrix row,
whose `reported_build` matches the exact FE/BE Version above, and whose `status`
is `passed`.

The earlier 2.1.11, 3.0.8, and 3.1.4 runs exercised a superseded implementation
that reconstructed view DDL. The production design now combines forward-only
physical CTAS snapshots with durable backup markers and never replays view DDL,
so those results are stale and are not formal compatibility evidence. All five
release rows were subsequently rerun; the passing results above supersede that
historical evidence.

The previous development mixed-cluster diagnostic (`321` Unit tests, `88`
Functional tests, and `26` focused Incremental tests) predates the durable-marker
and pre-model snapshot-ordering revisions and remains historical only. Doris
2.1.11 exposed that selecting an old View depends on the caller's current
`sql_mode`. Pre-model snapshot ordering fixed that case, and both the focused and
complete suites passed on 2.1.11 in the final matrix. An earlier five-version
run from a dirty adapter worktree was pre-validation only and is historical, not
formal release evidence; the Functional and focused Incremental results above
come from clean adapter commit
`7f6d9701140188f347e9f68a25ef9013551e4e48` with `dirty=false`.

Historical release-candidate verification also recorded `327 passed, 9 warnings`
in 57.99s for Unit tests; Flake8 and `git diff --check` passed. That evidence environment
used dbt Core 1.12.0, adapter 1.0.0, and Python 3.12.13. `python -m build`
produced the `dbt_doris-1.0.0` sdist and wheel under
`/tmp/dbt-doris-package-clean.tUhMxp`. The 75,660-byte wheel has SHA-256
`edcbc1bae94e440c7be25f71ec96b6c91e4a5e71af29604561f4d99264584725`; the
119,127-byte sdist has SHA-256
`ffe4c9c41e8a7f6a24fb43935ec30535748095b2a807b634fe2266ede0b43ef9`.
Twine 7.0.0 passed both. A clean Python 3.12.13 environment at
`/tmp/dbt-doris-wheel-clean-py312.lPTWhm` installed the wheel successfully,
imported the adapter from `site-packages`, contained all three checked macro
files, reported
`valid_incremental_strategies=[append,merge,insert_overwrite]`, and completed
`pip check` with no broken requirements.

Those package digests bind the package audit to implementation commit
`259b14e0ff77c1dac4c1963b918e0612b2901358`; they are evidence hashes, not a
promise that a later documentation-only or release build will have identical
archive bytes.

The exact-version evidence gate rejects `0.0.0` and requires every live FE and
BE to report one identical complete Version string for the requested release.

Functional-test database names now start with a prefix of at most 14 characters.
The longest known generated database name is 62 characters, and the
five-character base-36 random nonce provides 60,466,176 possible prefixes for
each configured schema identity.

The Python distribution remains named `dbt-doris`, and the adapter type used in
`profiles.yml` remains `doris`.

## Install from source

```shell
git clone https://github.com/velodb/dbt-for-apache-doris.git
cd dbt-for-apache-doris
python -m pip install .
```

For adapter development, use an editable install with the test dependencies:

```shell
python -m pip install -r dev-requirements.txt
python -m pip install -e .
```

## Configure a profile

Add an output like this to `~/.dbt/profiles.yml`:

```yaml
your_profile_name:
  target: dev
  outputs:
    dev:
      type: doris
      host: 127.0.0.1
      port: 9030
      username: root
      password: ""
      schema: dbt
      threads: 4
```

## Materializations

The adapter contains Doris implementations for table, view, incremental,
partition, snapshot, seed, and asynchronous materialized-view workflows.
Ephemeral models are compiled by dbt Core.

### dbt Core foundation capabilities

The adapter supports enforced model contracts, persisted relation and column
documentation, source freshness, stored data-test failures, and declarative
relation grants.

### Incremental models

The built-in Doris incremental strategies are `append`, `merge`,
`insert_overwrite`, and dbt Core 1.12 `microbatch`. If
`incremental_strategy` is omitted, a model with a `unique_key` uses `merge`; a
model without one uses `append`.

| Strategy | Target model | Incremental DML |
| --- | --- | --- |
| `append` | Duplicate Key | `INSERT INTO` append |
| `merge` | MOW or MOR Unique Key | full-row `INSERT INTO` upsert |
| `insert_overwrite` | writable Doris table | native whole-table or partition `INSERT OVERWRITE` |
| `microbatch` | Duplicate Key with one exact RANGE partition per `event_time` batch | one named-partition `INSERT OVERWRITE` per Core UTC window |

`merge` describes dbt's result semantics. Native Doris `MERGE INTO` is available
only on Doris 4.1 and newer, but the current adapter does not depend on it or
emit it: Doris Unique Key storage resolves the adapter's full-row `INSERT INTO`
upsert, including ordering from a visible column configured through
`function_column.sequence_col`. The source batch must contain each configured
key at most once. Partial-column merge configs and incremental predicates remain
unsupported.

Ordinary updates to an existing target with `on_schema_change='ignore'` create a
temporary logical view for column metadata, but do not copy the batch into a
physical staging table. Each built-in strategy then performs one final DML
statement. Schema-changing runs use a physical staging table to freeze the
batch, while full refresh builds an intermediate table and exposes it through a
metadata swap.

The adapter never replays View DDL or assumes that a View retains creation-time
SQL mode semantics. Doris 2.1.11 testing showed that selecting an old View can
be affected by the caller's current `sql_mode`. Therefore, only the forward
replacement of an active canonical View by a Table, Async MV, or Partition
materialization uses a dedicated physical snapshot, and that snapshot must run
before any new-model pre-hook, `sql_header`, or DDL:

```sql
CREATE TABLE backup
DISTRIBUTED BY RANDOM BUCKETS AUTO
PROPERTIES (
  "enable_duplicate_without_keys_by_default" = "true",
  "replication_num" = "..."
)
AS SELECT * FROM source_view;
```

The fixed Random/AUTO and duplicate-without-keys settings avoid selecting an
invalid Key or Hash column when, for example, the View's first column is DOUBLE.
The only model properties allowed on the snapshot are `replication_num` or
`replication_allocation`; they come from the current model configuration and are
never inferred from the old View. New-model key, distribution, partition,
contract, and `sql_header` settings are excluded. The snapshot is a point-in-time
copy of the rows queryable from the View in the current pre-model session. It
does not preserve the View definition, creation-time session state, comments,
grants, or identical schema properties.

CTAS failure leaves the canonical View online and prevents all new-model hooks,
headers, and DDL. After CTAS succeeds, the old View still remains online while
the replacement relation is built. Only after that build completes does the
adapter drop the old View and rename the replacement to the canonical name. The
physical snapshot remains a recovery marker until the complete lifecycle
succeeds. A source/destination name collision is rejected before any SQL. An
existing destination may require a read-only relation metadata lookup, but is
rejected before mutating SQL or a drop. Generic View rename and exchange remain
rejected.

If replacement construction fails while the old View is still canonical, the
next attempt cleans or replaces the stale physical marker before taking a new
snapshot. If failure occurs in the drop/rename window and leaves the canonical
name absent, the marker is retained as the only old-data copy. Recovery then
depends on the target materialization: Incremental/Partition follow the durable
no-restore rule below, while Table/Materialized View first restore the backup to
the canonical name and then retry their type-switch lifecycle.

Recovery from an existing `__dbt_backup` has a different boundary. When the
canonical Incremental or Partition relation is absent, the backup remains under
its original name as a durable marker; it may be a legacy View, Table, or Async
MV. The adapter does not restore, execute, snapshot, rename, or drop it before
building a fresh canonical relation from model SQL. Keeping the canonical name
absent makes `is_incremental()` false on every failed retry. Old data remains
queryable only through the backup name, and the canonical name is not guaranteed
to be available during failure recovery. The marker is deleted only after the
entire canonical build lifecycle succeeds. Thus legacy View backups never enter
the CTAS path.

This physical snapshot is limited to forward type switching; it does not change
the logical temporary View and one-final-DML contract for normal `append`,
`merge`, `insert_overwrite`, or each `microbatch` run. SQL-mode-sensitive tests must assert the
rows returned in the current pre-model session and the ordering boundary above;
they must not infer creation-time SQL mode preservation from the View DDL.

`delete+insert` (including the `delete_insert` spelling) is intentionally not
supported; use `merge` for Unique Key upserts. This release also corrects the
old adapter behavior where an explicitly configured `insert_overwrite` acted
like an upsert. To prevent a silent change to destructive overwrite semantics,
the legacy combination `insert_overwrite + unique_key` is rejected: change the
strategy to `merge` for upserts, or remove `unique_key` to explicitly opt in to
native overwrite, which can remove rows absent from the new batch.

### Asynchronous materialized views

To manage a Doris asynchronous materialized view, configure a model with
`materialized='materialized_view'`:

```sql
{{ config(
    materialized='materialized_view',
    build_mode='immediate',
    refresh_method='auto',
    refresh_trigger='manual',
    wait_for_refresh=true
) }}

select order_date, sum(amount) as sales
from {{ ref('orders') }}
group by order_date
```

`build_mode='immediate'` is the default. When `CREATE MATERIALIZED VIEW`
starts the initial build for a new or replacement definition, the adapter waits
for that task before exposing the MV, so downstream dbt models do not observe
an unfinished initial build. Create and replacement do not submit an additional
`REFRESH MATERIALIZED VIEW`; they only wait for the task produced by
`BUILD IMMEDIATE`.

For an unchanged `ON MANUAL` MV, every later `dbt run` that selects the model
submits
`REFRESH MATERIALIZED VIEW ... AUTO|COMPLETE` and waits for its new task by
default. The wait defaults to 300 seconds with one-second polling and can be
tuned with `refresh_wait_timeout` and `refresh_poll_interval`. Set
`wait_for_refresh=false` to submit without polling; this setting never
suppresses the refresh itself. Waiting requires Doris
materialized-view task history to remain enabled. The adapter identifies a task
by comparing task IDs before and after submission; concurrent refreshes of the
same MV can therefore be mistaken for the task submitted by dbt.

The supported refresh triggers are `manual`, `schedule`, and `commit`.
Refresh submission is determined directly by `refresh_trigger`; there is no
separate `refresh_on_run` switch. For an existing unchanged MV, `manual`
submits a refresh on every selected run, while `schedule` and `commit` skip and
leave subsequent refreshes to Doris.
Production schedules accept `minute`, `hour`, `day`, or `week`. The adapter
rejects `second` because Doris only enables second-level schedules through a
test-only setting.

Common asynchronous MV lifecycle and refresh settings are:

| Config | Purpose |
| --- | --- |
| `build_mode` | `immediate` (default) builds on create/replace; `deferred` creates without an initial build. |
| `refresh_method` | Refresh scope: `auto` (default) lets Doris select partitions when it can track base-table changes; `complete` always refreshes all partitions. For external tables whose changes Doris cannot detect, use `complete`. |
| `refresh_trigger` | Trigger: `manual` (default), `schedule`, or `commit`. |
| `refresh_schedule` | Schedule mapping with `interval`, `unit`, and optional `start_time`. |
| `wait_for_refresh` | Wait for an initial-build or adapter-submitted manual refresh task; defaults to `true`. |
| `refresh_wait_timeout` / `refresh_poll_interval` | Refresh-task timeout and polling interval in seconds. |
| `on_configuration_change` | `apply` (atomic replacement), `continue`, or `fail`. |

dbt-doris manages both MV deployment and the `ON MANUAL` run action. If the
deployed definition is unchanged:

- `ON MANUAL` submits `REFRESH MATERIALIZED VIEW ... AUTO|COMPLETE`;
- `ON SCHEDULE` and `ON COMMIT` skip, leaving refresh timing to Doris.

This also makes `BUILD DEFERRED + ON MANUAL` deterministic: the first
`dbt run` only creates the MV, and the second unchanged run submits its first
refresh.

If the definition changed and `on_configuration_change=continue`, the adapter
keeps the deployed definition and does not submit a manual refresh.

For an initial build or manual refresh that it waits for, the adapter polls
Doris `tasks('type'='mv')`; the dbt adapter response includes the successful
task ID, status, and last query ID when Doris provides one. A failed, canceled,
unexpected, or timed-out task fails the model instead of being reported as a
successful action. A dbt timeout does not cancel the asynchronous task already
submitted to Doris.

Outside-transaction pre-hooks normally run before deployed-definition
inspection. An active canonical View type replacement is the safety exception:
its physical data snapshot runs before every new-model pre-hook, header, or DDL.
Definition changes are built as a temporary MV and exposed through Doris's
atomic materialized-view replacement; with `BUILD IMMEDIATE`, exposure happens
only after the initial build succeeds, while `BUILD DEFERRED` intentionally has
no initial build task to wait for. The deployment marker is finalized after
inside-transaction post-hooks, allowing a later run to detect and recover an
interrupted deployment. If an atomic replacement succeeded but an inside
post-hook failed, the previous MV remains under the temporary name; the next
run atomically restores it before retrying the deployment.

`persist_docs` is supported for both the MV relation and its columns. The
relation description is included in the MV comment only when
`persist_docs.relation` is enabled; the adapter's definition/deployment marker
remains in that comment independently. Column descriptions are rendered in the
MV column definitions when `persist_docs.columns` is enabled.

Doris relation grants manage Doris users through dbt's standard `grants`
configuration:

```yaml
models:
  your_project:
    +grants:
      select:
        - "analyst"
        - "reporter@%"
```

Before materialized-view DDL or incremental DML, the adapter validates every
configured privilege and user so an invalid grant cannot expose a new MV
definition or leave partially written incremental data. A bare name means
`username@%`; use `username@host` for a host-specific identity. Doris roles are
not supported because `information_schema.table_privileges` cannot distinguish
direct user grants from inherited role grants safely.

Asynchronous-MV version evidence is:

| Check | Coverage | What it proves |
| --- | --- | --- |
| Historical full Functional run on a mixed cluster | FE `doris-4.1.2-rc01-4536b29f712`; BE `doris-0.0.0-0a5ad292e3f`; 87 passed | The implemented paths worked on that exact mixed development cluster; this is not official-release compatibility evidence |
| Focused Async MV E2E matrix | Clean commit `f5e30c64ef7eb8320cf359c3d96cf62b595faf00`, `dirty=false`; the same 21 MV tests passed without skips on 2.1.11, 3.0.8, 3.1.4, 4.0.7, and 4.1.3 | Async MV creation, refresh policy, task waiting, configuration change, rollback, docs, custom schema/alias, and relation-type switching passed on those exact builds |
| Unit tests with mocked `SHOW FRONTENDS` rows | 2.1.5, 2.1.10, 3.0.1, 3.1.0, and 4.1.2 | Version parsing and gate decisions only; no Doris feature compatibility |

Before managing an asynchronous MV, the adapter prefers the connected and
Master FE versions from `SHOW FRONTENDS`; if neither role can be identified, it
validates the first returned row. An unparsable or unsupported selected FE is
rejected. The current code gate accepts 2.x versions at 2.1.5 or newer, every
3.x version except 3.0.0, and major version 4 or newer.

Those boundaries are hard-coded runtime conditions, not results from the
official-release E2E matrix. In particular, this repository has not established
through live-cluster testing that 2.1.5 is the exact minimum or that 3.0.0 is
incompatible. Gate acceptance and the historical mixed-cluster run are therefore
not compatibility guarantees. Before production use, require a completed matrix
row for the exact Doris release or run the same evidence procedure against that
release.

Only Doris asynchronous materialized views are managed. Synchronous
materialized views (rollups) have a different lifecycle and remain explicitly
out of scope.

## Test

Unit tests do not require a Doris cluster:

```shell
python -m pytest test/unit
python -m flake8 dbt test
```

Functional tests require a reachable Doris cluster. The defaults target
`127.0.0.1:9030`, user `root`, schema `dbt_test`, and one replica:

```shell
DORIS_TEST_HOST=127.0.0.1 \
DORIS_TEST_PORT=9030 \
DORIS_TEST_USER=root \
DORIS_TEST_PASSWORD='' \
DORIS_TEST_SCHEMA=dbt_test \
DORIS_TEST_REPLICATION_NUM=1 \
  python -m pytest test/functional
```

## License and upstream

The code is licensed under Apache License 2.0. See [LICENSE](LICENSE) and
[NOTICE](NOTICE). The source snapshot and migration boundary are recorded in
[UPSTREAM.md](UPSTREAM.md).
