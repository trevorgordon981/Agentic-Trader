#!/bin/bash
# Daily RAG-usage readout -> Slack #deploy-notifications.
# Makes "is Alfred actually using RAG" a visible daily number. Reports searches
# since the last run (delta of the cumulative /search counter, robust to server
# restarts) plus the current source split (Alfred vs local-test vs other).
set -uo pipefail
NODE4=localhost
STATE="$HOME/.hermes/.rag_usage_last"
# Channels resolved from exitmgr config.yaml where it defines them; #deploy-notifications
# is not in that config, so it stays a literal (verified live 2026-08-12).
CHANNEL="C0XXXXXXXXX"   # #deploy-notifications
set -a; . "$HOME/.hermes/.env" 2>/dev/null; set +a
TOKEN="${SLACK_BOT_TOKEN:-}"

# chat.postMessage answers HTTP 200 with {"ok":false,"error":...} for a bad or
# unjoined channel, so an unchecked curl silently discards the message. Check it.
slack_ok() {  # $1=response body  $2=channel  $3=label
  if ! printf '%s' "$1" | grep -q '"ok":[[:space:]]*true'; then
    err=$(printf '%s' "$1" | sed -n 's/.*"error":"\([^"]*\)".*/\1/p')
    echo "SLACK POST FAILED [$3] channel=$2 error=${err:-unknown} -- REACHED NOBODY" >&2
    return 1
  fi
  return 0
}

# Cumulative /search count (one tool-call per search).
cur=$(curl -s --max-time 8 "http://${NODE4}:9000/metrics" \
  | awk '/^llm_llm_tool_calls_total\{.*endpoint="\/embed"/ {print $2}' | head -1)
cur=${cur%.*}; [ -z "$cur" ] && cur=0
last=$(cat "$STATE" 2>/dev/null || echo 0)
if [ "$cur" -ge "$last" ] 2>/dev/null; then delta=$((cur - last)); else delta=$cur; fi
echo "$cur" > "$STATE"

# Source split from the access log (since server start). localhost = your-host = Alfred.
counts=$(ssh -o ConnectTimeout=8 rag-host 'grep "POST /search" ~/rag-data/rag_server.log 2>/dev/null | awk "{print \$2}" | cut -d: -f1 | sort | uniq -c' 2>/dev/null)
alfred=$(echo "$counts" | awk '$2=="localhost"{print $1}'); alfred=${alfred:-0}
local=$(echo "$counts"  | awk '$2=="127.0.0.1"{print $1}');   local=${local:-0}
other=$(echo "$counts"  | awk '$2!="localhost" && $2!="127.0.0.1"{s+=$1} END{print s+0}')

msg=":mag: *RAG usage* — ${delta} searches since last readout (cumulative ${cur}). Source split this session: Alfred ${alfred}, local-tests ${local}, other ${other}."
if [ -n "$TOKEN" ]; then
  payload=$(python3 -c "import json,sys; print(json.dumps({'channel':'$CHANNEL','text':sys.argv[1]}))" "$msg")
  resp=$(curl -s -X POST https://slack.com/api/chat.postMessage \
    -H "Authorization: Bearer $TOKEN" -H "Content-type: application/json" \
    -d "$payload")
  slack_ok "$resp" "$CHANNEL" "rag-usage readout"
fi

# --- LOW-USAGE ALERT -> #error-logs (anti-"8 calls" regression detector) ---
# prefetch() fires one /search per Alfred turn, so any active day clears the floor
# easily; the old model-discretion state would fall under it. Only alarms when the
# RAG server is reachable (so a server-down day isn't misread as a reach regression).
ALERT_CHANNEL="$(python3 -c "import yaml,sys;print((yaml.safe_load(open(sys.argv[1])) or {}).get('trading',{}).get('error_channel','') or 'C0XXXXXXXXX')" "$HOME/exitmgr-app/config.yaml" 2>/dev/null || echo C0XXXXXXXXX)"   # #error-logs
LOWUSE_FLOOR="${RAG_LOWUSE_FLOOR:-3}"
rag_up=1; curl -s --max-time 8 "http://${NODE4}:9000/metrics" >/dev/null 2>&1 || rag_up=0
if [ "$rag_up" -eq 1 ] && [ "$delta" -lt "$LOWUSE_FLOOR" ] 2>/dev/null && [ -n "$TOKEN" ]; then
  alert=":rotating_light: *RAG LOW-USAGE* — only ${delta} searches since last readout (floor ${LOWUSE_FLOOR}). prefetch() may be reverted/disabled (Hermes upgrade wipe?) or the gateway isn't running the patched provider. Check: grep -c 'def prefetch' ~/.hermes/hermes-agent/plugins/memory/alfred-rag/__init__.py ; then run ~/scripts/reapply-rag-prefetch.sh"
  apayload=$(python3 -c "import json,sys; print(json.dumps({'channel':'$ALERT_CHANNEL','text':sys.argv[1]}))" "$alert")
  aresp=$(curl -s -X POST https://slack.com/api/chat.postMessage \
    -H "Authorization: Bearer $TOKEN" -H "Content-type: application/json" -d "$apayload")
  slack_ok "$aresp" "$ALERT_CHANNEL" "RAG low-usage alert"
  echo "$(date '+%Y-%m-%d %H:%M %Z') LOW-USAGE ALERT fired (delta=${delta} < ${LOWUSE_FLOOR})"
fi

echo "$(date '+%Y-%m-%d %H:%M %Z') ${msg}"
