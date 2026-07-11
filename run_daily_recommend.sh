#!/usr/bin/env bash
set -uo pipefail
cd "$HOME/exitmgr-app" || exit 1
source "$HOME/.hermes/.env" 2>/dev/null
export SLACK_BOT_TOKEN
"$HOME/ib-grader-venv/bin/python" -m exitmgr.entry_safety --config config.yaml || {
  echo "[run_daily_recommend] entry safety preflight blocked the slate" >&2
  exit 2
}
# RAG grounding for the slate (2026-07-10): enables the existing exitmgr/research.py
# rag_context_sync path (research.gather -> build_brief "Prior context" block).
# FAIL-SAFE: RAG server down/timeout/empty => slate proceeds UNGROUNDED, never blocks a trade
# (verified: rag_context_sync returns [] with no exception when the RAG service is down).
# POINT-IN-TIME: live decisions only (as_of = now). Do NOT reuse this flag for BACKTESTING
# without date-filtering retrieval to as-of-the-decision-date, or it leaks the future.
export STRATEGIST_RAG_ENABLED=1
export M3_PRIORITY_TOKEN_FILE="${M3_PRIORITY_TOKEN_FILE:-$HOME/.config/m3-serving/priority-token}"
export TRADER_LLM_PRIORITY=0
export TRADER_REQUIRE_PRIORITY_TOKEN=1
export TRADER_REQUIRE_RUNTIME_IDENTITY=1
export EXITMGR_ORDER_LOCK="${EXITMGR_ORDER_LOCK:-$HOME/.local/var/exitmgr/order-mutation.lock}"
APPROVALS_CHANNEL="C0XXXXXXXXX"  # replace with #trading-approvals channel ID
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
