#!/bin/bash
set -uo pipefail

APP="$HOME/exitmgr-app"
PY="$HOME/ib-grader-venv/bin/python"
EXPORTS="$HOME/fidelity-exports/Accounts_History*.csv"
LOG_DIR="$HOME/.hermes/logs"
mkdir -p "$LOG_DIR"

cd "$APP" || exit 1
echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') external-book snapshot ==="
"$PY" -c 'import sys; print("interpreter:", sys.executable, sys.version.split()[0])'

EXPORT="$("$PY" -c "import sys; sys.path.insert(0, '$APP'); import cross_book; print(cross_book.best_export('$EXPORTS') or '')")"
if [ -z "$EXPORT" ]; then
    echo "no Fidelity export under $EXPORTS -- previous snapshot left untouched"
    exit 2
fi
echo "export: $(basename "$EXPORT")"

PYTHONPATH="$APP" "$PY" "$APP/cross_book.py" --export "$EXPORT" --write-snapshot
wrc=$?
if [ $wrc -ne 0 ]; then
    echo "snapshot write FAILED rc=$wrc -- previous snapshot left untouched (write is atomic)"
    echo "exit=$wrc"
    exit $wrc
fi

PYTHONPATH="$APP" "$PY" "$APP/cross_book.py" --check --config "$APP/config.yaml"
rc=$?
echo "exit=$rc"
exit $rc
