#!/usr/bin/env bash
set -euo pipefail
MYSQL_BIN="${MYSQL_BIN:-mysql}"
DORIS_HOST="${DORIS_HOST:-127.0.0.1}"
DORIS_PORT="${DORIS_PORT:-9030}"
if [[ -n ${DORIS_PASSWORD:-} ]]; then
  export MYSQL_PWD="$DORIS_PASSWORD"
else
  unset MYSQL_PWD
fi
args=(-N -s -h "$DORIS_HOST" -P "$DORIS_PORT" -u "${DORIS_USER:-root}")
test "$("$MYSQL_BIN" "${args[@]}" -e "select count(*) from dbt_demo_snapshot_history.customer_snapshot")" -eq 3
test "$("$MYSQL_BIN" "${args[@]}" -e "select count(*) from dbt_demo_snapshot_history.customer_snapshot where customer_id=1 and dbt_valid_to is not null")" -eq 1
test "$("$MYSQL_BIN" "${args[@]}" -e "select count(*) from dbt_demo_snapshot_history.customer_snapshot where customer_id=1 and dbt_valid_to is null")" -eq 1
test "$("$MYSQL_BIN" "${args[@]}" -e "select count(*) from dbt_demo_snapshot_history.customer_snapshot where customer_id=2 and dbt_valid_to is not null")" -eq 1
test "$("$MYSQL_BIN" "${args[@]}" -e "select count(*) from dbt_demo_snapshot_history.customer_snapshot where customer_id=2 and dbt_valid_to is null")" -eq 0
test "$("$MYSQL_BIN" "${args[@]}" -e "select count(*) from dbt_demo_snapshot.dim_customer_current")" -eq 1
test "$("$MYSQL_BIN" "${args[@]}" -e "select email from dbt_demo_snapshot.dim_customer_current where customer_id=1")" = "alice.new@example.com"
"$MYSQL_BIN" "${args[@]}" -e "select customer_id, email, dbt_valid_from, dbt_valid_to from dbt_demo_snapshot_history.customer_snapshot order by customer_id, dbt_valid_from"
