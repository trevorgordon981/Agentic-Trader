"""Public API contract; production-derived narrative omitted."""
from __future__ import annotations

import json
import os
import hashlib
import fcntl
from contextlib import contextmanager
from pathlib import Path


class CampaignConflictRegistryError(RuntimeError):
    """Public API contract; production-derived narrative omitted."""


def campaign_conflict_registry_path(journal_path) -> Path:
    override = os.environ.get("EXITMGR_CAMPAIGN_CONFLICT_PATH")
    return (Path(override).expanduser() if override else
            Path(journal_path).expanduser().resolve().parent
            / ".campaign-journal-conflicts.json")


def journal_sha256(journal_path) -> str:
    """Public API contract; production-derived narrative omitted."""
    try:
        return hashlib.sha256(Path(journal_path).expanduser().read_bytes()).hexdigest()
    except OSError as exc:
        raise CampaignConflictRegistryError(
            f"campaign journal is unreadable: {journal_path}: {exc}") from exc


@contextmanager
def campaign_conflict_registry_lock(path: Path, *, exclusive: bool):
    """Public API contract; production-derived narrative omitted."""
    lock_path = Path(str(path) + ".lock")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        raise CampaignConflictRegistryError(
            f"campaign-conflict registry lock failed: {lock_path}: {exc}") from exc


def active_campaign_conflict_symbols(journal_path) -> frozenset[str]:
    """Public API contract; production-derived narrative omitted."""
    path = campaign_conflict_registry_path(journal_path)
    with campaign_conflict_registry_lock(path, exclusive=False):
        before = journal_sha256(journal_path)
        try:
            payload = json.loads(path.read_text())
        except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
            raise CampaignConflictRegistryError(
                f"campaign-conflict registry is unreadable: {path}: {exc}") from exc
        after = journal_sha256(journal_path)
    if before != after:
        raise CampaignConflictRegistryError(
            "campaign journal changed while its quarantine snapshot was read")
    if not isinstance(payload, dict) or payload.get("schema") != "campaign_conflicts.v2":
        raise CampaignConflictRegistryError("campaign-conflict registry schema is invalid")
    if payload.get("journal_sha256") != before:
        raise CampaignConflictRegistryError(
            "campaign-conflict registry does not match the current journal")
    conditions = payload.get("conditions")
    if not isinstance(conditions, dict):
        raise CampaignConflictRegistryError("campaign-conflict registry conditions are invalid")
    symbols = set()
    for fingerprint, record in conditions.items():
        if not isinstance(fingerprint, str) or not isinstance(record, dict):
            raise CampaignConflictRegistryError("campaign-conflict registry record is invalid")
        if not record.get("active"):
            continue
        evidence = record.get("evidence")
        symbol = (str((evidence or {}).get("symbol") or "").strip().upper()
                  if isinstance(evidence, dict) else "")
        if not symbol:
            raise CampaignConflictRegistryError(
                "active campaign-conflict record has no bound symbol")
        symbols.add(symbol)
    return frozenset(symbols)
