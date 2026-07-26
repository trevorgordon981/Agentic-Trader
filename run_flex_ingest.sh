#!/bin/bash
# Auto-fold IBKR Flex trade history into the v4 trade dataset.
# READ-ONLY (HTTPS GET to IBKR Flex web service) + idempotent (dedup by execID) + never raises.
# Scheduled via ~/Library/LaunchAgents/ai.alfred.flex-ingest.plist. Safe to run any time.
cd /path/to/home || exit 1
PY=/path/to/home
mkdir -p /path/to/home
echo "=== flex-ingest $(date '+%Y-%m-%d %H:%M %Z') ==="
"$PY" -c "
from exitmgr import flex_ingest as fx
import json
s = fx.ingest_flex()
r = s.get('reconcile') or {}
print(json.dumps({
    'ok': s.get('ok'), 'note': s.get('note'),
    'fills': s.get('fills'), 'contracts': s.get('contracts'),
    'flex_trade_rows': s.get('flex_trade_rows'),
    'existing': r.get('existing'), 'appended_trades': r.get('appended_trades'),
    'superseded': r.get('superseded'), 'skipped_execdup': r.get('skipped_execdup'),
    'final_rows': r.get('final_rows'),
}, default=str))
"
