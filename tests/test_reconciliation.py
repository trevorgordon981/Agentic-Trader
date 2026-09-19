"""Public API contract; production-derived narrative omitted."""

import pytest
from exitmgr.state import State, StateManager, InFlightClose, reconcile_state


class TestReconciliation:
    """Public API contract; production-derived narrative omitted."""

    def test_reconcile_clean_state(self):
        """Public API contract; production-derived narrative omitted."""
        state = State()
        live_positions = {123: {"qty": 1, "avg_cost": 5.0}}
        live_open_orders = {}
        journal_entries = {123: {"debit": 500.0}}

        safe, alerts = reconcile_state(state, live_positions, live_open_orders, journal_entries)

        assert safe is True
        assert len(alerts) == 0

    def test_reconcile_in_flight_with_live_order(self):
        """Public API contract; production-derived narrative omitted."""
        state = State()
        close = InFlightClose(con_id=123, order_id=100, remaining_qty=1, entry_debit=500.0,
                              client_id=42, identity_version=1)
        state.add_in_flight(close)

        live_positions = {123: {"qty": 1, "avg_cost": 5.0}}
        live_open_orders = {123: {"order_id": 100, "remaining": 1, "client_id": 42}}
        journal_entries = {123: {"debit": 500.0}}

        safe, alerts = reconcile_state(state, live_positions, live_open_orders, journal_entries)

        assert safe is True

    def test_reconcile_context_free_absence_is_not_a_fill(self):
        """Public API contract; production-derived narrative omitted."""
        state = State()
        close = InFlightClose(con_id=123, order_id=100, remaining_qty=1, entry_debit=500.0)
        state.add_in_flight(close)

        live_positions = {}
        live_open_orders = {}
        journal_entries = {123: {"debit": 500.0}}

        safe, alerts = reconcile_state(state, live_positions, live_open_orders, journal_entries)

        assert safe is True
        assert any("absence is not terminal evidence" in a.lower() for a in alerts)
        assert state.get_in_flight(123) is close

    def test_reconcile_in_flight_partial_fill(self):
        """Public API contract; production-derived narrative omitted."""
        state = State()
        close = InFlightClose(con_id=123, order_id=100, remaining_qty=5, entry_debit=2500.0,
                              client_id=42, identity_version=1)
        state.add_in_flight(close)


        live_positions = {123: {"qty": 3, "avg_cost": 5.0}}
        live_open_orders = {123: {"order_id": 100, "remaining": 3, "client_id": 42}}
        journal_entries = {123: {"debit": 2500.0}}

        safe, alerts = reconcile_state(state, live_positions, live_open_orders, journal_entries)

        assert safe is True

        assert close.remaining_qty == 3

    def test_reconcile_in_flight_qty_mismatch_abort(self):
        """Public API contract; production-derived narrative omitted."""
        state = State()
        close = InFlightClose(con_id=123, order_id=100, remaining_qty=5, entry_debit=2500.0)
        state.add_in_flight(close)


        live_positions = {123: {"qty": 2, "avg_cost": 5.0}}
        live_open_orders = {}
        journal_entries = {123: {"debit": 2500.0}}

        safe, alerts = reconcile_state(state, live_positions, live_open_orders, journal_entries)

        assert safe is False
        assert any("cannot reconcile" in a.lower() for a in alerts)

    def test_reconcile_live_order_not_in_flight_abort(self):
        """Public API contract; production-derived narrative omitted."""
        state = State()

        live_positions = {123: {"qty": 1, "avg_cost": 5.0}}
        live_open_orders = {123: {"order_id": 999, "remaining": 1}}
        journal_entries = {123: {"debit": 500.0}}

        safe, alerts = reconcile_state(state, live_positions, live_open_orders, journal_entries)

        assert safe is False
        assert any("not in in_flight" in a.lower() for a in alerts)

    def test_reconcile_unexpected_position_no_order_is_warn_not_fatal(self):
        """Public API contract; production-derived narrative omitted."""
        state = State()

        live_positions = {456: {"qty": 1, "avg_cost": 5.0}}
        live_open_orders = {}
        journal_entries = {}
        detail = {}
        safe, alerts = reconcile_state(state, live_positions, live_open_orders,
                                       journal_entries, detail=detail)
        assert safe is True
        assert any("[WARN]" in a and "456" in a for a in alerts)
        assert 456 not in detail["inconsistent"]

    def test_reconcile_unexpected_position_with_order_still_aborts(self):
        """Public API contract; production-derived narrative omitted."""
        state = State()
        live_positions = {456: {"qty": 1, "avg_cost": 5.0}}
        live_open_orders = {456: {"order_id": 777, "remaining": 1}}
        journal_entries = {}
        detail = {}
        safe, alerts = reconcile_state(state, live_positions, live_open_orders,
                                       journal_entries, detail=detail)
        assert safe is False
        assert 456 in detail["inconsistent"]

    def test_reconcile_clean_position_not_blocked_alongside_inconsistent(self):
        """Public API contract; production-derived narrative omitted."""
        state = State()

        state.add_in_flight(InFlightClose(
            con_id=123, order_id=100, remaining_qty=1, entry_debit=500.0,
            client_id=42, identity_version=1))

        live_positions = {123: {"qty": 1, "avg_cost": 5.0}, 456: {"qty": 1, "avg_cost": 5.0}}
        live_open_orders = {123: {"order_id": 100, "remaining": 1, "client_id": 42},
                            456: {"order_id": 777, "remaining": 1}}
        journal_entries = {123: {"debit": 500.0}}
        detail = {}
        safe, alerts = reconcile_state(state, live_positions, live_open_orders,
                                       journal_entries, detail=detail)
        assert safe is False
        assert 456 in detail["inconsistent"]
        assert 123 not in detail["inconsistent"]

    def test_reconcile_in_flight_order_id_mismatch_abort(self):
        """Public API contract; production-derived narrative omitted."""
        state = State()
        close = InFlightClose(con_id=123, order_id=100, remaining_qty=1, entry_debit=500.0)
        state.add_in_flight(close)

        live_positions = {123: {"qty": 1, "avg_cost": 5.0}}

        live_open_orders = {123: {"order_id": 999, "remaining": 1}}
        journal_entries = {123: {"debit": 500.0}}

        safe, alerts = reconcile_state(state, live_positions, live_open_orders, journal_entries)

        assert safe is False
        assert any("order_id" in a.lower() and "mismatch" in a.lower() for a in alerts)

    def test_legacy_order_id_reuse_cannot_bind_another_clients_live_order(self):
        """Public API contract; production-derived narrative omitted."""
        state = State()
        close = InFlightClose(
            con_id=123, order_id=100, remaining_qty=1, entry_debit=500.0)
        state.add_in_flight(close)

        safe, alerts = reconcile_state(
            state,
            {123: {"qty": 1, "avg_cost": 5.0}},
            {123: {"order_id": 100, "remaining": 1, "client_id": 999,
                   "perm_id": 777, "order_ref": "another-campaign"}},
            {123: {"debit": 500.0}})

        assert safe is False
        assert any("identity mismatch" in row.lower() for row in alerts)
        assert close.client_id is None and close.perm_id == 0 and close.order_ref is None
        assert close.identity_version == 0

    def test_reconcile_context_free_row_survives_empty_snapshots(self):
        """Public API contract; production-derived narrative omitted."""
        state = State()
        close = InFlightClose(con_id=123, order_id=100, remaining_qty=1, entry_debit=500.0)
        state.add_in_flight(close)


        live_positions = {}
        live_open_orders = {}
        journal_entries = {123: {"debit": 500.0}}

        safe, alerts = reconcile_state(state, live_positions, live_open_orders, journal_entries)

        assert safe is True
        assert state.get_in_flight(123) is close

    def test_context_free_missing_order_with_position_is_scoped_and_retained(self):
        state = State()
        close = InFlightClose(con_id=123, order_id=100, remaining_qty=1, entry_debit=500.0)
        state.add_in_flight(close)
        detail = {}

        safe, alerts = reconcile_state(
            state, {123: {"qty": 1, "avg_cost": 5.0}}, {},
            {123: {"debit": 500.0}}, detail=detail)

        assert safe
        assert 123 not in detail["inconsistent"]
        assert state.get_in_flight(123) is close
        assert any("block any duplicate close" in row for row in alerts)
        assert any("operator adjudication" in row for row in alerts)


class TestReconciliationAbortPath:
    """Public API contract; production-derived narrative omitted."""

    def test_abort_path_reachable_on_qty_mismatch(self):
        """Public API contract; production-derived narrative omitted."""
        state = State()
        close = InFlightClose(con_id=123, order_id=100, remaining_qty=10, entry_debit=5000.0)
        state.add_in_flight(close)


        live_positions = {123: {"qty": 5, "avg_cost": 5.0}}
        live_open_orders = {}
        journal_entries = {123: {"debit": 5000.0}}

        safe, alerts = reconcile_state(state, live_positions, live_open_orders, journal_entries)


        assert safe is False

        assert len(alerts) > 0

    def test_abort_path_reachable_on_unexpected_order(self):
        """Public API contract; production-derived narrative omitted."""
        state = State()


        live_positions = {123: {"qty": 1, "avg_cost": 5.0}}
        live_open_orders = {123: {"order_id": 500, "remaining": 1}}
        journal_entries = {123: {"debit": 500.0}}

        safe, alerts = reconcile_state(state, live_positions, live_open_orders, journal_entries)


        assert safe is False

        assert len(alerts) > 0
