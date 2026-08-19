"""Tests for the ATR cache READ path.

`refresh()` does network and is exercised for real by the daily job; what must never
break is `read()` -- it sits in front of live stops, so every malformed, stale or missing
case has to degrade to None rather than raise or return a number nobody can justify.
"""
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


TODAY = date(2026, 8, 19)


def test_reads_a_fresh_entry(cache):
    cache({"PFE": {"atr": 0.62, "spot": 28.08, "asof": "2026-08-18"}})
    rec = atr_cache.read("PFE", today=TODAY)
    assert rec["atr"] == 0.62 and rec["spot"] == 28.08


def test_symbol_lookup_is_case_insensitive(cache):
    cache({"PFE": {"atr": 0.62, "spot": 28.08, "asof": "2026-08-18"}})
    assert atr_cache.read("pfe", today=TODAY) is not None


def test_stale_entry_reads_as_absent(cache):
    old = (TODAY - timedelta(days=30)).isoformat()
    cache({"PFE": {"atr": 0.62, "spot": 28.08, "asof": old}})
    assert atr_cache.read("PFE", today=TODAY) is None


def test_long_weekend_staleness_is_tolerated(cache):
    """A Thursday refresh read on the following Tuesday is 5 days old and still valid --
    the threshold must not page/degrade over ordinary market closures."""
    asof = (TODAY - timedelta(days=5)).isoformat()
    cache({"PFE": {"atr": 0.62, "spot": 28.08, "asof": asof}})
    assert atr_cache.read("PFE", today=TODAY) is not None


def test_missing_symbol_and_missing_file_are_both_none(cache, tmp_path, monkeypatch):
    cache({"PFE": {"atr": 0.62, "spot": 28.08, "asof": "2026-08-18"}})
    assert atr_cache.read("NVDA", today=TODAY) is None
    monkeypatch.setattr(atr_cache, "CACHE_PATH", str(tmp_path / "nope.json"))
    assert atr_cache.read("PFE", today=TODAY) is None


@pytest.mark.parametrize("bad", [
    {"atr": 0.0, "spot": 28.0, "asof": "2026-08-18"},        # zero ATR
    {"atr": -1.0, "spot": 28.0, "asof": "2026-08-18"},       # negative
    {"atr": 0.6, "spot": 0.0, "asof": "2026-08-18"},         # zero spot
    {"atr": "x", "spot": 28.0, "asof": "2026-08-18"},        # non-numeric
    {"atr": 0.6, "spot": 28.0, "asof": "not-a-date"},        # unparseable date
    {"atr": 0.6, "spot": 28.0},                              # no date at all
    {"spot": 28.0, "asof": "2026-08-18"},                    # no atr
    "not-a-dict",
])
def test_malformed_entries_read_as_absent_never_raise(cache, bad):
    cache({"PFE": bad})
    assert atr_cache.read("PFE", today=TODAY) is None


def test_corrupt_cache_file_reads_as_absent(tmp_path, monkeypatch):
    p = tmp_path / "atr-cache.json"
    p.write_text("{ this is not json")
    monkeypatch.setattr(atr_cache, "CACHE_PATH", str(p))
    assert atr_cache.read("PFE", today=TODAY) is None


def test_empty_symbol_is_none(cache):
    cache({"PFE": {"atr": 0.62, "spot": 28.08, "asof": "2026-08-18"}})
    assert atr_cache.read("", today=TODAY) is None
    assert atr_cache.read(None, today=TODAY) is None


def test_symbols_from_journal_dedups_and_uppercases(tmp_path):
    j = tmp_path / "trades.log"
    j.write_text("\n".join([
        json.dumps({"symbol": "pfe", "contract_id": 1}),
        json.dumps({"symbol": "PFE", "contract_id": 2}),
        json.dumps({"symbol": "HL", "contract_id": 3}),
        "not json at all",
        json.dumps({"contract_id": 4}),
    ]))
    assert atr_cache.symbols_from_journal(str(j)) == ["HL", "PFE"]


def test_symbols_from_missing_journal_is_empty_not_an_error():
    assert atr_cache.symbols_from_journal("/nonexistent/trades.log") == []
