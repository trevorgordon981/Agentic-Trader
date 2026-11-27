"""Hardened protective stop & cancel-replace engine with fail-closed broker re-reads.

Key invariants:
  1. Every replacement or synced stop ALWAYS performs a fresh broker open-orders
     AND fresh broker positions re-read inside the mutation lock before sizing.
  2. Any timeout or broker error on either read fails closed into explicit
     ``uncertain_holdings=True`` and ``rearm_deferred_uncertain_qty=True``
     without sizing protection from stale pre-cancel holdings.
  3. Concurrent external or partial-fill close orders are accounted for using
     canonical IBKR identity (``perm_id`` when positive, else
     ``(account, client_id, order_id)``) so cross-client order-ID collisions
     never hide an external close and aggregate working close shares NEVER
     exceed broker held quantity.
  4. Modifying a partially-filled stop respects IBKR cumulative
     ``total_quantity = filled_qty + target_remaining_qty`` semantics.
"""

from __future__ import annotations

import asyncio
import math
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Callable, ContextManager, Optional, Protocol

from swing_executor.ledger import is_owned_order_ref, round_tick

WORKING_STATUSES: frozenset[str] = frozenset({"PreSubmitted", "Submitted"})
TERMINAL_STATUSES: frozenset[str] = frozenset({"Filled", "Cancelled", "ApiCancelled", "Inactive"})


@dataclass
class BrokerPosition:
    """Broker-reported stock/ETF position snapshot."""

    account: str
    symbol: str
    quantity: int
    avg_cost: float = 0.0
    sec_type: str = "STK"


@dataclass
class BrokerOrder:
    """Broker-reported working or terminal order snapshot."""

    account: str
    symbol: str
    action: str  # "BUY" | "SELL"
    order_type: str  # "STP" | "LMT" | "MKT" | "STP LMT"
    total_quantity: int
    client_id: int
    order_id: int
    perm_id: int = 0
    order_ref: str = ""
    aux_price: float = 0.0
    limit_price: float = 0.0
    status: str = "Submitted"
    filled: int = 0
    sec_type: str = "STK"
    tif: str = "GTC"

    @property
    def remaining(self) -> int:
        return max(0, int(self.total_quantity) - max(0, int(self.filled)))


class BrokerStopClient(Protocol):
    """Minimal async broker surface required by ``ProtectiveStopEngine``."""

    async def req_open_orders(self) -> list[BrokerOrder]:
        ...

    async def req_positions(self) -> list[BrokerPosition]:
        ...

    async def place_order(self, order: BrokerOrder) -> BrokerOrder:
        ...

    async def cancel_order(self, order: BrokerOrder) -> None:
        ...


@dataclass(frozen=True)
class StopActionResult:
    """Structured outcome from arming, syncing, re-arming, or deduplicating stops."""

    action: str
    symbol: str
    fresh_held_qty: Optional[int] = None
    stop_rearmed: bool = False
    uncertain_holdings: bool = False
    rearm_deferred_uncertain_qty: bool = False
    working_close_present: bool = False
    order: Optional[BrokerOrder] = None
    cancelled_order_ids: tuple[int, ...] = ()
    detail: dict[str, object] = field(default_factory=dict)


def same_broker_order(order_a: Optional[BrokerOrder], order_b: Optional[BrokerOrder]) -> bool:
    """Canonical IBKR order identity matching without cross-client orderId collisions.

    Matches by positive ``perm_id`` when both carry ``perm_id > 0``; otherwise
    matches strictly on ``(account, client_id, order_id)`` for positive ``order_id``.
    """
    if order_a is None or order_b is None:
        return False
    if order_a is order_b:
        return True
    if order_a.perm_id > 0 and order_b.perm_id > 0:
        return order_a.perm_id == order_b.perm_id
    if order_a.order_id <= 0 or order_b.order_id <= 0:
        return False
    return (
        order_a.order_id == order_b.order_id
        and order_a.account == order_b.account
        and order_a.client_id == order_b.client_id
    )


def is_owned_stop_order(
    order: BrokerOrder,
    *,
    account: str,
    client_id: int,
    expected_action: Optional[str] = None,
) -> bool:
    """Return True iff ``order`` is an owned protective stop for ``(account, client_id)``."""
    if order.account != account:
        return False
    if order.client_id != client_id:
        return False
    ref = str(order.order_ref or "").strip()
    owned = ref.startswith("SWING_STOP:") or (order.order_type in ("STP", "STP LMT") and is_owned_order_ref(ref))
    if not owned:
        return False
    if expected_action is not None and order.action.upper() != expected_action.upper():
        return False
    return True


def is_owned_working_exit(order: BrokerOrder, *, symbol: str, account: str, client_id: int) -> bool:
    """Return True if ``order`` is an owned working (non-stop) exit order."""
    if order.account != account or order.symbol.upper() != symbol.upper():
        return False
    if order.client_id != client_id:
        return False
    if order.status in TERMINAL_STATUSES:
        return False
    return str(order.order_ref or "").startswith("SWING_EXIT:")


def count_other_working_close_qty(
    fresh_orders: list[BrokerOrder],
    *,
    symbol: str,
    account: str,
    close_side: str,
    exclude_order: Optional[BrokerOrder] = None,
) -> int:
    """Count remaining working shares on ALL other close-side STK orders for ``(account, symbol)``.

    Deduplicates using canonical ``perm_id`` or ``(account, client_id, order_id)``
    so a same-``order_id`` order from another client is NEVER collapsed with an
    owned order.
    """
    seen: set[tuple] = set()
    total = 0
    sym_u = symbol.upper()
    side_u = close_side.upper()
    for order in fresh_orders:
        if exclude_order is not None and same_broker_order(order, exclude_order):
            continue
        if order.account != account or order.symbol.upper() != sym_u:
            continue
        if order.sec_type not in ("", "STK"):
            continue
        if order.status in TERMINAL_STATUSES:
            continue
        if order.action.upper() != side_u:
            continue
        key: tuple = (
            ("perm", order.perm_id)
            if order.perm_id > 0
            else (("acct_cid_oid", order.account, order.client_id, order.order_id) if order.order_id > 0 else ("obj", id(order)))
        )
        if key in seen:
            continue
        seen.add(key)
        total += max(0, order.remaining)
    return total


def _held_qty_for_symbol(positions: list[BrokerPosition], *, account: str, symbol: str) -> int:
    sym_u = symbol.upper()
    return sum(
        int(p.quantity)
        for p in positions
        if p.account == account and p.symbol.upper() == sym_u and p.sec_type in ("", "STK")
    )


class ProtectiveStopEngine:
    """Fail-closed protective stop arming, in-place sync, cancel-replace, and deduplication."""

    def __init__(
        self,
        *,
        broker: BrokerStopClient,
        account: str,
        client_id: int,
        broker_timeout_seconds: float = 6.0,
        lock_factory: Callable[[], ContextManager[None]] = nullcontext,
    ) -> None:
        if not str(account or "").strip():
            raise ValueError("account must be non-empty")
        if isinstance(client_id, bool) or not isinstance(client_id, int) or client_id < 0:
            raise ValueError(f"client_id must be a non-negative integer, got {client_id!r}")
        if isinstance(broker_timeout_seconds, bool) or not math.isfinite(broker_timeout_seconds) or broker_timeout_seconds <= 0:
            raise ValueError("broker_timeout_seconds must be positive and finite")
        self.broker = broker
        self.account = account
        self.client_id = client_id
        self.broker_timeout_seconds = float(broker_timeout_seconds)
        self._lock_factory = lock_factory
        self._next_order_id = 1000

    async def _read_fresh_broker_state(
        self, *, symbol: str
    ) -> tuple[Optional[list[BrokerOrder]], Optional[int], Optional[StopActionResult]]:
        """Read fresh open orders AND fresh positions; return fail-closed result on any timeout/error."""
        try:
            fresh_orders = list(
                await asyncio.wait_for(self.broker.req_open_orders(), timeout=self.broker_timeout_seconds)
            )
        except Exception as exc:
            return None, None, StopActionResult(
                action="rearm_deferred_uncertain_qty",
                symbol=symbol,
                fresh_held_qty=None,
                stop_rearmed=False,
                uncertain_holdings=True,
                rearm_deferred_uncertain_qty=True,
                working_close_present=True,
                detail={"stage": "open_orders_read", "error": f"{type(exc).__name__}: {exc}"},
            )

        try:
            fresh_positions = list(
                await asyncio.wait_for(self.broker.req_positions(), timeout=self.broker_timeout_seconds)
            )
        except Exception as exc:
            return None, None, StopActionResult(
                action="rearm_deferred_uncertain_qty",
                symbol=symbol,
                fresh_held_qty=None,
                stop_rearmed=False,
                uncertain_holdings=True,
                rearm_deferred_uncertain_qty=True,
                working_close_present=True,
                detail={"stage": "positions_read", "error": f"{type(exc).__name__}: {exc}"},
            )

        held = _held_qty_for_symbol(fresh_positions, account=self.account, symbol=symbol)
        return fresh_orders, held, None

    async def arm_protective_stop(
        self,
        *,
        symbol: str,
        direction: str,
        stop_price: float,
        managed_qty: int,
        trade_key: str,
        dry_run: bool = False,
    ) -> StopActionResult:
        """Arm a GTC protective stop strictly for broker-verified uncovered shares."""
        sym = symbol.upper()
        dir_u = direction.upper()
        if dir_u not in ("LONG", "SHORT"):
            raise ValueError(f"invalid direction: {direction!r}")
        if managed_qty <= 0 or not math.isfinite(stop_price) or stop_price <= 0:
            return StopActionResult(action="stop_arm_skipped_invalid_args", symbol=sym)
        if dry_run:
            return StopActionResult(action="stop_arm_dry_run", symbol=sym, detail={"stop_price": round_tick(stop_price)})

        expected_action = "SELL" if dir_u == "LONG" else "BUY"
        stop_px = round_tick(stop_price)

        with self._lock_factory():
            fresh_orders, fresh_held, fail_res = await self._read_fresh_broker_state(symbol=sym)
            if fail_res is not None:
                return fail_res
            assert fresh_orders is not None and fresh_held is not None

            signed_ok = (dir_u == "LONG" and fresh_held > 0) or (dir_u == "SHORT" and fresh_held < 0)
            if not signed_ok:
                return StopActionResult(
                    action="stop_arm_skipped_flat_or_reversed",
                    symbol=sym,
                    fresh_held_qty=fresh_held,
                )

            existing = [
                o
                for o in fresh_orders
                if o.symbol.upper() == sym
                and o.status in WORKING_STATUSES
                and is_owned_stop_order(o, account=self.account, client_id=self.client_id, expected_action=expected_action)
            ]
            if existing:
                existing.sort(key=lambda o: o.order_id)
                return StopActionResult(
                    action="stop_already_armed",
                    symbol=sym,
                    fresh_held_qty=fresh_held,
                    stop_rearmed=True,
                    order=existing[-1],
                )

            other_close_qty = count_other_working_close_qty(
                fresh_orders,
                symbol=sym,
                account=self.account,
                close_side=expected_action,
            )
            held_abs = abs(fresh_held)
            target_qty = max(0, min(managed_qty, held_abs - other_close_qty))
            if target_qty <= 0:
                return StopActionResult(
                    action="stop_arm_skipped_covered_by_other_closes",
                    symbol=sym,
                    fresh_held_qty=fresh_held,
                    working_close_present=other_close_qty > 0,
                    detail={"held_qty": held_abs, "other_working_close_qty": other_close_qty},
                )

            self._next_order_id += 1
            new_order = BrokerOrder(
                account=self.account,
                symbol=sym,
                action=expected_action,
                order_type="STP",
                total_quantity=target_qty,
                client_id=self.client_id,
                order_id=self._next_order_id,
                order_ref=f"SWING_STOP:{trade_key}",
                aux_price=stop_px,
                tif="GTC",
            )
            placed = await self.broker.place_order(new_order)
            if placed.account != self.account:
                return StopActionResult(
                    action="stop_arm_account_mismatch",
                    symbol=sym,
                    fresh_held_qty=fresh_held,
                    stop_rearmed=False,
                    uncertain_holdings=True,
                    rearm_deferred_uncertain_qty=True,
                    order=placed,
                    detail={"expected_account": self.account, "returned_account": placed.account},
                )
            if placed.status in TERMINAL_STATUSES:
                return StopActionResult(
                    action="stop_arm_rejected",
                    symbol=sym,
                    fresh_held_qty=fresh_held,
                    order=placed,
                    detail={"status": placed.status},
                )
            return StopActionResult(
                action="stop_armed_gtc",
                symbol=sym,
                fresh_held_qty=fresh_held,
                stop_rearmed=True,
                order=placed,
                detail={"qty": target_qty, "stop_price": stop_px, "other_working_close_qty": other_close_qty},
            )

    async def sync_protective_stop(
        self,
        *,
        symbol: str,
        direction: str,
        target_stop_price: float,
        managed_qty: int,
        cached_stop: BrokerOrder,
        mark_price: Optional[float] = None,
        dry_run: bool = False,
    ) -> StopActionResult:
        """Re-read fresh broker positions + open orders and safely sync/resize/cancel the working stop.

        Never oversells when external close orders or partial fills reduce
        uncovered holdings; preserves IBKR cumulative ``total_quantity = filled + remaining``
        semantics on partially-filled stops; fails closed on timeout.
        """
        sym = symbol.upper()
        dir_u = direction.upper()
        if dry_run:
            return StopActionResult(action="stop_sync_dry_run", symbol=sym)

        expected_action = "SELL" if dir_u == "LONG" else "BUY"
        target_aux = round_tick(target_stop_price)

        with self._lock_factory():
            fresh_orders, fresh_held, fail_res = await self._read_fresh_broker_state(symbol=sym)
            if fail_res is not None:
                return fail_res
            assert fresh_orders is not None and fresh_held is not None

            live_stops = [
                o
                for o in fresh_orders
                if o.symbol.upper() == sym
                and o.status in WORKING_STATUSES
                and is_owned_stop_order(o, account=self.account, client_id=self.client_id, expected_action=expected_action)
            ]
            live_stop: Optional[BrokerOrder] = None
            for cand in live_stops:
                if same_broker_order(cand, cached_stop):
                    live_stop = cand
                    break
            if live_stop is None and live_stops:
                live_stops.sort(key=lambda o: o.order_id)
                live_stop = live_stops[-1]
            if live_stop is None:
                return StopActionResult(
                    action="stop_sync_skipped_no_working_stop",
                    symbol=sym,
                    fresh_held_qty=fresh_held,
                )

            held_abs = abs(fresh_held)
            signed_ok = (dir_u == "LONG" and fresh_held > 0) or (dir_u == "SHORT" and fresh_held < 0)
            other_close_qty = count_other_working_close_qty(
                fresh_orders,
                symbol=sym,
                account=self.account,
                close_side=expected_action,
                exclude_order=live_stop,
            )
            target_remaining = 0 if not signed_ok else max(0, min(managed_qty, held_abs - other_close_qty))

            if target_remaining <= 0:
                await self.broker.cancel_order(live_stop)
                return StopActionResult(
                    action="stop_cancelled_covered_or_flat",
                    symbol=sym,
                    fresh_held_qty=fresh_held,
                    working_close_present=other_close_qty > 0,
                    cancelled_order_ids=(live_stop.order_id,),
                    detail={"held_qty": held_abs, "other_working_close_qty": other_close_qty},
                )

            stop_filled = max(0, int(live_stop.filled))
            cur_remaining = max(0, int(live_stop.total_quantity) - stop_filled)
            target_total_qty = stop_filled + target_remaining

            cents_diff = round(target_aux * 100) - round(live_stop.aux_price * 100)
            price_tightens = (
                (dir_u == "LONG" and cents_diff >= 2 and (mark_price is None or target_aux < mark_price))
                or (dir_u == "SHORT" and live_stop.aux_price > 0 and cents_diff <= -2 and (mark_price is None or target_aux > mark_price))
            )
            qty_changes = (cur_remaining != target_remaining) or (live_stop.total_quantity != target_total_qty)

            if not (price_tightens or qty_changes):
                return StopActionResult(
                    action="stop_sync_noop",
                    symbol=sym,
                    fresh_held_qty=fresh_held,
                    stop_rearmed=True,
                    order=live_stop,
                )

            updated = BrokerOrder(
                account=live_stop.account,
                symbol=live_stop.symbol,
                action=live_stop.action,
                order_type=live_stop.order_type,
                total_quantity=target_total_qty,
                client_id=live_stop.client_id,
                order_id=live_stop.order_id,
                perm_id=live_stop.perm_id,
                order_ref=live_stop.order_ref,
                aux_price=target_aux if price_tightens else live_stop.aux_price,
                limit_price=live_stop.limit_price,
                status=live_stop.status,
                filled=stop_filled,
                sec_type=live_stop.sec_type,
                tif=live_stop.tif,
            )
            placed = await self.broker.place_order(updated)
            if placed.account != self.account:
                return StopActionResult(
                    action="stop_sync_account_mismatch",
                    symbol=sym,
                    fresh_held_qty=fresh_held,
                    stop_rearmed=False,
                    uncertain_holdings=True,
                    rearm_deferred_uncertain_qty=True,
                    order=placed,
                    detail={"expected_account": self.account, "returned_account": placed.account},
                )
            if placed.status in TERMINAL_STATUSES:
                return StopActionResult(
                    action="stop_sync_rejected",
                    symbol=sym,
                    fresh_held_qty=fresh_held,
                    order=placed,
                    detail={"status": placed.status},
                )
            return StopActionResult(
                action="stop_synced_gtc",
                symbol=sym,
                fresh_held_qty=fresh_held,
                stop_rearmed=True,
                order=placed,
                detail={
                    "old_aux": live_stop.aux_price,
                    "new_aux": placed.aux_price,
                    "old_total_qty": live_stop.total_quantity,
                    "new_total_qty": placed.total_quantity,
                    "stop_filled_qty": stop_filled,
                    "target_remaining_qty": target_remaining,
                    "other_working_close_qty": other_close_qty,
                },
            )

    async def rearm_after_exit_attempt(
        self,
        *,
        symbol: str,
        direction: str,
        stop_price: float,
        managed_remaining_qty: int,
        trade_key: str,
    ) -> StopActionResult:
        """Re-arm protective stop after an incomplete/cancelled exit attempt.

        Refuses to re-arm when:
          - fresh broker open-orders or positions read times out/fails
            (``uncertain_holdings=True``, ``rearm_deferred_uncertain_qty=True``)
          - an owned working exit order is still active on the broker
          - broker position is already flat or covered by external close orders
        """
        sym = symbol.upper()
        if managed_remaining_qty <= 0:
            return StopActionResult(action="rearm_skipped_zero_managed_qty", symbol=sym, fresh_held_qty=0)

        with self._lock_factory():
            fresh_orders, fresh_held, fail_res = await self._read_fresh_broker_state(symbol=sym)
            if fail_res is not None:
                return fail_res
            assert fresh_orders is not None and fresh_held is not None

            if any(
                is_owned_working_exit(o, symbol=sym, account=self.account, client_id=self.client_id)
                for o in fresh_orders
            ):
                return StopActionResult(
                    action="rearm_skipped_working_close",
                    symbol=sym,
                    fresh_held_qty=fresh_held,
                    working_close_present=True,
                )

        return await self.arm_protective_stop(
            symbol=sym,
            direction=direction,
            stop_price=stop_price,
            managed_qty=managed_remaining_qty,
            trade_key=trade_key,
            dry_run=False,
        )

    async def dedupe_resting_stops(self, *, symbol: str, direction: str) -> StopActionResult:
        """Cancel duplicate owned resting stops while never touching other clients' orders."""
        sym = symbol.upper()
        expected_action = "SELL" if direction.upper() == "LONG" else "BUY"
        with self._lock_factory():
            fresh_orders, fresh_held, fail_res = await self._read_fresh_broker_state(symbol=sym)
            if fail_res is not None:
                return fail_res
            assert fresh_orders is not None

            owned_stops = [
                o
                for o in fresh_orders
                if o.symbol.upper() == sym
                and o.status not in TERMINAL_STATUSES
                and is_owned_stop_order(o, account=self.account, client_id=self.client_id, expected_action=expected_action)
            ]
            if len(owned_stops) <= 1:
                return StopActionResult(
                    action="dedupe_noop",
                    symbol=sym,
                    fresh_held_qty=fresh_held,
                    order=owned_stops[0] if owned_stops else None,
                )
            owned_stops.sort(key=lambda o: o.order_id)
            keep = owned_stops[-1]
            cancelled: list[int] = []
            for extra in owned_stops[:-1]:
                await self.broker.cancel_order(extra)
                cancelled.append(extra.order_id)
            return StopActionResult(
                action="cancelled_duplicate_stops",
                symbol=sym,
                fresh_held_qty=fresh_held,
                stop_rearmed=True,
                order=keep,
                cancelled_order_ids=tuple(cancelled),
            )


class SimulatedBroker:
    """Deterministic in-memory broker for offline simulations and race-condition tests."""

    def __init__(self, *, account: str = "SIM_PAPER") -> None:
        self.account = account
        self.positions: dict[str, BrokerPosition] = {}
        self.orders: list[BrokerOrder] = []
        self._next_perm_id = 50000
        self.fail_open_orders_exc: Optional[Exception] = None
        self.fail_positions_exc: Optional[Exception] = None
        self.reject_next_place_status: Optional[str] = None
        self.place_order_calls: int = 0
        self.cancel_order_calls: int = 0

    def set_position(self, symbol: str, quantity: int, avg_cost: float = 0.0) -> None:
        sym = symbol.upper()
        if quantity == 0:
            self.positions.pop(sym, None)
        else:
            self.positions[sym] = BrokerPosition(
                account=self.account,
                symbol=sym,
                quantity=int(quantity),
                avg_cost=float(avg_cost),
            )

    def add_external_order(self, order: BrokerOrder) -> BrokerOrder:
        if order.perm_id <= 0:
            self._next_perm_id += 1
            order.perm_id = self._next_perm_id
        self.orders.append(order)
        return order

    def simulate_partial_fill(self, order: BrokerOrder, fill_qty: int) -> None:
        """Apply a partial fill to ``order`` and update the corresponding broker position."""
        if fill_qty <= 0 or fill_qty > order.remaining:
            raise ValueError(f"invalid partial fill qty {fill_qty} for remaining {order.remaining}")
        order.filled += fill_qty
        if order.remaining == 0:
            order.status = "Filled"
        sym = order.symbol.upper()
        cur = self.positions.get(sym)
        cur_qty = cur.quantity if cur else 0
        delta = fill_qty if order.action.upper() == "BUY" else -fill_qty
        self.set_position(sym, cur_qty + delta, avg_cost=cur.avg_cost if cur else order.limit_price)

    async def req_open_orders(self) -> list[BrokerOrder]:
        if self.fail_open_orders_exc is not None:
            raise self.fail_open_orders_exc
        return [o for o in self.orders if o.status not in TERMINAL_STATUSES]

    async def req_completed_orders(self) -> list[BrokerOrder]:
        if self.fail_open_orders_exc is not None:
            raise self.fail_open_orders_exc
        return [o for o in self.orders if o.status in TERMINAL_STATUSES]

    async def req_positions(self) -> list[BrokerPosition]:
        if self.fail_positions_exc is not None:
            raise self.fail_positions_exc
        return list(self.positions.values())

    async def place_order(self, order: BrokerOrder) -> BrokerOrder:
        self.place_order_calls += 1
        if self.reject_next_place_status is not None:
            order.status = self.reject_next_place_status
            self.reject_next_place_status = None
            return order
        for idx, existing in enumerate(self.orders):
            if same_broker_order(existing, order):
                order.perm_id = existing.perm_id
                order.status = "Submitted"
                self.orders[idx] = order
                return order
        if order.perm_id <= 0:
            self._next_perm_id += 1
            order.perm_id = self._next_perm_id
        order.status = "Submitted"
        self.orders.append(order)
        return order

    async def cancel_order(self, order: BrokerOrder) -> None:
        self.cancel_order_calls += 1
        for existing in self.orders:
            if same_broker_order(existing, order):
                existing.status = "Cancelled"
                return
