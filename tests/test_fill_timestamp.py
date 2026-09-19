"""Public API contract; production-derived narrative omitted."""
import datetime as dt

import pytest

from exitmgr.order import fill_timestamp_from_trade


class _Exec:
    def __init__(self, time):
        self.time = time


class _Fill:
    def __init__(self, time):
        self.execution = _Exec(time)


class _Trade:
    def __init__(self, fills):
        self.fills = fills


def test_the_brokers_execution_time_is_used_not_the_local_clock():
    """Public API contract; production-derived narrative omitted."""
    real = dt.datetime(2026, 8, 21, 16, 20, 18, tzinfo=dt.timezone.utc)
    ts, src = fill_timestamp_from_trade(_Trade([_Fill(real)]))
    assert ts == real.isoformat()
    assert src == "broker_execution"

    now = dt.datetime.now(dt.timezone.utc)
    assert abs((dt.datetime.fromisoformat(ts) - now).total_seconds()) > 60


def test_the_last_fill_wins_for_a_partially_filled_order():
    """Public API contract; production-derived narrative omitted."""
    first = dt.datetime(2026, 8, 21, 16, 20, 18, tzinfo=dt.timezone.utc)
    last = dt.datetime(2026, 8, 21, 16, 24, 55, tzinfo=dt.timezone.utc)
    ts, src = fill_timestamp_from_trade(_Trade([_Fill(first), _Fill(last)]))
    assert ts == last.isoformat()
    assert src == "broker_execution"


def test_a_naive_broker_timestamp_is_pinned_to_utc_never_left_ambiguous():
    """Public API contract; production-derived narrative omitted."""
    naive = dt.datetime(2026, 8, 21, 16, 20, 18)
    ts, src = fill_timestamp_from_trade(_Trade([_Fill(naive)]))
    parsed = dt.datetime.fromisoformat(ts)
    assert parsed.tzinfo is not None, "a naive timestamp must never escape this function"
    assert parsed.utcoffset() == dt.timedelta(0)
    assert src == "broker_execution"


@pytest.mark.parametrize("trade", [
    _Trade([]),
    _Trade(None),
    object(),
])
def test_an_inferred_time_is_labelled_as_inferred(trade):
    """Public API contract; production-derived narrative omitted."""
    ts, src = fill_timestamp_from_trade(trade)
    assert src == "local_clock", "fallback must never claim to be broker_execution"
    assert dt.datetime.fromisoformat(ts).tzinfo is not None


def test_the_function_never_raises_into_the_order_path():
    """Public API contract; production-derived narrative omitted."""
    class Exploding:
        @property
        def fills(self):
            raise RuntimeError("broker object is garbage")
    ts, src = fill_timestamp_from_trade(Exploding())
    assert src == "local_clock"
    assert ts is not None
