"""SHORT-PUT ORDER PATH -- the code that actually sells options in a real-money account.

CREDIT_PATH_SPEC.md S2. Every one of the five safety invariants gets an explicit test, and every
invariant test is paired with a NEGATIVE CONTROL that deliberately breaks the implementation and
proves the assertion actually catches it (a green test that would stay green against a naked-call
bug is worse than no test).

IBKR is mocked in full: no gateway connection, no order, real or paper, is ever placed. The
order-mutation lock is redirected into tmp_path so a test can never contend with the LIVE trader
process for /tmp/alfred-order-mutation.lock.
"""
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from unittest.mock import AsyncMock, MagicMock

import exitmgr.trader as trader
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


# --------------------------------------------------------------------------- builders / fixtures

def _dte_str(days: int) -> str:
    return (datetime.now(timezone.utc).date() + timedelta(days=days)).strftime("%Y%m%d")


def _contract(con_id=901, strike=50.0, right="P", symbol="SPY", sec_type="OPT"):
    c = MagicMock()
    c.conId = con_id
    c.strike = strike
    c.right = right
    c.symbol = symbol
    c.secType = sec_type
    c.lastTradeDateOrContractMonth = _dte_str(30)
    return c


def _csp_idea(*, underlying="SPY", strike=50.0, contracts=1, credit=120.0, structure=CSP_STRUCTURE,
              conviction=7, target_dte=30, is_index=True):
    """A credit TradeIdea exactly as strategist.py (spec S1) emits one."""
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
    """A LIVE short put as reqPositionsAsync() reports it (negative quantity)."""
    p = MagicMock()
    p.contract = _contract(con_id=555, strike=strike, right="P", symbol=symbol)
    p.position = qty
    p.avgCost = 100.0
    return p


def _assigned_stock_position(symbol="QQQ", shares=200, *, residual_option_fields=False):
    """After assignment the short put is GONE and the account holds shares instead.

    ``residual_option_fields`` models the hostile case: a STOCK row that still carries a right/
    strike (a stale adapter field, a broker echo). secType -- not the absence of a strike -- must
    be what keeps it out of the collateral total, or an assigned position gets double-charged."""
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
    """Stands in for ib_async.Order so the test can read back exactly what was submitted."""

    instances = []

    def __init__(self, **kw):
        self.__dict__.update(kw)
        self.orderRef = None
        _CapturedOrder.instances.append(self)


def _pin_ibkr(monkeypatch):
    """Return exitmgr.ibkr, PINNED into sys.modules for the duration of the test.

    conftest's autouse mock_ib_async fixture wraps each test in ``patch.dict('sys.modules', ...)``,
    which drops any module first imported DURING a test when the test ends. exitmgr.ibkr is
    imported lazily inside trader.py, so without this pin a later test's monkeypatch would land on
    a stale module object while the code under test re-imported a fresh one -- and every assertion
    about the submitted order would silently read conftest's generic mock instead."""
    import importlib
    import sys
    mod = importlib.import_module("exitmgr.ibkr")
    monkeypatch.setitem(sys.modules, "exitmgr.ibkr", mod)
    return mod


def _trader(tmp_path, monkeypatch, *, net_liq=100_000.0, available=60_000.0,
            positions=(), open_orders=(), trading_down=False, limits=None):
    """A Trader wired to mocks only. Halt markers point INSIDE tmp_path so the repo's real
    TRADING_DOWN file can never silently decide a test's outcome in either direction."""
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
               # Per-test ledger. Without this the Trader falls back to the LIVE
               # /tmp/alfred-entry-reservations.json that the armed entry loop reads, so the
               # suite injected phantom collateral into a real-money admission path -- and
               # reservations retained between tests poisoned three cases downstream.
               entry_reservation_ledger=EntryReservationLedger(
                   ledger_path=tmp_path / "reservations.json",
                   lock_path=tmp_path / "reservations.lock"))
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


# =============================================================== INVARIANT 1: never sell a naked call

def test_invariant1_naked_call_credit_idea_is_rejected():
    """A credit idea that is not a cash-secured put is unrepresentable, not merely discouraged."""
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
    assert credit_structure_ok(naked_call_but_debit) == (True, "")   # not a short; not our gate


@pytest.mark.asyncio
async def test_invariant1_naked_call_builds_no_order(tmp_path, monkeypatch):
    """The resolver must refuse BEFORE it qualifies a contract -- no contract, no order."""
    t = _trader(tmp_path, monkeypatch)
    resolved = await t._resolve_order(_csp_idea(structure="naked call"), per_trade_cap=1e9)
    assert resolved is None
    t.ib_conn.ib.qualifyContractsAsync.assert_not_called()
    t.ib_conn.ib.placeOrder.assert_not_called()
    events = [json.loads(l)["event"] for l in (tmp_path / "a.jsonl").read_text().splitlines()]
    assert "credit_structure_rejected" in events


@pytest.mark.asyncio
async def test_invariant1_short_call_refused_at_submit_even_if_it_gets_that_far(tmp_path, monkeypatch):
    """DEFENCE IN DEPTH: hand-forge a credit order for a CALL and drive it straight at the
    submitter. It must raise and place nothing -- the resolver is not the only thing standing
    between this account and unbounded loss."""
    t = _trader(tmp_path, monkeypatch)
    r = _csp_order()
    r.right = "C"
    with pytest.raises(RuntimeError, match="NAKED-SHORT REFUSED"):
        await t._submit_order(r)
    t.ib_conn.ib.placeOrder.assert_not_called()
    assert _journal_rows(tmp_path) == []


@pytest.mark.asyncio
async def test_invariant1_multileg_short_refused_at_submit(tmp_path, monkeypatch):
    t = _trader(tmp_path, monkeypatch)
    r = _csp_order()
    r.short_contract = _contract(902, 45.0, "P")
    r.short_strike = 45.0
    with pytest.raises(RuntimeError, match="NAKED-SHORT REFUSED"):
        await t._submit_order(r)
    t.ib_conn.ib.placeOrder.assert_not_called()


@pytest.mark.asyncio
async def test_NEGATIVE_CONTROL_invariant1_test_is_not_vacuous(tmp_path, monkeypatch):
    """Break the guard (stub the structure check to always approve) and the naked-call idea DOES
    proceed to qualify a contract. That proves the assertion above is load-bearing -- and also
    shows the submit-time guard (previous test) is a genuinely independent second layer."""
    t = _trader(tmp_path, monkeypatch)
    monkeypatch.setattr(trader, "credit_structure_ok", lambda idea: (True, ""))
    monkeypatch.setattr(_pin_ibkr(monkeypatch), "Stock", lambda *a, **k: MagicMock())
    t.ib_conn.ib.qualifyContractsAsync = AsyncMock(return_value=[MagicMock(conId=1)])
    t.ib_conn.ib.reqSecDefOptParamsAsync = AsyncMock(return_value=[])
    await t._resolve_order(_csp_idea(structure="naked call"), per_trade_cap=1e9)
    t.ib_conn.ib.qualifyContractsAsync.assert_called()   # the broken build gets further -> test bites


# ================================= INVARIANT 2: never sell a put without reserved collateral

def test_invariant2_required_collateral_arithmetic():
    assert required_collateral(50.0, 1) == 5_000.0
    assert required_collateral(50.0, 3) == 15_000.0
    assert required_collateral(12.5, 4) == 5_000.0
    for bad in ((0.0, 1), (-50.0, 1), (50.0, 0), (50.0, -1), (None, 1), ("x", 1), (50.0, None)):
        assert required_collateral(*bad) is None, f"{bad} must be unusable, never assumed"


@pytest.mark.asyncio
async def test_invariant2_valid_csp_builds_the_right_sell_put_order(tmp_path, monkeypatch):
    """The happy path, asserted field by field: SELL, PUT, limit at the BID, right quantity,
    right reserved collateral, and a journal row the exit manager can read."""
    t = _trader(tmp_path, monkeypatch, net_liq=100_000.0, available=60_000.0)
    r = _csp_order(strike=50.0, qty=2, credit=1.20, bid=1.20, ask=1.35)
    assert capital_committed(r) == 10_000.0            # 50 * 100 * 2
    assert capital_at_risk(r) == 9_760.0               # collateral - $240 credit

    status, _ = await t._submit_order(r)
    assert status == "Filled"

    t.ib_conn.ib.placeOrder.assert_called_once()
    placed_contract = t.ib_conn.ib.placeOrder.call_args[0][0]
    assert placed_contract is r.contract               # the single put, never a combo
    t.ib_conn.create_combo_contract.assert_not_called()

    order = t.ib_conn.ib.placeOrder.call_args[0][1]
    assert order.action == "SELL"                      # <-- sells to OPEN
    assert order.orderType == "LMT" and order.tif == "DAY"
    assert order.lmtPrice == 1.20                      # we SELL, so we cross at the BID (not the ask)
    assert order.totalQuantity == 2
    assert order.orderRef == "alfred-entry:" + "a" * 32

    rec = _journal_rows(tmp_path)[-1]
    assert rec["side"] == "credit" and rec["structure"] == CSP_STRUCTURE
    assert rec["action"] == "SELL"
    assert rec["quantity"] == -2                       # SHORT, as the broker reports it
    assert rec["collateral_usd"] == 10_000.0
    assert rec["net_credit_usd"] == 240.0
    assert rec["max_loss_usd"] == 9_760.0
    assert rec["debit"] == 9_760.0                     # legacy exposure key = capital at risk
    assert rec["assignment_possible"] is True
    assert rec["right"] == "P" and rec["strike"] == 50.0
    assert rec["contract_id"] == 901


@pytest.mark.asyncio
async def test_invariant2_insufficient_cash_REFUSES(tmp_path, monkeypatch):
    """$4,000 of buying power cannot secure a $5,000 put. Refusal is the correct outcome."""
    t = _trader(tmp_path, monkeypatch, net_liq=100_000.0, available=4_000.0)
    # The cash limb moved from the inline collateral check into reserve_credit_entry ->
    # EntryReservationLedger.reserve, which subtracts OUTSTANDING PENDING reservations too
    # (entry_reservation.py:346-358) -- a strictly tighter gate. Only the wording changed.
    with pytest.raises(RuntimeError, match="insufficient unreserved funds"):
        await t._submit_order(_csp_order(strike=50.0, qty=1))
    t.ib_conn.ib.placeOrder.assert_not_called()
    assert _journal_rows(tmp_path) == []               # and nothing is orphaned in the journal


@pytest.mark.asyncio
async def test_invariant2_collateral_is_verified_AT_SUBMIT_not_at_proposal(tmp_path, monkeypatch):
    """THE invariant-2 test. The account is rich at proposal time and drained before submit --
    exactly what an intervening exit, assignment, or second entry does. A path that only checked
    at proposal time would sell an unsecured put here."""
    t = _trader(tmp_path, monkeypatch, net_liq=100_000.0, available=60_000.0)
    r = _csp_order(strike=50.0, qty=1)

    proposal, detail = await t._credit_capacity(r)
    assert proposal.allowed and detail["required"] == 5_000.0   # affordable at proposal time

    # ... and now the cash is gone.
    trader.get_pot_snapshot.return_value = PotSnapshot(100_000.0, 1_000.0, 1_000.0)
    with pytest.raises(RuntimeError, match="collateral gate blocks submit"):
        await t._submit_order(r)
    t.ib_conn.ib.placeOrder.assert_not_called()


@pytest.mark.asyncio
async def test_NEGATIVE_CONTROL_invariant2_submit_recheck_is_what_stops_it(tmp_path, monkeypatch):
    """Break the submit-time re-verification (freeze it at the proposal-time answer) and the very
    same drained-account scenario DOES place a SELL order. That is the naked put this test suite
    exists to prevent, and it proves the check above is the thing preventing it."""
    t = _trader(tmp_path, monkeypatch, net_liq=100_000.0, available=60_000.0)
    r = _csp_order(strike=50.0, qty=1)
    frozen, frozen_detail = await t._credit_capacity(r)
    assert frozen.allowed

    trader.get_pot_snapshot.return_value = PotSnapshot(100_000.0, 1_000.0, 1_000.0)
    # Freeze the LIVE seam. _submit_order_unlocked calls module-level reserve_credit_entry
    # (trader.py:2806), not t._credit_capacity -- patching the instance attribute injected
    # nothing and this control silently proved nothing.
    _allow = ReservationDecision(True, True, "reserved", (), order_ref=r.decision_id)
    monkeypatch.setattr(trader, "reserve_credit_entry",
                        AsyncMock(return_value=(_allow, None, None)))   # <-- the bug
    await t._submit_order(r)
    t.ib_conn.ib.placeOrder.assert_called_once()
    assert t.ib_conn.ib.placeOrder.call_args[0][1].action == "SELL"


@pytest.mark.asyncio
async def test_invariant2_unverifiable_book_REFUSES(tmp_path, monkeypatch):
    """FAIL CLOSED: if the deployed-collateral read blows up, the book is unknown -- and an
    unknown book is never treated as an empty one."""
    t = _trader(tmp_path, monkeypatch, net_liq=100_000.0, available=60_000.0)
    t.ib_conn.ib.reqPositionsAsync = AsyncMock(side_effect=ConnectionError("gateway flap"))
    assert await t._deployed_collateral() is None
    with pytest.raises(RuntimeError, match="could not be verified"):
        await t._submit_order(_csp_order())
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


# ======================================== INVARIANT 3: collateral <= 80% of net liq, AGGREGATE

def test_invariant3_cap_counts_already_deployed_collateral():
    nl = 100_000.0
    cap = CREDIT_MAX_COLLATERAL_PCT * nl               # $80,000
    assert collateral_capacity(required=80_000.0, deployed=0.0, net_liq=nl,
                               available_funds=90_000.0).allowed          # exactly at the cap
    assert not collateral_capacity(required=80_000.01, deployed=0.0, net_liq=nl,
                                   available_funds=90_000.0).allowed      # a cent over
    # this trade ALONE fits; with the book it does not -- the whole point of invariant 3
    alone = collateral_capacity(required=10_000.0, deployed=0.0, net_liq=nl,
                                available_funds=90_000.0)
    with_book = collateral_capacity(required=10_000.0, deployed=75_000.0, net_liq=nl,
                                    available_funds=90_000.0)
    assert alone.allowed and not with_book.allowed
    assert "collateral cap" in with_book.reasons[0] and f"{cap:,.2f}" in with_book.reasons[0]


@pytest.mark.asyncio
async def test_invariant3_deployed_collateral_reads_live_short_puts(tmp_path, monkeypatch):
    """IBConnection.get_positions() filters short legs out, so the deployed figure must come off
    the RAW broker positions -- otherwise the book always looks empty and the 80% cap is dead."""
    t = _trader(tmp_path, monkeypatch,
                positions=[_short_put_position(strike=40.0, qty=-2),      # $8,000
                           _short_put_position(strike=25.0, qty=-1)])     # $2,500
    assert await t._deployed_collateral() == 10_500.0


@pytest.mark.asyncio
async def test_invariant3_working_sell_to_open_reserves_collateral_before_it_fills(tmp_path, monkeypatch):
    working = MagicMock()
    working.order.action = "SELL"
    working.order.orderRef = "alfred-entry:" + "c" * 32
    working.order.totalQuantity = 1
    working.contract = _contract(strike=30.0, right="P")
    working.orderStatus.status = "Submitted"
    exit_order = MagicMock()                       # a sell-to-CLOSE frees collateral, never reserves it
    exit_order.order.action = "SELL"
    exit_order.order.orderRef = "alfred-exit:123"
    exit_order.order.totalQuantity = 5
    exit_order.contract = _contract(strike=99.0, right="P")
    exit_order.orderStatus.status = "Submitted"
    t = _trader(tmp_path, monkeypatch, open_orders=[working, exit_order])
    assert await t._deployed_collateral() == 3_000.0


@pytest.mark.asyncio
async def test_invariant3_exceeding_80pct_aggregate_REFUSES(tmp_path, monkeypatch):
    """$60,000 already deployed on a $100,000 pot, plus a $25,000 put = $85,000 > $80,000 cap.
    Buying power alone would have waved it straight through."""
    t = _trader(tmp_path, monkeypatch, net_liq=100_000.0, available=90_000.0,
                positions=[_short_put_position(strike=300.0, qty=-2)])   # $60,000 deployed
    assert await t._deployed_collateral() == 60_000.0
    with pytest.raises(RuntimeError, match="collateral cap"):
        await t._submit_order(_csp_order(strike=250.0, qty=1))           # $25,000 more
    t.ib_conn.ib.placeOrder.assert_not_called()


@pytest.mark.asyncio
async def test_NEGATIVE_CONTROL_invariant3_ignoring_the_book_would_let_it_through(tmp_path, monkeypatch):
    """Break the aggregate limb (report an empty book) and the same $85,000 trade IS submitted --
    the exact 'this trade fits, so it's fine' bug invariant 3 names."""
    t = _trader(tmp_path, monkeypatch, net_liq=100_000.0, available=90_000.0,
                positions=[_short_put_position(strike=300.0, qty=-2)])
    # The reservation reads the book via module-level broker_csp_collateral_snapshot
    # (trader.py:337); an instance patch on _deployed_collateral cannot shadow it.
    monkeypatch.setattr(trader, "broker_csp_collateral_snapshot",
                        AsyncMock(return_value=trader.BrokerCollateralSnapshot(
                            0.0, frozenset(), frozenset())))             # <-- the bug
    await t._submit_order(_csp_order(strike=250.0, qty=1))
    t.ib_conn.ib.placeOrder.assert_called_once()


def test_invariant3_risk_gate_measures_the_full_collateral_not_the_credit():
    """plan_idea must hand the SOLVENCY layer the cash the trade ties up. Measuring the $120
    credit instead would wave a $50,000 commitment through every cap in risk.py."""
    # Sized to $85,000 because _credit_limits (trader.py:540-573, the 2026-07-27 "80% of the
    # pot for CSPs" ruling) raises per-trade/name/sector caps to 0.80 for CREDIT ideas. Measured
    # boundary: $80,000 -> needs_approval, $85,000 -> gate_rejected. At $50,000 this asserted a
    # 12% cap that no longer applies to credit, and could not fail.
    idea = _csp_idea(strike=850.0, contracts=1, credit=120.0)            # $85,000 collateral
    plan = plan_idea(idea, net_liq=100_000.0, available_funds=90_000.0, positions=[],
                     baseline=100_000.0, approved_names={"SPY"}, limits=RiskLimits())
    assert plan.trade.notional == 85_000.0        # collateral, NOT the $120 credit -- the invariant
    assert plan.action == "gate_rejected"                                # 80%-of-pot CSP cap binds
    small = _csp_idea(strike=50.0, contracts=1, credit=120.0)            # $5,000 collateral
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
    """If plan_idea had used net_credit_usd, the $50,000 CSP above would read as a $120 trade
    and clear the gate. Shown explicitly so the choice of notional is provably load-bearing."""
    # 850 to match the test above: at strike 500 the honest $50,000 case ALSO returned
    # needs_approval, so this control distinguished nothing and passed vacuously.
    idea = _csp_idea(strike=850.0, contracts=1, credit=120.0)
    idea.collateral_usd = idea.net_credit_usd                            # <-- the bug
    plan = plan_idea(idea, net_liq=100_000.0, available_funds=90_000.0, positions=[],
                     baseline=100_000.0, approved_names={"SPY"}, limits=RiskLimits())
    assert plan.action == "needs_approval"


# ============================= INVARIANT 4: TRADING_DOWN / protective mode halt credit entries

@pytest.mark.asyncio
async def test_invariant4_trading_down_blocks_a_credit_entry_at_submit(tmp_path, monkeypatch):
    """The credit branch sits BEHIND the existing marker gate in _submit_order_unlocked, not
    beside it: the halt is checked before anything credit-specific runs."""
    t = _trader(tmp_path, monkeypatch, trading_down=True)
    with pytest.raises(RuntimeError, match="entry markers block submit"):
        await t._submit_order(_csp_order())
    t.ib_conn.ib.placeOrder.assert_not_called()
    assert _journal_rows(tmp_path) == []


@pytest.mark.asyncio
async def test_invariant4_kill_switch_blocks_a_credit_entry(tmp_path, monkeypatch):
    t = _trader(tmp_path, monkeypatch)
    (tmp_path / "KILL_SWITCH").write_text("")
    with pytest.raises(RuntimeError, match="entry markers block submit"):
        await t._submit_order(_csp_order())
    t.ib_conn.ib.placeOrder.assert_not_called()


@pytest.mark.asyncio
async def test_invariant4_trading_down_stops_a_credit_idea_before_it_is_ever_proposed(tmp_path, monkeypatch):
    """Same halt, whole-cycle: with TRADING_DOWN present run_once must not even call the
    strategist, so a credit idea can never enter the loop. Identical to the debit behaviour."""
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
    """Neutralise the halt marker check and the credit order DOES submit under TRADING_DOWN --
    proving the credit branch inherits the halt rather than merely coexisting with it."""
    import exitmgr.entry_safety as es
    t = _trader(tmp_path, monkeypatch, trading_down=True)
    monkeypatch.setattr(t, "_entry_markers_clear", lambda: es.SafetyResult(True, ()))   # <-- the bug
    await t._submit_order(_csp_order())
    t.ib_conn.ib.placeOrder.assert_called_once()


# ============================= INVARIANT 5: assignment is an accepted outcome, never an error

@pytest.mark.asyncio
async def test_invariant5_assignment_releases_collateral_without_crashing(tmp_path, monkeypatch):
    """After assignment the short put is gone and the account holds STOCK: the cash is spent, the
    shares are owned, and the reservation is correctly released. Nothing may raise."""
    t = _trader(tmp_path, monkeypatch,
                positions=[_short_put_position(strike=40.0, qty=-2),        # $8,000 still short
                           _assigned_stock_position("QQQ", 200)])           # assigned -> shares
    deployed = await t._deployed_collateral()
    assert deployed == 8_000.0, "assigned stock reserves nothing; only the live short put does"

    fully_assigned = _trader(tmp_path, monkeypatch,
                             positions=[_assigned_stock_position("QQQ", 200)])
    assert await fully_assigned._deployed_collateral() == 0.0


@pytest.mark.asyncio
async def test_invariant5_stock_is_excluded_by_secType_not_by_luck(tmp_path, monkeypatch):
    """Hostile variant of the assignment case: STOCK rows that still carry a right/strike, both
    long (a put that was assigned) and short (a call that was assigned away). Neither reserves put
    collateral -- and secType has to be what excludes them, since the option-shaped fields do not."""
    t = _trader(tmp_path, monkeypatch,
                positions=[_assigned_stock_position("QQQ", 200, residual_option_fields=True),
                           _assigned_stock_position("SPY", -100, residual_option_fields=True)])
    assert await t._deployed_collateral() == 0.0


@pytest.mark.asyncio
async def test_invariant5_assignment_leaves_no_orphaned_journal_state(tmp_path, monkeypatch):
    """Journal a CSP, then assign it. The journal row must remain readable and self-describing
    (side/collateral/credit/max_loss), and the entry-exposure readers must not choke on it."""
    t = _trader(tmp_path, monkeypatch, net_liq=100_000.0, available=60_000.0)
    await t._submit_order(_csp_order(strike=50.0, qty=1))
    rec = _journal_rows(tmp_path)[-1]
    assert rec["side"] == "credit" and rec["assignment_possible"] is True

    assigned = _trader(tmp_path, monkeypatch, positions=[_assigned_stock_position("SPY", 100)])
    assigned.journal_path = str(tmp_path / "trades.log")
    assert await assigned._deployed_collateral() == 0.0        # reservation released
    assigned._load_journal_debits()                            # must parse the credit row
    assert await assigned._open_positions() == []              # short/assigned -> no long exposure

    from exitmgr import construction
    book = construction.open_book_items({}, str(tmp_path / "trades.log"), [])
    assert book == {}, "a credit row must never leak into the long-premium deployment book"


@pytest.mark.asyncio
async def test_invariant5_a_short_call_in_the_book_is_surfaced_not_silently_valued(tmp_path, monkeypatch):
    """A short call must not exist in this account. If one ever appears it reserves no cash, so
    it cannot be added to the collateral total -- but it MUST be screamed about, not ignored."""
    short_call = _short_put_position(strike=40.0, qty=-1)
    short_call.contract.right = "C"
    t = _trader(tmp_path, monkeypatch, positions=[short_call])
    assert await t._deployed_collateral() == 0.0
    events = [json.loads(l)["event"] for l in (tmp_path / "a.jsonl").read_text().splitlines()]
    assert "short_call_position_detected" in events


@pytest.mark.asyncio
async def test_unreadable_short_put_strike_refuses_rather_than_undercounts(tmp_path, monkeypatch):
    broken = _short_put_position(strike=40.0, qty=-1)
    broken.contract.strike = None
    t = _trader(tmp_path, monkeypatch, positions=[broken])
    assert await t._deployed_collateral() is None      # unknown, never 0


# =============================================================== the debit path is untouched

@pytest.mark.asyncio
async def test_debit_path_still_buys_at_the_ask_and_journals_unchanged(tmp_path, monkeypatch):
    t = _trader(tmp_path, monkeypatch)
    r = _debit_order()
    status, _ = await t._submit_order(r)
    assert status == "Filled"
    order = t.ib_conn.ib.placeOrder.call_args[0][1]
    assert order.action == "BUY"                      # unchanged
    assert order.lmtPrice == 1.25                     # still the ASK
    rec = _journal_rows(tmp_path)[-1]
    assert rec["debit"] == 120.0 and rec["quantity"] == 1
    for credit_key in ("side", "collateral_usd", "net_credit_usd", "max_loss_usd",
                       "assignment_possible", "structure", "action"):
        assert credit_key not in rec, f"debit journal rows must not grow a {credit_key} key"


@pytest.mark.asyncio
async def test_debit_path_never_reads_live_short_positions(tmp_path, monkeypatch):
    """A debit entry must not acquire a dependency on the credit machinery."""
    t = _trader(tmp_path, monkeypatch)
    t._deployed_collateral = AsyncMock(side_effect=AssertionError("debit must not check collateral"))
    await t._submit_order(_debit_order())
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
    assert "BUY" not in s                             # the human must never read BUY on a sell
    assert "collateral $10,000" in s and "max loss $9,760" in s
    snap = contract_snapshot(r)
    assert snap["side"] == "credit" and snap["action"] == "SELL"
    assert snap["collateral_usd"] == 10_000.0 and snap["max_loss_usd"] == 9_760.0


def test_is_credit_defaults_to_debit_for_anything_ambiguous():
    for obj in (object(), MagicMock(side=None), MagicMock(side=""), MagicMock(side="DEBIT"),
                MagicMock(side=123), _debit_idea()):
        assert not is_credit(obj)
    assert is_credit(MagicMock(side="credit")) and is_credit(MagicMock(side="  CREDIT "))


# =============================================================== contract-field validation (spec S1)

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


# =============================================================== structure-aware DTE window (S3)

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
    """A 5-DTE weekly is legitimate for a CSP (it COLLECTS theta) and forbidden for a debit
    (the buyer PAYS it). Same chain, two doctrines."""
    t = _trader(tmp_path, monkeypatch)
    _wire_chain(t, monkeypatch, _chain(5, 40))
    r = await t._resolve_order(_csp_idea(strike=50.0, target_dte=5), per_trade_cap=1e9)
    assert r is not None and r.dte == 5 and r.right == "P" and is_credit(r)


@pytest.mark.asyncio
async def test_a_debit_idea_can_NEVER_reach_the_credit_floor(tmp_path, monkeypatch):
    """The hole the spec names explicitly. Asserted at the CALLER, not on the pure helper: drive
    the real debit resolver at a chain whose only expiries are 5 and 10 DTE -- inside the credit
    window, far below the 25-DTE debit floor -- and it must refuse rather than buy 5-DTE premium."""
    t = _trader(tmp_path, monkeypatch)
    _wire_chain(t, monkeypatch, _chain(5, 10))
    assert t.construction.min_dte >= 25
    assert await t._resolve_order(_debit_idea(), per_trade_cap=1e9) is None
    events = [json.loads(l) for l in (tmp_path / "a.jsonl").read_text().splitlines()]
    assert any(e["event"] == "construction_rejected" for e in events)

    # and the SAME chain is perfectly legal for a cash-secured put -- one chain, two doctrines
    t2 = _trader(tmp_path, monkeypatch)
    _wire_chain(t2, monkeypatch, _chain(5, 10))
    assert (await t2._resolve_order(_csp_idea(strike=50.0, target_dte=5),
                                    per_trade_cap=1e9)).dte == 5


@pytest.mark.asyncio
async def test_credit_ceiling_is_a_refusal_not_an_adjustment(tmp_path, monkeypatch):
    """Nothing inside 3-45 DTE -> refuse. A 200-DTE short put is a different risk than the one
    that was underwritten, so it must not be silently substituted."""
    t = _trader(tmp_path, monkeypatch)
    _wire_chain(t, monkeypatch, _chain(200, 400))
    assert await t._resolve_order(_csp_idea(strike=50.0, target_dte=30), per_trade_cap=1e9) is None


@pytest.mark.asyncio
async def test_credit_resolver_refuses_a_one_sided_quote(tmp_path, monkeypatch):
    t = _trader(tmp_path, monkeypatch)
    _wire_chain(t, monkeypatch, _chain(30), bid=-1.0, ask=1.30)   # IB's "no bid" sentinel
    assert await t._resolve_order(_csp_idea(strike=50.0), per_trade_cap=1e9) is None


@pytest.mark.asyncio
async def test_credit_resolver_hard_rejects_a_put_over_the_per_trade_cap(tmp_path, monkeypatch):
    """One contract already over the risk gate's $ cap is REJECTED, never clamped -- the same
    rule the debit path follows."""
    t = _trader(tmp_path, monkeypatch)
    _wire_chain(t, monkeypatch, _chain(30))
    assert await t._resolve_order(_csp_idea(strike=50.0), per_trade_cap=4_000.0) is None


@pytest.mark.asyncio
async def test_credit_resolver_sizes_to_the_cap_and_prices_off_the_bid(tmp_path, monkeypatch):
    t = _trader(tmp_path, monkeypatch)
    _wire_chain(t, monkeypatch, _chain(30), bid=1.10, ask=1.40)
    idea = _csp_idea(strike=50.0, contracts=3, credit=330.0)      # asks for 3 -> $15,000
    r = await t._resolve_order(idea, per_trade_cap=11_000.0)      # cap allows 2
    assert r.qty == 2
    assert r.collateral_usd == 10_000.0
    assert r.limit == 1.10                                        # the BID, not the mid or ask
    assert r.net_credit_usd == 220.0
    assert r.credit_max_loss_usd == 9_780.0
    assert r.net_delta < 0 and r.net_theta < 0                    # short put: negative net greeks


@pytest.mark.asyncio
async def test_credit_resolver_refuses_a_substituted_contract(tmp_path, monkeypatch):
    """A CSP is defined by its strike. If the broker qualifies a different one, refuse."""
    t = _trader(tmp_path, monkeypatch)
    _wire_chain(t, monkeypatch, _chain(30), strike=45.0)          # asked for 50, got 45
    assert await t._resolve_order(_csp_idea(strike=50.0), per_trade_cap=1e9) is None


# =============================================================== re-approval binding (SELL side)

def test_material_change_on_a_sell_is_measured_on_the_bid_and_the_collateral():
    a = _csp_order(strike=50.0, qty=1, bid=1.20, ask=1.30)
    same = _csp_order(strike=50.0, qty=1, bid=1.20, ask=1.90)     # ask moved; our SELL did not
    assert trader.credit_material_changes(a, same) == ()
    worse = _csp_order(strike=50.0, qty=1, bid=1.00, ask=1.30)    # our credit dropped 17%
    assert any("executable credit changed" in c for c in trader.credit_material_changes(a, worse))
    bigger = _csp_order(strike=50.0, qty=2, bid=1.20, ask=1.30)
    changes = trader.credit_material_changes(a, bigger)
    assert any("quantity changed" in c for c in changes)
    assert any("reserved collateral changed" in c for c in changes)


def test_credit_executable_price_is_the_bid():
    r = _csp_order(bid=1.20, ask=1.90)
    assert trader.credit_executable_price(r) == 1.20
    import exitmgr.entry_safety as es
    assert es.executable_price(r) == 1.90            # the BUY mirror, deliberately different
