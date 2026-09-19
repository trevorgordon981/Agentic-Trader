"""Public API contract; production-derived narrative omitted."""

import json
import hashlib
import os
from pathlib import Path

import pytest

from exitmgr import entry_reservation as er
from exitmgr.entry_reservation import EntryReservationError, EntryReservationLedger


def _store(path: Path, *, clock=100.0) -> EntryReservationLedger:
    return EntryReservationLedger(
        ledger_path=path, lock_path=path.with_suffix(".lock"), clock=lambda: clock)


def _state(ref="alfred-entry:one", con_id=101):
    return {
        "version": er.LEDGER_VERSION,
        "reservations": {
            ref: {
                "order_ref": ref,
                "con_id": con_id,
                "collateral_usd": 1000.0,
                "created_at": 10.0,
                "expires_at": 910.0,
            }
        },
        "intents": {},
    }


def _world(tmp_path, *, source_state=None, destination_state=None):
    legacy = tmp_path / "legacy.json"
    legacy_lock = tmp_path / "legacy.lock"
    destination = tmp_path / "durable" / "entry-reservations.json"
    receipt = tmp_path / "durable" / "entry-reservations.migration.json"
    source = _store(legacy)
    durable = _store(destination)
    if source_state is not None:
        source._write_state(source_state)
    if destination_state is not None:
        durable._write_state(destination_state)
    return source, durable, legacy_lock, receipt


def _migrate(durable, source, legacy_lock, receipt):
    durable._bootstrap_default_storage(
        legacy_entry_processes_stopped=True,
        absent_legacy_verified_empty=False,
        legacy_ledger=source.ledger_path,
        legacy_lock=legacy_lock,
        receipt_path=receipt)


def _activate_production_checks(durable, source, legacy_lock, receipt):
    durable._production_default = True
    durable._legacy_ledger_path = source.ledger_path
    durable._legacy_lock_path = legacy_lock
    durable.migration_receipt_path = receipt


def test_production_default_is_durable_but_explicit_paths_remain_exact(tmp_path, monkeypatch):
    fake_sys = type("FakeSys", (), {"modules": {}})()
    monkeypatch.setattr(er, "sys", fake_sys)
    monkeypatch.delenv("EXITMGR_ENTRY_RESERVATIONS", raising=False)
    monkeypatch.delenv("EXITMGR_ENTRY_RESERVATION_LOCK", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    assert er.default_ledger_path() == (
        tmp_path / "home/.local/var/exitmgr/entry-reservations.json")
    assert er.default_lock_path() == (
        tmp_path / "home/.local/var/exitmgr/entry-reservations.lock")

    explicit = tmp_path / "explicit.json"
    ledger = EntryReservationLedger(explicit, lock_path=tmp_path / "explicit.lock")
    assert ledger.ledger_path == explicit
    assert ledger.snapshot() == {"version": er.LEDGER_VERSION,
                                 "reservations": {}, "intents": {}}
    assert not ledger.migration_receipt_path.exists()

    env_ledger = tmp_path / "env" / "ledger.json"
    env_lock = tmp_path / "env" / "ledger.lock"
    monkeypatch.setenv("EXITMGR_ENTRY_RESERVATIONS", str(env_ledger))
    monkeypatch.setenv("EXITMGR_ENTRY_RESERVATION_LOCK", str(env_lock))
    assert er.default_ledger_path() == env_ledger and er.default_lock_path() == env_lock
    with pytest.raises(EntryReservationError, match="migration receipt"):
        EntryReservationLedger()
    assert not env_ledger.exists(), "an environment redirect must not bypass explicit cutover"


def test_legacy_evidence_is_migrated_once_and_legacy_is_preserved(tmp_path):
    source, durable, legacy_lock, receipt = _world(tmp_path, source_state=_state())
    expected = source.snapshot()
    source_before = source.ledger_path.read_bytes()

    _migrate(durable, source, legacy_lock, receipt)
    destination_before = durable.ledger_path.read_bytes()
    receipt_before = receipt.read_bytes()

    snapshot = durable.snapshot()
    assert snapshot["reservations"] == expected["reservations"]
    assert snapshot["migration_origin_sha256"]
    assert snapshot["ledger_generation"] == 0
    fence = json.loads(source.ledger_path.read_text())
    assert fence["schema"] == er.LEGACY_FENCE_SCHEMA
    assert durable.legacy_backup_path.read_bytes() == source_before
    assert hashlib.sha256(source_before).hexdigest() == fence["legacy_source_bytes_sha256"]
    assert os.stat(durable.ledger_path).st_mode & 0o777 == 0o600
    assert os.stat(durable.legacy_backup_path).st_mode & 0o777 == 0o600
    assert os.stat(receipt).st_mode & 0o777 == 0o600

    _migrate(durable, source, legacy_lock, receipt)
    assert durable.ledger_path.read_bytes() == destination_before
    assert receipt.read_bytes() == receipt_before
    assert durable.legacy_backup_path.read_bytes() == source_before


def test_equal_legacy_destination_recovers_a_crash_before_receipt_and_adds_lineage(tmp_path):
    state = _state()
    source, durable, legacy_lock, receipt = _world(
        tmp_path, source_state=state, destination_state=state)
    _migrate(durable, source, legacy_lock, receipt)

    assert receipt.exists()
    snapshot = durable.snapshot()
    assert snapshot["migration_origin_sha256"]
    assert snapshot["ledger_generation"] == 0


def test_receipt_write_failure_is_idempotently_recoverable(tmp_path, monkeypatch):
    source, durable, legacy_lock, receipt = _world(tmp_path, source_state=_state())
    source_before = source.ledger_path.read_bytes()
    original = durable._write_json_atomic

    def fail_receipt(path, payload):
        if Path(path) == receipt:
            raise OSError("simulated receipt fsync failure")
        return original(path, payload)

    monkeypatch.setattr(durable, "_write_json_atomic", fail_receipt)
    with pytest.raises(OSError, match="simulated receipt"):
        _migrate(durable, source, legacy_lock, receipt)
    assert durable.ledger_path.exists() and not receipt.exists()
    assert durable.legacy_backup_path.read_bytes() == source_before
    assert json.loads(source.ledger_path.read_text())["schema"] == er.LEGACY_FENCE_SCHEMA
    destination_before = durable.ledger_path.read_bytes()

    monkeypatch.setattr(durable, "_write_json_atomic", original)
    _migrate(durable, source, legacy_lock, receipt)
    assert receipt.exists()
    assert durable.ledger_path.read_bytes() == destination_before


def test_backup_failure_leaves_legacy_source_and_destination_untouched(tmp_path, monkeypatch):
    source, durable, legacy_lock, receipt = _world(tmp_path, source_state=_state())
    source_before = source.ledger_path.read_bytes()
    monkeypatch.setattr(
        durable, "_write_bytes_create_only",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("backup fsync failed")))

    with pytest.raises(OSError, match="backup fsync"):
        _migrate(durable, source, legacy_lock, receipt)

    assert source.ledger_path.read_bytes() == source_before
    assert not durable.ledger_path.exists()
    assert not receipt.exists()


def test_fence_write_failure_recovers_from_exact_create_only_backup(tmp_path, monkeypatch):
    source, durable, legacy_lock, receipt = _world(tmp_path, source_state=_state())
    source_before = source.ledger_path.read_bytes()
    original_write = durable._write_json_atomic

    def fail_fence(path, payload):
        if Path(path) == source.ledger_path and payload.get("schema") == er.LEGACY_FENCE_SCHEMA:
            raise OSError("fence fsync failed")
        return original_write(path, payload)

    monkeypatch.setattr(durable, "_write_json_atomic", fail_fence)
    with pytest.raises(OSError, match="fence fsync"):
        _migrate(durable, source, legacy_lock, receipt)
    assert source.ledger_path.read_bytes() == source_before
    assert durable.legacy_backup_path.read_bytes() == source_before
    assert durable.ledger_path.exists() and not receipt.exists()

    monkeypatch.setattr(durable, "_write_json_atomic", original_write)
    _migrate(durable, source, legacy_lock, receipt)
    assert durable.legacy_backup_path.read_bytes() == source_before
    assert json.loads(source.ledger_path.read_text())["schema"] == er.LEGACY_FENCE_SCHEMA
    assert receipt.exists()


def test_different_existing_destination_is_never_overwritten(tmp_path):
    source, durable, legacy_lock, receipt = _world(
        tmp_path, source_state=_state(),
        destination_state=_state("alfred-entry:different", 202))
    source_before = source.ledger_path.read_bytes()
    destination_before = durable.ledger_path.read_bytes()

    with pytest.raises(EntryReservationError, match="evidence differ"):
        _migrate(durable, source, legacy_lock, receipt)

    assert source.ledger_path.read_bytes() == source_before
    assert durable.ledger_path.read_bytes() == destination_before
    assert not receipt.exists()


@pytest.mark.parametrize("destination_exists", [False, True])
def test_missing_unreceipted_source_never_becomes_an_empty_history(
        tmp_path, destination_exists):
    destination_state = _state() if destination_exists else None
    source, durable, legacy_lock, receipt = _world(
        tmp_path, destination_state=destination_state)
    destination_before = (durable.ledger_path.read_bytes()
                          if destination_exists else None)

    with pytest.raises(EntryReservationError, match="legacy"):
        _migrate(durable, source, legacy_lock, receipt)

    assert not receipt.exists()
    if destination_exists:
        assert durable.ledger_path.read_bytes() == destination_before
    else:
        assert not durable.ledger_path.exists()


def test_a_changed_legacy_history_after_migration_fails_closed(tmp_path):
    source, durable, legacy_lock, receipt = _world(tmp_path, source_state=_state())
    _migrate(durable, source, legacy_lock, receipt)
    destination_before = durable.ledger_path.read_bytes()
    source._write_state(_state("alfred-entry:late-old-process", 303))

    with pytest.raises(EntryReservationError, match="not fenced"):
        _migrate(durable, source, legacy_lock, receipt)

    assert durable.ledger_path.read_bytes() == destination_before


def test_running_default_rechecks_legacy_under_the_admission_lock(tmp_path):
    source, durable, legacy_lock, receipt = _world(tmp_path, source_state=_state())
    _migrate(durable, source, legacy_lock, receipt)
    _activate_production_checks(durable, source, legacy_lock, receipt)
    source._write_state(_state("alfred-entry:raced-old-process", 404))

    with pytest.raises(EntryReservationError, match="not fenced"):
        durable.snapshot()


def test_runtime_fails_closed_but_explicit_stopped_service_bootstrap_refences_after_temp_cleanup(
        tmp_path):
    source, durable, legacy_lock, receipt = _world(tmp_path, source_state=_state())
    source_before = source.ledger_path.read_bytes()
    expected = source.snapshot()
    _migrate(durable, source, legacy_lock, receipt)
    destination_before = durable.ledger_path.read_bytes()
    source.ledger_path.unlink()
    _activate_production_checks(durable, source, legacy_lock, receipt)

    with pytest.raises(EntryReservationError, match="fence is missing"):
        durable.snapshot()

    _migrate(durable, source, legacy_lock, receipt)
    assert durable.ledger_path.read_bytes() == destination_before
    assert durable.legacy_backup_path.read_bytes() == source_before
    assert json.loads(source.ledger_path.read_text())["schema"] == er.LEGACY_FENCE_SCHEMA
    assert durable.snapshot()["reservations"] == expected["reservations"]


def test_missing_durable_ledger_after_receipt_is_not_recreated_from_stale_temp(tmp_path):
    source, durable, legacy_lock, receipt = _world(tmp_path, source_state=_state())
    _migrate(durable, source, legacy_lock, receipt)
    durable.ledger_path.unlink()

    with pytest.raises(EntryReservationError, match="missing after completed migration"):
        _migrate(durable, source, legacy_lock, receipt)

    assert not durable.ledger_path.exists()


@pytest.mark.parametrize("corrupt_target", ["legacy", "receipt"])
def test_corrupt_migration_evidence_refuses_without_touching_destination(
        tmp_path, corrupt_target):
    source, durable, legacy_lock, receipt = _world(tmp_path, source_state=_state())
    if corrupt_target == "receipt":
        _migrate(durable, source, legacy_lock, receipt)
        receipt.write_text("{not-json")
    else:
        source.ledger_path.write_text("{not-json")
    destination_before = (durable.ledger_path.read_bytes()
                          if durable.ledger_path.exists() else None)

    with pytest.raises(EntryReservationError, match="unreadable"):
        _migrate(durable, source, legacy_lock, receipt)

    assert ((durable.ledger_path.read_bytes() if durable.ledger_path.exists() else None)
            == destination_before)


def test_runtime_never_auto_migrates_without_explicit_bootstrap(tmp_path, monkeypatch):
    fake_sys = type("FakeSys", (), {"modules": {}})()
    monkeypatch.setattr(er, "sys", fake_sys)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("EXITMGR_ENTRY_RESERVATIONS", raising=False)
    monkeypatch.delenv("EXITMGR_ENTRY_RESERVATION_LOCK", raising=False)
    monkeypatch.setattr(er, "legacy_ledger_path", lambda: tmp_path / "legacy.json")
    monkeypatch.setattr(er, "legacy_lock_path", lambda: tmp_path / "legacy.lock")

    with pytest.raises(EntryReservationError, match="migration receipt"):
        EntryReservationLedger()
    assert not er.default_ledger_path().exists()


def test_absent_legacy_bootstrap_requires_both_attestations_and_fences_old_code(
        tmp_path, monkeypatch):
    fake_sys = type("FakeSys", (), {"modules": {}})()
    monkeypatch.setattr(er, "sys", fake_sys)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("EXITMGR_ENTRY_RESERVATIONS", raising=False)
    monkeypatch.delenv("EXITMGR_ENTRY_RESERVATION_LOCK", raising=False)
    legacy = tmp_path / "legacy.json"
    legacy_lock = tmp_path / "legacy.lock"
    monkeypatch.setattr(er, "legacy_ledger_path", lambda: legacy)
    monkeypatch.setattr(er, "legacy_lock_path", lambda: legacy_lock)

    with pytest.raises(EntryReservationError, match="processes_stopped"):
        EntryReservationLedger(bootstrap_default=True)
    with pytest.raises(EntryReservationError, match="verified_empty"):
        EntryReservationLedger(
            bootstrap_default=True, legacy_entry_processes_stopped=True)

    bootstrapped = EntryReservationLedger(
        bootstrap_default=True, legacy_entry_processes_stopped=True,
        absent_legacy_verified_empty=True)
    assert bootstrapped.snapshot()["reservations"] == {}
    fence = json.loads(legacy.read_text())
    assert fence["schema"] == er.LEGACY_FENCE_SCHEMA

    old_binary_view = EntryReservationLedger(ledger_path=legacy, lock_path=legacy_lock)
    with pytest.raises(EntryReservationError, match="unsupported schema"):
        old_binary_view.snapshot()
    assert EntryReservationLedger().snapshot()["ledger_generation"] == 0


def test_receipt_binding_rejects_valid_schema_rollback_and_replacement(tmp_path):
    source, durable, legacy_lock, receipt = _world(tmp_path, source_state=_state())
    _migrate(durable, source, legacy_lock, receipt)
    _activate_production_checks(durable, source, legacy_lock, receipt)
    generation_zero = durable.ledger_path.read_bytes()

    assert durable.clear_not_transmitted("alfred-entry:one") is True
    assert durable.snapshot()["ledger_generation"] == 1


    durable.ledger_path.write_bytes(generation_zero)
    with pytest.raises(EntryReservationError, match="rollback or replacement"):
        durable.snapshot()


def test_contradictory_legacy_presence_receipt_is_rejected(tmp_path):
    source, durable, legacy_lock, receipt = _world(tmp_path, source_state=_state())
    _migrate(durable, source, legacy_lock, receipt)
    payload = json.loads(receipt.read_text())
    assert payload["legacy_source_present"] is True
    payload["legacy_was_absent"] = True
    receipt.write_text(json.dumps(payload))
    _activate_production_checks(durable, source, legacy_lock, receipt)

    with pytest.raises(EntryReservationError, match="contradictory legacy presence"):
        durable.snapshot()


@pytest.mark.parametrize("field", ["legacy_source_present", "legacy_was_absent"])
def test_receipt_presence_evidence_requires_json_booleans(tmp_path, field):
    source, durable, legacy_lock, receipt = _world(tmp_path, source_state=_state())
    _migrate(durable, source, legacy_lock, receipt)
    payload = json.loads(receipt.read_text())
    payload[field] = 1
    receipt.write_text(json.dumps(payload))
    _activate_production_checks(durable, source, legacy_lock, receipt)

    with pytest.raises(EntryReservationError, match="must be a bool"):
        durable.snapshot()


def test_receipt_binding_rejects_changed_exact_legacy_backup(tmp_path):
    source, durable, legacy_lock, receipt = _world(tmp_path, source_state=_state())
    _migrate(durable, source, legacy_lock, receipt)
    _activate_production_checks(durable, source, legacy_lock, receipt)
    durable.legacy_backup_path.write_bytes(b"valid-json-is-not-the-original-source\n")

    with pytest.raises(EntryReservationError, match="backup does not match"):
        durable.snapshot()


def test_multiple_bound_writes_advance_one_receipt_generation_at_a_time(tmp_path):
    source, durable, legacy_lock, receipt = _world(tmp_path, source_state=_state())
    _migrate(durable, source, legacy_lock, receipt)
    _activate_production_checks(durable, source, legacy_lock, receipt)

    with durable._locked():
        state = durable._load_state()
        row = state["reservations"].pop("alfred-entry:one")
        durable._write_state(state)
        state["reservations"]["alfred-entry:one"] = row
        durable._write_state(state)

    snapshot = durable.snapshot()
    bound = json.loads(receipt.read_text())
    assert snapshot["ledger_generation"] == 2
    assert bound["active_generation"] == 2
    assert "alfred-entry:one" in snapshot["reservations"]
    assert bound["active_state_sha256"] == durable._active_state_sha256(snapshot)


def test_legacy_optional_reservation_keys_preserve_receipt_bound_active_hash(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    ledger = _store(tmp_path / "entry-reservations.json")
    ledger._write_state(_state())
    legacy = ledger.snapshot()
    row = legacy["reservations"]["alfred-entry:one"]
    row.pop("campaign_add_intent", None)
    row.pop("admission_receipt", None)
    legacy.update({
        "migration_origin_sha256": "a" * 64,
        "ledger_generation": 0,
        "previous_state_sha256": "b" * 64,
    })
    expected_hash = ledger._active_state_sha256(legacy)
    ledger._write_json_atomic(ledger.ledger_path, legacy)

    decoded = ledger._load_state()
    decoded_row = decoded["reservations"]["alfred-entry:one"]
    assert "campaign_add_intent" not in decoded_row
    assert "admission_receipt" not in decoded_row
    assert ledger._active_state_sha256(decoded) == expected_hash


def test_receipt_update_crash_repairs_only_one_proven_forward_generation(tmp_path, monkeypatch):
    source, durable, legacy_lock, receipt = _world(tmp_path, source_state=_state())
    _migrate(durable, source, legacy_lock, receipt)
    _activate_production_checks(durable, source, legacy_lock, receipt)
    original_write = durable._write_json_atomic
    failed = {"done": False}

    def fail_first_receipt_update(path, payload):
        if (Path(path) == receipt and payload.get("active_generation") == 1
                and not failed["done"]):
            failed["done"] = True
            raise OSError("simulated active-receipt fsync failure")
        return original_write(path, payload)

    monkeypatch.setattr(durable, "_write_json_atomic", fail_first_receipt_update)
    with pytest.raises(OSError, match="active-receipt"):
        durable.clear_not_transmitted("alfred-entry:one")
    monkeypatch.setattr(durable, "_write_json_atomic", original_write)

    assert durable.snapshot()["ledger_generation"] == 1
    repaired = json.loads(receipt.read_text())
    assert repaired["active_generation"] == 1
