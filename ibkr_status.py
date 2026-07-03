#!/usr/bin/env python
"""Read-only IBKR account snapshot. Prints JSON. Places NO orders, ever.

Run: ~/ib-grader-venv/bin/python ~/exitmgr-app/ibkr_status.py
Uses clientId 87 so it never collides with the trader service (clientId 88).
"""
import asyncio
import json

from ib_async import IB

HOST, PORT, CLIENT_ID = "127.0.0.1", 4001, 87
SUMMARY_TAGS = {
    "NetLiquidation", "TotalCashValue", "AvailableFunds",
    "BuyingPower", "UnrealizedPnL", "RealizedPnL",
}


async def snapshot():
    ib = IB()
    await ib.connectAsync(HOST, PORT, clientId=CLIENT_ID, timeout=15)
    try:
        account = {
            v.tag: v.value
            for v in await ib.accountSummaryAsync()
            if v.tag in SUMMARY_TAGS
        }
        positions = [
            {
                "symbol": p.contract.symbol,
                "secType": p.contract.secType,
                "right": getattr(p.contract, "right", "") or "",
                "strike": getattr(p.contract, "strike", 0.0) or 0.0,
                "expiry": getattr(p.contract, "lastTradeDateOrContractMonth", "") or "",
                "position": p.position,
                "avgCost": p.avgCost,
            }
            for p in await ib.reqPositionsAsync()
        ]
        await ib.reqAllOpenOrdersAsync()
        open_orders = [
            {
                "orderId": t.order.orderId,
                "symbol": t.contract.symbol,
                "action": t.order.action,
                "qty": t.order.totalQuantity,
                "orderType": t.order.orderType,
                "lmtPrice": t.order.lmtPrice,
                "status": t.orderStatus.status,
            }
            for t in ib.openTrades()
        ]
    finally:
        ib.disconnect()
    return {"connected": True, "account": account,
            "positions": positions, "open_orders": open_orders}


def main():
    try:
        out = asyncio.run(snapshot())
    except Exception as e:
        out = {"connected": False, "error": f"{type(e).__name__}: {e}"}
    print(json.dumps(out, indent=2, default=str))
    return 0 if out.get("connected") else 1


if __name__ == "__main__":
    raise SystemExit(main())
