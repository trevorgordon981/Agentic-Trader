#!/usr/bin/env bash
set -uo pipefail
cd "$HOME/exitmgr-app" || exit 1
source "$HOME/.hermes/.env" 2>/dev/null
export SLACK_BOT_TOKEN
exec "$HOME/ib-grader-venv/bin/python" morning_review.py --watch-minutes 120
