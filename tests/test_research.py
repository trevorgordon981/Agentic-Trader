"""Tests for the research brief: momentum math, event windows, RSS parsing, brief formatting."""
from datetime import date

from exitmgr.research import (
    momentum_stats, next_events, parse_rss_titles, build_brief, FOMC_DECISIONS_2026,
)
from exitmgr.risk import OpenPosition


# ---------------- momentum_stats

def test_momentum_basic():
    closes = [100.0] * 16 + [101, 102, 103, 104, 105.0]  # 21 closes, rising tail
    st = momentum_stats(closes)
    assert st is not None
    assert abs(st["last"] - 105.0) < 1e-9
    assert abs(st["ret_5d"] - 5.0) < 1e-9          # 100 -> 105
    assert abs(st["ret_20d"] - 5.0) < 1e-9         # 100 -> 105
    assert abs(st["from_high_pct"]) < 1e-9          # at the 20d high
    assert st["vol_20d_ann"] is not None and st["vol_20d_ann"] > 0


def test_momentum_filters_sentinels_and_short_history():
    assert momentum_stats([100.0, -1.0, float("nan"), 101.0]) is None  # too few usable
    assert momentum_stats([]) is None
    st = momentum_stats([-1.0] * 5 + [100, 101, 102, 103, 104, 105.0])  # sentinels dropped
    assert st is not None and st["last"] == 105.0


def test_momentum_20d_none_when_history_short():
    st = momentum_stats([100, 101, 102, 103, 104, 105.0])  # 6 closes: 5d ok, 20d not
    assert st["ret_5d"] is not None and st["ret_20d"] is None


# ---------------- next_events

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


# ---------------- parse_rss_titles

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
    assert not matches_blocked_sector("Biotechnology", "Healthcare", [])  # no keywords blocks nothing


# ---------------- build_brief

def _full_brief(**over):
    kw = dict(
        today="2026-06-12",
        quotes={"SPY": {"last": 737.76, "change_pct": 0.4}},
        universe=["SPY", "QQQ", "IWM"],
        allow_any_name=True,
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
    assert "SPY: 737.76 (+0.40%)" in s
    assert "20d vol" in s
    assert "VIX: 14.2" in s
    assert "FOMC rate decision" in s
    assert "Stocks rally on soft CPI" in s
    assert "NVDA: ~$120 at risk (single name)" in s
    assert "Day P&L: -1.20%" in s
    assert "single name you have real conviction on" in s
    assert "do NOT assume" in s


def test_brief_degrades_explicitly_when_sections_missing():
    s = build_brief(today="2026-06-12", quotes={}, universe=["SPY"], allow_any_name=False)
    assert "(quotes unavailable this cycle)" in s
    assert "(unavailable this cycle)" in s          # price structure
    assert "VIX: unavailable" in s
    assert "no open positions" in s
    assert "single name you have real conviction on" not in s
