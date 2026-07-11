#!/usr/bin/env bash
# Background ENTRY service. It is explicitly ARMED and still requires Slack approval for every BUY.
# Protective exits run in the separate CPU-only run_protective_service.sh process.
set -uo pipefail
cd "$HOME/exitmgr-app" || exit 1

# TRADING_DOWN blocks BUY entries inside Python before proposal and submit, but must not disarm
# protective SELL exits. Starting this service with the marker present is therefore safe and is
# required to preserve stops. The service remains disabled/unloaded until explicitly rearmed.
if [ -f "$HOME/exitmgr-app/TRADING_DOWN" ]; then
  echo "[run_trader_service] TRADING_DOWN active: entries blocked; protective exits remain armed." >&2
fi

source "$HOME/.hermes/.env" 2>/dev/null   # provides SLACK_BOT_TOKEN
export SLACK_BOT_TOKEN
export PYTHONUNBUFFERED=1
export EXITMGR_ORDER_LOCK="${EXITMGR_ORDER_LOCK:-$HOME/.local/var/exitmgr/order-mutation.lock}"
export M3_PRIORITY_TOKEN_FILE="${M3_PRIORITY_TOKEN_FILE:-$HOME/.config/m3-serving/priority-token}"
export TRADER_LLM_PRIORITY=0
export TRADER_REQUIRE_PRIORITY_TOKEN=1
export TRADER_REQUIRE_RUNTIME_IDENTITY=1
export TRADER_CAPTURE_EXIT_IDENTITY=1
exec "$HOME/ib-grader-venv/bin/python" run_trader.py --arm --loop --mode entry \
  --interval 1200 --protective-interval 30
