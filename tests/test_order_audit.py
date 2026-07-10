"""Audit-fix tests for exitmgr/order.py (2026-07-02).

Covers:
  P2.4 - single-leg close derives the option `right` (long PUT closes as a PUT, not a hardcoded C)
  P2.8 - triggered (market=True) exits are priced to GUARANTEE the fill (Fable reprice):
         bid-anchored marketable LIMIT when a bid is known (see test_exit_fill_price.py),
         else true MARKET for a stop/unknown trigger, passive LIMIT for a profit-target
  P2.7 - a REJECTED placed_trade must NOT leave a blocking in-flight record
"""
import asyncio
import json

from unittest.mock import AsyncMock, MagicMock

from exitmgr.order import OrderManager
from exitmgr.state import InFlightClose, State, StateManager, reconcile_state


def _order_manager(tmp_path, trade=None):
    ib_conn = MagicMock()
    if trade is None:
        trade = MagicMock()
        trade.order.orderId = 77
        # non-str status -> the reject poll breaks immediately (treated as live/accepted)
    ib_conn.place_order = AsyncMock(return_value=trade)
    sm = StateManager(str(tmp_path / "state.json"))
    return OrderManager(ib_conn, sm), ib_conn, sm


def _portfolio_pos(con_id, right):
    p = MagicMock()
    p.contract.conId = con_id
    p.contract.right = right
    p.position = 1
    return p


# ---------------- P2.4: option right on single-leg close

def test_put_close_builds_put_contract_when_right_passed(tmp_path):
    om, ib_conn, _ = _order_manager(tmp_path)
    res = asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=1, limit_price=2.50, entry_debit=120.0,
        live_open_orders={}, right="P"))
    assert res.success
    ib_conn.create_contract.assert_called_once_with(111, symbol="SPY", right="P")


def test_close_right_derived_from_portfolio_when_not_passed(tmp_path):
    om, ib_conn, _ = _order_manager(tmp_path)
    ib_conn.ib.portfolio = MagicMock(return_value=[_portfolio_pos(111, "P")])
    res = asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=1, limit_price=2.50, entry_debit=120.0,
        live_open_orders={}))  # right=None -> resolve from portfolio
    assert res.success
    ib_conn.create_contract.assert_called_once_with(111, symbol="SPY", right="P")


def test_close_right_defaults_to_C_when_unresolvable(tmp_path):
    om, ib_conn, _ = _order_manager(tmp_path)
    ib_conn.ib.portfolio = MagicMock(return_value=[])  # nothing to resolve from
    res = asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=1, limit_price=2.50, entry_debit=120.0,
        live_open_orders={}))
    assert res.success
    ib_conn.create_contract.assert_called_once_with(111, symbol="SPY", right="C")


# ---------------- P2.6: marketable-limit exits with a floor

def test_triggered_stop_without_bid_uses_market(tmp_path):
    # P2.8 (Fable reprice): a triggered exit with NO bid and no known target trigger must GUARANTEE
    # the fill -> true MARKET. A mark-anchored limit (the old mark*(1-5%)) could rest ABOVE the bid
    # on a wide book and never protect the position -- a stop that doesn't fill is worse than slippage.
    om, ib_conn, _ = _order_manager(tmp_path)
    res = asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=2, limit_price=2.00, entry_debit=400.0,
        live_open_orders={}, market=True, right="C"))
    assert res.success
    ib_conn.create_market_order.assert_called_once_with("SELL", 2)
    ib_conn.create_limit_order.assert_not_called()


def test_triggered_stop_tiny_mark_no_bid_uses_market(tmp_path):
    # P2.8: no bid, tiny mark, unknown trigger -> still guarantee the fill via MARKET.
    om, ib_conn, _ = _order_manager(tmp_path)
    res = asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=1, limit_price=0.005, entry_debit=1.0,
        live_open_orders={}, market=True, right="C"))
    assert res.success
    ib_conn.create_market_order.assert_called_once_with("SELL", 1)
    ib_conn.create_limit_order.assert_not_called()


def test_manual_exit_no_mark_falls_back_to_market(tmp_path):
    om, ib_conn, _ = _order_manager(tmp_path)
    res = asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=1, limit_price=0.0, entry_debit=100.0,
        live_open_orders={}, market=True, right="C"))  # 0.0 mark -> keep always-fills MARKET
    assert res.success
    ib_conn.create_market_order.assert_called_once_with("SELL", 1)
    ib_conn.create_limit_order.assert_not_called()


def test_default_close_is_plain_limit_at_mark(tmp_path):
    om, ib_conn, _ = _order_manager(tmp_path)
    res = asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=1, limit_price=2.50, entry_debit=120.0,
        live_open_orders={}, market=False, right="C"))
    assert res.success
    ib_conn.create_limit_order.assert_called_once_with("SELL", 1, 2.50)
    ib_conn.create_market_order.assert_not_called()


# ---------------- P2.7: rejected order must not block future closes

def _rejecting_trade(status="Cancelled"):
    trade = MagicMock()
    trade.order.orderId = 77
    trade.orderStatus.status = status          # real str -> reject poll sees it
    le = MagicMock(); le.errorCode = 201; le.message = "rejected: margin"
    trade.log = [le]
    return trade


def test_rejected_order_records_no_blocking_in_flight(tmp_path):
    trade = _rejecting_trade("Cancelled")
    om, ib_conn, sm = _order_manager(tmp_path, trade=trade)
    res = asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=1, limit_price=2.50, entry_debit=120.0,
        live_open_orders={}, right="C"))
    assert not res.success
    assert "Cancelled" in res.message
    # the whole point: no in-flight left behind to jam the next cycle's retry
    assert sm.state.get_in_flight(111) is None
    can_place, _ = asyncio.run(om.can_place_close(111, 1, {}))
    assert can_place


def test_inactive_status_also_treated_as_rejection(tmp_path):
    trade = _rejecting_trade("Inactive")
    om, _, sm = _order_manager(tmp_path, trade=trade)
    res = asyncio.run(om.place_close_order(
        con_id=222, symbol="QQQ", quantity=1, limit_price=1.20, entry_debit=120.0,
        live_open_orders={}, right="C"))
    assert not res.success
    assert sm.state.get_in_flight(222) is None


def test_accepted_order_still_records_in_flight(tmp_path):
    # a live ACK ("Submitted") must NOT be mistaken for a rejection
    trade = MagicMock()
    trade.order.orderId = 88
    trade.orderStatus.status = "Submitted"
    trade.log = []
    om, _, sm = _order_manager(tmp_path, trade=trade)
    res = asyncio.run(om.place_close_order(
        con_id=333, symbol="IWM", quantity=1, limit_price=3.00, entry_debit=300.0,
        live_open_orders={}, right="C"))
    assert res.success
    assert sm.state.get_in_flight(333) is not None


def test_immediate_partial_cancel_retains_context_for_finalization(tmp_path):
    trade = _rejecting_trade("Cancelled")
    trade.orderStatus.filled = 1
    trade.orderStatus.avgFillPrice = 2.25
    om, _, sm = _order_manager(tmp_path, trade=trade)
    res = asyncio.run(om.place_close_order(
        con_id=444, symbol="SPY", quantity=2, limit_price=2.50, entry_debit=500.0,
        live_open_orders={}, right="C", exit_context={"close_qty": 2, "position_qty": 2}))
    assert res.success
    assert res.trade is trade
    inf = sm.state.get_in_flight(444)
    assert inf is not None and inf.exit_context["close_qty"] == 2


def test_placement_intent_is_durable_before_broker_call(tmp_path):
    om, ib_conn, sm = _order_manager(tmp_path)
    ib_conn.reserve_order_id.return_value = 901
    seen = {}

    async def place_after_inspection(contract, order):
        data = json.loads((tmp_path / "state.json").read_text())
        seen.update(data["in_flight"]["555"])
        trade = MagicMock()
        trade.order.orderId = 901
        trade.order.clientId = 42
        trade.order.permId = 1234
        trade.order.orderRef = order.orderRef
        trade.orderStatus.status = "Submitted"
        return trade

    ib_conn.client_id = 42
    ib_conn.place_order = AsyncMock(side_effect=place_after_inspection)
    res = asyncio.run(om.place_close_order(
        con_id=555, symbol="SPY", quantity=1, limit_price=2.50, entry_debit=250.0,
        live_open_orders={}, right="C", exit_context={"when": "restart"}))
    assert res.success
    assert seen["placement_state"] == "intent"
    assert seen["order_id"] == 901 and seen["client_id"] == 42
    assert seen["order_ref"].startswith("exitmgr-555-")


def test_failed_intent_save_never_calls_broker(tmp_path):
    om, ib_conn, sm = _order_manager(tmp_path)
    sm.save = MagicMock(side_effect=OSError("disk full"))
    res = asyncio.run(om.place_close_order(
        con_id=666, symbol="SPY", quantity=1, limit_price=2.50, entry_debit=250.0,
        live_open_orders={}, right="C", exit_context={"x": 1}))
    assert not res.success
    ib_conn.place_order.assert_not_awaited()


def test_prepared_intent_binds_matching_live_order_identity():
    state = State()
    state.add_in_flight(InFlightClose(
        con_id=777, order_id=901, remaining_qty=2, entry_debit=500.0,
        exit_context={"close_qty": 2}, client_id=42, order_ref="stable-ref",
        placement_state="intent"))
    safe, _ = reconcile_state(
        state,
        live_positions={777: {"qty": 2, "avg_cost": 2.5}},
        live_open_orders={777: {"order_id": 901, "remaining": 2, "client_id": 42,
                                "perm_id": 1234, "order_ref": "stable-ref"}},
        journal_entries={777: {"debit": 500.0}},
        journal_qtys={777: 2},
    )
    inf = state.get_in_flight(777)
    assert safe and inf.placement_state == "submitted"
    assert inf.perm_id == 1234
