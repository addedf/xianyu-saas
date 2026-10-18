#!/bin/bash
set -uo pipefail
cd "$(dirname "$0")"
python_bin="$PWD/backend/.venv/bin/python"
if [[ ! -x "$python_bin" ]]; then
  echo "请先按 LOCAL-RUN.md 安装项目的本地 Python 环境。"
  exit 1
fi
if ! "$python_bin" -c 'from importlib.metadata import version; assert version("playwright") == "1.64.0"' >/dev/null 2>&1; then
  "$python_bin" -m pip --disable-pip-version-check install -r scripts/requirements-local-login.txt || exit 1
fi
"$python_bin" scripts/sync-local-login.py "$@"
login_sync_result=$?
if [[ $# -eq 0 && -t 0 ]]; then
  read -r -p "按回车关闭窗口。" _login_sync_done
fi
exit "$login_sync_result"
