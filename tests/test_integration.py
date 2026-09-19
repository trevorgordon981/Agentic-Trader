"""Public API contract; production-derived narrative omitted."""

import pytest
import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime

from exitmgr.config import Config, RulesConfig, TrailingConfig
from exitmgr.state import StateManager, InFlightClose, StateCorruptionError


class TestDryRunDefault:
    """Public API contract; production-derived narrative omitted."""

    def test_default_config_is_dry_run(self, sample_config):
        """Public API contract; production-derived narrative omitted."""

        assert sample_config.dry_run is True

    def test_arm_flag_disables_dry_run(self, sample_config):
        """Public API contract; production-derived narrative omitted."""
        sample_config.dry_run = not sample_config.arm

        sample_config.arm = True
        sample_config.dry_run = not sample_config.arm
        assert sample_config.dry_run is False


class TestScopeFiltering:
    """Public API contract; production-derived narrative omitted."""

    def test_journal_scope_filters_positions(self, temp_journal_file, sample_config):
        """Public API contract; production-derived narrative omitted."""
        from exitmgr.manager import ExitManager

        sample_config.journal.path = temp_journal_file
        sample_config.scope.mode = "journal"

        manager = ExitManager(sample_config)


        assert len(manager._journal_entries) == 3
        assert 123456 in manager._journal_entries
        assert 234567 in manager._journal_entries
        assert 345678 in manager._journal_entries

    def test_all_scope_includes_all_positions(self, sample_config):
        """Public API contract; production-derived narrative omitted."""
        from exitmgr.manager import ExitManager

        sample_config.scope.mode = "all"

        manager = ExitManager(sample_config)





class TestIntegrationFlow:
    """Public API contract; production-derived narrative omitted."""

    @pytest.mark.asyncio
    async def test_evaluation_logs_all_positions(self, sample_config, temp_journal_file, temp_state_file):
        """Public API contract; production-derived narrative omitted."""
        from exitmgr.manager import ExitManager
        from exitmgr.connection import IBConnection, PositionData

        sample_config.journal.path = temp_journal_file
        sample_config.state.path = temp_state_file


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


        manager = ExitManager(sample_config)
        manager.ib_conn = mock_ib


        await manager.run_cycle(dry_run=True)




    @pytest.mark.asyncio
    async def test_no_orders_in_dry_run(self, sample_config, temp_journal_file, temp_state_file):
        """Public API contract; production-derived narrative omitted."""
        from exitmgr.manager import ExitManager
        from exitmgr.connection import IBConnection, PositionData

        sample_config.journal.path = temp_journal_file
        sample_config.state.path = temp_state_file
        sample_config.dry_run = True


        mock_ib = MagicMock(spec=IBConnection)
        mock_ib.connect = AsyncMock(return_value=True)
        mock_ib.disconnect = AsyncMock()
        mock_ib.get_positions = AsyncMock(return_value={
            123456: PositionData(con_id=123456, symbol="AAPL", right="C", quantity=1, avg_cost=5.0),
        })
        mock_ib.get_open_orders = AsyncMock(return_value={})
        mock_ib.fetch_quotes = AsyncMock(return_value={
            123456: {"price": 12.0, "bid": 11.5, "ask": 12.5},
        })


        manager = ExitManager(sample_config)
        manager.ib_conn = mock_ib


        order_placed = []
        original_place_order = mock_ib.place_order
        async def track_order(*args, **kwargs):
            order_placed.append(True)
            return await original_place_order(*args, **kwargs)
        mock_ib.place_order = track_order


        await manager.run_cycle(dry_run=True)


        assert len(order_placed) == 0


class TestAtomicStatePersistence:
    """Public API contract; production-derived narrative omitted."""

    def test_state_write_is_atomic(self, temp_state_file):
        """Public API contract; production-derived narrative omitted."""
        sm = StateManager(temp_state_file)

        close = InFlightClose(con_id=123, order_id=100, remaining_qty=1, entry_debit=500.0)
        sm.state.add_in_flight(close)
        sm.save()


        temp_path = sm.state_path.with_suffix(".tmp")


        assert sm.state_path.exists()


        import json
        with open(sm.state_path, "r") as f:
            data = json.load(f)
        assert "in_flight" in data
        assert "123" in data["in_flight"]

    def test_corrupted_state_file_handled(self, temp_state_file):
        """Public API contract; production-derived narrative omitted."""

        with open(temp_state_file, "w") as f:
            f.write("{ invalid json }")

        sm = StateManager(temp_state_file)
        with pytest.raises(StateCorruptionError):
            _ = sm.state

    def test_corrupted_primary_never_auto_adopts_stale_backup(self, temp_state_file):
        temp_state_file = Path(temp_state_file)
        sm = StateManager(temp_state_file)
        sm.state.add_in_flight(
            InFlightClose(con_id=123, order_id=100, remaining_qty=1, entry_debit=500.0))
        sm.state.trail_confirmation["123"] = {
            "last_session": "2026-08-25", "consecutive_qualifying_closes": 2,
            "armed_at": "2026-08-25T10:00:00-07:00", "peak_since_arm": 6.0,
            "pinned_activation_gain_pct": 12.0, "pinned_giveback_fraction": 0.4,
        }
        sm.save()
        sm.state.last_cycle = "2026-08-25T10:01:00-07:00"
        sm.save()
        temp_state_file.write_text("{ broken")

        with pytest.raises(StateCorruptionError, match="NOT auto-adopted"):
            _ = StateManager(str(temp_state_file)).state

    def test_backup_is_preserved_as_repair_evidence_after_primary_corruption(self, temp_state_file):
        temp_state_file = Path(temp_state_file)
        sm = StateManager(temp_state_file)
        sm.state.add_in_flight(
            InFlightClose(con_id=77, order_id=8, remaining_qty=1, entry_debit=100.0))
        sm.save()
        sm.state.last_cycle = "2026-08-25T10:01:00+00:00"
        sm.save()
        backup = sm.backup_path
        temp_state_file.write_text("not-json")

        with pytest.raises(StateCorruptionError):
            _ = StateManager(str(temp_state_file)).state
        assert StateManager._read_path(backup).get_in_flight(77) is not None

    def test_newest_trail_cannot_be_silently_disarmed_by_prior_generation(self, temp_state_file):
        temp_state_file = Path(temp_state_file)
        sm = StateManager(temp_state_file)
        sm.save()
        sm.state.trail_confirmation["99"] = {
            "last_session": "2026-08-25", "consecutive_qualifying_closes": 2,
            "armed_at": "2026-08-25T10:00:00-07:00", "peak_since_arm": 7.0,
            "pinned_activation_gain_pct": 12.0, "pinned_giveback_fraction": 0.4,
        }
        sm.save()
        temp_state_file.write_text("{broken")
        with pytest.raises(StateCorruptionError, match="NOT auto-adopted"):
            _ = StateManager(str(temp_state_file)).state

    def test_zero_remaining_close_latch_survives_terminal_finalization_window(
            self, temp_state_file):
        sm = StateManager(temp_state_file)
        sm.state.add_in_flight(
            InFlightClose(con_id=123, order_id=100, remaining_qty=0, entry_debit=500.0))
        sm.save()
        reloaded = StateManager(temp_state_file).state.get_in_flight(123)
        assert reloaded is not None and reloaded.remaining_qty == 0

    def test_legacy_integral_numbers_are_normalized_not_left_ambiguous(self, temp_state_file):
        import json
        Path(temp_state_file).write_text(json.dumps({
            "in_flight": {"123": {
                "con_id": 123.0, "order_id": "100", "remaining_qty": 1.0,
                "entry_debit": 500,
            }},
            "daily_stats": {}, "last_cycle": None, "peak_prices": {},
        }))
        close = StateManager(temp_state_file).state.get_in_flight(123)
        assert close.con_id == 123 and type(close.con_id) is int
        assert close.order_id == 100 and type(close.order_id) is int
        assert close.remaining_qty == 1 and type(close.remaining_qty) is int

    @pytest.mark.parametrize("bad", [1.5, True, "1.0"])
    def test_fractional_or_ambiguous_close_quantity_fails_closed(self, temp_state_file, bad):
        import json
        Path(temp_state_file).write_text(json.dumps({
            "in_flight": {"123": {
                "con_id": 123, "order_id": 100, "remaining_qty": bad,
                "entry_debit": 500,
            }},
            "daily_stats": {}, "last_cycle": None, "peak_prices": {},
        }))
        with pytest.raises(StateCorruptionError):
            _ = StateManager(temp_state_file).state

    @pytest.mark.parametrize("bad_trail", [
        {},
        {"last_session": "2026-08-25", "consecutive_qualifying_closes": 2,
         "armed_at": None, "peak_since_arm": None},
        {"last_session": None, "consecutive_qualifying_closes": 0,
         "armed_at": None, "peak_since_arm": 7.0},
    ])
    def test_partial_or_inconsistent_trail_authority_fails_closed(
            self, temp_state_file, bad_trail):
        import json
        Path(temp_state_file).write_text(json.dumps({
            "in_flight": {}, "daily_stats": {}, "last_cycle": None, "peak_prices": {},
            "trail_confirmation": {"123": bad_trail},
        }))
        with pytest.raises(StateCorruptionError):
            _ = StateManager(temp_state_file).state

    def test_nonfinite_json_never_loads_as_protection_authority(self, temp_state_file):
        Path(temp_state_file).write_text(
            '{"in_flight": {}, "daily_stats": {}, "last_cycle": null, '
            '"peak_prices": {"123": NaN}}')
        with pytest.raises(StateCorruptionError):
            _ = StateManager(temp_state_file).state

    def test_save_refuses_corrupt_primary_without_overwriting_backup_or_primary(
            self, temp_state_file):
        temp_state_file = Path(temp_state_file)
        sm = StateManager(temp_state_file)
        sm.state.add_in_flight(
            InFlightClose(con_id=77, order_id=8, remaining_qty=1, entry_debit=100.0))
        sm.save()
        sm.state.last_cycle = "2026-08-25T10:01:00+00:00"
        sm.save()
        backup_before = sm.backup_path.read_bytes()
        temp_state_file.write_text("{broken-primary")
        primary_before = temp_state_file.read_bytes()
        sm.state.last_cycle = "2026-08-25T10:02:00+00:00"
        with pytest.raises(StateCorruptionError, match="current primary is unreadable"):
            sm.save()
        assert sm.backup_path.read_bytes() == backup_before
        assert temp_state_file.read_bytes() == primary_before

    def test_duplicate_keys_and_overflow_numbers_fail_closed(self, temp_state_file):
        for raw in (
                '{"in_flight":{},"in_flight":{},"daily_stats":{},'
                '"last_cycle":null,"peak_prices":{}}',
                '{"in_flight":{},"daily_stats":{},"last_cycle":null,'
                '"peak_prices":{"123":1e400}}'):
            Path(temp_state_file).write_text(raw)
            with pytest.raises(StateCorruptionError):
                _ = StateManager(temp_state_file).state

    def test_malformed_in_memory_authority_preserves_both_generations(self, temp_state_file):
        temp_state_file = Path(temp_state_file)
        sm = StateManager(temp_state_file)
        sm.save()
        sm.state.last_cycle = "2026-08-25T10:01:00+00:00"
        sm.save()
        before_primary = temp_state_file.read_bytes()
        before_backup = sm.backup_path.read_bytes()
        sm.state.campaign_bindings["123"] = {"campaign_seq": 1}
        with pytest.raises(TypeError):
            sm.save()
        assert temp_state_file.read_bytes() == before_primary
        assert sm.backup_path.read_bytes() == before_backup

    def test_valid_json_truncation_never_replaces_live_authority(self, temp_state_file):
        path = Path(temp_state_file)
        sm = StateManager(path)
        sm.state.add_in_flight(
            InFlightClose(con_id=123, order_id=100, remaining_qty=1, entry_debit=500.0))
        sm.state.peak_prices["123"] = 7.0
        sm.save()
        path.write_text("{}")
        with pytest.raises(StateCorruptionError, match="incomplete"):
            _ = StateManager(path).state

    @pytest.mark.parametrize("field,bad", [
        ("peak_prices", "poison"), ("mfe_pct", "poison"),
        ("mae_pct", []), ("mfe_ts", "not-a-time"),
    ])
    def test_poisoned_tracking_leaf_cannot_abort_the_protective_cycle(
            self, temp_state_file, field, bad):
        raw = {"in_flight": {}, "daily_stats": {}, "last_cycle": None,
               "peak_prices": {}}
        raw[field] = {"123": bad}
        Path(temp_state_file).write_text(json.dumps(raw))
        with pytest.raises(StateCorruptionError):
            _ = StateManager(temp_state_file).state

    def test_armed_startup_refuses_total_state_loss(self, temp_state_file):
        with pytest.raises(StateCorruptionError, match="no primary"):
            _ = StateManager(temp_state_file, require_existing=True).state



pytestmark = pytest.mark.asyncio
