"""Deterministic multi-slot reservation ledger, T+1 cash reserve guard, and trailing-stop math.

All persisted counters and ledger rows undergo strict domain validation on read
and write: negative, boolean, NaN, Inf, or malformed records raise
``LedgerCorruptionError`` (fail closed) before any broker order is attempted.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import math
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional, Sequence

OWNED_REF_PREFIXES: tuple[str, ...] = ("SWING_ENTRY:", "SWING_STOP:", "SWING_EXIT:")
ACTIVE_RESERVATION_STATUSES: frozenset[str] = frozenset({"RESERVED", "SUBMITTED", "WORKING", "UNKNOWN"})
ALL_RESERVATION_STATUSES: frozenset[str] = frozenset(
    {
        "RESERVED",
        "SUBMITTED",
        "WORKING",
        "PARTIALLY_FILLED",
        "FILLED",
        "CANCELLED",
        "REJECTED",
        "EXPIRED",
        "UNKNOWN",
    }
)


class ReservationError(RuntimeError):
    """Raised when a reservation request violates domain invariants."""


class AccountMismatchError(ReservationError):
    """Raised when snapshot, ledger, coordinator, or broker order accounts do not match."""


class LedgerCorruptionError(RuntimeError):
    """Raised when persisted ledger state or counters fail strict validation."""


def round_tick(price: float, tick: float = 0.01) -> float:
    """Round a positive price to the nearest tick increment."""
    if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price):
        raise ValueError(f"price must be finite, got {price!r}")
    if isinstance(tick, bool) or not isinstance(tick, (int, float)) or not math.isfinite(tick) or tick <= 0:
        raise ValueError(f"tick must be positive and finite, got {tick!r}")
    return round(round(price / tick) * tick, 4)


def _require_finite(value: object, *, name: str, positive: bool = False, non_negative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LedgerCorruptionError(f"{name} must be numeric, got {type(value).__name__} {value!r}")
    fval = float(value)
    if not math.isfinite(fval):
        raise LedgerCorruptionError(f"{name} must be finite, got {value!r}")
    if positive and fval <= 0.0:
        raise LedgerCorruptionError(f"{name} must be > 0, got {value!r}")
    if non_negative and fval < 0.0:
        raise LedgerCorruptionError(f"{name} must be >= 0, got {value!r}")
    return fval


def _require_int(value: object, *, name: str, positive: bool = False, non_negative: bool = False) -> int:
    if isinstance(value, bool):
        raise LedgerCorruptionError(f"{name} must be an integer, not bool")
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise LedgerCorruptionError(f"{name} must be an exact integer, got {value!r}")
        ival = int(value)
    elif isinstance(value, int):
        ival = value
    else:
        raise LedgerCorruptionError(f"{name} must be an integer, got {type(value).__name__} {value!r}")
    if positive and ival <= 0:
        raise LedgerCorruptionError(f"{name} must be > 0, got {ival!r}")
    if non_negative and ival < 0:
        raise LedgerCorruptionError(f"{name} must be >= 0, got {ival!r}")
    return ival


def is_owned_order_ref(order_ref: Optional[str]) -> bool:
    """Return True iff ``order_ref`` belongs to the swing_executor namespace."""
    ref = str(order_ref or "").strip()
    return ref.startswith(OWNED_REF_PREFIXES)


def compute_monotonic_stop(
    direction: str,
    current_stop: Optional[float],
    candidate_stop: Optional[float],
) -> float:
    """Merge a candidate stop monotonically so stops never loosen."""
    dir_u = str(direction or "").upper()
    if dir_u not in ("LONG", "SHORT"):
        raise ValueError(f"invalid direction: {direction!r}")
    cur = 0.0 if current_stop is None else _require_finite(current_stop, name="current_stop", non_negative=True)
    if candidate_stop is None:
        return cur
    cand = _require_finite(candidate_stop, name="candidate_stop", non_negative=True)
    if cand <= 0.0:
        return cur
    if cur <= 0.0:
        return cand
    return max(cur, cand) if dir_u == "LONG" else min(cur, cand)


def evaluate_trailing_stop(
    *,
    direction: str,
    entry_price: float,
    prev_peak: Optional[float],
    mark: float,
    current_stop: Optional[float],
    activate_pct: float = 4.0,
    distance_pct: float = 2.0,
) -> tuple[float, float, Optional[float]]:
    """Update high-water mark and return ``(new_peak, gain_pct, tightened_stop_or_none)``."""
    dir_u = str(direction or "").upper()
    if dir_u not in ("LONG", "SHORT"):
        raise ValueError(f"invalid direction: {direction!r}")
    ep = _require_finite(entry_price, name="entry_price", positive=True)
    mk = _require_finite(mark, name="mark", positive=True)
    act = _require_finite(activate_pct, name="activate_pct", positive=True)
    dist = _require_finite(distance_pct, name="distance_pct", positive=True)
    cur_stop = 0.0 if current_stop is None else _require_finite(current_stop, name="current_stop", non_negative=True)
    base_peak = ep if prev_peak is None else _require_finite(prev_peak, name="prev_peak", positive=True)

    if dir_u == "LONG":
        new_peak = max(base_peak, mk)
        gain_pct = ((new_peak - ep) / ep) * 100.0
        if gain_pct < act:
            return round(new_peak, 4), round(gain_pct, 4), None
        trail_cand = round_tick(max(new_peak * (1.0 - dist / 100.0), ep * 1.002))
        tightens = trail_cand > cur_stop and trail_cand < mk
        return round(new_peak, 4), round(gain_pct, 4), (trail_cand if tightens else None)

    new_peak = min(base_peak, mk)
    gain_pct = ((ep - new_peak) / ep) * 100.0
    if gain_pct < act:
        return round(new_peak, 4), round(gain_pct, 4), None
    trail_cand = round_tick(min(new_peak * (1.0 + dist / 100.0), ep * 0.998))
    tightens = (cur_stop <= 0.0 or trail_cand < cur_stop) and trail_cand > mk
    return round(new_peak, 4), round(gain_pct, 4), (trail_cand if tightens else None)


@dataclass(frozen=True)
class AccountCashState:
    """Cash-account settlement & ring-fence snapshot for T+1/cash reserve gating.

    ``spendable_cash_usd`` strictly excludes:
      1. ``ring_fenced_reserve_usd`` (e.g., cash reserved for another sleeve/account goal)
      2. ``unsettled_t1_proceeds_usd`` (cash awaiting T+1 settlement that cannot fund new buys)
    and never borrows above ``available_funds_usd``.
    """

    account: str
    net_liq_usd: float
    total_cash_usd: float
    settled_cash_usd: float
    available_funds_usd: float
    ring_fenced_reserve_usd: float = 0.0
    unsettled_t1_proceeds_usd: float = 0.0
    open_symbol_notional: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.account or "").strip():
            raise LedgerCorruptionError("AccountCashState.account must be non-empty")
        _require_finite(self.net_liq_usd, name="net_liq_usd", positive=True)
        _require_finite(self.total_cash_usd, name="total_cash_usd", non_negative=True)
        _require_finite(self.settled_cash_usd, name="settled_cash_usd", non_negative=True)
        _require_finite(self.available_funds_usd, name="available_funds_usd", non_negative=True)
        _require_finite(self.ring_fenced_reserve_usd, name="ring_fenced_reserve_usd", non_negative=True)
        _require_finite(self.unsettled_t1_proceeds_usd, name="unsettled_t1_proceeds_usd", non_negative=True)
        for sym, val in self.open_symbol_notional.items():
            if not str(sym or "").strip():
                raise LedgerCorruptionError("open_symbol_notional contains blank symbol")
            _require_finite(val, name=f"open_symbol_notional[{sym!r}]", non_negative=True)

    @property
    def spendable_cash_usd(self) -> float:
        """Strictly settled, unencumbered cash available for new swing entries."""
        post_reserve_total = max(0.0, self.total_cash_usd - self.ring_fenced_reserve_usd)
        post_reserve_settled = max(
            0.0,
            self.settled_cash_usd - self.ring_fenced_reserve_usd - self.unsettled_t1_proceeds_usd,
        )
        post_reserve_avail = max(0.0, self.available_funds_usd - self.ring_fenced_reserve_usd)
        return round(min(post_reserve_total, post_reserve_settled, post_reserve_avail), 2)


@dataclass(frozen=True)
class SlotReservation:
    """Durable multi-slot reservation record."""

    reservation_id: str
    account: str
    client_id: int
    symbol: str
    side: str
    qty: int
    limit_price: float
    notional_usd: float
    order_ref: str
    status: str
    session_day: str
    created_at: str
    updated_at: str
    order_id: Optional[int] = None
    perm_id: Optional[int] = None
    filled_qty: int = 0
    stop_price: Optional[float] = None
    strategy_id: str = ""
    stop_armed: bool = False
    stop_covered_qty: int = 0


@dataclass(frozen=True)
class ReservationDecision:
    """Outcome of attempting to reserve capacity for a swing order."""

    allowed: bool
    reason: str
    reservation: Optional[SlotReservation] = None


class SlotLedger:
    """SQLite-backed multi-slot reservation ledger with cross-process locking and strict validation."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        account: Optional[str] = None,
        lock_path: Optional[Path | str] = None,
        max_concurrent_slots: int = 4,
        max_slot_exposure_fraction: float = 0.20,
        max_deployed_fraction: float = 0.80,
        max_sector_fraction: float = 0.40,
        cash_buffer_fraction: float = 0.02,
        max_orders_per_day: int = 10,
        max_notional_per_day: float = 50000.0,
        sector_groups: Optional[Mapping[str, Sequence[str]]] = None,
    ) -> None:
        if account is not None:
            if not isinstance(account, str) or not account.strip() or account != account.strip():
                raise LedgerCorruptionError(f"SlotLedger.account must be a non-empty trimmed string, got {account!r}")
        self.account: Optional[str] = account
        self.max_concurrent_slots = _require_int(max_concurrent_slots, name="max_concurrent_slots", positive=True)
        self.max_slot_exposure_fraction = _require_finite(
            max_slot_exposure_fraction, name="max_slot_exposure_fraction", positive=True
        )
        self.max_deployed_fraction = _require_finite(
            max_deployed_fraction, name="max_deployed_fraction", positive=True
        )
        self.max_sector_fraction = _require_finite(
            max_sector_fraction, name="max_sector_fraction", positive=True
        )
        self.cash_buffer_fraction = _require_finite(
            cash_buffer_fraction, name="cash_buffer_fraction", non_negative=True
        )
        if not (0.0 < self.max_slot_exposure_fraction <= 1.0):
            raise LedgerCorruptionError("max_slot_exposure_fraction must be in (0, 1]")
        if not (0.0 < self.max_deployed_fraction <= 1.0):
            raise LedgerCorruptionError("max_deployed_fraction must be in (0, 1]")
        if not (0.0 < self.max_sector_fraction <= 1.0):
            raise LedgerCorruptionError("max_sector_fraction must be in (0, 1]")
        if not (0.0 <= self.cash_buffer_fraction < 1.0):
            raise LedgerCorruptionError("cash_buffer_fraction must be in [0, 1)")
        self.max_orders_per_day = _require_int(max_orders_per_day, name="max_orders_per_day", positive=True)
        self.max_notional_per_day = _require_finite(
            max_notional_per_day, name="max_notional_per_day", positive=True
        )

        self.sector_groups: dict[str, frozenset[str]] = {
            str(g): frozenset(str(m).strip().upper() for m in members if str(m).strip())
            for g, members in (sector_groups or {}).items()
        }

        self.db_path = Path(db_path)
        self.lock_path = Path(lock_path) if lock_path is not None else self.db_path.with_suffix(".lock")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS slot_reservations (
                    reservation_id TEXT PRIMARY KEY,
                    account TEXT NOT NULL,
                    client_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    qty INTEGER NOT NULL,
                    limit_price REAL NOT NULL,
                    notional_usd REAL NOT NULL,
                    order_ref TEXT NOT NULL,
                    status TEXT NOT NULL,
                    session_day TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    order_id INTEGER,
                    perm_id INTEGER,
                    filled_qty INTEGER NOT NULL DEFAULT 0,
                    stop_price REAL,
                    strategy_id TEXT NOT NULL DEFAULT '',
                    stop_armed INTEGER NOT NULL DEFAULT 0,
                    stop_covered_qty INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(account, order_ref)
                )
                """
            )
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(slot_reservations)").fetchall()}
            if "filled_qty" not in cols:
                conn.execute("ALTER TABLE slot_reservations ADD COLUMN filled_qty INTEGER NOT NULL DEFAULT 0")
            if "stop_price" not in cols:
                conn.execute("ALTER TABLE slot_reservations ADD COLUMN stop_price REAL")
            if "strategy_id" not in cols:
                conn.execute("ALTER TABLE slot_reservations ADD COLUMN strategy_id TEXT NOT NULL DEFAULT ''")
            if "stop_armed" not in cols:
                conn.execute("ALTER TABLE slot_reservations ADD COLUMN stop_armed INTEGER NOT NULL DEFAULT 0")
            if "stop_covered_qty" not in cols:
                # Legacy schemas lacked stop_covered_qty, so stop_armed=1 could mask an
                # underprotected fill after a rejected stop resize. Never infer full coverage
                # from legacy stop_armed=1; default stop_covered_qty=0 and clear stop_armed
                # so the next startup/reconciliation cycle verifies fresh broker stop coverage.
                conn.execute("BEGIN IMMEDIATE")
                try:
                    cols = {
                        row["name"]
                        for row in conn.execute("PRAGMA table_info(slot_reservations)").fetchall()
                    }
                    if "stop_covered_qty" not in cols:
                        conn.execute(
                            "ALTER TABLE slot_reservations ADD COLUMN stop_covered_qty INTEGER NOT NULL DEFAULT 0"
                        )
                        conn.execute(
                            "UPDATE slot_reservations SET stop_armed = 0 WHERE stop_armed = 1"
                        )
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_slot_res_active ON slot_reservations(account, status, symbol)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_counters (
                    account TEXT NOT NULL,
                    session_day TEXT NOT NULL,
                    orders_opened INTEGER NOT NULL,
                    notional_opened REAL NOT NULL,
                    PRIMARY KEY(account, session_day)
                )
                """
            )
        finally:
            conn.close()

    @contextmanager
    def mutation_lock(self, *, timeout_seconds: float = 5.0):
        fd = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Could not acquire slot ledger lock {self.lock_path}")
                    time.sleep(0.02)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _assert_account_match(
        self,
        account: str,
        *,
        context: str,
        expected_account: Optional[str] = None,
    ) -> None:
        if not isinstance(account, str) or not account.strip() or account != account.strip():
            raise AccountMismatchError(f"{context}: account must be a non-empty trimmed string, got {account!r}")
        if self.account is not None and account != self.account:
            raise AccountMismatchError(
                f"{context}: account mismatch (ledger={self.account!r}, got={account!r})"
            )
        if expected_account is not None:
            if (
                not isinstance(expected_account, str)
                or not expected_account.strip()
                or expected_account != expected_account.strip()
            ):
                raise AccountMismatchError(
                    f"{context}: expected_account must be a non-empty trimmed string, got {expected_account!r}"
                )
            if account != expected_account:
                raise AccountMismatchError(
                    f"{context}: account mismatch (expected={expected_account!r}, got={account!r})"
                )

    def _validate_row(self, row: sqlite3.Row) -> SlotReservation:
        account = str(row["account"] or "")
        if not account.strip() or account != account.strip():
            raise LedgerCorruptionError(f"invalid reservation account {account!r}")
        status = str(row["status"] or "")
        if status not in ALL_RESERVATION_STATUSES:
            raise LedgerCorruptionError(f"invalid reservation status {status!r}")
        side = str(row["side"] or "")
        if side not in ("BUY", "SELL"):
            raise LedgerCorruptionError(f"invalid reservation side {side!r}")
        sym = str(row["symbol"] or "").strip().upper()
        if not sym:
            raise LedgerCorruptionError("reservation symbol is blank")
        ref = str(row["order_ref"] or "").strip()
        if not is_owned_order_ref(ref):
            raise LedgerCorruptionError(f"reservation order_ref outside owned namespace: {ref!r}")
        qty = _require_int(row["qty"], name="reservation.qty", positive=True)
        limit_price = _require_finite(row["limit_price"], name="reservation.limit_price", positive=True)
        notional_usd = _require_finite(row["notional_usd"], name="reservation.notional_usd", positive=True)
        client_id = _require_int(row["client_id"], name="reservation.client_id", non_negative=True)
        order_id = None if row["order_id"] is None else _require_int(row["order_id"], name="reservation.order_id", non_negative=True)
        perm_id = None if row["perm_id"] is None else _require_int(row["perm_id"], name="reservation.perm_id", non_negative=True)
        row_keys = row.keys()
        filled_qty = (
            _require_int(row["filled_qty"], name="reservation.filled_qty", non_negative=True)
            if "filled_qty" in row_keys and row["filled_qty"] is not None
            else 0
        )
        if filled_qty > qty:
            raise LedgerCorruptionError(
                f"reservation.filled_qty ({filled_qty}) exceeds reservation.qty ({qty})"
            )
        stop_price = (
            _require_finite(row["stop_price"], name="reservation.stop_price", positive=True)
            if "stop_price" in row_keys and row["stop_price"] is not None
            else None
        )
        strategy_id = (
            str(row["strategy_id"] or "")
            if "strategy_id" in row_keys and row["strategy_id"] is not None
            else ""
        )
        stop_armed_raw = (
            _require_int(row["stop_armed"], name="reservation.stop_armed", non_negative=True)
            if "stop_armed" in row_keys and row["stop_armed"] is not None
            else 0
        )
        if stop_armed_raw not in (0, 1):
            raise LedgerCorruptionError(f"reservation.stop_armed must be 0 or 1, got {stop_armed_raw!r}")
        stop_covered_qty = (
            _require_int(row["stop_covered_qty"], name="reservation.stop_covered_qty", non_negative=True)
            if "stop_covered_qty" in row_keys and row["stop_covered_qty"] is not None
            else 0
        )
        if stop_covered_qty > qty:
            raise LedgerCorruptionError(
                f"reservation.stop_covered_qty ({stop_covered_qty}) exceeds reservation.qty ({qty})"
            )
        if stop_covered_qty > filled_qty:
            raise LedgerCorruptionError(
                f"reservation.stop_covered_qty ({stop_covered_qty}) exceeds reservation.filled_qty ({filled_qty})"
            )
        return SlotReservation(
            reservation_id=str(row["reservation_id"]),
            account=account,
            client_id=client_id,
            symbol=sym,
            side=side,
            qty=qty,
            limit_price=limit_price,
            notional_usd=notional_usd,
            order_ref=ref,
            status=status,
            session_day=str(row["session_day"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            order_id=order_id,
            perm_id=perm_id,
            filled_qty=filled_qty,
            stop_price=stop_price,
            strategy_id=strategy_id,
            stop_armed=bool(stop_armed_raw),
            stop_covered_qty=stop_covered_qty,
        )

    def validate_integrity(self, account: str, session_day: str) -> tuple[list[SlotReservation], int, float]:
        """Validate ALL persisted reservations and daily counters for ``account``; fail closed on corruption."""
        self._assert_account_match(account, context="validate_integrity")
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM slot_reservations WHERE account = ?",
                (account,),
            ).fetchall()
            validated = [self._validate_row(r) for r in rows]

            counter_rows = conn.execute(
                "SELECT session_day, orders_opened, notional_opened FROM daily_counters WHERE account = ?",
                (account,),
            ).fetchall()
            day_orders = 0
            day_notional = 0.0
            for crow in counter_rows:
                co = _require_int(crow["orders_opened"], name=f"daily_counters[{crow['session_day']}].orders_opened", non_negative=True)
                cn = _require_finite(crow["notional_opened"], name=f"daily_counters[{crow['session_day']}].notional_opened", non_negative=True)
                if str(crow["session_day"]) == str(session_day):
                    day_orders = co
                    day_notional = cn
            return validated, day_orders, day_notional
        finally:
            conn.close()

    def reserve_entry_slot(
        self,
        *,
        cash_state: AccountCashState,
        client_id: int,
        symbol: str,
        qty: int,
        limit_price: float,
        session_day: str,
        side: str = "BUY",
        stop_price: Optional[float] = None,
        strategy_id: str = "",
        order_ref: Optional[str] = None,
        external_open_symbols: Sequence[str] = (),
        now_iso: Optional[str] = None,
        expected_account: Optional[str] = None,
    ) -> ReservationDecision:
        """Atomically validate limits and reserve a multi-slot entry under the mutation lock."""
        self._assert_account_match(
            cash_state.account,
            context="reserve_entry_slot",
            expected_account=expected_account,
        )
        sym = str(symbol or "").strip().upper()
        if not sym:
            raise ReservationError("symbol must be non-empty")
        side_u = str(side or "").strip().upper()
        if side_u not in ("BUY", "SELL"):
            raise ReservationError(f"side must be 'BUY' or 'SELL', got {side!r}")
        cid = _require_int(client_id, name="client_id", non_negative=True)
        q = _require_int(qty, name="qty", positive=True)
        lmt = _require_finite(limit_price, name="limit_price", positive=True)
        stp = None if stop_price is None else _require_finite(stop_price, name="stop_price", positive=True)
        strat = str(strategy_id or "").strip()
        notional = round(q * lmt, 2)
        if notional <= 0.0:
            raise ReservationError("computed order notional must be positive")
        ts = now_iso or dt.datetime.now(dt.timezone.utc).isoformat()
        res_id = f"res_{uuid.uuid4().hex[:12]}"
        ref = order_ref or f"SWING_ENTRY:{sym}:{res_id}"
        if not is_owned_order_ref(ref):
            raise ReservationError(f"order_ref must start with one of {OWNED_REF_PREFIXES}, got {ref!r}")

        with self.mutation_lock():
            all_res, day_orders, day_notional = self.validate_integrity(cash_state.account, session_day)
            active = [r for r in all_res if r.status in ACTIVE_RESERVATION_STATUSES]

            if any(r.status == "UNKNOWN" for r in active):
                return ReservationDecision(False, "unresolved_unknown_reservation")

            # Daily order & notional caps
            if day_orders + 1 > self.max_orders_per_day:
                return ReservationDecision(
                    False,
                    f"daily_order_cap({day_orders + 1}>{self.max_orders_per_day})",
                )
            if day_notional + notional > self.max_notional_per_day + 1e-9:
                return ReservationDecision(
                    False,
                    f"daily_notional_cap({day_notional + notional:.2f}>{self.max_notional_per_day:.2f})",
                )

            # Duplicate active symbol guard (includes external/manual symbols)
            active_entry_syms = {r.symbol for r in active if r.side in ("BUY", "SELL")}
            open_syms = {s.upper() for s, val in cash_state.open_symbol_notional.items() if val > 0.0}
            ext_syms = {str(s).strip().upper() for s in external_open_symbols if str(s).strip()}
            if sym in active_entry_syms:
                return ReservationDecision(False, f"duplicate_active_reservation({sym})")
            if sym in open_syms:
                return ReservationDecision(False, f"position_already_open({sym})")
            if sym in ext_syms:
                return ReservationDecision(False, f"external_order_conflict({sym})")

            # Concurrent slot cap
            occupied_slots = open_syms | active_entry_syms
            if len(occupied_slots) >= self.max_concurrent_slots:
                return ReservationDecision(
                    False,
                    f"max_concurrent_slots({len(occupied_slots)}>={self.max_concurrent_slots})",
                )

            # Per-slot exposure cap
            slot_cap = cash_state.net_liq_usd * self.max_slot_exposure_fraction
            if notional > slot_cap + 1e-9:
                return ReservationDecision(
                    False,
                    f"per_slot_exposure_cap({notional:.2f}>{slot_cap:.2f})",
                )

            # Portfolio gross deployed cap
            def _unfilled_notional(r: SlotReservation) -> float:
                if r.filled_qty > 0:
                    return round(max(0, r.qty - r.filled_qty) * r.limit_price, 2)
                return r.notional_usd

            active_reserved_notional = sum(_unfilled_notional(r) for r in active if r.side in ("BUY", "SELL"))
            current_deployed = sum(cash_state.open_symbol_notional.values()) + active_reserved_notional
            deployed_cap = cash_state.net_liq_usd * self.max_deployed_fraction
            if current_deployed + notional > deployed_cap + 1e-9:
                return ReservationDecision(
                    False,
                    f"gross_deployed_cap({current_deployed + notional:.2f}>{deployed_cap:.2f})",
                )

            # Correlated sector-group exposure cap
            symbol_notional = {s.upper(): float(v) for s, v in cash_state.open_symbol_notional.items()}
            for r in active:
                if r.side in ("BUY", "SELL"):
                    symbol_notional[r.symbol] = symbol_notional.get(r.symbol, 0.0) + _unfilled_notional(r)
            for group_name, members in self.sector_groups.items():
                if sym in members:
                    group_total = sum(val for s, val in symbol_notional.items() if s in members)
                    group_cap = cash_state.net_liq_usd * self.max_sector_fraction
                    if group_total + notional > group_cap + 1e-9:
                        return ReservationDecision(
                            False,
                            f"sector_cap_{group_name}({group_total + notional:.2f}>{group_cap:.2f})",
                        )

            # T+1 settled cash & ring-fenced cash reserve guard
            remaining_spendable = max(0.0, cash_state.spendable_cash_usd - active_reserved_notional)
            usable_cash = remaining_spendable * (1.0 - self.cash_buffer_fraction)
            if notional > usable_cash + 1e-9:
                return ReservationDecision(
                    False,
                    f"t1_cash_reserve_guard({notional:.2f}>{usable_cash:.2f})",
                )

            reservation = SlotReservation(
                reservation_id=res_id,
                account=cash_state.account,
                client_id=cid,
                symbol=sym,
                side=side_u,
                qty=q,
                limit_price=lmt,
                notional_usd=notional,
                order_ref=ref,
                status="RESERVED",
                session_day=session_day,
                created_at=ts,
                updated_at=ts,
                stop_price=stp,
                strategy_id=strat,
                stop_armed=False,
                stop_covered_qty=0,
            )

            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    INSERT INTO slot_reservations (
                        reservation_id, account, client_id, symbol, side, qty, limit_price,
                        notional_usd, order_ref, status, session_day, created_at, updated_at,
                        stop_price, strategy_id, stop_armed, stop_covered_qty
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        reservation.reservation_id,
                        reservation.account,
                        reservation.client_id,
                        reservation.symbol,
                        reservation.side,
                        reservation.qty,
                        reservation.limit_price,
                        reservation.notional_usd,
                        reservation.order_ref,
                        reservation.status,
                        reservation.session_day,
                        reservation.created_at,
                        reservation.updated_at,
                        reservation.stop_price,
                        reservation.strategy_id,
                        0,
                        0,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO daily_counters (account, session_day, orders_opened, notional_opened)
                    VALUES (?, ?, 1, ?)
                    ON CONFLICT(account, session_day) DO UPDATE SET
                        orders_opened = orders_opened + 1,
                        notional_opened = ROUND(notional_opened + excluded.notional_opened, 6)
                    """,
                    (cash_state.account, session_day, notional),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

            return ReservationDecision(True, "reserved", reservation)

    def update_reservation_status(
        self,
        reservation_id: str,
        *,
        status: str,
        order_id: Optional[int] = None,
        perm_id: Optional[int] = None,
        filled_qty: Optional[int] = None,
        stop_armed: Optional[bool] = None,
        stop_covered_qty: Optional[int] = None,
        now_iso: Optional[str] = None,
        expected_account: Optional[str] = None,
    ) -> None:
        if status not in ALL_RESERVATION_STATUSES:
            raise ReservationError(f"unsupported reservation status {status!r}")
        if order_id is not None:
            _require_int(order_id, name="order_id", non_negative=True)
        if perm_id is not None:
            _require_int(perm_id, name="perm_id", non_negative=True)
        if filled_qty is not None:
            filled_qty = _require_int(filled_qty, name="filled_qty", non_negative=True)
        if stop_armed is not None and not isinstance(stop_armed, bool):
            raise ReservationError(f"stop_armed must be a bool, got {type(stop_armed).__name__} {stop_armed!r}")
        if stop_covered_qty is not None:
            stop_covered_qty = _require_int(stop_covered_qty, name="stop_covered_qty", non_negative=True)
        stop_armed_int = None if stop_armed is None else (1 if stop_armed else 0)
        ts = now_iso or dt.datetime.now(dt.timezone.utc).isoformat()
        with self.mutation_lock():
            conn = self._connect()
            try:
                existing = conn.execute(
                    "SELECT account, qty, filled_qty, stop_armed, stop_covered_qty "
                    "FROM slot_reservations WHERE reservation_id = ?",
                    (reservation_id,),
                ).fetchone()
                if existing is None:
                    raise ReservationError(f"reservation_id not found: {reservation_id!r}")
                self._assert_account_match(
                    str(existing["account"]),
                    context="update_reservation_status",
                    expected_account=expected_account,
                )
                eff_qty = int(existing["qty"])
                if filled_qty is not None and filled_qty > eff_qty:
                    raise ReservationError(
                        f"filled_qty ({filled_qty}) exceeds reserved qty ({eff_qty})"
                    )
                eff_filled_qty = int(existing["filled_qty"]) if filled_qty is None else filled_qty
                existing_covered = (
                    int(existing["stop_covered_qty"]) if existing["stop_covered_qty"] is not None else 0
                )
                if stop_covered_qty is None and stop_armed is True:
                    eff_stop_covered_qty = eff_filled_qty
                    stop_covered_qty_val: Optional[int] = eff_filled_qty
                else:
                    eff_stop_covered_qty = existing_covered if stop_covered_qty is None else stop_covered_qty
                    stop_covered_qty_val = stop_covered_qty
                if eff_stop_covered_qty > eff_filled_qty:
                    raise ReservationError(
                        f"stop_covered_qty ({eff_stop_covered_qty}) exceeds filled_qty ({eff_filled_qty})"
                    )
                if eff_filled_qty > eff_stop_covered_qty:
                    if stop_armed is True:
                        raise ReservationError(
                            f"cannot mark stop_armed=True while stop_covered_qty ({eff_stop_covered_qty}) "
                            f"< filled_qty ({eff_filled_qty})"
                        )
                    stop_armed_int = 0
                cur = conn.execute(
                    """
                    UPDATE slot_reservations
                    SET status = ?,
                        order_id = COALESCE(?, order_id),
                        perm_id = COALESCE(?, perm_id),
                        filled_qty = COALESCE(?, filled_qty),
                        stop_armed = COALESCE(?, stop_armed),
                        stop_covered_qty = COALESCE(?, stop_covered_qty),
                        updated_at = ?
                    WHERE reservation_id = ?
                    """,
                    (
                        status,
                        order_id,
                        perm_id,
                        filled_qty,
                        stop_armed_int,
                        stop_covered_qty_val,
                        ts,
                        reservation_id,
                    ),
                )
                if cur.rowcount != 1:
                    raise ReservationError(f"reservation_id not found: {reservation_id!r}")
            finally:
                conn.close()

    def active_reservations(self, account: str, session_day: str) -> list[SlotReservation]:
        rows, _, _ = self.validate_integrity(account, session_day)
        return [r for r in rows if r.status in ACTIVE_RESERVATION_STATUSES]

    def reconcilable_reservations(self, account: str, session_day: str) -> list[SlotReservation]:
        """Return persisted reservations requiring broker fill/stop reconciliation."""
        rows, _, _ = self.validate_integrity(account, session_day)
        candidates = [
            r
            for r in rows
            if r.status in ("SUBMITTED", "WORKING", "PARTIALLY_FILLED")
            or (
                r.status == "FILLED"
                and r.filled_qty > 0
                and (not r.stop_armed or r.stop_covered_qty < r.filled_qty)
            )
        ]
        candidates.sort(key=lambda r: (r.created_at, r.reservation_id))
        return candidates


def validate_persisted_counter_json(path: Path | str) -> dict[str, dict[str, float | int]]:
    """Strict validator for standalone JSON counter snapshots (rejects NaN/Inf/negatives/partial keys)."""
    p = Path(path)
    if not p.exists():
        return {}

    def _reject_constant(val: str) -> None:
        raise LedgerCorruptionError(f"non-finite JSON constant {val!r} in {p}")

    def _no_dup_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        out: dict[str, object] = {}
        for k, v in pairs:
            if k in out:
                raise LedgerCorruptionError(f"duplicate JSON key {k!r} in {p}")
            out[k] = v
        return out

    try:
        raw = json.loads(p.read_text(encoding="utf-8"), parse_constant=_reject_constant, object_pairs_hook=_no_dup_keys)
    except LedgerCorruptionError:
        raise
    except Exception as exc:
        raise LedgerCorruptionError(f"counter JSON unreadable ({p}): {exc}") from exc

    if not isinstance(raw, dict):
        raise LedgerCorruptionError(f"counter JSON root must be an object ({p})")

    validated: dict[str, dict[str, float | int]] = {}
    required = {"orders_opened", "notional_opened"}
    for day, rec in raw.items():
        if not isinstance(rec, dict) or set(rec.keys()) != required:
            raise LedgerCorruptionError(
                f"counter record for {day!r} must have exact keys {sorted(required)}, got {rec!r}"
            )
        orders = _require_int(rec["orders_opened"], name=f"{day}.orders_opened", non_negative=True)
        notional = _require_finite(rec["notional_opened"], name=f"{day}.notional_opened", non_negative=True)
        validated[str(day)] = {"orders_opened": orders, "notional_opened": notional}
    return validated
