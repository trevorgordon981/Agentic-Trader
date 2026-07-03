#!/usr/bin/env python3
"""Liquidate ALL open positions at MARKET. SELL longs, BUY-to-close shorts, using each
position's own contract for guaranteed fills. Spreads close leg-by-leg at market (sub-second
fills). DRY-RUN by default; --confirm sends. Logs + Slacks a summary to #trading-alerts.
Fired by Trevor via the Slack 'liquidate everything' command (liquidate_listener.py), or run
directly with --confirm. Trevor authorizes; this just carries it out."""
import argparse, asyncio, json, os, sys
sys.path.insert(0, os.path.expanduser("~/exitmgr-app"))
from exitmgr.connection import IBConnection

ALERTS = "C0XXXXXXXXX"  # #trading-alerts

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

async def cancel_resting_closes(ib, con_ids):
    """Cancel any resting order (esp. the exit manager's SELL-to-close orders, on another clientId)
    that touches a contract we are about to close at market. Without this, the tool's market close
    AND a resting exit-manager SELL can BOTH fill -> the position is over-sold / the short leg left
    naked (the 6/29 double-close family). Matches single-leg orders by conId and combo (BAG) orders
    by any leg's conId. Returns the count cancelled. Never raises (best-effort)."""
    con_ids = {int(c) for c in con_ids if c is not None}
    n = 0
    if not con_ids:
        return 0
    try:
        try:
            trades = await ib.reqAllOpenOrdersAsync()   # all clientIds (the trader loop's orders)
        except Exception:
            trades = await ib.reqOpenOrdersAsync()
        for t in trades or []:
            o = getattr(t, "order", None); c = getattr(t, "contract", None)
            if o is None or c is None:
                continue
            legs = set()
            cid = getattr(c, "conId", None)
            if cid:
                legs.add(int(cid))
            for leg in (getattr(c, "comboLegs", None) or []):
                lc = getattr(leg, "conId", None)
                if lc:
                    legs.add(int(lc))
            if legs & con_ids:
                try:
                    ib.cancelOrder(o)
                    n += 1
                    print(f"[CANCEL] resting order id={getattr(o,'orderId','?')} "
                          f"action={getattr(o,'action','?')} on con_ids={sorted(legs & con_ids)}")
                except Exception as e:
                    print("cancel FAIL", getattr(o, "orderId", "?"), e)
    except Exception as e:
        print("cancel_resting_closes error:", e)
    return n

async def run(confirm, client_id, symbol=None):
    conn = IBConnection("127.0.0.1", 4001, client_id, market_data_type=1)
    if not await conn.connect():
        print("connect failed"); slack(":x: liquidate: IBKR connect failed"); return 1
    ib = conn.ib
    await asyncio.sleep(1.0)
    positions = [p for p in await ib.reqPositionsAsync() if p.position != 0 and (not symbol or p.contract.symbol == symbol)]
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
    slack((":rotating_light: *CLOSING %s* at market:\n" % (symbol or "ALL POSITIONS")) +
          "\n".join(f"{a} {q} {d}" for c, a, q, d, ac in plan))
    # Cancel the exit manager's resting SELL-to-close orders on these contracts FIRST, so its
    # order + our market close can't both fill and over-sell/naked the position (2026-07-03).
    _ncx = await cancel_resting_closes(ib, {getattr(c, "conId", None) for c, a, q, d, ac in plan})
    if _ncx:
        slack(f":scissors: close_symbol: cancelled {_ncx} resting exit order(s) before closing.")
    await conn.ib.qualifyContractsAsync(*[x[0] for x in plan])
    trades = []
    for c, a, q, d, ac in plan:
        try:
            tr = await conn.place_order(c, conn.create_market_order(a, q))
            trades.append((c, tr))
        except Exception as e:
            print("order FAIL", d, e); slack(f":x: liquidate order failed {d}: {e}")
    for _ in range(25):
        await asyncio.sleep(1.0)
        if trades and all(getattr(t, "isDone", lambda: True)() for _, t in trades): break
    res = []
    for c, tr in trades:
        try: res.append(f"{desc(c)}: {tr.orderStatus.status} @ {tr.orderStatus.avgFillPrice}")
        except Exception: res.append(f"{desc(c)}: (status unknown)")
    # LAYER 1b (2026-06-29): journal our closes so the trader's exit manager won't see the
    # position as still-open and re-close it (the double-close that left a +2 long residual).
    try:
        import datetime as _dt
        jpath = os.path.expanduser("~/exitmgr-app/trades.log")
        with open(jpath, "a") as _jf:
            for c, tr in trades:
                _os = getattr(tr, "orderStatus", None)
                _st = getattr(_os, "status", "")
                # TERMINAL-STATE (2026-07-03): carry the fill price + tool name on the marker so
                # the exit manager can emit a full closed-trade dataset row with REAL realized P&L
                # (not a bare stub). avg_fill_price is null when unknown -> the manager records
                # realized as null (never fabricated). For a spread the manager nets both legs'
                # per-leg markers. `tool` distinguishes exit_reason (closed_by_tool vs liquidated).
                _afp = getattr(_os, "avgFillPrice", None)
                try:
                    _afp = float(_afp) if (_afp is not None and _afp == _afp and float(_afp) > 0) else None
                except (TypeError, ValueError):
                    _afp = None
                _jf.write(json.dumps({"ts": _dt.datetime.utcnow().isoformat(),
                    "contract_id": c.conId, "symbol": c.symbol, "event": "closed_by_tool",
                    "status": _st, "avg_fill_price": _afp, "tool": "close_symbol",
                    "client_id": client_id}) + "\n")
    except Exception as _e:
        print("close-marker journal write failed:", _e)
    av = {v.tag: v.value for v in ib.accountValues() if v.currency in ("USD","BASE","")}
    summary = ":white_check_mark: *Liquidation complete.*\n" + "\n".join(res) + \
              f"\nCash now: {av.get('TotalCashValue')} | NetLiq: {av.get('NetLiquidation')}"
    print(summary); slack(summary)
    await conn.disconnect(); return 0

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--confirm", action="store_true")
    ap.add_argument("--client-id", type=int, default=91)
    ap.add_argument("--symbol", default=None, help="close only this underlying (e.g. RKLB)")
    a = ap.parse_args()
    sys.exit(asyncio.run(run(a.confirm, a.client_id, a.symbol)))
