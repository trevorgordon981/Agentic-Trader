#!/usr/bin/env bash
set -euo pipefail

STAGE="$HOME/rag-news-stage"
RAG_HOST_NEWS="/home/rag-service/alfred-rag-active/data/news/"

echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] rag-news: fetching headlines"
python3 "$HOME/exitmgr-app/rag_news_fetch.py"

echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] rag-news: syncing staging -> rag-host (--delete = rolling window)"
rsync -az --delete "$STAGE/" "rag-host:${RAG_HOST_NEWS}"

echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] rag-news: triggering incremental ingest on rag-host"
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
