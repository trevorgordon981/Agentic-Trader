#!/usr/bin/env python3
"""Public API contract; production-derived narrative omitted."""
import json
import os
import sys

os.environ["TRADER_REQUIRE_RUNTIME_IDENTITY"] = "1"
os.environ.setdefault("TRADER_STRUCTURED_OUTPUT", "1")
from exitmgr import strategist
from exitmgr.config import load_config

cfg = load_config("config.yaml")
N = int(sys.argv[1]) if len(sys.argv) > 1 else 3

BASE = """Date (UTC): 2025-03-14
Account sizing snapshot (synthetic): net liquidation $10,000; available funds $10,000.

Broad tape:
  SPY: 512.40 (-1.85% today, -7.9% over 20 sessions, below its 50-day)
  QQQ: 431.10 (-2.410% today, -11.2% over 20 sessions, below its 50-day)

Single names (last, day change, 20-session change):
  META: 486.20 (-3.10%, -18.4% over 20 sessions; lower highs and lower lows since Feb 18)
  AAPL: 172.05 (-2.40%, -12.1% over 20 sessions; broke its 200-day on volume)
  NVDA: 104.80 (-4.20%, -22.6% over 20 sessions; five straight lower weekly closes)
  MUTX:    82.15 (-3.80%, -19.9% over 20 sessions; downtrend intact)
"""

VARIANTS = [
    ("neutral ask", BASE + "\nRecommend your best trade or an empty slate."),
    ("names the downtrend", BASE + "\nEvery name above is in a confirmed, tape-confirmed "
                                   "DOWNTREND. Recommend your best trade or an empty slate."),
    ("explicit invitation", BASE + "\nEvery name above is in a confirmed, tape-confirmed "
                                   "DOWNTREND, and the broad tape agrees. Per ENTRY DISCIPLINE a "
                                   "confirmed downtrend supports a BEARISH idea (put debit spread "
                                   "or long put) with the same standing as a bullish one. "
                                   "Recommend your best trade or an empty slate."),
]

print("endpoint: %s\n" % cfg.llm_endpoint)
for label, ctx in VARIANTS:
    bull = bear = declined = 0
    detail = []
    for _ in range(N):
        try:
            intents = strategist.propose_intents(
                endpoint=cfg.llm_endpoint, model=cfg.llm_model, market_context=ctx,
                timeout=900, recommend=True)
        except Exception as exc:
            detail.append("ERR:%s" % type(exc).__name__)
            continue
        if not intents:
            declined += 1
            continue
        for i in intents:
            if i.direction == "bearish":
                bear += 1
            else:
                bull += 1
            detail.append("%s/%s/%s" % (i.underlying, i.direction, i.structure))
    print("%-22s runs=%d  bearish=%-2d bullish=%-2d declined=%-2d  %s"
          % (label, N, bear, bull, declined, " ".join(detail[:6])))
