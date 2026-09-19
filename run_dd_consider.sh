#!/bin/bash
set -uo pipefail
cd "$HOME/exitmgr-app" || exit 1
source "$HOME/.hermes/.env" 2>/dev/null
export SLACK_BOT_TOKEN
export TRADER_STRUCTURED_OUTPUT=1
export PYTHONUNBUFFERED=1
exec "$HOME/ib-grader-venv/bin/python" dd_consider.py --arm
