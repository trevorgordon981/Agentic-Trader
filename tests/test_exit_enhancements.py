"""Tests for the exit-manager enhancements: real DTE + persisted trailing peak."""
from datetime import date
from exitmgr.rules import days_to_expiry
from exitmgr.state import StateManager


def test_days_to_expiry():
    assert days_to_expiry("20260620", date(2026, 6, 11)) == 9
    assert days_to_expiry("20260611", date(2026, 6, 11)) == 0
    assert days_to_expiry("20260611120000", date(2026, 6, 11)) == 0   # tolerates time suffix
    assert days_to_expiry("", date(2026, 6, 11)) is None
    assert days_to_expiry("garbage", date(2026, 6, 11)) is None


def test_peak_prices_persist_across_restart(tmp_path):
    p = str(tmp_path / "s.json")
    sm = StateManager(p)
    sm.state.peak_prices["123"] = 4.20
    sm.save()
    sm2 = StateManager(p)                  # simulate a restart -> reload from disk
    assert sm2.state.peak_prices.get("123") == 4.20
