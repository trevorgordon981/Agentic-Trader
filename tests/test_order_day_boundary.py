"""Public API contract; production-derived narrative omitted."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from exitmgr.order import _trading_day as order_trading_day
from exitmgr.trader import _trading_day as trader_trading_day

ET = ZoneInfo("America/New_York")


def test_evening_et_instant_that_is_already_tomorrow_in_utc():


    now_et = datetime(2026, 7, 6, 21, 30, tzinfo=ET)
    assert now_et.astimezone(timezone.utc).date().isoformat() == "2026-07-07"
    assert order_trading_day(now=now_et) == "2026-07-06"


def test_naive_utc_instant_is_treated_as_utc():


    naive_utc = datetime(2026, 7, 7, 1, 30)
    assert order_trading_day(now=naive_utc) == "2026-07-06"


def test_matches_canonical_trader_helper():


    for inst in (
        datetime(2026, 7, 6, 21, 30, tzinfo=ET),
        datetime(2026, 7, 6, 9, 45, tzinfo=ET),
        datetime(2026, 3, 8, 3, 30, tzinfo=timezone.utc),
        datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc),
        datetime(2026, 7, 7, 1, 30),
    ):
        assert order_trading_day(now=inst) == trader_trading_day(now=inst)
