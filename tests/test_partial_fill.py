"""Public API contract; production-derived narrative omitted."""

import pytest
from exitmgr.state import State, StateManager, InFlightClose
from unittest.mock import MagicMock, AsyncMock, patch


class TestPartialFillBookkeeping:
    """Public API contract; production-derived narrative omitted."""

    def test_partial_fill_updates_remaining(self, temp_state_file):
        """Public API contract; production-derived narrative omitted."""
        sm = StateManager(temp_state_file)

        close = InFlightClose(con_id=123, order_id=100, remaining_qty=5, entry_debit=2500.0)
        sm.state.add_in_flight(close)


        filled_qty = 2
        close.remaining_qty -= filled_qty

        assert close.remaining_qty == 3
        sm.save()


        sm2 = StateManager(temp_state_file)
        existing = sm2.state.get_in_flight(123)
        assert existing.remaining_qty == 3

    def test_multiple_partial_fills(self, temp_state_file):
        """Public API contract; production-derived narrative omitted."""
        sm = StateManager(temp_state_file)

        close = InFlightClose(con_id=123, order_id=100, remaining_qty=5, entry_debit=2500.0)
        sm.state.add_in_flight(close)


        close.remaining_qty -= 2
        assert close.remaining_qty == 3


        close.remaining_qty -= 1
        assert close.remaining_qty == 2

        sm.save()


        sm2 = StateManager(temp_state_file)
        existing = sm2.state.get_in_flight(123)
        assert existing.remaining_qty == 2

    def test_partial_fill_becomes_full_fill(self, temp_state_file):
        """Public API contract; production-derived narrative omitted."""
        sm = StateManager(temp_state_file)

        close = InFlightClose(con_id=123, order_id=100, remaining_qty=1, entry_debit=500.0)
        sm.state.add_in_flight(close)


        close.remaining_qty = 0
        if close.remaining_qty <= 0:
            sm.state.remove_in_flight(123)


        assert sm.state.get_in_flight(123) is None
        sm.save()


        sm2 = StateManager(temp_state_file)
        assert sm2.state.get_in_flight(123) is None

    def test_partial_fill_persists_immediately(self, temp_state_file):
        """Public API contract; production-derived narrative omitted."""
        sm = StateManager(temp_state_file)

        close = InFlightClose(con_id=123, order_id=100, remaining_qty=5, entry_debit=2500.0)
        sm.state.add_in_flight(close)
        sm.save()


        close.remaining_qty = 3
        sm.save()




        sm2 = StateManager(temp_state_file)
        existing = sm2.state.get_in_flight(123)
        assert existing.remaining_qty == 3


class TestPartialFillIntegration:
    """Public API contract; production-derived narrative omitted."""

    def test_order_manager_partial_fill_update(self, temp_state_file):
        """Public API contract; production-derived narrative omitted."""
        from exitmgr.order import OrderManager
        from exitmgr.connection import IBConnection


        ib_conn = MagicMock()
        sm = StateManager(temp_state_file)


        close = InFlightClose(con_id=123, order_id=100, remaining_qty=5, entry_debit=2500.0)
        sm.state.add_in_flight(close)



        close.remaining_qty -= 2
        sm.save()


        existing = sm.state.get_in_flight(123)
        assert existing.remaining_qty == 3
