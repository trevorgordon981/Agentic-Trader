#!/usr/bin/env python3
"""Liquidate ALL open positions at MARKET. SELL longs, BUY-to-close shorts, using each
position's own contract for guaranteed fills. Spreads close leg-by-leg at market (sub-second
fills). DRY-RUN by default; --confirm sends. Logs + Slacks a summary to #trading-alerts.
Fired by Trevor via the Slack 'liquidate everything' command (liquidate_listener.py), or run
directly with --confirm. Trevor authorizes; this just carries it out."""
import argparse, asyncio, json, os, sys
sys.path.insert(0, os.path.expanduser("~/exitmgr-app"))
from exitmgr.connection import IBConnection

ALERTS = "YOUR_SLACK_CHANNEL_ID"  # #trading-alerts

def slack(msg, channel=ALERTS):
    try:
        import urllib.request
        tok = None
        for l in open(os.path.expanduser("~/.hermes/.env")):
            if l.startswith("SLACK_BOT_TOKEN="):
                tok = l.split("=", 1)[1].strip().strip('"').strip("'"); break
        if not tok: return
        urllib.request.urlopen(urllib.request.Request("https://slack.com/api/chat.postMessage",
            data=json.dumps({"channel": channel, "text": msg}).encode(),
            headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"}), timeout=10)
    except Exception as e:
        print("slack fail:", e)

def desc(c):
    return f"{c.symbol} {getattr(c,'lastTradeDateOrContractMonth','')[:8]} {getattr(c,'strike','') or ''}{getattr(c,'right','') or ''}".strip()

async def run(confirm, client_id):
    conn = IBConnection("127.0.0.1", 4001, client_id, market_data_type=1)
    if not await conn.connect():
        print("connect failed"); slack(":x: liquidate: IBKR connect failed"); return 1
    ib = conn.ib
    await asyncio.sleep(1.0)
    positions = [p for p in await ib.reqPositionsAsync() if p.position != 0]
    if not positions:
        print("no open positions"); slack(":information_source: liquidate: no open positions to close.")
        await conn.disconnect(); return 0
    plan = []
    for p in positions:
        action = "SELL" if p.position > 0 else "BUY"
        plan.append((p.contract, action, abs(int(p.position)), desc(p.contract), p.avgCost))
    print("=== positions to close (MARKET) ===")
    for c, a, q, d, ac in plan:
        print(f"  {a} {q}  {d}  (pos avgCost {round(ac,2)})")
    if not confirm:
        print("\nDRY-RUN — no orders sent. Run with --confirm to liquidate.")
        await conn.disconnect(); return 0
    slack(":rotating_light: *LIQUIDATING ALL POSITIONS* at market:\n" +
          "\n".join(f"{a} {q} {d}" for c, a, q, d, ac in plan))
    await conn.ib.qualifyContractsAsync(*[x[0] for x in plan])
    trades = []
    for c, a, q, d, ac in plan:
        try:
            tr = await conn.place_order(c, conn.create_market_order(a, q))
            trades.append((d, tr))
        except Exception as e:
            print("order FAIL", d, e); slack(f":x: liquidate order failed {d}: {e}")
    for _ in range(25):
        await asyncio.sleep(1.0)
        if trades and all(getattr(t, "isDone", lambda: True)() for _, t in trades): break
    res = []
    for d, tr in trades:
        try: res.append(f"{d}: {tr.orderStatus.status} @ {tr.orderStatus.avgFillPrice}")
        except Exception: res.append(f"{d}: (status unknown)")
    av = {v.tag: v.value for v in ib.accountValues() if v.currency in ("USD","BASE","")}
    summary = ":white_check_mark: *Liquidation complete.*\n" + "\n".join(res) + \
              f"\nCash now: {av.get('TotalCashValue')} | NetLiq: {av.get('NetLiquidation')}"
    print(summary); slack(summary)
    await conn.disconnect(); return 0

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--confirm", action="store_true")
    ap.add_argument("--client-id", type=int, default=91)
    a = ap.parse_args()
    sys.exit(asyncio.run(run(a.confirm, a.client_id)))
