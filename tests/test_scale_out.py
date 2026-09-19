"""Public API contract; production-derived narrative omitted."""

import pytest
from exitmgr.rules import (
    evaluate_scale_out,
    evaluate_trailing_stop,
    evaluate_position,
)
from exitmgr.config import RulesConfig, TrailingConfig, ScaleOutConfig





class TestScaleOut:
    def test_trims_at_first_target(self):
        """Public API contract; production-derived narrative omitted."""

        t = evaluate_scale_out(
            current_price=6.0, entry_debit=2000.0, quantity=4,
            first_target_pct=20.0, trim_fraction=0.5,
        )
        assert t is not None
        assert t.trigger_type == "scale_out"
        assert t.quantity_fraction == 0.5
        assert t.pnl_pct == pytest.approx(20.0)

    def test_no_trim_below_first_target(self):
        t = evaluate_scale_out(
            current_price=5.9, entry_debit=2000.0, quantity=4,
            first_target_pct=20.0, trim_fraction=0.5,
        )
        assert t is None

    def test_no_trim_when_already_trimmed(self):
        """Public API contract; production-derived narrative omitted."""
        t = evaluate_scale_out(
            current_price=7.0, entry_debit=2000.0, quantity=4,
            first_target_pct=20.0, trim_fraction=0.5, already_trimmed=True,
        )
        assert t is None

    def test_no_trim_single_contract(self):
        """Public API contract; production-derived narrative omitted."""
        t = evaluate_scale_out(
            current_price=6.0, entry_debit=500.0, quantity=1,
            first_target_pct=20.0, trim_fraction=0.5,
        )
        assert t is None

    def test_full_quantity_fraction_default_is_one(self):
        """Public API contract; production-derived narrative omitted."""
        from exitmgr.rules import evaluate_profit_target
        t = evaluate_profit_target(10.0, 500.0, 1, 100.0)
        assert t.quantity_fraction == 1.0





class TestTrailingProtectsRealizedGains:
    def test_protects_more_than_activation_basis(self):
        """Public API contract; production-derived narrative omitted."""



        assert evaluate_trailing_stop(
            current_price=6.30, entry_debit=500.0, quantity=1,
            peak_since_arm=7.0, activation_gain_pct=20.0, giveback_fraction=0.4, armed=True,
        ) is None

        t = evaluate_trailing_stop(
            current_price=6.10, entry_debit=500.0, quantity=1,
            peak_since_arm=7.0, activation_gain_pct=20.0, giveback_fraction=0.4, armed=True,
        )
        assert t is not None
        assert t.trigger_type == "trailing_stop"
        assert t.pnl_pct == pytest.approx(22.0)

    def test_not_armed_below_activation(self):
        """Public API contract; production-derived narrative omitted."""

        assert evaluate_trailing_stop(
            current_price=5.20, entry_debit=500.0, quantity=1,
            peak_since_arm=5.50, activation_gain_pct=20.0, giveback_fraction=0.4, armed=True,
        ) is None





def _rules():
    """Public API contract; production-derived narrative omitted."""
    return RulesConfig(
        profit_target_pct=30.0,
        stop_pct=30.0,
        time_stop_days=10,
        trailing=TrailingConfig(enabled=True, activation_gain_pct=20.0, giveback_fraction=0.4),
        scale_out=ScaleOutConfig(enabled=True, first_target_pct=20.0, trim_fraction=0.5),
    )


class TestRoundTripScenario:
    """Public API contract; production-derived narrative omitted."""

    ENTRY = 2000.0
    QTY = 4

    def test_step1_trims_at_first_target(self):
        """Public API contract; production-derived narrative omitted."""
        t = evaluate_position(
            con_id=1, symbol="AAPL", quantity=self.QTY, entry_debit=self.ENTRY,
            current_price=6.00,
            days_to_expiry=30, peak_price=6.00, rules=_rules(),
            already_trimmed=False,
        )
        assert t is not None
        assert t.trigger_type == "scale_out"
        assert t.quantity_fraction == 0.5

    def test_step2_full_target_takes_priority_over_scale_out(self):
        """Public API contract; production-derived narrative omitted."""
        t = evaluate_position(
            con_id=1, symbol="AAPL", quantity=self.QTY, entry_debit=self.ENTRY,
            current_price=6.50,
            days_to_expiry=30, peak_price=6.50, rules=_rules(),
            already_trimmed=False,
        )
        assert t is not None
        assert t.trigger_type == "profit_target"
        assert t.quantity_fraction == 1.0

    def test_step3_runner_trails_out_after_fade(self):
        """Public API contract; production-derived narrative omitted."""
        rules = _rules()

        runner_debit = self.ENTRY / 2


        t = evaluate_position(
            con_id=1, symbol="AAPL", quantity=2, entry_debit=runner_debit,
            current_price=6.00, days_to_expiry=30, peak_price=7.00, rules=rules,
            already_trimmed=True,
            trail_armed=True, peak_since_arm=7.00,
        )
        assert t is not None
        assert t.trigger_type == "trailing_stop"
        assert t.quantity_fraction == 1.0

        assert t.pnl_pct == pytest.approx(20.0)

    def test_step3b_runner_holds_above_trail_floor(self):
        """Public API contract; production-derived narrative omitted."""
        rules = _rules()
        runner_debit = self.ENTRY / 2
        t = evaluate_position(
            con_id=1, symbol="AAPL", quantity=2, entry_debit=runner_debit,
            current_price=6.50, days_to_expiry=30, peak_price=7.00, rules=rules,
            already_trimmed=True,
        )


        assert t is not None
        assert t.trigger_type == "profit_target"
