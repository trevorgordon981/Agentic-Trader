#!/usr/bin/env bash
set -uo pipefail
cd "$HOME/exitmgr-app" || exit 1
source "$HOME/.hermes/.env" 2>/dev/null
export SLACK_BOT_TOKEN
APPROVALS_CHANNEL="C0BA42N472M"  # #trading-approvals
notify(){ curl -s -m 10 -X POST https://slack.com/api/chat.postMessage \
  -H "Authorization: Bearer ${SLACK_BOT_TOKEN:-}" -H "Content-type: application/json" \
  -d "{\"channel\":\"$APPROVALS_CHANNEL\",\"text\":\"$1\"}" >/dev/null 2>&1; }
# --- manual skip days (FOMC etc.): space-separated YYYY-MM-DD. Each NOTIFIES Slack (no silent skip). ---
SKIP_DATES="2026-06-17"
TODAY="$(date +%Y-%m-%d)"
for skd in $SKIP_DATES; do
  if [ "$TODAY" = "$skd" ]; then
    echo "$(date): daily slate skipped ($skd) -- notifying Slack."
    notify ":calendar: *Daily slate skipped today ($skd, e.g. FOMC).* It resumes automatically tomorrow. Run slate-now (or ask Claude) if you want one anyway."
    exit 0
  fi
done
exec "$HOME/ib-grader-venv/bin/python" daily_recommend.py --watch-mins 360
