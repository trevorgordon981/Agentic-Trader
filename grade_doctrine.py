#!/usr/bin/env python3
"""Public API contract; production-derived narrative omitted."""
import json
import os
import sys
import time

os.environ["TRADER_REQUIRE_RUNTIME_IDENTITY"] = "1"
from exitmgr import strategist
from exitmgr.config import load_config

DTE_WINDOW = (5, 8)
RUNS = int(sys.argv[1]) if len(sys.argv) > 1 else 3

briefs = [json.loads(l)["brief"] for l in open("audit.jsonl")
          if json.loads(l).get("event") == "strategist_brief"]
brief = briefs[-1]
cfg = load_config("config.yaml")
print("endpoint: %s" % cfg.llm_endpoint)
print("model:    %s" % cfg.llm_model)
print("brief:    %d chars (production, %d available)" % (len(brief), len(briefs)))

total = compliant = 0
for run in range(RUNS):
    start = time.time()
    try:
        intents, raw = strategist.propose_intents(endpoint=cfg.llm_endpoint, model=cfg.llm_model,
                                                  market_context=brief, timeout=900,
                                                  return_raw=True)
    except Exception as exc:
        print("-- run %d: FAILED %s: %s" % (run + 1, type(exc).__name__, exc))
        continue
    print("-- run %d: %.0fs, %d intents%s"
          % (run + 1, time.time() - start, len(intents),
             "  RAW=" + repr(raw[:120]) if not intents else ""))
    for intent in intents:
        lo = DTE_WINDOW[0] * intent.intended_hold_days
        hi = DTE_WINDOW[1] * intent.intended_hold_days
        need = "%d-%d" % (lo, hi)
        ok = intent.conviction >= 6 and lo <= intent.target_dte <= hi
        total += 1
        compliant += 1 if ok else 0
        print("   [%s] %-5s %-18s conv=%-2d dte=%-4d hold=%-3d window=%s"
              % ("OK " if ok else "BAD", intent.underlying, intent.structure,
                 intent.conviction, intent.target_dte, intent.intended_hold_days, need))
print("== doctrine adherence: %d/%d intents compliant" % (compliant, total))
