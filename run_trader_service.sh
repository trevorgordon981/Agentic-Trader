#!/usr/bin/env bash
# Background trader service. Runs the orchestrator loop in DRY-RUN (no --arm).
# Flip to live by adding --arm here ONLY after paper validation.
set -uo pipefail
cd "$HOME/exitmgr-app" || exit 1
source "$HOME/.hermes/.env" 2>/dev/null   # provides SLACK_BOT_TOKEN
export SLACK_BOT_TOKEN
export PYTHONUNBUFFERED=1
exec "$HOME/ib-grader-venv/bin/python" run_trader.py --arm --loop --interval 900
