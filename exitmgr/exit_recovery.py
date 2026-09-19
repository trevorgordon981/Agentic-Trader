"""Public API contract; production-derived narrative omitted."""
from copy import deepcopy
from datetime import datetime, timezone
import math
import time

KEY = "_protective_recovery"
MAX_AGE_SECONDS = 15.0
ACTIVE_STATUSES = {"Submitted", "PreSubmitted"}


def _get(obj, name, default=None):
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def _number(value):
    if isinstance(value, bool):
        raise ValueError("boolean is not a broker number")
    result = float(value)
    if not math.isfinite(result) or abs(result) >= 1e100:
        raise ValueError("nonfinite/unset broker number")
    return result


def _integer(value, minimum=0):
    result = _number(value)
    if result != int(result) or result < minimum:
        raise ValueError("invalid broker integer")
    return int(result)


def _utc(value):
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("timestamp has no timezone")
    return dt.astimezone(timezone.utc)


def _age(now, observed):
    age = now - _number(observed)
    if not 0 <= age <= MAX_AGE_SECONDS:
        raise ValueError("broker/quote evidence is stale")


def _identity(inf, order, live, client_id):
    expected = {
        "order_id": _integer(inf.order_id, 1),
        "perm_id": _integer(inf.perm_id, 1),
        "client_id": _integer(inf.client_id),
        "order_ref": inf.order_ref,
    }
    if not isinstance(expected["order_ref"], str) or not expected["order_ref"]:
        raise ValueError("durable order reference missing")
    if _integer(client_id) != expected["client_id"]:
        raise ValueError("order belongs to another broker client; cannot modify")
    for snake, camel in (("order_id", "orderId"), ("perm_id", "permId"),
                         ("client_id", "clientId"), ("order_ref", "orderRef")):
        for observed in (_get(order, camel), _get(live, snake)):
            if snake != "order_ref":
                observed = _integer(observed, 0 if snake == "client_id" else 1)
            if observed != expected[snake]:
                raise ValueError("exact broker identity mismatch: " + snake)
    if _integer(_get(live, "order_count", 1), 1) != 1:
        raise ValueError("multiple live close orders; cannot reprice")
    ids = _get(live, "order_ids", ())
    if ids and list(ids) != [expected["order_id"]]:
        raise ValueError("live order identity set differs")
    return expected


def _validate_trade(inf, trade, live, client_id):
    from exitmgr.order import submitted_close_snapshot
    order = getattr(trade, "order", None)
    contract = getattr(trade, "contract", None)
    status = getattr(trade, "orderStatus", None)
    _identity(inf, order, live, client_id)
    if (_get(status, "status") not in ACTIVE_STATUSES
            or _get(live, "status") not in ACTIVE_STATUSES):
        raise ValueError("order is not positively working; reconcile terminal/pending state")
    if _get(order, "orderType") != "LMT":
        raise ValueError("only existing plain limit orders can be repriced")
    total = _integer(_get(order, "totalQuantity"), 1)
    filled = _integer(_get(status, "filled"))
    remaining = _integer(_get(status, "remaining"), 1)
    if total != filled + remaining:
        raise ValueError("partial-fill quantity evidence is inconsistent")
    if remaining != _integer(_get(live, "remaining"), 1):
        raise ValueError("position book and trade remaining disagree")
    if remaining != _integer(inf.remaining_qty, 1):
        raise ValueError("durable remaining needs reconciliation")
    snapshot = submitted_close_snapshot(contract, order,
        parent_con_id=inf.con_id, combo_qty=total)
    saved = inf.submitted_close or {}
    for key in ("schema", "sec_type", "action", "combo_qty", "legs", "order_type", "tif"):
        if saved.get(key) != snapshot.get(key):
            raise ValueError("submitted close topology changed: " + key)
    if snapshot["sec_type"] not in {"OPT", "FOP", "BAG"}:
        raise ValueError("unsupported close security type")

    if (_get(order, "parentId", 0) or _get(order, "ocaGroup", "")
            or _get(order, "conditions", ())):
        raise ValueError("linked or conditional order requires its own recovery policy")
    old_price = _number(_get(order, "lmtPrice"))
    if old_price <= 0 or not math.isclose(old_price, _number(_get(live, "limit_price")), abs_tol=1e-8):
        raise ValueError("broker book and trade limit price disagree")
    return snapshot, old_price, remaining


def _anchor(snapshot, quotes, monotonic_now, generation):
    """Public API contract; production-derived narrative omitted."""
    cashflow = 0.0
    for leg in snapshot["legs"]:
        quote = (quotes or {}).get(int(leg["con_id"]))
        if quote is None or quote.get("generation") != generation:
            raise ValueError("missing or generation-mismatched leg quote")
        _age(monotonic_now, quote.get("observed_monotonic"))
        bid, ask = _number(quote.get("bid")), _number(quote.get("ask"))
        if not 0 < bid <= ask:
            raise ValueError("nonpositive/crossed leg quote")
        ratio = _integer(leg["ratio"], 1)
        if leg["expected_side"] == "SLD":
            cashflow += ratio * bid
        elif leg["expected_side"] == "BOT":
            cashflow -= ratio * ask
        else:
            raise ValueError("unknown submitted leg action")
    anchor = cashflow if snapshot["action"] == "SELL" else -cashflow
    if not math.isfinite(anchor) or anchor <= 0:
        raise ValueError("no positive executable net close quote")


    return round(anchor, 8)


async def reprice_unfilled_protective_exit(manager, con_id, *, live_order, trade, quotes,
        book_observed_monotonic, book_observed_at, book_generation, quote_generation, quote_healthy,
        quote_connection=None, now=None, monotonic_now=None):
    """Public API contract; production-derived narrative omitted."""
    conn, sm = manager.ib_conn, manager.state_manager
    inf = sm.state.get_in_flight(con_id)
    result = {"con_id": con_id, "outcome": "not_eligible", "urgent": False}
    if inf is None:
        return dict(result, reason="no existing close latch")
    ctx = inf.exit_context or {}
    if (ctx.get("trigger_type") not in {"stop", "trailing_stop"}
            or ctx.get("manual_request") is not False):
        return dict(result, reason="not an automatic protective stop/trail close")
    now = now or datetime.now(timezone.utc)
    stamp = now.astimezone(timezone.utc).isoformat()
    tick = time.monotonic() if monotonic_now is None else monotonic_now
    try:
        age = (now - _utc(inf.placed_at)).total_seconds()
        if age < 30:
            return dict(result, outcome="waiting", reason="initial close grace interval")
        if inf.placement_state != "submitted":
            raise ValueError("original transmission is not confirmed")
        if not conn.is_healthy() or not quote_healthy:
            raise ValueError("broker or quote connection is unhealthy")
        _integer(book_generation)
        _integer(quote_generation)
        if book_generation != getattr(conn, "_connection_generation", None):
            raise ValueError("order book belongs to another connection generation")
        _age(tick, book_observed_monotonic)
        book_time = _utc(book_observed_at)
        if not 0 <= (now - book_time).total_seconds() <= MAX_AGE_SECONDS:
            raise ValueError("order-book request timestamp is stale")
        snapshot, old_price, remaining = _validate_trade(inf, trade, live_order, conn.client_id)
        progress = deepcopy(ctx.get(KEY) or {})
        attempts = _integer(progress.get("attempts", 0))
        result["attempts"] = attempts
        if progress.get("phase") in {"intent", "awaiting_echo", "ambiguous"}:
            target = _number(progress.get("target_price"))


            if (not math.isclose(old_price, target, abs_tol=1e-8)
                    or (book_time - _utc(progress["attempted_at"])).total_seconds() < 2):
                return dict(result, outcome="ambiguous", urgent=True,
                    reason="waiting for exact broker echo of previous modification")
            progress.update(phase="confirmed", confirmed_at=stamp)
            ctx[KEY] = progress
            inf.exit_context = ctx
            inf.order_price = target
            sm.save()
        interval = 30 if attempts < 3 else 60
        if progress.get("attempted_at"):
            elapsed = (now - _utc(progress["attempted_at"])).total_seconds()
            if elapsed < interval:
                return dict(result, outcome="waiting", urgent=attempts >= 3,
                    reason="bounded reprice backoff", remaining=remaining)
        target = _anchor(snapshot, quotes, tick, quote_generation)
        improves = target < old_price - 1e-8 if snapshot["action"] == "SELL" else target > old_price + 1e-8
        if not improves:
            return dict(result, outcome="waiting", urgent=True,
                reason="working limit already crosses fresh quote; broker fill still pending")

        if not await conn.ensure_connected(probe=True, timeout=2.0):
            raise ValueError("final broker probe failed")
        if book_generation != getattr(conn, "_connection_generation", None):
            raise ValueError("connection changed during final probe")
        if quote_connection is not None and (
                not quote_connection.is_healthy()
                or quote_generation != getattr(quote_connection, "_connection_generation", None)):
            raise ValueError("quote connection changed during final probe")
        from exitmgr.order_lock import order_mutation_lock
        with order_mutation_lock():


            snapshot, latest_price, remaining = _validate_trade(inf, trade, live_order, conn.client_id)
            if latest_price != old_price:
                raise ValueError("order price changed during final probe")
            current_tick = time.monotonic() if monotonic_now is None else monotonic_now
            _age(current_tick, book_observed_monotonic)
            _anchor(snapshot, quotes, current_tick, quote_generation)
            modified = deepcopy(trade.order)
            modified.lmtPrice = target
            progress = dict(progress, schema="protective_reprice.v1", phase="intent",
                attempts=attempts + 1, attempted_at=stamp, old_price=old_price,
                target_price=target, remaining=remaining,
                total_quantity=_integer(modified.totalQuantity, 1))
            prior = deepcopy(ctx.get(KEY))
            ctx[KEY] = progress
            inf.exit_context = ctx
            try:
                sm.save()
            except Exception:
                if prior is None:
                    ctx.pop(KEY, None)
                else:
                    ctx[KEY] = prior
                raise
            try:


                conn.ib.placeOrder(trade.contract, modified)
            except Exception as exc:
                progress.update(phase="ambiguous", error=type(exc).__name__)
                try:
                    sm.save()
                except Exception:
                    pass
                return dict(result, outcome="ambiguous", urgent=True,
                    reason="same-order modification outcome unknown; latch retained",
                    attempts=attempts + 1)
            progress["phase"] = "awaiting_echo"
            try:
                sm.save()
            except Exception:
                pass
            return dict(result, outcome="modified", urgent=attempts + 1 >= 3,
                reason="same-order limit repriced; broker echo/fill not yet proven",
                attempts=attempts + 1, limit_price=target, remaining=remaining,
                order_id=inf.order_id, perm_id=inf.perm_id)
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        return dict(result, outcome="blocked", urgent=True, reason=str(exc))
    except Exception as exc:
        return dict(result, outcome="blocked", urgent=True,
            reason="recovery refused: " + type(exc).__name__)
