"""Tests for hard cap enforcement."""

import pytest
from datetime import datetime
from exitmgr.state import State, StateManager, DailyStats


class TestCapEnforcement:
    """Tests for daily and per-cycle cap tracking."""

    def test_daily_stats_initially_empty(self, temp_state_file):
        """Daily stats should be empty on first run."""
        sm = StateManager(temp_state_file)
        today = datetime.utcnow().strftime("%Y-%m-%d")
        assert today not in sm.state.daily_stats

    def test_daily_stats_update(self, temp_state_file):
        """Daily stats should update correctly."""
        sm = StateManager(temp_state_file)
        today = datetime.utcnow().strftime("%Y-%m-%d")

        sm.state.update_daily_stats(today, order_count=1, notional=500.0)
        sm.save()

        # Reload and verify
        sm2 = StateManager(temp_state_file)
        stats = sm2.state.daily_stats[today]
        assert stats.orders_placed == 1
        assert stats.notional_closed == 500.0

    def test_daily_stats_accumulate(self, temp_state_file):
        """Daily stats should accumulate across orders."""
        sm = StateManager(temp_state_file)
        today = datetime.utcnow().strftime("%Y-%m-%d")

        sm.state.update_daily_stats(today, order_count=1, notional=500.0)
        sm.state.update_daily_stats(today, order_count=2, notional=1000.0)
        sm.save()

        # Reload and verify
        sm2 = StateManager(temp_state_file)
        stats = sm2.state.daily_stats[today]
        assert stats.orders_placed == 3
        assert stats.notional_closed == 1500.0

    def test_cap_check_orders_per_day(self, temp_state_file, sample_config):
        """Should detect when daily order cap is exceeded."""
        sm = StateManager(temp_state_file)
        today = datetime.utcnow().strftime("%Y-%m-%d")

        # Fill up to cap
        sample_config.caps.max_orders_per_day = 5
        for i in range(5):
            sm.state.update_daily_stats(today, order_count=1, notional=100.0)

        # Check cap
        daily_stats = sm.state.daily_stats.get(today)
        assert daily_stats.orders_placed >= sample_config.caps.max_orders_per_day

    def test_cap_check_notional_per_day(self, temp_state_file, sample_config):
        """Should detect when daily notional cap is exceeded."""
        sm = StateManager(temp_state_file)
        today = datetime.utcnow().strftime("%Y-%m-%d")

        # Fill up to cap
        sample_config.caps.max_notional_per_day = 1000.0
        sm.state.update_daily_stats(today, order_count=1, notional=1000.0)

        # Check cap
        daily_stats = sm.state.daily_stats.get(today)
        assert daily_stats.notional_closed >= sample_config.caps.max_notional_per_day


class TestCycleCapTracking:
    """Tests for per-cycle order tracking."""

    def test_in_flight_count_per_cycle(self, temp_state_file):
        """In-flight orders should be tracked correctly."""
        from exitmgr.state import InFlightClose

        sm = StateManager(temp_state_file)

        # Add in-flight orders
        close1 = InFlightClose(con_id=1, order_id=100, remaining_qty=1, entry_debit=500.0)
        close2 = InFlightClose(con_id=2, order_id=101, remaining_qty=1, entry_debit=600.0)

        sm.state.add_in_flight(close1)
        sm.state.add_in_flight(close2)
        sm.save()

        # Verify count
        assert len(sm.state.in_flight) == 2
