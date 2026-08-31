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
test "$("$MYSQL_BIN" "${args[@]}" -e "select count(*) from dbt_demo_incremental.incremental_daily_sales")" -eq 4
test "$("$MYSQL_BIN" "${args[@]}" -e "select count(distinct order_id) from dbt_demo_incremental.incremental_daily_sales")" -eq 4
test "$("$MYSQL_BIN" "${args[@]}" -e "select grand_total from dbt_demo_incremental.incremental_daily_sales where order_id=101")" = "125.00"
test "$("$MYSQL_BIN" "${args[@]}" -e "select total_revenue from dbt_demo_incremental.daily_sales_summary where order_date='2026-08-01'")" = "245.00"
ddl=$("$MYSQL_BIN" "${args[@]}" -e "show create table dbt_demo_incremental.incremental_daily_sales")
grep -q 'UNIQUE KEY(`order_id`)' <<<"$ddl"
grep -q 'DISTRIBUTED BY HASH(`order_id`) BUCKETS 1' <<<"$ddl"
"$MYSQL_BIN" "${args[@]}" -e "select order_id, order_date, grand_total, version_num from dbt_demo_incremental.incremental_daily_sales order by order_id"
