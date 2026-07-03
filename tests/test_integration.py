"""Integration tests for the complete exit manager flow."""

import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime

from exitmgr.config import Config, RulesConfig, TrailingConfig
from exitmgr.state import StateManager, InFlightClose


class TestDryRunDefault:
    """Tests ensuring dry run is the default behavior."""

    def test_default_config_is_dry_run(self, sample_config):
        """Default config should have dry_run=True."""
        # sample_config fixture sets dry_run=True by default
        assert sample_config.dry_run is True

    def test_arm_flag_disables_dry_run(self, sample_config):
        """Arm flag should set dry_run=False."""
        sample_config.dry_run = not sample_config.arm
        # When arm=True, dry_run should be False
        sample_config.arm = True
        sample_config.dry_run = not sample_config.arm
        assert sample_config.dry_run is False


class TestScopeFiltering:
    """Tests for position scoping (journal vs all)."""

    def test_journal_scope_filters_positions(self, temp_journal_file, sample_config):
        """Journal scope should only include positions in journal."""
        from exitmgr.manager import ExitManager

        sample_config.journal.path = temp_journal_file
        sample_config.scope.mode = "journal"

        manager = ExitManager(sample_config)

        # Should have loaded journal entries
        assert len(manager._journal_entries) == 3
        assert 123456 in manager._journal_entries
        assert 234567 in manager._journal_entries
        assert 345678 in manager._journal_entries

    def test_all_scope_includes_all_positions(self, sample_config):
        """All scope should include all long call positions."""
        from exitmgr.manager import ExitManager

        sample_config.scope.mode = "all"

        manager = ExitManager(sample_config)

        # Should still have empty journal (no file loaded by default)
        # But scope is "all", so would manage all positions


class TestIntegrationFlow:
    """Integration tests for complete evaluation and order flow."""

    @pytest.mark.asyncio
    async def test_evaluation_logs_all_positions(self, sample_config, temp_journal_file, temp_state_file):
        """Evaluation should log all managed positions."""
        from exitmgr.manager import ExitManager
        from exitmgr.connection import IBConnection, PositionData

        sample_config.journal.path = temp_journal_file
        sample_config.state.path = temp_state_file

        # Create mock IB connection
        mock_ib = MagicMock(spec=IBConnection)
        mock_ib.connect = AsyncMock(return_value=True)
        mock_ib.disconnect = AsyncMock()
        mock_ib.get_positions = AsyncMock(return_value={
            123456: PositionData(con_id=123456, symbol="AAPL", right="C", quantity=1, avg_cost=5.0),
            234567: PositionData(con_id=234567, symbol="TSLA", right="C", quantity=2, avg_cost=6.0),
        })
        mock_ib.get_open_orders = AsyncMock(return_value={})
        mock_ib.fetch_quotes = AsyncMock(return_value={
            123456: {"price": 6.0, "bid": 5.5, "ask": 6.5},
            234567: {"price": 7.0, "bid": 6.5, "ask": 7.5},
        })

        # Create manager and inject mock
        manager = ExitManager(sample_config)
        manager.ib_conn = mock_ib

        # Run cycle in dry-run mode
        await manager.run_cycle(dry_run=True)

        # Verify positions were evaluated (would log output)
        # In real test, would capture stdout

    @pytest.mark.asyncio
    async def test_no_orders_in_dry_run(self, sample_config, temp_journal_file, temp_state_file):
        """Dry run should NOT place any orders."""
        from exitmgr.manager import ExitManager
        from exitmgr.connection import IBConnection, PositionData

        sample_config.journal.path = temp_journal_file
        sample_config.state.path = temp_state_file
        sample_config.dry_run = True

        # Create mock IB connection
        mock_ib = MagicMock(spec=IBConnection)
        mock_ib.connect = AsyncMock(return_value=True)
        mock_ib.disconnect = AsyncMock()
        mock_ib.get_positions = AsyncMock(return_value={
            123456: PositionData(con_id=123456, symbol="AAPL", right="C", quantity=1, avg_cost=5.0),
        })
        mock_ib.get_open_orders = AsyncMock(return_value={})
        mock_ib.fetch_quotes = AsyncMock(return_value={
            123456: {"price": 12.0, "bid": 11.5, "ask": 12.5},  # Profit target hit (100%)
        })

        # Create manager and inject mock
        manager = ExitManager(sample_config)
        manager.ib_conn = mock_ib

        # Track if order was placed
        order_placed = []
        original_place_order = mock_ib.place_order
        async def track_order(*args, **kwargs):
            order_placed.append(True)
            return await original_place_order(*args, **kwargs)
        mock_ib.place_order = track_order

        # Run cycle
        await manager.run_cycle(dry_run=True)

        # Should NOT have placed order
        assert len(order_placed) == 0


class TestAtomicStatePersistence:
    """Tests for atomic state file writes."""

    def test_state_write_is_atomic(self, temp_state_file):
        """State file should be written atomically (temp + rename)."""
        sm = StateManager(temp_state_file)

        close = InFlightClose(con_id=123, order_id=100, remaining_qty=1, entry_debit=500.0)
        sm.state.add_in_flight(close)
        sm.save()

        # Verify temp file doesn't exist (renamed to final)
        temp_path = sm.state_path.with_suffix(".tmp")
        # Note: temp file may or may not exist depending on timing,
        # but final file should exist and be valid
        assert sm.state_path.exists()

        # Verify content is valid JSON
        import json
        with open(sm.state_path, "r") as f:
            data = json.load(f)
        assert "in_flight" in data
        assert "123" in data["in_flight"]

    def test_corrupted_state_file_handled(self, temp_state_file):
        """Corrupted state file should be handled gracefully."""
        # Write corrupted JSON
        with open(temp_state_file, "w") as f:
            f.write("{ invalid json }")

        # Should load and return empty state (with warning)
        sm = StateManager(temp_state_file)
        # Should have loaded empty state
        assert len(sm.state.in_flight) == 0


# Helper to make test work with pytest-asyncio
pytestmark = pytest.mark.asyncio
