"""Public API contract; production-derived narrative omitted."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from exitmgr import construction
from exitmgr.entry_builder import (
    POLICY_MAX_PREMIUM_PCT_CEILING,
    _make_candidate,
    entry_policy,
    policy_capital_cap,
)
from exitmgr.entry_contract import (
    RuntimeLeg,
    StageAIntent,
    validate_candidates,
)


NET_LIQ = 5000.0
AVAILABLE = 5000.0
CONVICTIONS = tuple(range(1, 11))



ALLOCATIONS = (1.0, 2.5, 5.0, 8.0, 10.0, 15.0, 25.0, 40.0, 100.0)


class _Cons:
    """Public API contract; production-derived narrative omitted."""

    max_premium_pct = 0.10
    max_deployed_pct = 0.60
    max_decay_pct_per_day = 0.01
    max_portfolio_decay_pct_per_day = 0.04
    min_dte = 25
    prefer_dte_max = 800
    credit_min_dte = 3
    credit_max_dte = 45
    max_entry_spread_pct = 25.0


def _intent(conviction, *, allocation_pct=8.0, hold=21, side="debit",
            structure="call debit spread", direction="bullish"):
    return StageAIntent(
        underlying="MUTX", side=side, direction=direction, structure=structure,
        target_dte=180, intended_hold_days=hold, target_delta=0.45,
        conviction=conviction, allocation_pct_net_liq=allocation_pct,
        alpha="momentum", thesis="flat-sizing test intent",
    )


def _leg(strike, action, *, bid, ask, delta=0.45, con_id=None):
    return RuntimeLeg(con_id=con_id or int(strike * 100), action=action, right="call",
                      strike=float(strike), bid_per_share=bid, ask_per_share=ask,
                      delta=delta, iv=0.42)


def _variants():
    """Public API contract; production-derived narrative omitted."""
    out = []
    for i, strike in enumerate((100.0, 101.0, 102.0)):
        out.append([_leg(strike, "buy", bid=5.00, ask=5.20, con_id=9000 + i),
                    _leg(strike + 10.0, "sell", bid=3.10, ask=3.20, delta=0.30, con_id=9500 + i)])
    return out


def _admitted(conviction, allocation_pct):
    """Public API contract; production-derived narrative omitted."""
    intent = _intent(conviction, allocation_pct=allocation_pct)
    policy = entry_policy(intent, NET_LIQ, AVAILABLE, _Cons())
    candidates = [_make_candidate(
        intent_id="intent_1", intent=intent, expiry="20270101", dte=168, legs=legs,
        allocation=policy.allocation_budget_usd, live_cap=policy.live_cap_usd,
        quote_utc="2026-08-21T00:00:00Z", quote_mono=1.0,
        max_spread_pct=_Cons.max_entry_spread_pct) for legs in _variants()]
    assert all(c is not None for c in candidates), (conviction, allocation_pct)
    chosen = validate_candidates("intent_1", intent, candidates)[0]
    capital_at_risk = chosen.max_affordable_quantity * chosen.one_contract_cost_usd
    return policy, chosen, capital_at_risk



def test_capital_at_risk_and_contract_count_are_identical_across_the_whole_sweep():
    """Public API contract; production-derived narrative omitted."""
    cells = {(c, pct): _admitted(c, pct)[1:] for c in CONVICTIONS for pct in ALLOCATIONS}
    distinct = {(chosen.max_affordable_quantity, round(car, 2))
                for chosen, car in cells.values()}
    assert len(distinct) == 1, sorted(distinct)
    (quantity, capital), = distinct
    assert quantity == 2 and capital == pytest.approx(420.00, abs=0.01)


def test_the_live_cap_itself_is_identical_across_the_whole_sweep():
    caps = {(c, pct): _admitted(c, pct)[0].live_cap_usd
            for c in CONVICTIONS for pct in ALLOCATIONS}
    assert len(set(caps.values())) == 1, caps
    assert next(iter(caps.values())) == pytest.approx(500.0, abs=0.01)


def test_a_tiny_allocation_can_no_longer_veto_a_trade():
    """Public API contract; production-derived narrative omitted."""
    for pct in (1.0, 2.5):
        _policy, chosen, capital = _admitted(7, pct)
        assert chosen.max_affordable_quantity == 2
        assert capital == pytest.approx(420.00, abs=0.01)



def test_the_flat_size_is_the_policy_cap_not_a_new_number():
    """Public API contract; production-derived narrative omitted."""
    policy_cap = policy_capital_cap(_intent(7), NET_LIQ, AVAILABLE, _Cons())
    assert policy_cap == pytest.approx(_Cons.max_premium_pct * NET_LIQ, abs=1e-9)
    assert _Cons.max_premium_pct <= POLICY_MAX_PREMIUM_PCT_CEILING
    for c in CONVICTIONS:
        for pct in ALLOCATIONS:
            policy, _chosen, _car = _admitted(c, pct)
            assert policy.live_cap_usd == policy.policy_cap_usd
            assert policy.live_cap_usd <= policy_cap + 1e-9


def test_a_model_number_still_cannot_size_above_the_deterministic_policy():
    """Public API contract; production-derived narrative omitted."""
    for pct in ALLOCATIONS:
        p = entry_policy(_intent(7, allocation_pct=pct), NET_LIQ, AVAILABLE, _Cons())
        assert p.live_cap_usd <= p.policy_cap_usd + 1e-9


def test_the_book_cap_is_untouched_by_flat_sizing():
    """Public API contract; production-derived narrative omitted."""
    book_cap = _Cons.max_deployed_pct * NET_LIQ
    flat, old_typical = 500.0, 400.0
    assert book_cap == pytest.approx(3000.0, abs=0.01)



    assert book_cap / flat == pytest.approx(6.0, abs=1e-6)
    assert book_cap / old_typical == pytest.approx(7.5, abs=1e-3)



    six = [(flat, 168)] * 6
    ok, why = construction.check_budget(flat, 168, NET_LIQ, six, _Cons())
    assert not ok and any("total deployed premium" in r for r in why)
    five = [(flat, 168)] * 5
    ok, _why = construction.check_budget(flat, 168, NET_LIQ, five, _Cons())
    assert ok


def test_a_single_flat_entry_still_clears_the_per_trade_premium_cap():
    ok, why = construction.check_budget(500.0, 168, NET_LIQ, [], _Cons())
    assert ok, why



def test_the_model_suggestion_is_still_recorded_for_calibration():
    """Public API contract; production-derived narrative omitted."""
    for pct in ALLOCATIONS:
        policy, chosen, _car = _admitted(7, pct)
        assert policy.model_allocation_pct_net_liq == pytest.approx(pct)
        assert policy.allocation_budget_usd == pytest.approx(round(NET_LIQ * pct / 100.0, 2))
        assert chosen.to_dict()["allocation_budget_usd"] == pytest.approx(
            policy.allocation_budget_usd)
        assert policy.allocation_is_binding is False


def test_the_recorded_suggestion_changes_nothing_that_decides():
    """Public API contract; production-derived narrative omitted."""
    reference = None
    for pct in ALLOCATIONS:
        policy, chosen, capital = _admitted(7, pct)
        decided = (policy.dte_floor, policy.policy_cap_usd, policy.live_cap_usd,
                   chosen.max_affordable_quantity, chosen.one_contract_cost_usd, capital)
        if reference is None:
            reference = decided
        assert decided == reference, pct



def test_flat_sizing_did_not_re_admit_conviction():
    """Public API contract; production-derived narrative omitted."""
    reference = None
    for c in CONVICTIONS:
        policy, chosen, capital = _admitted(c, 8.0)
        assert policy.conviction == c
        decided = (policy.dte_floor, policy.policy_cap_usd, policy.live_cap_usd,
                   chosen.max_affordable_quantity, capital)
        if reference is None:
            reference = decided
        assert decided == reference, c



def test_the_credit_side_is_deliberately_not_flat():
    """Public API contract; production-derived narrative omitted."""
    csp = dict(side="credit", structure="cash secured put", direction="bullish")
    ceiling = entry_policy(_intent(7, allocation_pct=100.0, **csp),
                           NET_LIQ, AVAILABLE, _Cons()).policy_cap_usd
    assert ceiling == pytest.approx(0.80 * NET_LIQ, abs=0.01)
    sized = {pct: entry_policy(_intent(7, allocation_pct=pct, **csp),
                               NET_LIQ, AVAILABLE, _Cons())
             for pct in (5.0, 8.0, 25.0)}
    assert len({p.live_cap_usd for p in sized.values()}) == 3
    assert sized[5.0].live_cap_usd == pytest.approx(250.0, abs=0.01)
    assert all(p.allocation_is_binding is True for p in sized.values())
    assert all(p.live_cap_usd < ceiling for p in sized.values())
