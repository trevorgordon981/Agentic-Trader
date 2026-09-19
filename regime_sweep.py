#!/usr/bin/env python3
"""Public API contract; production-derived narrative omitted."""
import json
import os
import sys
import time
from collections import defaultdict

os.environ["TRADER_REQUIRE_RUNTIME_IDENTITY"] = "1"
os.environ.setdefault("TRADER_STRUCTURED_OUTPUT", "1")
from exitmgr import strategist
from exitmgr.config import load_config

PER_BUCKET = int(sys.argv[1]) if len(sys.argv) > 1 else 12

events = [json.loads(l) for l in open("audit.jsonl")]
pairs, last = [], None
for e in events:
    if e.get("event") == "regime":
        last = e
    elif e.get("event") == "strategist_brief" and last is not None:
        if last.get("regime") in ("bull", "neutral") and e.get("brief"):
            pairs.append((e["ts"][:10], last["regime"], last.get("trend_score"), e["brief"]))


seen, picked = set(), defaultdict(list)
for day, regime, trend, brief in pairs:
    key = (day, regime)
    if key in seen:
        continue
    seen.add(key)
    picked[regime].append((day, trend, brief))

cfg = load_config("config.yaml")
print("endpoint: %s" % cfg.llm_endpoint)
for r in ("bull", "neutral"):
    print("  %-7s distinct days available: %d" % (r, len(picked[r])))

stats = defaultdict(lambda: {"runs": 0, "declines": 0, "intents": 0, "errors": 0,
                             "convs": [], "compliant": 0})
for regime in ("bull", "neutral"):
    for day, trend, brief in picked[regime][:PER_BUCKET]:
        s = stats[regime]
        s["runs"] += 1
        t0 = time.time()
        try:
            intents = strategist.propose_intents(
                endpoint=cfg.llm_endpoint, model=cfg.llm_model, market_context=brief,
                timeout=900, recommend=True)
        except Exception as exc:
            s["errors"] += 1
            print("  %-7s %s trend=%-3s ERROR %s: %s"
                  % (regime, day, trend, type(exc).__name__, str(exc)[:70]), flush=True)
            continue
        el = time.time() - t0
        if not intents:
            s["declines"] += 1
            print("  %-7s %s trend=%-3s %5.0fs DECLINE" % (regime, day, trend, el), flush=True)
            continue
        s["intents"] += len(intents)
        names = []
        for i in intents:
            s["convs"].append(i.conviction)
            lo, hi = 5 * i.intended_hold_days, 8 * i.intended_hold_days
            if i.conviction >= 6 and lo <= i.target_dte <= hi:
                s["compliant"] += 1
            names.append("%s(c%d/%dd)" % (i.underlying, i.conviction, i.target_dte))
        print("  %-7s %s trend=%-3s %5.0fs %d ideas: %s"
              % (regime, day, trend, el, len(intents), " ".join(names)), flush=True)

print("\n=== RESULT")
for regime in ("bull", "neutral"):
    s = stats[regime]
    if not s["runs"]:
        continue
    dec = 100.0 * s["declines"] / s["runs"]
    mc = (sum(s["convs"]) / len(s["convs"])) if s["convs"] else float("nan")
    print("%-7s runs=%-3d declines=%-3d (%.0f%%)  ideas=%-3d  mean_conv=%.1f  compliant=%d/%d  errors=%d"
          % (regime, s["runs"], s["declines"], dec, s["intents"], mc,
             s["compliant"], s["intents"], s["errors"]))
b, n = stats["bull"], stats["neutral"]
if b["runs"] and n["runs"]:
    bd = b["declines"] / b["runs"]
    nd = n["declines"] / n["runs"]
    print("\ndecline rate: bull %.0f%% vs neutral %.0f%%  (gap %+.0f pts)"
          % (100 * bd, 100 * nd, 100 * (nd - bd)))
    print("reading: a LARGE positive gap = regime-conditional selectivity (working as designed).")
    print("         a near-zero gap with both high = the bar is miscalibrated, not the tape.")
