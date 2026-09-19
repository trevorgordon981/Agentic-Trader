#!/usr/bin/env python3
"""Public API contract; production-derived narrative omitted."""
import json
import os
import sys
import time

os.environ["TRADER_REQUIRE_RUNTIME_IDENTITY"] = "1"
from exitmgr import strategist
from exitmgr.config import load_config

RUNS = int(sys.argv[1]) if len(sys.argv) > 1 else 8
LO, HI = 5, 8

brief = [json.loads(l)["brief"] for l in open("audit.jsonl")
         if json.loads(l).get("event") == "strategist_brief"][-1]
cfg = load_config("config.yaml")

runs_ok = runs_declined = runs_failed = 0
total = compliant = 0
latencies = []
for run in range(RUNS):
    start = time.time()
    try:
        intents, raw = strategist.propose_intents(
            endpoint=cfg.llm_endpoint, model=cfg.llm_model, market_context=brief,
            timeout=2400, recommend=True, return_raw=True)
    except Exception as exc:
        runs_failed += 1
        print("-- run %2d: CONTRACT FAILURE after %.0fs -- %s: %s"
              % (run + 1, time.time() - start, type(exc).__name__, exc), flush=True)
        continue
    elapsed = time.time() - start
    latencies.append(elapsed)
    if not intents:
        runs_declined += 1
        print("-- run %2d: %.0fs, declined" % (run + 1, elapsed), flush=True)
        continue
    runs_ok += 1
    print("-- run %2d: %.0fs, %d intents" % (run + 1, elapsed, len(intents)), flush=True)
    for i in intents:
        lo, hi = LO * i.intended_hold_days, HI * i.intended_hold_days
        ok = i.conviction >= 6 and lo <= i.target_dte <= hi
        total += 1
        compliant += 1 if ok else 0
        print("   [%s] %-5s %-18s conv=%d dte=%-4d hold=%-3d window=%d-%d"
              % ("OK " if ok else "BAD", i.underlying, i.structure, i.conviction,
                 i.target_dte, i.intended_hold_days, lo, hi), flush=True)

print("\n== runs: %d proposed / %d declined / %d contract-failed (of %d)"
      % (runs_ok, runs_declined, runs_failed, RUNS))
print("== doctrine: %d/%d intents compliant" % (compliant, total))
if latencies:
    print("== latency: median %.0fs, max %.0fs" % (sorted(latencies)[len(latencies) // 2],
                                                   max(latencies)))
