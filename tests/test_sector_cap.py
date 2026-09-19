"""Public API contract; production-derived narrative omitted."""
from exitmgr.risk import (
    UNCLASSIFIED_SECTOR, RiskLimits, OpenPosition, ProposedTrade, evaluate_trade,
    sector_exposure, sector_of,
)








POT = 1010.0
SEMIS = {"NVDA": "semis", "AMD": "semis", "MUTX": "semis"}


def gate(trade, *, net_liq=POT, available=10_000.0, open_pos=None, limits=None):
    return evaluate_trade(
        trade, net_liq=net_liq, available_funds=available,
        open_positions=open_pos or [], pot_day_start=net_liq,
        approved_names=set(), limits=limits or RiskLimits(allow_any_name=True, sector_map=dict(SEMIS)),
    )




def test_sector_exposure_groups_cluster():
    pos = [OpenPosition("NVDA", 100.0, False), OpenPosition("AMD", 100.0, False)]
    agg = sector_exposure(pos, "MUTX", 100.0, SEMIS)
    assert agg == {"semis": 300.0}


def test_sector_exposure_excludes_index_and_pools_unmapped():
    pos = [OpenPosition("NVDA", 100.0, False), OpenPosition("SPY", 500.0, True),
           OpenPosition("AAPL", 40.0, False)]
    agg = sector_exposure(pos, "MUTX", 100.0, SEMIS)

    assert agg == {"semis": 200.0, UNCLASSIFIED_SECTOR: 40.0}


def test_sector_of_pools_unmapped_and_falls_back_only_on_an_empty_map():
    assert sector_of("aapl", SEMIS) == UNCLASSIFIED_SECTOR
    assert sector_of("nvda", SEMIS) == "semis"
    assert sector_of("AAPL", {}) == "AAPL"




def test_three_semis_breach_sector_cap():



    pos = [OpenPosition("NVDA", 100.0, False), OpenPosition("AMD", 100.0, False)]
    d = gate(ProposedTrade("MUTX", 100.0, False), open_pos=pos)
    assert not d.approved
    assert any("sector 'semis'" in r for r in d.reasons), d.reasons
    assert not any("single-name exposure" in r for r in d.reasons), d.reasons
    assert not any("12%-of-pot" in r for r in d.reasons), d.reasons


def test_two_semis_under_sector_cap_ok():

    d = gate(ProposedTrade("AMD", 100.0, False), open_pos=[OpenPosition("NVDA", 100.0, False)])
    assert d.approved, d.reasons




def test_unmapped_symbol_not_clustered():

    pos = [OpenPosition("NVDA", 120.0, False), OpenPosition("AMD", 120.0, False)]
    d = gate(ProposedTrade("AAPL", 100.0, False), open_pos=pos)
    assert d.approved, d.reasons




def test_empty_map_is_noop():

    lim = RiskLimits(allow_any_name=True, sector_map={})
    pos = [OpenPosition("NVDA", 100.0, False), OpenPosition("AMD", 100.0, False)]
    d = gate(ProposedTrade("MUTX", 100.0, False), open_pos=pos, limits=lim)
    assert d.approved, d.reasons
    assert not any("sector" in r for r in d.reasons)


def test_zero_pct_is_noop():
    lim = RiskLimits(allow_any_name=True, sector_map=dict(SEMIS), max_sector_agg_pct=0.0)
    pos = [OpenPosition("NVDA", 100.0, False), OpenPosition("AMD", 100.0, False)]
    d = gate(ProposedTrade("MUTX", 100.0, False), open_pos=pos, limits=lim)
    assert d.approved, d.reasons




def test_boundary_exactly_at_cap_approved():

    pos = [OpenPosition("NVDA", 150.0, False)]
    d = gate(ProposedTrade("AMD", 100.0, False), net_liq=1000.0, open_pos=pos)
    assert d.approved, d.reasons


def test_boundary_just_over_cap_blocked():

    pos = [OpenPosition("NVDA", 150.0, False)]
    d = gate(ProposedTrade("AMD", 101.0, False), net_liq=1000.0, open_pos=pos)
    assert not d.approved
    assert any("sector 'semis'" in r for r in d.reasons), d.reasons




def test_index_candidate_exempt():

    pos = [OpenPosition("NVDA", 200.0, False), OpenPosition("AMD", 200.0, False)]
    d = gate(ProposedTrade("SPY", 100.0, True), open_pos=pos)
    assert not any("sector" in r for r in d.reasons), d.reasons


def test_confident_cannot_relax_sector_cap():

    lim = RiskLimits(allow_any_name=True, sector_map=dict(SEMIS),
                     confident_full_size=True, cap_bypass_min_conviction=6, max_trade_pct_hard=1.0)
    pos = [OpenPosition("NVDA", 100.0, False), OpenPosition("AMD", 100.0, False)]
    d = gate(ProposedTrade("MUTX", 100.0, False, conviction=9), open_pos=pos, limits=lim)
    assert not d.approved
    assert any("sector" in r for r in d.reasons), d.reasons
