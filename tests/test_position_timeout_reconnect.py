"""Public API contract; production-derived narrative omitted."""
import ast
import asyncio
from pathlib import Path
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import exitmgr.connection as connection_mod
from exitmgr.connection import IBConnection


def connected():
    conn = IBConnection("127.0.0.1", 4001, 189)
    conn.ib = SimpleNamespace(
        isConnected=lambda: True,
        reqCurrentTimeAsync=AsyncMock(return_value="clock still answers"),
        reqPositionsAsync=AsyncMock(return_value=[]))
    conn._connected = True
    conn._uplink_ok = True
    conn._link_fault = False
    conn._price_events_accepted = True
    conn._connection_generation = 7
    conn._portfolio_observations[42] = {
        "price": 3.0, "observed_monotonic": time.monotonic(), "generation": 7}
    conn._qualified_quote_contracts[42] = object()
    return conn


@pytest.mark.asyncio
async def test_real_position_timeout_invalidates_lane_and_second_read_fails_fast(monkeypatch):
    conn = connected()
    cancelled = asyncio.Event()

    async def never_positions():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    conn.ib.reqPositionsAsync = AsyncMock(side_effect=never_positions)
    original_wait = asyncio.wait_for
    requested_budgets = []

    async def shorten_test_clock(awaitable, timeout):
        requested_budgets.append(timeout)
        return await original_wait(awaitable, 0.02)

    monkeypatch.setattr(connection_mod.asyncio, "wait_for", shorten_test_clock)
    with pytest.raises(asyncio.TimeoutError):
        await conn.get_positions()
    assert requested_budgets == [30]
    assert cancelled.is_set()
    assert conn._link_fault is True
    assert conn._connection_generation == 8
    assert conn.portfolio_mark_observations() == {}
    assert conn._qualified_quote_contracts == {}
    assert conn._price_events_accepted is False
    assert not conn.is_healthy()

    with pytest.raises(RuntimeError, match="Not connected to IB"):
        await conn.get_positions()
    conn.ib.reqPositionsAsync.assert_awaited_once()
    assert requested_budgets == [30], "second position read must fail before another timed wait"
    assert await conn.ensure_connected() is False
    conn.ib.reqCurrentTimeAsync.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_protective_loop_reconnects_instead_of_trusting_clock():
    conn = connected()
    conn.ib.reqPositionsAsync.side_effect = asyncio.TimeoutError()
    with pytest.raises(asyncio.TimeoutError):
        await conn.get_positions()
    conn.reconnect = AsyncMock(return_value=True)
    manager = SimpleNamespace(_reconcile_on_startup=AsyncMock(return_value=True))


    source = Path(__file__).resolve().parents[1] / "run_trader.py"
    tree = ast.parse(source.read_text())
    helper = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)
                  and n.name == "_ensure_live_connection")
    scope = {"connection_lock": asyncio.Lock(), "ib_conn": conn, "exit_mgr": manager}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[helper], type_ignores=[])),
                 str(source), "exec"), scope)

    assert await scope["_ensure_live_connection"]() is True
    conn.reconnect.assert_awaited_once_with(retries=3, retry_delay=10)
    manager._reconcile_on_startup.assert_awaited_once()
    conn.ib.reqCurrentTimeAsync.assert_not_awaited()


@pytest.mark.asyncio
async def test_connection_reset_keeps_position_book_unknown():
    conn = connected()
    failure = ConnectionResetError("fixture reset")
    conn.ib.reqPositionsAsync.side_effect = failure
    with pytest.raises(ConnectionResetError) as raised:
        await conn.get_positions(include_short=True, include_stock=True)
    assert raised.value is failure
    assert conn._link_fault is True
    assert conn.portfolio_mark_observations() == {}


@pytest.mark.asyncio
async def test_empty_successful_book_stays_known_and_connected():
    conn = connected()
    assert await conn.get_positions() == {}
    assert conn.is_healthy()
    assert conn._connection_generation == 7
    assert conn._link_fault is False


@pytest.mark.asyncio
async def test_caller_cancellation_is_not_misreported_as_gateway_fault():
    conn = connected()
    entered = asyncio.Event()

    async def pending():
        entered.set()
        await asyncio.Event().wait()

    conn.ib.reqPositionsAsync.side_effect = pending
    task = asyncio.create_task(conn.get_positions())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert conn._link_fault is False
    assert conn._connection_generation == 7
