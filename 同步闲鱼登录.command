#!/bin/bash
set -uo pipefail
cd "$(dirname "$0")"
python_bin="$PWD/backend/.venv/bin/python"
if [[ ! -x "$python_bin" ]]; then
  echo "请先按 LOCAL-RUN.md 安装项目的本地 Python 环境。"
  exit 1
fi

# The local workbench performs the QR login and writes its own private
# tenants/<user>/accounts/<shop>/cookies.txt.  Keep the local API in observe
# + login-only mode so completing the login cannot start a local Worker.
exec "$python_bin" scripts/sync-local-login.py "$@"
