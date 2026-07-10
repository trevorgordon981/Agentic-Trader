#!/usr/bin/env bash
# On-demand daily slate. Usage: slate-now [--full-pot] [--watch-mins N] [extra daily_recommend args]
# Handles token + clientId(97, no clash with cron's 93) + optional full-pot sizing.
set -uo pipefail
cd "$HOME/exitmgr-app" || exit 1
set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a
CONFIG=config.yaml; WATCH=240; EXTRA=()
while [ $# -gt 0 ]; do
  case "$1" in
    --full-pot) echo "slate-now: --full-pot is retired; hard size/concentration caps cannot be overridden" >&2
                exit 2 ;;
    --watch-mins) shift; WATCH="$1" ;;
    *) EXTRA+=("$1") ;;
  esac
  shift
done
"$HOME/ib-grader-venv/bin/python" -m exitmgr.entry_safety --config "$CONFIG" || {
  echo "[slate-now] entry safety preflight blocked the slate" >&2
  exit 2
}
echo "slate-now: config=$CONFIG watch=${WATCH}m clientId=97"
# bash 3.2 (macOS) safe empty-array expansion under set -u
exec "$HOME/ib-grader-venv/bin/python" daily_recommend.py --config "$CONFIG" \
     --watch-mins "$WATCH" --client-id 97 ${EXTRA[@]+"${EXTRA[@]}"}
