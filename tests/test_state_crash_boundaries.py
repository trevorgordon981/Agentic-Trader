"""Public API contract; production-derived narrative omitted."""

from pathlib import Path

import pytest

from exitmgr import state as state_mod
from exitmgr.state import StateCorruptionError, StateManager


class InjectedCrash(BaseException):
    """Public API contract; production-derived narrative omitted."""


def _seed_two_generations(tmp_path):
    path = tmp_path / "exitmgr_state.json"
    manager = StateManager(path)
    manager.state.peak_prices["101"] = 1.0
    manager.save()
    manager.state.peak_prices["101"] = 2.0
    manager.save()

    assert StateManager._read_path(path).peak_prices == {"101": 2.0}
    assert StateManager._read_path(manager.backup_path).peak_prices == {"101": 1.0}
    manager.state.peak_prices["101"] = 3.0
    return manager


def _generation(path):
    """Public API contract; production-derived narrative omitted."""
    return StateManager._read_path(Path(path)).peak_prices["101"]


def test_crash_during_temp_fsync_keeps_primary_authoritative(monkeypatch, tmp_path):
    manager = _seed_two_generations(tmp_path)
    real_fsync = state_mod.os.fsync
    calls = 0

    def crash_on_temp_fsync(fd):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise InjectedCrash("temp fsync")
        return real_fsync(fd)

    monkeypatch.setattr(state_mod.os, "fsync", crash_on_temp_fsync)
    with pytest.raises(InjectedCrash, match="temp fsync"):
        manager.save()


    assert _generation(manager.state_path) == 2.0
    assert _generation(manager.backup_path) == 1.0
    assert StateManager(manager.state_path).state.peak_prices == {"101": 2.0}


def test_crash_at_backup_rename_keeps_both_committed_generations(
        monkeypatch, tmp_path):
    manager = _seed_two_generations(tmp_path)
    real_replace = state_mod.os.replace

    def crash_at_backup_rename(source, destination):
        if Path(destination) == manager.backup_path:
            raise InjectedCrash("backup rename")
        return real_replace(source, destination)

    monkeypatch.setattr(state_mod.os, "replace", crash_at_backup_rename)
    with pytest.raises(InjectedCrash, match="backup rename"):
        manager.save()

    assert _generation(manager.state_path) == 2.0
    assert _generation(manager.backup_path) == 1.0
    assert StateManager(manager.state_path).state.peak_prices == {"101": 2.0}


def test_crash_at_backup_directory_fsync_preserves_old_primary(
        monkeypatch, tmp_path):
    manager = _seed_two_generations(tmp_path)
    calls = 0

    def crash_at_first_directory_fsync():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise InjectedCrash("backup directory fsync")
        raise AssertionError("primary rename must not be reached")

    monkeypatch.setattr(manager, "_fsync_state_directory", crash_at_first_directory_fsync)
    with pytest.raises(InjectedCrash, match="backup directory fsync"):
        manager.save()




    assert _generation(manager.state_path) == 2.0
    assert _generation(manager.backup_path) in {1.0, 2.0}
    assert StateManager(manager.state_path).state.peak_prices == {"101": 2.0}


def test_crash_at_primary_rename_leaves_old_primary_and_valid_backup(
        monkeypatch, tmp_path):
    manager = _seed_two_generations(tmp_path)
    real_replace = state_mod.os.replace

    def crash_at_primary_rename(source, destination):
        if Path(destination) == manager.state_path:
            raise InjectedCrash("primary rename")
        return real_replace(source, destination)

    monkeypatch.setattr(state_mod.os, "replace", crash_at_primary_rename)
    with pytest.raises(InjectedCrash, match="primary rename"):
        manager.save()


    assert _generation(manager.state_path) == 2.0
    assert _generation(manager.backup_path) == 2.0
    assert StateManager(manager.state_path).state.peak_prices == {"101": 2.0}


def test_crash_at_primary_directory_fsync_always_leaves_a_valid_generation(
        monkeypatch, tmp_path):
    manager = _seed_two_generations(tmp_path)
    calls = 0

    def crash_at_second_directory_fsync():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise InjectedCrash("primary directory fsync")

    monkeypatch.setattr(manager, "_fsync_state_directory", crash_at_second_directory_fsync)
    with pytest.raises(InjectedCrash, match="primary directory fsync"):
        manager.save()



    assert _generation(manager.state_path) == 3.0
    assert _generation(manager.backup_path) == 2.0
    assert StateManager(manager.state_path).state.peak_prices == {"101": 3.0}


@pytest.mark.parametrize("corrupt_bytes", [b"{", b'{"state_schema":'])
def test_partial_primary_is_never_loaded_or_replaced_by_stale_backup(
        tmp_path, corrupt_bytes):
    manager = _seed_two_generations(tmp_path)
    manager.state_path.write_bytes(corrupt_bytes)

    with pytest.raises(StateCorruptionError, match="NOT auto-adopted"):
        _ = StateManager(manager.state_path, require_existing=True).state
    assert _generation(manager.backup_path) == 1.0
