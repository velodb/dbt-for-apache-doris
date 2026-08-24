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

Run only one demo process at a time. The fixtures use fixed `dbt_demo_*`
database names and recreate them at the beginning of each run.

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

Do not overwrite values the user already supplied. Verify the endpoint without
putting the password in process arguments or inheriting an unrelated
`MYSQL_PWD` value:

```bash
if [[ -n ${DORIS_PASSWORD:-} ]]; then
  MYSQL_PWD="$DORIS_PASSWORD" mysql \
    -h "$DORIS_HOST" -P "$DORIS_PORT" -u "$DORIS_USER" \
    -e 'select sum(number) from numbers("number"="10")'
else
  (unset MYSQL_PWD; mysql \
    -h "$DORIS_HOST" -P "$DORIS_PORT" -u "$DORIS_USER" \
    -e 'select sum(number) from numbers("number"="10")')
fi
```

The result must be `45`. The Doris user must be able to create, drop, and
modify the dedicated `dbt_demo_*` databases used by the examples.

## Starting Doris When Needed

If the configured endpoint is unavailable and Docker is available, use the
official all-in-one image as an isolated demo runtime. Use the checked launcher
so every port stays bound to `127.0.0.1`, container conflicts fail without
deletion, and health waiting has a timeout with diagnostics.

The standard fallback is:

```bash
export DORIS_HOST=127.0.0.1
export DORIS_PORT="${DORIS_PORT:-29030}"
export DORIS_CONTAINER_NAME="${DORIS_CONTAINER_NAME:-dbt-doris-demo-agent}"
examples/doris-demos/scripts/start-doris.sh
```

The launcher accepts `DORIS_IMAGE`, `DORIS_PORT`, `DORIS_FE_HTTP_PORT`, and
`DORIS_BE_HTTP_PORT` overrides. It reuses an existing container only when it
is healthy and its image and port bindings match exactly. On a conflict,
choose an unused container name and set of ports; never stop or remove the
existing container. After startup, repeat the SQL preflight. Use a native
image for the host architecture when possible. If Docker is unavailable,
image startup fails, or no port is free, ask the user for a reachable Doris
FE endpoint instead of silently changing connection settings.

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

The launcher automatically uses the project venv and exposes `README.md` plus
the `notebooks/` folder from the Demo directory. Keep the server process alive,
return its URL/token to the user, and do not claim that a notebook was executed
unless its cells actually ran.

## Failure Handling

- Preserve the artifact directory and per-demo log when anything fails.
- Report the first failed demo, its exit status, and the log path.
- Retry only after identifying a transient cause such as Doris startup; do not
  repeatedly rerun a failing dbt model without inspecting its log.
- A successful `dbt debug` is only a connection check. The demo is successful
  only when its dbt build/run/snapshot steps and verifier finish successfully.
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
