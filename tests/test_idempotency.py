"""Public API contract; production-derived narrative omitted."""

import pytest
from exitmgr.state import State, StateManager, InFlightClose


class TestIdempotency:
    """Public API contract; production-derived narrative omitted."""

    def test_in_flight_prevents_duplicate_close(self, temp_state_file):
        """Public API contract; production-derived narrative omitted."""
        sm = StateManager(temp_state_file)


        close = InFlightClose(
            con_id=123,
            order_id=100,
            remaining_qty=1,
            entry_debit=500.0,
        )
        sm.state.add_in_flight(close)


        existing = sm.state.get_in_flight(123)
        assert existing is not None
        assert existing.order_id == 100

    def test_in_flight_different_contract_allowed(self, temp_state_file):
        """Public API contract; production-derived narrative omitted."""
        sm = StateManager(temp_state_file)


        close1 = InFlightClose(con_id=123, order_id=100, remaining_qty=1, entry_debit=500.0)
        sm.state.add_in_flight(close1)


        existing = sm.state.get_in_flight(456)
        assert existing is None

    def test_in_flight_partial_fill_update(self, temp_state_file):
        """Public API contract; production-derived narrative omitted."""
        sm = StateManager(temp_state_file)


        close = InFlightClose(con_id=123, order_id=100, remaining_qty=2, entry_debit=1000.0)
        sm.state.add_in_flight(close)


        close.remaining_qty -= 1
        assert close.remaining_qty == 1


        existing = sm.state.get_in_flight(123)
        assert existing is not None
        assert existing.remaining_qty == 1

    def test_in_flight_full_fill_removal(self, temp_state_file):
        """Public API contract; production-derived narrative omitted."""
        sm = StateManager(temp_state_file)


        close = InFlightClose(con_id=123, order_id=100, remaining_qty=1, entry_debit=500.0)
        sm.state.add_in_flight(close)


        close.remaining_qty = 0
        if close.remaining_qty <= 0:
            sm.state.remove_in_flight(123)


        existing = sm.state.get_in_flight(123)
        assert existing is None


class TestRestartIdempotency:
    """Public API contract; production-derived narrative omitted."""

    def test_in_flight_persists_across_restart(self, temp_state_file):
        """Public API contract; production-derived narrative omitted."""
        sm1 = StateManager(temp_state_file)

        close = InFlightClose(con_id=123, order_id=100, remaining_qty=1, entry_debit=500.0)
        sm1.state.add_in_flight(close)
        sm1.save()


        sm2 = StateManager(temp_state_file)


        existing = sm2.state.get_in_flight(123)
        assert existing is not None
        assert existing.order_id == 100
        assert existing.remaining_qty == 1

    def test_in_flight_prevents_duplicate_after_restart(self, temp_state_file):
        """Public API contract; production-derived narrative omitted."""
        sm1 = StateManager(temp_state_file)


        close = InFlightClose(con_id=123, order_id=100, remaining_qty=1, entry_debit=500.0)
        sm1.state.add_in_flight(close)
        sm1.save()


        sm2 = StateManager(temp_state_file)


        existing = sm2.state.get_in_flight(123)
        assert existing is not None
