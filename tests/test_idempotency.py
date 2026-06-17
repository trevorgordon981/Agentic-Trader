"""Tests for idempotency and double-close prevention."""

import pytest
from exitmgr.state import State, StateManager, InFlightClose


class TestIdempotency:
    """Tests for idempotency checks."""

    def test_in_flight_prevents_duplicate_close(self, temp_state_file):
        """Should detect existing in-flight order for same contract."""
        sm = StateManager(temp_state_file)

        # Add in-flight order
        close = InFlightClose(
            con_id=123,
            order_id=100,
            remaining_qty=1,
            entry_debit=500.0,
        )
        sm.state.add_in_flight(close)

        # Check if in-flight exists
        existing = sm.state.get_in_flight(123)
        assert existing is not None
        assert existing.order_id == 100

    def test_in_flight_different_contract_allowed(self, temp_state_file):
        """Different contracts should not affect each other."""
        sm = StateManager(temp_state_file)

        # Add in-flight for contract 123
        close1 = InFlightClose(con_id=123, order_id=100, remaining_qty=1, entry_debit=500.0)
        sm.state.add_in_flight(close1)

        # Contract 456 should not have in-flight
        existing = sm.state.get_in_flight(456)
        assert existing is None

    def test_in_flight_partial_fill_update(self, temp_state_file):
        """Partial fill should update remaining quantity."""
        sm = StateManager(temp_state_file)

        # Add in-flight order for 2 contracts
        close = InFlightClose(con_id=123, order_id=100, remaining_qty=2, entry_debit=1000.0)
        sm.state.add_in_flight(close)

        # Simulate partial fill of 1 contract
        close.remaining_qty -= 1
        assert close.remaining_qty == 1

        # Should still exist
        existing = sm.state.get_in_flight(123)
        assert existing is not None
        assert existing.remaining_qty == 1

    def test_in_flight_full_fill_removal(self, temp_state_file):
        """Full fill should remove in-flight record."""
        sm = StateManager(temp_state_file)

        # Add in-flight order
        close = InFlightClose(con_id=123, order_id=100, remaining_qty=1, entry_debit=500.0)
        sm.state.add_in_flight(close)

        # Simulate full fill
        close.remaining_qty = 0
        if close.remaining_qty <= 0:
            sm.state.remove_in_flight(123)

        # Should be removed
        existing = sm.state.get_in_flight(123)
        assert existing is None


class TestRestartIdempotency:
    """Tests for idempotency across restarts (state persistence)."""

    def test_in_flight_persists_across_restart(self, temp_state_file):
        """In-flight orders should survive restart."""
        sm1 = StateManager(temp_state_file)

        close = InFlightClose(con_id=123, order_id=100, remaining_qty=1, entry_debit=500.0)
        sm1.state.add_in_flight(close)
        sm1.save()

        # Simulate restart - create new StateManager
        sm2 = StateManager(temp_state_file)

        # Should still have in-flight
        existing = sm2.state.get_in_flight(123)
        assert existing is not None
        assert existing.order_id == 100
        assert existing.remaining_qty == 1

    def test_in_flight_prevents_duplicate_after_restart(self, temp_state_file):
        """After restart, existing in-flight should prevent new order."""
        sm1 = StateManager(temp_state_file)

        # Add in-flight order
        close = InFlightClose(con_id=123, order_id=100, remaining_qty=1, entry_debit=500.0)
        sm1.state.add_in_flight(close)
        sm1.save()

        # Simulate restart
        sm2 = StateManager(temp_state_file)

        # Check idempotency - should detect existing in-flight
        existing = sm2.state.get_in_flight(123)
        assert existing is not None
        # This would prevent placing a new order in OrderManager.can_place_close()
