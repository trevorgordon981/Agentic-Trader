"""Public API contract; production-derived narrative omitted."""
import pytest

from exitmgr.risk import OpenPosition, ProposedTrade, evaluate_trade, RiskLimits

POT = 5000.0


def _limits(**kw):
    lim = RiskLimits()
    lim.allow_any_name = True
    for k, v in kw.items():
        setattr(lim, k, v)
    return lim


def _book():
    """Public API contract; production-derived narrative omitted."""
    return [OpenPosition("SYMC", 800.0, False),
            OpenPosition("SYMD", 300.0, False),
            OpenPosition("SYME", 400.0, False)]


def _gate(trade, positions, limits=None):
    return evaluate_trade(trade, net_liq=POT, available_funds=POT * 10,
                          open_positions=positions, pot_day_start=POT,
                          approved_names=set(), limits=limits or _limits(),
                          regime_info=None)


def _single_name_reasons(gate):
    return [r for r in (gate.reasons or []) if "single-name exposure" in r]



def test_an_unrelated_book_no_longer_blocks_a_new_name():
    """Public API contract; production-derived narrative omitted."""
    gate = _gate(ProposedTrade("SYMB", 250.0, False, 7), _book())
    assert not _single_name_reasons(gate), (
        "SYMB is being charged for positions in other names: %s" % gate.reasons)


def test_the_cap_still_binds_when_the_name_IS_concentrated():
    """Public API contract; production-derived narrative omitted."""
    book = _book() + [OpenPosition("SYMB", 1700.0, False)]
    gate = _gate(ProposedTrade("SYMB", 250.0, False, 7), book)
    assert _single_name_reasons(gate), "a genuinely concentrated name slipped through"


def test_exposure_counts_only_the_same_name():
    """Public API contract; production-derived narrative omitted."""
    book = _book() + [OpenPosition("SYMB", 1700.0, False)]
    msg = _single_name_reasons(_gate(ProposedTrade("SYMB", 250.0, False, 7), book))[0]
    assert "1,950" in msg, msg


def test_case_and_whitespace_do_not_defeat_the_match():
    book = [OpenPosition("  SYMB ", 1700.0, False)]
    gate = _gate(ProposedTrade("SYMB", 250.0, False, 7), book)
    assert _single_name_reasons(gate), "case/whitespace let a concentrated name through"



def test_an_unreadable_underlying_is_counted_IN_not_dropped():
    """Public API contract; production-derived narrative omitted."""
    book = [OpenPosition(None, 1700.0, False)]
    gate = _gate(ProposedTrade("SYMB", 250.0, False, 7), book)
    assert _single_name_reasons(gate), "a position with no underlying was silently ignored"



def test_index_positions_are_still_excluded():
    book = [OpenPosition("SPY", 5000.0, True)]
    assert not _single_name_reasons(_gate(ProposedTrade("SYMB", 250.0, False, 7), book))


def test_an_index_trade_is_still_exempt_from_this_check():
    book = [OpenPosition("SPY", 5000.0, True)]
    assert not _single_name_reasons(_gate(ProposedTrade("SPY", 5000.0, True, 7), book))


def test_the_sector_cap_still_catches_correlated_DIFFERENT_names():
    """Public API contract; production-derived narrative omitted."""
    lim = _limits(sector_map={"NVDA": "semis", "MUTX": "semis"}, max_sector_agg_pct=0.25)
    book = [OpenPosition("MUTX", 1100.0, False)]
    gate = _gate(ProposedTrade("NVDA", 250.0, False, 7), book, lim)
    assert any("sector" in r for r in (gate.reasons or [])), (
        "the semis cluster is no longer caught: %s" % gate.reasons)


@pytest.mark.parametrize("held,blocked", [(400.0, False), (1700.0, True)])
def test_the_threshold_itself_is_unchanged_for_the_same_name(held, blocked):
    """Public API contract; production-derived narrative omitted."""
    gate = _gate(ProposedTrade("SYMB", 250.0, False, 7), [OpenPosition("SYMB", held, False)])
    assert bool(_single_name_reasons(gate)) is blocked
