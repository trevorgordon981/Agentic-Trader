#!/usr/bin/env bash
# Background trader service. Runs the orchestrator loop in DRY-RUN (no --arm).
# Flip to live by adding --arm here ONLY after paper validation.
set -uo pipefail
cd "$HOME/exitmgr-app" || exit 1

# TRADING-DOWN GUARD (2026-07-03): while the TRADING_DOWN marker exists (e.g. the GLM training
# window / any maintenance), REFUSE to arm live trading -- even if launchd/RunAtLoad tries to start
# us after a reboot. Remove $HOME/exitmgr-app/TRADING_DOWN to re-enable armed trading.
if [ -f "$HOME/exitmgr-app/TRADING_DOWN" ]; then
  echo "[run_trader_service] TRADING_DOWN marker present -- refusing to arm live trading." >&2
  echo "[run_trader_service] rm $HOME/exitmgr-app/TRADING_DOWN to re-enable." >&2
  exit 1
fi

source "$HOME/.hermes/.env" 2>/dev/null   # provides SLACK_BOT_TOKEN
export SLACK_BOT_TOKEN
export PYTHONUNBUFFERED=1
exec "$HOME/ib-grader-venv/bin/python" run_trader.py --arm --loop --interval 1200
