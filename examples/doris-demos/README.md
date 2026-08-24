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

The five examples start with common analytics and data engineering requests:

- give sales and finance a trusted daily and monthly revenue summary;
- show regional teams where customers, orders, and revenue are concentrated;
- give growth teams one consistent dataset for three advertising channels;
- keep current order reporting accurate when corrections arrive late;
- preserve customer attribute history while serving a current customer view.

Each Notebook makes every arrow in one flow visible. It shows the input rows,
the dbt file being used, the intermediate relation, the Data Test result, and
the final Doris rows step by step.

| Demo | Business question | Delivered dataset | Main dbt and Doris capabilities |
| --- | --- | --- | --- |
| [Daily order summary](notebooks/01-daily-order-summary.ipynb) | How many valid orders and how much valid-order revenue did we record each day and month? | Daily sales table and monthly summary MV | Source, Table, Data Test, partitioning, bucketing, Async MV |
| [Customer geographic analysis](notebooks/02-customer-geographic-analysis.ipynb) | Which states concentrate our customers, valid orders, revenue, and order frequency? | One state-level customer and sales table | Cross-database Source, View, Table, `ref()` |
| [Advertising consolidation](notebooks/03-advertising-consolidation.ipynb) | How can growth analysts compare Google, Meta, and TikTok exports with one schema? | Deduplicated channel-by-day advertising table | Seed, `dbt_utils`, `QUALIFY`, Data Test |
| [Late-arriving orders](notebooks/04-late-arriving-orders.ipynb) | How do current-order and revenue reports absorb delayed corrections without duplicate orders? | Current version of every order plus reconciliation models | Incremental `merge`, Unique Key, idempotency |
| [Customer Snapshot](notebooks/05-customer-snapshot.ipynb) | What did each customer record look like over time, and which version is current now? | SCD Type 2 history and current customer dimension | Snapshot, hard-delete tracking, current dimension |

Each demo recreates only its dedicated `dbt_demo_*` databases. Do not use those
database names for production data. Run one demo process at a time because the
fixtures use fixed database names and are recreated at the start of a run.

## What each demo does

### 1. Daily order summary

Sales operations needs a daily report for order volume and valid-order
revenue, while finance needs the same figures rolled up by month. Cancelled,
returned, and failed orders are excluded from both measures. The project
delivers `daily_order_summary` for daily reporting and
`monthly_order_summary_mv` for the monthly dashboard, with tests protecting
the date and revenue grain.

### 2. Customer geographic analysis

Regional operations wants to decide where to focus customer programs and
sales coverage. The report uses each customer's default shipping state and
only valid orders, then calculates customer count, order count, total revenue,
average order value, revenue per customer, and orders per customer. The final
table has one row per state and can feed a regional performance dashboard.

### 3. Advertising consolidation

Growth analysts receive daily exports from Google, Meta, and TikTok, but the
files use different column layouts and can contain duplicate rows. The project
normalizes clicks, impressions, views, and conversions, removes exact
duplicates, and adds a source label. The resulting channel-by-day table gives
downstream campaign reporting one stable input contract.

### 4. Late-arriving orders

Order systems can deliver a correction days after the original event. Without
version handling, the current-order table can duplicate an order or leave
revenue understated. The first load records three orders; the next load
changes order 101 from 100.00 to 125.00 and adds order 104. The project keeps
one current row per order and refreshes the reporting and reconciliation
models from the corrected state.

### 5. Customer Snapshot

CRM and analytics teams need both the current customer record and an audit of
how important attributes changed. The first load records Alice and Bob; the
second changes Alice's email and customer type and removes Bob from the source.
The Snapshot preserves the old versions, records when each version stopped
being valid, and supplies a current customer dimension for operational use.

## Prerequisites

Before starting a Notebook, prepare:

- A checkout of this repository and a Bash-compatible shell.
- [`uv`](https://docs.astral.sh/uv/), used by the setup script to create the
  pinned Python 3.12.13 environment.
- A MySQL-compatible command-line client. Doris uses the MySQL protocol for
  fixture setup and result queries.
- Docker, when using the optional all-in-one Doris image.
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

The launcher publishes every port on `127.0.0.1` and keeps the Docker instance
separate from a Doris cluster that may already use port `9030`:

```bash
export DORIS_PORT="${DORIS_PORT:-29030}"
examples/doris-demos/scripts/start-doris.sh
```

`start-doris.sh` pulls `apache/doris:all-in-one-4.1.3` when it needs to create
a container, waits up to 180 seconds for its health check, and prints useful
container logs when startup fails. It never stops or removes an existing
container. A container named `dbt-doris-demo` is reused only when it is
healthy and its image and three loopback port bindings match. Otherwise the
script exits with the existing container unchanged.

Continue with **Configure the Doris connection** below; its defaults use the
local all-in-one user and preserve the `DORIS_PORT` selected here.

Override the container, image, or host ports before running the launcher when
the defaults are already in use:

```bash
export DORIS_CONTAINER_NAME=dbt-doris-demo-2
export DORIS_IMAGE=apache/doris:all-in-one-4.1.3
export DORIS_PORT=39030
export DORIS_FE_HTTP_PORT=38030
export DORIS_BE_HTTP_PORT=38040
examples/doris-demos/scripts/start-doris.sh
```

The all-in-one image is multi-architecture. Use a native image for the host
architecture when possible, especially on Apple Silicon. Stop and remove the
demo container after use:

```bash
docker stop "${DORIS_CONTAINER_NAME:-dbt-doris-demo}"
docker rm "${DORIS_CONTAINER_NAME:-dbt-doris-demo}"
```

## Run the Jupyter Notebooks

Run all commands from the repository root.

### 1. Configure the Doris connection

The defaults connect to `root@127.0.0.1:9030` with an empty password. Override
them when your Doris FE uses different connection settings:

```bash
export DORIS_HOST="${DORIS_HOST:-127.0.0.1}"
export DORIS_PORT="${DORIS_PORT:-9030}"
export DORIS_USER="${DORIS_USER:-root}"
export DORIS_PASSWORD="${DORIS_PASSWORD:-}"
```

If you started the optional all-in-one image above, these variables keep its
mapped FE port (`29030`).

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
```

The setup script creates an isolated environment with Python 3.12.13, dbt Core
1.12.2, and `dbt-for-apache-doris` 1.1.0. Re-running it reuses a matching
environment. The Notebook and command-line runners use the executables in
this environment automatically. Set `DBT_BIN` only when using a custom dbt
installation.

### 3. Start JupyterLab

```bash
examples/doris-demos/scripts/start-notebook.sh
```

The default port is `18888`. Set `JUPYTER_PORT` before the command when that
port is already in use.

Keep this terminal running. JupyterLab prints a URL containing a token, for
example:

```text
http://127.0.0.1:18888/lab?token=...
```

Open that URL in a browser. JupyterLab opens the Demo directory, where
`README.md` remains visible alongside the `notebooks/` folder. Open
`notebooks/` and start with any of the five numbered files; each includes its
own environment check, fixture setup, dbt execution, Doris queries, and
verifier.

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
examples/doris-demos/scripts/run-all.sh
```

The runner verifies the exact dbt Core and adapter versions, waits for Doris,
runs the five demos serially, and writes environment information, per-demo logs,
and `summary.tsv` under `examples/doris-demos/artifacts/<run-id>/`.

To run one demo, invoke its script directly. For example:

```bash
examples/doris-demos/geographic/scripts/run.sh
```

The single-demo runners automatically use
`examples/doris-demos/.venv/bin/dbt`. Set `DBT_BIN` only to use a different
dbt installation; activating the virtual environment is not required.

Each demo directory contains:

- `scripts/setup.sql`: creates the small Doris fixture;
- `scripts/run.sh`: executes dbt and the verifier;
- `scripts/verify.sh`: checks the Doris relations and expected data;
- `models/`, `seeds/`, or `snapshots/`: the dbt project shown in its Notebook.
