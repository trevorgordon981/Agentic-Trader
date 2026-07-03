"""Tests for the layered days_to_earnings() feed in exitmgr/research.py.

Post-WSH (2026-07-03): the old IBKR CalendarReport (Reuters) PRIMARY + its running-loop network
path were REMOVED. Wall Street Horizon is now the primary source, prefetched ASYNC into the per-run
cache by gather()/prefetch_wsh_events (covered in tests/test_wsh_feed.py). days_to_earnings()'s own
layering is therefore: per-run cache (a WSH hit) -> yfinance FALLBACK -> None. These tests pin that
public behavior + the int|None contract, with NO network and NO live IBKR (yfinance is a fake module
in sys.modules; a populated cache stands in for a completed WSH prefetch).
"""
import sys
import types
from datetime import date

import pytest

import exitmgr.research as research

REF = date(2026, 7, 1)


@pytest.fixture(autouse=True)
def _clean_module_state():
    research._EARNINGS_DAYS_CACHE.clear()
    yield
    research._EARNINGS_DAYS_CACHE.clear()


def _install_fake_yfinance(monkeypatch, dates):
    """Replace `yfinance` in sys.modules with a fake whose Ticker.get_earnings_dates yields the
    given date objects (mimicking a pandas index of Timestamps with .date())."""
    class _Idx:
        def __init__(self, d):
            self._d = d

        def date(self):
            return self._d

    class _DF:
        def __init__(self, ds):
            self.index = [_Idx(d) for d in ds]

    class _Ticker:
        def __init__(self, sym):
            pass

        def get_earnings_dates(self, limit=12):
            return _DF(dates)

    fake = types.ModuleType("yfinance")
    fake.Ticker = _Ticker
    monkeypatch.setitem(sys.modules, "yfinance", fake)


def test_index_underlying_short_circuits_none():
    # any index/ETF underlying returns None without touching either source
    sym = next(iter(research.INDEX_UNDERLYINGS))
    assert research.days_to_earnings(sym, today=REF) is None


def test_fallback_to_yfinance_when_cache_empty(monkeypatch):
    # empty cache (no WSH hit) => yfinance FALLBACK yields the value -- identical to the pre-WSH path
    _install_fake_yfinance(monkeypatch, [date(2026, 7, 21)])
    assert "AAPL" not in research._EARNINGS_DAYS_CACHE
    assert research.days_to_earnings("AAPL", today=REF) == 20


def test_wsh_cache_hit_preferred_over_yfinance(monkeypatch):
    # a WSH prefetch hit sits in the cache; days_to_earnings returns it WITHOUT touching yfinance
    research._EARNINGS_DAYS_CACHE["AAPL"] = 7
    _install_fake_yfinance(monkeypatch, [date(2026, 8, 30)])  # would be 60 if the fallback ran
    assert research.days_to_earnings("AAPL", today=REF) == 7


def test_per_run_cache(monkeypatch):
    # first call computes via yfinance; second returns the cached value even if the source changes
    _install_fake_yfinance(monkeypatch, [date(2026, 7, 21)])
    assert research.days_to_earnings("AAPL", today=REF) == 20
    _install_fake_yfinance(monkeypatch, [date(2026, 7, 5)])  # source now says 4d
    assert research.days_to_earnings("AAPL", today=REF) == 20  # cached, unchanged


def test_beyond_horizon_is_none(monkeypatch):
    # earnings exist but only outside the 90d horizon => None
    _install_fake_yfinance(monkeypatch, [date(2026, 12, 31)])
    assert research.days_to_earnings("AAPL", today=REF) is None


def test_none_cached_and_never_raises(monkeypatch):
    # both sources miss => None, cached, and no exception ever propagates
    def _boom(*a, **k):
        raise RuntimeError("yfinance exploded")

    fake = types.ModuleType("yfinance")
    fake.Ticker = _boom
    monkeypatch.setitem(sys.modules, "yfinance", fake)
    assert research.days_to_earnings("ZZZZ", today=REF) is None
    assert research._EARNINGS_DAYS_CACHE.get("ZZZZ") is None
