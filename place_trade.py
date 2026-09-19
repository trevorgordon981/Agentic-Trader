#!/usr/bin/env python
"""Public API contract; production-derived narrative omitted."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import time
from datetime import date, datetime, timedelta, timezone

from exitmgr.account import get_pot_snapshot
from exitmgr.entry_reflection import capture_journal_basis, build_admission_risk_book
from exitmgr.connection import IBConnection
from exitmgr.ibkr import Stock, Order, option_contracts_for_expiry, pick_chain
from exitmgr.trader import (
    ResolvedOrder, order_summary, audit, _trading_day, admission_dimensions,
    observe_for_admission, resting_buy_positions, capital_committed,


    broker_entry_order_view,
)
from exitmgr import approval, construction, entry_safety, research, risk
from exitmgr.config import (
    construction_from_dict, load_config, require_present as _require_caps_present,
)
from exitmgr.runtime_identity import (
    execution_config_dict, freeze_runtime_identity, identity_fields,
)
from exitmgr.entry_reservation import EntryReservationLedger, reservation_as_dict
from exitmgr.campaign_conflicts import (
    CampaignConflictRegistryError, active_campaign_conflict_symbols,
)
from exitmgr.exec_capture import entry_journal_fields
from exitmgr.entry_throttle import (
    EntryThrottleStore, EntryThrottleUnreadable, entry_day_open_counts, record_entry_open,
)




MANUAL_ENTRY_RESERVATIONS = EntryReservationLedger()

CLIENT_ID = 90


class OpenBookUnreadable(RuntimeError):
    """Public API contract; production-derived narrative omitted."""


async def _verified_open_orders(ib, audit_path=None):
    """Public API contract; production-derived narrative omitted."""
    view = await broker_entry_order_view(ib, audit_path)
    if not view.readable:
        raise OpenBookUnreadable(
            "broker open-order book unreadable (%s); deployed premium cannot be valued, so a "
            "working entry would count as zero and check_budget's max_deployed_pct cap would "
            "under-count by exactly the orders in flight"
            % (view.error or "no reason reported"))
    return list(view.trades)


async def _open_positions_for_risk(conn, journal_path, *, journal_basis=None):
    basis = journal_basis if journal_basis is not None else capture_journal_basis(journal_path)
    position_read_started = time.monotonic()
    all_positions = await conn.get_positions(include_short=True)
    book = build_admission_risk_book(
        all_positions, basis, observed_at_monotonic=position_read_started)
    positions = {cid: p for cid, p in all_positions.items() if p.quantity > 0}
    return positions, book


def _yf_option_price(symbol, expiry_yyyymmdd, strike, right):
    """Public API contract; production-derived narrative omitted."""
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






_APPROVAL_TTL_MINUTES = max(1, int(entry_safety.DEFAULT_APPROVAL_TTL_SECONDS) // 60)


def _positive_hold_days(value):
    """Public API contract; production-derived narrative omitted."""
    if value is None or isinstance(value, bool):
        return None
    try:
        held = int(value)
    except (TypeError, ValueError):
        return None
    return held if held > 0 else None


def _is_exact_standard_option(contract, symbol, expiry, strike, right):
    """Public API contract; production-derived narrative omitted."""
    try:
        return (
            int(getattr(contract, "conId", 0) or 0) > 0
            and str(getattr(contract, "symbol", "") or "").upper() == str(symbol).upper()
            and str(getattr(contract, "lastTradeDateOrContractMonth", "") or "")[:8]
            == str(expiry)
            and float(getattr(contract, "strike", 0) or 0) == float(strike)
            and str(getattr(contract, "right", "") or "").upper() == str(right).upper()
            and str(getattr(contract, "secType", "") or "").upper() == "OPT"
            and str(getattr(contract, "exchange", "") or "").upper() == "SMART"
            and str(getattr(contract, "tradingClass", "") or "") == str(symbol).upper()
            and str(getattr(contract, "multiplier", "") or "") == "100"
            and str(getattr(contract, "currency", "") or "").upper() == "USD")
    except (TypeError, ValueError, OverflowError):
        return False


async def _resolve_exact_option(ib, symbol, right, requested_expiry, requested_strike):
    """Public API contract; production-derived narrative omitted."""
    stocks = await ib.qualifyContractsAsync(Stock(symbol, "SMART", "USD"))
    if len(stocks or ()) != 1 or int(getattr(stocks[0], "conId", 0) or 0) <= 0:
        raise ValueError("underlying did not qualify to one exact broker contract")
    params = await ib.reqSecDefOptParamsAsync(symbol, "", "STK", stocks[0].conId)
    chain = pick_chain(params, str(symbol).upper())
    if chain is None:
        raise ValueError("no option chain parameters")
    if (str(getattr(chain, "exchange", "") or "").upper() != "SMART"
            or str(getattr(chain, "tradingClass", "") or "") != str(symbol).upper()
            or str(getattr(chain, "multiplier", "") or "") != "100"):
        raise ValueError("no standard SMART 100-share option chain")
    expirations = tuple(getattr(chain, "expirations", ()) or ())
    if not expirations:
        raise ValueError("standard option chain has no expirations")
    target = datetime.strptime(str(requested_expiry), "%Y%m%d").date()
    expiry = min(
        expirations,
        key=lambda value: abs((datetime.strptime(str(value), "%Y%m%d").date() - target).days))
    contracts = await option_contracts_for_expiry(
        ib, symbol, expiry, right, exchange="SMART", ref_price=float(requested_strike),
        trading_class=str(symbol).upper(), multiplier="100", currency="USD")
    if not contracts:
        raise ValueError("expiry-specific standard option series is unavailable")
    opt = min(contracts, key=lambda contract: abs(
        float(getattr(contract, "strike", 0) or 0) - float(requested_strike)))
    strike = float(getattr(opt, "strike", 0) or 0)
    if not _is_exact_standard_option(opt, symbol, expiry, strike, right):
        raise ValueError("broker returned a nonstandard or ambiguous option identity")
    return chain, str(expiry), strike, opt


async def run(args):
    _resolved_config = load_config(args.config)
    _runtime_identity = freeze_runtime_identity(
        _resolved_config, require_clean_code=True)
    print("[IDENTITY] code_version=%s policy_version=%s" %
          (_runtime_identity.code_version, _runtime_identity.policy_version))


    cfg = execution_config_dict(_resolved_config)
    ibc = cfg.get("ib", {})
    tr = cfg.get("trading", {})
    cons = construction_from_dict(cfg.get("construction"))
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    channel = tr.get("slack_channel", "")
    approver_ids = set(tr.get("approver_ids", []))
    audit_path = tr.get("audit_path", "./audit.jsonl")
    audit(audit_path, "process_runtime_identity", mode="manual",
          **identity_fields(_runtime_identity))
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


        try:
            p, expiry, strike, opt = await _resolve_exact_option(
                ib, args.symbol, args.right, args.expiry, args.strike)
        except (ValueError, TypeError) as exc:
            print("[ERROR] exact option resolution failed:", exc)
            return 1

        mid = 0.0
        initial_bid = initial_ask = 0.0
        if args.limit and args.limit > 0:
            mid = args.limit
        else:

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
                mid = _yf_option_price(args.symbol, expiry, strike, args.right)
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




        if not entry_safety.is_no_earnings_etf(args.symbol):
            try:
                _initial_earnings_days = await asyncio.to_thread(
                    research.days_to_earnings, args.symbol)
            except Exception:
                _initial_earnings_days = None
            if _initial_earnings_days == 0:
                _initial_entry_date = date.today()
                _initial_earnings_date = _initial_entry_date
                _, _initial_earnings_warning = construction.earnings_ok(
                    _initial_entry_date, expiry, _initial_earnings_date, cons,
                    hold_days=_positive_hold_days(getattr(args, "hold_days", None)))
                r.earnings_date = _initial_earnings_date.isoformat()
                r.earnings_warn = _initial_earnings_warning
                r.earnings_day_warning = True

        line = order_summary(r)
        cost = r.limit * 100 * r.qty
        _earnings_banner = (
            ":rotating_light: *EARNINGS TODAY — ENTRY WARNING*\n"
            f"{r.earnings_warn}\n\n" if r.earnings_day_warning else "")
        msg = (_earnings_banner
               + f":pushpin: *Manual trade you requested* — *{args.symbol}*\n"
               f"*Order:* `{line}`\n"
               f"~${cost:,.0f} of your ${pot.available_funds:,.0f} ({cost/pot.available_funds*100:.0f}% of pot). Max loss = the debit.\n"
               f":point_down: *Tap :white_check_mark: to BUY or :x: to cancel* (already on this message).\n"
               f"_Decision ID: `{decision_id}` — approval expires in "
               f"{_APPROVAL_TTL_MINUTES} minutes._")
        ts = approval.post_proposal(token, channel, msg)
        if not ts:
            print("[ERROR] failed to post to Slack"); return 1
        audit(audit_path, "manual_proposal", underlying=args.symbol, order=line,
              decision_id=decision_id)
        if r.earnings_day_warning:
            audit(audit_path, "earnings_same_day_warning", phase="manual_proposal",
                  underlying=args.symbol, earnings_date=r.earnings_date,
                  reason=r.earnings_warn, decision_id=decision_id)
        print(f"[INFO] posted to #trading-approvals: {line} — waiting for your tap "
              f"({_APPROVAL_TTL_MINUTES} min)...")

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
                qualified = await ib.qualifyContractsAsync(opt)
                if len(qualified or ()) != 1:
                    raise ValueError("exact option did not requalify uniquely")
                fresh_opt = qualified[0]
                if (getattr(fresh_opt, "conId", None) != getattr(opt, "conId", None)
                        or not _is_exact_standard_option(
                            fresh_opt, args.symbol, expiry, strike, args.right)):
                    raise ValueError("exact option identity changed before submit")
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



                open_orders = await _verified_open_orders(ib, audit_path)
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
                    construction.open_book(raw_positions, journal_path, open_orders), cons)
                if not budget_ok:
                    reasons.extend(budget_reasons)











                _is_index_sym = (args.symbol.upper() in risk.INDEX_UNDERLYINGS
                                 or entry_safety.is_no_earnings_etf(args.symbol))
                earnings_days = (None if _is_index_sym else
                                 await asyncio.to_thread(research.days_to_earnings, args.symbol))



                fresh.earnings_date = None
                fresh.earnings_warn = ""
                fresh.earnings_unchecked = False
                fresh.earnings_day_warning = False
                if _is_index_sym:
                    pass
                elif earnings_days is None:
                    fresh.earnings_unchecked = True
                    reasons.append("earnings date unavailable at approval time")
                else:
                    entry_date = date.today()
                    earnings_date = entry_date + timedelta(days=earnings_days)
                    fresh.earnings_date = earnings_date.isoformat()
                    fresh.earnings_day_warning = (earnings_date == entry_date)








                    earnings_ok, earnings_reason = construction.earnings_ok(
                        entry_date, expiry, earnings_date, cons,
                        hold_days=_positive_hold_days(getattr(args, "hold_days", None)))
                    if not earnings_ok:
                        reasons.append(earnings_reason)
                    elif earnings_reason:
                        fresh.earnings_warn = earnings_reason
                        print("[WARN] " + earnings_reason)
                        audit(audit_path, ("earnings_same_day_warning"
                              if fresh.earnings_day_warning else "earnings_overlap_disclosed"),
                              phase="manual_final",
                              decision_id=decision_id,
                              underlying=args.symbol, earnings_date=earnings_date.isoformat(),
                              reason=earnings_reason)


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
                    construction.open_book(raw_positions, journal_path, open_orders), cons)
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
        _earnings_warning_was_shown = bool(
            getattr(r, "earnings_day_warning", False))
        if changes:
            _earnings_rewarning = (
                ":rotating_light: *EARNINGS TODAY — ENTRY WARNING*\n"
                f"{fresh.earnings_warn}\n\n"
                if getattr(fresh, "earnings_day_warning", False) else "")
            remsg = (_earnings_rewarning
                     + f":repeat: *Reapproval required — {args.symbol}*\n"
                     f"Refreshed order: `{order_summary(fresh)}`\n"
                     f"Executable BUY limit: *${entry_safety.executable_price(fresh):.2f}*\n"
                     f"Changed: {'; '.join(changes)}\n"
                     f":point_down: Approve again within {_APPROVAL_TTL_MINUTES} minutes.\n"
                     f"_Decision ID: `{decision_id}`, revision 1_")
            rts = approval.post_proposal(token, channel, remsg)
            if not rts:
                return 2
            if getattr(fresh, "earnings_day_warning", False):
                _earnings_warning_was_shown = True
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

        if (getattr(fresh, "earnings_day_warning", False)
                and not _earnings_warning_was_shown):


            _notice_ts = approval.post_proposal(
                token, channel,
                ":rotating_light: *EARNINGS TODAY — FINAL ENTRY WARNING*\n"
                f"{fresh.earnings_warn}\n"
                "Final pre-submit refresh found the event; proceeding under the existing "
                "approval. No additional approval is being awaited.\n"
                f"_Decision ID: `{decision_id}`_",
                seed_reactions=False)
            audit(audit_path, "earnings_same_day_warning_notice",
                  phase="manual_pre_submit", underlying=args.symbol,
                  decision_id=decision_id, earnings_date=fresh.earnings_date,
                  notice_posted=bool(_notice_ts))

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
        order = Order(action="BUY", orderType="LMT", totalQuantity=fresh.qty,
                      lmtPrice=entry_safety.executable_price(fresh), tif="DAY")
        order.orderRef = entry_safety.decision_order_ref(decision_id)


        async def _admission_positions(open_trades):
            basis = capture_journal_basis(journal_path)
            raw, book = await _open_positions_for_risk(conn, journal_path, journal_basis=basis)
            book.extend(resting_buy_positions(open_trades, basis.debits,
                                              existing_con_ids=set((raw or {}).keys())))
            return book

        _obs = await observe_for_admission(ib, _admission_positions, audit_path)
        if _obs is None:
            print("[BLOCKED] the broker book backing admission could not be verified "
                  "(an unreadable book is never treated as an empty one)")
            audit(audit_path, "admission_observation_blocked_submit",
                  decision_id=decision_id, underlying=args.symbol)
            return 2
        try:
            _throttle_store = EntryThrottleStore.for_state_path(
                (cfg.get("state") or {}).get("path", "./exitmgr_state.json"))
        except Exception as _tse:
            print(f"[WARN] entry throttle store unavailable (continuing): {_tse}")
            _throttle_store = None
        _caps = cfg.get("caps") or {}





        _require_caps_present(_caps, "caps")





        try:
            _day_orders, _day_notional = entry_day_open_counts(
                None, _throttle_store, _trading_day())
        except EntryThrottleUnreadable as _tue:
            print("[BLOCKED] daily entry-throttle counters unreadable, so "
                  "caps.max_orders_per_day / caps.max_notional_per_day cannot be "
                  "evaluated: %s" % _tue)
            audit(audit_path, "entry_throttle_unreadable_blocked_submit",
                  decision_id=decision_id, underlying=args.symbol, error=str(_tue))
            return 2
        try:
            _campaign_conflicts = active_campaign_conflict_symbols(journal_path)
        except CampaignConflictRegistryError as _cce:
            print("[BLOCKED] campaign-conflict quarantine unreadable: %s" % _cce)
            audit(audit_path, "campaign_conflict_registry_blocked_submit",
                  decision_id=decision_id, underlying=args.symbol, error=str(_cce))
            return 2
        _dims = admission_dimensions(
            fresh, _obs, order.orderRef, limits=limits, construction_cfg=cons,
            markers_clear=entry_safety.entry_markers_clear(
                config_path=args.config,
                kill_switch_path=(cfg.get("kill_switch") or {}).get("path")).allowed,
            day_orders=_day_orders, day_notional=_day_notional,
            max_orders_per_day=_caps["max_orders_per_day"],
            max_notional_per_day=_caps["max_notional_per_day"],
            runtime_identity=_runtime_identity,
            campaign_conflict_symbols=_campaign_conflicts)

        def _recheck():
            _m = entry_safety.entry_markers_clear(
                config_path=args.config,
                kill_switch_path=(cfg.get("kill_switch") or {}).get("path"))
            if not _m.allowed:
                return False, tuple(_m.reasons)
            _q = entry_safety.nbbo_valid(fresh)
            if not _q.allowed:
                return False, tuple(_q.reasons)
            try:
                _campaign_conflicts_now = active_campaign_conflict_symbols(journal_path)
            except CampaignConflictRegistryError as exc:
                return False, ("campaign-conflict quarantine became unreadable: %s" % exc,)
            if _campaign_conflicts_now != _campaign_conflicts:
                return False, ("campaign-conflict quarantine changed before broker submit",)
            if str(args.symbol).upper() in _campaign_conflicts_now:
                return False, ("%s has an active journal campaign-authority conflict"
                               % str(args.symbol).upper(),)
            return True, ()

        _placed = {}

        def _place():
            from exitmgr.order_lock import order_mutation_lock
            with order_mutation_lock():
                _placed["trade"] = ib.placeOrder(fresh.contract, order)






        _template = {"ts": datetime.now(timezone.utc).isoformat(),
                     "decision_id": decision_id,
                     "contract_id": fresh.contract.conId,
                     "symbol": args.symbol, "right": args.right, "expiry": expiry,
                     "strike": float(strike), "quantity": fresh.qty,
                     "debit": round(entry_safety.executable_price(fresh) * 100 * fresh.qty, 2),
                     "order_id": getattr(order, "orderId", None),
                     "order_ref": order.orderRef,
                     "entry_bid": fresh.entry_bid, "entry_ask": fresh.entry_ask,
                     "quote_observed_at": fresh.quote_observed_at,
                     "earnings_date": getattr(fresh, "earnings_date", None),
                     "earnings_unchecked": bool(
                         getattr(fresh, "earnings_unchecked", False)),
                     "earnings_overlap_warning": (
                         getattr(fresh, "earnings_warn", "") or None),
                     "earnings_day_warning": bool(
                         getattr(fresh, "earnings_day_warning", False))}
        _intent = {
            "order_ref": order.orderRef,
            "con_id": fresh.contract.conId,
            "symbol": args.symbol,
            "side": "debit",
            "source": "manual",
            "decision_id": decision_id,
            **identity_fields(_runtime_identity),
            "requested_qty": abs(int(fresh.qty)),
            "estimated_debit": _template["debit"],
            "transmitted": "unknown",
            "journal_template": _template,
        }
        _decision = MANUAL_ENTRY_RESERVATIONS.reserve_and_place(
            place=_place, recheck=_recheck, intent=_intent,
            on_placed=lambda: record_entry_open(
                None, _throttle_store, _trading_day(), 1, float(_dims["capital_usd"])),
            **_dims)
        audit(audit_path, "entry_admission", decision_id=decision_id,
              **reservation_as_dict(_decision))
        if not _decision.allowed or not _decision.should_place:
            print("[BLOCKED] admission refused (%s): %s"
                  % (_decision.status, "; ".join(_decision.reasons) or _decision.status))
            return 2
        trade = _placed.get("trade")
        if trade is None:
            print("[BLOCKED] admission transaction returned no broker trade object")
            return 2
        r = fresh
        line = order_summary(r)
        cost = entry_safety.executable_price(r) * 100 * r.qty
        audit(audit_path, "manual_executed", underlying=args.symbol, order=line,
              decision_id=decision_id, order_ref=order.orderRef)




        _live = {"Filled", "Submitted", "PreSubmitted"}
        _dead = {"Cancelled", "ApiCancelled", "Inactive", "Rejected"}
        for _ in range(24):
            await asyncio.sleep(0.5)
            if trade.orderStatus.status in _live or trade.orderStatus.status in _dead:
                break
        if trade.orderStatus.status not in _dead:
            for _ in range(20):
                if trade.orderStatus.status == "Filled":
                    break
                await asyncio.sleep(0.5)
        _journalable, _fill, _facts = entry_journal_fields(
            trade, requested_qty=r.qty, estimated_debit=round(cost, 2), credit=False)
        import json as _j
        _jpath = journal_path
        if _journalable:
            _row = dict(_template)
            _row.update(_fill)
            _row.update(identity_fields(_runtime_identity))
            _row["ts"] = datetime.now(timezone.utc).isoformat()
            with open(_jpath, "a") as f:
                f.write(_j.dumps(_row, default=str) + "\n")
            audit(audit_path, "entry_journalled_from_fill", order_ref=order.orderRef,
                  filled=_facts.filled_qty, requested=int(r.qty),
                  quantity_source=_facts.filled_qty_source,
                  basis_source=_row.get("basis_source"), order_status=_facts.status)
            try:
                MANUAL_ENTRY_RESERVATIONS.resolve_intent(
                    order.orderRef, outcome="journaled", filled_qty=int(_facts.filled_qty))
            except Exception as _ie:
                print(f"[WARN] entry intent not released (reconciler will retry): {_ie}")
        else:
            print("[NOT JOURNALLED] order status %s -- the broker has reported no filled "
                  "quantity, so no position is claimed. The durable entry intent keeps this "
                  "order accounted for." % (_facts.status or "unknown"))
            audit(audit_path, "entry_not_journalled_no_observed_fill",
                  order_ref=order.orderRef, order_status=_facts.status,
                  quantity_source=_facts.filled_qty_source,
                  executions_seen=_facts.execution_count,
                  exposure_unsized=_facts.exposure_unsized)
            if _facts.terminal and _facts.filled_qty == 0 and not _facts.exposure_unsized:
                try:
                    MANUAL_ENTRY_RESERVATIONS.resolve_intent(
                        order.orderRef, outcome="terminal_no_fill")
                except Exception as _ie:
                    print(f"[WARN] entry intent not released: {_ie}")



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
    except Exception as _exc:







        try:
            _amb_ref = locals().get("order")
            _amb_ref = getattr(_amb_ref, "orderRef", "") if _amb_ref is not None else ""
            audit(audit_path, "manual_entry_raised_after_admission",
                  order_ref=str(_amb_ref or ""), symbol=getattr(args, "symbol", ""),
                  error=str(_exc)[:300], error_type=type(_exc).__name__,
                  note=("the order may have reached IBKR; the durable entry intent is retained "
                        "and reconcile_entry_intents will materialise any fill"))
        except Exception:
            pass
        print("[AMBIGUOUS] the manual entry raised after admission (%s). The order MAY be live "
              "at IBKR. Its durable intent is retained -- do NOT resubmit; the entry loop's "
              "intent reconciliation will journal any fill." % _exc)
        raise
    finally:
        await conn.disconnect()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--right", default="C", choices=["C", "P"])
    ap.add_argument("--expiry", required=True, help="target expiry YYYYMMDD (nearest valid is used)")
    ap.add_argument("--strike", required=True, type=float)
    ap.add_argument("--qty", type=int, default=0, help="0 = size to available funds")
    ap.add_argument("--hold-days", type=int, default=None, dest="hold_days",
                    help="calendar days you are underwriting this thesis for. Used by the "
                         "earnings gate, which otherwise judges the blackout against EXPIRY "
                         "(with construction.earnings_use_hold_window on and expiry at ~8x "
                         "the hold, that rejects nearly everything). Never derived from --expiry.")
    ap.add_argument("--limit", type=float, default=0.0, help="per-contract premium (skips quoting)")
    ap.add_argument("--config", default="config.yaml")
    raise SystemExit(asyncio.run(run(ap.parse_args())))
