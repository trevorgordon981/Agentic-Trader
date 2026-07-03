#!/usr/bin/env python3
"""Apply the exit-manager correctness fixes (run inside ~/exitmgr-app)."""
edits = [
 ("exitmgr/connection.py",
  "        # Use ib's placeOrder which is async in ib_async\n"
  "        placed_order = await self.ib.placeOrderAsync(contract, order)\n"
  "        return placed_order",
  "        # ib_async: placeOrder is SYNCHRONOUS and returns a Trade immediately\n"
  "        trade = self.ib.placeOrder(contract, order)\n"
  "        return trade"),
 ("exitmgr/order.py",
  "            order_id = placed_order.orderId if placed_order and hasattr(placed_order, 'orderId') else 0",
  "            # placeOrder returns a Trade; the IB-assigned id lives on trade.order.orderId\n"
  "            order_id = placed_order.order.orderId if placed_order and getattr(placed_order, 'order', None) is not None else 0"),
 ("exitmgr/order.py",
  "            if in_flight.order_id != 0 and in_flight.remaining_qty >= quantity:\n"
  "                # Already have an in-flight order for this contract",
  "            if in_flight.order_id != 0:\n"
  "                # ANY active in-flight close blocks a new one (no double-close even after a partial fill)"),
 ("exitmgr/manager.py",
  "            if in_flight is not None and in_flight.remaining_qty >= pos_data.quantity:\n"
  "                print(f\"[CYCLE] con_id={con_id} already in-flight (remaining={in_flight.remaining_qty}), skipping\")",
  "            if in_flight is not None and in_flight.order_id != 0:\n"
  "                print(f\"[CYCLE] con_id={con_id} already in-flight (order_id={in_flight.order_id}, remaining={in_flight.remaining_qty}), skipping\")"),
 ("exitmgr/state.py",
  "f\"live order_id={live_order_id}. Cannot reconcile safely.\"",
  "f\"live order_id={live_order_id}. Order ID mismatch - cannot reconcile safely.\""),
 ("exitmgr/state.py",
  "            else:\n"
  "                alerts.append(\n"
  "                    f\"[WARN] con_id={con_id}: order missing but position still open. \"\n"
  "                    f\"Keeping in_flight for manual resolution.\"\n"
  "                )",
  "            else:\n"
  "                # Order vanished (cancelled/expired) but position intact and quantities agree:\n"
  "                # clear the stale in_flight so exits are re-evaluated/re-protected next cycle\n"
  "                # (idempotency vs live open orders still prevents a double-close).\n"
  "                alerts.append(\n"
  "                    f\"[INFO] con_id={con_id}: close order no longer live but position open; \"\n"
  "                    f\"clearing stale in_flight so exits are re-evaluated.\"\n"
  "                )\n"
  "                state.remove_in_flight(con_id)"),
]
for fn, old, new in edits:
    t = open(fn).read()
    head = new.split("\n")[0]
    if old not in t:
        if head in t:
            print("SKIP already-patched:", fn, "::", old[:50].replace("\n", " "))
        else:
            print("!! NO MATCH:", fn, "::", repr(old[:60]))
        continue
    open(fn, "w").write(t.replace(old, new, 1))
    print("patched:", fn, "::", old[:50].replace("\n", " "))

ti = "tests/test_integration.py"
t = open(ti).read()
n = t.count("async test_")
if n:
    open(ti, "w").write(t.replace("async test_", "async def test_"))
    print(f"fixed {n} async-def syntax error(s) in {ti}")
