#!/bin/bash
# Regenerate the trading-journal RAG corpus from the live exitmgr audit trail,
# sync it to the RAG box (rag-host), and trigger an incremental re-index.
set -euo pipefail
cd "$HOME/exitmgr-app"
NODE4=localhost

python3 rag_trade_journal.py

# Push fresh dated journal files into rag-host's trading domain. No --delete:
# the manual Fidelity journals live here too, and dated files only accumulate.
rsync -az \
  "$HOME"/rag-trading-stage/trades-*.md \
  "rag-host:/path/to/rag-data/trading/" 2>/dev/null || \
scp -q "$HOME"/rag-trading-stage/trades-*.md "rag-host:/path/to/rag-data/trading/"

# Incremental re-index (cheap: only changed chunks are re-embedded).
curl -s -X POST "http://${NODE4}:9000/ingest" \
  -H "Content-Type: application/json" -d '{"incremental":true}' >/dev/null
echo "$(date '+%Y-%m-%d %H:%M %Z') rag-trade-journal refresh complete"
