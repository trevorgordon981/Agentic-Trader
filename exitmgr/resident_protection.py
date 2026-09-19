"""Public API contract; production-derived narrative omitted."""

from __future__ import annotations

import hashlib
import json
import math


TRIGGER_METHODS_SOURCE = (
    "https://www.interactivebrokers.com/docs/tws-api/doc/orders/trigger-methods")


def capability_candidate(sec_type: str) -> dict:
    """Public API contract; production-derived narrative omitted."""
    if sec_type not in {"OPT", "BAG"}:
        raise ResidentProtectionPlanError("unsupported resident-protection security type")
    return {
        "status": "UNQUALIFIED_EXACT_CONTRACT_ROUTE",
        "trigger_method": 4 if sec_type == "BAG" else 1,
        "trigger_method_source": TRIGGER_METHODS_SOURCE,
        "trigger_basis": "IBKR_BID_ASK_NOT_EXECUTABLE_NET_BID",
        "exact_contract_route_qualified": False,
        "gateway_outage_residency_proven": False,
        "stop_election_proven": False,
        "production_qualified": False,
        "oca_overfill_protection_assumed": False,
        "price_increment_verified": False,
        "multiplier_verified": False,
    }


class ResidentProtectionPlanError(RuntimeError):
    pass


def plan_shadow(*, con_id, symbol, quantity, entry_debit, stop_pct, journal,
                campaign, code_version=None, policy_version=None) -> dict:
    """Public API contract; production-derived narrative omitted."""
    try:
        cid = int(con_id)
        qty = int(quantity)
        basis = float(entry_debit)
        stop = float(stop_pct)
    except (TypeError, ValueError) as exc:
        raise ResidentProtectionPlanError("unusable protection inputs") from exc
    if (cid <= 0 or qty <= 0 or not math.isfinite(basis) or basis <= 0
            or not math.isfinite(stop) or not (0 < stop <= 30)):
        raise ResidentProtectionPlanError("protection inputs violate quantity/basis/30% stop")
    entry_net = basis / (100.0 * qty)
    stop_price = round(max(0.01, entry_net * (1.0 - stop / 100.0)), 2)
    topology = [{"con_id": cid, "ratio": 1, "combo_action": "BUY", "close_side": "SLD"}]
    spread = (journal or {}).get("spread") or {}
    short_id = spread.get("short_con_id")
    sec_type = "OPT"
    if short_id is not None:
        short_id = int(short_id)
        if short_id <= 0:
            raise ResidentProtectionPlanError("invalid spread short leg")
        sec_type = "BAG"
        topology.append({"con_id": short_id, "ratio": 1,
                         "combo_action": "SELL", "close_side": "BOT"})
    entry_net = round(entry_net, 6)
    code_version = None if code_version is None else str(code_version)
    policy_version = None if policy_version is None else str(policy_version)
    capability = capability_candidate(sec_type)
    binding = {
        "schema": "resident_protection_plan.v1",
        "authority": "SHADOW_ONLY_NO_BROKER_ACTION", "approved": False,
        "mode": "shadow", "parent_con_id": cid, "symbol": str(symbol).upper(),
        "quantity": qty, "sec_type": sec_type, "action": "SELL",
        "order_type": "STP", "tif": "GTC", "outside_rth": False,
        "entry_debit": basis, "entry_net_price": entry_net,
        "stop_pct": stop, "aux_price": stop_price,
        "campaign": campaign, "topology": topology,
        "code_version": code_version, "policy_version": policy_version,
        "exchange": "SMART", "currency": "USD",
        "trigger_method": capability["trigger_method"], "capability": capability,
    }
    digest = hashlib.sha256(json.dumps(
        binding, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    return {"schema": "resident_protection_plan.v1",
            "authority": "SHADOW_ONLY_NO_BROKER_ACTION",
            "approved": False, "mode": "shadow", "parent_con_id": cid,
            "symbol": str(symbol).upper(), "quantity": qty, "sec_type": sec_type,
            "action": "SELL", "order_type": "STP", "tif": "GTC",
            "outside_rth": False, "aux_price": stop_price,
            "entry_debit": basis, "entry_net_price": entry_net, "stop_pct": stop,
            "topology": topology, "campaign": campaign,
            "order_ref": f"exitmgr-protect-v1:{cid}:{digest[:20]}",
            "binding_sha": digest, "code_version": code_version,
            "policy_version": policy_version,
            "exchange": "SMART", "currency": "USD",
            "trigger_method": capability["trigger_method"], "capability": capability,
            "routing_caveat": "SMART combo legs may execute separately; BAG is not claimed atomic"}


def shadow_handoff_decision(*, resident_status, identity_matches, orders_known,
                            executions_reconciled, inventory_fresh,
                            remaining_long, remaining_short) -> dict:
    """Public API contract; production-derived narrative omitted."""
    result = {"authority": "SHADOW_ONLY_NO_BROKER_ACTION",
              "broker_action_authorized": False, "replacement_quantity": None}
    if identity_matches is not True or orders_known is not True:
        return dict(result, next_step="RECONCILE_UNKNOWN_OWNER")
    if resident_status in {"PendingCancel", "PendingSubmit", "ApiPending"}:
        return dict(result, next_step="WAIT_FOR_BROKER_TERMINAL_STATE")
    if resident_status in {"Submitted", "PreSubmitted"}:
        return dict(result, next_step="REQUEST_OWNER_HANDOFF_NO_SECOND_ORDER")
    if resident_status not in {"Cancelled", "Filled"}:
        return dict(result, next_step="RECONCILE_UNKNOWN_OWNER")
    if executions_reconciled is not True or inventory_fresh is not True:
        return dict(result, next_step="RECONCILE_EXECUTIONS_AND_INVENTORY")
    try:
        if isinstance(remaining_long, bool) or isinstance(remaining_short, bool):
            raise ValueError("boolean quantity")
        long_qty, short_qty = float(remaining_long), float(remaining_short)
        if not (math.isfinite(long_qty) and math.isfinite(short_qty)
                and long_qty.is_integer() and short_qty.is_integer()):
            raise ValueError("unusable quantity")
    except (TypeError, ValueError, OverflowError):
        return dict(result, next_step="RECONCILE_EXECUTIONS_AND_INVENTORY")
    if long_qty == short_qty == 0:
        return dict(result, next_step="FLAT_NO_REPLACEMENT")
    if long_qty <= 0 or short_qty != -long_qty:
        return dict(result, next_step="RECONCILE_UNBALANCED_LEGS")
    return dict(result, next_step="BUILD_RESIDUAL_REPLACEMENT_PLAN",
                replacement_quantity=int(long_qty), protection_gap=True)
