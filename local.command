#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
exec backend/.venv/bin/python scripts/local-run.py "${1:-start}"
