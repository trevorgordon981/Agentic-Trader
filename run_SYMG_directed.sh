#!/usr/bin/env bash
set -uo pipefail
cd "$HOME/exitmgr-app" || exit 1
source "$HOME/.hermes/.env" 2>/dev/null   # SLACK_BOT_TOKEN; absent -> the card cannot post
export SLACK_BOT_TOKEN
export PYTHONUNBUFFERED=1
exec "$HOME/ib-grader-venv/bin/python" daily_recommend.py \
  --ticker SYMG --structure "call debit spread" --dte 301 --watch-mins 385 --client-id 110
