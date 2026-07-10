"""Order placement and tracking logic."""

import asyncio
import json
import math
import uuid
from typing import Optional, Dict, List
from dataclasses import dataclass
from datetime import datetime, timezone

from exitmgr.ibkr import Contract, Order

from exitmgr.connection import IBConnection
from exitmgr.state import State, StateManager, InFlightClose

# Module-level default for the bid-anchored exit slippage floor (config-driven, 2026-07-03).
# This is the ULTIMATE fallback: an OrderManager built with no `exit_slippage_floor` arg uses
# this value, making unconfigured behavior BYTE-IDENTICAL to the prior hardcoded 0.50. A tuned
# value flows in via config.yaml -> Config.rules.exit_slippage_floor -> OrderManager(...) without
# a code edit (see tune_exit_floor.py + config.py RulesConfig.exit_slippage_floor).
DEFAULT_EXIT_SLIPPAGE_FLOOR = 0.50


def _trading_day(now=None) -> str:
    """US/Eastern calendar date used to key daily order/notional stats.

    Kept identical to exitmgr.trader._trading_day (the canonical version the daily
    circuit-breaker baseline uses) so the order-count/notional counters roll over on
    the SAME exchange-timezone boundary instead of the old UTC boundary -- which
    mislabeled the ~20:00-00:00 UTC evening-ET window as the NEXT day and booked
    evening-session activity onto tomorrow's ledger. Replicated rather than imported
    to avoid pulling the heavy high-level trader module into this low-level component
    (and any future import cycle). If you change one, change both.
    """
    try:
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        n = now or datetime.now(et)
        if getattr(n, "tzinfo", None) is None:
            n = n.replace(tzinfo=timezone.utc)
        return str(n.astimezone(et).date())
    except Exception:
        return str((now or datetime.now(timezone.utc)).date())


@dataclass
class OrderResult:
    """Result of an order placement attempt."""
    success: bool
    order_id: Optional[int] = None
    message: str = ""
    con_id: Optional[int] = None
    trade: object = None  # the ib Trade object (fill-status verification, 2026-07-01); may be None
    perm_id: Optional[int] = None
    client_id: Optional[int] = None
    order_ref: Optional[str] = None


def _safe_int(value) -> int:
    """Return a real positive integer, never a MagicMock/bool/coercion surprise."""
    return int(value) if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _safe_client_id(value):
    """IB clientId 0 is valid; ``None`` is the only unknown sentinel."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return int(value)
    return None


def _json_safe(value):
    """Normalize placement context before any order can be transmitted.

    State persistence must not discover an unserializable datetime/object or NaN only after IB has
    accepted the close.  Unknown leaf objects become strings; non-finite floats become ``None``.
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return str(value)


def commission_from_trade(trade) -> Optional[float]:
    """Sum realized IBKR commission across EVERY fill of a Trade (both legs of a combo).

    The IBKR commissionReport arrives via a SEPARATE async callback that can lag the fill, and an
    un-reported CommissionReport defaults to commission=0.0 -- so a fill whose commission is
    0/absent/nan is treated as UNKNOWN (not free). Returns the summed fee (>0) when at least one
    fill carries a real commission, else None (=> caller flags commission_unknown and never
    fabricates a $0 fee). NEVER raises."""
    try:
        fills = getattr(trade, "fills", None) or []
        total = 0.0
        seen = False
        for fl in fills:
            cr = getattr(fl, "commissionReport", None)
            c = getattr(cr, "commission", None) if cr is not None else None
            if c is None:
                continue
            try:
                cf = float(c)
            except (TypeError, ValueError):
                continue
            if cf != cf or cf == 0.0:   # nan, or the un-reported 0.0 default -> unknown, skip
                continue
            total += cf
            seen = True
        return round(total, 4) if seen else None
    except Exception:
        return None


def compute_entry_basis(estimated_debit, avg_fill_price, quantity):
    """Real entry cost basis + slippage from the ACTUAL fill price.

    `avg_fill_price` is the per-share NET fill (IBKR reports the combo NET for a spread), so the
    real basis is avg_fill_price*100*qty for BOTH singles and spreads -- directly comparable to
    the estimated `debit` (resolved.limit*100*qty). Returns
    (entry_fill_debit, entry_slippage, entry_slippage_pct); (None,None,None) when the fill price
    is unknown (never fabricated). Slippage = actual - estimated (positive = paid up). NEVER raises."""
    try:
        if avg_fill_price is None:
            return None, None, None
        afp = float(avg_fill_price)
        if afp != afp:  # nan
            return None, None, None
        q = int(quantity or 0)
        entry_fill_debit = round(afp * 100 * q, 2)
        slippage = slippage_pct = None
        if estimated_debit is not None:
            try:
                est = float(estimated_debit)
                slippage = round(entry_fill_debit - est, 2)
                if est != 0:
                    slippage_pct = round(slippage / abs(est) * 100, 2)
            except (TypeError, ValueError):
                slippage = slippage_pct = None
        return entry_fill_debit, slippage, slippage_pct
    except Exception:
        return None, None, None


class OrderManager:
    """Manages order placement with idempotency and partial fill tracking."""

    def __init__(self, ib_conn: IBConnection, state_manager: StateManager,
                 exit_slippage_floor: Optional[float] = None):
        self.ib_conn = ib_conn
        self.state_manager = state_manager
        # Config-driven bid-anchored exit slippage floor (2026-07-03). When the caller passes a
        # value (from cfg.rules.exit_slippage_floor) it takes effect WITHOUT a code edit; when None
        # (every existing call-site today, incl. manager.py) it falls back to the module constant
        # DEFAULT_EXIT_SLIPPAGE_FLOOR (0.50) -> byte-identical to before. Shadows the class attr,
        # which _build_close_order reads via self.EXIT_SLIPPAGE_FLOOR.
        self.EXIT_SLIPPAGE_FLOOR = (exit_slippage_floor if exit_slippage_floor is not None
                                    else DEFAULT_EXIT_SLIPPAGE_FLOOR)

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
            # A pre-transmission intent is deliberately blocking too.  It is released only after
            # restart reconciliation proves that no matching broker order/fill exists; treating
            # order_id=0 as free recreated the exact crash-window double-close this record prevents.
            ident = (f"id={in_flight.order_id}" if in_flight.order_id
                     else f"ref={in_flight.order_ref or 'pending-intent'}")
            return False, (f"con_id={con_id} already has durable in-flight close "
                           f"({ident}, remaining={in_flight.remaining_qty})")

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
        right: Optional[str] = None,
        bid: Optional[float] = None,
        trigger_type: Optional[str] = None,
        exit_context: Optional[dict] = None,
    ) -> OrderResult:
        """
        Place a SELL-TO-CLOSE order to close a position. LIMIT by default (passive, resting at
        the mark). A TRIGGERED exit (market=True) MUST fill to protect the position, so it is
        priced to fill on ANY book width -- the fill matters more than a few cents of slippage
        (Fable review, 2026-07-02): a mark-anchored limit like mark*(1-5%) can still rest ABOVE
        the bid on a WIDE option book and never protect the position.
          * If a live `bid` is available (single-leg only), the SELL is a MARKETABLE LIMIT *at the
            bid* -- it crosses the standing bid so it fills on a narrow OR wide book, FLOORED at a
            fraction of the mark so a broken/stub bid can't dump the position for pennies.
          * With no bid: a hard STOP (or an unknown triggered exit) uses a true MARKET order so it
            ALWAYS fills; a profit-TARGET (`trigger_type` in TARGET_TRIGGERS) stays a passive LIMIT
            at the mark -- no urgency to cross.
          * No usable mark (limit_price<=0, e.g. a manual one-tap exit) -> true MARKET order.
        `bid` and `trigger_type` are OPTIONAL and default to the guaranteed-fill behavior; the
        caller (manager.py eval loop) HAS both in scope (quotes[con_id]['bid'] and
        trigger.trigger_type) and SHOULD pass them to unlock the bid-anchored floor + passive-target
        refinements -- FLAGGED for Trevor, couldn't edit manager.py.
        For journaled spreads (spread={"short_con_id": ...}) the close is a single BAG combo
        (SELL the same combo that was bought) so both legs always close atomically -- closing
        the long leg alone would leave a naked short option.
        `right` ('C'/'P') is the option right of the LONG leg for a single-leg close; when None it
        is resolved from the live portfolio, else defaults to 'C' (see _resolve_close_right).
        ``exit_context`` is a JSON-serializable snapshot of the entry, trigger, and close sizing.
        It is committed atomically with the in-flight order so an asynchronous or restart-time
        fill can be finalized with the actual fill price and correct cost basis.  Callers that do
        not supply it retain the legacy state shape.

        Returns OrderResult indicating success/failure.
        """
        # Idempotency check (keyed by the long leg's con_id for spreads)
        can_place, reason = await self.can_place_close(con_id, quantity, live_open_orders)
        if not can_place:
            print(f"[INFO] Skipping order for con_id={con_id}: {reason}")
            return OrderResult(success=False, message=reason, con_id=con_id)

        # Create contract and order
        if spread and spread.get("short_con_id"):
            scid = int(spread["short_con_id"])
            # IDEMPOTENT COVER (2026-06-29): before BUYing to cover the short leg via the combo,
            # verify the short leg is STILL short live. If another close path (the liquidate/close
            # tool, clientId 91) already covered it, the combo would OVER-cover -> a long residual
            # that jams reconciliation (the 6/29 double-close). If already covered/flat, close the
            # long leg ALONE.
            short_qty = None
            try:
                _portfolio = list(self.ib_conn.ib.portfolio())
                if _portfolio:  # only trust the covered-check when we actually have portfolio data
                    # ABSENT-vs-PRESENT (2026-07-03 fix): default to None when the short leg is NOT
                    # in the portfolio -- an absent leg is UNKNOWN (possibly an incomplete portfolio
                    # read), NOT proven-covered. Closing the long alone on an unknown short would
                    # ORPHAN a still-live short (naked option). Only a POSITIVELY-observed short at
                    # >=0 (flat/covered) lets us safely close the long alone; None (absent/unknown)
                    # or a still-short (<0) falls through to the atomic combo close.
                    short_qty = next((p.position for p in _portfolio
                                      if p.contract.conId == scid), None)
            except Exception as _e:
                print(f"[WARN] could not read short-leg qty for {scid} ({_e}); using combo close")
            if short_qty is not None and short_qty >= 0:
                print(f"[WARN] spread short leg {scid} already covered (qty={short_qty}) -- "
                      f"closing long {con_id} ALONE to avoid over-cover (idempotent).")
                contract = self.ib_conn.create_contract(
                con_id, symbol=symbol, right=self._resolve_close_right(con_id, right))
            else:
                contract = self.ib_conn.create_combo_contract(
                    symbol, [(con_id, "BUY"), (scid, "SELL")])
        else:
            contract = self.ib_conn.create_contract(
                con_id, symbol=symbol, right=self._resolve_close_right(con_id, right))
        # A single-leg BID anchors a marketable exit price; a combo's per-leg bid is NOT the
        # net combo price, so never bid-anchor a spread -- fall back to the no-bid logic there.
        _eff_bid = None if (spread and spread.get("short_con_id")) else bid
        order = self._build_close_order(quantity, limit_price, market,
                                        bid=_eff_bid, trigger_type=trigger_type)

        # Normalize and prove serializability BEFORE anything can reach IB.  This prevents a live
        # close from escaping restart tracking because a context leaf was NaN/datetime/mock-shaped.
        try:
            normalized_context = _json_safe(dict(exit_context or {}))
            json.dumps(normalized_context, allow_nan=False)
        except Exception as e:
            print(f"[ERROR] Refusing close for con_id={con_id}: exit context is not serializable: {e}")
            return OrderResult(success=False, message=f"invalid exit context: {e}", con_id=con_id)

        # Reserve the client-scoped order id, attach a globally unique orderRef, and fsync the
        # placement intent BEFORE transmission.  If an old/mock connection cannot reserve an id,
        # orderRef still gives restart reconciliation a stable identity.
        order_id = 0
        try:
            reserve = getattr(self.ib_conn, "reserve_order_id", None)
            candidate = reserve() if callable(reserve) else 0
            order_id = _safe_int(candidate)
        except Exception as e:
            print(f"[WARN] could not pre-reserve IB order id for con_id={con_id}: {e}; using orderRef")
        order_ref = f"exitmgr-{con_id}-{uuid.uuid4().hex[:20]}"
        try:
            order.orderRef = order_ref
            if order_id:
                order.orderId = order_id
        except Exception as e:
            print(f"[ERROR] Refusing close for con_id={con_id}: cannot bind durable order identity: {e}")
            return OrderResult(success=False, message=f"cannot bind order identity: {e}", con_id=con_id)
        client_id = _safe_client_id(getattr(self.ib_conn, "client_id", None))
        in_flight = InFlightClose(
            con_id=con_id,
            order_id=order_id,
            remaining_qty=quantity,
            entry_debit=entry_debit,
            order_price=limit_price,
            placed_at=datetime.now(timezone.utc).isoformat(),
            exit_context=normalized_context,
            client_id=client_id,
            order_ref=order_ref,
            identity_version=1,
            placement_state="intent",
        )
        self.state_manager.state.add_in_flight(in_flight)
        try:
            self.state_manager.save()
        except Exception as e:
            self.state_manager.state.remove_in_flight(con_id)
            print(f"[ERROR] Refusing close for con_id={con_id}: placement intent was not durable: {e}")
            return OrderResult(success=False, message=f"placement intent save failed: {e}", con_id=con_id)

        # Never blindly retry an ambiguous placeOrder exception: the first call may have reached
        # IB, and a retry can double-close.  Keep the prepared identity; the broker reconciliation
        # path binds it to an open/completed order or releases it only after exhaustive reads.
        try:
            placed_trade = await self.ib_conn.place_order(contract, order)
        except Exception as e:
            print(f"[ERROR] Close transmission outcome is ambiguous for con_id={con_id}: {e}; "
                  "durable intent retained for broker reconciliation (NOT retrying blindly)")
            return OrderResult(success=False, order_id=order_id, message=str(e), con_id=con_id,
                               client_id=client_id, order_ref=order_ref)

        trade_order = getattr(placed_trade, "order", None)
        order_id = (_safe_int(getattr(trade_order, "orderId", 0)) or order_id)
        perm_id = _safe_int(getattr(trade_order, "permId", 0))
        returned_client_id = _safe_client_id(getattr(trade_order, "clientId", None))
        client_id = returned_client_id if returned_client_id is not None else client_id
        returned_ref = getattr(trade_order, "orderRef", None)
        order_ref = returned_ref if isinstance(returned_ref, str) and returned_ref else order_ref
        in_flight.order_id = order_id
        in_flight.perm_id = perm_id
        in_flight.client_id = client_id
        in_flight.order_ref = order_ref
        in_flight.placement_state = "submitted"
        try:
            self.state_manager.save()
        except Exception as e:
            # The pre-transmission intent is already durable.  Continue with the returned Trade;
            # restart lookup can recover it by the reserved id/orderRef even if this enrichment
            # did not reach disk.
            print(f"[WARN] Could not persist submitted identity for con_id={con_id}: {e}; "
                  "pre-transmission intent remains durable")

        # Briefly poll for an immediate ACK/reject.  A terminal PARTIAL fill is a realized trade,
        # not a zero-fill rejection: retain it and return success so the caller finalizes its P&L.
        _dead = {"Cancelled", "ApiCancelled", "Inactive"}
        _live = {"Filled", "Submitted", "PreSubmitted"}
        _status = None
        _ost = getattr(placed_trade, "orderStatus", None)
        if _ost is not None:
            for _ in range(24):  # up to ~12s for IBKR to ACK or REJECT
                _status = getattr(_ost, "status", None)
                if not isinstance(_status, str) or _status in _live or _status in _dead:
                    break
                await asyncio.sleep(0.5)
        _filled_raw = getattr(_ost, "filled", 0) if _ost is not None else 0
        if isinstance(_filled_raw, (int, float)) and not isinstance(_filled_raw, bool):
            _filled = float(_filled_raw)
            if not math.isfinite(_filled) or _filled < 0:
                _filled = 0.0
        else:
            _filled = 0.0
        if isinstance(_status, str) and _status in _dead and _filled <= 0:
            _reasons = []
            try:
                _reasons = [le.message for le in placed_trade.log if getattr(le, "errorCode", 0)]
            except Exception:
                pass
            _why = _reasons[-1] if _reasons else _status
            self.state_manager.state.remove_in_flight(con_id)
            self.state_manager.save()
            print(f"[ORDER REJECTED] con_id={con_id}, order_id={order_id}, status={_status}, "
                  f"filled=0 -- retry allowed next cycle. reason: {_why}")
            return OrderResult(success=False, order_id=order_id,
                               message=f"order {_status}: {_why}", con_id=con_id,
                               trade=placed_trade, perm_id=perm_id, client_id=client_id,
                               order_ref=order_ref)

        # Count only accepted or partially-filled placements, not zero-fill rejects.
        notional = limit_price * 100 * quantity
        today = _trading_day()
        self.state_manager.state.update_daily_stats(today, order_count=1, notional=notional)
        try:
            self.state_manager.save()
        except Exception as e:
            # The close and its pre-transmission identity are already durable.  Do not turn an
            # accepted broker order into a caller-visible failure that could invite a retry.
            print(f"[WARN] Accepted close for con_id={con_id} but daily-stat persistence failed: {e}")
        msg = (f"Order {_status} after partial fill" if _status in _dead and _filled > 0
               else "Order placed successfully")
        print(f"[ORDER PLACED] con_id={con_id}, order_id={order_id}, perm_id={perm_id or 'pending'}, "
              f"qty={quantity}, price={limit_price}, status={_status or 'unknown'}")
        return OrderResult(success=True, order_id=order_id, message=msg, con_id=con_id,
                           trade=placed_trade, perm_id=perm_id, client_id=client_id,
                           order_ref=order_ref)

    def _resolve_close_right(self, con_id: int, right: Optional[str]) -> str:
        """Resolve the option `right` ('C'/'P') for a single-leg close (P2.4).
        Hardcoding 'C' would mis-close a long PUT / put-spread as a call. Precedence:
          1) an explicit `right` passed by the caller (backward-compatible; None today);
          2) the live portfolio's contract right for this con_id (the entry's real right);
          3) fall back to 'C' with a loud warning.
        TODO(caller): manager.place_close_order should pass right=je.get('right') from the
        journal so this never has to fall back -- flagged for Trevor (couldn't edit manager.py)."""
        if right in ("C", "P"):
            return right
        try:
            for p in self.ib_conn.ib.portfolio():
                if getattr(p.contract, "conId", None) == con_id and \
                        getattr(p.contract, "right", None) in ("C", "P"):
                    return p.contract.right
        except Exception as _e:
            print(f"[WARN] could not resolve option right for con_id={con_id} from portfolio ({_e})")
        print(f"[WARN] con_id={con_id}: option right unresolved -- defaulting to 'C' (P2.4). "
              f"Callers should pass the journaled right so a long PUT/put-spread closes correctly.")
        return "C"

    # (legacy) reference buffer; retained for config compat. No longer used to price a stop --
    # a fixed mark-buffer could rest ABOVE the bid on a wide book and fail to fill (Fable review).
    MARKETABLE_BUFFER = 0.05

    # Bid-anchored sanity FLOOR for a triggered exit (P2.8, 2026-07-02): never SELL a triggered
    # close below (1 - EXIT_SLIPPAGE_FLOOR) of the mark. This is a CATASTROPHE guard against a
    # broken/1-lot stub bid -- deliberately GENEROUS (a legitimately wide option book still fills;
    # only an obviously-broken bid is refused, and a refused/resting exit is escalated by the
    # manager's unfilled-order alarm). FLAG for Trevor: make this a config knob
    # (e.g. rules.exit_slippage_floor) rather than a module constant. WIRED 2026-07-03: now
    # overridable per-instance via the constructor `exit_slippage_floor` arg (config-driven); this
    # class attr is the ultimate class-level fallback, sourced from the module constant so there is
    # ONE source of truth. _build_close_order reads self.EXIT_SLIPPAGE_FLOOR (the instance attr set
    # in __init__, which defaults to this). No config/arg => byte-identical to before (0.50).
    EXIT_SLIPPAGE_FLOOR = DEFAULT_EXIT_SLIPPAGE_FLOOR

    # Triggers with no urgency to cross the spread -- a profit-target can rest passively at the mark.
    TARGET_TRIGGERS = frozenset({"profit_target", "take_profit", "scale_out", "target"})

    def _build_close_order(self, quantity: int, limit_price: float, market: bool,
                           bid: Optional[float] = None,
                           trigger_type: Optional[str] = None) -> Order:
        """Build the SELL-to-close order (P2.8). Default (market=False): a passive LIMIT resting
        at the mark. A TRIGGERED exit (market=True) must FILL -- priced so it crosses on any book:
          1) live `bid` -> MARKETABLE LIMIT *at the bid* (crosses the standing bid, fills on narrow
             OR wide books), FLOORED at mark*(1-EXIT_SLIPPAGE_FLOOR) so a broken/stub bid can't
             dump for pennies (when floored the limit rests above the junk bid and won't fill --
             the intended refusal; the manager's unfilled-order alarm escalates it).
          2) no bid, profit-TARGET -> passive LIMIT at the mark (no urgency to cross).
          3) no bid, hard STOP / unknown triggered exit -> true MARKET order: GUARANTEE the fill.
             Without a bid we can't know where a wide book sits, and a mark-anchored limit can rest
             above the bid and never protect the position -- a stop that doesn't fill is worse than
             slippage.
          4) no usable mark (limit_price<=0, e.g. manual one-tap) -> true MARKET."""
        mark = limit_price if (limit_price and limit_price > 0) else None

        if not market:
            # passive close -> rest a LIMIT at the mark (unchanged behaviour)
            return self.ib_conn.create_limit_order("SELL", quantity, limit_price)

        # ---- triggered exit: it MUST fill to protect the position ----
        # (1) BEST: a live bid -> SELL LIMIT at the bid (marketable on any book), floored.
        if bid is not None and bid == bid and bid > 0:  # bid==bid rejects NaN
            floor = round(mark * (1 - self.EXIT_SLIPPAGE_FLOOR), 2) if mark else 0.01
            floor = max(floor, 0.01)
            px = max(round(bid, 2), floor)
            return self.ib_conn.create_limit_order("SELL", quantity, px)

        # (2) no bid, known profit-TARGET -> passive LIMIT at the mark (no urgency).
        if (trigger_type or "").lower() in self.TARGET_TRIGGERS and mark:
            return self.ib_conn.create_limit_order("SELL", quantity, round(mark, 2))

        # (3)/(4) hard STOP, unknown triggered exit, or no usable mark -> true MARKET (always fills).
        return self.ib_conn.create_market_order("SELL", quantity)

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
