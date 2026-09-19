#!/usr/bin/env bash
set -uo pipefail
source "$HOME/.hermes/.env" 2>/dev/null
export SLACK_BOT_TOKEN
APPROVALS_CHANNEL="$(python3 -c "import yaml,sys;print((yaml.safe_load(open(sys.argv[1])) or {}).get('trading',{}).get('slack_channel','') or 'CHANNEL_ID_PLACEHOLDER')" "$HOME/exitmgr-app/config.yaml" 2>/dev/null || echo CHANNEL_ID_PLACEHOLDER)"
notify(){ resp=$(curl -s -m 10 -X POST https://slack.com/api/chat.postMessage \
  -H "Authorization: Bearer ${SLACK_BOT_TOKEN:-}" -H "Content-type: application/json" \
  -d "{\"channel\":\"$APPROVALS_CHANNEL\",\"text\":\"$1\"}" 2>/dev/null)
  printf '%s' "$resp" | grep -q '\"ok\":[[:space:]]*true' || \
    echo "SLACK POST FAILED [daily-recommend notify] channel=$APPROVALS_CHANNEL resp=$resp" >&2; }
DAILY_SLATE_PHASE="startup"
DAILY_SLATE_CHILD_PID=""
DAILY_SLATE_STARTING_CHILD=0
DAILY_SLATE_STOP_STATUS=0
daily_slate_signal() {
  DAILY_SLATE_STOP_STATUS="$1"
  if [ "$DAILY_SLATE_STARTING_CHILD" -ne 0 ]; then
    return
  fi
  trap '' TERM INT
  if [ -n "$DAILY_SLATE_CHILD_PID" ]; then
    kill -TERM "$DAILY_SLATE_CHILD_PID" 2>/dev/null || true
    wait "$DAILY_SLATE_CHILD_PID" 2>/dev/null || true
    DAILY_SLATE_CHILD_PID=""
  fi
  exit "$DAILY_SLATE_STOP_STATUS"
}
daily_slate_run_child() {
  local slate_child_status
  DAILY_SLATE_STARTING_CHILD=1
  "$@" &
  DAILY_SLATE_CHILD_PID=$!
  DAILY_SLATE_STARTING_CHILD=0
  if [ "$DAILY_SLATE_STOP_STATUS" -ne 0 ]; then
    daily_slate_signal "$DAILY_SLATE_STOP_STATUS"
  fi
  wait "$DAILY_SLATE_CHILD_PID"
  slate_child_status=$?
  DAILY_SLATE_CHILD_PID=""
  return "$slate_child_status"
}
daily_slate_exit() {
  local slate_exit_status="$1"
  trap - EXIT
  if [ "$slate_exit_status" -ne 0 ]; then
    echo "[run_daily_recommend] failed during $DAILY_SLATE_PHASE (exit $slate_exit_status)" >&2
    notify ":warning: *Daily slate failed during $DAILY_SLATE_PHASE (exit $slate_exit_status).* See daily-recommend.err on trader host for details."
  fi
  exit "$slate_exit_status"
}
trap 'daily_slate_exit "$?"' EXIT
trap 'daily_slate_signal 143' TERM
trap 'daily_slate_signal 130' INT
cd "$HOME/exitmgr-app" || exit "$?"
DAILY_SLATE_PHASE="entry safety preflight"
daily_slate_run_child "$HOME/ib-grader-venv/bin/python" -m exitmgr.entry_safety --config config.yaml || {
  slate_preflight_status=$?
  echo "[run_daily_recommend] entry safety preflight blocked the slate" >&2
  exit "$slate_preflight_status"
}
export STRATEGIST_RAG_ENABLED=1
export M3_PRIORITY_TOKEN_FILE="${M3_PRIORITY_TOKEN_FILE:-$HOME/.config/m3-serving/priority-token}"
export TRADER_LLM_PRIORITY=0
export TRADER_REQUIRE_PRIORITY_TOKEN=1
export TRADER_REQUIRE_RUNTIME_IDENTITY=1
export TRADER_STRUCTURED_OUTPUT=1

export EXITMGR_ORDER_LOCK="${EXITMGR_ORDER_LOCK:-$HOME/.local/var/exitmgr/order-mutation.lock}"
SKIP_DATES="2026-06-17"
TODAY="$(date +%Y-%m-%d)"
for skd in $SKIP_DATES; do
  if [ "$TODAY" = "$skd" ]; then
    echo "$(date): daily slate skipped ($skd) -- notifying Slack."
    notify ":calendar: *Daily slate skipped today ($skd, e.g. FOMC).* It resumes automatically tomorrow. Run slate-now (or ask Claude) if you want one anyway."
    exit 0
  fi
done
DAILY_SLATE_PHASE="slate startup or execution"
daily_slate_run_child "$HOME/ib-grader-venv/bin/python" daily_recommend.py --watch-mins 360
exit "$?"
