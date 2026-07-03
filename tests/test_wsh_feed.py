"""Tests for the Wall Street Horizon (WSH) earnings + ex-dividend feed in exitmgr/research.py.

WSH is the PRIMARY source (IBKR API: reqWshMetaData once, then reqWshEventData per contract),
prefetched ASYNC in gather()/prefetch_wsh_events into the per-run caches so the in-loop SYNC
days_to_earnings / days_to_ex_dividend readers just read cache; yfinance is the FALLBACK on a cache
miss. No network / no live IBKR here: the pure parser is tested directly and the prefetch is driven
with a FAKE ib whose async WSH accessors return the hand-built fixture below.

The WSH JSON fixture is REPRESENTATIVE and HAND-CONSTRUCTED (NOT a live capture). WSH corporate
events arrive as a nested {"data":[{event...}]} of typed events keyed off an event_type/category
plus ISO date fields. Live validation against the real reqWshMetaData/reqWshEventData shape is
DEFERRED until the IB gateway is back up (see ~/RUN-WHEN-TRAINED.md).
"""
import asyncio
import sys
import types
from datetime import date

import pytest

import exitmgr.research as research

REF = date(2026, 7, 1)

# --- Representative WSH reqWshEventData JSON (constructed, NOT a live capture) --------------
# earnings (announce_date) 2026-07-31 => 30d; a later 2026-10-30 (>90d) and a past 2026-04-30 must
# both drop out. The dividend/ex-div event carries ex_date 2026-07-20 => 19d, plus a pay_date
# 2026-08-05 and record_date that must NOT be chosen (the ex-date field is preferred). The fiscal
# period-end date on the earnings event must be ignored (metadata, not an event date).
WSH_JSON = '''{
  "meta": {"source": "Wall Street Horizon"},
  "data": [
    {"event_id": "e1", "event_type": "Earnings per Share (EPS) - Confirmed",
     "wsh_event_status": "Confirmed", "announce_date": "2026-07-31",
     "fiscal_period_end_date": "2026-06-27", "currency": "USD"},
    {"event_id": "e2", "event_type": "Earnings per Share (EPS) - Confirmed",
     "announce_date": "2026-10-30"},
    {"event_id": "e3", "event_type": "Earnings per Share (EPS) - Reported",
     "announce_date": "2026-04-30"},
    {"event_id": "d1", "event_type": "Dividend", "category": "Ex-Dividend",
     "ex_date": "2026-07-20", "pay_date": "2026-08-05", "record_date": "2026-07-21",
     "amount": 0.25}
  ]
}'''

WSH_PAST_ONLY = '''{"data": [
  {"event_type": "Earnings per Share (EPS)", "announce_date": "2026-01-15"},
  {"event_type": "Dividend", "ex_date": "2026-02-01"}
]}'''


@pytest.fixture(autouse=True)
def _clean_caches():
    research._EARNINGS_DAYS_CACHE.clear()
    research._EX_DIV_DAYS_CACHE.clear()
    yield
    research._EARNINGS_DAYS_CACHE.clear()
    research._EX_DIV_DAYS_CACHE.clear()


# ---------------------------------------------------------------- pure parser

def test_parser_extracts_earnings_and_exdiv():
    assert research._parse_wsh_events(WSH_JSON, today=REF) == {"earnings_days": 30, "ex_div_days": 19}


def test_parser_prefers_ex_date_over_pay_date():
    # 19 (ex_date 2026-07-20), never 35 (pay_date 2026-08-05) or 20 (record_date 2026-07-21)
    assert research._parse_wsh_events(WSH_JSON, today=REF)["ex_div_days"] == 19


def test_parser_ignores_fiscal_period_date():
    # earnings resolves to announce_date (30), not the fiscal_period_end_date (2026-06-27, past)
    assert research._parse_wsh_events(WSH_JSON, today=REF)["earnings_days"] == 30


def test_parser_beyond_horizon_none():
    assert research._parse_wsh_events(WSH_JSON, today=REF, horizon_days=10) == \
        {"earnings_days": None, "ex_div_days": None}


def test_parser_past_events_none():
    assert research._parse_wsh_events(WSH_PAST_ONLY, today=REF) == \
        {"earnings_days": None, "ex_div_days": None}


def test_parser_malformed_empty_none():
    for bad in ("{not valid json", "", None, "[]", "{}"):
        assert research._parse_wsh_events(bad, today=REF) == \
            {"earnings_days": None, "ex_div_days": None}


def test_parser_picks_soonest_earnings():
    payload = ('{"data":[{"event_type":"Earnings","announce_date":"2026-08-30"},'
               '{"event_type":"Earnings","announce_date":"2026-07-31"}]}')
    assert research._parse_wsh_events(payload, today=REF)["earnings_days"] == 30


# ---------------------------------------------------------------- prefetch (fake ib, no network)

class _FakeIB:
    """Minimal stand-in for the ib_async IB handle: qualifies a stock to a conId and returns the WSH
    fixture from the async WSH accessors. `naming` selects the getWsh*/reqWsh* method spelling to
    prove the prefetch adapts to either ib_async accessor name."""

    def __init__(self, payload, conid=265598, naming="get"):
        self._payload = payload
        self._conid = conid
        meta_name = "getWshMetaDataAsync" if naming == "get" else "reqWshMetaDataAsync"
        event_name = "getWshEventDataAsync" if naming == "get" else "reqWshEventDataAsync"
        setattr(self, meta_name, self._meta)
        setattr(self, event_name, self._event)

    async def qualifyContractsAsync(self, *contracts):
        for c in contracts:
            try:
                c.conId = self._conid
            except Exception:
                pass
        return list(contracts)

    async def _meta(self):
        return '{"types": []}'

    async def _event(self, data):
        return self._payload


def test_prefetch_populates_both_caches():
    ib = _FakeIB(WSH_JSON)
    asyncio.run(research.prefetch_wsh_events(ib, ["AAPL"], today=REF))
    assert research._EARNINGS_DAYS_CACHE.get("AAPL") == 30
    assert research._EX_DIV_DAYS_CACHE.get("AAPL") == 19


def test_prefetch_adapts_to_reqwsh_naming():
    ib = _FakeIB(WSH_JSON, naming="req")
    asyncio.run(research.prefetch_wsh_events(ib, ["AAPL"], today=REF))
    assert research._EARNINGS_DAYS_CACHE.get("AAPL") == 30
    assert research._EX_DIV_DAYS_CACHE.get("AAPL") == 19


def test_prefetch_never_raises_on_bad_handle():
    asyncio.run(research.prefetch_wsh_events(None, ["AAPL"], today=REF))
    asyncio.run(research.prefetch_wsh_events(_FakeIB(WSH_JSON), [], today=REF))

    class _Broken:
        async def qualifyContractsAsync(self, *a):
            raise RuntimeError("nope")

        async def getWshMetaDataAsync(self):
            return "{}"

        async def getWshEventDataAsync(self, data):
            return WSH_JSON

    asyncio.run(research.prefetch_wsh_events(_Broken(), ["AAPL"], today=REF))
    assert research._EARNINGS_DAYS_CACHE == {}
    assert research._EX_DIV_DAYS_CACHE == {}


def test_prefetch_index_names_skipped():
    idx = next(iter(research.INDEX_UNDERLYINGS))
    asyncio.run(research.prefetch_wsh_events(_FakeIB(WSH_JSON), [idx], today=REF))
    assert idx not in research._EARNINGS_DAYS_CACHE
    assert idx not in research._EX_DIV_DAYS_CACHE


# ---------------------------------------------------------------- WSH cache HIT vs yfinance MISS

def _install_fake_yfinance(monkeypatch, earnings_dates):
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
            return _DF(earnings_dates)

        @property
        def calendar(self):
            return {"Ex-Dividend Date": None}

    fake = types.ModuleType("yfinance")
    fake.Ticker = _Ticker
    monkeypatch.setitem(sys.modules, "yfinance", fake)


def _install_exploding_yfinance(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("yfinance must NOT be called on a WSH cache hit")

    fake = types.ModuleType("yfinance")
    fake.Ticker = _boom
    monkeypatch.setitem(sys.modules, "yfinance", fake)


def test_cache_hit_returns_wsh_value_without_yfinance(monkeypatch):
    # simulate a completed WSH prefetch, then prove the sync readers hit cache and never call yfinance
    research._EARNINGS_DAYS_CACHE["AAPL"] = 30
    research._EX_DIV_DAYS_CACHE["AAPL"] = 19
    _install_exploding_yfinance(monkeypatch)
    assert research.days_to_earnings("AAPL", today=REF) == 30
    assert research.days_to_ex_dividend("AAPL", today=REF) == 19


def test_cache_miss_falls_back_to_yfinance(monkeypatch):
    # empty caches (WSH gave nothing) => sync readers use yfinance, identical to the pre-WSH path
    _install_fake_yfinance(monkeypatch, [date(2026, 7, 21)])
    assert "AAPL" not in research._EARNINGS_DAYS_CACHE
    assert research.days_to_earnings("AAPL", today=REF) == 20


def test_prefetch_then_sync_read_end_to_end(monkeypatch):
    # full path: prefetch populates caches from the fixture, then the SYNC readers return WSH values
    # with yfinance guaranteed uncalled (proves the async-prefetch -> sync-read design).
    asyncio.run(research.prefetch_wsh_events(_FakeIB(WSH_JSON), ["AAPL"], today=REF))
    _install_exploding_yfinance(monkeypatch)
    assert research.days_to_earnings("AAPL", today=REF) == 30
    assert research.days_to_ex_dividend("AAPL", today=REF) == 19
