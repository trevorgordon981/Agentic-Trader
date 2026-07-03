"""Daily-stats key must use the US/Eastern trading day, not UTC.

Regression for the UTC day-boundary bug: in the evening ET the UTC date has already
rolled to tomorrow, so an evening-session order was booked onto tomorrow's ledger --
inconsistent with the daily circuit-breaker, which keys off trader._trading_day()
(America/New_York). order._trading_day() must agree with that ET convention.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from exitmgr.order import _trading_day as order_trading_day
from exitmgr.trader import _trading_day as trader_trading_day

ET = ZoneInfo("America/New_York")


def test_evening_et_instant_that_is_already_tomorrow_in_utc():
    # 2026-07-06 21:30 America/New_York == 2026-07-07 01:30 UTC.
    # UTC-keying would wrongly book this onto 2026-07-07; ET-keying must say 2026-07-06.
    now_et = datetime(2026, 7, 6, 21, 30, tzinfo=ET)
    assert now_et.astimezone(timezone.utc).date().isoformat() == "2026-07-07"  # UTC has rolled
    assert order_trading_day(now=now_et) == "2026-07-06"  # ET has NOT


def test_naive_utc_instant_is_treated_as_utc():
    # Naive datetime -> treated as UTC (matches trader._trading_day contract).
    # 2026-07-07 01:30 UTC (naive) is 2026-07-06 21:30 ET.
    naive_utc = datetime(2026, 7, 7, 1, 30)
    assert order_trading_day(now=naive_utc) == "2026-07-06"


def test_matches_canonical_trader_helper():
    # order._trading_day must agree with the canonical trader._trading_day everywhere,
    # including across the evening-ET / UTC-tomorrow boundary.
    for inst in (
        datetime(2026, 7, 6, 21, 30, tzinfo=ET),   # evening ET (UTC = next day)
        datetime(2026, 7, 6, 9, 45, tzinfo=ET),    # RTH
        datetime(2026, 3, 8, 3, 30, tzinfo=timezone.utc),  # near US DST spring-forward
        datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc), # near US DST fall-back
        datetime(2026, 7, 7, 1, 30),               # naive -> UTC
    ):
        assert order_trading_day(now=inst) == trader_trading_day(now=inst)
