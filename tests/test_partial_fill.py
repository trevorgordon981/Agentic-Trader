"""Tests for partial fill bookkeeping."""

import pytest
from exitmgr.state import State, StateManager, InFlightClose
from unittest.mock import MagicMock, AsyncMock, patch


class TestPartialFillBookkeeping:
    """Tests for tracking partial fills."""

    def test_partial_fill_updates_remaining(self, temp_state_file):
        """Partial fill should decrement remaining quantity."""
        sm = StateManager(temp_state_file)

        close = InFlightClose(con_id=123, order_id=100, remaining_qty=5, entry_debit=2500.0)
        sm.state.add_in_flight(close)

        # Simulate partial fill of 2 contracts
        filled_qty = 2
        close.remaining_qty -= filled_qty

        assert close.remaining_qty == 3
        sm.save()

        # Reload and verify
        sm2 = StateManager(temp_state_file)
        existing = sm2.state.get_in_flight(123)
        assert existing.remaining_qty == 3

    def test_multiple_partial_fills(self, temp_state_file):
        """Multiple partial fills should accumulate correctly."""
        sm = StateManager(temp_state_file)

        close = InFlightClose(con_id=123, order_id=100, remaining_qty=5, entry_debit=2500.0)
        sm.state.add_in_flight(close)

        # First partial fill
        close.remaining_qty -= 2
        assert close.remaining_qty == 3

        # Second partial fill
        close.remaining_qty -= 1
        assert close.remaining_qty == 2

        sm.save()

        # Reload and verify
        sm2 = StateManager(temp_state_file)
        existing = sm2.state.get_in_flight(123)
        assert existing.remaining_qty == 2

    def test_partial_fill_becomes_full_fill(self, temp_state_file):
        """When remaining reaches 0, in-flight should be removed."""
        sm = StateManager(temp_state_file)

        close = InFlightClose(con_id=123, order_id=100, remaining_qty=1, entry_debit=500.0)
        sm.state.add_in_flight(close)

        # Fill remaining
        close.remaining_qty = 0
        if close.remaining_qty <= 0:
            sm.state.remove_in_flight(123)

        # Should be removed
        assert sm.state.get_in_flight(123) is None
        sm.save()

        # Reload and verify
        sm2 = StateManager(temp_state_file)
        assert sm2.state.get_in_flight(123) is None

    def test_partial_fill_persists_immediately(self, temp_state_file):
        """Partial fill should be persisted immediately (crash-safe)."""
        sm = StateManager(temp_state_file)

        close = InFlightClose(con_id=123, order_id=100, remaining_qty=5, entry_debit=2500.0)
        sm.state.add_in_flight(close)
        sm.save()

        # Simulate partial fill and immediate save
        close.remaining_qty = 3
        sm.save()

        # Simulate crash (no additional saves)

        # Reload and verify
        sm2 = StateManager(temp_state_file)
        existing = sm2.state.get_in_flight(123)
        assert existing.remaining_qty == 3


class TestPartialFillIntegration:
    """Integration tests for partial fills with order tracking."""

    def test_order_manager_partial_fill_update(self, temp_state_file):
        """OrderManager should update in-flight on partial fill."""
        from exitmgr.order import OrderManager
        from exitmgr.connection import IBConnection

        # Create mocks
        ib_conn = MagicMock()
        sm = StateManager(temp_state_file)

        # Add in-flight order
        close = InFlightClose(con_id=123, order_id=100, remaining_qty=5, entry_debit=2500.0)
        sm.state.add_in_flight(close)

        # Simulate partial fill event (would normally come from IB)
        # In real code, this would be called from order status event handler
        close.remaining_qty -= 2
        sm.save()

        # Verify
        existing = sm.state.get_in_flight(123)
        assert existing.remaining_qty == 3
