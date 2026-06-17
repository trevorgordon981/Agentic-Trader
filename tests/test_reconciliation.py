"""Tests for state reconciliation and disagreement handling."""

import pytest
from exitmgr.state import State, StateManager, InFlightClose, reconcile_state


class TestReconciliation:
    """Tests for reconciliation logic."""

    def test_reconcile_clean_state(self):
        """Clean state with no in-flight should pass."""
        state = State()
        live_positions = {123: {"qty": 1, "avg_cost": 5.0}}
        live_open_orders = {}
        journal_entries = {123: {"debit": 500.0}}

        safe, alerts = reconcile_state(state, live_positions, live_open_orders, journal_entries)

        assert safe is True
        assert len(alerts) == 0

    def test_reconcile_in_flight_with_live_order(self):
        """In-flight with matching live order should reconcile."""
        state = State()
        close = InFlightClose(con_id=123, order_id=100, remaining_qty=1, entry_debit=500.0)
        state.add_in_flight(close)

        live_positions = {123: {"qty": 1, "avg_cost": 5.0}}
        live_open_orders = {123: {"order_id": 100, "remaining": 1}}
        journal_entries = {123: {"debit": 500.0}}

        safe, alerts = reconcile_state(state, live_positions, live_open_orders, journal_entries)

        assert safe is True

    def test_reconcile_in_flight_fully_filled(self):
        """In-flight with no position and no order (fill event) should clean up."""
        state = State()
        close = InFlightClose(con_id=123, order_id=100, remaining_qty=1, entry_debit=500.0)
        state.add_in_flight(close)

        live_positions = {}  # Position closed
        live_open_orders = {}  # Order filled/cancelled
        journal_entries = {123: {"debit": 500.0}}

        safe, alerts = reconcile_state(state, live_positions, live_open_orders, journal_entries)

        assert safe is True
        # Should have info alert about fill event
        assert any("fill event" in a.lower() for a in alerts)
        # In-flight should be removed
        assert state.get_in_flight(123) is None

    def test_reconcile_in_flight_partial_fill(self):
        """In-flight with partial fill should update remaining."""
        state = State()
        close = InFlightClose(con_id=123, order_id=100, remaining_qty=5, entry_debit=2500.0)
        state.add_in_flight(close)

        # Live order shows 3 remaining (2 filled)
        live_positions = {123: {"qty": 3, "avg_cost": 5.0}}
        live_open_orders = {123: {"order_id": 100, "remaining": 3}}
        journal_entries = {123: {"debit": 2500.0}}

        safe, alerts = reconcile_state(state, live_positions, live_open_orders, journal_entries)

        assert safe is True
        # Should update remaining
        assert close.remaining_qty == 3

    def test_reconcile_in_flight_qty_mismatch_abort(self):
        """In-flight qty > live position qty (unexpected) should abort."""
        state = State()
        close = InFlightClose(con_id=123, order_id=100, remaining_qty=5, entry_debit=2500.0)
        state.add_in_flight(close)

        # Live position only has 2 (but in-flight says 5 remaining - inconsistency)
        live_positions = {123: {"qty": 2, "avg_cost": 5.0}}
        live_open_orders = {}  # No order (cancelled?)
        journal_entries = {123: {"debit": 2500.0}}

        safe, alerts = reconcile_state(state, live_positions, live_open_orders, journal_entries)

        assert safe is False
        assert any("cannot reconcile" in a.lower() for a in alerts)

    def test_reconcile_live_order_not_in_flight_abort(self):
        """Live order not in in-flight should abort (unexpected order)."""
        state = State()  # No in-flight records

        live_positions = {123: {"qty": 1, "avg_cost": 5.0}}
        live_open_orders = {123: {"order_id": 999, "remaining": 1}}  # Order not in in-flight
        journal_entries = {123: {"debit": 500.0}}

        safe, alerts = reconcile_state(state, live_positions, live_open_orders, journal_entries)

        assert safe is False
        assert any("not in in_flight" in a.lower() for a in alerts)

    def test_reconcile_unexpected_position_abort(self):
        """Position not in journal and not in in-flight (under journal scope) should abort."""
        state = State()

        # Live position 456 not in journal and not in in-flight
        live_positions = {456: {"qty": 1, "avg_cost": 5.0}}
        live_open_orders = {}
        journal_entries = {}  # Empty - only managing journal positions

        safe, alerts = reconcile_state(state, live_positions, live_open_orders, journal_entries)

        assert safe is False
        assert any("unexpected" in a.lower() for a in alerts)

    def test_reconcile_in_flight_order_id_mismatch_abort(self):
        """In-flight order_id differs from live order_id should abort."""
        state = State()
        close = InFlightClose(con_id=123, order_id=100, remaining_qty=1, entry_debit=500.0)
        state.add_in_flight(close)

        live_positions = {123: {"qty": 1, "avg_cost": 5.0}}
        # Live order has different order_id
        live_open_orders = {123: {"order_id": 999, "remaining": 1}}
        journal_entries = {123: {"debit": 500.0}}

        safe, alerts = reconcile_state(state, live_positions, live_open_orders, journal_entries)

        assert safe is False
        assert any("order_id" in a.lower() and "mismatch" in a.lower() for a in alerts)

    def test_reconcile_in_flight_removed_on_clean_fill(self):
        """In-flight should be removed after reconciliation detects clean fill."""
        state = State()
        close = InFlightClose(con_id=123, order_id=100, remaining_qty=1, entry_debit=500.0)
        state.add_in_flight(close)

        # Both position and order gone (fill event)
        live_positions = {}
        live_open_orders = {}
        journal_entries = {123: {"debit": 500.0}}

        safe, alerts = reconcile_state(state, live_positions, live_open_orders, journal_entries)

        assert safe is True
        assert 123 not in state.in_flight


class TestReconciliationAbortPath:
    """Tests ensuring the disagreement abort path is actually reachable."""

    def test_abort_path_reachable_on_qty_mismatch(self):
        """Verify abort path triggers on quantity mismatch."""
        state = State()
        close = InFlightClose(con_id=123, order_id=100, remaining_qty=10, entry_debit=5000.0)
        state.add_in_flight(close)

        # Live position says 5 but in-flight says 10 remaining - can't reconcile
        live_positions = {123: {"qty": 5, "avg_cost": 5.0}}
        live_open_orders = {}
        journal_entries = {123: {"debit": 5000.0}}

        safe, alerts = reconcile_state(state, live_positions, live_open_orders, journal_entries)

        # Must NOT be safe
        assert safe is False
        # And must have alerted
        assert len(alerts) > 0

    def test_abort_path_reachable_on_unexpected_order(self):
        """Verify abort path triggers on unexpected order."""
        state = State()
        # No in-flight records

        live_positions = {123: {"qty": 1, "avg_cost": 5.0}}
        live_open_orders = {123: {"order_id": 500, "remaining": 1}}
        journal_entries = {123: {"debit": 500.0}}

        safe, alerts = reconcile_state(state, live_positions, live_open_orders, journal_entries)

        # Must NOT be safe
        assert safe is False
        # And must have alerted
        assert len(alerts) > 0
