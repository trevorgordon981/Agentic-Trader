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
# Constrain Stage A to the entry-contract JSON schema at DECODE time (vLLM/xgrammar).
# Measured on deepseek-v4-flash: duplicate-JSON-key contract failures 2/10 cycles -> 0/10.
export TRADER_STRUCTURED_OUTPUT=1

export EXITMGR_ORDER_LOCK="${EXITMGR_ORDER_LOCK:-$HOME/.local/var/exitmgr/order-mutation.lock}"
# #trading-approvals, read from config.yaml (trading.slack_channel) at runtime so a
# scrub/rename cannot silently mute the skip notice; literal is a last resort.
APPROVALS_CHANNEL="$(python3 -c "import yaml,sys;print((yaml.safe_load(open(sys.argv[1])) or {}).get('trading',{}).get('slack_channel','') or 'C0XXXXXXXXX')" "$HOME/exitmgr-app/config.yaml" 2>/dev/null || echo C0XXXXXXXXX)"
notify(){ resp=$(curl -s -m 10 -X POST https://slack.com/api/chat.postMessage \
  -H "Authorization: Bearer ${SLACK_BOT_TOKEN:-}" -H "Content-type: application/json" \
  -d "{\"channel\":\"$APPROVALS_CHANNEL\",\"text\":\"$1\"}" 2>/dev/null)
  # HTTP 200 + {"ok":false} is how a bad channel fails; a silent skip notice
  # means Trevor thinks the slate ran when it did not.
  printf '%s' "$resp" | grep -q '\"ok\":[[:space:]]*true' || \
    echo "SLACK POST FAILED [daily-recommend notify] channel=$APPROVALS_CHANNEL resp=$resp" >&2; }
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
