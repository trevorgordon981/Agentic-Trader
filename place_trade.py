#!/usr/bin/env python
"""Place ONE specific option trade through the approval pipeline (manual injection).

For when YOU have an exact trade in mind rather than waiting on the autonomous strategist. It
resolves the concrete contract, posts it to #trading-approvals for your one-tap approve/deny,
and ONLY on your approval places it. Uses clientId 90 so it never clashes with the trader (88),
status (87) or quote (86) scripts. Nothing is placed without your tap.

Usage (from ~/exitmgr-app):
  ~/ib-grader-venv/bin/python place_trade.py --symbol MU --right C --expiry 20270115 --strike 1000
  (omit --qty to size to available funds; add --qty N to fix the count)
"""
import argparse
import asyncio
import os
from datetime import date, datetime

import yaml

from exitmgr.account import get_pot_snapshot
from exitmgr.connection import IBConnection
from exitmgr.ibkr import Stock, Option, Order
from exitmgr.trader import ResolvedOrder, order_summary, audit
from exitmgr import approval

CLIENT_ID = 90


def _yf_option_price(symbol, expiry_yyyymmdd, strike, right):
    """Per-share option mid from yfinance (free, no IBKR subscription). 0.0 on any failure."""
    try:
        import yfinance as yf
        exp = datetime.strptime(expiry_yyyymmdd, "%Y%m%d").strftime("%Y-%m-%d")
        chain = yf.Ticker(symbol).option_chain(exp)
        tbl = chain.calls if right == "C" else chain.puts
        row = tbl[tbl["strike"] == float(strike)]
        if row.empty:
            return 0.0
        bid = float(row["bid"].iloc[0] or 0); ask = float(row["ask"].iloc[0] or 0)
        last = float(row["lastPrice"].iloc[0] or 0)
        mid = (bid + ask) / 2 if (bid > 0 and ask > 0) else last
        return mid if mid > 0 else 0.0
    except Exception:
        return 0.0


async def run(args):
    cfg = yaml.safe_load(open(args.config))
    ibc = cfg.get("ib", {})
    tr = cfg.get("trading", {})
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    channel = tr.get("slack_channel", "")
    approver_ids = set(tr.get("approver_ids", []))
    audit_path = tr.get("audit_path", "./audit.jsonl")

    conn = IBConnection(host=ibc.get("host", "127.0.0.1"), port=ibc.get("port", 4001),
                        client_id=CLIENT_ID, market_data_type=ibc.get("market_data_type", 3))
    if not await conn.connect():
        print("[ERROR] could not connect to IBKR"); return 1
    ib = conn.ib
    try:
        # resolve the concrete option: nearest valid expiry to target, nearest listed strike
        stk = (await ib.qualifyContractsAsync(Stock(args.symbol, "SMART", "USD")))[0]
        params = await ib.reqSecDefOptParamsAsync(args.symbol, "", "STK", stk.conId)
        if not params:
            print("[ERROR] no option params for", args.symbol); return 1
        p = params[0]
        target = datetime.strptime(args.expiry, "%Y%m%d").date()
        expiry = min(p.expirations, key=lambda e: abs((datetime.strptime(e, "%Y%m%d").date() - target).days))
        strike = min(p.strikes, key=lambda k: abs(k - args.strike))
        opt = (await ib.qualifyContractsAsync(Option(args.symbol, expiry, strike, args.right, p.exchange or "SMART")))[0]

        mid = 0.0
        if args.limit and args.limit > 0:
            mid = args.limit  # you gave the per-contract premium directly
        else:
            # try IBKR first; it fails (Error 10091) without an options market-data subscription
            try:
                tk = (await ib.reqTickersAsync(opt))[0]
                m = (tk.bid + tk.ask) / 2 if (tk.bid and tk.ask and tk.bid > 0 and tk.ask > 0) else (tk.last or tk.close or 0)
                if m and m == m and m > 0:
                    mid = m
            except Exception:
                pass
            if not mid:
                mid = _yf_option_price(args.symbol, expiry, strike, args.right)  # free fallback
        if not (mid and mid == mid and mid > 0):
            print("[ERROR] no price available. The account has no IBKR options market-data "
                  "subscription and the free fallback had no quote. Re-run with --limit <per-contract $>.")
            return 1

        pot = await get_pot_snapshot(ib)
        qty = args.qty or max(1, int(pot.available_funds // (mid * 100)))
        if mid * 100 * qty > pot.available_funds:
            qty = max(1, qty - 1)
        r = ResolvedOrder(args.symbol, args.right, expiry, float(strike), qty, round(mid, 2), opt)

        line = order_summary(r)
        cost = r.limit * 100 * r.qty
        msg = (f":pushpin: *Manual trade you requested* — *{args.symbol}*\n"
               f"*Order:* `{line}`\n"
               f"~${cost:,.0f} of your ${pot.available_funds:,.0f} ({cost/pot.available_funds*100:.0f}% of pot). Max loss = the debit.\n"
               f":point_down: *Tap :white_check_mark: to BUY or :x: to cancel* (already on this message).")
        ts = approval.post_proposal(token, channel, msg)
        if not ts:
            print("[ERROR] failed to post to Slack"); return 1
        audit(audit_path, "manual_proposal", underlying=args.symbol, order=line)
        print(f"[INFO] posted to #trading-approvals: {line} — waiting for your tap (15 min)...")

        decision = approval.await_approval(token, channel, ts, approver_ids, timeout_s=900)
        audit(audit_path, "manual_approval", underlying=args.symbol, order=line, decision=decision)
        if decision != "approve":
            print(f"[INFO] not approved (decision={decision}) — nothing placed."); return 0

        order = Order(action="BUY", orderType="LMT", totalQuantity=r.qty, lmtPrice=r.limit, tif="DAY")
        ib.placeOrder(opt, order)
        audit(audit_path, "manual_executed", underlying=args.symbol, order=line)
        # journal so the exit manager picks it up
        import json as _j, time as _t
        _jpath = cfg.get("journal", {}).get("path", "./trades.log")
        with open(_jpath, "a") as f:
            f.write(_j.dumps({"ts": datetime.utcnow().isoformat(), "contract_id": opt.conId,
                              "symbol": args.symbol, "right": args.right, "expiry": expiry,
                              "strike": float(strike), "quantity": r.qty,
                              "debit": round(cost, 2)}, default=str) + "\n")
        # DECISION CAPTURE (v2, record-only): a manual trade carries no model reasoning, but
        # record a minimal decision (source="manual", con_id known) so the closed-trade record
        # can still join a decision block and label it manual. Never raises.
        try:
            from exitmgr import trade_capture as _tc
            _tc.capture_decision(
                _tc.dataset_dir(_jpath), source="manual", symbol=args.symbol,
                right=args.right, strike=float(strike), expiry=expiry, structure="single",
                con_id=opt.conId,
                extra={"order": line, "note": "manual place_trade.py — no model context"})
        except Exception as _dce:
            print(f"[WARN] manual capture_decision failed (continuing): {_dce}")
        print(f"[PLACED] {line}")
        return 0
    finally:
        await conn.disconnect()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--right", default="C", choices=["C", "P"])
    ap.add_argument("--expiry", required=True, help="target expiry YYYYMMDD (nearest valid is used)")
    ap.add_argument("--strike", required=True, type=float)
    ap.add_argument("--qty", type=int, default=0, help="0 = size to available funds")
    ap.add_argument("--limit", type=float, default=0.0, help="per-contract premium (skips quoting)")
    ap.add_argument("--config", default="config.yaml")
    raise SystemExit(asyncio.run(run(ap.parse_args())))
