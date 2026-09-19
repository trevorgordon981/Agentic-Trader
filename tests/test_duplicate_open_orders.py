"""Public API contract; production-derived narrative omitted."""
import pytest
from unittest.mock import AsyncMock, MagicMock

from exitmgr.connection import IBConnection, OrderData, OrderView, OrderBookUnreadable


CON = 7100
OTHER = 7200


def _trade(con_id, order_id, remaining, *, action="SELL", total=None, filled=None,
           perm_id=0, client_id=None, order_ref=None, status="Submitted", lmt=1.23):
    t = MagicMock()
    t.contract = MagicMock()
    t.contract.secType = "OPT"
    t.contract.conId = con_id
    t.order = MagicMock()
    t.order.action = action
    t.order.orderId = order_id
    t.order.totalQuantity = total if total is not None else remaining
    t.order.lmtPrice = lmt
    t.order.permId = perm_id
    t.order.clientId = client_id
    t.order.orderRef = order_ref
    t.orderStatus = MagicMock()
    t.orderStatus.remaining = remaining
    t.orderStatus.filled = filled if filled is not None else 0
    t.orderStatus.status = status
    return t


def _conn(trades):
    c = IBConnection("127.0.0.1", 4001, 77)
    c.ib = MagicMock()
    c.ib.isConnected.return_value = True
    c._connected = True
    c.ib.reqAllOpenOrdersAsync = AsyncMock(return_value=trades)
    return c



@pytest.mark.asyncio
async def test_one_order_reads_exactly_as_before():
    """Public API contract; production-derived narrative omitted."""
    conn = _conn([_trade(CON, 900, 3, perm_id=55, client_id=91, order_ref="ref-a")])
    book = await conn.get_open_orders()

    od = book[CON]
    assert (od.con_id, od.order_id, od.remaining) == (CON, 900, 3)
    assert (od.perm_id, od.client_id, od.order_ref, od.status) == (55, 91, "ref-a", "Submitted")
    assert od.order_ids == (900,)
    assert od.order_count == 1
    assert od.is_duplicated is False



@pytest.mark.asyncio
async def test_two_resting_orders_aggregate_remaining_and_keep_both_ids():
    """Public API contract; production-derived narrative omitted."""
    conn = _conn([_trade(CON, 900, 1, perm_id=55),
                  _trade(CON, 901, 1, perm_id=56)])
    book = await conn.get_open_orders()

    assert len(book) == 1
    od = book[CON]
    assert od.remaining == 2, "the quantity working at the broker is the SUM, not the last one"
    assert od.order_ids == (900, 901)
    assert od.order_count == 2
    assert od.is_duplicated is True


    assert od.order_id == 900 and od.perm_id == 55


@pytest.mark.asyncio
async def test_three_orders_and_unequal_quantities():
    conn = _conn([_trade(CON, 910, 2), _trade(CON, 911, 5), _trade(CON, 912, 1)])
    od = (await conn.get_open_orders())[CON]
    assert od.remaining == 8
    assert od.order_ids == (910, 911, 912)
    assert od.order_count == 3


@pytest.mark.asyncio
async def test_duplication_on_one_contract_does_not_touch_another():
    conn = _conn([_trade(CON, 920, 1), _trade(OTHER, 921, 4), _trade(CON, 922, 1)])
    book = await conn.get_open_orders()
    assert book[CON].remaining == 2 and book[CON].order_count == 2
    assert book[OTHER].remaining == 4 and book[OTHER].order_count == 1
    assert book[OTHER].order_ids == (921,)


@pytest.mark.asyncio
async def test_a_covering_buy_and_a_sell_on_one_short_leg_both_count():
    """Public API contract; production-derived narrative omitted."""
    conn = _conn([_trade(CON, 930, 1, action="BUY"), _trade(CON, 931, 2, action="SELL")])
    od = (await conn.get_open_orders(short_leg_con_ids={CON}))[CON]
    assert od.remaining == 3
    assert od.order_ids == (930, 931)


@pytest.mark.asyncio
async def test_an_entry_buy_is_still_excluded_and_cannot_inflate_the_total():
    """Public API contract; production-derived narrative omitted."""
    conn = _conn([_trade(CON, 940, 2, action="BUY"), _trade(CON, 941, 1, action="SELL")])
    od = (await conn.get_open_orders())[CON]
    assert od.remaining == 1
    assert od.order_ids == (941,)


@pytest.mark.asyncio
async def test_spread_combo_legs_key_by_the_long_leg_and_still_aggregate():
    """Public API contract; production-derived narrative omitted."""
    def _combo(order_id, remaining):
        t = _trade(CON, order_id, remaining)
        t.contract.secType = "BAG"
        t.contract.conId = 0
        leg_buy, leg_sell = MagicMock(), MagicMock()
        leg_buy.action, leg_buy.conId = "BUY", CON
        leg_sell.action, leg_sell.conId = "SELL", OTHER
        t.contract.comboLegs = [leg_buy, leg_sell]
        return t

    conn = _conn([_combo(950, 1), _combo(951, 2)])
    book = await conn.get_open_orders()
    assert set(book) == {CON}
    assert book[CON].remaining == 3 and book[CON].order_count == 2



@pytest.mark.asyncio
async def test_the_state_dict_view_carries_multiplicity():
    """Public API contract; production-derived narrative omitted."""
    conn = _conn([_trade(CON, 960, 1), _trade(CON, 961, 1)])
    view = OrderView.known(await conn.get_open_orders())
    d = view.as_state_dicts()[CON]
    assert d["remaining"] == 2
    assert d["order_id"] == 960
    assert d["order_ids"] == [960, 961]
    assert d["order_count"] == 2


def test_a_hand_built_orderdata_still_reads_as_one_order():
    """Public API contract; production-derived narrative omitted."""
    od = OrderData(CON, 970, 4)
    assert od.order_count == 1 and od.is_duplicated is False
    d = OrderView.known({CON: od}).as_state_dicts()[CON]
    assert d["order_ids"] == [970] and d["order_count"] == 1


@pytest.mark.asyncio
async def test_an_unreadable_book_is_still_unknown_not_empty():
    """Public API contract; production-derived narrative omitted."""
    conn = _conn(None)
    with pytest.raises(OrderBookUnreadable):
        await conn.get_open_orders()

    v = await conn.get_open_orders_view()
    assert v.readable is False
    with pytest.raises(TypeError):
        bool(v)
    with pytest.raises(OrderBookUnreadable):
        v.as_state_dicts()
