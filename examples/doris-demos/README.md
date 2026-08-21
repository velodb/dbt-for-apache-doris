# Doris dbt demos

These are product-facing examples for `dbt-for-apache-doris`. They show how a
dbt project is compiled and executed on Apache Doris, and how the adapter maps
dbt concepts to Doris databases, Tables, Views, Unique Key tables, Snapshots,
and Async Materialized Views.

This is a standalone Doris dbt demo suite. Each example owns its small Doris
fixture and can be run independently. The shell scripts are the repeatable
path; five focused Jupyter Notebooks present the same work as interactive
walkthroughs.

## What the examples show

Every demo follows the same data path:

```text
Doris source table or CSV
        -> dbt Source or Seed
        -> staging View
        -> business model
        -> dbt Data Test or Snapshot check
        -> Doris verifier and result query
```

The five examples cover the adapter behaviors most users need to see first:

- how a Table model becomes a Doris table with partitioning, bucketing, and properties;
- how multiple Doris databases are declared as dbt Sources and connected with `ref()`;
- how CSV data is loaded with Seed and transformed with a package macro;
- how `incremental` with `unique_key` applies a late correction through Doris Unique Key merge;
- how Snapshot records an update and a hard delete as SCD Type 2 history.

Each Notebook makes every arrow in one flow visible. It shows the input rows,
the dbt file being used, the intermediate relation, the Data Test result, and
the final Doris rows step by step.

| Demo | What it demonstrates | Main dbt and Doris capabilities |
| --- | --- | --- |
| [Daily order summary](notebooks/01-daily-order-summary.ipynb) | Filter and aggregate orders by day and month | Source, Table, Data Test, partitioning, bucketing, Async MV |
| [Customer geographic analysis](notebooks/02-customer-geographic-analysis.ipynb) | Join customer addresses and orders by state | Cross-database Source, View, Table, `ref()` |
| [Advertising consolidation](notebooks/03-advertising-consolidation.ipynb) | Load and normalize three advertising CSV files | Seed, `dbt_utils`, `QUALIFY`, Data Test |
| [Late-arriving orders](notebooks/04-late-arriving-orders.ipynb) | Insert a corrected order version and a new order | Incremental `merge`, Unique Key, idempotency |
| [Customer Snapshot](notebooks/05-customer-snapshot.ipynb) | Track an update and hard delete over time | Snapshot, SCD Type 2, current dimension |

Each demo recreates only its dedicated `dbt_demo_*` databases. Do not use those
database names for production data.

## What each demo does

### 1. Daily order summary

This demo turns a small order table into a daily sales report. It filters out
cancelled, returned, and failed orders, groups the remaining orders by date,
and then builds a monthly summary from the daily table. The result shows a
dbt Table, Doris partitioning and bucketing, data tests, and an Async MV
working together.

### 2. Customer geographic analysis

This demo answers a reporting question: how many customers and orders does
each state have, and how much revenue do they represent? Customer addresses
and orders live in separate Doris databases, so the project declares two
Sources, creates two staging Views, and joins them with `ref()`. The final
Table contains one row per state.

### 3. Advertising consolidation

This demo combines Google, Meta, and TikTok advertising exports into one
consistent table. The three CSV files have slightly different column names and
one duplicate row, so dbt Seed loads the files, staging models normalize the
columns and remove duplicates, and a final model unions the channels. It shows
Seed, package macros, `QUALIFY`, and a uniqueness test.

### 4. Late-arriving orders

This demo models an order-event stream where an updated version can arrive
after the first load. The first run writes the current version of three
orders; the second run receives a correction for order 101 and a new order
104. An incremental model with `unique_key='order_id'` and Doris Unique Key
merge keeps one current row per order, and a third run confirms idempotency.

### 5. Customer Snapshot

This demo keeps a history of customer changes. The first Snapshot run records
Alice and Bob, then the fixture updates Alice and deletes Bob before the second
run. dbt Snapshot closes the old versions and writes the new state, while a
dimension model selects the current customer rows. The final result shows SCD
Type 2 history and hard-delete handling in Doris.

## Prerequisites

Before starting a Notebook, prepare:

- A checkout of this repository and a Bash-compatible shell.
- [`uv`](https://docs.astral.sh/uv/), used by the setup script to create the
  pinned Python 3.12.13 environment.
- A MySQL-compatible command-line client. Doris uses the MySQL protocol for
  fixture setup and result queries.
- An Apache Doris endpoint. Use an existing FE with at least one healthy BE,
  or start the optional all-in-one Docker image below. The FE query port must
  be reachable from the machine running dbt.
- A Doris user that can create, drop, and modify the dedicated `dbt_demo_*`
  databases and their tables, views, materialized views, and snapshots.

The setup script installs dbt Core 1.12.2, `dbt-for-apache-doris` 1.1.0, and
JupyterLab. The advertising Demo also runs `dbt deps` to install `dbt_utils`,
so the first setup needs access to the configured Python and package sources.

For a remote server, run dbt and JupyterLab on the server or on a machine that
can reach Doris. Use SSH port forwarding when the Notebook is opened from a
local browser.

### Optional: start Doris with the all-in-one Docker image

The official [Apache Doris all-in-one image](https://doris.apache.org/community/developer-guide/all-in-one-image/)
packages one FE and one BE in a single container. It is convenient for this
demo suite because the examples use one replica and create their own
`dbt_demo_*` databases. The image does not persist data unless you add a
volume.

The following commands keep the Docker instance separate from a Doris cluster
that may already be using port `9030`:

```bash
export DORIS_CONTAINER_NAME=dbt-doris-demo
export DORIS_IMAGE=apache/doris:all-in-one-4.1.3
export DORIS_PORT=29030

docker pull "$DORIS_IMAGE"
docker rm -f "$DORIS_CONTAINER_NAME" 2>/dev/null || true
docker run -d --name "$DORIS_CONTAINER_NAME" \
  -p 29030:9030 \
  -p 28030:8030 \
  -p 28040:8040 \
  "$DORIS_IMAGE"

until [ "$(docker inspect -f '{{.State.Health.Status}}' "$DORIS_CONTAINER_NAME")" = healthy ]; do
  sleep 1
done

export DORIS_HOST=127.0.0.1
export DORIS_PORT=29030
export DORIS_USER=root
export DORIS_PASSWORD=''
mysql -h "$DORIS_HOST" -P "$DORIS_PORT" -u "$DORIS_USER" \
  -e 'SHOW BACKENDS'
```

The all-in-one image is multi-architecture. Use a native image for the host
architecture when possible, especially on Apple Silicon. Stop and remove the
demo container after use:

```bash
docker stop "$DORIS_CONTAINER_NAME"
docker rm "$DORIS_CONTAINER_NAME"
```

## Run the Jupyter Notebooks

Run all commands from the repository root.

### 1. Configure the Doris connection

The defaults connect to `root@127.0.0.1:9030` with an empty password. Override
them when your Doris FE uses different connection settings:

```bash
export DORIS_HOST=127.0.0.1
export DORIS_PORT=9030
export DORIS_USER=root
export DORIS_PASSWORD=''
```

Verify that the FE and at least one Backend are available:

```bash
MYSQL_PWD="$DORIS_PASSWORD" mysql \
  -h "$DORIS_HOST" -P "$DORIS_PORT" -u "$DORIS_USER" \
  -e 'select sum(number) from numbers("number"="10")'
```

The expected result is `45`.

### 2. Prepare dbt and JupyterLab

Install [`uv`](https://docs.astral.sh/uv/) and a MySQL client first, then run:

```bash
INSTALL_JUPYTER=1 examples/doris-demos/scripts/prepare-python-env.sh
source examples/doris-demos/.venv/bin/activate
export DBT_BIN="$PWD/examples/doris-demos/.venv/bin/dbt"
```

The setup script creates an isolated environment with Python 3.12.13, dbt Core
1.12.2, and `dbt-for-apache-doris` 1.1.0. Re-running it reuses a matching
environment.

### 3. Start JupyterLab

```bash
export JUPYTER_PORT=18888
examples/doris-demos/scripts/start-notebook.sh
```

Keep this terminal running. JupyterLab prints a URL containing a token, for
example:

```text
http://127.0.0.1:18888/lab?token=...
```

Open that URL in a browser. JupyterLab opens the `notebooks/` directory with
five numbered Notebook files. Start with any Demo; each file includes its own
environment check, fixture setup, dbt execution, Doris queries, and verifier.

### 4. Execute the demos

1. Open one of the five numbered Notebook files.
2. Run the first **Check the execution environment** cell. It loads the visual
   styles and verifies dbt and Doris.
3. Use **Run All** to execute that Demo, or run the numbered cells in order to
   inspect every transformation step.
4. Expand **View full run log** only when you need the compiled dbt details.

Stop JupyterLab with `Ctrl+C` in the terminal that started it.

## Run all demos from the command line

The same five demos can run without JupyterLab:

```bash
source examples/doris-demos/.venv/bin/activate
export DBT_BIN="$PWD/examples/doris-demos/.venv/bin/dbt"
examples/doris-demos/scripts/run-all.sh
```

The runner verifies the exact dbt Core and adapter versions, waits for Doris,
runs the five demos serially, and writes environment information, per-demo logs,
and `summary.tsv` under `examples/doris-demos/artifacts/<run-id>/`.

To run one demo, invoke its script directly. For example:

```bash
examples/doris-demos/geographic/scripts/run.sh
```

Each demo directory contains:

- `scripts/setup.sql`: creates the small Doris fixture;
- `scripts/run.sh`: executes dbt and the verifier;
- `scripts/verify.sh`: checks the Doris relations and expected data;
- `models/`, `seeds/`, or `snapshots/`: the dbt project shown in its Notebook.
