#!/usr/bin/env python3
"""Public API contract; production-derived narrative omitted."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time

from exitmgr.ibkr import ComboLeg, Contract, IB, Order
from exitmgr.resident_protection import capability_candidate


PAPER_PORTS = frozenset({4002, 7497})
CONFIRM_PHRASE = "PLACE_CANCEL_RECONNECT_ON_PAPER"

WORKING = frozenset({"PreSubmitted", "Submitted"})
TERMINAL = frozenset({"ApiCancelled", "Cancelled", "Filled", "Inactive"})
CLEANUP_ATTEMPTS = 2
PLAN_BINDING_FIELDS = (
    "schema", "authority", "approved", "mode", "parent_con_id", "symbol",
    "quantity", "sec_type", "action", "order_type", "tif", "outside_rth",
    "entry_debit", "entry_net_price", "stop_pct", "aux_price", "campaign",
    "topology", "code_version", "policy_version",
    "exchange", "currency", "trigger_method", "capability",
)


class PaperQualificationError(RuntimeError):
    pass


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _paper_account(accounts) -> str:
    rows = [str(x).strip() for x in (accounts or ()) if str(x).strip()]
    if len(rows) != 1 or not rows[0].startswith("DU"):
        classes = ["paper" if x.startswith("DU") else "non-paper" for x in rows]
        raise PaperQualificationError(
            "refusing broker mutation: expected exactly one DU paper account, "
            f"observed count={len(rows)} classes={classes}")
    return rows[0]


def _planner_binding_sha(plan: dict) -> str:
    """Public API contract; production-derived narrative omitted."""
    missing = [key for key in PLAN_BINDING_FIELDS if key not in plan]
    if missing:
        raise PaperQualificationError(
            "plan is missing planner binding fields: " + ", ".join(missing))
    payload = {key: plan[key] for key in PLAN_BINDING_FIELDS}
    try:
        return hashlib.sha256(_canonical(payload)).hexdigest()
    except (TypeError, ValueError) as exc:
        raise PaperQualificationError("plan binding payload is not canonical JSON") from exc


def _validated_plan(raw) -> dict:
    if not isinstance(raw, dict):
        raise PaperQualificationError("plan must be a JSON object")
    required = {
        "schema": "resident_protection_plan.v1",
        "authority": "SHADOW_ONLY_NO_BROKER_ACTION",
        "approved": False,
        "mode": "shadow",
        "action": "SELL",
        "order_type": "STP",
        "tif": "GTC",
        "outside_rth": False,
    }
    for key, expected in required.items():
        if raw.get(key) != expected:
            raise PaperQualificationError(
                f"plan {key} must be {expected!r}, got {raw.get(key)!r}")
    if raw.get("sec_type") not in {"OPT", "BAG"}:
        raise PaperQualificationError("only OPT and BAG shadow plans are eligible")
    candidate = capability_candidate(raw["sec_type"])
    if (raw.get("exchange") != "SMART" or raw.get("currency") != "USD"
            or raw.get("trigger_method") != candidate["trigger_method"]
            or raw.get("capability") != candidate):
        raise PaperQualificationError(
            "unsupported or falsely qualified capability/route/trigger plan")
    try:
        qty = int(raw["quantity"])
        aux = float(raw["aux_price"])
        parent = int(raw["parent_con_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PaperQualificationError("plan quantity/aux_price/parent_con_id invalid") from exc
    if qty <= 0 or parent <= 0 or not math.isfinite(aux) or aux <= 0:
        raise PaperQualificationError("plan quantity/aux_price/parent_con_id out of range")
    topology = raw.get("topology")
    expected_len = 2 if raw["sec_type"] == "BAG" else 1
    if not isinstance(topology, list) or len(topology) != expected_len:
        raise PaperQualificationError("plan topology does not match sec_type")
    seen = set()
    for row in topology:
        try:
            cid = int(row["con_id"])
            ratio = int(row["ratio"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PaperQualificationError("invalid topology row") from exc
        if cid <= 0 or cid in seen or ratio != 1:
            raise PaperQualificationError("topology conIds must be unique positive 1:1 legs")
        seen.add(cid)
        if row.get("combo_action") not in {"BUY", "SELL"}:
            raise PaperQualificationError("invalid combo action")
    if parent not in seen:
        raise PaperQualificationError("parent_con_id absent from topology")
    expected_topology = [(parent, "BUY", "SLD")]
    if raw["sec_type"] == "BAG":
        expected_topology.append((int(topology[1]["con_id"]), "SELL", "BOT"))
    if [(int(row["con_id"]), row.get("combo_action"), row.get("close_side"))
            for row in topology] != expected_topology:
        raise PaperQualificationError("unsupported debit closing topology")
    binding = str(raw.get("binding_sha") or "")
    if len(binding) != 64 or any(c not in "0123456789abcdef" for c in binding):
        raise PaperQualificationError("plan binding_sha is not lowercase SHA-256")
    expected_binding = _planner_binding_sha(raw)
    if binding != expected_binding:
        raise PaperQualificationError(
            "plan binding_sha does not match resident-protection planner payload")
    return dict(raw)


def _require_closing_inventory(positions, plan: dict) -> None:
    by_con = {}
    for row in positions or ():
        contract = getattr(row, "contract", None)
        cid = int(getattr(contract, "conId", 0) or 0)
        if cid:
            by_con[cid] = by_con.get(cid, 0.0) + float(getattr(row, "position", 0.0) or 0.0)
    qty = int(plan["quantity"])
    for leg in plan["topology"]:
        cid = int(leg["con_id"])
        held = by_con.get(cid, 0.0)
        action = leg["combo_action"]
        if action == "BUY" and held < qty:
            raise PaperQualificationError(
                f"paper account lacks long closing inventory conId={cid}: {held} < {qty}")
        if action == "SELL" and held > -qty:
            raise PaperQualificationError(
                f"paper account lacks short closing inventory conId={cid}: {held} > {-qty}")


def _contract_and_order(plan: dict):
    qty = int(plan["quantity"])
    if plan["sec_type"] == "OPT":
        contract = Contract(conId=int(plan["parent_con_id"]), secType="OPT",
                            exchange="SMART", currency="USD")
    else:
        contract = Contract(symbol=str(plan["symbol"]), secType="BAG",
                            exchange="SMART", currency="USD")
        contract.comboLegs = [
            ComboLeg(conId=int(row["con_id"]), ratio=int(row["ratio"]),
                     action=str(row["combo_action"]), exchange="SMART")
            for row in plan["topology"]
        ]
    ref = f"paper-qual:resident-v1:{plan['binding_sha'][:20]}"
    order = Order(action="SELL", orderType="STP", totalQuantity=qty,
                  auxPrice=float(plan["aux_price"]), tif="GTC", outsideRth=False,
                  triggerMethod=int(plan["trigger_method"]),
                  orderRef=ref, transmit=True)
    return contract, order


def _order_snapshot(trade) -> dict:
    order = getattr(trade, "order", None)
    order_status = getattr(trade, "orderStatus", None)
    status = getattr(order_status, "status", None)

    def _identity(name):
        raw = getattr(order, name, None)
        if (raw is None or raw == 0) and order_status is not None:
            raw = getattr(order_status, name, raw)
        if raw is None or isinstance(raw, bool):
            return 0
        try:
            return int(raw)
        except (TypeError, ValueError, OverflowError):
            return 0

    def _quantity(name):
        raw = getattr(order_status if name in {"filled", "remaining"} else order,
                      name, None)
        if raw is None or isinstance(raw, bool):
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None

    return {
        "order_id": _identity("orderId"),
        "perm_id": _identity("permId"),
        "order_ref": str(getattr(order, "orderRef", "") or ""),
        "status": str(status or ""),
        "total_quantity": _quantity("totalQuantity"),
        "filled": _quantity("filled"),
        "remaining": _quantity("remaining"),
    }


def _require_plan_fields(trade, plan: dict) -> None:
    """Public API contract; production-derived narrative omitted."""
    contract, order = getattr(trade, "contract", None), getattr(trade, "order", None)
    expected_order = {
        "action": "SELL", "orderType": "STP", "tif": "GTC",
        "outsideRth": False, "triggerMethod": plan["trigger_method"],
    }
    for field, expected in expected_order.items():
        if getattr(order, field, None) != expected:
            raise PaperQualificationError(f"broker order field mismatch: {field}")
    for field, expected in (("auxPrice", plan["aux_price"]),
                            ("totalQuantity", plan["quantity"])):
        try:
            observed = float(getattr(order, field, None))
            matches = math.isfinite(observed) and math.isclose(
                observed, float(expected), rel_tol=0, abs_tol=1e-9)
        except (TypeError, ValueError, OverflowError):
            matches = False
        if not matches:
            raise PaperQualificationError(f"broker order field mismatch: {field}")
    for field, expected in (("secType", plan["sec_type"]),
                            ("exchange", plan["exchange"]), ("currency", plan["currency"])):
        if getattr(contract, field, None) != expected:
            raise PaperQualificationError(f"broker contract field mismatch: {field}")
    if plan["sec_type"] == "OPT":
        if getattr(contract, "conId", None) != plan["parent_con_id"]:
            raise PaperQualificationError("broker contract field mismatch: conId")
    else:
        observed_legs = [(getattr(leg, "conId", None), getattr(leg, "ratio", None),
                          getattr(leg, "action", None), getattr(leg, "exchange", None))
                         for leg in (getattr(contract, "comboLegs", None) or ())]
        expected_legs = [(leg["con_id"], leg["ratio"], leg["combo_action"], plan["exchange"])
                         for leg in plan["topology"]]
        if observed_legs != expected_legs:
            raise PaperQualificationError("broker contract field mismatch: comboLegs")


def _require_identity(snapshot: dict, *, stage: str,
                      expected: tuple[int, int] | None = None) -> tuple[int, int]:
    identity = (int(snapshot.get("order_id") or 0), int(snapshot.get("perm_id") or 0))
    if identity[0] <= 0 or identity[1] <= 0:
        raise PaperQualificationError(
            f"{stage} broker identity is unavailable: orderId/permId={identity}")
    if expected is not None and identity != expected:
        raise PaperQualificationError(
            f"{stage} broker identity changed: expected={expected} observed={identity}")
    return identity


def _require_zero_fill(snapshot: dict, *, stage: str) -> None:
    filled = snapshot.get("filled")
    if filled is None or not math.isclose(float(filled), 0.0, abs_tol=1e-9):
        raise PaperQualificationError(
            f"{stage} cannot prove zero fill: filled={filled!r}")


def _require_full_remaining(snapshot: dict, quantity: int, *, stage: str) -> None:
    _require_zero_fill(snapshot, stage=stage)
    remaining = snapshot.get("remaining")
    total = snapshot.get("total_quantity")
    if (remaining is None or total is None
            or not math.isclose(float(remaining), float(quantity), abs_tol=1e-9)
            or not math.isclose(float(total), float(quantity), abs_tol=1e-9)):
        raise PaperQualificationError(
            f"{stage} cannot prove full remaining quantity: "
            f"expected={quantity} remaining={remaining!r} total={total!r}")


async def _wait_status(trade, wanted, timeout_s: float) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        snap = _order_snapshot(trade)
        if snap["status"] in wanted:
            return snap
        await asyncio.sleep(0.2)
    raise PaperQualificationError(
        f"broker status timeout: last={_order_snapshot(trade)} wanted={sorted(wanted)}")


async def _wait_working_evidence(trade, quantity: int, timeout_s: float) -> dict:
    """Public API contract; production-derived narrative omitted."""
    deadline = time.monotonic() + timeout_s
    last = _order_snapshot(trade)
    last_reason = "no broker update"
    while time.monotonic() < deadline:
        last = _order_snapshot(trade)
        if last["filled"] is not None and float(last["filled"]) > 0:
            raise PaperQualificationError(
                f"placement filled before qualification cancellation: {last}")
        if last["status"] in TERMINAL:
            raise PaperQualificationError(
                f"placement became terminal before qualification cancellation: {last}")
        if last["status"] in WORKING:
            try:
                _require_identity(last, stage="placement")
                _require_full_remaining(last, quantity, stage="placement")
                return last
            except PaperQualificationError as exc:



                last_reason = str(exc)
        await asyncio.sleep(0.2)
    raise PaperQualificationError(
        f"placement evidence timeout: last={last}; reason={last_reason}")


async def _connect(ib, host: str, port: int, client_id: int) -> str:
    await asyncio.wait_for(
        ib.connectAsync(host, port, clientId=client_id, timeout=10, readonly=False), 12)
    return _paper_account(ib.managedAccounts())


def _find_ref(trades, order_ref: str, *, expected_identity: tuple[int, int] | None = None):
    matches = [t for t in (trades or ())
               if str(getattr(getattr(t, "order", None), "orderRef", "") or "") == order_ref]
    if len(matches) != 1:
        raise PaperQualificationError(
            f"expected one broker orderRef={order_ref!r}, observed {len(matches)}")
    if expected_identity is not None:
        _require_identity(_order_snapshot(matches[0]), stage="reconnect discovery",
                          expected=expected_identity)
    return matches[0]


async def _cleanup_after_failure(*, ib, host: str, port: int, client_id: int,
                                 account: str, order_ref: str,
                                 expected_identity: tuple[int, int] | None) -> None:
    """Public API contract; production-derived narrative omitted."""
    if int(port) not in PAPER_PORTS:
        raise PaperQualificationError(
            f"refusing cleanup on non-paper port {port}")
    errors = []
    for attempt in range(1, CLEANUP_ATTEMPTS + 1):
        try:
            if not ib.isConnected():
                observed_account = await _connect(ib, host, port, client_id)
            else:
                observed_account = _paper_account(ib.managedAccounts())
            if observed_account != account:
                raise PaperQualificationError(
                    "paper account identity drifted during cleanup")

            open_orders = await asyncio.wait_for(ib.reqOpenOrdersAsync(), 5)

            def _cleanup_target(trade) -> bool:
                snapshot = _order_snapshot(trade)
                by_ref = snapshot["order_ref"] == order_ref
                by_identity = (expected_identity is not None
                               and (snapshot["order_id"], snapshot["perm_id"])
                               == expected_identity)
                if by_ref:
                    return True
                return bool(by_identity)

            ref_matches = [trade for trade in (open_orders or ()) if _cleanup_target(trade)]




            cancel_errors = []
            for trade in ref_matches:
                try:
                    ib.cancelOrder(trade.order)
                except Exception as exc:
                    cancel_errors.append(f"cancel call: {exc}")
            for trade in ref_matches:
                try:
                    await _wait_status(trade, {"ApiCancelled", "Cancelled"}, 5)
                except Exception as exc:
                    cancel_errors.append(f"cancel status: {exc}")
            if cancel_errors:
                raise PaperQualificationError("; ".join(cancel_errors))

            ib.disconnect()
            observed_account = await _connect(ib, host, port, client_id)
            if observed_account != account:
                raise PaperQualificationError(
                    "paper account identity drifted during cleanup verification")
            remaining = await asyncio.wait_for(ib.reqOpenOrdersAsync(), 5)
            leftovers = [trade for trade in (remaining or ()) if _cleanup_target(trade)]
            if leftovers:
                raise PaperQualificationError(
                    f"cleanup left {len(leftovers)} matching paper order(s) resident")
            return
        except Exception as exc:
            errors.append(f"attempt {attempt}: {exc}")
            try:
                if ib.isConnected():
                    ib.disconnect()
            except Exception:
                pass
    raise PaperQualificationError(
        "paper cleanup could not prove order absence after bounded recovery: "
        + " | ".join(errors))


def _write_receipt(path: Path, receipt: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise PaperQualificationError(f"refusing to overwrite qualification evidence: {path}")
    payload = _canonical(receipt) + b"\n"
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())



        os.link(tmp, path)
        os.unlink(tmp)
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


async def qualify(*, host: str, port: int, client_id: int, plan: dict,
                  receipt_path: Path, ib_factory=IB, timeout_s: float = 20.0) -> dict:
    if int(port) not in PAPER_PORTS:
        raise PaperQualificationError(
            f"refusing non-paper port {port}; allowed={sorted(PAPER_PORTS)}")
    plan = _validated_plan(plan)
    ib = ib_factory()
    placed_ref = None
    placed_identity = None
    placement_attempted = False
    account = None
    cleanup_proven = False
    phases = []
    started = time.time()
    try:
        account = await _connect(ib, host, port, client_id)
        phases.append("PAPER_ACCOUNT_VERIFIED")
        _require_closing_inventory(ib.positions(), plan)
        phases.append("CLOSING_INVENTORY_VERIFIED")
        contract, order = _contract_and_order(plan)
        placed_ref = order.orderRef
        placement_attempted = True
        trade = ib.placeOrder(contract, order)
        placed = await _wait_working_evidence(trade, int(plan["quantity"]), timeout_s)
        placed_identity = _require_identity(placed, stage="placement")
        phases.append("PLACED_WORKING")

        ib.disconnect()
        account2 = await _connect(ib, host, port, client_id)
        if account2 != account:
            raise PaperQualificationError("paper account identity drifted across reconnect")
        reopened = _find_ref(await asyncio.wait_for(ib.reqOpenOrdersAsync(), 10), placed_ref,
                             expected_identity=placed_identity)
        _require_plan_fields(reopened, plan)
        discovered = _order_snapshot(reopened)
        if discovered["status"] not in WORKING:
            raise PaperQualificationError(f"reconnected order is not working: {discovered}")
        _require_full_remaining(
            discovered, int(plan["quantity"]), stage="reconnect discovery")
        phases.append("RECONNECT_DISCOVERED")

        ib.cancelOrder(reopened.order)
        cancelled = await _wait_status(reopened, {"ApiCancelled", "Cancelled"}, timeout_s)
        _require_identity(cancelled, stage="cancellation", expected=placed_identity)
        _require_zero_fill(cancelled, stage="cancellation")
        phases.append("CANCEL_CONFIRMED")

        ib.disconnect()
        account3 = await _connect(ib, host, port, client_id)
        if account3 != account:
            raise PaperQualificationError("paper account identity drifted after cancellation")
        remaining = await asyncio.wait_for(ib.reqOpenOrdersAsync(), 10)
        if any(str(getattr(getattr(t, "order", None), "orderRef", "") or "") == placed_ref
               for t in (remaining or ())):
            raise PaperQualificationError("cancelled paper order survived second reconnect")
        cleanup_proven = True
        phases.append("SECOND_RECONNECT_ABSENT")
        receipt = {
            "schema": "resident_protection_paper_qualification.v1",
            "status": "COMPLETE",
            "authority": "PAPER_EVIDENCE_ONLY_NO_LIVE_AUTHORITY",
            "production_activation_approved": False,
            "production_qualified": False,
            "qualification_scope": "PAPER_API_SOCKET_RECONNECT_AND_CLEANUP_ONLY",
            "api_socket_reconnect_proven": True,
            "reconnect_order_fields_match_plan": True,
            "gateway_shutdown_tested": False,
            "trader_host_outage_tested": False,
            "gateway_outage_residency_proven": False,
            "stop_election_proven": False,
            "native_exchange_residency_proven": False,
            "single_owner_handoff_proven": False,
            "account_class": "DU_PAPER",
            "zero_fill_proven": True,
            "full_remaining_before_cancel_proven": True,
            "plan_sha256": hashlib.sha256(_canonical(plan)).hexdigest(),
            "binding_sha": plan["binding_sha"],
            "sec_type": plan["sec_type"],
            "phases": phases,
            "placed": placed,
            "discovered": discovered,
            "cancelled": cancelled,
            "started_epoch": started,
            "completed_epoch": time.time(),
        }
        _write_receipt(receipt_path, receipt)
        return receipt
    finally:
        cleanup_error = None
        if placement_attempted and placed_ref and account and not cleanup_proven:
            try:
                await _cleanup_after_failure(
                    ib=ib, host=host, port=port, client_id=client_id, account=account,
                    order_ref=placed_ref, expected_identity=placed_identity)
            except Exception as exc:
                cleanup_error = exc
        try:
            if ib.isConnected():
                ib.disconnect()
        finally:
            if cleanup_error is not None:
                raise PaperQualificationError(
                    f"qualification failed with unproven paper cleanup: {cleanup_error}") \
                    from cleanup_error


def _parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4002, choices=sorted(PAPER_PORTS))
    p.add_argument("--client-id", type=int, default=119)
    p.add_argument("--plan", required=True, type=Path)
    p.add_argument("--receipt", required=True, type=Path)
    p.add_argument("--confirm-paper", required=True, choices=(CONFIRM_PHRASE,))
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    plan = json.loads(args.plan.read_text())
    try:
        result = asyncio.run(qualify(host=args.host, port=args.port,
                                     client_id=args.client_id, plan=plan,
                                     receipt_path=args.receipt))
    except Exception as exc:
        print(f"PAPER QUALIFICATION FAILED: {exc}")
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
