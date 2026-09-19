#!/usr/bin/env bash
set -uo pipefail
cd "$HOME/exitmgr-app" || exit 1
set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a
export SLACK_BOT_TOKEN
export STRATEGIST_RAG_ENABLED=1
export M3_PRIORITY_TOKEN_FILE="${M3_PRIORITY_TOKEN_FILE:-$HOME/.config/m3-serving/priority-token}"
export TRADER_LLM_PRIORITY=0
export TRADER_REQUIRE_PRIORITY_TOKEN=1
export TRADER_REQUIRE_RUNTIME_IDENTITY=1
export TRADER_STRUCTURED_OUTPUT=1
export EXITMGR_ORDER_LOCK="${EXITMGR_ORDER_LOCK:-$HOME/.local/var/exitmgr/order-mutation.lock}"
CONFIG=config.yaml; WATCH=360; EXTRA=()
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
echo "slate-now: config=$CONFIG watch=${WATCH}m clientId=111"
exec "$HOME/ib-grader-venv/bin/python" daily_recommend.py --config "$CONFIG" \
     --watch-mins "$WATCH" --client-id 111 ${EXTRA[@]+"${EXTRA[@]}"}
