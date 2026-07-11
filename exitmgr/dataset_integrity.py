"""Machine-readable canonical/legacy/invalid policy for trade-dataset consumers."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from typing import Any, Dict, Optional, Tuple


CANONICAL = "CANONICAL"
LEGACY = "LEGACY"
INVALID = "INVALID"
ESTIMATE = "ESTIMATE"


def mark(row: Dict[str, Any], *, status: str, training: bool, pnl: bool,
         reason: Optional[str] = None) -> Dict[str, Any]:
    row["record_status"] = status
    row["canonical"] = status == CANONICAL
    row["usable_for_training"] = bool(training)
    row["usable_for_pnl"] = bool(pnl)
    if not training:
        row["not_for_training_reason"] = reason or status.lower()
    if not pnl:
        row["not_for_pnl_reason"] = reason or status.lower()
    return row


def allowed(row: Dict[str, Any], purpose: str) -> Tuple[bool, Optional[str]]:
    """Fail closed unless status and the requested purpose are explicitly canonical/true."""
    field = "usable_for_training" if purpose == "training" else "usable_for_pnl"
    status = str(row.get("record_status") or "").upper()
    if status != CANONICAL or row.get("canonical") is not True:
        return False, status.lower() if status else "missing canonical record status"
    if row.get(field) is not True:
        return False, row.get(f"not_for_{purpose}_reason") or f"{field} is not explicitly true"
    return True, None


def quarantine_path(dataset_path: str) -> str:
    stem, _ = os.path.splitext(dataset_path)
    return stem + ".quarantine.jsonl"


def _key(row: Dict[str, Any]) -> str:
    existing = row.get("_dedup_key")
    if existing:
        return str(existing)
    return hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()


def migrate_ledger(dataset_path: str, *, dry_run: bool = False) -> Dict[str, Any]:
    """Move every unmarked/ambiguous record out of the canonical ledger atomically.

    A record remains only when it explicitly declares CANONICAL and includes boolean policy flags
    for both training and P&L. Everything else is retained in the quarantine sidecar as LEGACY or
    INVALID with both use flags false.
    """
    rows = []
    if os.path.exists(dataset_path):
        with open(dataset_path) as stream:
            for line in stream:
                if not line.strip():
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    rows.append({"raw_line_sha256": hashlib.sha256(line.encode()).hexdigest(),
                                 "quarantine_reason": "malformed JSONL record"})
    canonical_rows, legacy_rows = [], []
    for source in rows:
        explicit = (source.get("record_status") == CANONICAL
                    and source.get("canonical") is True
                    and isinstance(source.get("usable_for_training"), bool)
                    and isinstance(source.get("usable_for_pnl"), bool))
        if explicit:
            canonical_rows.append(source)
            continue
        row = dict(source)
        invalid = str(row.get("record_status") or "").upper() == INVALID
        reason = row.get("quarantine_reason") or "pre-canonical or incompletely marked ledger row"
        mark(row, status=(INVALID if invalid else LEGACY), training=False, pnl=False, reason=reason)
        original = _key(row)
        row["quarantine_original_key"] = original
        row["_dedup_key"] = f"quarantine:{original}"
        legacy_rows.append(row)
    result = {"before": len(rows), "canonical": len(canonical_rows),
              "migrated": len(legacy_rows), "quarantine_path": quarantine_path(dataset_path),
              "dry_run": dry_run}
    if dry_run or not legacy_rows:
        return result
    qpath = quarantine_path(dataset_path)
    existing = {}
    if os.path.exists(qpath):
        with open(qpath) as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                    existing[_key(row)] = row
                except (json.JSONDecodeError, TypeError):
                    continue
    for row in legacy_rows:
        existing[_key(row)] = row
    os.makedirs(os.path.dirname(dataset_path) or ".", exist_ok=True)
    backup = dataset_path + ".pre-canonical.bak"
    if os.path.exists(dataset_path) and not os.path.exists(backup):
        shutil.copy2(dataset_path, backup)
    qtmp = qpath + ".tmp"
    with open(qtmp, "w") as stream:
        for row in existing.values():
            stream.write(json.dumps(row, default=str) + "\n")
    os.replace(qtmp, qpath)
    tmp = dataset_path + ".canonical.tmp"
    with open(tmp, "w") as stream:
        for row in canonical_rows:
            stream.write(json.dumps(row, default=str) + "\n")
    os.replace(tmp, dataset_path)
    return result
