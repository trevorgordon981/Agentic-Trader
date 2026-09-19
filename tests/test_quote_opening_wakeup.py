"""Public API contract; production-derived narrative omitted."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import run_trader

PT = ZoneInfo('America/Los_Angeles')


@pytest.mark.parametrize('when,refresh,seconds', [
    ('2026-09-09T06:29:50', False, 10.0),
    ('2026-09-09T06:29:59', False, 1.0),
    ('2026-09-09T06:25:00', False, 300.0),
    ('2026-09-09T02:00:00', False, 300.0),
    ('2026-09-09T06:30:00', True, 10.0),
    ('2026-09-09T12:59:59', True, 10.0),
    ('2026-09-09T13:00:00', False, 300.0),
    ('2026-09-12T06:29:50', False, 300.0),
    ('2026-09-13T06:30:00', False, 300.0),
    ('2026-11-02T06:29:50', False, 10.0),
    ('2027-03-15T06:29:50', False, 10.0),
])
def test_quote_wakeup_uses_exchange_session_boundary(when, refresh, seconds):
    now = datetime.fromisoformat(when).replace(tzinfo=PT)
    actual_refresh, actual_seconds = run_trader._protective_quote_lane_plan(now)
    assert actual_refresh is refresh
    assert actual_seconds == pytest.approx(seconds)


def test_quote_opening_wakeup_ignores_entry_only_five_minute_delay():
    now = datetime(2026, 9, 9, 6, 29, 50, tzinfo=PT)
    assert run_trader._entry_window_wait(now) == pytest.approx(310.0)
    assert run_trader._protective_quote_lane_plan(now) == (False, 10.0)


def test_existing_calendar_limitation_is_preserved_not_falsely_qualified():


    labor_day = datetime(2026, 9, 7, 6, 30, tzinfo=PT)
    assert run_trader._protective_quote_lane_plan(labor_day)[0] is run_trader._market_open_at(labor_day)
