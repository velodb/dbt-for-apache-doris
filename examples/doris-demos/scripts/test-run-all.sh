#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
source_runner=$script_dir/run-all.sh
source_prepare=$script_dir/prepare-python-env.sh
test_root=$(mktemp -d)
trap 'rm -rf "$test_root"' EXIT

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

make_fixture() {
  local fixture_root=$1
  local daily_runner=$fixture_root/doris-daily-order-summary/scripts/run.sh
  local suite_root=$fixture_root/doris-demos

  mkdir -p "$(dirname "$daily_runner")" "$suite_root/scripts"
  cp "$source_runner" "$suite_root/scripts/run-all.sh"
  chmod +x "$suite_root/scripts/run-all.sh"

  for name in doris-customer-geographic-analysis doris-advertising-consolidation doris-late-arriving-orders doris-customer-snapshot; do
    mkdir -p "$fixture_root/$name/scripts"
    cat >"$fixture_root/$name/scripts/run.sh" <<'RUNNER'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$(basename "$(dirname "$(dirname "$0")")")" >>"$DEMO_MARKER"
RUNNER
    chmod +x "$fixture_root/$name/scripts/run.sh"
  done

  cat >"$daily_runner" <<'RUNNER'
#!/usr/bin/env bash
set -euo pipefail
printf 'daily-order-summary\n' >>"$DEMO_MARKER"
if [[ ${FAIL_DAILY:-0} == 1 ]]; then
  exit 7
fi
RUNNER
  chmod +x "$daily_runner"

  mkdir -p "$fixture_root/bin"
  cat >"$fixture_root/bin/dbt" <<'DBT'
#!/usr/bin/env bash
cat <<EOF
Core:
  - installed: ${MOCK_DBT_CORE:-1.12.2}
Plugins:
  - doris: ${MOCK_DORIS_ADAPTER:-1.1.0} - Up to date!
EOF
DBT
  cat >"$fixture_root/bin/mysql" <<'MYSQL'
#!/usr/bin/env bash
if [[ $* == *--password* || $* == *" -p"* ]]; then
  echo "password leaked through argv" >&2
  exit 91
fi
if [[ ${EXPECT_MYSQL_PWD_UNSET:-0} == 1 && -n ${MYSQL_PWD+x} ]]; then
  echo "stale MYSQL_PWD was not cleared" >&2
  exit 92
fi
if [[ ${EXPECT_MYSQL_PWD_UNSET:-0} != 1 && ${MYSQL_PWD:-} != secret ]]; then
  echo "password was not passed through MYSQL_PWD" >&2
  exit 92
fi
if [[ ${MYSQL_FAIL_COUNT:-0} -gt 0 ]]; then
  attempts_file=${MYSQL_ATTEMPTS_FILE:?MYSQL_ATTEMPTS_FILE is required when MYSQL_FAIL_COUNT is set}
  attempts=0
  if [[ -f $attempts_file ]]; then
    attempts=$(<"$attempts_file")
  fi
  attempts=$((attempts + 1))
  printf '%s\n' "$attempts" >"$attempts_file"
  if ((attempts <= MYSQL_FAIL_COUNT)); then
    echo "temporary Doris readiness failure" >&2
    exit 93
  fi
fi
echo "mysql: [Warning] Using a password on the command line interface can be insecure." >&2
printf '45\n'
MYSQL
  chmod +x "$fixture_root/bin/dbt" "$fixture_root/bin/mysql"
}

credential_scripts=(
  "$script_dir/../../doris-daily-order-summary/scripts/run.sh"
  "$script_dir/../../doris-daily-order-summary/scripts/verify.sh"
  "$script_dir/../../doris-customer-geographic-analysis/scripts/run.sh"
  "$script_dir/../../doris-customer-geographic-analysis/scripts/verify.sh"
  "$script_dir/../../doris-advertising-consolidation/scripts/run.sh"
  "$script_dir/../../doris-advertising-consolidation/scripts/verify.sh"
  "$script_dir/../../doris-late-arriving-orders/scripts/run.sh"
  "$script_dir/../../doris-late-arriving-orders/scripts/verify.sh"
  "$script_dir/../../doris-customer-snapshot/scripts/run.sh"
  "$script_dir/../../doris-customer-snapshot/scripts/verify.sh"
)
for credential_script in "${credential_scripts[@]}"; do
  grep -Fq 'export MYSQL_PWD="$DORIS_PASSWORD"' "$credential_script" || \
    fail "MYSQL_PWD is not exported by $credential_script"
  grep -Fq 'unset MYSQL_PWD' "$credential_script" || \
    fail "empty MYSQL_PWD is not cleared by $credential_script"
  if grep -Eq -- '--password=|-p\$' "$credential_script"; then
    fail "password is present in MySQL arguments in $credential_script"
  fi
done

run_fixture() {
  local fixture_root=$1
  local test_password=${TEST_DORIS_PASSWORD-secret}
  shift
  DEMO_MARKER=$fixture_root/marker \
  DBT_BIN=$fixture_root/bin/dbt \
  MYSQL_BIN=$fixture_root/bin/mysql \
  DORIS_PASSWORD=$test_password \
  EXPECT_MYSQL_PWD_UNSET=${EXPECT_MYSQL_PWD_UNSET:-0} \
  DEMO_RESULTS_DIR=$fixture_root/results \
    "$fixture_root/doris-demos/scripts/run-all.sh" "$@"
}

fixture=$test_root/success
make_fixture "$fixture"
run_fixture "$fixture" >/dev/null
[[ $(wc -l <"$fixture/marker") == 5 ]] || fail "success run did not execute five demos"
[[ $(awk -F '\t' 'NR > 1 && $2 == "passed" { count++ } END { print count + 0 }' "$fixture/results/summary.tsv") == 5 ]] || \
  fail "success summary does not contain five passed demos"
grep -Fq 'Using a password' "$fixture/results/environment.txt" || \
  fail "preflight stderr was not preserved"

fixture=$test_root/stale-password
make_fixture "$fixture"
MYSQL_PWD=stale \
TEST_DORIS_PASSWORD= \
EXPECT_MYSQL_PWD_UNSET=1 \
  run_fixture "$fixture" >/dev/null
[[ $(wc -l <"$fixture/marker") == 5 ]] || fail "password cleanup run did not execute five demos"

fixture=$test_root/retry
make_fixture "$fixture"
MYSQL_FAIL_COUNT=2 \
MYSQL_ATTEMPTS_FILE=$fixture/mysql-attempts \
DORIS_READY_TIMEOUT_SECONDS=5 \
DORIS_READY_INTERVAL_SECONDS=0 \
  run_fixture "$fixture" >/dev/null
[[ $(<"$fixture/mysql-attempts") == 3 ]] || fail "preflight did not retry until success"

fixture=$test_root/continue
make_fixture "$fixture"
set +e
FAIL_DAILY=1 run_fixture "$fixture" >/dev/null 2>&1
status=$?
set -e
[[ $status == 7 ]] || fail "expected first runner status 7, got $status"
[[ $(wc -l <"$fixture/marker") == 5 ]] || fail "failure stopped later demos"
grep -Fq $'daily-order-summary\tfailed(runner=7,tee=0)' "$fixture/results/summary.tsv" || \
  fail "runner failure was not recorded"

fixture=$test_root/tee-failure
make_fixture "$fixture"
mkdir -p "$fixture/fake-bin"
cat >"$fixture/fake-bin/tee" <<'TEE'
#!/usr/bin/env bash
cat >/dev/null
exit 9
TEE
chmod +x "$fixture/fake-bin/tee"
set +e
PATH="$fixture/fake-bin:$PATH" run_fixture "$fixture" >/dev/null 2>&1
status=$?
set -e
[[ $status == 9 ]] || fail "expected tee status 9, got $status"
[[ $(wc -l <"$fixture/marker") == 5 ]] || fail "tee failure stopped later demos"
[[ $(grep -Fc 'failed(runner=0,tee=9)' "$fixture/results/summary.tsv") == 5 ]] || \
  fail "tee failures were not recorded"

fixture=$test_root/core-version
make_fixture "$fixture"
set +e
MOCK_DBT_CORE=1.12.20 run_fixture "$fixture" --dry-run >/dev/null 2>&1
status=$?
set -e
[[ $status == 1 ]] || fail "inexact dbt Core version was accepted"

fixture=$test_root/adapter-version
make_fixture "$fixture"
set +e
MOCK_DORIS_ADAPTER=1.1.0.post1 run_fixture "$fixture" --dry-run >/dev/null 2>&1
status=$?
set -e
[[ $status == 1 ]] || fail "inexact adapter version was accepted"

fake_uv=$test_root/fake-uv
cat >"$fake_uv" <<'UV'
#!/usr/bin/env bash
exit 99
UV
chmod +x "$fake_uv"

repair_venv=$test_root/repair-venv
mkdir -p "$repair_venv"
touch "$repair_venv/pyvenv.cfg"
repair_uv=$test_root/repair-uv
repair_args=$test_root/repair-uv-args
cat >"$repair_uv" <<'UV'
#!/usr/bin/env bash
printf '%s\n' "$@" >"$REPAIR_UV_ARGS"
exit 77
UV
chmod +x "$repair_uv"
set +e
REPAIR_UV_ARGS=$repair_args \
DBT_DEMO_VENV=$repair_venv \
UV_BIN=$repair_uv \
  "$source_prepare" >/dev/null 2>&1
status=$?
set -e
[[ $status == 77 ]] || fail "repair venv did not invoke uv"
grep -Fxq -- '--clear' "$repair_args" || fail "repair venv did not pass --clear"

set +e
DBT_DEMO_PYTHON_VERSION=3.12 UV_BIN=$fake_uv "$source_prepare" >/dev/null 2>&1
status=$?
set -e
[[ $status == 2 ]] || fail "non-exact Python selector was not rejected before uv"

echo "run-all.sh tests passed"
