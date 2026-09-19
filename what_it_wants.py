#!/usr/bin/env python3
"""Public API contract; production-derived narrative omitted."""
import json
import os
import sys
import time

os.environ["TRADER_REQUIRE_RUNTIME_IDENTITY"] = "1"
os.environ.setdefault("TRADER_STRUCTURED_OUTPUT", "1")
sys.path.insert(0, os.path.expanduser("~/exitmgr-app"))
os.chdir(os.path.expanduser("~/exitmgr-app"))

from exitmgr import strategist
from exitmgr.config import load_config

DEADLINE = time.time() + 75 * 60


def latest_brief():
    ts = brief = None
    for line in open("audit.jsonl"):
        try:
            e = json.loads(line)
        except Exception:
            continue
        if e.get("event") == "strategist_brief" and e.get("brief"):
            ts, brief = e["ts"], e["brief"]
    return ts, brief


def has_structure(b):
    i = b.lower().find("price structure")
    return i >= 0 and "unavailable" not in b[i:i + 140].lower()


print("waiting for a brief with real price structure...", flush=True)
seen = None
while time.time() < DEADLINE:
    ts, b = latest_brief()
    if b and ts != seen:
        seen = ts
        print("  %s  price_structure=%s" % (ts[11:19], "YES" if has_structure(b) else "no"), flush=True)
        if has_structure(b):
            break
    time.sleep(45)
else:
    print("timed out waiting for populated market data")
    sys.exit(1)

cfg = load_config("config.yaml")
print("\nasking %s what it wants to trade...\n" % cfg.llm_model, flush=True)
t0 = time.time()
intents, raw, cot = strategist.propose_intents(
    endpoint=cfg.llm_endpoint, model=cfg.llm_model, market_context=b,
    timeout=900, recommend=True, return_raw=True, return_cot=True)
el = time.time() - t0

print("=" * 68)
if not intents:
    print("VERDICT: PASS — no trade wanted (%.0fs)" % el)
else:
    print("VERDICT: %d idea(s) (%.0fs)" % (len(intents), el))
for i in intents:
    lo, hi = 5 * i.intended_hold_days, 8 * i.intended_hold_days
    ok = i.conviction >= 6 and lo <= i.target_dte <= hi
    print("\n  %s  %s %s" % (i.underlying, i.direction, i.structure))
    print("    conviction %d | dte %d (hold %dd, window %d-%d) %s"
          % (i.conviction, i.target_dte, i.intended_hold_days, lo, hi,
             "OK" if ok else "** OUT OF DOCTRINE **"))
    print("    delta %s | alloc %s%% of net liq" % (i.target_delta, i.allocation_pct_net_liq))
    print("    alpha : %s" % (i.alpha or "")[:200])
    print("    thesis: %s" % (i.thesis or "")[:400])
print("=" * 68)
if cot:
    print("\nWHY (model's own reasoning, first 1200 chars):\n%s" % cot[:1200])
