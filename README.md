# dbt for Apache Doris

[![CI](https://github.com/velodb/dbt-for-apache-doris/actions/workflows/ci.yml/badge.svg)](https://github.com/velodb/dbt-for-apache-doris/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python-%3E%3D3.10-blue)
![dbt Core](https://img.shields.io/badge/dbt--core-1.12.x-orange)
![License](https://img.shields.io/badge/License-Apache--2.0-blue)

`dbt-doris` is a dbt Core adapter for transforming data in Apache Doris over
the Doris MySQL protocol. The project is maintained by the VeloDB community
and targets compatible VeloDB deployments as well.

> [!IMPORTANT]
> This adapter is currently **Beta**. GitHub Actions validates lint, 373 Unit
> tests, and Python package construction, but it does not run a live Doris
> cluster. VeloDB-specific release verification is still pending.

## Supported capabilities

### Materializations

| Capability | Status | Current support |
| --- | --- | --- |
| Table | ✅ Supported | Doris Duplicate Key and Unique Key tables, distribution, buckets, RANGE/LIST partitions, table properties, contracts, docs, grants, and hooks |
| View | ✅ Supported | Standard dbt view lifecycle, contracts, relation docs, grants, and hooks |
| Incremental | ✅ Supported | `append`, `merge`, `insert_overwrite`, and `microbatch`; all `on_schema_change` modes |
| Partition | ✅ Supported | Doris-specific replacement of selected RANGE partitions |
| Snapshot | ✅ Supported | `check` and `timestamp`, hard-delete modes, schema evolution, atomic replacement, and failed-run recovery |
| Async materialized view | ✅ Supported | Immediate/deferred build; manual/schedule/commit refresh; task waiting; configuration change; atomic replacement and recovery |
| Seed | ✅ Supported | CSV seeds and column type configuration |
| Ephemeral | ✅ Supported | Compiled and inlined by dbt Core |
| Sync materialized view | ❌ Not supported | Doris synchronous rollups have a different lifecycle and are outside this adapter |

### dbt features

| Capability | Status | Current support |
| --- | --- | --- |
| Sources and freshness | ✅ Supported | Source relations plus `loaded_at_field`, filter, and `loaded_at_query` freshness paths |
| Data tests | ✅ Supported | Singular and generic tests, including `store_failures` |
| dbt Unit tests | ✅ Supported | User-authored SQL model Unit tests |
| Model contracts | ✅ Supported | Enforced contracts for table, view, and incremental models |
| Persisted docs | ✅ Supported | Relation and column comments; View comments are updated when the View is recreated rather than with an in-place `ALTER` |
| Grants | ⚠️ Limited | Direct Doris user grants; role-based grants are not supported |
| Hooks | ✅ Supported | Pre-hooks and post-hooks |
| Metadata | ✅ Supported | Schema, relation, column, and internal-catalog introspection |
| Cross-database sources | ✅ Supported | Doris databases map through dbt schemas; dbt database metadata is omitted or normalized to the same value |

## Installation

This repository has not published a VeloDB-maintained package to PyPI yet.
The existing [`dbt-doris==1.0.0` project on PyPI](https://pypi.org/project/dbt-doris/1.0.0/)
is an earlier distribution and does not contain the current code from this
repository.

Install the current source in a virtual environment:

```shell
git clone https://github.com/velodb/dbt-for-apache-doris.git
cd dbt-for-apache-doris

python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install .
```

The adapter declares `dbt-core~=1.12.0` and `mysql-connector-python` as
dependencies, so `pip` installs them automatically. Verify the installation:

```shell
dbt --version
```

### Build and install a local package

Run the build from the repository root:

```shell
python -m pip install build
python -m build
```

The build creates two distributable files under `dist/`:

- `dbt_doris-1.0.0-py3-none-any.whl`: the pure-Python,
  platform-independent wheel
- `dbt_doris-1.0.0.tar.gz`: the source distribution

Install the wheel from the repository root:

```shell
python -m pip install dist/dbt_doris-1.0.0-py3-none-any.whl
```

The install command is not tied to the repository directory. From another
directory, pass a relative or absolute path to the wheel instead.

## Quick start

Add a Doris output to `~/.dbt/profiles.yml`:

```yaml
doris_demo:
  target: dev
  outputs:
    dev:
      type: doris
      host: 127.0.0.1
      port: 9030
      username: root
      password: ""
      schema: analytics
      threads: 4
```

On Doris, the dbt `schema` is the Doris database. If `database` is also set,
it must have the same value as `schema`.

Create a separate dbt project directory (outside this adapter repository), then
add the minimal project and model files below:

```shell
cd ..
mkdir -p doris-demo/models
cd doris-demo
```

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

select 1 as id, 'hello from dbt-doris' as message
```

```yaml
# models/schema.yml
version: 2
models:
  - name: example
    columns:
      - name: id
        data_tests:
          - not_null
          - unique
```

Verify the connection, build the model, and run its tests:

```shell
dbt debug
dbt run
dbt test
```

## Incremental strategies

| Strategy | Doris target | Behavior and boundaries |
| --- | --- | --- |
| `append` | Duplicate Key table | Appends rows with `INSERT INTO` |
| `merge` | MOW or MOR Unique Key table | Full-row `INSERT INTO` upsert using Doris Unique Key semantics; it does not emit `MERGE INTO` |
| `insert_overwrite` | Writable Doris table | Native whole-table or named-partition `INSERT OVERWRITE`; `unique_key` is rejected to prevent accidental destructive semantics |
| `microbatch` | Duplicate Key table with exact RANGE partitions | One named-partition overwrite per dbt Core UTC window; supports hour/day/month/year windows and currently runs batches serially |

If `incremental_strategy` is omitted, a model with `unique_key` uses `merge`;
a model without one uses `append`.

The adapter intentionally does not support `delete+insert`, partial-column
merge (`merge_update_columns` or `merge_exclude_columns`), or
`incremental_predicates`.

## Asynchronous materialized views

Use the standard dbt materialized-view materialization with Doris-specific
refresh configuration:

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

| Config | Values and behavior |
| --- | --- |
| `build_mode` | `immediate` (default) or `deferred` |
| `refresh_method` | `auto` (default) or `complete` |
| `refresh_trigger` | `manual` (default), `schedule`, or `commit` |
| `refresh_schedule` | Mapping with `interval`, `unit`, and optional `start_time`; production units are minute/hour/day/week |
| `wait_for_refresh` | Wait for the initial build or adapter-submitted manual refresh; defaults to `true` |
| `refresh_wait_timeout` / `refresh_poll_interval` | Task timeout and polling interval in seconds |
| `on_configuration_change` | `apply`, `continue`, or `fail` |

An unchanged manual MV is refreshed whenever the model is selected again;
scheduled and commit-triggered MVs leave refresh timing to Doris. The adapter
waits for Doris task history by comparing task IDs before and after submission.
Concurrent refreshes of the same MV can therefore be associated with the wrong
task, and a dbt timeout does not cancel a task already submitted to Doris.

## Compatibility and verification

| Component | Current policy or evidence |
| --- | --- |
| Python | 3.10 or newer |
| dbt Core | 1.12.x |
| Doris | Doris 4.1.3 is the latest recorded full-suite baseline; earlier exact releases have historical focused coverage |
| Async MV runtime gate | Accepts Doris 2.1.5+, except 3.0.0; this code gate is not a full compatibility guarantee |
| VeloDB | Release-specific live-cluster verification is pending |
| GitHub CI | Flake8, 373 Unit tests on Python 3.10 and 3.14, plus wheel/sdist build and Twine checks |
| Live Functional tests | 168 tests are collected, but they are not run by GitHub Actions |

Before production use, validate the adapter against the exact Doris or VeloDB
release and topology you deploy.

## Known limitations

- Microbatch execution is serial; concurrent batches are disabled.
- Running the same Snapshot concurrently from multiple dbt processes is not
  supported.
- Async MV task correlation is not safe for concurrent refreshes of the same MV.
- Aggregate Key modeling, complete Doris partition/distribution abstractions,
  secondary indexes, and synchronous MVs are not implemented.
- SSL settings, connection timeout/retry, multi-FE failover, server-side query
  cancellation, and complete query telemetry are not implemented.
- External Catalog is not represented as a full dbt namespace.
- GitHub Actions does not yet provide a live-Doris Functional gate or a
  multi-version nightly matrix.

## Development and testing

Install the development dependencies and the adapter in editable mode:

```shell
python -m pip install -r dev-requirements.txt
python -m pip install -e .
```

Unit tests and lint do not require Doris:

```shell
python -m flake8 dbt test
python -m pytest test/unit
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

## Release model

The intended user installation channel is PyPI. A release should publish one
immutable build to both PyPI and the matching GitHub Release:

- `dbt_doris-X.Y.Z-py3-none-any.whl`
- `dbt_doris-X.Y.Z.tar.gz`

Before the first repository release, the maintainers must confirm ownership of
the existing `dbt-doris` PyPI project, choose a new version (PyPI versions are
immutable), and configure PyPI Trusted Publishing. The current CI only builds
and inspects distributions; it does not upload them.

## License

The code is licensed under Apache License 2.0. See [LICENSE](LICENSE) and
[NOTICE](NOTICE).
