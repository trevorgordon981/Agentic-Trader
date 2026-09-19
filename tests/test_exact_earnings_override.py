from datetime import date
from dataclasses import replace
from types import SimpleNamespace
import inspect
import time

import pytest

import daily_recommend as dr
from exitmgr.trader import ResolvedOrder


def _args(**overrides):
    base = dict(
        ticker="NVDA", right="C", dte=86, delta=0.60, structure="",
        hold_days=7, conviction=7, thesis="model recommendation", tp=0.0, stop=0.0,
        exact_expiry="20261120", exact_long_strike=205.0,
        exact_short_strike=210.0, exact_qty=1, exact_max_debit=2.70,
        expected_earnings_date="2026-08-26",
        override_earnings_blackout=True,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _resolved(**overrides):
    long_contract = SimpleNamespace(
        conId=101, secType="OPT", symbol="NVDA", right="C", strike=205.0,
        lastTradeDateOrContractMonth="20261120")
    short_contract = SimpleNamespace(
        conId=102, secType="OPT", symbol="NVDA", right="C", strike=210.0,
        lastTradeDateOrContractMonth="20261120")
    values = dict(
        underlying="NVDA", right="C", expiry="20261120", strike=205.0,
        qty=1, limit=2.65, contract=long_contract,
        short_strike=210.0, short_contract=short_contract,
        structure="call debit spread", entry_bid=2.55, entry_ask=2.65,
        quote_observed_at=time.monotonic(),
    )
    values.update(overrides)
    return ResolvedOrder(**values)


def _bound_spec(**overrides):
    fields = {"long_con_id": 101, "short_con_id": 102}
    fields.update(overrides)
    return replace(dr.exact_spread_from_args(_args()), **fields)


def test_exact_args_are_all_or_none_and_fix_structure_quantity():
    spec = dr.exact_spread_from_args(_args())
    assert (spec.underlying, spec.right, spec.expiry, spec.long_strike,
            spec.short_strike, spec.qty, spec.max_debit) == (
                "NVDA", "C", "20261120", 205.0, 210.0, 1, 2.70)
    idea = dr.user_directed_idea(_args())
    assert idea.structure == "call debit spread"
    assert dr.exact_spread_for(idea) == spec

    with pytest.raises(ValueError, match="missing --exact-short-strike"):
        dr.exact_spread_from_args(_args(exact_short_strike=None))
    with pytest.raises(ValueError, match="missing --right"):
        dr.exact_spread_from_args(_args(right=None))
    with pytest.raises(ValueError, match="not accepted with an exact spread"):
        dr.user_directed_idea(_args(structure="call debit spread"))


def test_exact_put_topology_and_max_debit_are_fail_closed():
    with pytest.raises(ValueError, match="topology"):
        dr.exact_spread_from_args(_args(right="P"))
    with pytest.raises(ValueError, match="must be below"):
        dr.exact_spread_from_args(_args(exact_max_debit=5.0))


def test_only_exact_current_earnings_match_can_waive(monkeypatch):
    spec = dr.exact_spread_from_args(_args())
    monkeypatch.setattr(dr.construction, "earnings_ok", lambda *a, **k: (False, "normal blackout"))
    ok, waived, reason = dr.exact_earnings_gate(
        date(2026, 8, 26), spec.expiry, date(2026, 8, 26), 7, spec)
    assert (ok, waived, reason) == (True, True, "normal blackout")

    ok, waived, reason = dr.exact_earnings_gate(
        date(2026, 8, 26), spec.expiry, date(2026, 8, 27), 7, spec)
    assert not ok and not waived and "!= expected" in reason
    ok, waived, reason = dr.exact_earnings_gate(
        date(2026, 8, 26), spec.expiry, None, 7, spec)
    assert not ok and not waived and "unavailable" in reason

    no_flag = dr.exact_spread_from_args(_args(override_earnings_blackout=False))
    ok, waived, reason = dr.exact_earnings_gate(
        date(2026, 8, 26), no_flag.expiry, date(2026, 8, 26), 7, no_flag)
    assert not ok and not waived and "not explicitly authorized" in reason

    monkeypatch.setattr(dr.construction, "earnings_ok", lambda *a, **k: (True, ""))
    assert dr.exact_earnings_gate(
        date(2026, 8, 26), spec.expiry, date(2026, 8, 26), 7, spec) == (True, False, "")



    monkeypatch.setattr(
        dr.construction, "earnings_ok",
        lambda *a, **k: (True, "earnings overlap -- holding through earnings is allowed"))
    no_flag = dr.exact_spread_from_args(_args(override_earnings_blackout=False))
    assert dr.exact_earnings_gate(
        date(2026, 8, 26), no_flag.expiry, date(2026, 8, 26), 7, no_flag) == (
            True, False, "earnings overlap -- holding through earnings is allowed")


    assert dr.exact_earnings_gate(
        date(2026, 8, 26), no_flag.expiry, date(2026, 8, 27), 7, no_flag) == (
            True, False, "earnings overlap -- holding through earnings is allowed")
    assert dr.exact_earnings_gate(
        date(2026, 8, 26), no_flag.expiry, None, 7, no_flag) == (
            True, False, "earnings overlap -- holding through earnings is allowed")


def test_exact_order_gate_binds_identity_qty_fresh_ask_cap_and_availability():
    spec = _bound_spec()
    assert dr.exact_spread_order_gate(_resolved(), spec, available=265.0) == (True, "")
    assert not dr.exact_spread_order_gate(_resolved(qty=2), spec)[0]
    assert not dr.exact_spread_order_gate(_resolved(entry_ask=2.71), spec)[0]
    assert not dr.exact_spread_order_gate(_resolved(), spec, available=264.99)[0]
    assert not dr.exact_spread_order_gate(
        _resolved(quote_observed_at=time.monotonic() - 30), spec)[0]


@pytest.mark.parametrize("mutate", [
    lambda r: setattr(r.contract, "secType", "STK"),
    lambda r: setattr(r.contract, "symbol", "SYMU"),
    lambda r: setattr(r.contract, "right", "P"),
    lambda r: setattr(r.contract, "lastTradeDateOrContractMonth", "20261218"),
    lambda r: setattr(r.contract, "strike", 210.0),
    lambda r: setattr(r.contract, "conId", 0),
    lambda r: setattr(r.short_contract, "conId", r.contract.conId),
    lambda r: (setattr(r, "contract", r.short_contract),
               setattr(r, "short_contract", SimpleNamespace(
                   conId=101, secType="OPT", symbol="NVDA", right="C", strike=205.0,
                   lastTradeDateOrContractMonth="20261120"))),
])
def test_exact_money_boundary_rejects_mutated_contract_objects(mutate):
    spec = _bound_spec()
    resolved = _resolved()
    mutate(resolved)
    ok, reason = dr.exact_spread_order_gate(resolved, spec)
    assert not ok
    assert "contract object" in reason or "duplicate conIds" in reason


def test_exact_money_boundary_requires_and_preserves_pinned_conids():
    unbound = dr.exact_spread_from_args(_args())
    assert not dr.exact_spread_order_gate(_resolved(), unbound)[0]
    assert dr.exact_spread_order_gate(_resolved(), _bound_spec()) == (True, "")
    wrong = _bound_spec(long_con_id=999)
    ok, reason = dr.exact_spread_order_gate(_resolved(), wrong)
    assert not ok and "conIds changed" in reason


@pytest.mark.asyncio
async def test_exact_resolver_qualifies_only_named_legs_and_uses_combo_ask(monkeypatch):
    spec = dr.exact_spread_from_args(_args(exact_max_debit=3.0))
    idea = dr.user_directed_idea(_args(exact_max_debit=3.0))
    stock = SimpleNamespace(conId=11, symbol="NVDA")
    long = SimpleNamespace(conId=101, secType="OPT", symbol="NVDA", right="C", strike=205.0,
                           lastTradeDateOrContractMonth="20261120")
    short = SimpleNamespace(conId=102, secType="OPT", symbol="NVDA", right="C", strike=210.0,
                            lastTradeDateOrContractMonth="20261120")
    greeks = SimpleNamespace(delta=0.60, impliedVol=0.40)

    class FakeIB:
        def __init__(self):
            self.qualified_requests = []

        async def qualifyContractsAsync(self, *contracts):
            self.qualified_requests.append(contracts)
            return [stock] if len(contracts) == 1 and getattr(contracts[0], "secType", "") == "STK" else [long, short]

        async def reqTickersAsync(self, *contracts):
            return [SimpleNamespace(contract=long, bid=15.0, ask=15.2,
                                    modelGreeks=greeks, lastGreeks=None),
                    SimpleNamespace(contract=short, bid=12.3, ask=12.5,
                                    modelGreeks=None, lastGreeks=None)]

    ib = FakeIB()
    async def _spot(*_):
        return 200.0
    monkeypatch.setattr(dr, "underlying_price", _spot)
    resolved, why = await dr._resolve(ib, idea, available=300.0, net_liq=4000.0)
    assert why is None
    assert resolved is not None
    assert (resolved.strike, resolved.short_strike, resolved.qty) == (
        spec.long_strike, spec.short_strike, spec.qty)
    assert resolved.entry_bid == pytest.approx(2.5)
    assert resolved.entry_ask == pytest.approx(2.9)
    assert resolved.limit == pytest.approx(2.9)
    assert len(ib.qualified_requests) == 2
    assert dr.exact_spread_for(idea).long_con_id == 101
    assert dr.exact_spread_for(idea).short_con_id == 102
    long.conId, short.conId = 201, 202
    drifted, why = await dr._resolve(ib, idea, available=300.0, net_liq=4000.0)
    assert drifted is None
    assert "changed from pinned binding" in why


@pytest.mark.asyncio
async def test_exact_resolver_itself_refuses_a_mutated_hold_floor():
    idea = dr.user_directed_idea(_args(hold_days=7))


    idea.intended_hold_days = 20

    class BrokerMustNotBeTouched:
        async def qualifyContractsAsync(self, *_):
            raise AssertionError("broker touched before exact resolver DTE gate")

    resolved, why = await dr._resolve(
        BrokerMustNotBeTouched(), idea, available=1000.0, net_liq=4000.0)
    assert resolved is None
    assert "below intended-hold floor" in why


def test_exact_expiry_must_satisfy_hold_multiple():
    with pytest.raises(ValueError, match="below intended-hold floor"):
        dr.user_directed_idea(_args(hold_days=20))
    assert dr.user_directed_idea(_args(hold_days=7)).target_dte >= (
        7 * dr.DEBIT_HOLD_FLOOR_MULTIPLE)


def test_exact_gates_are_wired_at_proposal_and_every_final_requote():
    post_source = inspect.getsource(dr._post_idea)
    run_source = inspect.getsource(dr.run)
    resolve_source = inspect.getsource(dr._resolve)
    assert "exact_earnings_gate(" in post_source
    assert "exact_earnings_gate(" in run_source


    assert "exact_spread_order_gate(" in resolve_source
    assert run_source.count("exact_spread_order_gate(") >= 3
