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
rows=$("$MYSQL_BIN" -N -s -h "$DORIS_HOST" -P "$DORIS_PORT" -u "${DORIS_USER:-root}" -e "select source, ad_date, clicks, impressions, views, conversions from dbt_demo_consolidate.int__ads_unified order by source, ad_date")
test "$(printf '%s\n' "$rows" | wc -l)" -eq 6
test "$("$MYSQL_BIN" -N -s -h "$DORIS_HOST" -P "$DORIS_PORT" -u "${DORIS_USER:-root}" -e "select count(*) from dbt_demo_consolidate.int__ads_unified")" -eq 6
test "$("$MYSQL_BIN" -N -s -h "$DORIS_HOST" -P "$DORIS_PORT" -u "${DORIS_USER:-root}" -e "select count(*) from dbt_demo_consolidate.int__ads_unified where ad_date is null")" -eq 0
types=$("$MYSQL_BIN" -N -s -h "$DORIS_HOST" -P "$DORIS_PORT" -u "${DORIS_USER:-root}" -e "select table_name, table_type from information_schema.tables where table_schema='dbt_demo_consolidate' and table_name in ('stg__ads_googleads','stg__ads_metaads','stg__ads_tiktokads','int__ads_unified') order by table_name")
test "$types" = $'int__ads_unified\tBASE TABLE\nstg__ads_googleads\tVIEW\nstg__ads_metaads\tVIEW\nstg__ads_tiktokads\tVIEW'
printf '%s\n' "$rows"
