"""Orchestrator for the LLM trading system.

Flow each cycle:  live pot -> day-start baseline (circuit breaker) -> market context ->
strategist proposes -> risk gate filters -> RESOLVE the concrete option order -> Slack
approve-each (showing the EXACT order) -> submit exactly what was approved -> exit manager.

Hard invariants:
  * Nothing is submitted unless dry_run is OFF (--arm) AND an explicit approval came back.
  * The approval message shows the resolved order (strike/expiry/qty/limit) -- you approve the
    real order, not just the idea.
  * Every proposal, gate decision, resolution, approval, and fill is appended to the audit log.
"""
import dataclasses
import asyncio
import json
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set

from exitmgr.account import get_pot_snapshot
from exitmgr.risk import (
    RiskLimits, OpenPosition, ProposedTrade, GateDecision, evaluate_trade, day_pnl_pct,
    INDEX_UNDERLYINGS,
)
from exitmgr.strategist import (
    propose, propose_intents, select_candidate, TradeIdea,
    # THE structure allow-list and its choke point are IMPORTED, never re-declared here.
    # Two copies of a safety list can drift out of step, and a drifted allow-list is a
    # worse bug than a gate in the wrong place.
    DEBIT_STRUCTURES, _require_allowed_structure,
)
from exitmgr.entry_contract import StageAIntent, RuntimeCandidate
from exitmgr.entry_builder import (
    CandidateBinding, CandidateBuildError, build_entry_candidates,
    bindings_for_stage_b, reprice_binding, select_binding,
)
from exitmgr.entry_reservation import (
    EntryReservationLedger, ReservationDecision, reservation_as_dict,
)
from exitmgr import approval, construction, research, regime, slate_lock, trade_capture, reload_queue
from exitmgr import entry_safety
from exitmgr.entry_throttle import (
    EntryThrottleStore, entry_day_open_counts, record_entry_open,
)
from dataclasses import replace as _replace_dc
from exitmgr.config import ConstructionConfig

# BOUNDED BROKER READS (2026-08-13). An unbounded `await ib.*Async()` wedged the exit
# loop for 6+ minutes tonight with every position unevaluated. Same guard applied here.
# A timeout raises into the caller's existing error handling; a hang has no handler.
_IB_CALL_TIMEOUT_S = 30



# ---------------------------------------------------------------- pure helpers (unit-tested)

def audit(path: str, event: str, **fields) -> dict:
    rec = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(rec, default=str) + "\n")
    return rec


def _trading_day(now=None) -> str:
    """US/Eastern calendar date -- the trading day the circuit-breaker baseline is keyed to.
    Using the exchange timezone (not UTC) means the day-start baseline rolls over exactly once
    per session, in the gap between the prior close and the next open, and NEVER mid-RTH. The old
    UTC-date key mislabeled the 20:00-00:00 UTC window (8pm-ET -> midnight-ET) as the NEXT day,
    so the breaker's reference pot could be captured from a stale prior-evening read."""
    try:
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        n = now or datetime.now(et)
        if getattr(n, "tzinfo", None) is None:
            n = n.replace(tzinfo=timezone.utc)
        return str(n.astimezone(et).date())
    except Exception:
        return str((now or datetime.now(timezone.utc)).date())


def day_start_pot(baselines: Dict[str, float], today: str, current_net_liq: float):
    """Return (day_start_baseline, updated_baselines). The baseline is the pot value at the START
    of the trading day `today`; it gates the -8% daily circuit breaker (the solvency backstop), so
    it must roll over exactly once per day and can NEVER go stale or be poisoned by a garbage read.

    HARDENING (P3.9 audit, 2026-07-02):
      * A bad net-liq read (None / NaN / <=0) never SETS or overwrites a baseline -- a transient
        $0 from IBKR would otherwise make day_pnl_pct explode or silently disable the breaker.
      * The baseline is STICKY within the day (only set when `today` is absent), so the pot at the
        first GOOD read of the session stays the reference all day -- it can't drift.
      * A fresh day drops stale prior days, so the store can't grow unbounded.
    Failure is SAFE: with no valid baseline yet, day_pnl_pct sees pot_day_start<=0 and returns 0%,
    so the breaker neither fabricates a halt nor suppresses one on bogus data."""
    b = dict(baselines or {})
    valid = isinstance(current_net_liq, (int, float)) and current_net_liq == current_net_liq \
        and current_net_liq > 0
    if today not in b:
        if not valid:
            return (b.get(today, 0.0) or 0.0), b   # don't invent a baseline from a bad read
        b = {today: float(current_net_liq)}        # fresh day -> drop stale prior days
    return b[today], b


def _market_open() -> bool:
    """US regular session ~13:30-20:00 UTC, Mon-Fri (ignores holidays; gateway maint handled
    elsewhere). When closed, the trader skips the strategist/model call — no trade can be
    entered, so there's nothing to propose."""
    t = datetime.now(timezone.utc)
    if t.weekday() >= 5:  # Sat/Sun
        return False
    mins = t.hour * 60 + t.minute
    return 13 * 60 + 30 <= mins <= 20 * 60


# ----------------------------------------------------------- CREDIT LIMB (CREDIT_PATH_SPEC.md S2)
# The ONLY short-premium structure this system may ever submit is a CASH-SECURED PUT. Everything
# below exists to make the two catastrophic failure modes UNREACHABLE rather than merely
# discouraged:
#   INVARIANT 1  a naked call (or any short that is not a CSP) never reaches an order;
#   INVARIANT 2  a put is never sold without `strike * 100 * contracts` of cash verified
#                AVAILABLE AT SUBMIT TIME (not merely at proposal time).
CREDIT_SIDE = "credit"
CSP_STRUCTURE = "cash secured put"
# Trevor's ruling (RULING_CREDIT_CAP.md): total reserved collateral -- ALREADY-DEPLOYED plus this
# trade -- may never exceed 80% of net liquidation value.
CREDIT_MAX_COLLATERAL_PCT = 0.80
# Trevor's ruling (2026-07-27): a cash-secured put must earn at least this fraction of its
# RESERVED COLLATERAL in premium. Until now this bar lived only in the strategist prompt, which
# made the model its sole enforcement point -- a CSP earning $3 on $1,500 of collateral passed
# every structural gate. Collateral, not the credit, is the capital at risk, so the bar is
# expressed against collateral. Enforced in credit_structure_ok(), which every credit idea passes
# through regardless of whether it came from the parser, a CLI structure string, or a reload ticket.
CREDIT_MIN_PREMIUM_PCT = 0.0125
# Credit DTE window (spec S3). Read from ConstructionConfig when the structure-aware floor lands;
# these are the fallbacks so this path is never accidentally handed the 25-DTE DEBIT floor.
CREDIT_MIN_DTE_DEFAULT = 3
CREDIT_MAX_DTE_DEFAULT = 45
_EPS = 1e-6


def _side_of(obj) -> str:
    """"credit" | "debit". Absent/unparseable == "debit" -- the historical contract."""
    try:
        s = str(getattr(obj, "side", "debit") or "debit").strip().lower()
    except Exception:
        return "debit"
    return CREDIT_SIDE if s == CREDIT_SIDE else "debit"


def is_credit(obj) -> bool:
    """True for a credit TradeIdea or a credit ResolvedOrder. Everything else is a debit."""
    return _side_of(obj) == CREDIT_SIDE


def _fnum(x, default=None):
    """float(x) if finite, else `default`. Never raises."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if v == v and v not in (float("inf"), float("-inf")) else default


def credit_structure_ok(idea) -> tuple:
    """INVARIANT 1 + the credit contract's field requirements. Returns (ok, reason).

    A short structure that is NOT a cash-secured put has unbounded (naked call) or undefined
    risk, so it is REFUSED here -- before any contract is qualified and long before an order
    object exists. Debit ideas pass through untouched (this is a no-op for them)."""
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
    # Trevor's ruling (2026-07-27, "the 1.25% bar belongs in code"): a CSP must EARN its collateral.
    # Return-on-collateral is scale-invariant -- halving the contracts halves credit and collateral
    # alike -- so a miss is rejected, never downsized: it is the wrong trade, not a too-large one.
    roc = net_credit / collateral
    if roc + 1e-9 < CREDIT_MIN_PREMIUM_PCT:
        return False, (f"credit idea earns {roc:.3%} of its ${collateral:,.2f} collateral, under the "
                       f"{CREDIT_MIN_PREMIUM_PCT:.2%} premium bar (needs "
                       f"${collateral * CREDIT_MIN_PREMIUM_PCT:,.2f}, offers ${net_credit:,.2f})")
    return True, ""


# ------------------------------------------------------------------- DEBIT LIMB STRUCTURE GATE
# THE DEFECT THIS CLOSES (2026-07-26). strategist._require_allowed_structure() only runs on ideas
# the STRATEGIST PARSES. Three routes build a TradeIdea directly and never touch the parser, so
# the allow-list never saw them:
#   1. daily_recommend.py  --structure          -- an arbitrary CLI string
#   2. trader.py           _drain_reload_ideas  -- reload tickets
#   3. daily_recommend.py  Slack approval structure/direction override
#
# THE CONSEQUENCE IS SILENT SEMANTIC CORRUPTION, NOT UNBOUNDED LOSS -- state it accurately.
# Both executors branch on the substring "spread" (trader._resolve_order, daily_recommend._resolve)
# and take the option right from `direction`, NEVER from the structure string. "naked call" has no
# "spread" in it, so it routed to the SINGLE LONG LEG builder and BOUGHT a call. The debit path
# cannot emit a sell-to-open at all. What it COULD do is fill a long call and journal it as a
# "naked call" / "short strangle" -- and the journal is the training corpus, so the model would be
# taught from a row describing a position the account never held. That is the defect: a
# correctness and data-integrity failure at the label, not a risk failure at the position.
def _structure_implied_right(raw) -> str:
    '''"C" | "P" | "" -- the option right a structure NAMES, or "" when it names none
    ("long option", "debit spread"). Uses the same two substrings strategist.normalize_direction()
    already uses to infer a direction FROM a structure, so a consistent pair is classified exactly
    as it is today. No entry in DEBIT_STRUCTURES contains both words, so this is unambiguous for
    everything the allow-list permits.'''
    s = str(raw or "").lower()
    has_call, has_put = "call" in s, "put" in s
    if has_call and not has_put:
        return "C"
    if has_put and not has_call:
        return "P"
    return ""


def debit_structure_ok(idea) -> tuple:
    """THE debit-side structure gate. Returns (ok, reason). Credit ideas pass through untouched --
    credit_structure_ok() is their gate exactly as this is the debit side's, and the two never
    both judge the same idea.

    Two independent conditions:
      1. the structure is on strategist.DEBIT_STRUCTURES (the ONE allow-list, imported above);
      2. the structure does not CONTRADICT the direction.

    (2) closes a pre-existing hole that is the same defect as (1) wearing different clothes:
    {"structure": "bull call spread", "direction": "bearish"} passes the allow-list, and then the
    constructor -- which reads the right off `direction` -- builds a PUT vertical and journals it
    as a bull call spread. REJECTED rather than repaired: `direction` and the structure's own noun
    disagree, and nothing in the idea says which of the two the author meant. Deriving the right
    from the structure instead would silently overrule an explicit `direction` and swap the trade's
    side -- trading one silent corruption for a worse one. A CONSISTENT pair is unaffected: the
    check only fires when the structure names a right and the direction names the opposite one.

    A failure is REFUSED and reported. It is never coerced to a default -- quietly substituting
    "long call" for a rejected string is precisely the mislabelling this gate exists to stop."""
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
    """INVARIANT 2's arithmetic: cash that must be RESERVED = strike * 100 * contracts.
    None when either input is unusable -- the caller must then REFUSE, never assume."""
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
    """Pure, FAIL-CLOSED capacity check for a cash-secured put. Returns SafetyResult.

    Refuses unless ALL of the following are verifiably true:
      * `required` (strike*100*qty) is a positive finite number;
      * live net_liq / available_funds are usable (positive / non-negative finite);
      * `deployed` -- collateral ALREADY reserved by open short puts and working sell-to-open
        orders -- is known.  Unknown (None) REFUSES: an unverifiable book can never be treated
        as empty (invariant 3 counts already-deployed collateral, not just this trade);
      * required <= available_funds  (invariant 2: cash-secured, not naked);
      * deployed + required <= max_pct * net_liq  (invariant 3: the 80% ruling).

    A refusal is a CORRECT outcome, not an error."""
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
    """Cash already reserved by live short puts and our working sell-to-open puts.

    Shared by continuous, daily-slate, and add-name. Any unreadable short-put reservation
    returns ``None`` so every caller fails closed rather than treating an unknown book as empty.
    """
    total = 0.0
    visible_order_refs = set()
    visible_con_ids = set()
    try:
        positions = await asyncio.wait_for(ib.reqPositionsAsync(), _IB_CALL_TIMEOUT_S)
        for pos in positions or []:
            contract = getattr(pos, "contract", None)
            quantity = _fnum(getattr(pos, "position", 0), None)
            if contract is None or quantity is None or quantity >= 0:
                continue
            sec_type = str(getattr(contract, "secType", "") or "").upper()
            if sec_type and sec_type not in ("OPT", "FOP"):
                continue
            right = str(getattr(contract, "right", "") or "").upper()[:1]
            if right == "C":
                if audit_path:
                    audit(audit_path, "short_call_position_detected",
                          symbol=str(getattr(contract, "symbol", "")),
                          con_id=getattr(contract, "conId", None), quantity=quantity,
                          note="INVARIANT 1 VIOLATION: account holds a short call")
                continue
            if right != "P":
                continue
            strike = _fnum(getattr(contract, "strike", None), None)
            if strike is None or strike <= 0:
                return None
            total += strike * 100.0 * abs(quantity)
            con_id = getattr(contract, "conId", None)
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


async def broker_deployed_csp_collateral(ib, audit_path: Optional[str] = None) -> Optional[float]:
    snapshot = await broker_csp_collateral_snapshot(ib, audit_path)
    return None if snapshot is None else snapshot.total


async def reserve_credit_entry(ib, r, order_ref: str, *, ledger: EntryReservationLedger,
                               audit_path: Optional[str] = None):
    """Atomically admit one CSP against a fresh account/broker observation.

    Account and broker I/O intentionally happens outside both file locks.  The reservation
    ledger then serializes only the final arithmetic/commit across the continuous and daily
    processes, closing their check-then-place race without delaying protective exits.
    """
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
    decision = await asyncio.to_thread(
        ledger.reserve,
        order_ref=order_ref,
        con_id=getattr(getattr(r, "contract", None), "conId", None),
        collateral_usd=committed,
        net_liq=getattr(snap, "net_liq", None),
        available_funds=getattr(snap, "available_funds", None),
        broker_deployed_usd=broker.total,
        visible_order_refs=broker.visible_order_refs,
    )
    return finish(decision, snap, broker)


def credit_executable_price(r) -> float:
    """The SELL limit that crosses the final observed NBBO: we sell TO the bid.
    (entry_safety.executable_price is the BUY mirror -- it crosses at the ask.)"""
    return round(float(getattr(r, "entry_bid")), 2)


def credit_material_changes(original, refreshed, *,
                            max_price_change_pct: float = entry_safety.DEFAULT_MATERIAL_PRICE_PCT):
    """entry_safety.material_changes for a SELL-to-open: the executable leg is the BID, and a
    change in reserved collateral also invalidates the human's approval."""
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
    """$ this order can lose -- the figure every existing $-ceiling should measure.
    Debit: the net debit paid (limit*100*qty). Credit (CSP): collateral - credit received."""
    if is_credit(r):
        ml = _fnum(getattr(r, "credit_max_loss_usd", 0.0), 0.0) or 0.0
        if ml > 0:
            return round(ml, 2)
        coll = _fnum(getattr(r, "collateral_usd", 0.0), 0.0) or 0.0
        return round(max(0.0, coll - (_fnum(getattr(r, "net_credit_usd", 0.0), 0.0) or 0.0)), 2)
    return round(float(r.limit) * 100 * int(r.qty), 2)


def capital_committed(r) -> float:
    """$ of the pot this order ties up -- what the risk gate / throttles should count.
    Debit: the debit paid. Credit: the FULL reserved collateral (cash locked, not max loss)."""
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
    """RiskLimits for a CASH-SECURED PUT. Trevor's ruling 2026-07-27: "do 80% of the pot for CSPs".

    A credit idea's gate `notional` is its RESERVED COLLATERAL, and until this change that
    collateral was measured against a debit's caps -- max_trade_pct_hard 25%, single-name 36%,
    sector 25%. At $1,893 that pinned one write to $473.25 (a $4.73 strike), so
    CREDIT_MAX_COLLATERAL_PCT could never bind on a single write at ANY account size; it first bound
    on the FOURTH concurrent CSP. Trevor's earlier "i dont care what name it enters or if it takes
    up to 80% of the pot" is what authorizes lifting concentration as well as size -- lifting only
    the per-trade cap would leave a CSP silently bound at 36% and the ruling inert.

    Raise ONLY the three percentage caps, and only UPWARD -- `min` so a future tightening of any of
    them below 80% still wins. Everything that actually secures the put is untouched and still
    binds: the submit-time cash check (INVARIANT 2), the 80% TOTAL-collateral ceiling across all
    open CSPs (INVARIANT 3 -- so one 80% write consumes the entire credit budget rather than adding
    to it), the 5% cash buffer, available_funds, max_concurrent, the daily breaker, the blocklist,
    and INVARIANT 1 (only a CSP may ever be submitted).

    Debit ideas never reach this function.
    """
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
        # Never fail an entry because the limits object is an unexpected shape: fall back to the
        # ORIGINAL (tighter) limits. A refusal is a correct outcome; a silent widening is not.
        return limits


def plan_idea(idea: TradeIdea, *, net_liq: float, available_funds: float,
              positions: List[OpenPosition], baseline: float,
              approved_names: Set[str], limits: RiskLimits, regime=None) -> Plan:
    # CREDIT (spec S2): the gate's `notional` is the capital the trade ties up. For a CSP that is
    # the RESERVED COLLATERAL, not the (tiny) credit received -- so a short put is measured against
    # exactly the same per-trade cap, buying-power check, concurrent cap, daily breaker, and
    # single-name/sector concentration caps as a debit of the same size. Using the credit received
    # would make every CSP look like a ~$100 trade and wave a $50,000 cash commitment straight
    # through the solvency layer.
    if is_credit(idea):
        notional = _fnum(getattr(idea, "collateral_usd", 0.0), None)
        if notional is None or notional <= 0:
            # unusable collateral -> a notional the gate can only REJECT (never a free pass)
            notional = float("inf")
        # never let a short-premium entry be regime-SIZED-UP: is_long=False pins the regime
        # multiplier at 1.0 regardless of the idea's stated direction.
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
    """The concrete order, resolved from an idea BEFORE approval, so the human approves it."""
    underlying: str
    right: str          # "C" | "P"
    expiry: str         # YYYYMMDD
    strike: float       # long leg
    qty: int
    limit: float        # per-share debit: the premium, or the NET debit for spreads
    contract: object = None         # qualified IB contract (long leg), for submission
    short_strike: float = 0.0       # debit spread sold leg; 0 = single-leg order
    short_contract: object = None
    conviction: float = -1.0        # entry conviction (1-10) carried through for the journal; -1 = unknown
    thesis: str = ""                # strategist entry thesis, carried through for the durable journal
    # 2026-07-01 constructor rework: construction/enrichment facts carried into the journal.
    tp_pct: float = 0.0             # clamped take-profit % (25-35 band; 0 = global default rule)
    sl_pct: float = 0.0             # clamped stop % (default 30; 0 = global default rule)
    spot: float = 0.0               # underlying price at construction (0 = unknown)
    entry_delta: float = 0.0        # long-leg delta at construction (0 = unknown)
    entry_iv: float = 0.0           # long-leg implied vol at construction (0 = unknown)
    dte: int = 0                    # DTE of the chosen expiry at construction
    dte_adjusted: bool = False      # True when the expiry was ADJUSTED to satisfy the min-DTE floor
    # FULL GREEKS + LIQUIDITY at construction (v2 dataset, record-only; 0/None = unavailable from
    # the feed). Net greeks == long-leg greeks for a single; spread net greeks need the short leg's
    # greeks (not retained) so they stay None for spreads.
    entry_gamma: float = 0.0
    entry_theta: float = 0.0
    entry_vega: float = 0.0
    entry_ivr: float = 0.0          # IV rank/percentile if available (0 = unknown)
    entry_bid: float = 0.0
    entry_ask: float = 0.0
    entry_spread_pct: float = 0.0   # (ask-bid)/mid -- a bid/ask liquidity measure
    net_delta: float = 0.0
    net_theta: float = 0.0
    net_gamma: float = 0.0
    net_vega: float = 0.0
    quote_observed_at: float = 0.0  # monotonic timestamp of the reqTickersAsync result
    decision_id: str = ""          # immutable proposal -> order -> fill -> close lineage
    decision_revision: int = 0
    model_identity: Optional[dict] = None
    intended_hold_days: Optional[int] = None  # source-bound calendar underwriting window
    # -- CREDIT LIMB (spec S2). ADDITIVE + defaulted, so every existing debit construction site
    # (including the positional-arg ones in tests) is byte-identical. side == "credit" is the ONLY
    # thing that can produce a SELL-to-open action in _submit_order_unlocked.
    side: str = "debit"                 # "debit" | "credit"
    collateral_usd: float = 0.0         # credit: strike * 100 * qty -- cash RESERVED
    net_credit_usd: float = 0.0         # credit: total dollars received (limit * 100 * qty)
    credit_max_loss_usd: float = 0.0    # credit: collateral_usd - net_credit_usd
    # -- STRUCTURE (2026-07-26). ADDITIVE + defaulted, so every existing construction site --
    # including the positional-arg ones in the tests and in place_trade.py -- is byte-identical.
    # It carries the idea's structure string down to the money boundary so _submit_order_unlocked
    # can re-check it there, the way the credit branch re-checks its right and leg count. "" means
    # "no structure was declared" (place_trade.py builds an order with no TradeIdea behind it at
    # all) and is NOT an allow-list failure; every route that HAS a structure is gated upstream.
    structure: str = ""


def order_summary(r: ResolvedOrder) -> str:
    if is_credit(r):
        # SELL wording is deliberate: the human must never see "BUY" on an order that sells.
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
    """JSON-safe exact terms approved/submitted for one decision revision."""
    snap = {
        "underlying": r.underlying, "right": r.right, "expiry": r.expiry,
        "long_con_id": getattr(r.contract, "conId", None), "long_strike": r.strike,
        "short_con_id": getattr(r.short_contract, "conId", None),
        "short_strike": (r.short_strike or None), "quantity": r.qty,
        "limit": r.limit, "max_loss_usd": capital_at_risk(r),
        "quote_observed_at": r.quote_observed_at,
    }
    if is_credit(r):
        snap.update(side=CREDIT_SIDE, action="SELL", structure=CSP_STRUCTURE,
                    collateral_usd=capital_committed(r),
                    net_credit_usd=round(_fnum(r.net_credit_usd, 0.0) or 0.0, 2))
    return snap


def pick_spread_short(candidates, long_strike: float, long_mid: float, right: str,
                      per_trade_cap: float, *, spot=None, dte=None, atm_iv=None, cons=None):
    """Choose the sold leg of a debit vertical: the WIDEST further-OTM strike whose net debit
    (long_mid - short_mid, for one spread) still fits the per-trade cap. candidates =
    [(strike, mid), ...]. Returns (short_strike, net_debit_per_share) or None.
    STRUCTURE SANITY (2026-07-01, keyword-only so old callers/tests are unchanged): when
    `cons` (ConstructionConfig) is given, strikes failing spread_structure_ok -- short leg
    beyond ~1 expected move of spot (or the conservative width fallback) -- are SKIPPED, so
    a lottery vertical (the NOK 14/25 on a $13.62 stock) can never be constructed."""
    otm = [(k, m) for k, m in candidates
           if m is not None and m == m and m > 0
           and ((right == "C" and k > long_strike) or (right == "P" and k < long_strike))]
    otm.sort(key=lambda km: abs(km[0] - long_strike), reverse=True)  # widest first
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
    """Contracts to buy so the order fits BOTH the idea budget and the per-trade $ cap.

    HARD-REJECT (2026-07-03): returns None -- reject the trade -- when even ONE contract exceeds the
    per-trade cap, instead of the old `max(1, ...)` that force-clamped qty to 1 and shipped an order
    OVER the risk cap. `unit_cost` is the per-contract cost (mid*100 for a single, net*100 for a
    spread). Pure + unit-tested."""
    if unit_cost <= 0:
        return None
    qty = int(min(budget, per_trade_cap) // unit_cost)
    if qty < 1:
        return None   # a single contract already exceeds the cap -> reject, never clamp to 1
    if unit_cost * qty > per_trade_cap + 1e-6:
        qty -= 1
    return qty if qty >= 1 else None


# ---------------------------------------------------------------- orchestrator (I/O)

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
                 trading_down_path: Optional[str] = None,
                 broker_order_lock=None,
                 entry_reservation_ledger: Optional[EntryReservationLedger] = None,
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
        # KILL SWITCH (2026-07-03): the manager's kill switch only gated EXITS; the trader never
        # checked it, so entries kept flowing under a kill switch. When configured (run_trader.py
        # passes cfg.kill_switch.path) an existing file HALTS new entries. None (bare Trader/tests)
        # -> never active.
        self.kill_switch_path = kill_switch_path or "./KILL_SWITCH"
        self.config_path = config_path
        self.trading_down_path = trading_down_path
        self.broker_order_lock = broker_order_lock
        self.entry_reservation_ledger = entry_reservation_ledger or EntryReservationLedger()
        # EXIT-CYCLE FAILURE STREAK (2026-07-03): consecutive run_cycle failures. After
        # _EXIT_FAIL_SUPPRESS_ENTRIES in a row, new entries are suppressed (a broken exit path must
        # not be compounded by opening MORE positions we then can't manage).
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
        self.journal_path = journal_path
        self.blocked_sector_keywords = [k for k in (blocked_sector_keywords or []) if k.strip()]
        self.entry_limit_buffer_pct = float(entry_limit_buffer_pct)
        # 2026-07-01 constructor-rework thresholds (min DTE / TP-SL clamp / structure sanity /
        # budget gates); defaults apply when the config has no `construction:` section.
        self.construction = construction_cfg or ConstructionConfig()
        # POT-TIERED TP RUNNER CEILING rows (caps.tp_tiers). None/empty => flat no-op (today's
        # behavior). Scales ONLY the take-profit ceiling at ENTRY; never the protective stop.
        self.caps_tp_tiers = list(caps_tp_tiers or [])
        # ENTRY THROTTLE CEILINGS (2026-07-03 gap-fix). caps.max_orders_per_cycle / _per_day /
        # notional_per_day were loaded but only enforced on the EXIT path; enforce them on NEW
        # entries too. None => that ceiling is disabled (bare Trader / older tests keep prior
        # behavior). These are CEILINGS that only ADD safety -- they never up-size or force a trade.
        self.max_orders_per_cycle = max_orders_per_cycle
        self.max_orders_per_day = max_orders_per_day
        self.max_notional_per_day = max_notional_per_day
        # TAKE-PROFIT-AND-RELOAD (2026-07-03). OFF BY DEFAULT: reload_enabled=False => the drain is a
        # pure no-op and behavior is byte-identical to today. When enabled, ready reload tickets
        # (written by the exit manager ONLY after a Filled take-profit close) are drained each cycle
        # into synthetic same-name suggestions that flow through the SAME gate/construct/approve/
        # submit path as strategist ideas. reload_conviction_min + reload_friction_k gate churn;
        # reload_max_per_name_per_day + reload_ttl_cycles bound anti-churn. NEVER auto-fires -- every
        # reload is a normal human-approved suggestion.
        self.reload_enabled = bool(reload_enabled)
        self.reload_conviction_min = float(reload_conviction_min)
        self.reload_friction_k = float(reload_friction_k)
        # MEASURED constant, not a per-ticket forecast -- see config.py for the byron evidence.
        self.reload_expected_continuation_pct = float(reload_expected_continuation_pct)
        self.reload_max_per_name_per_day = int(reload_max_per_name_per_day)
        self.reload_ttl_cycles = int(reload_ttl_cycles)
        # market regime + per-underlying momentum, refreshed each cycle in _market_context;
        # feeds both regime-aware sizing (entries) and the position manager (exits)
        self._regime = None
        self._price_stats = {}
        # CREDIT (spec S2, invariant 3): collateral reserved by CSPs submitted EARLIER IN THIS
        # CYCLE. The live short-position read can lag a just-submitted sell-to-open, so this
        # accrual is added to the live figure -- a second CSP in the same cycle can never be
        # sized against a book that has forgotten the first. Double-counting a fill that HAS
        # already appeared live only OVER-states deployment, which can only refuse. Reset each
        # run_once.
        self._cycle_credit_collateral = 0.0

    # After this many consecutive exit-cycle failures, stop opening NEW entries.
    _EXIT_FAIL_SUPPRESS_ENTRIES = 3

    def _kill_switch_active(self) -> bool:
        """True if the KILL_SWITCH file exists (halts ENTRIES; exits are gated by the manager).
        No path configured (bare Trader / tests) -> never active. Never raises."""
        try:
            return bool(self.kill_switch_path) and Path(self.kill_switch_path).exists()
        except Exception:
            return False

    def _entry_markers_clear(self) -> entry_safety.SafetyResult:
        return entry_safety.entry_markers_clear(
            config_path=self.config_path,
            kill_switch_path=self.kill_switch_path,
            trading_down_path=self.trading_down_path)

    def _load_baselines(self) -> Dict[str, float]:
        p = Path(self.baseline_path)
        if p.exists():
            try:
                return json.loads(p.read_text())
            except json.JSONDecodeError:
                return {}
        return {}

    def _save_baselines(self, b: Dict[str, float]) -> None:
        # Atomic replacement: a crash during a direct write must not erase the daily-loss baseline
        # and silently disarm the circuit breaker.
        path = Path(self.baseline_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(b))
        tmp.replace(path)

    def _load_journal_debits(self) -> Dict[int, float]:
        """Map long-leg contract_id -> journaled NET debit (max loss) for every entry in
        trades.log. The journal records `debit` = limit*100*qty, i.e. the net debit paid for a
        spread (or the premium for a single leg) -- the TRUE capital at risk for these
        defined-risk structures. The newest entry per contract_id wins (re-entries)."""
        debits: Dict[int, float] = {}
        p = Path(self.journal_path)
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
            cid = rec.get("contract_id")
            d = rec.get("debit")
            if cid is None or d is None:
                continue
            try:
                debits[int(cid)] = float(d)
            except (TypeError, ValueError):
                continue
        return debits

    async def _open_positions(self) -> List[OpenPosition]:
        raw = await self.ib_conn.get_positions()
        journal_debits = self._load_journal_debits()
        out = []
        for pd in raw.values():
            is_index = pd.symbol.upper() in {"SPY", "QQQ", "IWM"}
            # EXPOSURE = MAX LOSS for these defined-risk structures (long calls/puts + debit
            # spreads): the NET debit paid, not the long-leg notional. get_positions() returns
            # ONLY the long leg of a spread (short leg filtered), so avg_cost*100*qty is the GROSS
            # long-leg value -- e.g. a NOK 14C/25C spread (qty2, net debit ~$152) reads as ~$16,140
            # off the deep-ITM long 14C alone. That bogus ~100x number then blows the single-name
            # aggregate cap and gates every idea. Use the journaled net debit keyed by the long
            # leg's contract_id; fall back to the gross formula ONLY when there's no journal entry
            # (conservative -- never silently 0), and log it so the blow-up can't hide.
            con_id = getattr(pd, "con_id", None)
            gross = abs(pd.avg_cost) * 100 * abs(pd.quantity)
            net_debit = journal_debits.get(int(con_id)) if con_id is not None else None
            if net_debit is not None:
                notional = float(net_debit)
            else:
                notional = gross
                audit(self.audit_path, "exposure_no_journal_debit",
                      symbol=pd.symbol.upper(), con_id=con_id, fallback_gross=round(gross, 2),
                      note="no journaled net debit; using gross long-leg value (conservative)")
            out.append(OpenPosition(pd.symbol.upper(), notional, is_index))

        # FOLD RESTING ENTRY BUYS (2026-07-03): get_positions() returns FILLED positions only, so a
        # BUY entry we placed that is still resting/unfilled is INVISIBLE to the concurrent-count and
        # aggregate-exposure caps -> the gate could wave through an additional over-concentrating or
        # over-limit entry while the first is still working. Count live open BUY orders (all
        # clientIds) as pseudo-positions in the exposure book. Best-effort: a read failure just
        # leaves the filled-only book (never blocks the cycle).
        try:
            existing_cids = set(raw.keys())
            open_trades = await self.ib_conn.ib.reqAllOpenOrdersAsync()
            for t in open_trades or []:
                o = getattr(t, "order", None)
                c = getattr(t, "contract", None)
                st = getattr(getattr(t, "orderStatus", None), "status", None)
                if o is None or c is None or getattr(o, "action", "") != "BUY":
                    continue
                if st in {"Cancelled", "ApiCancelled", "Inactive", "Filled"}:
                    continue
                sym = (getattr(c, "symbol", "") or "").upper()
                if not sym:
                    continue
                cid = getattr(c, "conId", None)
                if cid is not None and int(cid) in existing_cids:
                    continue  # already counted as a filled position
                is_index = sym in {"SPY", "QQQ", "IWM"}
                nd = journal_debits.get(int(cid)) if cid is not None else None
                if nd is None:
                    lp = getattr(o, "lmtPrice", None)
                    q = getattr(o, "totalQuantity", 0) or 0
                    nd = (float(lp) * 100 * q) if (isinstance(lp, (int, float)) and lp == lp and lp > 0) else 0.0
                out.append(OpenPosition(sym, float(nd), is_index))
                audit(self.audit_path, "resting_entry_folded", symbol=sym, con_id=cid,
                      notional=round(float(nd), 2))
        except Exception as _oe:
            audit(self.audit_path, "open_buy_fold_error", error=str(_oe))

        # FOLD LIVE SHORT (CREDIT) POSITIONS -- CONCURRENCY, VALUED AT COLLATERAL (2026-07-26).
        #
        # `raw` above is get_positions() with NO keywords, i.e. LONG options only, so a filled
        # cash-secured put contributed NOTHING to this book: risk.evaluate_trade check #4 counted
        # it as zero open positions on every cycle after the fill, and max_concurrent could be
        # exceeded by exactly the number of CSPs held.
        #
        # WHY IT IS DONE HERE AND NOT BY FLIPPING get_positions(include_short=True). The
        # 2026-07-26 consumer audit measured what that would do: construction.open_book_items()
        # keys the journal by con_id and reads `debit`, which a credit row deliberately sets to
        # MAX LOSS -- so a single $175-credit CSP adds ~$49,825 to the LONG-PREMIUM deployment
        # book, blows the deployed-% and theta caps, and rejects EVERY entry, credit and debit
        # alike, for as long as the put is open. That book is fed from get_positions() directly
        # (trader.run_once / _final_entry_checks), so it stays long-only and untouched; only THIS
        # list, whose consumer is the risk gate, learns about the short.
        #
        # VALUATION. A CSP's capital at risk is the COLLATERAL pledged (strike * 100 * contracts),
        # never its premium and never its journal `debit`. Preferred source is the journaled
        # collateral_usd the trader itself reserved; then the broker strike; and if neither is
        # readable the position is still COUNTED (that is the whole point) at a notional of 0.0,
        # loudly audited, so an unknown size can never masquerade as a known one in caps #6/#6b.
        #
        # EXCLUDED, deliberately: short CALLS (a spread's short leg is already counted through its
        # long leg's journaled net debit, and a naked call must not exist -- _deployed_collateral
        # audits that separately), and any con_id that is a journaled spread's short_con_id.
        try:
            short_rows = await self.ib_conn.get_positions(include_short=True)
            spread_legs, collateral_by_cid = self._journal_short_context()
            for cid, pd in (short_rows or {}).items():
                try:
                    q = int(getattr(pd, "quantity", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if q >= 0:
                    continue                                  # long leg: already in `out`
                if str(getattr(pd, "sec_type", "OPT") or "OPT").upper() not in ("", "OPT", "FOP"):
                    continue                                  # assigned stock is not a position here
                if str(getattr(pd, "right", "") or "").upper()[:1] != "P":
                    continue                                  # short call: see the note above
                if int(cid) in spread_legs:
                    continue                                  # counted via its long leg
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
                out.append(OpenPosition(sym, float(notional),
                                        sym in {"SPY", "QQQ", "IWM"}, is_credit=True))
                audit(self.audit_path, "short_position_folded", symbol=sym, con_id=int(cid),
                      quantity=q, collateral=round(float(notional), 2))
        except Exception as _se:
            # A read failure UNDER-counts concurrency, which is the permissive direction, so it is
            # never silent. The binding solvency gate for credit is _deployed_collateral(), which
            # returns None (-> refuse) on the same failure.
            audit(self.audit_path, "short_concurrency_read_error", error=str(_se))
        return out

    def _journal_short_context(self):
        """(spread_short_leg_con_ids, {con_id: collateral_usd}) read from trades.log.

        Two things _open_positions needs that _load_journal_debits does not carry: which con_ids
        are a debit spread's SHORT LEG (so a short that is half of a long structure is not counted
        a second time), and the COLLATERAL a credit entry reserved (the honest notional for a CSP,
        as opposed to `debit`, which on a credit row is MAX LOSS). Newest row per con_id wins,
        matching _load_journal_debits. Never raises: an unreadable journal yields empty maps and
        the broker strike is used instead."""
        legs, collateral = set(), {}
        try:
            p = Path(self.journal_path)
            if not p.exists():
                return legs, collateral
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
                cid, coll = rec.get("contract_id"), rec.get("collateral_usd")
                if cid is None or coll is None:
                    continue
                try:
                    c = float(coll)
                    if c > 0:
                        collateral[int(cid)] = round(c, 2)
                except (TypeError, ValueError):
                    continue
        except Exception:
            return legs, collateral
        return legs, collateral

    async def _deployed_collateral(self) -> Optional[float]:
        """Collateral ALREADY reserved against the account, in dollars. `None` == UNVERIFIABLE,
        and every caller must then REFUSE (invariant 3 counts already-deployed collateral; an
        unreadable book may never be treated as an empty one).

        Sources, summed:
          (1) LIVE SHORT OPTION POSITIONS off reqPositionsAsync(). IBConnection.get_positions()
              deliberately filters to LONG options, so a short put is INVISIBLE there -- this
              path reads the raw broker positions instead. Note the direction of the two
              possible errors: over-counting refuses a trade (safe), under-counting sells an
              unsecured put (catastrophic), so anything unparseable returns None.
          (2) WORKING SELL-TO-OPEN ORDERS (ours, by orderRef) that have not filled yet -- their
              collateral is committed the moment the order rests, not when it fills.
          (3) same-cycle accrual (see __init__).

        ASSIGNMENT (invariant 7) is handled by construction, not by a special case: an assigned
        put stops being a short option position and becomes a STOCK position, so it drops out of
        (1) automatically -- the cash is spent, the shares are owned, and the reservation is
        correctly released. Nothing here crashes on, or needs to know about, the assignment."""
        total = 0.0
        try:
            positions = await self.ib_conn.ib.reqPositionsAsync()
            for pos in positions or []:
                c = getattr(pos, "contract", None)
                q = _fnum(getattr(pos, "position", 0), None)
                if c is None or q is None or q >= 0:
                    continue                      # long, flat, or unreadable qty -> not a short
                sec_type = str(getattr(c, "secType", "") or "").upper()
                if sec_type and sec_type not in ("OPT", "FOP"):
                    continue                      # assigned STOCK: cash already spent, no reservation
                right = str(getattr(c, "right", "") or "").upper()[:1]
                if right == "C":
                    # A short CALL must not exist in this account. It reserves no cash, so it is
                    # not added here, but it is a violation of invariant 1 and is surfaced loudly.
                    audit(self.audit_path, "short_call_position_detected",
                          symbol=str(getattr(c, "symbol", "")), con_id=getattr(c, "conId", None),
                          quantity=q, note="INVARIANT 1 VIOLATION: account holds a short call")
                    continue
                if right != "P":
                    continue
                k = _fnum(getattr(c, "strike", None), None)
                if k is None or k <= 0:
                    audit(self.audit_path, "deployed_collateral_unreadable",
                          con_id=getattr(c, "conId", None), strike=getattr(c, "strike", None),
                          note="short put with no usable strike -> refusing to value the book")
                    return None
                total += k * 100.0 * abs(q)
        except Exception as e:
            audit(self.audit_path, "deployed_collateral_error", stage="positions", error=str(e))
            return None
        try:
            trades = await self.ib_conn.ib.reqAllOpenOrdersAsync()
            terminal = {"Cancelled", "ApiCancelled", "Inactive", "Filled"}
            for t in trades or []:
                o = getattr(t, "order", None)
                c = getattr(t, "contract", None)
                st = getattr(getattr(t, "orderStatus", None), "status", None)
                if o is None or c is None:
                    continue
                if str(getattr(o, "action", "") or "").upper() != "SELL" or st in terminal:
                    continue
                order_ref = str(getattr(o, "orderRef", "") or "")
                open_close = str(getattr(o, "openClose", "") or "").upper()
                # Every sell-to-open put consumes cash, including manual/external orders. An
                # explicit sell-to-close frees a long and is the only safe exclusion.
                if open_close == "C" or order_ref.startswith("alfred-exit:"):
                    continue
                if str(getattr(c, "right", "") or "").upper()[:1] != "P":
                    continue
                k = _fnum(getattr(c, "strike", None), None)
                qty = _fnum(getattr(o, "totalQuantity", 0), None)
                if k is None or k <= 0 or qty is None or qty <= 0:
                    audit(self.audit_path, "deployed_collateral_unreadable", stage="working_order",
                          strike=getattr(c, "strike", None),
                          qty=getattr(o, "totalQuantity", None))
                    return None
                total += k * 100.0 * abs(qty)
        except Exception as e:
            audit(self.audit_path, "deployed_collateral_error", stage="open_orders", error=str(e))
            return None
        return round(total + max(0.0, float(getattr(self, "_cycle_credit_collateral", 0.0) or 0.0)), 2)

    async def _credit_capacity(self, r: ResolvedOrder, *, pot=None):
        """INVARIANT 2 + 3, evaluated against LIVE broker state. Returns (SafetyResult, detail).

        `pot` may be passed when the caller already holds a fresh snapshot; otherwise a new one
        is read here -- which is what makes the submit-time call a genuine RE-verification rather
        than a replay of the proposal-time numbers. Any failure to read live state becomes a
        refusal, never an approval."""
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

    async def _underlyings_with_close_in_flight(self) -> Set[str]:
        """Uppercased underlyings that currently have an IN-FLIGHT or RESTING close (SELL-to-close)
        working. Used by GUARDRAIL 2 to DEFER a NEW entry into a name whose exit is still settling,
        so we never stack a second spread on a slot that is mid-close (transient double exposure).
        Two independent sources, unioned:
          (1) RESTING SELL/close orders on the book across ALL clientIds -- via the same
              reqAllOpenOrdersAsync() machinery _open_positions folds resting BUYs with; the
              underlying comes straight off the order's contract.symbol (a spread BAG carries the
              underlying symbol too).
          (2) StateManager in-flight closes (keyed by con_id) -- mapped to an underlying via the
              live position book (con_id -> symbol). A close still in-flight means the position is
              still (partly) open, so it is present in that book.
        Best-effort: any read failure just yields the names we DID find (never raises into the entry
        path). A miss can only FAIL TO DEFER an entry -- it can never loosen a real risk gate."""
        names: Set[str] = set()
        # (1) resting SELL/close orders -> underlying via contract.symbol
        try:
            trades = await self.ib_conn.ib.reqAllOpenOrdersAsync()
            for t in trades or []:
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
            audit(self.audit_path, "close_inflight_orders_error", error=str(_re))
        # (2) StateManager in-flight closes -> underlying via the live position book (con_id->symbol)
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
                    # a con_id with no live position: the close likely already filled (reconcile
                    # pending) -> can't map a name; the resting-order scan above covers a still-working
                    # close, so skipping here is safe (conservative).
        except Exception as _se:
            audit(self.audit_path, "close_inflight_state_error", error=str(_se))
        return names

    def _book_detail(self, positions) -> dict:
        """Per-underlying live state for the brief's Current-book section.

        The strategist used to see only "$X at risk" per name, so it could not tell a winner from
        a loser, how much time was left, or how close a stop was -- and therefore could not weigh
        what it already owns when choosing the next move (Trevor, 2026-08-12). Sourced from data
        that already exists: the exit manager's marks (P&L, DTE, days held, distance to stop) and
        the trade journal (structure, intended hold, entry thesis).

        Fail-soft by construction: any error yields {} and the brief renders exactly as before.
        This is CONTEXT ONLY -- it grants no execution authority and does not touch exit logic.
        """
        try:
            syms = {str(getattr(p, "underlying", "")).upper() for p in (positions or [])}
            if not syms:
                return {}
            sm = getattr(self.exit_manager, "state_manager", None)
            marks = dict(getattr(getattr(sm, "state", None), "mark_path", {}) or {})
            journal = {}
            try:
                with open(self.journal_path) as fh:
                    for line in fh:
                        if not line.strip():
                            continue
                        row = json.loads(line)
                        cid = row.get("contract_id")
                        if cid is not None:
                            journal[str(cid)] = row
            except Exception:
                journal = {}
            out = {}
            for cid, series in marks.items():
                if not series:
                    continue
                row = journal.get(str(cid)) or {}
                sym = str(row.get("symbol") or "").upper()
                if sym not in syms:
                    continue
                last = series[-1]
                out[sym] = {
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
            return out
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
        # research brief (price structure / events / headlines); falls back to the bare
        # quote context if the research layer itself blows up
        try:
            single_names = sorted(self.approved_names
                                  | {p.underlying for p in positions or [] if not p.is_index})
            data = await research.gather(self.ib_conn.ib, names, single_names=single_names)
            # Classify the market regime from the index momentum + VIX gather() already fetched,
            # and keep the per-underlying momentum -- feeds regime-aware sizing + the position manager.
            ps = data.get("price_stats") or {}
            self._price_stats = ps
            self._regime = regime.classify_regime([ps.get("SPY"), ps.get("QQQ"), ps.get("IWM")], data.get("vix"))
            audit(self.audit_path, "regime", **(self._regime or {}))
            brief = research.build_brief(today=today, quotes=quotes, universe=names,
                                         book_detail=self._book_detail(positions),
                                         allow_any_name=self.limits.allow_any_name,
                                         book=positions, day_pnl_pct=day_pnl,
                                         net_liq=net_liq, available_funds=available_funds,
                                         **data)
            audit(self.audit_path, "strategist_brief", brief=brief)
            return brief
        except Exception as e:
            audit(self.audit_path, "research_error", error=str(e))
            fallback = format_context(quotes, names, today,
                                      allow_any_name=self.limits.allow_any_name)
            return research.with_account_sizing_snapshot(
                fallback, net_liq=net_liq, available_funds=available_funds)

    async def _drop_blocked_sectors(self, ideas):
        """Drop single-name ideas whose sector/industry matches a blocked keyword (e.g. biotech).
        Best-effort: a lookup failure lets the name through to human approval. Index ETFs and
        explicit-ticker blocks aren't handled here -- the risk gate's blocked_names covers those."""
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
        """Bridge the frozen Stage A/B contract into the existing risk/approval carrier.

        Every monetary field is copied from the selected executable candidate.  The model never
        authors these values; attaching the binding makes later refreshes conId-exact.
        """
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
        """Build priced candidates and obtain one exact Stage-B choice per intent."""
        ideas = []
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
                audit(self.audit_path, "stage_b_candidate_error", intent_id=intent_id,
                      underlying=getattr(intent, "underlying", None), error=str(exc))
                continue
            _pre_stage_b = len(bindings or [])
            _max_age = float(getattr(self.construction, "stage_b_quote_max_age_s", 0)
                             or entry_safety.DEFAULT_NBBO_MAX_AGE_SECONDS)
            bindings = bindings_for_stage_b(bindings, max_age_seconds=_max_age)
            # Make the staleness cull VISIBLE (2026-08-13). It used to happen silently, which is
            # how a 91% starvation ("0 prefiltered candidates") went unnoticed across two days.
            if _pre_stage_b and len(bindings) < _pre_stage_b:
                print(f"[BUILD] {intent.underlying}: {_pre_stage_b - len(bindings)}/"
                      f"{_pre_stage_b} candidates dropped for stale NBBO "
                      f"(> {_max_age:.0f}s); {len(bindings)} survive")
            if len(bindings) < 3:
                audit(self.audit_path, "stage_b_skipped", intent_id=intent_id,
                      underlying=intent.underlying,
                      reason=f"only {len(bindings)} prefiltered candidates; requires 3")
                continue
            candidates = [binding.candidate for binding in bindings]
            try:
                # thinking="enabled": Stage B picks WHICH priced contract to buy, and it
                # was running flat on select_candidate's `thinking="disabled"` default while
                # Stage A reasoned -- so the selection's cot was captured as null too.
                # Measured 2026-08-15, BFCL multi_turn_base native FC, n=200: Flash scores
                # 63.00% thinking-off vs 75.00% thinking-on, a 12-point swing on exactly this
                # shape of decision. The 1200s cycle absorbs the extra decode.
                result = await asyncio.to_thread(
                    select_candidate, self.endpoint, self.model, intent, candidates,
                    intent_id=intent_id, return_raw=True, return_cot=True,
                    return_identity=True, thinking="enabled")
                selected = result
                raw_b = cot_b = identity_b = None
                if isinstance(result, tuple):
                    selected = result[0] if result else None
                    raw_b = result[1] if len(result) > 1 else None
                    cot_b = result[2] if len(result) > 2 else None
                    identity_b = result[3] if len(result) > 3 else None
            except Exception as exc:
                audit(self.audit_path, "stage_b_error", intent_id=intent_id,
                      underlying=intent.underlying, error=str(exc))
                continue
            if selected is None:
                audit(self.audit_path, "stage_b_declined", intent_id=intent_id,
                      underlying=intent.underlying)
                continue
            selected_id = getattr(selected, "candidate_id", None)
            binding = select_binding(bindings, selected_id)
            if binding is None:
                audit(self.audit_path, "stage_b_invalid_selection", intent_id=intent_id,
                      underlying=intent.underlying, candidate_id=selected_id)
                continue
            idea = self._idea_from_stage_b(intent, binding)
            idea._stage_b_candidates = tuple(candidates)
            idea._stage_b_raw = raw_b
            idea._stage_b_cot = cot_b
            idea._stage_b_identity = identity_b
            ideas.append(idea)
            audit(self.audit_path, "stage_b_selected", intent_id=intent_id,
                  underlying=intent.underlying, candidate_id=selected_id,
                  candidates=len(candidates))
        return ideas

    async def run_once(self, dry_run: bool, *, skip_exit_cycle: bool = False) -> None:
        # SOFT-MUTEX (2026-06-23): if the daily slate is mid-generation on the single-threaded
        # model server, DEFER this tick's exit-management model call so we don't collide/queue
        # behind it. Static stops/targets still run; the model tuning is picked up next tick.
        defer_model = slate_lock.slate_active()
        # CREDIT: same-cycle collateral accrual starts empty every cycle (see __init__).
        self._cycle_credit_collateral = 0.0
        pot = await get_pot_snapshot(self.ib_conn.ib)
        today = _trading_day()   # US/Eastern trading day (P3.9): rolls between sessions, not mid-RTH
        baselines = self._load_baselines()
        baseline, baselines = day_start_pot(baselines, today, pot.net_liq)
        self._save_baselines(baselines)

        dp = day_pnl_pct(pot.net_liq, baseline)
        audit(self.audit_path, "cycle_start", net_liq=pot.net_liq, available=pot.available_funds,
              day_start=baseline, day_pnl_pct=round(dp, 4), dry_run=dry_run)
        if defer_model:
            audit(self.audit_path, "model_deferred", reason="slate_active")

        positions = await self._open_positions()
        context = await self._market_context(
            positions, dp, net_liq=pot.net_liq, available_funds=pot.available_funds)

        # EXITS-FIRST (2026-06-26 reliability fix): manage stops/targets on open positions
        # BEFORE the slow/failure-prone strategist+approval path, so a hung model call (30-min
        # propose timeout) or a pending Slack approval can never delay or skip a stop. The exit
        # cycle only needs positions + regime/price_stats (set by _market_context above) and
        # defer_model (set at top of run_once).
        if not skip_exit_cycle:
            try:
                await self.exit_manager.run_cycle(
                    dry_run, regime=self._regime, price_stats=self._price_stats,
                    defer_model=defer_model)
                self._exit_fail_streak = 0
            except Exception as e:
                self._exit_fail_streak += 1
                audit(self.audit_path, "exit_cycle_error", error=str(e), streak=self._exit_fail_streak)
                # NO SILENT FAILURE (2026-07-03): a failing exit cycle means positions may be going
                # unmanaged -- Slack it, and after N in a row suppress new entries (below) so we don't
                # pile on more positions we can't exit.
                try:
                    approval.post_proposal(self.slack_token, self.slack_channel,
                        f":warning: *Exit cycle FAILED* ({self._exit_fail_streak}x consecutive): {e}"
                        + (f"\n_New entries SUPPRESSED until exits recover._"
                           if self._exit_fail_streak >= self._EXIT_FAIL_SUPPRESS_ENTRIES else ""))
                except Exception as _se:
                    print(f"[WARN] exit-cycle-failure Slack alert failed: {_se}")

        # Market-closed guard (2026-06-28): skip the strategist/model call when the market is
        # closed — nothing can be entered, so there's nothing to propose; spares the single-
        # threaded model server a ~20k-token brief every 15 min on weekends/overnight. Exits-
        # first (above) still runs every tick; resumes automatically at the next open.
        # ENTRY HALT GATES (2026-07-03): halt NEW entries (exits already ran above) when
        #   * the KILL_SWITCH file is present (previously only gated exits);
        #   * the exit manager's most recent per-cycle reconcile was UNSAFE (don't open into an
        #     inconsistent book);
        #   * the exit cycle has failed N times in a row (don't pile on unmanageable positions).
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

        _raw_strategist = None      # verbatim model output, for decision/no-trade capture (v2)
        _cot = None                 # [m3cot] chain-of-thought (reasoning_content), separate from the answer
        _model_identity = None      # immutable artifact/runtime/prompt/request/context hashes
        if not _market_open():
            audit(self.audit_path, "strategist_skipped", reason="market_closed")
            ideas = []
        elif _entries_halted:
            audit(self.audit_path, "strategist_skipped", reason=_halt_reason)
            ideas = []
        else:
            try:
                # STAGE A: the model authors intent only.  It cannot guess a premium, strike,
                # expiry, conId, or quantity.  The priced Stage-B candidates are built from IBKR
                # immediately afterward and the same model may only select an immutable ID.
                # thinking="enabled": the entry loop reasons explicitly. It defaulted to OFF
                # (propose_intents only turns thinking on for recommend/ticker calls), so every
                # live entry decision was made flat AND captured with cot=null -- the reasoning
                # behind real-money entries was never recorded. The 1200s cycle absorbs the extra
                # decode easily; strategist raises the token budget for this path to match.
                _res = await asyncio.to_thread(
                    propose_intents, self.endpoint, self.model, context,
                    thinking="enabled", return_cot=True, return_identity=True)
                # Robust to capture-enabled and minimal/mocked result shapes.
                if isinstance(_res, tuple) and len(_res) == 4:
                    intents, _raw_strategist, _cot, _model_identity = _res
                elif isinstance(_res, tuple) and len(_res) == 3:
                    intents, _raw_strategist, _cot = _res
                elif isinstance(_res, tuple) and len(_res) == 2:
                    intents, _raw_strategist = _res
                else:
                    intents = _res
                # DETERMINISTIC CONSTRUCTION (2026-08-16). Expiry/delta come from the rule,
                # not the model. Measured on duel at n=759: the deterministic arm scored
                # +12.4 GNA vs +5.9 for the best-scaffolded model and -5.0 unguided, while
                # direction skill was statistically identical (p=0.77) -- so the model's
                # name/side call is kept and its expiry choice is discarded. Never raises
                # into the trading path; on any failure the model's own numbers survive.
                try:
                    if getattr(self.construction, "deterministic_construction", True):
                        from exitmgr.construction import apply_deterministic_construction
                        _mind = int(getattr(self.construction, "min_dte", 25) or 25)
                        for _i in (intents or []):
                            _before = getattr(_i, "target_dte", None)
                            apply_deterministic_construction(_i, _mind, enabled=True)
                            _after = getattr(_i, "target_dte", None)
                            if _before != _after:
                                audit(self.audit_path, "deterministic_construction",
                                      underlying=getattr(_i, "underlying", None),
                                      intended_hold_days=getattr(_i, "intended_hold_days", None),
                                      model_target_dte=_before, rule_target_dte=_after,
                                      model_target_delta=getattr(_i, "model_target_delta", None),
                                      rule_target_delta=getattr(_i, "target_delta", None))
                except Exception as _dce:
                    audit(self.audit_path, "deterministic_construction_error", error=str(_dce))
                ideas = await self._materialize_stage_b(intents, pot)
            except Exception as e:
                audit(self.audit_path, "strategist_error", error=str(e))
                ideas = []
        audit(self.audit_path, "proposals", count=len(ideas))

        # TAKE-PROFIT-AND-RELOAD (2026-07-03): drain ready reload tickets into synthetic high-priority
        # same-name suggestions. OFF BY DEFAULT (reload_enabled=False => no-op). Sits AFTER the
        # kill-switch / reconcile-halt / exit-fail-streak entry gates (skipped when entries are halted)
        # and only when the market is open. The reload ideas are PREPENDED so they run first, and then
        # flow through EXACTLY the same _drop_blocked_sectors -> risk gate -> construct -> throttle ->
        # G1 fresh-book / G2 in-flight-defer -> Slack-approve -> submit path as strategist ideas (so
        # they can never bypass a cap or the human approval). Never raises into the trading path.
        if self.reload_enabled and not _entries_halted and _market_open():
            try:
                _reload_ideas = self._drain_reload_ideas(today)
            except Exception as _rde:
                audit(self.audit_path, "reload_drain_error", error=str(_rde))
                _reload_ideas = []
            if _reload_ideas:
                ideas = list(_reload_ideas) + list(ideas)
                audit(self.audit_path, "reload_ideas_drained", count=len(_reload_ideas),
                      symbols=[i.underlying for i in _reload_ideas])

        # RECORD-ONLY (v2): learn from PASSES too -- when the model proposes nothing (market
        # closed or an empty/silent slate), capture a light NO_TRADE row with the raw output +
        # context + regime. Never raises into the trading path.
        try:
            if not ideas:
                trade_capture.capture_no_trade(
                    trade_capture.dataset_dir(self.journal_path), source="trader",
                    reason=("market_closed" if not _market_open() else "empty_slate"),
                    raw_strategist=_raw_strategist, cot=_cot, candidates=None,
                    regime=self._regime, market_context=context)
        except Exception as _ce:
            print(f"[WARN] no_trade capture failed (continuing): {_ce}")

        ideas = await self._drop_blocked_sectors(ideas)

        # 2026-07-01 budget gates value the open book as (net_debit, dte) pairs -- the
        # deployed-premium (<=40% net-liq) and portfolio-decay (<=4%/day) caps need DTE,
        # which OpenPosition doesn't carry. One extra positions fetch, only when there are ideas.
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

        def _cap_rej(stage, reason, *, resolved=None, gate=None, construction=None):
            """Record-only: append a REJECTED row for a killed idea. Never raises."""
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

        # ENTRY THROTTLE CEILINGS (2026-07-03 gap-fix): enforce caps.max_orders_per_cycle /
        # max_orders_per_day / max_notional_per_day on NEW entries (previously only max_concurrent
        # bound). Per-cycle count is this run_once's submitted entries; per-day figures come from the
        # persisted DailyStats (orders_opened / notional_opened), keyed by the SAME US/Eastern trading
        # day used elsewhere. Ceilings only ADD safety -- they skip an idea, never up-size one.
        _orders_this_cycle = 0
        _sm = getattr(self.exit_manager, "state_manager", None)
        # LOST-UPDATE FIX (2026-08-12). The day-open counters used to be persisted INTO the shared
        # exitmgr_state.json. StateManager loads that file exactly once per process (lazy `_state`,
        # no reload path anywhere) and every save() re-serialises that process's WHOLE in-memory
        # State -- so the entry process's save flushed an hours-old snapshot over every peak_price
        # and in_flight close the protective loop had recorded since entry started, and the
        # protective loop's own 30s save erased these counters right back. orders_opened /
        # notional_opened are entry-only data, so they now live in their own small file with its own
        # private lock. The protective loop never touches that file or that lock -- this cannot
        # block or deadlock exit management. See exitmgr/entry_throttle.py.
        _throttle_store = None
        try:
            if _sm is not None:
                _throttle_store = EntryThrottleStore.for_state_path(_sm.state_path)
        except Exception as _tse:
            print(f"[WARN] entry throttle store unavailable (continuing): {_tse}")

        def _day_open_counts():
            """(orders_opened_today, notional_opened_today): MAX of the durable throttle file and
            this process's in-memory State. Fail-conservative -- max() can only bind a cap sooner."""
            return entry_day_open_counts(_sm, _throttle_store, today)

        # GUARDRAIL 1 (2026-07-03 order-state fix): the entry RISK gate must evaluate exposure off
        # the FRESH POST-EXIT book, NOT the stale `positions` fetched at the top of run_once BEFORE
        # exit_manager.run_cycle ran. A name CLOSED during THIS cycle's exit run is otherwise still
        # counted against max_concurrent / single-name-agg / sector caps -- wrongly blocking a legit
        # new entry, and (critically) blocking a same-name re-entry on its own just-vacated slot.
        # Re-fetch the book (same _open_positions() used pre-exit, so fresh vs stale can't diverge in
        # shape; it also folds resting BUYs). Fall back to the stale `positions` on any error -- the
        # stale book OVER-counts, so a fallback can only BLOCK, never loosen a gate. Intra-cycle
        # sequential gating is PRESERVED: accepted fills are appended to THIS `entry_positions` list
        # below, so later ideas in the same cycle still see earlier fills.
        entry_positions = positions
        if ideas:
            try:
                entry_positions = await self._open_positions()
            except Exception as _pfe:
                audit(self.audit_path, "entry_book_refetch_error", error=str(_pfe))
                entry_positions = positions

        # GUARDRAIL 2 (2026-07-03 order-state fix): compute, ONCE per cycle, the set of underlyings
        # that currently have an IN-FLIGHT or RESTING close (SELL-to-close) working. A new entry into
        # such a name would stack a second spread on a slot that is still mid-exit -> transient double
        # exposure in that name. Any idea in one of these names is DEFERRED below (fires a later cycle
        # once the underlying is confirmed flat/settled). Best-effort read; a miss can only fail to
        # defer, never loosen a gate.
        _closing_names = await self._underlyings_with_close_in_flight() if ideas else set()

        for idea in ideas:
            if _budget_snapshot_error is not None:
                _cap_rej("budget_snapshot", "working BUY/deployment snapshot unavailable; entry blocked")
                continue
            # INVARIANT 1, enforcement point #1 (of three: here, _resolve_credit_order, and
            # _submit_order_unlocked). A short structure that is not a cash-secured put is killed
            # at the very top of the entry loop -- before the risk gate, before any contract is
            # qualified, before any order object can exist.
            _ok_credit, _why_credit = credit_structure_ok(idea)
            if not _ok_credit:
                audit(self.audit_path, "credit_structure_rejected", underlying=idea.underlying,
                      structure=getattr(idea, "structure", None),
                      side=getattr(idea, "side", None), reason=_why_credit)
                _cap_rej("credit_structure", _why_credit)
                approval.post_proposal(self.slack_token, self.slack_channel,
                    f":no_entry: REFUSED *{idea.underlying}* — {_why_credit}")
                continue
            # DEBIT STRUCTURE GATE, enforcement point #1 (of three: here, _resolve_order, and
            # _submit_order_unlocked). Runs at the same place, and on the same terms, as the credit
            # check above: an idea whose structure is not on the allow-list -- or whose structure
            # contradicts its direction -- is killed before the risk gate and before any contract
            # is qualified. This catches every route that builds a TradeIdea, including the three
            # that never touch the strategist's parser.
            _ok_debit, _why_debit = debit_structure_ok(idea)
            if not _ok_debit:
                audit(self.audit_path, "debit_structure_rejected", underlying=idea.underlying,
                      structure=getattr(idea, "structure", None),
                      direction=getattr(idea, "direction", None), reason=_why_debit)
                _cap_rej("debit_structure", _why_debit)
                approval.post_proposal(self.slack_token, self.slack_channel,
                    f":no_entry: REFUSED *{idea.underlying}* — {_why_debit}")
                continue
            # GUARDRAIL 2: DEFER an entry whose underlying has an in-flight/resting close in progress.
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
                continue

            # Resolve the CONCRETE order BEFORE asking -- so the human approves the real order.
            try:
                resolved = await self._resolve_order(
                    idea, plan.gate.per_trade_cap,
                    net_liq=pot.net_liq, available_funds=pot.available_funds)
            except Exception as e:
                audit(self.audit_path, "resolve_error", underlying=idea.underlying, error=str(e))
                _cap_rej("resolve_error", str(e), gate=plan.gate)
                continue
            if resolved is None:
                audit(self.audit_path, "resolve_failed", underlying=idea.underlying)
                _cap_rej("resolve_failed", "no usable contract (construction/chain)", gate=plan.gate)
                continue
            resolved.conviction = getattr(idea, "conviction", -1.0)  # carry conviction into the journal
            resolved.intended_hold_days = getattr(idea, "intended_hold_days", None)
            try:  # carry the entry thesis into the journal (non-blocking)
                resolved.thesis = str(getattr(idea, "thesis", "") or "")
            except Exception as _te:
                print(f"[WARN] thesis carry failed (continuing): {_te}")
            try:  # carry the entry-time technical_card into the journal so a later reload/continuation
                  # call is NOT price-only (fed back to position_manager via _build_position_views).
                resolved.technical_card = (self._price_stats or {}).get(idea.underlying)
            except Exception as _tce:
                print(f"[WARN] technical_card carry failed (continuing): {_tce}")

            # CONSTRUCTION GATES (2026-07-01). TP/SL clamp: +75-100% targets were touched
            # 0/9 times -- clamp any model target into the 25-35% band (default +30%); stop
            # defaults -30% and may only be tighter. Then the budget gates: premium <=15% of
            # net-liq (downsize qty first), deployed premium <=40%, theta decay <=1%/day per
            # trade (<=4%/day portfolio).
            # POT-TIERED TP CEILING (2026-07-03): scale ONLY the runner ceiling + default target
            # with the LIVE pot; per-call cons copy so the shared self.construction is untouched.
            # Stop (sl_pct) + tp_min are unchanged; empty tiers => flat no-op.
            _tp_max, _tp_def = construction.tp_tier_for_pot(
                pot.net_liq, self.caps_tp_tiers, self.construction.tp_max_pct, self.construction.tp_pct)
            _cons_tp = _replace_dc(self.construction, tp_max_pct=_tp_max, tp_pct=_tp_def)
            resolved.tp_pct, resolved.sl_pct = construction.clamp_tp_sl(
                getattr(idea, "profit_target_pct", 0.0), getattr(idea, "stop_pct", 0.0),
                _cons_tp)
            # RELOAD FRICTION GATE (2026-07-03, anti-churn): a reload suggestion clears ONLY if the
            # model's reload_conviction >= reload_conviction_min AND the expected continuation
            # (clamped tp% x new debit) exceeds reload_friction_k x (fresh-entry commission + one-cycle
            # theta + entry slippage). Rejects a churn that just feeds the broker. Applies ONLY to
            # reload ideas; strategist ideas are untouched. Uses the RESOLVED order (final qty/limit/
            # tp% + captured greeks/liquidity), so it runs here, right after the tp/sl clamp.
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
            # BUDGET GATES: check_budget measures LONG PREMIUM -- premium paid, total deployed
            # premium, and theta DECAY (debit/DTE). A cash-secured put pays no premium and COLLECTS
            # theta, so passing its collateral in would both double-count against the debit book's
            # 40% cap and produce a nonsense decay figure that rejects every CSP outright. Credit
            # therefore contributes 0 premium / 0 DTE here -- the open book is still evaluated
            # unchanged -- and its capital is bound instead by the RISK GATE (per-trade cap,
            # buying power, concurrent, breaker, single-name + sector caps, all measured on the
            # FULL collateral in plan_idea) plus the 80%-of-net-liq collateral cap below.
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

            # COLLATERAL RESERVATION (spec S2, invariants 2 + 3) -- PROPOSAL-TIME check. This is
            # the FIRST of two: refusing here means an unaffordable put is never even offered to
            # the human. It is NOT the binding one -- _submit_order_unlocked re-verifies against
            # live account + short-position state immediately before placeOrder, because cash can
            # be spent between approval and submit. A refusal is a correct outcome.
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

            # EARNINGS BLACKOUT (2026-07-03 gate A5): a DEBIT held THROUGH an earnings print is an
            # IV-crush loser by construction (IV collapses post-print; the long premium bleeds even
            # when direction is right). Block if a KNOWN next-earnings date (research.days_to_earnings
            # via yfinance) lands within the holding horizon (on/before expiry + cushion). FAIL-OPEN
            # on unknown earnings: never hard-block, but flag it 'unchecked' (surfaced below +
            # journaled) so an unchecked trade is never treated as verified-clear of earnings.
            resolved.earnings_unchecked = False
            _entry = datetime.now(timezone.utc).date()
            try:
                # OFF-LOOP: blocking yfinance lookup -> worker thread (don't stall the IBKR loop).
                _edays = await asyncio.to_thread(research.days_to_earnings, idea.underlying)
            except Exception as _ee:
                print(f"[WARN] earnings lookup failed for {idea.underlying} (fail-open, unchecked): {_ee}")
                _edays = None
            _earn_date = (_entry + timedelta(days=_edays)) if _edays is not None else None
            resolved.earnings_unchecked = _earn_date is None
            # hold_days lets the gate judge the window the position is actually OPEN rather
            # than the expiry -- required once expiry runs ~8x the hold. No-op while
            # construction.earnings_use_hold_window is false.
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
            if resolved.earnings_unchecked:
                # A5: no earnings date available -- the IV-crush blackout could NOT be checked.
                # Surfaced (never silent-clear) via audit + Slack note + journal field so an
                # unchecked entry is never presented as verified-clear of earnings.
                audit(self.audit_path, "earnings_unchecked", underlying=idea.underlying,
                      order=order_summary(resolved))

            # EARLY-ASSIGNMENT / EX-DIV RISK (2026-07-03 gate A6): a DEBIT SPREAD whose ITM short
            # leg heads into an ex-dividend date can be assigned EARLY (a counterparty exercises
            # the ITM short to grab the dividend, converting the spread). Applies ONLY to spreads
            # -- a single long leg has no short to be assigned. Default disposition is WARN (surface
            # the risk, still allow); hard-block only when construction.assignment_block_hard is set.
            # FAIL-OPEN on an unknown ex-div date, flagged 'unchecked' (never silent-clear).
            resolved.assignment_warn = ""
            resolved.assignment_unchecked = False
            if resolved.short_contract is not None:
                _entry_a = datetime.now(timezone.utc).date()
                try:
                    # OFF-LOOP: blocking yfinance lookup -> worker thread.
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

            # ENTRY THROTTLE (2026-07-03 gap-fix): refuse a NEW entry that would breach a per-cycle
            # or per-day ceiling BEFORE asking the human, so an over-cap idea is simply not offered.
            # resolved.qty/limit are final here (premium downsizing already applied). None => disabled.
            _throttle = None
            # capital_committed == the debit paid, or (credit) the FULL reserved collateral -- so a
            # CSP consumes the daily notional ceiling in proportion to the cash it locks up.
            _cost_throttle = capital_committed(resolved)
            _od_today, _nd_today = _day_open_counts()
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
            resolved.model_identity = _model_identity
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
                    model_identity=_model_identity, final_contract=contract_snapshot(resolved))
            except Exception as _capture_error:
                print(f"[WARN] proposal capture failed (continuing): {_capture_error}")
            _approval_ttl = min(entry_safety.DEFAULT_APPROVAL_TTL_SECONDS,
                                max(1, int(self.approve_timeout_s)))
            msg = approval.format_proposal(idea, pot.net_liq, plan.gate.per_trade_cap,
                                           max(1, _approval_ttl // 60), order_summary(resolved))
            if getattr(idea, "is_reload", False):
                # RELOAD: make it unmistakable in Slack that this is a same-name RE-ENTRY on
                # continued conviction (banked the prior winner; fresh basis + fresh 30% stop).
                msg = (f":arrows_counterclockwise: *RELOAD / continuation* — re-entering "
                       f"*{idea.underlying}* after banking a take-profit "
                       f"(model reload_conviction {getattr(idea, 'reload_conviction', '?')}).\n") + msg
            if getattr(resolved, "earnings_unchecked", False):
                # A5: never present an unchecked trade as verified-clear of earnings.
                msg += "\n:grey_question: earnings date unknown — IV-crush blackout UNCHECKED"
            if getattr(resolved, "assignment_warn", ""):
                # A6 (warn disposition): surface the ITM-short ex-div early-assignment risk.
                msg += f"\n:warning: {resolved.assignment_warn}"
            elif getattr(resolved, "assignment_unchecked", False):
                # A6: never present a spread as verified-clear of assignment risk when ex-div is unknown.
                msg += "\n:grey_question: ex-dividend date unknown — early-assignment risk UNCHECKED"
            msg += (f"\n_Decision ID: `{resolved.decision_id}` — approval expires in "
                    f"{_approval_ttl // 60 or 1} minutes._")
            if dry_run:
                approval.post_proposal(self.slack_token, self.slack_channel,
                                       "[DRY RUN — nothing will be submitted]\n" + msg)
                audit(self.audit_path, "dry_run_proposal", underlying=idea.underlying,
                      order=order_summary(resolved))
                continue

            # RISK SCREEN (live only): block genuinely oversized orders. This account is
            # long-debit-only (long calls/puts + debit spreads), so the capital actually at risk
            # is the NET DEBIT (max loss = limit*100*qty), NOT the strike notional. The old screen
            # measured strike notional (~1.1x single / ~2.4x sum-of-strikes spread) vs 30x NetLiq,
            # which structurally rejected every cheap defined-risk debit spread (e.g. ORCL
            # 162.5/160P @ $1.25 = $125 risk read as ~$32k). Measure the real max loss instead;
            # available_funds already bounds it to the pot. Skip BEFORE asking the human.
            # capital_at_risk == the net debit, or (credit) collateral - credit received. Same
            # ceiling, same meaning: the most this order can lose.
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

            # AUTO-APPROVE (2026-08-14). The risk gate has already cleared this trade and
            # sized it; waiting on a Slack tap adds latency, not safety. Skip the wait ONLY when
            # the gate approved with ZERO reasons and capital at risk is inside the gate's own
            # per-trade cap. Everything below still rebuilds from a fresh account/chain/NBBO and
            # re-runs every hard gate, so this removes the human tap and no risk check.
            _auto_ok = (self.auto_approve_within_gates
                        and bool(getattr(plan.gate, "approved", False))
                        and not list(getattr(plan.gate, "reasons", []) or [])
                        and _est_gross > 0
                        and _est_gross <= float(getattr(plan.gate, "per_trade_cap", 0) or 0))
            if _auto_ok:
                # Still post -- as a notification, not a request. The book must never change
                # silently just because no human was in the loop.
                approval.post_proposal(
                    self.slack_token, self.slack_channel,
                    ":robot_face: *AUTO-APPROVED (within risk gates)* — submitting now. "
                    "_This is a receipt; no approval is being awaited._\n" + msg,
                    seed_reactions=False)
                audit(self.audit_path, "auto_approved", underlying=idea.underlying,
                      order=order_summary(resolved), decision_id=resolved.decision_id,
                      est_gross=round(_est_gross), per_trade_cap=round(
                          float(getattr(plan.gate, "per_trade_cap", 0) or 0)))
                _posted_at = time.monotonic()
                decision = "approve"
            else:
                ts = approval.post_proposal(self.slack_token, self.slack_channel, msg)
                if not ts:
                    audit(self.audit_path, "slack_post_failed", underlying=idea.underlying)
                    continue
            # OFF-LOOP (2026-07-03): await_approval BLOCKS (polls Slack + sleeps) for up to the
            # approve timeout; run it in a worker thread so the IBKR event loop / exit I/O isn't
            # starved for minutes while a human decides.
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

            # The approval binds to the displayed terms, not merely the ticker. Rebuild from a new
            # account/chain/NBBO and rerun every hard gate. A material change gets one new proposal
            # and a second explicit tap; continued movement aborts rather than looping forever.
            fresh, _pot2, _final_reasons = await self._refresh_approved_entry(
                idea, resolved, baseline)
            if _final_reasons or fresh is None:
                audit(self.audit_path, "final_entry_gate_blocked", underlying=idea.underlying,
                      decision_id=resolved.decision_id, reasons=_final_reasons)
                approval.post_proposal(self.slack_token, self.slack_channel,
                    f":no_entry: Approved *{idea.underlying}* was NOT submitted — final hard gate: "
                    + "; ".join(_final_reasons or ("fresh order unavailable",)))
                continue
            # CREDIT: a SELL-to-open is re-priced off the BID (and its reserved collateral must
            # not have moved), so it gets the mirror-image comparison. Debit path unchanged.
            _changes = (credit_material_changes(resolved, fresh) if is_credit(resolved)
                        else entry_safety.material_changes(resolved, fresh))
            if _changes:
                _remsg = (f":repeat: *Reapproval required — {idea.underlying}*\n"
                           f"Refreshed order: `{order_summary(fresh)}`\n"
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
                _reposted_at = time.monotonic()
                _rdecision = await asyncio.to_thread(
                    approval.await_approval, self.slack_token, self.slack_channel, _rts,
                    self.approver_ids, _approval_ttl)
                audit(self.audit_path, "reapproval", underlying=idea.underlying,
                      decision_id=resolved.decision_id, decision=_rdecision, changes=_changes)
                if _rdecision != "approve" or not entry_safety.approval_expired(
                        _reposted_at, ttl_seconds=_approval_ttl).allowed:
                    continue
                fresh2, _pot3, _recheck_reasons = await self._refresh_approved_entry(
                    idea, fresh, baseline)
                if _recheck_reasons or fresh2 is None:
                    audit(self.audit_path, "reapproval_gate_blocked", underlying=idea.underlying,
                          decision_id=resolved.decision_id, reasons=_recheck_reasons)
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
            resolved = fresh

            # Adjacent marker stat: if a halt flips after refresh, _submit_order also refuses.
            _marker_now = self._entry_markers_clear()
            if not _marker_now.allowed:
                audit(self.audit_path, "marker_blocked_submit", underlying=idea.underlying,
                      decision_id=resolved.decision_id, reasons=_marker_now.reasons)
                continue
            try:
                status, reasons = await self._submit_order(resolved)
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
                    # DECISION CAPTURE (v2, record-only): the full DECISION -> ENTRY context for an
                    # ENTERED trade -- raw strategist reasoning, EVERY candidate + conviction, the
                    # chosen idea, the risk GateDecision + bound caps, construction adjustments,
                    # regime, and the RAG/news/journal brief. con_id is known here (resolved.contract);
                    # joined into the closed-trade v2 record at close. Never raises into the loop.
                    try:
                        trade_capture.capture_decision(
                            _ddir, source="trader", symbol=idea.underlying,
                            right=resolved.right, strike=resolved.strike, expiry=resolved.expiry,
                            structure=(CSP_STRUCTURE if is_credit(resolved) else
                               ("spread" if resolved.short_contract is not None else "single")),
                            con_id=getattr(resolved.contract, "conId", None),
                            chosen_idea=idea, candidates=ideas, raw_strategist=_raw_strategist, cot=_cot,
                            gate=plan.gate, regime=self._regime, market_context=context,
                            technical_card=self._price_stats,  # per-name technical indicators fed to the model
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
                            final_contract=contract_snapshot(resolved),
                            order_ref=entry_safety.decision_order_ref(resolved.decision_id),
                            human_action={"action": "approve"})
                    except Exception as _dce:
                        print(f"[WARN] capture_decision failed (continuing): {_dce}")
                    # GUARDRAIL 1: append to the FRESH entry book so later ideas THIS cycle see this
                    # fill (intra-cycle sequential gating preserved on the post-exit book).
                    _resolved_cost = capital_committed(resolved)
                    entry_positions.append(OpenPosition(idea.underlying, _resolved_cost, idea.is_index))
                    if is_credit(resolved):
                        # CREDIT: accrue the reserved collateral so a SECOND cash-secured put this
                        # cycle is measured against a book that already contains this one (the live
                        # short-position read can lag a just-submitted order). It is deliberately
                        # NOT added to _budget_items: that book is the LONG-PREMIUM deployment /
                        # theta-decay ledger, and a CSP's collateral is neither.
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
                    # ENTRY THROTTLE accrual (2026-07-03 gap-fix): count this submitted entry against
                    # the per-cycle counter and the persisted per-day opened order/notional aggregates
                    # so subsequent ideas (this cycle and later cycles today) see the updated ceiling.
                    _orders_this_cycle += 1
                    try:
                        # LOST-UPDATE FIX (2026-08-12): accrue in memory (so later ideas THIS cycle
                        # see it, exactly as before) and persist to the ENTRY-ONLY throttle file.
                        # Deliberately NO _sm.save() here -- that call flushed this process's stale
                        # whole-State snapshot over the protective loop's peaks / in-flight closes.
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
        """Rebuild and hard-gate an approved BUY from current broker state.

        Returns ``(fresh_order, fresh_pot, reasons)``. Any exception becomes a blocking reason;
        this method never converts unavailable risk data into approval.
        """
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
            # Slow/error-prone earnings lookup happens before the final contract request so the
            # NBBO timestamp remains tight at the money boundary.
            days = await asyncio.to_thread(research.days_to_earnings, idea.underlying)
            if days is None:
                reasons.append("earnings date unavailable at approval time")
            fresh = await self._resolve_order(
                idea, gate.per_trade_cap,
                net_liq=pot.net_liq, available_funds=pot.available_funds)
            if fresh is None:
                reasons.append("fresh contract/NBBO resolution returned no order")
            else:
                if _stage_b_bound:
                    # Repricing updates the idea's runtime-authored dollars. Re-run the risk gate
                    # on those exact fresh totals; the pre-requote values are not authoritative.
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
                fresh.conviction = original.conviction
                fresh.intended_hold_days = getattr(original, "intended_hold_days", None)
                fresh.thesis = original.thesis
                fresh.tp_pct = original.tp_pct
                fresh.sl_pct = original.sl_pct
                fresh.technical_card = getattr(original, "technical_card", None)
                reasons.extend(entry_safety.nbbo_valid(fresh).reasons)
                # STRUCTURE: a refreshed DEBIT order must still carry a permitted structure that
                # agrees with its direction. A no-op for credit ideas (their gate is just below).
                _ok_rstruct, _why_rstruct = debit_structure_ok(idea)
                if not _ok_rstruct:
                    reasons.append(_why_rstruct)
                # INVARIANT 1: a refreshed order must still be the cash-secured put that was
                # approved -- never a call, never a multi-leg short.
                if is_credit(fresh):
                    ok_fresh, why_fresh = credit_structure_ok(idea)
                    if not ok_fresh:
                        reasons.append(why_fresh)
                    if str(fresh.right).upper()[:1] != "P" or fresh.short_contract is not None:
                        reasons.append("NAKED-SHORT REFUSED: refreshed credit order is not a "
                                       "single-leg cash-secured put")
                    # INVARIANTS 2+3 re-verified post-approval, against this fresh account read.
                    cap_ok, cap_detail = await self._credit_capacity(fresh, pot=pot)
                    if not cap_ok.allowed:
                        reasons.extend(cap_ok.reasons)
                budget_items = construction.open_book_items(
                    raw_positions, self.journal_path, working_orders)
                budget_items.update(getattr(self, "_same_cycle_budget_items", {}) or {})
                # see the check_budget note in run_once: a CSP contributes no long premium and no
                # decay, so it is measured 0/0 here too (its capital is bound by the risk gate on
                # full collateral + the 80% collateral cap above).
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
                    if not earn_ok:
                        reasons.append(earn_reason)
        except Exception as exc:
            reasons.append(f"fresh account/contract/NBBO/risk/earnings gate failed: {exc}")
        return fresh, pot, tuple(dict.fromkeys(str(r) for r in reasons if r))

    def _drain_reload_ideas(self, today: str) -> List[TradeIdea]:
        """Drain ready reload tickets into synthetic TradeIdea suggestions (2026-07-03).
        Consume-once + TTL + per-name-per-day depth cap are enforced by ReloadQueue.drain(). Each
        idea is tagged is_reload so the friction gate + Slack banner fire, then it flows through the
        SAME entry path as a strategist idea. A fresh entry journals its OWN debit + 30% stop, so the
        basis/stop re-anchor to the new position automatically (banked gain can't be given back)."""
        q = reload_queue.ReloadQueue(reload_queue.queue_path(self.journal_path))
        ready, summary = q.drain(today=today, max_per_name=self.reload_max_per_name_per_day)
        if summary.get("expired") or summary.get("capped"):
            audit(self.audit_path, "reload_tickets_dropped",
                  expired=summary.get("expired", 0), capped=summary.get("capped", 0))
        ideas: List[TradeIdea] = []
        for t in ready:
            right = (t.get("right") or "C").upper()
            direction = "bullish" if right == "C" else "bearish"
            # "spread"/"single" in the ticket -> the structure string _resolve_order keys on.
            structure = "debit spread" if t.get("structure") == "spread" else "long option"
            dte = int(t.get("dte_target") or getattr(self.construction, "min_dte", 30) or 30)
            # budget the reload to the ORIGINAL spend when known (bounded further by the per-trade
            # cap in the gate); unknown -> a large sentinel so ONLY the per-trade cap binds.
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
            # dynamic tags read by the friction gate + Slack banner (dataclass has no slots).
            idea.is_reload = True
            idea.reload_conviction = conv
            # EXPECTED CONTINUATION for the friction gate -- a MEASURED CONSTANT.
            # This replaced resolved.tp_pct, which has been None on every order since the R5/R2
            # take-profit ruling and made the gate's numerator a structural zero (every reload
            # silently rejected since 2026-07-26). My first replacement used the ticket's own
            # banked gain; byron refuted it (corr(leg1,leg2) = -0.05/-0.00/+0.09 over 5,712 setups
            # per arm -- the prior leg's size says nothing about the next). What IS measurable is
            # the LEVEL: ~+3% mean on a same-name re-entry the day a harvest filled. The ticket's
            # realized_pnl stays on the record below as evidence; it just no longer forecasts.
            idea.reload_expected_continuation_pct = self.reload_expected_continuation_pct
            idea.reload_realized_pnl = t.get("realized_pnl")          # audit only
            idea.reload_original_debit = t.get("original_debit")      # audit only
            # BYPASS 2 CLOSED (2026-07-26): this constructs a TradeIdea directly, so the
            # strategist's allow-list never saw it. The mapping above only ever emits
            # "debit spread" or "long option" -- both permitted, and neither names an option
            # right -- so NOTHING that is correct today changes. The gate is here so a later edit
            # to that mapping, or a ticket field that starts reaching it, cannot reintroduce a
            # mislabelled idea. A bad ticket is DROPPED and audited, never rewritten to a default.
            _ok_reload, _why_reload = debit_structure_ok(idea)
            if not _ok_reload:
                audit(self.audit_path, "reload_structure_rejected", underlying=idea.underlying,
                      structure=idea.structure, direction=idea.direction, reason=_why_reload)
                continue
            ideas.append(idea)
        return ideas

    async def _resolve_credit_order(self, idea: TradeIdea,
                                    per_trade_cap: float) -> Optional[ResolvedOrder]:
        """Resolve a CASH-SECURED PUT (spec S2). Returns a ResolvedOrder (nothing placed) or None.

        Deliberately NOT a copy of the debit resolver: a CSP is defined by its STRIKE (the idea
        supplies it), not by a delta search, so there is no strike hunting, no spread short-leg
        selection, and no lottery-long check to get wrong. Refusal returns None -- a correct
        outcome, audited, never an exception."""
        from exitmgr.ibkr import Option, Stock, pick_chain, underlying_price
        from exitmgr.market import usable_price
        # INVARIANT 1, enforcement point #1 (there are three): a short that is not a cash-secured
        # put never gets as far as qualifying a contract.
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
        # STRUCTURE-AWARE DTE FLOOR (spec S3): a CSP COLLECTS theta, so it gets its own 3-45 DTE
        # write window instead of the 25-DTE long-premium floor. The bounds are NOT chosen here --
        # pick_expiry_for_side is construction.py's sanctioned dispatcher and derives them from
        # `side`, which is what makes it structurally impossible for a debit idea to be handed the
        # credit floor (the debit branch of _resolve_order calls the same dispatcher with
        # side="debit"). CREDIT_MIN/MAX_DTE_DEFAULT below are only for the refusal messages.
        _bounds = construction.dte_bounds_for_side(CREDIT_SIDE, cons)
        credit_min, credit_max = int(_bounds.min_dte), int(_bounds.max_dte or CREDIT_MAX_DTE_DEFAULT)
        expiry, chosen_dte, dte_adjusted = construction.pick_expiry_for_side(
            p.expirations, idea.target_dte, side=CREDIT_SIDE, cons=cons)
        if expiry is None:
            audit(self.audit_path, "construction_rejected", underlying=idea.underlying,
                  reason=f"no expiry inside the {credit_min}-{credit_max} DTE credit window")
            return None
        if chosen_dte is not None and int(chosen_dte) > credit_max:
            # the CEILING is a hard refusal, not an adjustment: a 200-DTE short put is a
            # different (much larger, much longer-dated) risk than the one that was underwritten.
            audit(self.audit_path, "construction_rejected", underlying=idea.underlying,
                  reason=f"nearest credit expiry {chosen_dte} DTE exceeds the "
                         f"{credit_max}-DTE credit ceiling")
            return None
        if dte_adjusted:
            audit(self.audit_path, "dte_adjusted", underlying=idea.underlying, side=CREDIT_SIDE,
                  requested_dte=idea.target_dte, adjusted_dte=chosen_dte, min_dte=credit_min)
        spot = await underlying_price(ib, stk)
        # Qualify the option AT THE IDEA'S STRIKE -- exactly one contract, no substitution.
        qualified = await asyncio.wait_for(ib.qualifyContractsAsync(
            Option(idea.underlying, expiry, strike, "P", "SMART")), _IB_CALL_TIMEOUT_S)
        contract = next((c for c in (qualified or []) if getattr(c, "conId", None)), None)
        if contract is None:
            audit(self.audit_path, "construction_rejected", underlying=idea.underlying,
                  reason=f"could not qualify {idea.underlying} {expiry} {strike:g}P")
            return None
        if abs(float(getattr(contract, "strike", 0.0) or 0.0) - strike) > 1e-6 \
                or str(getattr(contract, "right", "") or "").upper()[:1] != "P":
            # a substituted contract is a different trade than the one underwritten -> refuse.
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
            # No two-sided quote -> no executable credit. Refuse rather than sell into the dark.
            audit(self.audit_path, "construction_rejected", underlying=idea.underlying,
                  reason="no two-sided NBBO on the put -- cannot price a sell-to-open")
            return None
        # We SELL, so the executable price is the BID; sizing/credit are computed off it and the
        # final NBBO gate re-checks it immediately before placeOrder.
        credit_per_share = round(float(bid), 2)
        if credit_per_share <= 0:
            return None
        # SIZE TO THE RISK GATE'S $ CAP using the SAME pure helper the debit path uses -- the unit
        # cost of a CSP is its collateral (strike*100), so one contract over the per-trade cap is
        # HARD-REJECTED rather than clamped, exactly as for a debit.
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
            # short put: net greeks are the NEGATIVE of the leg's (we are short it)
            net_delta=-abs(_d), net_theta=-_th, net_gamma=-_ga, net_vega=-_ve,
            side=CREDIT_SIDE, collateral_usd=collateral, net_credit_usd=net_credit,
            credit_max_loss_usd=max_loss)

    async def _resolve_order(self, idea: TradeIdea, per_trade_cap: float, *,
                             net_liq: Optional[float] = None,
                             available_funds: Optional[float] = None) -> Optional[ResolvedOrder]:
        """Select the concrete option contract from target DTE/delta and size it. Returns a
        ResolvedOrder (no order placed yet) or None if nothing usable. Validate on first live run."""
        # Frozen Stage A/B path: reprice ONLY the selected conIds.  Running the legacy selector
        # here would silently substitute a different contract after the model/human chose one.
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
                # Record WHICH guard refused. Nine distinct paths previously collapsed into a
                # silent None here, so a skipped entry left no trace of its cause (2026-08-12).
                audit(self.audit_path, "reprice_refused",
                      underlying=getattr(intent, "underlying", None),
                      reason="; ".join(_rs) if _rs else "unspecified")
                return None
            # The immutable candidate ID must survive quote refresh; only price/qty may move.
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
        # CREDIT (spec S2): one dispatch point, so the credit branch is reached through the SAME
        # method the proposal path AND the post-approval refresh path both call. There is no
        # second pipeline.
        if is_credit(idea):
            return await self._resolve_credit_order(idea, per_trade_cap)
        # DEBIT STRUCTURE GATE, enforcement point #2 -- the mirror of the credit_structure_ok()
        # call at the top of _resolve_credit_order. Every debit order in this process is built
        # here, so an idea that reached construction by some route the entry loop does not run
        # (a future caller, a direct unit invocation) still cannot produce a contract.
        _ok_struct, _why_struct = debit_structure_ok(idea)
        if not _ok_struct:
            audit(self.audit_path, "debit_structure_rejected", underlying=idea.underlying,
                  structure=getattr(idea, "structure", None),
                  direction=getattr(idea, "direction", None), reason=_why_struct)
            return None
        from exitmgr.ibkr import Option, Stock, pick_chain, strikes_near, underlying_price
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
        # MIN-DTE FLOOR (2026-07-01 gate A1): median 17.5 DTE at entry bled 5.9-12.5%/day
        # theta. Nearest expiry to target among those >= min_dte; a too-short model target is
        # ADJUSTED into the 25-45 band (annotated on the journal), rejected only if no valid
        # expiry exists at all.
        # SANCTIONED ENTRY POINT (spec S3): the DEBIT side dispatches through the same function the
        # credit side does, with side="debit" -- which resolves to (cons.min_dte, prefer_dte_max,
        # NO hard ceiling), i.e. byte-identical to the previous explicit call. Routing both sides
        # through one dispatcher is what guarantees the credit floor is unreachable from here.
        expiry, chosen_dte, dte_adjusted = construction.pick_expiry_for_side(
            p.expirations, idea.target_dte, side="debit", cons=cons)
        if expiry is None:
            audit(self.audit_path, "construction_rejected", underlying=idea.underlying,
                  reason=f"no expiry >= {cons.min_dte} DTE available")
            return None
        if dte_adjusted:
            audit(self.audit_path, "dte_adjusted", underlying=idea.underlying,
                  requested_dte=idea.target_dte, adjusted_dte=chosen_dte, min_dte=cons.min_dte)
        spot = await underlying_price(ib, stk)
        cands = [Option(idea.underlying, expiry, k, right, "SMART") for k in strikes_near(p.strikes, spot)]
        qualified = await asyncio.wait_for(ib.qualifyContractsAsync(*cands), _IB_CALL_TIMEOUT_S)
        tickers = await asyncio.wait_for(ib.reqTickersAsync(*[c for c in qualified if getattr(c, "conId", None)]), _IB_CALL_TIMEOUT_S)
        # LONG-LEG DELTA BAND (gate A3): target ~0.55-0.65 delta -- a leg that is already
        # working, not a lottery ticket. The model's target_delta is clamped into the band.
        tgt_delta = construction.effective_delta(idea.target_delta, cons)
        best, best_err, best_greeks = None, 1e9, None
        best_bidask = (None, None)   # bid/ask of the winning long leg (record-only liquidity capture)
        by_strike = {}   # strike -> (mid, contract), for spread short-leg selection
        quote_by_strike = {}  # strike -> (bid, ask), needed for executable combo NBBO
        for tk in tickers:
            # -1 SENTINEL GUARD (2026-07-03): IB reports "no quote" as None/NaN/-1.0. The old
            # `tk.bid and tk.ask` let a -1 bid/ask through (truthy) and averaged it into a bogus mid;
            # `tk.last or 0` let a -1 last through too. usable_price() accepts only a finite positive
            # price, so a one-sided -1 falls back to last, and a -1 last falls back to 0 (skipped).
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
            # No greeks at all (delayed feed etc.) -> conservative fallback: nearest-to-spot
            # priced strike, only if within the near-spot band (same fallback logic as A3).
            k_near = min(by_strike, key=lambda k: abs(k - spot))
            if abs(k_near - spot) <= cons.strike_near_spot_pct * spot:
                best = (by_strike[k_near][1], by_strike[k_near][0])
                best_bidask = quote_by_strike.get(k_near, (None, None))
        if not best:
            return None
        contract, mid = best
        atm_iv = getattr(best_greeks, "impliedVol", None) if best_greeks else None
        # LOTTERY-LONG check (gate A3): the long strike may not sit further OTM than ~1
        # expected move for the horizon (fallback: 3% of spot).
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
        # FULL GREEKS + LIQUIDITY capture (v2, record-only). Best-effort off the already-fetched
        # ticker/greeks -- no extra IBKR round-trip. Never raises into the resolve path.
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
                # single-leg net greeks == long-leg greeks; spread net greeks need the short leg's
                # greeks (not retained) -> left 0/unknown for spreads.
                net_delta=(abs(_d) if _single else 0.0),
                net_theta=(_th if _single else 0.0),
                net_gamma=(_ga if _single else 0.0),
                net_vega=(_ve if _single else 0.0),
            )
        except Exception as _ge:
            print(f"[WARN] greeks/liquidity capture failed for {idea.underlying} (continuing): {_ge}")

        if "spread" in (idea.structure or "").lower():
            # STRUCTURE SANITY (gate A3): short leg constrained to ~1 expected move of spot
            # (conservative width fallback when IV is unavailable) inside pick_spread_short.
            pick = pick_spread_short([(k, m) for k, (m, _) in by_strike.items()],
                                     float(contract.strike), mid, right, per_trade_cap,
                                     spot=spot, dte=chosen_dte, atm_iv=atm_iv, cons=cons)
            if not pick:
                audit(self.audit_path, "construction_rejected", underlying=idea.underlying,
                      reason="no sane affordable short leg (structure-sanity + cap)")
                return None
            short_strike, net = pick
            short_contract = by_strike[short_strike][1]
            # Synthetic executable combo NBBO for a BUY debit: sell the short at its bid and buy
            # the long at its ask.  The reverse legs form the combo bid.  Missing/one-sided quotes
            # remain zero and the final fail-closed NBBO gate refuses submission.
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
            # HARD-REJECT (2026-07-03): if a single spread already exceeds the per-trade cap, reject
            # instead of clamping qty to 1 and shipping an order over the risk cap.
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

        # size to the per-trade cap (never exceed the gate's $ cap); HARD-REJECT if even one
        # contract exceeds it (no clamp-to-1 past the cap).
        qty = size_within_cap(mid * 100, idea.est_debit_usd, per_trade_cap)
        if qty is None:
            audit(self.audit_path, "construction_rejected", underlying=idea.underlying,
                  reason=f"one contract (${mid*100:,.0f}) exceeds per-trade cap ${per_trade_cap:,.0f}")
            return None
        return ResolvedOrder(idea.underlying, right, expiry, float(contract.strike),
                             qty, round(mid, 2), contract,
                             structure=str(getattr(idea, "structure", "") or ""), **enrich)

    async def _submit_order(self, r: ResolvedOrder):
        """Serialize BUY placement against the fast protective-exit mutation loop."""
        # CSP admission performs account/broker I/O before taking the host-wide order-mutation
        # lock. Its separate atomic reservation ledger prevents entry-entry races; only the final
        # placeOrder is serialized with protective exits, so slow reads cannot delay a close.
        if is_credit(r):
            return await self._submit_order_unlocked(r)
        lock = self.broker_order_lock
        if lock is None:
            return await self._submit_order_unlocked(r)
        async with lock:
            return await self._submit_order_unlocked(r)

    async def _submit_order_unlocked(self, r: ResolvedOrder):
        """Place the entry as a MARKETABLE LIMIT at the freshly observed executable ask, never raw
        MKT or a stale mid-plus-buffer. Wait for a decisive IBKR status and return
        (status, [reason msgs]); journal only if it was NOT rejected, so a bounced order never
        pollutes the exit-managed book."""
        from exitmgr.ibkr import Order
        marker_gate = self._entry_markers_clear()
        if not marker_gate.allowed:
            raise RuntimeError("entry markers block submit: " + "; ".join(marker_gate.reasons))
        quote_gate = entry_safety.nbbo_valid(r)
        if not quote_gate.allowed:
            raise RuntimeError("fresh NBBO blocks submit: " + "; ".join(quote_gate.reasons))
        # ---------------------------------------------------------------- CREDIT: SELL PUT to open
        # This is the ONLY place in the system that can emit a sell-to-OPEN, and it is guarded by
        # invariants 1 and 2 re-checked here, at the money boundary, against LIVE broker state --
        # not replayed from the proposal. Everything above this block (halt markers, fresh NBBO)
        # has already run for this order exactly as it does for a debit.
        if is_credit(r):
            # INVARIANT 1, enforcement point #3: the contract about to be sold must be a single
            # PUT. A call, a spread leg, or a malformed order raises rather than trading.
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
            # Arithmetic identity first; the sole live submit-time account/book check is the
            # atomic reserve_credit_entry() call below.
            required = required_collateral(r.strike, r.qty)
            if required is None:
                raise RuntimeError("credit order has invalid collateral inputs")
            if abs(float(required) - capital_committed(r)) > 0.01:
                raise RuntimeError(
                    f"collateral mismatch: recomputed ${required:,.2f} != approved "
                    f"${capital_committed(r):,.2f}")
            _lmt_credit = credit_executable_price(r)   # we SELL, so we cross at the bid
            if not (_lmt_credit > 0):
                raise RuntimeError("credit order has no positive executable credit")
            order = Order(action="SELL", orderType="LMT", lmtPrice=_lmt_credit,
                          totalQuantity=r.qty, tif="DAY")
            order.orderRef = entry_safety.decision_order_ref(r.decision_id)
            reservation, _reservation_pot, _reservation_broker = await reserve_credit_entry(
                self.ib_conn.ib, r, order.orderRef,
                ledger=self.entry_reservation_ledger, audit_path=self.audit_path)
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
                    marker_now = self._entry_markers_clear()
                    if not marker_now.allowed:
                        pre_submit_error = (
                            "entry markers block submit after reservation: "
                            + "; ".join(marker_now.reasons))
                    else:
                        quote_now = entry_safety.nbbo_valid(r)
                        if not quote_now.allowed:
                            pre_submit_error = (
                                "fresh NBBO blocks submit after reservation: "
                                + "; ".join(quote_now.reasons))
                    if pre_submit_error is None:
                        # order_mutation_lock is held across this final money-boundary call.
                        place_invoked = True
                        trade = self.ib_conn.ib.placeOrder(r.contract, order)
            except Exception as exc:
                if not place_invoked:
                    await asyncio.to_thread(self.entry_reservation_ledger.clear, order.orderRef)
                    audit(self.audit_path, "credit_entry_reservation_cleared",
                          order_ref=order.orderRef, status="definite_pre_submit_failure")
                else:
                    # Whether IBKR accepted an order whose API call raised is ambiguous. Retain
                    # until broker reconciliation/TTL rather than risking a duplicate CSP.
                    audit(self.audit_path, "credit_place_ambiguous_reservation_retained",
                          order_ref=order.orderRef, error=str(exc))
                raise
            if pre_submit_error is not None:
                await asyncio.to_thread(self.entry_reservation_ledger.clear, order.orderRef)
                audit(self.audit_path, "credit_entry_reservation_cleared",
                      order_ref=order.orderRef, status="definite_pre_submit_block")
                raise RuntimeError(pre_submit_error)
            status, reasons = await self._await_and_journal(trade, r)
            if await asyncio.to_thread(
                    self.entry_reservation_ledger.clear_for_status, order.orderRef, status):
                audit(self.audit_path, "credit_entry_reservation_cleared",
                      order_ref=order.orderRef, status=status)
            return status, reasons
        # -------------------------------------------------------------- DEBIT: BUY to open
        # DEBIT STRUCTURE GATE, enforcement point #3 -- the debit mirror of the invariant-1
        # re-check the credit branch performs immediately above, at the money boundary. Every
        # route that builds a TradeIdea is gated upstream; this stands here so that a route
        # invented LATER is still caught, with no order emitted and nothing journalled. It
        # raises rather than returning, because a submit that must not happen is not a
        # "no trade today" -- it is a bug that must be visible.
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
        # Submission is bound to the final, two-sided reqTickersAsync observation.  Using a stale
        # mid + percentage buffer can still rest below a wide ask; crossing at the observed ask is
        # both executable and capped.  Callers must run nbbo_valid immediately beforehand.
        _lmt = entry_safety.executable_price(r)
        order = Order(action="BUY", orderType="LMT", lmtPrice=_lmt, totalQuantity=r.qty, tif="DAY")
        order.orderRef = entry_safety.decision_order_ref(r.decision_id)
        if r.short_contract is not None:
            # spreads trade as ONE combo order -- legs can never fill/close independently
            combo = self.ib_conn.create_combo_contract(
                r.underlying,
                [(r.contract.conId, "BUY"), (r.short_contract.conId, "SELL")])
            from exitmgr.order_lock import order_mutation_lock
            with order_mutation_lock():
                trade = self.ib_conn.ib.placeOrder(combo, order)
        else:
            from exitmgr.order_lock import order_mutation_lock
            with order_mutation_lock():
                trade = self.ib_conn.ib.placeOrder(r.contract, order)
        return await self._await_and_journal(trade, r)

    async def _await_and_journal(self, trade, r: ResolvedOrder):
        """Wait for a decisive IBKR status, then journal unless the order was rejected. Shared
        verbatim by the debit and credit branches -- there is ONE fill/journal path, not two."""
        live = {"Filled", "Submitted", "PreSubmitted"}
        dead = {"Cancelled", "ApiCancelled", "Inactive", "Rejected"}
        for _ in range(24):  # up to ~12s for IBKR to ACK or REJECT
            await asyncio.sleep(0.5)
            if trade.orderStatus.status in live or trade.orderStatus.status in dead:
                break
        # FILL VERIFICATION (2026-07-01 audit: 5/15 "executed" entries never filled and the
        # journal recorded intent, not fills). After the ACK, wait a bit longer for the actual
        # fill so the journal entry carries fill status + price + timestamp. A marketable-LIMIT
        # DAY order (2026-07-03: entries are LMT, not MKT) normally fills in seconds; one that
        # doesn't is caught by the exit-manager's unfilled-order alarm on the next cycle.
        if trade.orderStatus.status not in dead:
            for _ in range(20):  # up to ~10 more seconds for the fill itself
                if trade.orderStatus.status == "Filled":
                    break
                await asyncio.sleep(0.5)
        status = trade.orderStatus.status
        reasons = [le.message for le in trade.log if getattr(le, "errorCode", 0)]
        if status not in dead:
            _afp = getattr(trade.orderStatus, "avgFillPrice", None)
            _afp_val = (float(_afp) if (status == "Filled" and _afp and _afp == _afp) else None)
            # COMMISSIONS + REAL BASIS (2026-07-03): capture the actual entry fee + the fill-based
            # cost basis so realized P&L can be reported NET of fees and entry slippage is recorded.
            # All ADDITIVE; never raises into the order path; never fabricates a fee/price.
            from exitmgr.order import commission_from_trade, compute_entry_basis
            _est_debit = round(r.limit * 100 * r.qty, 2)
            _entry_comm = commission_from_trade(trade) if status == "Filled" else None
            _efd, _eslip, _eslip_pct = compute_entry_basis(_est_debit, _afp_val, r.qty)
            fill = {
                "decision_id": r.decision_id,
                "decision_revision": r.decision_revision,
                "model_identity": r.model_identity,
                "order_ref": getattr(trade.order, "orderRef", None),
                "order_id": getattr(trade.order, "orderId", None),
                "order_status": status,
                "avg_fill_price": _afp_val,
                "fill_ts": (datetime.now(timezone.utc).isoformat() if status == "Filled" else None),
                "entry_commission": _entry_comm,          # actual IBKR entry fee (both legs), $ or None
                "entry_fill_debit": _efd,                 # real cost basis from avg_fill_price ($)
                "entry_slippage": _eslip,                 # actual - estimated ($)
                "entry_slippage_pct": _eslip_pct,
                "basis_source": ("fill" if _efd is not None else "estimate"),
            }
            self._journal_entry(r, fill=fill)
        return status, reasons

    def _journal_entry(self, r: ResolvedOrder, fill: Optional[dict] = None) -> None:
        """Append the entry to trades.log so the exit manager picks it up. Journal-at-submit is
        safe: with scope=journal the manager only acts on journal ∩ live positions, so an
        unfilled order is simply never matched. All 2026-07-01 fields are ADDITIVE -- every
        existing consumer keys on contract_id/debit and ignores unknown fields."""
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "contract_id": getattr(r.contract, "conId", None),
            "symbol": r.underlying,
            "right": r.right,
            "expiry": r.expiry,
            "strike": r.strike,
            "quantity": r.qty,
            "debit": round(r.limit * 100 * r.qty, 2),
            "profit_target_pct": (getattr(r, "tp_pct", 0.0) or None),  # clamped 25-35 band
            "stop_pct": (getattr(r, "sl_pct", 0.0) or None),           # clamped, default 30
            "conviction": getattr(r, "conviction", -1.0),
            "intended_hold_days": getattr(r, "intended_hold_days", None),
            "thesis": getattr(r, "thesis", ""),
            "decision_id": getattr(r, "decision_id", None),
            "decision_revision": getattr(r, "decision_revision", 0),
            "model_identity": getattr(r, "model_identity", None),
            "order_ref": entry_safety.decision_order_ref(r.decision_id),
            # entry-time technical_card (per-name indicators fed to the model at entry). ADDITIVE;
            # consumed by the exit manager's position view so a reload/continuation call sees the
            # ORIGINAL setup, not just live price. None when unavailable (older/mocked entries).
            "technical_card": getattr(r, "technical_card", None),
            # construction/enrichment annotations (2026-07-01)
            "underlying_price_at_entry": (getattr(r, "spot", 0.0) or None),
            "entry_delta": (getattr(r, "entry_delta", 0.0) or None),
            "entry_iv": (getattr(r, "entry_iv", 0.0) or None),
            "dte_at_entry": (getattr(r, "dte", 0) or None),
            "dte_adjusted": bool(getattr(r, "dte_adjusted", False)),
            "earnings_unchecked": bool(getattr(r, "earnings_unchecked", False)),  # A5: IV-crush blackout could not be verified
            # v2 full greeks + liquidity (ADDITIVE; every existing consumer ignores unknown keys)
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
            # CREDIT LIMB (spec S2): the exit manager must be able to tell a SHORT put apart from
            # a long one -- the position is negative, the "debit" is not a cost, and assignment
            # (invariant 7) is a normal terminal state rather than a failure. `debit` deliberately
            # carries MAX LOSS (collateral - credit): every legacy consumer reads `debit` as the
            # capital at risk, so the conservative figure is the correct one to publish there.
            rec.update({
                "side": CREDIT_SIDE,
                "structure": CSP_STRUCTURE,
                "action": "SELL",
                "quantity": -abs(int(r.qty)),          # SHORT: negative, as the broker reports it
                "contracts": abs(int(r.qty)),
                "collateral_usd": capital_committed(r),
                "net_credit_usd": round(_fnum(r.net_credit_usd, 0.0) or 0.0, 2),
                "max_loss_usd": capital_at_risk(r),
                "debit": capital_at_risk(r),
                "assignment_possible": True,           # accepted outcome, never an error
            })
        if fill:
            rec.update(fill)
        if r.short_contract is not None:
            rec["spread"] = {
                "short_con_id": getattr(r.short_contract, "conId", None),
                "short_strike": r.short_strike,
                "width": abs(r.short_strike - r.strike),
            }
        with open(self.journal_path, "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
