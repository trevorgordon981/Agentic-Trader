from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import portfolio


def test_days_held_accepts_z_and_offset_timestamps():
    now = datetime(2026, 9, 2, 17, 0, tzinfo=timezone.utc)
    assert portfolio._days_held("2026-08-31T17:00:00Z", now=now) == 2
    assert portfolio._days_held("2026-08-31T10:00:00-07:00", now=now) == 2


def test_days_held_pins_legacy_naive_rows_to_los_angeles():
    now = datetime(2026, 9, 2, 17, 0, tzinfo=timezone.utc)
    assert portfolio._days_held("2026-08-31T10:00:00", now=now) == 2


def test_days_held_uses_aware_elapsed_time_across_dst():

    now = datetime(2026, 3, 9, 8, 30, tzinfo=timezone.utc)
    assert portfolio._days_held("2026-03-08T01:30:00", now=now) == 0


@pytest.mark.parametrize("value", [None, "", "not-a-time", "2026-13-40T99:00:00Z"])
def test_days_held_rejects_unusable_timestamps(value):
    with pytest.raises(ValueError):
        portfolio._days_held(value, now=datetime(2026, 9, 2, tzinfo=timezone.utc))


def test_days_held_rejects_future_rows():
    with pytest.raises(ValueError, match="future"):
        portfolio._days_held(
            "2026-09-03T00:00:00Z",
            now=datetime(2026, 9, 2, tzinfo=timezone.utc),
        )


@pytest.mark.asyncio
async def test_review_skips_bad_timestamp_without_losing_valid_position(monkeypatch):
    valid = {
        "contract_id": "101",
        "ts": "2020-01-01T00:00:00Z",
        "symbol": "GOOD",
        "expiry": "20991231",
        "strike": 100.0,
        "right": "C",
        "debit": 100.0,
        "quantity": 1,
    }
    invalid = dict(valid, contract_id="102", symbol="BAD", ts="not-a-time")
    monkeypatch.setattr(portfolio, "_load_journal", lambda _path: {
        "101": valid,
        "102": invalid,
    })

    class _IB:
        async def reqPositionsAsync(self):
            return [
                SimpleNamespace(contract=SimpleNamespace(conId=101), position=1),
                SimpleNamespace(contract=SimpleNamespace(conId=102), position=1),
            ]

    async def _pot(_ib):
        return SimpleNamespace(available_funds=1_000.0, net_liq=10_000.0)

    priced = []

    async def _price(_ib, _entry):
        priced.append(_entry["symbol"])
        return 100.0, 0.0

    monkeypatch.setattr(portfolio, "get_pot_snapshot", _pot)
    monkeypatch.setattr(portfolio, "_price", _price)
    monkeypatch.setattr(
        portfolio,
        "_llm",
        lambda *_args, **_kwargs: '{"reviews":[],"rotation":{"sell":null}}',
    )

    result = await portfolio.review_positions(_IB())

    assert [row["symbol"] for row in result["book"]] == ["GOOD"]
    assert priced == ["GOOD"], "an invalid journal row must not trigger a broker quote"
