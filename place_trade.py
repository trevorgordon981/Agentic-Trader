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
import json
import os
from pathlib import Path
import time
from datetime import date, datetime, timedelta

import yaml

from exitmgr.account import get_pot_snapshot
from exitmgr.connection import IBConnection
from exitmgr.ibkr import Stock, Option, Order
from exitmgr.trader import ResolvedOrder, order_summary, audit, _trading_day
from exitmgr import (approval, construction, entry_safety, research, risk,
                     model_release_gate)
from exitmgr.config import construction_from_dict

CLIENT_ID = 90


async def _open_positions_for_risk(conn, journal_path):
    positions = await conn.get_positions()
    debits = {}
    p = Path(journal_path)
    if p.exists():
        for line in p.read_text().splitlines():
            try:
                rec = json.loads(line)
                if rec.get("event"):
                    continue
                debits[int(rec["contract_id"])] = float(rec["debit"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
    out = []
    for pos in positions.values():
        symbol = str(getattr(pos, "symbol", "")).upper()
        cid = getattr(pos, "con_id", None)
        gross = abs(float(getattr(pos, "avg_cost", 0.0))) * 100 * abs(int(getattr(pos, "quantity", 0)))
        notional = debits.get(int(cid), gross) if cid is not None else gross
        out.append(risk.OpenPosition(symbol, notional, symbol in risk.INDEX_UNDERLYINGS))
    return positions, out


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
    with open(args.config) as _cfg_file:
        cfg = yaml.safe_load(_cfg_file)
    if not isinstance(cfg, dict):
        print("[BLOCKED] invalid config")
        return 2
    ibc = cfg.get("ib", {})
    tr = cfg.get("trading", {})
    try:
        release_gate = model_release_gate.settings_from_mapping(tr)
    except model_release_gate.ModelReleaseGateError as exc:
        print(f"[BLOCKED] invalid model release gate configuration: {exc}")
        return 2
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    channel = tr.get("slack_channel", "")
    approver_ids = set(tr.get("approver_ids", []))
    audit_path = tr.get("audit_path", "./audit.jsonl")
    journal_path = cfg.get("journal", {}).get("path", "./trades.log")
    marker_gate = entry_safety.entry_markers_clear(
        config_path=args.config,
        kill_switch_path=(cfg.get("kill_switch") or {}).get("path"))
    if not marker_gate.allowed:
        print("[BLOCKED] " + "; ".join(marker_gate.reasons))
        return 2

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
        initial_bid = initial_ask = 0.0
        if args.limit and args.limit > 0:
            mid = args.limit  # you gave the per-contract premium directly
        else:
            # try IBKR first; it fails (Error 10091) without an options market-data subscription
            try:
                tk = (await ib.reqTickersAsync(opt))[0]
                initial_bid = float(tk.bid) if tk.bid and tk.bid == tk.bid and tk.bid > 0 else 0.0
                initial_ask = float(tk.ask) if tk.ask and tk.ask == tk.ask and tk.ask > 0 else 0.0
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
        account_gate = entry_safety.account_snapshot_valid(pot)
        if not account_gate.allowed or pot.available_funds <= 0:
            print("[BLOCKED] " + "; ".join(account_gate.reasons or ("no available funds",)))
            return 2
        qty = args.qty or max(1, int(pot.available_funds // (mid * 100)))
        if mid * 100 * qty > pot.available_funds:
            qty = max(1, qty - 1)
        decision_id = entry_safety.new_decision_id()
        r = ResolvedOrder(args.symbol, args.right, expiry, float(strike), qty, round(mid, 2), opt,
                          entry_bid=initial_bid, entry_ask=initial_ask,
                          quote_observed_at=time.monotonic(), decision_id=decision_id)

        line = order_summary(r)
        cost = r.limit * 100 * r.qty
        msg = (f":pushpin: *Manual trade you requested* — *{args.symbol}*\n"
               f"*Order:* `{line}`\n"
               f"~${cost:,.0f} of your ${pot.available_funds:,.0f} ({cost/pot.available_funds*100:.0f}% of pot). Max loss = the debit.\n"
               f":point_down: *Tap :white_check_mark: to BUY or :x: to cancel* (already on this message).\n"
               f"_Decision ID: `{decision_id}` — approval expires in 5 minutes._")
        ts = approval.post_proposal(token, channel, msg)
        if not ts:
            print("[ERROR] failed to post to Slack"); return 1
        audit(audit_path, "manual_proposal", underlying=args.symbol, order=line,
              decision_id=decision_id)
        print(f"[INFO] posted to #trading-approvals: {line} — waiting for your tap (5 min)...")

        posted_at = time.monotonic()
        decision = await asyncio.to_thread(
            approval.await_approval, token, channel, ts, approver_ids,
            entry_safety.DEFAULT_APPROVAL_TTL_SECONDS)
        audit(audit_path, "manual_approval", underlying=args.symbol, order=line,
              decision=decision, decision_id=decision_id)
        if decision != "approve":
            print(f"[INFO] not approved (decision={decision}) — nothing placed."); return 0

        age_gate = entry_safety.approval_expired(posted_at)
        if not age_gate.allowed:
            print("[BLOCKED] " + "; ".join(age_gate.reasons))
            return 2

        cons = construction_from_dict(cfg.get("construction"))
        limits = entry_safety.risk_limits_from_config(tr)
        baseline_path = Path(args.config).resolve().parent / tr.get("baseline_path", "./day_baseline.json")

        async def _fresh_gate(prior):
            reasons = list(entry_safety.entry_markers_clear(
                config_path=args.config,
                kill_switch_path=(cfg.get("kill_switch") or {}).get("path")).reasons)
            fresh = None
            fresh_pot = None
            try:
                fresh_pot = await get_pot_snapshot(ib)
                reasons.extend(entry_safety.account_snapshot_valid(fresh_pot).reasons)
                fresh_opt = (await ib.qualifyContractsAsync(
                    Option(args.symbol, expiry, strike, args.right, p.exchange or "SMART")))[0]
                fresh_ticker = (await ib.reqTickersAsync(fresh_opt))[0]
                bid = float(fresh_ticker.bid)
                ask = float(fresh_ticker.ask)
                mid_now = round((bid + ask) / 2, 2)
                fresh = ResolvedOrder(
                    args.symbol, args.right, expiry, float(strike), prior.qty, mid_now, fresh_opt,
                    entry_bid=bid, entry_ask=ask, quote_observed_at=time.monotonic(),
                    decision_id=decision_id,
                    dte=max(0, (datetime.strptime(expiry, "%Y%m%d").date() - date.today()).days))
                reasons.extend(entry_safety.nbbo_valid(fresh).reasons)
                raw_positions, risk_positions = await _open_positions_for_risk(conn, journal_path)
                baseline = entry_safety.day_start_value(baseline_path, _trading_day())
                if isinstance(baseline, entry_safety.SafetyResult):
                    reasons.extend(baseline.reasons)
                    baseline_value = 0.0
                else:
                    baseline_value = baseline
                cost_now = entry_safety.executable_price(fresh) * 100 * fresh.qty
                gate = risk.evaluate_trade(
                    risk.ProposedTrade(
                        underlying=args.symbol, notional=cost_now,
                        is_index=args.symbol.upper() in risk.INDEX_UNDERLYINGS,
                        conviction=1, is_long=args.right == "C"),
                    net_liq=fresh_pot.net_liq,
                    available_funds=fresh_pot.available_funds,
                    open_positions=risk_positions,
                    pot_day_start=baseline_value,
                    approved_names={str(n).upper() for n in tr.get("approved_names", [])},
                    limits=limits)
                if not gate.approved:
                    reasons.extend(gate.reasons)
                budget_ok, budget_reasons = construction.check_budget(
                    cost_now, fresh.dte, fresh_pot.net_liq,
                    construction.open_book(raw_positions, journal_path), cons)
                if not budget_ok:
                    reasons.extend(budget_reasons)
                earnings_days = await asyncio.to_thread(research.days_to_earnings, args.symbol)
                if earnings_days is None:
                    reasons.append("earnings date unavailable at approval time")
                else:
                    entry_date = date.today()
                    earnings_date = entry_date + timedelta(days=earnings_days)
                    earnings_ok, earnings_reason = construction.earnings_ok(
                        entry_date, expiry, earnings_date, cons)
                    if not earnings_ok:
                        reasons.append(earnings_reason)
                # Make the selected contract's NBBO the final network read, then rerun pure
                # dollar gates against its executable ask.
                final_ticker = (await ib.reqTickersAsync(fresh.contract))[0]
                fresh.entry_bid = float(final_ticker.bid)
                fresh.entry_ask = float(final_ticker.ask)
                fresh.limit = round((fresh.entry_bid + fresh.entry_ask) / 2, 2)
                fresh.quote_observed_at = time.monotonic()
                reasons.extend(entry_safety.nbbo_valid(fresh).reasons)
                final_cost = entry_safety.executable_price(fresh) * 100 * fresh.qty
                final_gate = risk.evaluate_trade(
                    risk.ProposedTrade(
                        underlying=args.symbol, notional=final_cost,
                        is_index=args.symbol.upper() in risk.INDEX_UNDERLYINGS,
                        conviction=1, is_long=args.right == "C"),
                    net_liq=fresh_pot.net_liq,
                    available_funds=fresh_pot.available_funds,
                    open_positions=risk_positions,
                    pot_day_start=baseline_value,
                    approved_names={str(n).upper() for n in tr.get("approved_names", [])},
                    limits=limits)
                if not final_gate.approved:
                    reasons.extend(final_gate.reasons)
                final_budget_ok, final_budget_reasons = construction.check_budget(
                    final_cost, fresh.dte, fresh_pot.net_liq,
                    construction.open_book(raw_positions, journal_path), cons)
                if not final_budget_ok:
                    reasons.extend(final_budget_reasons)
            except Exception as exc:
                reasons.append(f"fresh account/contract/NBBO/risk/earnings gate failed: {exc}")
            return fresh, fresh_pot, tuple(dict.fromkeys(str(x) for x in reasons if x))

        fresh, fresh_pot, final_reasons = await _fresh_gate(r)
        if final_reasons or fresh is None:
            print("[BLOCKED] " + "; ".join(final_reasons or ("fresh order unavailable",)))
            audit(audit_path, "manual_final_gate_blocked", decision_id=decision_id,
                  underlying=args.symbol, reasons=final_reasons)
            return 2
        changes = entry_safety.material_changes(r, fresh)
        if changes:
            remsg = (f":repeat: *Reapproval required — {args.symbol}*\n"
                     f"Refreshed order: `{order_summary(fresh)}`\n"
                     f"Executable BUY limit: *${entry_safety.executable_price(fresh):.2f}*\n"
                     f"Changed: {'; '.join(changes)}\n"
                     f":point_down: Approve again within 5 minutes.\n"
                     f"_Decision ID: `{decision_id}`, revision 1_")
            rts = approval.post_proposal(token, channel, remsg)
            if not rts:
                return 2
            reposted_at = time.monotonic()
            rdecision = await asyncio.to_thread(
                approval.await_approval, token, channel, rts, approver_ids,
                entry_safety.DEFAULT_APPROVAL_TTL_SECONDS)
            if rdecision != "approve" or not entry_safety.approval_expired(reposted_at).allowed:
                return 0
            fresh2, fresh_pot, second_reasons = await _fresh_gate(fresh)
            if second_reasons or fresh2 is None or entry_safety.material_changes(fresh, fresh2):
                print("[BLOCKED] terms changed again or final gate failed after reapproval")
                return 2
            fresh = fresh2

        marker_now = entry_safety.entry_markers_clear(
            config_path=args.config,
            kill_switch_path=(cfg.get("kill_switch") or {}).get("path"))
        if not marker_now.allowed:
            print("[BLOCKED] " + "; ".join(marker_now.reasons))
            return 2
        quote_now = entry_safety.nbbo_valid(fresh)
        if not quote_now.allowed:
            print("[BLOCKED] " + "; ".join(quote_now.reasons))
            return 2
        manual_intent = None
        manual_proof = None
        if release_gate.enabled:
            manual_intent = model_release_gate.manual_order_intent(
                decision_id=decision_id, symbol=args.symbol, right=args.right,
                expiry=expiry, strike=float(strike), quantity=fresh.qty,
                limit_price=entry_safety.executable_price(fresh),
                contract_id=int(getattr(fresh.contract, "conId", 0) or 0))
            manual_proof = model_release_gate.issue_manual_decision_proof(
                release_gate, intent=manual_intent, approved=True)
        try:
            release_evidence = model_release_gate.require_v3_release(
                release_gate, endpoint=tr.get("llm_endpoint", ""),
                decision_identity=None, decision_origin="manual",
                manual_proof=manual_proof, manual_intent=manual_intent)
        except model_release_gate.ModelReleaseGateError as exc:
            print(f"[BLOCKED] v3 model release gate: {exc}")
            audit(audit_path, "model_release_gate_blocked", decision_id=decision_id,
                  underlying=args.symbol, reason=str(exc))
            return 2
        if release_evidence.get("enabled"):
            audit(audit_path, "model_release_gate_passed", decision_id=decision_id,
                  underlying=args.symbol, promotion=release_evidence)
        order = Order(action="BUY", orderType="LMT", totalQuantity=fresh.qty,
                      lmtPrice=entry_safety.executable_price(fresh), tif="DAY")
        order.orderRef = entry_safety.decision_order_ref(decision_id)
        from exitmgr.order_lock import order_mutation_lock
        with order_mutation_lock():
            model_release_gate.revalidate_v3_release(
                release_evidence, release_gate, endpoint=tr.get("llm_endpoint", ""),
                decision_origin="manual", manual_proof=manual_proof, manual_intent=manual_intent)
            trade = ib.placeOrder(fresh.contract, order)
        r = fresh
        line = order_summary(r)
        cost = entry_safety.executable_price(r) * 100 * r.qty
        audit(audit_path, "manual_executed", underlying=args.symbol, order=line,
              decision_id=decision_id, order_ref=order.orderRef)
        # journal so the exit manager picks it up
        import json as _j
        _jpath = journal_path
        with open(_jpath, "a") as f:
            f.write(_j.dumps({"ts": datetime.utcnow().isoformat(),
                              "decision_id": decision_id,
                              "contract_id": r.contract.conId,
                              "symbol": args.symbol, "right": args.right, "expiry": expiry,
                              "strike": float(strike), "quantity": r.qty,
                              "debit": round(cost, 2),
                              "order_id": getattr(order, "orderId", None),
                              "order_ref": order.orderRef,
                              "entry_bid": r.entry_bid, "entry_ask": r.entry_ask,
                              "quote_observed_at": r.quote_observed_at}, default=str) + "\n")
        # DECISION CAPTURE (v2, record-only): a manual trade carries no model reasoning, but
        # record a minimal decision (source="manual", con_id known) so the closed-trade record
        # can still join a decision block and label it manual. Never raises.
        try:
            from exitmgr import trade_capture as _tc
            _tc.capture_decision(
                _tc.dataset_dir(_jpath), source="manual", symbol=args.symbol,
                right=args.right, strike=float(strike), expiry=expiry, structure="single",
                con_id=r.contract.conId,
                extra={"order": line, "decision_id": decision_id,
                       "order_ref": order.orderRef,
                       "note": "manual place_trade.py — no model context"})
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
