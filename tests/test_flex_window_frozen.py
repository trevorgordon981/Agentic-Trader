"""Public API contract; production-derived narrative omitted."""
from datetime import datetime

import pytest

from exitmgr.flex_ingest import FLEX_WINDOW_STALE_DAYS, flex_window_staleness

NOW = datetime(2026, 8, 23)


def test_the_frozen_window_is_named_with_its_cause_and_remedy():
    days, msg = flex_window_staleness({"fromDate": "20250703", "toDate": "20260702"}, NOW)
    assert days == 52
    assert msg, "a 52-day-stale window must produce a diagnosis"
    assert "IBKR_FLEX_QUERY_ID" in msg, "the message must name the cause"
    assert "rolling" in msg.lower(), "the message must name the remedy"
    assert "no code change" in msg.lower(), (
        "it must say this cannot be fixed in code, or someone will try")


def test_a_current_window_says_nothing():
    days, msg = flex_window_staleness({"fromDate": "20250822", "toDate": "20260821"}, NOW)
    assert days == 2 and msg is None


def test_a_normal_settlement_lag_is_not_called_stale():
    """Public API contract; production-derived narrative omitted."""
    for back in range(0, FLEX_WINDOW_STALE_DAYS + 1):
        d = NOW.date().toordinal() - back
        stamp = datetime.fromordinal(d).strftime("%Y%m%d")
        _, msg = flex_window_staleness({"toDate": stamp}, NOW)
        assert msg is None, "%d days behind must not be called stale" % back


def test_one_day_past_the_threshold_does_fire():
    d = NOW.date().toordinal() - (FLEX_WINDOW_STALE_DAYS + 1)
    stamp = datetime.fromordinal(d).strftime("%Y%m%d")
    _, msg = flex_window_staleness({"toDate": stamp}, NOW)
    assert msg, "the threshold must actually be a threshold"


@pytest.mark.parametrize("meta", [{}, {"toDate": ""}, {"toDate": None},
                                  {"toDate": "not-a-date"}, {"toDate": "2026-99-99"}])
def test_an_unreadable_window_is_silent_not_noisy(meta):
    """Public API contract; production-derived narrative omitted."""
    days, msg = flex_window_staleness(meta, NOW)
    assert msg is None


def test_a_hyphenated_date_is_understood():
    days, msg = flex_window_staleness({"toDate": "2026-07-02"}, NOW)
    assert days == 52 and msg


def test_the_reconcile_post_names_the_root_cause_only_when_frozen(monkeypatch):
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import flex_reconcile as F
    import exitmgr.flex_ingest as flex_ingest



    real_staleness = flex_ingest.flex_window_staleness
    monkeypatch.setattr(
        flex_ingest, "flex_window_staleness",
        lambda meta: real_staleness(meta, NOW))

    assert F._frozen_query_note("20260821") is None, "a current window must stay silent"
    note = F._frozen_query_note("20260702")
    assert note and "IBKR_FLEX_QUERY_ID" in note and "REMEDY" in note


def test_the_note_never_breaks_the_post():
    """Public API contract; production-derived narrative omitted."""
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import flex_reconcile as F

    for junk in (None, "", "garbage", 12345, object()):
        F._frozen_query_note(junk)
