# dbt for Apache Doris

[![CI](https://github.com/velodb/dbt-for-apache-doris/actions/workflows/ci.yml/badge.svg)](https://github.com/velodb/dbt-for-apache-doris/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/dbt-for-apache-doris)](https://pypi.org/project/dbt-for-apache-doris/)
![Python](https://img.shields.io/badge/Python-%3E%3D3.10-blue)
![dbt Core](https://img.shields.io/badge/dbt--core-1.12.x-orange)
[![License](https://img.shields.io/badge/License-Apache--2.0-blue)](https://github.com/velodb/dbt-for-apache-doris/blob/main/LICENSE)

`dbt-for-apache-doris` enables Python dbt Core projects to transform data in
Apache Doris through the Doris MySQL protocol. It is maintained by the VeloDB
community.

**[Installation](#installation)** · **[Quickstart](#quickstart)** ·
**[Examples](#end-to-end-examples)** ·
**[Compatibility](#compatibility)** ·
**[PyPI](https://pypi.org/project/dbt-for-apache-doris/)** ·
**[dbt docs](https://docs.getdbt.com/)** ·
**[Doris docs](https://doris.apache.org/docs/)** ·
**[Issues](https://github.com/velodb/dbt-for-apache-doris/issues)** ·
**[Releases](https://github.com/velodb/dbt-for-apache-doris/releases)**

## Supported capabilities

Status: ✅ Supported · ❌ Not supported

Feature status and database-version compatibility are separate contracts.
`Supported` means the documented scope is implemented and tested; explicit
platform boundaries are described alongside each capability.

### Materializations

| Capability | Status | Current support and boundaries |
| --- | --- | --- |
| Table | ✅ Supported | Duplicate Key CTAS; configurable HASH distribution, integer buckets, RANGE/LIST partitions, properties, contracts, docs, grants, and hooks. Unique Key creation belongs to incremental `merge` |
| View | ✅ Supported | Standard lifecycle, contracts, docs, grants, and hooks; relation-type switching is not zero-downtime |
| Incremental | ✅ Supported | Four strategies and every `on_schema_change` mode; boundaries are listed below |
| Snapshot | ✅ Supported | `check`/`timestamp`, hard-delete modes, schema evolution, atomic replacement, and recovery; same-target runs must be serialized by the scheduler |
| Materialized view | ✅ Supported | Standard dbt `materialized_view`, implemented with Doris Async MV; build/refresh lifecycle, task waiting, configuration changes, atomic replacement, and recovery. Same-target dbt runs must be serialized by the scheduler |
| Seed | ✅ Supported | CSV loading, type inference, `column_types`, and `ref` |
| Ephemeral | ✅ Supported | Compiled and inlined by dbt Core |

### dbt capabilities

| Capability | Status | Current support and boundaries |
| --- | --- | --- |
| Sources and freshness | ✅ Supported | `loaded_at_field`, filter, and `loaded_at_query`; sources may use Internal or External Catalog relations |
| Data tests | ✅ Supported | Singular, generic, ephemeral, and `store_failures` paths |
| dbt Unit tests | ✅ Supported | Inline-row and CSV fixtures, case-insensitive columns, invalid-input validation, quoted reserved words, Doris-adapted data-type fixtures, and non-truncating VARCHAR fixtures |
| Model contracts | ✅ Supported | Column names/types for Table, View, and Incremental; not database PK/NOT NULL constraints |
| Persisted docs | ✅ Supported | Relation and column comments for Table, View, Incremental, Snapshot, Seed, and Async MV; updating View comments or comment text containing both quote delimiters may require recreation/full refresh |
| Grants | ✅ Supported | Reconciles supported Doris table privileges for `user` and `user@host` principals on Table, View, Incremental, Seed, Snapshot, and Async MV; role principals are not reconciled |
| Hooks | ✅ Supported | Pre-hooks and post-hooks across adapter materializations; Doris does not provide transactional rollback for hook side effects |
| Metadata and dbt docs catalog | ✅ Supported | Relation and column discovery for Internal and External Catalogs; Internal Catalog Async MV detection |
| Cross-database and cross-catalog sources | ✅ Supported | Two-part `database.table` within Internal Catalog and three-part `catalog.database.table` for configured External Catalogs |
| Advanced metadata APIs | ❌ Not supported | Catalogs V2, metadata-by-relation, single-relation catalog, and last-modified metadata are not declared |

## Compatibility

| Component | Declared or runtime constraint | Current evidence or status |
| --- | --- | --- |
| Python | `>=3.10` | Unit CI covers 3.10 and 3.14; the distribution-build job uses 3.12 |
| dbt Core | `>=1.12,<1.13` | Declared lower bound is 1.12.0; Python dbt Core v1 only. Fusion/v2 compatibility is not claimed |
| MySQL connector | `>=8.0.33` | Installed automatically with the adapter |
| Apache Doris | No package-wide minimum is declared | Validate the adapter against the Doris release and topology used in production |
| Async MV | Doris 2.x >=2.1.5; Doris 3.x except 3.0.0; Doris 4.x+ | This runtime gate applies to Async MV. Identifiable source builds are accepted for development testing only |
| VeloDB | No release range is declared | Validate the adapter against the VeloDB release and topology used in production |

Before production use, validate the adapter against your exact database release
and deployment topology.

## Installation

Install the VeloDB-maintained distribution from PyPI:

```shell
python -m venv .venv
source .venv/bin/activate
python -m pip install "dbt-for-apache-doris==1.1.0"
dbt --version
```

On Windows, create the environment with `py -m venv .venv`, activate it using
`.venv\Scripts\Activate.ps1`, and run the same `pip install` command.

The adapter declares dbt Core and the MySQL connector as dependencies, so they
are installed automatically. You do not need to install dbt Core separately or
download a standalone binary.

## Quickstart

Add a Doris output to `~/.dbt/profiles.yml`. Keep credentials outside version
control; this example reads the password from an environment variable:

```yaml
doris_demo:
  target: dev
  outputs:
    dev:
      type: doris
      host: 127.0.0.1
      port: 9030
      username: root
      password: "{{ env_var('DORIS_PASSWORD') }}"
      schema: analytics
      threads: 4
```

On Doris, dbt `schema` is a Doris Database. Omit dbt `database` for the
Internal Catalog.

Create a new `doris-demo` directory with a `models` subdirectory, then add:

```yaml
# dbt_project.yml
name: doris_demo
version: 1.0.0
config-version: 2
profile: doris_demo
model-paths: ["models"]
```

```sql
-- models/example.sql
{{ config(materialized='table', replication_num=1) }}
select 1 as id, 'hello from dbt-for-apache-doris' as message
```

```yaml
# models/schema.yml
version: 2
models:
  - name: example
    columns:
      - name: id
        data_tests: [not_null, unique]
```

`replication_num=1` is only for a local single-BE Quickstart.

```shell
export DORIS_PASSWORD='<your-password>'
dbt debug
dbt build
```

On Windows PowerShell, set the password with
`$env:DORIS_PASSWORD = '<your-password>'`, then run the same dbt commands.

### External Catalog sources

dbt relation fields map to Doris as `database.schema.identifier` →
`catalog.database.table`. Set `database` to an existing Doris Catalog and
`schema` to the Database inside that Catalog:

```yaml
sources:
  - name: lakehouse
    database: hive_catalog
    schema: ods
    tables:
      - name: orders
```

`{{ source('lakehouse', 'orders') }}` renders as
`` `hive_catalog`.`ods`.`orders` ``. The adapter discovers its tables and
columns from the selected Catalog and includes them in `dbt docs generate`.
The External Catalog must already exist in Doris. DDL and write support depend
on the corresponding Doris Catalog connector.

### Creating Iceberg targets

Set the profile's `database` to an existing writable Iceberg Catalog and
`schema` to a Database inside it. An omitted `engine` leaves the SQL engine
clause unset so Doris can infer it from the target Catalog. An explicit
`engine='iceberg'` is emitted in CREATE TABLE. Catalog-based inference was
verified against Doris 4.1.3; an older Doris release may require the explicit
engine. Explicit engines are preserved, including incompatible values that
Doris must reject.

Catalog discovery supports dbt's default identifier quoting. Incremental
strategy selection uses the actual Catalog type reported by Doris, so omitting
`engine` does not accidentally select OLAP Key validation for Iceberg.

Iceberg CREATE paths do not automatically add Doris UNIQUE KEY,
Merge-on-Write properties, or default OLAP distribution. A dbt `unique_key`
remains a logical matching key. Explicit physical options such as
`duplicate_key`, `distributed_by`, and `properties` are preserved for Doris to
validate; OLAP-only options are incompatible with Iceberg targets.

Iceberg append uses INSERT INTO. Merge uses native MERGE INTO with null-safe
matching on the logical `unique_key`, full-row updates and inserts. The frozen
source is checked for duplicate keys before target schema changes or DML; a
duplicate batch returns an error instead of choosing an arbitrary row. Merge
requires a Doris version with native Iceberg MERGE and Iceberg format version 2
or higher. This workflow was verified on Doris 4.1.3 with V2 tables. Doris
[documents MERGE as experimental since 4.1.0](https://doris.apache.org/docs/4.x/lakehouse/catalogs/iceberg-catalog/#merge-into).
OLAP merge retains its existing Unique Key INSERT upsert.

View and Async MV targets must use the Internal Catalog. The adapter rejects
external targets before model hooks, sql_header or relation replacement. Changing
an existing Iceberg table model to `materialized='view'` returns a clear error
and preserves the original table. This check uses the target Catalog regardless
of the configured engine. Data tests with `store_failures_as='view'` also reject
external targets before deleting existing audit results. Views and MVs created
in the Internal Catalog can still query Iceberg sources.

Table model reruns and incremental `--full-refresh` use a non-atomic replacement
for external targets. The adapter renames the old target to dbt's backup name,
then renames the fully built intermediate table to the target name. Table models
delete the backup after publication; incremental full refresh moves it to the
intermediate name so the existing post-processing and cleanup keep the old data
until they finish. OLAP targets retain the atomic `REPLACE WITH TABLE` path.

The target name is briefly absent between the two renames. If publication
fails, the old data remains in the backup table. The existing Table retry path
can restore it; the incremental retry path uses it as a marker for a complete
rebuild. Failure after publication does not roll the new target back. Rename
destinations are not deleted in this replacement path, so conflicting names
are rejected by Doris. Source, target and backup names must be distinct and
within the same Catalog and Database. The Catalog must support table rename;
this behavior was verified with an Iceberg REST Catalog on Doris 4.1.3.

Ordinary Iceberg incremental runs can use
`incremental_strategy='insert_overwrite'` with no `unique_key`. The adapter
freezes the model result in a physical Iceberg staging table, then executes
one native INSERT OVERWRITE against the target and cleans up the stage. It
does not create the logical metadata View used by ordinary OLAP incrementals.
This adds a staging write. The target is not renamed during ordinary overwrite.

On an unpartitioned Iceberg target, the SELECT must provide the complete
replacement result; an empty result clears the table. On a partitioned target,
an unspecified scope uses native dynamic overwrite: partitions absent from
the result remain unchanged, including when the result is empty.

To replace a static Iceberg partition, configure a non-empty mapping rather
than Doris's OLAP partition names:

```sql
{{ config(materialized='incremental', incremental_strategy='insert_overwrite',
          partition_type='LIST', partition_by=['dt'],
          overwrite_partitions={'dt': '2025-01-25'}) }}
select id, value, dt from {{ ref('input') }} where dt = '2025-01-25'
```

The model SELECT must return the configured partition columns and keep every
row within that scope. The adapter validates the frozen source, removes static
columns from the INSERT projection and emits `PARTITION(dt='2025-01-25')`.
An empty result clears the selected partition while preserving other
partitions. String, finite numeric, boolean and NULL values can be configured;
Doris validates their compatibility with the actual partition specification.
A non-empty `unique_key` is still rejected for insert_overwrite.

The four `on_schema_change` policies are exercised on Iceberg: `ignore`, `fail`,
`append_new_columns`, and `sync_all_columns`. External column DDL executes
through the connector without polling an OLAP Schema Change job; OLAP keeps
its asynchronous job wait. Supported type changes depend on the connector and
its actual ALTER result. Schema DDL and overwrite are separate statements and
do not provide rollback for an entire dbt run. Column documentation uses
Catalog-qualified metadata and preserves the current type, nullability and
default when an Iceberg comment changes. Grants and revokes retain the complete
Catalog/Database/Table name; reconciliation checks exact object grants rather
than mixing permissions from identically named tables in other Catalogs.
Updating an existing Iceberg table-level comment remains unsupported by the
tested Doris 4.1.3 ALTER paths.

Iceberg microbatch freezes and validates each Core UTC event-time window,
then uses one native MERGE to delete the old window and insert its replacement.
An empty batch still clears that window; other windows remain unchanged.
This works on unpartitioned tables and Iceberg LIST partition layouts such as
`day(event_time)`. Configure `event_time`, `begin` and `batch_size`; use V2 or
higher tables and a Doris version supporting Iceberg MERGE. Do not configure
`unique_key`, named `overwrite_partitions`, OLAP Dynamic Partition properties,
or `partition_by_init`. Core batch execution stays serial. Each window has one
atomic data publication; a multi-window run and schema changes are not one
transaction. Missing-target recovery restores the complete backup before
applying the next window.

Iceberg Snapshots build a helper with the target's current column definitions
and LIST partition expressions instead of unsupported CREATE TABLE LIKE.
Repeated runs preserve SCD history and support added columns without polling
OLAP jobs. Publication uses the same non-atomic backup/rename path. A retry
restores a missing target from its backup; an existing target plus backup is
rejected for inspection rather than deleting the recovery copy.

Iceberg seeds load every bound CSV batch into a private table before publishing
any of it. Ordinary reload verifies the CSV schema and performs one native
INSERT OVERWRITE, avoiding unsupported TRUNCATE and repeated batch overwrites.
A header-only CSV clears an existing unpartitioned target. Ordinary reload
defaults to the target's existing field types, including when new samples are
all NULL or numeric text. Explicit column types take precedence. A schema
change requires `dbt seed --full-refresh`; first creation and full refresh infer
types from the CSV unless explicit column types are configured.

First creation publishes the completely loaded stage. Full refresh uses the
backup replacement described above, after all CSV batches have loaded. A
database loading error leaves the existing target data intact for retry; the
adapter retains Doris's configured casting behavior. If a missing target has a
backup, Seed can restore it before retrying. When both target and backup exist,
the adapter refuses to remove the recovery copy automatically. These paths
were verified for unpartitioned Iceberg on Doris 4.1.3. Full-refresh publication
remains non-atomic, and post-publication errors do not roll data back.

Internal OLAP seeds delegate to Core's existing materialization: ordinary
reload remains TRUNCATE plus INSERT, and full refresh remains DROP plus CREATE
and INSERT. CSV batching, bindings, hooks, grants, documentation and result row
counts retain Core's behavior.

The opt-in functional regression uses the configured Doris test endpoint and
an existing writable Catalog:

```bash
DBT_DORIS_ICEBERG_CATALOG=iceberg_catalog make test
```

Without that environment variable, Iceberg-specific tests are skipped.

## End-to-end examples

The [`examples`](https://github.com/velodb/dbt-for-apache-doris/tree/main/examples)
tree contains five runnable Doris projects and a single user-facing entry point
at `examples/doris-demos`. Open its README and `notebooks/` directory; the
project directories and runner scripts are implementation details. Five
focused Jupyter Notebooks cover Table, View, Seed, Data Test, cross-database
Source, incremental `merge`, Snapshot, and Async MV workflows.

Follow the [examples quick start](https://github.com/velodb/dbt-for-apache-doris/tree/main/examples/doris-demos#run-the-jupyter-notebooks)
to configure Doris, create the pinned dbt environment, start JupyterLab, and run
any of the five demos.

## Doris-specific highlights

### Incremental strategies

| Strategy | Doris target | Behavior and boundaries |
| --- | --- | --- |
| `append` | OLAP Duplicate Key or writable Iceberg table | Appends rows with `INSERT INTO` |
| `merge` | OLAP MOW/MOR Unique Key or Iceberg V2+ table | Requires `unique_key`; OLAP uses full-row INSERT upsert, Iceberg uses native MERGE with null-safe matching and duplicate-source validation |
| `insert_overwrite` | Writable OLAP or Iceberg table | Native `INSERT OVERWRITE`; OLAP supports named/dynamic partitions, Iceberg supports dynamic scope or a static column/value mapping; `unique_key` is rejected |
| `microbatch` | OLAP Duplicate Key with exact RANGE partitions, or Iceberg V2+ | OLAP overwrites one named partition; Iceberg atomically replaces one UTC window using MERGE, including empty windows; batches run serially |

Without an explicit strategy, `unique_key` selects `merge`; otherwise dbt uses
`append`.

### Materialized views

Use dbt's standard `materialized_view` materialization. The adapter implements
it with Doris Async MV and exposes Doris-specific refresh configuration:

```sql
{{ config(
    materialized='materialized_view',
    refresh_trigger='manual',
    wait_for_refresh=true
) }}

select order_date, sum(amount) as sales
from {{ ref('orders') }}
group by order_date
```

Supported lifecycles include immediate/deferred build, manual/schedule/commit
refresh, task waiting, configuration changes, docs, grants, and recovery.
Overlapping dbt runs against the same MV target must be serialized, and a dbt
wait timeout does not cancel a submitted Doris task.

## Known limitations

- Aggregate Key table modeling and secondary-index configuration are not
  supported.
- Catalogs V2 and connector-specific External Catalog write guarantees are not supported.
- SSL configuration, timeout/retry, multi-FE failover, server-side cancellation,
  and complete query telemetry are not implemented.
- Some Table/View/MV type changes have a short canonical-name availability
  window rather than a zero-downtime switch.

## Development and testing

Install development dependencies, then run local checks:

```shell
python -m pip install -r dev-requirements.txt
python -m pip install -e .
make lint
make test-unit
```

Functional tests need a dedicated non-production cluster. Edit
`test/doris_test.env`; use an external file for private credentials:

```shell
make test
make test DORIS_TEST_CONFIG=/secure/path/doris_test.env
```

Preflight records live FE/BE versions, checks replication against live BEs, and
requires `cross_db_test` to be absent. Tests create/drop databases, relations,
users, and grants, so the account needs those permissions. Never use a shared
or production cluster or run Functional sessions concurrently.

```shell
python scripts/run_doris_functional_tests.py --preflight-only
python scripts/run_doris_functional_tests.py -- -k snapshot -vv
```

The runner records evidence about the connected cluster but does not certify a
release compatibility matrix.

## License

The code is licensed under Apache License 2.0. See the
[license](https://github.com/velodb/dbt-for-apache-doris/blob/main/LICENSE),
[notice](https://github.com/velodb/dbt-for-apache-doris/blob/main/NOTICE), and
[migration provenance](https://github.com/velodb/dbt-for-apache-doris/blob/main/UPSTREAM.md).
