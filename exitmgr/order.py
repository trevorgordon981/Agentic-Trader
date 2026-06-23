"""Order placement and tracking logic."""

import asyncio
from typing import Optional, Dict, List
from dataclasses import dataclass
from datetime import datetime

from exitmgr.ibkr import Contract, Order

from exitmgr.connection import IBConnection
from exitmgr.state import State, StateManager, InFlightClose


@dataclass
class OrderResult:
    """Result of an order placement attempt."""
    success: bool
    order_id: Optional[int] = None
    message: str = ""
    con_id: Optional[int] = None


class OrderManager:
    """Manages order placement with idempotency and partial fill tracking."""

    def __init__(self, ib_conn: IBConnection, state_manager: StateManager):
        self.ib_conn = ib_conn
        self.state_manager = state_manager

    async def can_place_close(
        self,
        con_id: int,
        quantity: int,
        live_open_orders: Dict[int, dict],
    ) -> tuple[bool, str]:
        """
        Check if we can safely place a close order (idempotency check).
        Returns (can_place, reason).
        """
        # Check in-flight state
        in_flight = self.state_manager.state.get_in_flight(con_id)
        if in_flight is not None:
            if in_flight.order_id != 0:
                # ANY active in-flight close blocks a new one (no double-close even after a partial fill)
                return False, f"con_id={con_id} already has in-flight order (id={in_flight.order_id}, remaining={in_flight.remaining_qty})"

        # Check live open orders
        if con_id in live_open_orders:
            live_order = live_open_orders[con_id]
            live_order_id = live_order.get("order_id", 0)
            if live_order_id != 0:
                return False, f"con_id={con_id} already has live open order (id={live_order_id})"

        return True, ""

    async def place_close_order(
        self,
        con_id: int,
        symbol: str,
        quantity: int,
        limit_price: float,
        entry_debit: float,
        live_open_orders: Dict[int, dict],
        spread: Optional[dict] = None,
        market: bool = False,
    ) -> OrderResult:
        """
        Place a SELL-TO-CLOSE order to close a position. LIMIT by default; MARKET when market=True
        so a triggered stop/target always fills (option bid/ask don't stream cleanly here).
        For journaled spreads (spread={"short_con_id": ...}) the close is a single BAG combo
        (SELL the same combo that was bought) so both legs always close atomically -- closing
        the long leg alone would leave a naked short option.
        Returns OrderResult indicating success/failure.
        """
        # Idempotency check (keyed by the long leg's con_id for spreads)
        can_place, reason = await self.can_place_close(con_id, quantity, live_open_orders)
        if not can_place:
            print(f"[INFO] Skipping order for con_id={con_id}: {reason}")
            return OrderResult(success=False, message=reason, con_id=con_id)

        # Create contract and order
        if spread and spread.get("short_con_id"):
            contract = self.ib_conn.create_combo_contract(
                symbol, [(con_id, "BUY"), (int(spread["short_con_id"]), "SELL")])
        else:
            contract = self.ib_conn.create_contract(con_id, symbol=symbol, right="C")
        order = (self.ib_conn.create_market_order("SELL", quantity) if market
                 else self.ib_conn.create_limit_order("SELL", quantity, limit_price))

        # Place order. SELF-HEAL: a stale IBKR link (post Error-1100) makes placeOrder raise
        # "Not connected to IB"; that must NOT silently skip an exit for 15 min. On a
        # connection-type failure we force ONE reconnect via the connection wrapper and retry
        # the placement once (single retry only -- never hammer the gateway).
        async def _place():
            placed_order = await self.ib_conn.place_order(contract, order)
            # placeOrder returns a Trade; the IB-assigned id lives on trade.order.orderId
            return placed_order.order.orderId if placed_order and getattr(placed_order, 'order', None) is not None else 0

        try:
            try:
                order_id = await _place()
            except Exception as e:
                msg = str(e).lower()
                if "not connected" in msg or "connect" in msg or isinstance(e, (ConnectionError, OSError)):
                    print(f"[WARN] place_close_order: link appears down ({e}) -- reconnecting and retrying ONCE for con_id={con_id}")
                    if await self.ib_conn.reconnect(retries=2, retry_delay=10):
                        print(f"[INFO] reconnected; retrying close order for con_id={con_id}")
                        order_id = await _place()
                    else:
                        raise
                else:
                    raise

            # Record in-flight close
            in_flight = InFlightClose(
                con_id=con_id,
                order_id=order_id,
                remaining_qty=quantity,
                entry_debit=entry_debit,
                order_price=limit_price,
                placed_at=datetime.utcnow().isoformat(),
            )
            self.state_manager.state.add_in_flight(in_flight)

            # Update daily stats (notional = limit_price * 100 * quantity)
            notional = limit_price * 100 * quantity
            today = datetime.utcnow().strftime("%Y-%m-%d")
            self.state_manager.state.update_daily_stats(today, order_count=1, notional=notional)

            # Persist immediately (crash-safe)
            self.state_manager.save()

            print(f"[ORDER PLACED] con_id={con_id}, order_id={order_id}, qty={quantity}, price={limit_price}, notional=${notional:.2f}")
            return OrderResult(success=True, order_id=order_id, message="Order placed successfully", con_id=con_id)

        except Exception as e:
            print(f"[ERROR] Failed to place order for con_id={con_id}: {e}")
            return OrderResult(success=False, message=str(e), con_id=con_id)

    async def update_in_flight_from_fill(
        self,
        con_id: int,
        filled_qty: int,
    ) -> None:
        """Update in-flight record after a fill event (partial or full)."""
        in_flight = self.state_manager.state.get_in_flight(con_id)
        if in_flight is None:
            print(f"[WARN] Fill event for con_id={con_id} but no in-flight record found")
            return

        in_flight.remaining_qty -= filled_qty
        if in_flight.remaining_qty <= 0:
            # Fully closed - remove in-flight
            self.state_manager.state.remove_in_flight(con_id)
            print(f"[INFO] Position con_id={con_id} fully closed (fill event)")
        else:
            print(f"[INFO] Partial fill for con_id={con_id}, remaining={in_flight.remaining_qty}")

        # Persist immediately
        self.state_manager.save()
