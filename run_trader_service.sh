#!/usr/bin/env bash
# Background trader service. Runs the orchestrator loop in DRY-RUN (no --arm).
# Flip to live by adding --arm here ONLY after paper validation.
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
exec "$HOME/ib-grader-venv/bin/python" run_trader.py --arm --loop \
  --interval 1200 --protective-interval 30
