#!/usr/bin/env python
"""IBKR data-source probe (read-only). Determines whether IB Gateway can supply:
  1. historical OPTION_IMPLIED_VOLATILITY + HISTORICAL_VOLATILITY (true IV for IV-rank)
  2. earnings / expected report dates (CalendarReport, ReportsFinSummary)

Uses the app's IBConnection wrapper (the WORKING connect path -- connectAsync + reqMarketDataType,
NO auto account/positions subscription). Read-only, unused clientId. Does NOT place trades or
restart the gateway. Run from ~/exitmgr-app with ~/ib-grader-venv/bin/python.
"""
import asyncio, sys, os
sys.path.insert(0, os.path.expanduser('~/exitmgr-app'))
from exitmgr.connection import IBConnection
from exitmgr.ibkr import Stock

CLIENT_ID = 97  # unused: app uses 88/93/95/96 (+87/90/94)


def _bars_info(bars):
    if not bars:
        return "0 bars"
    n = len(bars)
    d0 = getattr(bars[0], "date", "?")
    d1 = getattr(bars[-1], "date", "?")
    return f"{n} bars, {d0} -> {d1}"


async def probe_hist(ib, contract, what, durations):
    print(f"\n--- reqHistoricalData whatToShow={what} ---")
    for dur in durations:
        try:
            bars = await ib.reqHistoricalDataAsync(
                contract, endDateTime="", durationStr=dur, barSizeSetting="1 day",
                whatToShow=what, useRTH=True, formatDate=1, timeout=60,
            )
            print(f"  dur={dur:>4}: {_bars_info(bars)}")
            if bars:
                b = bars[-1]
                print(f"            last bar: date={b.date} open={b.open} high={b.high} "
                      f"low={b.low} close={b.close}")
        except Exception as e:
            print(f"  dur={dur:>4}: ERROR {type(e).__name__}: {e}")


async def probe_fundamental(ib, contract, report):
    print(f"\n--- reqFundamentalData report={report} ---")
    try:
        xml = await ib.reqFundamentalDataAsync(contract, report)
        if not xml:
            print("  (empty / None)")
            return
        print(f"  length={len(xml)} chars")
        low = xml.lower()
        for kw in ("earnings", "report", "expected", "fiscal", "announc", "date", "calendar"):
            if kw in low:
                idx = low.find(kw)
                print(f"  keyword '{kw}' @ {idx}: ...{xml[max(0,idx-40):idx+120]}...")
        # dump a chunk so we can eyeball structure
        print("  --- first 1500 chars ---")
        print(xml[:1500])
    except Exception as e:
        print(f"  ERROR {type(e).__name__}: {e}")


async def main():
    conn = IBConnection(host="127.0.0.1", port=4001, client_id=CLIENT_ID, market_data_type=1)
    ok = await conn.connect()
    if not ok:
        print("CONNECT FAILED -- gateway logged out / unreachable")
        return 1
    ib = conn.ib
    print(f"CONNECTED clientId={CLIENT_ID}; accounts={ib.managedAccounts()}")
    try:
        aapl = Stock("AAPL", "SMART", "USD")
        q = await ib.qualifyContractsAsync(aapl)
        aapl = q[0] if q else aapl
        print(f"qualified: conId={aapl.conId} {aapl.symbol} {aapl.primaryExchange}")

        durs = ["1 Y", "5 Y", "15 Y"]
        await probe_hist(ib, aapl, "OPTION_IMPLIED_VOLATILITY", durs)
        await probe_hist(ib, aapl, "HISTORICAL_VOLATILITY", durs)

        await probe_fundamental(ib, aapl, "CalendarReport")
        await probe_fundamental(ib, aapl, "ReportsFinSummary")
    finally:
        await conn.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
