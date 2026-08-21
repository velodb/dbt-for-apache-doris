# Agent Instructions

This repository contains five runnable Apache Doris dbt demos under
`examples/doris-demos/`. When a user asks to run, verify, or demonstrate the
Doris dbt examples, follow this workflow. The default objective is to execute
the real dbt project against Doris and report the result; do not replace the
run with a static review.

## Demo Scope

The demo suite contains:

| User-facing demo | Capability | Runner |
| --- | --- | --- |
| Daily order summary | Source, table model, data tests, partitioning, bucketing, async MV | `examples/doris-demos/scripts/run-all.sh` or `examples/doris-daily-order-summary/scripts/run.sh` |
| Customer geographic analysis | Cross-database sources, staging views, `ref()` and table model | `examples/doris-demos/geographic/scripts/run.sh` |
| Advertising consolidation | Seeds, `dbt_utils`, `QUALIFY` and uniqueness test | `examples/doris-demos/consolidate/scripts/run.sh` |
| Late-arriving orders | Incremental model, `unique_key`, Doris Unique Key merge and idempotency | `examples/doris-demos/incremental/scripts/run.sh` |
| Customer snapshot | Snapshot, SCD Type 2, hard-delete handling and current dimension | `examples/doris-demos/snapshot/scripts/run.sh` |

`run-all.sh` is the default non-interactive acceptance path. It runs all five
demos serially, performs a Doris preflight, and writes durable logs and a
`summary.tsv` under `examples/doris-demos/artifacts/`.

## Environment Discovery

Run commands from the repository root. Resolve tools in this order:

1. Use `DBT_BIN` when the user explicitly set it.
2. Otherwise use `examples/doris-demos/.venv/bin/dbt`.
3. If that executable is missing, run:

   ```bash
   INSTALL_JUPYTER=1 examples/doris-demos/scripts/prepare-python-env.sh
   ```

   The script creates the pinned environment and installs dbt Core,
   `dbt-for-apache-doris`, and JupyterLab. Do not install packages globally.

The MySQL-compatible client is required for fixture setup and verification.
Check it with `command -v mysql` before running a demo.

Resolve Doris connection settings from the environment, with these defaults:

```text
DORIS_HOST=127.0.0.1
DORIS_PORT=9030
DORIS_USER=root
DORIS_PASSWORD=
```

Do not overwrite values the user already supplied. Verify the endpoint with:

```bash
mysql -h "$DORIS_HOST" -P "$DORIS_PORT" -u "$DORIS_USER" \
  -e 'select sum(number) from numbers("number"="10")'
```

The result must be `45`. The Doris user must be able to create, drop, and
modify the dedicated `dbt_demo_*` databases used by the examples.

## Starting Doris When Needed

If the configured endpoint is unavailable and Docker is available, use the
official all-in-one image as an isolated demo runtime. Do not stop or remove
an existing Doris container or cluster. Reuse a healthy demo container if one
already exists; otherwise choose an unused container name and host port.

The standard fallback is:

```bash
export DORIS_CONTAINER_NAME=dbt-doris-demo-agent
export DORIS_IMAGE=apache/doris:all-in-one-4.1.3
export DORIS_HOST=127.0.0.1
export DORIS_PORT=29030
export DORIS_USER=root
export DORIS_PASSWORD=

docker pull "$DORIS_IMAGE"
docker run -d --name "$DORIS_CONTAINER_NAME" \
  -p 29030:9030 \
  -p 28030:8030 \
  -p 28040:8040 \
  "$DORIS_IMAGE"

until [ "$(docker inspect -f '{{.State.Health.Status}}' "$DORIS_CONTAINER_NAME")" = healthy ]; do
  sleep 1
done
```

After startup, repeat the SQL preflight. Use a native image for the host
architecture when possible. If Docker is unavailable, image startup fails,
or no port is free, ask the user for a reachable Doris FE endpoint instead of
silently changing connection settings.

## Running Demos

For a request to run the demo suite, execute:

```bash
DBT_BIN="${DBT_BIN:-$PWD/examples/doris-demos/.venv/bin/dbt}" \
  examples/doris-demos/scripts/run-all.sh
```

The script already reads `DORIS_HOST`, `DORIS_PORT`, `DORIS_USER`, and
`DORIS_PASSWORD`; do not duplicate connection flags in each demo command.
Wait for the command to finish and report the final `summary.tsv` status and
the artifact directory. A successful run must say `All five demos passed` and
contain five `passed` rows.

For a single demo, use the matching runner from the table above. The daily
summary project is kept in the sibling directory
`examples/doris-daily-order-summary/`; `run-all.sh` handles that path for the
user-facing suite. Run a single demo only when the user requests it or when
isolating a failure.

For an interactive walkthrough, start JupyterLab after the environment is
ready:

```bash
examples/doris-demos/scripts/start-notebook.sh
```

The launcher automatically uses the project venv and opens the five numbered
notebooks. Keep the server process alive, return its URL/token to the user,
and do not claim that a notebook was executed unless its cells actually ran.

## Failure Handling

- Preserve the artifact directory and per-demo log when anything fails.
- Report the first failed demo, its exit status, and the log path.
- Retry only after identifying a transient cause such as Doris startup; do not
  repeatedly rerun a failing dbt model without inspecting its log.
- A successful `dbt debug` is only a connection check. The demo is successful
  only when its dbt build/run/snapshot steps and verifier finish successfully.
- Do not claim that the full upstream benchmark or unrelated tasks were run;
  this workflow covers the five product demos only.
- Do not modify demo SQL, profiles, fixtures, or adapter code while running.
  Make code changes only when the user explicitly asks for a fix.

## Cleanup

Do not delete demo databases during troubleshooting because the scripts manage
their own dedicated databases. If the Agent started the fallback Docker
container, report its name and leave it running unless the user asks for
cleanup. Cleanup is:

```bash
docker stop "$DORIS_CONTAINER_NAME"
docker rm "$DORIS_CONTAINER_NAME"
```
