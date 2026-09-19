"""Public API contract; production-derived narrative omitted."""
import json
import math
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import time

from . import entry_reservation as er


def _boot_epoch():
    result = subprocess.run(
        ["/usr/sbin/sysctl", "-n", "kern.boottime"], capture_output=True,
        text=True, check=True, timeout=5)
    match = re.search(r"sec\s*=\s*(\d+)\s*,\s*usec\s*=\s*(\d+)", result.stdout)
    if not match:
        raise er.EntryReservationError("cannot verify host reboot time")
    return int(match[1]) + int(match[2]) / 1_000_000


def _other_entry_processes():
    result = subprocess.run(
        ["/bin/ps", "-axo", "pid=,command="], capture_output=True,
        text=True, check=True, timeout=5)
    entry_scripts = {
        "daily_recommend.py", "place_trade.py", "run_SYMG_directed.sh",
        "run_trader_service.sh", "run_daily_recommend.sh", "slate-now.sh",
    }
    found = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        pid = int(parts[0])
        if pid == os.getpid():
            continue
        if not any(name in parts[1] for name in entry_scripts | {"run_trader.py"}):
            continue
        try:
            args = shlex.split(parts[1])
        except ValueError as exc:
            raise er.EntryReservationError("cannot verify entry process inventory") from exc
        names = {Path(arg).name for arg in args}
        protective = "--mode=protective" in args or any(
            a == "--mode" and b == "protective" for a, b in zip(args, args[1:]))
        if names & entry_scripts or ("run_trader.py" in names and not protective):
            found.append(pid)
    return found


def _publish_missing_fence(path, payload):
    """Public API contract; production-derived narrative omitted."""
    fd, temporary = tempfile.mkstemp(prefix=".entry-fence-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
    finally:
        os.unlink(temporary)


def _restore_completed_fence(ledger, *, boot_epoch, process_probe):
    if not math.isfinite(boot_epoch) or not 0 < boot_epoch <= time.time():
        raise er.EntryReservationError("invalid host reboot time")
    legacy = ledger._legacy_ledger_path
    receipt = ledger.migration_receipt_path
    with er._exclusive_lock(ledger.lock_path, ledger.lock_timeout_seconds,
                            label="reboot fence durable evidence"):
        with er._exclusive_lock(ledger._legacy_lock_path, ledger.lock_timeout_seconds,
                                label="reboot fence legacy evidence"):
            if legacy.exists() or legacy.is_symlink():
                return False
            for path in (ledger.ledger_path, receipt):
                if path.is_symlink() or not path.is_file():
                    raise er.EntryReservationError("reboot fence recovery requires existing durable evidence")
                info = path.stat()
                if info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise er.EntryReservationError("reboot fence durable evidence must be owner-only")
                if info.st_mtime >= boot_epoch:
                    raise er.EntryReservationError("missing fence is not proven to predate this boot")
            bound = ledger._read_migration_receipt(receipt, legacy=legacy)
            if not 0 < bound["migrated_at"] < boot_epoch:
                raise er.EntryReservationError("migration does not predate this boot")


            ledger._validate_active_binding_locked(bound, repair_forward=False)
            if process_probe():
                raise er.EntryReservationError("other entry-capable processes prevent fence recovery")
            _publish_missing_fence(legacy, ledger._legacy_fence_payload(bound, receipt=receipt))
            return True


def ensure_entry_startup_fence():
    """Public API contract; production-derived narrative omitted."""
    legacy = er.legacy_ledger_path()
    if legacy.exists() or legacy.is_symlink():
        return False
    if any(os.environ.get(k) for k in (
        "EXITMGR_ENTRY_RESERVATIONS", "EXITMGR_ENTRY_RESERVATION_LOCK")):
        raise er.EntryReservationError("automatic reboot recovery refuses storage overrides")
    ledger = er.EntryReservationLedger(
        ledger_path=er.default_ledger_path(), lock_path=er.default_lock_path())
    return _restore_completed_fence(
        ledger, boot_epoch=_boot_epoch(), process_probe=_other_entry_processes)
