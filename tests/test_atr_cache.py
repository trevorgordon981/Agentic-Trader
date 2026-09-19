"""Public API contract; production-derived narrative omitted."""
import json
from datetime import date, timedelta

import pytest

from exitmgr import atr_cache


@pytest.fixture
def cache(tmp_path, monkeypatch):
    p = tmp_path / "atr-cache.json"
    monkeypatch.setattr(atr_cache, "CACHE_PATH", str(p))

    def write(payload):
        p.write_text(json.dumps(payload))
    return write


TODAY = date(2037, 8, 19)


def test_reads_a_fresh_entry(cache):
    cache({"SYMN": {"atr": 1.25, "spot": 75.0, "asof": "2037-08-18"}})
    rec = atr_cache.read("SYMN", today=TODAY)
    assert rec["atr"] == 1.25 and rec["spot"] == 75.0


def test_symbol_lookup_is_case_insensitive(cache):
    cache({"SYMN": {"atr": 1.25, "spot": 75.0, "asof": "2037-08-18"}})
    assert atr_cache.read("SYMN", today=TODAY) is not None


def test_stale_entry_reads_as_absent(cache):
    old = (TODAY - timedelta(days=30)).isoformat()
    cache({"SYMN": {"atr": 1.25, "spot": 75.0, "asof": old}})
    assert atr_cache.read("SYMN", today=TODAY) is None


def test_long_weekend_staleness_is_tolerated(cache):
    """Public API contract; production-derived narrative omitted."""
    asof = (TODAY - timedelta(days=5)).isoformat()
    cache({"SYMN": {"atr": 1.25, "spot": 75.0, "asof": asof}})
    assert atr_cache.read("SYMN", today=TODAY) is not None


def test_missing_symbol_and_missing_file_are_both_none(cache, tmp_path, monkeypatch):
    cache({"SYMN": {"atr": 1.25, "spot": 75.0, "asof": "2037-08-18"}})
    assert atr_cache.read("NVDA", today=TODAY) is None
    monkeypatch.setattr(atr_cache, "CACHE_PATH", str(tmp_path / "nope.json"))
    assert atr_cache.read("SYMN", today=TODAY) is None


@pytest.mark.parametrize("bad", [
    {"atr": 0.0, "spot": 28.0, "asof": "2037-08-18"},
    {"atr": -1.0, "spot": 28.0, "asof": "2037-08-18"},
    {"atr": 0.6, "spot": 0.0, "asof": "2037-08-18"},
    {"atr": "x", "spot": 28.0, "asof": "2037-08-18"},
    {"atr": 0.6, "spot": 28.0, "asof": "not-a-date"},
    {"atr": 0.6, "spot": 28.0},
    {"spot": 28.0, "asof": "2037-08-18"},
    "not-a-dict",
])
def test_malformed_entries_read_as_absent_never_raise(cache, bad):
    cache({"SYMN": bad})
    assert atr_cache.read("SYMN", today=TODAY) is None


def test_corrupt_cache_file_reads_as_absent(tmp_path, monkeypatch):
    p = tmp_path / "atr-cache.json"
    p.write_text("{ this is not json")
    monkeypatch.setattr(atr_cache, "CACHE_PATH", str(p))
    assert atr_cache.read("SYMN", today=TODAY) is None


def test_empty_symbol_is_none(cache):
    cache({"SYMN": {"atr": 1.25, "spot": 75.0, "asof": "2037-08-18"}})
    assert atr_cache.read("", today=TODAY) is None
    assert atr_cache.read(None, today=TODAY) is None


def test_symbols_from_journal_dedups_and_uppercases(tmp_path):
    j = tmp_path / "trades.log"
    j.write_text("\n".join([
        json.dumps({"symbol": "SYMN", "contract_id": 1}),
        json.dumps({"symbol": "SYMN", "contract_id": 2}),
        json.dumps({"symbol": "SYMO", "contract_id": 3}),
        "not json at all",
        json.dumps({"contract_id": 4}),
    ]))
    assert atr_cache.symbols_from_journal(str(j)) == ["SYMN", "SYMO"]


def test_symbols_from_missing_journal_is_empty_not_an_error():
    assert atr_cache.symbols_from_journal("/nonexistent/trades.log") == []
