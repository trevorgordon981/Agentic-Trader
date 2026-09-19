"""Public API contract; production-derived narrative omitted."""
import ast
import asyncio
from pathlib import Path
import socket
import sys
from unittest.mock import AsyncMock, Mock

import pytest
from ib_async import IB

SOURCE = Path(__file__).resolve().parents[1] / 'gateway_health_alert.py'
TREE = ast.parse(SOURCE.read_text())


def monitor(ib):
    scope = {'asyncio': asyncio, 'sys': sys, 'IB': lambda: ib, 'CLIENT_ID': 930}
    nodes = [n for n in TREE.body if isinstance(n, ast.AsyncFunctionDef) and n.name in ('probe', 'main')]
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(SOURCE), 'exec'), scope)
    return scope


@pytest.fixture
def broker(monkeypatch):


    monkeypatch.setattr(socket.socket, 'connect', Mock(side_effect=AssertionError('network forbidden')))
    ib = IB()
    ib.client.connectAsync = AsyncMock()
    ib.client.getAccounts = Mock(return_value=['fixture-account'])
    ib.client.serverVersion = Mock(return_value=200)
    ib.client.isReady = Mock(return_value=True)
    ib.managedAccounts = Mock(return_value=['fixture-account'])
    ib.accountValues = Mock(return_value=[object()])
    ib.disconnect = Mock()
    for name in ('reqPositionsAsync', 'reqOpenOrdersAsync', 'reqCompletedOrdersAsync',
                 'reqAccountUpdatesAsync', 'reqAccountUpdatesMultiAsync', 'reqExecutionsAsync', 'reqAllOpenOrdersAsync'):
        setattr(ib, name, AsyncMock(return_value=[]))
    real_wait = asyncio.wait_for
    budgets = []
    async def accelerated_wait(awaitable, timeout):
        budgets.append(timeout)
        return await real_wait(awaitable, 0.02 if timeout in (6, 10) else 0.3)
    monkeypatch.setattr(asyncio, 'wait_for', accelerated_wait)
    ib.test_budgets = budgets
    return ib


async def never(*args, **kwargs):
    await asyncio.Event().wait()


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['reqPositionsAsync', 'reqOpenOrdersAsync',
    'reqAccountUpdatesAsync', 'reqAccountUpdatesMultiAsync', 'reqExecutionsAsync'])
async def test_real_library_sync_timeout_never_reports_up(broker, method):
    getattr(broker, method).side_effect = never
    scope = monitor(broker)
    scope.update(read_state=lambda: {'status': 'up'}, write_state=Mock(), slack=Mock(return_value=True))
    result = await scope['main'](False, 116)
    assert result == 1
    scope['write_state'].assert_called_once_with('down')
    assert 'Gateway DOWN' in scope['slack'].call_args.args[0]
    assert 'request timed out' in scope['slack'].call_args.args[0]
    assert not any('back UP' in call.args[0] for call in scope['slack'].call_args_list)
    broker.client.connectAsync.assert_awaited_once_with('127.0.0.1', 4001, 116, 10)
    assert 30 in broker.test_budgets
    broker.disconnect.assert_called()


@pytest.mark.asyncio
async def test_completed_empty_position_book_is_healthy_and_disconnects(broker):
    healthy, detail = await monitor(broker)['probe']()
    assert healthy is True
    assert 'healthy' in detail
    assert broker.reqPositionsAsync.await_count == 2
    broker.reqAllOpenOrdersAsync.assert_awaited_once()
    broker.reqOpenOrdersAsync.assert_awaited_once()
    broker.reqAccountUpdatesAsync.assert_awaited_once_with('fixture-account')
    broker.disconnect.assert_called_once()


@pytest.mark.asyncio
async def test_no_accounts_is_not_readiness(broker):
    broker.managedAccounts.return_value = []
    healthy, detail = await monitor(broker)['probe']()
    assert healthy is False
    assert 'NO accounts' in detail
    broker.disconnect.assert_called_once()


@pytest.mark.asyncio
async def test_outer_deadline_cleans_up_unresponsive_api_connection(broker):
    broker.client.connectAsync.side_effect = never
    healthy, detail = await monitor(broker)['probe']()
    assert healthy is False
    assert 'TimeoutError' in detail
    assert broker.test_budgets == [30]
    broker.disconnect.assert_called()


@pytest.mark.asyncio
async def test_cancellation_propagates_and_disconnects(broker):
    entered = asyncio.Event()
    async def blocked(*args):
        entered.set()
        await asyncio.Event().wait()
    broker.client.connectAsync.side_effect = blocked
    task = asyncio.create_task(monitor(broker)['probe']())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    broker.disconnect.assert_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['reqPositionsAsync', 'reqAllOpenOrdersAsync'])
async def test_fresh_read_exceptions_never_report_healthy(broker, method):
    if method == 'reqPositionsAsync':
        broker.reqPositionsAsync.side_effect = [[], ConnectionError('fixture broken stream')]
    else:
        broker.reqAllOpenOrdersAsync.side_effect = ConnectionError('fixture broken stream')
    healthy, detail = await monitor(broker)['probe']()
    assert healthy is False
    assert 'ConnectionError' in detail
    broker.disconnect.assert_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['reqPositionsAsync', 'reqAllOpenOrdersAsync'])
async def test_none_book_is_unknown_not_empty(broker, method):
    getattr(broker, method).return_value = None
    healthy, detail = await monitor(broker)['probe']()
    assert healthy is False
    assert 'UNKNOWN' in detail


@pytest.mark.asyncio
async def test_no_account_values_is_not_healthy(broker):
    broker.accountValues.return_value = []
    healthy, detail = await monitor(broker)['probe']()
    assert healthy is False
    assert 'NO account values' in detail
