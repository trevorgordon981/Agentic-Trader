"""Public API contract; production-derived narrative omitted."""
import sys
import types
from datetime import date

import pytest

import exitmgr.research as research
from exitmgr.entry_safety import NO_EARNINGS_ETFS

REF = date(2026, 7, 1)


@pytest.fixture(autouse=True)
def _clean_module_state():
    research._EARNINGS_DAYS_CACHE.clear()
    yield
    research._EARNINGS_DAYS_CACHE.clear()


def _install_fake_yfinance(monkeypatch, dates, seen=None):
    """Public API contract; production-derived narrative omitted."""
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
            if seen is not None:
                seen.append(sym)

        def get_earnings_dates(self, limit=12):
            return _DF(dates)

    fake = types.ModuleType("yfinance")
    fake.Ticker = _Ticker
    monkeypatch.setitem(sys.modules, "yfinance", fake)


def test_index_underlying_short_circuits_none():

    sym = next(iter(research.INDEX_UNDERLYINGS))
    assert research.days_to_earnings(sym, today=REF) is None


def test_fallback_to_yfinance_when_cache_empty(monkeypatch):

    _install_fake_yfinance(monkeypatch, [date(2026, 7, 21)])
    assert "AAPL" not in research._EARNINGS_DAYS_CACHE
    assert research.days_to_earnings("AAPL", today=REF) == 20


def test_wsh_cache_hit_preferred_over_yfinance(monkeypatch):

    research._EARNINGS_DAYS_CACHE["AAPL"] = 7
    _install_fake_yfinance(monkeypatch, [date(2026, 8, 30)])
    assert research.days_to_earnings("AAPL", today=REF) == 7


def test_per_run_cache(monkeypatch):

    _install_fake_yfinance(monkeypatch, [date(2026, 7, 21)])
    assert research.days_to_earnings("AAPL", today=REF) == 20
    _install_fake_yfinance(monkeypatch, [date(2026, 7, 5)])
    assert research.days_to_earnings("AAPL", today=REF) == 20


def test_money_boundary_force_refresh_bypasses_proposal_cache(monkeypatch):

    _install_fake_yfinance(monkeypatch, [date(2026, 7, 2)])
    assert research.days_to_earnings("AAPL", today=REF) == 1
    _install_fake_yfinance(monkeypatch, [date(2026, 7, 1)])
    assert research.days_to_earnings("AAPL", today=REF, force_refresh=True) == 0
    assert research._EARNINGS_DAYS_CACHE["AAPL"] == 0


def test_forced_fallback_failure_retains_authoritative_wsh_day_zero(monkeypatch):
    research._EARNINGS_DAYS_CACHE["AAPL"] = 0

    def _boom(*a, **k):
        raise RuntimeError("fallback feed unavailable")

    fake = types.ModuleType("yfinance")
    fake.Ticker = _boom
    monkeypatch.setitem(sys.modules, "yfinance", fake)
    assert research.days_to_earnings("AAPL", today=REF, force_refresh=True) == 0


def test_display_horizon_does_not_hide_raw_next_event(monkeypatch):

    _install_fake_yfinance(monkeypatch, [date(2026, 12, 31)])
    assert research.days_to_earnings("AAPL", today=REF, horizon_days=90) is None
    assert research.days_to_earnings("AAPL", today=REF) == 183


def test_transient_none_is_retryable_and_never_raises(monkeypatch):

    def _boom(*a, **k):
        raise RuntimeError("yfinance exploded")

    fake = types.ModuleType("yfinance")
    fake.Ticker = _boom
    monkeypatch.setitem(sys.modules, "yfinance", fake)
    assert research.days_to_earnings("ZZZZ", today=REF) is None
    assert "ZZZZ" not in research._EARNINGS_DAYS_CACHE
    _install_fake_yfinance(monkeypatch, [date(2026, 7, 21)])
    assert research.days_to_earnings("ZZZZ", today=REF) == 20


def test_single_stock_leveraged_etf_inherits_reference_equity_earnings(monkeypatch):
    seen = []
    _install_fake_yfinance(monkeypatch, [date(2026, 7, 21)], seen)
    assert research.earnings_reference_symbol("nvdx") == "NVDA"
    assert research.days_to_earnings("NVDX", today=REF) == 20
    assert seen == ["NVDA"]
    assert research._EARNINGS_DAYS_CACHE["NVDA"] == 20
    assert "NVDX" not in research._EARNINGS_DAYS_CACHE


def test_proxy_and_no_earnings_registries_are_disjoint():
    assert set(research.EARNINGS_PROXY_SYMBOLS).isdisjoint(NO_EARNINGS_ETFS)


def test_proxy_reads_reference_equity_wsh_cache_without_yfinance(monkeypatch):
    research._EARNINGS_DAYS_CACHE["NVDA"] = 7
    fake = types.ModuleType("yfinance")
    fake.Ticker = lambda *_a, **_k: pytest.fail("yfinance called despite WSH proxy cache")
    monkeypatch.setitem(sys.modules, "yfinance", fake)
    assert research.days_to_earnings("NVDX", today=REF) == 7
