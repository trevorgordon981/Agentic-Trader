#!/usr/bin/env python
"""Public API contract; production-derived narrative omitted."""
import argparse
import contextvars
import functools
import math
import asyncio
import json
import os
import subprocess
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

from exitmgr.account import get_pot_snapshot
from exitmgr import pipeline_notice
from exitmgr.entry_reflection import capture_journal_basis, build_admission_risk_book
from exitmgr.connection import IBConnection
from exitmgr.ibkr import (
    Stock, Option, Order, option_contracts_for_expiry, pick_chain, strikes_near,
    underlying_price,
)
from exitmgr.strategist import (
    propose, discover_names, propose_one, propose_intents, select_candidate, TradeIdea,
)
from exitmgr.trader import (
    Trader, ResolvedOrder, order_summary, contract_snapshot, audit, _trading_day,



    STAGE_A_REQUEST_SLATE, STAGE_A_REQUEST_DIRECTED, STAGE_B_REQUEST,
    stage_b_technical_failures, stage_b_failure_detail,


    debit_structure_ok, _structure_implied_right, _require_allowed_structure,
    is_credit, capital_at_risk, capital_committed, credit_executable_price,
    credit_material_changes, credit_structure_ok, collateral_capacity,
    required_collateral, broker_deployed_csp_collateral, _credit_limits,
    reserve_credit_entry, admission_dimensions, observe_for_admission, resting_buy_positions,
    entry_eligible_universe, reconsider_new_same_day_earnings,
    earnings_reconsideration_receipt_valid, autonomous_entry_blockers,
    autonomous_execution_gate,


    broker_entry_order_view,
)
from exitmgr.entry_reservation import EntryReservationLedger, reservation_as_dict
from exitmgr.campaign_conflicts import (
    CampaignConflictRegistryError, active_campaign_conflict_symbols,
)
from exitmgr.exec_capture import entry_journal_fields
from exitmgr.entry_throttle import (
    EntryThrottleStore, EntryThrottleUnreadable, entry_day_open_counts, record_entry_open,
)
from exitmgr.entry_contract import StageAIntent
from exitmgr.entry_builder import (
    CandidateBinding, DEBIT_HOLD_FLOOR_MULTIPLE, build_entry_candidates, bindings_for_stage_b,
    combo_spread_ceiling, reprice_binding, select_binding,
)
from exitmgr import (
    apewisdom, approval, config, construction, entry_safety, research, risk,
    setup_watchlist, trade_capture,
)
from exitmgr.config import ConstructionConfig, construction_from_dict, load_config
from exitmgr.runtime_identity import (
    execution_config_dict, freeze_runtime_identity, identity_fields,
)
from exitmgr.slate_lock import slate_active_guard
from exitmgr.market import fetch_universe_quotes, usable_price

CLIENT_ID = 93




CASH_BUFFER_PCT = 0.05
DAILY_ENTRY_RESERVATIONS = EntryReservationLedger()





CONS = ConstructionConfig()













_APPROVAL_TTL_MINUTES = max(1, int(entry_safety.DEFAULT_APPROVAL_TTL_SECONDS) // 60)


def intended_hold_for_gate(*sources):
    """Public API contract; production-derived narrative omitted."""
    for src in sources:
        held = positive_hold_days(getattr(src, "intended_hold_days", None))
        if held is not None:
            return held
    return None


def _tp_level_text(tp_pct, tp_price):
    """Public API contract; production-derived narrative omitted."""
    if tp_pct is None:
        return "take profit *none* (doctrine, not a clamp — the model decides on thesis + giveback)"
    return f"take profit ~${tp_price:.2f} (+{tp_pct:.0f}%, explicit catastrophe backstop)"


def bind_protective_terms(order, tp_pct, sl_pct):
    """Public API contract; production-derived narrative omitted."""
    if is_credit(order):
        order.tp_pct = None
        order.sl_pct = 0.0
        return order
    if tp_pct is not None:
        tp_pct = float(tp_pct)
        if not math.isfinite(tp_pct) or tp_pct <= 0:
            raise ValueError("debit take-profit must be positive or None")
    sl_pct = float(sl_pct)
    if not math.isfinite(sl_pct) or sl_pct <= 0 or sl_pct > 30.0:
        raise ValueError("debit stop must be finite and in (0, 30]")
    order.tp_pct = tp_pct
    order.sl_pct = sl_pct
    return order


def daily_autonomy_blockers(symbol, *, market_context, technical_card):
    """Public API contract; production-derived narrative omitted."""
    degraded = None
    if not market_context:
        degraded = "model request context is unavailable"
    elif not isinstance(technical_card, dict) or not technical_card.get(symbol):
        degraded = f"daily price history unavailable for {symbol}"
    return autonomous_entry_blockers(
        symbol,
        approved_names=getattr(_RESOLVED_CONFIG, "approved_names", ()),
        limits=_RISK_LIMITS,
        research_degraded=degraded,
    )


def _pct_text(v):
    """Public API contract; production-derived narrative omitted."""
    return "none" if v is None else f"{v:.0f}%"
CONN = None
JOURNAL_PATH = "./trades.log"
ERROR_CHANNEL = ""
FILLS_PATH = "./fills.log"

SETUP_WATCHLIST_PATH = setup_watchlist.DEFAULT_PATH








_RISK_LIMITS = risk.RiskLimits()
_RESOLVED_CONFIG = None
_RUNTIME_IDENTITY = None


def _short_option_entry_authority(obj):
    """Public API contract; production-derived narrative omitted."""
    return entry_safety.short_option_entry_authority(
        credit=is_credit(obj),
        credit_entries_enabled=getattr(_RESOLVED_CONFIG, "credit_entries_enabled", False),
        assigned_stock_authority_enabled=getattr(
            _RESOLVED_CONFIG, "assigned_stock_authority_enabled", False),
    )


class OpenBookUnreadable(RuntimeError):
    """Public API contract; production-derived narrative omitted."""


async def _open_book(ib=None, audit_path=None, positions=None):
    """Public API contract; production-derived narrative omitted."""
    view = await broker_entry_order_view(ib if ib is not None else CONN.ib, audit_path)
    if not view.readable:
        raise OpenBookUnreadable(
            "broker open-order book unreadable (%s); deployed premium cannot be valued, so a "
            "working entry would count as zero and check_budget's max_deployed_pct cap would "
            "under-count by exactly the orders in flight"
            % (view.error or "no reason reported"))
    return construction.open_book(
        await CONN.get_positions() if positions is None else positions,
        JOURNAL_PATH, list(view.trades))


async def _open_positions_for_risk(positions=None):
    """Public API contract; production-derived narrative omitted."""
    positions = await CONN.get_positions() if positions is None else positions
    debits = {}
    p = Path(JOURNAL_PATH)
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("event"):
                continue
            cid, d = rec.get("contract_id"), rec.get("debit")
            if cid is None or d is None:
                continue
            try:
                debits[int(cid)] = float(d)
            except (TypeError, ValueError):
                continue
    out = []
    for pd_ in (positions or {}).values():
        sym = (getattr(pd_, "symbol", "") or "").upper()
        is_index = sym in {"SPY", "QQQ", "IWM"}
        con_id = getattr(pd_, "con_id", None)
        gross = abs(getattr(pd_, "avg_cost", 0.0)) * 100 * abs(getattr(pd_, "quantity", 0))
        nd = debits.get(int(con_id)) if con_id is not None else None
        notional = float(nd) if nd is not None else gross
        try:
            cid = int(con_id or 0)
            qty = abs(int(getattr(pd_, "quantity", 0) or 0))
        except (TypeError, ValueError):
            cid, qty = 0, 0
        out.append(risk.OpenPosition(
            sym, notional, is_index, primary_con_id=(cid or None),
            leg_con_ids=((cid,) if cid else ()), contracts=qty,
            campaign_id=(f"contract:{cid}:live" if cid else "")))
    return out


def _journal_debits():
    """Public API contract; production-derived narrative omitted."""
    debits = {}
    p = Path(JOURNAL_PATH)
    if not p.exists():
        return debits
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("event"):
            continue
        cid, d = rec.get("contract_id"), rec.get("debit")
        if cid is None or d is None:
            continue
        try:
            debits[int(cid)] = float(d)
        except (TypeError, ValueError):
            continue
    return debits


def _admission_positions_fn(conn):
    """Public API contract; production-derived narrative omitted."""
    async def _positions(open_trades):
        basis = capture_journal_basis(JOURNAL_PATH)
        position_read_started = time.monotonic()
        positions = await conn.get_positions(include_short=True)
        book = build_admission_risk_book(
            positions, basis, observed_at_monotonic=position_read_started)
        book.extend(resting_buy_positions(open_trades, basis.debits,
                                          existing_con_ids={cid for cid, p in positions.items()
                                                            if p.quantity > 0}))
        return book
    return _positions


async def _prefilter_positions_for_risk(ib, conn, audit_path=None):
    """Public API contract; production-derived narrative omitted."""
    view = await broker_entry_order_view(ib, audit_path)
    if not view.readable:
        raise OpenBookUnreadable(
            "broker open-order book unreadable (%s); entry eligibility cannot count working "
            "BUYs, so the strategist was not called"
            % (view.error or "no reason reported"))
    return await _admission_positions_fn(conn)(view.trades)


def _concentration_notes(open_positions, underlying, candidate_debit, is_index, pot, limits):
    """Public API contract; production-derived narrative omitted."""
    notes = []
    u = (underlying or "").upper()
    if is_index or pot <= 0:
        return notes
    _EPS = 1e-9






    name_exposure = risk.same_name_notional(open_positions, u) + candidate_debit
    name_cap = limits.max_single_name_agg_pct * pot
    if name_exposure > name_cap + _EPS:
        notes.append((
            f":warning: concentration — single-name book would reach ${name_exposure:,.0f} "
            f"({name_exposure / pot * 100:.0f}% of pot, cap {limits.max_single_name_agg_pct:.0%})",
            dict(kind="single_name_agg", underlying=u, exposure=round(name_exposure, 2),
                 cap=round(name_cap, 2), pot=round(pot, 2)),
        ))

    if limits.sector_map and limits.max_sector_agg_pct > 0:
        sec = risk.sector_of(u, limits.sector_map)
        sec_exposure = risk.sector_exposure(
            open_positions, u, candidate_debit, limits.sector_map).get(sec, 0.0)
        sec_cap = limits.max_sector_agg_pct * pot
        if sec_exposure > sec_cap + _EPS:
            notes.append((
                f":warning: concentration — sector '{sec}' would reach ${sec_exposure:,.0f} "
                f"({sec_exposure / pot * 100:.0f}% of pot, cap {limits.max_sector_agg_pct:.0%})",
                dict(kind="sector_agg", underlying=u, sector=sec,
                     exposure=round(sec_exposure, 2), cap=round(sec_cap, 2), pot=round(pot, 2)),
            ))
    return notes


async def _probe_apewisdom_row(ib, row, blocked_sector_keywords, semaphore):
    """Public API contract; production-derived narrative omitted."""
    ticker = row["ticker"]
    try:
        async with semaphore:
            profile = await asyncio.wait_for(
                asyncio.to_thread(apewisdom.security_profile, ticker), timeout=20)
            allowed, reason = apewisdom.profile_eligible(profile, blocked_sector_keywords)
            if not allowed:
                return None, reason
            qualified = await asyncio.wait_for(
                ib.qualifyContractsAsync(Stock(ticker, "SMART", "USD")), timeout=20)
            stock = next((c for c in qualified
                          if getattr(c, "conId", None)
                          and getattr(c, "secType", None) == "STK"
                          and getattr(c, "currency", None) == "USD"), None)
            if stock is None:
                return None, "smart_usd_stock_unqualified"
            params = await asyncio.wait_for(
                ib.reqSecDefOptParamsAsync(ticker, "", "STK", stock.conId), timeout=20)
            if pick_chain(params, ticker) is None:
                return None, "no_smart_option_chain"
            return row, "eligible"
    except Exception as exc:
        return None, f"probe_error:{type(exc).__name__}"


async def _load_apewisdom_pool(ib, tr, audit_path):
    """Public API contract; production-derived narrative omitted."""
    cfg = tr.get("apewisdom_discovery") or {}
    if not cfg.get("enabled", False):
        return None, []
    try:
        source_limit = max(1, min(100, int(cfg.get("source_limit", 20))))
        probe_limit = max(1, min(source_limit, int(cfg.get("probe_limit", 12))))
        feed = await asyncio.to_thread(
            apewisdom.load_trends, cfg.get("filter", "all-stocks"), source_limit)
        initial = apewisdom.initial_rows(feed, blocked=tr.get("blocked_names", []))[:probe_limit]
        semaphore = asyncio.Semaphore(4)
        probed = await asyncio.gather(*[
            _probe_apewisdom_row(ib, row, tr.get("blocked_sector_keywords", []), semaphore)
            for row in initial
        ])
        eligible = [row for row, _reason in probed if row is not None]
        dropped = [{"ticker": initial[i]["ticker"], "reason": reason}
                   for i, (row, reason) in enumerate(probed) if row is None]
        audit(audit_path, "apewisdom_source_screen",
              source_url=feed.get("source_url"), stale=bool(feed.get("stale")),
              age_seconds=feed.get("age_seconds"), fetched=len(feed.get("results", [])),
              probed=len(initial), eligible=[r["ticker"] for r in eligible], dropped=dropped,
              signal_type="attention_only", trade_authority=False, training_eligible=False)
        return feed, eligible
    except Exception as exc:
        audit(audit_path, "apewisdom_source_error", error=f"{type(exc).__name__}: {exc}")
        return None, []


def _merge_discovery_candidates(*groups):
    """Public API contract; production-derived narrative omitted."""
    out, seen = [], set()
    for group in groups:
        for ticker, reason in group or []:
            ticker = str(ticker).upper()
            if ticker in seen:
                continue
            seen.add(ticker)
            out.append((ticker, reason))
    return out


def _watch_entry_fills(placed_watch, token, audit_path):
    """Public API contract; production-derived narrative omitted."""
    import json as _j
    for w in placed_watch:
        try:
            st = w["trade"].orderStatus
            status = st.status
        except Exception:
            continue
        if (w.get("credit_reservation")
                and status in {"Cancelled", "ApiCancelled", "Inactive", "Rejected"}):
            try:
                if DAILY_ENTRY_RESERVATIONS.clear_for_status(w.get("order_ref"), status):
                    audit(audit_path, "credit_entry_reservation_cleared",
                          order_ref=w.get("order_ref"), status=status)
            except Exception as exc:
                audit(audit_path, "credit_entry_reservation_clear_error",
                      order_ref=w.get("order_ref"), status=status, error=str(exc))
        if status == "Filled" and not w["filled_logged"]:
            _afp = getattr(st, "avgFillPrice", None)
            fill_px = float(_afp) if (_afp and _afp == _afp) else None
            try:
                from exitmgr.order import commission_from_trade as _comm_from_trade
                _late_comm = _comm_from_trade(w["trade"])
            except Exception:
                _late_comm = None
            try:
                with open(FILLS_PATH, "a") as f:
                    f.write(_j.dumps({"ts": datetime.now(timezone.utc).isoformat(), "event": "entry_fill",
                                      "decision_id": w.get("decision_id"),
                                      "model_identity": w.get("model_identity"),
                                      "model_identity_source": w.get("model_identity_source"),
                                      "contract_id": getattr(w["r"].contract, "conId", None),
                                      "symbol": w["r"].underlying,
                                      "order_id": getattr(getattr(w["trade"], "order", None), "orderId", None),
                                      "order_ref": getattr(getattr(w["trade"], "order", None), "orderRef", None),
                                      "status": status, "avg_fill_price": fill_px,
                                      "entry_commission": _late_comm,
                                      "quantity": w["r"].qty}) + "\n")
            except Exception as e:
                print(f"[WARN] fills.log write failed: {e}")
            audit(audit_path, "entry_filled", underlying=w["r"].underlying,
                  decision_id=w.get("decision_id"),
                  order_id=getattr(getattr(w["trade"], "order", None), "orderId", None),
                  avg_fill_price=fill_px)
            w["filled_logged"] = True
        elif (status != "Filled" and not w["alerted"]
                and time.monotonic() - w["t0"] > float(CONS.fill_alarm_minutes) * 60):
            w["alerted"] = True
            audit(audit_path, "entry_unfilled_alarm", underlying=w["r"].underlying,
                  status=status, minutes=CONS.fill_alarm_minutes)
            if token and ERROR_CHANNEL:
                approval.post_proposal(token, ERROR_CHANNEL,
                    f":hourglass_flowing_sand: *ENTRY UNFILLED* — `{order_summary(w['r'])}` has not filled "
                    f"after {CONS.fill_alarm_minutes:.0f} min (status {status}). Check the gateway; the "
                    f"trader's cycle alarm will keep escalating.")


def deployable_funds(pot):
    """Public API contract; production-derived narrative omitted."""
    floor = max(0.0, CASH_BUFFER_PCT) * (pot.net_liq or 0.0)
    return max(0.0, (pot.available_funds or 0.0) - floor)


def _append_watchlist(config_path, tickers):
    """Public API contract; production-derived narrative omitted."""
    import re
    s = open(config_path).read()
    m = re.search(r"(  approved_names: \[)([^\]]*)(\])", s)
    if not m:
        return []
    cur = [x.strip() for x in m.group(2).split(",") if x.strip()]
    cur_up = {c.upper() for c in cur}
    add = [t for t in tickers if t.upper() not in cur_up]
    if not add:
        return []
    new_inner = ", ".join(cur + add)
    s = s[:m.start()] + "  approved_names: [" + new_inner + "]" + s[m.end():]
    open(config_path, "w").write(s)
    return add


def _score_tag(s):
    if s < 4:
        return ":warning: *desperate-only*"
    if s >= 8:
        return "*high confidence*"
    if s >= 6:
        return "*medium confidence*"
    return "_below-average / middle_"






























def positive_hold_days(value):
    """Public API contract; production-derived narrative omitted."""
    if value is None or isinstance(value, bool):
        return None
    try:
        held = int(value)
    except (TypeError, ValueError):
        return None
    return held if held > 0 else None


def carry_intended_hold(dst, *sources):
    """Public API contract; production-derived narrative omitted."""
    for src in sources:
        held = positive_hold_days(getattr(src, "intended_hold_days", None))
        if held is not None:
            dst.intended_hold_days = held
            return held
    return None


def doctrine_stamp(intended_hold_days, credit: bool) -> dict:
    """Public API contract; production-derived narrative omitted."""
    held = positive_hold_days(intended_hold_days)
    multiple = 1 if credit else int(DEBIT_HOLD_FLOOR_MULTIPLE)
    return {
        "doctrine_hold_multiple": multiple,
        "doctrine_dte_floor": (held * multiple) if held is not None else None,
        "doctrine_side": ("credit" if credit else "debit"),
    }


@dataclass(frozen=True)
class ExactDirectedSpread:
    """Public API contract; production-derived narrative omitted."""
    underlying: str
    right: str
    expiry: str
    long_strike: float
    short_strike: float
    qty: int
    max_debit: float
    expected_earnings_date: date
    override_earnings_blackout: bool
    long_con_id: int = 0
    short_con_id: int = 0


_EXACT_SPREAD_ARG_NAMES = (
    "exact_expiry", "exact_long_strike", "exact_short_strike", "exact_qty",
    "exact_max_debit", "expected_earnings_date",
)


def exact_spread_from_args(args):
    """Public API contract; production-derived narrative omitted."""
    raw = {name: getattr(args, name, None) for name in _EXACT_SPREAD_ARG_NAMES}
    if not any(value not in (None, "") for value in raw.values()):
        return None
    missing = [name.replace("_", "-") for name, value in raw.items()
               if value in (None, "")]
    if not getattr(args, "ticker", None):
        missing.append("ticker")
    if getattr(args, "right", None) in (None, ""):
        missing.append("right")
    if missing:
        raise ValueError("exact spread requires all fields; missing --" + ", --".join(missing))
    symbol = str(args.ticker).strip().upper()
    right = str(args.right).strip().upper()
    if right not in ("C", "P"):
        raise ValueError("exact spread --right must be C or P")
    try:
        expiry_text = str(raw["exact_expiry"]).strip().replace("-", "")
        expiry_date = datetime.strptime(expiry_text, "%Y%m%d").date()
        earnings_date = datetime.strptime(
            str(raw["expected_earnings_date"]).strip(), "%Y-%m-%d").date()
        long_strike = float(raw["exact_long_strike"])
        short_strike = float(raw["exact_short_strike"])
        qty = int(raw["exact_qty"])
        max_debit = float(raw["exact_max_debit"])
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid exact-spread field: %s" % exc) from exc
    values = (long_strike, short_strike, max_debit)
    if not all(math.isfinite(v) and v > 0 for v in values) or qty < 1:
        raise ValueError("exact spread strikes/max debit must be finite and positive; qty >= 1")
    if (right == "C" and not long_strike < short_strike) or (
            right == "P" and not long_strike > short_strike):
        raise ValueError("exact spread strikes are not a debit-vertical topology for --right %s" % right)
    width = abs(short_strike - long_strike)
    if max_debit >= width:
        raise ValueError("exact spread --exact-max-debit must be below its %.2f width" % width)
    if expiry_date <= datetime.now(timezone.utc).date():
        raise ValueError("exact spread expiry must be in the future")
    return ExactDirectedSpread(
        symbol, right, expiry_text, long_strike, short_strike, qty, max_debit,
        earnings_date, bool(getattr(args, "override_earnings_blackout", False)))


def exact_spread_for(idea):
    spec = getattr(idea, "_exact_spread_spec", None)
    return spec if isinstance(spec, ExactDirectedSpread) else None


def exact_earnings_gate(entry_date, expiry, earnings_date, hold_days, spec):
    """Public API contract; production-derived narrative omitted."""
    normal_ok, normal_reason = construction.earnings_ok(
        entry_date, expiry, earnings_date, CONS, hold_days=hold_days)
    if normal_ok:
        return True, False, normal_reason
    if spec is None:
        return normal_ok, False, normal_reason
    if earnings_date is None:
        return False, False, "exact earnings override refused: current earnings date unavailable"
    if earnings_date != spec.expected_earnings_date:
        return False, False, (
            "exact earnings override refused: current date %s != expected %s"
            % (earnings_date.isoformat(), spec.expected_earnings_date.isoformat()))
    if not spec.override_earnings_blackout:
        return False, False, (normal_reason +
                              "; earnings blackout override was not explicitly authorized")
    return True, True, normal_reason


def exact_spread_order_gate(resolved, spec, *, available=None):
    """Public API contract; production-derived narrative omitted."""
    if spec is None:
        return True, ""
    got = (str(getattr(resolved, "underlying", "")).upper(),
           str(getattr(resolved, "right", "")).upper()[:1],
           str(getattr(resolved, "expiry", "")),
           float(getattr(resolved, "strike", 0) or 0),
           float(getattr(resolved, "short_strike", 0) or 0),
           int(getattr(resolved, "qty", 0) or 0))
    want = (spec.underlying, spec.right, spec.expiry, spec.long_strike,
            spec.short_strike, spec.qty)
    if got != want or getattr(resolved, "short_contract", None) is None:
        return False, "exact spread identity/quantity changed (got %r; expected %r)" % (got, want)
    if (not isinstance(spec.long_con_id, int) or isinstance(spec.long_con_id, bool)
            or spec.long_con_id <= 0 or not isinstance(spec.short_con_id, int)
            or isinstance(spec.short_con_id, bool) or spec.short_con_id <= 0
            or spec.long_con_id == spec.short_con_id):
        return False, "exact spread contract binding is missing/invalid"
    con_ids = []
    for label, contract, strike in (
            ("long", getattr(resolved, "contract", None), spec.long_strike),
            ("short", getattr(resolved, "short_contract", None), spec.short_strike)):
        contract_got = (
            str(getattr(contract, "secType", "")).upper(),
            str(getattr(contract, "symbol", "")).upper(),
            str(getattr(contract, "right", "")).upper()[:1],
            str(getattr(contract, "lastTradeDateOrContractMonth", ""))[:8],
            float(getattr(contract, "strike", 0) or 0),
        )
        contract_want = ("OPT", spec.underlying, spec.right, spec.expiry, strike)
        con_id = getattr(contract, "conId", None)
        if contract_got != contract_want:
            return False, "exact %s contract object changed (got %r; expected %r)" % (
                label, contract_got, contract_want)
        if isinstance(con_id, bool) or not isinstance(con_id, int) or con_id <= 0:
            return False, "exact %s contract object has invalid conId" % label
        con_ids.append(con_id)
    if con_ids[0] == con_ids[1]:
        return False, "exact spread contract objects have duplicate conIds"
    if tuple(con_ids) != (spec.long_con_id, spec.short_con_id):
        return False, "exact spread contract conIds changed (got %r; expected %r)" % (
            tuple(con_ids), (spec.long_con_id, spec.short_con_id))
    quote = entry_safety.nbbo_valid(resolved)
    if not quote.allowed:
        return False, "; ".join(quote.reasons)
    ask = float(resolved.entry_ask)
    if ask > spec.max_debit + 1e-9:
        return False, "executable combo ask $%.2f exceeds exact max debit $%.2f" % (
            ask, spec.max_debit)
    if available is not None and ask * 100 * spec.qty > float(available) + 1e-6:
        return False, "exact quantity costs $%.2f at ask, above available $%.2f" % (
            ask * 100 * spec.qty, float(available))
    return True, ""


def user_directed_idea(args, *, fallback_hold_days=None) -> TradeIdea:
    """Public API contract; production-derived narrative omitted."""
    _exact = exact_spread_from_args(args)
    _right = str(getattr(args, "right", None) or "C").upper()
    direction = "bullish" if _right == "C" else "bearish"
    structure = (("call debit spread" if _right == "C" else "put debit spread")
                 if _exact is not None else
                 (args.structure or ("long call" if direction == "bullish" else "long put")))
    if _exact is not None and getattr(args, "structure", ""):
        raise ValueError("--structure is not accepted with an exact spread; right/legs fix it")









    _hold = positive_hold_days(getattr(args, "hold_days", 0))
    _hold_source = "explicit"
    if _hold is None:
        _hold = positive_hold_days(fallback_hold_days)
        _hold_source = "config_fallback"
    if _hold is None:
        raise ValueError(
            "intended_hold_days is REQUIRED and was not stated. Pass --hold-days N (the calendar "
            "days you are underwriting this thesis for; the entry then needs DTE >= %dx N). "
            "Alternatively set trading.intended_hold_days_fallback in config.yaml -- it is "
            "currently null/unset, which makes this refusal absolute by design."
            % int(DEBIT_HOLD_FLOOR_MULTIPLE))
    if _exact is not None:
        _exact_dte = (datetime.strptime(_exact.expiry, "%Y%m%d").date()
                      - datetime.now(timezone.utc).date()).days
        _required_dte = _hold * int(DEBIT_HOLD_FLOOR_MULTIPLE)
        if _exact_dte < _required_dte:
            raise ValueError(
                "exact spread expiry is %d DTE, below intended-hold floor %d (%dd x %d)"
                % (_exact_dte, _required_dte, _hold, int(DEBIT_HOLD_FLOOR_MULTIPLE)))
    else:
        _exact_dte = None
    idea = TradeIdea(underlying=args.ticker.upper(),
                     is_index=args.ticker.upper() in ("SPY", "QQQ", "IWM"),
                     direction=direction, structure=structure,
                     target_dte=(_exact_dte if _exact_dte is not None else args.dte),
                     target_delta=args.delta,
                     est_debit_usd=0.0, conviction=int(args.conviction),
                     thesis=args.thesis, profit_target_pct=args.tp, stop_pct=args.stop,
                     intended_hold_days=_hold)


    idea._intended_hold_days_source = _hold_source
    if _exact is not None:
        idea._exact_spread_spec = _exact
    if _hold_source == "config_fallback":
        print("[WARN] --hold-days not given; using trading.intended_hold_days_fallback=%d. "
              "The DTE floor for this entry is %d days."
              % (_hold, _hold * int(DEBIT_HOLD_FLOOR_MULTIPLE)), flush=True)
    ok, why = debit_structure_ok(idea)
    if not ok:
        raise ValueError("--structure %r refused. %s" % (structure, why))
    return idea


def apply_structure_override(idea, ovr):
    """Public API contract; production-derived narrative omitted."""
    from dataclasses import replace as _replace





    try:
        _require_allowed_structure("debit", idea.structure)
    except ValueError as _incoming_exc:
        return idea, ("structure override refused. STRUCTURE REFUSED: %s" % (_incoming_exc,)), ""
    nd = idea.direction
    _dir_overridden = False
    if ovr.get("direction") == "flip":
        nd = "bearish" if idea.direction == "bullish" else "bullish"
        _dir_overridden = True
    elif ovr.get("direction") in ("bullish", "bearish"):
        nd = ovr["direction"]
        _dir_overridden = True
    ns = idea.structure
    if ovr.get("structure") == "single":
        ns = "long put" if nd == "bearish" else "long call"
    elif ovr.get("structure") == "spread":
        ns = "put debit spread" if nd == "bearish" else "call debit spread"
    note = ""


    if _dir_overridden and not ovr.get("structure"):
        _implied = _structure_implied_right(ns)
        _want = "C" if nd == "bullish" else "P"
        if _implied and _implied != _want:
            _relabelled = (("put debit spread" if nd == "bearish" else "call debit spread")
                           if "spread" in str(ns).lower()
                           else ("long put" if nd == "bearish" else "long call"))
            note = ("structure relabelled %r -> %r to match the direction you set (%s); the "
                    "single-vs-spread shape and the contract are unchanged" % (ns, _relabelled, nd))
            ns = _relabelled
    effective = _replace(idea, direction=nd, structure=ns)
    ok, why = debit_structure_ok(effective)
    if not ok:
        return effective, ("structure override refused. %s" % why), note
    return effective, "", note


def submit_structure_ok(r, idea) -> tuple:
    """Public API contract; production-derived narrative omitted."""
    if not getattr(r, "structure", ""):
        return True, ""
    ok, why = debit_structure_ok(idea)
    if not ok:
        return False, why
    implied = _structure_implied_right(r.structure)
    if implied and implied != str(r.right).upper()[:1]:
        return False, ("resolved order buys a %r but its structure %r names a %s -- filling it "
                       "would journal the position under a name describing a different trade"
                       % (r.right, r.structure, "call" if implied == "C" else "put"))
    return True, ""


async def _resolve(ib, idea, available, net_liq=None):
    """Resolve a candidate against executable contracts and deterministic risk limits.

Underlying share notional is not capital at risk for a defined-risk option structure;
the maximum debit or spread width is the bounded exposure used by the gate.
    """
    _exact = exact_spread_for(idea)
    if _exact is not None:
        today = datetime.now(timezone.utc).date()
        expiry_date = datetime.strptime(_exact.expiry, "%Y%m%d").date()
        dte = (expiry_date - today).days
        if dte < int(CONS.min_dte):
            return None, "exact expiry is %d DTE, below min-DTE floor %d" % (dte, CONS.min_dte)
        held = intended_hold_for_gate(idea)
        if held is None:
            return None, "exact spread intended_hold_days is missing"
        required_dte = held * int(DEBIT_HOLD_FLOOR_MULTIPLE)
        if dte < required_dte:
            return None, "exact spread expiry is %d DTE, below intended-hold floor %d" % (
                dte, required_dte)
        stock_rows = await ib.qualifyContractsAsync(Stock(_exact.underlying, "SMART", "USD"))
        if len(stock_rows or []) != 1 or not getattr(stock_rows[0], "conId", None):
            return None, "exact spread underlying did not qualify uniquely"
        stock = stock_rows[0]
        spot = await underlying_price(ib, stock)
        requested = [Option(_exact.underlying, _exact.expiry, strike, _exact.right, "SMART")
                     for strike in (_exact.long_strike, _exact.short_strike)]
        qualified = await ib.qualifyContractsAsync(*requested)
        if len(qualified or []) != 2:
            return None, "exact spread legs did not both qualify"
        for contract, strike in zip(qualified, (_exact.long_strike, _exact.short_strike)):
            identity = (str(getattr(contract, "symbol", "")).upper(),
                        str(getattr(contract, "right", "")).upper()[:1],
                        str(getattr(contract, "lastTradeDateOrContractMonth", ""))[:8],
                        float(getattr(contract, "strike", 0) or 0))
            if identity != (_exact.underlying, _exact.right, _exact.expiry, strike):
                return None, "qualified exact leg identity changed: %r" % (identity,)
            if not getattr(contract, "conId", None):
                return None, "qualified exact leg has no conId"
        long_con_id = getattr(qualified[0], "conId", None)
        short_con_id = getattr(qualified[1], "conId", None)
        if (isinstance(long_con_id, bool) or not isinstance(long_con_id, int)
                or long_con_id <= 0 or isinstance(short_con_id, bool)
                or not isinstance(short_con_id, int) or short_con_id <= 0
                or long_con_id == short_con_id):
            return None, "qualified exact legs require positive distinct conIds"
        if _exact.long_con_id or _exact.short_con_id:
            if (long_con_id, short_con_id) != (_exact.long_con_id, _exact.short_con_id):
                return None, "qualified exact leg conIds changed from pinned binding"
        else:
            _exact = replace(_exact, long_con_id=long_con_id, short_con_id=short_con_id)
            idea._exact_spread_spec = _exact
        tickers = await ib.reqTickersAsync(*qualified)
        by_conid = {getattr(t.contract, "conId", None): t for t in (tickers or [])}
        try:
            long_ticker = by_conid[qualified[0].conId]
            short_ticker = by_conid[qualified[1].conId]
            long_bid, long_ask = float(long_ticker.bid), float(long_ticker.ask)
            short_bid, short_ask = float(short_ticker.bid), float(short_ticker.ask)
        except Exception as exc:
            return None, "exact spread quote missing: %s" % exc
        if not all(usable_price(v) for v in (long_bid, long_ask, short_bid, short_ask)):
            return None, "exact spread requires fresh two-sided quotes on both legs"
        if long_ask < long_bid or short_ask < short_bid:
            return None, "exact spread leg NBBO is crossed"
        long_greeks = (getattr(long_ticker, "modelGreeks", None)
                       or getattr(long_ticker, "lastGreeks", None))
        atm_iv = getattr(long_greeks, "impliedVol", None) if long_greeks else None
        sane, why_sane = construction.spread_structure_ok(
            _exact.long_strike, _exact.short_strike, spot, _exact.right, dte, atm_iv, CONS)
        if not sane:
            return None, why_sane
        combo_bid = round(long_bid - short_ask, 6)
        combo_ask = round(long_ask - short_bid, 6)
        width = abs(_exact.short_strike - _exact.long_strike)
        if not (0 < combo_bid <= combo_ask <= width):
            return None, "invalid exact combo NBBO bid=%.2f ask=%.2f width=%.2f" % (
                combo_bid, combo_ask, width)
        combo_mid = (combo_bid + combo_ask) / 2
        spread_pct = (combo_ask - combo_bid) / combo_mid * 100
        _combo_ceiling = combo_spread_ceiling(CONS, _exact.underlying)
        if _combo_ceiling is None:
            return None, "combo spread ceiling is unavailable"
        if spread_pct > _combo_ceiling + 1e-12:
            return None, "exact combo spread %.1f%% exceeds %.1f%% ceiling" % (
                spread_pct, _combo_ceiling)
        if combo_ask > _exact.max_debit + 1e-9:
            return None, "executable combo ask $%.2f exceeds exact max debit $%.2f" % (
                combo_ask, _exact.max_debit)
        if combo_ask * 100 * _exact.qty > float(available) + 1e-6:
            return None, "exact quantity costs $%.2f at ask, above available $%.2f" % (
                combo_ask * 100 * _exact.qty, float(available))
        resolved = ResolvedOrder(
            _exact.underlying, _exact.right, _exact.expiry, _exact.long_strike,
            _exact.qty, round(combo_ask, 2), qualified[0],
            short_strike=_exact.short_strike, short_contract=qualified[1],
            structure=str(idea.structure), spot=float(spot),
            entry_delta=(abs(float(long_greeks.delta)) if long_greeks and
                         getattr(long_greeks, "delta", None) is not None else 0.0),
            entry_iv=(float(atm_iv) if atm_iv is not None and math.isfinite(float(atm_iv)) else 0.0),
            dte=dte, dte_adjusted=False, entry_bid=combo_bid, entry_ask=combo_ask,
            entry_spread_pct=round(spread_pct, 2), quote_observed_at=time.monotonic(),
            intended_hold_days=held)
        ok_exact, why_exact = exact_spread_order_gate(resolved, _exact, available=available)
        return (resolved, None) if ok_exact else (None, why_exact)



    binding = getattr(idea, "_stage_b_binding", None)
    intent = getattr(idea, "_stage_a_intent", None)
    if binding is not None or intent is not None:
        if not isinstance(binding, CandidateBinding) or not isinstance(intent, StageAIntent):
            return None, "invalid Stage-B binding"
        try:
            deployed_credit = (await broker_deployed_csp_collateral(ib)
                               if intent.side == "credit" else 0.0)
            _rs = []
            refreshed = await reprice_binding(
                ib, binding, intent, net_liq=net_liq,
                available_funds=available, cons=CONS,
                deployed_credit_usd=deployed_credit,

                cash_buffer_pct=0.0, reasons=_rs)
        except Exception as exc:
            return None, f"selected-candidate requote failed: {exc}"
        if refreshed is None or refreshed.candidate.candidate_id != binding.candidate.candidate_id:


            _why = "; ".join(_rs) if _rs else "candidate identity changed on requote"
            return None, "cannot place: %s" % _why
        idea._stage_b_binding = refreshed
        resolved = refreshed.to_resolved_order(intent)
        if resolved.side == "credit":
            idea.strike = resolved.strike
            idea.collateral_usd = resolved.collateral_usd
            idea.net_credit_usd = resolved.net_credit_usd
            idea.max_loss_usd = resolved.credit_max_loss_usd
        else:
            idea.est_debit_usd = round(
                refreshed.candidate.one_contract_cost_usd * resolved.qty, 2)
        return resolved, None





    _ok_struct, _why_struct = debit_structure_ok(idea)
    if not _ok_struct:
        return None, _why_struct
    right = "C" if idea.direction == "bullish" else "P"
    stk = (await ib.qualifyContractsAsync(Stock(idea.underlying, "SMART", "USD")))[0]
    params = await ib.reqSecDefOptParamsAsync(idea.underlying, "", "STK", stk.conId)
    if not params:
        return None, "no option chain"
    p = pick_chain(params, idea.underlying)
    if p is None:
        return None, "no SMART option chain"




    expiry, chosen_dte, dte_adjusted = construction.pick_expiry(
        p.expirations, idea.target_dte, CONS.min_dte, CONS.prefer_dte_max)
    if expiry is None:
        return None, f"no expiry >= {CONS.min_dte} DTE (min-DTE floor)"
    spot = await underlying_price(ib, stk)



    qualified = await option_contracts_for_expiry(
        ib, idea.underlying, expiry, right, "SMART", spot, timeout_s=20,
        trading_class=getattr(p, "tradingClass", None),
        multiplier=getattr(p, "multiplier", None), currency="USD")
    if qualified is None:
        cands = [Option(idea.underlying, expiry, k, right, "SMART")
                 for k in strikes_near(p.strikes, spot)]
        qualified = await ib.qualifyContractsAsync(*cands)
    if not qualified:
        return None, "no contracts exist for selected expiry/right"
    tickers = await ib.reqTickersAsync(*[c for c in qualified if getattr(c, "conId", None)])


    tgt_delta = construction.effective_delta(idea.target_delta, CONS)
    best, best_err, best_greeks = None, 1e9, None
    best_bidask = (None, None)
    by_strike = {}
    quote_by_strike = {}
    for tk in tickers:



        if usable_price(tk.bid) and usable_price(tk.ask):
            mid = (tk.bid + tk.ask) / 2
        else:
            mid = tk.last if usable_price(tk.last) else 0
        if not (mid == mid and mid > 0):
            continue
        k = float(getattr(tk.contract, "strike", 0) or 0)
        if k:
            by_strike[k] = (mid, tk.contract)
            quote_by_strike[k] = (getattr(tk, "bid", None), getattr(tk, "ask", None))
        g = getattr(tk, "modelGreeks", None) or getattr(tk, "lastGreeks", None)
        if g and g.delta is not None:
            err = abs(abs(g.delta) - tgt_delta)
            if err < best_err:
                best, best_err, best_greeks = (tk.contract, mid), err, g
                best_bidask = (getattr(tk, "bid", None), getattr(tk, "ask", None))
    if not best and by_strike and spot:


        k_near = min(by_strike, key=lambda k: abs(k - spot))
        if abs(k_near - spot) <= CONS.strike_near_spot_pct * spot:
            best = (by_strike[k_near][1], by_strike[k_near][0])
            best_bidask = quote_by_strike.get(k_near, (None, None))
    if not best:
        return None, "no priced strike (OPRA active?)"
    contract, mid = best
    atm_iv = getattr(best_greeks, "impliedVol", None) if best_greeks else None


    ok, why = construction.long_strike_ok(float(contract.strike), spot, right, chosen_dte, atm_iv, CONS)
    if not ok:
        return None, why
    def _quote_value(value):
        try:
            value = float(value)
            return value if value == value and value > 0 else 0.0
        except (TypeError, ValueError):
            return 0.0

    _long_bid, _long_ask = (_quote_value(x) for x in best_bidask)
    enrich = dict(spot=float(spot or 0.0),
                  entry_delta=float(abs(best_greeks.delta)) if (best_greeks and best_greeks.delta is not None) else 0.0,
                  entry_iv=float(atm_iv) if (atm_iv and atm_iv == atm_iv) else 0.0,
                  dte=int(chosen_dte), dte_adjusted=bool(dte_adjusted),
                  entry_bid=_long_bid, entry_ask=_long_ask,
                  entry_spread_pct=(round((_long_ask - _long_bid) / mid * 100, 2)
                                    if mid > 0 and _long_ask >= _long_bid > 0 else 0.0),
                  quote_observed_at=time.monotonic())


    if "spread" in (idea.structure or "").lower():
        from exitmgr.trader import pick_spread_short, size_within_cap



        pick = pick_spread_short([(k, m) for k, (m, _) in by_strike.items()],
                                 float(contract.strike), mid, right, available,
                                 spot=spot, dte=chosen_dte, atm_iv=atm_iv, cons=CONS)
        if pick:
            short_strike, net = pick
            short_contract = by_strike[short_strike][1]
            _short_bid, _short_ask = (_quote_value(x) for x in quote_by_strike[short_strike])
            if _long_bid > 0 and _long_ask > 0 and _short_bid > 0 and _short_ask > 0:
                enrich["entry_bid"] = round(_long_bid - _short_ask, 4)
                enrich["entry_ask"] = round(_long_ask - _short_bid, 4)
                _net_mid = (enrich["entry_bid"] + enrich["entry_ask"]) / 2
                enrich["entry_spread_pct"] = (
                    round((enrich["entry_ask"] - enrich["entry_bid"]) / _net_mid * 100, 2)
                    if _net_mid > 0 and enrich["entry_ask"] >= enrich["entry_bid"] else 0.0)
            else:
                enrich["entry_bid"] = enrich["entry_ask"] = 0.0
                enrich["entry_spread_pct"] = 0.0




            qty = size_within_cap(net * 100, available, available)
            if qty is None:
                return None, (f"one spread contract ${net*100:,.0f} > available ${available:,.0f}")
            return ResolvedOrder(idea.underlying, right, expiry, float(contract.strike), qty, net,
                                 contract, short_strike=short_strike, short_contract=short_contract,
                                 structure=str(getattr(idea, "structure", "") or ""),
                                 **enrich), None


    if mid * 100 > available + 1e-6:
        return None, f"one contract ${mid*100:,.0f} > available ${available:,.0f}"
    qty = max(1, int(available // (mid * 100)))
    return ResolvedOrder(idea.underlying, right, expiry, float(contract.strike), qty, round(mid, 2),
                         contract, structure=str(getattr(idea, "structure", "") or ""),
                         **enrich), None


def _daily_cap_rejected(stage, reason, idea, resolved):
    """Public API contract; production-derived narrative omitted."""
    try:
        trade_capture.capture_rejected(
            trade_capture.dataset_dir(JOURNAL_PATH), source="daily_slate",
            symbol=getattr(idea, "underlying", None), reason=reason, stage=stage,
            idea=idea, structure=getattr(idea, "structure", None),
            right=getattr(resolved, "right", None), strike=getattr(resolved, "strike", None),
            expiry=getattr(resolved, "expiry", None),
            order=(order_summary(resolved) if resolved is not None else None))
    except Exception as _re:
        print(f"[WARN] daily-slate capture_rejected failed (continuing): {_re}")


async def _await_slate_model(call, *args, **kwargs):
    """Public API contract; production-derived narrative omitted."""
    context = contextvars.copy_context()
    future = asyncio.get_running_loop().run_in_executor(
        None, context.run, functools.partial(call, *args, **kwargs))
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:

                continue
            except Exception:
                break
        if not future.cancelled():
            future.exception()
        raise


async def _materialize_stage_b(ib, intents, pot, tr, audit_path, *, with_outcomes=False):
    """Public API contract; production-derived narrative omitted."""







    _cx = construction.apply_construction_policy(
        intents, min_dte=getattr(CONS, "min_dte", None),
        enabled=bool(getattr(CONS, "deterministic_construction", True)))
    for _ch in _cx.changes:
        audit(audit_path, "deterministic_construction", route="daily_or_add_name", **_ch)
    outcomes = []
    for _bad_intent, _why_dropped in _cx.dropped:
        outcomes.append({"outcome": "construction_rejected",
                         "underlying": getattr(_bad_intent, "underlying", None),
                         "reason": _why_dropped})
        audit(audit_path, "deterministic_construction_refused",
              underlying=getattr(_bad_intent, "underlying", None),
              intended_hold_days=getattr(_bad_intent, "intended_hold_days", None),
              reason=_why_dropped, route="daily_or_add_name")
    intents = list(_cx.ideas)
    ideas = []
    for index, intent in enumerate(intents or [], start=1):
        intent_id = f"intent_{index}"
        try:
            deployed_credit = (await broker_deployed_csp_collateral(ib, audit_path)
                               if intent.side == "credit" else 0.0)
            bindings = await build_entry_candidates(
                ib, intent, intent_id, net_liq=pot.net_liq,
                available_funds=pot.available_funds, cons=CONS,
                deployed_credit_usd=deployed_credit,
                cash_buffer_pct=CASH_BUFFER_PCT)
        except Exception as exc:
            outcomes.append({"intent_id": intent_id, "outcome": "candidate_error",
                             "underlying": getattr(intent, "underlying", None),
                             "error": str(exc)})
            audit(audit_path, "stage_b_candidate_error", intent_id=intent_id,
                  underlying=getattr(intent, "underlying", None), error=str(exc),
                  route="daily_or_add_name")
            continue







        _max_age = construction.stage_b_quote_max_age_s(CONS)
        _pre_stage_b = len(bindings or [])
        bindings = bindings_for_stage_b(bindings, max_age_seconds=_max_age)
        if _pre_stage_b and len(bindings) < _pre_stage_b:
            print(f"[BUILD] {intent.underlying}: {_pre_stage_b - len(bindings)}/"
                  f"{_pre_stage_b} candidates dropped for stale NBBO "
                  f"(> {_max_age:.0f}s); {len(bindings)} survive")
        if len(bindings) < 3:
            outcomes.append({"intent_id": intent_id, "outcome": "construction_rejected",
                             "underlying": intent.underlying,
                             "candidate_count": len(bindings)})
            audit(audit_path, "stage_b_skipped", intent_id=intent_id,
                  underlying=intent.underlying,
                  reason=f"only {len(bindings)} prefiltered candidates; requires 3",
                  route="daily_or_add_name")
            continue
        candidates = [binding.candidate for binding in bindings]
        try:
            result = await _await_slate_model(
                select_candidate, tr.get("llm_endpoint"), tr.get("llm_model"),
                intent, candidates, intent_id=intent_id, **STAGE_B_REQUEST)
            selected = result
            raw_b = cot_b = identity_b = None
            if isinstance(result, tuple):
                selected = result[0] if result else None
                raw_b = result[1] if len(result) > 1 else None
                cot_b = result[2] if len(result) > 2 else None
                identity_b = result[3] if len(result) > 3 else None
        except Exception as exc:
            outcomes.append({"intent_id": intent_id, "outcome": "selector_error",
                             "underlying": intent.underlying, "error": str(exc)})
            audit(audit_path, "stage_b_error", intent_id=intent_id,
                  underlying=intent.underlying, error=str(exc),
                  route="daily_or_add_name")
            continue
        if selected is None:
            outcomes.append({"intent_id": intent_id, "outcome": "stage_b_declined",
                             "underlying": intent.underlying,
                             "candidate_count": len(candidates), "raw": raw_b, "cot": cot_b})
            audit(audit_path, "stage_b_declined", intent_id=intent_id,
                  underlying=intent.underlying, route="daily_or_add_name")
            continue
        selected_id = getattr(selected, "candidate_id", None)
        binding = select_binding(bindings, selected_id)
        if binding is None:
            outcomes.append({"intent_id": intent_id, "outcome": "invalid_selection",
                             "underlying": intent.underlying, "candidate_id": selected_id})
            audit(audit_path, "stage_b_invalid_selection", intent_id=intent_id,
                  underlying=intent.underlying, candidate_id=selected_id,
                  route="daily_or_add_name")
            continue
        idea = Trader._idea_from_stage_b(intent, binding)
        idea._stage_b_candidates = tuple(candidates)
        idea._stage_b_raw = raw_b
        idea._stage_b_cot = cot_b
        idea._stage_b_identity = identity_b
        ideas.append(idea)
        outcomes.append({"intent_id": intent_id, "outcome": "selected",
                         "underlying": intent.underlying, "candidate_id": selected_id})
        audit(audit_path, "stage_b_selected", intent_id=intent_id,
              underlying=intent.underlying, candidate_id=selected_id,
              candidates=len(candidates), route="daily_or_add_name")
    return (ideas, outcomes, len(intents)) if with_outcomes else ideas


async def _post_idea(ib, idea, pot, default_pct, token, channel, audit_path, pending,
                     label="Daily rec", audit_event="daily_rec_posted",
                     candidates=None, raw_strategist=None, market_context=None, regime=None,
                     technical_card=None, cot=None, model_identity=None,
                     model_identity_source=None):
    """Public API contract; production-derived narrative omitted."""
    model_identity, model_identity_source = approval.decisive_model_identity(
        model_identity, model_identity_source,
        getattr(idea, "_stage_b_identity", None))
    _credit = is_credit(idea)
    _authority = _short_option_entry_authority(idea)
    if not _authority.allowed:
        _daily_cap_rejected("assigned_stock_authority", _authority.reasons, idea, None)
        audit(audit_path, "credit_assignment_authority_rejected",
              underlying=idea.underlying, reasons=_authority.reasons)
        return None
    deployable = deployable_funds(pot)



    if _credit:
        _deployed_credit = await broker_deployed_csp_collateral(ib, audit_path)
        if _deployed_credit is None:
            _daily_cap_rejected("collateral", "deployed CSP collateral unverifiable", idea, None)
            return None
        deployable = min(
            deployable,
            max(0.0, 0.80 * pot.net_liq - _deployed_credit),
        )
    else:
        _max_prem = construction.max_premium_budget(pot.net_liq, CONS)
        if _max_prem > 0:
            deployable = min(deployable, _max_prem)
    _stage_b_bound = getattr(idea, "_stage_b_binding", None) is not None
    cons_budget = deployable if _stage_b_bound else min(deployable, default_pct * pot.net_liq)
    resolved, why = await _resolve(ib, idea, cons_budget, net_liq=pot.net_liq)
    over_default = False
    if not resolved and not _stage_b_bound:

        resolved, why = await _resolve(ib, idea, deployable, net_liq=pot.net_liq)
        over_default = resolved is not None
    if resolved is not None and _credit:
        _ok_credit, _why_credit = credit_structure_ok(idea)
        if not _ok_credit:
            _daily_cap_rejected("credit_structure", _why_credit, idea, resolved)
            return None
        _capacity = collateral_capacity(
            required=required_collateral(resolved.strike, resolved.qty),
            deployed=_deployed_credit, net_liq=pot.net_liq,
            available_funds=pot.available_funds)
        if not _capacity.allowed:
            _daily_cap_rejected("collateral", _capacity.reasons, idea, resolved)
            return None






    if resolved is not None and pot.net_liq:
        _eg = capital_at_risk(resolved)
        if _eg > 0.95 * 30 * pot.net_liq:
            audit(audit_path, "daily_rec_gross_rejected", underlying=idea.underlying,
                  order=order_summary(resolved), est_gross=round(_eg), cap=round(30 * pot.net_liq))
            _daily_cap_rejected("gross", f"capital at risk ${_eg:,.0f} exceeds 30x-NetLiq cap",
                                idea, resolved)
            approval.post_proposal(token, channel,
                f":no_entry: *{idea.underlying}* {order_summary(resolved)} skipped — capital at risk "
                f"${_eg:,.0f} exceeds the 30x-NetLiq cap (${30*pot.net_liq:,.0f}). Too large for this ${pot.net_liq:,.0f} pot.")
            return None


    if resolved is not None and pot.net_liq and not _credit:
        _debit = resolved.limit * 100 * resolved.qty



        try:
            _book = await _open_book(ib, audit_path)
        except Exception as _obe:
            audit(audit_path, "budget_book_unreadable", underlying=idea.underlying,
                  order=order_summary(resolved), error=str(_obe) or type(_obe).__name__)
            approval.post_proposal(token, channel,
                f":no_entry: *{idea.underlying}* {order_summary(resolved)} skipped — the budget "
                f"gate could not value the open book: {_obe}")
            return None
        ok_b, why_b = construction.check_budget(_debit, resolved.dte, pot.net_liq,
                                                _book, CONS)
        if not ok_b:
            audit(audit_path, "budget_rejected", underlying=idea.underlying,
                  order=order_summary(resolved), reasons=why_b)
            _daily_cap_rejected("budget", why_b, idea, resolved)
            approval.post_proposal(token, channel,
                f":no_entry: *{idea.underlying}* {order_summary(resolved)} skipped — budget gate: "
                + "; ".join(why_b))
            return None


    _earn_unchecked = False
    _earn_waived = False
    _earn_warn = ""
    _earn_day_warning = False
    _exact = exact_spread_for(idea)
    if resolved is not None and not _credit:
        _entry = datetime.now(timezone.utc).date()
        try:
            _edays = research.days_to_earnings(idea.underlying)
        except Exception as _ee:
            print(f"[WARN] earnings lookup failed for {idea.underlying} (fail-open, unchecked): {_ee}")
            _edays = None
        _earn_date = (_entry + timedelta(days=_edays)) if _edays is not None else None
        _earn_unchecked = _earn_date is None
        resolved.earnings_date = _earn_date.isoformat() if _earn_date is not None else None
        resolved.earnings_unchecked = _earn_unchecked
        resolved.earnings_day_warning = (_earn_date == _entry)
        _earn_day_warning = resolved.earnings_day_warning






        ok_e, _earn_waived, why_e = exact_earnings_gate(
            _entry, resolved.expiry, _earn_date,
            intended_hold_for_gate(resolved, idea), _exact)
        if not ok_e:
            audit(audit_path, "earnings_blackout_rejected", underlying=idea.underlying,
                  order=order_summary(resolved), reason=why_e)
            _daily_cap_rejected("earnings_blackout", why_e, idea, resolved)
            approval.post_proposal(token, channel,
                f":no_entry: *{idea.underlying}* {order_summary(resolved)} skipped — {why_e}")
            return None
        if why_e and not _earn_waived:
            _earn_warn = why_e
            resolved.earnings_warn = why_e
            audit(audit_path, ("earnings_same_day_warning"
                  if _earn_day_warning else "earnings_overlap_disclosed"), phase="proposal",
                  underlying=idea.underlying, order=order_summary(resolved),
                  earnings_date=(_earn_date.isoformat() if _earn_date else None), reason=why_e)
        if _earn_waived:
            audit(audit_path, "exact_earnings_waiver", phase="proposal",
                  underlying=idea.underlying, order=order_summary(resolved),
                  expected_earnings_date=_exact.expected_earnings_date.isoformat(),
                  observed_earnings_date=_earn_date.isoformat(), normal_gate_reason=why_e,
                  max_debit=_exact.max_debit, exact_qty=_exact.qty)
    elif resolved is not None and _credit:



        _entry = datetime.now(timezone.utc).date()
        try:
            _edays = research.days_to_earnings(idea.underlying)
        except Exception as _ee:
            print(f"[WARN] earnings lookup failed for {idea.underlying}: {_ee}")
            _edays = None
        _earn_unchecked = (_edays is None and
                           not entry_safety.is_no_earnings_etf(idea.underlying))
        resolved.earnings_unchecked = _earn_unchecked
        if _edays is not None:
            _earn_date = _entry + timedelta(days=_edays)
            resolved.earnings_date = _earn_date.isoformat()
            if _edays == 0:
                _earn_day_warning = True
                resolved.earnings_day_warning = True
                _, _earn_warn = construction.earnings_ok(
                    _entry, resolved.expiry, _earn_date, CONS,
                    hold_days=intended_hold_for_gate(resolved, idea))
                resolved.earnings_warn = _earn_warn
                audit(audit_path, "earnings_same_day_warning", phase="proposal",
                      underlying=idea.underlying, order=order_summary(resolved),
                      earnings_date=resolved.earnings_date, reason=_earn_warn)






    _assign_warn = ""
    _assign_unchecked = False
    if resolved is not None and resolved.short_contract is not None:
        _entry_a = datetime.now(timezone.utc).date()
        try:
            _xdays = research.days_to_ex_dividend(idea.underlying)
        except Exception as _xe:
            print(f"[WARN] ex-div lookup failed for {idea.underlying} (fail-open, unchecked): {_xe}")
            _xdays = None
        _exdiv_date = (_entry_a + timedelta(days=_xdays)) if _xdays is not None else None
        _assign_unchecked = _exdiv_date is None
        ok_a, why_a = construction.assignment_risk_ok(
            resolved.short_strike, resolved.spot, resolved.right, resolved.expiry,
            _exdiv_date, resolved.dte, CONS)
        if not ok_a:
            audit(audit_path, "assignment_risk_rejected", underlying=idea.underlying,
                  order=order_summary(resolved), reason=why_a)
            _daily_cap_rejected("assignment_risk", why_a, idea, resolved)
            approval.post_proposal(token, channel,
                f":no_entry: *{idea.underlying}* {order_summary(resolved)} skipped — {why_a}")
            return None
        if why_a:
            _assign_warn = why_a
            audit(audit_path, "assignment_risk_warn", underlying=idea.underlying,
                  order=order_summary(resolved), reason=why_a)
    head = (f":calendar: *{label} — {idea.underlying}* {idea.direction} {idea.structure}\n"
            f"Conviction *{idea.conviction}/10* — {_score_tag(idea.conviction)}\n"
            + approval.model_attribution_line(model_identity, model_identity_source)
            + f"_Thesis:_ {idea.thesis}\n")
    if not resolved:
        _daily_cap_rejected("not_placeable", why, idea, None)
        approval.post_proposal(token, channel, head + f"_(not placeable: {why})_")
        return None
    if resolved.dte_adjusted:

        head += (f"_:calendar: expiry adjusted to *{resolved.dte} DTE* (model asked ~{idea.target_dte}; "
                 f"min-DTE floor {CONS.min_dte} — short DTE was the audit's biggest theta killer)_\n")
        audit(audit_path, "dte_adjusted", underlying=idea.underlying,
              requested_dte=idea.target_dte, adjusted_dte=resolved.dte, min_dte=CONS.min_dte)
    if _earn_day_warning:
        head = (":rotating_light: *EARNINGS TODAY — ENTRY WARNING*\n"
                f"{_earn_warn}\n\n") + head
    elif _earn_warn:
        head += f"_:warning: {_earn_warn}_\n"
    elif _earn_unchecked:

        head += ("_:grey_question: earnings date unknown — event risk UNCHECKED; "
                 "final earnings verification is still required before submission._\n")
    if _earn_waived:
        head += (":warning: *ONE-TIME USER-DIRECTED EARNINGS OVERRIDE:* the normal IV-crush "
                 f"blackout rejects this trade for earnings on {_earn_date.isoformat()}. "
                 "Only these exact legs, quantity and max debit are authorized; every other "
                 "safety gate remains binding.\n")
    if _assign_warn:


        head += f"_:warning: {_assign_warn}_\n"
    elif _assign_unchecked:

        head += "_:grey_question: ex-dividend date unknown — early-assignment risk UNCHECKED_\n"






    try:
        _cnotes = _concentration_notes(
            await _open_positions_for_risk(), idea.underlying,
            capital_committed(resolved),
            idea.is_index, risk.effective_pot(pot.net_liq, _RISK_LIMITS.pot_cap_usd), _RISK_LIMITS)
        for _txt, _akw in _cnotes:
            head += f"_{_txt}_\n"
            audit(audit_path, "concentration_warning", order=order_summary(resolved), **_akw)
    except Exception as _ce:
        print(f"[WARN] concentration check failed for {getattr(idea, 'underlying', '?')} (continuing): {_ce}")
    cost = capital_committed(resolved)






    if _credit:
        tp_pct, sl_pct, tp_price, sl_price = None, 0.0, None, None
    else:
        tp_pct, sl_pct = construction.clamp_tp_sl(
            idea.profit_target_pct, idea.stop_pct, CONS)
        _tp_note = construction.optional_take_profit_pct(idea.profit_target_pct)[1]
        if _tp_note:
            print(f"[TP] {idea.underlying}: {_tp_note}")
            audit(audit_path, "take_profit_refused", underlying=idea.underlying,
                  requested_profit_target_pct=idea.profit_target_pct, reason=_tp_note)
        tp_price = (resolved.limit * (1 + tp_pct / 100.0)) if tp_pct is not None else None
        sl_price = resolved.limit * (1 - sl_pct / 100.0)
    bind_protective_terms(resolved, tp_pct, sl_pct)
    pct_pot = (cost / pot.net_liq * 100) if pot.net_liq else 0.0
    if _credit:
        size_line = (f"Collateral ~${cost:,.0f} (*{pct_pot:.0f}% of pot*); executable credit "
                     f"~${resolved.net_credit_usd:,.0f}; max loss if assigned stock goes to zero "
                     f"~${resolved.credit_max_loss_usd:,.0f}.")
    elif over_default:
        size_line = (f":warning: ~${cost:,.0f} = *{pct_pot:.0f}% of pot* — ABOVE your {default_pct:.0%} "
                     f"default (1 contract is the smallest size). Tap :white_check_mark: only if you want this size.")
    else:
        size_line = (f"~${cost:,.0f} (*{pct_pot:.0f}% of pot*, your {default_pct:.0%} default). "
                     f"Reply `full size` to use ~${deployable:,.0f} (keeps a 5% cash buffer).")
    decision_id = entry_safety.new_decision_id()
    resolved.decision_id = decision_id
    resolved.decision_revision = 0
    resolved.model_identity = model_identity
    resolved.model_identity_source = model_identity_source
    resolved.intended_hold_days = getattr(idea, "intended_hold_days", None)
    resolved.thesis = str(getattr(idea, "thesis", "") or "")
    _auto_blockers = daily_autonomy_blockers(
        idea.underlying, market_context=market_context, technical_card=technical_card)
    _auto_candidate = bool(
        getattr(_RESOLVED_CONFIG, "auto_approve_within_gates", False)
        and not _auto_blockers
    )
    if getattr(_RESOLVED_CONFIG, "auto_approve_within_gates", False) and _auto_blockers:
        audit(audit_path, "auto_approve_withheld", underlying=idea.underlying,
              decision_id=decision_id, reasons=list(_auto_blockers), source="daily_slate")
        head = (":lock: *Auto-execution withheld — your tap is required.*\n"
                + "\n".join("• " + reason for reason in _auto_blockers) + "\n" + head)
    _auto_instruction = (
        ":robot_face: *Autonomous final-gate review* — no tap is required. The exact order "
        "will submit only if the fresh broker book, quote, event, and every risk gate pass.\n"
        if _auto_candidate else ""
    )
    if _exact is not None:
        msg = (_auto_instruction + head + f"*Exact order:* `{order_summary(resolved)}`\n"
               f"{size_line} Max loss = the debit. Executable ask must remain <= "
               f"*${_exact.max_debit:.2f}*.\n"
               f"*Sell levels (auto):* {_tp_level_text(tp_pct, tp_price)} | "
               f"stop ~${sl_price:.2f} (-{sl_pct:.0f}%)\n"
               + ("Structure and quantity edits are refused; autonomous submission remains "
                  "bound to these terms and fresh gates.\n" if _auto_candidate else
                  ":point_down: *Tap :white_check_mark: to BUY these exact terms* or :x: to skip. "
                  "Structure and quantity edits are refused; a material quote change is shown "
                  "for reapproval.\n")
               + (f"_Decision ID: `{decision_id}`._" if _auto_candidate else
                  f"_Decision ID: `{decision_id}` — approval expires in "
                  f"{_APPROVAL_TTL_MINUTES} minutes._"))
    elif _stage_b_bound:
        action_word = "SELL the cash-secured put" if _credit else "BUY"
        msg = (_auto_instruction + head + f"*Order:* `{order_summary(resolved)}`\n"
               f"{size_line}\n"
               + ("Stage-B contract/structure/size terms are fixed for autonomous final-gate "
                  "review.\n" if _auto_candidate else
                  f":point_down: *Tap :white_check_mark: to {action_word}* or :x: to skip. "
                  "Stage-B contract/structure/size edits are not accepted; a changed quote is "
                  "shown for reapproval.\n")
               + (f"_Decision ID: `{decision_id}`._" if _auto_candidate else
                  f"_Decision ID: `{decision_id}` — approval expires in "
                  f"{_APPROVAL_TTL_MINUTES} minutes._"))
    else:
        msg = (_auto_instruction + head + f"*Order:* `{order_summary(resolved)}`\n"
               f"{size_line} Max loss = the debit.\n"
               f"*Sell levels (auto):* {_tp_level_text(tp_pct, tp_price)} | "
               f"stop ~${sl_price:.2f} (-{sl_pct:.0f}%)\n"
               + ("Autonomous final-gate review uses these exact terms; no reply is consumed.\n"
                  if _auto_candidate else
                  ":point_down: *Tap :white_check_mark: to BUY*, or REPLY to tweak: `full size`, "
                  "levels (`tp 60 stop 30`), direction (`flip` / `make it bearish`), or "
                  "`just the call` / `make it a spread`. :x: to skip.\n")
               + (f"_Decision ID: `{decision_id}`._" if _auto_candidate else
                  f"_Decision ID: `{decision_id}` — approval expires in "
                  f"{_APPROVAL_TTL_MINUTES} minutes._"))
    ts = approval.post_proposal(token, channel, msg, seed_reactions=not _auto_candidate)
    if ts:
        pending.append((ts, resolved, tp_pct, sl_pct, idea, over_default,
                        time.monotonic(), decision_id, 0, candidates, raw_strategist,
                        cot, market_context, technical_card, _auto_candidate))
        audit(audit_path, audit_event, underlying=idea.underlying,
              conviction=idea.conviction, order=order_summary(resolved),
              profit_target_pct=tp_pct, stop_pct=sl_pct, over_default=over_default,
              decision_id=decision_id)




        try:
            trade_capture.capture_decision(
                trade_capture.dataset_dir(JOURNAL_PATH), source="daily_slate",
                symbol=idea.underlying, right=resolved.right, strike=resolved.strike,
                expiry=resolved.expiry,
                structure=("cash secured put" if is_credit(resolved) else
                           ("spread" if resolved.short_contract is not None else "single")),
                con_id=None, chosen_idea=idea, candidates=candidates,
                raw_strategist=raw_strategist, cot=cot, market_context=market_context, regime=regime,
                technical_card=technical_card,
                construction={"tp_pct": tp_pct, "sl_pct": sl_pct, "dte": resolved.dte,
                              "dte_adjusted": resolved.dte_adjusted, "qty": resolved.qty,
                              "limit": resolved.limit, "over_default": over_default,
                              "short_strike": resolved.short_strike},
                sizing={"cost": cost, "pct_pot": pct_pot, "net_liq": pot.net_liq,
                        "available_funds": pot.available_funds},
                extra={"label": label, "order": order_summary(resolved),
                       "decision_id": decision_id}, decision_id=decision_id,
                revision=0, event="proposal", model_identity=model_identity,
                model_identity_source=model_identity_source,
                final_contract=contract_snapshot(resolved))
        except Exception as _dce:
            print(f"[WARN] daily-slate capture_decision failed (continuing): {_dce}")
    return ts


async def run(args):
    global CASH_BUFFER_PCT, CONS, CONN, JOURNAL_PATH, ERROR_CHANNEL, _RISK_LIMITS
    global _RESOLVED_CONFIG, _RUNTIME_IDENTITY
    _RESOLVED_CONFIG = load_config(args.config)
    _RUNTIME_IDENTITY = freeze_runtime_identity(
        _RESOLVED_CONFIG, require_clean_code=True)
    print("[IDENTITY] code_version=%s policy_version=%s" %
          (_RUNTIME_IDENTITY.code_version, _RUNTIME_IDENTITY.policy_version))


    cfg = execution_config_dict(_RESOLVED_CONFIG)



    if (cfg.get("caps") or {}).get("tp_tiers"):
        print("[WARN] config.yaml caps.tp_tiers is present but IGNORED — mechanical pot-tiered "
              "take-profit was removed by Sol audit R5 R2 / RULING_TAKE_PROFIT.md. Delete it.")
    ibc, tr = cfg.get("ib", {}), cfg.get("trading", {})
    CASH_BUFFER_PCT = float(tr.get("cash_buffer_pct", 0.05))
    CONS = construction_from_dict(cfg.get("construction"))
    _RISK_LIMITS = entry_safety.risk_limits_from_config(tr)



    _CAPS = cfg.get("caps") or {}





    config.require_present(_CAPS, "caps")
    _MAX_ORDERS_PER_DAY = _CAPS["max_orders_per_day"]
    _MAX_NOTIONAL_PER_DAY = _CAPS["max_notional_per_day"]
    try:
        _THROTTLE_STORE = EntryThrottleStore.for_state_path(
            (cfg.get("state") or {}).get("path", "./exitmgr_state.json"))
    except Exception as _tse:
        print(f"[WARN] entry throttle store unavailable (continuing): {_tse}")
        _THROTTLE_STORE = None
    ERROR_CHANNEL = tr.get("error_channel", "") or tr.get("alerts_channel", "")
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    channel = tr.get("slack_channel", "")
    approver_ids = set(tr.get("approver_ids", []))
    audit_path = tr.get("audit_path", "./audit.jsonl")
    audit(audit_path, "process_runtime_identity", mode="daily_slate",
          **identity_fields(_RUNTIME_IDENTITY))
    journal_path = cfg.get("journal", {}).get("path", "./trades.log")
    JOURNAL_PATH = journal_path



    _markers = entry_safety.entry_markers_clear(
        config_path=args.config,
        kill_switch_path=(cfg.get("kill_switch") or {}).get("path"))
    if not _markers.allowed:
        print("[BLOCKED] " + "; ".join(_markers.reasons))
        return 2

    conn = IBConnection(host=ibc.get("host", "127.0.0.1"), port=ibc.get("port", 4001),
                        client_id=(getattr(args, "client_id", None) or CLIENT_ID),
                        market_data_type=ibc.get("market_data_type", 1),
                        startup_completed_orders=False)
    CONN = conn
    if not await conn.connect(retries=10, retry_delay=30):

        print("[WARN] no IBKR connection -- attempting IBC gateway restart")
        try:
            subprocess.run(["launchctl", "kickstart", "-k", "gui/%d/ai.alfred.ibgateway" % os.getuid()],
                           timeout=30, check=False)
        except Exception as e:
            print("[WARN] gateway kickstart failed: %s" % e)
        await asyncio.sleep(90)
        if not await conn.connect(retries=4, retry_delay=20):

            print("[ERROR] no IBKR connection after restart")
            if token and channel:
                approval.post_proposal(token, channel,
                    ":warning: *Daily slate skipped -- IBKR gateway unreachable.* An auto-restart was tried; "
                    "it is likely logged out (IBKR's ~weekly forced 2FA, which auto-restart cannot bypass). "
                    "Do 2FA via ~/trader_host-screen.sh, then run slate-now (or ask Claude) for today's slate.")
            return 1
    ib = conn.ib
    try:
        disc_ts, disc_cands = None, []



        _defer_this_opening = set()
        _ape_rows, _ape_addable = [], set()
        _raw_slate = None
        _slate_cot = None
        _slate_identity = None


















        _slate_identity_source = None
        brief = None
        _slate_price_stats = None
        if args.ticker:





            _ud_idea = user_directed_idea(
                args, fallback_hold_days=tr.get("intended_hold_days_fallback"))
            direction, structure = _ud_idea.direction, _ud_idea.structure













            _ud_model_note = None
            if not getattr(args, "no_model_thesis", False):
                try:




                    _bfile, _brief_txt = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                      "audit.jsonl"), None
                    with open(_bfile) as _bf:
                        for _bl in _bf:
                            try:
                                _be = json.loads(_bl)
                            except Exception:
                                continue
                            if _be.get("event") == "strategist_brief" and _be.get("brief"):
                                _brief_txt = _be["brief"]
                    if not _brief_txt:
                        raise RuntimeError("no research brief available yet")


                    _mv = await _await_slate_model(
                        propose_intents,
                        tr.get("llm_endpoint"), tr.get("llm_model"), _brief_txt,
                        ticker=args.ticker.upper(), timeout=1200,
                        **STAGE_A_REQUEST_DIRECTED)
                    if _mv:
                        _mi = _mv[0]
                        _ud_idea.thesis = _mi.thesis or _ud_idea.thesis
                        _ud_idea.conviction = int(_mi.conviction)



















                        _mi_hold = positive_hold_days(
                            getattr(_mi, "intended_hold_days", None))
                        _ud_idea._model_intended_hold_days = _mi_hold
                        _ud_model_note = "model endorses (conviction %d)" % _mi.conviction
                        if _mi_hold is not None and _mi_hold != _ud_idea.intended_hold_days:
                            audit(audit_path, "user_directed_hold_preserved",
                                  underlying=args.ticker.upper(),
                                  user_intended_hold_days=_ud_idea.intended_hold_days,
                                  user_hold_source=getattr(
                                      _ud_idea, "_intended_hold_days_source", None),
                                  model_intended_hold_days=_mi_hold)
                            _ud_model_note += (
                                "; model would hold %dd, yours (%s) %dd kept"
                                % (_mi_hold,
                                   getattr(_ud_idea, "_intended_hold_days_source", "explicit"),
                                   _ud_idea.intended_hold_days))
                    else:
                        _ud_idea.thesis = ("USER-DIRECTED. The model DECLINED to endorse %s on "
                                           "today's evidence; this trade is an operator's call, not "
                                           "the model's. Original note: %s"
                                           % (args.ticker.upper(), args.thesis))
                        _ud_model_note = "model DECLINED to endorse this name"
                except Exception as _mte:
                    _ud_model_note = "model view unavailable (%s)" % str(_mte)[:80]
            ideas = [_ud_idea]
            audit(audit_path, "user_directed_proposal", underlying=args.ticker.upper(),
                  direction=direction, structure=structure, dte=args.dte, delta=args.delta,
                  model_view=_ud_model_note, thesis=_ud_idea.thesis[:300])
        else:





            _setup_snapshot = setup_watchlist.read_watchlist(SETUP_WATCHLIST_PATH)
            _blocked = {str(n).upper() for n in tr.get("blocked_names", [])}
            _setup_targets = tuple(
                t for t in _setup_snapshot.targets
                if t.symbol not in _blocked and t.symbol != "TSLA")
            _setup_symbols = [t.symbol for t in _setup_targets]
            _approved = {n.upper() for n in tr.get("approved_names", [])}
            _all = sorted({"SPY", "QQQ", "IWM"} | _approved | set(_setup_symbols))
            _core = ["SPY", "QQQ", "IWM"]
            _rotation_pool = [n for n in sorted(_approved) if n not in _core
                              and n not in set(_setup_symbols)]
            _off = (datetime.now(timezone.utc).timetuple().tm_yday
                    % max(1, len(_rotation_pool)))
            _rotated = _rotation_pool[_off:] + _rotation_pool[:_off]
            _base_names = setup_watchlist.prioritize_for_opening(
                _core, _rotated, _setup_symbols, non_core_limit=35)
            audit(audit_path, "opening_setup_watchlist_loaded",
                  watchlist_path=SETUP_WATCHLIST_PATH,
                  active_symbols=_setup_symbols,
                  ignored_blocked=sorted(set(_setup_snapshot.symbols) - set(_setup_symbols)),
                  warnings=list(_setup_snapshot.warnings),
                  watchlist_sha256=_setup_snapshot.sha256,
                  trade_authority=False)
            _ape_feed, _ape_probe_rows = await _load_apewisdom_pool(ib, tr, audit_path)
            _candidate_names = apewisdom.merge_research_universe(_base_names, _ape_probe_rows)
            today = str(datetime.now(timezone.utc).date())
            quotes = await fetch_universe_quotes(ib, _candidate_names)
            _ape_cfg = tr.get("apewisdom_discovery") or {}
            _ape_limit = max(1, min(20, int(_ape_cfg.get("research_limit", 8))))
            _ape_quoted_rows = [r for r in _ape_probe_rows
                                if usable_price((quotes.get(r["ticker"]) or {}).get("last"))]
            _ape_rows = _ape_quoted_rows[:_ape_limit]
            names = apewisdom.merge_research_universe(_base_names, _ape_rows)
            _ape_names = {r["ticker"] for r in _ape_rows}




            if _ape_feed is not None:
                audit(audit_path, "apewisdom_research_universe",
                      candidates=sorted(_ape_names), count=len(_ape_names),
                      excluded_no_live_quote=sorted(
                          r["ticker"] for r in _ape_probe_rows if r not in _ape_quoted_rows),
                      excluded_by_research_cap=[r["ticker"] for r in _ape_quoted_rows[_ape_limit:]],
                      attention_metrics_in_main_brief=False,
                      trade_authority=False, training_eligible=False)
            data = await research.gather(ib, names, single_names=[n for n in names if n not in ("SPY", "QQQ", "IWM")])
            try:



                _slate_book = await _prefilter_positions_for_risk(ib, CONN, audit_path)
            except Exception as _book_error:
                reason = str(_book_error) or type(_book_error).__name__
                print(f"[WARN] slate book fetch failed; strategist skipped: {reason}")
                audit(audit_path, "strategist_skipped", reason="book_unreadable: " + reason)
                approval.post_proposal(
                    token, channel,
                    ":warning: *Daily slate skipped* — the open-position book could not be "
                    f"verified ({reason}). No model trade was requested.")
                return 1
            _brief_pot = await get_pot_snapshot(ib)
            _brief_account = entry_safety.account_snapshot_valid(_brief_pot)
            if not _brief_account.allowed:
                reason = "; ".join(_brief_account.reasons)
                audit(audit_path, "strategist_skipped", reason="account_snapshot_invalid: " + reason)
                approval.post_proposal(
                    token, channel,
                    ":warning: *Daily slate skipped* — live account sizing data is invalid or "
                    f"unavailable ({reason}). No model trade was requested.")
                return 1
            _baseline_path = Path(args.config).resolve().parent / tr.get(
                "baseline_path", "./day_baseline.json")
            _brief_baseline = entry_safety.day_start_value(_baseline_path, _trading_day())
            if isinstance(_brief_baseline, entry_safety.SafetyResult):
                reason = "; ".join(_brief_baseline.reasons)
                audit(audit_path, "strategist_skipped", reason="baseline_unreadable: " + reason)
                approval.post_proposal(
                    token, channel, f":warning: *Daily slate skipped* — {reason}. No model "
                    "trade was requested.")
                return 1
            _eligible_names, _eligibility_rejections = entry_eligible_universe(
                names,
                quote_prices={n: (quotes.get(n) or {}).get("last") for n in names},
                research_symbols={n for n, st in (data.get("price_stats") or {}).items() if st},
                net_liq=_brief_pot.net_liq,
                available_funds=_brief_pot.available_funds,
                positions=_slate_book, pot_day_start=_brief_baseline,
                approved_names={str(n).upper() for n in tr.get("approved_names", [])},
                limits=_RISK_LIMITS, require_autonomous=False)
            audit(audit_path, "entry_eligible_universe",
                  eligible=list(_eligible_names),
                  rejected={k: list(v) for k, v in _eligibility_rejections.items()},
                  probe_notional_usd=1.0, exact_contract_gate_still_required=True)
            names = list(_eligible_names)
            _ape_names = {t for t in _ape_names if t in set(names)}
            if not names:
                approval.post_proposal(
                    token, channel,
                    ":pause_button: *Daily slate* — no name has both fresh research and current "
                    "entry capacity. The model was not asked to invent an unexecutable trade.")
                audit(audit_path, "strategist_skipped", reason="empty_entry_eligible_universe")
                return 0
            _fresh_setup_symbols = {
                n for n in names
                if usable_price((quotes.get(n) or {}).get("last"))
            }
            _setup_targets_for_brief = tuple(
                t for t in _setup_targets if t.symbol in _fresh_setup_symbols)
            _setup_targets_omitted = sorted(
                set(_setup_symbols) - {t.symbol for t in _setup_targets_for_brief})
            if _setup_targets_omitted:
                audit(audit_path, "opening_setup_watchlist_context_omitted",
                      symbols=_setup_targets_omitted,
                      reason="not in bounded fresh-data opening universe or no usable quote",
                      trade_authority=False)
            brief = research.build_brief(
                today=today, quotes=quotes, universe=names,
                allow_any_name=False, book=_slate_book,
                net_liq=_brief_pot.net_liq,
                available_funds=_brief_pot.available_funds,
                market_data_type=_RESOLVED_CONFIG.ib.market_data_type,
                max_premium_pct=_RESOLVED_CONFIG.construction.max_premium_pct,
                setup_watchlist_context=setup_watchlist.render_for_brief(
                    setup_watchlist.WatchlistSnapshot(
                        targets=_setup_targets_for_brief,
                        warnings=_setup_snapshot.warnings,
                        sha256=_setup_snapshot.sha256)),
                **data)
            _slate_price_stats = data.get("price_stats")






            _slate_gen = slate_active_guard(); _slate_gen.__enter__()
            broad_cands = []
            try:
                broad_cands = await _await_slate_model(
                    discover_names,
                    tr.get("llm_endpoint"), tr.get("llm_model"), brief,



                    exclude=set(_setup_symbols) | set(_core) | _ape_names, timeout=600,
                    blocked=tr.get("blocked_names", []))
            except Exception as e:
                audit(audit_path, "discovery_error", source="ordinary", error=str(e))

            ape_cands = []
            _ape_new_rows = [r for r in _ape_rows if r["ticker"] not in set(_all)]
            if _ape_new_rows:
                try:
                    _ape_discovery_brief = apewisdom.discovery_context(
                        brief, _ape_new_rows, watched=_all,
                        price_stats=_slate_price_stats)
                    _ape_reviewed = await _await_slate_model(
                        discover_names,
                        tr.get("llm_endpoint"), tr.get("llm_model"), _ape_discovery_brief,
                        exclude=set(_all), timeout=600, blocked=tr.get("blocked_names", []))
                    ape_cands = apewisdom.bind_reviewed_candidates(
                        _ape_reviewed, _ape_new_rows, watched=_all,
                        price_stats=_slate_price_stats)
                    _ape_addable = {t for t, _ in ape_cands}
                    audit(audit_path, "apewisdom_discovery",
                          eligible=[r["ticker"] for r in _ape_new_rows],
                          candidates=[t for t, _ in ape_cands],
                          signal_type="attention_only", trade_authority=False,
                          training_eligible=False)
                except Exception as e:
                    audit(audit_path, "discovery_error", source="apewisdom", error=str(e))

            cands = _merge_discovery_candidates(broad_cands, ape_cands)
            if cands:
                disc_cands = [t for t, _ in cands]
                _defer_this_opening |= set(disc_cands)
                _new_setup_targets, _existing_setup_targets, _setup_write_errors = [], [], []
                for _ticker, _why in cands:
                    try:
                        if setup_watchlist.add_target(
                                _ticker,
                                setup=_why or "Opening discovery candidate; re-underwrite fresh.",
                                source=f"opening discovery {today}",
                                path=SETUP_WATCHLIST_PATH):
                            _new_setup_targets.append(_ticker)
                        elif _ticker in set(setup_watchlist.read_watchlist(
                                SETUP_WATCHLIST_PATH).symbols):
                            _existing_setup_targets.append(_ticker)
                    except Exception as _swe:
                        _setup_write_errors.append({"ticker": _ticker, "error": str(_swe)})
                audit(audit_path, "opening_setup_watchlist_maintained",
                      candidates=disc_cands, added=_new_setup_targets,
                      already_active=_existing_setup_targets,
                      errors=_setup_write_errors, trade_authority=False)
                _persisted = sorted(set(_new_setup_targets) | set(_existing_setup_targets))
                _failed = sorted(e["ticker"] for e in _setup_write_errors)
                _persist_note = (
                    "\n_Recorded for fresh-data review in the next opening slate: "
                    + ", ".join(f"*{t}*" for t in _persisted) + "._"
                    if _persisted else "")
                _failure_note = (
                    "\n:warning: _Not persisted: "
                    + ", ".join(f"*{t}*" for t in _failed)
                    + ". They are still excluded from today's slate, but will not carry "
                    "forward until the watchlist write succeeds._"
                    if _failed else "")
                disc_ts = approval.post_proposal(token, channel,
                    ":mag: *Names to consider* (entry-timing targets; context only):\n"
                    + "\n".join(f"  • *{t}* — {why}" for t, why in cands)
                    + _persist_note + _failure_note
                    + "\n_Listing one is not approval to trade. It cannot become a trade in "
                    "this opening run._"
                    + "\n_Reply *add TICKER* (or *add all*) to add it to the approved research "
                    "universe too._")
                audit(audit_path, "discovery", candidates=disc_cands,
                      apewisdom_candidates=[t for t, _ in ape_cands])

            try:









                _slate_identity_source = "unknown"
                _res = await _await_slate_model(
                    propose_intents,
                    tr.get("llm_endpoint"), tr.get("llm_model"), brief,
                    timeout=1200, recommend=True, **STAGE_A_REQUEST_SLATE)


                if isinstance(_res, tuple) and len(_res) == 4:
                    intents, _raw_slate, _slate_cot, _slate_identity = _res


                    if _slate_identity is not None:
                        _slate_identity_source = "meta"
                elif isinstance(_res, tuple) and len(_res) == 3:
                    intents, _raw_slate, _slate_cot = _res
                elif isinstance(_res, tuple) and len(_res) == 2:
                    intents, _raw_slate = _res
                else:
                    intents = _res
                try:
                    intents = list(intents or [])
                    _stage_a_raw_intent_count = len(intents)
                except TypeError:
                    _stage_a_raw_intent_count = None
                _eligible_set = set(_eligible_names)
                _out_of_universe = []
                _inside_universe = []
                for _intent in intents or []:
                    _sym = str(getattr(
                        _intent, "underlying", getattr(_intent, "symbol", ""))).upper()
                    if _sym not in _eligible_set:
                        _out_of_universe.append(_sym)
                    else:
                        _inside_universe.append(_intent)
                intents = _inside_universe
                if _out_of_universe:
                    audit(audit_path, "stage_a_outside_eligible_universe_dropped",
                          symbols=sorted(set(_out_of_universe)),
                          eligible=sorted(_eligible_set))
                _deferred_stage_a = []
                if _defer_this_opening and isinstance(intents, list):
                    _kept_intents = []
                    for _intent in intents:
                        _intent_symbol = str(getattr(
                            _intent, "underlying", getattr(_intent, "symbol", ""))).upper()
                        if _intent_symbol in _defer_this_opening:
                            _deferred_stage_a.append(_intent_symbol)
                        else:
                            _kept_intents.append(_intent)
                    intents = _kept_intents
                    if _deferred_stage_a:
                        audit(audit_path, "opening_setup_target_deferred",
                              symbols=sorted(set(_deferred_stage_a)),
                              reason="new setup target cannot become a same-opening trade",
                              watchlist_persisted=sorted(
                                  set(_new_setup_targets) | set(_existing_setup_targets)),
                              trade_authority=False)
                try:
                    _stage_a_intent_count = len(intents or [])
                except TypeError:
                    _stage_a_intent_count = None
                ideas, _stage_b_outcomes, _post_construction_intent_count = await _materialize_stage_b(
                    ib, intents, _brief_pot, tr, audit_path, with_outcomes=True)
            except Exception as e:
                audit(audit_path, "propose_error", error=str(e))
                approval.post_proposal(token, channel,
                    f":warning: *Daily slate* — couldn't reach the model to generate ideas ({e}). Retry with slate-now.")
                print(f"[ERROR] propose failed: {e}")
                _slate_gen.__exit__(None, None, None)
                return 1
            ideas.sort(key=lambda i: -i.conviction)
            audit(audit_path, "apewisdom_trade_consideration",
                  researched=sorted(_ape_names),
                  proposed=sorted(i.underlying for i in ideas if i.underlying in _ape_names),
                  attention_metrics_in_main_brief=False, trade_authority=False)
            audit(audit_path, "daily_recommend", count=len(ideas),
                  scores=[i.conviction for i in ideas])
            _slate_gen.__exit__(None, None, None)
            _stage_b_failed = bool(stage_b_technical_failures(_stage_b_outcomes))
            if _stage_b_failed and ideas:


                await asyncio.to_thread(
                    approval.post_proposal, token, channel,
                    ":warning: *Daily slate contract evaluation incomplete* — "
                    + pipeline_notice.safe_detail(stage_b_failure_detail(_stage_b_outcomes)))
            if not ideas:
                if _stage_b_failed:
                    _no_trade_reason, _training_ok = "stage_b_error", False
                    _message = (":warning: *Daily slate* — contract evaluation had technical "
                                "failures and produced no actionable contract. This is not "
                                "a model abstention. "
                                + pipeline_notice.safe_detail(stage_b_failure_detail(_stage_b_outcomes)))
                elif _deferred_stage_a and _stage_a_intent_count == 0:
                    _no_trade_reason, _training_ok = "setup_target_deferred", False
                    _message = (":hourglass_flowing_sand: *Daily slate* — the strategist named "
                                + ", ".join(f"*{t}*" for t in sorted(set(_deferred_stage_a)))
                                + " after first identifying them as entry-timing targets. They "
                                "were deferred for a later fresh-data opening review; no "
                                "same-opening chase was proposed.")
                elif _stage_a_intent_count == 0:
                    _no_trade_reason, _training_ok = "empty_slate", True
                    _message = (":calendar: *Daily slate* — the strategist explicitly proposed "
                                "no trade today.")
                elif _post_construction_intent_count == 0:
                    _no_trade_reason, _training_ok = "construction_rejected", False
                    _message = (f":no_entry_sign: *Daily slate* — the strategist proposed "
                                f"{_stage_a_intent_count} idea(s), but deterministic construction "
                                "rejected all of them. This is not a model abstention.")
                else:
                    _no_trade_reason, _training_ok = "stage_b_rejected", False
                    _message = (f":no_entry_sign: *Daily slate* — the strategist proposed "
                                f"{_stage_a_intent_count} idea(s), but no contract survived "
                                "candidate construction/Stage B. This is not an empty slate.")
                approval.post_proposal(token, channel,
                    _message)
                audit(audit_path, "daily_pipeline_no_trade", reason=_no_trade_reason,
                      stage_a_intent_count=_stage_a_intent_count,
                      post_construction_intent_count=_post_construction_intent_count,
                      stage_b_outcomes=_stage_b_outcomes,
                      training_eligible=_training_ok)


                try:
                    trade_capture.capture_no_trade(
                        trade_capture.dataset_dir(JOURNAL_PATH), source="daily_slate",
                        reason=_no_trade_reason, raw_strategist=_raw_slate, cot=_slate_cot,
                        market_context=brief, model_identity=_slate_identity,
                        model_identity_source=_slate_identity_source,
                        training_eligible=_training_ok,
                        extra={"stage_a_intent_count": _stage_a_intent_count,
                               "post_construction_intent_count": _post_construction_intent_count,
                               "stage_b_outcomes": _stage_b_outcomes})
                except Exception as _ce:
                    print(f"[WARN] daily-slate no_trade capture failed (continuing): {_ce}")
                print("[INFO] no ideas"); return 1 if _stage_b_failed else 0

        pot = await get_pot_snapshot(ib)
        default_pct = float(tr.get("max_trade_pct", 0.12))
        cons_budget = min(pot.available_funds, default_pct * pot.net_liq)



        pending = []
        placed_watch = []



        try:
            from portfolio import review_positions, format_synopsis
            _top = ideas[0]
            _rev = await review_positions(ib, idea={"symbol": _top.underlying, "structure": _top.structure,
                                                    "est_cost_usd": round(_top.est_debit_usd), "conviction": _top.conviction})
            if _rev.get("book"):
                approval.post_proposal(token, channel, format_synopsis(_rev))
                audit(audit_path, "book_review", reviews=_rev.get("reviews"), rotation=_rev.get("rotation"))
        except Exception as _e:
            audit(audit_path, "review_error", error=str(_e))
        for idea in ideas:

            await _post_idea(ib, idea, pot, default_pct, token, channel, audit_path, pending,
                             candidates=ideas, raw_strategist=_raw_slate, cot=_slate_cot, market_context=brief,
                             technical_card=_slate_price_stats, model_identity=_slate_identity,
                             model_identity_source=_slate_identity_source)


        deadline = time.monotonic() + args.watch_mins * 60
        done = set()
        added_watch = set()


        while time.monotonic() < deadline and (
                ((pending or disc_cands)
                 and (len(done) < len(pending) or len(added_watch) < len(disc_cands)))
                or any(not w["filled_logged"] for w in placed_watch)):

            if disc_ts and disc_cands and len(added_watch) < len(disc_cands):
                rep = approval._api("conversations.replies", token, {"channel": channel, "ts": disc_ts}, http_post=False)
                want = set()
                for m in (rep.get("messages", []) if rep.get("ok") else []):
                    if m.get("ts") == disc_ts:
                        continue
                    if approver_ids and m.get("user") not in approver_ids:
                        continue
                    want |= set(approval.parse_add_tickers(m.get("text", ""), disc_cands))
                new = sorted(want - added_watch)
                if new:






                    _fresh_tr = tr
                    _fresh_blocked = {str(t).upper() for t in _fresh_tr.get("blocked_names", [])}
                    _ape_row_by_ticker = {r["ticker"]: r for r in _ape_rows}
                    _accepted_new, _rejected_new = [], []
                    for _ticker in new:
                        if _ticker in _fresh_blocked or _ticker == "TSLA":
                            _rejected_new.append((_ticker, "currently_blocked"))
                            continue
                        if _ticker in _ape_addable:
                            _row, _reason = await _probe_apewisdom_row(
                                ib, _ape_row_by_ticker[_ticker],
                                _fresh_tr.get("blocked_sector_keywords", []),
                                asyncio.Semaphore(1))
                            if _row is None:
                                _rejected_new.append((_ticker, _reason))
                                continue
                        _accepted_new.append(_ticker)
                    new = _accepted_new
                    if _rejected_new:
                        audit(audit_path, "discovery_add_rejected_recheck",
                              rejected=[{"ticker": t, "reason": r} for t, r in _rejected_new])
                        approval.post_proposal(token, channel,
                            ":warning: Not added after the fresh eligibility recheck: "
                            + ", ".join(f"*{t}* ({r})" for t, r in _rejected_new))
                if new:
                    really = _append_watchlist(args.config, new)
                    added_watch |= set(new)
                    if really:
                        approval.post_proposal(token, channel,
                            f":white_check_mark: Added to watchlist: *{', '.join(really)}*")
                        audit(audit_path, "watchlist_added", tickers=really)



                        min_conv = float(tr.get("add_suggest_min_conviction", 6))
                        for tk in really:





                            if tk in set(setup_watchlist.read_watchlist(
                                    SETUP_WATCHLIST_PATH).symbols):
                                approval.post_proposal(
                                    token, channel,
                                    f":hourglass_flowing_sand: *{tk}* is now an approved name and "
                                    "a setup target. It will be rechecked with fresh market data "
                                    "in the next opening slate; no same-session trade was proposed.")
                                audit(audit_path, "add_suggest_deferred_to_opening_watchlist",
                                      ticker=tk, trade_authority=False)
                                continue
                            _one_raw = None
                            _one_cot = None
                            _one_identity = None
                            _one_identity_source = None
                            _one_brief = brief
                            _one_price_stats = _slate_price_stats
                            try:
                                _one_pot = await get_pot_snapshot(ib)
                                _one_account = entry_safety.account_snapshot_valid(_one_pot)
                                if not _one_account.allowed:
                                    raise RuntimeError(
                                        "account snapshot invalid: " + "; ".join(_one_account.reasons))
                                if tk in _ape_addable:



                                    _one_names = ["SPY", "QQQ", "IWM", tk]
                                    _one_quotes = await fetch_universe_quotes(ib, _one_names)
                                    if not usable_price((_one_quotes.get(tk) or {}).get("last")):
                                        raise RuntimeError("fresh underlying quote unavailable")
                                    _one_data = await research.gather(
                                        ib, _one_names, single_names=[tk])
                                    _one_book = await _open_positions_for_risk()
                                    _one_brief = research.build_brief(
                                        today=str(datetime.now(timezone.utc).date()),
                                        quotes=_one_quotes, universe=_one_names,
                                        allow_any_name=False, book=_one_book,
                                        net_liq=_one_pot.net_liq,
                                        available_funds=_one_pot.available_funds,
                                        market_data_type=_RESOLVED_CONFIG.ib.market_data_type,
                                        max_premium_pct=(
                                            _RESOLVED_CONFIG.construction.max_premium_pct),
                                        **_one_data)
                                    _one_price_stats = _one_data.get("price_stats")
                                else:
                                    _one_brief = research.with_account_sizing_snapshot(
                                        _one_brief, net_liq=_one_pot.net_liq,
                                        available_funds=_one_pot.available_funds,
                                        max_premium_pct=(
                                            _RESOLVED_CONFIG.construction.max_premium_pct))
                                with slate_active_guard():





                                    _one_identity_source = "unknown"
                                    _one = await _await_slate_model(
                                        propose_intents,
                                        tr.get("llm_endpoint"), tr.get("llm_model"),
                                        _one_brief, ticker=tk, timeout=1200,
                                        **STAGE_A_REQUEST_SLATE)
                                if isinstance(_one, tuple) and len(_one) == 4:
                                    _one_intents, _one_raw, _one_cot, _one_identity = _one
                                    if _one_identity is not None:
                                        _one_identity_source = "meta"
                                elif isinstance(_one, tuple) and len(_one) == 3:
                                    _one_intents, _one_raw, _one_cot = _one
                                elif isinstance(_one, tuple) and len(_one) == 2:
                                    _one_intents, _one_raw = _one
                                else:
                                    _one_intents = _one
                                with slate_active_guard():
                                    _one_ideas = await _materialize_stage_b(
                                        ib, _one_intents, _one_pot, tr, audit_path)
                                idea = _one_ideas[0] if _one_ideas else None
                            except Exception as e:
                                audit(audit_path, "add_suggest_error", ticker=tk, error=str(e))
                                continue
                            if not idea:
                                continue
                            if idea.conviction < min_conv:
                                approval.post_proposal(token, channel,
                                    f":information_source: *{tk}*: best idea today is {idea.direction} "
                                    f"{idea.structure} at conviction *{idea.conviction}/10* — below your same-day "
                                    f"bar ({min_conv:.0f}). It'll ride the daily slate.")
                                audit(audit_path, "add_suggest_below_bar", ticker=tk, conviction=idea.conviction)
                                continue
                            snap = await get_pot_snapshot(ib)
                            await _post_idea(ib, idea, snap, default_pct, token, channel, audit_path, pending,
                                             label="Added & suggested", audit_event="add_suggest_posted",
                                             candidates=[idea], raw_strategist=_one_raw, cot=_one_cot,
                                             market_context=_one_brief, technical_card=_one_price_stats,
                                             model_identity=_one_identity,
                                             model_identity_source=_one_identity_source)
            for (ts, r, tp_pct, sl_pct, idea, over_default, posted_at, decision_id,
                 revision, capture_candidates, capture_raw, capture_cot, capture_context,
                 capture_technical_card, autonomous_candidate) in pending:
                if ts in done:
                    continue
                if autonomous_candidate:
                    reactions, replies = [], []
                else:
                    rxn = approval._api("reactions.get", token,
                                        {"channel": channel, "timestamp": ts},
                                        http_post=False)
                    reactions = ((rxn.get("message", {}) or {}).get("reactions", [])
                                 if rxn.get("ok") else [])
                    rep = approval._api("conversations.replies", token,
                                        {"channel": channel, "ts": ts},
                                        http_post=False)
                    replies = ([m for m in rep.get("messages", []) if m.get("ts") != ts]
                               if rep.get("ok") else [])
                if approval.decision_from_reactions(reactions, approver_ids) == "reject" \
                        or approval.decision_from_replies(replies, approver_ids, ts) == "reject":
                    done.add(ts); continue

                ov_tp = ov_sl = None
                ovr = {}
                full_size = False
                qty_ovr = {}
                for m in replies:
                    if approver_ids and m.get("user") not in approver_ids:
                        continue
                    a, b = approval.parse_levels(m.get("text", ""))
                    if a: ov_tp = a
                    if b: ov_sl = b
                    ovr.update(approval.parse_structure_override(m.get("text", "")))
                    qty_ovr.update(approval.parse_qty_override(m.get("text", "")))
                    if approval.parse_size_override(m.get("text", "")):
                        full_size = True





                approved = (autonomous_candidate
                            or approval.decision_from_reactions(
                                reactions, approver_ids) == "approve"
                            or approval.decision_from_replies(
                                replies, approver_ids, ts) == "approve")
                if not approved:
                    continue
                _stage_b_pending = getattr(idea, "_stage_b_binding", None) is not None
                _exact_pending = exact_spread_for(idea)



                if _exact_pending is not None and any((ovr, full_size, qty_ovr)):
                    approval.post_proposal(
                        token, channel,
                        f":no_entry: *{r.underlying}* exact-spread structure/quantity was edited; "
                        "nothing placed. Generate a new exact request instead.")
                    audit(audit_path, "exact_spread_override_refused",
                          underlying=r.underlying, decision_id=decision_id,
                          structure_override=ovr, quantity_override=qty_ovr,
                          full_size=full_size)
                    done.add(ts)
                    continue
                if _stage_b_pending and (ovr or full_size or qty_ovr or ov_tp or ov_sl):
                    approval.post_proposal(
                        token, channel,
                        f":no_entry: *{r.underlying}* Stage-B terms were edited; nothing placed. "
                        "Generate a fresh intent/candidate decision instead.")
                    audit(audit_path, "stage_b_override_refused", underlying=r.underlying,
                          decision_id=decision_id, structure_override=ovr,
                          quantity_override=qty_ovr, full_size=full_size,
                          tp_pct=ov_tp, sl_pct=ov_sl)
                    done.add(ts)
                    continue
                _age = (entry_safety.SafetyResult(True) if autonomous_candidate
                        else entry_safety.approval_expired(posted_at))
                if not _age.allowed:
                    approval.post_proposal(token, channel,
                        f":hourglass: Approval expired — *{r.underlying}* was NOT placed. "
                        "Generate a fresh slate to approve a current quote.")
                    audit(audit_path, "approval_expired", underlying=r.underlying,
                          decision_id=decision_id, reasons=_age.reasons)
                    done.add(ts)
                    continue



                from dataclasses import replace as _replace



                if _stage_b_pending or _exact_pending is not None:
                    effective_idea, _ovr_error, _ovr_note = idea, "", ""
                else:
                    effective_idea, _ovr_error, _ovr_note = apply_structure_override(idea, ovr)
                nd, ns = effective_idea.direction, effective_idea.structure
                if _ovr_error:
                    approval.post_proposal(token, channel,
                        f":no_entry: *{r.underlying}* NOT placed — {_ovr_error}")
                    audit(audit_path, "structure_override_rejected", underlying=r.underlying,
                          decision_id=decision_id, override=dict(ovr),
                          structure=idea.structure, direction=idea.direction, reason=_ovr_error)
                    done.add(ts)
                    continue
                if _ovr_note:
                    approval.post_proposal(token, channel,
                        f":pencil2: *{r.underlying}* — {_ovr_note}")
                    audit(audit_path, "structure_relabelled", underlying=r.underlying,
                          decision_id=decision_id, override=dict(ovr),
                          was=idea.structure, now=ns, direction=nd, note=_ovr_note)

                _block_reasons = []
                try:
                    _markers = entry_safety.entry_markers_clear(
                        config_path=args.config,
                        kill_switch_path=(cfg.get("kill_switch") or {}).get("path"))
                    _block_reasons.extend(_markers.reasons)
                    snap = await get_pot_snapshot(ib)
                    _acct = entry_safety.account_snapshot_valid(snap)
                    _block_reasons.extend(_acct.reasons)
                except Exception as _account_error:
                    snap = None
                    _block_reasons.append(f"fresh account/stand-down check failed: {_account_error}")
                if _block_reasons:
                    approval.post_proposal(token, channel,
                        f":no_entry: *{r.underlying}* NOT placed — final safety gate: "
                        + "; ".join(_block_reasons))
                    audit(audit_path, "final_entry_gate_blocked", underlying=r.underlying,
                          decision_id=decision_id, reasons=_block_reasons)
                    done.add(ts)
                    continue

                _dep = deployable_funds(snap)
                if is_credit(effective_idea):
                    _deployed_now = await broker_deployed_csp_collateral(ib, audit_path)
                    if _deployed_now is None:
                        audit(audit_path, "deployed_collateral_unverifiable",
                              decision_id=decision_id)
                        done.add(ts)
                        continue
                    _dep = min(_dep, max(0.0, 0.80 * snap.net_liq - _deployed_now))
                    avail = _dep
                else:
                    _mp = construction.max_premium_budget(snap.net_liq, CONS)
                    if _mp > 0:
                        _dep = min(_dep, _mp)
                    avail = (_dep if (_stage_b_pending or full_size or over_default)
                             else min(_dep, default_pct * snap.net_liq))
                try:
                    fresh_r, why2 = await _resolve(
                        ib, effective_idea, avail, net_liq=snap.net_liq)
                except Exception as _resolve_error:
                    fresh_r, why2 = None, f"fresh contract/NBBO resolution failed: {_resolve_error}"
                if fresh_r is None:
                    approval.post_proposal(token, channel,
                        f":no_entry: *{r.underlying}* NOT placed — {why2}")
                    audit(audit_path, "fresh_resolve_blocked", underlying=r.underlying,
                          decision_id=decision_id, reason=why2)
                    done.add(ts)
                    continue
                fresh_r.decision_id = decision_id
                fresh_r.decision_revision = revision
                fresh_r.model_identity = getattr(r, "model_identity", None)
                fresh_r.model_identity_source = getattr(r, "model_identity_source", None)
                fresh_r.earnings_reconsidered = bool(
                    getattr(r, "earnings_reconsidered", False))
                fresh_r.earnings_reconsideration_decision = getattr(
                    r, "earnings_reconsideration_decision", None)
                fresh_r.earnings_reconsideration_phase = getattr(
                    r, "earnings_reconsideration_phase", None)
                fresh_r.earnings_reconsideration_reason = getattr(
                    r, "earnings_reconsideration_reason", "")
                fresh_r.earnings_reconsideration_model_identity = getattr(
                    r, "earnings_reconsideration_model_identity", None)
                fresh_r.earnings_reconsideration_model_identity_source = getattr(
                    r, "earnings_reconsideration_model_identity_source", None)
                fresh_r.earnings_reconsideration_order_snapshot = getattr(
                    r, "earnings_reconsideration_order_snapshot", None)
                fresh_r.earnings_reconsideration_order_sha256 = getattr(
                    r, "earnings_reconsideration_order_sha256", None)



                carry_intended_hold(fresh_r, r, effective_idea, idea)
                _exact_ok, _exact_why = exact_spread_order_gate(
                    fresh_r, _exact_pending, available=avail)
                if not _exact_ok:
                    approval.post_proposal(token, channel,
                        f":no_entry: *{r.underlying}* NOT placed — {_exact_why}")
                    audit(audit_path, "exact_spread_gate_blocked", phase="post_approval_requote",
                          decision_id=decision_id, underlying=r.underlying,
                          reason=_exact_why)
                    done.add(ts)
                    continue

                if qty_ovr:
                    newq = (qty_ovr["contracts"] if "contracts" in qty_ovr
                            else max(1, round(fresh_r.qty * qty_ovr["fraction"])))
                    _unit_cost = entry_safety.executable_price(fresh_r) * 100
                    if _unit_cost * newq > snap.available_funds + 1e-6:



                        affordable = int(snap.available_funds // _unit_cost)
                        if affordable < 1:
                            approval.post_proposal(token, channel,
                                f":no_entry: even 1 contract (~${_unit_cost:,.0f}) exceeds available "
                                f"funds ${snap.available_funds:,.0f} — not placing.")
                            audit(audit_path, "qty_override_refused_funds", underlying=fresh_r.underlying,
                                  order=order_summary(fresh_r), available=snap.available_funds,
                                  decision_id=decision_id)
                            done.add(ts)
                            continue
                        approval.post_proposal(token, channel,
                            f":warning: {newq}x ~${_unit_cost*newq:,.0f} > available ${snap.available_funds:,.0f} — revised to {affordable}x.")
                        newq = affordable
                    _mp2 = construction.max_premium_budget(snap.net_liq, CONS)
                    if _mp2 > 0 and _unit_cost * newq > _mp2 + 1e-6:

                        capq = int(_mp2 // _unit_cost)
                        if capq < 1:
                            approval.post_proposal(token, channel,
                                f":no_entry: even 1 contract (~${_unit_cost:,.0f}) exceeds the "
                                f"{CONS.max_premium_pct:.0%}-of-net-liq premium cap (${_mp2:,.0f}) — not placing.")
                            audit(audit_path, "qty_override_refused_premium_cap", underlying=fresh_r.underlying,
                                  order=order_summary(fresh_r), premium_cap=_mp2,
                                  decision_id=decision_id)
                            done.add(ts)
                            continue
                        approval.post_proposal(token, channel,
                            f":warning: {newq}x ~${_unit_cost*newq:,.0f} exceeds the {CONS.max_premium_pct:.0%}-of-net-liq "
                            f"premium cap (${_mp2:,.0f}) — revised to {capq}x.")
                        newq = capq
                    if newq != fresh_r.qty:
                        fresh_r = _replace(fresh_r, qty=newq)
                        fresh_r.decision_id = decision_id
                        fresh_r.decision_revision = revision
                        fresh_r.model_identity = getattr(r, "model_identity", None)
                        fresh_r.model_identity_source = getattr(
                            r, "model_identity_source", None)








                        carry_intended_hold(fresh_r, fresh_r, r, effective_idea, idea)













                if ov_tp:
                    if ov_tp > 0 and ov_tp <= 1000.0:
                        eff_tp = float(ov_tp)
                        _band_note = construction.optional_take_profit_pct(ov_tp)[1]
                        _outside = f" (outside the model's {_band_note})" if _band_note else ""
                        approval.post_proposal(token, channel,
                            f":pushpin: take-profit set to `{ov_tp:g}%` by human override{_outside}"
                            f" — this trade will NOT run free.")
                        audit(audit_path, "take_profit_override_accepted_human",
                              underlying=fresh_r.underlying, profit_target_pct=eff_tp,
                              outside_model_band=bool(_band_note), decision_id=decision_id)
                    else:
                        approval.post_proposal(token, channel,
                            f":no_entry_sign: take-profit override `{ov_tp:g}` refused — "
                            f"outside sanity bounds (0 < tp <= 1000).")
                        audit(audit_path, "take_profit_override_refused",
                              underlying=fresh_r.underlying, requested_profit_target_pct=ov_tp,
                              reason="outside sanity bounds", decision_id=decision_id)
                        eff_tp = tp_pct
                else:
                    eff_tp = tp_pct


                eff_sl = max(10.0, min(30.0, ov_sl)) if ov_sl else sl_pct




















                try:
                    from exitmgr import atr_cache as _ac_d, atr_levels as _alv_d
                    _acfg_d = getattr(_RESOLVED_CONFIG.rules, "atr_levels", None)
                    if (_acfg_d is not None and getattr(_acfg_d, "enabled", False)
                            and not is_credit(fresh_r) and eff_sl and float(eff_sl) > 0):
                        _aref_d = _ac_d.read(fresh_r.underlying)
                        _eps_d = float(getattr(fresh_r, "limit", 0.0) or 0.0)
                        _q_d = abs(int(getattr(fresh_r, "qty", 0) or 0))
                        _nd_d = None
                        if _aref_d:
                            _ss_d = getattr(fresh_r, "short_strike", None)
                            _nd_d = _alv_d.net_structure_delta(
                                float(_aref_d["spot"]), float(fresh_r.strike),
                                (float(_ss_d) if _ss_d else None),
                                int(getattr(fresh_r, "dte", 0) or 0),
                                float(getattr(fresh_r, "entry_iv", 0.0) or 0.0),
                                right=getattr(fresh_r, "right", "C"))
                        if _aref_d and _nd_d and _q_d > 0 and _eps_d > 0:
                            _hold_d = float(getattr(fresh_r, "intended_hold_days", 0) or 0)
                            if _hold_d <= 0:
                                _hold_d = float(max(1, -(-int(getattr(fresh_r, "dte", 1) or 1) // 8)))
                            _keff_d = float(_acfg_d.k_stop)
                            if getattr(_acfg_d, "horizon_scaling", False):
                                _hk_d = _alv_d.horizon_k(
                                    float(getattr(_acfg_d, "k_stop_horizon", 0.5)), _hold_d)
                                if _hk_d is not None:
                                    _keff_d = _hk_d
                            _as_d = _alv_d.atr_stop_pct(
                                _eps_d, _aref_d["atr"], _nd_d, _keff_d,
                                float(eff_sl), float(_acfg_d.min_stop_pct))
                            if _as_d is not None and float(_as_d) < float(eff_sl):
                                print("[ATR] %s directed stop %.1f%% -> %.1f%% "
                                      "(%.2f ATR over %.0fd hold, net_delta %.3f)"
                                      % (fresh_r.underlying, float(eff_sl), float(_as_d),
                                         _keff_d, _hold_d, _nd_d))
                                eff_sl = round(float(_as_d), 1)
                        elif not _aref_d:
                            print("[ATR] %s: no ATR cache entry; directed stop stays %.1f%%"
                                  % (fresh_r.underlying, float(eff_sl)))
                except Exception as _ae_d:
                    print("[ATR] directed stop sizing skipped for %s (%s)"
                          % (getattr(fresh_r, "underlying", "?"), _ae_d))
                bind_protective_terms(fresh_r, eff_tp, eff_sl)


                try:
                    _positions = await CONN.get_positions()
                    _risk_positions = await _open_positions_for_risk(_positions)
                    _baseline_path = Path(args.config).resolve().parent / tr.get("baseline_path", "./day_baseline.json")
                    _baseline = entry_safety.day_start_value(_baseline_path, _trading_day())
                    if isinstance(_baseline, entry_safety.SafetyResult):
                        _block_reasons.extend(_baseline.reasons)
                    _nbbo = entry_safety.nbbo_valid(fresh_r)
                    _block_reasons.extend(_nbbo.reasons)






                    if carry_intended_hold(fresh_r, fresh_r, r, effective_idea, idea) is None:
                        _block_reasons.append(
                            "intended_hold_days is not stated for this entry, and it is the "
                            "denominator of the DTE doctrine, the ATR stop horizon and the "
                            "re-arm bar's criterion 5. Pass --hold-days N, or set "
                            "trading.intended_hold_days_fallback in config.yaml.")
                    _credit_fresh = is_credit(fresh_r)
                    _cost = capital_committed(fresh_r)
                    _gate = risk.evaluate_trade(
                        risk.ProposedTrade(
                            underlying=fresh_r.underlying, notional=_cost,
                            is_index=bool(effective_idea.is_index),
                            conviction=int(getattr(effective_idea, "conviction", 1)),
                            is_long=(False if _credit_fresh else fresh_r.right == "C"),
                            profit_target_pct=(0.0 if _credit_fresh else fresh_r.tp_pct),
                            stop_pct=(0.0 if _credit_fresh else fresh_r.sl_pct)),
                        net_liq=snap.net_liq, available_funds=snap.available_funds,
                        open_positions=_risk_positions,
                        pot_day_start=(_baseline if not isinstance(_baseline, entry_safety.SafetyResult)
                                       else 0.0),
                        approved_names={str(n).upper() for n in tr.get("approved_names", [])},
                        limits=(_credit_limits(_RISK_LIMITS) if _credit_fresh else _RISK_LIMITS))
                    if not _gate.approved:
                        _block_reasons.extend(_gate.reasons)
                    if _credit_fresh:
                        _deployed_final = await broker_deployed_csp_collateral(ib, audit_path)
                        _collateral_final = collateral_capacity(
                            required=required_collateral(fresh_r.strike, fresh_r.qty),
                            deployed=_deployed_final, net_liq=snap.net_liq,
                            available_funds=snap.available_funds)
                        _block_reasons.extend(_collateral_final.reasons)


                        fresh_r.earnings_date = None
                        fresh_r.earnings_warn = ""
                        fresh_r.earnings_day_warning = False
                        try:
                            _edays_credit = await asyncio.to_thread(
                                research.days_to_earnings, fresh_r.underlying,
                                force_refresh=True)
                        except Exception as _credit_earn_exc:
                            print(f"[WARN] final earnings lookup failed for "
                                  f"{fresh_r.underlying}: {_credit_earn_exc}")
                            _edays_credit = None
                        fresh_r.earnings_unchecked = (
                            _edays_credit is None and
                            not entry_safety.is_no_earnings_etf(fresh_r.underlying))
                        if _edays_credit is not None:
                            _entry_credit = datetime.now(timezone.utc).date()
                            _earn_credit = _entry_credit + timedelta(days=_edays_credit)
                            fresh_r.earnings_date = _earn_credit.isoformat()
                            if _edays_credit == 0:
                                fresh_r.earnings_day_warning = True
                                _, _credit_earn_why = construction.earnings_ok(
                                    _entry_credit, fresh_r.expiry, _earn_credit, CONS,
                                    hold_days=intended_hold_for_gate(
                                        fresh_r, effective_idea, idea))
                                fresh_r.earnings_warn = _credit_earn_why
                                audit(audit_path, "earnings_same_day_warning",
                                      phase="post_approval", decision_id=decision_id,
                                      underlying=fresh_r.underlying,
                                      order=order_summary(fresh_r),
                                      earnings_date=fresh_r.earnings_date,
                                      reason=_credit_earn_why)
                    else:




                        okf, whyf = construction.check_budget(
                            _cost, fresh_r.dte, snap.net_liq,
                            await _open_book(ib, audit_path, _positions), CONS)
                        if not okf:
                            _block_reasons.extend(whyf)
                        _edays_final = await asyncio.to_thread(
                            research.days_to_earnings, fresh_r.underlying,
                            force_refresh=True)








                        if _edays_final is None:




                            if not entry_safety.is_no_earnings_etf(fresh_r.underlying):
                                _block_reasons.append("earnings date unavailable at approval time")
                        else:
                            _entry_final = datetime.now(timezone.utc).date()
                            _earn_final = _entry_final + timedelta(days=_edays_final)


                            _earn_ok, _earn_waived_final, _earn_why = exact_earnings_gate(
                                _entry_final, fresh_r.expiry, _earn_final,
                                intended_hold_for_gate(fresh_r, effective_idea, idea),
                                _exact_pending)
                            fresh_r.earnings_date = _earn_final.isoformat()
                            fresh_r.earnings_unchecked = False
                            fresh_r.earnings_day_warning = (_earn_final == _entry_final)
                            fresh_r.earnings_warn = _earn_why if (_earn_ok and _earn_why) else ""
                            if not _earn_ok:
                                _block_reasons.append(_earn_why)
                            elif _earn_waived_final:
                                audit(audit_path, "exact_earnings_waiver",
                                      phase="post_approval", decision_id=decision_id,
                                      underlying=fresh_r.underlying,
                                      order=order_summary(fresh_r),
                                      expected_earnings_date=(
                                          _exact_pending.expected_earnings_date.isoformat()),
                                      observed_earnings_date=_earn_final.isoformat(),
                                      normal_gate_reason=_earn_why,
                                      max_debit=_exact_pending.max_debit,
                                      exact_qty=_exact_pending.qty)
                            elif _earn_why:
                                audit(audit_path, ("earnings_same_day_warning"
                                      if fresh_r.earnings_day_warning
                                      else "earnings_overlap_disclosed"),
                                      phase="post_approval", decision_id=decision_id,
                                      underlying=fresh_r.underlying,
                                      order=order_summary(fresh_r),
                                      earnings_date=_earn_final.isoformat(), reason=_earn_why)
                except Exception as _be:
                    _block_reasons.append(f"final risk/NBBO/earnings gate failed: {_be}")
                if _block_reasons:
                    approval.post_proposal(token, channel,
                        f":no_entry: *{fresh_r.underlying}* `{order_summary(fresh_r)}` NOT placed — "
                        "final hard gate: " + "; ".join(_block_reasons))
                    audit(audit_path, "final_entry_gate_blocked", underlying=fresh_r.underlying,
                          order=order_summary(fresh_r), decision_id=decision_id,
                          reasons=_block_reasons)
                    done.add(ts)
                    continue

                _earnings_review_ok, _earnings_reviewed = (
                    await reconsider_new_same_day_earnings(
                        endpoint=tr.get("llm_endpoint"), model=tr.get("llm_model"),
                        market_context=capture_context, idea=effective_idea,
                        previous=r, fresh=fresh_r, slack_token=token,
                        slack_channel=channel, audit_path=audit_path,
                        decision_id=decision_id))
                if not _earnings_review_ok:
                    done.add(ts)
                    continue




                try:
                    _latest_r, _latest_why = await _resolve(
                        ib, effective_idea, avail, net_liq=snap.net_liq)
                    if _latest_r is None:
                        raise RuntimeError(_latest_why or "final NBBO refresh returned no order")
                    if qty_ovr:
                        _latest_r = _replace(_latest_r, qty=fresh_r.qty)
                    _latest_r.decision_id = decision_id
                    _latest_r.decision_revision = revision
                    _latest_r.model_identity = getattr(r, "model_identity", None)
                    _latest_r.model_identity_source = getattr(
                        r, "model_identity_source", None)
                    carry_intended_hold(_latest_r, fresh_r, r, effective_idea, idea)
                    bind_protective_terms(_latest_r, eff_tp, eff_sl)



                    _latest_r.earnings_date = getattr(fresh_r, "earnings_date", None)
                    _latest_r.earnings_warn = getattr(fresh_r, "earnings_warn", "")
                    _latest_r.earnings_unchecked = bool(
                        getattr(fresh_r, "earnings_unchecked", False))
                    _latest_r.earnings_day_warning = bool(
                        getattr(fresh_r, "earnings_day_warning", False))
                    _latest_r.earnings_reconsidered = bool(
                        getattr(fresh_r, "earnings_reconsidered", False))
                    _latest_r.earnings_reconsideration_decision = getattr(
                        fresh_r, "earnings_reconsideration_decision", None)
                    _latest_r.earnings_reconsideration_phase = getattr(
                        fresh_r, "earnings_reconsideration_phase", None)
                    _latest_r.earnings_reconsideration_reason = getattr(
                        fresh_r, "earnings_reconsideration_reason", "")
                    _latest_r.earnings_reconsideration_model_identity = getattr(
                        fresh_r, "earnings_reconsideration_model_identity", None)
                    _latest_r.earnings_reconsideration_model_identity_source = getattr(
                        fresh_r, "earnings_reconsideration_model_identity_source", None)
                    _latest_r.earnings_reconsideration_order_snapshot = getattr(
                        fresh_r, "earnings_reconsideration_order_snapshot", None)
                    _latest_r.earnings_reconsideration_order_sha256 = getattr(
                        fresh_r, "earnings_reconsideration_order_sha256", None)
                    _latest_exact_ok, _latest_exact_why = exact_spread_order_gate(
                        _latest_r, _exact_pending, available=avail)
                    if not _latest_exact_ok:
                        raise RuntimeError(_latest_exact_why)
                    _latest_nbbo = entry_safety.nbbo_valid(_latest_r)
                    if not _latest_nbbo.allowed:
                        raise RuntimeError("; ".join(_latest_nbbo.reasons))
                    _latest_credit = is_credit(_latest_r)
                    _latest_cost = capital_committed(_latest_r)
                    _latest_gate = risk.evaluate_trade(
                        risk.ProposedTrade(
                            underlying=_latest_r.underlying, notional=_latest_cost,
                            is_index=bool(effective_idea.is_index),
                            conviction=int(getattr(effective_idea, "conviction", 1)),
                            is_long=(False if _latest_credit else _latest_r.right == "C"),
                            profit_target_pct=(0.0 if _latest_credit else _latest_r.tp_pct),
                            stop_pct=(0.0 if _latest_credit else _latest_r.sl_pct)),
                        net_liq=snap.net_liq, available_funds=snap.available_funds,
                        open_positions=_risk_positions,
                        pot_day_start=_baseline,
                        approved_names={str(n).upper() for n in tr.get("approved_names", [])},
                        limits=(_credit_limits(_RISK_LIMITS) if _latest_credit else _RISK_LIMITS))
                    if not _latest_gate.approved:
                        raise RuntimeError("; ".join(_latest_gate.reasons))
                    if _latest_credit:
                        _latest_deployed = await broker_deployed_csp_collateral(ib, audit_path)
                        _latest_capacity = collateral_capacity(
                            required=required_collateral(_latest_r.strike, _latest_r.qty),
                            deployed=_latest_deployed, net_liq=snap.net_liq,
                            available_funds=snap.available_funds)
                        if not _latest_capacity.allowed:
                            raise RuntimeError("; ".join(_latest_capacity.reasons))
                    else:



                        _latest_budget, _latest_budget_reasons = construction.check_budget(
                            _latest_cost, _latest_r.dte, snap.net_liq,
                            await _open_book(ib, audit_path, _positions), CONS)
                        if not _latest_budget:
                            raise RuntimeError("; ".join(_latest_budget_reasons))
                    fresh_r = _latest_r
                except Exception as _latest_error:
                    approval.post_proposal(token, channel,
                        f":no_entry: *{fresh_r.underlying}* NOT placed — final NBBO refresh/gate: {_latest_error}")
                    audit(audit_path, "final_nbbo_gate_blocked", decision_id=decision_id,
                          underlying=fresh_r.underlying, error=str(_latest_error))
                    done.add(ts)
                    continue

                if autonomous_candidate:
                    _final_auto = autonomous_execution_gate(
                        enabled=getattr(
                            _RESOLVED_CONFIG, "auto_approve_within_gates", False),
                        blockers=daily_autonomy_blockers(
                            fresh_r.underlying,
                            market_context=capture_context,
                            technical_card=capture_technical_card,
                        ),
                        gate=_latest_gate,
                        capital_at_risk_usd=capital_at_risk(fresh_r),
                    )
                    if not _final_auto.allowed:
                        _downgrade_msg = (
                            f":lock: *Auto-execution withheld — {fresh_r.underlying}*\n"
                            f"Exact order: `{order_summary(fresh_r)}`\n"
                            + "\n".join("• " + reason for reason in _final_auto.reasons)
                            + f"\n:point_down: Tap :white_check_mark: within "
                            f"{_APPROVAL_TTL_MINUTES} minutes to approve these exact terms.\n"
                            f"_Decision ID: `{decision_id}`, revision {revision + 1}_"
                        )
                        _human_ts = approval.post_proposal(token, channel, _downgrade_msg)
                        done.add(ts)
                        if _human_ts:
                            fresh_r.decision_revision = revision + 1
                            pending.append((
                                _human_ts, fresh_r, eff_tp, eff_sl, effective_idea,
                                False, time.monotonic(), decision_id, revision + 1,
                                capture_candidates, capture_raw, capture_cot,
                                capture_context, capture_technical_card, False,
                            ))
                        audit(audit_path, "auto_approve_withheld_final",
                              decision_id=decision_id,
                              underlying=fresh_r.underlying,
                              reasons=_final_auto.reasons)
                        continue

                _changes = list(
                    credit_material_changes(r, fresh_r) if is_credit(r)
                    else entry_safety.material_changes(r, fresh_r))
                if revision == 0 and (ovr or full_size or qty_ovr or ov_tp or ov_sl):
                    _changes.append("human override changed approved terms")
                if _changes:
                    if revision >= 2:
                        approval.post_proposal(token, channel,
                            f":no_entry: *{fresh_r.underlying}* kept moving after two refreshes — "
                            "nothing placed; generate a new slate.")
                        audit(audit_path, "reapproval_churn_blocked", decision_id=decision_id,
                              underlying=fresh_r.underlying, changes=_changes)
                        done.add(ts)
                        continue
                    _exec_label = ("Executable SELL credit" if is_credit(fresh_r)
                                   else "Executable BUY limit")
                    _exec_price = (credit_executable_price(fresh_r) if is_credit(fresh_r)
                                   else entry_safety.executable_price(fresh_r))
                    _earn_rewarning = (
                        ":rotating_light: *EARNINGS TODAY — ENTRY WARNING*\n"
                        f"{fresh_r.earnings_warn}\n\n"
                        if getattr(fresh_r, "earnings_day_warning", False) else "")
                    if autonomous_candidate:
                        _remsg = (_earn_rewarning
                                  + f":robot_face: *Autonomous terms refreshed — "
                                  f"{fresh_r.underlying}*\n"
                                  + approval.model_attribution_line(
                                      getattr(fresh_r, "model_identity", None),
                                      getattr(fresh_r, "model_identity_source", None))
                                  + f"Refreshed order: `{order_summary(fresh_r)}`\n"
                                  f"{_exec_label}: *${_exec_price:.2f}*\n"
                                  f"Changed: {'; '.join(_changes)}\n"
                                  "No tap is required. The refreshed exact order will repeat "
                                  "the model/event review and every final hard gate before submit.\n"
                                  f"_Decision ID: `{decision_id}`, revision {revision + 1}_")
                    else:
                        _remsg = (_earn_rewarning
                                  + f":repeat: *Reapproval required — {fresh_r.underlying}*\n"
                                  + approval.model_attribution_line(
                                      getattr(fresh_r, "model_identity", None),
                                      getattr(fresh_r, "model_identity_source", None))
                                  + f"Refreshed order: `{order_summary(fresh_r)}`\n"
                                  f"{_exec_label}: *${_exec_price:.2f}*\n"
                                  f"Changed: {'; '.join(_changes)}\n"
                                  f":point_down: Tap :white_check_mark: again within "
                                  f"{_APPROVAL_TTL_MINUTES} minutes to approve these exact terms.\n"
                                  f"_Decision ID: `{decision_id}`, revision {revision + 1}_")
                    _new_ts = approval.post_proposal(
                        token, channel, _remsg,
                        seed_reactions=not autonomous_candidate)
                    done.add(ts)
                    if _new_ts:
                        fresh_r.decision_revision = revision + 1
                        pending.append((_new_ts, fresh_r, eff_tp, eff_sl, effective_idea,
                                        False, time.monotonic(), decision_id, revision + 1,
                                        capture_candidates, capture_raw, capture_cot,
                                        capture_context, capture_technical_card,
                                        autonomous_candidate))
                    audit(audit_path, ("auto_terms_refresh_required" if autonomous_candidate
                                      else "reapproval_required"), decision_id=decision_id,
                          underlying=fresh_r.underlying, changes=_changes,
                          revision=revision + 1)
                    continue

                if (getattr(fresh_r, "earnings_reconsidered", False)
                        and not earnings_reconsideration_receipt_valid(fresh_r)):
                    approval.post_proposal(
                        token, channel,
                        f":no_entry: *{fresh_r.underlying}* was NOT placed — its same-day "
                        "earnings review no longer matches the executable order. Generate a "
                        "fresh decision.", seed_reactions=False)
                    audit(audit_path, "earnings_reconsideration_receipt_invalid",
                          decision_id=decision_id, underlying=fresh_r.underlying,
                          order=order_summary(fresh_r))
                    done.add(ts)
                    continue




                _final_age = (entry_safety.SafetyResult(True) if autonomous_candidate
                              else entry_safety.approval_expired(posted_at))
                if not _final_age.allowed:
                    approval.post_proposal(
                        token, channel,
                        f":hourglass: Approval expired during final review — "
                        f"*{fresh_r.underlying}* was NOT placed. Generate a fresh slate.",
                        seed_reactions=False)
                    audit(audit_path, "approval_expired_pre_submit",
                          underlying=fresh_r.underlying,
                          decision_id=decision_id, reasons=_final_age.reasons)
                    done.add(ts)
                    continue

                if (getattr(fresh_r, "earnings_day_warning", False)
                        and not getattr(r, "earnings_day_warning", False)):


                    _warning_authority = (
                        "within-risk-gates autonomous authority. No approval is being awaited.\n"
                        if autonomous_candidate else
                        "still-live exact human approval.\n"
                    )
                    _notice_ts = (True if getattr(fresh_r, "earnings_reconsidered", False)
                                  else approval.post_proposal(
                        token, channel,
                        ":rotating_light: *EARNINGS TODAY — FINAL ENTRY WARNING*\n"
                        f"{fresh_r.earnings_warn}\n"
                        "Final pre-submit refresh found the event; proceeding under the "
                        + _warning_authority
                        + f"_Decision ID: `{decision_id}`_",
                        seed_reactions=False))
                    audit(audit_path, "earnings_same_day_warning_notice",
                          phase="pre_submit", underlying=fresh_r.underlying,
                          decision_id=decision_id,
                          earnings_date=fresh_r.earnings_date,
                          notice_posted=bool(_notice_ts))

                _submission_authority = (
                    entry_safety.EntrySubmissionAuthority.autonomous()
                    if autonomous_candidate else
                    entry_safety.EntrySubmissionAuthority.human(posted_at)
                )
                fresh_r.execution_authority = _submission_authority.mode
                if autonomous_candidate:
                    approval.post_proposal(
                        token, channel,
                        ":robot_face: *AUTO-APPROVED (within risk gates)* — submitting now. "
                        "_This is a receipt; no approval is being awaited._\n"
                        f"`{order_summary(fresh_r)}`\n"
                        f"_Decision ID: `{decision_id}`, revision {revision}_",
                        seed_reactions=False)
                    audit(audit_path, "auto_approved", source="daily_slate",
                          underlying=fresh_r.underlying, decision_id=decision_id,
                          order=order_summary(fresh_r),
                          capital_at_risk=capital_at_risk(fresh_r))

                r = fresh_r
                _authority_now = _short_option_entry_authority(r)
                if not _authority_now.allowed:
                    approval.post_proposal(
                        token, channel,
                        f":no_entry: *{r.underlying}* NOT placed — short-option authority: "
                        f"{'; '.join(_authority_now.reasons)}")
                    audit(audit_path, "assignment_authority_blocked_submit",
                          decision_id=decision_id, underlying=r.underlying,
                          reasons=_authority_now.reasons)
                    done.add(ts)
                    continue


                _markers_now = entry_safety.entry_markers_clear(
                    config_path=args.config,
                    kill_switch_path=(cfg.get("kill_switch") or {}).get("path"))
                if not _markers_now.allowed:
                    audit(audit_path, "marker_blocked_submit", decision_id=decision_id,
                          reasons=_markers_now.reasons)
                    done.add(ts)
                    continue
                _quote_now = entry_safety.nbbo_valid(r)
                if not _quote_now.allowed:
                    audit(audit_path, "stale_nbbo_blocked_submit", decision_id=decision_id,
                          reasons=_quote_now.reasons)
                    done.add(ts)
                    continue



                _ok_submit, _why_submit = submit_structure_ok(r, effective_idea)
                if not _ok_submit:
                    approval.post_proposal(token, channel,
                        f":no_entry: *{r.underlying}* NOT placed — structure gate at submit: "
                        f"{_why_submit}")
                    audit(audit_path, "structure_blocked_submit", decision_id=decision_id,
                          underlying=r.underlying, structure=r.structure, right=r.right,
                          reason=_why_submit)
                    done.add(ts)
                    continue
                if is_credit(r):
                    if (str(r.right).upper()[:1] != "P" or r.short_contract is not None
                            or str(r.structure).strip().lower() != "cash secured put"):
                        audit(audit_path, "credit_structure_blocked_submit",
                              decision_id=decision_id, underlying=r.underlying)
                        done.add(ts)
                        continue
                    _lmt = credit_executable_price(r)
                else:
                    _lmt = entry_safety.executable_price(r)
                try:
                    trade_capture.capture_decision(
                        trade_capture.dataset_dir(JOURNAL_PATH), source="daily_slate",
                        symbol=r.underlying, right=r.right, strike=r.strike, expiry=r.expiry,
                        structure=("cash secured put" if is_credit(r) else
                                   ("spread" if r.short_contract is not None else "single")),
                        con_id=getattr(r.contract, "conId", None), chosen_idea=effective_idea,
                        candidates=(capture_candidates or [effective_idea]),
                        raw_strategist=capture_raw, cot=capture_cot,
                        market_context=capture_context,
                        technical_card=capture_technical_card,
                        decision_id=decision_id, revision=revision, event="approved",
                        model_identity=getattr(r, "model_identity", None),
                        model_identity_source=getattr(r, "model_identity_source", None),
                        final_contract=contract_snapshot(r),
                        order_ref=entry_safety.decision_order_ref(decision_id),
                        execution_authority=_submission_authority.mode,
                        human_action=(None if autonomous_candidate else
                                      {"action": "approve", "structure_override": ovr,
                                       "quantity_override": qty_ovr,
                                       "full_size": full_size,
                                       "tp_pct": r.tp_pct, "sl_pct": r.sl_pct}))
                except Exception as _capture_error:
                    print(f"[WARN] final decision capture failed (continuing): {_capture_error}")
                order = Order(action=("SELL" if is_credit(r) else "BUY"),
                              orderType="LMT", lmtPrice=_lmt,
                              totalQuantity=r.qty, tif="DAY")
                order.orderRef = entry_safety.decision_order_ref(decision_id)
                _order_contract = (conn.create_combo_contract(
                    r.underlying, [(r.contract.conId, "BUY"),
                                   (r.short_contract.conId, "SELL")])
                    if r.short_contract is not None and not is_credit(r) else r.contract)









                _obs = await observe_for_admission(
                    ib, _admission_positions_fn(conn), audit_path)
                if _obs is None:
                    audit(audit_path, "admission_observation_blocked_submit",
                          decision_id=decision_id, underlying=r.underlying)
                    approval.post_proposal(token, channel,
                        f":no_entry: *{r.underlying}* NOT placed — the broker book backing "
                        f"admission could not be verified (an unreadable book is never treated "
                        f"as an empty one).")
                    done.add(ts)
                    continue






                try:
                    _day_orders, _day_notional = entry_day_open_counts(
                        None, _THROTTLE_STORE, _trading_day())
                except EntryThrottleUnreadable as _tue:
                    audit(audit_path, "entry_throttle_unreadable_blocked_submit",
                          decision_id=decision_id, underlying=r.underlying, error=str(_tue))
                    approval.post_proposal(token, channel,
                        f":no_entry: *{r.underlying}* NOT placed — the daily entry-throttle "
                        f"counters could not be read, so caps.max_orders_per_day / "
                        f"caps.max_notional_per_day cannot be evaluated ({_tue}).")
                    done.add(ts)
                    continue
                try:
                    _campaign_conflicts = active_campaign_conflict_symbols(JOURNAL_PATH)
                except CampaignConflictRegistryError as _cce:
                    audit(audit_path, "campaign_conflict_registry_blocked_submit",
                          decision_id=decision_id, underlying=r.underlying, error=str(_cce))
                    approval.post_proposal(
                        token, channel,
                        f":no_entry: *{r.underlying}* NOT placed — campaign-conflict "
                        f"quarantine could not be verified ({_cce}).")
                    done.add(ts)
                    continue
                _dims = admission_dimensions(
                    r, _obs, order.orderRef, limits=_RISK_LIMITS, construction_cfg=CONS,
                    markers_clear=entry_safety.entry_markers_clear(
                        config_path=args.config,
                        kill_switch_path=(cfg.get("kill_switch") or {}).get("path")).allowed,
                    day_orders=_day_orders, day_notional=_day_notional,
                    max_orders_per_day=_MAX_ORDERS_PER_DAY,
                    max_notional_per_day=_MAX_NOTIONAL_PER_DAY,
                    runtime_identity=_RUNTIME_IDENTITY,
                    campaign_conflict_symbols=_campaign_conflicts)

                def _admission_recheck(_r=r):
                    """Public API contract; production-derived narrative omitted."""
                    _submission = entry_safety.submission_authority_valid(
                        _submission_authority)
                    if not _submission.allowed:
                        return False, tuple(_submission.reasons)
                    _a = _short_option_entry_authority(_r)
                    if not _a.allowed:
                        return False, tuple(_a.reasons)
                    _m = entry_safety.entry_markers_clear(
                        config_path=args.config,
                        kill_switch_path=(cfg.get("kill_switch") or {}).get("path"))
                    if not _m.allowed:
                        return False, tuple(_m.reasons)
                    _q = entry_safety.nbbo_valid(_r)
                    if not _q.allowed:
                        return False, tuple(_q.reasons)
                    _xok, _xwhy = exact_spread_order_gate(_r, _exact_pending)
                    if not _xok:
                        return False, (_xwhy,)
                    if not is_credit(_r):
                        if (getattr(_r, "tp_pct", None) != eff_tp
                                or getattr(_r, "sl_pct", None) != eff_sl):
                            return False, (
                                "effective protective terms changed before broker submit",)
                    if (getattr(_r, "earnings_day_warning", False)
                            and not earnings_reconsideration_receipt_valid(_r)):
                        return False, (
                            "same-day earnings review receipt is absent or stale",)
                    try:
                        _campaign_conflicts_now = active_campaign_conflict_symbols(JOURNAL_PATH)
                    except CampaignConflictRegistryError as _cce:
                        return False, (
                            "campaign-conflict quarantine became unreadable: %s" % _cce,)
                    if _campaign_conflicts_now != _campaign_conflicts:
                        return False, (
                            "campaign-conflict quarantine changed before broker submit",)
                    if str(_r.underlying).upper() in _campaign_conflicts_now:
                        return False, (
                            "%s has an active journal campaign-authority conflict"
                            % str(_r.underlying).upper(),)
                    return True, ()

                def _record_day(_r=r):
                    return record_entry_open(None, _THROTTLE_STORE, _trading_day(), 1,
                                             float(_dims["capital_usd"]))







                _entry_intent = {
                    "order_ref": order.orderRef,
                    "con_id": getattr(r.contract, "conId", None),
                    "symbol": r.underlying,
                    "side": "credit" if is_credit(r) else "debit",
                    "structure": str(getattr(r, "structure", "") or ""),
                    "source": "daily_slate",
                    "decision_id": decision_id,
                    "execution_authority": _submission_authority.mode,
                    **identity_fields(_RUNTIME_IDENTITY),
                    "requested_qty": abs(int(r.qty)),
                    "estimated_debit": float(_dims["final_contract"]["max_loss_usd"]),
                    "transmitted": "unknown",
                    "journal_template": {
                        "decision_id": decision_id,
                        "decision_revision": revision,
                        "model_identity": getattr(r, "model_identity", None),
                        "model_identity_source": getattr(r, "model_identity_source", None),
                        "contract_id": getattr(r.contract, "conId", None),
                        "symbol": r.underlying, "right": r.right, "expiry": r.expiry,
                        "strike": r.strike,
                        "profit_target_pct": (None if is_credit(r) else r.tp_pct),
                        "tp_policy": construction.TP_POLICY_CURRENT,
                        "stop_pct": (None if is_credit(r) else r.sl_pct),
                        "conviction": getattr(idea, "conviction", -1),
                        "intended_hold_days": positive_hold_days(
                            getattr(r, "intended_hold_days", None)),
                        "intended_hold_days_source": getattr(
                            idea, "_intended_hold_days_source", "model"),

                        **doctrine_stamp(getattr(r, "intended_hold_days", None), is_credit(r)),


                        "model_intended_hold_days": positive_hold_days(
                            getattr(idea, "_model_intended_hold_days", None)),
                        "earnings_date": getattr(r, "earnings_date", None),
                        "earnings_unchecked": bool(
                            getattr(r, "earnings_unchecked", False)),
                        "earnings_overlap_warning": (
                            getattr(r, "earnings_warn", "") or None),
                        "earnings_day_warning": bool(
                            getattr(r, "earnings_day_warning", False)),
                        "earnings_reconsidered": bool(
                            getattr(r, "earnings_reconsidered", False)),
                        "earnings_reconsideration_decision": getattr(
                            r, "earnings_reconsideration_decision", None),
                        "earnings_reconsideration_phase": getattr(
                            r, "earnings_reconsideration_phase", None),
                        "earnings_reconsideration_reason": (
                            getattr(r, "earnings_reconsideration_reason", "") or None),
                        "earnings_reconsideration_model_identity": getattr(
                            r, "earnings_reconsideration_model_identity", None),
                        "earnings_reconsideration_model_identity_source": getattr(
                            r, "earnings_reconsideration_model_identity_source", None),
                        "earnings_reconsideration_order_snapshot": getattr(
                            r, "earnings_reconsideration_order_snapshot", None),
                        "earnings_reconsideration_order_sha256": getattr(
                            r, "earnings_reconsideration_order_sha256", None),
                        "order_ref": order.orderRef,
                        **({"side": "credit", "structure": CSP_STRUCTURE, "action": "SELL",
                            "quantity": -abs(int(r.qty)), "contracts": abs(int(r.qty)),
                            "collateral_usd": capital_committed(r),
                            "net_credit_usd": _dims["final_contract"]["net_credit_usd"],
                            "max_loss_usd": _dims["final_contract"]["max_loss_usd"],
                            "debit": _dims["final_contract"]["max_loss_usd"],
                            "assignment_possible": True} if is_credit(r) else {}),
                        **({"spread": {"short_con_id": r.short_contract.conId,
                                       "short_strike": r.short_strike,
                                       "width": abs(r.short_strike - r.strike)}}
                           if r.short_contract is not None else {}),
                    },
                }
                _credit_reservation = None
                if is_credit(r):



                    _credit_reservation, _reservation_pot, _reservation_broker = \
                        await reserve_credit_entry(
                            ib, r, order.orderRef, ledger=DAILY_ENTRY_RESERVATIONS,
                            dimensions=_dims, intent=_entry_intent, audit_path=audit_path)
                    if not _credit_reservation.allowed or not _credit_reservation.should_place:
                        audit(audit_path, "credit_collateral_blocked_submit",
                              decision_id=decision_id,
                              status=_credit_reservation.status,
                              reasons=_credit_reservation.reasons)
                        done.add(ts)
                        continue
                    from exitmgr.order_lock import order_mutation_lock
                    _place_invoked = False
                    _pre_submit_error = None
                    try:
                        with order_mutation_lock():
                            _ok_final, _why_final = _admission_recheck()
                            if not _ok_final:
                                _pre_submit_error = (
                                    "halt marker or NBBO blocks submit after reservation: "
                                    + "; ".join(_why_final))
                            else:

                                # order_mutation_lock is held by the surrounding block.
                                def _place_credit():
                                    nonlocal _place_invoked
                                    _place_invoked = True
                                    return ib.placeOrder(_order_contract, order)

                                trade = entry_safety.place_with_live_authority(
                                    _submission_authority, _place_credit)
                    except Exception as _place_error:
                        if not _place_invoked:
                            await asyncio.to_thread(
                                DAILY_ENTRY_RESERVATIONS.clear_not_transmitted, order.orderRef)
                            audit(audit_path, "credit_entry_reservation_cleared",
                                  order_ref=order.orderRef,
                                  status="definite_pre_submit_failure")
                        else:
                            audit(audit_path, "credit_place_ambiguous_reservation_retained",
                                  order_ref=order.orderRef, error=str(_place_error))
                        raise
                    if _pre_submit_error is not None:
                        await asyncio.to_thread(
                            DAILY_ENTRY_RESERVATIONS.clear_not_transmitted, order.orderRef)
                        audit(audit_path, "credit_entry_reservation_cleared",
                              order_ref=order.orderRef, status="definite_pre_submit_block",
                              reason=_pre_submit_error)
                        done.add(ts)
                        continue
                    try:
                        await asyncio.to_thread(
                            DAILY_ENTRY_RESERVATIONS.update_intent,
                            order.orderRef, transmitted="yes")
                    except Exception as _intent_error:


                        audit(audit_path, "credit_intent_transmit_state_unknown",
                              order_ref=order.orderRef, error=str(_intent_error)[:200])
                    _record_day()
                else:
                    _placed = {}

                    def _place():
                        from exitmgr.order_lock import order_mutation_lock
                        with order_mutation_lock():
                            _placed["trade"] = entry_safety.place_with_live_authority(
                                _submission_authority,
                                lambda: ib.placeOrder(_order_contract, order),
                            )

                    _decision = DAILY_ENTRY_RESERVATIONS.reserve_and_place(
                        place=_place, recheck=_admission_recheck, on_placed=_record_day,
                        intent=_entry_intent, **_dims)
                    audit(audit_path, "entry_admission", decision_id=decision_id,
                          **reservation_as_dict(_decision))
                    if not _decision.allowed or not _decision.should_place:
                        approval.post_proposal(token, channel,
                            f":no_entry: *{r.underlying}* NOT placed — admission refused "
                            f"({_decision.status}): {'; '.join(_decision.reasons) or _decision.status}")
                        done.add(ts)
                        continue
                    trade = _placed.get("trade")
                    if trade is None:
                        audit(audit_path, "admission_returned_no_trade",
                              decision_id=decision_id, order_ref=order.orderRef)
                        done.add(ts)
                        continue


                    _credit_reservation = _decision

                _reject_states = {"Cancelled", "ApiCancelled", "Inactive", "Rejected"}
                _live_states = {"PreSubmitted", "Submitted", "Filled"}
                for _ in range(16):
                    await asyncio.sleep(0.5)
                    st = trade.orderStatus.status
                    if st in _live_states or st in _reject_states:
                        break
                st = trade.orderStatus.status
                _reasons = [le.message for le in trade.log if getattr(le, "errorCode", 0)]
                if _credit_reservation is not None:
                    await asyncio.to_thread(
                        DAILY_ENTRY_RESERVATIONS.clear_for_status, order.orderRef, st)
                if st in _reject_states:
                    reason = _reasons[-1] if _reasons else f"order status {st}"
                    approval.post_proposal(token, channel,
                        f":x: *Order REJECTED by IBKR* — `{order_summary(r)}` was NOT placed.\n{reason}")
                    audit(audit_path, "daily_rec_rejected", underlying=r.underlying,
                          order=order_summary(r), status=st, reason=reason)
                    done.add(ts)
                    continue
                try:
                    trade_capture.capture_decision(
                        trade_capture.dataset_dir(JOURNAL_PATH), source="daily_slate",
                        symbol=r.underlying, right=r.right, strike=r.strike, expiry=r.expiry,
                        structure=("cash secured put" if is_credit(r) else
                                   ("spread" if r.short_contract is not None else "single")),
                        con_id=getattr(r.contract, "conId", None), chosen_idea=effective_idea,
                        candidates=(capture_candidates or [effective_idea]),
                        raw_strategist=capture_raw, cot=capture_cot,
                        market_context=capture_context,
                        technical_card=capture_technical_card,
                        decision_id=decision_id, revision=revision, event="submitted",
                        model_identity=getattr(r, "model_identity", None),
                        model_identity_source=getattr(r, "model_identity_source", None),
                        final_contract=contract_snapshot(r),
                        order_ref=getattr(trade.order, "orderRef", None),
                        execution_authority=_submission_authority.mode,
                        human_action=(None if autonomous_candidate else
                                      {"action": "approve", "structure_override": ovr,
                                       "quantity_override": qty_ovr,
                                       "full_size": full_size,
                                       "tp_pct": r.tp_pct, "sl_pct": r.sl_pct}))
                except Exception as _capture_error:
                    print(f"[WARN] submitted decision capture failed (continuing): {_capture_error}")




                for _ in range(20):
                    if trade.orderStatus.status == "Filled":
                        break
                    await asyncio.sleep(0.5)
                st = trade.orderStatus.status
                if _credit_reservation is not None:
                    await asyncio.to_thread(
                        DAILY_ENTRY_RESERVATIONS.clear_for_status, order.orderRef, st)
                if st in _reject_states:
                    reason = _reasons[-1] if _reasons else f"order status {st} after ACK"
                    audit(audit_path, "daily_rec_rejected_after_ack",
                          underlying=r.underlying, order=order_summary(r),
                          status=st, reason=reason)
                    done.add(ts)
                    continue
                _est_debit = (capital_at_risk(r) if is_credit(r)
                              else round(r.limit * 100 * abs(int(r.qty)), 2))


                try:
                    _thesis_str = str(getattr(idea, "thesis", "") or "")
                except Exception as _te:
                    print(f"[WARN] thesis capture failed (continuing): {_te}")
                    _thesis_str = ""




                import json as _j
                _journalable, _fill, _facts = entry_journal_fields(
                    trade, requested_qty=r.qty, estimated_debit=_est_debit, credit=is_credit(r))
                spread_j = ({"spread": {"short_con_id": r.short_contract.conId,
                                        "short_strike": r.short_strike,
                                        "width": abs(r.short_strike - r.strike)}}
                            if r.short_contract is not None else {})
                _journal = {"ts": datetime.now(timezone.utc).isoformat(),
                                  "decision_id": decision_id,
                                  "decision_revision": revision,
                                  "model_identity": getattr(r, "model_identity", None),
                                  "model_identity_source": getattr(
                                      r, "model_identity_source", None),
                                  "contract_id": r.contract.conId,
                                  "symbol": r.underlying, "right": r.right, "expiry": r.expiry,
                                  "strike": r.strike, "quantity": r.qty,
                                  "debit": (capital_at_risk(r) if is_credit(r) else
                                            round(r.limit * 100 * r.qty, 2)),
                                  "profit_target_pct": (None if is_credit(r) else r.tp_pct),






                                  "tp_policy": construction.TP_POLICY_CURRENT,
                                  "stop_pct": (None if is_credit(r) else r.sl_pct),
                                  "conviction": getattr(idea, "conviction", -1),













                                  "intended_hold_days": positive_hold_days(
                                      getattr(r, "intended_hold_days", None)),
                                  "intended_hold_days_source": getattr(
                                      idea, "_intended_hold_days_source", "model"),



                                  **doctrine_stamp(
                                      getattr(r, "intended_hold_days", None), is_credit(r)),
                                  "model_intended_hold_days": positive_hold_days(
                                      getattr(idea, "_model_intended_hold_days", None)),
                                  "earnings_date": getattr(r, "earnings_date", None),
                                  "earnings_unchecked": bool(
                                      getattr(r, "earnings_unchecked", False)),
                                  "earnings_overlap_warning": (
                                      getattr(r, "earnings_warn", "") or None),
                                  "earnings_day_warning": bool(
                                      getattr(r, "earnings_day_warning", False)),
                                  "earnings_reconsidered": bool(
                                      getattr(r, "earnings_reconsidered", False)),
                                  "earnings_reconsideration_decision": getattr(
                                      r, "earnings_reconsideration_decision", None),
                                  "earnings_reconsideration_phase": getattr(
                                      r, "earnings_reconsideration_phase", None),
                                  "earnings_reconsideration_reason": (
                                      getattr(r, "earnings_reconsideration_reason", "") or None),
                                  "earnings_reconsideration_model_identity": getattr(
                                      r, "earnings_reconsideration_model_identity", None),
                                  "earnings_reconsideration_model_identity_source": getattr(
                                      r, "earnings_reconsideration_model_identity_source", None),
                                  "earnings_reconsideration_order_snapshot": getattr(
                                      r, "earnings_reconsideration_order_snapshot", None),
                                  "earnings_reconsideration_order_sha256": getattr(
                                      r, "earnings_reconsideration_order_sha256", None),
                                  "thesis": _thesis_str,

                                  "order_id": getattr(trade.order, "orderId", None),
                                  "order_ref": getattr(trade.order, "orderRef", None),
                                  "order_status": st,
                                  "underlying_price_at_entry": (r.spot or None),
                                  "entry_delta": (r.entry_delta or None),
                                  "entry_iv": (r.entry_iv or None),


















                                  "entry_bid": (getattr(r, "entry_bid", 0.0) or None),
                                  "entry_ask": (getattr(r, "entry_ask", 0.0) or None),
                                  "entry_spread_pct": (getattr(r, "entry_spread_pct", 0.0) or None),
                                  "dte_at_entry": (r.dte or None),
                                  "dte_adjusted": bool(r.dte_adjusted),
                                  **spread_j}
                if is_credit(r):
                    _journal.update({
                        "side": "credit", "structure": "cash secured put",
                        "action": "SELL", "quantity": -abs(int(r.qty)),
                        "contracts": abs(int(r.qty)),
                        "collateral_usd": capital_committed(r),
                        "net_credit_usd": round(r.net_credit_usd, 2),
                        "max_loss_usd": capital_at_risk(r),
                        "assignment_possible": True,
                    })
                _journal.update(_fill)
                _journal.update(identity_fields(_RUNTIME_IDENTITY))
                if _journalable:
                    from exitmgr.journal_io import append_jsonl_locked
                    append_jsonl_locked(journal_path, _journal)
                    audit(audit_path, "entry_journalled_from_fill", order_ref=order.orderRef,
                          filled=_facts.filled_qty, requested=int(r.qty),
                          quantity_source=_facts.filled_qty_source,
                          basis_source=_journal.get("basis_source"), order_status=_facts.status)
                    try:
                        DAILY_ENTRY_RESERVATIONS.resolve_intent(
                            order.orderRef, outcome="journaled", filled_qty=int(_facts.filled_qty))
                    except Exception as _ie:
                        print(f"[WARN] entry intent not released (reconciler will retry): {_ie}")
                else:


                    audit(audit_path, "entry_not_journalled_no_observed_fill",
                          order_ref=order.orderRef, order_status=_facts.status,
                          quantity_source=_facts.filled_qty_source,
                          executions_seen=_facts.execution_count,
                          exposure_unsized=_facts.exposure_unsized)
                    if _facts.terminal and _facts.filled_qty == 0 and not _facts.exposure_unsized:
                        try:
                            DAILY_ENTRY_RESERVATIONS.resolve_intent(
                                order.orderRef, outcome="terminal_no_fill")
                        except Exception as _ie:
                            print(f"[WARN] entry intent not released: {_ie}")
                placed_watch.append({"trade": trade, "r": r, "t0": time.monotonic(),
                                     "decision_id": decision_id,
                                     "model_identity": getattr(r, "model_identity", None),
                                     "model_identity_source": getattr(
                                         r, "model_identity_source", None),
                                     "alerted": False, "filled_logged": st == "Filled",
                                     "credit_reservation": is_credit(r),
                                     "order_ref": order.orderRef})
                tag = " _(your levels)_" if (ov_tp or ov_sl) else ""
                _model_receipt = approval.model_attribution_line(
                    getattr(r, "model_identity", None),
                    getattr(r, "model_identity_source", None))
                _earn_receipt = (
                    ":rotating_light: *EARNINGS TODAY — ENTRY WARNING remains attached:* "
                    f"{r.earnings_warn}\n"
                    if getattr(r, "earnings_day_warning", False) else "")
                if is_credit(r):
                    approval.post_proposal(
                        token, channel,
                        _earn_receipt + _model_receipt
                        + f":white_check_mark: *Placed* `{order_summary(r)}` — collateral and "
                        "short-option lifecycle tracked in the journal.")
                else:
                    approval.post_proposal(token, channel,
                        _earn_receipt + _model_receipt
                        + f":white_check_mark: *Placed* `{order_summary(r)}` — take profit "
                        f"{_pct_text(r.tp_pct)} / stop -{r.sl_pct:.0f}%{tag}")
                audit(audit_path, "daily_rec_executed", underlying=r.underlying, order=order_summary(r),
                      decision_id=decision_id,
                      profit_target_pct=r.tp_pct, stop_pct=r.sl_pct)
                done.add(ts)


            _watch_entry_fills(placed_watch, token, audit_path)
            await asyncio.sleep(15)

        _watch_entry_fills(placed_watch, token, audit_path)
        for w in placed_watch:
            if not w["filled_logged"]:
                try:
                    import json as _j
                    with open(FILLS_PATH, "a") as f:
                        f.write(_j.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                                          "event": "entry_fill_final",
                                          "decision_id": w.get("decision_id"),
                                          "model_identity": w.get("model_identity"),
                                          "model_identity_source": w.get("model_identity_source"),
                                          "contract_id": getattr(w["r"].contract, "conId", None),
                                          "symbol": w["r"].underlying,
                                          "order_id": getattr(getattr(w["trade"], "order", None), "orderId", None),
                                          "order_ref": getattr(getattr(w["trade"], "order", None), "orderRef", None),
                                          "status": w["trade"].orderStatus.status,
                                          "note": "still unfilled when the slate watcher exited"}) + "\n")
                except Exception as e:
                    print(f"[WARN] final fill-status write failed: {e}")
        print(f"[INFO] daily slate done — {len(done)}/{len(pending)} decided")
        return 0
    finally:
        from exitmgr.slate_lock import clear_slate_active
        clear_slate_active()
        await conn.disconnect()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch-mins", type=int, default=360, help="how long to watch for your taps")
    ap.add_argument("--config", default="config.yaml")


    ap.add_argument("--ticker", default=None, help="user-directed: underlying symbol (e.g. QQQ). Skips the model slate.")
    ap.add_argument("--right", default=None, choices=["C", "P", "c", "p"],
                    help="C=call (bullish), P=put (bearish); ordinary directed default is C, "
                         "but exact spreads require this explicitly")
    ap.add_argument("--dte", type=int, default=30, help="target days-to-expiry (min-DTE floor 25 applies; prefer 25-45)")
    ap.add_argument("--delta", type=float, default=0.60, help="target option delta (clamped into the 0.55-0.65 band)")
    ap.add_argument("--structure", default="", help="override structure, e.g. 'call debit spread' (default: long call/put)")
    ap.add_argument("--tp", type=float, default=0.0,
                    help="OPTIONAL explicit catastrophe backstop, 100-500%% (0 = none, the normal "
                         "case). There is no longer a global/tiered default take-profit — see "
                         "RULING_TAKE_PROFIT.md / Sol audit R5 R2.")
    ap.add_argument("--stop", type=float, default=0.0, help="stop %% (0 = global default -50%%)")
    ap.add_argument("--no-model-thesis", action="store_true", dest="no_model_thesis",
                    help="skip asking the model for its view of a --ticker name (keeps --thesis "
                         "verbatim). Default is to ask and record the real reasoning.")
    ap.add_argument("--hold-days", type=int, default=0, dest="hold_days",
                    help="REQUIRED for --ticker: the intended hold in calendar days you are "
                         "underwriting. The entry then needs DTE >= %dx it. Omitted, the run "
                         "falls back to trading.intended_hold_days_fallback in config.yaml and "
                         "REFUSES if that is null. It is never derived from --dte."
                         % DEBIT_HOLD_FLOOR_MULTIPLE)
    ap.add_argument("--conviction", type=int, default=6, help="conviction 1-10 (display only)")
    ap.add_argument("--thesis", default="User-directed proposal.", help="thesis line shown in the proposal")
    ap.add_argument("--exact-expiry", default=None,
                    help="exact spread expiry YYYYMMDD (or YYYY-MM-DD)")
    ap.add_argument("--exact-long-strike", type=float, default=None)
    ap.add_argument("--exact-short-strike", type=float, default=None)
    ap.add_argument("--exact-qty", type=int, default=None)
    ap.add_argument("--exact-max-debit", type=float, default=None,
                    help="maximum executable net debit per spread")
    ap.add_argument("--expected-earnings-date", default=None,
                    help="YYYY-MM-DD; the one-time waiver is valid only on an exact live match")
    ap.add_argument("--override-earnings-blackout", action="store_true",
                    help="explicitly authorize the one-time earnings-blackout waiver for the "
                         "exact spread; without this flag the normal earnings gate still blocks")
    ap.add_argument("--client-id", type=int, default=None, dest="client_id", help="override IBKR clientId (avoid clash with the cron's 93)")
    _args = ap.parse_args()



    if _args.ticker:
        try:
            user_directed_idea(_args)
        except ValueError as _structure_error:
            raise SystemExit("[REFUSED] %s" % _structure_error)
    raise SystemExit(asyncio.run(run(_args)))
