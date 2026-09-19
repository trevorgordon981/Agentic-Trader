"""Public API contract; production-derived narrative omitted."""
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from exitmgr import entry_safety, risk
from exitmgr.config import load_config
from exitmgr.entry_builder import (
    DEBIT_HOLD_FLOOR_MULTIPLE,
    CandidateBuildError,
    _expiry_choices,
    _make_candidate,
    debit_hold_floor,
    entry_policy,
    policy_capital_cap,
    policy_dte_floor,
)
from exitmgr.entry_contract import RuntimeLeg, StageAIntent, validate_candidates
from exitmgr.risk import (
    ProposedTrade,
    conviction_size_authority,
    evaluate_trade,
    soft_size_fraction,
)

CONVICTIONS = tuple(range(1, 11))



_ALLOCATION_SWEEP = (5.0, 8.0, 15.0, 25.0)



NET_LIQ = 5000.0
AVAILABLE = 5000.0
CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "config.yaml")


class _Cons:
    """Public API contract; production-derived narrative omitted."""

    max_premium_pct = 0.10
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
        alpha="momentum", thesis="deterministic-policy test intent",
    )


def _leg(strike, action, *, bid, ask, delta=0.45, con_id=None):
    return RuntimeLeg(con_id=con_id or int(strike * 100), action=action, right="call",
                      strike=float(strike), bid_per_share=bid, ask_per_share=ask,
                      delta=delta, iv=0.42)


def _spread_legs():
    """Public API contract; production-derived narrative omitted."""
    return [_leg(100.0, "buy", bid=5.00, ask=5.20),
            _leg(110.0, "sell", bid=3.10, ask=3.20)]


def _candidate_for(conviction, *, allocation_pct=8.0, hold=21, dte=168):
    intent = _intent(conviction, allocation_pct=allocation_pct, hold=hold)
    policy = entry_policy(intent, NET_LIQ, AVAILABLE, _Cons())
    return intent, policy, _make_candidate(
        intent_id="intent_1", intent=intent, expiry="20270101", dte=dte,
        legs=_spread_legs(), allocation=policy.allocation_budget_usd,
        live_cap=policy.live_cap_usd, quote_utc="2026-08-21T00:00:00Z",
        quote_mono=1.0, max_spread_pct=_Cons.max_entry_spread_pct)



def test_the_dte_floor_is_the_same_for_every_conviction():
    """Public API contract; production-derived narrative omitted."""
    floors = {c: debit_hold_floor(_intent(c, hold=21)) for c in CONVICTIONS}
    assert len(set(floors.values())) == 1, floors
    assert set(floors.values()) == {21 * DEBIT_HOLD_FLOOR_MULTIPLE}


def test_the_dte_floor_is_eight_times_the_intended_hold():
    """Public API contract; production-derived narrative omitted."""
    assert DEBIT_HOLD_FLOOR_MULTIPLE == 8
    for hold in (5, 7, 14, 21, 30):
        for c in CONVICTIONS:
            assert debit_hold_floor(_intent(c, hold=hold)) == hold * 8


def test_the_policy_dte_floor_is_the_same_for_every_conviction():
    for hold in (7, 21):
        floors = {c: policy_dte_floor(_intent(c, hold=hold), _Cons()) for c in CONVICTIONS}
        assert len(set(floors.values())) == 1, floors
        assert floors[10] == max(_Cons.min_dte, hold * 8)


def test_the_credit_dte_floor_is_the_same_for_every_conviction():
    floors = {c: policy_dte_floor(
        _intent(c, hold=7, side="credit", structure="cash secured put"), _Cons())
        for c in CONVICTIONS}
    assert len(set(floors.values())) == 1, floors



_EXPIRIES = ("20260930", "20261120", "20270115", "20270618", "20280121")


def test_expiry_admission_is_identical_for_every_conviction():
    """Public API contract; production-derived narrative omitted."""
    admitted = {c: _expiry_choices(_EXPIRIES, _intent(c, hold=21), _Cons(),
                                   dte_floor=policy_dte_floor(_intent(c, hold=21), _Cons()))
                for c in CONVICTIONS}
    first = admitted[1]
    for c in CONVICTIONS:
        assert admitted[c] == first, (c, admitted[c], first)


def test_a_short_runway_expiry_is_refused_at_every_conviction():
    """Public API contract; production-derived narrative omitted."""
    hold = 21
    short_only = ("20261120",)
    for c in CONVICTIONS:
        intent = _intent(c, hold=hold)
        assert _expiry_choices(short_only, intent, _Cons(),
                               dte_floor=policy_dte_floor(intent, _Cons())) == []


def test_expiry_choices_defaults_to_the_policy_floor_when_none_is_supplied():
    """Public API contract; production-derived narrative omitted."""
    for c in CONVICTIONS:
        intent = _intent(c, hold=21)
        assert (_expiry_choices(_EXPIRIES, intent, _Cons())
                == _expiry_choices(_EXPIRIES, intent, _Cons(),
                                   dte_floor=policy_dte_floor(intent, _Cons())))



def test_the_live_capital_cap_is_identical_for_every_conviction():
    """Public API contract; production-derived narrative omitted."""
    for pct in _ALLOCATION_SWEEP:
        caps = {c: entry_policy(_intent(c, allocation_pct=pct),
                                NET_LIQ, AVAILABLE, _Cons()).live_cap_usd
                for c in CONVICTIONS}
        assert len(set(caps.values())) == 1, (pct, caps)


def test_the_policy_cap_is_identical_for_every_conviction():
    caps = {c: policy_capital_cap(_intent(c), NET_LIQ, AVAILABLE, _Cons())
            for c in CONVICTIONS}
    assert len(set(caps.values())) == 1, caps


    assert math.isclose(caps[1], 0.10 * NET_LIQ, rel_tol=0, abs_tol=1e-9)


def test_the_recorded_conviction_is_carried_but_changes_nothing():
    """Public API contract; production-derived narrative omitted."""
    ref = entry_policy(_intent(1), NET_LIQ, AVAILABLE, _Cons())
    for c in CONVICTIONS:
        p = entry_policy(_intent(c), NET_LIQ, AVAILABLE, _Cons())
        assert p.conviction == c
        assert (p.live_cap_usd, p.policy_cap_usd, p.allocation_budget_usd, p.dte_floor) == (
            ref.live_cap_usd, ref.policy_cap_usd, ref.allocation_budget_usd, ref.dte_floor)



def test_the_affordable_quantity_is_identical_for_every_conviction():
    """Public API contract; production-derived narrative omitted."""
    for pct in _ALLOCATION_SWEEP:
        quantities = {}
        for c in CONVICTIONS:
            _, _, candidate = _candidate_for(c, allocation_pct=pct)
            assert candidate is not None
            quantities[c] = candidate.max_affordable_quantity
        assert len(set(quantities.values())) == 1, (pct, quantities)


def test_the_whole_candidate_is_identical_for_every_conviction():
    """Public API contract; production-derived narrative omitted."""
    reference = None
    for c in CONVICTIONS:
        _, _, candidate = _candidate_for(c)
        payload = candidate.to_dict()
        if reference is None:
            reference = payload
        assert payload == reference, c



def test_contract_validation_admits_identically_for_every_conviction():
    """Public API contract; production-derived narrative omitted."""
    for c in CONVICTIONS:
        intent, policy, candidate = _candidate_for(c)
        variants = []
        for i, strike in enumerate((100.0, 101.0, 102.0)):
            legs = [_leg(strike, "buy", bid=5.00, ask=5.20, con_id=9000 + i),
                    _leg(strike + 10.0, "sell", bid=3.10, ask=3.20, con_id=9500 + i)]
            variants.append(_make_candidate(
                intent_id="intent_1", intent=intent, expiry="20270101", dte=168,
                legs=legs, allocation=policy.allocation_budget_usd,
                live_cap=policy.live_cap_usd, quote_utc="2026-08-21T00:00:00Z",
                quote_mono=1.0, max_spread_pct=_Cons.max_entry_spread_pct))
        admitted = validate_candidates("intent_1", intent, variants)
        assert len(admitted) == 3, c


def test_an_unaffordable_contract_is_refused_at_every_conviction():
    """Public API contract; production-derived narrative omitted."""
    for c in CONVICTIONS:
        intent, policy, _ = _candidate_for(c)
        expensive = [_leg(100.0, "buy", bid=40.0, ask=41.0),
                     _leg(110.0, "sell", bid=3.10, ask=3.20)]
        assert _make_candidate(
            intent_id="intent_1", intent=intent, expiry="20270101", dte=168,
            legs=expensive, allocation=policy.allocation_budget_usd,
            live_cap=policy.live_cap_usd, quote_utc="2026-08-21T00:00:00Z",
            quote_mono=1.0, max_spread_pct=_Cons.max_entry_spread_pct) is None















def test_the_allocation_suggestion_can_never_raise_the_cap():
    """Public API contract; production-derived narrative omitted."""
    policy_cap = policy_capital_cap(_intent(7), NET_LIQ, AVAILABLE, _Cons())
    for pct in (1.0, 2.5, 5.0, 8.0, 10.0, 15.0, 40.0, 100.0):
        cap = entry_policy(_intent(7, allocation_pct=pct), NET_LIQ, AVAILABLE, _Cons()).live_cap_usd
        assert cap <= policy_cap + 1e-9, (pct, cap, policy_cap)


def test_a_greedy_allocation_is_clamped_to_the_deterministic_policy():
    for pct in (25.0, 50.0, 100.0):
        p = entry_policy(_intent(9, allocation_pct=pct), NET_LIQ, AVAILABLE, _Cons())
        assert p.live_cap_usd == p.policy_cap_usd
        assert p.allocation_budget_usd > p.policy_cap_usd


def test_the_credit_side_still_honours_the_suggestion_downward():
    """Public API contract; production-derived narrative omitted."""
    csp = dict(side="credit", structure="cash secured put", direction="bullish")
    policy_cap = entry_policy(_intent(7, allocation_pct=100.0, **csp),
                              NET_LIQ, AVAILABLE, _Cons()).policy_cap_usd
    assert policy_cap == pytest.approx(0.80 * NET_LIQ, abs=0.01)
    small = entry_policy(_intent(7, allocation_pct=5.0, **csp), NET_LIQ, AVAILABLE, _Cons())
    mid = entry_policy(_intent(7, allocation_pct=8.0, **csp), NET_LIQ, AVAILABLE, _Cons())
    assert small.live_cap_usd < mid.live_cap_usd < policy_cap
    assert small.allocation_is_binding is True and mid.allocation_is_binding is True
    assert small.live_cap_usd == pytest.approx(250.0, abs=0.01)
    assert mid.live_cap_usd == pytest.approx(400.0, abs=0.01)


def test_the_cash_buffer_still_binds_over_the_policy():
    """Public API contract; production-derived narrative omitted."""
    p = entry_policy(_intent(10, allocation_pct=100.0), NET_LIQ, 300.0, _Cons())
    assert p.live_cap_usd == pytest.approx(300.0 - 0.05 * NET_LIQ, abs=0.01)


def test_a_dead_account_fails_closed_at_every_conviction():
    for c in CONVICTIONS:
        with pytest.raises(CandidateBuildError):
            entry_policy(_intent(c), 0.0, 0.0, _Cons())



def _gate(conviction, limits):
    return evaluate_trade(
        ProposedTrade("MUTX", 100.0, False, conviction=conviction),
        net_liq=NET_LIQ, available_funds=AVAILABLE, open_positions=[],
        pot_day_start=NET_LIQ, approved_names={"MUTX"}, limits=limits)


def test_the_live_config_grants_conviction_no_size_authority():
    """Public API contract; production-derived narrative omitted."""
    cfg = load_config(config_path=CONFIG_PATH, arm=False, loop=False, interval=900)
    limits = entry_safety.risk_limits_from_config(cfg)
    report = conviction_size_authority(limits)
    assert report["is_flat"], report
    assert report["spread_pct_points"] == 0.0, report
    assert not report["bypass_reachable"], report
    caps = {c: _gate(c, limits).per_trade_cap for c in CONVICTIONS}
    assert len(set(caps.values())) == 1, caps


def test_the_risk_gate_soft_fraction_is_read_in_exactly_one_place():
    """Public API contract; production-derived narrative omitted."""
    limits = risk.RiskLimits(max_trade_pct=0.12, max_trade_pct_hard=0.25, allow_any_name=True,
                             conviction_size_multipliers={c: (1.5 if c >= 8 else 1.0)
                                                          for c in CONVICTIONS})
    report = conviction_size_authority(limits)
    for c in CONVICTIONS:
        assert report["per_conviction_soft_pct"][c] == soft_size_fraction(c, limits)[0]
        assert _gate(c, limits).per_trade_cap == pytest.approx(
            min(report["per_conviction_soft_pct"][c] * NET_LIQ, 0.25 * NET_LIQ,
                AVAILABLE - 0.05 * NET_LIQ))

    assert not report["is_flat"]


def test_the_risk_gate_soft_cap_sits_above_the_entry_policy_cap():
    """Public API contract; production-derived narrative omitted."""
    cfg = load_config(config_path=CONFIG_PATH, arm=False, loop=False, interval=900)
    limits = entry_safety.risk_limits_from_config(cfg)
    soft_usd = soft_size_fraction(7, limits)[0] * NET_LIQ
    policy_usd = policy_capital_cap(_intent(7), NET_LIQ, AVAILABLE, _Cons())
    assert soft_usd > policy_usd, (soft_usd, policy_usd)



def _source(name):
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), name)
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def test_evaluate_ticker_does_not_call_the_add_lane_a_trade_bar():
    """Public API contract; production-derived narrative omitted."""
    src = _source("evaluate_ticker.py")
    assert "def _live_trade_bar(" not in src
    assert "(trade bar is >= %d)" not in src
    assert "BELOW THE BAR -- the live trader would NOT take this." not in src
    assert "def _add_lane_min_conviction(" in src
    assert "NOT a trade gate" in src
    assert "NO general conviction bar" in src
    assert "same-day add-name lane in daily_recommend.py" in src


def test_the_entry_builder_never_reads_conviction_for_authority():
    """Public API contract; production-derived narrative omitted."""
    import ast

    tree = ast.parse(_source(os.path.join("exitmgr", "entry_builder.py")))
    functions = {node.name: node for node in ast.walk(tree)
                 if isinstance(node, ast.FunctionDef)}

    def _references(node):
        found = []
        for child in ast.walk(node):
            if isinstance(child, ast.Attribute) and child.attr == "conviction":
                found.append("attribute")
            elif isinstance(child, ast.keyword) and child.arg == "conviction":
                found.append("keyword")
            elif isinstance(child, ast.Name) and child.id == "conviction":
                found.append("name")
        return found

    for name in ("debit_hold_floor", "policy_dte_floor", "policy_capital_cap",
                 "_expiry_choices", "_make_candidate"):
        assert name in functions, name
        assert _references(functions[name]) == [], (name, _references(functions[name]))

    assert sorted(_references(functions["entry_policy"])) == ["attribute", "keyword"]
