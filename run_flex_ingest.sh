#!/bin/bash
cd /opt/agentic-trader/exitmgr-app || exit 1
PY=/opt/agentic-trader/.pyenv/versions/3.12.13/bin/python
mkdir -p /opt/agentic-trader/.hermes/logs
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
