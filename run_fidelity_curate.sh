#!/bin/bash
set -uo pipefail

APP="$HOME/exitmgr-app"
PY="$HOME/ib-grader-venv/bin/python"
LOG_DIR="$HOME/.hermes/logs"
mkdir -p "$LOG_DIR"

cd "$APP" || exit 1
echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') fidelity curate ==="
"$PY" -c 'import sys; print("interpreter:", sys.executable, sys.version.split()[0])'

PYTHONPATH="$APP" "$PY" "$APP/fidelity_freshness.py" "$@"
rc=$?

echo "exit=$rc"
exit $rc
