"""Public API contract; production-derived narrative omitted."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import fcntl
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple


CANONICAL = "CANONICAL"
LEGACY = "LEGACY"
INVALID = "INVALID"
ESTIMATE = "ESTIMATE"
TRAINING_EXCLUSION_SCHEMA = "trade_dataset.training_exclusion.v1"


class DatasetIntegrityError(RuntimeError):
    """Public API contract; production-derived narrative omitted."""


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
    """Public API contract; production-derived narrative omitted."""
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


def training_exclusions_path(dataset_path: str) -> str:
    stem, _ = os.path.splitext(dataset_path)
    return stem + ".training_exclusions.jsonl"


def raw_jsonl_line_sha256(raw_line: bytes) -> str:
    """Public API contract; production-derived narrative omitted."""
    if not isinstance(raw_line, (bytes, bytearray)):
        raise TypeError("raw JSONL line must be bytes")
    raw = bytes(raw_line)
    if raw.endswith(b"\n"):
        raw = raw[:-1]
        if raw.endswith(b"\r"):
            raw = raw[:-1]
    return hashlib.sha256(raw).hexdigest()


def read_source_rows(dataset_path: str) -> List[Tuple[int, Optional[dict], str]]:
    """Public API contract; production-derived narrative omitted."""
    rows = []
    if not os.path.exists(dataset_path):
        return rows
    with open(dataset_path, "rb") as stream:
        for lineno, raw in enumerate(stream, 1):
            if not raw.strip():
                continue
            digest = raw_jsonl_line_sha256(raw)
            try:
                parsed = json.loads(raw.decode("utf-8"))
                if not isinstance(parsed, dict):
                    parsed = None
            except (UnicodeDecodeError, json.JSONDecodeError):
                parsed = None
            rows.append((lineno, parsed, digest))
    return rows


def _sha256(value, *, field_name: str) -> str:
    text = str(value or "")
    if len(text) != 64 or text.lower() != text or any(
            ch not in "0123456789abcdef" for ch in text):
        raise DatasetIntegrityError(f"{field_name} must be a lowercase SHA-256")
    return text


_EXCLUSION_FIELDS = frozenset({
    "schema", "action", "purpose", "target_line_sha256", "target_dedup_key",
    "target_kind", "target_source", "target_ts", "reason_code",
    "evidence_line_sha256", "created_at",
})


def _validate_exclusion_record(record: dict) -> dict:
    if not isinstance(record, dict):
        raise DatasetIntegrityError("training exclusion record must be a mapping")
    if set(record) != _EXCLUSION_FIELDS:
        missing = sorted(_EXCLUSION_FIELDS - set(record))
        extra = sorted(set(record) - _EXCLUSION_FIELDS)
        raise DatasetIntegrityError(
            f"training exclusion fields differ (missing={missing}, extra={extra})")
    if record["schema"] != TRAINING_EXCLUSION_SCHEMA:
        raise DatasetIntegrityError("unknown training exclusion schema")
    if record["action"] != "EXCLUDE" or record["purpose"] != "training":
        raise DatasetIntegrityError("training exclusion has unsupported authority")
    normalized = dict(record)
    normalized["target_line_sha256"] = _sha256(
        record["target_line_sha256"], field_name="target_line_sha256")
    for name in ("target_dedup_key", "target_kind", "target_source", "target_ts",
                 "reason_code", "created_at"):
        if not isinstance(record[name], str) or not record[name].strip():
            raise DatasetIntegrityError(f"{name} must be a nonempty string")
    evidence = record["evidence_line_sha256"]
    if isinstance(evidence, str):
        evidence = [evidence]
    if not isinstance(evidence, list) or not evidence:
        raise DatasetIntegrityError("evidence_line_sha256 must contain at least one receipt")
    normalized["evidence_line_sha256"] = [
        _sha256(value, field_name="evidence_line_sha256") for value in evidence
    ]
    if len(set(normalized["evidence_line_sha256"])) != len(
            normalized["evidence_line_sha256"]):
        raise DatasetIntegrityError("evidence_line_sha256 contains duplicates")
    return normalized


def load_training_exclusions(
        dataset_path: str,
        source_rows: Iterable[Tuple[int, Optional[dict], str]],
) -> Dict[str, Any]:
    """Public API contract; production-derived narrative omitted."""
    source_rows = list(source_rows)
    by_sha: Dict[str, list] = {}
    for lineno, row, digest in source_rows:
        by_sha.setdefault(digest, []).append((lineno, row))

    path = training_exclusions_path(dataset_path)
    result = {
        "path": path,
        "ledger_sha256": None,
        "records_by_target_sha256": {},
        "record_line_sha256_by_target_sha256": {},
    }
    if not os.path.exists(path):
        return result
    with open(path, "rb") as stream:
        ledger_bytes = stream.read()
    result["ledger_sha256"] = hashlib.sha256(ledger_bytes).hexdigest()

    seen_dedup = {}
    for lineno, raw in enumerate(ledger_bytes.splitlines(keepends=True), 1):
        if not raw.strip():
            continue
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DatasetIntegrityError(
                f"malformed training exclusion at line {lineno}") from exc
        record = _validate_exclusion_record(parsed)
        target_sha = record["target_line_sha256"]
        target_key = record["target_dedup_key"]
        if target_sha in result["records_by_target_sha256"]:
            raise DatasetIntegrityError(f"duplicate training exclusion target SHA {target_sha}")
        if target_key in seen_dedup:
            raise DatasetIntegrityError(f"duplicate training exclusion target key {target_key}")
        matches = by_sha.get(target_sha, [])
        if len(matches) != 1 or matches[0][1] is None:
            raise DatasetIntegrityError(
                f"training exclusion target {target_sha} does not match exactly one source row")
        _source_lineno, source = matches[0]
        selectors = {
            "target_dedup_key": source.get("_dedup_key"),
            "target_kind": source.get("kind"),
            "target_source": source.get("source"),
            "target_ts": source.get("ts"),
        }
        for name, actual in selectors.items():
            if record[name] != actual:
                raise DatasetIntegrityError(
                    f"training exclusion {name} does not match source row {target_sha}")
        eligible, detail = allowed(source, "training")
        if not eligible:
            raise DatasetIntegrityError(
                f"training exclusion target is no longer eligible for training: {detail}")
        result["records_by_target_sha256"][target_sha] = record
        result["record_line_sha256_by_target_sha256"][target_sha] = raw_jsonl_line_sha256(raw)
        seen_dedup[target_key] = target_sha
    return result


def append_training_exclusion(
        dataset_path: str, *, target_line_sha256: str, target_dedup_key: str,
        target_kind: str, target_source: str, target_ts: str, reason_code: str,
        evidence_line_sha256, created_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Public API contract; production-derived narrative omitted."""
    target_line_sha256 = _sha256(
        target_line_sha256, field_name="target_line_sha256")
    source_rows = read_source_rows(dataset_path)
    path = training_exclusions_path(dataset_path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "r+b", closefd=False) as locked:
            fcntl.flock(locked.fileno(), fcntl.LOCK_EX)
            current = load_training_exclusions(dataset_path, source_rows)
            semantic = {
                "schema": TRAINING_EXCLUSION_SCHEMA,
                "action": "EXCLUDE",
                "purpose": "training",
                "target_line_sha256": target_line_sha256,
                "target_dedup_key": target_dedup_key,
                "target_kind": target_kind,
                "target_source": target_source,
                "target_ts": target_ts,
                "reason_code": reason_code,
                "evidence_line_sha256": evidence_line_sha256,
                "created_at": created_at or datetime.now().astimezone().isoformat(),
            }
            record = _validate_exclusion_record(semantic)
            existing = current["records_by_target_sha256"].get(target_line_sha256)
            if existing is not None:
                compare_fields = _EXCLUSION_FIELDS - {"created_at"}
                if all(existing[name] == record[name] for name in compare_fields):
                    return {"appended": False, "path": path, "record": existing}
                raise DatasetIntegrityError("conflicting exclusion already owns target SHA")
            for other in current["records_by_target_sha256"].values():
                if other["target_dedup_key"] == target_dedup_key:
                    raise DatasetIntegrityError("conflicting exclusion already owns target key")


            candidate = dict(current)
            matches = [row for _line, row, digest in source_rows
                       if digest == target_line_sha256]
            if len(matches) != 1 or matches[0] is None:
                raise DatasetIntegrityError("target does not match exactly one source row")
            source = matches[0]
            for name, source_name in (("target_dedup_key", "_dedup_key"),
                                      ("target_kind", "kind"),
                                      ("target_source", "source"),
                                      ("target_ts", "ts")):
                if record[name] != source.get(source_name):
                    raise DatasetIntegrityError(f"{name} does not match source target")
            eligible, detail = allowed(source, "training")
            if not eligible:
                raise DatasetIntegrityError(
                    f"target is not currently eligible for training: {detail}")

            payload = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
            locked.seek(0, os.SEEK_END)
            locked.write(payload)
            locked.flush()
            os.fsync(locked.fileno())
            verified = load_training_exclusions(dataset_path, source_rows)
            if target_line_sha256 not in verified["records_by_target_sha256"]:
                raise DatasetIntegrityError("appended exclusion did not verify")
            return {"appended": True, "path": path, "record": record,
                    "record_line_sha256": raw_jsonl_line_sha256(payload)}
    finally:
        os.close(fd)


def _key(row: Dict[str, Any]) -> str:
    existing = row.get("_dedup_key")
    if existing:
        return str(existing)
    return hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()


def migrate_ledger(dataset_path: str, *, dry_run: bool = False) -> Dict[str, Any]:
    """Public API contract; production-derived narrative omitted."""
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
