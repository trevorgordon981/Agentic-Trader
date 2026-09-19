"""Public API contract; production-derived narrative omitted."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import exitmgr.order as order_module
from exitmgr.connection import IBConnection, OrderNotTransmittedError
from exitmgr.order import OrderManager
from exitmgr.state import StateManager


CON_ID = 990001


def _manager(tmp_path, *, status="Submitted"):
    conn = MagicMock()
    conn.ensure_connected = AsyncMock(return_value=True)
    conn.reserve_order_id.return_value = 7001
    trade = MagicMock()
    trade.order.orderId = 7001
    trade.order.clientId = 189
    trade.order.permId = 8001
    trade.order.orderRef = ""
    trade.orderStatus.status = status
    trade.orderStatus.filled = 0
    trade.log = []
    conn.place_order = AsyncMock(return_value=trade)
    sm = StateManager(str(tmp_path / "state.json"))
    return OrderManager(conn, sm), conn, sm, trade


def _close(om):
    return om.place_close_order(
        con_id=CON_ID, symbol="SPY", quantity=1, limit_price=2.50,
        entry_debit=250.0, live_open_orders={}, right="C",
        exit_context={"reason": "trailing_stop"})


def test_health_predicate_cannot_disagree_with_broker_call_gate():
    conn = IBConnection("127.0.0.1", 4001, 189)
    conn.ib = MagicMock()
    conn.ib.isConnected.return_value = True
    conn._connected = False
    conn._uplink_ok = True
    conn._link_fault = False

    assert conn.is_healthy() is False
    with pytest.raises(RuntimeError, match="Not connected"):
        conn._require_link()


@pytest.mark.parametrize(
    "connected,socket,uplink,fault,usable",
    [
        (True, True, True, False, True),
        (False, True, True, False, False),
        (True, False, True, False, False),
        (True, True, False, False, False),
        (True, True, True, True, False),
    ],
)
def test_health_and_direct_broker_gate_share_one_predicate(
        connected, socket, uplink, fault, usable):
    conn = IBConnection("127.0.0.1", 4001, 189)
    conn.ib = MagicMock()
    conn.ib.isConnected.return_value = socket
    conn._connected = connected
    conn._uplink_ok = uplink
    conn._link_fault = fault

    assert conn.is_healthy() is usable
    if usable:
        conn._require_link()
    else:
        with pytest.raises(RuntimeError, match="Not connected"):
            conn._require_link()
        assert conn._link_fault is True


def test_socket_health_exception_fails_closed_and_arms_repair():
    conn = IBConnection("127.0.0.1", 4001, 189)
    conn.ib = MagicMock()
    conn.ib.isConnected.side_effect = RuntimeError("socket object broken")
    conn._connected = True
    conn._uplink_ok = True
    conn._link_fault = False

    assert conn.is_healthy() is False
    with pytest.raises(RuntimeError, match="Not connected"):
        conn._require_link()
    assert conn._link_fault is True


def test_disconnected_event_invalidates_every_local_health_bit():
    conn = IBConnection("127.0.0.1", 4001, 189)
    conn._connected = True
    conn._uplink_ok = True
    conn._link_fault = False

    conn._on_disconnected()

    assert conn._connected is False
    assert conn._uplink_ok is False
    assert conn._link_fault is True


@pytest.mark.asyncio
async def test_connection_final_probe_fails_before_place_order_is_called():
    conn = IBConnection("127.0.0.1", 4001, 189)
    conn.ib = MagicMock()
    conn.ensure_connected = AsyncMock(return_value=False)

    with pytest.raises(OrderNotTransmittedError, match="nothing sent"):
        await conn.place_order(MagicMock(), MagicMock())

    conn.ib.placeOrder.assert_not_called()


def test_reserve_link_failure_never_reconnects_and_continues_from_stale_truth(tmp_path):
    om, conn, sm, _trade = _manager(tmp_path)
    conn.reserve_order_id.side_effect = RuntimeError("Not connected to IB")
    conn.reconnect = AsyncMock(return_value=True)

    result = asyncio.run(_close(om))

    assert result.success is False and result.ambiguous is False
    assert "reconcile next cycle" in result.message
    conn.reconnect.assert_not_awaited()
    conn.place_order.assert_not_awaited()
    assert sm.state.get_in_flight(CON_ID) is None


def test_final_connection_fence_releases_only_a_proven_untransmitted_intent(tmp_path):
    om, conn, sm, _trade = _manager(tmp_path)
    conn.place_order = AsyncMock(side_effect=OrderNotTransmittedError("nothing sent"))

    result = asyncio.run(_close(om))

    assert result.success is False and result.ambiguous is False
    assert sm.state.get_in_flight(CON_ID) is None


def test_missing_broker_ack_is_not_reported_as_placed(tmp_path, monkeypatch):
    om, conn, sm, trade = _manager(tmp_path, status=None)
    alert = MagicMock()
    monkeypatch.setattr(order_module, "_alert_ambiguous_close", alert)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    result = asyncio.run(_close(om))

    assert result.success is False and result.ambiguous is True
    assert result.acknowledged is False and result.trade is trade
    assert "no broker ACK" in result.message
    inf = sm.state.get_in_flight(CON_ID)
    assert inf is not None and inf.placement_state == "transmission_ambiguous"
    alert.assert_called_once()


def test_pending_submit_must_advance_to_a_real_broker_ack(tmp_path, monkeypatch):
    om, _conn, sm, trade = _manager(tmp_path, status="PendingSubmit")

    async def advance(_delay):
        trade.orderStatus.status = "Submitted"

    monkeypatch.setattr(asyncio, "sleep", advance)
    result = asyncio.run(_close(om))

    assert result.success is True and result.acknowledged is True
    assert result.ambiguous is False
    assert sm.state.get_in_flight(CON_ID).placement_state == "submitted"


def test_transmission_exception_remains_ambiguous_and_latched(tmp_path):
    om, conn, sm, _trade = _manager(tmp_path)
    conn.place_order = AsyncMock(side_effect=RuntimeError("socket died during send"))

    result = asyncio.run(_close(om))

    assert result.success is False and result.ambiguous is True
    assert sm.state.get_in_flight(CON_ID).placement_state == "transmission_ambiguous"
