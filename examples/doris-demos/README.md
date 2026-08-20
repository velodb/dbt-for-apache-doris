# Doris dbt demos

These examples show five common dbt workflows running end to end on Apache Doris.
The Jupyter Notebook exposes every source, model, intermediate relation, data
change, test, and final result.

| Demo | What it demonstrates | Main dbt and Doris capabilities |
| --- | --- | --- |
| Daily order summary | Filter and aggregate orders by day and month | Source, Table, Data Test, partitioning, bucketing, Async MV |
| Customer geographic analysis | Join customer addresses and orders by state | Cross-database Source, View, Table, `ref()` |
| Advertising consolidation | Load and normalize three advertising CSV files | Seed, `dbt_utils`, `QUALIFY`, Data Test |
| Late-arriving orders | Insert a corrected order version and a new order | Incremental `merge`, Unique Key, idempotency |
| Customer Snapshot | Track an update and hard delete over time | Snapshot, SCD Type 2, current dimension |

Each demo recreates only its dedicated `dbt_demo_*` databases. Do not use those
database names for production data.

## Run the Jupyter Notebook

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

### 3. Start the Notebook

```bash
export JUPYTER_PORT=18888
examples/doris-demos/scripts/start-notebook.sh
```

Keep this terminal running. JupyterLab prints a URL containing a token, for
example:

```text
http://127.0.0.1:18888/lab?token=...
```

Open that URL in a browser. The server opens
[`dbt-for-apache-doris-demos.ipynb`](dbt-for-apache-doris-demos.ipynb).

### 4. Execute the demos

1. Run the first **Check the execution environment** cell. It loads the visual
   styles and verifies dbt and Doris.
2. Use **Run All** to execute all five demos, or run the numbered cells in order
   to inspect every transformation step.
3. Expand **View full run log** only when you need the compiled dbt details.

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
- `models/`, `seeds/`, or `snapshots/`: the dbt project shown in the Notebook.
