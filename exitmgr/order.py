"""Public API contract; production-derived narrative omitted."""

import asyncio
import json
import math
import uuid
from typing import Optional, Dict, List
from dataclasses import dataclass
from datetime import datetime, timezone

from exitmgr.ibkr import Contract, Order

from exitmgr.connection import IBConnection, OrderNotTransmittedError, OrderView
from exitmgr.runtime_identity import RuntimeIdentity, identity_fields
from exitmgr.state import State, StateManager, InFlightClose






DEFAULT_EXIT_SLIPPAGE_FLOOR = 0.50


def _trading_day(now=None) -> str:
    """Public API contract; production-derived narrative omitted."""
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
    """Public API contract; production-derived narrative omitted."""
    success: bool
    order_id: Optional[int] = None
    message: str = ""
    con_id: Optional[int] = None
    trade: object = None
    perm_id: Optional[int] = None
    client_id: Optional[int] = None
    order_ref: Optional[str] = None
    acknowledged: bool = False
    ambiguous: bool = False


def _safe_int(value) -> int:
    """Public API contract; production-derived narrative omitted."""
    return int(value) if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _safe_client_id(value):
    """Public API contract; production-derived narrative omitted."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return int(value)
    return None


def _json_safe(value):
    """Public API contract; production-derived narrative omitted."""
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


def trade_reported_filled(trade) -> Optional[float]:
    """Public API contract; production-derived narrative omitted."""
    value = getattr(getattr(trade, "orderStatus", None), "filled", None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and value >= 0 else None


def trade_has_proven_zero_fill(trade) -> bool:
    """Public API contract; production-derived narrative omitted."""
    fills = getattr(trade, "fills", None)
    return (trade_reported_filled(trade) == 0.0
            and isinstance(fills, (list, tuple)) and len(fills) == 0)


def submitted_close_snapshot(contract, order, *, parent_con_id: int,
                             combo_qty: int) -> dict:
    """Public API contract; production-derived narrative omitted."""
    try:
        qty = int(combo_qty)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"unusable submitted close quantity {combo_qty!r}") from exc
    if qty <= 0:
        raise ValueError(f"submitted close quantity must be positive, got {qty}")
    outer = str(getattr(order, "action", "") or "").upper()
    if outer not in {"BUY", "SELL"}:
        raise ValueError(f"submitted close action is unusable: {outer!r}")
    sec_type = str(getattr(contract, "secType", "") or "").upper()
    legs = []
    combo_legs = list(getattr(contract, "comboLegs", None) or [])
    if sec_type == "BAG" or combo_legs:
        if sec_type != "BAG" or not combo_legs:
            raise ValueError("submitted combo contract has inconsistent BAG topology")
        for leg in combo_legs:
            con_id = _safe_int(getattr(leg, "conId", 0))
            ratio = _safe_int(getattr(leg, "ratio", 0))
            leg_action = str(getattr(leg, "action", "") or "").upper()
            if not con_id or not ratio or leg_action not in {"BUY", "SELL"}:
                raise ValueError("submitted BAG contains an unusable leg")

            actual = leg_action if outer == "BUY" else ("SELL" if leg_action == "BUY" else "BUY")
            legs.append({"con_id": con_id, "ratio": ratio,
                         "expected_side": "BOT" if actual == "BUY" else "SLD",
                         "multiplier": 100})
        if len(legs) < 2 or len({leg["con_id"] for leg in legs}) != len(legs):
            raise ValueError("submitted BAG must contain distinct legs")
    else:
        con_id = _safe_int(getattr(contract, "conId", 0)) or _safe_int(parent_con_id)
        if not con_id:
            raise ValueError("submitted single-leg close has no contract id")
        legs = [{"con_id": con_id, "ratio": 1,
                 "expected_side": "BOT" if outer == "BUY" else "SLD",
                 "multiplier": 100}]
        sec_type = sec_type or "OPT"
    def _finite_or_none(value):
        try:
            number = float(value)


            return number if math.isfinite(number) and abs(number) < 1e100 else None
        except (TypeError, ValueError):
            return None
    order_type = str(getattr(order, "orderType", "") or "").upper()
    limit_price = (_finite_or_none(getattr(order, "lmtPrice", None))
                   if "LMT" in order_type else None)
    stop_price = (_finite_or_none(getattr(order, "auxPrice", None))
                  if ("STP" in order_type or "TRAIL" in order_type) else None)
    return {"schema": "submitted_close.v1", "sec_type": sec_type,
            "action": outer,
            "combo_qty": qty, "legs": legs,
            "order_type": order_type,
            "tif": str(getattr(order, "tif", "") or ""),
            "limit_price": limit_price, "stop_price": stop_price}


def commission_from_trade(trade) -> Optional[float]:
    """Public API contract; production-derived narrative omitted."""
    try:
        fills = getattr(trade, "fills", None) or []
        if not fills:
            return None
        total = 0.0
        for fl in fills:
            cr = getattr(fl, "commissionReport", None)
            c = getattr(cr, "commission", None) if cr is not None else None
            if c is None:
                return None
            try:
                cf = float(c)
            except (TypeError, ValueError):
                return None
            if not math.isfinite(cf) or cf == 0.0:
                return None
            total += cf
        return round(total, 4)
    except Exception:
        return None


def fill_timestamp_from_trade(trade):
    """Public API contract; production-derived narrative omitted."""
    try:
        times = [getattr(getattr(fl, "execution", None), "time", None)
                 for fl in (getattr(trade, "fills", None) or [])]
        times = [t for t in times if t is not None]
        if times:
            t = max(times)


            if getattr(t, "tzinfo", None) is None:
                t = t.replace(tzinfo=timezone.utc)
            return t.isoformat(), "broker_execution"
    except Exception:
        pass


    return datetime.now(timezone.utc).isoformat(), "local_clock"


def compute_entry_basis(estimated_debit, avg_fill_price, quantity):
    """Public API contract; production-derived narrative omitted."""
    try:
        if avg_fill_price is None:
            return None, None, None
        afp = float(avg_fill_price)
        if afp != afp:
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


@dataclass(frozen=True)
class CloseRight:
    """Public API contract; production-derived narrative omitted."""

    right: Optional[str]
    resolved: bool
    error: Optional[str] = None

    def __bool__(self):
        raise TypeError(
            "CloseRight has no truth value -- check .resolved explicitly. An unresolved option "
            "right is UNKNOWN, never 'C'.")

    @classmethod
    def known(cls, right: str) -> "CloseRight":
        assert right in ("C", "P"), "a known right is 'C' or 'P', got %r" % (right,)
        return cls(right=right, resolved=True)

    @classmethod
    def unknown(cls, error: str) -> "CloseRight":
        return cls(right=None, resolved=False, error=error or "option right unresolved")

    @property
    def for_contract(self) -> str:
        """Public API contract; production-derived narrative omitted."""
        return self.right if self.resolved else ""


class OrderManager:
    """Public API contract; production-derived narrative omitted."""

    def __init__(self, ib_conn: IBConnection, state_manager: StateManager,
                 exit_slippage_floor: Optional[float] = None,
                 runtime_identity: Optional[RuntimeIdentity] = None):
        self.ib_conn = ib_conn
        self.state_manager = state_manager


        self._runtime_identity_fields = (
            identity_fields(runtime_identity) if runtime_identity is not None else None)





        self.EXIT_SLIPPAGE_FLOOR = (exit_slippage_floor if exit_slippage_floor is not None
                                    else DEFAULT_EXIT_SLIPPAGE_FLOOR)

    async def _resting_short_close(self, con_id: int) -> tuple[bool, Optional[object], Optional[str]]:
        """Public API contract; production-derived narrative omitted."""
        getter = getattr(self.ib_conn, "get_open_orders", None)
        if not callable(getter):
            return False, None, "connection cannot report open orders"
        try:
            live = await getter(short_leg_con_ids={int(con_id)})
        except TypeError as e:


            return False, None, f"get_open_orders cannot admit a short leg: {e}"
        except Exception as e:
            return False, None, f"open-order read failed: {e}"
        try:
            od = (live or {}).get(int(con_id))
        except Exception as e:
            return False, None, f"unusable open-order response: {e}"
        return (od is not None), od, None

    async def can_place_close(
        self,
        con_id: int,
        quantity: int,
        live_open_orders,
        short_close: bool = False,
    ) -> tuple[bool, str]:
        """Public API contract; production-derived narrative omitted."""



        if isinstance(live_open_orders, OrderView):
            if not live_open_orders.readable:
                return False, (f"con_id={con_id}: cannot verify whether a close is already "
                               f"resting ({live_open_orders.error}); refusing to place a "
                               f"possible duplicate close")
            live_open_orders = live_open_orders.orders


        in_flight = self.state_manager.state.get_in_flight(con_id)
        if in_flight is not None:



            ident = (f"id={in_flight.order_id}" if in_flight.order_id
                     else f"ref={in_flight.order_ref or 'pending-intent'}")
            return False, (f"con_id={con_id} already has durable in-flight close "
                           f"({ident}, remaining={in_flight.remaining_qty})")


        if con_id in live_open_orders:
            live_order = live_open_orders[con_id]

            live_order_id = (live_order.get("order_id", 0) if isinstance(live_order, dict)
                             else getattr(live_order, "order_id", 0))
            if live_order_id != 0:
                return False, f"con_id={con_id} already has live open order (id={live_order_id})"



        if short_close:
            visible, live_short_order, err = await self._resting_short_close(con_id)
            if err is not None:
                return False, (f"con_id={con_id}: cannot verify whether a buy-to-close is already "
                               f"resting ({err}); refusing to place a possible duplicate cover")
            if visible:
                _oid = getattr(live_short_order, "order_id", 0)
                _rem = getattr(live_short_order, "remaining", "?")
                return False, (f"con_id={con_id} already has a resting buy-to-close at the broker "
                               f"(id={_oid}, remaining={_rem})")

        return True, ""

    def _preflight_short_close(self, con_id: int, symbol: str, quantity: int,
                               spread: Optional[dict]) -> tuple[bool, str, int, Optional[str]]:
        """Public API contract; production-derived narrative omitted."""
        if spread and spread.get("short_con_id"):
            return (False, "buy-to-close does not accept a spread combo (a standalone short closes "
                           "as a single leg; a spread is submitted and verified as one combo)",
                    0, None)
        try:
            requested = abs(int(quantity))
        except (TypeError, ValueError):
            return (False, f"unusable close quantity {quantity!r}", 0, None)
        if requested <= 0:
            return (False, f"close quantity must be non-zero (got {quantity!r})", 0, None)

        try:
            portfolio = list(self.ib_conn.ib.portfolio())
        except Exception as e:
            return (False, f"could not read the portfolio to confirm the short is still open ({e}); "
                           f"refusing to BUY a contract we cannot prove we are short", 0, None)
        if not portfolio:
            return (False, "portfolio read returned no rows; refusing to BUY a contract we cannot "
                           "prove we are short (an unverified buy-to-close OPENS a long)", 0, None)

        live = next((p for p in portfolio
                     if getattr(getattr(p, "contract", None), "conId", None) == con_id), None)
        if live is None:
            return (False, f"con_id={con_id} is no longer in the portfolio -- already covered, "
                           f"assigned, or an incomplete read. Refusing (a BUY here opens a long).",
                    0, None)
        try:
            live_qty = int(getattr(live, "position", 0) or 0)
        except (TypeError, ValueError):
            return (False, f"con_id={con_id}: unreadable live position quantity", 0, None)
        if live_qty >= 0:
            return (False, f"con_id={con_id} is not short (live qty={live_qty}) -- already covered "
                           f"or long. Refusing to BUY.", 0, None)

        contract = getattr(live, "contract", None)
        sec_type = str(getattr(contract, "secType", "") or "").upper()
        if sec_type not in ("", "OPT", "FOP"):
            return (False, f"con_id={con_id}: secType={sec_type!r} is not an option (an ASSIGNED put "
                           f"leaves a STOCK row); an option close is the wrong instrument", 0, None)
        right = getattr(contract, "right", None)
        if right not in ("C", "P"):
            return (False, f"con_id={con_id}: option right unresolved from the portfolio "
                           f"(got {right!r}); refusing rather than defaulting to 'C' -- a CSP is a "
                           f"PUT and a wrong-right contract is a wrong-instrument order", 0, None)

        held = abs(live_qty)
        close_qty = min(requested, held)
        if close_qty < requested:
            print(f"[WARN] con_id={con_id} ({symbol}): requested buy-to-close {requested} but only "
                  f"{held} short -- clamping to {close_qty} (over-cover would open a long).")
        return (True, "", close_qty, right)

    @staticmethod
    def _normalize_short_context(ctx: Optional[dict], close_qty: int) -> dict:
        """Public API contract; production-derived narrative omitted."""
        out = dict(ctx or {})
        q = abs(int(close_qty))
        out["close_qty"] = q
        try:
            pq = abs(int(out.get("position_qty") or q))
        except (TypeError, ValueError):
            pq = q
        out["position_qty"] = pq or q
        out["is_short"] = True
        out["close_action"] = "BUY"
        return out

    @staticmethod
    def _validate_close_order(order, expected_action: str,
                              *, strict: bool = False) -> tuple[bool, str]:
        """Public API contract; production-derived narrative omitted."""
        action = getattr(order, "action", None)
        if isinstance(action, str):
            if action != expected_action:
                return False, (f"wrong-side close order: action={action!r}, "
                               f"expected {expected_action!r}")
        elif strict:
            return False, (f"close order action is unreadable ({action!r}); refusing to transmit "
                           f"an order that cannot be proven {expected_action}-side")

        raw = getattr(order, "totalQuantity", None)
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            if raw != raw or raw <= 0:
                return False, f"close order quantity must be positive, got {raw!r}"
        elif strict:
            return False, f"close order quantity is not a number: {raw!r}"

        if strict and getattr(order, "orderType", None) == "LMT":
            px = getattr(order, "lmtPrice", None)
            if isinstance(px, bool) or not isinstance(px, (int, float)) or px != px or px <= 0:
                return False, f"close limit price must be positive, got {px!r}"
        return True, ""

    @staticmethod
    def resolve_close_anchor(bid, combo_bid, spread):
        """Public API contract; production-derived narrative omitted."""
        eff = None if (spread and spread.get("short_con_id")) else bid
        if eff is None and combo_bid is not None:
            try:
                cb = float(combo_bid)
                if cb == cb and cb > 0:
                    eff = cb
            except (TypeError, ValueError):
                pass
        return eff

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
        short_close: bool = False,
        ask: Optional[float] = None,
        combo_bid: Optional[float] = None,
    ) -> OrderResult:
        """Public API contract; production-derived narrative omitted."""

        can_place, reason = await self.can_place_close(con_id, quantity, live_open_orders,
                                                       short_close=short_close)
        if not can_place:
            print(f"[INFO] Skipping order for con_id={con_id}: {reason}")
            return OrderResult(success=False, message=reason, con_id=con_id)


        _short_right: Optional[str] = None
        if short_close:
            _ok, _why, _close_qty, _short_right = self._preflight_short_close(
                con_id, symbol, quantity, spread)
            if not _ok:
                print(f"[ERROR] Refusing buy-to-close for con_id={con_id}: {_why}")
                return OrderResult(success=False, message=_why, con_id=con_id)


            quantity = _close_qty


        if short_close:
            contract = self.ib_conn.create_contract(con_id, symbol=symbol, right=_short_right)
        elif spread and spread.get("short_con_id"):
            scid = int(spread["short_con_id"])





            short_qty = None
            try:
                _portfolio = list(self.ib_conn.ib.portfolio())
                if _portfolio:






                    short_qty = next((p.position for p in _portfolio
                                      if p.contract.conId == scid), None)
            except Exception as _e:
                print(f"[WARN] could not read short-leg qty for {scid} ({_e}); using combo close")
            if short_qty is not None and short_qty >= 0:
                print(f"[WARN] spread short leg {scid} already covered (qty={short_qty}) -- "
                      f"closing long {con_id} ALONE to avoid over-cover (idempotent).")
                contract = self.ib_conn.create_contract(
                    con_id, symbol=symbol,
                    right=self._resolve_close_right(con_id, right).for_contract)
            else:
                contract = self.ib_conn.create_combo_contract(
                    symbol, [(con_id, "BUY"), (scid, "SELL")])
        else:
            contract = self.ib_conn.create_contract(
                con_id, symbol=symbol,
                right=self._resolve_close_right(con_id, right).for_contract)












        _eff_bid = self.resolve_close_anchor(bid, combo_bid, spread)
        order = self._build_close_order(quantity, limit_price, market,
                                        bid=_eff_bid, trigger_type=trigger_type,
                                        short_close=short_close, ask=ask)



        _expected_action = "BUY" if short_close else "SELL"
        try:
            order.action = _expected_action
        except Exception as exc:
            return OrderResult(success=False,
                               message=f"cannot stamp close action: {exc}", con_id=con_id)


        _ok_order, _order_why = self._validate_close_order(
            order, _expected_action, strict=short_close)
        if not _ok_order:
            print(f"[ERROR] Refusing close for con_id={con_id}: {_order_why}")
            return OrderResult(success=False, message=_order_why, con_id=con_id)



        try:
            normalized_context = _json_safe(dict(exit_context or {}))
            if self._runtime_identity_fields is not None:
                for key, expected in self._runtime_identity_fields.items():
                    observed = normalized_context.get(key)
                    if observed not in (None, "", expected):
                        raise ValueError(
                            "%s conflicts with the frozen process identity" % key)
                    normalized_context[key] = expected
            if short_close:


                normalized_context = self._normalize_short_context(normalized_context, quantity)
            json.dumps(normalized_context, allow_nan=False)
            submitted_close = submitted_close_snapshot(
                contract, order, parent_con_id=con_id, combo_qty=quantity)
            json.dumps(submitted_close, allow_nan=False)
        except Exception as e:
            print(f"[ERROR] Refusing close for con_id={con_id}: exit context is not serializable: {e}")
            return OrderResult(success=False, message=f"invalid exit context: {e}", con_id=con_id)




        order_id = 0
        try:
            reserve = getattr(self.ib_conn, "reserve_order_id", None)
            candidate = reserve() if callable(reserve) else 0
            order_id = _safe_int(candidate)
        except Exception as e:




            if _is_link_down(e):
                print(f"[ERROR] Refusing close for con_id={con_id}: link failed while reserving "
                      f"the order id ({e}). NOTHING was transmitted; reconnect and reconcile "
                      "on the next cycle before retrying.")
                return OrderResult(
                    success=False, con_id=con_id,
                    message=("link down before transmission (nothing sent; "
                             "reconcile next cycle)"))
            else:
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
            submitted_close=submitted_close,
        )
        self.state_manager.state.add_in_flight(in_flight)
        try:
            self.state_manager.save()
        except Exception as e:
            self.state_manager.state.remove_in_flight(con_id)
            print(f"[ERROR] Refusing close for con_id={con_id}: placement intent was not durable: {e}")
            return OrderResult(success=False, message=f"placement intent save failed: {e}", con_id=con_id)




        try:
            placed_trade = await self.ib_conn.place_order(contract, order)
        except OrderNotTransmittedError as e:



            self.state_manager.state.remove_in_flight(con_id)
            try:
                self.state_manager.save()
            except Exception as save_error:

                self.state_manager.state.add_in_flight(in_flight)
                print(f"[ERROR] Nothing was transmitted for con_id={con_id}, but the durable "
                      f"intent could not be released ({save_error}); retaining the latch")
                return OrderResult(
                    success=False, order_id=order_id, con_id=con_id,
                    message=f"nothing sent; durable latch release failed: {save_error}",
                    client_id=client_id, order_ref=order_ref)
            print(f"[ERROR] Refusing close for con_id={con_id}: {e}. Durable intent released; "
                  "canonical reconciliation is required before retry.")
            return OrderResult(
                success=False, order_id=order_id, con_id=con_id,
                message=str(e), client_id=client_id, order_ref=order_ref)
        except Exception as e:
            print(f"[ERROR] Close transmission outcome is ambiguous for con_id={con_id}: {e}; "
                  "durable intent retained for broker reconciliation (NOT retrying blindly)")
            in_flight.placement_state = "transmission_ambiguous"
            try:
                self.state_manager.save()
            except Exception as save_error:
                print(f"[ERROR] Could not persist transmission-ambiguous state for "
                      f"con_id={con_id}: {save_error}; in-memory latch remains engaged")
            _alert_ambiguous_close(con_id, e, getattr(contract, "symbol", None))
            return OrderResult(success=False, order_id=order_id, message=str(e), con_id=con_id,
                               client_id=client_id, order_ref=order_ref, ambiguous=True)

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
        try:
            self.state_manager.save()
        except Exception as e:



            print(f"[WARN] Could not persist submitted identity for con_id={con_id}: {e}; "
                  "pre-transmission intent remains durable")



        _dead = {"Cancelled", "ApiCancelled", "Inactive"}
        _live = {"Filled", "Submitted", "PreSubmitted", "PendingCancel"}
        _status = None
        _ost = None
        for _ in range(24):
            _ost = getattr(placed_trade, "orderStatus", None)
            _status = getattr(_ost, "status", None) if _ost is not None else None
            if isinstance(_status, str) and (_status in _live or _status in _dead):
                break
            await asyncio.sleep(0.5)
        _filled = trade_reported_filled(placed_trade)
        if isinstance(_status, str) and _status in _dead and trade_has_proven_zero_fill(placed_trade):
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
                               order_ref=order_ref, acknowledged=True)

        if isinstance(_status, str) and _status in _dead and (_filled is None or _filled <= 0):


            _why = RuntimeError(
                f"terminal {_status} has unproven zero fills; retaining close for execution recovery")
            in_flight.placement_state = "transmission_ambiguous"
            try:
                self.state_manager.save()
            except Exception as save_error:
                print(f"[ERROR] Could not persist terminal-fill uncertainty for con_id={con_id}: "
                      f"{save_error}; original durable intent remains")
            _alert_ambiguous_close(con_id, _why, getattr(contract, "symbol", None))
            return OrderResult(
                success=False, order_id=order_id, message=str(_why), con_id=con_id,
                trade=placed_trade, perm_id=perm_id, client_id=client_id,
                order_ref=order_ref, acknowledged=True, ambiguous=True)

        if not (isinstance(_status, str) and (_status in _live or _status in _dead)):



            _why = RuntimeError(
                f"no broker ACK after 12s (status={_status!r}); order may or may not be live")
            print(f"[ERROR] Close ACK is ambiguous for con_id={con_id}: {_why}; durable intent "
                  "retained for broker reconciliation (NOT retrying blindly)")
            in_flight.placement_state = "transmission_ambiguous"
            try:
                self.state_manager.save()
            except Exception as save_error:
                print(f"[ERROR] Could not persist transmission-ambiguous state for "
                      f"con_id={con_id}: {save_error}; in-memory latch remains engaged")
            _alert_ambiguous_close(con_id, _why, getattr(contract, "symbol", None))
            return OrderResult(
                success=False, order_id=order_id, message=str(_why), con_id=con_id,
                trade=placed_trade, perm_id=perm_id, client_id=client_id,
                order_ref=order_ref, ambiguous=True)

        in_flight.placement_state = "submitted"
        try:
            self.state_manager.save()
        except Exception as e:
            print(f"[WARN] Broker acknowledged close for con_id={con_id}, but submitted-state "
                  f"persistence failed: {e}; the durable intent remains a conservative latch")


        notional = limit_price * 100 * quantity
        today = _trading_day()
        self.state_manager.state.update_daily_stats(today, order_count=1, notional=notional)
        try:
            self.state_manager.save()
        except Exception as e:


            print(f"[WARN] Accepted close for con_id={con_id} but daily-stat persistence failed: {e}")
        msg = (f"Order {_status} after partial fill" if _status in _dead and _filled is not None and _filled > 0
               else "Order placed successfully")
        print(f"[ORDER PLACED] con_id={con_id}, order_id={order_id}, perm_id={perm_id or 'pending'}, "
              f"qty={quantity}, price={limit_price}, status={_status or 'unknown'}")
        return OrderResult(success=True, order_id=order_id, message=msg, con_id=con_id,
                           trade=placed_trade, perm_id=perm_id, client_id=client_id,
                           order_ref=order_ref, acknowledged=True)

    def _resolve_close_right(self, con_id: int, right: Optional[str]) -> CloseRight:
        """Public API contract; production-derived narrative omitted."""
        if right in ("C", "P"):
            return CloseRight.known(right)
        try:
            for p in self.ib_conn.ib.portfolio():
                if getattr(p.contract, "conId", None) == con_id and \
                        getattr(p.contract, "right", None) in ("C", "P"):
                    return CloseRight.known(p.contract.right)
        except Exception as _e:
            print(f"[WARN] could not read the portfolio to resolve the option right for "
                  f"con_id={con_id} ({_e})")
            return CloseRight.unknown(f"portfolio unreadable: {_e}")
        print(f"[WARN] con_id={con_id}: option right UNRESOLVED -- neither the caller nor the "
              f"portfolio names it. Sending the close on its conId alone rather than asserting "
              f"'C'; pass the journaled right so a long PUT/put-spread is named explicitly.")
        return CloseRight.unknown("absent from the portfolio and not supplied by the caller")



    MARKETABLE_BUFFER = 0.05











    EXIT_SLIPPAGE_FLOOR = DEFAULT_EXIT_SLIPPAGE_FLOOR










    SHORT_EXIT_SLIPPAGE_CEILING = "mark * (1 + EXIT_SLIPPAGE_FLOOR)"


    TARGET_TRIGGERS = frozenset({"profit_target", "take_profit", "scale_out", "target"})

    def _build_short_close_order(self, quantity: int, limit_price: float, market: bool,
                                 ask: Optional[float] = None,
                                 trigger_type: Optional[str] = None) -> Order:
        """Public API contract; production-derived narrative omitted."""
        qty = abs(int(quantity))
        mark = limit_price if (limit_price and limit_price > 0) else None

        if not market:

            return self.ib_conn.create_limit_order("BUY", qty, limit_price)


        if ask is not None and ask == ask and ask > 0:
            px = round(ask, 2)
            if mark:
                px = min(px, round(mark * (1 + self.EXIT_SLIPPAGE_FLOOR), 2))
            px = max(px, 0.01)
            return self.ib_conn.create_limit_order("BUY", qty, px)


        if (trigger_type or "").lower() in self.TARGET_TRIGGERS and mark:
            return self.ib_conn.create_limit_order("BUY", qty, round(mark, 2))


        return self.ib_conn.create_market_order("BUY", qty)

    def _build_close_order(self, quantity: int, limit_price: float, market: bool,
                           bid: Optional[float] = None,
                           trigger_type: Optional[str] = None,
                           short_close: bool = False,
                           ask: Optional[float] = None) -> Order:
        """Public API contract; production-derived narrative omitted."""
        if short_close:
            return self._build_short_close_order(quantity, limit_price, market,
                                                 ask=ask, trigger_type=trigger_type)

        mark = limit_price if (limit_price and limit_price > 0) else None

        if not market:

            return self.ib_conn.create_limit_order("SELL", quantity, limit_price)



        if bid is not None and bid == bid and bid > 0:
            floor = round(mark * (1 - self.EXIT_SLIPPAGE_FLOOR), 2) if mark else 0.01
            floor = max(floor, 0.01)
            px = max(round(bid, 2), floor)
            return self.ib_conn.create_limit_order("SELL", quantity, px)


        if (trigger_type or "").lower() in self.TARGET_TRIGGERS and mark:
            return self.ib_conn.create_limit_order("SELL", quantity, round(mark, 2))


        return self.ib_conn.create_market_order("SELL", quantity)

    async def reprice_unfilled_protective_exit(self, con_id, **evidence):
        """Public API contract; production-derived narrative omitted."""
        from exitmgr.exit_recovery import reprice_unfilled_protective_exit
        return await reprice_unfilled_protective_exit(self, con_id, **evidence)

    async def update_in_flight_from_fill(
        self,
        con_id: int,
        filled_qty: int,
    ) -> None:
        """Public API contract; production-derived narrative omitted."""
        in_flight = self.state_manager.state.get_in_flight(con_id)
        if in_flight is None:
            print(f"[WARN] Fill event for con_id={con_id} but no in-flight record found")
            return

        in_flight.remaining_qty -= filled_qty
        if in_flight.remaining_qty <= 0:

            self.state_manager.state.remove_in_flight(con_id)
            print(f"[INFO] Position con_id={con_id} fully closed (fill event)")
        else:
            print(f"[INFO] Partial fill for con_id={con_id}, remaining={in_flight.remaining_qty}")


        self.state_manager.save()


def _alert_ambiguous_close(con_id, exc, symbol=None):
    """Public API contract; production-derived narrative omitted."""
    try:
        from exitmgr import alerting
        name = symbol or "con_id=%s" % con_id
        alerting.post(
            ":rotating_light: *EXIT TRANSMISSION/ACK UNKNOWN* -- %s\n"
            "The stop/exit fired, but IBKR did not provide a provable broker acknowledgement "
            "(`%s`). The order may or may not be live.\n"
            "Durable intent retained; the system will NOT blind-retry or claim the order was "
            "placed until broker reconciliation resolves it." % (name, str(exc)[:200]),
            alerting.alerts_channel(),
            label="ambiguous-close",
            fallback_channel=alerting.error_channel(),
        )
    except Exception:
        pass





_LINK_DOWN_MARKERS = ("not connected", "connection", "connect call failed", "broken pipe",
                      "timed out", "timeout", "econnreset", "econnrefused", "errno 61",
                      "errno 54", "errno 32", "socket", "disconnected", "not open",
                      "peer closed", "connection reset")


def _is_link_down(exc) -> bool:
    try:
        return any(m in str(exc).lower() for m in _LINK_DOWN_MARKERS)
    except Exception:
        return False
