#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
DBT_BIN="${DBT_BIN:-dbt}"
MYSQL_BIN="${MYSQL_BIN:-mysql}"
DORIS_HOST="${DORIS_HOST:-127.0.0.1}"
DORIS_PORT="${DORIS_PORT:-9030}"
if [[ -n ${DORIS_PASSWORD:-} ]]; then
  export MYSQL_PWD="$DORIS_PASSWORD"
else
  unset MYSQL_PWD
fi
MYSQL=("$MYSQL_BIN" -h "$DORIS_HOST" -P "$DORIS_PORT" -u "${DORIS_USER:-root}")
"${MYSQL[@]}" < scripts/setup.sql
"$DBT_BIN" debug --project-dir . --profiles-dir .
"$DBT_BIN" build --project-dir . --profiles-dir .
"${MYSQL[@]}" -e "insert into dbt_demo_incremental_source.ORDERS values (4,101,1,'web',125.00,'COMPLETED','2026-08-01 09:00:00','2026-08-05 09:00:00'), (5,104,3,'mobile',70.00,'COMPLETED','2026-08-01 12:00:00','2026-08-05 10:00:00')"
"$DBT_BIN" build --project-dir . --profiles-dir .
"$DBT_BIN" build --project-dir . --profiles-dir .
./scripts/verify.sh
