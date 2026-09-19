"""Public API contract; production-derived narrative omitted."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path, PurePath
import time
from typing import Optional


SCHEMA = "protective_cycle_heartbeat.v1"
DEADLINE_SECONDS = 30.0
STALE_AFTER_SECONDS = 60.0
ALERT_THROTTLE_SECONDS = 900.0
_ALERT_KINDS = {
    "cycle_running_over_deadline",
    "interrupted_cycle_overrun",
    "consecutive_deadline_misses",
    "protective_cycle_stale",
    "broker_view_stale",
}
_FIELDS = {
    "schema",
    "cycle_sequence",
    "active",
    "cycle_start_at",
    "cycle_end_at",
    "duration_seconds",
    "deadline_seconds",
    "deadline_missed",
    "consecutive_deadline_misses",
    "last_good_broker_view_at",
    "unresolved_close_count",
    "updated_at",
    "pending_alert",
    "alerts",
}


class ProtectiveClockError(RuntimeError):
    """Public API contract; production-derived narrative omitted."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse(value, field: str, *, optional: bool = False) -> Optional[datetime]:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value:
        raise ProtectiveClockError(f"{field} is not an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProtectiveClockError(f"{field} is not an ISO timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _finite(value, field: str, *, optional: bool = False) -> Optional[float]:
    if value is None and optional:
        return None
    if isinstance(value, bool):
        raise ProtectiveClockError(f"{field} is not numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ProtectiveClockError(f"{field} is not numeric") from exc
    if not math.isfinite(number) or number < 0:
        raise ProtectiveClockError(f"{field} is out of range")
    return number


class ProtectiveClock:
    """Public API contract; production-derived narrative omitted."""

    def __init__(self, path, *, deadline_seconds: float = DEADLINE_SECONDS,
                 stale_after_seconds: float = STALE_AFTER_SECONDS,
                 alert_throttle_seconds: float = ALERT_THROTTLE_SECONDS,
                 lock_timeout_seconds: float = 0.25):
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.deadline_seconds = max(0.001, float(deadline_seconds))
        self.stale_after_seconds = max(self.deadline_seconds,
                                       float(stale_after_seconds))
        self.alert_throttle_seconds = max(0.0, float(alert_throttle_seconds))
        self.lock_timeout_seconds = max(0.0, float(lock_timeout_seconds))

    @classmethod
    def for_state_path(cls, state_path, **kwargs) -> "ProtectiveClock":
        if not isinstance(state_path, (str, bytes, PurePath)):
            raise TypeError("protective clock needs a real state path")
        state = Path(state_path)
        return cls(state.with_name(state.stem + ".protective-clock.json"), **kwargs)

    @contextmanager
    def _locked(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + self.lock_timeout_seconds
        try:
            os.fchmod(fd, 0o600)
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ProtectiveClockError(
                            f"protective-clock lock busy: {self.lock_path}")
                    time.sleep(0.01)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _validate(self, raw) -> dict:
        if not isinstance(raw, dict) or set(raw) != _FIELDS or raw.get("schema") != SCHEMA:
            raise ProtectiveClockError("protective heartbeat has an unknown shape/schema")
        if not isinstance(raw["active"], bool) or not isinstance(raw["deadline_missed"], bool):
            raise ProtectiveClockError("protective heartbeat booleans are invalid")
        for field in ("cycle_sequence", "consecutive_deadline_misses",
                      "unresolved_close_count"):
            value = raw[field]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ProtectiveClockError(f"{field} is not a non-negative integer")
        _parse(raw["cycle_start_at"], "cycle_start_at", optional=True)
        _parse(raw["cycle_end_at"], "cycle_end_at", optional=True)
        _parse(raw["last_good_broker_view_at"], "last_good_broker_view_at", optional=True)
        _parse(raw["updated_at"], "updated_at")
        _finite(raw["duration_seconds"], "duration_seconds", optional=True)
        _finite(raw["deadline_seconds"], "deadline_seconds")
        alerts = raw["alerts"]
        if not isinstance(alerts, dict) or not set(alerts).issubset(_ALERT_KINDS):
            raise ProtectiveClockError("protective heartbeat alerts are invalid")
        pending = raw["pending_alert"]
        if pending is not None and pending not in _ALERT_KINDS:
            raise ProtectiveClockError("protective heartbeat pending alert is invalid")
        for kind, timestamp in alerts.items():
            _parse(timestamp, f"alerts.{kind}")
        return raw

    def _read_unlocked(self) -> Optional[dict]:
        try:
            with open(self.path) as handle:
                return self._validate(json.load(handle))
        except FileNotFoundError:
            return None
        except ProtectiveClockError:
            raise
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ProtectiveClockError(
                f"protective heartbeat unreadable ({self.path}): {exc}") from exc

    def read(self) -> Optional[dict]:
        """Public API contract; production-derived narrative omitted."""
        return self._read_unlocked()

    def _write_unlocked(self, state: dict) -> None:
        state = self._validate(state)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
        encoded = (json.dumps(state, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
            try:
                dfd = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            except OSError:
                pass
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                temp.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _count(value) -> int:
        if isinstance(value, bool):
            raise ProtectiveClockError("unresolved close count is invalid")
        count = int(value)
        if count < 0:
            raise ProtectiveClockError("unresolved close count is invalid")
        return count

    def start_cycle(self, *, unresolved_close_count: int,
                    last_good_broker_view_at: Optional[str] = None,
                    now: Optional[datetime] = None) -> int:
        now = (now or _utc_now()).astimezone(timezone.utc)
        with self._locked():
            previous = self._read_unlocked()
            sequence = int((previous or {}).get("cycle_sequence", 0)) + 1
            consecutive = int((previous or {}).get("consecutive_deadline_misses", 0))
            pending_alert = (previous or {}).get("pending_alert")
            alerts = dict((previous or {}).get("alerts") or {})
            if previous and previous["active"]:
                prior_start = _parse(previous["cycle_start_at"], "cycle_start_at")
                if (now - prior_start).total_seconds() > self.deadline_seconds:
                    consecutive += 1



                    last_claim = _parse(
                        alerts.get("cycle_running_over_deadline"),
                        "alerts.cycle_running_over_deadline", optional=True)
                    if last_claim is None or last_claim <= prior_start:
                        pending_alert = "interrupted_cycle_overrun"
            last_good = last_good_broker_view_at or (
                previous or {}).get("last_good_broker_view_at")
            if last_good is not None:
                _parse(last_good, "last_good_broker_view_at")
            state = {
                "schema": SCHEMA,
                "cycle_sequence": sequence,
                "active": True,
                "cycle_start_at": _iso(now),
                "cycle_end_at": None,
                "duration_seconds": None,
                "deadline_seconds": self.deadline_seconds,
                "deadline_missed": False,
                "consecutive_deadline_misses": consecutive,
                "last_good_broker_view_at": last_good,
                "unresolved_close_count": self._count(unresolved_close_count),
                "updated_at": _iso(now),
                "pending_alert": pending_alert,
                "alerts": alerts,
            }
            self._write_unlocked(state)
            return sequence

    def complete_cycle(self, sequence: int, *, duration_seconds: float,
                       unresolved_close_count: int,
                       last_good_broker_view_at: Optional[str] = None,
                       now: Optional[datetime] = None) -> dict:
        now = (now or _utc_now()).astimezone(timezone.utc)
        duration = _finite(duration_seconds, "duration_seconds")
        with self._locked():
            state = self._read_unlocked()
            if (state is None or not state["active"]
                    or state["cycle_sequence"] != int(sequence)):
                raise ProtectiveClockError(
                    "refusing stale protective-cycle completion")
            missed = duration > self.deadline_seconds
            state.update({
                "active": False,
                "cycle_end_at": _iso(now),
                "duration_seconds": round(duration, 6),
                "deadline_missed": missed,
                "consecutive_deadline_misses": (
                    state["consecutive_deadline_misses"] + 1 if missed else 0),
                "last_good_broker_view_at": (
                    last_good_broker_view_at or state["last_good_broker_view_at"]),
                "unresolved_close_count": self._count(unresolved_close_count),
                "updated_at": _iso(now),
            })
            if state["last_good_broker_view_at"] is not None:
                _parse(state["last_good_broker_view_at"], "last_good_broker_view_at")
            self._write_unlocked(state)
            return dict(state)

    def _alert_reason(self, state: dict, now: datetime) -> Optional[str]:
        if state["pending_alert"] is not None:
            return state["pending_alert"]
        if state["active"]:
            started = _parse(state["cycle_start_at"], "cycle_start_at")
            if (now - started).total_seconds() > self.deadline_seconds:
                return "cycle_running_over_deadline"
            last_good = _parse(
                state["last_good_broker_view_at"], "last_good_broker_view_at", optional=True)
            if (last_good is not None
                    and (now - last_good).total_seconds() > self.stale_after_seconds):
                return "broker_view_stale"
            return None
        if state["consecutive_deadline_misses"] >= 2:
            return "consecutive_deadline_misses"
        last_good = _parse(
            state["last_good_broker_view_at"], "last_good_broker_view_at", optional=True)
        if (last_good is not None
                and (now - last_good).total_seconds() > self.stale_after_seconds):
            return "broker_view_stale"
        ended = _parse(state["cycle_end_at"], "cycle_end_at", optional=True)
        if ended is not None and (now - ended).total_seconds() > self.stale_after_seconds:
            return "protective_cycle_stale"
        return None

    def claim_alert(self, *, now: Optional[datetime] = None):
        """Public API contract; production-derived narrative omitted."""
        now = (now or _utc_now()).astimezone(timezone.utc)
        with self._locked():
            state = self._read_unlocked()
            if state is None:
                return None
            kind = self._alert_reason(state, now)
            if kind is None:
                return None
            last = _parse(state["alerts"].get(kind), f"alerts.{kind}", optional=True)
            if last is not None and (now - last).total_seconds() < self.alert_throttle_seconds:
                return None
            state["alerts"][kind] = _iso(now)
            if state["pending_alert"] == kind:
                state["pending_alert"] = None
            state["updated_at"] = _iso(now)
            self._write_unlocked(state)
            return kind, dict(state)


def alert_text(kind: str, state: dict) -> str:
    """Public API contract; production-derived narrative omitted."""
    labels = {
        "cycle_running_over_deadline": "protective cycle is still running past 30 seconds",
        "interrupted_cycle_overrun": (
            "previous protective cycle was interrupted after exceeding 30 seconds"),
        "consecutive_deadline_misses": "protective cycle missed 30 seconds consecutively",
        "protective_cycle_stale": "protective cycle heartbeat is stale",
        "broker_view_stale": "protective loop has no fresh readable broker view",
    }
    return (
        ":rotating_light: *PROTECTIVE CLOCK SLO*\n"
        f"• {labels.get(kind, kind)}\n"
        f"• cycle={state.get('cycle_sequence')} active={state.get('active')} "
        f"duration={state.get('duration_seconds')}s "
        f"consecutive_misses={state.get('consecutive_deadline_misses')}\n"
        f"• started={state.get('cycle_start_at')} ended={state.get('cycle_end_at')}\n"
        f"• last_good_broker_view={state.get('last_good_broker_view_at')} "
        f"unresolved_closes={state.get('unresolved_close_count')}"
    )
