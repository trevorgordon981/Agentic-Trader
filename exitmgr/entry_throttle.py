"""Public API contract; production-derived narrative omitted."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path, PurePath
from typing import Optional, Tuple
import fcntl
import json
import os
import time



RETENTION_DAYS = 30

DEFAULT_LOCK_TIMEOUT_SECONDS = 2.0


class EntryThrottleUnreadable(RuntimeError):
    """Public API contract; production-derived narrative omitted."""


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
                data = json.load(f)
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as e:
            raise EntryThrottleUnreadable(
                f"entry throttle file unreadable ({self.path}): {e}") from e
        if not isinstance(data, dict):
            raise EntryThrottleUnreadable(
                f"entry throttle file is a {type(data).__name__}, not an object ({self.path})")
        return data

    def read_day(self, date_str) -> Tuple[int, float]:
        """Public API contract; production-derived narrative omitted."""
        rec = self._read_all().get(str(date_str))
        if rec is None:
            return 0, 0.0
        if not isinstance(rec, dict):
            raise EntryThrottleUnreadable(
                f"entry throttle record for {date_str} is a {type(rec).__name__}, not an object "
                f"({self.path})")
        try:
            return (int(rec.get("orders_opened", 0) or 0),
                    float(rec.get("notional_opened", 0.0) or 0.0))
        except (TypeError, ValueError) as e:
            raise EntryThrottleUnreadable(
                f"entry throttle counters for {date_str} are unusable ({self.path}): {e}") from e



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
            json.dump(data, f, indent=2, sort_keys=True)
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
            with self._locked():
                data = self._read_all()
                rec = data.get(key)
                if not isinstance(rec, dict):
                    rec = {"orders_opened": 0, "notional_opened": 0.0}
                try:
                    prev_orders = int(rec.get("orders_opened", 0) or 0)
                except (TypeError, ValueError):
                    prev_orders = 0
                try:
                    prev_notional = float(rec.get("notional_opened", 0.0) or 0.0)
                except (TypeError, ValueError):
                    prev_notional = 0.0
                data[key] = {
                    "orders_opened": prev_orders + int(order_count),
                    "notional_opened": round(prev_notional + float(notional), 6),
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
    try:
        ds = state_manager.state.daily_stats.get(date_str) if state_manager is not None else None
        if ds is not None:
            orders = max(orders, int(getattr(ds, "orders_opened", 0)))
            notional = max(notional, float(getattr(ds, "notional_opened", 0.0)))
    except Exception:





        pass
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
