"""Pot-tiered take-profit runner ceiling (2026-07-03).

The TP ceiling (runner leash) + default target scale UP with account size while the protective
stop is left untouched. Covers tier selection at every boundary, empty/None no-op reversibility,
and the clamp behavior (a proposed TP above the tier ceiling clips to the ceiling; below passes
through).
"""
import math
from dataclasses import replace

import pytest

from exitmgr import construction
from exitmgr.config import ConstructionConfig

# The shipped production ladder (mirrors config.yaml caps.tp_tiers), FRACTION units.
TIERS = [
    {"min_pot": 0,      "tp_max_pct": 0.25, "tp_pct": 0.20},   # < $2,500
    {"min_pot": 2500,   "tp_max_pct": 0.30, "tp_pct": 0.22},
    {"min_pot": 5000,   "tp_max_pct": 0.35, "tp_pct": 0.25},
    {"min_pot": 7500,   "tp_max_pct": 0.40, "tp_pct": 0.28},
    {"min_pot": 10000,  "tp_max_pct": 0.45, "tp_pct": 0.32},
    {"min_pot": 25000,  "tp_max_pct": 0.50, "tp_pct": 0.38},
    {"min_pot": 50000,  "tp_max_pct": 0.55, "tp_pct": 0.45},
    {"min_pot": 100000, "tp_max_pct": 0.60, "tp_pct": 0.50},   # max ceiling 60%
]

C = ConstructionConfig()  # flat fallback: tp_max_pct 0.35, tp_pct 0.30


def _tier(net_liq, tiers=TIERS):
    return construction.tp_tier_for_pot(net_liq, tiers, C.tp_max_pct, C.tp_pct)


class TestTierBoundaries:
    def test_below_lowest_uses_floor_row(self):
        assert _tier(1000) == (0.25, 0.20)
        assert _tier(0) == (0.25, 0.20)

    def test_2500_boundary(self):
        assert _tier(2499) == (0.25, 0.20)   # just below -> floor row
        assert _tier(2500) == (0.30, 0.22)   # exactly at -> next tier

    def test_10000_boundary(self):
        assert _tier(9999) == (0.40, 0.28)
        assert _tier(10000) == (0.45, 0.32)

    def test_100k_and_above_caps_at_60(self):
        assert _tier(100000) == (0.60, 0.50)
        assert _tier(250000) == (0.60, 0.50)   # never exceeds the 60% ceiling
        assert _tier(10_000_000) == (0.60, 0.50)

    def test_sample_pots(self):
        assert _tier(1000) == (0.25, 0.20)
        assert _tier(6000) == (0.35, 0.25)
        assert _tier(30000) == (0.50, 0.38)
        assert _tier(150000) == (0.60, 0.50)

    def test_monotonic_non_decreasing_and_capped_60(self):
        pots = [0, 2500, 5000, 7500, 10000, 25000, 50000, 100000, 500000]
        maxes = [_tier(p)[0] for p in pots]
        defs = [_tier(p)[1] for p in pots]
        assert maxes == sorted(maxes)          # ceiling never decreases with pot
        assert defs == sorted(defs)            # default never decreases with pot
        assert max(maxes) == 0.60              # nothing exceeds the 60% ceiling
        # default target always <= its ceiling for every tier
        for p in pots:
            m, d = _tier(p)
            assert d <= m

    def test_tiers_unordered_input_still_correct(self):
        shuffled = list(reversed(TIERS))
        assert construction.tp_tier_for_pot(30000, shuffled, C.tp_max_pct, C.tp_pct) == (0.50, 0.38)


class TestNoOp:
    def test_empty_map_is_full_noop(self):
        assert _tier(6000, tiers=[]) == (C.tp_max_pct, C.tp_pct)
        assert _tier(150000, tiers=None) == (C.tp_max_pct, C.tp_pct)

    def test_none_net_liq_noop_no_upsize(self):
        assert _tier(None) == (C.tp_max_pct, C.tp_pct)

    def test_nan_net_liq_noop(self):
        assert _tier(float("nan")) == (C.tp_max_pct, C.tp_pct)

    def test_garbage_net_liq_noop(self):
        assert _tier("not-a-number") == (C.tp_max_pct, C.tp_pct)

    def test_malformed_rows_skipped_not_crash(self):
        bad = [{"min_pot": 0, "tp_max_pct": 0.25, "tp_pct": 0.20}, {"oops": 1}]
        assert construction.tp_tier_for_pot(9999, bad, C.tp_max_pct, C.tp_pct) == (0.25, 0.20)

    def test_all_malformed_rows_fall_back_flat(self):
        bad = [{"oops": 1}, {"nope": 2}]
        assert construction.tp_tier_for_pot(9999, bad, C.tp_max_pct, C.tp_pct) == (C.tp_max_pct, C.tp_pct)


class TestClampAgainstTierCeiling:
    """SUPERSEDED 2026-07-26 -- Sol audit R5 section R2 / RULING_TAKE_PROFIT.md.

    This class used to assert that `clamp_tp_sl` clipped a model take-profit to the pot-tier
    ceiling and, when none was supplied, installed the tier DEFAULT (+20% at the live account
    size). That is precisely the mechanism Trevor's ruling abolishes: "+20% is doctrine, not a
    clamp -- a potential exit when the thesis has changed AND value has been given back, never a
    mechanical target."

    The assertions are inverted rather than deleted, so the removal stays under test: a tiered
    `cons` may no longer produce a take-profit at ANY pot size, from ANY model input. The tier
    arithmetic itself (TestTierBoundaries above) is untouched and still passes -- the function is
    inert, not broken. Full coverage of the new contract lives in tests/test_no_mechanical_tp.py.
    """

    def _cons_for(self, net_liq):
        tp_max, tp_def = _tier(net_liq)
        return replace(C, tp_max_pct=tp_max, tp_pct=tp_def)

    def test_proposed_tp_is_no_longer_clipped_into_a_tier_ceiling(self):
        # Small pot: a model +75% target used to be clipped to the 25% ceiling and installed.
        tp, sl = construction.clamp_tp_sl(75.0, 30.0, self._cons_for(1000))
        assert tp is None
        assert sl == 30.0            # the stop is untouched by any of this
        # Big pot: same input, same answer. Account size is not an exit signal.
        tp2, sl2 = construction.clamp_tp_sl(75.0, 30.0, self._cons_for(150000))
        assert tp2 is None
        assert sl2 == 30.0

    def test_a_sub_ceiling_proposal_is_also_refused(self):
        # Used to pass straight through as a +40% mechanical target.
        tp, sl = construction.clamp_tp_sl(40.0, 30.0, self._cons_for(150000))
        assert tp is None

    def test_zero_tp_no_longer_installs_the_tier_default(self):
        tp, sl = construction.clamp_tp_sl(0.0, 30.0, self._cons_for(30000))  # was +38%
        assert tp is None
