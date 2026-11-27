"""Public API contract; production-derived narrative omitted."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path, PurePath
from typing import Optional, Tuple
import fcntl
import json
import math
import os
import time



RETENTION_DAYS = 30

DEFAULT_LOCK_TIMEOUT_SECONDS = 2.0
_DAY_RECORD_KEYS = frozenset({"orders_opened", "notional_opened"})


class EntryThrottleUnreadable(RuntimeError):
    """Public API contract; production-derived narrative omitted."""


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value!r}")


def _no_duplicate_keys(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate JSON object key {key!r}")
        out[key] = value
    return out


def _validate_nonneg_int(value, *, field_name: str, date_str, path: Path) -> int:
    if isinstance(value, bool):
        raise EntryThrottleUnreadable(
            f"entry throttle {field_name} for {date_str} must be an integer, not bool ({path})"
        )
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise EntryThrottleUnreadable(
                f"entry throttle {field_name} for {date_str} must be a whole number, got {value!r} ({path})"
            )
        parsed = int(value)
    elif isinstance(value, int):
        parsed = value
    else:
        raise EntryThrottleUnreadable(
            f"entry throttle {field_name} for {date_str} must be an integer, got {type(value).__name__} ({path})"
        )
    if parsed < 0:
        raise EntryThrottleUnreadable(
            f"entry throttle {field_name} for {date_str} must be >= 0, got {parsed!r} ({path})"
        )
    return parsed


def _validate_nonneg_finite(value, *, field_name: str, date_str, path: Path) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EntryThrottleUnreadable(
            f"entry throttle {field_name} for {date_str} must be a finite number, got {type(value).__name__} ({path})"
        )
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0.0:
        raise EntryThrottleUnreadable(
            f"entry throttle {field_name} for {date_str} must be finite and >= 0, got {value!r} ({path})"
        )
    return parsed


def _validate_day_record(rec, date_str, path: Path) -> Tuple[int, float]:
    if not isinstance(rec, dict):
        raise EntryThrottleUnreadable(
            f"entry throttle record for {date_str} is a {type(rec).__name__}, not an object ({path})"
        )
    keys = set(rec.keys())
    if keys != _DAY_RECORD_KEYS:
        missing = sorted(_DAY_RECORD_KEYS - keys)
        extra = sorted(keys - _DAY_RECORD_KEYS)
        raise EntryThrottleUnreadable(
            f"entry throttle record for {date_str} has invalid schema "
            f"(missing={missing}, extra={extra}) ({path})"
        )
    orders = _validate_nonneg_int(
        rec["orders_opened"], field_name="orders_opened", date_str=date_str, path=path
    )
    notional = _validate_nonneg_finite(
        rec["notional_opened"], field_name="notional_opened", date_str=date_str, path=path
    )
    return orders, notional


class EntryThrottleStore:
    """Public API contract; production-derived narrative omitted."""

    def __init__(self, path, lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS):
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.stem + ".lock")
        self.lock_timeout_seconds = max(0.0, float(lock_timeout_seconds))

    @classmethod
    def for_state_path(cls, state_path, **kwargs) -> "EntryThrottleStore":
        """Public API contract; production-derived narrative omitted."""


        if not isinstance(state_path, (str, bytes, PurePath)):
            raise TypeError("entry throttle needs a real state path, got %s"
                            % type(state_path).__name__)
        return cls(Path(state_path).with_name("entry_throttle.json"), **kwargs)



    def _read_all(self) -> dict:
        """Public API contract; production-derived narrative omitted."""
        try:
            with open(self.path, "r") as f:
                data = json.load(
                    f,
                    parse_constant=_reject_json_constant,
                    object_pairs_hook=_no_duplicate_keys,
                )
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as e:
            raise EntryThrottleUnreadable(
                f"entry throttle file unreadable ({self.path}): {e}") from e
        if not isinstance(data, dict):
            raise EntryThrottleUnreadable(
                f"entry throttle file is a {type(data).__name__}, not an object ({self.path})")
        for day_key, rec in data.items():
            if not isinstance(day_key, str) or not day_key.strip():
                raise EntryThrottleUnreadable(
                    f"entry throttle file contains invalid day key {day_key!r} ({self.path})")
            _validate_day_record(rec, day_key, self.path)
        return data

    def read_day(self, date_str) -> Tuple[int, float]:
        """Public API contract; production-derived narrative omitted."""
        key = str(date_str)
        data = self._read_all()
        if key not in data:
            return 0, 0.0
        return _validate_day_record(data[key], key, self.path)



    @contextmanager
    def _locked(self):
        """Public API contract; production-derived narrative omitted."""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + self.lock_timeout_seconds
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"entry throttle lock busy: {self.lock_path}")
                    time.sleep(0.02)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)



    def _write_all(self, data: dict) -> None:
        """Public API contract; production-derived narrative omitted."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_name(self.path.stem + ".tmp")
        fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, self.path)
        try:
            dfd = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:


            pass

    def add(self, date_str, order_count: int, notional: float) -> bool:
        """Public API contract; production-derived narrative omitted."""
        key = str(date_str)
        try:
            delta_orders = _validate_nonneg_int(
                order_count, field_name="order_count", date_str=key, path=self.path
            )
            delta_notional = _validate_nonneg_finite(
                notional, field_name="notional", date_str=key, path=self.path
            )
            with self._locked():
                data = self._read_all()
                if key in data:
                    prev_orders, prev_notional = _validate_day_record(data[key], key, self.path)
                else:
                    prev_orders, prev_notional = 0, 0.0
                new_notional = round(prev_notional + delta_notional, 6)
                if not math.isfinite(new_notional):
                    raise EntryThrottleUnreadable(
                        f"entry throttle notional overflow for {key} ({self.path})"
                    )
                data[key] = {
                    "orders_opened": prev_orders + delta_orders,
                    "notional_opened": new_notional,
                }

                for stale in sorted(data)[:-RETENTION_DAYS]:
                    data.pop(stale, None)
                self._write_all(data)
            return True
        except EntryThrottleUnreadable as e:
            print(f"[WARN] entry throttle counter NOT persisted for {key}: {e}. "
                  f"Entries are BLOCKED until this file is repaired or removed.")
            return False
        except Exception as e:
            print(f"[WARN] entry throttle counter NOT persisted for {key}: {e}")
            return False


def entry_day_open_counts(state_manager, throttle_store: Optional[EntryThrottleStore],
                          date_str) -> Tuple[int, float]:
    """Public API contract; production-derived narrative omitted."""
    if throttle_store is None and state_manager is None:
        raise EntryThrottleUnreadable(
            f"no source at all for the {date_str} entry-throttle counters (no durable store, "
            f"no in-memory state): caps.max_orders_per_day / caps.max_notional_per_day cannot "
            f"be evaluated, so no entry may be admitted against them")
    orders, notional = 0, 0.0
    if throttle_store is not None:

        orders, notional = throttle_store.read_day(date_str)
    ds = None
    key = str(date_str)
    daily_stats = None
    try:
        daily_stats = state_manager.state.daily_stats if state_manager is not None else None
    except Exception:
        daily_stats = None
    if isinstance(daily_stats, dict):
        if date_str in daily_stats:
            ds = daily_stats[date_str]
            if ds is None:
                raise EntryThrottleUnreadable(
                    f"in-memory entry throttle record for {date_str} is NoneType, not DailyStats (<in-memory>)"
                )
        elif key in daily_stats:
            ds = daily_stats[key]
            if ds is None:
                raise EntryThrottleUnreadable(
                    f"in-memory entry throttle record for {key} is NoneType, not DailyStats (<in-memory>)"
                )
    if ds is not None:
        mem_orders = _validate_nonneg_int(
            getattr(ds, "orders_opened", 0),
            field_name="in_memory.orders_opened",
            date_str=date_str,
            path=Path("<in-memory>"),
        )
        mem_notional = _validate_nonneg_finite(
            getattr(ds, "notional_opened", 0.0),
            field_name="in_memory.notional_opened",
            date_str=date_str,
            path=Path("<in-memory>"),
        )
        orders = max(orders, mem_orders)
        notional = max(notional, mem_notional)
    return orders, notional


def record_entry_open(state_manager, throttle_store: Optional[EntryThrottleStore],
                      date_str, order_count: int, notional: float) -> bool:
    """Public API contract; production-derived narrative omitted."""
    try:
        if state_manager is not None:
            state_manager.state.update_daily_open_stats(date_str, order_count, notional)
    except Exception as e:
        print(f"[WARN] in-memory entry daily-stats update failed (continuing): {e}")
    if throttle_store is None:
        return False
    return throttle_store.add(date_str, order_count, notional)
