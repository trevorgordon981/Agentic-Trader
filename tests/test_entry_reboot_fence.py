import hashlib
import json
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from exitmgr import entry_startup as startup
from exitmgr.entry_reservation import EntryReservationError
from tests.test_entry_reservation_migration import (
    _world, _state, _migrate, _activate_production_checks,
)


@pytest.fixture
def migrated(tmp_path):
    source, ledger, lock, receipt = _world(tmp_path, source_state=_state())
    _migrate(ledger, source, lock, receipt)
    _activate_production_checks(ledger, source, lock, receipt)
    for path in (ledger.ledger_path, receipt):
        os.utime(path, (200, 200))
    return ledger


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_reboot_restores_only_fence_and_preserves_all_reservations(migrated):
    ledger = migrated
    paths = [ledger.ledger_path, ledger.migration_receipt_path, ledger.legacy_backup_path]
    before = [digest(path) for path in paths]
    ledger._legacy_ledger_path.unlink()
    assert startup._restore_completed_fence(ledger, boot_epoch=1000, process_probe=lambda: [])
    assert [digest(path) for path in paths] == before
    assert ledger.snapshot()["reservations"]["alfred-entry:one"]["collateral_usd"] == 1000
    assert ledger._read_legacy_fence(ledger._legacy_ledger_path)["schema"].endswith(".v1")
    assert ledger._legacy_ledger_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("broken", ["ledger_missing", "receipt_missing", "backup_changed",
    "ledger_changed", "receipt_changed", "ledger_after_boot", "receipt_after_boot",
    "recent_migration", "public_ledger", "symlink_ledger", "other_entry"])
def test_ambiguous_evidence_never_creates_a_fence(migrated, broken, tmp_path):
    ledger = migrated
    ledger._legacy_ledger_path.unlink()
    if broken == "ledger_missing": ledger.ledger_path.unlink()
    elif broken == "receipt_missing": ledger.migration_receipt_path.unlink()
    elif broken == "backup_changed": ledger.legacy_backup_path.write_text("changed")
    elif broken in {"ledger_changed", "receipt_changed", "recent_migration"}:
        path = ledger.ledger_path if broken == "ledger_changed" else ledger.migration_receipt_path
        data = json.loads(path.read_text())
        if broken == "ledger_changed": data["reservations"] = {}
        elif broken == "receipt_changed": data["source_evidence_sha256"] = "0" * 64
        else: data["migrated_at"] = 1001
        path.write_text(json.dumps(data)); os.utime(path, (200, 200))
    elif broken == "ledger_after_boot": os.utime(ledger.ledger_path, (1001, 1001))
    elif broken == "receipt_after_boot": os.utime(ledger.migration_receipt_path, (1001, 1001))
    elif broken == "public_ledger": os.chmod(ledger.ledger_path, 0o644)
    elif broken == "symlink_ledger":
        target = tmp_path / "other-ledger"; ledger.ledger_path.rename(target)
        ledger.ledger_path.symlink_to(target)
    before = {p: p.read_bytes() for p in [ledger.ledger_path, ledger.migration_receipt_path,
                                        ledger.legacy_backup_path] if p.exists()}
    with pytest.raises(EntryReservationError):
        startup._restore_completed_fence(ledger, boot_epoch=1000,
            process_probe=lambda: [99] if broken == "other_entry" else [])
    assert not ledger._legacy_ledger_path.exists()
    assert all(p.read_bytes() == data for p, data in before.items())


def test_existing_fence_is_untouched(migrated):
    path = migrated._legacy_ledger_path
    before = path.read_bytes()
    probe = Mock(side_effect=AssertionError("must not probe"))
    assert not startup._restore_completed_fence(migrated, boot_epoch=1000, process_probe=probe)
    assert path.read_bytes() == before


def test_atomic_publish_never_overwrites_a_concurrent_legacy_record(tmp_path):
    path = tmp_path / "fence"; path.write_text("existing intent")
    with pytest.raises(FileExistsError): startup._publish_missing_fence(path, {"new": True})
    assert path.read_text() == "existing intent"
    assert not list(tmp_path.glob(".entry-fence-*"))


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), 9e20])
def test_invalid_boot_proof_fails_closed(migrated, value):
    migrated._legacy_ledger_path.unlink()
    with pytest.raises(EntryReservationError):
        startup._restore_completed_fence(migrated, boot_epoch=value, process_probe=lambda: [])
    assert not migrated._legacy_ledger_path.exists()


def test_process_probe_excludes_only_self_and_protective(monkeypatch):
    monkeypatch.setattr(startup.os, "getpid", lambda: 77)
    output = "\n".join([
        "77 python run_trader.py --arm --mode entry",
        "78 python run_trader.py --arm --mode protective",
        "79 python run_trader.py --arm --mode=protective",
        "80 python run_trader.py --arm --mode entry",
        "81 python /a/daily_recommend.py --watch-mins 1",
        "82 python place_trade.py", "83 launchd", "84 unrelated 'unterminated",
    ])
    monkeypatch.setattr(startup.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=output))
    assert startup._other_entry_processes() == [80, 81, 82]


def test_boot_epoch_parser_requires_native_proof(monkeypatch):
    monkeypatch.setattr(startup.subprocess, "run", lambda *a, **k:
        SimpleNamespace(stdout="{ sec = 1000, usec = 500000 } Thu Sep 17"))
    assert startup._boot_epoch() == 1000.5
    monkeypatch.setattr(startup.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="unknown"))
    with pytest.raises(EntryReservationError): startup._boot_epoch()


def test_previously_verified_absent_legacy_source_is_restored_without_new_attestation(tmp_path):
    source, ledger, lock, receipt = _world(tmp_path)
    ledger._bootstrap_default_storage(legacy_entry_processes_stopped=True,
        absent_legacy_verified_empty=True, legacy_ledger=source.ledger_path,
        legacy_lock=lock, receipt_path=receipt)
    _activate_production_checks(ledger, source, lock, receipt)
    before = [digest(p) for p in (ledger.ledger_path, receipt)]
    for p in (ledger.ledger_path, receipt): os.utime(p, (200, 200))
    source.ledger_path.unlink()
    assert startup._restore_completed_fence(ledger, boot_epoch=1000, process_probe=lambda: [])
    assert [digest(p) for p in (ledger.ledger_path, receipt)] == before
    assert json.loads(source.ledger_path.read_text())["legacy_source_present"] is False


@pytest.mark.parametrize("stamp", [0, -1])
def test_nonpositive_migration_time_is_not_historical_proof(migrated, stamp):
    migrated._legacy_ledger_path.unlink()
    path = migrated.migration_receipt_path
    data = json.loads(path.read_text()); data["migrated_at"] = stamp
    path.write_text(json.dumps(data)); os.utime(path, (200, 200))
    with pytest.raises(EntryReservationError):
        startup._restore_completed_fence(migrated, boot_epoch=1000, process_probe=lambda: [])
    assert not migrated._legacy_ledger_path.exists()


def test_storage_override_is_not_automatically_bootstrapped(tmp_path, monkeypatch):
    monkeypatch.setattr(startup.er, "legacy_ledger_path", lambda: tmp_path / "missing")
    monkeypatch.setenv("EXITMGR_ENTRY_RESERVATIONS", str(tmp_path / "custom.json"))
    with pytest.raises(EntryReservationError, match="overrides"):
        startup.ensure_entry_startup_fence()
    assert not (tmp_path / "custom.json").exists()


@pytest.mark.parametrize("arm,mode,called", [(True,"entry",True), (False,"entry",False),
    (True,"protective",False), (False,"protective",False), (True,"combined",False)])
def test_hook_is_exclusive_to_armed_entry_startup(monkeypatch, arm, mode, called):
    from run_trader import _prepare_entry_fence
    ensure = Mock(return_value=True)
    monkeypatch.setattr(startup, "ensure_entry_startup_fence", ensure)
    assert _prepare_entry_fence(arm, mode) is called
    assert ensure.call_count == int(called)
