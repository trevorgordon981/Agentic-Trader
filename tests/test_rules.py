"""Public API contract; production-derived narrative omitted."""

import pytest
from exitmgr.rules import (
    evaluate_profit_target,
    evaluate_stop,
    evaluate_time_stop,
    evaluate_trailing_stop,
    evaluate_position,
    calculate_pnl_pct,
)
from exitmgr.config import RulesConfig, TrailingConfig


class TestProfitTarget:
    """Public API contract; production-derived narrative omitted."""

    def test_profit_target_hit(self):
        """Public API contract; production-derived narrative omitted."""


        trigger = evaluate_profit_target(
            current_price=10.0,
            entry_debit=500.0,
            quantity=1,
            profit_target_pct=100.0,
        )
        assert trigger is not None
        assert trigger.trigger_type == "profit_target"
        assert trigger.pnl_pct == pytest.approx(100.0)

    def test_profit_target_not_hit(self):
        """Public API contract; production-derived narrative omitted."""
        trigger = evaluate_profit_target(
            current_price=8.0,
            entry_debit=500.0,
            quantity=1,
            profit_target_pct=100.0,
        )
        assert trigger is None

    def test_profit_target_exactly_at_threshold(self):
        """Public API contract; production-derived narrative omitted."""
        trigger = evaluate_profit_target(
            current_price=10.0,
            entry_debit=500.0,
            quantity=1,
            profit_target_pct=100.0,
        )
        assert trigger is not None


class TestStopLoss:
    """Public API contract; production-derived narrative omitted."""

    def test_stop_hit(self):
        """Public API contract; production-derived narrative omitted."""


        trigger = evaluate_stop(
            current_price=2.0,
            entry_debit=500.0,
            quantity=1,
            stop_pct=50.0,
        )
        assert trigger is not None
        assert trigger.trigger_type == "stop"
        assert trigger.pnl_pct == pytest.approx(-60.0)

    def test_stop_not_hit(self):
        """Public API contract; production-derived narrative omitted."""
        trigger = evaluate_stop(
            current_price=3.0,
            entry_debit=500.0,
            quantity=1,
            stop_pct=50.0,
        )
        assert trigger is None


class TestTimeStop:
    """Public API contract; production-derived narrative omitted."""

    def test_time_stop_hit(self):
        """Public API contract; production-derived narrative omitted."""
        trigger = evaluate_time_stop(
            current_price=5.0,
            entry_debit=500.0,
            quantity=1,
            days_to_expiry=2,
            time_stop_days=3,
        )
        assert trigger is not None
        assert trigger.trigger_type == "time_stop"

    def test_time_stop_not_hit(self):
        """Public API contract; production-derived narrative omitted."""
        trigger = evaluate_time_stop(
            current_price=5.0,
            entry_debit=500.0,
            quantity=1,
            days_to_expiry=5,
            time_stop_days=3,
        )
        assert trigger is None

    def test_time_stop_no_dte(self):
        """Public API contract; production-derived narrative omitted."""
        trigger = evaluate_time_stop(
            current_price=5.0,
            entry_debit=500.0,
            quantity=1,
            days_to_expiry=None,
            time_stop_days=3,
        )
        assert trigger is None


class TestTrailingStop:
    """Public API contract; production-derived narrative omitted."""

    def test_trailing_not_activated(self):
        """Public API contract; production-derived narrative omitted."""

        trigger = evaluate_trailing_stop(
            current_price=6.0,
            entry_debit=500.0,
            quantity=1,
            peak_since_arm=6.0,
            activation_gain_pct=50.0,
            giveback_fraction=0.5,
            armed=True,
        )
        assert trigger is None

    def test_trailing_activated_not_triggered(self):
        """Public API contract; production-derived narrative omitted."""




        trigger = evaluate_trailing_stop(
            current_price=9.0,
            entry_debit=500.0,
            quantity=1,
            peak_since_arm=10.0,
            activation_gain_pct=50.0,
            giveback_fraction=0.5,
            armed=True,
        )
        assert trigger is None

    def test_trailing_triggered(self):
        """Public API contract; production-derived narrative omitted."""


        trigger = evaluate_trailing_stop(
            current_price=7.0,
            entry_debit=500.0,
            quantity=1,
            peak_since_arm=10.0,
            activation_gain_pct=50.0,
            giveback_fraction=0.5,
            armed=True,
        )
        assert trigger is not None
        assert trigger.trigger_type == "trailing_stop"


class TestEvaluatePosition:
    """Public API contract; production-derived narrative omitted."""

    def test_evaluate_with_all_rules_disabled(self):
        """Public API contract; production-derived narrative omitted."""
        rules = RulesConfig(
            profit_target_pct=None,
            stop_pct=None,
            time_stop_days=None,
            trailing=TrailingConfig(enabled=False),
        )
        trigger = evaluate_position(
            con_id=123,
            symbol="AAPL",
            quantity=1,
            entry_debit=500.0,
            current_price=5.0,
            days_to_expiry=5,
            peak_price=5.0,
            rules=rules,
        )
        assert trigger is None

    def test_evaluate_priority(self):
        """Public API contract; production-derived narrative omitted."""
        rules = RulesConfig(
            profit_target_pct=100.0,
            stop_pct=50.0,
            time_stop_days=None,
            trailing=TrailingConfig(enabled=False),
        )

        trigger = evaluate_position(
            con_id=123,
            symbol="AAPL",
            quantity=1,
            entry_debit=500.0,
            current_price=10.0,
            days_to_expiry=5,
            peak_price=10.0,
            rules=rules,
        )
        assert trigger is not None
        assert trigger.trigger_type == "profit_target"


class TestPnlCalculation:
    """Public API contract; production-derived narrative omitted."""

    def test_pnl_profit(self):
        """Public API contract; production-derived narrative omitted."""

        pnl = calculate_pnl_pct(current_price=10.0, entry_debit=500.0, quantity=1)
        assert pnl == pytest.approx(100.0)

    def test_pnl_loss(self):
        """Public API contract; production-derived narrative omitted."""

        pnl = calculate_pnl_pct(current_price=2.5, entry_debit=500.0, quantity=1)
        assert pnl == pytest.approx(-50.0)

    def test_pnl_zero_entry(self):
        """Public API contract; production-derived narrative omitted."""
        pnl = calculate_pnl_pct(current_price=5.0, entry_debit=0.0, quantity=1)
        assert pnl == 0.0
