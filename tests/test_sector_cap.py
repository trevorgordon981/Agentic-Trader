"""Tests for the correlation / sector concentration cap in the hard risk gate.

The single-name-agg cap treats NVDA + AMD + MU as three separate names; this cap groups them
into ONE correlated macro bet via a static sector_map and bounds the cluster's aggregate premium
at max_sector_agg_pct of pot. See risk.sector_exposure / evaluate_trade block #6b.
"""
from exitmgr.risk import (
    RiskLimits, OpenPosition, ProposedTrade, evaluate_trade, sector_exposure, sector_of,
)

POT = 1010.0  # pot -> sector cap at the 0.25 default = $252.50; single-name-agg (0.36) = $363.60
SEMIS = {"NVDA": "semis", "AMD": "semis", "MU": "semis"}


def gate(trade, *, net_liq=POT, available=10_000.0, open_pos=None, limits=None):
    return evaluate_trade(
        trade, net_liq=net_liq, available_funds=available,
        open_positions=open_pos or [], pot_day_start=net_liq,
        approved_names=set(), limits=limits or RiskLimits(allow_any_name=True, sector_map=dict(SEMIS)),
    )


# ---- pure function -------------------------------------------------------------------------

def test_sector_exposure_groups_cluster():
    pos = [OpenPosition("NVDA", 100.0, False), OpenPosition("AMD", 100.0, False)]
    agg = sector_exposure(pos, "MU", 100.0, SEMIS)
    assert agg == {"semis": 300.0}


def test_sector_exposure_excludes_index_and_splits_unmapped():
    pos = [OpenPosition("NVDA", 100.0, False), OpenPosition("SPY", 500.0, True),
           OpenPosition("AAPL", 40.0, False)]
    agg = sector_exposure(pos, "MU", 100.0, SEMIS)
    assert agg == {"semis": 200.0, "AAPL": 40.0}  # SPY (index) excluded; AAPL unmapped = own name


def test_sector_of_fallback_to_own_name():
    assert sector_of("aapl", SEMIS) == "AAPL"       # unmapped -> own uppercased name
    assert sector_of("nvda", SEMIS) == "semis"
    assert sector_of("AAPL", {}) == "AAPL"           # empty map -> own name


# ---- enforcement: 3 semis breach the cluster cap while each is under the single-name cap ----

def test_three_semis_breach_sector_cap():
    # NVDA 100 + AMD 100 already ($200 semis), + MU 100 -> $300 > $252.50 sector cap.
    # single-name-agg ($300 < $363.60) does NOT trip, per-trade ($100 < $121) does NOT trip:
    # ONLY the sector cap blocks -> proves the new cluster gate is what caught the macro bet.
    pos = [OpenPosition("NVDA", 100.0, False), OpenPosition("AMD", 100.0, False)]
    d = gate(ProposedTrade("MU", 100.0, False), open_pos=pos)
    assert not d.approved
    assert any("sector 'semis'" in r for r in d.reasons), d.reasons
    assert not any("single-name exposure" in r for r in d.reasons), d.reasons
    assert not any("12%-of-pot" in r for r in d.reasons), d.reasons


def test_two_semis_under_sector_cap_ok():
    # NVDA 100 + AMD 100 = $200 < $252.50 -> two soft-sized correlated names are allowed.
    d = gate(ProposedTrade("AMD", 100.0, False), open_pos=[OpenPosition("NVDA", 100.0, False)])
    assert d.approved, d.reasons


# ---- unmapped symbol behaves as its own name (no regression) --------------------------------

def test_unmapped_symbol_not_clustered():
    # AAPL is not in the semis map; a big semis book must NOT drag an AAPL entry over the cap.
    pos = [OpenPosition("NVDA", 120.0, False), OpenPosition("AMD", 120.0, False)]  # $240 semis
    d = gate(ProposedTrade("AAPL", 100.0, False), open_pos=pos)
    assert d.approved, d.reasons  # AAPL cluster = $100 (its own name), well under $252.50


# ---- disabled / empty map is a no-op --------------------------------------------------------

def test_empty_map_is_noop():
    # Same 3-semis book that breaches above, but with NO sector_map -> sector gate never fires.
    lim = RiskLimits(allow_any_name=True, sector_map={})
    pos = [OpenPosition("NVDA", 100.0, False), OpenPosition("AMD", 100.0, False)]
    d = gate(ProposedTrade("MU", 100.0, False), open_pos=pos, limits=lim)
    assert d.approved, d.reasons
    assert not any("sector" in r for r in d.reasons)


def test_zero_pct_is_noop():
    lim = RiskLimits(allow_any_name=True, sector_map=dict(SEMIS), max_sector_agg_pct=0.0)
    pos = [OpenPosition("NVDA", 100.0, False), OpenPosition("AMD", 100.0, False)]
    d = gate(ProposedTrade("MU", 100.0, False), open_pos=pos, limits=lim)
    assert d.approved, d.reasons


# ---- boundary ------------------------------------------------------------------------------

def test_boundary_exactly_at_cap_approved():
    # net_liq 1000 -> cap $250. NVDA 150 + AMD 100 = $250 exactly -> approved (<= cap).
    pos = [OpenPosition("NVDA", 150.0, False)]
    d = gate(ProposedTrade("AMD", 100.0, False), net_liq=1000.0, open_pos=pos)
    assert d.approved, d.reasons


def test_boundary_just_over_cap_blocked():
    # net_liq 1000 -> cap $250. NVDA 150 + AMD 101 = $251 -> just over -> blocked by sector cap.
    pos = [OpenPosition("NVDA", 150.0, False)]
    d = gate(ProposedTrade("AMD", 101.0, False), net_liq=1000.0, open_pos=pos)
    assert not d.approved
    assert any("sector 'semis'" in r for r in d.reasons), d.reasons


# ---- index + confident interactions (mirror single-name-agg exemptions) --------------------

def test_index_candidate_exempt():
    # An index ETF entry is never subject to the sector cap even if mapped positions exist.
    pos = [OpenPosition("NVDA", 200.0, False), OpenPosition("AMD", 200.0, False)]
    d = gate(ProposedTrade("SPY", 100.0, True), open_pos=pos)
    assert not any("sector" in r for r in d.reasons), d.reasons


def test_confident_cannot_relax_sector_cap():
    # Conviction can alter the soft size curve, never a hard concentration gate.
    lim = RiskLimits(allow_any_name=True, sector_map=dict(SEMIS),
                     confident_full_size=True, cap_bypass_min_conviction=6, max_trade_pct_hard=1.0)
    pos = [OpenPosition("NVDA", 100.0, False), OpenPosition("AMD", 100.0, False)]
    d = gate(ProposedTrade("MU", 100.0, False, conviction=9), open_pos=pos, limits=lim)
    assert not d.approved
    assert any("sector" in r for r in d.reasons), d.reasons
