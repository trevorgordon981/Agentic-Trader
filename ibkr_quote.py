#!/usr/bin/env python
"""Read-only delayed quotes for arbitrary US symbols. Prints JSON. Places NO orders, ever.

Run: ~/ib-grader-venv/bin/python ~/exitmgr-app/ibkr_quote.py NVDA TSLA AMD
Delayed data (market_data_type=3, ~15min lag) — free, no IBKR subscription needed.
Uses clientId 86 so it never collides with the trader service (88) or ibkr_status.py (87).
"""
import asyncio
import json
import sys

from ib_async import IB, Stock

HOST, PORT, CLIENT_ID = "127.0.0.1", 4001, 86


async def quotes(symbols):
    ib = IB()
    await ib.connectAsync(HOST, PORT, clientId=CLIENT_ID, timeout=15)
    try:
        ib.reqMarketDataType(3)
        qc = await ib.qualifyContractsAsync(*[Stock(s.upper(), "SMART", "USD") for s in symbols])
        tickers = await ib.reqTickersAsync(*[c for c in qc if getattr(c, "conId", None)])
        def px(v):
            # IB "no data" sentinels: None, NaN, or -1.0 (common after hours)
            return v if (v is not None and v == v and v > 0) else None

        out = {}
        for tk in tickers:
            close = px(tk.close)
            last = px(tk.last) or close
            chg = round((last - close) / close * 100.0, 2) if (last and close) else None
            out[tk.contract.symbol] = {
                "last": last, "close": close, "change_pct": chg,
                "bid": px(tk.bid), "ask": px(tk.ask), "data": "delayed(~15min)",
            }
        missing = set(s.upper() for s in symbols) - set(out)
        if missing:
            out["_unresolved"] = sorted(missing)
        return out
    finally:
        ib.disconnect()


def main():
    syms = [a for a in sys.argv[1:] if a.strip()]
    if not syms:
        print(json.dumps({"error": "usage: ibkr_quote.py SYMBOL [SYMBOL ...]"}))
        return 1
    try:
        out = asyncio.run(quotes(syms))
    except Exception as e:
        out = {"error": f"{type(e).__name__}: {e}"}
    print(json.dumps(out, indent=2, default=str))
    return 0 if "error" not in out else 1


if __name__ == "__main__":
    raise SystemExit(main())
