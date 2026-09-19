"""Public API contract; production-derived narrative omitted."""
import asyncio
import socket
from unittest.mock import AsyncMock, Mock
import pytest
from ib_async import IB
import exitmgr.connection as module
from exitmgr.connection import IBConnection

@pytest.fixture
def native(monkeypatch):
    monkeypatch.setattr(socket.socket, 'connect', Mock(side_effect=AssertionError('network forbidden')))
    ib = IB()
    ib.client.connectAsync = AsyncMock()
    ib.client.getAccounts = Mock(return_value=['fixture-account'])
    ib.client.serverVersion = Mock(return_value=200)
    ib.client.isReady = Mock(return_value=True)
    ib.disconnect = Mock()
    ib.reqMarketDataType = Mock()
    for name in ('reqPositionsAsync', 'reqOpenOrdersAsync', 'reqCompletedOrdersAsync',
                 'reqAccountUpdatesAsync', 'reqAccountUpdatesMultiAsync', 'reqExecutionsAsync'):
        setattr(ib, name, AsyncMock(return_value=[]))
    monkeypatch.setattr(module, 'IB', lambda: ib)
    real_wait = asyncio.wait_for
    async def short_wait(awaitable, timeout):
        return await real_wait(awaitable, 0.02 if timeout == 10 else 0.3)
    monkeypatch.setattr(asyncio, 'wait_for', short_wait)
    return ib

@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['reqPositionsAsync', 'reqOpenOrdersAsync',
    'reqAccountUpdatesAsync', 'reqAccountUpdatesMultiAsync', 'reqExecutionsAsync'])
async def test_native_partial_sync_cannot_pass_application_connect(native, method):
    async def never(*args, **kwargs):
        await asyncio.Event().wait()
    getattr(native, method).side_effect = never
    conn = IBConnection('127.0.0.1', 4001, 189)
    assert await conn.connect() is False
    assert conn._connected is False
    assert conn._price_events_accepted is False
    assert conn.ib is None
    native.disconnect.assert_called()
    native.reqMarketDataType.assert_not_called()

@pytest.mark.asyncio
async def test_native_completed_sync_preserves_success_and_empty_book(native):
    conn = IBConnection('127.0.0.1', 4001, 189)
    assert await conn.connect() is True
    assert conn._connected is True
    assert conn._link_fault is False
    assert conn._price_events_accepted is True
    assert await conn.get_positions() == {}
    native.reqMarketDataType.assert_called_once_with(3)
    native.disconnect.assert_not_called()
