#!/usr/bin/env python
"""Public API contract; production-derived narrative omitted."""
import asyncio

from ib_async import IB, Stock

KNOWN_ETF = ["SPY", "QQQ", "IWM", "SMH", "SOXL", "SYMX", "SCHD", "XLF", "XLK", "XLV", "DRAQ"]
KNOWN_SINGLE = ["NVDA", "AMD", "SYMZ", "SOFI", "SYMD", "TSM", "ASTS", "COHR"]


async def main():
    ib = IB()
    await ib.connectAsync("127.0.0.1", 4001, clientId=115, timeout=20)
    try:
        print("  %-7s %-10s %-14s %s" % ("symbol", "secType", "stockType", "expected"))
        print("  " + "-" * 52)
        for group, expect in ((KNOWN_ETF, "ETF"), (KNOWN_SINGLE, "single")):
            for sym in group:
                try:
                    cds = await asyncio.wait_for(
                        ib.reqContractDetailsAsync(Stock(sym, "SMART", "USD")), 15)
                except Exception as e:
                    print("  %-7s %s" % (sym, str(e)[:40]))
                    continue
                if not cds:
                    print("  %-7s (no contract details)" % sym)
                    continue
                cd = cds[0]
                st = (getattr(cd, "stockType", "") or "").strip()
                print("  %-7s %-10s %-14s %s"
                      % (sym, cd.contract.secType, st or "(empty)", expect))
            print()
    finally:
        ib.disconnect()


asyncio.run(main())
