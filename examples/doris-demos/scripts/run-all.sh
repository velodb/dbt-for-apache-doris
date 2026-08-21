#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
demo_dir=$(cd "$script_dir/.." && pwd)
daily_demo_dir=$(cd "$demo_dir/../doris-daily-order-summary" && pwd)

dbt_bin=${DBT_BIN:-$demo_dir/.venv/bin/dbt}
mysql_bin=${MYSQL_BIN:-mysql}
doris_host=${DORIS_HOST:-127.0.0.1}
doris_port=${DORIS_PORT:-9030}
doris_user=${DORIS_USER:-root}
doris_password=${DORIS_PASSWORD:-}
expected_dbt_core=${EXPECTED_DBT_CORE_VERSION:-1.12.2}
expected_adapter=${EXPECTED_DORIS_ADAPTER_VERSION:-1.1.0}
ready_timeout_seconds=${DORIS_READY_TIMEOUT_SECONDS:-180}
ready_interval_seconds=${DORIS_READY_INTERVAL_SECONDS:-2}
run_id=$(date -u +%Y%m%dT%H%M%SZ)-$$
results_dir=${DEMO_RESULTS_DIR:-$demo_dir/artifacts/$run_id}
dry_run=0

usage() {
  cat <<EOF
Usage: $(basename "$0") [--dry-run]

Run all five Doris dbt demos serially and keep durable logs.

Environment overrides:
  DBT_BIN              dbt executable (default: $demo_dir/.venv/bin/dbt)
  MYSQL_BIN            MySQL client (default: mysql)
  DORIS_HOST           Doris FE host (default: 127.0.0.1)
  DORIS_PORT           Doris FE query port (default: 9030)
  DORIS_USER           Doris user (default: root)
  DORIS_PASSWORD       Doris password (default: empty)
  DORIS_READY_TIMEOUT_SECONDS  FE/BE readiness timeout (default: 180)
  DORIS_READY_INTERVAL_SECONDS readiness retry interval (default: 2)
  DEMO_RESULTS_DIR     output directory (default: $demo_dir/artifacts/<run-id>)

Each demo recreates only its dedicated dbt_demo_* databases.
EOF
}

while (($#)); do
  case "$1" in
    --dry-run)
      dry_run=1
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

demos=(
  "daily-order-summary:$daily_demo_dir/scripts/run.sh"
  "geographic:$demo_dir/geographic/scripts/run.sh"
  "consolidate:$demo_dir/consolidate/scripts/run.sh"
  "incremental:$demo_dir/incremental/scripts/run.sh"
  "snapshot:$demo_dir/snapshot/scripts/run.sh"
)

resolved_dbt_bin=$(command -v "$dbt_bin" 2>/dev/null || true)
if [[ -z $resolved_dbt_bin || ! -x $resolved_dbt_bin ]]; then
  echo "dbt executable not found: $dbt_bin" >&2
  echo "run $script_dir/prepare-python-env.sh first" >&2
  exit 1
fi
dbt_bin=$(cd "$(dirname "$resolved_dbt_bin")" && pwd -P)/$(basename "$resolved_dbt_bin")
resolved_mysql_bin=$(command -v "$mysql_bin" 2>/dev/null || true)
if [[ -z $resolved_mysql_bin || ! -x $resolved_mysql_bin ]]; then
  echo "MySQL client not found: $mysql_bin" >&2
  exit 1
fi
mysql_bin=$(cd "$(dirname "$resolved_mysql_bin")" && pwd -P)/$(basename "$resolved_mysql_bin")
for entry in "${demos[@]}"; do
  runner=${entry#*:}
  if [[ ! -x $runner ]]; then
    echo "demo runner is not executable: $runner" >&2
    exit 1
  fi
done

version_output=$("$dbt_bin" --version)
actual_dbt_core=$(
  awk '
    /^Core:/ { in_core = 1; next }
    in_core && /installed:/ {
      sub(/^.*installed:[[:space:]]*/, "")
      sub(/[[:space:]].*$/, "")
      print
      exit
    }
  ' <<<"$version_output"
)
actual_adapter=$(
  awk '
    /^[[:space:]]*-[[:space:]]+doris:/ {
      sub(/^.*doris:[[:space:]]*/, "")
      sub(/[[:space:]].*$/, "")
      print
      exit
    }
  ' <<<"$version_output"
)
if [[ -z $actual_adapter ]]; then
  dbt_python_bin=$(cd "$(dirname "$dbt_bin")" && pwd -P)/python
  if [[ -x $dbt_python_bin ]]; then
    actual_adapter=$(
      "$dbt_python_bin" - <<'PY'
from importlib.metadata import PackageNotFoundError, version

try:
    print(version("dbt-for-apache-doris"))
except PackageNotFoundError:
    pass
PY
    )
  fi
fi
if [[ $actual_dbt_core != "$expected_dbt_core" ]]; then
  echo "expected dbt Core $expected_dbt_core" >&2
  printf '%s\n' "$version_output" >&2
  exit 1
fi
if [[ $actual_adapter != "$expected_adapter" ]]; then
  echo "expected Doris adapter $expected_adapter" >&2
  printf '%s\n' "$version_output" >&2
  exit 1
fi

if ((dry_run)); then
  printf 'Python/dbt environment:\n%s\n\n' "$version_output"
  printf 'Doris endpoint: %s:%s\n' "$doris_host" "$doris_port"
  printf 'Results directory: %s\n' "$results_dir"
  printf 'Demo commands:\n'
  for entry in "${demos[@]}"; do
    printf '  %-24s %s\n' "${entry%%:*}" "${entry#*:}"
  done
  exit 0
fi

mkdir -p "$results_dir"
summary_file=$results_dir/summary.tsv
environment_file=$results_dir/environment.txt
printf 'demo\tstatus\tduration_seconds\tlog\n' >"$summary_file"

mysql_args=(-h "$doris_host" -P "$doris_port" -u "$doris_user")
preflight_stdout=$results_dir/preflight.stdout
preflight_stderr=$results_dir/preflight.stderr
if [[ -z $doris_password ]]; then
  unset MYSQL_PWD
fi

ready_deadline=$((SECONDS + ready_timeout_seconds))
backend_status=1
backend_check=
backend_error=
while ((SECONDS <= ready_deadline)); do
  : >"$preflight_stdout"
  : >"$preflight_stderr"
  set +e
  if [[ -n $doris_password ]]; then
    MYSQL_PWD="$doris_password" "$mysql_bin" "${mysql_args[@]}" -Nse \
      'SELECT SUM(number) FROM numbers("number"="10")' \
      >"$preflight_stdout" 2>"$preflight_stderr"
  else
    "$mysql_bin" "${mysql_args[@]}" -Nse \
      'SELECT SUM(number) FROM numbers("number"="10")' \
      >"$preflight_stdout" 2>"$preflight_stderr"
  fi
  backend_status=$?
  set -e
  backend_check=$(<"$preflight_stdout")
  backend_error=$(<"$preflight_stderr")
  if ((backend_status == 0)) && [[ $backend_check == 45 ]]; then
    break
  fi
  sleep "$ready_interval_seconds"
done

{
  printf 'started_at_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'doris_endpoint=%s:%s\n' "$doris_host" "$doris_port"
  printf 'dbt_bin=%s\n' "$dbt_bin"
  printf '\n%s\n' "$version_output"
  printf '\nDoris preflight stdout:\n%s\n' "$backend_check"
  if [[ -n $backend_error ]]; then
    printf '\nDoris preflight stderr:\n%s\n' "$backend_error"
  fi
} >"$environment_file"

if ((backend_status != 0)); then
  printf 'preflight\tfailed(%s)\t0\t%s\n' \
    "$backend_status" "$environment_file" >>"$summary_file"
  echo "Doris connection preflight failed (exit $backend_status)" >&2
  echo "results: $results_dir" >&2
  exit "$backend_status"
fi
if [[ $backend_check != 45 ]]; then
  printf 'preflight\tfailed(be-query)\t0\t%s\n' "$environment_file" >>"$summary_file"
  echo "Doris BE execution check failed; expected 45" >&2
  echo "results: $results_dir" >&2
  exit 1
fi

export DBT_BIN="$dbt_bin"
export MYSQL_BIN="$mysql_bin"
export DORIS_HOST="$doris_host"
export DORIS_PORT="$doris_port"
export DORIS_USER="$doris_user"
if [[ -n $doris_password ]]; then
  export DORIS_PASSWORD="$doris_password"
else
  unset DORIS_PASSWORD MYSQL_PWD
fi

overall_status=0
for entry in "${demos[@]}"; do
  name=${entry%%:*}
  runner=${entry#*:}
  log_file=$results_dir/$name.log
  started=$(date +%s)

  printf '\n=== %s ===\n' "$name"
  set +e
  "$runner" 2>&1 | tee "$log_file"
  pipeline_status=("${PIPESTATUS[@]}")
  set -e
  runner_status=${pipeline_status[0]}
  tee_status=${pipeline_status[1]}

  finished=$(date +%s)
  duration=$((finished - started))
  if ((runner_status == 0 && tee_status == 0)); then
    printf '%s\tpassed\t%s\t%s\n' "$name" "$duration" "$log_file" >>"$summary_file"
  else
    printf '%s\tfailed(runner=%s,tee=%s)\t%s\t%s\n' \
      "$name" "$runner_status" "$tee_status" "$duration" "$log_file" >>"$summary_file"
    echo "demo failed: $name (runner=$runner_status, tee=$tee_status)" >&2
    if ((overall_status == 0)); then
      if ((runner_status != 0)); then
        overall_status=$runner_status
      else
        overall_status=$tee_status
      fi
    fi
  fi
done

if ((overall_status == 0)); then
  printf '\nAll five demos passed.\nResults: %s\n' "$results_dir"
else
  printf '\nOne or more demos failed.\nResults: %s\n' "$results_dir" >&2
fi
column -t -s $'\t' "$summary_file" 2>/dev/null || cat "$summary_file"
exit "$overall_status"
