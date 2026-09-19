"""Public API contract; production-derived narrative omitted."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Iterable, Optional, List

from exitmgr.manager import ExitManager, open_campaign, _campaign_receipt
from exitmgr.manual_exit_queue import binding_sha
from exitmgr.runtime_identity import identity_fields


class ManualExitPreflightError(RuntimeError):
    pass


def _campaign_binding(manager, con_id: int) -> dict:
    campaign = open_campaign(manager._journal_campaigns, con_id)
    if campaign is None:
        raise ManualExitPreflightError(f"parent {con_id} has no open journal campaign")





    receipt = _campaign_receipt(campaign)
    if not receipt:
        raise ManualExitPreflightError(f"parent {con_id} has no valid campaign receipt")
    return receipt


def _journal_identity(row: dict) -> dict:
    return {key: row.get(key) for key in (
        "symbol", "right", "expiry", "strike", "decision_id", "ts")}


def bound_requests(config, positions: Iterable[object],
                   symbol: Optional[str] = None, parent_con_id: Optional[int] = None,
                   runtime_identity=None) -> List[dict]:
    """Public API contract; production-derived narrative omitted."""
    try:
        authority = identity_fields(runtime_identity)
    except Exception as exc:
        raise ManualExitPreflightError(
            f"exact code/policy identity is required before queuing: {exc}") from exc

    manager = ExitManager(config, journal_side_effects=False, state_persist=False)
    all_positions = []
    for position in positions or []:
        contract = getattr(position, "contract", None)
        if contract is None or not getattr(position, "position", 0):
            continue
        all_positions.append(position)
    live_all = {int(position.contract.conId): position for position in all_positions
                if int(getattr(position.contract, "conId", 0) or 0) > 0}
    if parent_con_id is not None:
        try:
            requested_ids = {int(parent_con_id)}
        except (TypeError, ValueError) as exc:
            raise ManualExitPreflightError("parent conId is invalid") from exc
    elif symbol:
        requested_ids = {
            cid for cid, position in live_all.items()
            if str(getattr(position.contract, "symbol", "")).upper() == symbol.upper()
        }
    else:
        requested_ids = set(live_all)
    if parent_con_id is not None and symbol:
        selected = live_all.get(next(iter(requested_ids)))
        selected_symbol = str(getattr(
            getattr(selected, "contract", None), "symbol", "") or "").upper()
        if selected is None or selected_symbol != symbol.upper():
            raise ManualExitPreflightError(
                f"approved symbol/conId binding drift: {symbol.upper()} does not own "
                f"parent {next(iter(requested_ids))}")
    if not requested_ids:
        return []
    covered, records = set(), []
    for con_id, journal in manager._journal_entries.items():
        if con_id not in requested_ids:
            continue
        if symbol and str(journal.get("symbol") or "").upper() != symbol.upper():
            raise ManualExitPreflightError(
                f"journal symbol/conId binding drift for parent {con_id}")
        parent = live_all.get(con_id)
        if parent is None:
            continue
        held = int(getattr(parent, "position", 0) or 0)
        expected = int(journal.get("quantity") or 0)
        if held <= 0 or expected <= 0 or held != expected:
            raise ManualExitPreflightError(
                f"parent {con_id} quantity mismatch: journal={expected}, broker={held}")
        topology = [{"con_id": int(con_id), "position": held, "role": "long"}]
        spread = journal.get("spread") or {}
        short_id = spread.get("short_con_id")
        if short_id is not None:
            short_id = int(short_id)
            short = live_all.get(short_id)
            if short is None:
                raise ManualExitPreflightError(
                    f"spread {con_id}/{short_id} is incomplete in the broker snapshot")
            short_qty = int(getattr(short, "position", 0) or 0)
            if short_qty != -held:
                raise ManualExitPreflightError(
                    f"spread {con_id}/{short_id} quantity mismatch: long={held}, short={short_qty}")
            topology.append({"con_id": short_id, "position": short_qty, "role": "short"})
            covered.add(short_id)
        record = {"request_id": str(uuid.uuid4()),
                  "created_at": datetime.now(timezone.utc).isoformat(),
                  "parent_con_id": int(con_id),
                  "symbol": str(journal.get("symbol") or getattr(parent.contract, "symbol", "")).upper(),
                  "quantity": held,
                  "campaign": _campaign_binding(manager, con_id),
                  "journal_identity": _journal_identity(journal),
                  "topology": topology,
                  "code_version": authority["code_version"],
                  "policy_version": authority["policy_version"],
                  "action": "SELL", "order_type": "MARKET",
                  "sec_type": "BAG" if len(topology) > 1 else "OPT"}
        record["binding_sha"] = binding_sha(record)
        records.append(record)
        covered.add(con_id)
    unsupported = sorted(requested_ids - covered)
    if unsupported:
        raise ManualExitPreflightError(
            "refusing manual exit: positions lack an exact journaled topology: "
            f"{unsupported}")
    return records


def request_matches_live(request: dict, manager, parent_position) -> tuple[bool, str]:
    """Public API contract; production-derived narrative omitted."""
    try:
        con_id = int(request["parent_con_id"])
        if int(parent_position.con_id) != con_id:
            return False, "parent conId drift"
        journal = manager._journal_entries.get(con_id)
        if not journal:
            return False, "journal campaign is no longer open"
        if _campaign_binding(manager, con_id) != request.get("campaign"):
            return False, "journal campaign changed since approval"
        if _journal_identity(journal) != request.get("journal_identity"):
            return False, "journal identity changed since approval"
        authority = dict(getattr(manager, "_runtime_identity_fields", None) or {})
        if (request.get("code_version") != authority.get("code_version")
                or request.get("policy_version") != authority.get("policy_version")):
            return False, "protective owner code/policy changed since approval"
        if request.get("action") != "SELL" or request.get("order_type") != "MARKET":
            return False, "approved close semantics are invalid"
        qty = int(getattr(parent_position, "quantity", 0) or 0)
        approved_qty = int(request["quantity"])
        if qty <= 0 or qty > approved_qty:
            return False, f"parent quantity changed (approved={approved_qty}, live={qty})"
        topology = [{"con_id": con_id, "position": qty, "role": "long"}]
        spread = journal.get("spread") or {}
        short_id = spread.get("short_con_id")
        if short_id is not None:
            short_id = int(short_id)
            short = manager._short_positions.get(short_id)
            short_qty = int(getattr(short, "quantity", 0) or 0) if short is not None else 0
            if short_qty != -qty:
                return False, f"short leg {short_id} changed or is unreadable"
            topology.append({"con_id": short_id, "position": short_qty, "role": "short"})
        approved_topology = request.get("topology")
        if not isinstance(approved_topology, list) or len(topology) != len(approved_topology):
            return False, "broker topology changed since approval"
        approved_by_key = {
            (int(leg["con_id"]), str(leg.get("role") or "")): int(leg["position"])
            for leg in approved_topology
        }
        live_by_key = {
            (int(leg["con_id"]), str(leg.get("role") or "")): int(leg["position"])
            for leg in topology
        }
        if set(approved_by_key) != set(live_by_key):
            return False, "broker topology changed since approval"



        for key, approved_position in approved_by_key.items():
            live_position = live_by_key[key]
            if (approved_position == 0 or live_position == 0
                    or (approved_position > 0) != (live_position > 0)
                    or abs(live_position) > abs(approved_position)
                    or live_position * approved_qty != approved_position * qty):
                return False, "broker topology changed since approval"
        if binding_sha(request) != request.get("binding_sha"):
            return False, "request binding checksum mismatch"
        expected_sec_type = "BAG" if len(topology) > 1 else "OPT"
        if request.get("sec_type") != expected_sec_type:
            return False, "approved close contract type changed"
        return True, ""
    except Exception as exc:
        return False, f"request validation failed: {exc}"


def parent_targets(config, positions: Iterable[object], symbol: Optional[str] = None,
                   parent_con_id: Optional[int] = None, runtime_identity=None):
    """Public API contract; production-derived narrative omitted."""
    return {record["parent_con_id"] for record in bound_requests(
        config, positions, symbol, parent_con_id, runtime_identity)}
