"""The armed trader supports auto_approve_within_gates; no approval is awaited when that
authority applies. A refreshed exact order clears every hard gate before autonomous
submission; otherwise authority fails closed.
"""
import dataclasses
import hashlib
import asyncio
import json
import math
import os
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Dict, List, Optional, Set

from exitmgr.account import get_pot_snapshot
from exitmgr.entry_reflection import (
    capture_journal_basis, build_admission_risk_book, build_debit_risk_book,
)
from exitmgr.risk import (
    RiskLimits, OpenPosition, ProposedTrade, GateDecision, evaluate_trade, day_pnl_pct,
    INDEX_UNDERLYINGS, effective_pot, sector_of, sector_exposure, external_book_for,
)
from exitmgr.strategist import (
    propose, propose_intents, select_candidate, reconsider_same_day_earnings, TradeIdea,



    DEBIT_STRUCTURES, _require_allowed_structure,
)
from exitmgr.entry_contract import StageAIntent, RuntimeCandidate
from exitmgr.entry_builder import (
    CandidateBinding, CandidateBuildError, build_entry_candidates,
    bindings_for_stage_b, reprice_binding, select_binding,
)
from exitmgr.entry_reservation import (
    DEFAULT_MAX_OBSERVATION_AGE_S, EntryReservationLedger, ReservationDecision,
    reservation_as_dict,
)
from exitmgr import approval, construction, research, regime, slate_lock, trade_capture, reload_queue
from exitmgr import entry_safety, pipeline_notice
from exitmgr.entry_throttle import (
    EntryThrottleStore, EntryThrottleUnreadable, entry_day_open_counts, record_entry_open,
)
from exitmgr.campaign_conflicts import (
    CampaignConflictRegistryError, active_campaign_conflict_symbols,
)
from dataclasses import replace as _replace_dc
from exitmgr.config import ConstructionConfig, load_config
from exitmgr.runtime_identity import (
    RuntimeIdentity, RuntimeIdentityError, freeze_runtime_identity, identity_fields,
)




_IB_CALL_TIMEOUT_S = 30




_UNREAD = object()





























STAGE_A_REQUEST_CONTINUOUS = {"thinking": "disabled", "return_cot": True,
                              "return_identity": True}
SAME_DAY_EARNINGS_REVIEW_REQUEST = {
    "thinking": "disabled", "return_raw": True, "return_identity": True,
}
STAGE_A_REQUEST_SLATE = {"thinking": "enabled", "return_cot": True, "return_identity": True}


STAGE_A_REQUEST_SHADOW = {"thinking": "enabled", "return_cot": True, "return_identity": True}




STAGE_A_REQUEST_DIRECTED = {"thinking": "enabled"}
STAGE_B_REQUEST = {"thinking": "enabled", "return_raw": True, "return_cot": True,
                   "return_identity": True, "timeout": 600}


def stage_b_technical_failures(outcomes):
    """Public API contract; production-derived narrative omitted."""
    return [row for row in outcomes or ()
            if row.get("outcome") in {
                "candidate_error", "selector_error", "invalid_selection"}]


def stage_b_failure_detail(outcomes):
    labels = {"candidate_error": "contract data/build failed",
              "selector_error": "contract-selection model failed",
              "invalid_selection": "model returned an invalid contract selection"}
    return "; ".join(
        f"{row.get('underlying', '?')}: {labels[row['outcome']]}"
        + (f" ({row['error']})" if row.get("error") else "")
        for row in stage_b_technical_failures(outcomes))


def autonomous_entry_blockers(symbol, *, approved_names, limits, research_degraded=None):
    """Public API contract; production-derived narrative omitted."""
    reasons = []
    if research_degraded:
        reasons.append(
            "research is degraded (%s) -- a quote-only context may be proposed, never "
            "auto-submitted" % (str(research_degraded)[:160],))
    u = str(symbol or "").strip().upper()
    if not u:
        reasons.append("no underlying symbol on the order")
        return tuple(reasons)
    if u in INDEX_UNDERLYINGS:
        return tuple(reasons)
    if u not in {str(n).upper() for n in (approved_names or ())}:
        reasons.append(
            "%s is not in trading.approved_names -- allow_model_names lets the MODEL name it, "
            "which is not the same as a human having approved it" % u)
    sector_map = getattr(limits, "sector_map", None) or {}
    if u not in {str(k).upper() for k in sector_map}:
        reasons.append(
            "%s is not classified in trading.sector_map -- risk.sector_of() keys an unmapped "
            "symbol to itself, so it is a sector of one and max_sector_agg_pct cannot bind "
            "on it" % u)
    return tuple(reasons)


def autonomous_execution_gate(*, enabled, blockers, gate, capital_at_risk_usd):
    """Public API contract; production-derived narrative omitted."""
    reasons = list(blockers or ())
    if enabled is not True:
        reasons.append("autonomous entry is disabled")
    if not bool(getattr(gate, "approved", False)):
        reasons.extend(getattr(gate, "reasons", ()) or ("risk gate did not approve",))
    elif list(getattr(gate, "reasons", ()) or ()):
        reasons.extend(getattr(gate, "reasons", ()))
    try:
        risk_usd = float(capital_at_risk_usd)
        cap_usd = float(getattr(gate, "per_trade_cap", 0.0) or 0.0)
        if not math.isfinite(risk_usd) or risk_usd <= 0:
            reasons.append("capital at risk is missing/non-positive")
        if not math.isfinite(cap_usd) or cap_usd <= 0:
            reasons.append("risk gate per-trade cap is missing/non-positive")
        elif risk_usd > cap_usd + 1e-6:
            reasons.append(
                f"capital at risk ${risk_usd:,.2f} exceeds gate cap ${cap_usd:,.2f}"
            )
    except Exception as exc:
        reasons.append(f"autonomous dollar boundary unavailable: {exc}")
    return entry_safety.SafetyResult(
        not reasons, tuple(dict.fromkeys(str(reason) for reason in reasons if reason))
    )


def entry_eligible_universe(names, *, quote_prices, research_symbols, net_liq,
                            available_funds, positions, pot_day_start, approved_names,
                            limits, require_autonomous=False, external_book=None):
    """Public API contract; production-derived narrative omitted."""
    eligible, rejected = [], {}



    xb = external_book
    if xb is None and getattr(limits, "external_book_path", None):
        xb = external_book_for(limits)
    quote_map = {
        str(symbol or "").strip().upper(): price
        for symbol, price in dict(quote_prices or {}).items()
    }
    research_set = {str(s or "").strip().upper() for s in (research_symbols or ())}
    approved_set = {str(s or "").strip().upper() for s in (approved_names or ())}



    position_book = list(positions or ())
    seen = set()
    for raw in names or ():
        symbol = str(raw or "").strip().upper()
        if symbol in seen:
            continue
        seen.add(symbol)
        reasons = []
        if not symbol:
            rejected[symbol] = ("no underlying symbol",)
            continue
        try:
            px = float(quote_map.get(symbol))
            if not math.isfinite(px) or px <= 0:
                raise ValueError
        except (TypeError, ValueError):
            reasons.append("no usable live underlying quote")
        if symbol not in research_set:
            reasons.append("daily price research unavailable")
        if require_autonomous:
            reasons.extend(autonomous_entry_blockers(
                symbol, approved_names=approved_set, limits=limits))
        gate = evaluate_trade(
            ProposedTrade(symbol, 1.0, symbol in INDEX_UNDERLYINGS, 1,
                          is_long=True, stop_pct=30.0),
            net_liq=net_liq, available_funds=available_funds,
            open_positions=position_book, pot_day_start=pot_day_start,
            approved_names=approved_set, limits=limits,
            external_book=xb)
        reasons.extend(gate.reasons)
        reasons = list(dict.fromkeys(str(r) for r in reasons if r))
        if reasons:
            rejected[symbol] = tuple(reasons)
        else:
            eligible.append(symbol)
    return tuple(eligible), rejected





def audit(path: str, event: str, **fields) -> dict:
    rec = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(rec, default=str) + "\n")
    return rec


def _trading_day(now=None) -> str:
    """Public API contract; production-derived narrative omitted."""
    try:
        et = _US_EASTERN
        n = now or datetime.now(et)
        if getattr(n, "tzinfo", None) is None:
            n = n.replace(tzinfo=timezone.utc)
        return str(n.astimezone(et).date())
    except Exception:
        return str((now or datetime.now(timezone.utc)).date())


def day_start_pot(baselines: Dict[str, float], today: str, current_net_liq: float):
    """Return the sticky start-of-day baseline used by the circuit breaker.

The active threshold is ``trading.daily_halt_pct = 0.20`` (-20%). Invalid or nonpositive
NetLiq reads cannot create or overwrite the baseline, and a new date drops stale keys.
    """
    b = dict(baselines or {})
    valid = isinstance(current_net_liq, (int, float)) and current_net_liq == current_net_liq \
        and current_net_liq > 0
    if today not in b:
        if not valid:
            return (b.get(today, 0.0) or 0.0), b
        b = {today: float(current_net_liq)}
    return b[today], b


_US_EASTERN = ZoneInfo("America/New_York")
_SESSION_OPEN_MIN_ET = 9 * 60 + 30
_SESSION_CLOSE_MIN_ET = 16 * 60


def _market_open_at(now: Optional[datetime] = None) -> bool:
    """Public API contract; production-derived narrative omitted."""
    t = now or datetime.now(timezone.utc)
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    et = t.astimezone(_US_EASTERN)
    if et.weekday() >= 5:
        return False
    mins = et.hour * 60 + et.minute + et.second / 60.0
    return _SESSION_OPEN_MIN_ET <= mins < _SESSION_CLOSE_MIN_ET


def _market_open() -> bool:
    """Public API contract; production-derived narrative omitted."""
    return _market_open_at()






OPEN_DELAY_MIN = int(os.environ.get("EXITMGR_OPEN_DELAY_MIN", "5"))

def entry_window_wait_seconds(now: Optional[datetime] = None,
                              delay_min: Optional[int] = None) -> float:
    """Public API contract; production-derived narrative omitted."""
    t = now or datetime.now(timezone.utc)
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    et = t.astimezone(_US_EASTERN)
    delay = OPEN_DELAY_MIN if delay_min is None else int(delay_min)
    open_min = _SESSION_OPEN_MIN_ET + max(0, delay)

    mins_now = et.hour * 60 + et.minute + et.second / 60.0
    if et.weekday() < 5 and open_min <= mins_now < _SESSION_CLOSE_MIN_ET:
        return 0.0


    day = et
    if not (et.weekday() < 5 and mins_now < open_min):
        day = et + timedelta(days=1)
    while day.weekday() >= 5:
        day = day + timedelta(days=1)

    target = day.replace(hour=open_min // 60, minute=open_min % 60, second=0, microsecond=0)
    return max(0.0, (target - et).total_seconds())









CREDIT_SIDE = "credit"
CSP_STRUCTURE = "cash secured put"


CREDIT_MAX_COLLATERAL_PCT = 0.80






CREDIT_MIN_PREMIUM_PCT = 0.0125


CREDIT_MIN_DTE_DEFAULT = 3
CREDIT_MAX_DTE_DEFAULT = 45
_EPS = 1e-6


def _side_of(obj) -> str:
    """Public API contract; production-derived narrative omitted."""
    try:
        s = str(getattr(obj, "side", "debit") or "debit").strip().lower()
    except Exception:
        return "debit"
    return CREDIT_SIDE if s == CREDIT_SIDE else "debit"


def is_credit(obj) -> bool:
    """Public API contract; production-derived narrative omitted."""
    return _side_of(obj) == CREDIT_SIDE


def _short_option_authority_from_env(obj):
    """Public API contract; production-derived narrative omitted."""
    truthy = {"1", "true", "yes", "on"}
    return entry_safety.short_option_entry_authority(
        credit=is_credit(obj),
        credit_entries_enabled=(
            str(os.environ.get("EXITMGR_CREDIT_ENTRIES", "")).strip().lower() in truthy),
        assigned_stock_authority_enabled=(
            str(os.environ.get("EXITMGR_ASSIGNED_STOCK_AUTHORITY", "")).strip().lower()
            in truthy),
    )


def _fnum(x, default=None):
    """Public API contract; production-derived narrative omitted."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if v == v and v not in (float("inf"), float("-inf")) else default


def credit_structure_ok(idea) -> tuple:
    """Public API contract; production-derived narrative omitted."""
    if not is_credit(idea):
        return True, ""
    structure = str(getattr(idea, "structure", "") or "").strip().lower()
    if structure != CSP_STRUCTURE:
        return False, (f"NAKED-SHORT REFUSED: credit structure {structure!r} is not a "
                       f"{CSP_STRUCTURE!r} -- the only short this account may ever sell")
    strike = _fnum(getattr(idea, "strike", 0.0), None)
    collateral = _fnum(getattr(idea, "collateral_usd", 0.0), None)
    net_credit = _fnum(getattr(idea, "net_credit_usd", 0.0), None)
    max_loss = _fnum(getattr(idea, "max_loss_usd", 0.0), None)
    for label, v in (("strike", strike), ("collateral_usd", collateral),
                     ("net_credit_usd", net_credit), ("max_loss_usd", max_loss)):
        if v is None or v <= 0:
            return False, f"credit idea missing/invalid {label} ({v!r})"
    if abs(max_loss - (collateral - net_credit)) > 0.01:
        return False, (f"credit idea inconsistent: max_loss ${max_loss:,.2f} != collateral "
                       f"${collateral:,.2f} - credit ${net_credit:,.2f}")
    if net_credit >= collateral:
        return False, "credit idea claims a credit >= its collateral (impossible for a CSP)"



    roc = net_credit / collateral
    if roc + 1e-9 < CREDIT_MIN_PREMIUM_PCT:
        return False, (f"credit idea earns {roc:.3%} of its ${collateral:,.2f} collateral, under the "
                       f"{CREDIT_MIN_PREMIUM_PCT:.2%} premium bar (needs "
                       f"${collateral * CREDIT_MIN_PREMIUM_PCT:,.2f}, offers ${net_credit:,.2f})")
    return True, ""


















def _structure_implied_right(raw) -> str:
    """Public API contract; production-derived narrative omitted."""
    s = str(raw or "").lower()
    has_call, has_put = "call" in s, "put" in s
    if has_call and not has_put:
        return "C"
    if has_put and not has_call:
        return "P"
    return ""


def debit_structure_ok(idea) -> tuple:
    """Public API contract; production-derived narrative omitted."""
    if is_credit(idea):
        return True, ""
    raw = getattr(idea, "structure", "")
    try:
        _require_allowed_structure("debit", raw)
    except ValueError as exc:
        return False, "STRUCTURE REFUSED: %s" % (exc,)
    implied = _structure_implied_right(raw)
    stated = {"bullish": "C", "bearish": "P"}.get(
        str(getattr(idea, "direction", "") or "").strip().lower())
    if implied and stated and implied != stated:
        _word = {"C": "call", "P": "put"}
        return False, (
            "STRUCTURE/DIRECTION CONTRADICTION: structure %r names a %s but direction is %r. The "
            "constructor takes the option right from `direction`, so this idea would be built and "
            "journalled as a %s under a %s's name. Refused, not repaired -- neither field can be "
            "shown to be the mistaken one. Permitted: %s."
            % (raw, _word[implied], getattr(idea, "direction", ""), _word[stated], _word[implied],
               ", ".join(sorted(DEBIT_STRUCTURES))))
    return True, ""


def required_collateral(strike: float, contracts: int) -> Optional[float]:
    """Public API contract; production-derived narrative omitted."""
    k = _fnum(strike, None)
    try:
        q = int(contracts)
    except (TypeError, ValueError):
        return None
    if k is None or k <= 0 or q < 1:
        return None
    return round(k * 100.0 * q, 2)


def collateral_capacity(*, required: Optional[float], deployed: Optional[float],
                        net_liq: Optional[float], available_funds: Optional[float],
                        max_pct: float = CREDIT_MAX_COLLATERAL_PCT):
    """Public API contract; production-derived narrative omitted."""
    reasons: List[str] = []
    req = _fnum(required, None)
    nl = _fnum(net_liq, None)
    af = _fnum(available_funds, None)
    dep = _fnum(deployed, None) if deployed is not None else None
    if req is None or req <= 0:
        reasons.append(f"required collateral unavailable/non-positive ({required!r})")
    if nl is None or nl <= 0:
        reasons.append(f"net liquidation value unusable ({net_liq!r})")
    if af is None or af < 0:
        reasons.append(f"available funds unusable ({available_funds!r})")
    if dep is None or dep < 0:
        reasons.append("already-deployed collateral could not be verified "
                       "(refusing: an unverifiable book is never treated as empty)")
    if reasons:
        return entry_safety.SafetyResult(False, tuple(reasons))
    if req > af + _EPS:
        reasons.append(f"insufficient cash to secure the put: need ${req:,.2f}, "
                       f"available ${af:,.2f}")
    cap = max(0.0, float(max_pct)) * nl
    if dep + req > cap + _EPS:
        reasons.append(f"collateral cap: deployed ${dep:,.2f} + this ${req:,.2f} = "
                       f"${dep + req:,.2f} > {float(max_pct):.0%}-of-net-liq cap ${cap:,.2f}")
    return entry_safety.SafetyResult(not reasons, tuple(reasons))


@dataclass(frozen=True)
class BrokerCollateralSnapshot:
    total: float
    visible_order_refs: frozenset
    visible_con_ids: frozenset


async def broker_csp_collateral_snapshot(
        ib, audit_path: Optional[str] = None) -> Optional[BrokerCollateralSnapshot]:
    """Public API contract; production-derived narrative omitted."""
    total = 0.0
    visible_order_refs = set()
    visible_con_ids = set()
    try:
        positions = await asyncio.wait_for(ib.reqPositionsAsync(), _IB_CALL_TIMEOUT_S)




        long_puts = {}
        short_puts = []
        long_calls = {}
        short_calls = []
        for pos in positions or []:
            contract = getattr(pos, "contract", None)
            quantity = _fnum(getattr(pos, "position", 0), None)
            if contract is None or quantity is None or quantity == 0:
                continue
            sec_type = str(getattr(contract, "secType", "") or "").upper()
            if sec_type and sec_type not in ("OPT", "FOP"):
                continue
            right = str(getattr(contract, "right", "") or "").upper()[:1]
            if right not in ("C", "P"):
                continue
            strike = _fnum(getattr(contract, "strike", None), None)
            symbol = getattr(contract, "symbol", "")
            expiry = getattr(contract, "lastTradeDateOrContractMonth", "")
            currency = getattr(contract, "currency", "")
            trading_class = getattr(contract, "tradingClass", "")
            multiplier = getattr(contract, "multiplier", "")
            symbol = symbol.strip().upper() if isinstance(symbol, str) else ""
            expiry = expiry.strip() if isinstance(expiry, str) else ""
            currency = currency.strip().upper() if isinstance(currency, str) else ""
            trading_class = (
                trading_class.strip().upper() if isinstance(trading_class, str) else "")
            if isinstance(multiplier, (str, int, float)) and not isinstance(multiplier, bool):
                multiplier = str(multiplier).strip()
            else:
                multiplier = ""
            series_parts = (symbol, expiry, currency, trading_class, multiplier)
            series = series_parts if all(series_parts) else None
            con_id = getattr(contract, "conId", None)
            if right == "C":



                if quantity > 0:
                    if series is not None and strike is not None and strike > 0:
                        long_calls.setdefault(series, []).append([strike, float(quantity)])
                else:
                    short_calls.append((series, strike, abs(float(quantity)), con_id, symbol))
                continue
            if strike is None or strike <= 0:
                return None
            if quantity > 0:
                if series is not None:
                    long_puts.setdefault(series, []).append([strike, float(quantity)])
                continue
            short_puts.append((series, strike, abs(float(quantity)), con_id, symbol))

        for series, strike, quantity, con_id, symbol in sorted(
                short_calls, key=lambda row: (row[1] is None, row[1] or 0.0)):
            residual = quantity
            if series is not None and strike is not None and strike > 0:



                for long_leg in sorted(
                        long_calls.get(series, []), key=lambda row: row[0], reverse=True):
                    if long_leg[0] >= strike or long_leg[1] <= 0:
                        continue
                    covered = min(residual, long_leg[1])
                    residual -= covered
                    long_leg[1] -= covered
                    if covered and audit_path:
                        audit(audit_path, "debit_spread_short_call_classified",
                              symbol=symbol, con_id=con_id, strike=strike,
                              covered_quantity=covered)
                    if residual <= 1e-9:
                        break
            if residual > 1e-9 and audit_path:
                audit(audit_path, "short_call_position_detected",
                      symbol=symbol, con_id=con_id, strike=strike,
                      quantity=-residual, covered_quantity=quantity - residual,
                      note=("INVARIANT 1 VIOLATION: account holds residual uncovered "
                            "short-call quantity"))

        for series, strike, quantity, con_id, symbol in sorted(
                short_puts, key=lambda row: row[1]):
            residual = quantity
            if series is not None:


                for long_leg in sorted(long_puts.get(series, []), key=lambda row: row[0]):
                    if long_leg[0] < strike or long_leg[1] <= 0:
                        continue
                    covered = min(residual, long_leg[1])
                    residual -= covered
                    long_leg[1] -= covered
                    if covered and audit_path:
                        audit(audit_path, "debit_spread_short_put_excluded_from_csp_collateral",
                              symbol=symbol, con_id=con_id, strike=strike,
                              covered_quantity=covered)
                    if residual <= 1e-9:
                        break
            if residual <= 1e-9:
                continue
            total += strike * 100.0 * residual
            if isinstance(con_id, int) and con_id > 0:
                visible_con_ids.add(con_id)
    except Exception as exc:
        if audit_path:
            audit(audit_path, "deployed_collateral_error", stage="positions", error=str(exc))
        return None
    try:
        orders = await asyncio.wait_for(ib.reqAllOpenOrdersAsync(), _IB_CALL_TIMEOUT_S)
        terminal = {"Cancelled", "ApiCancelled", "Inactive", "Filled"}
        for trade in orders or []:
            order = getattr(trade, "order", None)
            contract = getattr(trade, "contract", None)
            status = getattr(getattr(trade, "orderStatus", None), "status", None)
            if order is None or contract is None or status in terminal:
                continue
            if str(getattr(order, "action", "") or "").upper() != "SELL":
                continue
            order_ref = str(getattr(order, "orderRef", "") or "")
            open_close = str(getattr(order, "openClose", "") or "").upper()
            if open_close == "C" or order_ref.startswith("alfred-exit:"):
                continue
            if str(getattr(contract, "right", "") or "").upper()[:1] != "P":
                continue
            strike = _fnum(getattr(contract, "strike", None), None)
            quantity = _fnum(getattr(order, "totalQuantity", 0), None)
            if strike is None or strike <= 0 or quantity is None or quantity <= 0:
                return None
            total += strike * 100.0 * abs(quantity)
            if order_ref.startswith("alfred-entry:"):
                visible_order_refs.add(order_ref)
            con_id = getattr(contract, "conId", None)
            if isinstance(con_id, int) and con_id > 0:
                visible_con_ids.add(con_id)
    except Exception as exc:
        if audit_path:
            audit(audit_path, "deployed_collateral_error", stage="open_orders", error=str(exc))
        return None
    return BrokerCollateralSnapshot(
        total=round(total, 2),
        visible_order_refs=frozenset(visible_order_refs),
        visible_con_ids=frozenset(visible_con_ids),
    )


@dataclass(frozen=True)
class EntryOrderView:
    """Public API contract; production-derived narrative omitted."""

    readable: bool
    trades: tuple = ()
    entry_order_refs: frozenset = frozenset()
    open_buy_count: int = 0
    error: Optional[str] = None



    working: tuple = ()

    def working_for_ref(self, order_ref) -> "Optional[WorkingEntryOrder]":
        ref = str(order_ref or "")
        if not ref:
            return None
        for row in self.working:
            if row.order_ref == ref:
                return row
        return None

    def working_buys_for_con_id(self, con_id) -> tuple:
        try:
            cid = int(con_id)
        except (TypeError, ValueError):
            return ()
        if cid <= 0:
            return ()
        return tuple(row for row in self.working
                     if row.action == "BUY" and cid in row.con_ids)


@dataclass(frozen=True)
class WorkingEntryOrder:
    """Public API contract; production-derived narrative omitted."""

    order_ref: str
    action: str
    status: str
    con_ids: frozenset = frozenset()
    remaining: Optional[float] = None
    filled: Optional[float] = None


async def broker_entry_order_view(ib, audit_path: Optional[str] = None) -> EntryOrderView:
    """Public API contract; production-derived narrative omitted."""
    try:
        trades = await asyncio.wait_for(ib.reqAllOpenOrdersAsync(), _IB_CALL_TIMEOUT_S)
    except Exception as exc:
        if audit_path:
            audit(audit_path, "admission_observation_unreadable", stage="open_orders",
                  error=str(exc) or type(exc).__name__)
        return EntryOrderView(False, error=str(exc) or type(exc).__name__)
    if trades is None:


        if audit_path:
            audit(audit_path, "admission_observation_unreadable", stage="open_orders",
                  error="reqAllOpenOrdersAsync returned None")
        return EntryOrderView(False, error="open-order read returned None")
    terminal = {"Cancelled", "ApiCancelled", "Inactive", "Filled"}
    refs = set()
    open_buys = 0
    working = []
    try:
        rows = list(trades)
        for t in rows:
            order = getattr(t, "order", None)
            status = getattr(getattr(t, "orderStatus", None), "status", None)
            if order is None or status in terminal:
                continue
            ref = str(getattr(order, "orderRef", "") or "")
            if ref.startswith("alfred-entry:"):
                refs.add(ref)
            action = str(getattr(order, "action", "") or "").upper()
            if action == "BUY":
                open_buys += 1
            working.append(WorkingEntryOrder(
                order_ref=ref, action=action, status=str(status or ""),
                con_ids=_order_con_ids(t),
                remaining=_opt_number(getattr(getattr(t, "orderStatus", None), "remaining", None)),
                filled=_opt_number(getattr(getattr(t, "orderStatus", None), "filled", None))))
    except Exception as exc:
        if audit_path:
            audit(audit_path, "admission_observation_unreadable", stage="open_orders_parse",
                  error=str(exc) or type(exc).__name__)
        return EntryOrderView(False, error=str(exc) or type(exc).__name__)
    return EntryOrderView(True, tuple(rows), frozenset(refs), open_buys, None, tuple(working))


def _opt_number(value):
    """Public API contract; production-derived narrative omitted."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")) or abs(f) >= 1.7e308:
        return None
    return f


def _order_con_ids(trade) -> frozenset:
    """Public API contract; production-derived narrative omitted."""
    out = set()
    contract = getattr(trade, "contract", None)
    for candidate in (getattr(contract, "conId", None),):
        try:
            cid = int(candidate)
        except (TypeError, ValueError):
            cid = 0
        if cid > 0:
            out.add(cid)
    for leg in (getattr(contract, "comboLegs", None) or []):
        try:
            cid = int(getattr(leg, "conId", 0) or 0)
        except (TypeError, ValueError):
            continue
        if cid > 0:
            out.add(cid)
    return frozenset(out)


@dataclass(frozen=True)
class ProtectiveEntryGate:
    """Public API contract; production-derived narrative omitted."""

    allowed: bool
    reasons: tuple = ()
    remaining_qty: Optional[float] = None
    manageable_qty: Optional[int] = None
    entry_order_status: str = ""


def protective_sell_gate(journal_row, view: EntryOrderView) -> ProtectiveEntryGate:
    """Public API contract; production-derived narrative omitted."""
    row = journal_row if isinstance(journal_row, dict) else {}
    qty = row.get("quantity")
    try:
        manageable = int(abs(float(qty))) if qty is not None else None
    except (TypeError, ValueError):
        manageable = None
    if not getattr(view, "readable", False):
        return ProtectiveEntryGate(
            False,
            ("the broker open-order book could not be read (%s); an unknown book is never an "
             "empty one, and selling against one can recreate exposure"
             % (getattr(view, "error", None) or "no error reported"),),
            None, manageable)

    ref = str(row.get("order_ref") or "")
    working = view.working_for_ref(ref) if ref else None
    if working is None:


        con_id = row.get("contract_id")
        buys = view.working_buys_for_con_id(con_id)
        if buys:
            first = buys[0]
            return ProtectiveEntryGate(
                False,
                ("an opening BUY (%s, status %s) is still working on contract %s"
                 % (first.order_ref or "no orderRef", first.status, con_id),),
                first.remaining, manageable, first.status)
        return ProtectiveEntryGate(True, (), 0.0, manageable, "terminal")

    if working.action == "BUY":
        return ProtectiveEntryGate(
            False,
            ("opening order %s is still %s with %s remaining; a protective SELL now closes the "
             "filled part and the remainder then RECREATES the position"
             % (ref, working.status,
                "an unreadable quantity" if working.remaining is None else working.remaining),),
            working.remaining, manageable, working.status)
    return ProtectiveEntryGate(True, (), working.remaining, manageable, working.status)


@dataclass(frozen=True)
class IntentReconciliation:
    """Public API contract; production-derived narrative omitted."""

    readable: bool
    journaled: tuple = ()
    released: tuple = ()
    still_working: tuple = ()
    retained: tuple = ()
    reasons: tuple = ()


async def reconcile_entry_intents(ib, *, ledger, journal_append, audit_path=None,
                                  view=None, fills=None, lookback_days: int = 7,
                                  min_settle_age_s: float = 120.0,
                                  already_journalled=None,
                                  now=None) -> IntentReconciliation:
    """Public API contract; production-derived narrative omitted."""
    from exitmgr import exec_capture as _ec

    try:
        intents = tuple(ledger.open_intents())
    except Exception as exc:
        if audit_path:
            audit(audit_path, "entry_intent_ledger_unreadable", error=str(exc)[:200])
        return IntentReconciliation(False, reasons=(f"intent ledger unreadable: {exc}",))
    if not intents:
        return IntentReconciliation(True)

    if view is None:
        view = await broker_entry_order_view(ib, audit_path)
    if not getattr(view, "readable", False):
        if audit_path:
            audit(audit_path, "entry_intent_reconcile_refused", stage="open_orders",
                  error=str(getattr(view, "error", "")), intents=len(intents))
        return IntentReconciliation(
            False, retained=tuple(i["order_ref"] for i in intents),
            reasons=("the open-order book could not be read; every intent is retained",))

    if fills is None:
        try:
            fills = await _ec.fetch_fills(
                ib, lookback_days, strict=True,
                relevant_order_refs={str(i.get("order_ref") or "") for i in intents})
        except Exception as exc:
            if audit_path:
                audit(audit_path, "entry_intent_reconcile_refused", stage="executions",
                      error=str(exc)[:200], intents=len(intents))
            return IntentReconciliation(
                False, retained=tuple(i["order_ref"] for i in intents),
                reasons=(f"the execution feed could not be read: {exc}",))
    if fills is None:
        return IntentReconciliation(
            False, retained=tuple(i["order_ref"] for i in intents),
            reasons=("the execution feed returned None, which is UNKNOWN, not empty",))

    by_ref = {}
    for row in fills:
        if not isinstance(row, dict):
            continue
        by_ref.setdefault(str(row.get("order_ref") or ""), []).append(row)




    journal_membership = {}
    if callable(already_journalled):
        for intent in intents:
            ref = str(intent.get("order_ref") or "")
            try:
                known = already_journalled(ref)
            except Exception:
                known = None
            if known is None:
                if audit_path:
                    audit(audit_path, "entry_intent_reconcile_refused", stage="journal",
                          order_ref=ref, intents=len(intents))
                return IntentReconciliation(
                    False, retained=tuple(i["order_ref"] for i in intents),
                    reasons=("the entry journal could not be read; every intent is retained",))
            journal_membership[ref] = bool(known)

    clock = float(now) if now is not None else time.time()
    journaled, released, working, retained, reasons = [], [], [], [], []
    for intent in intents:
        ref = str(intent.get("order_ref") or "")
        if view.working_for_ref(ref) is not None:
            working.append(ref)
            try:
                ledger.note_intent_observed(ref)
            except Exception:
                pass
            continue
        matched = by_ref.get(ref, [])
        if any(row.get("_normalization_error") is True for row in matched
               if isinstance(row, dict)):
            retained.append(ref)
            reasons.append(
                "%s: an execution carrying this order_ref was malformed; intent retained" % ref)
            try:
                ledger.note_intent_observed(ref)
            except Exception:
                pass
            continue
        credit = str(intent.get("side") or "debit").lower() == "credit"
        ok, fields = _ec.entry_fill_fields_from_executions(
            matched, primary_con_id=intent.get("con_id"),
            requested_qty=intent.get("requested_qty"),
            estimated_debit=intent.get("estimated_debit"), credit=credit,
            leg_con_ids=intent.get("leg_con_ids"))
        if ok:
            filled = abs(int(fields.get("quantity") or 0))
            row = dict(intent.get("journal_template") or {})
            row.update(fields)
            row["ts"] = datetime.now(timezone.utc).isoformat()
            row["order_ref"] = ref
            row["decision_id"] = row.get("decision_id") or intent.get("decision_id")
            row["entry_reconciled_from_intent"] = True
            row["entry_intent_created_at"] = intent.get("created_at")
            row["entry_intent_transmitted"] = intent.get("transmitted")
            row["code_version"] = intent.get("code_version")
            row["policy_version"] = intent.get("policy_version")
            settled = ledger.journal_and_resolve_intent(
                ref, row=row, filled_qty=filled, journal_append=journal_append,
                already_journalled=already_journalled)
            duplicate = bool(settled.get("duplicate"))
            journaled.append(ref)
            if audit_path:
                audit(audit_path, "entry_intent_materialised", order_ref=ref, filled=filled,
                      duplicate=duplicate, basis_source=fields.get("basis_source"),
                      quantity_source=fields.get("quantity_source"))
            continue





        age = clock - float(intent.get("created_at") or 0.0)
        retained.append(ref)
        reasons.append(
            "%s: no execution and no positively observed terminal-zero broker status "
            "(age %.0fs); intent retained" % (ref, age))
        try:
            ledger.note_intent_observed(ref)
        except Exception:
            pass
    return IntentReconciliation(True, tuple(journaled), tuple(released), tuple(working),
                                tuple(retained), tuple(reasons))


@dataclass(frozen=True)
class AdmissionObservation:
    """Public API contract; production-derived narrative omitted."""

    observed_at_monotonic: float
    pot: object
    positions: tuple
    csp: "BrokerCollateralSnapshot"
    view: EntryOrderView


async def observe_for_admission(ib, positions_fn, audit_path: Optional[str] = None):
    """Public API contract; production-derived narrative omitted."""
    observed_at = time.monotonic()
    view = await broker_entry_order_view(ib, audit_path)
    if not view.readable:
        return None
    try:
        pot = await get_pot_snapshot(ib)
    except Exception as exc:
        if audit_path:
            audit(audit_path, "admission_observation_unreadable", stage="account",
                  error=str(exc) or type(exc).__name__)
        return None
    account_gate = entry_safety.account_snapshot_valid(pot)
    if not account_gate.allowed:
        if audit_path:
            audit(audit_path, "admission_observation_unreadable", stage="account",
                  error="; ".join(account_gate.reasons))
        return None
    try:
        positions = list(await positions_fn(view.trades))
    except Exception as exc:
        if audit_path:
            audit(audit_path, "admission_observation_unreadable", stage="positions",
                  error=str(exc) or type(exc).__name__)
        return None
    csp = await broker_csp_collateral_snapshot(ib, audit_path)
    if csp is None:
        if audit_path:
            audit(audit_path, "admission_observation_unreadable", stage="deployed_collateral",
                  error="deployed collateral or broker visibility could not be verified")
        return None
    return AdmissionObservation(observed_at, pot, tuple(positions), csp, view)


def resting_buy_positions(open_trades, journal_debits, existing_con_ids=()):
    """Public API contract; production-derived narrative omitted."""
    known = {int(c) for c in (existing_con_ids or ()) if c is not None}
    terminal = {"Cancelled", "ApiCancelled", "Inactive", "Filled"}
    out = []
    for t in open_trades or []:
        order = getattr(t, "order", None)
        contract = getattr(t, "contract", None)
        status = getattr(getattr(t, "orderStatus", None), "status", None)
        if order is None or contract is None:
            continue
        if str(getattr(order, "action", "") or "").upper() != "BUY" or status in terminal:
            continue
        symbol = str(getattr(contract, "symbol", "") or "").upper()
        if not symbol:
            continue
        con_id = getattr(contract, "conId", None)
        if con_id is not None and int(con_id) in known:
            continue
        raw_qty = getattr(order, "totalQuantity", None)
        try:
            qty = float(raw_qty)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(
                f"working BUY {symbol} con_id={con_id} has unreadable quantity") from exc
        if isinstance(raw_qty, bool) or not math.isfinite(qty) or qty <= 0:
            raise ValueError(
                f"working BUY {symbol} con_id={con_id} has invalid quantity {raw_qty!r}")
        notional = journal_debits.get(int(con_id)) if con_id is not None else None
        if notional is None:
            raw_limit = getattr(order, "lmtPrice", None)
            try:
                limit = float(raw_limit)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(
                    f"working BUY {symbol} con_id={con_id} has no verifiable exposure price") from exc
            if isinstance(raw_limit, bool) or not math.isfinite(limit) or limit <= 0:
                raise ValueError(
                    f"working BUY {symbol} con_id={con_id} has no verifiable exposure price")
            notional = limit * 100.0 * qty
        try:
            notional = float(notional)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(
                f"working BUY {symbol} con_id={con_id} has unreadable exposure") from exc
        if not math.isfinite(notional) or notional <= 0:
            raise ValueError(
                f"working BUY {symbol} con_id={con_id} has invalid exposure {notional!r}")
        leg_ids = []
        for leg in (getattr(contract, "comboLegs", None) or ()):
            try:
                leg_id = int(getattr(leg, "conId", 0) or 0)
            except (TypeError, ValueError):
                leg_id = 0
            if leg_id > 0 and leg_id not in leg_ids:
                leg_ids.append(leg_id)
        try:
            primary_id = int(con_id or 0)
        except (TypeError, ValueError):
            primary_id = 0
        if not leg_ids and primary_id > 0:
            leg_ids.append(primary_id)
        if primary_id <= 0 and leg_ids:
            primary_id = leg_ids[0]
        out.append(OpenPosition(
            symbol, notional, symbol in INDEX_UNDERLYINGS,
            primary_con_id=(primary_id or None), leg_con_ids=tuple(leg_ids),
            contracts=int(qty),
            campaign_id=(f"working-order:{getattr(order, 'orderRef', '')}"
                         if leg_ids else "")))
    return out


def _same_underlying(position, symbol: str) -> bool:
    """Public API contract; production-derived narrative omitted."""
    raw = getattr(position, "underlying", None)
    if raw is None:
        return True
    try:
        return str(raw).strip().upper() == symbol
    except Exception:
        return True


def admission_dimensions(r, obs, order_ref: str, *, limits, construction_cfg, markers_clear,
                         day_orders, day_notional, max_orders_per_day, max_notional_per_day,
                         runtime_identity, campaign_conflict_symbols,
                         open_campaign_con_ids=()) -> dict:
    """Public API contract; production-derived narrative omitted."""
    symbol = str(getattr(r, "underlying", "") or "").upper()
    credit = is_credit(r)
    resolved_structure = (CSP_STRUCTURE if credit
                          else str(getattr(r, "structure", "") or ""))

    eff_limits = _credit_limits(limits) if credit else limits
    is_index = symbol in INDEX_UNDERLYINGS
    pot_value = effective_pot(getattr(obs.pot, "net_liq", 0.0), eff_limits.pot_cap_usd)
    cluster = sector_of(symbol, eff_limits.sector_map)
    positions = list(obs.positions)
    name_agg = sum(p.notional for p in positions
                   if not p.is_index and _same_underlying(p, symbol))
    sector_agg = 0.0
    if eff_limits.sector_map:
        sector_agg = sector_exposure(positions, symbol, 0.0, eff_limits.sector_map).get(cluster, 0.0)
    deployed_agg = sum(p.notional for p in positions if not getattr(p, "is_credit", False))
    legs = []
    for contract in (getattr(r, "contract", None), getattr(r, "short_contract", None)):
        con_id = getattr(contract, "conId", None)
        try:
            con_id = int(con_id)
        except (TypeError, ValueError):
            continue
        if con_id > 0:
            legs.append(con_id)



    final_contract = admission_contract_snapshot(r)
    capital = (float(final_contract["collateral_usd"]) if credit
               else float(final_contract["max_loss_usd"]))
    max_deployed = None
    if construction_cfg is not None and not credit:
        try:
            max_deployed = float(construction_cfg.max_deployed_pct) * float(obs.pot.net_liq)
        except (AttributeError, TypeError, ValueError):
            max_deployed = None
    reflected = []
    for p in positions:
        proof = getattr(p, "entry_reflection", None)
        if not proof or getattr(p, "is_credit", False):
            continue
        try:
            capital_in_book = float(p.notional)
            if (not math.isfinite(capital_in_book) or capital_in_book <= 0
                    or abs(capital_in_book - float(proof["capital_usd"])) > 0.005
                    or str(p.underlying).upper() != proof["symbol"]
                    or not (obs.observed_at_monotonic <= proof["journal_captured_at_monotonic"]
                            <= proof["position_observed_at_monotonic"] <= time.monotonic())):
                continue
            reflected.append(dict(
                order_ref=proof["order_ref"], con_id=proof["con_id"],
                leg_con_ids=proof["leg_con_ids"], contracts=proof["contracts"],
                capital_usd=proof["capital_usd"], symbol=proof["symbol"],
                sector_cluster=sector_of(proof["symbol"], eff_limits.sector_map).strip().upper(),
                name_counted_usd=(capital_in_book if not p.is_index
                                  and _same_underlying(p, symbol) else 0.0),
                sector_counted_usd=(sector_exposure([p], symbol, 0.0, eff_limits.sector_map)
                                    .get(cluster, 0.0) if eff_limits.sector_map else 0.0)))
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    open_campaigns = []
    derived_campaign_ids = set()
    for p in positions:
        try:
            primary = int(getattr(p, "primary_con_id", 0) or 0)
            leg_ids = tuple(dict.fromkeys(
                int(x) for x in (getattr(p, "leg_con_ids", ()) or ()) if int(x) > 0))
            if primary <= 0:
                continue
            if not leg_ids:
                leg_ids = (primary,)
            if primary not in leg_ids:
                raise ValueError("primary contract absent from campaign legs")
            contracts = int(getattr(p, "contracts", 0) or 0)
            risk_usd = float(getattr(p, "notional", 0.0))
            if contracts <= 0 or not math.isfinite(risk_usd) or risk_usd < 0:
                raise ValueError("open campaign quantity/risk is unreadable")
            campaign_id = str(getattr(p, "campaign_id", "") or f"contract:{primary}:live")
            open_campaigns.append({
                "campaign_id": campaign_id,
                "primary_con_id": primary,
                "leg_con_ids": list(leg_ids),
                "symbol": str(getattr(p, "underlying", "") or "").upper(),
                "contracts": contracts,
                "risk_usd": round(risk_usd, 2),
                "risk_known": risk_usd > 0,
            })
            derived_campaign_ids.update(leg_ids)
        except (TypeError, ValueError, OverflowError):



            try:
                if int(getattr(p, "primary_con_id", 0) or 0) > 0:
                    derived_campaign_ids.add(int(getattr(p, "primary_con_id")))
            except (TypeError, ValueError):
                pass
    structured_intent = structured_entry_intent(r, final_contract=final_contract)
    return dict(
        order_ref=order_ref,
        envelope_id=str(getattr(r, "decision_id", "") or ""),
        **identity_fields(runtime_identity),
        con_id=getattr(getattr(r, "contract", None), "conId", None),
        leg_con_ids=tuple(legs),
        symbol=symbol,
        sector_cluster=cluster,
        structure=resolved_structure,
        side="credit" if credit else "debit",
        capital_usd=capital,
        collateral_usd=capital,
        contracts=int(getattr(r, "qty", 0) or 0),
        net_liq=getattr(obs.pot, "net_liq", None),
        available_funds=getattr(obs.pot, "available_funds", None),
        broker_deployed_usd=obs.csp.total,
        observation_readable=True,
        observed_at_monotonic=obs.observed_at_monotonic,
        max_observation_age_s=DEFAULT_MAX_OBSERVATION_AGE_S,
        open_position_count=len(positions),
        max_concurrent=int(eff_limits.max_concurrent),
        name_aggregate_usd=round(float(name_agg), 2),
        name_cap_usd=(None if is_index
                      else round(eff_limits.max_single_name_agg_pct * pot_value, 2)),
        sector_aggregate_usd=round(float(sector_agg), 2),
        sector_cap_usd=(None if (is_index or not eff_limits.sector_map
                                 or eff_limits.max_sector_agg_pct <= 0)
                        else round(eff_limits.max_sector_agg_pct * pot_value, 2)),
        deployed_aggregate_usd=round(float(deployed_agg), 2),
        deployed_cap_usd=max_deployed,
        day_orders=int(day_orders or 0),
        max_orders_per_day=max_orders_per_day,
        day_notional=float(day_notional or 0.0),
        max_notional_per_day=max_notional_per_day,
        open_campaign_con_ids=tuple(sorted(
            derived_campaign_ids | {int(x) for x in open_campaign_con_ids})),
        open_campaigns=tuple(open_campaigns),
        campaign_conflict_symbols=tuple(sorted(
            str(value).strip().upper() for value in campaign_conflict_symbols
            if str(value).strip())),
        campaign_add_intent=getattr(r, "campaign_add_intent", None),
        final_contract=final_contract,
        structured_intent=structured_intent,
        markers_clear=bool(markers_clear),
        visible_order_refs=tuple(set(obs.csp.visible_order_refs)
                                 | set(obs.view.entry_order_refs)),
        reflected_debit_positions=tuple(reflected),
    )


async def broker_deployed_csp_collateral(ib, audit_path: Optional[str] = None) -> Optional[float]:
    snapshot = await broker_csp_collateral_snapshot(ib, audit_path)
    return None if snapshot is None else snapshot.total


async def reserve_credit_entry(ib, r, order_ref: str, *, ledger: EntryReservationLedger,
                               dimensions=None, throttle_recorded: bool = False,
                               intent=None,
                               audit_path: Optional[str] = None):
    """Public API contract; production-derived narrative omitted."""
    def finish(decision, snap=None, broker=None):
        if audit_path:
            audit(audit_path, "credit_entry_reservation", **reservation_as_dict(decision))
        return decision, snap, broker

    required = required_collateral(getattr(r, "strike", None), getattr(r, "qty", None))
    committed = capital_committed(r)
    if required is None or abs(float(required) - float(committed)) > 0.01:
        return finish(ReservationDecision(
            False, False, "collateral_mismatch",
            (f"recomputed collateral {required!r} does not match order ${committed:,.2f}",),
            order_ref=order_ref))
    try:
        snap = await get_pot_snapshot(ib)
    except Exception as exc:
        return finish(ReservationDecision(
            False, False, "account_snapshot_error", (str(exc),), order_ref=order_ref))
    account_gate = entry_safety.account_snapshot_valid(snap)
    if not account_gate.allowed:
        return finish(ReservationDecision(
            False, False, "account_snapshot_invalid", tuple(account_gate.reasons),
            order_ref=order_ref), snap)
    broker = await broker_csp_collateral_snapshot(ib, audit_path)
    if broker is None:
        return finish(ReservationDecision(
            False, False, "broker_collateral_unverifiable",
            ("deployed CSP collateral or broker visibility could not be verified",),
            order_ref=order_ref), snap)
    if dimensions is None:




        return finish(ReservationDecision(
            False, False, "admission_dimensions_missing",
            ("credit admission was called without its categorical dimensions",),
            order_ref=order_ref), snap, broker)
    dims = dict(dimensions)
    dims.update(
        order_ref=order_ref,
        net_liq=getattr(snap, "net_liq", None),
        available_funds=getattr(snap, "available_funds", None),
        broker_deployed_usd=broker.total,
        visible_order_refs=tuple(set(dims.get("visible_order_refs") or ())
                                 | set(broker.visible_order_refs)),
    )
    decision = await asyncio.to_thread(
        lambda: ledger.admit(throttle_recorded=throttle_recorded, intent=intent, **dims))
    return finish(decision, snap, broker)


def credit_executable_price(r) -> float:
    """Public API contract; production-derived narrative omitted."""
    return round(float(getattr(r, "entry_bid")), 2)


def credit_material_changes(original, refreshed, *,
                            max_price_change_pct: float = entry_safety.DEFAULT_MATERIAL_PRICE_PCT):
    """Public API contract; production-derived narrative omitted."""
    changes = []
    try:
        if entry_safety.contract_fingerprint(original) != entry_safety.contract_fingerprint(refreshed):
            changes.append("contract/structure changed")
    except Exception as exc:
        changes.append(f"contract identity could not be compared: {exc}")
    try:
        if int(getattr(original, "qty")) != int(getattr(refreshed, "qty")):
            changes.append(f"quantity changed {getattr(original, 'qty')} -> {getattr(refreshed, 'qty')}")
    except Exception as exc:
        changes.append(f"quantity could not be compared: {exc}")
    try:
        o = _fnum(getattr(original, "collateral_usd", 0.0), None)
        n = _fnum(getattr(refreshed, "collateral_usd", 0.0), None)
        if not o or not n or o <= 0 or n <= 0:
            raise ValueError("non-positive collateral")
        if abs(n - o) > 0.01:
            changes.append(f"reserved collateral changed ${o:,.2f} -> ${n:,.2f}")
    except Exception as exc:
        changes.append(f"collateral could not be compared: {exc}")
    try:
        old = credit_executable_price(original)
        new = credit_executable_price(refreshed)
        if old <= 0 or new <= 0:
            raise ValueError("non-positive executable credit")
        move = abs(new - old) / old
        if move > max(0.0, float(max_price_change_pct)) + 1e-12:
            changes.append(f"executable credit changed {move:.1%} (${old:.2f} -> ${new:.2f})")
    except Exception as exc:
        changes.append(f"executable credit could not be compared: {exc}")
    return tuple(changes)


def capital_at_risk(r) -> float:
    """Public API contract; production-derived narrative omitted."""
    if is_credit(r):
        ml = _fnum(getattr(r, "credit_max_loss_usd", 0.0), 0.0) or 0.0
        if ml > 0:
            return round(ml, 2)
        coll = _fnum(getattr(r, "collateral_usd", 0.0), 0.0) or 0.0
        return round(max(0.0, coll - (_fnum(getattr(r, "net_credit_usd", 0.0), 0.0) or 0.0)), 2)
    return round(float(r.limit) * 100 * int(r.qty), 2)


def capital_committed(r) -> float:
    """Public API contract; production-derived narrative omitted."""
    if is_credit(r):
        return round(_fnum(getattr(r, "collateral_usd", 0.0), 0.0) or 0.0, 2)
    return round(float(r.limit) * 100 * int(r.qty), 2)


@dataclass
class Plan:
    idea: TradeIdea
    trade: ProposedTrade
    gate: GateDecision
    action: str


def _credit_limits(limits):
    """Public API contract; production-derived narrative omitted."""
    pct = CREDIT_MAX_COLLATERAL_PCT
    try:
        return dataclasses.replace(
            limits,
            max_trade_pct=max(limits.max_trade_pct, pct),
            max_trade_pct_hard=max(limits.max_trade_pct_hard, pct),
            max_single_name_agg_pct=max(limits.max_single_name_agg_pct, pct),
            max_sector_agg_pct=max(limits.max_sector_agg_pct, pct),
        )
    except Exception:


        return limits


def plan_idea(idea: TradeIdea, *, net_liq: float, available_funds: float,
              positions: List[OpenPosition], baseline: float,
              approved_names: Set[str], limits: RiskLimits, regime=None) -> Plan:






    if is_credit(idea):



        authority = _short_option_authority_from_env(idea)
        if not authority.allowed:
            trade = ProposedTrade(idea.underlying, float("inf"), idea.is_index, idea.conviction,
                                  is_long=False)
            return Plan(idea, trade, GateDecision(False, authority.reasons),
                        "gate_rejected")
        notional = _fnum(getattr(idea, "collateral_usd", 0.0), None)
        if notional is None or notional <= 0:

            notional = float("inf")


        trade = ProposedTrade(idea.underlying, notional, idea.is_index, idea.conviction,
                              is_long=False,
                              profit_target_pct=getattr(idea, "profit_target_pct", 0.0) or 0.0,
                              stop_pct=getattr(idea, "stop_pct", 0.0) or 0.0)
        gate = evaluate_trade(
            trade, net_liq=net_liq, available_funds=available_funds,
            open_positions=positions, pot_day_start=baseline,
            approved_names=approved_names, limits=_credit_limits(limits),
            regime_info=regime,
        )
        return Plan(idea, trade, gate, "needs_approval" if gate.approved else "gate_rejected")
    trade = ProposedTrade(idea.underlying, idea.est_debit_usd, idea.is_index, idea.conviction,
                          is_long=(getattr(idea, "direction", "bullish") == "bullish"),
                          profit_target_pct=getattr(idea, "profit_target_pct", 0.0) or 0.0,
                          stop_pct=getattr(idea, "stop_pct", 0.0) or 0.0)
    gate = evaluate_trade(
        trade, net_liq=net_liq, available_funds=available_funds,
        open_positions=positions, pot_day_start=baseline,
        approved_names=approved_names, limits=limits, regime_info=regime,
    )
    return Plan(idea, trade, gate, "needs_approval" if gate.approved else "gate_rejected")


@dataclass
class ResolvedOrder:
    """Public API contract; production-derived narrative omitted."""
    underlying: str
    right: str
    expiry: str
    strike: float
    qty: int
    limit: float
    contract: object = None
    short_strike: float = 0.0
    short_contract: object = None
    conviction: float = -1.0
    thesis: str = ""

    tp_pct: float = 0.0
    sl_pct: float = 0.0
    spot: float = 0.0
    entry_delta: float = 0.0
    entry_iv: float = 0.0
    dte: int = 0
    dte_adjusted: bool = False



    entry_gamma: float = 0.0
    entry_theta: float = 0.0
    entry_vega: float = 0.0
    entry_ivr: float = 0.0
    entry_bid: float = 0.0
    entry_ask: float = 0.0
    entry_spread_pct: float = 0.0
    net_delta: float = 0.0
    net_theta: float = 0.0
    net_gamma: float = 0.0
    net_vega: float = 0.0
    quote_observed_at: float = 0.0
    decision_id: str = ""
    decision_revision: int = 0
    model_identity: Optional[dict] = None






    model_identity_source: Optional[str] = None
    intended_hold_days: Optional[int] = None



    side: str = "debit"
    collateral_usd: float = 0.0
    net_credit_usd: float = 0.0
    credit_max_loss_usd: float = 0.0






    structure: str = ""


    earnings_date: Optional[str] = None
    earnings_warn: str = ""
    earnings_unchecked: bool = False
    earnings_day_warning: bool = False



    earnings_reconsidered: bool = False
    earnings_reconsideration_decision: Optional[str] = None
    earnings_reconsideration_phase: Optional[str] = None
    earnings_reconsideration_reason: str = ""
    earnings_reconsideration_model_identity: Optional[dict] = None
    earnings_reconsideration_model_identity_source: Optional[str] = None
    earnings_reconsideration_order_snapshot: Optional[dict] = None
    earnings_reconsideration_order_sha256: Optional[str] = None
    execution_authority: Optional[str] = None


    stage_a_intent_payload: Optional[dict] = None
    stage_b_candidate_payload: Optional[dict] = None


    campaign_add_intent: Optional[dict] = None


def order_summary(r: ResolvedOrder) -> str:
    if is_credit(r):

        return (f"SELL {r.qty}x {r.underlying} {r.expiry} {r.strike:g}P cash-secured put "
                f"@ ~${r.limit:.2f} credit (marketable limit at the fresh NBBO bid)  "
                f"(collateral ${capital_committed(r):,.0f}, credit "
                f"${_fnum(r.net_credit_usd, 0.0) or 0.0:,.0f}, max loss "
                f"${capital_at_risk(r):,.0f} if it goes to zero)")
    if r.short_contract is not None:
        width = abs(r.short_strike - r.strike)
        return (f"BUY {r.qty}x {r.underlying} {r.expiry} {r.strike:g}/{r.short_strike:g}{r.right} "
                f"debit spread @ ~${r.limit:.2f} (marketable limit after fresh NBBO)  "
                f"(max loss ~${r.limit * 100 * r.qty:,.0f}, max value ${width * 100 * r.qty:,.0f})")
    return (f"BUY {r.qty}x {r.underlying} {r.expiry} {r.strike:g}{r.right} "
            f"@ ~${r.limit:.2f} (marketable limit after fresh NBBO)  (~${r.limit * 100 * r.qty:,.0f})")


def contract_snapshot(r: ResolvedOrder) -> dict:
    """Public API contract; production-derived narrative omitted."""
    snap = {
        "underlying": getattr(r, "underlying", ""), "right": getattr(r, "right", ""),
        "expiry": getattr(r, "expiry", ""),
        "long_con_id": getattr(getattr(r, "contract", None), "conId", None),
        "long_strike": getattr(r, "strike", 0.0),
        "short_con_id": getattr(getattr(r, "short_contract", None), "conId", None),
        "short_strike": (getattr(r, "short_strike", 0.0) or None),
        "quantity": getattr(r, "qty", 0),
        "limit": getattr(r, "limit", 0.0), "max_loss_usd": capital_at_risk(r),
        "profit_target_pct": getattr(r, "tp_pct", None),
        "stop_pct": getattr(r, "sl_pct", None),
        "execution_authority": getattr(r, "execution_authority", None),
        "quote_observed_at": getattr(r, "quote_observed_at", 0.0),
        "earnings_date": getattr(r, "earnings_date", None),
        "earnings_day_warning": bool(getattr(r, "earnings_day_warning", False)),
        "earnings_overlap_warning": (getattr(r, "earnings_warn", "") or None),
        "earnings_unchecked": bool(getattr(r, "earnings_unchecked", False)),
        "earnings_reconsidered": bool(getattr(r, "earnings_reconsidered", False)),
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
    }
    if is_credit(r):
        snap.update(side=CREDIT_SIDE, action="SELL", structure=CSP_STRUCTURE,
                    collateral_usd=capital_committed(r),
                    net_credit_usd=round(_fnum(r.net_credit_usd, 0.0) or 0.0, 2))
    return snap


def admission_contract_snapshot(r: ResolvedOrder) -> dict:
    """Public API contract; production-derived narrative omitted."""
    snap = contract_snapshot(r)
    qty = abs(int(getattr(r, "qty", 0) or 0))
    executable_limit = (credit_executable_price(r) if is_credit(r)
                        else entry_safety.executable_price(r))
    snap["limit"] = executable_limit
    if is_credit(r):
        collateral = capital_committed(r)
        net_credit = round(executable_limit * 100.0 * qty, 2)
        snap.update(collateral_usd=collateral, net_credit_usd=net_credit,
                    max_loss_usd=round(collateral - net_credit, 2))
    else:
        snap["max_loss_usd"] = round(executable_limit * 100.0 * qty, 2)
    snap.setdefault("side", CREDIT_SIDE if is_credit(r) else "debit")
    snap.setdefault("structure", (CSP_STRUCTURE if is_credit(r)
                                  else str(getattr(r, "structure", "") or "")))
    return snap


def structured_entry_intent(r: ResolvedOrder, *, final_contract=None) -> dict:
    """Public API contract; production-derived narrative omitted."""
    stage_a = getattr(r, "stage_a_intent_payload", None)
    stage_b = getattr(r, "stage_b_candidate_payload", None)
    direction = (stage_a.get("direction") if isinstance(stage_a, dict)
                 else ("bullish" if str(getattr(r, "right", "")).upper() == "C"
                       else "bearish"))
    return {
        "schema": "structured_entry_intent.v1",
        "underlying": str(getattr(r, "underlying", "") or "").upper(),
        "side": (CREDIT_SIDE if is_credit(r) else "debit"),
        "direction": direction,
        "structure": (CSP_STRUCTURE if is_credit(r)
                      else str(getattr(r, "structure", "") or "")),
        "intended_hold_days": getattr(r, "intended_hold_days", None),
        "stage_a_intent_id": getattr(r, "stage_a_intent_id", None),
        "stage_b_candidate_id": getattr(r, "stage_b_candidate_id", None),
        "stage_a_intent": stage_a,
        "stage_b_candidate": stage_b,
        "campaign_add_intent": getattr(r, "campaign_add_intent", None),
        "final_contract": (final_contract if final_contract is not None
                           else admission_contract_snapshot(r)),
    }


def earnings_reconsideration_order_snapshot(r: ResolvedOrder) -> dict:
    """Public API contract; production-derived narrative omitted."""
    return {
        "underlying": r.underlying,
        "right": r.right,
        "expiry": r.expiry,
        "long_con_id": getattr(r.contract, "conId", None),
        "long_strike": r.strike,
        "short_con_id": getattr(r.short_contract, "conId", None),
        "short_strike": (r.short_strike or None),
        "quantity": r.qty,
        "executable_limit": (credit_executable_price(r) if is_credit(r)
                             else entry_safety.executable_price(r)),
        "side": (CREDIT_SIDE if is_credit(r) else "debit"),
        "structure": getattr(r, "structure", "") or None,
        "tp_pct": getattr(r, "tp_pct", 0.0),
        "sl_pct": getattr(r, "sl_pct", 0.0),
        "earnings_date": getattr(r, "earnings_date", None),
    }


def earnings_reconsideration_snapshot_sha256(snapshot: dict) -> str:
    """Public API contract; production-derived narrative omitted."""
    return hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":"),
                   allow_nan=False).encode()).hexdigest()


def earnings_reconsideration_receipt_valid(r: ResolvedOrder) -> bool:
    """Public API contract; production-derived narrative omitted."""
    if not bool(getattr(r, "earnings_reconsidered", False)):
        return False
    if getattr(r, "earnings_reconsideration_decision", None) != "proceed":
        return False
    old = getattr(r, "earnings_reconsideration_order_snapshot", None)
    if not isinstance(old, dict):
        return False
    try:
        if getattr(r, "earnings_reconsideration_order_sha256", None) != (
                earnings_reconsideration_snapshot_sha256(old)):
            return False
    except (TypeError, ValueError):
        return False
    current = earnings_reconsideration_order_snapshot(r)
    for key in current:
        if key == "executable_limit":
            continue
        if old.get(key) != current.get(key):
            return False
    try:
        old_limit = float(old["executable_limit"])
        new_limit = float(current["executable_limit"])
        if not all(math.isfinite(v) and v > 0 for v in (old_limit, new_limit)):
            return False
        return (abs(new_limit - old_limit) / old_limit
                <= entry_safety.DEFAULT_MATERIAL_PRICE_PCT + 1e-12)
    except (KeyError, TypeError, ValueError):
        return False


async def reconsider_new_same_day_earnings(*, endpoint: str, model: str,
                                           market_context: str, idea: TradeIdea,
                                           previous: ResolvedOrder, fresh: ResolvedOrder,
                                           slack_token: str, slack_channel: str,
                                           audit_path: str, decision_id: str):
    """Public API contract; production-derived narrative omitted."""
    is_today = bool(getattr(fresh, "earnings_day_warning", False))
    newly_today = is_today and not bool(
        getattr(previous, "earnings_day_warning", False))
    stale_receipt = (is_today
                     and bool(getattr(fresh, "earnings_reconsidered", False))
                     and not earnings_reconsideration_receipt_valid(fresh))
    if not (newly_today or stale_receipt):
        return True, False
    trade_context = {
        "decision_id": decision_id,
        "idea": {
            "underlying": getattr(idea, "underlying", fresh.underlying),
            "direction": getattr(idea, "direction", None),
            "structure": getattr(idea, "structure", None),
            "side": getattr(idea, "side", None),
            "conviction": getattr(idea, "conviction", None),
            "intended_hold_days": getattr(idea, "intended_hold_days", None),
            "thesis": getattr(idea, "thesis", ""),
        },
        "exact_order": contract_snapshot(fresh),
        "transmit_order": earnings_reconsideration_order_snapshot(fresh),
        "earnings_date": getattr(fresh, "earnings_date", None),
        "earnings_warning": getattr(fresh, "earnings_warn", ""),
    }
    audit(audit_path, "earnings_reconsideration_started", phase="pre_submit",
          underlying=fresh.underlying, decision_id=decision_id,
          order=order_summary(fresh), earnings_date=fresh.earnings_date)
    try:
        review, _raw, _cot, identity = await asyncio.to_thread(
            reconsider_same_day_earnings, endpoint, model, market_context, trade_context,
            **SAME_DAY_EARNINGS_REVIEW_REQUEST)
    except Exception as exc:
        audit(audit_path, "earnings_reconsideration_blocked", phase="pre_submit",
              underlying=fresh.underlying, decision_id=decision_id,
              error_type=type(exc).__name__, error=str(exc)[:1000])
        approval.post_proposal(
            slack_token, slack_channel,
            f":no_entry: *{fresh.underlying}* was NOT placed — the final refresh newly found "
            "earnings today, and the required model reconsideration failed closed. Check "
            "audit/gateway logs for diagnostics; no approval is being awaited.",
            seed_reactions=False)
        return False, True

    fresh.earnings_reconsidered = True
    fresh.earnings_reconsideration_decision = review.decision
    fresh.earnings_reconsideration_phase = review.event_phase
    fresh.earnings_reconsideration_reason = review.reason
    fresh.earnings_reconsideration_model_identity = identity
    fresh.earnings_reconsideration_model_identity_source = (
        "meta" if isinstance(identity, dict) and identity.get("verified") is True
        else "unknown")
    fresh.earnings_reconsideration_order_snapshot = (
        earnings_reconsideration_order_snapshot(fresh))
    fresh.earnings_reconsideration_order_sha256 = (
        earnings_reconsideration_snapshot_sha256(
            fresh.earnings_reconsideration_order_snapshot))
    audit(audit_path, "earnings_reconsideration_result", phase="pre_submit",
          underlying=fresh.underlying, decision_id=decision_id,
          decision=review.decision, event_phase=review.event_phase,
          reason=review.reason,
          model_identity=identity,
          model_identity_source=fresh.earnings_reconsideration_model_identity_source,
          order_snapshot_sha256=fresh.earnings_reconsideration_order_sha256)
    if not review.proceed:
        approval.post_proposal(
            slack_token, slack_channel,
            f":no_entry: *{fresh.underlying}* was NOT placed — earnings today was newly found "
            f"at the final refresh and the model declined the exact order.\n"
            f"Event phase: *{review.event_phase}*\nReason: {review.reason}\n"
            f"_Decision ID: `{decision_id}`; no approval is being awaited._",
            seed_reactions=False)
        return False, True

    approval.post_proposal(
        slack_token, slack_channel,
        ":rotating_light: *EARNINGS TODAY — MODEL RECONSIDERATION PASSED*\n"
        f"*{fresh.underlying}* exact order retained; event phase: *{review.event_phase}*.\n"
        f"Reason: {review.reason}\n"
        "A new executable NBBO refresh is required before transmit. No approval is being "
        f"awaited for this earnings review.\n_Decision ID: `{decision_id}`_",
        seed_reactions=False)
    return True, True


def pick_spread_short(candidates, long_strike: float, long_mid: float, right: str,
                      per_trade_cap: float, *, spot=None, dte=None, atm_iv=None, cons=None):
    """Public API contract; production-derived narrative omitted."""
    otm = [(k, m) for k, m in candidates
           if m is not None and m == m and m > 0
           and ((right == "C" and k > long_strike) or (right == "P" and k < long_strike))]
    otm.sort(key=lambda km: abs(km[0] - long_strike), reverse=True)
    for k, m in otm:
        if cons is not None:
            ok, _why = construction.spread_structure_ok(long_strike, k, spot, right, dte, atm_iv, cons)
            if not ok:
                continue
        net = long_mid - m
        if net <= 0.01:
            continue
        if net * 100 <= per_trade_cap + 1e-6:
            return k, round(net, 2)
    return None


def size_within_cap(unit_cost: float, budget: float, per_trade_cap: float) -> Optional[int]:
    """Public API contract; production-derived narrative omitted."""
    if unit_cost <= 0:
        return None
    qty = int(min(budget, per_trade_cap) // unit_cost)
    if qty < 1:
        return None
    if unit_cost * qty > per_trade_cap + 1e-6:
        qty -= 1
    return qty if qty >= 1 else None




class Trader:
    def __init__(self, *, ib_conn, exit_manager, limits: RiskLimits, approved_names: Set[str],
                 endpoint: str, model: str, slack_token: str, slack_channel: str,
                 approver_ids: Set[str], baseline_path: str, audit_path: str,
                 approve_timeout_s: int = 1800, journal_path: str = "./trades.log",
                 auto_approve_within_gates: bool = False,
                 blocked_sector_keywords: Optional[List[str]] = None,
                 entry_limit_buffer_pct: float = 0.05,
                 construction_cfg: Optional[ConstructionConfig] = None,
                 caps_tp_tiers: Optional[List[dict]] = None,
                 kill_switch_path: Optional[str] = None,
                 config_path: str = "config.yaml",
                 resolved_config=None,
                 runtime_identity: Optional[RuntimeIdentity] = None,
                 trading_down_path: Optional[str] = None,
                 broker_order_lock=None,
                 entry_reservation_ledger: Optional[EntryReservationLedger] = None,
                 entry_ledger_required: bool = True,
                 max_orders_per_cycle: Optional[int] = None,
                 max_orders_per_day: Optional[int] = None,
                 max_notional_per_day: Optional[float] = None,
                 reload_enabled: bool = False,
                 reload_conviction_min: float = 6,
                 reload_friction_k: float = 1.5,
                 reload_expected_continuation_pct: float = 3.0,
                 reload_max_per_name_per_day: int = 2,
                 reload_ttl_cycles: int = 3):
        self.ib_conn = ib_conn




        self.kill_switch_path = kill_switch_path or "./KILL_SWITCH"
        self.config_path = config_path


        self.resolved_config = resolved_config or load_config(config_path)
        expected_identity = freeze_runtime_identity(self.resolved_config)
        if runtime_identity is not None and runtime_identity != expected_identity:
            raise RuntimeIdentityError(
                "supplied runtime identity does not match this checkout and resolved config")
        self.runtime_identity = (runtime_identity
                                 if runtime_identity is not None else expected_identity)
        identity_fields(self.runtime_identity)
        self.trading_down_path = trading_down_path
        self.broker_order_lock = broker_order_lock
        if entry_reservation_ledger is not None and entry_ledger_required is not True:
            raise ValueError("protective-only Trader may not receive an entry reservation ledger")
        self.entry_reservation_ledger = (
            entry_reservation_ledger or EntryReservationLedger()) if entry_ledger_required else None



        self._exit_fail_streak = 0
        self.exit_manager = exit_manager
        self.limits = limits
        self.approved_names = {n.upper() for n in approved_names}
        self.endpoint, self.model = endpoint, model
        self.slack_token, self.slack_channel = slack_token, slack_channel
        self.approver_ids = approver_ids
        self.baseline_path, self.audit_path = baseline_path, audit_path
        self.approve_timeout_s = approve_timeout_s
        self.auto_approve_within_gates = bool(auto_approve_within_gates)



        self._research_degraded = None
        self.journal_path = journal_path
        self.blocked_sector_keywords = [k for k in (blocked_sector_keywords or []) if k.strip()]
        self.entry_limit_buffer_pct = float(entry_limit_buffer_pct)


        self.construction = construction_cfg or ConstructionConfig()


        self.caps_tp_tiers = list(caps_tp_tiers or [])




        self.max_orders_per_cycle = max_orders_per_cycle
        self.max_orders_per_day = max_orders_per_day
        self.max_notional_per_day = max_notional_per_day







        self.reload_enabled = bool(reload_enabled)
        self.reload_conviction_min = float(reload_conviction_min)
        self.reload_friction_k = float(reload_friction_k)

        self.reload_expected_continuation_pct = float(reload_expected_continuation_pct)
        self.reload_max_per_name_per_day = int(reload_max_per_name_per_day)
        self.reload_ttl_cycles = int(reload_ttl_cycles)


        self._regime = None
        self._price_stats = {}






        self._cycle_credit_collateral = 0.0


    _EXIT_FAIL_SUPPRESS_ENTRIES = 3

    def _kill_switch_active(self) -> bool:
        """Public API contract; production-derived narrative omitted."""
        try:
            return bool(self.kill_switch_path) and Path(self.kill_switch_path).exists()
        except Exception:
            return False

    def _entry_markers_clear(self) -> entry_safety.SafetyResult:
        return entry_safety.entry_markers_clear(
            config_path=self.config_path,
            kill_switch_path=self.kill_switch_path,
            trading_down_path=self.trading_down_path)

    async def _positions_for_admission(self, open_trades):
        """Public API contract; production-derived narrative omitted."""
        return await self._open_positions(open_trades=list(open_trades), strict=True)

    def _entry_throttle_store(self):
        """Public API contract; production-derived narrative omitted."""
        try:
            sm = getattr(self.exit_manager, "state_manager", None)
            if sm is None:
                return None
            return EntryThrottleStore.for_state_path(sm.state_path)
        except Exception:
            return None

    def _day_open_counts_now(self):
        """Public API contract; production-derived narrative omitted."""
        sm = getattr(self.exit_manager, "state_manager", None)
        return entry_day_open_counts(sm, self._entry_throttle_store(), _trading_day())

    def _admission_dimensions(self, r, obs, order_ref):
        return admission_dimensions(
            r, obs, order_ref, limits=self.limits, construction_cfg=self.construction,
            markers_clear=self._entry_markers_clear().allowed,
            day_orders=self._day_open_counts_now()[0],
            day_notional=self._day_open_counts_now()[1],
            max_orders_per_day=self.max_orders_per_day,
            max_notional_per_day=self.max_notional_per_day,
            runtime_identity=self.runtime_identity,
            campaign_conflict_symbols=self.exit_manager.campaign_conflict_symbols())

    async def reconcile_open_entry_intents(self, view=None):
        """Public API contract; production-derived narrative omitted."""
        try:
            return await reconcile_entry_intents(
                self.ib_conn.ib, ledger=self.entry_reservation_ledger,
                journal_append=self._append_reconciled_journal, audit_path=self.audit_path,
                view=view, already_journalled=self._journal_has_order_ref)
        except Exception as exc:
            audit(self.audit_path, "entry_intent_reconcile_error", error=str(exc)[:200])
            return IntentReconciliation(False, reasons=(str(exc),))

    def _journal_has_order_ref(self, order_ref):
        """Public API contract; production-derived narrative omitted."""
        ref = str(order_ref or "")
        if not ref:
            return False
        try:
            with open(self.journal_path) as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        return None
                    if not isinstance(row, dict):
                        return None
                    if row.get("event"):
                        continue
                    if str(row.get("order_ref") or "") == ref:
                        return True
        except FileNotFoundError:
            return False
        except Exception:
            return None
        return False

    def _entry_intent_or_none(self, r, order_ref):
        """Public API contract; production-derived narrative omitted."""
        try:
            return self.entry_intent_payload(r, order_ref)
        except Exception as exc:
            audit(self.audit_path, "entry_intent_construction_refused",
                  order_ref=str(order_ref), error=str(exc)[:200])
            raise

    def _admission_recheck(self, r, submission_authority):
        """Public API contract; production-derived narrative omitted."""
        def _check():
            submission = entry_safety.submission_authority_valid(submission_authority)
            if not submission.allowed:
                return False, tuple(submission.reasons)
            authority = self._short_option_entry_authority(r)
            if not authority.allowed:
                return False, tuple(authority.reasons)
            markers = self._entry_markers_clear()
            if not markers.allowed:
                return False, tuple(markers.reasons)
            quote = entry_safety.nbbo_valid(r)
            if not quote.allowed:
                return False, tuple(quote.reasons)
            try:
                live_conflicts = set(self.exit_manager.campaign_conflict_symbols())
                durable_conflicts = set(active_campaign_conflict_symbols(self.journal_path))
            except (CampaignConflictRegistryError, OSError, ValueError) as exc:
                return False, (f"campaign-conflict quarantine unreadable: {exc}",)
            if live_conflicts != durable_conflicts:
                return False, ("campaign-conflict quarantine changed before broker submit",)
            if str(r.underlying).strip().upper() in durable_conflicts:
                return False, (f"{r.underlying} has a quarantined campaign conflict",)
            return True, ()
        return _check

    def _short_option_entry_authority(self, obj):
        """Public API contract; production-derived narrative omitted."""
        return entry_safety.short_option_entry_authority(
            credit=is_credit(obj),
            credit_entries_enabled=getattr(
                self.resolved_config, "credit_entries_enabled", False),
            assigned_stock_authority_enabled=getattr(
                self.resolved_config, "assigned_stock_authority_enabled", False),
        )

    def _load_baselines(self) -> Dict[str, float]:
        """Public API contract; production-derived narrative omitted."""
        p = Path(self.baseline_path)
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError as exc:
            audit(self.audit_path, "baseline_corrupt_fail_closed",
                  path=str(p), error=str(exc)[:200])
            print("[FATAL] daily-loss baseline at %s is unreadable (%s). Refusing to re-seed "
                  "from the current NetLiq -- that would disarm the circuit breaker. Entries "
                  "halt until it is repaired or removed." % (p, str(exc)[:120]))
            raise

    def _save_baselines(self, b: Dict[str, float]) -> None:


        path = Path(self.baseline_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(b))
        tmp.replace(path)

    def _load_journal_debits(self) -> Dict[int, float]:
        """Public API contract; production-derived narrative omitted."""
        debits: Dict[int, float] = {}
        p = Path(self.journal_path)
        if not p.exists():
            return debits
        rows = []
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        try:
            from exitmgr.manager import build_journal_campaigns, open_campaign
            campaigns = build_journal_campaigns(rows)
            for con_id in campaigns:
                campaign = open_campaign(campaigns, con_id)
                if campaign is None:
                    continue
                total = campaign.total("debit")
                if total is None:
                    continue
                debits[int(con_id)] = float(total)
            return debits
        except Exception as exc:



            audit(self.audit_path, "journal_campaign_read_degraded", error=str(exc)[:200])
        for rec in rows:
            cid = rec.get("contract_id")
            d = rec.get("debit")
            if cid is None or d is None:
                continue
            try:
                debits[int(cid)] = float(d)
            except (TypeError, ValueError):
                continue
        return debits

    async def _open_positions(self, *, open_trades=_UNREAD,
                              strict: bool = False) -> List[OpenPosition]:
        """Public API contract; production-derived narrative omitted."""
        journal_basis = capture_journal_basis(self.journal_path)
        position_read_started = time.monotonic()
        try:
            all_raw = await self.ib_conn.get_positions(include_short=True)
        except Exception as exc:
            audit(self.audit_path, "risk_position_read_error", error=str(exc),
                  note="the unified long/short book is UNKNOWN; refusing entry evaluation")
            raise
        raw = {cid: pd for cid, pd in all_raw.items() if pd.quantity > 0}
        journal_debits = journal_basis.debits



        out = (build_admission_risk_book(
                   all_raw, journal_basis, observed_at_monotonic=position_read_started)
               if strict else build_debit_risk_book(
                   all_raw, journal_basis, observed_at_monotonic=position_read_started))
        for cid, pd in raw.items():
            if cid not in journal_debits:
                audit(self.audit_path, "exposure_no_journal_debit",
                      symbol=pd.symbol.upper(), con_id=cid,
                      fallback_gross=round(abs(pd.avg_cost) * 100 * abs(pd.quantity), 2),
                      note="no journaled net debit; using gross long-leg value (conservative)")








        try:
            existing_cids = set(raw.keys())
            if open_trades is _UNREAD:
                open_trades = await self.ib_conn.ib.reqAllOpenOrdersAsync()
            folded = resting_buy_positions(
                open_trades, journal_debits, existing_con_ids=existing_cids)
            out.extend(folded)
            for position in folded:
                audit(self.audit_path, "resting_entry_folded",
                      symbol=position.underlying,
                      con_id=position.primary_con_id,
                      notional=round(float(position.notional), 2))
        except Exception as _oe:
            audit(self.audit_path, "open_buy_fold_error", error=str(_oe))
            if strict:


                raise



        if strict:
            return out


























        try:
            short_rows = all_raw
            spread_legs, collateral_by_cid = self._journal_short_context()
            for cid, pd in (short_rows or {}).items():
                try:
                    q = int(getattr(pd, "quantity", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if q >= 0:
                    continue
                if str(getattr(pd, "sec_type", "OPT") or "OPT").upper() not in ("", "OPT", "FOP"):
                    continue
                if str(getattr(pd, "right", "") or "").upper()[:1] != "P":
                    continue
                if int(cid) in spread_legs:
                    continue
                sym = (getattr(pd, "symbol", "") or "").upper()
                if not sym:
                    continue
                notional = collateral_by_cid.get(int(cid))
                if notional is None:
                    try:
                        strike = float(getattr(pd, "strike", 0.0) or 0.0)
                    except (TypeError, ValueError):
                        strike = 0.0
                    notional = round(strike * 100 * abs(q), 2) if strike > 0 else None
                if notional is None:
                    notional = 0.0
                    audit(self.audit_path, "short_collateral_unreadable", symbol=sym,
                          con_id=int(cid), quantity=q,
                          note="counted toward max_concurrent at $0 notional; strike and "
                               "journaled collateral_usd both unreadable")
                out.append(OpenPosition(
                    sym, float(notional), sym in {"SPY", "QQQ", "IWM"}, is_credit=True,
                    primary_con_id=int(cid), leg_con_ids=(int(cid),), contracts=abs(q),
                    campaign_id=f"contract:{int(cid)}:credit"))
                audit(self.audit_path, "short_position_folded", symbol=sym, con_id=int(cid),
                      quantity=q, collateral=round(float(notional), 2))
        except Exception as _se:



            audit(self.audit_path, "short_concurrency_read_error", error=str(_se))
            if strict:
                raise
        return out

    def _journal_short_context(self):
        """Public API contract; production-derived narrative omitted."""
        legs, collateral = set(), {}
        try:
            p = Path(self.journal_path)
            if not p.exists():
                return legs, collateral
            rows = []
            for line in p.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sp = rec.get("spread") or {}
                if sp.get("short_con_id") is not None:
                    try:
                        legs.add(int(sp["short_con_id"]))
                    except (TypeError, ValueError):
                        pass
                rows.append(rec)








            from exitmgr.manager import build_journal_campaigns, open_campaign
            campaigns = build_journal_campaigns(rows)
            for con_id in campaigns:
                campaign = open_campaign(campaigns, con_id)
                if campaign is None:
                    continue
                total = campaign.total("collateral_usd")
                if total is None or float(total) <= 0:
                    continue
                collateral[int(con_id)] = round(float(total), 2)
        except Exception:
            return legs, collateral
        return legs, collateral

    async def _deployed_collateral(self) -> Optional[float]:
        """Public API contract; production-derived narrative omitted."""
        snapshot = await broker_csp_collateral_snapshot(self.ib_conn.ib, self.audit_path)
        if snapshot is None:
            return None
        return round(snapshot.total + max(
            0.0, float(getattr(self, "_cycle_credit_collateral", 0.0) or 0.0)), 2)

    async def _credit_capacity(self, r: ResolvedOrder, *, pot=None):
        """Public API contract; production-derived narrative omitted."""
        detail = {"required": None, "deployed": None, "net_liq": None, "available_funds": None}
        req = required_collateral(getattr(r, "strike", 0.0), getattr(r, "qty", 0))
        detail["required"] = req
        try:
            snap = pot if pot is not None else await get_pot_snapshot(self.ib_conn.ib)
        except Exception as e:
            return entry_safety.SafetyResult(
                False, (f"live account snapshot unavailable for collateral check: {e}",)), detail
        acct = entry_safety.account_snapshot_valid(snap)
        if not acct.allowed:
            return entry_safety.SafetyResult(False, acct.reasons), detail
        detail["net_liq"] = _fnum(getattr(snap, "net_liq", None), None)
        detail["available_funds"] = _fnum(getattr(snap, "available_funds", None), None)
        deployed = await self._deployed_collateral()
        detail["deployed"] = deployed
        result = collateral_capacity(
            required=req, deployed=deployed,
            net_liq=detail["net_liq"], available_funds=detail["available_funds"],
            max_pct=CREDIT_MAX_COLLATERAL_PCT)
        return result, detail

    async def _underlyings_with_close_in_flight(self) -> tuple[Set[str], bool]:
        """Public API contract; production-derived narrative omitted."""
        names: Set[str] = set()
        readable = True

        try:
            trades = await asyncio.wait_for(
                self.ib_conn.ib.reqAllOpenOrdersAsync(), _IB_CALL_TIMEOUT_S)
            if trades is None:


                raise RuntimeError("reqAllOpenOrdersAsync returned None")
            for t in trades:
                o = getattr(t, "order", None)
                c = getattr(t, "contract", None)
                st = getattr(getattr(t, "orderStatus", None), "status", None)
                if o is None or c is None or getattr(o, "action", "") != "SELL":
                    continue
                if st in {"Cancelled", "ApiCancelled", "Inactive", "Filled"}:
                    continue
                sym = (getattr(c, "symbol", "") or "").upper()
                if sym:
                    names.add(sym)
        except Exception as _re:
            readable = False
            audit(self.audit_path, "close_inflight_orders_error",
                  error=str(_re) or type(_re).__name__)

        try:
            sm = getattr(self.exit_manager, "state_manager", None)
            inflight_cids = {int(k) for k in sm.state.in_flight.keys()} if sm is not None else set()
            if inflight_cids:
                raw = await self.ib_conn.get_positions()
                cid_to_sym: Dict[int, str] = {}
                for pd in raw.values():
                    cid = getattr(pd, "con_id", None)
                    if cid is not None:
                        cid_to_sym[int(cid)] = (getattr(pd, "symbol", "") or "").upper()
                for cid in inflight_cids:
                    sym = cid_to_sym.get(cid)
                    if sym:
                        names.add(sym)



        except Exception as _se:
            audit(self.audit_path, "close_inflight_state_error", error=str(_se))
        return names, readable

    def _book_detail(self, positions) -> dict:
        """Public API contract; production-derived narrative omitted."""
        try:
            syms = {str(getattr(p, "underlying", "")).upper() for p in (positions or [])}
            if not syms:
                return {}
            sm = getattr(self.exit_manager, "state_manager", None)
            marks = dict(getattr(getattr(sm, "state", None), "mark_path", {}) or {})
            journal_rows = []
            try:
                with open(self.journal_path) as fh:
                    for line in fh:
                        if not line.strip():
                            continue
                        row = json.loads(line)
                        if isinstance(row, dict):
                            journal_rows.append(row)
            except Exception:
                journal_rows = []
            from exitmgr.manager import build_journal_campaigns, open_campaign
            campaigns = build_journal_campaigns(journal_rows)
            by_contract, by_symbol = {}, {}
            for cid, series in marks.items():
                if not series:
                    continue
                try:
                    campaign = open_campaign(campaigns, int(cid))
                except (TypeError, ValueError):
                    campaign = None
                row = campaign.as_journal_row() if campaign is not None else {}
                sym = str(row.get("symbol") or "").upper()
                if sym not in syms:
                    continue
                last = series[-1]
                detail = {
                    "symbol": sym,
                    "contract_id": int(cid),
                    "campaign_seq": (int(campaign.campaign_seq)
                                     if campaign is not None else None),
                    "pnl_pct": last.get("pnl_pct"),
                    "dte": last.get("dte"),
                    "days_held": last.get("days_held"),
                    "dist_to_sl_pct": last.get("dist_to_sl_pct"),
                    "structure": row.get("structure") or (
                        "%s/%s%s spread" % (row.get("strike"),
                                            (row.get("spread") or {}).get("short_strike"),
                                            row.get("right", ""))
                        if row.get("spread") else None),
                    "intended_hold_days": row.get("intended_hold_days"),
                    "thesis": row.get("thesis"),
                }
                by_contract[str(cid)] = detail
                by_symbol.setdefault(sym, []).append(detail)
            return {"contracts": by_contract, "by_symbol": by_symbol}
        except Exception:
            return {}

    async def _market_context(self, positions: Optional[List[OpenPosition]] = None,
                              day_pnl: Optional[float] = None, *,
                              net_liq: Optional[float] = None,
                              available_funds: Optional[float] = None) -> str:
        from exitmgr.market import fetch_universe_quotes, format_context
        names = sorted({"SPY", "QQQ", "IWM"} | self.approved_names)
        today = str(datetime.now(timezone.utc).date())
        try:
            quotes = await fetch_universe_quotes(self.ib_conn.ib, names)
        except Exception as e:
            audit(self.audit_path, "context_quote_error", error=str(e))
            quotes = {}


        try:
            single_names = sorted(self.approved_names
                                  | {p.underlying for p in positions or [] if not p.is_index})
            data = await research.gather(self.ib_conn.ib, names, single_names=single_names)


            ps = data.get("price_stats") or {}
            self._price_stats = ps
            _history_available = sum(1 for _st in ps.values() if _st)
            audit(self.audit_path, "price_structure_coverage",
                  requested=len(names), available=_history_available,
                  missing=max(0, len(names) - _history_available),
                  complete=(_history_available == len(names)))




            self._research_degraded = None
            self._regime = regime.classify_regime([ps.get("SPY"), ps.get("QQQ"), ps.get("IWM")], data.get("vix"))
            audit(self.audit_path, "regime", **(self._regime or {}))
            brief = research.build_brief(today=today, quotes=quotes, universe=names,
                                         book_detail=self._book_detail(positions),
                                         allow_any_name=self.limits.allow_any_name,
                                         book=positions, day_pnl_pct=day_pnl,
                                         net_liq=net_liq, available_funds=available_funds,
                                         market_data_type=self.resolved_config.ib.market_data_type,
                                         max_premium_pct=(
                                             self.resolved_config.construction.max_premium_pct),
                                         **data)
            audit(self.audit_path, "strategist_brief", brief=brief)
            return brief
        except Exception as e:
            audit(self.audit_path, "research_error", error=str(e))





            self._research_degraded = str(e)[:200] or e.__class__.__name__
            audit(self.audit_path, "research_degraded_no_auto_submit",
                  error=self._research_degraded)
            fallback = format_context(quotes, names, today,
                                      allow_any_name=self.limits.allow_any_name)
            return research.with_account_sizing_snapshot(
                fallback, net_liq=net_liq, available_funds=available_funds,
                max_premium_pct=self.resolved_config.construction.max_premium_pct)

    async def _drop_blocked_sectors(self, ideas):
        """Public API contract; production-derived narrative omitted."""
        if not self.blocked_sector_keywords or not ideas:
            return ideas
        kept = []
        for idea in ideas:
            if idea.is_index:
                kept.append(idea)
                continue
            industry, sector = await asyncio.to_thread(research.sector_of, idea.underlying)
            if research.matches_blocked_sector(industry, sector, self.blocked_sector_keywords):
                audit(self.audit_path, "blocked_sector", underlying=idea.underlying,
                      industry=industry, sector=sector)
            else:
                kept.append(idea)
        return kept

    @staticmethod
    def _idea_from_stage_b(intent: StageAIntent, binding: CandidateBinding) -> TradeIdea:
        """Public API contract; production-derived narrative omitted."""
        resolved = binding.to_resolved_order(intent)
        qty = int(resolved.qty)
        is_credit_intent = intent.side == CREDIT_SIDE
        debit_total = (0.0 if is_credit_intent else
                       round(float(binding.candidate.one_contract_cost_usd) * qty, 2))
        idea = TradeIdea(
            underlying=intent.underlying,
            is_index=intent.underlying.upper() in INDEX_UNDERLYINGS,
            direction=intent.direction,
            structure=intent.structure,
            target_dte=int(intent.target_dte),
            target_delta=float(intent.target_delta),
            est_debit_usd=debit_total,
            conviction=int(intent.conviction),
            thesis=str(intent.thesis),
            intended_hold_days=int(intent.intended_hold_days),
            side=intent.side,
            collateral_usd=(float(resolved.collateral_usd) if is_credit_intent else 0.0),
            net_credit_usd=(float(resolved.net_credit_usd) if is_credit_intent else 0.0),
            max_loss_usd=(float(resolved.credit_max_loss_usd) if is_credit_intent else 0.0),
            strike=(float(resolved.strike) if is_credit_intent else 0.0),
        )
        idea.allocation_pct_net_liq = float(intent.allocation_pct_net_liq)
        idea.alpha = str(intent.alpha)
        idea._stage_a_intent = intent
        idea._stage_b_binding = binding
        return idea

    async def _materialize_stage_b(self, intents, pot):
        """Public API contract; production-derived narrative omitted."""
        ideas = []
        self._last_stage_b_outcomes = []
        for index, intent in enumerate(intents or [], start=1):
            intent_id = f"intent_{index}"
            try:
                deployed_credit = (await self._deployed_collateral()
                                   if intent.side == CREDIT_SIDE else 0.0)
                bindings = await build_entry_candidates(
                    self.ib_conn.ib, intent, intent_id,
                    net_liq=pot.net_liq, available_funds=pot.available_funds,
                    cons=self.construction,
                    deployed_credit_usd=deployed_credit,
                    cash_buffer_pct=self.limits.cash_buffer_pct)
            except Exception as exc:
                self._last_stage_b_outcomes.append(
                    {"intent_id": intent_id, "outcome": "candidate_error",
                     "underlying": getattr(intent, "underlying", None), "error": str(exc)})
                audit(self.audit_path, "stage_b_candidate_error", intent_id=intent_id,
                      underlying=getattr(intent, "underlying", None), error=str(exc))
                continue
            _pre_stage_b = len(bindings or [])



            _max_age = construction.stage_b_quote_max_age_s(self.construction)
            bindings = bindings_for_stage_b(bindings, max_age_seconds=_max_age)


            if _pre_stage_b and len(bindings) < _pre_stage_b:
                print(f"[BUILD] {intent.underlying}: {_pre_stage_b - len(bindings)}/"
                      f"{_pre_stage_b} candidates dropped for stale NBBO "
                      f"(> {_max_age:.0f}s); {len(bindings)} survive")
            if len(bindings) < 3:
                self._last_stage_b_outcomes.append(
                    {"intent_id": intent_id, "outcome": "construction_rejected",
                     "underlying": intent.underlying, "candidate_count": len(bindings)})
                audit(self.audit_path, "stage_b_skipped", intent_id=intent_id,
                      underlying=intent.underlying,
                      reason=f"only {len(bindings)} prefiltered candidates; requires 3")
                continue
            candidates = [binding.candidate for binding in bindings]
            try:






                result = await asyncio.to_thread(
                    select_candidate, self.endpoint, self.model, intent, candidates,
                    intent_id=intent_id, **STAGE_B_REQUEST)
                selected = result
                raw_b = cot_b = identity_b = None
                if isinstance(result, tuple):
                    selected = result[0] if result else None
                    raw_b = result[1] if len(result) > 1 else None
                    cot_b = result[2] if len(result) > 2 else None
                    identity_b = result[3] if len(result) > 3 else None
            except Exception as exc:
                self._last_stage_b_outcomes.append(
                    {"intent_id": intent_id, "outcome": "selector_error",
                     "underlying": intent.underlying, "error": str(exc)})
                audit(self.audit_path, "stage_b_error", intent_id=intent_id,
                      underlying=intent.underlying, error=str(exc))
                continue
            if selected is None:
                self._last_stage_b_outcomes.append(
                    {"intent_id": intent_id, "outcome": "stage_b_declined",
                     "underlying": intent.underlying, "candidate_count": len(candidates),
                     "raw": raw_b, "cot": cot_b})
                audit(self.audit_path, "stage_b_declined", intent_id=intent_id,
                      underlying=intent.underlying)
                continue
            selected_id = getattr(selected, "candidate_id", None)
            binding = select_binding(bindings, selected_id)
            if binding is None:
                self._last_stage_b_outcomes.append(
                    {"intent_id": intent_id, "outcome": "invalid_selection",
                     "underlying": intent.underlying, "candidate_id": selected_id})
                audit(self.audit_path, "stage_b_invalid_selection", intent_id=intent_id,
                      underlying=intent.underlying, candidate_id=selected_id)
                continue
            idea = self._idea_from_stage_b(intent, binding)
            idea._stage_b_candidates = tuple(candidates)
            idea._stage_b_raw = raw_b
            idea._stage_b_cot = cot_b
            idea._stage_b_identity = identity_b
            ideas.append(idea)
            self._last_stage_b_outcomes.append(
                {"intent_id": intent_id, "outcome": "selected",
                 "underlying": intent.underlying, "candidate_id": selected_id})
            audit(self.audit_path, "stage_b_selected", intent_id=intent_id,
                  underlying=intent.underlying, candidate_id=selected_id,
                  candidates=len(candidates))
        return ideas

    async def _notify_pipeline(self, kind, detail="", *, symbol="", recovery=False):
        """Public API contract; production-derived narrative omitted."""
        titles = {
            "model_error": (":white_check_mark: *Trader model responses recovered*" if recovery
                            else ":warning: *Trader evaluation failed*"),
            "contract_error": (":white_check_mark: *Trader contract evaluation recovered*" if recovery
                               else ":warning: *Trader contract evaluation incomplete*"),
            "entry_halted": ":pause_button: *New entry evaluation is paused*",
            "no_candidate": ":information_source: *Trader ideas did not produce an actionable contract*",
            "quote_rejected": f":pause_button: *Trader skipped {pipeline_notice.safe_detail(symbol)} after refreshing its quote*",
            "resolve_error": f":warning: *Trader could not resolve {pipeline_notice.safe_detail(symbol)}*",
        }
        text = titles[kind] + "\n" + pipeline_notice.safe_detail(detail)
        try:
            result = await asyncio.to_thread(
                pipeline_notice.deliver,
                path=str(Path(self.audit_path).with_suffix(".pipeline-notices.json")),
                token=self.slack_token, channel=self.slack_channel,
                kind=kind, symbol=symbol, text=text, recovery=recovery)
            audit(self.audit_path, "entry_status_notice", kind=kind, symbol=symbol,
                  recovery=recovery, **result)
        except Exception as exc:

            audit(self.audit_path, "entry_status_notice", kind=kind, symbol=symbol,
                  recovery=recovery, status="failed", reason=type(exc).__name__)

    async def _shadow_cot_capture(self, context):
        """Public API contract; production-derived narrative omitted."""
        try:
            if not isinstance(context, dict) or not context.get("model_was_asked"):
                return
            def digest(value):
                return (hashlib.sha256(value.encode("utf-8")).hexdigest()
                        if isinstance(value, str) else None)
            audit(
                self.audit_path, "entry_reasoning_shadow_skipped",
                reason="foreground_capture_reused_no_extra_inference",
                thinking=STAGE_A_REQUEST_CONTINUOUS["thinking"],
                shadow_inference_started=False,
                model_identity=context.get("model_identity"),
                model_identity_source=context.get("model_identity_source", "unknown"),
                foreground_data_ref={
                    "dataset_dir": trade_capture.dataset_dir(self.journal_path),
                    "market_context_sha256": digest(context.get("market_context")),
                    "raw_response_sha256": digest(context.get("raw")),
                    "cot_sha256": digest(context.get("cot")),
                },
            )
        except Exception:

            pass

    async def run_once(self, dry_run: bool, *, skip_exit_cycle: bool = False) -> None:


        shadow_contexts = []
        try:
            return await self._run_once(
                dry_run, skip_exit_cycle=skip_exit_cycle,
                _shadow_contexts=shadow_contexts)
        finally:
            for context in shadow_contexts:
                try:
                    await self._shadow_cot_capture(context)
                except Exception:


                    pass

    async def _run_once(self, dry_run: bool, *, skip_exit_cycle: bool = False,
                        _shadow_contexts) -> None:



        defer_model = slate_lock.slate_active()

        self._cycle_credit_collateral = 0.0


        self._last_stage_b_outcomes = []
        _is_market_open = _market_open()
        pot = await get_pot_snapshot(self.ib_conn.ib)
        today = _trading_day()
        baselines = self._load_baselines()
        baseline, baselines = day_start_pot(baselines, today, pot.net_liq)
        self._save_baselines(baselines)

        dp = day_pnl_pct(pot.net_liq, baseline)
        audit(self.audit_path, "cycle_start", net_liq=pot.net_liq, available=pot.available_funds,
              day_start=baseline, day_pnl_pct=round(dp, 4), dry_run=dry_run)
        if defer_model:
            audit(self.audit_path, "model_deferred", reason="slate_active")













        _entry_book_unreadable = None
        _entry_order_view = await broker_entry_order_view(self.ib_conn.ib, self.audit_path)
        if _entry_order_view.readable:
            positions = await self._open_positions(open_trades=list(_entry_order_view.trades))
        else:
            _entry_book_unreadable = _entry_order_view.error
            audit(self.audit_path, "entry_blocked_order_book_unreadable", stage="pre_exit_book",
                  error=_entry_order_view.error)
            positions = await self._open_positions()








        if _entry_order_view.readable:
            _intents = await self.reconcile_open_entry_intents(view=_entry_order_view)
            if (_intents.journaled or _intents.released or _intents.retained
                    or _intents.still_working):
                audit(self.audit_path, "entry_intent_reconcile",
                      readable=_intents.readable, journaled=list(_intents.journaled),
                      released=list(_intents.released),
                      still_working=list(_intents.still_working),
                      retained=list(_intents.retained), reasons=list(_intents.reasons))
            if _intents.journaled:

                positions = await self._open_positions(
                    open_trades=list(_entry_order_view.trades))

        if skip_exit_cycle and self.exit_manager.state_manager.persist is False:


            try:
                _entry_reconciled = await self.exit_manager.refresh_entry_reconciliation()
                self.exit_manager._reconcile_ok = _entry_reconciled is True
                audit(self.audit_path, "entry_reconciliation_refreshed",
                      allowed=self.exit_manager._reconcile_ok,
                      reason=getattr(self.exit_manager, "_entry_reconcile_error", None))
            except Exception as exc:
                self.exit_manager._reconcile_ok = False
                audit(self.audit_path, "entry_reconciliation_refreshed", allowed=False,
                      reason="refresh_failed:" + type(exc).__name__)

        context = await self._market_context(
            positions, dp, net_liq=pot.net_liq, available_funds=pot.available_funds)






        if not skip_exit_cycle:
            try:
                await self.exit_manager.run_cycle(
                    dry_run, regime=self._regime, price_stats=self._price_stats,
                    defer_model=defer_model)
                self._exit_fail_streak = 0
            except Exception as e:
                self._exit_fail_streak += 1
                audit(self.audit_path, "exit_cycle_error", error=str(e), streak=self._exit_fail_streak)



                try:
                    approval.post_proposal(self.slack_token, self.slack_channel,
                        f":warning: *Exit cycle FAILED* ({self._exit_fail_streak}x consecutive): {e}"
                        + (f"\n_New entries SUPPRESSED until exits recover._"
                           if self._exit_fail_streak >= self._EXIT_FAIL_SUPPRESS_ENTRIES else ""))
                except Exception as _se:
                    print(f"[WARN] exit-cycle-failure Slack alert failed: {_se}")










        _entries_halted, _halt_reason = False, None
        _marker_gate = self._entry_markers_clear()
        _account_gate = entry_safety.account_snapshot_valid(pot)
        if not _account_gate.allowed:
            _entries_halted = True
            _halt_reason = "account_snapshot_invalid: " + "; ".join(_account_gate.reasons)
        elif not _marker_gate.allowed:
            _entries_halted, _halt_reason = True, "; ".join(_marker_gate.reasons)
        elif getattr(self.exit_manager, "_reconcile_ok", True) is False:
            _entries_halted, _halt_reason = True, "reconcile_unsafe"
        elif self._exit_fail_streak >= self._EXIT_FAIL_SUPPRESS_ENTRIES:
            _entries_halted, _halt_reason = True, f"exit_cycle_failing_{self._exit_fail_streak}x"

        _raw_strategist = None
        _cot = None
        _model_identity = None





        _model_identity_source = None
        _stage_a_intent_count = None
        _post_construction_intent_count = None
        _entry_pipeline_error = None
        if not _is_market_open:
            audit(self.audit_path, "strategist_skipped", reason="market_closed")
            ideas = []
        elif _entries_halted:
            audit(self.audit_path, "strategist_skipped", reason=_halt_reason)
            await self._notify_pipeline("entry_halted", _halt_reason)
            ideas = []
        else:
            try:






































                _model_identity_source = "unknown"
                _res = await asyncio.to_thread(
                    propose_intents, self.endpoint, self.model, context,
                    **STAGE_A_REQUEST_CONTINUOUS)
                await self._notify_pipeline(
                    "model_error", "The latest intent response passed validation; contract and risk checks still apply.",
                    recovery=True)

                if isinstance(_res, tuple) and len(_res) == 4:
                    intents, _raw_strategist, _cot, _model_identity = _res




                    if _model_identity is not None:
                        _model_identity_source = "meta"
                elif isinstance(_res, tuple) and len(_res) == 3:
                    intents, _raw_strategist, _cot = _res
                elif isinstance(_res, tuple) and len(_res) == 2:
                    intents, _raw_strategist = _res
                else:
                    intents = _res


                _shadow_contexts.append({
                    "model_was_asked": True,
                    "market_context": context,
                    "raw": _raw_strategist,
                    "cot": _cot,
                    "model_identity": _model_identity,
                    "model_identity_source": _model_identity_source,
                })
                try:
                    _stage_a_intent_count = len(intents or [])
                except TypeError:
                    _stage_a_intent_count = None















                _cx = construction.apply_construction_policy(
                    intents,
                    min_dte=getattr(self.construction, "min_dte", None),
                    enabled=bool(getattr(self.construction,
                                         "deterministic_construction", True)))
                for _ch in _cx.changes:
                    audit(self.audit_path, "deterministic_construction", **_ch)
                for _bad_intent, _why_dropped in _cx.dropped:
                    audit(self.audit_path, "deterministic_construction_refused",
                          underlying=getattr(_bad_intent, "underlying", None),
                          intended_hold_days=getattr(_bad_intent, "intended_hold_days", None),
                          reason=_why_dropped)
                intents = list(_cx.ideas)
                _post_construction_intent_count = len(intents)
                ideas = await self._materialize_stage_b(intents, pot)
                if stage_b_technical_failures(self._last_stage_b_outcomes):
                    await self._notify_pipeline(
                        "contract_error", stage_b_failure_detail(self._last_stage_b_outcomes))
                elif any(row.get("outcome") in {"selected", "stage_b_declined"}
                         for row in self._last_stage_b_outcomes):
                    await self._notify_pipeline(
                        "contract_error", "The latest priced-contract evaluation completed; risk checks still apply.",
                        recovery=True)
            except Exception as e:
                _entry_pipeline_error = str(e)
                audit(self.audit_path, "strategist_error", error=str(e))
                await self._notify_pipeline("model_error", str(e))
                ideas = []
        audit(self.audit_path, "proposals", count=len(ideas))








        if self.reload_enabled and not _entries_halted and _is_market_open:
            try:
                _reload_ideas = self._drain_reload_ideas(today)
            except Exception as _rde:
                audit(self.audit_path, "reload_drain_error", error=str(_rde))
                _reload_ideas = []
            if _reload_ideas:
                ideas = list(_reload_ideas) + list(ideas)
                audit(self.audit_path, "reload_ideas_drained", count=len(_reload_ideas),
                      symbols=[i.underlying for i in _reload_ideas])




        try:
            if not ideas:
                if not _is_market_open:
                    _no_trade_reason, _training_ok = "market_closed", False
                elif _entries_halted:
                    _no_trade_reason, _training_ok = "entry_pipeline_halted", False
                elif _entry_pipeline_error is not None:
                    _no_trade_reason, _training_ok = "entry_pipeline_error", False
                elif stage_b_technical_failures(self._last_stage_b_outcomes):
                    _no_trade_reason, _training_ok = "stage_b_error", False
                elif _stage_a_intent_count == 0:
                    _no_trade_reason, _training_ok = "empty_slate", True
                elif _post_construction_intent_count == 0:
                    _no_trade_reason, _training_ok = "construction_rejected", False
                else:
                    _no_trade_reason, _training_ok = "stage_b_rejected", False
                audit(self.audit_path, "entry_pipeline_no_trade",
                      reason=_no_trade_reason,
                      stage_a_intent_count=_stage_a_intent_count,
                      post_construction_intent_count=_post_construction_intent_count,
                      stage_b_outcomes=getattr(self, "_last_stage_b_outcomes", []),
                      training_eligible=_training_ok)
                if _no_trade_reason in {"construction_rejected", "stage_b_rejected"}:
                    outcomes = getattr(self, "_last_stage_b_outcomes", [])
                    labels = {"construction_rejected": "no contracts passed construction",
                              "selector_error": "contract-selection model failed",
                              "stage_b_declined": "model declined the priced contracts",
                              "invalid_selection": "model returned an invalid contract selection"}
                    summary = "; ".join(
                        f"{row.get('underlying', '?')}: {labels.get(row.get('outcome'), 'selection unavailable')}"
                        for row in outcomes)
                    await self._notify_pipeline(
                        "no_candidate", summary or "No proposed intent survived contract construction.")
                trade_capture.capture_no_trade(
                    trade_capture.dataset_dir(self.journal_path), source="trader",
                    reason=_no_trade_reason,
                    raw_strategist=_raw_strategist, cot=_cot, candidates=None,
                    regime=self._regime, market_context=context,







                    model_identity=_model_identity,
                    model_identity_source=_model_identity_source,
                    training_eligible=_training_ok,
                    extra={"stage_a_intent_count": _stage_a_intent_count,
                           "post_construction_intent_count": _post_construction_intent_count,
                           "stage_b_outcomes": getattr(self, "_last_stage_b_outcomes", [])})
        except Exception as _ce:
            print(f"[WARN] no_trade capture failed (continuing): {_ce}")

        ideas = await self._drop_blocked_sectors(ideas)
        _conflict_symbols = set()
        try:
            _conflict_symbols = self.exit_manager.campaign_conflict_symbols()
        except Exception:


            pass
        if _conflict_symbols:
            _kept = []
            for _idea in ideas:
                if str(_idea.underlying).upper() in _conflict_symbols:
                    audit(self.audit_path, "campaign_conflict_entry_rejected",
                          underlying=_idea.underlying,
                          reason="ordered campaign conflicts with legacy journal row")
                else:
                    _kept.append(_idea)
            ideas = _kept




        _open_book = []
        _budget_items = {}
        _budget_snapshot_error = None
        self._same_cycle_budget_items = {}
        if ideas:
            try:
                _raw_budget_positions = await self.ib_conn.get_positions()
                _working_orders = await self.ib_conn.ib.reqAllOpenOrdersAsync()
                _budget_items = construction.open_book_items(
                    _raw_budget_positions, self.journal_path, _working_orders)
                _open_book = list(_budget_items.values())
            except Exception as e:
                _budget_snapshot_error = str(e)
                audit(self.audit_path, "open_book_error", error=str(e))

        _ddir = trade_capture.dataset_dir(self.journal_path)

        def _notify_gate_hold(idea_obj, gate_obj):
            """Public API contract; production-derived narrative omitted."""
            try:
                import hashlib as _SYMO, json as _js, os as _os, time as _t
                reasons = list(getattr(gate_obj, "reasons", []) or [])
                if not reasons:
                    return
                sym = getattr(idea_obj, "underlying", "?")
                fp = _SYMO.sha256(_js.dumps({"r": sorted(reasons), "s": sym},
                                          sort_keys=True).encode()).hexdigest()
                path = _os.path.join(
                    _os.path.dirname(_os.path.abspath(self.audit_path)) or ".",
                    ".gate_hold_notify.json")
                prev = {}
                try:
                    with open(path) as _fh:
                        prev = _js.load(_fh)
                except Exception:
                    prev = {}
                now = _t.time()
                same = prev.get("fp") == fp
                recent = (now - float(prev.get("ts", 0) or 0)) < 4 * 3600
                if same and recent:
                    return
                try:
                    tmp = path + ".tmp"
                    with open(tmp, "w") as _fh:
                        _js.dump({"fp": fp, "ts": now}, _fh)
                    _os.replace(tmp, path)
                except Exception:
                    pass
                why = "; ".join(str(r) for r in reasons)[:180]
                conv = getattr(idea_obj, "conviction", None)
                approval.post_proposal(
                    self.slack_token, self.slack_channel,
                    f":pause_button: *Holding {sym}* "
                    f"({getattr(idea_obj, 'direction', '?')} "
                    f"{getattr(idea_obj, 'structure', '?')}"
                    + (f", conviction {conv}/10" if conv is not None else "") + ")"
                    f"\n_The trader is running and finding ideas — the gate is declining them._"
                    f"\nReason: {why}")
            except Exception as _ne:
                print(f"[WARN] gate-hold notify failed (continuing): {_ne}")

        def _cap_rej(stage, reason, *, resolved=None, gate=None, construction=None):
            """Public API contract; production-derived narrative omitted."""
            try:
                trade_capture.capture_rejected(
                    _ddir, source="trader", symbol=idea.underlying, reason=reason, stage=stage,
                    idea=idea, gate=gate, construction=construction,
                    structure=getattr(idea, "structure", None),
                    right=(getattr(resolved, "right", None)),
                    strike=(getattr(resolved, "strike", None)),
                    expiry=(getattr(resolved, "expiry", None)),
                    order=(order_summary(resolved) if resolved is not None else None),
                    regime=self._regime)
            except Exception as _re:
                print(f"[WARN] capture_rejected failed (continuing): {_re}")






        _orders_this_cycle = 0
        _sm = getattr(self.exit_manager, "state_manager", None)









        _throttle_store = None
        try:
            if _sm is not None:
                _throttle_store = EntryThrottleStore.for_state_path(_sm.state_path)
        except Exception as _tse:
            print(f"[WARN] entry throttle store unavailable (continuing): {_tse}")

        def _day_open_counts():
            """Public API contract; production-derived narrative omitted."""
            return entry_day_open_counts(_sm, _throttle_store, today)











        entry_positions = positions
        if ideas:





            try:
                _refetch_view = await broker_entry_order_view(self.ib_conn.ib, self.audit_path)
                if _refetch_view.readable:
                    entry_positions = await self._open_positions(
                        open_trades=list(_refetch_view.trades))
                else:
                    _entry_book_unreadable = _entry_book_unreadable or _refetch_view.error
                    audit(self.audit_path, "entry_blocked_order_book_unreadable",
                          stage="post_exit_refetch", error=_refetch_view.error)
                    entry_positions = positions
            except Exception as _SYMN:
                _entry_book_unreadable = _entry_book_unreadable or str(_SYMN) or type(_SYMN).__name__
                audit(self.audit_path, "entry_book_refetch_error", error=str(_SYMN))
                entry_positions = positions







        _closing_names: Set[str] = set()
        if ideas:




            _closing_names, _closing_readable = await self._underlyings_with_close_in_flight()
            if not _closing_readable:
                _entry_book_unreadable = _entry_book_unreadable or (
                    "close-in-flight scan unreadable")
                audit(self.audit_path, "entry_blocked_order_book_unreadable",
                      stage="close_in_flight_scan")

        for idea in ideas:
            if _entry_book_unreadable is not None:



                _cap_rej("order_book_unreadable",
                         f"broker open-order book UNKNOWN ({_entry_book_unreadable}); an "
                         "unreadable book is never an empty one -- entry blocked")
                continue
            if _budget_snapshot_error is not None:
                _cap_rej("budget_snapshot", "working BUY/deployment snapshot unavailable; entry blocked")
                continue




            _ok_credit, _why_credit = credit_structure_ok(idea)
            if not _ok_credit:
                audit(self.audit_path, "credit_structure_rejected", underlying=idea.underlying,
                      structure=getattr(idea, "structure", None),
                      side=getattr(idea, "side", None), reason=_why_credit)
                _cap_rej("credit_structure", _why_credit)
                approval.post_proposal(self.slack_token, self.slack_channel,
                    f":no_entry: REFUSED *{idea.underlying}* — {_why_credit}")
                continue






            _ok_debit, _why_debit = debit_structure_ok(idea)
            if not _ok_debit:
                audit(self.audit_path, "debit_structure_rejected", underlying=idea.underlying,
                      structure=getattr(idea, "structure", None),
                      direction=getattr(idea, "direction", None), reason=_why_debit)
                _cap_rej("debit_structure", _why_debit)
                approval.post_proposal(self.slack_token, self.slack_channel,
                    f":no_entry: REFUSED *{idea.underlying}* — {_why_debit}")
                continue

            if idea.underlying.upper() in _closing_names:
                audit(self.audit_path, "entry_deferred_close_in_flight", underlying=idea.underlying)
                _cap_rej("close_in_flight",
                         "entry_deferred_close_in_flight: underlying has an in-flight/resting close")
                continue
            plan = plan_idea(idea, net_liq=pot.net_liq, available_funds=pot.available_funds,
                             positions=entry_positions, baseline=baseline,
                             approved_names=self.approved_names, limits=self.limits,
                             regime=self._regime)
            audit(self.audit_path, "gated", idea=asdict(idea),
                  approved=plan.gate.approved, reasons=plan.gate.reasons,
                  per_trade_cap=plan.gate.per_trade_cap)
            if not plan.gate.approved:
                _cap_rej("risk_gate", plan.gate.reasons, gate=plan.gate)


                _notify_gate_hold(idea, plan.gate)
                continue


            try:
                resolved = await self._resolve_order(
                    idea, plan.gate.per_trade_cap,
                    net_liq=pot.net_liq, available_funds=pot.available_funds)
            except Exception as e:
                audit(self.audit_path, "resolve_error", underlying=idea.underlying, error=str(e))
                await self._notify_pipeline("resolve_error", str(e), symbol=idea.underlying)
                _cap_rej("resolve_error", str(e), gate=plan.gate)
                continue
            if resolved is None:
                audit(self.audit_path, "resolve_failed", underlying=idea.underlying)
                _cap_rej("resolve_failed", "no usable contract (construction/chain)", gate=plan.gate)
                continue
            resolved.conviction = getattr(idea, "conviction", -1.0)
            resolved.intended_hold_days = getattr(idea, "intended_hold_days", None)
            try:
                resolved.thesis = str(getattr(idea, "thesis", "") or "")
            except Exception as _te:
                print(f"[WARN] thesis carry failed (continuing): {_te}")
            try:

                resolved.technical_card = (self._price_stats or {}).get(idea.underlying)
            except Exception as _tce:
                print(f"[WARN] technical_card carry failed (continuing): {_tce}")









            _tp_max, _tp_def = construction.tp_tier_for_pot(
                pot.net_liq, self.caps_tp_tiers, self.construction.tp_max_pct, self.construction.tp_pct)
            _cons_tp = _replace_dc(self.construction, tp_max_pct=_tp_max, tp_pct=_tp_def)
            resolved.tp_pct, resolved.sl_pct = construction.clamp_tp_sl(
                getattr(idea, "profit_target_pct", 0.0), getattr(idea, "stop_pct", 0.0),
                _cons_tp)






            if getattr(idea, "is_reload", False):
                _rf_ok, _rf_reason, _rf_detail = reload_queue.reload_friction_ok(
                    reload_conviction=getattr(idea, "reload_conviction", None),
                    conviction_min=self.reload_conviction_min,
                    expected_continuation_pct=getattr(
                        idea, "reload_expected_continuation_pct", None),
                    new_debit=resolved.limit * 100 * resolved.qty,
                    qty=resolved.qty,
                    is_spread=(resolved.short_contract is not None),
                    theta_per_share=getattr(resolved, "entry_theta", 0.0),
                    entry_spread_pct=getattr(resolved, "entry_spread_pct", 0.0),
                    k=self.reload_friction_k)
                if not _rf_ok:
                    audit(self.audit_path, "reload_friction_rejected", underlying=idea.underlying,
                          order=order_summary(resolved), reason=_rf_reason, detail=_rf_detail)
                    _cap_rej("reload_friction", _rf_reason, resolved=resolved, gate=plan.gate)
                    approval.post_proposal(self.slack_token, self.slack_channel,
                        f":no_entry: Skipped RELOAD *{idea.underlying}* {order_summary(resolved)} — "
                        f"anti-churn: {_rf_reason}")
                    continue
            _is_credit = is_credit(resolved)
            if not _is_credit:
                _max_prem = construction.max_premium_budget(pot.net_liq, self.construction)
                if _max_prem > 0 and resolved.limit * 100 * resolved.qty > _max_prem + 1e-6:
                    _newq = int(_max_prem // (resolved.limit * 100))
                    if _newq >= 1:
                        audit(self.audit_path, "premium_downsized", underlying=idea.underlying,
                              from_qty=resolved.qty, to_qty=_newq, premium_cap=round(_max_prem))
                        resolved.qty = _newq








            _budget_cost = 0.0 if _is_credit else resolved.limit * 100 * resolved.qty
            _budget_dte = 0 if _is_credit else resolved.dte
            ok_budget, budget_reasons = construction.check_budget(
                _budget_cost, _budget_dte, pot.net_liq, _open_book, self.construction)
            if not ok_budget:
                audit(self.audit_path, "budget_rejected", underlying=idea.underlying,
                      order=order_summary(resolved), reasons=budget_reasons)
                _cap_rej("budget", budget_reasons, resolved=resolved, gate=plan.gate,
                         construction={"tp_pct": resolved.tp_pct, "sl_pct": resolved.sl_pct,
                                       "dte": resolved.dte, "dte_adjusted": resolved.dte_adjusted,
                                       "qty": resolved.qty, "budget_reasons": budget_reasons})
                approval.post_proposal(self.slack_token, self.slack_channel,
                    f":no_entry: Skipped *{idea.underlying}* {order_summary(resolved)} — budget gate: "
                    + "; ".join(budget_reasons))
                continue






            if _is_credit:
                _cap_ok, _cap_detail = await self._credit_capacity(resolved, pot=pot)
                audit(self.audit_path, "credit_collateral_check", stage="proposal",
                      underlying=idea.underlying, allowed=_cap_ok.allowed,
                      reasons=list(_cap_ok.reasons), **_cap_detail)
                if not _cap_ok.allowed:
                    _why_cap = "; ".join(_cap_ok.reasons)
                    _cap_rej("credit_collateral", _why_cap, resolved=resolved, gate=plan.gate)
                    approval.post_proposal(self.slack_token, self.slack_channel,
                        f":no_entry: Skipped *{idea.underlying}* {order_summary(resolved)} — "
                        f"collateral gate: {_why_cap}")
                    continue




            resolved.earnings_unchecked = False
            resolved.earnings_warn = ""
            resolved.earnings_day_warning = False
            _entry = datetime.now(timezone.utc).date()
            try:

                _edays = await asyncio.to_thread(research.days_to_earnings, idea.underlying)
            except Exception as _ee:
                print(f"[WARN] earnings lookup failed for {idea.underlying} (fail-open, unchecked): {_ee}")
                _edays = None
            _earn_date = (_entry + timedelta(days=_edays)) if _edays is not None else None
            resolved.earnings_unchecked = _earn_date is None
            resolved.earnings_date = _earn_date.isoformat() if _earn_date is not None else None
            resolved.earnings_day_warning = (_earn_date == _entry)



            ok_earn, why_earn = construction.earnings_ok(_entry, resolved.expiry, _earn_date,
                                                         self.construction,
                                                         hold_days=getattr(idea, "intended_hold_days", None))
            if not ok_earn:
                audit(self.audit_path, "earnings_blackout_rejected", underlying=idea.underlying,
                      order=order_summary(resolved), reason=why_earn)
                _cap_rej("earnings_blackout", why_earn, resolved=resolved, gate=plan.gate,
                         construction={"tp_pct": resolved.tp_pct, "sl_pct": resolved.sl_pct,
                                       "dte": resolved.dte, "dte_adjusted": resolved.dte_adjusted,
                                       "qty": resolved.qty, "earnings_date": str(_earn_date),
                                       "earnings_reason": why_earn})
                approval.post_proposal(self.slack_token, self.slack_channel,
                    f":no_entry: Skipped *{idea.underlying}* {order_summary(resolved)} — {why_earn}")
                continue
            if why_earn:
                resolved.earnings_warn = why_earn
                audit(self.audit_path, ("earnings_same_day_warning"
                       if resolved.earnings_day_warning else "earnings_overlap_disclosed"),
                      phase="proposal", underlying=idea.underlying,
                      order=order_summary(resolved), earnings_date=resolved.earnings_date,
                      reason=why_earn)
            if resolved.earnings_unchecked:



                audit(self.audit_path, "earnings_unchecked", underlying=idea.underlying,
                      order=order_summary(resolved))







            resolved.assignment_warn = ""
            resolved.assignment_unchecked = False
            if resolved.short_contract is not None:
                _entry_a = datetime.now(timezone.utc).date()
                try:

                    _xdays = await asyncio.to_thread(research.days_to_ex_dividend, idea.underlying)
                except Exception as _xe:
                    print(f"[WARN] ex-div lookup failed for {idea.underlying} (fail-open, unchecked): {_xe}")
                    _xdays = None
                _exdiv_date = (_entry_a + timedelta(days=_xdays)) if _xdays is not None else None
                resolved.assignment_unchecked = _exdiv_date is None
                ok_assign, why_assign = construction.assignment_risk_ok(
                    resolved.short_strike, resolved.spot, resolved.right, resolved.expiry,
                    _exdiv_date, resolved.dte, self.construction)
                if not ok_assign:
                    audit(self.audit_path, "assignment_risk_rejected", underlying=idea.underlying,
                          order=order_summary(resolved), reason=why_assign)
                    _cap_rej("assignment_risk", why_assign, resolved=resolved, gate=plan.gate,
                             construction={"tp_pct": resolved.tp_pct, "sl_pct": resolved.sl_pct,
                                           "dte": resolved.dte, "dte_adjusted": resolved.dte_adjusted,
                                           "qty": resolved.qty, "short_strike": resolved.short_strike,
                                           "ex_div_date": str(_exdiv_date),
                                           "assignment_reason": why_assign})
                    approval.post_proposal(self.slack_token, self.slack_channel,
                        f":no_entry: Skipped *{idea.underlying}* {order_summary(resolved)} — {why_assign}")
                    continue
                if why_assign:
                    resolved.assignment_warn = why_assign
                    audit(self.audit_path, "assignment_risk_warn", underlying=idea.underlying,
                          order=order_summary(resolved), reason=why_assign)
                elif resolved.assignment_unchecked:
                    audit(self.audit_path, "assignment_unchecked", underlying=idea.underlying,
                          order=order_summary(resolved))




            _throttle = None


            _cost_throttle = capital_committed(resolved)




            try:
                _od_today, _nd_today = _day_open_counts()
            except EntryThrottleUnreadable as _tue:
                _throttle_unreadable = (
                    f"daily entry-throttle counters unreadable, so caps.max_orders_per_day / "
                    f"caps.max_notional_per_day cannot be evaluated: {_tue}")
                audit(self.audit_path, "entry_cap_skipped", underlying=idea.underlying,
                      reason=_throttle_unreadable, order=order_summary(resolved))
                _cap_rej("entry_cap", _throttle_unreadable, resolved=resolved, gate=plan.gate)
                continue
            if self.max_orders_per_cycle is not None and _orders_this_cycle >= self.max_orders_per_cycle:
                _throttle = (f"per-cycle order cap reached "
                             f"({_orders_this_cycle} >= {self.max_orders_per_cycle})")
            elif self.max_orders_per_day is not None and _od_today + 1 > self.max_orders_per_day:
                _throttle = (f"daily order cap reached "
                             f"({_od_today} >= {self.max_orders_per_day})")
            elif (self.max_notional_per_day is not None
                  and _nd_today + _cost_throttle > self.max_notional_per_day + 1e-6):
                _throttle = (f"daily notional cap: ${_nd_today:,.0f} + ${_cost_throttle:,.0f} "
                             f"> ${self.max_notional_per_day:,.0f}")
            if _throttle:
                audit(self.audit_path, "entry_cap_skipped", underlying=idea.underlying,
                      reason=_throttle, order=order_summary(resolved))
                _cap_rej("entry_cap", _throttle, resolved=resolved, gate=plan.gate)
                continue

            resolved.decision_id = entry_safety.new_decision_id()








            _is_reload = bool(getattr(idea, "is_reload", False))
            if _is_reload:
                resolved.model_identity = None
                resolved.model_identity_source = "unknown"
            else:
                resolved.model_identity, resolved.model_identity_source = (
                    approval.decisive_model_identity(
                        _model_identity, _model_identity_source,
                        getattr(idea, "_stage_b_identity", None)))
            resolved.decision_revision = 0
            try:
                trade_capture.capture_decision(
                    _ddir, source="trader", symbol=idea.underlying,
                    right=resolved.right, strike=resolved.strike, expiry=resolved.expiry,
                    structure=(CSP_STRUCTURE if is_credit(resolved) else
                               ("spread" if resolved.short_contract is not None else "single")),
                    con_id=getattr(resolved.contract, "conId", None), chosen_idea=idea,
                    candidates=ideas, raw_strategist=_raw_strategist, cot=_cot,
                    gate=plan.gate, regime=self._regime, market_context=context,
                    technical_card=self._price_stats,
                    decision_id=resolved.decision_id, revision=0, event="proposal",
                    model_identity=resolved.model_identity,
                    model_identity_source=resolved.model_identity_source,
                    final_contract=contract_snapshot(resolved))
            except Exception as _capture_error:
                print(f"[WARN] proposal capture failed (continuing): {_capture_error}")
            _approval_ttl = min(entry_safety.DEFAULT_APPROVAL_TTL_SECONDS,
                                max(1, int(self.approve_timeout_s)))
            msg = approval.format_proposal(
                idea, pot.net_liq, plan.gate.per_trade_cap,
                max(1, _approval_ttl // 60), order_summary(resolved),
                model_identity=resolved.model_identity,
                model_identity_source=resolved.model_identity_source,
                approval_required=False)
            if getattr(idea, "is_reload", False):


                msg = (f":arrows_counterclockwise: *RELOAD / continuation* — re-entering "
                       f"*{idea.underlying}* after banking a take-profit "
                       f"(model reload_conviction {getattr(idea, 'reload_conviction', '?')}).\n") + msg
            if getattr(resolved, "earnings_day_warning", False):
                msg = (":rotating_light: *EARNINGS TODAY — ENTRY WARNING*\n"
                       f"{resolved.earnings_warn}\n\n") + msg
            elif getattr(resolved, "earnings_warn", ""):
                msg += f"\n:warning: {resolved.earnings_warn}"
            elif getattr(resolved, "earnings_unchecked", False):

                msg += ("\n:grey_question: earnings date unknown — event risk UNCHECKED; "
                        "final earnings verification is still required before submission")
            if getattr(resolved, "assignment_warn", ""):

                msg += f"\n:warning: {resolved.assignment_warn}"
            elif getattr(resolved, "assignment_unchecked", False):

                msg += "\n:grey_question: ex-dividend date unknown — early-assignment risk UNCHECKED"
            _manual_msg = approval.format_decision_notice(
                msg, resolved.decision_id, _approval_ttl // 60 or 1,
                approval_required=True,
            )
            if dry_run:
                approval.post_proposal(self.slack_token, self.slack_channel,
                                       "[DRY RUN — nothing will be submitted]\n" + _manual_msg)
                audit(self.audit_path, "dry_run_proposal", underlying=idea.underlying,
                      order=order_summary(resolved))
                continue










            _est_gross = capital_at_risk(resolved)
            if pot.net_liq > 0 and _est_gross > 0.95 * 30 * pot.net_liq:
                audit(self.audit_path, "gross_rejected", underlying=idea.underlying,
                      order=order_summary(resolved), est_gross=round(_est_gross), cap=round(30 * pot.net_liq))
                _cap_rej("gross", f"capital at risk ${_est_gross:,.0f} exceeds 30x-NetLiq cap",
                         resolved=resolved, gate=plan.gate)
                approval.post_proposal(self.slack_token, self.slack_channel,
                    f":no_entry: Skipped *{idea.underlying}* {order_summary(resolved)} — capital at risk "
                    f"${_est_gross:,.0f} exceeds the 30x-NetLiq cap (${30*pot.net_liq:,.0f}). "
                    f"Too large for this ${pot.net_liq:,.0f} pot.")
                continue













            _research_blocker = getattr(self, "_research_degraded", None)
            if not (self._price_stats or {}).get(idea.underlying):
                _research_blocker = (_research_blocker or
                                     "daily price history unavailable for %s" % idea.underlying)
            _auto_blockers = autonomous_entry_blockers(
                idea.underlying, approved_names=self.approved_names, limits=self.limits,
                research_degraded=_research_blocker)
            _withheld_banner = ""
            if self.auto_approve_within_gates and _auto_blockers:
                audit(self.audit_path, "auto_approve_withheld", underlying=idea.underlying,
                      decision_id=resolved.decision_id, reasons=list(_auto_blockers))
                _withheld_banner = (
                    ":lock: *Auto-approval withheld — your tap is required.*\n"
                    + "\n".join("• " + _r for _r in _auto_blockers) + "\n")
            _auto_authority_gate = autonomous_execution_gate(
                enabled=self.auto_approve_within_gates,
                blockers=_auto_blockers,
                gate=plan.gate,
                capital_at_risk_usd=_est_gross,
            )
            _auto_ok = _auto_authority_gate.allowed
            if _auto_ok:


                approval.post_proposal(
                    self.slack_token, self.slack_channel,
                    ":robot_face: *AUTO-APPROVED (within risk gates)* — submitting now. "
                    "_This is a receipt; no approval is being awaited._\n"
                    + approval.format_decision_notice(
                        msg, resolved.decision_id, _approval_ttl // 60 or 1,
                        approval_required=False),
                    seed_reactions=False)
                audit(self.audit_path, "auto_approved", underlying=idea.underlying,
                      order=order_summary(resolved), decision_id=resolved.decision_id,
                      est_gross=round(_est_gross), per_trade_cap=round(
                          float(getattr(plan.gate, "per_trade_cap", 0) or 0)))
                _posted_at = time.monotonic()
                decision = "approve"
            else:
                ts = approval.post_proposal(self.slack_token, self.slack_channel,
                                            _withheld_banner + _manual_msg)
                if not ts:
                    audit(self.audit_path, "slack_post_failed", underlying=idea.underlying)
                    continue



                _posted_at = time.monotonic()
                decision = await asyncio.to_thread(
                    approval.await_approval, self.slack_token, self.slack_channel, ts,
                    self.approver_ids, _approval_ttl)
                audit(self.audit_path, "approval", underlying=idea.underlying,
                      order=order_summary(resolved), decision=decision,
                      decision_id=resolved.decision_id)
            if decision != "approve":
                _cap_rej("approval", f"human decision: {decision}", resolved=resolved, gate=plan.gate)
                continue

            _age = entry_safety.approval_expired(_posted_at, ttl_seconds=_approval_ttl)
            if not _age.allowed:
                audit(self.audit_path, "approval_expired", underlying=idea.underlying,
                      decision_id=resolved.decision_id, reasons=_age.reasons)
                continue
            _binding_approval_at = _posted_at




            fresh, _pot2, _final_reasons = await self._refresh_approved_entry(
                idea, resolved, baseline)
            if _final_reasons or fresh is None:
                audit(self.audit_path, "final_entry_gate_blocked", underlying=idea.underlying,
                      decision_id=resolved.decision_id, reasons=_final_reasons)
                approval.post_proposal(self.slack_token, self.slack_channel,
                    f":no_entry: Approved *{idea.underlying}* was NOT submitted — final hard gate: "
                    + "; ".join(_final_reasons or ("fresh order unavailable",)))
                continue
            _earnings_review_ok, _earnings_reviewed = await reconsider_new_same_day_earnings(
                endpoint=self.endpoint, model=self.model, market_context=context, idea=idea,
                previous=resolved, fresh=fresh, slack_token=self.slack_token,
                slack_channel=self.slack_channel, audit_path=self.audit_path,
                decision_id=resolved.decision_id)
            if not _earnings_review_ok:
                continue
            if _earnings_reviewed:


                fresh_after_review, _pot_review, _review_refresh_reasons = (
                    await self._refresh_approved_entry(idea, fresh, baseline))
                if _review_refresh_reasons or fresh_after_review is None:
                    audit(self.audit_path, "earnings_reconsideration_refresh_blocked",
                          underlying=idea.underlying, decision_id=resolved.decision_id,
                          reasons=_review_refresh_reasons)
                    approval.post_proposal(
                        self.slack_token, self.slack_channel,
                        f":no_entry: *{idea.underlying}* passed the same-day earnings review but "
                        "was NOT submitted — the required post-model NBBO/risk refresh failed: "
                        + "; ".join(_review_refresh_reasons or ("fresh order unavailable",)),
                        seed_reactions=False)
                    continue
                fresh = fresh_after_review
                if (getattr(fresh, "earnings_day_warning", False)
                        and not earnings_reconsideration_receipt_valid(fresh)):


                    _review_ok2, _reviewed_again = await reconsider_new_same_day_earnings(
                        endpoint=self.endpoint, model=self.model, market_context=context,
                        idea=idea, previous=fresh, fresh=fresh,
                        slack_token=self.slack_token, slack_channel=self.slack_channel,
                        audit_path=self.audit_path, decision_id=resolved.decision_id)
                    if not _review_ok2 or not _reviewed_again:
                        continue
                    fresh_after_review2, _pot_review2, _review_refresh_reasons2 = (
                        await self._refresh_approved_entry(idea, fresh, baseline))
                    if _review_refresh_reasons2 or fresh_after_review2 is None:
                        audit(self.audit_path, "earnings_reconsideration_refresh_blocked",
                              underlying=idea.underlying,
                              decision_id=resolved.decision_id,
                              reasons=_review_refresh_reasons2)
                        continue
                    fresh = fresh_after_review2
                    if (getattr(fresh, "earnings_day_warning", False)
                            and not earnings_reconsideration_receipt_valid(fresh)):
                        audit(self.audit_path, "earnings_reconsideration_churn_blocked",
                              underlying=idea.underlying,
                              decision_id=resolved.decision_id,
                              order=order_summary(fresh))
                        approval.post_proposal(
                            self.slack_token, self.slack_channel,
                            f":no_entry: *{idea.underlying}* kept moving materially across two "
                            "same-day earnings reviews — nothing submitted; generate a fresh "
                            "decision.", seed_reactions=False)
                        continue


            _changes = (credit_material_changes(resolved, fresh) if is_credit(resolved)
                        else entry_safety.material_changes(resolved, fresh))
            _earnings_warning_was_shown = any((
                bool(getattr(resolved, "earnings_day_warning", False)),
                bool(getattr(fresh, "earnings_reconsidered", False)),
            ))
            if _changes and _auto_ok:





                approval.post_proposal(
                    self.slack_token, self.slack_channel,
                    ":robot_face: *AUTO-APPROVED TERMS REFRESHED* — every hard gate passed; "
                    "submitting the refreshed exact order without waiting for approval.\n"
                    f"`{order_summary(fresh)}`\nChanged: {'; '.join(_changes)}\n"
                    f"_Decision ID: `{resolved.decision_id}`_",
                    seed_reactions=False)
                audit(self.audit_path, "auto_approved_terms_refreshed",
                      underlying=idea.underlying, decision_id=resolved.decision_id,
                      order=order_summary(fresh), changes=_changes)
            elif _changes:
                _earn_rewarning = (
                    ":rotating_light: *EARNINGS TODAY — ENTRY WARNING*\n"
                    f"{fresh.earnings_warn}\n\n"
                    if getattr(fresh, "earnings_day_warning", False) else "")
                _remsg = (_earn_rewarning
                           + f":repeat: *Reapproval required — {idea.underlying}*\n"
                           + approval.model_attribution_line(
                               resolved.model_identity, resolved.model_identity_source)
                           + f"Refreshed order: `{order_summary(fresh)}`\n"
                           + (f"Executable SELL credit: *${credit_executable_price(fresh):.2f}* "
                              f"(collateral ${capital_committed(fresh):,.0f})\n"
                              if is_credit(fresh) else
                              f"Executable BUY limit: *${entry_safety.executable_price(fresh):.2f}*\n")
                           +
                           f"Changed: {'; '.join(_changes)}\n"
                           f":point_down: Approve again within {_approval_ttl // 60 or 1} minutes.\n"
                           f"_Decision ID: `{resolved.decision_id}`, revision 1_")
                _rts = approval.post_proposal(self.slack_token, self.slack_channel, _remsg)
                if not _rts:
                    audit(self.audit_path, "reapproval_post_failed", decision_id=resolved.decision_id)
                    continue
                if getattr(fresh, "earnings_day_warning", False):
                    _earnings_warning_was_shown = True
                _reposted_at = time.monotonic()
                _rdecision = await asyncio.to_thread(
                    approval.await_approval, self.slack_token, self.slack_channel, _rts,
                    self.approver_ids, _approval_ttl)
                audit(self.audit_path, "reapproval", underlying=idea.underlying,
                      decision_id=resolved.decision_id, decision=_rdecision, changes=_changes)
                if _rdecision != "approve" or not entry_safety.approval_expired(
                        _reposted_at, ttl_seconds=_approval_ttl).allowed:
                    continue
                _binding_approval_at = _reposted_at
                fresh2, _pot3, _recheck_reasons = await self._refresh_approved_entry(
                    idea, fresh, baseline)
                if _recheck_reasons or fresh2 is None:
                    audit(self.audit_path, "reapproval_gate_blocked", underlying=idea.underlying,
                          decision_id=resolved.decision_id, reasons=_recheck_reasons)
                    continue
                _earnings_review_ok2, _earnings_reviewed2 = (
                    await reconsider_new_same_day_earnings(
                        endpoint=self.endpoint, model=self.model, market_context=context,
                        idea=idea, previous=fresh, fresh=fresh2,
                        slack_token=self.slack_token, slack_channel=self.slack_channel,
                        audit_path=self.audit_path, decision_id=resolved.decision_id))
                if not _earnings_review_ok2:
                    continue
                if _earnings_reviewed2:
                    fresh3, _pot_review2, _review_refresh_reasons2 = (
                        await self._refresh_approved_entry(idea, fresh2, baseline))
                    if _review_refresh_reasons2 or fresh3 is None:
                        audit(self.audit_path, "earnings_reconsideration_refresh_blocked",
                              underlying=idea.underlying,
                              decision_id=resolved.decision_id,
                              reasons=_review_refresh_reasons2)
                        approval.post_proposal(
                            self.slack_token, self.slack_channel,
                            f":no_entry: *{idea.underlying}* passed the same-day earnings review "
                            "but was NOT submitted — the required post-model NBBO/risk refresh "
                            "failed: " + "; ".join(
                                _review_refresh_reasons2 or ("fresh order unavailable",)),
                            seed_reactions=False)
                        continue
                    fresh2 = fresh3
                    _earnings_warning_was_shown = True
                    if (getattr(fresh2, "earnings_day_warning", False)
                            and not earnings_reconsideration_receipt_valid(fresh2)):
                        audit(self.audit_path, "earnings_reconsideration_churn_blocked",
                              underlying=idea.underlying,
                              decision_id=resolved.decision_id,
                              order=order_summary(fresh2))
                        approval.post_proposal(
                            self.slack_token, self.slack_channel,
                            f":no_entry: *{idea.underlying}* moved materially after its final "
                            "same-day earnings review — nothing submitted; generate a fresh "
                            "decision.", seed_reactions=False)
                        continue
                _changes2 = (credit_material_changes(fresh, fresh2) if is_credit(fresh)
                             else entry_safety.material_changes(fresh, fresh2))
                if _changes2:
                    audit(self.audit_path, "reapproval_churn_blocked", underlying=idea.underlying,
                          decision_id=resolved.decision_id, changes=_changes2)
                    approval.post_proposal(self.slack_token, self.slack_channel,
                        f":no_entry: *{idea.underlying}* changed again after reapproval — nothing submitted.")
                    continue
                fresh = fresh2
                fresh.decision_revision = 1
            if not _auto_ok:



                _final_age = entry_safety.approval_expired(
                    _binding_approval_at, ttl_seconds=_approval_ttl)
                if not _final_age.allowed:
                    audit(self.audit_path, "approval_expired_pre_submit",
                          underlying=idea.underlying,
                          decision_id=resolved.decision_id,
                          reasons=_final_age.reasons)
                    approval.post_proposal(
                        self.slack_token, self.slack_channel,
                        f":hourglass: Approval expired during final review — "
                        f"*{idea.underlying}* was NOT submitted. Generate a fresh decision.",
                        seed_reactions=False)
                    continue
            if (getattr(fresh, "earnings_reconsidered", False)
                    and not earnings_reconsideration_receipt_valid(fresh)):
                audit(self.audit_path, "earnings_reconsideration_receipt_invalid",
                      underlying=idea.underlying, decision_id=resolved.decision_id,
                      order=order_summary(fresh))
                approval.post_proposal(
                    self.slack_token, self.slack_channel,
                    f":no_entry: *{idea.underlying}* was NOT submitted — its same-day earnings "
                    "review no longer matches the executable order.", seed_reactions=False)
                continue
            if (getattr(fresh, "earnings_day_warning", False)
                    and not _earnings_warning_was_shown):



                _notice_ts = approval.post_proposal(
                    self.slack_token, self.slack_channel,
                    ":rotating_light: *EARNINGS TODAY — FINAL ENTRY WARNING*\n"
                    f"{fresh.earnings_warn}\n"
                    "Final pre-submit refresh found the event; proceeding under the existing "
                    "within-risk-gates authority. No approval is being awaited.\n"
                    f"_Decision ID: `{resolved.decision_id}`_",
                    seed_reactions=False)
                audit(self.audit_path, "earnings_same_day_warning_notice",
                      phase="pre_submit", underlying=idea.underlying,
                      decision_id=resolved.decision_id,
                      earnings_date=fresh.earnings_date,
                      notice_posted=bool(_notice_ts))
            resolved = fresh
            _submission_authority = (
                entry_safety.EntrySubmissionAuthority.autonomous()
                if _auto_ok else
                entry_safety.EntrySubmissionAuthority.human(
                    _binding_approval_at, ttl_seconds=_approval_ttl)
            )
            resolved.execution_authority = _submission_authority.mode


            _marker_now = self._entry_markers_clear()
            if not _marker_now.allowed:
                audit(self.audit_path, "marker_blocked_submit", underlying=idea.underlying,
                      decision_id=resolved.decision_id, reasons=_marker_now.reasons)
                continue
            try:
                status, reasons = await self._submit_order(
                    resolved, submission_authority=_submission_authority)
                if status in ("Cancelled", "ApiCancelled", "Inactive"):
                    reason = reasons[-1] if reasons else f"order {status}"
                    audit(self.audit_path, "rejected", underlying=idea.underlying,
                          order=order_summary(resolved), status=status, reason=reason)
                    _cap_rej("ibkr_rejected", reason, resolved=resolved, gate=plan.gate)
                    approval.post_proposal(self.slack_token, self.slack_channel,
                        f":x: *Order REJECTED by IBKR* — {idea.underlying} {order_summary(resolved)} "
                        f"was NOT placed.\n{reason}")
                else:
                    audit(self.audit_path, "executed", underlying=idea.underlying,
                          order=order_summary(resolved), status=status,
                          decision_id=resolved.decision_id)





                    try:
                        trade_capture.capture_decision(
                            _ddir, source="trader", symbol=idea.underlying,
                            right=resolved.right, strike=resolved.strike, expiry=resolved.expiry,
                            structure=(CSP_STRUCTURE if is_credit(resolved) else
                               ("spread" if resolved.short_contract is not None else "single")),
                            con_id=getattr(resolved.contract, "conId", None),
                            chosen_idea=idea, candidates=ideas, raw_strategist=_raw_strategist, cot=_cot,
                            gate=plan.gate, regime=self._regime, market_context=context,
                            technical_card=self._price_stats,
                            construction={"tp_pct": resolved.tp_pct, "sl_pct": resolved.sl_pct,
                                          "dte": resolved.dte, "dte_adjusted": resolved.dte_adjusted,
                                          "qty": resolved.qty, "limit": resolved.limit,
                                          "short_strike": resolved.short_strike},
                            sizing={"per_trade_cap": plan.gate.per_trade_cap,
                                    "net_liq": pot.net_liq, "available_funds": pot.available_funds,
                                    "qty": resolved.qty, "limit": resolved.limit},
                            extra={"order": order_summary(resolved), "status": status,
                                   "decision_id": resolved.decision_id},
                            decision_id=resolved.decision_id,
                            revision=resolved.decision_revision, event="submitted",
                            model_identity=resolved.model_identity,
                            model_identity_source=getattr(
                                resolved, "model_identity_source", None),
                            final_contract=contract_snapshot(resolved),
                            order_ref=entry_safety.decision_order_ref(resolved.decision_id),
                            execution_authority=_submission_authority.mode,
                            human_action=(None if _auto_ok else {"action": "approve"}))
                    except Exception as _dce:
                        print(f"[WARN] capture_decision failed (continuing): {_dce}")


                    _resolved_cost = capital_committed(resolved)
                    _entry_legs = tuple(
                        int(getattr(_c, "conId")) for _c in
                        (resolved.contract, resolved.short_contract)
                        if _c is not None and int(getattr(_c, "conId", 0) or 0) > 0)
                    entry_positions.append(OpenPosition(
                        idea.underlying, _resolved_cost, idea.is_index,
                        is_credit=is_credit(resolved),
                        primary_con_id=(_entry_legs[0] if _entry_legs else None),
                        leg_con_ids=_entry_legs, contracts=abs(int(resolved.qty)),
                        campaign_id=f"pending:{resolved.decision_id}"))
                    if is_credit(resolved):





                        self._cycle_credit_collateral = round(
                            float(getattr(self, "_cycle_credit_collateral", 0.0) or 0.0)
                            + _resolved_cost, 2)
                        audit(self.audit_path, "credit_collateral_reserved",
                              underlying=idea.underlying, decision_id=resolved.decision_id,
                              collateral=_resolved_cost,
                              cycle_total=self._cycle_credit_collateral)
                    else:
                        _budget_key = f"decision:{resolved.decision_id}"
                        _budget_value = (_resolved_cost, int(resolved.dte or 0))
                        _budget_items[_budget_key] = _budget_value
                        self._same_cycle_budget_items[_budget_key] = _budget_value
                        _open_book = list(_budget_items.values())



                    _orders_this_cycle += 1
                    try:




                        if not record_entry_open(_sm, _throttle_store, today, 1, _resolved_cost):
                            print("[WARN] entry throttle counter NOT persisted (lock busy or I/O "
                                  "error) -- the daily cap will under-count after a restart")
                            audit(self.audit_path, "entry_throttle_persist_failed",
                                  underlying=idea.underlying, date=today, cost=_resolved_cost)
                    except Exception as _ue:
                        print(f"[WARN] entry daily-stats update failed (continuing): {_ue}")
            except Exception as e:
                audit(self.audit_path, "submit_error", underlying=idea.underlying, error=str(e))


    async def _refresh_approved_entry(self, idea, original: ResolvedOrder, baseline: float):
        """Public API contract; production-derived narrative omitted."""
        reasons = list(self._entry_markers_clear().reasons)
        fresh = None
        pot = None
        try:
            pot = await get_pot_snapshot(self.ib_conn.ib)
            reasons.extend(entry_safety.account_snapshot_valid(pot).reasons)
            open_positions = await self._open_positions()
            gate = plan_idea(
                idea, net_liq=pot.net_liq, available_funds=pot.available_funds,
                positions=open_positions, baseline=baseline,
                approved_names=self.approved_names, limits=self.limits,
                regime=self._regime).gate
            _stage_b_bound = getattr(idea, "_stage_b_binding", None) is not None
            if not gate.approved and not _stage_b_bound:
                reasons.extend(gate.reasons)
            raw_positions = await self.ib_conn.get_positions()
            working_orders = await self.ib_conn.ib.reqAllOpenOrdersAsync()


            try:


                days = await asyncio.to_thread(
                    research.days_to_earnings, idea.underlying, force_refresh=True)
            except Exception as exc:
                days = None
                audit(self.audit_path, "earnings_refresh_unavailable",
                      underlying=idea.underlying, error=str(exc))
            if days is None:
                if not entry_safety.is_no_earnings_etf(idea.underlying):
                    reasons.append("earnings date unavailable at approval time")

            fresh = await self._resolve_order(
                idea, gate.per_trade_cap,
                net_liq=pot.net_liq, available_funds=pot.available_funds)
            if fresh is None:
                reasons.append("fresh contract/NBBO resolution returned no order")
            else:
                if _stage_b_bound:


                    gate = plan_idea(
                        idea, net_liq=pot.net_liq, available_funds=pot.available_funds,
                        positions=open_positions, baseline=baseline,
                        approved_names=self.approved_names, limits=self.limits,
                        regime=self._regime).gate
                    if not gate.approved:
                        reasons.extend(gate.reasons)
                fresh.decision_id = original.decision_id
                fresh.decision_revision = original.decision_revision
                fresh.model_identity = original.model_identity
                fresh.model_identity_source = getattr(original, "model_identity_source", None)
                fresh.conviction = original.conviction
                fresh.intended_hold_days = getattr(original, "intended_hold_days", None)
                fresh.earnings_date = None
                fresh.earnings_warn = ""
                fresh.earnings_unchecked = days is None
                fresh.earnings_day_warning = False
                fresh.earnings_reconsidered = bool(
                    getattr(original, "earnings_reconsidered", False))
                fresh.earnings_reconsideration_decision = getattr(
                    original, "earnings_reconsideration_decision", None)
                fresh.earnings_reconsideration_phase = getattr(
                    original, "earnings_reconsideration_phase", None)
                fresh.earnings_reconsideration_reason = getattr(
                    original, "earnings_reconsideration_reason", "")
                fresh.earnings_reconsideration_model_identity = getattr(
                    original, "earnings_reconsideration_model_identity", None)
                fresh.earnings_reconsideration_model_identity_source = getattr(
                    original, "earnings_reconsideration_model_identity_source", None)
                fresh.earnings_reconsideration_order_snapshot = getattr(
                    original, "earnings_reconsideration_order_snapshot", None)
                fresh.earnings_reconsideration_order_sha256 = getattr(
                    original, "earnings_reconsideration_order_sha256", None)
                fresh.thesis = original.thesis
                fresh.tp_pct = original.tp_pct
                fresh.sl_pct = original.sl_pct
                fresh.technical_card = getattr(original, "technical_card", None)
                reasons.extend(entry_safety.nbbo_valid(fresh).reasons)


                _ok_rstruct, _why_rstruct = debit_structure_ok(idea)
                if not _ok_rstruct:
                    reasons.append(_why_rstruct)


                if is_credit(fresh):
                    ok_fresh, why_fresh = credit_structure_ok(idea)
                    if not ok_fresh:
                        reasons.append(why_fresh)
                    if str(fresh.right).upper()[:1] != "P" or fresh.short_contract is not None:
                        reasons.append("NAKED-SHORT REFUSED: refreshed credit order is not a "
                                       "single-leg cash-secured put")

                    cap_ok, cap_detail = await self._credit_capacity(fresh, pot=pot)
                    if not cap_ok.allowed:
                        reasons.extend(cap_ok.reasons)
                budget_items = construction.open_book_items(
                    raw_positions, self.journal_path, working_orders)
                budget_items.update(getattr(self, "_same_cycle_budget_items", {}) or {})



                ok_budget, budget_reasons = construction.check_budget(
                    0.0 if is_credit(fresh)
                    else entry_safety.executable_price(fresh) * 100 * fresh.qty,
                    0 if is_credit(fresh) else fresh.dte, pot.net_liq,
                    list(budget_items.values()), self.construction)
                if not ok_budget:
                    reasons.extend(budget_reasons)
                if days is not None:
                    entry_date = datetime.now(timezone.utc).date()
                    earnings_date = entry_date + timedelta(days=days)
                    earn_ok, earn_reason = construction.earnings_ok(
                        entry_date, fresh.expiry, earnings_date, self.construction,
                        hold_days=getattr(idea, "intended_hold_days", None))
                    fresh.earnings_date = earnings_date.isoformat()
                    fresh.earnings_day_warning = (earnings_date == entry_date)
                    fresh.earnings_warn = earn_reason if earn_ok else ""
                    if not earn_ok:
                        reasons.append(earn_reason)
                    elif earn_reason:
                        audit(self.audit_path, ("earnings_same_day_warning"
                              if fresh.earnings_day_warning else "earnings_overlap_disclosed"),
                              phase="post_approval", underlying=idea.underlying,
                              decision_id=original.decision_id,
                              order=order_summary(fresh),
                              earnings_date=fresh.earnings_date, reason=earn_reason)
        except Exception as exc:
            reasons.append(f"fresh account/contract/NBBO/risk/earnings gate failed: {exc}")
        return fresh, pot, tuple(dict.fromkeys(str(r) for r in reasons if r))

    def _drain_reload_ideas(self, today: str) -> List[TradeIdea]:
        """Public API contract; production-derived narrative omitted."""
        q = reload_queue.ReloadQueue(reload_queue.queue_path(self.journal_path))
        ready, summary = q.drain(today=today, max_per_name=self.reload_max_per_name_per_day)
        if summary.get("expired") or summary.get("capped"):
            audit(self.audit_path, "reload_tickets_dropped",
                  expired=summary.get("expired", 0), capped=summary.get("capped", 0))
        ideas: List[TradeIdea] = []
        for t in ready:
            right = (t.get("right") or "C").upper()
            direction = "bullish" if right == "C" else "bearish"

            structure = "debit spread" if t.get("structure") == "spread" else "long option"




            _dt = t.get("dte_target")
            if _dt is None:
                _dt = getattr(self.construction, "min_dte", None)
            dte = int(30 if _dt is None else _dt)


            _od = t.get("original_debit")
            budget = float(_od) if (_od and float(_od) > 0) else 1e12
            conv = t.get("reload_conviction")
            try:
                conv_int = int(round(float(conv))) if conv is not None else 0
            except (TypeError, ValueError):
                conv_int = 0
            base_thesis = (t.get("thesis") or "").strip()
            thesis = (f"[RELOAD/continuation] {base_thesis}" if base_thesis else "[RELOAD/continuation]")
            idea = TradeIdea(underlying=str(t["symbol"]), is_index=bool(t.get("is_index")),
                             direction=direction, structure=structure, target_dte=dte,
                             target_delta=0.0, est_debit_usd=budget, conviction=conv_int,
                             thesis=thesis)

            idea.is_reload = True
            idea.reload_conviction = conv








            idea.reload_expected_continuation_pct = self.reload_expected_continuation_pct
            idea.reload_realized_pnl = t.get("realized_pnl")
            idea.reload_original_debit = t.get("original_debit")






            _ok_reload, _why_reload = debit_structure_ok(idea)
            if not _ok_reload:
                audit(self.audit_path, "reload_structure_rejected", underlying=idea.underlying,
                      structure=idea.structure, direction=idea.direction, reason=_why_reload)
                continue
            ideas.append(idea)
        return ideas

    async def _resolve_credit_order(self, idea: TradeIdea,
                                    per_trade_cap: float) -> Optional[ResolvedOrder]:
        """Public API contract; production-derived narrative omitted."""
        from exitmgr.ibkr import Option, Stock, pick_chain, underlying_price
        from exitmgr.market import usable_price


        ok_struct, why_struct = credit_structure_ok(idea)
        if not ok_struct:
            audit(self.audit_path, "credit_structure_rejected",
                  underlying=idea.underlying, structure=getattr(idea, "structure", None),
                  side=getattr(idea, "side", None), reason=why_struct)
            return None
        ib = self.ib_conn.ib
        cons = self.construction
        strike = float(getattr(idea, "strike"))
        stk = (await asyncio.wait_for(ib.qualifyContractsAsync(Stock(idea.underlying, "SMART", "USD")), _IB_CALL_TIMEOUT_S))[0]
        params = await asyncio.wait_for(ib.reqSecDefOptParamsAsync(idea.underlying, "", "STK", stk.conId), _IB_CALL_TIMEOUT_S)
        if not params:
            return None
        p = pick_chain(params, idea.underlying)
        if p is None:
            return None






        _bounds = construction.dte_bounds_for_side(CREDIT_SIDE, cons)
        credit_min, credit_max = int(_bounds.min_dte), int(_bounds.max_dte or CREDIT_MAX_DTE_DEFAULT)
        expiry, chosen_dte, dte_adjusted = construction.pick_expiry_for_side(
            p.expirations, idea.target_dte, side=CREDIT_SIDE, cons=cons)
        if expiry is None:
            audit(self.audit_path, "construction_rejected", underlying=idea.underlying,
                  reason=f"no expiry inside the {credit_min}-{credit_max} DTE credit window")
            return None
        if chosen_dte is not None and int(chosen_dte) > credit_max:


            audit(self.audit_path, "construction_rejected", underlying=idea.underlying,
                  reason=f"nearest credit expiry {chosen_dte} DTE exceeds the "
                         f"{credit_max}-DTE credit ceiling")
            return None
        if dte_adjusted:
            audit(self.audit_path, "dte_adjusted", underlying=idea.underlying, side=CREDIT_SIDE,
                  requested_dte=idea.target_dte, adjusted_dte=chosen_dte, min_dte=credit_min)
        spot = await underlying_price(ib, stk)

        qualified = await asyncio.wait_for(ib.qualifyContractsAsync(
            Option(idea.underlying, expiry, strike, "P", "SMART")), _IB_CALL_TIMEOUT_S)
        contract = next((c for c in (qualified or []) if getattr(c, "conId", None)), None)
        if contract is None:
            audit(self.audit_path, "construction_rejected", underlying=idea.underlying,
                  reason=f"could not qualify {idea.underlying} {expiry} {strike:g}P")
            return None
        if abs(float(getattr(contract, "strike", 0.0) or 0.0) - strike) > 1e-6 \
                or str(getattr(contract, "right", "") or "").upper()[:1] != "P":

            audit(self.audit_path, "construction_rejected", underlying=idea.underlying,
                  reason="qualified contract does not match the requested put strike")
            return None
        tickers = await asyncio.wait_for(ib.reqTickersAsync(contract), _IB_CALL_TIMEOUT_S)
        tk = next(iter(tickers or []), None)
        if tk is None:
            return None
        bid = _fnum(getattr(tk, "bid", None), None)
        ask = _fnum(getattr(tk, "ask", None), None)
        if not (usable_price(bid) and usable_price(ask)):

            audit(self.audit_path, "construction_rejected", underlying=idea.underlying,
                  reason="no two-sided NBBO on the put -- cannot price a sell-to-open")
            return None


        credit_per_share = round(float(bid), 2)
        if credit_per_share <= 0:
            return None



        qty = size_within_cap(strike * 100.0,
                              _fnum(getattr(idea, "collateral_usd", 0.0), 0.0) or 0.0,
                              per_trade_cap)
        if qty is None:
            audit(self.audit_path, "construction_rejected", underlying=idea.underlying,
                  reason=f"one cash-secured put (collateral ${strike*100:,.0f}) exceeds the "
                         f"per-trade cap ${per_trade_cap:,.0f}")
            return None
        collateral = required_collateral(strike, qty)
        if collateral is None:
            return None
        net_credit = round(credit_per_share * 100.0 * qty, 2)
        max_loss = round(collateral - net_credit, 2)
        if max_loss <= 0:
            return None
        g = getattr(tk, "modelGreeks", None) or getattr(tk, "lastGreeks", None)

        def _f(x):
            v = _fnum(x, 0.0)
            return v if v is not None else 0.0
        _d, _th = _f(getattr(g, "delta", None)) if g else 0.0, _f(getattr(g, "theta", None)) if g else 0.0
        _ga, _ve = _f(getattr(g, "gamma", None)) if g else 0.0, _f(getattr(g, "vega", None)) if g else 0.0
        _iv = _f(getattr(g, "impliedVol", None)) if g else 0.0
        _mid = (float(bid) + float(ask)) / 2
        return ResolvedOrder(
            idea.underlying, "P", expiry, strike, qty, credit_per_share, contract,
            spot=float(spot or 0.0), entry_delta=abs(_d), entry_iv=_iv,
            dte=int(chosen_dte or 0), dte_adjusted=bool(dte_adjusted),
            quote_observed_at=time.monotonic(),
            entry_gamma=_ga, entry_theta=_th, entry_vega=_ve,
            entry_bid=float(bid), entry_ask=float(ask),
            entry_spread_pct=(round((float(ask) - float(bid)) / _mid * 100, 2) if _mid > 0 else 0.0),

            net_delta=-abs(_d), net_theta=-_th, net_gamma=-_ga, net_vega=-_ve,
            side=CREDIT_SIDE, collateral_usd=collateral, net_credit_usd=net_credit,
            credit_max_loss_usd=max_loss)

    async def _resolve_order(self, idea: TradeIdea, per_trade_cap: float, *,
                             net_liq: Optional[float] = None,
                             available_funds: Optional[float] = None) -> Optional[ResolvedOrder]:
        """Public API contract; production-derived narrative omitted."""


        binding = getattr(idea, "_stage_b_binding", None)
        intent = getattr(idea, "_stage_a_intent", None)
        if binding is not None or intent is not None:
            if not isinstance(binding, CandidateBinding) or not isinstance(intent, StageAIntent):
                return None
            refreshed = binding
            if net_liq is not None and available_funds is not None:
                try:
                    deployed_credit = (await self._deployed_collateral()
                                       if intent.side == CREDIT_SIDE else 0.0)
                    _rs = []
                    refreshed = await reprice_binding(
                        self.ib_conn.ib, binding, intent,
                        net_liq=net_liq, available_funds=available_funds,
                        cons=self.construction,
                        deployed_credit_usd=deployed_credit,
                        cash_buffer_pct=self.limits.cash_buffer_pct, reasons=_rs)
                except Exception as _re:
                    audit(self.audit_path, "reprice_failed",
                          underlying=getattr(intent, "underlying", None), error=str(_re)[:200])
                    return None
            if refreshed is None:


                audit(self.audit_path, "reprice_refused",
                      underlying=getattr(intent, "underlying", None),
                      reason="; ".join(_rs) if _rs else "unspecified")
                await self._notify_pipeline(
                    "quote_rejected", "; ".join(_rs) if _rs else "Fresh quote failed validation.",
                    symbol=getattr(intent, "underlying", "?"))
                return None

            if refreshed.candidate.candidate_id != binding.candidate.candidate_id:
                audit(self.audit_path, "reprice_identity_changed",
                      underlying=getattr(intent, "underlying", None))
                return None
            idea._stage_b_binding = refreshed
            resolved = refreshed.to_resolved_order(intent)
            if is_credit(resolved):
                idea.strike = resolved.strike
                idea.collateral_usd = resolved.collateral_usd
                idea.net_credit_usd = resolved.net_credit_usd
                idea.max_loss_usd = resolved.credit_max_loss_usd
            else:
                idea.est_debit_usd = round(
                    float(refreshed.candidate.one_contract_cost_usd) * int(resolved.qty), 2)
            return resolved



        if is_credit(idea):
            return await self._resolve_credit_order(idea, per_trade_cap)




        _ok_struct, _why_struct = debit_structure_ok(idea)
        if not _ok_struct:
            audit(self.audit_path, "debit_structure_rejected", underlying=idea.underlying,
                  structure=getattr(idea, "structure", None),
                  direction=getattr(idea, "direction", None), reason=_why_struct)
            return None
        from exitmgr.ibkr import (
            Option, Stock, option_contracts_for_expiry, pick_chain, strikes_near,
            underlying_price,
        )
        from exitmgr.market import usable_price
        ib = self.ib_conn.ib
        right = "C" if idea.direction == "bullish" else "P"
        stk = (await asyncio.wait_for(ib.qualifyContractsAsync(Stock(idea.underlying, "SMART", "USD")), _IB_CALL_TIMEOUT_S))[0]
        params = await asyncio.wait_for(ib.reqSecDefOptParamsAsync(idea.underlying, "", "STK", stk.conId), _IB_CALL_TIMEOUT_S)
        if not params:
            return None
        p = pick_chain(params, idea.underlying)
        if p is None:
            return None
        cons = self.construction
















        _open_exp = {}
        try:
            _dv_positions = await self.ib_conn.get_positions()
            _dv_orders = await self.ib_conn.ib.reqAllOpenOrdersAsync()
            _open_exp = construction.open_expiry_counts(
                _dv_positions, self.journal_path, _dv_orders)
        except Exception as _dve:
            print(f"[EXPIRY] crowding lookup unavailable for {idea.underlying} ({_dve}); "
                  f"placing without diversification")
        _choice = construction.pick_expiry_diversified(
            p.expirations, idea.target_dte, side="debit", cons=cons, open_expiries=_open_exp)
        expiry, chosen_dte, dte_adjusted = _choice.expiry, _choice.dte, _choice.adjusted
        if getattr(_choice, "shifted", False):

            print(f"[EXPIRY] {idea.underlying}: {_choice.reason}")
            audit(self.audit_path, "expiry_diversified", underlying=idea.underlying,
                  from_expiry=_choice.from_expiry, from_dte=_choice.from_dte,
                  to_expiry=_choice.expiry, to_dte=_choice.dte, reason=_choice.reason)
        if expiry is None:
            audit(self.audit_path, "construction_rejected", underlying=idea.underlying,
                  reason=f"no expiry >= {cons.min_dte} DTE available")
            return None
        if dte_adjusted:
            audit(self.audit_path, "dte_adjusted", underlying=idea.underlying,
                  requested_dte=idea.target_dte, adjusted_dte=chosen_dte, min_dte=cons.min_dte)
        spot = await underlying_price(ib, stk)
        qualified = await option_contracts_for_expiry(
            ib, idea.underlying, expiry, right, "SMART", spot,
            timeout_s=_IB_CALL_TIMEOUT_S,
            trading_class=getattr(p, "tradingClass", None),
            multiplier=getattr(p, "multiplier", None), currency="USD")
        if qualified is None:
            cands = [Option(idea.underlying, expiry, k, right, "SMART")
                     for k in strikes_near(p.strikes, spot)]
            qualified = await asyncio.wait_for(
                ib.qualifyContractsAsync(*cands), _IB_CALL_TIMEOUT_S)
        if not qualified:
            audit(self.audit_path, "construction_rejected", underlying=idea.underlying,
                  reason="no contracts exist for selected expiry/right")
            return None
        tickers = await asyncio.wait_for(ib.reqTickersAsync(*[c for c in qualified if getattr(c, "conId", None)]), _IB_CALL_TIMEOUT_S)


        tgt_delta = construction.effective_delta(idea.target_delta, cons)
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
            if abs(k_near - spot) <= cons.strike_near_spot_pct * spot:
                best = (by_strike[k_near][1], by_strike[k_near][0])
                best_bidask = quote_by_strike.get(k_near, (None, None))
        if not best:
            return None
        contract, mid = best
        atm_iv = getattr(best_greeks, "impliedVol", None) if best_greeks else None


        ok, why = construction.long_strike_ok(float(contract.strike), spot, right,
                                              chosen_dte, atm_iv, cons)
        if not ok:
            audit(self.audit_path, "construction_rejected", underlying=idea.underlying, reason=why)
            return None
        enrich = dict(spot=float(spot or 0.0),
                      entry_delta=float(abs(best_greeks.delta)) if (best_greeks and best_greeks.delta is not None) else 0.0,
                      entry_iv=float(atm_iv) if (atm_iv and atm_iv == atm_iv) else 0.0,
                      dte=int(chosen_dte), dte_adjusted=bool(dte_adjusted),
                      quote_observed_at=time.monotonic())


        try:
            def _f(x):
                try:
                    return float(x) if (x is not None and x == x) else 0.0
                except (TypeError, ValueError):
                    return 0.0
            g = best_greeks
            _d = _f(getattr(g, "delta", None)) if g else 0.0
            _th = _f(getattr(g, "theta", None)) if g else 0.0
            _ga = _f(getattr(g, "gamma", None)) if g else 0.0
            _ve = _f(getattr(g, "vega", None)) if g else 0.0
            _bid, _ask = best_bidask
            _bid, _ask = _f(_bid), _f(_ask)
            _spr = round((_ask - _bid) / mid * 100, 2) if (mid and _ask >= _bid > 0) else 0.0
            _single = "spread" not in (idea.structure or "").lower()
            enrich.update(
                entry_gamma=_ga, entry_theta=_th, entry_vega=_ve,
                entry_bid=_bid, entry_ask=_ask, entry_spread_pct=_spr,


                net_delta=(abs(_d) if _single else 0.0),
                net_theta=(_th if _single else 0.0),
                net_gamma=(_ga if _single else 0.0),
                net_vega=(_ve if _single else 0.0),
            )
        except Exception as _ge:
            print(f"[WARN] greeks/liquidity capture failed for {idea.underlying} (continuing): {_ge}")

        if "spread" in (idea.structure or "").lower():


            pick = pick_spread_short([(k, m) for k, (m, _) in by_strike.items()],
                                     float(contract.strike), mid, right, per_trade_cap,
                                     spot=spot, dte=chosen_dte, atm_iv=atm_iv, cons=cons)
            if not pick:
                audit(self.audit_path, "construction_rejected", underlying=idea.underlying,
                      reason="no sane affordable short leg (structure-sanity + cap)")
                return None
            short_strike, net = pick
            short_contract = by_strike[short_strike][1]



















            try:
                from exitmgr import atr_levels as _alv_e
                _iv_e = float(atm_iv or enrich.get("entry_iv") or 0.0)
                _nd_e = _alv_e.net_structure_delta(
                    float(spot or 0.0), float(contract.strike), float(short_strike),
                    int(chosen_dte or 0), _iv_e, right=right)
                if _nd_e:
                    enrich["net_delta"] = float(_nd_e)
            except Exception as _nde:
                print(f"[ATR] {idea.underlying}: spread net-delta unavailable ({_nde})")



            try:
                _lb, _la = (float(x) for x in best_bidask)
                _sb, _sa = (float(x) for x in quote_by_strike[short_strike])
                enrich["entry_bid"] = round(_lb - _sa, 4)
                enrich["entry_ask"] = round(_la - _sb, 4)
                _net_mid = (enrich["entry_bid"] + enrich["entry_ask"]) / 2
                enrich["entry_spread_pct"] = (
                    round((enrich["entry_ask"] - enrich["entry_bid"]) / _net_mid * 100, 2)
                    if _net_mid > 0 and enrich["entry_ask"] >= enrich["entry_bid"] else 0.0)
            except (TypeError, ValueError, KeyError):
                enrich["entry_bid"] = enrich["entry_ask"] = 0.0
                enrich["entry_spread_pct"] = 0.0


            qty = size_within_cap(net * 100, idea.est_debit_usd, per_trade_cap)
            if qty is None:
                audit(self.audit_path, "construction_rejected", underlying=idea.underlying,
                      reason=f"one spread (${net*100:,.0f}) exceeds per-trade cap ${per_trade_cap:,.0f}")
                return None
            return ResolvedOrder(idea.underlying, right, expiry, float(contract.strike),
                                 qty, net, contract,
                                 short_strike=short_strike, short_contract=short_contract,
                                 structure=str(getattr(idea, "structure", "") or ""),
                                 **enrich)



        qty = size_within_cap(mid * 100, idea.est_debit_usd, per_trade_cap)
        if qty is None:
            audit(self.audit_path, "construction_rejected", underlying=idea.underlying,
                  reason=f"one contract (${mid*100:,.0f}) exceeds per-trade cap ${per_trade_cap:,.0f}")
            return None
        return ResolvedOrder(idea.underlying, right, expiry, float(contract.strike),
                             qty, round(mid, 2), contract,
                             structure=str(getattr(idea, "structure", "") or ""), **enrich)

    async def _submit_order(self, r: ResolvedOrder, *, submission_authority):
        """Public API contract; production-derived narrative omitted."""



        if is_credit(r):
            return await self._submit_order_unlocked(
                r, submission_authority=submission_authority)
        lock = self.broker_order_lock
        if lock is None:
            return await self._submit_order_unlocked(
                r, submission_authority=submission_authority)
        async with lock:
            return await self._submit_order_unlocked(
                r, submission_authority=submission_authority)

    async def _submit_order_unlocked(self, r: ResolvedOrder, *, submission_authority):
        """Public API contract; production-derived narrative omitted."""
        from exitmgr.ibkr import Order
        _conflict_reader = getattr(self.exit_manager, "campaign_conflict_symbols", None)
        try:
            _conflicts = set(_conflict_reader()) if _conflict_reader is not None else set()
        except Exception as exc:
            raise RuntimeError(
                f"campaign-conflict admission fence unreadable at submit: {exc}") from exc
        if str(r.underlying).upper() in _conflicts:
            raise RuntimeError(
                f"admission refused: {r.underlying} has a quarantined campaign-journal "
                "authority conflict; new/scale-in exposure is blocked")
        authority_gate = self._short_option_entry_authority(r)
        if not authority_gate.allowed:
            raise RuntimeError(
                "short-option authority blocks submit: " + "; ".join(authority_gate.reasons))
        marker_gate = self._entry_markers_clear()
        if not marker_gate.allowed:
            raise RuntimeError("entry markers block submit: " + "; ".join(marker_gate.reasons))
        quote_gate = entry_safety.nbbo_valid(r)
        if not quote_gate.allowed:
            raise RuntimeError("fresh NBBO blocks submit: " + "; ".join(quote_gate.reasons))
        submission_gate = entry_safety.submission_authority_valid(submission_authority)
        if not submission_gate.allowed:
            raise entry_safety.ApprovalExpiredAtSubmit(
                "; ".join(submission_gate.reasons))





        if is_credit(r):


            if str(r.right).upper()[:1] != "P":
                raise RuntimeError(
                    f"NAKED-SHORT REFUSED at submit: cannot sell a {r.right!r} to open -- only a "
                    f"cash-secured put may ever be sold")
            if r.short_contract is not None or (r.short_strike or 0):
                raise RuntimeError(
                    "NAKED-SHORT REFUSED at submit: a credit order must be a single-leg "
                    "cash-secured put, not a multi-leg short structure")
            if int(r.qty) < 1:
                raise RuntimeError("credit order has no positive quantity")


            required = required_collateral(r.strike, r.qty)
            if required is None:
                raise RuntimeError("credit order has invalid collateral inputs")
            if abs(float(required) - capital_committed(r)) > 0.01:
                raise RuntimeError(
                    f"collateral mismatch: recomputed ${required:,.2f} != approved "
                    f"${capital_committed(r):,.2f}")
            _lmt_credit = credit_executable_price(r)
            if not (_lmt_credit > 0):
                raise RuntimeError("credit order has no positive executable credit")
            order = Order(action="SELL", orderType="LMT", lmtPrice=_lmt_credit,
                          totalQuantity=r.qty, tif="DAY")
            order.orderRef = entry_safety.decision_order_ref(r.decision_id)




            _credit_obs = await observe_for_admission(
                self.ib_conn.ib, self._positions_for_admission, self.audit_path)
            if _credit_obs is None:
                raise RuntimeError(
                    "admission refused: the broker observation backing this credit entry could "
                    "not be verified (an unreadable book is never treated as an empty one)")
            reservation, _reservation_pot, _reservation_broker = await reserve_credit_entry(
                self.ib_conn.ib, r, order.orderRef,
                ledger=self.entry_reservation_ledger,
                dimensions=self._admission_dimensions(r, _credit_obs, order.orderRef),


                intent=self._entry_intent_or_none(r, order.orderRef),


                throttle_recorded=True,
                audit_path=self.audit_path)
            if not reservation.allowed:
                raise RuntimeError(
                    "collateral gate blocks submit: " + "; ".join(reservation.reasons))
            if not reservation.should_place:
                raise RuntimeError(
                    f"credit reservation forbids duplicate placement ({reservation.status})")
            from exitmgr.order_lock import order_mutation_lock
            place_invoked = False
            pre_submit_error = None
            try:
                with order_mutation_lock():
                    authority_now = self._short_option_entry_authority(r)
                    if not authority_now.allowed:
                        pre_submit_error = (
                            "short-option authority blocks submit after reservation: "
                            + "; ".join(authority_now.reasons))
                    marker_now = self._entry_markers_clear()
                    if pre_submit_error is None and not marker_now.allowed:
                        pre_submit_error = (
                            "entry markers block submit after reservation: "
                            + "; ".join(marker_now.reasons))
                    elif pre_submit_error is None:
                        quote_now = entry_safety.nbbo_valid(r)
                        if not quote_now.allowed:
                            pre_submit_error = (
                                "fresh NBBO blocks submit after reservation: "
                                + "; ".join(quote_now.reasons))
                    if pre_submit_error is None:

                        # order_mutation_lock is held by the surrounding block.
                        def _place_credit():
                            nonlocal place_invoked
                            place_invoked = True
                            return self.ib_conn.ib.placeOrder(r.contract, order)

                        trade = entry_safety.place_with_live_authority(
                            submission_authority, _place_credit)
            except Exception as exc:
                if not place_invoked:
                    await asyncio.to_thread(
                        self.entry_reservation_ledger.clear_not_transmitted, order.orderRef)
                    audit(self.audit_path, "credit_entry_reservation_cleared",
                          order_ref=order.orderRef, status="definite_pre_submit_failure")
                else:


                    audit(self.audit_path, "credit_place_ambiguous_reservation_retained",
                          order_ref=order.orderRef, error=str(exc))
                raise
            if pre_submit_error is not None:
                await asyncio.to_thread(
                    self.entry_reservation_ledger.clear_not_transmitted, order.orderRef)
                audit(self.audit_path, "credit_entry_reservation_cleared",
                      order_ref=order.orderRef, status="definite_pre_submit_block")
                raise RuntimeError(pre_submit_error)
            try:
                await asyncio.to_thread(
                    self.entry_reservation_ledger.update_intent,
                    order.orderRef, transmitted="yes")
            except Exception as exc:



                audit(self.audit_path, "credit_intent_transmit_state_unknown",
                      order_ref=order.orderRef, error=str(exc)[:200])
            status, reasons = await self._await_and_journal(trade, r)
            if await asyncio.to_thread(
                    self.entry_reservation_ledger.clear_for_status, order.orderRef, status):
                audit(self.audit_path, "credit_entry_reservation_cleared",
                      order_ref=order.orderRef, status=status)
            return status, reasons







        if r.structure:
            try:
                _require_allowed_structure("debit", r.structure)
            except ValueError as _struct_exc:
                raise RuntimeError(
                    "STRUCTURE REFUSED at submit: %s (permitted: %s)"
                    % (_struct_exc, ", ".join(sorted(DEBIT_STRUCTURES))))
            _implied_right = _structure_implied_right(r.structure)
            if _implied_right and _implied_right != str(r.right).upper()[:1]:
                raise RuntimeError(
                    "STRUCTURE REFUSED at submit: this order buys a %r but its structure %r names "
                    "a %s -- filling it would journal the position under a name describing a "
                    "different trade" % (r.right, r.structure,
                                         "call" if _implied_right == "C" else "put"))



        _lmt = entry_safety.executable_price(r)
        order = Order(action="BUY", orderType="LMT", lmtPrice=_lmt, totalQuantity=r.qty, tif="DAY")
        order.orderRef = entry_safety.decision_order_ref(r.decision_id)
        if r.short_contract is not None:

            _order_contract = self.ib_conn.create_combo_contract(
                r.underlying,
                [(r.contract.conId, "BUY"), (r.short_contract.conId, "SELL")])
        else:
            _order_contract = r.contract










        _obs = await observe_for_admission(
            self.ib_conn.ib, self._positions_for_admission, self.audit_path)
        if _obs is None:
            raise RuntimeError(
                "admission refused: the broker observation backing this entry could not be "
                "verified (an unreadable book is never treated as an empty one)")
        _placed = {}

        def _place():
            from exitmgr.order_lock import order_mutation_lock
            with order_mutation_lock():
                _placed["trade"] = entry_safety.place_with_live_authority(
                    submission_authority,
                    lambda: self.ib_conn.ib.placeOrder(_order_contract, order),
                )

        decision = self.entry_reservation_ledger.reserve_and_place(
            place=_place,
            recheck=self._admission_recheck(r, submission_authority),



            intent=self._entry_intent_or_none(r, order.orderRef),


            on_placed=lambda: True,
            **self._admission_dimensions(r, _obs, order.orderRef))
        audit(self.audit_path, "entry_admission", **reservation_as_dict(decision))
        if not decision.allowed or not decision.should_place:
            raise RuntimeError(
                "admission refused (%s): %s"
                % (decision.status, "; ".join(decision.reasons) or decision.status))
        trade = _placed.get("trade")
        if trade is None:
            raise RuntimeError("admission transaction returned no broker trade object")
        status, reasons = await self._await_and_journal(trade, r)
        if await asyncio.to_thread(
                self.entry_reservation_ledger.clear_for_status, order.orderRef, status):
            audit(self.audit_path, "entry_reservation_cleared",
                  order_ref=order.orderRef, status=status)
        return status, reasons

    async def _await_and_journal(self, trade, r: ResolvedOrder):
        """Public API contract; production-derived narrative omitted."""
        live = {"Filled", "Submitted", "PreSubmitted"}
        dead = {"Cancelled", "ApiCancelled", "Inactive", "Rejected"}
        for _ in range(24):
            await asyncio.sleep(0.5)
            if trade.orderStatus.status in live or trade.orderStatus.status in dead:
                break





        if trade.orderStatus.status not in dead:
            for _ in range(20):
                if trade.orderStatus.status == "Filled":
                    break
                await asyncio.sleep(0.5)
        status = trade.orderStatus.status
        reasons = [le.message for le in trade.log if getattr(le, "errorCode", 0)]








        from exitmgr.exec_capture import entry_journal_fields
        _credit = is_credit(r)
        _est_debit = capital_at_risk(r) if _credit else round(r.limit * 100 * abs(int(r.qty)), 2)
        _journalable, fill, _facts = entry_journal_fields(
            trade, requested_qty=r.qty, estimated_debit=_est_debit, credit=_credit)
        fill.update({
            "decision_id": r.decision_id,
            "decision_revision": r.decision_revision,
            "model_identity": r.model_identity,
            "model_identity_source": getattr(r, "model_identity_source", None),
            "order_ref": getattr(trade.order, "orderRef", None),
            "order_id": getattr(trade.order, "orderId", None),
        })
        _ref = str(getattr(trade.order, "orderRef", "") or "")
        if _journalable:
            self._journal_entry(r, fill=fill)
            audit(self.audit_path, "entry_journalled_from_fill", order_ref=_ref,
                  filled=_facts.filled_qty, requested=int(r.qty),
                  quantity_source=_facts.filled_qty_source,
                  basis_source=fill.get("basis_source"),
                  remaining=_facts.remaining_qty, order_status=status)
            self._release_entry_intent(_ref, outcome="journaled",
                                       filled_qty=int(_facts.filled_qty))
        else:



            audit(self.audit_path, "entry_not_journalled_no_observed_fill",
                  order_ref=_ref, order_status=status,
                  quantity_source=_facts.filled_qty_source,
                  executions_seen=_facts.execution_count,
                  exposure_unsized=_facts.exposure_unsized)
            if _facts.terminal and _facts.filled_qty == 0 and not _facts.exposure_unsized:
                self._release_entry_intent(_ref, outcome="terminal_no_fill")
        return status, reasons

    def _release_entry_intent(self, order_ref, *, outcome, filled_qty=None) -> None:
        """Public API contract; production-derived narrative omitted."""
        if not order_ref:
            return
        try:
            self.entry_reservation_ledger.resolve_intent(
                order_ref, outcome=outcome, filled_qty=filled_qty)
        except Exception as exc:


            audit(self.audit_path, "entry_intent_release_failed",
                  order_ref=str(order_ref), outcome=str(outcome), error=str(exc)[:200])

    def _journal_entry(self, r: ResolvedOrder, fill: Optional[dict] = None) -> None:
        """Public API contract; production-derived narrative omitted."""
        self._append_journal(self._entry_record(r, fill=fill))

    def _entry_record(self, r: ResolvedOrder, fill: Optional[dict] = None) -> dict:
        """Public API contract; production-derived narrative omitted."""
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "contract_id": getattr(r.contract, "conId", None),
            "symbol": r.underlying,
            "right": r.right,
            "expiry": r.expiry,
            "strike": r.strike,
            "quantity": r.qty,
            "debit": round(r.limit * 100 * r.qty, 2),
            "profit_target_pct": (getattr(r, "tp_pct", 0.0) or None),
            "stop_pct": (getattr(r, "sl_pct", 0.0) or None),
            "conviction": getattr(r, "conviction", -1.0),
            "intended_hold_days": getattr(r, "intended_hold_days", None),
            "thesis": getattr(r, "thesis", ""),
            "decision_id": getattr(r, "decision_id", None),
            "decision_revision": getattr(r, "decision_revision", 0),
            "execution_authority": getattr(r, "execution_authority", None),
            "model_identity": getattr(r, "model_identity", None),
            "model_identity_source": getattr(r, "model_identity_source", None),
            "order_ref": entry_safety.decision_order_ref(r.decision_id),



            "technical_card": getattr(r, "technical_card", None),

            "underlying_price_at_entry": (getattr(r, "spot", 0.0) or None),
            "entry_delta": (getattr(r, "entry_delta", 0.0) or None),
            "entry_iv": (getattr(r, "entry_iv", 0.0) or None),
            "dte_at_entry": (getattr(r, "dte", 0) or None),
            "dte_adjusted": bool(getattr(r, "dte_adjusted", False)),
            "earnings_unchecked": bool(getattr(r, "earnings_unchecked", False)),
            "earnings_date": getattr(r, "earnings_date", None),
            "earnings_overlap_warning": (getattr(r, "earnings_warn", "") or None),
            "earnings_day_warning": bool(getattr(r, "earnings_day_warning", False)),
            "earnings_reconsidered": bool(getattr(r, "earnings_reconsidered", False)),
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

            "entry_gamma": (getattr(r, "entry_gamma", 0.0) or None),
            "entry_theta": (getattr(r, "entry_theta", 0.0) or None),
            "entry_vega": (getattr(r, "entry_vega", 0.0) or None),
            "entry_ivr": (getattr(r, "entry_ivr", 0.0) or None),
            "entry_bid": (getattr(r, "entry_bid", 0.0) or None),
            "entry_ask": (getattr(r, "entry_ask", 0.0) or None),
            "entry_spread_pct": (getattr(r, "entry_spread_pct", 0.0) or None),
            "net_delta": (getattr(r, "net_delta", 0.0) or None),
            "net_theta": (getattr(r, "net_theta", 0.0) or None),
            "net_gamma": (getattr(r, "net_gamma", 0.0) or None),
            "net_vega": (getattr(r, "net_vega", 0.0) or None),
        }
        if is_credit(r):





            rec.update({
                "side": CREDIT_SIDE,
                "structure": CSP_STRUCTURE,
                "action": "SELL",
                "quantity": -abs(int(r.qty)),
                "contracts": abs(int(r.qty)),
                "collateral_usd": capital_committed(r),
                "net_credit_usd": round(_fnum(r.net_credit_usd, 0.0) or 0.0, 2),
                "max_loss_usd": capital_at_risk(r),
                "debit": capital_at_risk(r),
                "assignment_possible": True,
            })
        if fill:
            rec.update(fill)
        if r.short_contract is not None:
            rec["spread"] = {
                "short_con_id": getattr(r.short_contract, "conId", None),
                "short_strike": r.short_strike,
                "width": abs(r.short_strike - r.strike),
            }












        try:
            from exitmgr import atr_cache as _ac, atr_levels as _alv
            _rules = self.resolved_config.rules
            _acfg = getattr(_rules, "atr_levels", None)
            if _acfg is not None and getattr(_acfg, "enabled", False) and not is_credit(r):
                _aref = _ac.read(r.underlying)
                _nd = abs(float(rec.get("net_delta") or 0.0))
                _q = abs(int(r.qty))
                _debit = float(rec.get("debit") or 0.0)
                if _aref and _nd > 0 and _q > 0 and _debit > 0:
                    _eps = _debit / (100.0 * _q)
                    _cap = float(rec.get("stop_pct") or getattr(_rules, "stop_pct", 30.0))




                    _hold_e = rec.get("intended_hold_days") or getattr(r, "intended_hold_days", None)
                    try:
                        _hold_e = float(_hold_e) if _hold_e else 0.0
                    except (TypeError, ValueError):
                        _hold_e = 0.0
                    if _hold_e <= 0:
                        _hold_e = float(max(1, -(-int(getattr(r, "dte", 0) or 1) // 8)))
                    _keff_e = float(_acfg.k_stop)
                    if getattr(_acfg, "horizon_scaling", False):
                        _hk_e = _alv.horizon_k(float(getattr(_acfg, "k_stop_horizon", 0.5)),
                                               _hold_e)
                        if _hk_e is not None:
                            _keff_e = _hk_e
                    _astop = _alv.atr_stop_pct(_eps, _aref["atr"], _nd, _keff_e,
                                               _cap, float(_acfg.min_stop_pct))
                    if _astop is not None:
                        rec["stop_pct"] = round(float(_astop), 1)
                        rec["stop_basis"] = "atr_k%.2f_h%.0fd" % (_keff_e, _hold_e)
                        rec["stop_atr_ref"] = {"atr": _aref["atr"], "asof": _aref["asof"],
                                               "net_delta": _nd, "was_pct": _cap,
                                               "hold_days": _hold_e, "k_eff": _keff_e}
                        print("[ATR] %s entry stop %.0f%% -> %.1f%% (%.2f ATR over %.0fd hold, "
                              "net_delta %.3f)"
                              % (r.underlying, _cap, rec["stop_pct"], _keff_e, _hold_e, _nd))
        except Exception as _se:
            print("[ATR] entry stop sizing skipped for %s (%s)"
                  % (getattr(r, "underlying", "?"), _se))
        return rec

    def _append_journal(self, rec: dict) -> None:
        rec = dict(rec)


        rec["code_version"] = self.runtime_identity.code_version
        rec["policy_version"] = self.runtime_identity.policy_version
        from exitmgr.journal_io import append_jsonl_locked
        append_jsonl_locked(self.journal_path, rec)

    def _append_reconciled_journal(self, rec: dict) -> None:
        """Public API contract; production-derived narrative omitted."""
        rec = dict(rec)
        code = str(rec.get("code_version") or "")
        policy = str(rec.get("policy_version") or "")
        if not code and not policy:
            rec["code_version"] = ""
            rec["policy_version"] = ""
            rec["provenance_status"] = "legacy_unknown"
        else:
            RuntimeIdentity(code_version=code, policy_version=policy)
            rec["provenance_status"] = "source_bound_intent"
        from exitmgr.journal_io import append_jsonl_locked
        append_jsonl_locked(self.journal_path, rec)

    def entry_intent_payload(self, r: ResolvedOrder, order_ref: str) -> dict:
        """Public API contract; production-derived narrative omitted."""
        final = admission_contract_snapshot(r)
        template = self._entry_record(r)
        template["debit"] = final["max_loss_usd"]
        if is_credit(r):
            template.update(collateral_usd=final["collateral_usd"],
                            net_credit_usd=final["net_credit_usd"],
                            max_loss_usd=final["max_loss_usd"])
        return {
            "order_ref": order_ref,
            "con_id": getattr(r.contract, "conId", None),
            "symbol": getattr(r, "underlying", ""),
            "side": "credit" if is_credit(r) else "debit",
            "structure": (CSP_STRUCTURE if is_credit(r)
                          else str(getattr(r, "structure", "") or "")),
            "source": "continuous",
            "decision_id": str(getattr(r, "decision_id", "") or ""),
            **identity_fields(self.runtime_identity),
            "requested_qty": abs(int(getattr(r, "qty", 0) or 0)),
            "estimated_debit": final["max_loss_usd"],
            "transmitted": "unknown",
            "journal_template": template,
        }
