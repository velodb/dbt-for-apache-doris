#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
source_runner=$script_dir/start-doris.sh
test_root=$(mktemp -d)
trap 'rm -rf "$test_root"' EXIT

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

mkdir -p "$test_root/bin"
apply_mock=$test_root/bin/docker
cat >"$apply_mock" <<'DOCKER'
#!/usr/bin/env bash
set -u

printf '%s' "$1" >>"$MOCK_DOCKER_LOG"
for arg in "${@:2}"; do
  printf '\t%s' "$arg" >>"$MOCK_DOCKER_LOG"
done
printf '\n' >>"$MOCK_DOCKER_LOG"

command_name=$1
shift
case "$command_name" in
  info)
    exit 0
    ;;
  ps)
    case "$MOCK_DOCKER_MODE" in
      reuse|mismatch|existing-unhealthy) printf '%s\n' "${DORIS_CONTAINER_NAME:-dbt-doris-demo}" ;;
    esac
    ;;
  pull)
    printf 'pulled %s\n' "$1"
    ;;
  run)
    if [[ $MOCK_DOCKER_MODE == run-failure ]]; then
      echo "mock port conflict" >&2
      exit 17
    fi
    printf 'mock-container-id\n'
    ;;
  inspect)
    attempts=0
    if [[ -f $MOCK_INSPECT_COUNT ]]; then
      attempts=$(<"$MOCK_INSPECT_COUNT")
    fi
    attempts=$((attempts + 1))
    printf '%s\n' "$attempts" >"$MOCK_INSPECT_COUNT"
    image=${DORIS_IMAGE:-apache/doris:all-in-one-4.1.3}
    fe_port=${DORIS_PORT:-29030}
    fe_http_port=${DORIS_FE_HTTP_PORT:-28030}
    be_http_port=${DORIS_BE_HTTP_PORT:-28040}
    case "$MOCK_DOCKER_MODE" in
      new-success)
        if ((attempts == 1)); then
          health=starting
        else
          health=healthy
        fi
        printf 'running|%s|%s|127.0.0.1:%s|127.0.0.1:%s|127.0.0.1:%s\n' \
          "$health" "$image" "$fe_port" "$fe_http_port" "$be_http_port"
        ;;
      reuse)
        printf 'running|healthy|%s|127.0.0.1:%s|127.0.0.1:%s|127.0.0.1:%s\n' \
          "$image" "$fe_port" "$fe_http_port" "$be_http_port"
        ;;
      mismatch)
        printf 'running|healthy|%s|0.0.0.0:%s|0.0.0.0:%s|0.0.0.0:%s\n' \
          "$image" "$fe_port" "$fe_http_port" "$be_http_port"
        ;;
      existing-unhealthy|new-unhealthy)
        printf 'running|unhealthy|%s|127.0.0.1:%s|127.0.0.1:%s|127.0.0.1:%s\n' \
          "$image" "$fe_port" "$fe_http_port" "$be_http_port"
        ;;
      new-exited)
        printf 'exited|starting|%s|127.0.0.1:%s|127.0.0.1:%s|127.0.0.1:%s\n' \
          "$image" "$fe_port" "$fe_http_port" "$be_http_port"
        ;;
      timeout)
        printf 'running|starting|%s|127.0.0.1:%s|127.0.0.1:%s|127.0.0.1:%s\n' \
          "$image" "$fe_port" "$fe_http_port" "$be_http_port"
        ;;
      inspect-failure)
        echo "mock inspect error" >&2
        exit 18
        ;;
      *)
        echo "unknown mock mode: $MOCK_DOCKER_MODE" >&2
        exit 19
        ;;
    esac
    ;;
  logs)
    echo "mock Doris logs" >&2
    ;;
  *)
    echo "unexpected docker command: $command_name" >&2
    exit 20
    ;;
esac
DOCKER
chmod +x "$apply_mock"

run_mock() {
  local mode=$1
  local case_dir=$test_root/$mode
  shift
  mkdir -p "$case_dir"
  : >"$case_dir/docker.log"
  PATH="$test_root/bin:$PATH" \
  MOCK_DOCKER_MODE="$mode" \
  MOCK_DOCKER_LOG="$case_dir/docker.log" \
  MOCK_INSPECT_COUNT="$case_dir/inspect-count" \
  DORIS_READY_INTERVAL_SECONDS=0 \
    "$@" "$source_runner" >"$case_dir/stdout" 2>"$case_dir/stderr"
}

bash -n "$source_runner" || fail "start-doris.sh does not parse"

run_mock new-success env \
  DORIS_CONTAINER_NAME=custom-doris \
  DORIS_IMAGE=example/doris:test \
  DORIS_PORT=39030 \
  DORIS_FE_HTTP_PORT=38030 \
  DORIS_BE_HTTP_PORT=38040
new_log=$test_root/new-success/docker.log
grep -Fq $'run\t-d\t--name\tcustom-doris\t-p\t127.0.0.1:39030:9030\t-p\t127.0.0.1:38030:8030\t-p\t127.0.0.1:38040:8040\texample/doris:test' "$new_log" || \
  fail "new container did not use the requested loopback bindings"
grep -Fq $'pull\texample/doris:test' "$new_log" || fail "requested image was not pulled"
grep -Fq 'Doris is healthy.' "$test_root/new-success/stdout" || fail "success was not reported"
[[ $(<"$test_root/new-success/inspect-count") == 2 ]] || fail "health was not polled until ready"

run_mock reuse env
reuse_log=$test_root/reuse/docker.log
grep -Fq 'Reusing healthy Doris container' "$test_root/reuse/stdout" || fail "healthy container was not reused"
if grep -Eq '^(pull|run|stop|rm)([[:space:]]|$)' "$reuse_log"; then
  fail "reusing a container performed a mutating Docker command"
fi

set +e
run_mock mismatch env
status=$?
set -e
[[ $status == 1 ]] || fail "mismatched container should fail with status 1"
grep -Fq 'already exists but cannot be reused' "$test_root/mismatch/stderr" || \
  fail "mismatched container did not produce a clear error"
grep -Fq 'left unchanged' "$test_root/mismatch/stderr" || fail "mismatch did not promise preservation"
if grep -Eq '^(pull|run|stop|rm)([[:space:]]|$)' "$test_root/mismatch/docker.log"; then
  fail "mismatch modified the existing container"
fi

set +e
run_mock existing-unhealthy env
status=$?
set -e
[[ $status == 1 ]] || fail "existing unhealthy container should fail with status 1"
grep -Eq '^logs[[:space:]]' "$test_root/existing-unhealthy/docker.log" || \
  fail "existing unhealthy container logs were not requested"
if grep -Eq '^(pull|run|stop|rm)([[:space:]]|$)' "$test_root/existing-unhealthy/docker.log"; then
  fail "existing unhealthy container was modified"
fi

set +e
run_mock new-unhealthy env
status=$?
set -e
[[ $status == 1 ]] || fail "unhealthy container should fail with status 1"
grep -Fq 'is unhealthy' "$test_root/new-unhealthy/stderr" || fail "unhealthy state was not reported"
grep -Eq '^logs[[:space:]]' "$test_root/new-unhealthy/docker.log" || fail "unhealthy logs were not requested"

set +e
run_mock new-exited env
status=$?
set -e
[[ $status == 1 ]] || fail "exited container should fail with status 1"
grep -Fq 'entered state exited' "$test_root/new-exited/stderr" || fail "exited state was not reported"
grep -Eq '^logs[[:space:]]' "$test_root/new-exited/docker.log" || fail "exited logs were not requested"

set +e
run_mock inspect-failure env
status=$?
set -e
[[ $status == 1 ]] || fail "inspect failure should fail with status 1"
grep -Fq 'unable to inspect Docker container' "$test_root/inspect-failure/stderr" || \
  fail "inspect failure was not reported"
grep -Eq '^logs[[:space:]]' "$test_root/inspect-failure/docker.log" || \
  fail "inspect failure logs were not requested"

set +e
run_mock timeout env DORIS_READY_TIMEOUT_SECONDS=0
status=$?
set -e
[[ $status == 1 ]] || fail "health timeout should fail with status 1"
grep -Fq 'did not become healthy within 0s' "$test_root/timeout/stderr" || fail "timeout was not reported"
grep -Eq '^logs[[:space:]]' "$test_root/timeout/docker.log" || fail "timeout logs were not requested"

set +e
run_mock run-failure env
status=$?
set -e
[[ $status == 1 ]] || fail "docker run failure should fail with status 1"
grep -Fq 'mock port conflict' "$test_root/run-failure/stderr" || fail "docker run error was hidden"
grep -Fq 'Choose unused DORIS_* host ports' "$test_root/run-failure/stderr" || \
  fail "docker run failure did not explain recovery"

for log_file in "$test_root"/*/docker.log; do
  if grep -Eq '^(stop|rm)([[:space:]]|$)' "$log_file"; then
    fail "start-doris.sh attempted to stop or remove a container"
  fi
done

echo "start-doris.sh tests passed"
