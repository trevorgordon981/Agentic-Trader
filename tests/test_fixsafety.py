"""Tests for the 2026-07-03 real-money-path safety wave (Fable audit).

Each block maps to one audited bug fix:
  * trader entry HALT on kill switch / reconcile-unsafe / exit-cycle-failure streak
  * marketable-limit entries + hard-reject sizing (never clamp qty to 1 past the cap)
  * connection.get_open_orders iterates Trade objects; spread combo keyed by the long leg
  * reconcile tolerance: order-without-position + position==journal-remaining consistency
  * scaled_out marked on FILL, not placement
  * update_in_flight_from_fill wired (per-cycle in-flight fill poll)
  * NaN portfolio mark no longer disables stops
  * unfilled-order alarm datetime (aware) no longer TypeErrors
  * close/liquidate tools cancel the exit manager's resting SELL orders
"""
import asyncio
import json
import os
from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock, MagicMock

import exitmgr.trader as trader
from exitmgr.trader import Trader, ResolvedOrder, size_within_cap
from exitmgr.risk import RiskLimits, OpenPosition
from exitmgr.strategist import TradeIdea
from exitmgr.account import PotSnapshot
from exitmgr.connection import IBConnection, PositionData
from exitmgr.state import State, StateManager, InFlightClose, reconcile_state
from exitmgr.config import Config, RulesConfig, TrailingConfig, ScaleOutConfig
from exitmgr.order import OrderResult
from exitmgr.manager import ExitManager

LIM = RiskLimits()
IDEA = TradeIdea("SPY", True, "bullish", "long call", 7, 0.35, 90.0, 4, "trend")


# ============================================================ size_within_cap (pure, hard-reject)
def test_size_within_cap_rejects_when_one_contract_over_cap():
    # one contract = $20,000 > $15,000 cap -> HARD REJECT (None), NOT a clamp-to-1 over the cap
    assert size_within_cap(200 * 100, 100_000, 15_000) is None
    assert size_within_cap(100, 10_000, 500) == 5        # 500 // 100
    assert size_within_cap(120, 10_000, 500) == 4        # 500 // 120 = 4 (480 <= 500)
    assert size_within_cap(0, 1, 1) is None              # zero/neg unit cost -> reject
    assert size_within_cap(300, 250, 10_000) is None     # budget < one contract -> reject


# ============================================================ connection: Trade iteration + BAG key
def test_order_key_con_id_bag_uses_long_leg():
    bag = SimpleNamespace(secType="BAG", conId=0, comboLegs=[
        SimpleNamespace(action="SELL", conId=222),
        SimpleNamespace(action="BUY", conId=111)])
    assert IBConnection._order_key_con_id(bag) == 111   # the BUY (long) leg, NOT the BAG's conId
    single = SimpleNamespace(secType="OPT", conId=999)
    assert IBConnection._order_key_con_id(single) == 999


def test_get_open_orders_iterates_trade_objects():
    conn = IBConnection(host="h", port=1, client_id=2)
    conn._connected = True
    conn.ib = MagicMock()
    sell = SimpleNamespace(
        order=SimpleNamespace(action="SELL", orderId=55, totalQuantity=2, lmtPrice=1.2),
        contract=SimpleNamespace(secType="OPT", conId=111),
        orderStatus=SimpleNamespace(remaining=2, filled=0))
    buy = SimpleNamespace(  # an entry BUY must be excluded from the SELL-close view
        order=SimpleNamespace(action="BUY", orderId=66, totalQuantity=1, lmtPrice=3.0),
        contract=SimpleNamespace(secType="OPT", conId=222),
        orderStatus=SimpleNamespace(remaining=1, filled=0))
    conn.ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[sell, buy])
    out = asyncio.run(conn.get_open_orders())
    assert set(out) == {111}                    # only the SELL, keyed by its conId
    assert out[111].order_id == 55 and out[111].remaining == 2


def test_get_open_orders_spread_combo_keyed_by_long_leg():
    conn = IBConnection(host="h", port=1, client_id=2)
    conn._connected = True
    conn.ib = MagicMock()
    combo = SimpleNamespace(
        order=SimpleNamespace(action="SELL", orderId=77, totalQuantity=1, lmtPrice=1.1),
        contract=SimpleNamespace(secType="BAG", conId=0, comboLegs=[
            SimpleNamespace(action="BUY", conId=111), SimpleNamespace(action="SELL", conId=222)]),
        orderStatus=SimpleNamespace(remaining=1, filled=0))
    conn.ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[combo])
    out = asyncio.run(conn.get_open_orders())
    assert set(out) == {111}                    # keyed by the long leg, not the BAG (0)


# ============================================================ reconcile tolerance
def test_reconcile_order_without_position_not_fatal():
    state = State()
    safe, alerts = reconcile_state(state, {}, {999: {"order_id": 5, "remaining": 1}}, {})
    assert safe is True
    assert any("order-without-position" in a.lower() for a in alerts)


def test_reconcile_position_with_untracked_order_still_aborts():
    # regression guard: an untracked order sitting on a live position we HOLD is still fatal
    state = State()
    safe, alerts = reconcile_state(
        state, {123: {"qty": 1, "avg_cost": 5.0}},
        {123: {"order_id": 5, "remaining": 1}}, {123: {"debit": 500.0}})
    assert safe is False


def test_reconcile_pos_consistent_with_journal_qty_not_fatal():
    # in_flight thinks 1 remains, live order shows 2 remaining -> normally aborts; but with
    # journal qty and a consistent position (held == order remaining) it is a legit close-in-progress
    def _fresh():
        st = State()
        st.add_in_flight(InFlightClose(con_id=123, order_id=100, remaining_qty=1, entry_debit=500.0))
        return st
    lp = {123: {"qty": 2, "avg_cost": 5.0}}
    lo = {123: {"order_id": 100, "remaining": 2}}
    je = {123: {"debit": 500.0}}
    safe0, _ = reconcile_state(_fresh(), lp, lo, je)                       # no journal_qtys -> abort
    assert safe0 is False
    safe1, alerts = reconcile_state(_fresh(), lp, lo, je, journal_qtys={123: 5})
    assert safe1 is True
    assert any("consistent" in a.lower() for a in alerts)


# ============================================================ trader: helpers + entry halts
def _trader(tmp_path, *, kill_switch_path=None, exit_mgr=None):
    ibc = MagicMock()
    ibc.ib = MagicMock()
    ibc.get_positions = AsyncMock(return_value={})
    em = exit_mgr if exit_mgr is not None else MagicMock()
    if exit_mgr is None:
        em.run_cycle = AsyncMock()
    t = Trader(ib_conn=ibc, exit_manager=em, limits=LIM, approved_names=set(),
               endpoint="http://x", model="m", slack_token="tok", slack_channel="C1",
               approver_ids={"OWNER"}, baseline_path=str(tmp_path / "b.json"),
               audit_path=str(tmp_path / "a.jsonl"), approve_timeout_s=60,
               kill_switch_path=kill_switch_path)
    t._resolve_order = AsyncMock(return_value=ResolvedOrder("SPY", "C", "20260620", 50.0, 1, 1.20, object()))
    t._submit_order = AsyncMock(return_value=("Filled", []))
    return t


def _wire_trader_env(monkeypatch):
    monkeypatch.setattr(trader.research, "gather", AsyncMock(return_value={}))
    monkeypatch.setattr(trader, "_market_open", lambda: True)
    monkeypatch.setattr(trader, "get_pot_snapshot", AsyncMock(return_value=PotSnapshot(1010.0, 9000.0, 1010.0)))
    monkeypatch.setattr(trader, "propose", lambda *a, **k: [IDEA])
    monkeypatch.setattr(trader.approval, "post_proposal", lambda *a, **k: "ts1")
    monkeypatch.setattr(trader.approval, "await_approval", lambda *a, **k: "approve")
    monkeypatch.setattr(trader.research, "days_to_earnings", lambda *a, **k: None)


@pytest.mark.asyncio
async def test_kill_switch_halts_entries(tmp_path, monkeypatch):
    _wire_trader_env(monkeypatch)
    ks = tmp_path / "KILL_SWITCH"
    ks.write_text("STOP")
    t = _trader(tmp_path, kill_switch_path=str(ks))
    await t.run_once(dry_run=False)
    t._submit_order.assert_not_called()          # kill switch present -> no entry submitted


@pytest.mark.asyncio
async def test_no_kill_switch_allows_entry(tmp_path, monkeypatch):
    _wire_trader_env(monkeypatch)
    t = _trader(tmp_path, kill_switch_path=str(tmp_path / "absent"))
    await t.run_once(dry_run=False)
    t._submit_order.assert_awaited_once()        # control: absent file -> entry proceeds


@pytest.mark.asyncio
async def test_reconcile_unsafe_halts_entries(tmp_path, monkeypatch):
    _wire_trader_env(monkeypatch)
    em = MagicMock()
    em.run_cycle = AsyncMock()
    em._reconcile_ok = False                      # manager reported an inconsistent book
    t = _trader(tmp_path, exit_mgr=em)
    await t.run_once(dry_run=False)
    t._submit_order.assert_not_called()


@pytest.mark.asyncio
async def test_exit_cycle_failure_streak_suppresses_entries(tmp_path, monkeypatch):
    _wire_trader_env(monkeypatch)
    em = MagicMock()
    em.run_cycle = AsyncMock(side_effect=RuntimeError("exit boom"))
    em._reconcile_ok = True
    t = _trader(tmp_path, exit_mgr=em)
    await t.run_once(dry_run=False)               # streak 1 -> entry still allowed
    await t.run_once(dry_run=False)               # streak 2 -> allowed
    await t.run_once(dry_run=False)               # streak 3 -> entries suppressed
    assert t._submit_order.await_count == 2


@pytest.mark.asyncio
async def test_entry_submits_marketable_limit(tmp_path):
    # _submit_order builds a marketable LIMIT (not a raw MKT). Use a real Trader + mocked ib.
    ibc = MagicMock(); ibc.ib = MagicMock()
    ibc.ib.placeOrder.return_value.orderStatus.status = "Filled"   # short-circuit the ACK/fill polls
    t = Trader(ib_conn=ibc, exit_manager=MagicMock(), limits=RiskLimits(), approved_names=set(),
               endpoint="http://x", model="m", slack_token="t", slack_channel="C",
               approver_ids=set(), baseline_path=str(tmp_path / "b.json"),
               audit_path=str(tmp_path / "a.jsonl"), journal_path=str(tmp_path / "trades.log"),
               entry_limit_buffer_pct=0.05)
    c = MagicMock(); c.conId = 111
    r = ResolvedOrder("SPY", "C", "20260620", 610.0, 1, 2.00, c)
    await t._submit_order(r)
    placed_order = ibc.ib.placeOrder.call_args[0][1]
    assert placed_order.orderType == "LMT"
    assert placed_order.lmtPrice == pytest.approx(2.10)   # 2.00 * (1 + 0.05)


# ============================================================ manager helpers
CON = 1500
JOURNAL = {"ts": "2026-07-01T14:00:00+00:00", "contract_id": CON, "symbol": "AAPL",
           "right": "C", "strike": 200.0, "expiry": "20261231", "quantity": 4, "debit": 2000.0,
           "conviction": 6}


def _mgr(tmp_path, journal=JOURNAL, *, scale_out=False):
    cfg = Config()
    cfg.dry_run = False
    cfg.loop_mode = False
    cfg.journal.path = str(tmp_path / "trades.log")
    cfg.state.path = str(tmp_path / "state.json")
    cfg.kill_switch.path = str(tmp_path / "KILL")
    cfg.audit_path = str(tmp_path / "audit.jsonl")
    cfg.manage_positions = False
    cfg.alerts_channel = ""
    cfg.error_channel = ""
    cfg.rules = RulesConfig(profit_target_pct=30.0, stop_pct=30.0, time_stop_days=10,
                            trailing=TrailingConfig(enabled=False),
                            scale_out=ScaleOutConfig(enabled=scale_out, first_target_pct=20.0,
                                                     trim_fraction=0.5))
    (tmp_path / "trades.log").write_text(json.dumps(journal) + "\n")
    return ExitManager(cfg), cfg


def _wire_mgr(mgr, *, qty, price, portfolio=None, place_trade=None):
    pos = {CON: PositionData(con_id=CON, symbol="AAPL", right="C", quantity=qty,
                             avg_cost=5.00, expiry="20261231")}
    mgr.ib_conn.get_positions = AsyncMock(return_value=pos)
    mgr.ib_conn.get_open_orders = AsyncMock(return_value={})
    mgr.ib_conn.fetch_quotes = AsyncMock(return_value={CON: {"price": price}})
    mgr.ib_conn.ib = MagicMock()
    mgr.ib_conn.ib.portfolio = (lambda: portfolio) if portfolio is not None else (lambda: [])
    mgr._spot_price = AsyncMock(return_value=None)
    place = AsyncMock(return_value=OrderResult(success=True, order_id=555, con_id=CON, trade=place_trade))
    mgr.order_manager.place_close_order = place
    return place


def _fill_trade(status):
    tr = MagicMock()
    tr.orderStatus.status = status
    tr.orderStatus.avgFillPrice = 6.0
    return tr


# ============================================================ scaled_out on FILL (not placement)
@pytest.mark.asyncio
async def test_scaled_out_set_on_fill(tmp_path):
    mgr, cfg = _mgr(tmp_path, scale_out=True)
    _wire_mgr(mgr, qty=4, price=6.00, place_trade=_fill_trade("Filled"))
    await mgr.run_cycle(dry_run=False)
    assert mgr.state_manager.state.scaled_out.get(str(CON)) is True


@pytest.mark.asyncio
async def test_scaled_out_NOT_set_when_trim_not_filled(tmp_path):
    mgr, cfg = _mgr(tmp_path, scale_out=True)
    _wire_mgr(mgr, qty=4, price=6.00, place_trade=_fill_trade("Submitted"))
    await mgr.run_cycle(dry_run=False)
    assert str(CON) not in mgr.state_manager.state.scaled_out   # resting, not filled -> deferred


# ============================================================ NaN portfolio mark must not disable stops
@pytest.mark.asyncio
async def test_nan_portfolio_mark_falls_back_to_quote_and_stop_fires(tmp_path):
    mgr, cfg = _mgr(tmp_path)
    nan_item = SimpleNamespace(contract=SimpleNamespace(conId=CON), position=4,
                               marketPrice=float("nan"))
    # -30% on the 5.00 basis via the streaming quote; the NaN server mark must be IGNORED so the
    # stop still fires (pre-fix the NaN mark became current_price and every comparison was False).
    place = _wire_mgr(mgr, qty=4, price=3.50, portfolio=[nan_item])
    await mgr.run_cycle(dry_run=False)
    place.assert_called_once()
    assert place.call_args.kwargs["quantity"] == 4


# ============================================================ update_in_flight_from_fill wiring
def test_poll_in_flight_partial_fill_decrements(tmp_path):
    mgr, cfg = _mgr(tmp_path)
    mgr.state_manager.state.add_in_flight(
        InFlightClose(con_id=111, order_id=5, remaining_qty=3, entry_debit=300.0))
    asyncio.run(mgr._poll_in_flight_fills({111: {"order_id": 5, "remaining": 1}}))
    assert mgr.state_manager.state.get_in_flight(111).remaining_qty == 1   # 3 -> 1 (2 filled)


def test_poll_in_flight_full_fill_clears_stale_record(tmp_path):
    mgr, cfg = _mgr(tmp_path)
    mgr.state_manager.state.add_in_flight(
        InFlightClose(con_id=111, order_id=5, remaining_qty=2, entry_debit=300.0))
    mgr.ib_conn.ib = MagicMock()
    mgr.ib_conn.ib.trades = lambda: [SimpleNamespace(
        order=SimpleNamespace(orderId=5), orderStatus=SimpleNamespace(status="Filled"))]
    asyncio.run(mgr._poll_in_flight_fills({}))   # order gone from live book, Trade shows Filled
    assert mgr.state_manager.state.get_in_flight(111) is None


def test_poll_in_flight_gone_but_cancelled_is_not_a_fill(tmp_path):
    mgr, cfg = _mgr(tmp_path)
    mgr.state_manager.state.add_in_flight(
        InFlightClose(con_id=111, order_id=5, remaining_qty=2, entry_debit=300.0))
    mgr.ib_conn.ib = MagicMock()
    mgr.ib_conn.ib.trades = lambda: [SimpleNamespace(
        order=SimpleNamespace(orderId=5), orderStatus=SimpleNamespace(status="Cancelled"))]
    asyncio.run(mgr._poll_in_flight_fills({}))
    # a Cancelled order is NOT a fill -> record left for reconcile to handle, not silently cleared
    assert mgr.state_manager.state.get_in_flight(111) is not None


# ============================================================ unfilled-order alarm datetime (aware)
@pytest.mark.asyncio
async def test_unfilled_alarm_handles_aware_placed_at(tmp_path, monkeypatch):
    from datetime import datetime, timezone, timedelta
    mgr, cfg = _mgr(tmp_path)
    cfg.error_channel = "C_ERR"
    monkeypatch.setenv("SLACK_BOT_TOKEN", "tok")
    monkeypatch.setattr(trader, "_market_open", lambda: True)
    posts = []
    monkeypatch.setattr("exitmgr.approval.post_proposal", lambda tok, ch, txt: posts.append(txt) or "ts")
    mgr.ib_conn.ib = MagicMock()
    mgr.ib_conn.ib.reqOpenOrdersAsync = AsyncMock(return_value=[])
    # aware UTC placed_at, 20 min old (> 15-min default) -> must alarm, not TypeError
    placed = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
    mgr.state_manager.state.add_in_flight(
        InFlightClose(con_id=CON, order_id=9, remaining_qty=1, entry_debit=500.0, placed_at=placed))
    await mgr._alert_unfilled_orders()
    assert posts and any("EXIT" in p and "unfilled" in p for p in posts)


# ============================================================ close/liquidate cancel resting SELLs
def test_cancel_resting_closes_matches_single_and_combo():
    import close_symbol
    cancelled = []
    ib = MagicMock()
    single = SimpleNamespace(order=SimpleNamespace(orderId=1, action="SELL"),
                             contract=SimpleNamespace(conId=111, comboLegs=None))
    combo = SimpleNamespace(order=SimpleNamespace(orderId=2, action="SELL"),
                            contract=SimpleNamespace(conId=0, comboLegs=[
                                SimpleNamespace(conId=111), SimpleNamespace(conId=222)]))
    unrelated = SimpleNamespace(order=SimpleNamespace(orderId=3, action="SELL"),
                                contract=SimpleNamespace(conId=999, comboLegs=None))
    ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[single, combo, unrelated])
    ib.cancelOrder = lambda o: cancelled.append(o.orderId)
    n = asyncio.run(close_symbol.cancel_resting_closes(ib, {111}))
    assert n == 2 and set(cancelled) == {1, 2}     # the single-leg + the combo whose long leg is 111


def test_liquidate_cancel_resting_closes_importable():
    import liquidate
    ib = MagicMock()
    ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[])
    assert asyncio.run(liquidate.cancel_resting_closes(ib, {1})) == 0
