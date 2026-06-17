"""Crash-safe state persistence and reconciliation logic."""

import json
import os
import shutil
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from typing import Optional, Dict, List
from pathlib import Path


@dataclass
class InFlightClose:
    """Record of a pending close order for a contract."""
    con_id: int
    order_id: int  # IB order id (0 if not yet placed)
    remaining_qty: int  # contracts still to be closed
    entry_debit: float  # total dollars paid at entry (for P&L calc)
    order_price: Optional[float] = None  # limit price if order placed
    placed_at: Optional[str] = None  # ISO timestamp


@dataclass
class DailyStats:
    """Daily aggregate statistics."""
    date: str  # YYYY-MM-DD
    orders_placed: int = 0
    notional_closed: float = 0.0


@dataclass
class State:
    """Persisted state for crash-safe operation."""
    in_flight: Dict[str, InFlightClose] = field(default_factory=dict)  # key: con_id as str
    daily_stats: Dict[str, DailyStats] = field(default_factory=dict)  # key: date str
    last_cycle: Optional[str] = None  # ISO timestamp of last evaluation cycle
    peak_prices: Dict[str, float] = field(default_factory=dict)  # con_id(str)->peak mark

    def get_in_flight(self, con_id: int) -> Optional[InFlightClose]:
        return self.in_flight.get(str(con_id))

    def add_in_flight(self, close: InFlightClose) -> None:
        self.in_flight[str(close.con_id)] = close

    def remove_in_flight(self, con_id: int) -> None:
        if str(con_id) in self.in_flight:
            del self.in_flight[str(con_id)]

    def update_daily_stats(self, date_str: str, order_count: int, notional: float) -> None:
        if date_str not in self.daily_stats:
            self.daily_stats[date_str] = DailyStats(date=date_str)
        stats = self.daily_stats[date_str]
        stats.orders_placed += order_count
        stats.notional_closed += notional


class StateManager:
    """Manages crash-safe state persistence using atomic writes."""

    def __init__(self, state_path: str):
        self.state_path = Path(state_path)
        self._state: Optional[State] = None

    @property
    def state(self) -> State:
        if self._state is None:
            self._state = self._load()
        return self._state

    def _load(self) -> State:
        """Load state from disk, or return empty state if file doesn't exist."""
        if not self.state_path.exists():
            return State()
        try:
            with open(self.state_path, "r") as f:
                data = json.load(f)

            # Reconstruct in_flight dict with InFlightClose objects
            in_flight = {}
            for con_id_str, close_data in data.get("in_flight", {}).items():
                in_flight[con_id_str] = InFlightClose(**close_data)

            # Reconstruct daily_stats dict
            daily_stats = {}
            for date_str, stats_data in data.get("daily_stats", {}).items():
                daily_stats[date_str] = DailyStats(**stats_data)

            return State(
                in_flight=in_flight,
                daily_stats=daily_stats,
                last_cycle=data.get("last_cycle"),
                peak_prices=data.get("peak_prices", {}),
            )
        except (json.JSONDecodeError, TypeError, KeyError) as e:
            # Corrupted state file - treat as empty but log
            print(f"[WARN] Could not parse state file {self.state_path}: {e}. Starting fresh.")
            return State()

    def save(self) -> None:
        """Atomically write state to disk using temp file + rename."""
        # Ensure parent directory exists
        self.state_path.parent.mkdir(parents=True, exist_ok=True)

        # Serialize to JSON
        data = {
            "in_flight": {k: asdict(v) for k, v in self.state.in_flight.items()},
            "daily_stats": {k: asdict(v) for k, v in self.state.daily_stats.items()},
            "last_cycle": self.state.last_cycle,
            "peak_prices": self.state.peak_prices,
        }

        # Write to temp file first, then rename (atomic on POSIX)
        temp_path = self.state_path.with_suffix(".tmp")
        with open(temp_path, "w") as f:
            json.dump(data, f, indent=2)
        temp_path.rename(self.state_path)

    def update_last_cycle(self) -> None:
        """Update last cycle timestamp and persist."""
        self.state.last_cycle = datetime.utcnow().isoformat()
        self.save()


def reconcile_state(
    state: State,
    live_positions: Dict[int, dict],  # con_id -> position data (qty, avg_cost)
    live_open_orders: Dict[int, dict],  # con_id -> order data (order_id, remaining_qty)
    journal_entries: Dict[int, dict],  # con_id -> journal entry (debit)
) -> tuple[bool, List[str]]:
    """
    Reconcile persisted in-flight state against live broker positions and open orders.

    Returns:
        (safe_to_proceed, alerts). If safe_to_proceed is False, caller must NOT take any action.

    Reconciliation rules:
    1. If in_flight record exists but no live position and no live order -> position was closed (fill event),
       remove in_flight and continue (OK).
    2. If in_flight record exists and live order exists -> reconcile quantities (OK if consistent).
    3. If in_flight record exists and NO live order but position still exists -> order was cancelled/failed,
       keep in_flight but alert (need manual intervention?).
    4. If in_flight record exists and NO live order and NO position -> fully filled (OK).
    5. If in_flight record exists and live position qty < in_flight remaining_qty -> partial fill (OK).
    6. If in_flight record exists and live position qty > in_flight remaining_qty -> inconsistency (ABORT).
    7. If live position exists but NOT in journal (under journal scope) and NOT in in_flight -> ABORT (unexpected).
    8. If live order exists for contract NOT in in_flight -> ABORT (unexpected order).
    """
    alerts: List[str] = []
    safe = True

    # Build sets
    in_flight_con_ids = set(int(k) for k in state.in_flight.keys())
    live_position_con_ids = set(live_positions.keys())
    live_order_con_ids = set(live_open_orders.keys())
    journal_con_ids = set(journal_entries.keys())

    # Check 1: in_flight but no position and no order -> fully closed (fill event)
    for con_id in in_flight_con_ids:
        if con_id not in live_position_con_ids and con_id not in live_order_con_ids:
            # Position fully closed, remove in_flight
            alerts.append(f"[INFO] Position con_id={con_id} fully closed (fill event), removing in_flight.")
            state.remove_in_flight(con_id)

    # Check 2 & 3: in_flight with position but no order
    for con_id in in_flight_con_ids:
        if con_id in live_position_con_ids and con_id not in live_order_con_ids:
            # Order missing but position still there - could be cancelled/failed
            in_flight = state.get_in_flight(con_id)
            live_qty = live_positions[con_id].get("qty", 0)
            if in_flight and in_flight.remaining_qty != live_qty:
                # Quantities don't match - inconsistency
                alerts.append(
                    f"[ERROR] con_id={con_id}: in_flight remaining_qty={in_flight.remaining_qty}, "
                    f"live position qty={live_qty}. Cannot reconcile safely."
                )
                safe = False
            else:
                # Order vanished (cancelled/expired) but position intact and quantities agree:
                # clear the stale in_flight so exits are re-evaluated/re-protected next cycle
                # (idempotency vs live open orders still prevents a double-close).
                alerts.append(
                    f"[INFO] con_id={con_id}: close order no longer live but position open; "
                    f"clearing stale in_flight so exits are re-evaluated."
                )
                state.remove_in_flight(con_id)

    # Check 4: in_flight with order - reconcile quantities
    for con_id in in_flight_con_ids:
        if con_id in live_order_con_ids:
            in_flight = state.get_in_flight(con_id)
            live_order = live_open_orders[con_id]
            live_order_id = live_order.get("order_id", 0)
            live_remaining = live_order.get("remaining", 0)

            if in_flight and in_flight.order_id != live_order_id and in_flight.order_id != 0:
                alerts.append(
                    f"[ERROR] con_id={con_id}: in_flight order_id={in_flight.order_id}, "
                    f"live order_id={live_order_id}. Order ID mismatch - cannot reconcile safely."
                )
                safe = False

            if in_flight and in_flight.remaining_qty != live_remaining:
                # Partial fill or quantity mismatch
                if in_flight.remaining_qty > live_remaining:
                    # Some contracts closed - update in_flight
                    alerts.append(
                        f"[INFO] con_id={con_id}: partial fill detected, "
                        f"remaining_qty updated from {in_flight.remaining_qty} to {live_remaining}."
                    )
                    in_flight.remaining_qty = live_remaining
                else:
                    alerts.append(
                        f"[ERROR] con_id={con_id}: in_flight remaining_qty={in_flight.remaining_qty}, "
                        f"live order remaining={live_remaining}. Cannot reconcile safely."
                    )
                    safe = False

    # Check 5: live position NOT in journal (under journal scope) and NOT in in_flight
    # This is only a problem if scope is "journal"
    for con_id in live_position_con_ids:
        if con_id not in in_flight_con_ids and con_id not in journal_con_ids:
            # Unexpected position not in journal and not being managed
            alerts.append(
                f"[ERROR] con_id={con_id}: live position exists but NOT in journal and NOT in in_flight. "
                f"This is unexpected under journal scope. Aborting for safety."
            )
            safe = False

    # Check 6: live order NOT in in_flight
    for con_id in live_order_con_ids:
        if con_id not in in_flight_con_ids:
            alerts.append(
                f"[ERROR] con_id={con_id}: live order exists but NOT in in_flight. "
                f"Cannot reconcile safely. Aborting for safety."
            )
            safe = False

    return safe, alerts
