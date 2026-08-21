#!/usr/bin/env bash
set -euo pipefail

demo_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
mysql_bin=${MYSQL_BIN:-mysql}
doris_host=${DORIS_HOST:-127.0.0.1}
doris_port=${DORIS_PORT:-9030}
doris_user=${DORIS_USER:-root}
if [[ -n ${DORIS_PASSWORD:-} ]]; then
  export MYSQL_PWD="$DORIS_PASSWORD"
else
  unset MYSQL_PWD
fi

mysql_args=(-h "$doris_host" -P "$doris_port" -u "$doris_user")

actual=$(
  "$mysql_bin" "${mysql_args[@]}" -Nse "
    SELECT concat_ws('|', cast(order_date AS string), cast(order_count AS string), cast(total_revenue AS string))
    FROM dbt_demo_daily.daily_order_summary
    ORDER BY order_date
  "
)
expected=$'2026-08-01|1|100.00\n2026-08-02|1|80.00\n2026-08-03|1|40.20'
[[ "$actual" == "$expected" ]]

table_ddl=$("$mysql_bin" "${mysql_args[@]}" -Nse "SHOW CREATE TABLE dbt_demo_daily.daily_order_summary" | cut -f2-)
[[ "$table_ddl" == *'DUPLICATE KEY(`order_date`)'* ]]
[[ "$table_ddl" == *'PARTITION BY RANGE(`order_date`)'* ]]
[[ "$table_ddl" == *'DISTRIBUTED BY HASH(`order_date`) BUCKETS 1'* ]]

mv_status=
for ((attempt = 0; attempt < 60; attempt++)); do
  mv_status=$("$mysql_bin" "${mysql_args[@]}" -Nse "SELECT Status FROM tasks('type'='mv') WHERE MvDatabaseName = 'dbt_demo_daily' AND MvName = 'monthly_order_summary_mv' ORDER BY CreateTime DESC LIMIT 1")
  if [[ "$mv_status" == 'SUCCESS' ]]; then
    break
  fi
  if [[ "$mv_status" == 'FAILED' || "$mv_status" == 'CANCELLED' ]]; then
    printf 'MV refresh ended with status: %s\n' "$mv_status" >&2
    exit 1
  fi
  sleep 1
done
[[ "$mv_status" == 'SUCCESS' ]]

mv_rows=$("$mysql_bin" "${mysql_args[@]}" -Nse "SELECT concat_ws('|', cast(order_month AS string), cast(order_count AS string), cast(total_revenue AS string)) FROM dbt_demo_daily.monthly_order_summary_mv")
[[ "$mv_rows" == '2026-08-01|3|220.20' ]]

printf '%s\n' 'Demo verification passed.'
