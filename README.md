# dbt-doris-adapter

An independent, community-maintained dbt adapter for Apache Doris.

This repository is derived from the
[`extension/dbt-doris`](https://github.com/apache/doris/tree/master/extension/dbt-doris)
component of Apache Doris. It is not an official Apache Software Foundation or
dbt Labs release.

## Compatibility

| Component | Supported baseline |
| --- | --- |
| dbt Core | 1.12.x |
| Apache Doris | 2.1.5+ expected minimum; exact releases are listed in the Incremental guide |
| Python | 3.10 or newer |
| Database protocol | Doris MySQL protocol |

The Python distribution remains named `dbt-doris`, and the adapter type used in
`profiles.yml` remains `doris`.

## Install from source

```shell
git clone https://github.com/xylaaaaa/dbt-doris-adapter.git
cd dbt-doris-adapter
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

Incremental models support `append`, `merge`, native `insert_overwrite`, and
dbt Core 1.12 `microbatch`. Microbatch replaces each exact Doris time partition
with a named `INSERT OVERWRITE`, including empty batches. `delete+insert` is
rejected; use a Doris Unique Key target with `merge` for upsert semantics.
Configuration, generated Doris SQL, staging behavior, migration notes, and the
validation matrix are documented in
[Incremental user guide](https://github.com/xylaaaaa/dbt-doris-adapter/blob/main/docs/incremental.zh-CN.md),
[Incremental test documentation](https://github.com/xylaaaaa/dbt-doris-adapter/blob/main/docs/incremental-tests.zh-CN.md),
and [release test plan](https://github.com/xylaaaaa/dbt-doris-adapter/blob/main/docs/incremental-test-plan.zh-CN.md).

To manage a Doris asynchronous materialized view, configure a model with
`materialized='materialized_view'`:

```sql
{{ config(
    materialized='materialized_view',
    build_mode='immediate',
    refresh_method='auto',
    refresh_trigger='manual',
    wait_for_refresh=true,
    properties={'replication_num': '1'}
) }}

select order_date, sum(amount) as sales
from {{ ref('orders') }}
group by order_date
```

`build_mode='immediate'` is the default. The adapter builds and waits for a
temporary MV before exposing it or atomically replacing an existing MV, so
downstream dbt models do not observe an unfinished refresh. The wait defaults
to 300 seconds with one-second polling and can be tuned with
`refresh_wait_timeout` and `refresh_poll_interval`. Set
`wait_for_refresh=false` only when asynchronous completion is intentional.
Waiting requires Doris materialized-view task history to remain enabled so the
adapter can identify the task submitted by the current dbt action.

The supported refresh triggers are `manual`, `schedule`, and `commit`.
Schedules accept `second`, `minute`, `hour`, `day`, or `week`. Set
`refresh_on_run=true` to refresh an unchanged manual MV on each dbt run.
Materialized-view `grants` are rejected until Doris-specific grant
introspection and DCL are implemented.

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
