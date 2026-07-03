#!/usr/bin/env python3
"""Offline self-test for the 2026-07-01 trade-construction rework.
Exercises the new gate functions with synthetic ideas -- NO IBKR, NO model, NO services.
Run: python3 selftest_construction.py <path-to-exitmgr-app>
"""
import sys
from datetime import date

sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else ".")

from exitmgr.config import ConstructionConfig, construction_from_dict
from exitmgr import construction as C
from exitmgr.trader import pick_spread_short

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    ok = bool(cond)
    PASS += ok
    FAIL += (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


cons = ConstructionConfig()
today = date(2026, 7, 1)
# synthetic chain: weeklies out to ~60 days
chain = ["20260703", "20260710", "20260717", "20260724", "20260731",
         "20260807", "20260814", "20260828"]

print("== A1: min-DTE floor (25, prefer 25-45) ==")
exp, dte, adj = C.pick_expiry(chain, target_dte=10, min_dte=25, prefer_dte_max=45, today=today)
check("too-short target (10 DTE) ADJUSTED up", exp == "20260731" and dte == 30 and adj,
      f"chose {exp} ({dte} DTE), adjusted={adj}")
exp2, dte2, adj2 = C.pick_expiry(chain, target_dte=30, min_dte=25, prefer_dte_max=45, today=today)
check("in-band target (30 DTE) NOT adjusted", exp2 == "20260731" and dte2 == 30 and not adj2,
      f"chose {exp2} ({dte2} DTE), adjusted={adj2}")
exp3, dte3, adj3 = C.pick_expiry(["20260703", "20260710"], 10, 25, 45, today=today)
check("no expiry >= 25 DTE -> REJECT (None)", exp3 is None, f"got {exp3}")
exp4, dte4, adj4 = C.pick_expiry(chain, target_dte=90, min_dte=25, prefer_dte_max=45, today=today)
check("too-long target (90) pulled toward 45", dte4 == 44 and adj4, f"chose {exp4} ({dte4} DTE)")

print("== A2: TP/SL clamp (band 25-35, defaults +30/-30) ==")
tp, sl = C.clamp_tp_sl(100.0, 50.0, cons)  # the OLD defaults, straight from the audit
check("model TP 100% clamped to 35%", tp == 35.0, f"tp={tp}")
check("model SL 50% tightened to 30%", sl == 30.0, f"sl={sl}")
tp, sl = C.clamp_tp_sl(0, 0, cons)
check("missing levels -> defaults +30/-30", tp == 30.0 and sl == 30.0, f"tp={tp} sl={sl}")
tp, sl = C.clamp_tp_sl(20.0, 20.0, cons)
check("TP below band raised to 25; tighter SL 20 kept", tp == 25.0 and sl == 20.0, f"tp={tp} sl={sl}")

print("== A3: structure sanity ==")
# NOK case from the audit: $13.62 stock, 14/25 call vertical, 15 DTE, IV ~0.45
ok, why = C.spread_structure_ok(14.0, 25.0, 13.62, "C", 15, 0.45, cons)
check("NOK 14/25 lottery vertical REJECTED (IV path)", not ok, why)
ok, why = C.spread_structure_ok(14.0, 15.0, 13.62, "C", 30, 0.45, cons)
check("sane 14/15 vertical on $13.62 ACCEPTED", ok, why or "ok")
ok, why = C.spread_structure_ok(100.0, 112.0, 100.0, "C", 30, None, cons)
check("no-IV fallback: 12%-wide spread REJECTED (>8% of spot)", not ok, why)
ok, why = C.spread_structure_ok(100.0, 106.0, 100.0, "C", 30, None, cons)
check("no-IV fallback: 6%-wide ATM spread ACCEPTED", ok, why or "ok")
# SNDK case: needed +3.5% in 15d just to reach the LONG strike (low IV -> tiny EM)
ok, why = C.long_strike_ok(103.5, 100.0, "C", 15, 0.15, cons)
check("far-OTM long strike (SNDK-style) REJECTED", not ok, why)
ok, why = C.long_strike_ok(98.0, 100.0, "C", 30, 0.30, cons)
check("ITM long strike ACCEPTED", ok, why or "ok")
check("delta band: 0.35 -> 0.55, 0.9 -> 0.65, 0.6 kept",
      C.effective_delta(0.35, cons) == 0.55 and C.effective_delta(0.9, cons) == 0.65
      and C.effective_delta(0.60, cons) == 0.60)
# pick_spread_short with the sanity filter: $13.62 spot, long 14C @ .70, IV .45, 30 DTE
cands = [(15.0, 0.45), (17.0, 0.20), (20.0, 0.08), (25.0, 0.02)]
pick = pick_spread_short(cands, 14.0, 0.70, "C", 500.0, spot=13.62, dte=30, atm_iv=0.45, cons=cons)
check("pick_spread_short skips lottery shorts (25/20/17), takes 15",
      pick is not None and pick[0] == 15.0, f"pick={pick}")
pick_legacy = pick_spread_short(cands, 14.0, 0.70, "C", 500.0)
check("legacy call (no cons) unchanged -> widest affordable (25)",
      pick_legacy is not None and pick_legacy[0] == 25.0, f"pick={pick_legacy}")

print("== A4: budget gates (net_liq=$1000) ==")
ok, why = C.check_budget(300.0, 30, 1000.0, [], cons)  # 30% of pot in one trade
check("oversized premium (30% > 15%) REJECTED", not ok, "; ".join(why))
ok, why = C.check_budget(140.0, 30, 1000.0, [], cons)  # 14%, 30 DTE -> 0.47%/day
check("sane trade (14%, 30 DTE) ACCEPTED", ok, "; ".join(why) or "ok")
ok, why = C.check_budget(140.0, 30, 1000.0, [(150.0, 20), (150.0, 25)], cons)
check("deployed cap: 15+15+14=44% > 40% REJECTED", not ok, "; ".join(why))
ok, why = C.check_budget(150.0, 10, 1000.0, [], cons)  # 15%/10d = 1.5%/day decay
check("decay budget: 1.5%/day > 1%/day REJECTED", not ok, "; ".join(why))
ok, why = C.check_budget(100.0, 30, 1000.0, [(120.0, 6), (100.0, 4)], cons)
check("portfolio decay: 0.33+2.0+2.5 = 4.83%/day > 4% REJECTED", not ok, "; ".join(why))
ok, why = C.check_budget(100.0, 30, 1000.0, [(120.0, 8), (100.0, 6)], cons)
check("portfolio decay: 3.5%/day < 4% ACCEPTED", ok, "; ".join(why) or "ok")
check("max_premium_budget($1000) == $150", C.max_premium_budget(1000.0, cons) == 150.0)
ok, why = C.check_budget(300.0, 30, 0.0, [], cons)
check("unknown net-liq -> pass-through (other gates bind)", ok)

print("== config plumbing ==")
c2 = construction_from_dict({"min_dte": 30, "bogus_key": 1, "tp_pct": 0.28})
check("construction_from_dict: overrides applied, unknown keys ignored",
      c2.min_dte == 30 and c2.tp_pct == 0.28 and c2.max_premium_pct == 0.15)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
