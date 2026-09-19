from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from run_trader import _ensure_quote_connection


@pytest.mark.asyncio
async def test_cold_quote_lane_uses_configured_id_without_rotation():
    conn = SimpleNamespace(
        ib=None,
        client_id=118,
        connect=AsyncMock(return_value=True),
        ensure_connected=AsyncMock(),
        reconnect=AsyncMock(),
    )

    assert await _ensure_quote_connection(conn)
    conn.connect.assert_awaited_once_with(retries=1, retry_delay=1)
    conn.ensure_connected.assert_not_awaited()
    conn.reconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_healthy_existing_quote_lane_is_reused():
    conn = SimpleNamespace(
        ib=object(),
        client_id=118,
        connect=AsyncMock(),
        ensure_connected=AsyncMock(return_value=True),
        reconnect=AsyncMock(),
    )

    assert await _ensure_quote_connection(conn)
    conn.connect.assert_not_awaited()
    conn.reconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_dead_established_quote_lane_uses_rotation_reconnect():
    conn = SimpleNamespace(
        ib=object(),
        client_id=118,
        connect=AsyncMock(),
        ensure_connected=AsyncMock(return_value=False),
        reconnect=AsyncMock(return_value=True),
    )

    assert await _ensure_quote_connection(conn)
    conn.connect.assert_not_awaited()
    conn.reconnect.assert_awaited_once_with(retries=1, retry_delay=1)
