#!/usr/bin/env bash
set -euo pipefail

demo_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
notebook="$demo_dir/dbt-for-apache-doris-demos.ipynb"
jupyter_port=${JUPYTER_PORT:-18888}

: "${DBT_BIN:?Set DBT_BIN to the dbt Core executable with dbt-for-apache-doris installed.}"

if [[ "$DBT_BIN" == "/path/to/dbt" ]]; then
  echo "DBT_BIN still contains the example placeholder /path/to/dbt." >&2
  exit 2
fi

if [[ ! -x "$DBT_BIN" ]]; then
  echo "DBT_BIN is not executable: $DBT_BIN" >&2
  exit 2
fi

export DBT_BIN

echo "Starting the Jupyter server on 127.0.0.1:$jupyter_port"
echo "Keep this process running and open the URL printed by JupyterLab."

jupyter_args=(
  "$notebook"
  --no-browser
  --ip=127.0.0.1
  --port="$jupyter_port"
  --ServerApp.port_retries=0
)

if command -v jupyter-lab >/dev/null 2>&1; then
  exec jupyter-lab "${jupyter_args[@]}"
fi

if command -v uvx >/dev/null 2>&1; then
  exec uvx --from jupyterlab jupyter-lab "${jupyter_args[@]}"
fi

echo "Neither jupyter-lab nor uvx is installed on the server." >&2
exit 2
