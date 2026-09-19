"""Public API contract; production-derived narrative omitted."""
import asyncio
import json
from datetime import date
from types import SimpleNamespace

import pytest

import exitmgr.research as research

from exitmgr.research import (
    account_sizing_snapshot, build_brief, FOMC_DECISIONS_2026, momentum_stats,
    next_events, parse_rss_titles, with_account_sizing_snapshot,
)
from exitmgr.risk import OpenPosition


def test_same_symbol_contracts_render_independent_campaign_detail():
    book = [
        OpenPosition("SYMQ", 300.0, False, primary_con_id=101, leg_con_ids=(101,),
                     contracts=1, campaign_id="contract:101:campaign:1"),
        OpenPosition("SYMQ", 450.0, False, primary_con_id=202, leg_con_ids=(202,),
                     contracts=1, campaign_id="contract:202:campaign:2"),
    ]
    detail = {"contracts": {
        "101": {"campaign_seq": 1, "pnl_pct": 10.0, "thesis": "first thesis"},
        "202": {"campaign_seq": 2, "pnl_pct": -5.0, "thesis": "second thesis"},
    }, "by_symbol": {}}
    brief = build_brief(today="2026-09-18", quotes={}, universe=[], allow_any_name=False,
                        book=book, book_detail=detail, net_liq=10000.0,
                        available_funds=5000.0)
    assert "contract 101, campaign #1, +10.0% P&L" in brief
    assert "contract 202, campaign #2, -5.0% P&L" in brief
    assert "first thesis" in brief and "second thesis" in brief


def test_book_detail_keeps_two_same_symbol_contracts_instead_of_overwriting(tmp_path):
    from exitmgr.trader import Trader
    rows = [
        {"contract_id": 101, "symbol": "SYMQ", "right": "C", "expiry": "20261218",
         "strike": 200.0, "quantity": 1, "debit": 300.0,
         "ts": "2026-09-01T12:00:00+00:00", "thesis": "first"},
        {"contract_id": 202, "symbol": "SYMQ", "right": "C", "expiry": "20270115",
         "strike": 210.0, "quantity": 1, "debit": 450.0,
         "ts": "2026-09-02T12:00:00+00:00", "thesis": "second"},
    ]
    journal = tmp_path / "trades.log"
    journal.write_text("".join(json.dumps(row) + "\n" for row in rows))
    state = SimpleNamespace(mark_path={
        "101": [{"pnl_pct": 10.0}], "202": [{"pnl_pct": -5.0}]})
    fake = SimpleNamespace(
        journal_path=str(journal),
        exit_manager=SimpleNamespace(state_manager=SimpleNamespace(state=state)))
    positions = [
        OpenPosition("SYMQ", 300.0, False, primary_con_id=101, leg_con_ids=(101,)),
        OpenPosition("SYMQ", 450.0, False, primary_con_id=202, leg_con_ids=(202,)),
    ]
    details = Trader._book_detail(fake, positions)
    assert set(details["contracts"]) == {"101", "202"}
    assert details["contracts"]["101"]["thesis"] == "first"
    assert details["contracts"]["202"]["thesis"] == "second"




def test_momentum_basic():
    closes = [100.0] * 16 + [101, 102, 103, 104, 105.0]
    st = momentum_stats(closes)
    assert st is not None
    assert abs(st["last"] - 105.0) < 1e-9
    assert abs(st["ret_5d"] - 5.0) < 1e-9
    assert abs(st["ret_20d"] - 5.0) < 1e-9
    assert abs(st["from_high_pct"]) < 1e-9
    assert st["vol_20d_ann"] is not None and st["vol_20d_ann"] > 0


def test_momentum_filters_sentinels_and_short_history():
    assert momentum_stats([100.0, -1.0, float("nan"), 101.0]) is None
    assert momentum_stats([]) is None
    st = momentum_stats([-1.0] * 5 + [100, 101, 102, 103, 104, 105.0])
    assert st is not None and st["last"] == 105.0


def test_momentum_20d_none_when_history_short():
    st = momentum_stats([100, 101, 102, 103, 104, 105.0])
    assert st["ret_5d"] is not None and st["ret_20d"] is None


@pytest.mark.asyncio
async def test_price_structure_preserves_partial_results_and_warms_daily_cache(monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    class Contract:
        def __init__(self, symbol):
            self.symbol = symbol
            self.conId = abs(hash(symbol)) or 1

    class FakeIB:
        def __init__(self):
            self.phase = 1
            self.qualify_calls = []
            self.cancelled = set()

        async def qualifyContractsAsync(self, *contracts):
            self.qualify_calls.append([c.symbol for c in contracts])
            return list(contracts)

        async def reqHistoricalDataAsync(self, contract, **_kwargs):
            if self.phase == 1 and contract.symbol not in {"SPY", "QQQ", "IWM"}:
                try:
                    await asyncio.sleep(1)
                except asyncio.CancelledError:
                    self.cancelled.add(contract.symbol)
                    raise
            return [SimpleNamespace(close=float(100 + i)) for i in range(30)]

    import exitmgr.ibkr as ibkr
    monkeypatch.setattr(ibkr, "Stock", lambda symbol, *_args: Contract(symbol))
    monkeypatch.setattr(research, "PRICE_STRUCTURE_BUDGET_S", 0.03)
    monkeypatch.setattr(research, "_price_structure_trading_day", lambda: date(2026, 8, 31))
    research._PRICE_STRUCTURE_CACHE.clear()
    monkeypatch.setattr(research, "_PRICE_STRUCTURE_CACHE_DAY", None)

    ib = FakeIB()
    symbols = ["AAPL", "SPY", "MSFT", "QQQ", "IWM"]
    first = await research._price_structure(ib, symbols)
    assert set(first) == {"SPY", "QQQ", "IWM"}
    assert ib.cancelled == {"AAPL", "MSFT"}

    ib.phase = 2
    second = await research._price_structure(ib, symbols)
    assert set(second) == set(symbols)
    assert set(ib.qualify_calls[-1]) == {"AAPL", "MSFT"}

    brief = _full_brief(price_stats=first)
    assert "Price structure (daily closes):" in brief
    assert "  SPY:" in brief
    assert "Price structure (daily closes):\n  (unavailable this cycle)" not in brief


@pytest.mark.asyncio
async def test_price_structure_cache_invalidates_on_next_trading_day(monkeypatch):
    class Contract:
        def __init__(self, symbol):
            self.symbol = symbol
            self.conId = 1

    class FakeIB:
        def __init__(self):
            self.qualify_calls = 0

        async def qualifyContractsAsync(self, *contracts):
            self.qualify_calls += 1
            return list(contracts)

        async def reqHistoricalDataAsync(self, _contract, **_kwargs):
            return [SimpleNamespace(close=float(100 + i)) for i in range(30)]

    import exitmgr.ibkr as ibkr
    monkeypatch.setattr(ibkr, "Stock", lambda symbol, *_args: Contract(symbol))
    days = iter((date(2026, 8, 31), date(2026, 8, 31), date(2026, 9, 1)))
    monkeypatch.setattr(research, "_price_structure_trading_day", lambda: next(days))
    research._PRICE_STRUCTURE_CACHE.clear()
    monkeypatch.setattr(research, "_PRICE_STRUCTURE_CACHE_DAY", None)
    ib = FakeIB()

    assert (await research._price_structure(ib, ["SPY"]))["SPY"]
    assert (await research._price_structure(ib, ["SPY"]))["SPY"]
    assert ib.qualify_calls == 1
    assert (await research._price_structure(ib, ["SPY"]))["SPY"]
    assert ib.qualify_calls == 2




def test_next_fomc_within_horizon():
    ev = next_events(date(2026, 6, 12))
    assert any("FOMC" in e and "2026-06-17" in e and "(in 5d)" in e for e in ev)


def test_fomc_skipped_outside_horizon():
    ev = next_events(date(2026, 6, 12), fomc_dates=["2026-12-09"], horizon_days=45)
    assert not any("FOMC" in e for e in ev)


def test_earnings_events_and_bad_dates_ignored():
    ev = next_events(date(2026, 6, 12), earnings=[("NVDA", "2026-06-25"), ("AAPL", "garbage")])
    assert any("NVDA earnings 2026-06-25 (in 13d)" in e for e in ev)
    assert not any("AAPL" in e for e in ev)


def test_fomc_schedule_is_iso_dates():
    assert all(date.fromisoformat(d) for d in FOMC_DECISIONS_2026)




RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
<title>Yahoo Finance: SPY</title>
<item><title>Stocks rally on soft CPI</title></item>
<item><title>Fed officials split on cuts</title></item>
<item><title>Chip names extend gains</title></item>
<item><title>Fourth headline beyond limit</title></item>
</channel></rss>"""


def test_parse_rss_skips_channel_title_and_limits():
    t = parse_rss_titles(RSS, limit=3)
    assert t == ["Stocks rally on soft CPI", "Fed officials split on cuts", "Chip names extend gains"]


def test_parse_rss_garbage_returns_empty():
    assert parse_rss_titles("not xml at all") == []
    assert parse_rss_titles("") == []


def test_matches_blocked_sector():
    from exitmgr.research import matches_blocked_sector
    kw = ["biotech", "drug manufacturers", "pharmaceutical"]
    assert matches_blocked_sector("Biotechnology", "Healthcare", kw)
    assert matches_blocked_sector("Drug Manufacturers - General", "Healthcare", kw)
    assert not matches_blocked_sector("Semiconductors", "Technology", kw)
    assert not matches_blocked_sector(None, None, kw)
    assert not matches_blocked_sector("Biotechnology", "Healthcare", [])




def _full_brief(**over):
    kw = dict(
        today="2026-06-12",
        quotes={"SPY": {"last": 737.76, "change_pct": 0.4}},
        universe=["SPY", "QQQ", "IWM"],
        allow_any_name=True,
        net_liq=5000.0,
        available_funds=4000.0,
        price_stats={"SPY": momentum_stats([100.0] * 16 + [101, 102, 103, 104, 105.0])},
        vix=14.2,
        events=["FOMC rate decision 2026-06-17 (in 5d)"],
        headlines=["Stocks rally on soft CPI"],
        book=[OpenPosition("NVDA", 120.0, False)],
        day_pnl_pct=-0.012,
    )
    kw.update(over)
    return build_brief(**kw)


def test_brief_renders_all_sections():
    s = _full_brief()
    assert "Net liquidation value: $5,000.00" in s
    assert "Available funds: $4,000.00" in s
    assert "SPY: 737.76 (+0.40%)" in s
    assert "20d vol" in s
    assert "VIX: 14.2" in s
    assert "FOMC rate decision" in s
    assert "Stocks rally on soft CPI" in s
    assert "NVDA: ~$120 at risk (single name)" in s
    assert "Day P&L: -1.20%" in s
    assert "single name you have real conviction on" in s
    assert "do NOT assume" in s
    assert "may execute automatically without a human tap" in s
    assert "every entry still needs human approval" not in s


def test_brief_degrades_explicitly_when_sections_missing():
    s = build_brief(today="2026-06-12", quotes={}, universe=["SPY"],
                    allow_any_name=False, net_liq=5000.0, available_funds=0.0)
    assert "(quotes unavailable this cycle)" in s
    assert "(unavailable this cycle)" in s
    assert "VIX: unavailable" in s
    assert "no open positions" in s
    assert "single name you have real conviction on" not in s


def test_brief_marks_same_day_earnings_as_unresolved_before_inference(monkeypatch):
    monkeypatch.setattr(research, "days_to_earnings", lambda *a, **k: 0)
    s = _full_brief()
    assert "EARNINGS TODAY — SAME-DAY BINARY EVENT WARNING" in s
    assert "treat it as UPCOMING" in s
    assert "0d to earnings" not in s


def test_opening_setup_watchlist_context_is_optional_and_rendered_once():
    base = _full_brief()
    context = ("Persistent setup watchlist (context only; never an order or approval):\n"
               "  NVDA: bias=bullish | wait_for=pullback plus stabilization")
    with_watchlist = _full_brief(setup_watchlist_context=context)
    assert "Persistent setup watchlist" not in base
    assert with_watchlist.count("Persistent setup watchlist") == 1
    assert "NVDA: bias=bullish" in with_watchlist
    assert with_watchlist.index("Persistent setup watchlist") < with_watchlist.index("Current book:")


def test_account_snapshot_invalid_is_explicit_and_forbids_entry():
    s = account_sizing_snapshot(net_liq=float("nan"), available_funds=100.0)
    assert "UNAVAILABLE OR INVALID" in s
    assert "do not propose a new trade" in s
    assert "Net liquidation value:" not in s


def test_account_snapshot_refresh_replaces_instead_of_duplicates():
    old = _full_brief()
    new = with_account_sizing_snapshot(old, net_liq=2001.25, available_funds=999.50)
    assert new.count("Account sizing snapshot") == 1
    assert "Net liquidation value: $2,001.25" in new
    assert "Available funds: $999.50" in new
    assert "$5,000.00" not in new
