from pathlib import Path

from exitmgr import setup_watchlist as sw


ROOT = Path(__file__).resolve().parents[1]


def test_mutable_markdown_lives_outside_git_repo():
    assert not Path(sw.DEFAULT_PATH).resolve().is_relative_to(ROOT.resolve())


def test_opening_slate_loads_targets_before_market_research_and_maintains_discoveries():
    source = (ROOT / "daily_recommend.py").read_text()
    load = source.index("_setup_snapshot = setup_watchlist.read_watchlist")
    quote = source.index("quotes = await fetch_universe_quotes", load)
    gather = source.index("data = await research.gather", quote)
    brief = source.index("setup_watchlist_context=", gather)
    model = source.index("intents, _raw_slate", brief)
    assert load < quote < gather < brief < model
    assert "setup_watchlist.add_target(" in source
    assert "add_suggest_deferred_to_opening_watchlist" in source


def test_approved_names_can_become_targets_and_cannot_trade_same_opening():
    source = (ROOT / "daily_recommend.py").read_text()
    discovery = source.index("broad_cands = await _await_slate_model(")
    defer = source.index("_defer_this_opening |= set(disc_cands)", discovery)
    propose = source.index("_res = await _await_slate_model(", defer)
    filter_intents = source.index("opening_setup_target_deferred", propose)
    materialize = source.index("_materialize_stage_b(", filter_intents)
    assert "exclude=set(_setup_symbols) | set(_core) | _ape_names" in source[discovery:defer]
    assert discovery < defer < propose < filter_intents < materialize
    assert "if _intent_symbol in _defer_this_opening" in source[propose:materialize]


def test_brief_renders_only_targets_in_the_fresh_data_universe():
    source = (ROOT / "daily_recommend.py").read_text()
    fresh = source.index("_fresh_setup_symbols =")
    filtered = source.index("_setup_targets_for_brief =", fresh)
    brief = source.index("setup_watchlist_context=", filtered)
    assert "targets=_setup_targets_for_brief" in source[brief:brief + 500]


def test_final_nbbo_refresh_carries_earnings_disclosure_fields():
    source = (ROOT / "daily_recommend.py").read_text()
    refresh = source.index("carry_intended_hold(_latest_r")
    gate = source.index("exact_spread_order_gate(", refresh)
    between = source[refresh:gate]
    assert "_latest_r.earnings_date" in between
    assert "_latest_r.earnings_warn" in between
    assert "_latest_r.earnings_unchecked" in between


def test_setup_watchlist_never_enters_continuous_auto_approval_route():
    trader_source = (ROOT / "exitmgr" / "trader.py").read_text()
    watch_source = (ROOT / "exitmgr" / "setup_watchlist.py").read_text()
    assert "setup_watchlist" not in trader_source
    assert "placeOrder" not in watch_source
    assert "_submit_order" not in watch_source
