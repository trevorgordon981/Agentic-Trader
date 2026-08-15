#!/bin/bash
# Extract the day's key facts from Alfred's conversation history into the RAG
# "memory" domain, sync to rag-host, and re-index. Runs every ~2h; each run
# regenerates the day so the journal fills in as the day progresses.
set -euo pipefail
cd "$HOME/exitmgr-app"
# rag-host hosts the RAG API; this curl runs FROM studio, so localhost was wrong and
# curl exited 7 (couldn-t connect) which set -e turned into the job exiting 7.
# rag-news is unaffected because it runs its curl through ssh, where localhost is right.
NODE4=localhost
BACKFILL="${1:-1}"

python3 rag_journal_builder.py --backfill "$BACKFILL"

# Ground-truth system state. The journal above is built from CONVERSATIONS, so an
# architecture change only reaches Alfred's memory if it happened to be discussed --
# which is how he came to report a deliberately-stopped :8082 as a "crashed strategist".
# This introspects the LIVE system instead and refreshes the facts every run.
"$HOME/ib-grader-venv/bin/python" alfred_system_state.py || \
  echo "[WARN] system-state doc failed (continuing)" >&2

if compgen -G "$HOME/rag-journal-stage/journal-*.md" >/dev/null; then
  rsync -az "$HOME"/rag-journal-stage/journal-*.md "$HOME"/rag-journal-stage/alfred-system-state-*.md \
    "rag-host:/path/to/rag-data/memory/" 2>/dev/null || \
  scp -q "$HOME"/rag-journal-stage/journal-*.md "$HOME"/rag-journal-stage/alfred-system-state-*.md "rag-host:/path/to/rag-data/memory/"
  curl -s -X POST "http://${NODE4}:9000/ingest" \
    -H "Content-Type: application/json" -d '{"incremental":true}' >/dev/null
fi
echo "$(date '+%Y-%m-%d %H:%M %Z') rag-journal refresh complete (backfill=$BACKFILL)"
