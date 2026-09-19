"""Public API contract; production-derived narrative omitted."""
import asyncio
from types import SimpleNamespace

import pytest

import daily_recommend as dr
from exitmgr import risk
from exitmgr import trader as trader_mod

POT = 1000.0


LIMITS = risk.RiskLimits(
    max_sector_agg_pct=0.25,
    sector_map={"NVDA": "semis", "AMD": "semis", "MUTX": "semis"},
)



def _pos(sym, notional, is_index=False):
    return risk.OpenPosition(sym, notional, is_index)


def notes(open_pos, sym, debit, is_index=False, pot=POT, limits=LIMITS):
    return dr._concentration_notes(open_pos, sym, debit, is_index, pot, limits)


def _kinds(ns):
    return {kw["kind"] for _txt, kw in ns}


def test_third_correlated_name_surfaces_sector_warning():


    book = [_pos("NVDA", 100.0), _pos("AMD", 100.0)]
    ns = notes(book, "MUTX", 100.0)
    assert _kinds(ns) == {"sector_agg"}, ns
    txt, kw = ns[0]
    assert kw["sector"] == "semis"
    assert kw["exposure"] == 300.0 and kw["cap"] == 250.0
    assert "concentration" in txt and "semis" in txt


def test_uncorrelated_under_cap_idea_no_warning():

    book = [_pos("NVDA", 100.0)]
    assert notes(book, "KO", 100.0) == []

    assert notes([_pos("NVDA", 50.0)], "AMD", 50.0) == []


def test_single_name_agg_breach_surfaces_that_warning():



    limits = risk.RiskLimits(max_sector_agg_pct=0.0, sector_map={})
    book = [_pos("WMT", 300.0), _pos("KO", 900.0)]
    ns = notes(book, "WMT", 100.0, limits=limits)
    assert _kinds(ns) == {"single_name_agg"}, ns
    _txt, kw = ns[0]
    assert kw["exposure"] == 400.0 and kw["cap"] == 360.0


def test_both_caps_can_fire_together():

    book = [_pos("NVDA", 300.0), _pos("AMD", 100.0)]
    ns = notes(book, "NVDA", 100.0)
    assert _kinds(ns) == {"sector_agg", "single_name_agg"}, ns


def test_index_candidate_is_exempt():

    book = [_pos("KO", 300.0), _pos("PEP", 300.0)]
    assert notes(book, "SPY", 500.0, is_index=True) == []


def test_index_positions_excluded_from_aggregates():

    book = [_pos("SPY", 100000.0, is_index=True), _pos("NVDA", 100.0)]
    assert notes(book, "MUTX", 100.0) == []


def test_empty_sector_map_makes_sector_check_a_noop():
    lim = risk.RiskLimits(max_sector_agg_pct=0.25, sector_map={})
    book = [_pos("NVDA", 200.0), _pos("AMD", 200.0)]
    ns = notes(book, "MUTX", 100.0, limits=lim)
    assert ns == []


def test_unrelated_positions_never_create_a_false_single_name_warning():



    lim = risk.RiskLimits(max_sector_agg_pct=0.0, sector_map={})
    book = [_pos("SYMZ", 600.0), _pos("GOOG", 500.0), _pos("TPR", 400.0)]
    assert notes(book, "SYMS", 250.0, limits=lim) == []


def test_never_raises_and_never_blocks_on_edge_inputs():


    assert notes([], "NVDA", 100.0, pot=0.0) == []
    assert notes([], "NVDA", 0.0) == []
    assert notes([], None, 100.0) == []


def test_surface_only_returns_data_never_mutates_book():

    book = [_pos("NVDA", 200.0), _pos("AMD", 200.0)]
    before = [(p.underlying, p.notional, p.is_index) for p in book]
    notes(book, "MUTX", 100.0)
    after = [(p.underlying, p.notional, p.is_index) for p in book]
    assert before == after


def _eligible(names, **overrides):
    base = dict(
        quote_prices={str(n).strip().upper(): 100.0 for n in names},
        research_symbols={str(n).strip().upper() for n in names},
        net_liq=1000.0,
        available_funds=1000.0,
        positions=(),
        pot_day_start=1000.0,
        approved_names={str(n).strip().upper() for n in names},
        limits=risk.RiskLimits(
            max_concurrent=20, max_sector_agg_pct=0.0, sector_map={},
            allow_any_name=True, cash_buffer_pct=0.0),
    )
    base.update(overrides)
    return trader_mod.entry_eligible_universe(names, **base)


def test_entry_eligible_universe_requires_finite_quote_and_research():
    eligible, rejected = _eligible(
        ["SYMS", "SYMZ", "GOOG", "HPQ"],
        quote_prices={"SYMS": 100.0, "SYMZ": float("nan"),
                      "GOOG": float("inf"), "HPQ": 30.0},
        research_symbols={"SYMS", "SYMZ", "GOOG"})
    assert eligible == ("SYMS",)
    assert rejected["SYMZ"] == ("no usable live underlying quote",)
    assert rejected["GOOG"] == ("no usable live underlying quote",)
    assert rejected["HPQ"] == ("daily price research unavailable",)


def test_entry_eligible_universe_snapshots_generator_backed_position_book_once():


    limits = risk.RiskLimits(
        max_concurrent=1, max_sector_agg_pct=0.0, sector_map={},
        allow_any_name=True, cash_buffer_pct=0.0)
    eligible, rejected = _eligible(
        ["SYMS", "SYMZ"], positions=(_pos("HPQ", 100.0) for _ in range(1)), limits=limits)
    assert eligible == ()
    assert all(any("max concurrent" in reason for reason in rejected[symbol])
               for symbol in ("SYMS", "SYMZ"))


def test_entry_eligible_universe_snapshots_generator_backed_approved_names_once():
    limits = risk.RiskLimits(
        max_concurrent=20, max_sector_agg_pct=0.0, sector_map={},
        allow_any_name=False, cash_buffer_pct=0.0)
    eligible, rejected = _eligible(
        ["SYMS", "SYMZ"], approved_names=(s for s in ("SYMS", "SYMZ")), limits=limits)
    assert eligible == ("SYMS", "SYMZ")
    assert rejected == {}


def test_entry_eligible_universe_is_per_name_and_stably_deduplicated():
    limits = risk.RiskLimits(
        max_concurrent=20, max_single_name_agg_pct=0.36,
        max_sector_agg_pct=0.0, sector_map={}, allow_any_name=True,
        cash_buffer_pct=0.0)


    eligible, rejected = _eligible(
        ["SYMS", "SYMS", "SYMZ"], positions=[_pos("HPQ", 900.0), _pos("SYMS", 360.0)],
        limits=limits)
    assert eligible == ("SYMZ",)
    assert len(rejected) == 1 and "SYMS" in rejected
    assert any("single-name exposure" in reason for reason in rejected["SYMS"])


def test_entry_eligible_universe_does_not_arm_an_unconfigured_external_book():
    limits = risk.RiskLimits(
        max_concurrent=20, max_single_name_agg_pct=0.36,
        max_sector_agg_pct=0.0, sector_map={}, allow_any_name=True,
        cash_buffer_pct=0.0, external_book_path=None)



    eligible, rejected = _eligible(
        ["SYMS"], positions=[_pos("SYMS", 100.0)], limits=limits)
    assert eligible == ("SYMS",)
    assert rejected == {}


def test_entry_eligible_universe_autonomous_filter_keeps_index_exemption():
    limits = risk.RiskLimits(
        max_concurrent=20, max_sector_agg_pct=0.25,
        sector_map={"SYMS": "energy"}, allow_any_name=True, cash_buffer_pct=0.0)
    eligible, rejected = _eligible(
        ["SYMS", "ZZZZ", "SPY"], approved_names={"SYMS"}, limits=limits,
        require_autonomous=True)
    assert eligible == ("SYMS", "SPY")
    assert any("approved_names" in reason for reason in rejected["ZZZZ"])
    assert any("sector_map" in reason for reason in rejected["ZZZZ"])


def test_entry_eligible_universe_captures_external_book_once(monkeypatch):
    calls = []
    snapshot = risk.ExternalBook.not_supplied()

    def _load(limits):
        calls.append(limits)
        return snapshot

    monkeypatch.setattr(trader_mod, "external_book_for", _load)
    limits = risk.RiskLimits(
        max_concurrent=20, max_sector_agg_pct=0.0, sector_map={},
        allow_any_name=True, cash_buffer_pct=0.0,
        external_book_path="/configured/external-book.json")
    eligible, rejected = _eligible(["SYMS", "SYMZ"], limits=limits)
    assert eligible == ("SYMS", "SYMZ") and rejected == {}
    assert len(calls) == 1


def test_slate_never_flattens_an_unreadable_book_to_empty():
    source = __import__("inspect").getsource(dr.run)
    assert "positions=_slate_book or ()" not in source
    assert 'reason="book_unreadable: " + reason' in source


def test_slate_prefilter_counts_a_working_buy_before_calling_strategist(monkeypatch, tmp_path):
    trade = SimpleNamespace(
        order=SimpleNamespace(action="BUY", lmtPrice=2.0, totalQuantity=1),
        orderStatus=SimpleNamespace(status="Submitted"),
        contract=SimpleNamespace(symbol="HPQ", conId=901))

    async def _view(_ib, _audit):
        return SimpleNamespace(readable=True, trades=(trade,), error=None)

    calls = []
    class _Conn:
        async def get_positions(self, *, include_short=False):
            calls.append(include_short)
            assert include_short is True
            return {}

    monkeypatch.setattr(dr, "broker_entry_order_view", _view)
    journal = tmp_path / "trades.log"
    journal.write_text("")
    monkeypatch.setattr(dr, "JOURNAL_PATH", str(journal))
    book = asyncio.run(dr._prefilter_positions_for_risk(object(), _Conn(), None))

    assert calls == [True]
    assert [(row.underlying, row.notional) for row in book] == [("HPQ", 200.0)]
    limits = risk.RiskLimits(
        max_concurrent=1, max_sector_agg_pct=0.0, sector_map={},
        allow_any_name=True, cash_buffer_pct=0.0)
    eligible, rejected = _eligible(["SYMS"], positions=book, limits=limits)
    assert eligible == ()
    assert any("max concurrent" in reason for reason in rejected["SYMS"])


@pytest.mark.parametrize("bad_order", [
    SimpleNamespace(action="BUY", orderType="MKT", totalQuantity=2),
    SimpleNamespace(action="BUY", orderType="LMT", lmtPrice=0.0, totalQuantity=2),
    SimpleNamespace(action="BUY", orderType="LMT", lmtPrice=2.0, totalQuantity=0),
])
def test_working_buy_with_unverifiable_exposure_refuses_instead_of_counting_zero(bad_order):
    trade = SimpleNamespace(
        order=bad_order,
        orderStatus=SimpleNamespace(status="Submitted"),
        contract=SimpleNamespace(symbol="HPQ", conId=901))

    with pytest.raises(ValueError, match="working BUY HPQ"):
        trader_mod.resting_buy_positions((trade,), {}, ())


def test_working_buy_can_use_positive_journal_debit_when_limit_is_unavailable():
    trade = SimpleNamespace(
        order=SimpleNamespace(action="BUY", orderType="MKT", totalQuantity=2),
        orderStatus=SimpleNamespace(status="Submitted"),
        contract=SimpleNamespace(symbol="HPQ", conId=901))

    book = trader_mod.resting_buy_positions((trade,), {901: 475.0}, ())

    assert [(row.underlying, row.notional) for row in book] == [("HPQ", 475.0)]


def test_slate_prefilter_refuses_an_unreadable_working_order_book(monkeypatch):
    async def _view(_ib, _audit):
        return SimpleNamespace(readable=False, trades=(), error="timeout")

    monkeypatch.setattr(dr, "broker_entry_order_view", _view)
    with pytest.raises(dr.OpenBookUnreadable, match="working BUYs"):
        asyncio.run(dr._prefilter_positions_for_risk(object(), object(), None))
