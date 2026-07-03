#!/usr/bin/env bash
# Refresh the Alfred RAG "news" domain end-to-end:
#   1. fetch fresh Google-News headlines into ~/rag-news-stage/ (self-pruning 14d window)
#   2. mirror that staging dir to rag-host's news corpus (--delete enforces the rolling window)
#   3. trigger an incremental ingest on rag-host so the new digest is searchable
# Staged news stays opt-in in the RAG server (domain="news").
set -euo pipefail

STAGE="$HOME/rag-news-stage"
NODE4_NEWS="/path/to/rag-data/news/"

echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] rag-news: fetching headlines"
python3 "$HOME/exitmgr-app/rag_news_fetch.py"

echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] rag-news: syncing staging -> rag-host (--delete = rolling window)"
rsync -az --delete "$STAGE/" "rag-host:${NODE4_NEWS}"

echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] rag-news: triggering incremental ingest on rag-host"
# Lock-serialized ingest: retry if another job holds the lock. Track whether we
# actually triggered one — don't report "done" if all attempts were blocked
# (the old behavior silently looked successful while never ingesting).
ingest_started=0
for attempt in 1 2 3 4 5 6 7 8; do
  resp="$(ssh rag-host "curl -s -X POST http://localhost:9000/ingest -H 'Content-Type: application/json' -d '{\"incremental\":true}'")"
  echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] rag-news: ingest attempt ${attempt}: ${resp}"
  if [[ "$resp" == *\"started\"* || "$resp" == *\"queued\"* ]]; then
    ingest_started=1; break
  fi
  if [[ "$resp" != *already_running* ]]; then
    break  # some other non-retryable response; stop
  fi
  sleep 45
done

if [[ "$ingest_started" -eq 1 ]]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] rag-news: done (ingest triggered)"
else
  echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] rag-news: WARNING — ingest never triggered (lock held every attempt). The fresh digest is staged on rag-host and will be picked up by the next ingest, but it is NOT indexed yet." >&2
fi
