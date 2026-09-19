"""Public API contract; production-derived narrative omitted."""

import pytest
from pathlib import Path


class TestKillSwitch:
    """Public API contract; production-derived narrative omitted."""

    def test_kill_switch_not_present(self, tmp_path, sample_config):
        """Public API contract; production-derived narrative omitted."""
        sample_config.kill_switch.path = str(tmp_path / "nonexistent_kill_switch")
        from exitmgr.manager import ExitManager


        manager = ExitManager(sample_config)


        is_active = manager._check_kill_switch()
        assert is_active is False

    def test_kill_switch_present(self, tmp_path, sample_config):
        """Public API contract; production-derived narrative omitted."""
        kill_switch_path = tmp_path / "KILL_SWITCH"
        kill_switch_path.write_text("STOP")
        sample_config.kill_switch.path = str(kill_switch_path)

        from exitmgr.manager import ExitManager
        manager = ExitManager(sample_config)

        is_active = manager._check_kill_switch()
        assert is_active is True

    def test_kill_switch_logs_warning(self, tmp_path, sample_config, capsys):
        """Public API contract; production-derived narrative omitted."""
        kill_switch_path = tmp_path / "KILL_SWITCH"
        kill_switch_path.write_text("STOP")
        sample_config.kill_switch.path = str(kill_switch_path)

        from exitmgr.manager import ExitManager
        manager = ExitManager(sample_config)

        manager._check_kill_switch()
        captured = capsys.readouterr()
        assert "KILL SWITCH" in captured.out
        assert "halting" in captured.out
