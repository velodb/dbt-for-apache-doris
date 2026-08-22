#!/usr/bin/env bash
set -euo pipefail
project_dir=$(cd "$(dirname "$0")/.." && pwd)
demo_dir=$(cd "$project_dir/.." && pwd)
cd "$project_dir"
DBT_BIN="${DBT_BIN:-$demo_dir/.venv/bin/dbt}"
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
"$DBT_BIN" run --project-dir . --profiles-dir . --select stg_customers
"$DBT_BIN" snapshot --project-dir . --profiles-dir . --select customer_snapshot --threads 1
"$DBT_BIN" run --project-dir . --profiles-dir . --select dim_customer_current
"$DBT_BIN" test --project-dir . --profiles-dir . --select dim_customer_current --threads 1
"${MYSQL[@]}" -e "update dbt_demo_snapshot_source.CUSTOMERS set email='alice.new@example.com', customer_type='INDIVIDUAL_PLUS' where customer_id=1; delete from dbt_demo_snapshot_source.CUSTOMERS where customer_id=2"
"$DBT_BIN" snapshot --project-dir . --profiles-dir . --select customer_snapshot --threads 1
"$DBT_BIN" run --project-dir . --profiles-dir . --select dim_customer_current
"$DBT_BIN" test --project-dir . --profiles-dir . --select dim_customer_current --threads 1
./scripts/verify.sh
