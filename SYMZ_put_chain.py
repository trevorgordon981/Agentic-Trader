#!/usr/bin/env python
"""Public API contract; production-derived narrative omitted."""
import asyncio
import json
import math
import os
import sys
from datetime import date, datetime

from ib_async import IB, Option, Stock

HOST, PORT, CLIENT_ID = "127.0.0.1", 4001, 113
SYM = os.environ.get("CHAIN_SYM", "SYMZ")
TARGET_YIELD = float(os.environ.get("TARGET_YIELD", "0.01"))
DELTA_LO, DELTA_HI = 0.10, 0.20


def _n(v):
    """Public API contract; production-derived narrative omitted."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or f == -1.0:
        return None
    return f


async def main():
    ib = IB()
    await ib.connectAsync(HOST, PORT, clientId=CLIENT_ID, timeout=20)
    try:

        ib.reqMarketDataType(int(os.environ.get("MKT_DATA_TYPE", "1")))
        (stk,) = await ib.qualifyContractsAsync(Stock(SYM, "SMART", "USD"))
        (t,) = await ib.reqTickersAsync(stk)
        spot = _n(t.marketPrice()) or _n(t.close) or _n(t.last)
        print("  %s spot: %s" % (SYM, ("$%.2f" % spot) if spot else "UNAVAILABLE"))
        if not spot:
            print("  cannot proceed without a spot price")
            return

        params = await ib.reqSecDefOptParamsAsync(SYM, "", "STK", stk.conId)
        smart = [p for p in params if p.exchange == "SMART"] or params
        if not smart:
            print("  no option params returned")
            return
        p = smart[0]
        today = date.today()
        exps = sorted(e for e in p.expirations
                      if datetime.strptime(e, "%Y%m%d").date() >= today)
        if not exps:
            print("  no forward expirations")
            return
        exp = exps[0]
        dte = (datetime.strptime(exp, "%Y%m%d").date() - today).days
        print("  nearest expiry: %s (%d DTE)  |  %d strikes available" % (exp, dte, len(p.strikes)))


        strikes = sorted(s for s in p.strikes if 0.60 * spot <= s <= 1.02 * spot)
        conts = [Option(SYM, exp, s, "P", "SMART", tradingClass=p.tradingClass) for s in strikes]
        conts = await ib.qualifyContractsAsync(*conts)
        conts = [c for c in conts if getattr(c, "conId", None)]
        if not conts:
            print("  no qualified put contracts")
            return
        tick = await ib.reqTickersAsync(*conts)
        await asyncio.sleep(3)

        print()
        print("  %-9s %7s %8s %8s %9s %11s %8s  %s"
              % ("strike", "delta", "bid", "ask", "mid", "collateral", "yield", "target"))
        print("  " + "-" * 78)
        rows = []
        for tk in tick:
            k = tk.contract.strike
            g = tk.modelGreeks
            delta = abs(_n(getattr(g, "delta", None))) if g else None
            bid, ask = _n(tk.bid), _n(tk.ask)
            mid = (bid + ask) / 2 if (bid is not None and ask is not None) else (bid or ask)
            if mid is None:
                continue
            coll = k * 100
            y = (mid * 100) / coll if coll else 0
            rows.append({"strike": k, "delta": delta, "bid": bid, "ask": ask,
                         "mid": mid, "collateral": coll, "yield": y})
            in_band = delta is not None and DELTA_LO <= delta <= DELTA_HI
            mark = ("<== in 0.10-0.20 band" if in_band else "")
            if in_band and y >= TARGET_YIELD:
                mark = "<== IN BAND AND CLEARS %.1f%%" % (TARGET_YIELD * 100)
            print("  %-9.2f %7s %8s %8s %9s %11s %7.2f%%  %s"
                  % (k, ("%.3f" % delta) if delta is not None else "n/a",
                     ("%.2f" % bid) if bid is not None else "-",
                     ("%.2f" % ask) if ask is not None else "-",
                     "%.2f" % mid, "$%s" % format(int(coll), ","), 100 * y, mark))

        band = [r for r in rows if r["delta"] is not None and DELTA_LO <= r["delta"] <= DELTA_HI]
        print()
        if not band:
            print("  NOTHING in the 0.10-0.20 delta band (greeks may be unavailable on delayed data)")
        else:
            best = max(band, key=lambda r: r["yield"])
            print("  best in band: strike $%.2f delta %.3f mid $%.2f -> %.2f%% on $%s collateral"
                  % (best["strike"], best["delta"], best["mid"], 100 * best["yield"],
                     format(int(best["collateral"]), ",")))
            print("  %s the %.1f%% target"
                  % ("CLEARS" if best["yield"] >= TARGET_YIELD else "DOES NOT CLEAR",
                     TARGET_YIELD * 100))
        json.dump(rows, open("/tmp/SYMZ_chain.json", "w"), indent=1, default=str)
        print("  rows -> /tmp/SYMZ_chain.json")
    finally:
        ib.disconnect()


asyncio.run(main())
