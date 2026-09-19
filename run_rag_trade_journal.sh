#!/bin/bash
set -euo pipefail
cd "$HOME/exitmgr-app"
RAG_HOST=127.0.0.1

python3 rag_trade_journal.py

rsync -az \
  "$HOME"/rag-trading-stage/trades-*.md \
  "rag-host:/home/rag-service/alfred-rag-active/data/trading/" 2>/dev/null || \
scp -q "$HOME"/rag-trading-stage/trades-*.md "rag-host:/home/rag-service/alfred-rag-active/data/trading/"

if [ "${RAG_SKIP_INGEST:-0}" = "1" ]; then
  echo "$(date '+%Y-%m-%d %H:%M %Z') rag-trade-journal: files pushed; ingest deferred to caller"
else
  curl -s -X POST "http://${RAG_HOST}:9000/ingest" \
    -H "Content-Type: application/json" -d '{"incremental":true}' >/dev/null
fi
echo "$(date '+%Y-%m-%d %H:%M %Z') rag-trade-journal refresh complete"
