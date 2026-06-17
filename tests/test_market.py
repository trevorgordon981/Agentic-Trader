"""Tests for the market-context formatter."""
from exitmgr.market import format_context


def test_format_with_quotes():
    q = {"SPY": {"last": 612.4, "change_pct": 0.42},
         "QQQ": {"last": 540.1, "change_pct": 0.61},
         "IWM": {"last": 218.3, "change_pct": -0.15}}
    s = format_context(q, ["SPY", "QQQ", "IWM"], "2026-06-11")
    assert "2026-06-11" in s
    assert "SPY: 612.40 (+0.42%)" in s
    assert "IWM: 218.30 (-0.15%)" in s
    assert "Universe: SPY, QQQ, IWM" in s


def test_format_no_quotes_fallback():
    s = format_context({}, ["SPY", "QQQ"], "2026-06-11")
    assert "unavailable" in s and "Universe: SPY, QQQ" in s


def test_format_handles_missing_change():
    s = format_context({"SPY": {"last": 612.4, "change_pct": None}}, ["SPY"], "2026-06-11")
    assert "SPY: 612.40 (n/a)" in s


def test_usable_price_rejects_ib_no_data_sentinels():
    from exitmgr.market import usable_price
    assert not usable_price(-1.0)          # IB after-hours "no data" sentinel
    assert not usable_price(0.0)
    assert not usable_price(float("nan"))
    assert not usable_price(None)
    assert usable_price(204.87)


def test_format_open_universe_invites_single_names():
    s = format_context({}, ["SPY", "QQQ", "IWM"], "2026-06-11", allow_any_name=True)
    assert "single name" in s and "human approval" in s


def test_format_closed_universe_by_default():
    s = format_context({}, ["SPY", "QQQ", "IWM"], "2026-06-11")
    assert "single name" not in s


def test_connect_requests_configured_market_data_type():
    import asyncio
    from unittest.mock import AsyncMock, patch
    from exitmgr.connection import IBConnection

    with patch("exitmgr.connection.IB") as mock_ib_cls:
        inst = mock_ib_cls.return_value
        inst.connectAsync = AsyncMock(return_value=None)
        conn = IBConnection(host="127.0.0.1", port=4001, client_id=87)  # default = 3 (delayed)
        assert asyncio.run(conn.connect())
        inst.reqMarketDataType.assert_called_once_with(3)
