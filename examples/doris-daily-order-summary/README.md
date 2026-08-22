# Doris Daily Order Summary Demo

This example creates a daily order summary Table from an order source table,
then builds an Apache Doris asynchronous materialized view on top of it.

The run creates:

```text
dbt_demo_daily_source.orders                 order source table
dbt_demo_daily.daily_order_summary           dbt Table model
dbt_demo_daily.monthly_order_summary_mv      Doris Async Materialized View
```

`scripts/setup.sql` drops and recreates only the dedicated `dbt_demo_daily` and
`dbt_demo_daily_source` Demo databases. Do not use them for business data.

## Prerequisites

- Install `uv` and a MySQL-compatible client.
- Use a reachable Doris FE with at least one healthy BE, or the official
  all-in-one Docker image.
- Use a Doris user that can create and modify the `dbt_demo_daily*` databases.

For the complete environment requirements, all-in-one Docker startup command,
and JupyterLab instructions, see the
[Doris dbt demos Quick Start](../doris-demos/README.md#prerequisites).

## Run

From the repository root, prepare the pinned dbt environment. The script
installs dbt Core and `dbt-for-apache-doris`; no editable install is required:

```bash
examples/doris-demos/scripts/prepare-python-env.sh
```

An existing Doris cluster defaults to `root@127.0.0.1:9030`. Run the daily
order summary Demo:

```bash
examples/doris-daily-order-summary/scripts/run.sh
```

When using the all-in-one Docker image from the central Quick Start, map the FE
to host port `29030`:

```bash
DORIS_PORT=29030 \
  examples/doris-daily-order-summary/scripts/run.sh
```

Use `DORIS_HOST`, `DORIS_PORT`, `DORIS_USER`, and `DORIS_PASSWORD` to connect
to another Doris environment. The script uses the dbt CLI created by the
central setup step by default; set `DBT_BIN` to use a custom dbt CLI.

The script initializes the fixture, runs `dbt debug`, creates the Table and
four data tests, creates the MV, submits a second MV refresh, and verifies the
data, Doris DDL, and latest MV task status.

## Run All Five Release Demos

From the repository root, run these commands to create an isolated environment
with Python 3.12.13, dbt Core 1.12.2, and dbt-for-apache-doris 1.1.0, then run
the five Demos: daily summary, geographic analysis, advertising consolidation,
late-arriving order incremental processing, and Snapshot.

```bash
examples/doris-demos/scripts/prepare-python-env.sh
examples/doris-demos/scripts/run-all.sh
```

If the all-in-one Docker image uses host port `29030`, prefix the second command
with `DORIS_PORT=29030`.

Each run stores environment information, per-Demo logs, and `summary.tsv` in
`examples/doris-demos/artifacts/<run-id>`. Set `DEMO_RESULTS_DIR` to choose a
different output directory.

Expected daily result:

| order_date | order_count | total_revenue |
| --- | ---: | ---: |
| 2026-08-01 | 1 | 100.00 |
| 2026-08-02 | 1 | 80.00 |
| 2026-08-03 | 1 | 40.20 |
