"""Public API contract; production-derived narrative omitted."""
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock, MagicMock

import exitmgr.trader as trader
from exitmgr import entry_safety
from exitmgr.config import load_config
from exitmgr.entry_reservation import EntryReservationLedger, ReservationDecision
from exitmgr.trader import (
    CREDIT_MAX_COLLATERAL_PCT, CSP_STRUCTURE, ResolvedOrder, Trader, capital_at_risk,
    capital_committed, collateral_capacity, contract_snapshot, credit_structure_ok,
    is_credit, order_summary, plan_idea, required_collateral,
)
from exitmgr.account import PotSnapshot
from exitmgr.risk import RiskLimits
from exitmgr.strategist import TradeIdea
from tests._stage_stub import stub_stage_a


@pytest.fixture(autouse=True)
def _enable_credit_entries(monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    monkeypatch.setenv("EXITMGR_CREDIT_ENTRIES", "1")
    monkeypatch.setenv("EXITMGR_ASSIGNED_STOCK_AUTHORITY", "1")




def _dte_str(days: int) -> str:
    return (datetime.now(timezone.utc).date() + timedelta(days=days)).strftime("%Y%m%d")


def _contract(con_id=901, strike=50.0, right="P", symbol="SPY", sec_type="OPT"):
    c = MagicMock()
    c.conId = con_id
    c.strike = strike
    c.right = right
    c.symbol = symbol
    c.secType = sec_type
    c.currency = "USD"
    c.tradingClass = symbol
    c.multiplier = "100"
    c.lastTradeDateOrContractMonth = _dte_str(30)
    return c


def _csp_idea(*, underlying="SPY", strike=50.0, contracts=1, credit=120.0, structure=CSP_STRUCTURE,
              conviction=7, target_dte=30, is_index=True):
    """Public API contract; production-derived narrative omitted."""
    collateral = strike * 100 * contracts
    idea = TradeIdea(underlying, is_index, "bullish", structure, target_dte, 0.30,
                     0.0, conviction, "sell premium into elevated IV")
    idea.side = "credit"
    idea.strike = strike
    idea.collateral_usd = collateral
    idea.net_credit_usd = credit
    idea.max_loss_usd = round(collateral - credit, 2)
    return idea


def _debit_idea():
    return TradeIdea("SPY", True, "bullish", "long call", 30, 0.60, 300.0, 6, "trend")


def _short_put_position(strike=40.0, qty=-2, symbol="QQQ"):
    """Public API contract; production-derived narrative omitted."""
    p = MagicMock()
    p.contract = _contract(con_id=555, strike=strike, right="P", symbol=symbol)
    p.position = qty
    p.avgCost = 100.0
    return p


def _assigned_stock_position(symbol="QQQ", shares=200, *, residual_option_fields=False):
    """Public API contract; production-derived narrative omitted."""
    p = MagicMock()
    c = MagicMock()
    c.conId = 777
    c.symbol = symbol
    c.secType = "STK"
    c.right = "P" if residual_option_fields else ""
    c.strike = 40.0 if residual_option_fields else 0.0
    p.contract = c
    p.position = shares
    p.avgCost = 40.0
    return p


class _CapturedOrder:
    """Public API contract; production-derived narrative omitted."""

    instances = []

    def __init__(self, **kw):
        self.__dict__.update(kw)
        self.orderRef = None
        _CapturedOrder.instances.append(self)


def _pin_ibkr(monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    import importlib
    import sys
    mod = importlib.import_module("exitmgr.ibkr")
    monkeypatch.setitem(sys.modules, "exitmgr.ibkr", mod)
    return mod


def _trader(tmp_path, monkeypatch, *, net_liq=100_000.0, available=60_000.0,
            positions=(), open_orders=(), trading_down=False, limits=None,
            credit_enabled=True, assigned_stock_authority=True):
    """Public API contract; production-derived narrative omitted."""
    monkeypatch.setenv("EXITMGR_ORDER_LOCK", str(tmp_path / "order.lock"))
    _CapturedOrder.instances = []
    monkeypatch.setattr(_pin_ibkr(monkeypatch), "Order", _CapturedOrder)
    monkeypatch.setattr(trader, "get_pot_snapshot",
                        AsyncMock(return_value=PotSnapshot(net_liq, available, available)))

    placed = MagicMock()
    placed.orderStatus.status = "Filled"
    placed.orderStatus.avgFillPrice = 1.20
    placed.log = []
    placed.fills = []
    placed.order.orderRef = "alfred-entry:x"
    placed.order.orderId = 42

    ibc = MagicMock()
    ibc.ib = MagicMock()
    ibc.ib.placeOrder.return_value = placed





    def _fill_completely(_contract, _order):
        placed.orderStatus.filled = float(getattr(_order, "totalQuantity", 0) or 0)
        placed.orderStatus.remaining = 0.0
        return placed
    ibc.ib.placeOrder.side_effect = _fill_completely
    ibc.ib.reqPositionsAsync = AsyncMock(return_value=list(positions))
    ibc.ib.reqAllOpenOrdersAsync = AsyncMock(return_value=list(open_orders))
    ibc.get_positions = AsyncMock(return_value={})

    if trading_down:
        (tmp_path / "TRADING_DOWN").write_text("")
    t = Trader(ib_conn=ibc, exit_manager=MagicMock(), limits=limits or RiskLimits(),
               approved_names={"SPY"}, endpoint="http://x", model="m", slack_token="tok",
               slack_channel="C1", approver_ids={"OWNER"},
               baseline_path=str(tmp_path / "b.json"), audit_path=str(tmp_path / "a.jsonl"),
               journal_path=str(tmp_path / "trades.log"),
               config_path=str(tmp_path / "config.yaml"),
               trading_down_path=str(tmp_path / "TRADING_DOWN"),
               kill_switch_path=str(tmp_path / "KILL_SWITCH"),




               entry_reservation_ledger=EntryReservationLedger(
                   ledger_path=tmp_path / "reservations.json",
                   lock_path=tmp_path / "reservations.lock"))



    t.resolved_config.credit_entries_enabled = credit_enabled
    t.resolved_config.assigned_stock_authority_enabled = assigned_stock_authority
    return t


def _csp_order(*, strike=50.0, qty=1, credit=1.20, bid=1.20, ask=1.30, con_id=901,
               decision_id="decision-" + "a" * 32):
    collateral = round(strike * 100 * qty, 2)
    net_credit = round(credit * 100 * qty, 2)
    return ResolvedOrder(
        "SPY", "P", _dte_str(30), strike, qty, credit, _contract(con_id, strike, "P"),
        entry_bid=bid, entry_ask=ask, quote_observed_at=time.monotonic(),
        decision_id=decision_id, dte=30,
        side="credit", collateral_usd=collateral, net_credit_usd=net_credit,
        credit_max_loss_usd=round(collateral - net_credit, 2))


def _debit_order(decision_id="decision-" + "d" * 32):
    return ResolvedOrder(
        "SPY", "C", _dte_str(30), 610.0, 1, 1.20, _contract(111, 610.0, "C"),
        entry_bid=1.15, entry_ask=1.25, quote_observed_at=time.monotonic(),
        decision_id=decision_id, dte=30)


def _journal_rows(tmp_path):
    p = tmp_path / "trades.log"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]




def test_short_option_authority_requires_two_literal_true_values_and_ignores_debits():
    assert entry_safety.short_option_entry_authority(
        credit=True, credit_entries_enabled=True,
        assigned_stock_authority_enabled=True).allowed
    for credit_enabled, stock_authority in ((False, True), (True, False), ("true", True),
                                             (True, "true"), (None, True), (True, None)):
        decision = entry_safety.short_option_entry_authority(
            credit=True, credit_entries_enabled=credit_enabled,
            assigned_stock_authority_enabled=stock_authority)
        assert not decision.allowed
    assert entry_safety.short_option_entry_authority(
        credit=False, credit_entries_enabled=False,
        assigned_stock_authority_enabled=False).allowed


def test_csp_is_rejected_at_proposal_when_assigned_stock_authority_is_absent(monkeypatch):
    monkeypatch.setenv("EXITMGR_ASSIGNED_STOCK_AUTHORITY", "0")
    plan = plan_idea(
        _csp_idea(), net_liq=100_000.0, available_funds=90_000.0, positions=[],
        baseline=100_000.0, approved_names={"SPY"}, limits=RiskLimits())
    assert plan.action == "gate_rejected"
    assert any("assigned-stock authority" in reason for reason in plan.gate.reasons)


@pytest.mark.asyncio
async def test_daily_csp_is_rejected_before_resolution_when_assignment_authority_is_absent(
        tmp_path, monkeypatch):
    import daily_recommend as daily
    monkeypatch.setattr(
        daily, "_RESOLVED_CONFIG",
        SimpleNamespace(credit_entries_enabled=True, assigned_stock_authority_enabled=False))
    resolve = AsyncMock(side_effect=AssertionError("authority gate must precede resolution"))
    collateral = AsyncMock(side_effect=AssertionError("authority gate must precede broker reads"))
    monkeypatch.setattr(daily, "_resolve", resolve)
    monkeypatch.setattr(daily, "broker_deployed_csp_collateral", collateral)
    rejected = MagicMock()
    monkeypatch.setattr(daily, "_daily_cap_rejected", rejected)

    result = await daily._post_idea(
        MagicMock(), _csp_idea(), PotSnapshot(100_000.0, 90_000.0, 90_000.0), 0.12,
        "token", "channel", str(tmp_path / "audit.jsonl"), [])

    assert result is None
    resolve.assert_not_awaited()
    collateral.assert_not_awaited()
    assert rejected.call_args.args[0] == "assigned_stock_authority"


@pytest.mark.asyncio
async def test_csp_is_rejected_at_final_submit_when_assigned_stock_authority_is_absent(
        tmp_path, monkeypatch):
    t = _trader(tmp_path, monkeypatch, assigned_stock_authority=False)
    with pytest.raises(RuntimeError, match="assigned-stock authority"):
        await t._submit_order(
            _csp_order(),
            submission_authority=entry_safety.EntrySubmissionAuthority.autonomous())
    t.ib_conn.ib.placeOrder.assert_not_called()
    assert _journal_rows(tmp_path) == []


@pytest.mark.asyncio
async def test_assignment_authority_gate_does_not_change_long_debit_submit(tmp_path, monkeypatch):
    t = _trader(tmp_path, monkeypatch, credit_enabled=False, assigned_stock_authority=False)
    status, _ = await t._submit_order(
        _debit_order(),
        submission_authority=entry_safety.EntrySubmissionAuthority.autonomous())
    assert status == "Filled"
    assert t.ib_conn.ib.placeOrder.call_args[0][1].action == "BUY"


def test_checked_in_config_cannot_silently_leave_credit_entries_live():
    repo = Path(__file__).resolve().parents[1]
    cfg = load_config(str(repo / "config.yaml"))
    fixture_cfg = load_config(str(repo / "tests/fixtures/config_live_snapshot.yaml"))
    for resolved in (cfg, fixture_cfg):
        assert resolved.credit_entries_enabled is False
        assert resolved.assigned_stock_authority_enabled is False




def test_invariant1_naked_call_credit_idea_is_rejected():
    """Public API contract; production-derived narrative omitted."""
    for structure in ("naked call", "short call", "short strangle", "iron condor",
                      "cash secured call", "put", "credit spread"):
        idea = _csp_idea(structure=structure)
        ok, why = credit_structure_ok(idea)
        assert not ok, f"{structure!r} must be REFUSED"
        assert "NAKED-SHORT REFUSED" in why
    ok, _ = credit_structure_ok(_csp_idea())
    assert ok, "a genuine cash-secured put must still pass"


def test_invariant1_debit_ideas_are_untouched_by_the_credit_check():
    ok, why = credit_structure_ok(_debit_idea())
    assert ok and why == ""
    naked_call_but_debit = TradeIdea("SPY", True, "bullish", "naked call", 30, 0.6, 300.0, 6, "x")
    assert credit_structure_ok(naked_call_but_debit) == (True, "")


@pytest.mark.asyncio
async def test_invariant1_naked_call_builds_no_order(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    t = _trader(tmp_path, monkeypatch)
    resolved = await t._resolve_order(_csp_idea(structure="naked call"), per_trade_cap=1e9)
    assert resolved is None
    t.ib_conn.ib.qualifyContractsAsync.assert_not_called()
    t.ib_conn.ib.placeOrder.assert_not_called()
    events = [json.loads(l)["event"] for l in (tmp_path / "a.jsonl").read_text().splitlines()]
    assert "credit_structure_rejected" in events


@pytest.mark.asyncio
async def test_invariant1_short_call_refused_at_submit_even_if_it_gets_that_far(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    t = _trader(tmp_path, monkeypatch)
    r = _csp_order()
    r.right = "C"
    with pytest.raises(RuntimeError, match="NAKED-SHORT REFUSED"):
        await t._submit_order(
            r, submission_authority=entry_safety.EntrySubmissionAuthority.autonomous())
    t.ib_conn.ib.placeOrder.assert_not_called()
    assert _journal_rows(tmp_path) == []


@pytest.mark.asyncio
async def test_invariant1_multileg_short_refused_at_submit(tmp_path, monkeypatch):
    t = _trader(tmp_path, monkeypatch)
    r = _csp_order()
    r.short_contract = _contract(902, 45.0, "P")
    r.short_strike = 45.0
    with pytest.raises(RuntimeError, match="NAKED-SHORT REFUSED"):
        await t._submit_order(
            r, submission_authority=entry_safety.EntrySubmissionAuthority.autonomous())
    t.ib_conn.ib.placeOrder.assert_not_called()


@pytest.mark.asyncio
async def test_NEGATIVE_CONTROL_invariant1_test_is_not_vacuous(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    t = _trader(tmp_path, monkeypatch)
    monkeypatch.setattr(trader, "credit_structure_ok", lambda idea: (True, ""))
    monkeypatch.setattr(_pin_ibkr(monkeypatch), "Stock", lambda *a, **k: MagicMock())
    t.ib_conn.ib.qualifyContractsAsync = AsyncMock(return_value=[MagicMock(conId=1)])
    t.ib_conn.ib.reqSecDefOptParamsAsync = AsyncMock(return_value=[])
    await t._resolve_order(_csp_idea(structure="naked call"), per_trade_cap=1e9)
    t.ib_conn.ib.qualifyContractsAsync.assert_called()




def test_invariant2_required_collateral_arithmetic():
    assert required_collateral(50.0, 1) == 5_000.0
    assert required_collateral(50.0, 3) == 15_000.0
    assert required_collateral(12.5, 4) == 5_000.0
    for bad in ((0.0, 1), (-50.0, 1), (50.0, 0), (50.0, -1), (None, 1), ("x", 1), (50.0, None)):
        assert required_collateral(*bad) is None, f"{bad} must be unusable, never assumed"


@pytest.mark.asyncio
async def test_invariant2_valid_csp_builds_the_right_sell_put_order(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    t = _trader(tmp_path, monkeypatch, net_liq=100_000.0, available=60_000.0)
    r = _csp_order(strike=50.0, qty=2, credit=1.20, bid=1.20, ask=1.35)
    assert capital_committed(r) == 10_000.0
    assert capital_at_risk(r) == 9_760.0

    status, _ = await t._submit_order(
        r, submission_authority=entry_safety.EntrySubmissionAuthority.autonomous())
    assert status == "Filled"

    t.ib_conn.ib.placeOrder.assert_called_once()
    placed_contract = t.ib_conn.ib.placeOrder.call_args[0][0]
    assert placed_contract is r.contract
    t.ib_conn.create_combo_contract.assert_not_called()

    order = t.ib_conn.ib.placeOrder.call_args[0][1]
    assert order.action == "SELL"
    assert order.orderType == "LMT" and order.tif == "DAY"
    assert order.lmtPrice == 1.20
    assert order.totalQuantity == 2
    assert order.orderRef == "alfred-entry:" + "a" * 32

    rec = _journal_rows(tmp_path)[-1]
    assert rec["side"] == "credit" and rec["structure"] == CSP_STRUCTURE
    assert rec["action"] == "SELL"
    assert rec["quantity"] == -2
    assert rec["collateral_usd"] == 10_000.0
    assert rec["net_credit_usd"] == 240.0
    assert rec["max_loss_usd"] == 9_760.0
    assert rec["debit"] == 9_760.0
    assert rec["assignment_possible"] is True
    assert rec["right"] == "P" and rec["strike"] == 50.0
    assert rec["contract_id"] == 901


@pytest.mark.asyncio
async def test_invariant2_insufficient_cash_REFUSES(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    t = _trader(tmp_path, monkeypatch, net_liq=100_000.0, available=4_000.0)



    with pytest.raises(RuntimeError, match="insufficient unreserved funds"):
        await t._submit_order(
            _csp_order(strike=50.0, qty=1),
            submission_authority=entry_safety.EntrySubmissionAuthority.autonomous())
    t.ib_conn.ib.placeOrder.assert_not_called()
    assert _journal_rows(tmp_path) == []


@pytest.mark.asyncio
async def test_invariant2_collateral_is_verified_AT_SUBMIT_not_at_proposal(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    t = _trader(tmp_path, monkeypatch, net_liq=100_000.0, available=60_000.0)
    r = _csp_order(strike=50.0, qty=1)

    proposal, detail = await t._credit_capacity(r)
    assert proposal.allowed and detail["required"] == 5_000.0


    trader.get_pot_snapshot.return_value = PotSnapshot(100_000.0, 1_000.0, 1_000.0)
    with pytest.raises(RuntimeError, match="collateral gate blocks submit"):
        await t._submit_order(
            r, submission_authority=entry_safety.EntrySubmissionAuthority.autonomous())
    t.ib_conn.ib.placeOrder.assert_not_called()


@pytest.mark.asyncio
async def test_NEGATIVE_CONTROL_invariant2_submit_recheck_is_what_stops_it(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    t = _trader(tmp_path, monkeypatch, net_liq=100_000.0, available=60_000.0)
    r = _csp_order(strike=50.0, qty=1)
    frozen, frozen_detail = await t._credit_capacity(r)
    assert frozen.allowed

    trader.get_pot_snapshot.return_value = PotSnapshot(100_000.0, 1_000.0, 1_000.0)



    _allow = ReservationDecision(True, True, "reserved", (), order_ref=r.decision_id)
    monkeypatch.setattr(trader, "reserve_credit_entry",
                        AsyncMock(return_value=(_allow, None, None)))
    await t._submit_order(
        r, submission_authority=entry_safety.EntrySubmissionAuthority.autonomous())
    t.ib_conn.ib.placeOrder.assert_called_once()
    assert t.ib_conn.ib.placeOrder.call_args[0][1].action == "SELL"


@pytest.mark.asyncio
async def test_invariant2_unverifiable_book_REFUSES(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    t = _trader(tmp_path, monkeypatch, net_liq=100_000.0, available=60_000.0)
    t.ib_conn.ib.reqPositionsAsync = AsyncMock(side_effect=ConnectionError("gateway flap"))
    assert await t._deployed_collateral() is None
    with pytest.raises(RuntimeError, match="could not be verified"):
        await t._submit_order(
            _csp_order(),
            submission_authority=entry_safety.EntrySubmissionAuthority.autonomous())
    t.ib_conn.ib.placeOrder.assert_not_called()


def test_invariant2_capacity_is_fail_closed_on_every_missing_input():
    good = dict(required=5_000.0, deployed=0.0, net_liq=100_000.0, available_funds=60_000.0)
    assert collateral_capacity(**good).allowed
    for field in ("required", "deployed", "net_liq", "available_funds"):
        bad = dict(good, **{field: None})
        assert not collateral_capacity(**bad).allowed, f"{field}=None must REFUSE"
    for bad in (dict(good, required=0.0), dict(good, required=-1.0), dict(good, net_liq=0.0),
                dict(good, available_funds=-1.0), dict(good, required=float("nan")),
                dict(good, net_liq=float("inf"))):
        assert not collateral_capacity(**bad).allowed




def test_invariant3_cap_counts_already_deployed_collateral():
    nl = 100_000.0
    cap = CREDIT_MAX_COLLATERAL_PCT * nl
    assert collateral_capacity(required=80_000.0, deployed=0.0, net_liq=nl,
                               available_funds=90_000.0).allowed
    assert not collateral_capacity(required=80_000.01, deployed=0.0, net_liq=nl,
                                   available_funds=90_000.0).allowed

    alone = collateral_capacity(required=10_000.0, deployed=0.0, net_liq=nl,
                                available_funds=90_000.0)
    with_book = collateral_capacity(required=10_000.0, deployed=75_000.0, net_liq=nl,
                                    available_funds=90_000.0)
    assert alone.allowed and not with_book.allowed
    assert "collateral cap" in with_book.reasons[0] and f"{cap:,.2f}" in with_book.reasons[0]


@pytest.mark.asyncio
async def test_invariant3_deployed_collateral_reads_live_short_puts(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    t = _trader(tmp_path, monkeypatch,
                positions=[_short_put_position(strike=40.0, qty=-2),
                           _short_put_position(strike=25.0, qty=-1)])
    assert await t._deployed_collateral() == 10_500.0


@pytest.mark.asyncio
async def test_debit_put_vertical_short_legs_are_not_csp_collateral(tmp_path, monkeypatch):
    positions = []
    for con_id, expiry, strike, qty in (
            (101, "20270115", 17.5, 2), (102, "20270115", 15.0, -2),
            (103, "20270319", 17.5, 1), (104, "20270319", 15.0, -1)):
        pos = _short_put_position(strike=strike, qty=qty, symbol="SYMZ")
        pos.contract.conId = con_id
        pos.contract.lastTradeDateOrContractMonth = expiry
        positions.append(pos)
    t = _trader(tmp_path, monkeypatch, positions=positions)
    assert await t._deployed_collateral() == 0.0


@pytest.mark.asyncio
async def test_only_quantity_proven_by_long_put_is_excluded(tmp_path, monkeypatch):
    long_leg = _short_put_position(strike=17.5, qty=1, symbol="SYMZ")
    short_leg = _short_put_position(strike=15.0, qty=-2, symbol="SYMZ")
    t = _trader(tmp_path, monkeypatch, positions=[long_leg, short_leg])
    assert await t._deployed_collateral() == 1_500.0


@pytest.mark.asyncio
async def test_adjusted_contract_cannot_cover_standard_short_put(tmp_path, monkeypatch):
    long_leg = _short_put_position(strike=17.5, qty=1, symbol="SYMZ")
    long_leg.contract.multiplier = "10"
    short_leg = _short_put_position(strike=15.0, qty=-1, symbol="SYMZ")
    short_leg.contract.multiplier = "100"
    t = _trader(tmp_path, monkeypatch, positions=[long_leg, short_leg])
    assert await t._deployed_collateral() == 1_500.0


@pytest.mark.asyncio
async def test_invariant3_working_sell_to_open_reserves_collateral_before_it_fills(tmp_path, monkeypatch):
    working = MagicMock()
    working.order.action = "SELL"
    working.order.orderRef = "alfred-entry:" + "c" * 32
    working.order.totalQuantity = 1
    working.contract = _contract(strike=30.0, right="P")
    working.orderStatus.status = "Submitted"
    exit_order = MagicMock()
    exit_order.order.action = "SELL"
    exit_order.order.orderRef = "alfred-exit:123"
    exit_order.order.totalQuantity = 5
    exit_order.contract = _contract(strike=99.0, right="P")
    exit_order.orderStatus.status = "Submitted"
    t = _trader(tmp_path, monkeypatch, open_orders=[working, exit_order])
    assert await t._deployed_collateral() == 3_000.0


@pytest.mark.asyncio
async def test_invariant3_exceeding_80pct_aggregate_REFUSES(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    t = _trader(tmp_path, monkeypatch, net_liq=100_000.0, available=90_000.0,
                positions=[_short_put_position(strike=300.0, qty=-2)])
    assert await t._deployed_collateral() == 60_000.0
    with pytest.raises(RuntimeError, match="collateral cap"):
        await t._submit_order(
            _csp_order(strike=250.0, qty=1),
            submission_authority=entry_safety.EntrySubmissionAuthority.autonomous())
    t.ib_conn.ib.placeOrder.assert_not_called()


@pytest.mark.asyncio
async def test_NEGATIVE_CONTROL_invariant3_ignoring_the_book_would_let_it_through(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    t = _trader(tmp_path, monkeypatch, net_liq=100_000.0, available=90_000.0,
                positions=[_short_put_position(strike=300.0, qty=-2)])


    monkeypatch.setattr(trader, "broker_csp_collateral_snapshot",
                        AsyncMock(return_value=trader.BrokerCollateralSnapshot(
                            0.0, frozenset(), frozenset())))
    await t._submit_order(
        _csp_order(strike=250.0, qty=1),
        submission_authority=entry_safety.EntrySubmissionAuthority.autonomous())
    t.ib_conn.ib.placeOrder.assert_called_once()


def test_invariant3_risk_gate_measures_the_full_collateral_not_the_credit():
    """Public API contract; production-derived narrative omitted."""




    idea = _csp_idea(strike=850.0, contracts=1, credit=120.0)
    plan = plan_idea(idea, net_liq=100_000.0, available_funds=90_000.0, positions=[],
                     baseline=100_000.0, approved_names={"SPY"}, limits=RiskLimits())
    assert plan.trade.notional == 85_000.0
    assert plan.action == "gate_rejected"
    small = _csp_idea(strike=50.0, contracts=1, credit=120.0)
    assert plan_idea(small, net_liq=100_000.0, available_funds=90_000.0, positions=[],
                     baseline=100_000.0, approved_names={"SPY"},
                     limits=RiskLimits()).action == "needs_approval"


def test_invariant3_credit_idea_with_unusable_collateral_can_only_be_rejected():
    idea = _csp_idea()
    idea.collateral_usd = 0.0
    plan = plan_idea(idea, net_liq=100_000.0, available_funds=90_000.0, positions=[],
                     baseline=100_000.0, approved_names={"SPY"}, limits=RiskLimits())
    assert plan.action == "gate_rejected"


def test_NEGATIVE_CONTROL_gate_on_credit_received_would_pass_the_oversized_put():
    """Public API contract; production-derived narrative omitted."""


    idea = _csp_idea(strike=850.0, contracts=1, credit=120.0)
    idea.collateral_usd = idea.net_credit_usd
    plan = plan_idea(idea, net_liq=100_000.0, available_funds=90_000.0, positions=[],
                     baseline=100_000.0, approved_names={"SPY"}, limits=RiskLimits())
    assert plan.action == "needs_approval"




@pytest.mark.asyncio
async def test_invariant4_trading_down_blocks_a_credit_entry_at_submit(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    t = _trader(tmp_path, monkeypatch, trading_down=True)
    with pytest.raises(RuntimeError, match="entry markers block submit"):
        await t._submit_order(
            _csp_order(),
            submission_authority=entry_safety.EntrySubmissionAuthority.autonomous())
    t.ib_conn.ib.placeOrder.assert_not_called()
    assert _journal_rows(tmp_path) == []


@pytest.mark.asyncio
async def test_invariant4_kill_switch_blocks_a_credit_entry(tmp_path, monkeypatch):
    t = _trader(tmp_path, monkeypatch)
    (tmp_path / "KILL_SWITCH").write_text("")
    with pytest.raises(RuntimeError, match="entry markers block submit"):
        await t._submit_order(
            _csp_order(),
            submission_authority=entry_safety.EntrySubmissionAuthority.autonomous())
    t.ib_conn.ib.placeOrder.assert_not_called()


@pytest.mark.asyncio
async def test_invariant4_trading_down_stops_a_credit_idea_before_it_is_ever_proposed(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    monkeypatch.setattr(trader.research, "gather", AsyncMock(return_value={}))
    monkeypatch.setattr(trader, "_market_open", lambda: True)
    monkeypatch.setattr(trader.approval, "post_proposal", lambda *a, **k: "ts1")
    monkeypatch.setattr(trader.approval, "await_approval",
                        lambda *a, **k: pytest.fail("must never seek approval under TRADING_DOWN"))
    called = []
    stub_stage_a(monkeypatch, lambda *a, **k: called.append(1) or [_csp_idea()])
    t = _trader(tmp_path, monkeypatch, trading_down=True)
    t.exit_manager.run_cycle = AsyncMock()
    t._submit_order = AsyncMock()
    await t.run_once(dry_run=False)
    assert called == [], "strategist must not run while entries are halted"
    t._submit_order.assert_not_called()
    events = [json.loads(l) for l in (tmp_path / "a.jsonl").read_text().splitlines()]
    skipped = [e for e in events if e["event"] == "strategist_skipped"]
    assert skipped and "TRADING_DOWN active" in skipped[-1]["reason"]


@pytest.mark.asyncio
async def test_NEGATIVE_CONTROL_invariant4_marker_gate_is_what_stops_the_credit_order(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    import exitmgr.entry_safety as es
    t = _trader(tmp_path, monkeypatch, trading_down=True)
    monkeypatch.setattr(t, "_entry_markers_clear", lambda: es.SafetyResult(True, ()))
    await t._submit_order(
        _csp_order(),
        submission_authority=entry_safety.EntrySubmissionAuthority.autonomous())
    t.ib_conn.ib.placeOrder.assert_called_once()




@pytest.mark.asyncio
async def test_invariant5_assignment_releases_collateral_without_crashing(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    t = _trader(tmp_path, monkeypatch,
                positions=[_short_put_position(strike=40.0, qty=-2),
                           _assigned_stock_position("QQQ", 200)])
    deployed = await t._deployed_collateral()
    assert deployed == 8_000.0, "assigned stock reserves nothing; only the live short put does"

    fully_assigned = _trader(tmp_path, monkeypatch,
                             positions=[_assigned_stock_position("QQQ", 200)])
    assert await fully_assigned._deployed_collateral() == 0.0


@pytest.mark.asyncio
async def test_invariant5_stock_is_excluded_by_secType_not_by_luck(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    t = _trader(tmp_path, monkeypatch,
                positions=[_assigned_stock_position("QQQ", 200, residual_option_fields=True),
                           _assigned_stock_position("SPY", -100, residual_option_fields=True)])
    assert await t._deployed_collateral() == 0.0


@pytest.mark.asyncio
async def test_invariant5_assignment_leaves_no_orphaned_journal_state(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    t = _trader(tmp_path, monkeypatch, net_liq=100_000.0, available=60_000.0)
    await t._submit_order(
        _csp_order(strike=50.0, qty=1),
        submission_authority=entry_safety.EntrySubmissionAuthority.autonomous())
    rec = _journal_rows(tmp_path)[-1]
    assert rec["side"] == "credit" and rec["assignment_possible"] is True

    assigned = _trader(tmp_path, monkeypatch, positions=[_assigned_stock_position("SPY", 100)])
    assigned.journal_path = str(tmp_path / "trades.log")
    assert await assigned._deployed_collateral() == 0.0
    assigned._load_journal_debits()
    assert await assigned._open_positions() == []

    from exitmgr import construction
    book = construction.open_book_items({}, str(tmp_path / "trades.log"), [])
    assert book == {}, "a credit row must never leak into the long-premium deployment book"


@pytest.mark.asyncio
async def test_invariant5_a_short_call_in_the_book_is_surfaced_not_silently_valued(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    short_call = _short_put_position(strike=40.0, qty=-1)
    short_call.contract.right = "C"
    t = _trader(tmp_path, monkeypatch, positions=[short_call])
    assert await t._deployed_collateral() == 0.0
    events = [json.loads(l)["event"] for l in (tmp_path / "a.jsonl").read_text().splitlines()]
    assert "short_call_position_detected" in events


@pytest.mark.asyncio
async def test_invariant5_debit_vertical_short_call_is_classified_not_alarmed(tmp_path, monkeypatch):
    long_call = _short_put_position(strike=40.0, qty=2, symbol="QQQ")
    long_call.contract.right = "C"
    long_call.contract.conId = 601
    short_call = _short_put_position(strike=45.0, qty=-2, symbol="QQQ")
    short_call.contract.right = "C"
    short_call.contract.conId = 602
    t = _trader(tmp_path, monkeypatch, positions=[long_call, short_call])

    assert await t._deployed_collateral() == 0.0
    rows = [json.loads(line) for line in (tmp_path / "a.jsonl").read_text().splitlines()]
    assert "short_call_position_detected" not in {row["event"] for row in rows}
    classified = [row for row in rows if row["event"] == "debit_spread_short_call_classified"]
    assert len(classified) == 1
    assert classified[0]["covered_quantity"] == 2.0


@pytest.mark.asyncio
async def test_invariant5_short_call_alert_reports_only_residual_naked_quantity(tmp_path, monkeypatch):
    long_call = _short_put_position(strike=40.0, qty=1, symbol="QQQ")
    long_call.contract.right = "C"
    short_call = _short_put_position(strike=45.0, qty=-3, symbol="QQQ")
    short_call.contract.right = "C"
    t = _trader(tmp_path, monkeypatch, positions=[long_call, short_call])

    assert await t._deployed_collateral() == 0.0
    rows = [json.loads(line) for line in (tmp_path / "a.jsonl").read_text().splitlines()]
    alarms = [row for row in rows if row["event"] == "short_call_position_detected"]
    assert len(alarms) == 1
    assert alarms[0]["quantity"] == -2.0
    assert alarms[0]["covered_quantity"] == 1.0


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["expiry", "currency", "tradingClass", "multiplier", "strike"])
async def test_invariant5_only_exact_lower_strike_long_call_can_cover_short(
        tmp_path, monkeypatch, mismatch):
    long_call = _short_put_position(strike=40.0, qty=1, symbol="QQQ")
    long_call.contract.right = "C"
    short_call = _short_put_position(strike=45.0, qty=-1, symbol="QQQ")
    short_call.contract.right = "C"
    if mismatch == "expiry":
        long_call.contract.lastTradeDateOrContractMonth = _dte_str(31)
    elif mismatch == "currency":
        long_call.contract.currency = "CAD"
    elif mismatch == "tradingClass":
        long_call.contract.tradingClass = "QQQ1"
    elif mismatch == "multiplier":
        long_call.contract.multiplier = "10"
    else:
        long_call.contract.strike = 50.0
    t = _trader(tmp_path, monkeypatch, positions=[long_call, short_call])

    assert await t._deployed_collateral() == 0.0
    rows = [json.loads(line) for line in (tmp_path / "a.jsonl").read_text().splitlines()]
    alarms = [row for row in rows if row["event"] == "short_call_position_detected"]
    assert len(alarms) == 1
    assert alarms[0]["quantity"] == -1.0


@pytest.mark.asyncio
async def test_invariant5_long_call_quantity_is_not_reused_across_short_legs(tmp_path, monkeypatch):
    long_call = _short_put_position(strike=40.0, qty=1, symbol="QQQ")
    long_call.contract.right = "C"
    short_45 = _short_put_position(strike=45.0, qty=-1, symbol="QQQ")
    short_45.contract.right = "C"
    short_50 = _short_put_position(strike=50.0, qty=-1, symbol="QQQ")
    short_50.contract.right = "C"
    t = _trader(tmp_path, monkeypatch, positions=[long_call, short_45, short_50])

    assert await t._deployed_collateral() == 0.0
    rows = [json.loads(line) for line in (tmp_path / "a.jsonl").read_text().splitlines()]
    alarms = [row for row in rows if row["event"] == "short_call_position_detected"]
    assert len(alarms) == 1
    assert alarms[0]["quantity"] == -1.0


@pytest.mark.asyncio
async def test_unreadable_short_put_strike_refuses_rather_than_undercounts(tmp_path, monkeypatch):
    broken = _short_put_position(strike=40.0, qty=-1)
    broken.contract.strike = None
    t = _trader(tmp_path, monkeypatch, positions=[broken])
    assert await t._deployed_collateral() is None




@pytest.mark.asyncio
async def test_debit_path_still_buys_at_the_ask_and_journals_unchanged(tmp_path, monkeypatch):
    t = _trader(tmp_path, monkeypatch)
    r = _debit_order()
    status, _ = await t._submit_order(
        r, submission_authority=entry_safety.EntrySubmissionAuthority.autonomous())
    assert status == "Filled"
    order = t.ib_conn.ib.placeOrder.call_args[0][1]
    assert order.action == "BUY"
    assert order.lmtPrice == 1.25
    rec = _journal_rows(tmp_path)[-1]
    assert rec["debit"] == 120.0 and rec["quantity"] == 1
    for credit_key in ("side", "collateral_usd", "net_credit_usd", "max_loss_usd",
                       "assignment_possible", "structure", "action"):
        assert credit_key not in rec, f"debit journal rows must not grow a {credit_key} key"


@pytest.mark.asyncio
async def test_debit_path_never_reads_live_short_positions(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    t = _trader(tmp_path, monkeypatch)
    t._deployed_collateral = AsyncMock(side_effect=AssertionError("debit must not check collateral"))
    await t._submit_order(
        _debit_order(),
        submission_authority=entry_safety.EntrySubmissionAuthority.autonomous())
    t.ib_conn.ib.placeOrder.assert_called_once()


def test_debit_helpers_are_byte_identical():
    r = _debit_order()
    assert not is_credit(r)
    assert capital_at_risk(r) == round(r.limit * 100 * r.qty, 2) == 120.0
    assert capital_committed(r) == 120.0
    snap = contract_snapshot(r)
    assert snap["max_loss_usd"] == 120.0
    assert "side" not in snap and "collateral_usd" not in snap
    assert "BUY 1x SPY" in order_summary(r)
    spread = ResolvedOrder("IWM", "C", "20260626", 300.0, 1, 1.10, object(),
                           short_strike=305.0, short_contract=object())
    assert "300/305C debit spread" in order_summary(spread)


def test_credit_summary_and_snapshot_say_SELL():
    r = _csp_order(strike=50.0, qty=2, credit=1.20)
    s = order_summary(r)
    assert s.startswith("SELL 2x SPY") and "cash-secured put" in s
    assert "BUY" not in s
    assert "collateral $10,000" in s and "max loss $9,760" in s
    snap = contract_snapshot(r)
    assert snap["side"] == "credit" and snap["action"] == "SELL"
    assert snap["collateral_usd"] == 10_000.0 and snap["max_loss_usd"] == 9_760.0


def test_is_credit_defaults_to_debit_for_anything_ambiguous():
    for obj in (object(), MagicMock(side=None), MagicMock(side=""), MagicMock(side="DEBIT"),
                MagicMock(side=123), _debit_idea()):
        assert not is_credit(obj)
    assert is_credit(MagicMock(side="credit")) and is_credit(MagicMock(side="  CREDIT "))




def test_credit_contract_field_requirements():
    bad_maxloss = _csp_idea()
    bad_maxloss.max_loss_usd = 999.0
    assert not credit_structure_ok(bad_maxloss)[0]

    no_strike = _csp_idea()
    no_strike.strike = 0.0
    assert not credit_structure_ok(no_strike)[0]

    no_credit = _csp_idea()
    no_credit.net_credit_usd = 0.0
    assert not credit_structure_ok(no_credit)[0]

    impossible = _csp_idea(strike=1.0, contracts=1, credit=100.0)
    impossible.max_loss_usd = 0.0
    assert not credit_structure_ok(impossible)[0]

    ok = _csp_idea(strike=50.0, contracts=2, credit=241.0)
    assert credit_structure_ok(ok)[0]




def _chain(*dtes):
    p = MagicMock()
    p.exchange = "SMART"
    p.tradingClass = "SPY"
    p.expirations = [_dte_str(d) for d in dtes]
    p.strikes = [45.0, 50.0, 55.0]
    return p


def _wire_chain(t, monkeypatch, chain, *, bid=1.20, ask=1.30, strike=50.0):
    stk = MagicMock(conId=7)
    opt = _contract(901, strike, "P")
    tk = MagicMock()
    tk.contract, tk.bid, tk.ask, tk.last = opt, bid, ask, (bid + ask) / 2
    tk.modelGreeks = MagicMock(delta=-0.30, theta=0.05, gamma=0.01, vega=0.1, impliedVol=0.35)
    ibkr = _pin_ibkr(monkeypatch)
    monkeypatch.setattr(ibkr, "Stock", lambda *a, **k: MagicMock())
    monkeypatch.setattr(ibkr, "Option", lambda *a, **k: MagicMock())
    monkeypatch.setattr(ibkr, "underlying_price", AsyncMock(return_value=52.0))
    t.ib_conn.ib.qualifyContractsAsync = AsyncMock(side_effect=[[stk], [opt]])
    t.ib_conn.ib.reqSecDefOptParamsAsync = AsyncMock(return_value=[chain])
    t.ib_conn.ib.reqTickersAsync = AsyncMock(return_value=[tk])
    return opt


@pytest.mark.asyncio
async def test_credit_uses_the_3_45_dte_window_a_debit_may_never_reach(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    t = _trader(tmp_path, monkeypatch)
    _wire_chain(t, monkeypatch, _chain(5, 40))
    r = await t._resolve_order(_csp_idea(strike=50.0, target_dte=5), per_trade_cap=1e9)
    assert r is not None and r.dte == 5 and r.right == "P" and is_credit(r)


@pytest.mark.asyncio
async def test_a_debit_idea_can_NEVER_reach_the_credit_floor(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    t = _trader(tmp_path, monkeypatch)
    _wire_chain(t, monkeypatch, _chain(5, 10))
    assert t.construction.min_dte >= 25
    assert await t._resolve_order(_debit_idea(), per_trade_cap=1e9) is None
    events = [json.loads(l) for l in (tmp_path / "a.jsonl").read_text().splitlines()]
    assert any(e["event"] == "construction_rejected" for e in events)


    t2 = _trader(tmp_path, monkeypatch)
    _wire_chain(t2, monkeypatch, _chain(5, 10))
    assert (await t2._resolve_order(_csp_idea(strike=50.0, target_dte=5),
                                    per_trade_cap=1e9)).dte == 5


@pytest.mark.asyncio
async def test_credit_ceiling_is_a_refusal_not_an_adjustment(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    t = _trader(tmp_path, monkeypatch)
    _wire_chain(t, monkeypatch, _chain(200, 400))
    assert await t._resolve_order(_csp_idea(strike=50.0, target_dte=30), per_trade_cap=1e9) is None


@pytest.mark.asyncio
async def test_credit_resolver_refuses_a_one_sided_quote(tmp_path, monkeypatch):
    t = _trader(tmp_path, monkeypatch)
    _wire_chain(t, monkeypatch, _chain(30), bid=-1.0, ask=1.30)
    assert await t._resolve_order(_csp_idea(strike=50.0), per_trade_cap=1e9) is None


@pytest.mark.asyncio
async def test_credit_resolver_hard_rejects_a_put_over_the_per_trade_cap(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    t = _trader(tmp_path, monkeypatch)
    _wire_chain(t, monkeypatch, _chain(30))
    assert await t._resolve_order(_csp_idea(strike=50.0), per_trade_cap=4_000.0) is None


@pytest.mark.asyncio
async def test_credit_resolver_sizes_to_the_cap_and_prices_off_the_bid(tmp_path, monkeypatch):
    t = _trader(tmp_path, monkeypatch)
    _wire_chain(t, monkeypatch, _chain(30), bid=1.10, ask=1.40)
    idea = _csp_idea(strike=50.0, contracts=3, credit=330.0)
    r = await t._resolve_order(idea, per_trade_cap=11_000.0)
    assert r.qty == 2
    assert r.collateral_usd == 10_000.0
    assert r.limit == 1.10
    assert r.net_credit_usd == 220.0
    assert r.credit_max_loss_usd == 9_780.0
    assert r.net_delta < 0 and r.net_theta < 0


@pytest.mark.asyncio
async def test_credit_resolver_refuses_a_substituted_contract(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    t = _trader(tmp_path, monkeypatch)
    _wire_chain(t, monkeypatch, _chain(30), strike=45.0)
    assert await t._resolve_order(_csp_idea(strike=50.0), per_trade_cap=1e9) is None




def test_material_change_on_a_sell_is_measured_on_the_bid_and_the_collateral():
    a = _csp_order(strike=50.0, qty=1, bid=1.20, ask=1.30)
    same = _csp_order(strike=50.0, qty=1, bid=1.20, ask=1.90)
    assert trader.credit_material_changes(a, same) == ()
    worse = _csp_order(strike=50.0, qty=1, bid=1.00, ask=1.30)
    assert any("executable credit changed" in c for c in trader.credit_material_changes(a, worse))
    bigger = _csp_order(strike=50.0, qty=2, bid=1.20, ask=1.30)
    changes = trader.credit_material_changes(a, bigger)
    assert any("quantity changed" in c for c in changes)
    assert any("reserved collateral changed" in c for c in changes)


def test_credit_executable_price_is_the_bid():
    r = _csp_order(bid=1.20, ask=1.90)
    assert trader.credit_executable_price(r) == 1.20
    import exitmgr.entry_safety as es
    assert es.executable_price(r) == 1.90
