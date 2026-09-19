"""Public API contract; production-derived narrative omitted."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

from exitmgr.order import OrderManager
from exitmgr.state import StateManager


def _om(tmp_path):
    ib_conn = MagicMock()
    trade = MagicMock()
    trade.order.orderId = 77
    trade.orderStatus.status = "Submitted"
    ib_conn.place_order = AsyncMock(return_value=trade)
    sm = StateManager(str(tmp_path / "state.json"))
    return OrderManager(ib_conn, sm), ib_conn


def _limit_px(ib_conn):
    action, qty, px = ib_conn.create_limit_order.call_args[0]
    assert action == "SELL"
    return px




def test_wide_book_prices_at_bid_not_above_it(tmp_path):


    om, ib_conn = _om(tmp_path)
    res = asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=1, limit_price=2.00, entry_debit=200.0,
        live_open_orders={}, market=True, right="C", bid=1.20, trigger_type="stop"))
    assert res.success
    ib_conn.create_market_order.assert_not_called()
    px = _limit_px(ib_conn)
    assert px == 1.20
    assert px < round(2.00 * (1 - om.MARKETABLE_BUFFER), 2)


def test_narrow_book_gets_good_price_near_mark(tmp_path):

    om, ib_conn = _om(tmp_path)
    res = asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=3, limit_price=2.00, entry_debit=600.0,
        live_open_orders={}, market=True, right="C", bid=1.95, trigger_type="stop"))
    assert res.success
    assert _limit_px(ib_conn) == 1.95
    ib_conn.create_market_order.assert_not_called()


def test_sanity_floor_blocks_absurd_bid(tmp_path):



    om, ib_conn = _om(tmp_path)
    res = asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=1, limit_price=2.00, entry_debit=200.0,
        live_open_orders={}, market=True, right="C", bid=0.02, trigger_type="stop"))
    assert res.success
    px = _limit_px(ib_conn)
    assert px == round(2.00 * (1 - om.EXIT_SLIPPAGE_FLOOR), 2)
    assert px > 0.02


def test_nan_bid_falls_back_to_market(tmp_path):
    om, ib_conn = _om(tmp_path)
    res = asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=1, limit_price=2.00, entry_debit=200.0,
        live_open_orders={}, market=True, right="C", bid=float("nan"), trigger_type="stop"))
    assert res.success
    ib_conn.create_market_order.assert_called_once_with("SELL", 1)




def test_profit_target_without_bid_stays_passive_limit(tmp_path):
    om, ib_conn = _om(tmp_path)
    res = asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=2, limit_price=2.50, entry_debit=300.0,
        live_open_orders={}, market=True, right="C", trigger_type="profit_target"))
    assert res.success
    ib_conn.create_market_order.assert_not_called()
    ib_conn.create_limit_order.assert_called_once_with("SELL", 2, 2.50)


def test_scale_out_target_stays_passive_limit(tmp_path):
    om, ib_conn = _om(tmp_path)
    res = asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=1, limit_price=3.00, entry_debit=300.0,
        live_open_orders={}, market=True, right="C", trigger_type="scale_out"))
    assert res.success
    ib_conn.create_limit_order.assert_called_once_with("SELL", 1, 3.00)
    ib_conn.create_market_order.assert_not_called()




def test_named_stop_without_bid_uses_market(tmp_path):
    for i, tt in enumerate(("stop", "time_stop", "trailing_stop")):
        om2, ib2 = _om(tmp_path)
        res = asyncio.run(om2.place_close_order(
            con_id=500 + i, symbol="SPY", quantity=1, limit_price=2.00, entry_debit=200.0,
            live_open_orders={}, market=True, right="C", trigger_type=tt))
        assert res.success, tt
        ib2.create_market_order.assert_called_once_with("SELL", 1)
        ib2.create_limit_order.assert_not_called()




def test_spread_ignores_single_leg_bid(tmp_path):

    om, ib_conn = _om(tmp_path)
    res = asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=1, limit_price=2.00, entry_debit=200.0,
        live_open_orders={}, market=True, right="C",
        spread={"short_con_id": 999}, bid=1.50, trigger_type="stop"))
    assert res.success
    ib_conn.create_combo_contract.assert_called_once()
    ib_conn.create_market_order.assert_called_once_with("SELL", 1)
    ib_conn.create_limit_order.assert_not_called()




def test_passive_default_still_limit_at_mark(tmp_path):
    om, ib_conn = _om(tmp_path)
    res = asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=1, limit_price=2.50, entry_debit=250.0,
        live_open_orders={}, right="C"))
    assert res.success
    ib_conn.create_limit_order.assert_called_once_with("SELL", 1, 2.50)
    ib_conn.create_market_order.assert_not_called()
