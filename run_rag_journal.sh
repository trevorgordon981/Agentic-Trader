#!/bin/bash
# Extract the day's key facts from Alfred's conversation history into the RAG
# "memory" domain, sync to rag-host, and re-index. Runs every ~2h; each run
# regenerates the day so the journal fills in as the day progresses.
set -euo pipefail
cd "$HOME/exitmgr-app"
NODE4=localhost
BACKFILL="${1:-1}"

python3 rag_journal_builder.py --backfill "$BACKFILL"

if compgen -G "$HOME/rag-journal-stage/journal-*.md" >/dev/null; then
  rsync -az "$HOME"/rag-journal-stage/journal-*.md \
    "rag-host:/path/to/rag-data/memory/" 2>/dev/null || \
  scp -q "$HOME"/rag-journal-stage/journal-*.md "rag-host:/path/to/rag-data/memory/"
  curl -s -X POST "http://${NODE4}:9000/ingest" \
    -H "Content-Type: application/json" -d '{"incremental":true}' >/dev/null
fi
echo "$(date '+%Y-%m-%d %H:%M %Z') rag-journal refresh complete (backfill=$BACKFILL)"
