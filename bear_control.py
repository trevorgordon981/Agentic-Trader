#!/usr/bin/env python3
"""Public API contract; production-derived narrative omitted."""
import importlib.util, json, os, sys
os.environ["TRADER_REQUIRE_RUNTIME_IDENTITY"] = "1"
os.environ.setdefault("TRADER_STRUCTURED_OUTPUT", "1")
from exitmgr.config import load_config
import bear_probe as probe

from importlib.machinery import SourceFileLoader
_bak = os.path.expanduser("~/exitmgr-app/exitmgr/strategist.py.bak-pre-bearish-20260811")
spec = importlib.util.spec_from_loader("old_strategist", SourceFileLoader("old_strategist", _bak))
old = importlib.util.module_from_spec(spec)
sys.modules["old_strategist"] = old
spec.loader.exec_module(old)

cfg = load_config("config.yaml")
print("OLD prompt chars:", len(old.STAGE_A_SYSTEM_PROMPT),
      "| has EITHER DIRECTION:", "EITHER DIRECTION" in old.STAGE_A_SYSTEM_PROMPT)
label, ctx = probe.VARIANTS[0]
bear = bull = declined = 0
for _ in range(4):
    try:
        intents = old.propose_intents(endpoint=cfg.llm_endpoint, model=cfg.llm_model,
                                      market_context=ctx, timeout=900, recommend=True)
    except Exception as exc:
        print("  ERR", type(exc).__name__, str(exc)[:60]); continue
    if not intents:
        declined += 1; continue
    for i in intents:
        if i.direction == "bearish": bear += 1
        else: bull += 1
print("OLD prompt, %-14s runs=4  bearish=%d bullish=%d declined=%d" % (label, bear, bull, declined))
