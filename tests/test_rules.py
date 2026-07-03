"""Tests for exit rule evaluation."""

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
    """Tests for profit target rule."""

    def test_profit_target_hit(self):
        """Profit target should trigger when price >= entry * (1 + target_pct)."""
        # Entry debit = $500 (5.00 per share * 100), target = 100%
        # Target price = 5.00 * 2 = 10.00
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
        """Profit target should NOT trigger below threshold."""
        trigger = evaluate_profit_target(
            current_price=8.0,
            entry_debit=500.0,
            quantity=1,
            profit_target_pct=100.0,
        )
        assert trigger is None

    def test_profit_target_exactly_at_threshold(self):
        """Profit target should trigger at exact threshold."""
        trigger = evaluate_profit_target(
            current_price=10.0,
            entry_debit=500.0,
            quantity=1,
            profit_target_pct=100.0,
        )
        assert trigger is not None


class TestStopLoss:
    """Tests for stop loss rule."""

    def test_stop_hit(self):
        """Stop should trigger when price <= entry * (1 - stop_pct)."""
        # Entry = 5.00, stop = 50%
        # Stop price = 5.00 * 0.5 = 2.50
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
        """Stop should NOT trigger above threshold."""
        trigger = evaluate_stop(
            current_price=3.0,
            entry_debit=500.0,
            quantity=1,
            stop_pct=50.0,
        )
        assert trigger is None


class TestTimeStop:
    """Tests for time stop rule."""

    def test_time_stop_hit(self):
        """Time stop should trigger when DTE <= threshold."""
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
        """Time stop should NOT trigger when DTE > threshold."""
        trigger = evaluate_time_stop(
            current_price=5.0,
            entry_debit=500.0,
            quantity=1,
            days_to_expiry=5,
            time_stop_days=3,
        )
        assert trigger is None

    def test_time_stop_no_dte(self):
        """Time stop should skip when DTE is unknown."""
        trigger = evaluate_time_stop(
            current_price=5.0,
            entry_debit=500.0,
            quantity=1,
            days_to_expiry=None,
            time_stop_days=3,
        )
        assert trigger is None


class TestTrailingStop:
    """Tests for trailing stop rule."""

    def test_trailing_not_activated(self):
        """Trailing stop should not trigger before activation."""
        # Entry = 5.00, activation = 50% (7.50), current = 6.00
        trigger = evaluate_trailing_stop(
            current_price=6.0,
            entry_debit=500.0,
            quantity=1,
            peak_price=6.0,
            activation_gain_pct=50.0,
            giveback_fraction=0.5,
        )
        assert trigger is None

    def test_trailing_activated_not_triggered(self):
        """Trailing stop should not trigger if price above trigger level."""
        # NEW semantics (protect realized gains, giveback off ENTRY basis):
        # Entry = 5.00, activation = 50% (7.50), peak = 10.00, giveback = 0.5
        # Floor = entry + (peak - entry) * (1 - giveback) = 5 + (5 * 0.5) = 7.50
        # Current = 9.00 > 7.50, no trigger
        trigger = evaluate_trailing_stop(
            current_price=9.0,
            entry_debit=500.0,
            quantity=1,
            peak_price=10.0,
            activation_gain_pct=50.0,
            giveback_fraction=0.5,
        )
        assert trigger is None

    def test_trailing_triggered(self):
        """Trailing stop should trigger if price gives back too much."""
        # NEW semantics: Entry = 5.00, peak = 10.00, giveback = 0.5
        # Floor = entry + (peak - entry) * (1 - giveback) = 7.50; current = 7.00 < 7.50 -> trigger
        trigger = evaluate_trailing_stop(
            current_price=7.0,
            entry_debit=500.0,
            quantity=1,
            peak_price=10.0,
            activation_gain_pct=50.0,
            giveback_fraction=0.5,
        )
        assert trigger is not None
        assert trigger.trigger_type == "trailing_stop"


class TestEvaluatePosition:
    """Tests for combined position evaluation."""

    def test_evaluate_with_all_rules_disabled(self):
        """Should return None when all rules are disabled."""
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
        """Profit target should take priority over stop."""
        rules = RulesConfig(
            profit_target_pct=100.0,
            stop_pct=50.0,
            time_stop_days=None,
            trailing=TrailingConfig(enabled=False),
        )
        # Price at 10.00 triggers profit target (100%) and stop (would trigger at 2.50)
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
    """Tests for P&L percentage calculation."""

    def test_pnl_profit(self):
        """Calculate profit percentage correctly."""
        # Entry = 500, current value = 1000 (price = 10.00)
        pnl = calculate_pnl_pct(current_price=10.0, entry_debit=500.0, quantity=1)
        assert pnl == pytest.approx(100.0)

    def test_pnl_loss(self):
        """Calculate loss percentage correctly."""
        # Entry = 500, current value = 250 (price = 2.50)
        pnl = calculate_pnl_pct(current_price=2.5, entry_debit=500.0, quantity=1)
        assert pnl == pytest.approx(-50.0)

    def test_pnl_zero_entry(self):
        """Return 0 for zero entry debit."""
        pnl = calculate_pnl_pct(current_price=5.0, entry_debit=0.0, quantity=1)
        assert pnl == 0.0
