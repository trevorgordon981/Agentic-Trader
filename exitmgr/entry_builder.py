"""Public API contract; production-derived narrative omitted."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from exitmgr import construction, entry_safety
from exitmgr.entry_contract import (
    CONTRACT_VERSION,
    RuntimeCandidate,
    RuntimeLeg,
    StageAIntent,
    validate_candidates,
    validate_runtime_candidate,
)
from exitmgr.ibkr import (
    Option, Stock, option_contracts_for_expiry, pick_chain, strikes_near,
    underlying_price,
)
from exitmgr.market import usable_price





_IB_READ_TIMEOUT_S = 20



DEBIT_STRUCTURES = frozenset({
    "long call", "long put", "call debit spread", "put debit spread",
})
CSP_STRUCTURE = "cash secured put"
CREDIT_MAX_COLLATERAL_PCT = 0.80
CREDIT_MIN_PREMIUM_PCT = 0.0125


class CandidateBuildError(RuntimeError):
    """Public API contract; production-derived narrative omitted."""


@dataclass(frozen=True)
class CandidateBinding:
    """Public API contract; production-derived narrative omitted."""

    candidate: RuntimeCandidate
    primary_contract: Any
    secondary_contract: Any = None
    quote_observed_monotonic: float = 0.0
    underlying_contract: Any = None

    def to_resolved_order(self, intent: StageAIntent):
        """Public API contract; production-derived narrative omitted."""

        from exitmgr.trader import ResolvedOrder

        c = self.candidate
        qty = int(c.max_affordable_quantity)
        if qty < 1:
            raise CandidateBuildError("selected candidate has no affordable quantity")
        is_credit = c.side == "credit"
        limit = (float(c.combo_bid_per_share) if is_credit
                 else float(c.combo_ask_per_share))
        if not math.isfinite(limit) or limit <= 0:
            raise CandidateBuildError("selected candidate has no executable price")
        result = ResolvedOrder(
            c.underlying,
            "C" if c.legs[0].right == "call" else "P",
            c.expiry,
            float(c.legs[0].strike),
            qty,
            round(limit, 4),
            self.primary_contract,
            short_strike=(float(c.legs[1].strike) if len(c.legs) == 2 else 0.0),
            short_contract=(self.secondary_contract if len(c.legs) == 2 else None),
            conviction=float(intent.conviction),
            thesis=str(intent.thesis),
            entry_delta=float(c.primary_leg_delta),
            entry_iv=float(c.primary_leg_iv),
            dte=int(c.dte),
            dte_adjusted=(int(c.dte) != int(intent.target_dte)),
            entry_bid=float(c.combo_bid_per_share),
            entry_ask=float(c.combo_ask_per_share),
            entry_spread_pct=float(c.bid_ask_spread_pct),
            quote_observed_at=float(self.quote_observed_monotonic),
            intended_hold_days=int(intent.intended_hold_days),
            side=str(c.side),
            collateral_usd=(round(float(c.one_contract_cost_usd) * qty, 2)
                            if is_credit else 0.0),
            net_credit_usd=(round(float(c.one_contract_credit_usd) * qty, 2)
                            if is_credit and c.one_contract_credit_usd is not None else 0.0),
            credit_max_loss_usd=(round(float(c.one_contract_max_loss_usd) * qty, 2)
                                 if is_credit else 0.0),
            structure=str(c.structure),
        )

        result.stage_b_candidate_id = c.candidate_id
        result.stage_a_intent_id = c.intent_id
        result.stage_a_intent_payload = intent.to_dict()
        result.stage_b_candidate_payload = c.to_dict()
        return result


def _finite(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _dte(expiry: Any, now: Optional[datetime] = None) -> Optional[int]:
    try:
        day = datetime.strptime(str(expiry)[:8], "%Y%m%d").date()
    except (TypeError, ValueError):
        return None
    return (day - (now or datetime.now(timezone.utc)).date()).days




























































DEBIT_HOLD_FLOOR_MULTIPLE = 8



POLICY_MAX_PREMIUM_PCT_CEILING = 0.25



@dataclass(frozen=True)
class EntryPolicy:
    """Public API contract; production-derived narrative omitted."""

    dte_floor: int
    policy_cap_usd: float
    live_cap_usd: float

    allocation_budget_usd: float

    conviction: int
    model_allocation_pct_net_liq: float = 0.0

    allocation_is_binding: bool = False



def debit_hold_floor(intent: StageAIntent) -> int:
    """Public API contract; production-derived narrative omitted."""
    return int(intent.intended_hold_days) * DEBIT_HOLD_FLOOR_MULTIPLE


def policy_dte_floor(intent: StageAIntent, cons) -> int:
    """Public API contract; production-derived narrative omitted."""
    bounds = construction.dte_bounds_for_side(intent.side, cons)
    floor = int(bounds.min_dte)
    if intent.side == "credit":


        return max(floor, int(intent.intended_hold_days))
    return max(floor, debit_hold_floor(intent))


def policy_capital_cap(intent: StageAIntent, net_liq: float, available_funds: float, cons, *,
                       deployed_credit_usd: Optional[float] = 0.0,
                       cash_buffer_pct: float = 0.05) -> float:
    """Public API contract; production-derived narrative omitted."""
    nl = _finite(net_liq)
    af = _finite(available_funds)
    if nl is None or nl <= 0 or af is None or af < 0:
        raise CandidateBuildError("invalid live account snapshot")
    buffer_pct = _finite(cash_buffer_pct)
    if buffer_pct is None or not 0 <= buffer_pct < 1:
        raise CandidateBuildError("invalid cash-buffer percentage")
    deployable = max(0.0, af - buffer_pct * nl)
    if intent.side == "credit":
        deployed = _finite(deployed_credit_usd)
        if deployed is None or deployed < 0:
            raise CandidateBuildError("deployed CSP collateral is unverifiable")
        structural = max(0.0, nl * CREDIT_MAX_COLLATERAL_PCT - deployed)
    else:
        structural = nl * min(POLICY_MAX_PREMIUM_PCT_CEILING, float(cons.max_premium_pct))
    return min(deployable, structural)


def entry_policy(intent: StageAIntent, net_liq: float, available_funds: float, cons, *,
                 deployed_credit_usd: Optional[float] = 0.0,
                 cash_buffer_pct: float = 0.05) -> EntryPolicy:
    """Public API contract; production-derived narrative omitted."""
    policy_cap = policy_capital_cap(
        intent, net_liq, available_funds, cons,
        deployed_credit_usd=deployed_credit_usd, cash_buffer_pct=cash_buffer_pct)
    allocation = round(float(_finite(net_liq)) * float(intent.allocation_pct_net_liq) / 100.0, 2)




    if intent.side == "credit":
        cap = min(policy_cap, allocation)
        binding = allocation < policy_cap - 1e-9
    else:
        cap = policy_cap
        binding = False
    if not math.isfinite(cap) or cap <= 0:
        raise CandidateBuildError("allocation/live cap leaves no buying power")
    if cap > policy_cap + 1e-9:



        raise CandidateBuildError("model allocation may not exceed the deterministic policy cap")
    return EntryPolicy(
        dte_floor=policy_dte_floor(intent, cons),
        policy_cap_usd=round(policy_cap, 2),
        live_cap_usd=round(cap, 2),
        allocation_budget_usd=allocation,
        conviction=int(intent.conviction),
        model_allocation_pct_net_liq=float(intent.allocation_pct_net_liq),
        allocation_is_binding=binding,
    )


def _candidate_id(intent_id: str, intent: StageAIntent, expiry: str,
                  legs: Iterable[RuntimeLeg]) -> str:
    identity = {
        "contract_version": CONTRACT_VERSION,
        "intent_id": intent_id,
        "underlying": intent.underlying,
        "side": intent.side,
        "direction": intent.direction,
        "structure": intent.structure,
        "expiry": str(expiry),
        "legs": [
            {
                "con_id": int(leg.con_id),
                "action": str(leg.action),
                "right": str(leg.right),
                "strike": float(leg.strike),
            }
            for leg in legs
        ],
    }
    raw = json.dumps(identity, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False, allow_nan=False).encode("utf-8")
    return "cand_" + hashlib.sha256(raw).hexdigest()


def _leg(ticker, action: str, right: str) -> Optional[RuntimeLeg]:
    contract = getattr(ticker, "contract", None)
    con_id = getattr(contract, "conId", None)
    strike = _finite(getattr(contract, "strike", None))
    bid = _finite(getattr(ticker, "bid", None))
    ask = _finite(getattr(ticker, "ask", None))
    greeks = getattr(ticker, "modelGreeks", None) or getattr(ticker, "lastGreeks", None)
    delta = _finite(getattr(greeks, "delta", None)) if greeks is not None else None
    iv = _finite(getattr(greeks, "impliedVol", None)) if greeks is not None else None
    if (not isinstance(con_id, int) or con_id < 1 or strike is None or strike <= 0
            or bid is None or ask is None or not usable_price(bid) or not usable_price(ask)
            or ask < bid or delta is None or not -1 <= delta <= 1 or iv is None or iv <= 0):
        return None
    return RuntimeLeg(
        con_id=int(con_id), action=action, right=right, strike=float(strike),
        bid_per_share=float(bid), ask_per_share=float(ask),
        delta=float(delta), iv=float(iv),
    )


def _spread_pct(bid: float, ask: float) -> Optional[float]:
    if not (math.isfinite(bid) and math.isfinite(ask) and ask >= bid > 0):
        return None
    mid = (bid + ask) / 2.0
    return round((ask - bid) / mid * 100.0, 6) if mid > 0 else None


def _structure_ok(intent: StageAIntent, legs: list[RuntimeLeg], spot: float,
                  dte: int, cons, reasons: Optional[list] = None) -> bool:
    """Public API contract; production-derived narrative omitted."""
    def _no(why):
        if reasons is not None:
            reasons.append(why)
        return False
    if intent.side == "credit":
        return True
    if len(legs) == 1:
        ok, why = construction.long_strike_ok(
            legs[0].strike, spot, "C" if legs[0].right == "call" else "P",
            dte, legs[0].iv, cons)
        return True if ok else _no(why or "long-strike check failed")
    if len(legs) == 2:
        ok, why = construction.spread_structure_ok(
            legs[0].strike, legs[1].strike, spot,
            "C" if legs[0].right == "call" else "P", dte, legs[0].iv, cons)
        return True if ok else _no(why or "spread-structure check failed")
    return _no("unsupported leg count: %d" % len(legs))


def combo_spread_ceiling(cons, symbol: str) -> Optional[float]:
    """Public API contract; production-derived narrative omitted."""
    ordinary = _finite(getattr(cons, "max_combo_spread_pct", None))
    if ordinary is None or ordinary <= 0:
        return None
    if entry_safety.is_leveraged_etf(symbol):
        leveraged = _finite(getattr(cons, "max_leveraged_etf_combo_spread_pct", None))
        if leveraged is not None and leveraged > 0:
            return leveraged
    return ordinary


def _make_candidate(*, intent_id: str, intent: StageAIntent, expiry: str, dte: int,
                    legs: list[RuntimeLeg], allocation: float, live_cap: float,
                    quote_utc: str, quote_mono: float, max_spread_pct: float,
                    max_combo_spread_pct: Optional[float] = None,
                    why: Optional[list] = None) -> Optional[RuntimeCandidate]:


    is_credit = intent.side == "credit"
    is_spread = len(legs) == 2
    if is_credit:
        combo_bid = float(legs[0].bid_per_share)
        combo_ask = float(legs[0].ask_per_share)
        width = None
        cost = round(float(legs[0].strike) * 100.0, 2)
        credit = round(combo_bid * 100.0, 2)
        max_loss = round(cost - credit, 2)
        if credit / cost + 1e-12 < CREDIT_MIN_PREMIUM_PCT or max_loss <= 0:



            if why is not None:
                why.append("credit_premium(credit=%.2f cost=%.2f max_loss=%.2f min_pct=%.3f)"
                           % (credit, cost, max_loss, CREDIT_MIN_PREMIUM_PCT))
            return None
    elif is_spread:
        long_leg, short_leg = legs
        combo_bid = round(float(long_leg.bid_per_share) - float(short_leg.ask_per_share), 6)
        combo_ask = round(float(long_leg.ask_per_share) - float(short_leg.bid_per_share), 6)
        width = round(abs(float(short_leg.strike) - float(long_leg.strike)), 6)
        if not (0 < combo_bid <= combo_ask <= width):
            if why is not None:
                why.append("bad_combo(bid=%.2f ask=%.2f width=%.2f)" % (combo_bid, combo_ask, width))
            return None
        cost = round(combo_ask * 100.0, 2)
        credit = None
        max_loss = cost
    else:
        combo_bid = float(legs[0].bid_per_share)
        combo_ask = float(legs[0].ask_per_share)
        width = None
        cost = round(combo_ask * 100.0, 2)
        credit = None
        max_loss = cost
    spread = _spread_pct(combo_bid, combo_ask)


    _limit = max_spread_pct
    if is_spread:
        _combo_limit = _finite(max_combo_spread_pct)
        _limit = float(_combo_limit if _combo_limit is not None and _combo_limit > 0
                       else max_spread_pct)
    if spread is None or spread > _limit + 1e-12 or cost <= 0:
        if why is not None:



            why.append("wide(spread=%s max=%.1f cost=%.2f)"
                       % ("None" if spread is None else "%.1f" % spread, _limit, cost))
        return None
    qty = int(live_cap // cost)
    if qty < 1:
        if why is not None:
            why.append("unafford(cost=%.2f live_cap=%.2f)" % (cost, live_cap))
        return None
    candidate_id = _candidate_id(intent_id, intent, expiry, legs)
    return RuntimeCandidate(
        candidate_id=candidate_id,
        intent_id=intent_id,
        underlying=intent.underlying,
        side=intent.side,
        direction=intent.direction,
        structure=intent.structure,
        quote_observed_at_utc=quote_utc,
        quote_age_seconds=0.0,
        expiry=str(expiry),
        dte=int(dte),
        legs=list(legs),
        combo_bid_per_share=float(combo_bid),
        combo_ask_per_share=float(combo_ask),
        width_usd=width,
        primary_leg_delta=abs(float(legs[0].delta)),
        primary_leg_iv=float(legs[0].iv),
        bid_ask_spread_pct=float(spread),
        liquidity_status="pass",
        one_contract_cost_usd=float(cost),
        one_contract_credit_usd=credit,
        one_contract_max_loss_usd=float(max_loss),
        allocation_budget_usd=float(allocation),
        live_cap_usd=float(live_cap),
        max_affordable_quantity=qty,
    )


def _expiry_choices(expirations, intent: StageAIntent, cons, *,
                    dte_floor: Optional[int] = None) -> list[tuple[str, int]]:
    """Public API contract; production-derived narrative omitted."""
    bounds = construction.dte_bounds_for_side(intent.side, cons)
    ceiling = int(bounds.max_dte) if bounds.max_dte is not None else 800
    floor = int(dte_floor) if dte_floor is not None else policy_dte_floor(intent, cons)
    parsed = []
    for expiry in expirations or []:
        dte = _dte(expiry)
        if dte is not None and floor <= dte <= ceiling:
            parsed.append((str(expiry), int(dte)))
    parsed.sort(key=lambda item: (abs(item[1] - int(intent.target_dte)), item[1], item[0]))
    return parsed[:2]


async def build_entry_candidates(ib, intent: StageAIntent, intent_id: str, *,
                                 net_liq: float, available_funds: float, cons,
                                 deployed_credit_usd: Optional[float] = 0.0,
                                 cash_buffer_pct: float = 0.05) -> list[CandidateBinding]:
    """Public API contract; production-derived narrative omitted."""
    if intent.structure not in DEBIT_STRUCTURES | {CSP_STRUCTURE}:
        raise CandidateBuildError("unsupported frozen structure")
    policy = entry_policy(
        intent, net_liq, available_funds, cons,
        deployed_credit_usd=deployed_credit_usd,
        cash_buffer_pct=cash_buffer_pct)
    allocation, live_cap = policy.allocation_budget_usd, policy.live_cap_usd
    max_spread = _finite(getattr(cons, "max_entry_spread_pct", None))
    if max_spread is None or max_spread <= 0:
        raise CandidateBuildError("max_entry_spread_pct missing/invalid (fail closed)")
    combo_limit = combo_spread_ceiling(cons, intent.underlying)
    if combo_limit is None:
        raise CandidateBuildError("max_combo_spread_pct missing/invalid (fail closed)")
    qualified_stock = await asyncio.wait_for(
        ib.qualifyContractsAsync(Stock(intent.underlying, "SMART", "USD")), _IB_READ_TIMEOUT_S)
    if not qualified_stock:
        return []
    stock = qualified_stock[0]
    params = await asyncio.wait_for(
        ib.reqSecDefOptParamsAsync(intent.underlying, "", "STK", stock.conId), _IB_READ_TIMEOUT_S)
    chain = pick_chain(params, intent.underlying) if params else None
    if chain is None:
        print(f"[BUILD-DIAG] {intent.underlying}: no option chain (params={bool(params)})")
        return []
    spot = _finite(await underlying_price(ib, stock))
    if spot is None or spot <= 0:
        print(f"[BUILD-DIAG] {intent.underlying}: no usable spot price")
        return []
    expiries = _expiry_choices(chain.expirations, intent, cons, dte_floor=policy.dte_floor)
    if not expiries:
        print(f"[BUILD-DIAG] {intent.underlying}: no expiry matches target_dte="
              f"{getattr(intent, 'target_dte', '?')} among {len(chain.expirations or [])} listed")
        return []
    right_letter = "C" if intent.structure in {"long call", "call debit spread"} else "P"
    right_word = "call" if right_letter == "C" else "put"
    target_delta = (float(intent.target_delta) if intent.side == "credit"
                    else construction.effective_delta(float(intent.target_delta), cons))




    if intent.side != "credit":
        try:
            _req = float(intent.target_delta)
            if abs(_req - target_delta) > 0.05:
                print(f"[BUILD] {intent.underlying}: DELTA CLAMPED {_req:.2f} -> "
                      f"{target_delta:.2f} (band {float(cons.delta_min):.2f}-"
                      f"{float(cons.delta_max):.2f}) -- model sized for {_req:.2f}")
        except (TypeError, ValueError):
            pass
    bindings: list[CandidateBinding] = []
    seen: set[str] = set()
    print(f"[BUILD-DIAG] {intent.underlying}: spot={spot:.2f} expiries={len(expiries)} "
          f"strikes={len(chain.strikes or [])} target_delta={target_delta:.2f}")
    for expiry, dte in expiries:
        qualified = await option_contracts_for_expiry(
            ib, intent.underlying, expiry, right_letter, "SMART", spot,
            timeout_s=_IB_READ_TIMEOUT_S,
            trading_class=getattr(chain, "tradingClass", None),
            multiplier=getattr(chain, "multiplier", None), currency="USD")
        if qualified is None:
            contracts = [Option(intent.underlying, expiry, strike, right_letter, "SMART")
                         for strike in strikes_near(chain.strikes, spot)]
            qualified = [c for c in await asyncio.wait_for(
                             ib.qualifyContractsAsync(*contracts), _IB_READ_TIMEOUT_S)
                         if getattr(c, "conId", None)]
        else:
            contracts = qualified
        if not qualified:
            print(f"[BUILD-DIAG] {intent.underlying} {expiry}: 0/{len(contracts)} contracts "
                  f"exist for the exact expiry/right")
            continue
        tickers = list(await asyncio.wait_for(
            ib.reqTickersAsync(*qualified), _IB_READ_TIMEOUT_S))


        quote_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        quote_mono = time.monotonic()
        usable = []
        for ticker in tickers:
            action = "sell" if intent.side == "credit" else "buy"
            leg = _leg(ticker, action, right_word)
            if leg is not None:
                usable.append((ticker, leg))
        if not usable:



            _greeks = sum(1 for t in tickers if getattr(t, "modelGreeks", None) is not None)
            print(f"[BUILD-DIAG] {intent.underlying} {expiry}: 0/{len(tickers)} legs usable "
                  f"({_greeks} tickers had modelGreeks) -- quote/greeks missing")
        usable.sort(key=lambda pair: (abs(abs(pair[1].delta) - target_delta),
                                      abs(pair[1].strike - spot), pair[1].strike))
        if intent.structure in {"long call", "long put", CSP_STRUCTURE}:
            for ticker, leg in usable:
                if not _structure_ok(intent, [leg], spot, dte, cons):
                    continue
                candidate = _make_candidate(
                    intent_id=intent_id, intent=intent, expiry=expiry, dte=dte,
                    legs=[leg], allocation=allocation, live_cap=live_cap,
                    quote_utc=quote_utc, quote_mono=quote_mono,
                    max_spread_pct=max_spread, max_combo_spread_pct=combo_limit)
                if candidate is None or candidate.candidate_id in seen:
                    continue
                seen.add(candidate.candidate_id)
                bindings.append(CandidateBinding(
                    candidate=candidate, primary_contract=ticker.contract,
                    secondary_contract=None, quote_observed_monotonic=quote_mono,
                    underlying_contract=stock))
                if len(bindings) >= 5:
                    break
        else:
            _sd = {"longs": 0, "no_partner": 0, "unsane": 0, "mk_none": 0, "pairs": 0}
            for long_ticker, long_leg in usable:
                _sd["longs"] += 1
                shorts = []
                for short_ticker, short_raw in usable:
                    further_otm = (right_letter == "C" and short_raw.strike > long_leg.strike) or (
                        right_letter == "P" and short_raw.strike < long_leg.strike)
                    if not further_otm:
                        continue
                    _sd["pairs"] += 1
                    sane, _sane_why = construction.spread_structure_ok(
                        long_leg.strike, short_raw.strike, spot, right_letter, dte,
                        long_leg.iv, cons)
                    if sane:
                        shorts.append((abs(short_raw.strike - long_leg.strike),
                                       short_ticker, short_raw))
                    else:
                        _sd["unsane"] += 1
                        _sd.setdefault("why_unsane", _sane_why)
                shorts.sort(key=lambda row: (row[0], row[2].strike))
                for _, short_ticker, short_raw in shorts:
                    short_leg = RuntimeLeg(
                        con_id=short_raw.con_id, action="sell", right=short_raw.right,
                        strike=short_raw.strike, bid_per_share=short_raw.bid_per_share,
                        ask_per_share=short_raw.ask_per_share, delta=short_raw.delta, iv=short_raw.iv)
                    candidate = _make_candidate(
                        intent_id=intent_id, intent=intent, expiry=expiry, dte=dte,
                        legs=[long_leg, short_leg], allocation=allocation, live_cap=live_cap,
                        quote_utc=quote_utc, quote_mono=quote_mono,
                        max_spread_pct=max_spread, max_combo_spread_pct=combo_limit,
                        why=_sd.setdefault("why", []))
                    if candidate is None or candidate.candidate_id in seen:
                        if candidate is None:
                            _sd["mk_none"] += 1
                        continue
                    seen.add(candidate.candidate_id)
                    bindings.append(CandidateBinding(
                        candidate=candidate, primary_contract=long_ticker.contract,
                        secondary_contract=short_ticker.contract,
                        quote_observed_monotonic=quote_mono,
                        underlying_contract=stock))
                    break
                if not shorts:
                    _sd["no_partner"] += 1
                if len(bindings) >= 5:
                    break
            if not bindings:



                _why_list = _sd.get("why", []) or []
                _cause_counts = {}
                _cause_examples = {}
                for _w in _why_list:
                    _name = _w.split("(")[0]
                    _cause_counts[_name] = _cause_counts.get(_name, 0) + 1
                    _cause_examples.setdefault(_name, _w)
                _ranked = sorted(_cause_counts.items(), key=lambda kv: (-kv[1], kv[0]))
                _causes_str = ""
                if _ranked:
                    _causes_str = " | causes: " + " ".join(
                        f"{_n}={_c}" for _n, _c in _ranked[:5])
                    _top = _ranked[0][0]
                    _causes_str += f" | top example: {_cause_examples.get(_top, '')[:90]}"
                print(f"[SPREAD-DIAG] {intent.underlying} {expiry}: longs={_sd['longs']} "
                      f"pairs={_sd['pairs']} rejected_structure={_sd['unsane']} "
                      f"longs_with_no_partner={_sd['no_partner']} "
                      f"make_candidate_None={_sd['mk_none']}"
                      + _causes_str
                      + (f" | first structure reason: {_sd.get('why_unsane','')[:90]}"
                         if _sd.get("why_unsane") else ""))
        if len(bindings) >= 5:
            break
    if len(bindings) < 3:
        return []
    validated = validate_candidates(intent_id, intent, [b.candidate for b in bindings[:5]])
    valid_ids = {candidate.candidate_id for candidate in validated}
    return [binding for binding in bindings[:5]
            if binding.candidate.candidate_id in valid_ids]


async def reprice_binding(ib, binding: CandidateBinding, intent: StageAIntent, *,
                          net_liq: float, available_funds: float, cons,
                          deployed_credit_usd: Optional[float] = 0.0,
                          cash_buffer_pct: float = 0.05,
                          reasons: Optional[list] = None) -> Optional[CandidateBinding]:
    """Public API contract; production-derived narrative omitted."""
    def _no(why):
        if reasons is not None:
            reasons.append(why)
        return None
    policy = entry_policy(
        intent, net_liq, available_funds, cons,
        deployed_credit_usd=deployed_credit_usd,
        cash_buffer_pct=cash_buffer_pct)
    allocation, live_cap = policy.allocation_budget_usd, policy.live_cap_usd
    max_spread = _finite(getattr(cons, "max_entry_spread_pct", None))
    if max_spread is None or max_spread <= 0:
        return _no("construction.max_entry_spread_pct is unset or <= 0")
    combo_limit = combo_spread_ceiling(cons, intent.underlying)
    if combo_limit is None:
        return _no("construction.max_combo_spread_pct is unset or <= 0")
    if intent.side == "debit":
        if binding.underlying_contract is None:
            return _no("no underlying contract bound to this candidate")
        spot = _finite(await underlying_price(ib, binding.underlying_contract))
        if spot is None or spot <= 0:
            return _no("underlying spot price unavailable at approval time")
    else:
        spot = 0.0
    contracts = [binding.primary_contract]
    if binding.secondary_contract is not None:
        contracts.append(binding.secondary_contract)
    tickers = list(await asyncio.wait_for(
        ib.reqTickersAsync(*contracts), _IB_READ_TIMEOUT_S))
    by_id = {int(getattr(getattr(t, "contract", None), "conId", 0) or 0): t for t in tickers}
    expected_ids = [int(leg.con_id) for leg in binding.candidate.legs]
    if any(con_id not in by_id for con_id in expected_ids):
        return _no("broker returned no quote for one or more selected legs")
    refreshed_legs = []
    for old_leg in binding.candidate.legs:
        new_leg = _leg(by_id[int(old_leg.con_id)], old_leg.action, old_leg.right)
        if new_leg is None or int(new_leg.con_id) != int(old_leg.con_id) \
                or abs(float(new_leg.strike) - float(old_leg.strike)) > 1e-9:
            return _no("leg %s %s: quote unusable or contract identity changed"
                       % (old_leg.strike, old_leg.right))
        refreshed_legs.append(new_leg)
    current_dte = _dte(binding.candidate.expiry)
    if current_dte is None or current_dte < 1:
        return _no("expiry %s is no longer tradeable (dte=%s)" % (binding.candidate.expiry, current_dte))
    _sr = []
    if not _structure_ok(intent, refreshed_legs, spot, current_dte, cons, reasons=_sr):
        return _no("structure check failed at spot %.2f: %s" % (spot, "; ".join(_sr) or "unspecified"))
    now_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    now_mono = time.monotonic()
    candidate_reasons = []
    refreshed = _make_candidate(
        intent_id=binding.candidate.intent_id, intent=intent,
        expiry=binding.candidate.expiry, dte=current_dte,
        legs=refreshed_legs, allocation=allocation, live_cap=live_cap,
        quote_utc=now_utc, quote_mono=now_mono, max_spread_pct=max_spread,
        max_combo_spread_pct=combo_limit, why=candidate_reasons)
    if refreshed is None:
        return _no("requote rejected: " + ("; ".join(candidate_reasons)
                   if candidate_reasons else "candidate validation failed without a reason"))
    if refreshed.candidate_id != binding.candidate.candidate_id:
        return _no("contract identity changed on requote -- refusing to substitute")
    try:
        validate_runtime_candidate(refreshed, refreshed.intent_id, intent)
    except Exception as _ve:
        return _no("revalidation failed: %s" % _ve)
    return CandidateBinding(
        candidate=refreshed, primary_contract=binding.primary_contract,
        secondary_contract=binding.secondary_contract,
        quote_observed_monotonic=now_mono,
        underlying_contract=binding.underlying_contract)


def bindings_for_stage_b(bindings: Iterable[CandidateBinding], *,
                         max_age_seconds: float = 10.0) -> list[CandidateBinding]:
    """Public API contract; production-derived narrative omitted."""
    limit = _finite(max_age_seconds)
    if limit is None or limit <= 0:
        raise CandidateBuildError("invalid Stage-B quote-age limit")
    now = time.monotonic()
    fresh: list[CandidateBinding] = []
    for binding in list(bindings or []):
        observed = _finite(binding.quote_observed_monotonic)
        if observed is None or observed <= 0:
            continue
        age = max(0.0, now - observed)
        if age > limit:
            continue
        candidate = replace(binding.candidate, quote_age_seconds=round(age, 6))
        fresh.append(replace(binding, candidate=candidate))
    return fresh


def select_binding(bindings: Iterable[CandidateBinding], candidate_id: str) -> Optional[CandidateBinding]:
    matches = [binding for binding in bindings
               if binding.candidate.candidate_id == candidate_id]
    return matches[0] if len(matches) == 1 else None
