"""Gate H2 (2026-07-03): the daily slate -- Trevor's PRIMARY entry path -- now SURFACES a
concentration/correlation warning when adding a candidate would breach the single-name-agg cap
(risk.py #6) or the sector/correlated-cluster cap (risk.py #6b). It is SURFACE-ONLY: it appends a
warning note and audits it, but NEVER hard-blocks (the human tap still decides) and NEVER changes
which trades pass/fail. These tests exercise the pure computation `_concentration_notes`, which is
what `_post_idea` calls inside a fail-safe try/except.
"""
import daily_recommend as dr
from exitmgr import risk

POT = 1000.0

# semis cluster maps NVDA/AMD/MU -> 'semis'; KO/PEP/WMT are unmapped (each its own singleton).
LIMITS = risk.RiskLimits(
    max_sector_agg_pct=0.25,   # sector cap = $250 on a $1000 pot
    sector_map={"NVDA": "semis", "AMD": "semis", "MU": "semis"},
)
# max_single_name_agg_pct stays at its 0.36 default -> single-name cap = $360 on a $1000 pot.


def _pos(sym, notional, is_index=False):
    return risk.OpenPosition(sym, notional, is_index)


def notes(open_pos, sym, debit, is_index=False, pot=POT, limits=LIMITS):
    return dr._concentration_notes(open_pos, sym, debit, is_index, pot, limits)


def _kinds(ns):
    return {kw["kind"] for _txt, kw in ns}


def test_third_correlated_name_surfaces_sector_warning():
    # NVDA + AMD already open ($100 each), adding MU pushes semis to $300 > $250 cap.
    # single-name total is $300 < $360, so ONLY the sector warning should fire (isolated).
    book = [_pos("NVDA", 100.0), _pos("AMD", 100.0)]
    ns = notes(book, "MU", 100.0)
    assert _kinds(ns) == {"sector_agg"}, ns
    txt, kw = ns[0]
    assert kw["sector"] == "semis"
    assert kw["exposure"] == 300.0 and kw["cap"] == 250.0
    assert "concentration" in txt and "semis" in txt


def test_uncorrelated_under_cap_idea_no_warning():
    # One semis name open + an UNMAPPED candidate (own singleton cluster) well under both caps.
    book = [_pos("NVDA", 100.0)]
    assert notes(book, "KO", 100.0) == []           # semis=100, KO=100, single-name=200 -- all under
    # a correlated candidate that stays under the sector cap also warns nothing.
    assert notes([_pos("NVDA", 50.0)], "AMD", 50.0) == []   # semis=100 < 250


def test_single_name_agg_breach_surfaces_that_warning():
    # Three UNMAPPED names (distinct singleton clusters) so no sector breach, but the aggregate
    # non-index single-name book ($200+$150+$100 = $450) exceeds the $360 cap.
    book = [_pos("KO", 200.0), _pos("PEP", 150.0)]
    ns = notes(book, "WMT", 100.0)
    assert _kinds(ns) == {"single_name_agg"}, ns
    _txt, kw = ns[0]
    assert kw["exposure"] == 450.0 and kw["cap"] == 360.0


def test_both_caps_can_fire_together():
    # A concentrated semis book that breaches BOTH the sector cap AND the single-name-agg cap.
    book = [_pos("NVDA", 200.0), _pos("AMD", 200.0)]   # semis already $400
    ns = notes(book, "MU", 100.0)                       # semis -> $500, single-name -> $500
    assert _kinds(ns) == {"sector_agg", "single_name_agg"}, ns


def test_index_candidate_is_exempt():
    # An index ETF candidate is never a concentration concern (mirrors risk.py).
    book = [_pos("KO", 300.0), _pos("PEP", 300.0)]
    assert notes(book, "SPY", 500.0, is_index=True) == []


def test_index_positions_excluded_from_aggregates():
    # A huge index position in the book must NOT count toward single-name/sector aggregates.
    book = [_pos("SPY", 100000.0, is_index=True), _pos("NVDA", 100.0)]
    assert notes(book, "MU", 100.0) == []   # semis=200, single-name=200 -- index ignored


def test_empty_sector_map_makes_sector_check_a_noop():
    lim = risk.RiskLimits(max_sector_agg_pct=0.25, sector_map={})
    book = [_pos("NVDA", 200.0), _pos("AMD", 200.0)]
    ns = notes(book, "MU", 100.0, limits=lim)   # would be $500 semis, but no map -> no sector note
    assert _kinds(ns) == {"single_name_agg"}, ns   # single-name still binds ($500 > $360)


def test_never_raises_and_never_blocks_on_edge_inputs():
    # pot <= 0, empty book, None-ish symbol: returns [] (no note) rather than raising -- so the
    # surrounding fail-safe would never block a proposal.
    assert notes([], "NVDA", 100.0, pot=0.0) == []
    assert notes([], "NVDA", 0.0) == []
    assert notes([], None, 100.0) == []          # underlying None -> no crash


def test_surface_only_returns_data_never_mutates_book():
    # _concentration_notes is pure: it returns notes, it does not mutate the passed-in book.
    book = [_pos("NVDA", 200.0), _pos("AMD", 200.0)]
    before = [(p.underlying, p.notional, p.is_index) for p in book]
    notes(book, "MU", 100.0)
    after = [(p.underlying, p.notional, p.is_index) for p in book]
    assert before == after
