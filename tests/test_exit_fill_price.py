"""Fill-price reprice tests for exitmgr/order.py (P2.8, 2026-07-02).

Fable review: the prior P2.6 pricing (marketable LIMIT at mark*(1-5%)) can rest ABOVE the bid on a
genuinely WIDE option book -> a triggered STOP never fills -> the position is unprotected exactly
when it matters. These tests pin the reprice:

  * bid available  -> SELL LIMIT *at the bid* (marketable on a narrow OR wide book), FLOORED at
                      mark*(1-EXIT_SLIPPAGE_FLOOR) so a broken/stub bid can't dump for pennies.
  * no bid, STOP/unknown -> true MARKET (guaranteed fill; protecting the position > a few cents).
  * no bid, profit-TARGET -> passive LIMIT at the mark (no urgency to cross).
  * a spread (combo) is NEVER bid-anchored (a per-leg bid is not the net combo price).
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

from exitmgr.order import OrderManager
from exitmgr.state import StateManager


def _om(tmp_path):
    ib_conn = MagicMock()
    trade = MagicMock()
    trade.order.orderId = 77  # non-str orderStatus.status -> reject-poll breaks immediately
    ib_conn.place_order = AsyncMock(return_value=trade)
    sm = StateManager(str(tmp_path / "state.json"))
    return OrderManager(ib_conn, sm), ib_conn


def _limit_px(ib_conn):
    action, qty, px = ib_conn.create_limit_order.call_args[0]
    assert action == "SELL"
    return px


# ---------------- bid-anchored: fills on a WIDE book (the core Fable fix)

def test_wide_book_prices_at_bid_not_above_it(tmp_path):
    # WIDE book: mark 2.00 but bid only 1.20 (40% below mark). The OLD mark*(1-5%)=1.90 limit
    # would rest ABOVE the 1.20 bid and never fill. The reprice sits AT the bid -> fills.
    om, ib_conn = _om(tmp_path)
    res = asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=1, limit_price=2.00, entry_debit=200.0,
        live_open_orders={}, market=True, right="C", bid=1.20, trigger_type="stop"))
    assert res.success
    ib_conn.create_market_order.assert_not_called()
    px = _limit_px(ib_conn)
    assert px == 1.20                                   # AT the bid -> marketable, fills
    assert px < round(2.00 * (1 - om.MARKETABLE_BUFFER), 2)  # proves old logic (1.90) sat above the bid


def test_narrow_book_gets_good_price_near_mark(tmp_path):
    # NARROW book: bid 1.95 vs mark 2.00 -> limit at 1.95, barely any slippage.
    om, ib_conn = _om(tmp_path)
    res = asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=3, limit_price=2.00, entry_debit=600.0,
        live_open_orders={}, market=True, right="C", bid=1.95, trigger_type="stop"))
    assert res.success
    assert _limit_px(ib_conn) == 1.95
    ib_conn.create_market_order.assert_not_called()


def test_sanity_floor_blocks_absurd_bid(tmp_path):
    # BROKEN/stub bid 0.02 against a 2.00 mark: the floor (mark*(1-0.50)=1.00) engages so we do
    # NOT dump the position for pennies. The limit rests above the junk bid (won't fill) -- the
    # intended refusal; the manager's unfilled-order alarm escalates a resting exit.
    om, ib_conn = _om(tmp_path)
    res = asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=1, limit_price=2.00, entry_debit=200.0,
        live_open_orders={}, market=True, right="C", bid=0.02, trigger_type="stop"))
    assert res.success
    px = _limit_px(ib_conn)
    assert px == round(2.00 * (1 - om.EXIT_SLIPPAGE_FLOOR), 2)  # 1.00 floor
    assert px > 0.02                                            # refuses to dump at the junk bid


def test_nan_bid_falls_back_to_market(tmp_path):
    om, ib_conn = _om(tmp_path)
    res = asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=1, limit_price=2.00, entry_debit=200.0,
        live_open_orders={}, market=True, right="C", bid=float("nan"), trigger_type="stop"))
    assert res.success
    ib_conn.create_market_order.assert_called_once_with("SELL", 1)  # NaN bid -> guarantee fill


# ---------------- profit-target stays passive (no urgency)

def test_profit_target_without_bid_stays_passive_limit(tmp_path):
    om, ib_conn = _om(tmp_path)
    res = asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=2, limit_price=2.50, entry_debit=300.0,
        live_open_orders={}, market=True, right="C", trigger_type="profit_target"))
    assert res.success
    ib_conn.create_market_order.assert_not_called()
    ib_conn.create_limit_order.assert_called_once_with("SELL", 2, 2.50)  # passive, at the mark


def test_scale_out_target_stays_passive_limit(tmp_path):
    om, ib_conn = _om(tmp_path)
    res = asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=1, limit_price=3.00, entry_debit=300.0,
        live_open_orders={}, market=True, right="C", trigger_type="scale_out"))
    assert res.success
    ib_conn.create_limit_order.assert_called_once_with("SELL", 1, 3.00)
    ib_conn.create_market_order.assert_not_called()


# ---------------- stop with a known trigger, no bid -> MARKET

def test_named_stop_without_bid_uses_market(tmp_path):
    for i, tt in enumerate(("stop", "time_stop", "trailing_stop")):
        om2, ib2 = _om(tmp_path)
        res = asyncio.run(om2.place_close_order(
            con_id=500 + i, symbol="SPY", quantity=1, limit_price=2.00, entry_debit=200.0,
            live_open_orders={}, market=True, right="C", trigger_type=tt))
        assert res.success, tt
        ib2.create_market_order.assert_called_once_with("SELL", 1)
        ib2.create_limit_order.assert_not_called()


# ---------------- a spread is NEVER bid-anchored (per-leg bid != net combo price)

def test_spread_ignores_single_leg_bid(tmp_path):
    # even if a caller passes a single-leg bid, a combo close must not anchor to it.
    om, ib_conn = _om(tmp_path)
    res = asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=1, limit_price=2.00, entry_debit=200.0,
        live_open_orders={}, market=True, right="C",
        spread={"short_con_id": 999}, bid=1.50, trigger_type="stop"))
    assert res.success
    ib_conn.create_combo_contract.assert_called_once()      # combo path taken
    ib_conn.create_market_order.assert_called_once_with("SELL", 1)  # bid ignored -> guaranteed fill
    ib_conn.create_limit_order.assert_not_called()


# ---------------- backward compatibility: unchanged passive default

def test_passive_default_still_limit_at_mark(tmp_path):
    om, ib_conn = _om(tmp_path)
    res = asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=1, limit_price=2.50, entry_debit=250.0,
        live_open_orders={}, right="C"))  # market defaults False; no bid/trigger
    assert res.success
    ib_conn.create_limit_order.assert_called_once_with("SELL", 1, 2.50)
    ib_conn.create_market_order.assert_not_called()
