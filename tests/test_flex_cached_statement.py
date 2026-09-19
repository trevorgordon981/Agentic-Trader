"""Public API contract; production-derived narrative omitted."""
from datetime import datetime

import pytest

from exitmgr.flex_ingest import (FLEX_GENERATED_STALE_DAYS, flex_generated_staleness,
                                 flex_window_staleness)

NOW = datetime(2026, 8, 23)


def test_a_cached_statement_is_named_as_cached():
    days, msg = flex_generated_staleness({"whenGenerated": "20260703;171453"}, NOW)
    assert days == 51 and msg
    assert "CACHED" in msg
    assert "period" in msg, "it must say the period attribute is not evidence of freshness"


def test_a_freshly_generated_statement_is_silent():
    days, msg = flex_generated_staleness({"whenGenerated": "20260823;211319"}, NOW)
    assert days == 0 and msg is None


def test_the_two_stalenesses_are_independent_questions():
    """Public API contract; production-derived narrative omitted."""
    frozen_but_fresh = {"whenGenerated": "20260823;120000", "toDate": "20260702"}
    _, gen = flex_generated_staleness(frozen_but_fresh, NOW)
    _, win = flex_window_staleness(frozen_but_fresh, NOW)
    assert gen is None and win, "a freshly generated statement can still carry a dead window"

    cached_but_current = {"whenGenerated": "20260703;120000", "toDate": "20260821"}
    _, gen2 = flex_generated_staleness(cached_but_current, NOW)
    _, win2 = flex_window_staleness(cached_but_current, NOW)
    assert gen2 and win2 is None, "a current window can still come from a cached statement"


@pytest.mark.parametrize("meta", [{}, {"whenGenerated": ""}, {"whenGenerated": None},
                                  {"whenGenerated": "nonsense"}, {"whenGenerated": "2026-99-99"}])
def test_an_unreadable_generation_stamp_is_silent(meta):
    _, msg = flex_generated_staleness(meta, NOW)
    assert msg is None


def test_the_threshold_is_tight_because_generation_is_immediate():
    """Public API contract; production-derived narrative omitted."""
    assert FLEX_GENERATED_STALE_DAYS <= 3
    d = NOW.date().toordinal() - (FLEX_GENERATED_STALE_DAYS + 1)
    stamp = datetime.fromordinal(d).strftime("%Y%m%d")
    _, msg = flex_generated_staleness({"whenGenerated": stamp + ";120000"}, NOW)
    assert msg
