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
| Sources and freshness | ✅ Supported | `loaded_at_field`, filter, and `loaded_at_query`; cross-database uses database-as-schema, not External Catalog |
| Data tests | ✅ Supported | Singular, generic, ephemeral, and `store_failures` paths |
| dbt Unit tests | ✅ Supported | Inline-row and CSV fixtures, case-insensitive columns, invalid-input validation, quoted reserved words, Doris-adapted data-type fixtures, and non-truncating VARCHAR fixtures |
| Model contracts | ✅ Supported | Column names/types for Table, View, and Incremental; not database PK/NOT NULL constraints |
| Persisted docs | ✅ Supported | Relation and column comments for Table, View, Incremental, Snapshot, Seed, and Async MV; updating View comments or comment text containing both quote delimiters may require recreation/full refresh |
| Grants | ✅ Supported | Reconciles supported Doris table privileges for `user` and `user@host` principals on Table, View, Incremental, Seed, Snapshot, and Async MV; role principals are not reconciled |
| Hooks | ✅ Supported | Pre-hooks and post-hooks across adapter materializations; Doris does not provide transactional rollback for hook side effects |
| Internal metadata and dbt docs catalog | ✅ Supported | Relation discovery and docs catalog for Doris databases, tables, views, columns, comments, and Async MVs |
| Cross-database sources | ✅ Supported | Sources in other Doris databases, including database-only source definitions; External Catalog three-part names are not supported |
| Advanced metadata / External Catalog | ❌ Not supported | Catalogs V2, metadata-by-relation, single-relation catalog, last-modified metadata, and External Catalog three-part namespaces are not declared |

## Compatibility

| Component | Declared or runtime constraint | Current evidence or status |
| --- | --- | --- |
| Python | `>=3.10` | Unit CI covers 3.10 and 3.14; the distribution-build job uses 3.12 |
| dbt Core | `>=1.12,<1.13` | Declared lower bound is 1.12.0; Python dbt Core v1 only. Fusion/v2 compatibility is not claimed |
| MySQL connector | `>=8.0.33` | Installed automatically with the adapter |
| Apache Doris | No package-wide minimum is declared | Validate the adapter against the Doris release and topology used in production |
| Async MV | Doris 2.x >=2.1.5; Doris 3.x except 3.0.0; Doris 4.x+ | This runtime gate is not a whole-adapter compatibility guarantee. Identifiable source builds are accepted for development testing only |
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

On Doris, `schema` is a database; an optional `database` must match it.

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

## End-to-end examples

The [`examples/doris-demos`](examples/doris-demos/README.md) directory contains
five runnable Doris projects and a guided Jupyter Notebook covering Table,
View, Seed, Data Test, cross-database Source, incremental `merge`, Snapshot,
and Async MV workflows.

Follow the [examples quick start](examples/doris-demos/README.md#run-the-jupyter-notebook)
to configure Doris, create the pinned dbt environment, start JupyterLab, and
execute all five demos.

## Doris-specific highlights

### Incremental strategies

| Strategy | Doris target | Behavior and boundaries |
| --- | --- | --- |
| `append` | Duplicate Key table | Appends rows with `INSERT INTO` |
| `merge` | MOW or MOR Unique Key table | Full-row `INSERT INTO` upsert using Doris Unique Key semantics; requires `unique_key` and does not emit SQL `MERGE INTO` |
| `insert_overwrite` | Writable Doris table | Whole-table, named-partition, or dynamic-partition `INSERT OVERWRITE`; `unique_key` is rejected |
| `microbatch` | Duplicate Key table with exact RANGE partitions | One named-partition overwrite per dbt Core UTC window; hour/day/month/year windows; static or dynamic partitions; batches run serially |

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
- A complete External Catalog namespace is unsupported.
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
