"""Public API contract; production-derived narrative omitted."""
import os

import pytest
import yaml

from exitmgr import config as config_mod
from exitmgr import entry_safety
from exitmgr.risk import (
    INDEX_UNDERLYINGS,
    UNCLASSIFIED_SECTOR,
    OpenPosition,
    ProposedTrade,
    effective_pot,
    evaluate_trade,
    same_name_notional,
    sector_exposure,
    sector_of,
)

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(APP, "config.yaml")




REAL_BOOK = (("SYMY", 400.0), ("SYMK", 500.0), ("SYML", 450.0),
             ("SYMZ", 350.0), ("SYMJ", 425.0), ("SYMG", 475.0), ("SYMZ", 200.0))
NET_LIQ = 10000.0
AVAILABLE = 7000.0
DAY_START = 10000.0
TYPICAL_DEBIT = 800.0







UNCLASSIFIED_BOOK_NAMES = ("TSLA", "NVO", "UBER", "RIVN", "CVNA")
UNCLASSIFIED_CANDIDATE = "SHOP"


def _positions(rows):
    return [OpenPosition(underlying=u, notional=n, is_index=(u in INDEX_UNDERLYINGS))
            for u, n in rows]


def _live_trading():
    return (yaml.safe_load(open(CONFIG_PATH)) or {})["trading"]


def _live_limits():
    return entry_safety.risk_limits_from_config(_live_trading())


def _gate(symbol, debit, book, limits, approved=("NOTHING",), net_liq=NET_LIQ):
    return evaluate_trade(
        ProposedTrade(underlying=symbol, notional=debit,
                      is_index=symbol in INDEX_UNDERLYINGS, conviction=7),
        net_liq=net_liq, available_funds=AVAILABLE, open_positions=_positions(book),
        pot_day_start=DAY_START, approved_names=set(approved), limits=limits)



def test_an_unmapped_non_index_name_keys_to_the_shared_pool():
    """Public API contract; production-derived narrative omitted."""
    m = {"NVDA": "semis", "SYMK": "energy"}
    assert sector_of("SYMF", m) == UNCLASSIFIED_SECTOR


def test_two_unrelated_unmapped_names_land_in_the_SAME_cluster():
    """Public API contract; production-derived narrative omitted."""
    m = {"NVDA": "semis"}
    assert sector_of("SYMF", m) == sector_of("LYTE", m) == UNCLASSIFIED_SECTOR


def test_a_mapped_name_is_untouched():
    assert sector_of("nvda", {"NVDA": "semis"}) == "semis"


def test_an_empty_sector_map_is_still_a_full_no_op():
    """Public API contract; production-derived narrative omitted."""
    assert sector_of("SYMF", {}) == "SYMF"
    assert sector_of("SYMF", None) == "SYMF"


@pytest.mark.parametrize("sym", sorted(INDEX_UNDERLYINGS))
def test_an_index_underlying_never_enters_the_pool(sym):
    """Public API contract; production-derived narrative omitted."""
    assert sector_of(sym, {"NVDA": "semis"}) == sym


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_a_blank_sector_value_reads_as_UNMAPPED_not_as_a_cluster_named_empty(blank):
    """Public API contract; production-derived narrative omitted."""
    assert sector_of("SYMF", {"SYMF": blank}) == UNCLASSIFIED_SECTOR


def test_a_position_with_no_underlying_is_counted_IN_not_dropped():
    """Public API contract; production-derived narrative omitted."""
    assert sector_of("", {"NVDA": "semis"}) == UNCLASSIFIED_SECTOR
    assert sector_of(None, {"NVDA": "semis"}) == UNCLASSIFIED_SECTOR


def test_the_sentinel_is_not_a_plausible_sector_name():
    """Public API contract; production-derived narrative omitted."""
    assert UNCLASSIFIED_SECTOR == "__unclassified__"
    assert not UNCLASSIFIED_SECTOR.isalnum()


def test_config_and_risk_agree_on_the_sentinel_because_there_is_one_of_them():
    """Public API contract; production-derived narrative omitted."""
    assert config_mod.UNCLASSIFIED_SECTOR is UNCLASSIFIED_SECTOR



def test_the_pool_is_a_superset_of_the_singleton_it_replaced():
    """Public API contract; production-derived narrative omitted."""
    limits = _live_limits()
    book = _positions(REAL_BOOK)
    for symbol in ("SYMF", "ZZZZ", "SYMJ"):
        pooled = sector_exposure(book, symbol, TYPICAL_DEBIT, limits.sector_map).get(
            sector_of(symbol, limits.sector_map), 0.0)
        old_singleton = same_name_notional(book, symbol) + TYPICAL_DEBIT
        assert pooled >= old_singleton - 1e-9, symbol


def test_five_unclassified_names_now_aggregate_and_the_sixth_is_REFUSED():
    """Public API contract; production-derived narrative omitted."""
    limits = _live_limits()
    book = [(s, TYPICAL_DEBIT) for s in UNCLASSIFIED_BOOK_NAMES]

    for sym in UNCLASSIFIED_BOOK_NAMES + (UNCLASSIFIED_CANDIDATE,):
        assert sector_of(sym, limits.sector_map) == UNCLASSIFIED_SECTOR, sym

    pot = effective_pot(NET_LIQ, limits.pot_cap_usd)
    agg = sector_exposure(_positions(book), UNCLASSIFIED_CANDIDATE, TYPICAL_DEBIT,
                          limits.sector_map)[UNCLASSIFIED_SECTOR]
    assert agg == pytest.approx(6 * TYPICAL_DEBIT)
    assert agg > limits.max_sector_agg_pct * pot

    d = _gate(UNCLASSIFIED_CANDIDATE, TYPICAL_DEBIT, book, limits,
              approved=(UNCLASSIFIED_CANDIDATE,))
    assert not d.approved
    assert any("unclassified exposure" in r for r in d.reasons), d.reasons


def test_the_refusal_names_the_symbol_and_the_one_line_that_fixes_it():
    """Public API contract; production-derived narrative omitted."""
    limits = _live_limits()
    book = [(s, TYPICAL_DEBIT) for s in UNCLASSIFIED_BOOK_NAMES]
    d = _gate(UNCLASSIFIED_CANDIDATE, TYPICAL_DEBIT, book, limits,
              approved=(UNCLASSIFIED_CANDIDATE,))
    reason = next(r for r in d.reasons if "unclassified" in r)
    assert UNCLASSIFIED_CANDIDATE in reason
    assert "trading.sector_map" in reason
    assert UNCLASSIFIED_SECTOR not in reason, "do not show an operator the sentinel"


def test_a_classified_cluster_still_reports_as_that_cluster():
    """Public API contract; production-derived narrative omitted."""
    limits = _live_limits()
    book = [("NVDA", 1800.0), ("AMD", 1800.0)]
    d = _gate("MUTX", 900.0, book, limits, approved=("MUTX",))
    assert any("sector 'semis'" in r for r in d.reasons), d.reasons
    assert not any("unclassified" in r for r in d.reasons), d.reasons


def test_a_classified_name_is_unaffected_by_a_book_full_of_unclassified_ones():
    """Public API contract; production-derived narrative omitted."""
    limits = _live_limits()
    book = [(s, TYPICAL_DEBIT) for s in UNCLASSIFIED_BOOK_NAMES]
    d = _gate("NVDA", TYPICAL_DEBIT, book, limits, approved=("NVDA",))
    assert d.approved, d.reasons



def test_every_approved_name_carries_a_cluster():
    """Public API contract; production-derived narrative omitted."""
    tr = _live_trading()
    mapped = {str(k).upper() for k in (tr.get("sector_map") or {})}
    unmapped = sorted({str(s).upper() for s in tr["approved_names"]} - mapped)
    assert not unmapped, (
        "approved but unclassified -- each will be pooled into %s and compete for one shared "
        "budget with every other unclassified name: %s" % (UNCLASSIFIED_SECTOR, unmapped))


def test_no_live_sector_value_collides_with_the_sentinel_or_is_blank():
    tr = _live_trading()
    for sym, sec in (tr.get("sector_map") or {}).items():
        assert str(sec).strip(), sym
        assert str(sec).strip() != UNCLASSIFIED_SECTOR, sym


def test_fertilizer_trio_shares_one_live_cluster():
    limits = _live_limits()
    assert {sector_of(sym, limits.sector_map) for sym in ("CF", "MOS", "NTR")} == {
        "fertilizer"
    }


def test_SYMJ_and_SYMK_are_ONE_energy_bet_on_the_live_config():
    """Public API contract; production-derived narrative omitted."""
    limits = _live_limits()
    assert sector_of("SYMK", limits.sector_map) == "energy"
    assert sector_of("SYMJ", limits.sector_map) == "energy"
    agg = sector_exposure(_positions(REAL_BOOK), "SYMS", TYPICAL_DEBIT,
                          limits.sector_map)["energy"]
    assert agg == pytest.approx(500.0 + 425.0 + TYPICAL_DEBIT)


def test_the_live_pool_holds_nothing_the_book_currently_owns():
    """Public API contract; production-derived narrative omitted."""
    limits = _live_limits()
    pooled = [u for u, _ in REAL_BOOK
              if sector_of(u, limits.sector_map) == UNCLASSIFIED_SECTOR]
    assert not pooled, pooled
