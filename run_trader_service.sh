#!/usr/bin/env bash
set -uo pipefail
cd "$HOME/exitmgr-app" || exit 1

if [ -f "$HOME/exitmgr-app/TRADING_DOWN" ]; then
  echo "[run_trader_service] TRADING_DOWN active: entries blocked; protective exits remain armed." >&2
fi

source "$HOME/.hermes/.env" 2>/dev/null   # provides SLACK_BOT_TOKEN
export SLACK_BOT_TOKEN
export PYTHONUNBUFFERED=1
export EXITMGR_ORDER_LOCK="${EXITMGR_ORDER_LOCK:-$HOME/.local/var/exitmgr/order-mutation.lock}"
export EXITMGR_ROTATE_SERVICE_LOGS=1
export M3_PRIORITY_TOKEN_FILE="${M3_PRIORITY_TOKEN_FILE:-$HOME/.config/m3-serving/priority-token}"
export TRADER_LLM_PRIORITY=0
export TRADER_REQUIRE_PRIORITY_TOKEN=1
export TRADER_REQUIRE_RUNTIME_IDENTITY=1
export TRADER_STRUCTURED_OUTPUT=1

export TRADER_CAPTURE_EXIT_IDENTITY=1
exec "$HOME/ib-grader-venv/bin/python" run_trader.py --arm --loop --mode entry \
  --interval 1200 --protective-interval 30
