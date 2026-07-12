"""Feature-gated source capture for Byron's production portfolio replay.

This module records facts at the live observation points without changing a
decision, order, or exit.  It is OFF unless ``BYRON_SOURCE_CAPTURE_PATH`` is set.
Capture failures write a sibling ``.INVALID.json`` marker and are swallowed so
they can never interfere with protective trading.  Byron must refuse any run
whose source stream has that marker or cannot be joined into its strict v2 event
contract.

This is intentionally a source stream, not a P&L scorer.  The Byron repository
owns terminal attestation, canonical joins, reconciliation, and promotion gates.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


SCHEMA = "byron.exitmgr-source.v1"
PATH_ENV = "BYRON_SOURCE_CAPTURE_PATH"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, allow_nan=False, ensure_ascii=False,
        separators=(",", ":"), sort_keys=True, default=str,
    ).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _path() -> Path | None:
    raw = os.environ.get(PATH_ENV, "").strip()
    return Path(raw).expanduser().resolve() if raw else None


def _event_hash(event: Mapping[str, Any]) -> str:
    return _sha({key: value for key, value in event.items() if key != "event_hash"})


def verify_source_capture(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    previous = None
    with Path(path).open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                raise ValueError(f"blank source-capture line {line_number}")
            event = json.loads(line)
            if not isinstance(event, dict) or event.get("schema") != SCHEMA:
                raise ValueError(f"invalid source-capture event {line_number}")
            if event.get("sequence") != line_number:
                raise ValueError(f"source-capture sequence gap at {line_number}")
            if event.get("prev_hash") != previous:
                raise ValueError(f"source-capture hash link mismatch at {line_number}")
            if event.get("event_hash") != _event_hash(event):
                raise ValueError(f"source-capture event hash mismatch at {line_number}")
            previous = event["event_hash"]
            events.append(event)
    return events


def _invalid_path(path: Path) -> Path:
    return path.with_name(path.name + ".INVALID.json")


def _invalidate(path: Path, *, reason: str, context: Mapping[str, Any] | None = None) -> None:
    marker = _invalid_path(path)
    value = {
        "schema": "byron.exitmgr-source-invalid.v1",
        "valid": False,
        "first_observed_at": _now(),
        "reason": reason,
        "context": dict(context or {}),
    }
    if marker.exists():
        return
    marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{marker.name}.", dir=marker.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_canonical(value) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, marker)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def _append(
    event_type: str, payload: Mapping[str, Any], *,
    observed_at: str | None = None, event_id: str | None = None,
) -> dict[str, Any] | None:
    path = _path()
    if path is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    event_id = event_id or f"source-{uuid.uuid4()}"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "r+", encoding="utf-8", closefd=False) as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.seek(0)
            existing = []
            if handle.read(1):
                handle.seek(0)
                existing = verify_source_capture(path)
            prior_by_id = {event["event_id"]: event for event in existing}
            if event_id in prior_by_id:
                return prior_by_id[event_id]
            recorded_at = _now()
            event = {
                "schema": SCHEMA,
                "event_id": event_id,
                "event_type": event_type,
                "sequence": len(existing) + 1,
                "observed_at": observed_at or recorded_at,
                "recorded_at": recorded_at,
                "payload": dict(payload),
                "prev_hash": existing[-1]["event_hash"] if existing else None,
            }
            event["event_hash"] = _event_hash(event)
            handle.seek(0, os.SEEK_END)
            handle.write(_canonical(event).decode("utf-8") + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            return event
    except Exception as exc:
        _invalidate(path, reason="append_failed", context={"error": str(exc), "event": event_type})
        return None
    finally:
        os.close(descriptor)


def record_audit(record: Mapping[str, Any]) -> None:
    """Mirror the exact existing audit record into the protected source stream."""
    try:
        _append("audit_event", dict(record), observed_at=str(record.get("ts") or _now()))
    except Exception:
        pass


def _timestamp(value: Any) -> str | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return None
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return None


def _number(value: Any, *, nonnegative: bool = True) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or (nonnegative and number < 0):
        return None
    return number


def record_ibkr_quotes(
    tickers: Iterable[Any], *, context: str,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    """Record source-native two-sided quote facts; never substitute midpoint/last.

    IB's own ticker timestamp and sizes are mandatory.  If the feed object lacks
    any of them, the stream is permanently marked invalid for portfolio replay.
    Trading continues using its existing safety behavior.
    """
    path = _path()
    if path is None:
        return
    received_at = _now()
    quotes = []
    gaps = []
    for ticker in list(tickers or []):
        contract = getattr(ticker, "contract", None)
        contract_id = getattr(contract, "conId", None)
        source_timestamp = _timestamp(getattr(ticker, "time", None))
        quote = {
            "contract_id": str(contract_id) if contract_id else None,
            "symbol": getattr(contract, "symbol", None),
            "right": getattr(contract, "right", None),
            "expiry": getattr(contract, "lastTradeDateOrContractMonth", None),
            "strike": _number(getattr(contract, "strike", None)),
            "bid": _number(getattr(ticker, "bid", None)),
            "ask": _number(getattr(ticker, "ask", None)),
            "bid_size": _number(getattr(ticker, "bidSize", None)),
            "ask_size": _number(getattr(ticker, "askSize", None)),
            "source": "IBKR",
            "source_timestamp": source_timestamp,
            "received_at": received_at,
        }
        missing = [name for name in (
            "contract_id", "bid", "ask", "bid_size", "ask_size", "source_timestamp"
        ) if quote[name] is None]
        if quote["bid"] is not None and quote["ask"] is not None \
                and quote["ask"] < quote["bid"]:
            missing.append("crossed_market")
        if missing:
            gaps.append({"contract_id": quote["contract_id"], "missing": missing})
            continue
        quote["source_artifact_sha256"] = _sha(quote)
        quotes.append(quote)
    payload = {
        "source": "IBKR",
        "context": context,
        "received_at": received_at,
        "metadata": dict(metadata or {}),
        "quotes": quotes,
        "gaps": gaps,
    }
    _append("ibkr_quote_snapshot", payload, observed_at=received_at)
    if gaps or not quotes:
        _invalidate(path, reason="incomplete_quote_provenance", context={
            "capture_context": context, "gaps": gaps, "complete_quotes": len(quotes),
        })


def record_fill(fill: Mapping[str, Any]) -> None:
    try:
        execution_id = str(fill.get("exec_id") or "")
        if not execution_id:
            return
        observed_at = _timestamp(fill.get("time")) or _now()
        _append(
            "broker_fill", dict(fill), observed_at=observed_at,
            event_id=f"fill-{execution_id}",
        )
    except Exception:
        pass


def record_account_snapshot(snapshot: Mapping[str, Any]) -> None:
    try:
        _append("account_snapshot", dict(snapshot))
    except Exception:
        pass


def capture_status() -> dict[str, Any]:
    path = _path()
    if path is None:
        return {"enabled": False, "valid": False, "reason": "capture_disabled"}
    invalid = _invalid_path(path)
    try:
        events = verify_source_capture(path) if path.exists() else []
    except Exception as exc:
        return {"enabled": True, "valid": False, "path": str(path), "error": str(exc)}
    return {
        "enabled": True,
        "valid": bool(events) and not invalid.exists(),
        "path": str(path),
        "events": len(events),
        "invalid_marker": str(invalid) if invalid.exists() else None,
        "head_sha256": events[-1]["event_hash"] if events else None,
    }
