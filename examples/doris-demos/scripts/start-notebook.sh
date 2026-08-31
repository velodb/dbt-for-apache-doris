#!/usr/bin/env bash
set -euo pipefail

demo_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
jupyter_port=${JUPYTER_PORT:-18888}
dbt_bin=${DBT_BIN:-$demo_dir/.venv/bin/dbt}

if [[ "$dbt_bin" == "/path/to/dbt" ]]; then
  echo "DBT_BIN still contains the example placeholder /path/to/dbt." >&2
  exit 2
fi

if [[ ! -x "$dbt_bin" ]]; then
  echo "dbt executable is not available: $dbt_bin" >&2
  echo "run $demo_dir/scripts/prepare-python-env.sh first or set DBT_BIN" >&2
  exit 2
fi

export DBT_BIN="$dbt_bin"

echo "Starting the Jupyter server on 127.0.0.1:$jupyter_port"
echo "Keep this process running. Read README.md, then select a demo from notebooks/."

jupyter_args=(
  --no-browser
  --ip=127.0.0.1
  --port="$jupyter_port"
  --ServerApp.port_retries=0
  --ServerApp.root_dir="$demo_dir"
  --ServerApp.default_url=/lab/tree
  --ContentsManager.hide_globs='["scripts", "artifacts", ".venv", ".ipynb_checkpoints", "__pycache__", "*.pyc", "*.pyo", "*.user.yml", ".DS_Store", "*~"]'
)

if [[ -x "$demo_dir/.venv/bin/jupyter-lab" ]]; then
  exec "$demo_dir/.venv/bin/jupyter-lab" "${jupyter_args[@]}"
fi

if command -v jupyter-lab >/dev/null 2>&1; then
  exec jupyter-lab "${jupyter_args[@]}"
fi

if command -v uvx >/dev/null 2>&1; then
  exec uvx --from jupyterlab jupyter-lab "${jupyter_args[@]}"
fi

echo "Neither jupyter-lab nor uvx is installed on the server." >&2
exit 2
