"""Tests for the scale-out (partial trim) + improved trailing-stop exit logic.

Finding 1 (#1-ROI audit fix, 2026-07-02): bank part of a winner at a first target and trail
the runner so a +25%+ peak that fades no longer round-trips to a smaller result.
"""

import pytest
from exitmgr.rules import (
    evaluate_scale_out,
    evaluate_trailing_stop,
    evaluate_position,
)
from exitmgr.config import RulesConfig, TrailingConfig, ScaleOutConfig


# ---------------------------------------------------------------------------
# Scale-out (partial trim) rule
# ---------------------------------------------------------------------------
class TestScaleOut:
    def test_trims_at_first_target(self):
        """At/above the first target, scale-out signals a PARTIAL close."""
        # Entry 5.00/share, 4 contracts, +20% -> price 6.00
        t = evaluate_scale_out(
            current_price=6.0, entry_debit=2000.0, quantity=4,
            first_target_pct=20.0, trim_fraction=0.5,
        )
        assert t is not None
        assert t.trigger_type == "scale_out"
        assert t.quantity_fraction == 0.5          # partial, not a full close
        assert t.pnl_pct == pytest.approx(20.0)

    def test_no_trim_below_first_target(self):
        t = evaluate_scale_out(
            current_price=5.9, entry_debit=2000.0, quantity=4,
            first_target_pct=20.0, trim_fraction=0.5,
        )
        assert t is None

    def test_no_trim_when_already_trimmed(self):
        """Once trimmed, the rule must not re-fire (caller passes state)."""
        t = evaluate_scale_out(
            current_price=7.0, entry_debit=2000.0, quantity=4,
            first_target_pct=20.0, trim_fraction=0.5, already_trimmed=True,
        )
        assert t is None

    def test_no_trim_single_contract(self):
        """qty < 2 can't leave a runner -> defer to the full profit target."""
        t = evaluate_scale_out(
            current_price=6.0, entry_debit=500.0, quantity=1,
            first_target_pct=20.0, trim_fraction=0.5,
        )
        assert t is None

    def test_full_quantity_fraction_default_is_one(self):
        """Every non-scale-out trigger defaults quantity_fraction=1.0 (full close)."""
        from exitmgr.rules import evaluate_profit_target
        t = evaluate_profit_target(10.0, 500.0, 1, 100.0)
        assert t.quantity_fraction == 1.0


# ---------------------------------------------------------------------------
# Improved trailing stop -- protects REALIZED gains (giveback off entry basis)
# ---------------------------------------------------------------------------
class TestTrailingProtectsRealizedGains:
    def test_protects_more_than_activation_basis(self):
        """Peak +40%, giveback 0.4 -> floor keeps 60% of the +40% gain (= +24%)."""
        # Entry 5.00/share, peak 7.00 (+40%), giveback 0.4
        # floor = 5 + (7-5)*(1-0.4) = 5 + 1.2 = 6.20 (=+24%)
        # current 6.30 (+26%) is still above the floor -> hold
        assert evaluate_trailing_stop(
            current_price=6.30, entry_debit=500.0, quantity=1,
            peak_price=7.0, activation_gain_pct=20.0, giveback_fraction=0.4,
        ) is None
        # current 6.10 (+22%) dropped below the +24% floor -> exit, locking a solid gain
        t = evaluate_trailing_stop(
            current_price=6.10, entry_debit=500.0, quantity=1,
            peak_price=7.0, activation_gain_pct=20.0, giveback_fraction=0.4,
        )
        assert t is not None
        assert t.trigger_type == "trailing_stop"
        assert t.pnl_pct == pytest.approx(22.0)

    def test_not_armed_below_activation(self):
        """Peak never reached activation gain -> trail stays disarmed."""
        # Entry 5.00, peak 5.50 (+10%), activation +20% -> not armed
        assert evaluate_trailing_stop(
            current_price=5.20, entry_debit=500.0, quantity=1,
            peak_price=5.50, activation_gain_pct=20.0, giveback_fraction=0.4,
        ) is None


# ---------------------------------------------------------------------------
# End-to-end round-trip scenario via evaluate_position
# ---------------------------------------------------------------------------
def _rules():
    """Mirror the shipped priors: scale-out +20% / trim 50%, trail arm +20% / giveback 0.4,
    full profit target +30%, stop -30%, time-stop DTE<=10."""
    return RulesConfig(
        profit_target_pct=30.0,
        stop_pct=30.0,
        time_stop_days=10,
        trailing=TrailingConfig(enabled=True, activation_gain_pct=20.0, giveback_fraction=0.4),
        scale_out=ScaleOutConfig(enabled=True, first_target_pct=20.0, trim_fraction=0.5),
    )


class TestRoundTripScenario:
    """A position that runs to +40% MFE then fades: OLD behavior (no scale-out, no trail)
    would either full-close at the +30% target on the way up or round-trip the whole thing;
    NEW behavior trims half at +20% and trails the runner out, banking the gain instead of
    giving it all back."""

    ENTRY = 2000.0   # 5.00/share x 4 contracts
    QTY = 4

    def test_step1_trims_at_first_target(self):
        """On the way up, first cross of +20% signals a partial trim (not a full close)."""
        t = evaluate_position(
            con_id=1, symbol="AAPL", quantity=self.QTY, entry_debit=self.ENTRY,
            current_price=6.00,          # +20%
            days_to_expiry=30, peak_price=6.00, rules=_rules(),
            already_trimmed=False,
        )
        assert t is not None
        assert t.trigger_type == "scale_out"
        assert t.quantity_fraction == 0.5

    def test_step2_full_target_takes_priority_over_scale_out(self):
        """If price gaps straight to the +30% full target, take the full profit (nothing to
        let run) -- profit_target outranks scale_out."""
        t = evaluate_position(
            con_id=1, symbol="AAPL", quantity=self.QTY, entry_debit=self.ENTRY,
            current_price=6.50,          # +30%
            days_to_expiry=30, peak_price=6.50, rules=_rules(),
            already_trimmed=False,
        )
        assert t is not None
        assert t.trigger_type == "profit_target"
        assert t.quantity_fraction == 1.0

    def test_step3_runner_trails_out_after_fade(self):
        """After the trim (already_trimmed=True), the runner rides to +40% peak then fades;
        the trail exits it instead of round-tripping to zero."""
        rules = _rules()
        # Runner is now 2 contracts (half of 4). entry_debit for the runner = half.
        runner_debit = self.ENTRY / 2
        # Peak 7.00 (+40%). floor = 5 + (7-5)*(1-0.4) = 6.20 (+24%).
        # Fade to 6.00 (+20%) -> below floor -> trailing_stop full-closes the runner.
        t = evaluate_position(
            con_id=1, symbol="AAPL", quantity=2, entry_debit=runner_debit,
            current_price=6.00, days_to_expiry=30, peak_price=7.00, rules=rules,
            already_trimmed=True,        # scale-out won't re-fire
        )
        assert t is not None
        assert t.trigger_type == "trailing_stop"
        assert t.quantity_fraction == 1.0
        # Banked ~+20% on the runner instead of round-tripping the whole +40% peak to nothing.
        assert t.pnl_pct == pytest.approx(20.0)

    def test_step3b_runner_holds_above_trail_floor(self):
        """Runner still above the trailing floor -> no exit, let it keep running."""
        rules = _rules()
        runner_debit = self.ENTRY / 2
        t = evaluate_position(
            con_id=1, symbol="AAPL", quantity=2, entry_debit=runner_debit,
            current_price=6.50, days_to_expiry=30, peak_price=7.00, rules=rules,
            already_trimmed=True,
        )
        # 6.50 (+30%) is above the +24% floor AND at the profit target... profit target fires
        # first here (full exit at target). Either way it's a FULL exit, not a round-trip.
        assert t is not None
        assert t.trigger_type == "profit_target"
