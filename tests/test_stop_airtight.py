"""Airtight protective stop (2026-07-03).

The pot-tiered take-profit ceiling scales the TP side ONLY. The protective stop must:
  (a) keep firing across the whole trade life, including after the TP ceiling is raised for a
      runner (raising TP must never suppress the stop check);
  (b) never be mutated by the tiering logic (stop stays at its entry-stamped value in every tier);
  (c) never lapse -- no open position is ever left without an active stop (the manager backstop
      forces a default stop when neither journal nor config supplies one).
"""
from dataclasses import replace

import pytest

from exitmgr import construction
from exitmgr.rules import evaluate_position, evaluate_stop
from exitmgr.config import ConstructionConfig, RulesConfig

C = ConstructionConfig()

TIERS = [
    {"min_pot": 0,      "tp_max_pct": 0.25, "tp_pct": 0.20},
    {"min_pot": 100000, "tp_max_pct": 0.60, "tp_pct": 0.50},
]


def _rules(tp, sl):
    return RulesConfig(profit_target_pct=tp, stop_pct=sl, time_stop_days=None)


class TestStopFiresAcrossLife:
    def test_stop_fires_even_with_high_runner_tp(self):
        """A runner carrying a raised TP ceiling (e.g. +60%) still stops out when price craters.
        entry_debit=$500 (5.00/sh), stop 30% -> stop price 3.50; price 3.00 must trigger."""
        rules = _rules(tp=60.0, sl=30.0)   # high TP (runner) + normal stop
        trig = evaluate_position(
            con_id=1, symbol="XYZ", quantity=1, entry_debit=500.0,
            current_price=3.00, days_to_expiry=20, peak_price=None, rules=rules)
        assert trig is not None
        assert trig.trigger_type == "stop"

    def test_high_tp_does_not_swallow_stop_between_levels(self):
        """Price down at the stop but nowhere near the raised TP: stop still wins."""
        rules = _rules(tp=60.0, sl=30.0)
        trig = evaluate_position(
            con_id=1, symbol="XYZ", quantity=1, entry_debit=500.0,
            current_price=3.49, days_to_expiry=20, peak_price=None, rules=rules)
        assert trig is not None and trig.trigger_type == "stop"

    def test_stop_independent_of_tp_magnitude(self):
        """Same price/stop fires regardless of whether TP is 25 (small pot) or 60 (big pot)."""
        for tp in (25.0, 60.0):
            trig = evaluate_stop(current_price=3.0, entry_debit=500.0, quantity=1, stop_pct=30.0)
            assert trig is not None and trig.trigger_type == "stop"
            # and the full evaluator agrees under either TP ceiling
            full = evaluate_position(
                con_id=1, symbol="X", quantity=1, entry_debit=500.0,
                current_price=3.0, days_to_expiry=20, peak_price=None, rules=_rules(tp, 30.0))
            assert full is not None and full.trigger_type == "stop"


class TestTieringNeverTouchesStop:
    def test_tp_tier_helper_returns_no_stop_field(self):
        """The tier helper only yields (tp_max, tp_pct) -- it structurally cannot alter a stop."""
        res = construction.tp_tier_for_pot(150000, TIERS, C.tp_max_pct, C.tp_pct)
        assert res == (0.60, 0.50)
        assert len(res) == 2

    def test_per_call_cons_override_leaves_sl_unchanged(self):
        """The entry wiring builds a per-call cons copy overriding ONLY tp_max_pct/tp_pct.
        sl_pct (and tp_min_pct) must be identical to the shared singleton in every tier."""
        for nl in (500, 6000, 30000, 150000):
            tp_max, tp_def = construction.tp_tier_for_pot(nl, TIERS, C.tp_max_pct, C.tp_pct)
            cons_tp = replace(C, tp_max_pct=tp_max, tp_pct=tp_def)
            assert cons_tp.sl_pct == C.sl_pct           # stop untouched
            assert cons_tp.tp_min_pct == C.tp_min_pct   # floor untouched
        assert C.sl_pct == ConstructionConfig().sl_pct  # shared singleton not mutated

    def test_clamp_stop_identical_across_tiers(self):
        """clamp_tp_sl's stop output is the same whatever the pot tier: only TP scales."""
        stops = []
        for nl in (500, 6000, 30000, 150000):
            tp_max, tp_def = construction.tp_tier_for_pot(nl, TIERS, C.tp_max_pct, C.tp_pct)
            cons_tp = replace(C, tp_max_pct=tp_max, tp_pct=tp_def)
            _tp, sl = construction.clamp_tp_sl(75.0, 30.0, cons_tp)
            stops.append(sl)
        assert len(set(stops)) == 1 and stops[0] == 30.0


class TestNoPositionLeftUnstopped:
    """Replicates the manager's per-position stop-resolution + airtight backstop so a position is
    NEVER evaluated without an active stop (bare config, legacy pre-stop fill, corrupt journal)."""

    def _resolve_rules(self, journal_entry, config_rules):
        """Mirror of exitmgr/manager.py run_cycle stop resolution + _STOP_BACKSTOP_PCT backstop."""
        from exitmgr.manager import _STOP_BACKSTOP_PCT
        rules = config_rules
        je = journal_entry or {}
        if je.get("profit_target_pct") or je.get("stop_pct"):
            rules = replace(config_rules,
                            profit_target_pct=je.get("profit_target_pct") or config_rules.profit_target_pct,
                            stop_pct=je.get("stop_pct") or config_rules.stop_pct)
        if rules.stop_pct is None or float(rules.stop_pct) <= 0:
            rules = replace(rules, stop_pct=_STOP_BACKSTOP_PCT)
        return rules

    def test_backstop_constant_positive(self):
        from exitmgr.manager import _STOP_BACKSTOP_PCT
        assert _STOP_BACKSTOP_PCT > 0

    def test_journal_stamped_stop_used(self):
        cfg = RulesConfig(profit_target_pct=30.0, stop_pct=30.0)
        rules = self._resolve_rules({"stop_pct": 25.0}, cfg)
        assert rules.stop_pct == 25.0   # entry-stamped stop honored

    def test_config_fallback_when_journal_missing_stop(self):
        cfg = RulesConfig(profit_target_pct=30.0, stop_pct=30.0)
        rules = self._resolve_rules({}, cfg)          # no journal stop
        assert rules.stop_pct == 30.0

    def test_backstop_fires_when_journal_and_config_both_absent(self):
        """The gap this hardening closes: bare config (stop_pct None) + no journal stop.
        WITHOUT the backstop the position would run unstopped; WITH it, a stop is forced."""
        from exitmgr.manager import _STOP_BACKSTOP_PCT
        bare = RulesConfig()  # stop_pct defaults None
        # Prove the gap exists absent the backstop:
        assert bare.stop_pct is None
        trig_gap = evaluate_position(
            con_id=1, symbol="X", quantity=1, entry_debit=500.0, current_price=1.0,
            days_to_expiry=20, peak_price=None, rules=bare)
        assert trig_gap is None  # unstopped -- price cratered to $1 yet no stop trigger
        # The manager backstop closes it:
        rules = self._resolve_rules({}, bare)
        assert rules.stop_pct == _STOP_BACKSTOP_PCT
        trig = evaluate_position(
            con_id=1, symbol="X", quantity=1, entry_debit=500.0, current_price=1.0,
            days_to_expiry=20, peak_price=None, rules=rules)
        assert trig is not None and trig.trigger_type == "stop"

    def test_partial_fill_or_reentry_smaller_qty_still_stopped(self):
        """A runner left after a scale-out trim (smaller qty) keeps its stop every cycle."""
        cfg = RulesConfig(profit_target_pct=60.0, stop_pct=30.0)
        rules = self._resolve_rules({"profit_target_pct": 60.0, "stop_pct": 30.0}, cfg)
        # qty reduced to 1 after a trim; stop still evaluated on the remaining runner
        trig = evaluate_position(
            con_id=1, symbol="X", quantity=1, entry_debit=250.0, current_price=1.5,
            days_to_expiry=20, peak_price=None, rules=rules)
        assert trig is not None and trig.trigger_type == "stop"
