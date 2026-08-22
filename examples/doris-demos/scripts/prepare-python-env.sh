#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
demo_dir=$(cd "$script_dir/.." && pwd)

uv_bin=${UV_BIN:-uv}
venv_dir=${DBT_DEMO_VENV:-$demo_dir/.venv}
python_version=${DBT_DEMO_PYTHON_VERSION:-3.12.13}
dbt_core_version=${DBT_CORE_VERSION:-1.12.2}
adapter_requirement=${DBT_DORIS_PACKAGE:-dbt-for-apache-doris==1.1.0}

if [[ ! $python_version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "DBT_DEMO_PYTHON_VERSION must be an exact X.Y.Z version: $python_version" >&2
  exit 2
fi

if ! command -v "$uv_bin" >/dev/null 2>&1; then
  echo "uv is required; set UV_BIN to its executable path" >&2
  exit 1
fi

reuse_venv=0
if [[ -x "$venv_dir/bin/python" && -x "$venv_dir/bin/dbt" ]]; then
  if DBT_DEMO_EXPECTED_PYTHON="$python_version" \
    DBT_DEMO_EXPECTED_CORE="$dbt_core_version" \
    DBT_DEMO_EXPECTED_ADAPTER="$adapter_requirement" \
    "$venv_dir/bin/python" - <<'PY'
from importlib.metadata import version
import os
import sys

expected_python = tuple(map(int, os.environ["DBT_DEMO_EXPECTED_PYTHON"].split(".")))
expected_core = os.environ["DBT_DEMO_EXPECTED_CORE"]
expected_adapter = os.environ["DBT_DEMO_EXPECTED_ADAPTER"]
adapter_name, _, adapter_version = expected_adapter.partition("==")
try:
    matches = (
        sys.version_info[:3] == expected_python
        and version("dbt-core") == expected_core
        and version(adapter_name) == adapter_version
    )
except Exception:
    matches = False
raise SystemExit(0 if matches else 1)
PY
  then
    reuse_venv=1
  fi
fi

if [[ $reuse_venv == 0 ]]; then
  venv_args=()
  if [[ -e $venv_dir || -L $venv_dir ]]; then
    if [[ -L $venv_dir || ! -f $venv_dir/pyvenv.cfg ]]; then
      echo "refusing to replace a directory that is not a virtual environment: $venv_dir" >&2
      exit 1
    fi
    venv_args+=(--clear)
  fi
  "$uv_bin" venv "${venv_args[@]}" "$venv_dir" --python "$python_version"
  "$uv_bin" pip install \
    --link-mode copy \
    --python "$venv_dir/bin/python" \
    "dbt-core==$dbt_core_version" \
    "$adapter_requirement"
fi

if [[ ${INSTALL_JUPYTER:-0} == 1 ]]; then
  "$uv_bin" pip install \
    --link-mode copy \
    --python "$venv_dir/bin/python" \
    jupyterlab
fi

"$venv_dir/bin/python" - <<PY
import sys

expected = tuple(map(int, "$python_version".split(".")))
actual = sys.version_info[:3]
if actual != expected:
    raise SystemExit(f"expected Python {expected}, got {actual}")
print(f"Python {sys.version.split()[0]}")
PY
"$venv_dir/bin/dbt" --version

printf '\nEnvironment ready. Run all demos with:\n'
printf '  %q\n' "$script_dir/run-all.sh"
printf 'Start JupyterLab with:\n'
printf '  %q\n' "$script_dir/start-notebook.sh"
