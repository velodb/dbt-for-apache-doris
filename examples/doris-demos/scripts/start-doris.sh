#!/usr/bin/env bash
set -euo pipefail

container_name=${DORIS_CONTAINER_NAME:-dbt-doris-demo}
doris_image=${DORIS_IMAGE:-apache/doris:all-in-one-4.1.3}
doris_port=${DORIS_PORT:-29030}
fe_http_port=${DORIS_FE_HTTP_PORT:-28030}
be_http_port=${DORIS_BE_HTTP_PORT:-28040}
ready_timeout_seconds=${DORIS_READY_TIMEOUT_SECONDS:-180}
ready_interval_seconds=${DORIS_READY_INTERVAL_SECONDS:-2}

usage() {
  cat <<EOF
Usage: $(basename "$0")

Start or reuse an Apache Doris all-in-one container for the demos.

Environment overrides:
  DORIS_CONTAINER_NAME       container name (default: dbt-doris-demo)
  DORIS_IMAGE                image (default: apache/doris:all-in-one-4.1.3)
  DORIS_PORT                 host FE query port (default: 29030)
  DORIS_FE_HTTP_PORT         host FE HTTP port (default: 28030)
  DORIS_BE_HTTP_PORT         host BE HTTP port (default: 28040)
  DORIS_READY_TIMEOUT_SECONDS  health timeout (default: 180)
  DORIS_READY_INTERVAL_SECONDS health poll interval (default: 2)

Every published port is bound to 127.0.0.1. Existing containers are never
stopped or removed. A container is reused only when it is healthy and its
image and port bindings exactly match the requested configuration.
EOF
}

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

validate_port() {
  local name=$1
  local value=$2
  case "$value" in
    ''|*[!0-9]*) fail "$name must be an integer from 1 to 65535: $value" ;;
  esac
  if ((value < 1 || value > 65535)); then
    fail "$name must be an integer from 1 to 65535: $value"
  fi
}

validate_nonnegative_integer() {
  local name=$1
  local value=$2
  case "$value" in
    ''|*[!0-9]*) fail "$name must be a non-negative integer: $value" ;;
  esac
}

show_logs() {
  echo "Last Doris container log lines:" >&2
  if ! docker logs --tail 80 "$container_name" >&2; then
    echo "Unable to read logs for $container_name." >&2
  fi
}

inspect_container() {
  local inspect_format
  local inspect_output

  inspect_format='{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|{{.Config.Image}}|{{with (index .HostConfig.PortBindings "9030/tcp")}}{{(index . 0).HostIp}}:{{(index . 0).HostPort}}{{end}}|{{with (index .HostConfig.PortBindings "8030/tcp")}}{{(index . 0).HostIp}}:{{(index . 0).HostPort}}{{end}}|{{with (index .HostConfig.PortBindings "8040/tcp")}}{{(index . 0).HostIp}}:{{(index . 0).HostPort}}{{end}}'
  if ! inspect_output=$(docker inspect --format "$inspect_format" "$container_name" 2>&1); then
    echo "ERROR: unable to inspect Docker container $container_name:" >&2
    echo "$inspect_output" >&2
    return 1
  fi
  IFS='|' read -r container_state container_health container_image \
    container_fe_binding container_fe_http_binding container_be_http_binding \
    <<<"$inspect_output"
}

wait_until_healthy() {
  local deadline=$((SECONDS + ready_timeout_seconds))

  while :; do
    if ! inspect_container; then
      show_logs
      return 1
    fi
    case "$container_state" in
      exited|dead|removing)
        echo "ERROR: Doris container $container_name entered state $container_state." >&2
        show_logs
        return 1
        ;;
    esac
    case "$container_health" in
      healthy)
        return 0
        ;;
      unhealthy)
        echo "ERROR: Doris container $container_name is unhealthy." >&2
        show_logs
        return 1
        ;;
      starting)
        ;;
      *)
        echo "ERROR: Doris container $container_name has no usable health check (status: $container_health)." >&2
        show_logs
        return 1
        ;;
    esac
    if ((SECONDS >= deadline)); then
      echo "ERROR: Doris container $container_name did not become healthy within ${ready_timeout_seconds}s." >&2
      show_logs
      return 1
    fi
    sleep "$ready_interval_seconds"
  done
}

if (($#)); then
  case "$1" in
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
fi

case "$container_name" in
  ''|*[!A-Za-z0-9_.-]*|[.-]*) fail "invalid DORIS_CONTAINER_NAME: $container_name" ;;
esac
[[ -n $doris_image ]] || fail "DORIS_IMAGE must not be empty"
validate_port DORIS_PORT "$doris_port"
validate_port DORIS_FE_HTTP_PORT "$fe_http_port"
validate_port DORIS_BE_HTTP_PORT "$be_http_port"
if [[ $doris_port == "$fe_http_port" || $doris_port == "$be_http_port" || \
      $fe_http_port == "$be_http_port" ]]; then
  fail "DORIS_PORT, DORIS_FE_HTTP_PORT, and DORIS_BE_HTTP_PORT must be different"
fi
validate_nonnegative_integer DORIS_READY_TIMEOUT_SECONDS "$ready_timeout_seconds"
validate_nonnegative_integer DORIS_READY_INTERVAL_SECONDS "$ready_interval_seconds"

command -v docker >/dev/null 2>&1 || fail "docker is required to start the all-in-one Doris image"
if ! docker info >/dev/null 2>&1; then
  fail "the Docker daemon is unavailable"
fi

if ! existing_names=$(docker ps -a --filter "name=^/${container_name}$" --format '{{.Names}}' 2>&1); then
  echo "ERROR: unable to list Docker containers:" >&2
  echo "$existing_names" >&2
  exit 1
fi

expected_fe_binding=127.0.0.1:$doris_port
expected_fe_http_binding=127.0.0.1:$fe_http_port
expected_be_http_binding=127.0.0.1:$be_http_port

if [[ -n $existing_names ]]; then
  if [[ $existing_names != "$container_name" ]]; then
    fail "Docker returned an unexpected container match: $existing_names"
  fi
  if ! inspect_container; then
    show_logs
    exit 1
  fi
  if [[ $container_state == running && $container_health == healthy && \
        $container_image == "$doris_image" && \
        $container_fe_binding == "$expected_fe_binding" && \
        $container_fe_http_binding == "$expected_fe_http_binding" && \
        $container_be_http_binding == "$expected_be_http_binding" ]]; then
    echo "Reusing healthy Doris container $container_name."
  else
    cat >&2 <<EOF
ERROR: Docker container $container_name already exists but cannot be reused.
Expected: state=running, health=healthy, image=$doris_image,
          ports=$expected_fe_binding,$expected_fe_http_binding,$expected_be_http_binding
Actual:   state=$container_state, health=$container_health, image=$container_image,
          ports=$container_fe_binding,$container_fe_http_binding,$container_be_http_binding
The existing container was left unchanged. Set DORIS_CONTAINER_NAME and, when
needed, DORIS_PORT, DORIS_FE_HTTP_PORT, and DORIS_BE_HTTP_PORT to unused values.
EOF
    if [[ $container_health == unhealthy || $container_state == exited || $container_state == dead ]]; then
      show_logs
    fi
    exit 1
  fi
else
  echo "Pulling $doris_image..."
  if ! docker pull "$doris_image"; then
    fail "unable to pull Doris image $doris_image"
  fi
  echo "Starting Doris container $container_name..."
  if ! run_output=$(docker run -d --name "$container_name" \
    -p "$expected_fe_binding:9030" \
    -p "$expected_fe_http_binding:8030" \
    -p "$expected_be_http_binding:8040" \
    "$doris_image" 2>&1); then
    echo "ERROR: unable to start Doris container $container_name:" >&2
    echo "$run_output" >&2
    echo "Choose unused DORIS_* host ports or a different DORIS_CONTAINER_NAME." >&2
    exit 1
  fi
  echo "Container ID: $run_output"
  wait_until_healthy || exit 1
fi

cat <<EOF
Doris is healthy.
  FE query: 127.0.0.1:$doris_port
  FE HTTP:  http://127.0.0.1:$fe_http_port
  BE HTTP:  http://127.0.0.1:$be_http_port

Use this endpoint for the demos:
  export DORIS_HOST=127.0.0.1
  export DORIS_PORT=$doris_port
EOF
