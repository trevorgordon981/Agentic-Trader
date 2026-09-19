from __future__ import annotations

import json
import tempfile
import urllib.error
from pathlib import Path

import pytest

from exitmgr import apewisdom as aw


def fixture():
    return {
        "count": 4, "pages": 1, "current_page": 1,
        "results": [
            {"rank": "1", "ticker": "MUTX", "name": "Micron Technology",
             "mentions": "450", "upvotes": "1000", "rank_24h_ago": "4",
             "mentions_24h_ago": "200"},
            {"rank": 2, "ticker": "SPY", "name": "SPDR S&amp;P 500 ETF",
             "mentions": 300, "upvotes": 900, "rank_24h_ago": 3,
             "mentions_24h_ago": 280},
            {"rank": 3, "ticker": "TSLA", "name": "Tesla",
             "mentions": 250, "upvotes": 500, "rank_24h_ago": 2,
             "mentions_24h_ago": 300},
            {"rank": 4, "ticker": "SYMD", "name": "SpaceX tracker",
             "mentions": 200, "upvotes": 400, "rank_24h_ago": 10,
             "mentions_24h_ago": None},
        ],
    }


def trend_rows():
    values = [round(i * 100 / 30) for i in range(31)]
    return {ticker: {"table_rank": rank, "mention_trend_30d": values}
            for rank, ticker in enumerate(("MUTX", "SPY", "TSLA", "SYMD"), 1)}


def trend_html(ticker="MUTX", rank=1):
    coords = " ".join(f"{x},{41 - (x // 5) % 39}" for x in range(0, 151, 5))
    return f'''<table id="default-table"><thead><tr><th>Trend (30 days)</th></tr></thead>
    <tbody><tr><td></td><td data-sort="{rank}">{rank}</td>
    <td><a href="/stocks/{ticker}/">Name</a></td><td>{ticker}</td><td></td><td></td>
    <td class="sparkline-td"><svg><path d="M-1,60 {coords} 165,50"></path></svg></td>
    <td></td></tr></tbody></table>'''


def normalized():
    return aw.normalize_payload(fixture(), "all-stocks", 100, trend_rows())


def test_normalizes_types_entities_and_attention_contract():
    data = normalized()
    assert [r["ticker"] for r in data["results"]] == ["MUTX", "SPY", "TSLA", "SYMD"]
    assert data["results"][1]["name"] == "SPDR S&P 500 ETF"
    assert set(data["results"][0]) == {
        "rank", "ticker", "name", "mention_trend_30d", "mention_trend_30d_sparkline"}
    assert len(data["results"][0]["mention_trend_30d"]) == 31
    assert data["observed_window_days"] == 30
    assert data["signal_type"] == "attention_only"
    assert data["sentiment_direction"] is None
    assert data["trade_authority"] is False
    assert data["training_eligible"] is False


def test_parses_exact_31_point_table_sparkline_and_rejects_drift():
    rows = aw.parse_trend_table(trend_html())
    assert rows["MUTX"]["table_rank"] == 1
    assert len(rows["MUTX"]["mention_trend_30d"]) == 31
    assert aw.trend_sparkline(rows["MUTX"]["mention_trend_30d"]) != "n/a"
    with pytest.raises(aw.ApeWisdomError):
        aw.parse_trend_table("<table>no trend contract</table>")


def test_initial_filter_blocks_configured_and_tsla_but_allows_SYMD_exception():
    rows = aw.initial_rows(normalized(), blocked={"MUTX"})
    assert [r["ticker"] for r in rows] == ["SPY", "SYMD"]


@pytest.mark.parametrize(
    "patch,reason",
    [
        ({"quote_type": "CRYPTOCURRENCY"}, "not_equity_or_etf"),
        ({"currency": "CAD"}, "not_usd"),




        ({"price": 1.49}, "price_below_1.5"),
        ({"price": 0.0}, "price_below_1.5"),
        ({"price": float("nan")}, "price_below_1.5"),
        ({"price": float("inf")}, "price_below_1.5"),
        ({"average_volume": 999_999}, "average_volume_below_1000000"),
        ({"industry": "", "sector": ""}, "sector_unknown"),
        ({"industry": "Biotechnology"}, "blocked_sector"),
    ],
)
def test_profile_gate_fails_closed(patch, reason):
    profile = {"quote_type": "EQUITY", "currency": "USD", "price": 50.0,
               "average_volume": 4_000_000, "industry": "Semiconductors",
               "sector": "Technology"}
    profile.update(patch)
    assert aw.profile_eligible(profile, ["biotech", "pharmaceutical"]) == (False, reason)


@pytest.mark.parametrize("ticker,price,volume", [
    ("PLUG", 2.09, 25_000_000),
    ("VUZI", 2.29, 3_500_000),
    ("BBAI", 2.76, 40_000_000),
    ("ACHR", 4.77, 30_000_000),
    ("SYMO", 15.14, 12_000_000),
    ("SOFI", 16.46, 45_000_000),
])
def test_affordable_small_names_now_pass(ticker, price, volume):
    """Public API contract; production-derived narrative omitted."""
    profile = {"quote_type": "EQUITY", "currency": "USD", "price": price,
               "average_volume": volume, "industry": "Semiconductors",
               "sector": "Technology"}
    assert aw.profile_eligible(profile, ["biotech", "pharmaceutical"]) == (True, "eligible")


def test_profile_gate_accepts_liquid_us_equity():
    profile = {"quote_type": "EQUITY", "currency": "USD", "price": 50.0,
               "average_volume": 4_000_001, "industry": "Semiconductors",
               "sector": "Technology"}
    assert aw.profile_eligible(profile, ["biotech"])[0] is True


def test_rank_metrics_cannot_change_main_research_universe():
    rows_a = normalized()["results"][:2]
    rows_b = [dict(rows_a[1], rank=1, mention_trend_30d=list(reversed(rows_a[1]["mention_trend_30d"]))),
              dict(rows_a[0], rank=2, mention_trend_30d=list(reversed(rows_a[0]["mention_trend_30d"])))]
    assert aw.merge_research_universe(["SPY", "QQQ", "AAPL"], rows_a) == \
           aw.merge_research_universe(["SPY", "QQQ", "AAPL"], rows_b)
    assert aw.merge_research_universe(["SPY", "QQQ", "AAPL"], rows_a) == \
           ["SPY", "QQQ", "AAPL", "MUTX"]


def test_attention_metrics_exist_only_in_separate_discovery_context():
    main_brief = "Independent quote and technical brief for MUTX and SPY"
    discovery = aw.discovery_context(
        main_brief, normalized()["results"], watched={"SPY"},
        price_stats={"MUTX": {"ret_20d": 12.34}})
    assert main_brief == "Independent quote and technical brief for MUTX and SPY"
    assert "ApeWisdom" not in main_brief
    assert "mentions" not in main_brief
    assert "ApeWisdom candidate pool" in discovery
    assert "NOT sentiment" in discovery
    assert "MUTX (Micron Technology): ApeWisdom rank #1" in discovery
    assert "30d mention shape" in discovery
    assert "20 sessions) +12.3%" in discovery
    assert "24h" not in discovery and "upvotes" not in discovery
    assert "SPY (" not in discovery


def test_review_binding_drops_off_pool_watched_and_duplicates():
    reviewed = [("MUTX", "model prose"), ("FAKE", "hallucinated"),
                ("SPY", "watched"), ("MUTX", "duplicate")]
    bound = aw.bind_reviewed_candidates(
        reviewed, normalized()["results"], watched={"SPY"},
        price_stats={"MUTX": {"ret_20d": -3.21}})
    assert [t for t, _ in bound] == ["MUTX"]
    assert "not a buy signal" in bound[0][1]
    assert "rank #1" in bound[0][1]
    assert "20 sessions) -3.2%" in bound[0][1]
    assert "24h" not in bound[0][1] and "upvotes" not in bound[0][1]
    assert "model prose" not in bound[0][1]


def test_fresh_cache_and_bounded_stale_fallback():
    with tempfile.TemporaryDirectory() as td:
        calls = []
        first = aw.load_trends(cache_dir=Path(td), now=1000,
                               fetch_json=lambda url: calls.append(url) or fixture(),
                               fetch_text=lambda _url: trend_html())
        cached = aw.load_trends(cache_dir=Path(td), now=1100,
                                fetch_json=lambda _url: pytest.fail("network called"))
        stale = aw.load_trends(
            cache_dir=Path(td), now=3000,
            fetch_text=lambda _url: trend_html(),
            fetch_json=lambda _url: (_ for _ in ()).throw(urllib.error.URLError("offline")))
        assert len(calls) == 1 and first["cache_hit"] is False
        assert cached["cache_hit"] is True and cached["stale"] is False
        assert stale["stale"] is True and stale["age_seconds"] == 2000


def test_expired_cache_and_schema_drift_fail_closed():
    bad_trends = {}
    with pytest.raises(aw.ApeWisdomError):
        aw.normalize_payload(fixture(), "all-stocks", 100, bad_trends)
    with tempfile.TemporaryDirectory() as td:
        aw.load_trends(cache_dir=Path(td), now=1000, fetch_json=lambda _url: fixture(),
                       fetch_text=lambda _url: trend_html())
        with pytest.raises(aw.ApeWisdomError):
            aw.load_trends(
                cache_dir=Path(td), now=1000 + aw.MAX_STALE_SECONDS + 1,
                fetch_text=lambda _url: trend_html(),
                fetch_json=lambda _url: (_ for _ in ()).throw(urllib.error.URLError("offline")))


def test_module_has_no_order_or_watchlist_authority():
    source = Path(aw.__file__).read_text()
    assert "placeOrder" not in source
    assert "_append_watchlist" not in source
    assert "TradeIdea" not in source
