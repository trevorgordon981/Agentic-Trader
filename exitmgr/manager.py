"""Public API contract; production-derived narrative omitted."""

import asyncio
import copy
import hashlib
import os
import json
import math
import signal
import sys
import time
import uuid
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Optional, Dict, List, Set

from exitmgr.config import Config
from exitmgr.connection import IBConnection, PositionData




from exitmgr.order import (OrderManager, _trading_day, commission_from_trade,
                           trade_reported_filled, trade_has_proven_zero_fill,
                           compute_entry_basis, submitted_close_snapshot)
from exitmgr.rules import (evaluate_position, evaluate_short_position, ExitTrigger,
                           days_to_expiry)



from exitmgr import rules as rules_mod
from exitmgr import construction as _construction
from exitmgr.position_manager import assess_positions
from exitmgr import regime as regime_mod
from exitmgr.state import StateManager, atr_stop_basis, MAX_MARK_PATH_ROWS
from exitmgr import trade_capture, dataset_integrity
from exitmgr import event_capture as _evcap
from exitmgr.event_capture import identity_source as _identity_source
from exitmgr.runtime_identity import RuntimeIdentity, RuntimeIdentityError, identity_fields
from exitmgr.campaign_conflicts import (
    CampaignConflictRegistryError,
    campaign_conflict_registry_lock,
    campaign_conflict_registry_path,
    journal_sha256,
)






_STOP_BACKSTOP_PCT = 30.0















_SHORT_STOP_BACKSTOP_PCT = 100.0


class _AtrStopSkip(Exception):
    """Public API contract; production-derived narrative omitted."""


def _cfg_num(obj, name, default):
    """Public API contract; production-derived narrative omitted."""
    v = getattr(obj, name, None)
    if v is None:
        return float(default)
    try:
        f = float(v)
    except (TypeError, ValueError):
        return float(default)
    return float(default) if f != f else f














def _first_present(mapping, *keys):
    """Public API contract; production-derived narrative omitted."""
    for k in keys:
        v = mapping.get(k)
        if v is not None:
            return v
    return None


class _MalformedCloseQty(ValueError):
    """Public API contract; production-derived narrative omitted."""


def _durable_qty(ctx, key, fallback, *, where=""):
    """Public API contract; production-derived narrative omitted."""
    if key not in ctx or ctx.get(key) is None:
        try:
            return int(fallback or 0)
        except (TypeError, ValueError):
            return 0
    raw = ctx.get(key)
    if isinstance(raw, bool):
        raise _MalformedCloseQty(f"durable {key}={raw!r} is not a contract count ({where})")
    try:
        f = float(raw)
    except (TypeError, ValueError):
        raise _MalformedCloseQty(f"durable {key}={raw!r} is not numeric ({where})") from None
    if f != f or f in (float("inf"), float("-inf")) or f != int(f) or int(f) <= 0:
        raise _MalformedCloseQty(
            f"durable {key}={raw!r} is not a whole positive contract count ({where}); "
            "refusing to act on it -- a zero close is not an instruction to close everything")
    return int(f)


def _calendar_days_elapsed(entry_ts, now=None):
    """Public API contract; production-derived narrative omitted."""
    if not entry_ts:
        return None
    try:
        entered = datetime.fromisoformat(str(entry_ts).replace("Z", "+00:00"))
        if entered.tzinfo is None:
            entered = entered.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return round(max(0.0, (current - entered).total_seconds() / 86400.0), 3)
    except (TypeError, ValueError, OverflowError):
        return None


def _enforce_airtight_stop(rules):
    """Public API contract; production-derived narrative omitted."""
    from dataclasses import replace
    try:
        stop = float(rules.stop_pct)
    except (TypeError, ValueError):
        stop = 0.0
    return replace(rules, stop_pct=(min(stop, _STOP_BACKSTOP_PCT)
                                    if stop > 0 else _STOP_BACKSTOP_PCT))



def _usable_px(px):
    """Public API contract; production-derived narrative omitted."""
    if px is None or isinstance(px, bool):
        return None
    try:
        p = float(px)
    except (TypeError, ValueError):
        return None
    return p if p == p else None


def _resolve_entry_basis(journal_entry, pos):
    """Public API contract; production-derived narrative omitted."""
    je = journal_entry or {}
    if not je:
        return (pos.avg_cost * 100 * pos.quantity, pos.quantity, "avg_cost")

    journal_debit = je.get("debit")
    basis_source = "debit"

    _jefd = je.get("entry_fill_debit")
    if _jefd is not None:
        try:
            _f = float(_jefd)
            if _f == _f and _f > 0:
                journal_debit, basis_source = _f, "entry_fill_debit"
        except (TypeError, ValueError):
            pass

    try:
        journal_debit = float(journal_debit)
    except (TypeError, ValueError):
        journal_debit = 0.0
    if not (journal_debit > 0):

        return (pos.avg_cost * 100 * pos.quantity, pos.quantity, "avg_cost")

    quantity_in_journal = je.get("quantity", pos.quantity)
    quantity = min(pos.quantity, quantity_in_journal)


    if quantity_in_journal and quantity_in_journal > 0 and quantity != quantity_in_journal:
        entry_debit = journal_debit * quantity / quantity_in_journal
        basis_source += "+prorated"
    else:
        entry_debit = journal_debit
    return (entry_debit, quantity, basis_source)

































_CAMPAIGN_ADDITIVE_FIELDS = ("debit", "entry_fill_debit", "entry_commission",
                             "collateral_usd", "max_loss_usd", "net_credit_usd")



_CAMPAIGN_FLAT_EVENTS = frozenset({
    "closed_by_tool", "position_closed", "campaign_conflict_position_closed",
})


def _canonical_sha256(value) -> str:
    """Public API contract; production-derived narrative omitted."""
    try:
        return hashlib.sha256(json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    except (TypeError, ValueError):
        return ""


def _valid_campaign_conflict_authority(authority) -> bool:
    """Public API contract; production-derived narrative omitted."""
    required = {
        "schema", "fingerprint", "evidence", "evidence_sha256", "quarantined_row",
        "quarantined_row_sha256", "broker_position", "entry_debit", "stop_pct",
    }
    if not isinstance(authority, dict) or set(authority) != required:
        return False
    evidence = authority.get("evidence")
    raw = authority.get("quarantined_row")
    broker = authority.get("broker_position")
    evidence_payload = dict(evidence) if isinstance(evidence, dict) else {}
    evidence_fingerprint = evidence_payload.pop("fingerprint", None)
    if (authority.get("schema") != "campaign_conflict_emergency_authority.v1"
            or not isinstance(evidence, dict) or not isinstance(raw, dict)
            or not isinstance(broker, dict)
            or authority.get("fingerprint") != evidence.get("fingerprint")
            or evidence_fingerprint != _canonical_sha256(evidence_payload)
            or authority.get("evidence_sha256") != _canonical_sha256(evidence)
            or authority.get("quarantined_row_sha256") != _canonical_sha256(raw)):
        return False
    broker_required = {
        "source", "observed_at", "con_id", "sec_type", "symbol", "right", "expiry",
        "strike", "quantity", "avg_cost_per_share",
    }
    if set(broker) != broker_required or broker.get("source") != "ibkr_reqPositions":
        return False
    try:
        datetime.fromisoformat(str(broker["observed_at"]).replace("Z", "+00:00"))
        con_id = int(broker["con_id"])
        quantity = int(broker["quantity"])
        strike = float(broker["strike"])
        avg_cost = float(broker["avg_cost_per_share"])
        entry_debit = float(authority["entry_debit"])
        stop_pct = float(authority["stop_pct"])
        if (con_id <= 0 or quantity <= 0 or not math.isfinite(strike) or strike <= 0
                or not math.isfinite(avg_cost) or avg_cost <= 0
                or not math.isfinite(entry_debit) or entry_debit <= 0
                or not math.isfinite(stop_pct) or not 0 < stop_pct <= _STOP_BACKSTOP_PCT
                or not math.isclose(entry_debit, avg_cost * 100.0 * quantity,
                                    rel_tol=0.0, abs_tol=0.01)):
            return False
        identity = (str(broker["symbol"] or "").upper(),
                    str(broker["right"] or "").upper(), str(broker["expiry"] or ""),
                    round(strike, 6))
        if (broker["sec_type"] != "OPT" or identity[1] not in {"C", "P"}
                or not identity[0] or not identity[2]
                or con_id != int(evidence.get("con_id") or 0)
                or identity[0] != str(evidence.get("symbol") or "").upper()
                or list(identity) != list(evidence.get("raw_identity") or ())):
            return False
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    return True


def _terminal_tool_marker(row) -> bool:
    """Public API contract; production-derived narrative omitted."""
    return (isinstance(row, dict)
            and row.get("event") == "closed_by_tool"
            and row.get("status") == "Filled"
            and (row.get("broker_flat_confirmed") is True
                 or row.get("topology_complete") is True))


def _valid_campaign_binding(binding) -> bool:
    try:
        if not isinstance(binding, dict) or set(binding) != {
                "campaign_seq", "identity", "first_lot_line", "first_lot_ts"}:
            return False
        if isinstance(binding["campaign_seq"], bool) or int(binding["campaign_seq"]) <= 0:
            return False
        if isinstance(binding["first_lot_line"], bool) or int(binding["first_lot_line"]) <= 0:
            return False
        identity = binding["identity"]
        if not isinstance(identity, list) or len(identity) != 4:
            return False
        if any(value is not None and not isinstance(value, str) for value in identity[:3]):
            return False
        if identity[3] is not None and not math.isfinite(float(identity[3])):
            return False
        ts = binding["first_lot_ts"]
        if ts is not None:
            if not isinstance(ts, str) or not ts.strip():
                return False
            datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return True
    except (TypeError, ValueError, OverflowError):
        return False


def _valid_submitted_close(snapshot) -> bool:
    try:
        required = {"schema", "sec_type", "action", "combo_qty", "legs",
                    "order_type", "tif", "limit_price", "stop_price"}
        if (not isinstance(snapshot, dict) or set(snapshot) != required
                or snapshot.get("schema") != "submitted_close.v1"):
            return False
        qty = snapshot.get("combo_qty")
        action = str(snapshot.get("action") or "").upper()
        sec_type = str(snapshot.get("sec_type") or "").upper()
        legs = snapshot.get("legs")
        if (isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0
                or action not in {"BUY", "SELL"} or sec_type not in {"OPT", "BAG"}
                or not isinstance(snapshot.get("order_type"), str)
                or not isinstance(snapshot.get("tif"), str)):
            return False
        for price_name in ("limit_price", "stop_price"):
            price = snapshot.get(price_name)
            if price is not None and (isinstance(price, bool)
                                      or not math.isfinite(float(price))
                                      or abs(float(price)) >= 1e100):
                return False
        if not isinstance(legs, list) or not legs:
            return False
        if sec_type == "OPT" and len(legs) != 1:
            return False
        if sec_type == "BAG" and len(legs) < 2:
            return False
        seen = set()
        for leg in legs:
            if (not isinstance(leg, dict)
                    or set(leg) != {"con_id", "ratio", "expected_side", "multiplier"}):
                return False
            cid = leg.get("con_id")
            ratio = leg.get("ratio")
            side = str(leg.get("expected_side") or "").upper()
            multiplier = leg.get("multiplier")
            if (any(isinstance(value, bool) or not isinstance(value, int)
                    for value in (cid, ratio, multiplier))
                    or cid <= 0 or cid in seen or ratio <= 0
                    or side not in {"BOT", "SLD"}):
                return False
            if multiplier != 100:
                return False
            seen.add(cid)
        if sec_type == "OPT":
            expected = "BOT" if action == "BUY" else "SLD"
            if legs[0]["expected_side"] != expected or legs[0]["ratio"] != 1:
                return False
        return True
    except (TypeError, ValueError, OverflowError):
        return False


def _valid_fill_evidence_receipt(evidence, snapshot) -> bool:
    """Public API contract; production-derived narrative omitted."""
    try:
        required = {"schema", "source", "sec_type", "action", "combo_qty",
                    "planned_combo_qty", "terminal_status", "net_fill_price", "formula",
                    "executions", "fill_ts", "binding_sha"}
        if (not isinstance(evidence, dict) or set(evidence) != required
                or evidence.get("schema") != "close_fill_evidence.v1"
                or evidence.get("source") != "ibkr_executions"
                or not _valid_submitted_close(snapshot)):
            return False
        payload = dict(evidence)
        expected_sha = str(payload.pop("binding_sha", ""))
        observed_sha = hashlib.sha256(json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        if expected_sha != observed_sha:
            return False
        qty = evidence["combo_qty"]
        planned = evidence["planned_combo_qty"]
        terminal_status = evidence["terminal_status"]
        if (isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0
                or isinstance(planned, bool) or not isinstance(planned, int)
                or planned != snapshot["combo_qty"] or qty > planned
                or terminal_status not in {"Filled", "Cancelled", "ApiCancelled", "Inactive"}
                or (terminal_status == "Filled" and qty != planned)
                or evidence["action"] != snapshot["action"]
                or evidence["sec_type"] != snapshot["sec_type"]):
            return False
        net = float(evidence["net_fill_price"])
        if not math.isfinite(net) or net < 0:
            return False
        if evidence["fill_ts"] is not None:
            datetime.fromisoformat(str(evidence["fill_ts"]).replace("Z", "+00:00"))
        rows = evidence["executions"]
        row_fields = {"exec_id", "con_id", "side", "quantity", "price", "perm_id",
                      "order_id", "client_id", "commission", "time"}
        if not isinstance(rows, list) or not rows:
            return False
        expected_legs = {leg["con_id"]: leg for leg in snapshot["legs"]}
        totals = {con_id: 0 for con_id in expected_legs}
        cash = 0.0
        seen_exec_ids = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != row_fields:
                return False
            exec_id = row["exec_id"]
            con_id = row["con_id"]
            row_qty = row["quantity"]
            if (not isinstance(exec_id, str) or not exec_id.strip()
                    or exec_id in seen_exec_ids
                    or isinstance(con_id, bool) or not isinstance(con_id, int)
                    or con_id not in expected_legs
                    or isinstance(row_qty, bool) or not isinstance(row_qty, int)
                    or row_qty <= 0
                    or row["side"] != expected_legs[con_id]["expected_side"]):
                return False
            seen_exec_ids.add(exec_id)
            price = float(row["price"])
            if not math.isfinite(price) or price < 0:
                return False
            for identity_name in ("perm_id", "order_id"):
                identity = row[identity_name]
                if isinstance(identity, bool) or not isinstance(identity, int) or identity < 0:
                    return False
            client_id = row["client_id"]
            if (client_id is not None and (isinstance(client_id, bool)
                                           or not isinstance(client_id, int)
                                           or client_id < 0)):
                return False
            commission = row["commission"]
            if commission is not None and not math.isfinite(float(commission)):
                return False
            if row["time"] is not None:
                datetime.fromisoformat(str(row["time"]).replace("Z", "+00:00"))
            totals[con_id] += row_qty
            cash += (1.0 if row["side"] == "SLD" else -1.0) * row_qty * price
        if any(total != qty * expected_legs[con_id]["ratio"]
               for con_id, total in totals.items()):
            return False
        recomputed = (cash if evidence["action"] == "SELL" else -cash) / qty
        return math.isclose(max(0.0, recomputed), net, rel_tol=0.0, abs_tol=1e-9)
    except (TypeError, ValueError, OverflowError, KeyError):
        return False


def _terminal_manager_marker(row) -> bool:
    """Public API contract; production-derived narrative omitted."""
    required = {
        "event", "schema", "ts", "contract_id", "symbol", "status", "fill_complete",
        "topology_complete", "broker_flat_confirmed", "broker_flat_evidence",
        "filled_quantity", "fill_key", "order_id", "perm_id", "client_id", "order_ref",
        "submitted_close", "campaign", "fill_evidence", "binding_sha",
    }
    if not (isinstance(row, dict) and set(row) == required
            and row.get("event") == "position_closed"
            and row.get("schema") == "position_closed.v1" and row.get("status") == "Filled"
            and row.get("fill_complete") is True and row.get("topology_complete") is True
            and row.get("broker_flat_confirmed") is True
            and isinstance(row.get("fill_key"), str) and row["fill_key"].strip()
            and isinstance(row.get("symbol"), str) and row["symbol"].strip()
            and _valid_campaign_binding(row.get("campaign"))
            and _valid_submitted_close(row.get("submitted_close"))):
        return False
    try:
        filled = int(row.get("filled_quantity"))
        parent = int(row.get("contract_id"))
        if filled <= 0 or parent <= 0 or filled != int(row["submitted_close"]["combo_qty"]):
            return False
        datetime.fromisoformat(str(row["ts"]).replace("Z", "+00:00"))
        perm_id = int(row.get("perm_id") or 0)
        order_id = int(row.get("order_id") or 0)
        client_id = row.get("client_id")
        order_ref = row.get("order_ref")
        if client_id is not None:
            client_id = int(client_id)
            if client_id < 0:
                return False
        if not (perm_id > 0 or (isinstance(order_ref, str) and order_ref.strip())
                or (order_id > 0 and client_id is not None)):
            return False
        evidence = row["broker_flat_evidence"]
        topology_ids = sorted(int(leg["con_id"])
                              for leg in row["submitted_close"]["legs"])
        if parent not in topology_ids or row["symbol"].upper() != str(
                row["campaign"]["identity"][0] or "").upper():
            return False
        if not (isinstance(evidence, dict)
                and set(evidence) == {"schema", "source", "observed_at", "absent_con_ids"}
                and evidence.get("schema") == "position_flat_evidence.v1"
                and evidence.get("source") == "ibkr_reqPositions"
                and sorted(evidence.get("absent_con_ids") or []) == topology_ids):
            return False
        datetime.fromisoformat(str(evidence["observed_at"]).replace("Z", "+00:00"))
        fill_evidence = row.get("fill_evidence")
        if fill_evidence is not None and not _valid_fill_evidence_receipt(
                fill_evidence, row["submitted_close"]):
            return False
        valid_fill_keys = set()
        if perm_id > 0:
            valid_fill_keys.add(f"perm:{perm_id}:con:{parent}")
        if isinstance(order_ref, str) and order_ref.strip():
            valid_fill_keys.add(f"ref:{order_ref}:con:{parent}")
        if order_id > 0 and client_id is not None:
            valid_fill_keys.add(f"api:{client_id}:order:{order_id}:con:{parent}")
        if row["fill_key"] not in valid_fill_keys:
            return False
    except (TypeError, ValueError, OverflowError, KeyError):
        return False
    expected = str(row.get("binding_sha") or "")
    if len(expected) != 64:
        return False
    payload = dict(row)
    payload.pop("binding_sha", None)
    try:
        observed = hashlib.sha256(json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    except (TypeError, ValueError):
        return False
    return observed == expected


def _terminal_campaign_conflict_marker(row, quarantined_row=None) -> bool:
    """Public API contract; production-derived narrative omitted."""
    required = {
        "event", "schema", "source", "ts", "recorded_at", "contract_id", "symbol",
        "status", "fill_complete", "topology_complete", "broker_flat_confirmed",
        "broker_flat_evidence", "filled_quantity", "fill_key", "order_id", "perm_id",
        "client_id", "order_ref", "submitted_close", "conflict_authority",
        "fill_evidence", "binding_sha",
    }
    if not (isinstance(row, dict) and set(row) == required
            and row.get("event") == "campaign_conflict_position_closed"
            and row.get("schema") == "campaign_conflict_position_closed.v1"
            and row.get("source") == "emergency_hard_stop"
            and row.get("status") == "Filled" and row.get("fill_complete") is True
            and row.get("topology_complete") is True
            and row.get("broker_flat_confirmed") is True
            and isinstance(row.get("fill_key"), str) and row["fill_key"].strip()
            and isinstance(row.get("symbol"), str) and row["symbol"].strip()
            and _valid_campaign_conflict_authority(row.get("conflict_authority"))
            and _valid_submitted_close(row.get("submitted_close"))):
        return False
    authority = row["conflict_authority"]
    broker = authority["broker_position"]
    try:
        datetime.fromisoformat(str(row["ts"]).replace("Z", "+00:00"))
        datetime.fromisoformat(str(row["recorded_at"]).replace("Z", "+00:00"))
        parent = int(row["contract_id"])
        filled = int(row["filled_quantity"])
        order_id = int(row.get("order_id") or 0)
        perm_id = int(row.get("perm_id") or 0)
        client_id = row.get("client_id")
        if client_id is not None:
            client_id = int(client_id)
            if client_id < 0:
                return False
        order_ref = row.get("order_ref")
        snapshot = row["submitted_close"]
        topology_ids = sorted(int(leg["con_id"]) for leg in snapshot["legs"])
        if (parent <= 0 or filled <= 0 or filled != int(snapshot["combo_qty"])
                or filled != int(broker["quantity"]) or parent != int(broker["con_id"])
                or parent not in topology_ids
                or row["symbol"].upper() != str(broker["symbol"]).upper()):
            return False
        if not (perm_id > 0 or (isinstance(order_ref, str) and order_ref.strip())
                or (order_id > 0 and client_id is not None)):
            return False
        evidence = row["broker_flat_evidence"]
        if (not isinstance(evidence, dict)
                or set(evidence) != {"schema", "source", "observed_at", "absent_con_ids"}
                or evidence.get("schema") != "position_flat_evidence.v1"
                or evidence.get("source") != "ibkr_reqPositions"
                or sorted(evidence.get("absent_con_ids") or []) != topology_ids):
            return False
        datetime.fromisoformat(str(evidence["observed_at"]).replace("Z", "+00:00"))
        fill_evidence = row.get("fill_evidence")
        if fill_evidence is not None and not _valid_fill_evidence_receipt(fill_evidence, snapshot):
            return False
        valid_fill_keys = set()
        if perm_id > 0:
            valid_fill_keys.add(f"perm:{perm_id}:con:{parent}")
        if isinstance(order_ref, str) and order_ref.strip():
            valid_fill_keys.add(f"ref:{order_ref}:con:{parent}")
        if order_id > 0 and client_id is not None:
            valid_fill_keys.add(f"api:{client_id}:order:{order_id}:con:{parent}")
        if row["fill_key"] not in valid_fill_keys:
            return False
        if (quarantined_row is not None
                and _canonical_sha256(quarantined_row)
                    != authority["quarantined_row_sha256"]):
            return False
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    expected = str(row.get("binding_sha") or "")
    payload = dict(row)
    payload.pop("binding_sha", None)
    return len(expected) == 64 and _canonical_sha256(payload) == expected


def _terminal_flex_marker(row) -> bool:
    """Public API contract; production-derived narrative omitted."""
    try:
        from exitmgr.flex_close_recovery import digest, validate_stored_receipt
        required = {"event", "schema", "source", "ts", "recorded_at", "contract_id", "symbol",
                    "filled_quantity", "fill_key", "order_ref", "campaign", "close_topology",
                    "fill_evidence", "broker_flat_evidence", "binding_sha"}
        if (not isinstance(row, dict) or set(row) != required
                or row["event"] != "position_closed" or row["schema"] != "position_closed.flex.v1"
                or row["source"] != "ibkr_flex" or not _valid_campaign_binding(row["campaign"])):
            return False
        receipt = row["fill_evidence"]
        validate_stored_receipt(receipt)
        if (row["contract_id"] != receipt["con_id"] or row["filled_quantity"] != receipt["combo_qty"]
                or row["order_ref"] != receipt["close_order_ref"] or row["ts"] != receipt["fill_ts"]
                or row["close_topology"] != receipt["close_topology"]
                or digest(row["campaign"]) != receipt["bindings"]["campaign_sha256"]
                or row["symbol"] != row["campaign"]["identity"][0]
                or row["fill_key"] != _flex_fill_identity(receipt)):
            return False
        flat = row["broker_flat_evidence"]
        if (not isinstance(flat, dict) or set(flat) != {"schema", "source", "observed_at", "absent_con_ids", "no_resting_order_con_ids"}
                or flat["schema"] != "archived_close_broker_view.v1"
                or flat["source"] != "ibkr_reqPositions+reqAllOpenOrders"):
            return False
        legs = sorted(leg["con_id"] for leg in receipt["close_topology"]["legs"])
        if flat["absent_con_ids"] != legs or flat["no_resting_order_con_ids"] != legs:
            return False
        for value in (row["ts"], row["recorded_at"], flat["observed_at"]):
            if datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is None:
                return False
        fill_time, recorded, observed = (datetime.fromisoformat(value.replace("Z", "+00:00"))
            for value in (row["ts"], row["recorded_at"], flat["observed_at"]))
        if not (fill_time <= observed <= recorded and (recorded-observed).total_seconds() <= 5):
            return False
        payload = dict(row); claimed = payload.pop("binding_sha")
        return digest(payload) == claimed
    except (ImportError, KeyError, TypeError, ValueError, OverflowError, AttributeError):
        return False


def _flex_fill_identity(receipt) -> str:
    from exitmgr.flex_close_recovery import digest
    return "flex:" + digest({"account_sha256": receipt["account_sha256"],
        "con_id": receipt["con_id"], "order_ref": receipt["close_order_ref"],
        "execution_ids": sorted(row["exec_id"] for row in receipt["executions"])})


def _campaign_boundary_marker(row) -> bool:
    """Public API contract; production-derived narrative omitted."""
    if not isinstance(row, dict):
        return False
    if row.get("event") == "closed_by_tool":
        return True
    return _terminal_manager_marker(row) or _terminal_flex_marker(row)


def _campaign_identity(row):
    """Public API contract; production-derived narrative omitted."""
    def _s(v):
        return None if v is None else str(v)

    def _f(v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return None if f != f else round(f, 6)

    return (_s(row.get("symbol")), _s(row.get("right")),
            _s(row.get("expiry")), _f(row.get("strike")))


class JournalLot:
    """Public API contract; production-derived narrative omitted."""

    __slots__ = ("con_id", "line_no", "ts", "row")

    def __init__(self, con_id, line_no, row):
        self.con_id = int(con_id)
        self.line_no = int(line_no)
        self.ts = row.get("ts")
        self.row = row

    def get(self, field):
        return self.row.get(field)

    def receipt(self):
        """Public API contract; production-derived narrative omitted."""
        return {"line_no": self.line_no, "ts": self.ts,
                "quantity": self.row.get("quantity"), "debit": self.row.get("debit"),
                "entry_fill_debit": self.row.get("entry_fill_debit")}


class JournalCampaign:
    """Public API contract; production-derived narrative omitted."""

    __slots__ = ("con_id", "campaign_seq", "identity", "lots", "closed", "close_row",
                 "opened_after_flat")

    def __init__(self, con_id, campaign_seq, identity, opened_after_flat=False):
        self.con_id = int(con_id)
        self.campaign_seq = int(campaign_seq)
        self.identity = identity
        self.lots = []
        self.closed = False
        self.close_row = None
        self.opened_after_flat = bool(opened_after_flat)


    def total(self, field):
        """Public API contract; production-derived narrative omitted."""
        acc = 0.0
        for lot in self.lots:
            v = lot.row.get(field)
            if v is None:
                return None
            try:
                f = float(v)
            except (TypeError, ValueError):
                return None
            if f != f:
                return None
            acc += f
        return acc if self.lots else None

    @property
    def quantity(self):
        """Public API contract; production-derived narrative omitted."""
        acc = 0
        for lot in self.lots:
            v = lot.row.get("quantity")
            if v is None:
                return None
            try:
                acc += int(v)
            except (TypeError, ValueError):
                return None
        return acc if self.lots else None

    def as_journal_row(self):
        """Public API contract; production-derived narrative omitted."""
        if not self.lots:
            return {}
        row = dict(self.lots[-1].row)
        if len(self.lots) < 2:
            return row

        q = self.quantity
        if q is None:


            row.pop("quantity", None)
        else:
            row["quantity"] = q
        for f in _CAMPAIGN_ADDITIVE_FIELDS:
            if not any(f in lot.row for lot in self.lots):
                continue
            t = self.total(f)
            if t is None:
                row.pop(f, None)
            else:
                row[f] = t

        if self.lots[0].ts is not None:
            row["ts"] = self.lots[0].ts

        row["campaign_seq"] = self.campaign_seq
        row["campaign_lots"] = [lot.receipt() for lot in self.lots]
        row["campaign_last_lot_ts"] = self.lots[-1].ts
        return row

    def reconcile(self, broker_quantity):
        """Public API contract; production-derived narrative omitted."""
        q = self.quantity
        out = {"con_id": self.con_id, "campaign_seq": self.campaign_seq,
               "lots": len(self.lots), "journal_quantity": q,
               "broker_quantity": broker_quantity, "basis": self.total("debit")}
        if q is None or broker_quantity is None:
            out["state"] = "unknown"
        elif q == broker_quantity:
            out["state"] = "confirmed"
        elif q > broker_quantity:
            out["state"] = "partially_closed"
        else:
            out["state"] = "under_journaled"
        return out


def _campaign_receipt(campaign) -> dict:
    if campaign is None or not campaign.lots:
        return {}
    return {
        "campaign_seq": int(campaign.campaign_seq),
        "identity": list(campaign.identity),
        "first_lot_line": int(campaign.lots[0].line_no),
        "first_lot_ts": campaign.lots[0].ts,
    }


def build_journal_campaigns(rows):
    """Public API contract; production-derived narrative omitted."""
    campaigns = {}



    short_leg_owner = {}




    pending_legacy_flat = {}

    def _open(cid):
        lst = campaigns.get(cid)
        if lst and not lst[-1].closed:
            return lst[-1]
        return None

    def _flatten(cid, row):
        c = _open(cid)
        if row.get("event") == "position_closed":


            if c is None or row.get("campaign") != _campaign_receipt(c):
                return
        if c is not None:
            c.closed, c.close_row = True, row
        parent = short_leg_owner.pop(cid, None)
        if parent is not None:
            pc = _open(parent)
            if pc is not None:
                pc.closed, pc.close_row = True, row
        for scid, p in list(short_leg_owner.items()):
            if p == cid:
                short_leg_owner.pop(scid, None)

    for line_no, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        raw_cid = row.get("contract_id")
        if raw_cid is None:
            continue
        try:
            cid = int(raw_cid)
        except (TypeError, ValueError):
            continue

        event = row.get("event")
        if event:
            if event == "closed_by_tool" and not _terminal_tool_marker(row):
                owner = short_leg_owner.get(cid, cid)
                pending_legacy_flat[int(owner)] = row
            elif _campaign_boundary_marker(row):
                _flatten(cid, row)
            continue

        pending = pending_legacy_flat.pop(cid, None)
        if pending is not None:
            _flatten(cid, pending)

        identity = _campaign_identity(row)
        cur = _open(cid)
        new_after_flat = False
        if cur is not None and cur.identity != identity:

            cur.closed = True
            cur = None
        if cid in short_leg_owner:


            if cur is not None:
                cur.closed = True
            short_leg_owner.pop(cid, None)
            cur = None
        if cur is None:
            prev = campaigns.get(cid) or []
            new_after_flat = bool(prev)
            cur = JournalCampaign(cid, len(prev) + 1, identity, opened_after_flat=new_after_flat)
            campaigns.setdefault(cid, []).append(cur)
        cur.lots.append(JournalLot(cid, line_no, row))

        sp = row.get("spread") or {}
        if sp.get("short_con_id") is not None:
            try:
                short_leg_owner[int(sp["short_con_id"])] = cid
            except (TypeError, ValueError):
                pass

    return campaigns


def open_campaign(campaigns, con_id):
    """Public API contract; production-derived narrative omitted."""
    try:
        lst = (campaigns or {}).get(int(con_id))
    except (TypeError, ValueError):
        return None
    if not lst:
        return None
    last = lst[-1]
    return None if (last.closed or not last.lots) else last





























_CAMPAIGN_REPAIR_TAG = "20260822"


def _norm_ts(value):
    """Public API contract; production-derived narrative omitted."""
    if value is None:
        return None
    s = str(value)
    for cut in ("+", "Z"):
        i = s.find(cut)
        if i > 0:
            s = s[:i]
            break
    return s.strip()


def plan_campaign_repair(journal_rows, exit_rows):
    """Public API contract; production-derived narrative omitted."""
    campaigns = build_journal_campaigns(journal_rows)


    by_lot_ts = {}
    for cid, camps in campaigns.items():
        for camp in camps:
            for lot in camp.lots:
                key = (cid, _norm_ts(lot.ts))
                by_lot_ts.setdefault(key, camp)

    repairs, unmatched, unchanged, findings = [], [], [], []
    for line_no, row in enumerate(exit_rows, start=1):
        if not isinstance(row, dict):
            continue
        cid = row.get("contract_id", row.get("conId"))
        if cid is None:
            continue
        try:
            cid = int(cid)
        except (TypeError, ValueError):
            continue
        camp = by_lot_ts.get((cid, _norm_ts(row.get("entry_ts"))))
        if camp is None:
            unmatched.append({"line_no": line_no, "con_id": cid,
                              "entry_ts": row.get("entry_ts"),
                              "why": "no journal lot with this (contract_id, entry_ts)"})
            continue
        if len(camp.lots) < 2:
            unchanged.append({"line_no": line_no, "con_id": cid,
                              "campaign_seq": camp.campaign_seq, "lots": 1})
            continue

        basis = camp.total("debit")
        qty = camp.quantity
        if basis is None or not (basis > 0) or qty is None:
            findings.append({"line_no": line_no, "con_id": cid,
                             "why": "multi-lot campaign with an UNKNOWN total -- a lot is "
                                    "missing debit or quantity; repair it before migrating"})
            continue
        if row.get("quantity") == qty and row.get("entry_debit") == basis:
            unchanged.append({"line_no": line_no, "con_id": cid,
                              "campaign_seq": camp.campaign_seq, "lots": len(camp.lots)})
            continue

        after = dict(row)
        for key in ("quantity", "entry_debit", "entry_ts", "holding_days",
                    "realized_pnl_pct"):
            if key in row:
                after["%s_precampaign_%s" % (key, _CAMPAIGN_REPAIR_TAG)] = row.get(key)
        after["quantity"] = qty
        after["entry_debit"] = basis
        opened = camp.lots[0].ts
        if opened is not None:
            after["entry_ts"] = opened
            hd = _campaign_holding_days(opened, row.get("close_ts") or row.get("ts"))
            if hd is not None:
                after["holding_days"] = hd
        realized = row.get("realized_pnl")
        if realized is None:
            after["realized_pnl_pct"] = None
            after["realized_unknown_reason"] = row.get("realized_unknown_reason") or "no realized_pnl"
        else:
            after["realized_pnl_pct"] = round(float(realized) / basis * 100.0, 4)

        stale = [k for k in ("proceeds", "exit_price_per_share") if row.get(k) is not None]
        after["campaign_repair_%s" % _CAMPAIGN_REPAIR_TAG] = {
            "reason": ("the close was sized and valued from ONE lot of a %d-lot campaign; "
                       "quantity and basis are restored from the journal's own lots"
                       % len(camp.lots)),
            "campaign_seq": camp.campaign_seq,
            "lots": [lot.receipt() for lot in camp.lots],
            "basis_from_lots": basis,
            "quantity_from_lots": qty,
            "realized_pnl_untouched": realized,
            "realized_pnl_authority": row.get("exit_basis_source") or "local_reconstruction",
            "stale_derived_fields": stale,
        }
        entry = {"line_no": line_no, "con_id": cid, "symbol": row.get("symbol"),
                 "before": {k: row.get(k) for k in
                            ("quantity", "entry_debit", "realized_pnl",
                             "realized_pnl_pct", "entry_ts")},
                 "after": {k: after.get(k) for k in
                           ("quantity", "entry_debit", "realized_pnl",
                            "realized_pnl_pct", "entry_ts")},
                 "row": after}



        if realized is not None and row.get("reason") == "expired":
            gap = round(float(realized) + basis, 4)
            if abs(gap) > 0.01:
                findings.append({
                    "line_no": line_no, "con_id": cid,
                    "why": ("row is `expired` but the broker's realized %.2f is not -basis "
                            "(%.2f); unexplained difference %+.2f. The repair fixes quantity and "
                            "basis and leaves the broker's number alone -- reconcile the gap "
                            "against Flex before treating this trade's P&L as settled."
                            % (float(realized), -basis, gap))})
        repairs.append(entry)

    return {"repairs": repairs, "unmatched": unmatched, "unchanged": unchanged,
            "findings": findings,
            "campaigns_multi_lot": sorted(
                cid for cid, camps in campaigns.items()
                if any(len(c.lots) > 1 for c in camps))}


def _campaign_holding_days(entry_ts, close_ts):
    """Public API contract; production-derived narrative omitted."""
    from datetime import datetime as _dt

    def _p(v):
        if v is None:
            return None
        try:
            d = _dt.fromisoformat(str(v))
        except ValueError:
            return None
        return d.replace(tzinfo=None) if d.tzinfo is None else d.astimezone(timezone.utc).replace(tzinfo=None)

    a, b = _p(entry_ts), _p(close_ts)
    if a is None or b is None:
        return None
    return round((b - a).total_seconds() / 86400.0, 3)


class ExitManager:
    """Public API contract; production-derived narrative omitted."""













    MARK_PATH_CAP = MAX_MARK_PATH_ROWS





    PROTECTIVE_QUOTE_TIMEOUT_S = 8.0
    PROTECTIVE_PRICE_MAX_AGE_S = 45.0



    TRAIL_QUALIFICATION_WARMUP_S = 45.0

    def __init__(self, config: Config,
                 runtime_identity: Optional[RuntimeIdentity] = None,
                 journal_side_effects: bool = True,
                 state_persist: bool = True):
        self.config = config
        self._journal_side_effects = bool(journal_side_effects)
        self._runtime_identity_fields = (
            identity_fields(runtime_identity) if runtime_identity is not None else None)
        self._broker_protection_mode = str(
            getattr(config, "broker_protection_mode", "disabled") or "disabled")
        if self._broker_protection_mode not in {"disabled", "shadow"}:
            raise RuntimeIdentityError(
                "broker_protection_mode=%s is not placement-qualified; only disabled/shadow "
                "may start until paper place/cancel/reconnect and an exact live approval exist"
                % self._broker_protection_mode)





        self._intent_unreadable_observations = {}
        self._INTENT_LATCH_MIN_AGE_S = 300.0

        self._intent_page_last = {}
        self._INTENT_PAGE_THROTTLE_S = 900.0





        self._terminal_history_cache = {}




        self._terminal_history_legacy_cache = {}
        self._terminal_history_covered_keys = set()


        self._protective_quote_snapshot = {}
        self._protective_quote_con_ids: Set[int] = set()
        self._price_stale_cycles = {}
        self._trail_qualification_alert_not_before = (
            time.monotonic() + self.TRAIL_QUALIFICATION_WARMUP_S)
        self._terminal_history_refreshed_at = 0.0
        self._terminal_history_ttl_s = 90.0
        self._terminal_history_refresh_lock = asyncio.Lock()
        self._execution_lookup_next_at = 0.0
        self._completed_lookup_next_at = 0.0
        self._terminal_lookup_complete = False
        self._last_good_broker_view_at = None




        self._atr_refresh_requested: Set[str] = set()
        self._atr_refresh_lock = asyncio.Lock()
        self.ib_conn = IBConnection(
            host=config.ib.host,
            port=config.ib.port,
            client_id=config.ib.client_id,
        )



        self.quote_ib_conn = None
        self.state_manager = StateManager(
            config.state.path, persist=state_persist,
            require_existing=bool(getattr(config, "arm", False)))








        self._mgmt_views = []
        self._mgmt_views_ts = 0.0
        self._mgmt_views_regime = None
        self._mgmt_views_qty = {}
        self._mgmt_cache = None



        self._mgmt_last_attempt = None

        self._trail_state_ignored_logged = set()
        self._remit_refused_logged = set()
        self._mgmt_alerted = self._load_mgmt_alerted()


        self.order_manager = OrderManager(
            self.ib_conn, self.state_manager,


            exit_slippage_floor=getattr(self.config.rules, "exit_slippage_floor", 0.50),
            runtime_identity=runtime_identity,
        )

        self._running = False
        self._shutdown_requested = False



        self._reconcile_ok = True




        self._reconcile_bad_con_ids: Optional[Set[int]] = set()




        self._order_book_unreadable: Optional[str] = None



        self._unjournaled_con_ids: Set[int] = set()








        self._entry_order_view = None


        self._peak_prices: Dict[int, float] = {}









        self._short_positions: Dict[int, PositionData] = {}
        self._stock_positions: Dict[int, PositionData] = {}
        self._credit_con_ids: Set[int] = set()


        self._short_alerted: Set[int] = set()

        self._short_report: List[dict] = []



        self._short_managed: Dict[int, dict] = {}


        self._journal_entries: Dict[int, dict] = {}


        self._journal_campaigns: Dict[int, list] = {}



        self._campaign_conflict_rows: Dict[int, dict] = {}
        self._campaign_conflicts: Dict[int, dict] = {}


        self._journal_warning_signatures: Set[tuple] = set()
        self._load_journal()

    def _note_good_broker_view(self) -> None:
        """Public API contract; production-derived narrative omitted."""
        self._last_good_broker_view_at = datetime.now(timezone.utc).isoformat()

    def protective_clock_status(self) -> dict:
        """Public API contract; production-derived narrative omitted."""
        try:
            unresolved = len(self.state_manager.state.in_flight)
        except Exception:

            unresolved = 1
        return {
            "last_good_broker_view_at": self._last_good_broker_view_at,
            "unresolved_close_count": int(unresolved),
        }

    def _close_campaign_binding(self, con_id: int) -> Optional[dict]:
        """Public API contract; production-derived narrative omitted."""
        campaign = open_campaign(self._journal_campaigns, con_id)
        receipt = _campaign_receipt(campaign)
        return receipt if _valid_campaign_binding(receipt) else None

    def _campaign_conflict_path(self) -> Path:
        return campaign_conflict_registry_path(self.config.journal.path)

    @staticmethod
    def _campaign_conflict_evidence(con_id: int, row: dict, campaigns) -> dict:
        def clean(value):
            if isinstance(value, dict):
                return {str(key): clean(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [clean(item) for item in value]
            if isinstance(value, float) and not math.isfinite(value):
                return None
            if value is None or isinstance(value, (str, int, float, bool)):
                return value
            return str(value)

        history = []
        for campaign in list((campaigns or {}).get(int(con_id)) or []):
            close = campaign.close_row if isinstance(campaign.close_row, dict) else {}
            history.append({
                "campaign": _campaign_receipt(campaign),
                "closed": bool(campaign.closed),
                "close_event": close.get("event"),
                "close_status": close.get("status"),
            })
        evidence = {
            "condition": "ordered_campaign_flat_but_legacy_row_live",
            "con_id": int(con_id),
            "symbol": str(row.get("symbol") or ""),
            "raw_identity": list(_campaign_identity(row)),
            "raw_quantity": row.get("quantity"),
            "raw_ts": row.get("ts"),
            "campaign_history": history,
        }
        evidence = clean(evidence)
        encoded = json.dumps(evidence, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode()
        evidence["fingerprint"] = hashlib.sha256(encoded).hexdigest()
        return evidence

    def _persist_campaign_conflicts(self) -> None:
        """Public API contract; production-derived narrative omitted."""
        if not self._journal_side_effects:
            return
        path = self._campaign_conflict_path()
        try:
            with campaign_conflict_registry_lock(path, exclusive=True):
                digest = journal_sha256(self.config.journal.path)
                try:
                    with open(path) as handle:
                        state = json.load(handle)
                    if (not isinstance(state, dict)
                            or state.get("schema") != "campaign_conflicts.v2"
                            or state.get("journal_sha256") != digest):
                        state = {"schema": "campaign_conflicts.v2",
                                 "journal_sha256": digest, "conditions": {}}
                except (FileNotFoundError, json.JSONDecodeError, OSError):
                    state = {"schema": "campaign_conflicts.v2",
                             "journal_sha256": digest, "conditions": {}}
                conditions = state.setdefault("conditions", {})
                now = datetime.now(timezone.utc).isoformat()
                active = {row["fingerprint"] for row in self._campaign_conflicts.values()}
                for fingerprint, rec in list(conditions.items()):
                    if isinstance(rec, dict) and rec.get("active") and fingerprint not in active:
                        rec["active"] = False
                        rec["resolved_at"] = now
                for evidence in self._campaign_conflicts.values():
                    fingerprint = evidence["fingerprint"]
                    prior = (conditions.get(fingerprint)
                             if isinstance(conditions.get(fingerprint), dict) else {})
                    conditions[fingerprint] = {
                        "active": True,
                        "first_seen": prior.get("first_seen") or now,
                        "last_seen": now,
                        "alerted": bool(prior.get("alerted", False)),
                        "evidence": evidence,
                    }
                state["journal_sha256"] = digest
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = Path(str(path) + f".tmp.{os.getpid()}.{time.time_ns()}")
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.fchmod(fd, 0o600)
                try:
                    with os.fdopen(fd, "w") as handle:
                        json.dump(state, handle, indent=1, sort_keys=True, allow_nan=False)
                        handle.write("\n")
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(tmp, path)
                finally:
                    if tmp.exists():
                        tmp.unlink()
        except Exception as exc:

            print(f"[CAMPAIGN-CONFLICT] receipt persistence failed: {exc}")

    def _post_campaign_conflict_alerts(self) -> None:
        if not self._campaign_conflicts or not self._journal_side_effects:
            return
        path = self._campaign_conflict_path()
        try:
            with campaign_conflict_registry_lock(path, exclusive=False):
                digest = journal_sha256(self.config.journal.path)
                with open(path) as handle:
                    state = json.load(handle)
                if (state.get("schema") != "campaign_conflicts.v2"
                        or state.get("journal_sha256") != digest):
                    raise CampaignConflictRegistryError(
                        "campaign-conflict alert snapshot is stale")
                conditions = state.get("conditions") or {}
        except Exception as exc:
            print(f"[CAMPAIGN-CONFLICT] cannot read durable receipt for alerting: {exc}")
            return
        for cid, evidence in self._campaign_conflicts.items():
            rec = conditions.get(evidence["fingerprint"]) or {}
            if rec.get("alerted"):
                continue
            symbol = evidence.get("symbol") or "?"
            body = (
                ":rotating_light: *Journal campaign authority conflict*\n"
                f"• *{symbol}* con_id={cid} — ordered campaign ledger says flat while the "
                "legacy row says live.\n"
                "_The row is quarantined from thesis, trail, profit-taking, and new/scale-in "
                "authority. Only a broker-bound emergency hard-loss path may act, and only when "
                "basis and topology are independently provable; otherwise protection is "
                "unverifiable and no money mutation occurs._")
            fingerprint = evidence["fingerprint"]
            self._post_unthrottled_alert(
                body, "campaign-journal-conflict", condition_key=fingerprint,
                on_delivery=lambda delivered, fp=fingerprint:
                    self._checkpoint_campaign_conflict_alert(fp, delivered))

    def _checkpoint_campaign_conflict_alert(self, fingerprint: str, delivered: bool) -> None:
        """Public API contract; production-derived narrative omitted."""
        if not delivered:
            return
        path = self._campaign_conflict_path()
        try:
            with campaign_conflict_registry_lock(path, exclusive=True):
                digest = journal_sha256(self.config.journal.path)
                with open(path) as handle:
                    state = json.load(handle)
                if (state.get("schema") != "campaign_conflicts.v2"
                        or state.get("journal_sha256") != digest):
                    return
                conditions = state.get("conditions") or {}
                rec = conditions.get(str(fingerprint))
                if not isinstance(rec, dict) or not rec.get("active"):
                    return
                evidence = rec.get("evidence") or {}
                if evidence.get("fingerprint") != str(fingerprint):
                    return
                rec["alerted"] = True
                rec["alerted_at"] = datetime.now(timezone.utc).isoformat()
                conditions[str(fingerprint)] = rec
                state["conditions"] = conditions
                tmp = Path(str(path) + f".tmp.{os.getpid()}.{time.time_ns()}")
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.fchmod(fd, 0o600)
                try:
                    with os.fdopen(fd, "w") as handle:
                        json.dump(state, handle, indent=1, sort_keys=True, allow_nan=False)
                        handle.write("\n")
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(tmp, path)
                finally:
                    if tmp.exists():
                        tmp.unlink()
        except Exception as exc:

            print(f"[CAMPAIGN-CONFLICT] alert receipt update failed: {exc}")

    def _load_journal(self, *, strict: bool = False) -> None:
        """Public API contract; production-derived narrative omitted."""
        journal_path = Path(self.config.journal.path)
        if not journal_path.exists():
            if strict:
                raise FileNotFoundError("entry reconciliation journal is missing")
            print(f"[WARN] Journal file not found: {self.config.journal.path}")
            return

        self._journal_entries = {}
        self._campaign_conflict_rows = {}
        self._campaign_conflicts = {}
        self._spread_short_legs = {}



        self._credit_con_ids = set()




        _tool_markers: Dict[int, dict] = {}
        _tool_close_arcs: Dict[int, dict] = {}



        _rows: List[dict] = []
        try:
            with open(journal_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if strict and not isinstance(entry, dict):
                            raise ValueError("entry reconciliation journal row is not an object")
                        _rows.append(entry)
                        con_id = entry.get("contract_id")
                        if con_id is not None:
                            _event = entry.get("event")
                            if _event in _CAMPAIGN_FLAT_EVENTS:
                                _is_tool = _event == "closed_by_tool"
                                if _event == "campaign_conflict_position_closed":



                                    _terminal = _terminal_campaign_conflict_marker(
                                        entry, self._journal_entries.get(int(con_id)))
                                else:
                                    _terminal = (_terminal_tool_marker(entry) if _is_tool
                                                 else (_terminal_manager_marker(entry)
                                                       or _terminal_flex_marker(entry)))
                                if not _terminal:
                                    _sig = (str(con_id), str(_event), str(entry.get("status")))
                                    if _sig not in self._journal_warning_signatures:
                                        print(f"[WARN] Ignoring non-terminal close-tool marker for "
                                              f"con_id={con_id}: event={_event!r} "
                                              f"status={entry.get('status')!r}")
                                        self._journal_warning_signatures.add(_sig)
                                    continue



                                _cid = int(con_id)
                                if _is_tool:
                                    _tool_markers[_cid] = entry



                                _arc_je = self._journal_entries.get(_cid)
                                _arc_cid = _cid
                                if _arc_je is None:
                                    _par = self._spread_short_legs.get(_cid)
                                    if _par is not None:
                                        _arc_cid = int(_par)
                                        _arc_je = self._journal_entries.get(_arc_cid)
                                if _is_tool and _arc_je is not None:
                                    _tool_close_arcs.setdefault(_arc_cid, _arc_je)
                                self._journal_entries.pop(_cid, None)
                                self._credit_con_ids.discard(_cid)
                                _parent = self._spread_short_legs.pop(_cid, None)
                                if _parent is not None:
                                    self._journal_entries.pop(int(_parent), None)
                                    self._credit_con_ids.discard(int(_parent))
                                for _scid, _p in list(self._spread_short_legs.items()):
                                    if _p == _cid:
                                        self._spread_short_legs.pop(_scid, None)
                                continue
                            self._journal_entries[int(con_id)] = entry





                            try:
                                _side = str(entry.get("side") or "").lower()
                                _q = entry.get("quantity")
                                if _side == "credit" or (_q is not None and int(_q) < 0):
                                    self._credit_con_ids.add(int(con_id))
                                else:
                                    self._credit_con_ids.discard(int(con_id))
                            except (TypeError, ValueError):
                                pass



                            _sp = entry.get("spread") or {}
                            if _sp.get("short_con_id") is not None:
                                self._spread_short_legs[int(_sp["short_con_id"])] = int(con_id)
                    except json.JSONDecodeError as e:
                        if strict:
                            raise
                        print(f"[WARN] Could not parse journal line: {e}")
        except Exception as e:
            if strict:
                raise
            print(f"[ERROR] Could not load journal: {e}")













        try:
            self._journal_campaigns = build_journal_campaigns(_rows)
        except Exception as e:
            if strict:
                raise
            print(f"[WARN] campaign ledger build failed: {e} -- falling back to last-write-wins")
            self._journal_campaigns = {}




        for _cid in list(self._journal_campaigns):
            _open = open_campaign(self._journal_campaigns, _cid)
            if _open is not None and _open.lots and _cid not in self._journal_entries:
                self._journal_entries[_cid] = _open.as_journal_row()
                _restored = self._journal_entries[_cid]
                try:
                    _side = str(_restored.get("side") or "").lower()
                    _q = _restored.get("quantity")
                    if _side == "credit" or (_q is not None and int(_q) < 0):
                        self._credit_con_ids.add(int(_cid))
                    else:
                        self._credit_con_ids.discard(int(_cid))
                except (TypeError, ValueError):
                    self._credit_con_ids.discard(int(_cid))
                _sp = self._journal_entries[_cid].get("spread") or {}
                if _sp.get("short_con_id") is not None:
                    self._spread_short_legs[int(_sp["short_con_id"])] = int(_cid)






        if self.state_manager.persist:
            try:
                _binding_dirty = False
                _state = self.state_manager.state
                for _cid, _campaigns in self._journal_campaigns.items():
                    _open = open_campaign(self._journal_campaigns, _cid)
                    if _open is None or not _open.lots:
                        continue
                    _binding = _campaign_receipt(_open)
                    _key = str(_cid)
                    _prior = _state.campaign_bindings.get(_key)
                    if _prior is None:
                        _state.campaign_bindings[_key] = _binding
                        _binding_dirty = True
                    elif _prior != _binding:
                        _state.clear_position_tracking(
                            _cid, clear_campaign_binding=False)
                        _state.campaign_bindings[_key] = _binding
                        _binding_dirty = True
                        print(f"[CAMPAIGN] con_id={_cid}: journal campaign changed "
                              f"{_prior!r} -> {_binding!r}; prior tracking cleared once")
                if _binding_dirty:
                    self.state_manager.save()
            except Exception:


                raise
        for _cid in list(self._journal_entries.keys()):
            try:
                _camp = open_campaign(self._journal_campaigns, _cid)
                if _camp is None or not _camp.lots:


                    if _camp is None:
                        _raw = dict(self._journal_entries.pop(_cid))
                        self._campaign_conflict_rows[_cid] = _raw
                        self._campaign_conflicts[_cid] = self._campaign_conflict_evidence(
                            _cid, _raw, self._journal_campaigns)
                        self._credit_con_ids.discard(_cid)
                        for _short, _parent in list(self._spread_short_legs.items()):
                            if int(_short) == int(_cid) or int(_parent) == int(_cid):
                                self._spread_short_legs.pop(_short, None)
                        print(f"[CAMPAIGN-CONFLICT] con_id={_cid}: ledger has no OPEN "
                              "campaign but the legacy pass kept a row; row QUARANTINED")
                    continue
                if len(_camp.lots) > 1:
                    _agg = _camp.as_journal_row()
                    print(f"[INFO] con_id={_cid} ({_agg.get('symbol')}): SCALE-IN campaign "
                          f"#{_camp.campaign_seq} of {len(_camp.lots)} lots -> quantity="
                          f"{_agg.get('quantity')} basis={_agg.get('debit')} "
                          f"(lots: {[l.get('debit') for l in _camp.lots]})")
                    self._journal_entries[_cid] = _agg
            except Exception as _oe:
                if strict:
                    raise
                print(f"[WARN] campaign overlay failed for con_id={_cid}: {_oe} (continuing)")
        self._persist_campaign_conflicts()
        print(f"[INFO] Loaded {len(self._journal_entries)} journal entries")




        if _tool_close_arcs:








            self._journal_load_in_progress = True
            try:
                for _acid, _aje in _tool_close_arcs.items():
                    if self._journal_side_effects:
                        self._emit_tool_close(_acid, _aje, _tool_markers)
            except Exception as e:
                print(f"[WARN] tool-close terminal logging failed: {e} (continuing)")
            finally:
                self._journal_load_in_progress = False

    def _post_exit_alert(self, symbol: str, trigger, client_msg_id: Optional[str] = None) -> bool:
        """Public API contract; production-derived narrative omitted."""
        import os
        channel = getattr(self.config, "alerts_channel", "") or ""
        token = os.environ.get("SLACK_BOT_TOKEN", "")
        if not (channel and token):
            return True
        names = {"profit_target": "take-profit :dart:", "stop": "STOP :octagonal_sign:",
                 "time_stop": "time stop :hourglass:", "trailing_stop": "trailing stop"}
        why = names.get(getattr(trigger, "trigger_type", ""), getattr(trigger, "trigger_type", "exit"))
        pnl = getattr(trigger, "pnl_pct", 0.0)
        emoji = ":green_circle:" if pnl >= 0 else ":red_circle:"
        try:
            from exitmgr import approval
            ts = approval.post_proposal(token, channel,
                f":rotating_light: *EXIT — {symbol}* sold ({why}) {emoji} *P&L {pnl:+.1f}%*\n"
                f"_{getattr(trigger, 'message', '')}_", client_msg_id=client_msg_id)
            return bool(ts)
        except Exception as e:
            print(f"[WARN] exit alert post failed: {e}")
            return False

    def _exits_log_path(self) -> str:
        """Public API contract; production-derived narrative omitted."""
        cfg_path = getattr(getattr(self.config, "exits", None), "path", None)
        if cfg_path:
            return cfg_path
        return os.path.join(os.path.dirname(self.config.journal.path) or ".", "exits.log")

    def _dataset_path(self) -> str:
        """Public API contract; production-derived narrative omitted."""








        env_dir = os.environ.get("EXITMGR_DATASET_DIR")
        if env_dir:
            try:
                os.makedirs(env_dir, exist_ok=True)
            except Exception:
                pass
            return os.path.join(env_dir, "trade_dataset.jsonl")
        cfg_path = getattr(getattr(self.config, "dataset", None), "path", None)
        if cfg_path:
            return cfg_path
        base = os.path.dirname(self.config.journal.path) or "."
        d = os.path.join(base, "data")
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            return os.path.join(base, "trade_dataset.jsonl")
        return os.path.join(d, "trade_dataset.jsonl")

    def _dataset_dir(self) -> str:
        """Public API contract; production-derived narrative omitted."""
        try:
            return os.path.dirname(self._dataset_path()) or "."
        except Exception:
            return "."

    def _log_trade_dataset(self, exit_rec: dict, je: dict, con_id: int) -> bool:
        """Public API contract; production-derived narrative omitted."""
        try:
            st = self.state_manager.state
            k = str(con_id)
            mark_path = list(st.mark_path.get(k, []))
            mfe = st.mfe_pct.get(k)
            mae = st.mae_pct.get(k)
            realized_pct = exit_rec.get("realized_pnl_pct")

            dd_from_peak = round(mfe - realized_pct, 2) if (mfe is not None and realized_pct is not None) else None

            outcome = None
            if realized_pct is not None:
                outcome = "win" if realized_pct > 1.0 else ("loss" if realized_pct < -1.0 else "scratch")

            round_trip = bool(mfe is not None and mfe >= 15.0
                              and realized_pct is not None and realized_pct <= 0)

            tp_pct = je.get("profit_target_pct")
            sl_pct = je.get("stop_pct")
            tp_hit = None
            sl_hit = None
            if realized_pct is not None:
                try:
                    tp_hit = (tp_pct is not None and realized_pct >= float(tp_pct))
                except (TypeError, ValueError):
                    tp_hit = None
                try:
                    sl_hit = (sl_pct is not None and realized_pct <= -abs(float(sl_pct)))
                except (TypeError, ValueError):
                    sl_hit = None
            sp = je.get("spread") or {}



            trigger_mark = exit_rec.get("trigger_mark")
            fill_px = exit_rec.get("avg_fill_price")
            slippage = None
            slippage_pct = None
            try:
                if fill_px is not None and trigger_mark is not None:
                    slippage = round(float(fill_px) - float(trigger_mark), 4)
                    if float(trigger_mark) != 0:
                        slippage_pct = round(slippage / abs(float(trigger_mark)) * 100, 2)
            except (TypeError, ValueError):
                slippage = slippage_pct = None





            decision = None
            try:
                decision = trade_capture.load_decision_context(
                    self._dataset_dir(), decision_id=je.get("decision_id"),
                    con_id=con_id, symbol=je.get("symbol"),
                    strike=je.get("strike"), expiry=je.get("expiry"), right=je.get("right"))
            except Exception as _de:
                print(f"[WARN] decision-context join failed for con_id={con_id}: {_de}")




            review = None
            try:
                review = trade_capture.load_review(
                    self._dataset_dir(), decision_id=je.get("decision_id"),
                    con_id=con_id, symbol=je.get("symbol"),
                    date=(str(je.get("ts") or "")[:10] or None))
            except Exception as _re:
                print(f"[WARN] review join failed for con_id={con_id}: {_re}")
            rec = {
                "schema": "trade_dataset.v2",
                "kind": "trade",
                "decision_id": je.get("decision_id"),
                "model_identity": je.get("model_identity"),
                "model_identity_source": _identity_source(
                    je.get("model_identity"), je.get("model_identity_source")),
                "con_id": con_id,
                "symbol": exit_rec.get("symbol"),

                "decision": decision,

                "entry": {
                    "ts": je.get("ts"),
                    "decision_id": je.get("decision_id"),
                    "model_identity": je.get("model_identity"),
                    "model_identity_source": _identity_source(
                        je.get("model_identity"), je.get("model_identity_source")),
                    "symbol": je.get("symbol"),
                    "right": je.get("right"),
                    "strike": je.get("strike"),
                    "expiry": je.get("expiry"),
                    "structure": exit_rec.get("structure"),
                    "spread": ({"short_con_id": sp.get("short_con_id"),
                                "short_strike": sp.get("short_strike"),
                                "width": sp.get("width")} if sp else None),
                    "quantity": exit_rec.get("quantity"),
                    "debit": exit_rec.get("entry_debit"),
                    "dte_at_entry": je.get("dte_at_entry"),
                    "dte_adjusted": je.get("dte_adjusted"),
                    "profit_target_pct": tp_pct,
                    "stop_pct": sl_pct,
                    "conviction": exit_rec.get("conviction"),
                    "thesis": je.get("thesis"),

                    "entry_delta": je.get("entry_delta"),
                    "entry_gamma": je.get("entry_gamma"),
                    "entry_theta": je.get("entry_theta"),
                    "entry_vega": je.get("entry_vega"),
                    "entry_iv": je.get("entry_iv"),
                    "entry_ivr": je.get("entry_ivr"),
                    "net_delta": je.get("net_delta"),
                    "net_theta": je.get("net_theta"),
                    "net_gamma": je.get("net_gamma"),
                    "net_vega": je.get("net_vega"),

                    "entry_bid": je.get("entry_bid"),
                    "entry_ask": je.get("entry_ask"),
                    "entry_spread_pct": je.get("entry_spread_pct"),
                    "entry_liquidity": je.get("entry_liquidity"),
                    "underlying_price_at_entry": je.get("underlying_price_at_entry"),
                    "entry_order_status": je.get("order_status"),
                    "entry_avg_fill_price": je.get("avg_fill_price"),
                    "entry_fill_ts": je.get("fill_ts"),


                    "entry_commission": je.get("entry_commission"),
                    "entry_fill_debit": je.get("entry_fill_debit"),
                    "entry_slippage": je.get("entry_slippage"),
                    "entry_slippage_pct": je.get("entry_slippage_pct"),
                    "basis_source": je.get("basis_source"),
                },




                "lifecycle": {
                    "mark_path": mark_path,
                    "marks": len(mark_path),
                    "mfe_pct": mfe,
                    "mfe_ts": st.mfe_ts.get(k),
                    "mae_pct": mae,
                    "mae_ts": st.mae_ts.get(k),
                    "peak_price": st.peak_prices.get(k),
                    "drawdown_from_peak_pct": dd_from_peak,
                },

                "close": {
                    "ts": exit_rec.get("close_ts"),



                    "order_id": exit_rec.get("order_id"),
                    "perm_id": exit_rec.get("perm_id"),
                    "client_id": exit_rec.get("client_id"),
                    "order_ref": exit_rec.get("order_ref"),
                    "close_identity": exit_rec.get("close_identity"),
                    "reason": exit_rec.get("reason"),
                    "rule_fired": exit_rec.get("rule_fired"),
                    "exit_reasoning": exit_rec.get("exit_reasoning"),
                    "exit_model_identity": exit_rec.get("exit_model_identity"),
                    "exit_model_identity_source": exit_rec.get("exit_model_identity_source"),
                    "exit_price_per_share": exit_rec.get("exit_price_per_share"),
                    "proceeds": exit_rec.get("proceeds"),
                    "realized_pnl": exit_rec.get("realized_pnl"),


                    "realized_pnl_net": exit_rec.get("realized_pnl_net"),
                    "entry_commission": exit_rec.get("entry_commission"),
                    "exit_commission": exit_rec.get("exit_commission"),
                    "commission_unknown": exit_rec.get("commission_unknown"),
                    "realized_pnl_pct": realized_pct,



                    "mark_estimate_pnl_pct": exit_rec.get("mark_estimate_pnl_pct"),
                    "holding_days": exit_rec.get("holding_days"),
                    "fill_status": exit_rec.get("fill_status"),
                    "avg_fill_price": exit_rec.get("avg_fill_price"),
                    "trigger_mark": trigger_mark,
                    "triggered_at": exit_rec.get("triggered_at"),
                    "trigger_threshold_pct": exit_rec.get("trigger_threshold_pct"),
                    "trigger_threshold_price": exit_rec.get("trigger_threshold_price"),
                    "trigger_threshold_basis": exit_rec.get("trigger_threshold_basis"),
                    "fill_ts": exit_rec.get("fill_ts"),
                    "trigger_to_fill_seconds": exit_rec.get("trigger_to_fill_seconds"),
                    "realized_loss_usd": exit_rec.get("realized_loss_usd"),
                    "realized_loss_pct": exit_rec.get("realized_loss_pct"),
                    "threshold_to_fill_slippage_per_share": exit_rec.get(
                        "threshold_to_fill_slippage_per_share"),
                    "adverse_slippage_beyond_threshold_per_share": exit_rec.get(
                        "adverse_slippage_beyond_threshold_per_share"),
                    "threshold_to_fill_slippage_pct_points": exit_rec.get(
                        "threshold_to_fill_slippage_pct_points"),
                    "adverse_slippage_beyond_threshold_pct_points": exit_rec.get(
                        "adverse_slippage_beyond_threshold_pct_points"),
                    "slippage_per_share": slippage,
                    "slippage_pct": slippage_pct,
                    "underlying_price_at_exit": exit_rec.get("underlying_price"),
                    "exit_iv": exit_rec.get("iv"),
                    "exit_delta": exit_rec.get("delta"),
                    "exit_gamma": exit_rec.get("gamma"),
                    "exit_theta": exit_rec.get("theta"),
                    "exit_vega": exit_rec.get("vega"),
                    "tp_hit": tp_hit,
                    "sl_hit": sl_hit,




                    "partial": bool(exit_rec.get("partial", False)),
                    "close_qty": exit_rec.get("close_qty"),
                    "remaining_qty": exit_rec.get("remaining_qty"),





                    "exit_event": exit_rec.get("exit_event"),
                    "expiry_value_unknown": exit_rec.get("expiry_value_unknown"),
                    "realized_unknown_reason": exit_rec.get("realized_unknown_reason"),
                    "close_client_id": exit_rec.get("close_client_id"),
                    "dte_at_close": exit_rec.get("dte_at_close"),
                },

                "labels": {
                    "outcome": outcome,
                    "win": (outcome == "win") if outcome is not None else None,
                    "round_trip": round_trip,
                },

                "review": review,
            }
            _pnl_canonical = (rec["close"].get("realized_pnl_net") is not None
                              and not bool(rec["close"].get("commission_unknown")))
            _training_canonical = _pnl_canonical and decision is not None
            _reason = ("missing immutable decision join" if decision is None else
                       "net realized P&L unavailable")
            dataset_integrity.mark(
                rec, status=dataset_integrity.CANONICAL,
                training=_training_canonical, pnl=_pnl_canonical, reason=_reason)
            try:
                from exitmgr import event_capture as _evt
                _evt.on_exit(exit_rec, je, con_id, mfe=mfe, mae=mae, mark_path=mark_path)
            except Exception:
                pass
            with open(self._dataset_path(), "a") as f:
                f.write(json.dumps(rec, default=str) + "\n")
                f.flush()
                os.fsync(f.fileno())
            print(f"[TRADE-DS] {exit_rec.get('symbol')} con_id={con_id} "
                  f"mfe={mfe} mae={mae} outcome={outcome} round_trip={round_trip} marks={len(mark_path)}")
            return True
        except Exception as e:
            print(f"[WARN] trade_dataset write failed for con_id={con_id}: {e}")
            return False

    def _recover_conviction(self, je: dict, symbol: str) -> Optional[float]:
        """Public API contract; production-derived narrative omitted."""
        c = je.get("conviction")
        try:
            if c is not None and float(c) >= 0:
                return float(c)
        except (TypeError, ValueError):
            pass

        audit_path = getattr(self.config, "audit_path", "") or os.path.join(
            os.path.dirname(self.config.journal.path) or ".", "audit.jsonl")
        if not (symbol and os.path.exists(audit_path)):
            return None
        long_strike = je.get("strike")
        best = None
        try:
            with open(audit_path) as f:
                for line in f:
                    line = line.strip()
                    if not line or '"daily_rec_posted"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if d.get("event") != "daily_rec_posted" or d.get("underlying") != symbol:
                        continue
                    conv = d.get("conviction")
                    if conv is None:
                        continue
                    order = d.get("order") or ""
                    toks = order.replace("/", " ").replace("C", " ").replace("P", " ")
                    strike_hit = False
                    if long_strike is not None:
                        for tok in toks.split():
                            t = tok.strip("$x@()~,")
                            try:
                                v = float(t)
                            except ValueError:
                                continue
                            if 1e7 <= v <= 9.9e7:
                                continue
                            if abs(v - float(long_strike)) < 1e-6:
                                strike_hit = True
                                break
                    if strike_hit:
                        return float(conv)
                    if best is None:
                        best = float(conv)
        except Exception:
            return best
        return best

    async def _spot_price(self, symbol: str) -> Optional[float]:
        """Public API contract; production-derived narrative omitted."""
        try:
            from exitmgr.ibkr import Stock, underlying_price
            q = await self.ib_conn.ib.qualifyContractsAsync(Stock(symbol, "SMART", "USD"))
            if not q:
                return None
            return await underlying_price(self.ib_conn.ib, q[0])
        except Exception as e:
            print(f"[WARN] spot fetch for {symbol} failed (exit record will omit it): {e}")
            return None

    def _fills_log_path(self) -> str:
        """Public API contract; production-derived narrative omitted."""
        return os.path.join(os.path.dirname(self.config.journal.path) or ".", "fills.log")

    def _join_entry_fill(self, con_id, je) -> dict:
        """Public API contract; production-derived narrative omitted."""
        try:
            afp = je.get("avg_fill_price")
            comm = je.get("entry_commission")
            if afp is not None and comm is not None:
                return {}
            path = self._fills_log_path()
            if not os.path.exists(path):
                return {}
            latest = None
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if d.get("event") != "entry_fill" or d.get("contract_id") != con_id:
                        continue
                    latest = d
            if not latest:
                return {}
            out = {}
            j_afp = afp if afp is not None else latest.get("avg_fill_price")
            j_comm = comm if comm is not None else latest.get("entry_commission")
            if afp is None and j_afp is not None:
                out["avg_fill_price"] = j_afp
            if comm is None and j_comm is not None:
                out["entry_commission"] = j_comm
            if je.get("entry_fill_debit") is None and j_afp is not None:
                efd, eslip, eslip_pct = compute_entry_basis(je.get("debit"), j_afp, je.get("quantity"))
                out["entry_fill_debit"] = efd
                out["entry_slippage"] = eslip
                out["entry_slippage_pct"] = eslip_pct
                out["basis_source"] = ("fill" if efd is not None else je.get("basis_source"))
            return out
        except Exception as e:
            print(f"[WARN] fills.log entry-join failed for con_id={con_id}: {e}")
            return {}

    def _existing_exit_for_order(self, order_id, close_identity=None, con_id=None) -> Optional[dict]:
        """Public API contract; production-derived narrative omitted."""
        if not close_identity and order_id in (None, 0, "0"):
            return None
        try:
            with open(self._exits_log_path()) as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    identity_match = (bool(close_identity)
                                      and rec.get("close_identity") == close_identity)
                    legacy_match = (not str(close_identity or "").startswith("flex:")
                                    and not rec.get("close_identity")
                                    and str(rec.get("order_id")) == str(order_id)
                                    and (con_id is None
                                         or str(rec.get("contract_id")) == str(con_id)))
                    if (identity_match or legacy_match) and rec.get("fill_status") == "Filled":
                        return rec
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[WARN] exit dedupe scan failed for order_id={order_id}: {e}")
        return None

    def _dataset_has_exit_order(self, order_id, close_identity=None, con_id=None) -> bool:
        if not close_identity and order_id in (None, 0, "0"):
            return False
        try:
            with open(self._dataset_path()) as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    close = rec.get("close") or {}
                    identity_match = (bool(close_identity)
                                      and close.get("close_identity") == close_identity)
                    legacy_match = (not str(close_identity or "").startswith("flex:")
                                    and not close.get("close_identity")
                                    and str(close.get("order_id")) == str(order_id)
                                    and (con_id is None
                                         or str(rec.get("con_id", rec.get("contract_id")))
                                         == str(con_id)))
                    if (rec.get("kind") == "trade" and (identity_match or legacy_match)
                            and close.get("fill_status") == "Filled"):
                        return True
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[WARN] dataset dedupe scan failed for order_id={order_id}: {e}")
        return False






    @staticmethod
    def _is_credit_row(je: Optional[dict], quantity: Optional[int] = None) -> bool:
        """Public API contract; production-derived narrative omitted."""
        try:
            if quantity is not None and int(quantity) < 0:
                return True
        except (TypeError, ValueError):
            pass
        if not isinstance(je, dict):
            return False
        if str(je.get("side") or "").lower() == "credit":
            return True
        try:
            q = je.get("quantity")
            return q is not None and int(q) < 0
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _credit_received_usd(je: Optional[dict], contracts: int) -> Optional[float]:
        """Public API contract; production-derived narrative omitted."""
        if not isinstance(je, dict):
            return None
        v = je.get("net_credit_usd")
        try:
            if v is not None and float(v) > 0:
                return round(float(v), 2)
        except (TypeError, ValueError):
            pass

        try:
            coll, ml = je.get("collateral_usd"), je.get("max_loss_usd")
            if coll is not None and ml is not None:
                c = round(float(coll) - float(ml), 2)
                if c > 0:
                    return c
        except (TypeError, ValueError):
            pass
        for k in ("avg_fill_price", "limit", "entry_price"):
            try:
                px = je.get(k)
                if px is not None and float(px) > 0:
                    return round(float(px) * 100 * abs(int(contracts or 1)), 2)
            except (TypeError, ValueError):
                continue
        return None

    @classmethod
    def short_pnl(cls, je: Optional[dict], current_price: Optional[float],
                  contracts: int) -> Dict[str, Optional[float]]:
        """Public API contract; production-derived narrative omitted."""
        out: Dict[str, Optional[float]] = {"pnl_usd": None, "pct_of_credit": None,
                                           "pct_of_max_loss": None, "credit_usd": None,
                                           "cost_to_close_usd": None}
        n = abs(int(contracts or 0)) or 1
        credit = cls._credit_received_usd(je, n)
        out["credit_usd"] = credit
        try:
            px = float(current_price) if current_price is not None else None
            if px is not None and px != px:
                px = None
        except (TypeError, ValueError):
            px = None
        if px is None or px < 0:
            return out
        cost = round(px * 100 * n, 2)
        out["cost_to_close_usd"] = cost
        if credit is None:
            return out
        pnl = round(credit - cost, 2)
        out["pnl_usd"] = pnl
        out["pct_of_credit"] = round(pnl / credit * 100, 2) if credit else None
        try:
            ml = (je or {}).get("max_loss_usd")
            if ml is None:
                ml = (je or {}).get("debit")
            ml = float(ml) if ml is not None else None
            out["pct_of_max_loss"] = round(pnl / ml * 100, 2) if ml else None
        except (TypeError, ValueError):
            out["pct_of_max_loss"] = None
        return out

    async def _fetch_position_book(self):
        """Public API contract; production-derived narrative omitted."""
        try:
            allpos = await self.ib_conn.get_positions(include_short=True, include_stock=True)
        except TypeError:
            allpos = await self.ib_conn.get_positions()
        longs, shorts, stocks = {}, {}, {}
        for cid, pd in (allpos or {}).items():
            try:
                q = int(getattr(pd, "quantity", 0) or 0)
            except (TypeError, ValueError):
                q = None
            st = str(getattr(pd, "sec_type", "OPT") or "OPT").upper()
            if st == "STK":
                stocks[cid] = pd
            elif q is not None and q < 0:
                shorts[cid] = pd
            else:




                longs[cid] = pd
        self._short_positions = shorts
        self._stock_positions = stocks
        return longs, shorts, stocks

    def _log_exit(self, con_id: int, symbol: str, trigger, exit_price_per_share: Optional[float],
                  quantity: int, reason: str, extra: Optional[dict] = None,
                  entry_debit: Optional[float] = None, je: Optional[dict] = None,
                  defer_position_clear: bool = False,
                  historical_fill_ts: Optional[str] = None) -> bool:
        """Public API contract; production-derived narrative omitted."""
        try:




            if je is None:
                je = self._journal_entries.get(con_id) or {}



            je = dict(je or {})
            je.update(self._join_entry_fill(con_id, je))



            _order_id = (extra or {}).get("order_id")
            _close_identity = (extra or {}).get("close_identity")
            _existing = self._existing_exit_for_order(
                _order_id, close_identity=_close_identity, con_id=con_id)
            if _existing is not None:
                if self._dataset_has_exit_order(
                        _order_id, close_identity=_close_identity, con_id=con_id):
                    return True
                return self._log_trade_dataset(_existing, je, con_id)
            if entry_debit is None:





                _efd = je.get("entry_fill_debit")
                _bs = je.get("basis_source")
                entry_debit = _efd if (_efd is not None and (_bs == "fill" or _efd is not None)) else je.get("debit")
            try:
                entry_debit = float(entry_debit) if entry_debit is not None else None
            except (TypeError, ValueError):
                entry_debit = None
            qty = int(quantity) if quantity else int(je.get("quantity", 1) or 1)
            sp = je.get("spread")
            structure = "spread" if sp else "single"
            proceeds = None
            realized_pnl = None
            realized_pct = None

















            _short = self._is_credit_row(je, qty) or bool((extra or {}).get("is_short"))
            if _short:
                _contracts = abs(qty) or 1
                _credit = self._credit_received_usd(je, _contracts)
                if _credit is None:






                    try:
                        _ec = float((extra or {}).get("entry_credit_usd"))
                    except (TypeError, ValueError):
                        _ec = None
                    if _ec is not None and _ec == _ec and _ec > 0:
                        _credit = round(_ec, 2)
                        print(f"[EXIT-LOG] con_id={con_id}: credit basis taken from the durable "
                              f"exit context (${_credit}); the journal row no longer carries it")
                _cost = None
                if exit_price_per_share is not None:
                    try:
                        _cost = round(float(exit_price_per_share) * 100 * _contracts, 2)
                    except (TypeError, ValueError):
                        _cost = None
                if _cost is not None:



                    proceeds = round(-_cost, 2)
                if _cost is not None and _credit is not None:
                    realized_pnl = round(_credit - _cost, 2)




                    realized_pct = (round(realized_pnl / _credit * 100, 2) if _credit else None)
            else:
                if exit_price_per_share is not None:
                    try:
                        proceeds = round(float(exit_price_per_share) * 100 * qty, 2)
                    except (TypeError, ValueError):
                        proceeds = None
                if proceeds is not None and entry_debit is not None:
                    realized_pnl = round(proceeds - entry_debit, 2)
                    realized_pct = round(realized_pnl / entry_debit * 100, 2) if entry_debit else None
            entry_ts = je.get("ts")
            holding_days = None
            now = datetime.now().astimezone()
            if historical_fill_ts is not None:
                if (extra or {}).get("fill_evidence_source") != "ibkr_flex":
                    raise ValueError("historical close timestamp requires explicit Flex provenance")
                historical = datetime.fromisoformat(historical_fill_ts.replace("Z", "+00:00"))
                if historical.tzinfo is None or historical > now:
                    raise ValueError("historical fill timestamp is naive or future")
                now = historical
            if entry_ts:
                try:
                    et = datetime.fromisoformat(str(entry_ts).replace("Z", "+00:00"))
                    if et.tzinfo is None:
                        from datetime import timezone as _tz
                        et = et.replace(tzinfo=_tz.utc)
                    holding_days = round((now - et).total_seconds() / 86400.0, 3)
                except Exception:
                    holding_days = None




            _explicit_authority = bool(extra is not None and (
                "code_version" in extra or "policy_version" in extra))
            if _explicit_authority:
                _code = str((extra or {}).get("code_version") or "")
                _policy = str((extra or {}).get("policy_version") or "")
                if bool(_code) != bool(_policy):
                    raise RuntimeIdentityError(
                        "exit authority must contain both code_version and policy_version")
                _authority = (identity_fields(RuntimeIdentity(_code, _policy))
                              if _code else {"code_version": "", "policy_version": ""})
            else:
                _authority = (dict(self._runtime_identity_fields)
                              if self._runtime_identity_fields is not None
                              else {"code_version": "", "policy_version": ""})

            rec = {
                "ts": now.isoformat(),
                "close_ts": now.isoformat(),
                "decision_id": je.get("decision_id"),
                "model_identity": je.get("model_identity"),
                "model_identity_source": _identity_source(
                    je.get("model_identity"), je.get("model_identity_source")),
                "contract_id": con_id,
                "conId": con_id,
                "symbol": symbol,
                "right": je.get("right"),
                "strike": je.get("strike"),
                "structure": structure,
                "quantity": qty,
                "entry_debit": entry_debit,
                "exit_price_per_share": (round(float(exit_price_per_share), 4)
                                         if exit_price_per_share is not None else None),
                "proceeds": proceeds,
                "realized_pnl": realized_pnl,
                "realizedPNL": realized_pnl,
                "realized_pnl_pct": realized_pct,
                "reason": reason,
                "entry_ts": entry_ts,
                "holding_days": holding_days,
                "conviction": self._recover_conviction(je, symbol),
            }
            rec.update(_authority)
            if _short:



                _contracts = abs(qty) or 1
                _credit = self._credit_received_usd(je, _contracts)
                if _credit is None:
                    try:
                        _ec2 = float((extra or {}).get("entry_credit_usd"))
                        _credit = round(_ec2, 2) if (_ec2 == _ec2 and _ec2 > 0) else None
                    except (TypeError, ValueError):
                        _credit = None
                _cost = (round(-proceeds, 2) if proceeds is not None else None)




                _max_loss = je.get("max_loss_usd")
                if _max_loss is None and not bool((extra or {}).get("is_short")):
                    _max_loss = entry_debit
                try:
                    _max_loss = float(_max_loss) if _max_loss is not None else None
                except (TypeError, ValueError):
                    _max_loss = None
                rec.update({
                    "side": "credit",
                    "structure": je.get("structure") or "cash secured put",
                    "is_short": True,
                    "contracts": _contracts,
                    "entry_credit_usd": _credit,
                    "collateral_usd": je.get("collateral_usd"),
                    "max_loss_usd": _max_loss,
                    "close_cost_usd": _cost,
                    "realized_pct_of_credit": realized_pct,
                    "realized_pct_of_max_loss": (
                        round(realized_pnl / _max_loss * 100, 2)
                        if (realized_pnl is not None and _max_loss) else None),
                    "pnl_basis": "credit_received_minus_close_cost",
                })
            if sp:
                rec["spread"] = {
                    "short_con_id": sp.get("short_con_id"),
                    "short_strike": sp.get("short_strike"),
                    "width": sp.get("width"),
                }
            if extra:
                rec.update({k: v for k, v in extra.items() if k not in rec or rec[k] is None})













            _fs = rec.get("fill_status")
            if _fs is not None and _fs != "Filled":
                rec["mark_estimate_pnl"] = realized_pnl
                rec["mark_estimate_pnl_pct"] = realized_pct
                rec["realized_pnl"] = None
                rec["realizedPNL"] = None
                rec["realized_pnl_pct"] = None
                realized_pnl = None






            def _numok(v):
                try:
                    return v is not None and float(v) == float(v)
                except (TypeError, ValueError):
                    return False
            try:
                _full_q = int(je.get("quantity", qty) or qty)
            except (TypeError, ValueError):
                _full_q = qty
            _entry_comm_raw = je.get("entry_commission")
            _entry_comm = None
            if _numok(_entry_comm_raw):
                _entry_comm = (round(float(_entry_comm_raw) * qty / _full_q, 4)
                               if _full_q else float(_entry_comm_raw))
            _exit_comm_raw = rec.get("exit_commission")
            _exit_comm = float(_exit_comm_raw) if _numok(_exit_comm_raw) else None
            _commission_unknown = (_entry_comm is None) or (_exit_comm is None)
            _final_gross = rec.get("realized_pnl")
            _realized_net = None
            if _final_gross is not None and not _commission_unknown:
                _realized_net = round(float(_final_gross) - _entry_comm - _exit_comm, 2)
            rec["entry_commission"] = _entry_comm
            rec["exit_commission"] = _exit_comm
            rec["commission_unknown"] = _commission_unknown
            rec["realized_pnl_net"] = _realized_net

            rec["entry_fill_debit"] = je.get("entry_fill_debit")
            rec["entry_slippage"] = je.get("entry_slippage")
            rec["entry_slippage_pct"] = je.get("entry_slippage_pct")
            rec["basis_source"] = je.get("basis_source")
            with open(self._exits_log_path(), "a") as f:
                f.write(json.dumps(rec, default=str) + "\n")
                f.flush()
                os.fsync(f.fileno())
            print(f"[EXIT-LOG] {symbol} con_id={con_id} reason={reason} "
                  f"pnl=${realized_pnl if realized_pnl is not None else 'n/a'}")



            _dataset_ok = self._log_trade_dataset(rec, je, con_id)








            _partial = bool((extra or {}).get("partial"))
            _fs_final = rec.get("fill_status")
            _terminal_reason = str(reason) in ("expired", "closed_by_tool", "liquidated")
            if (_dataset_ok and not defer_position_clear and (not _partial)
                    and (_fs_final == "Filled" or _terminal_reason)):
                self._clear_closed_position(con_id)
            return _dataset_ok
        except Exception as e:
            print(f"[WARN] exits.log write failed for con_id={con_id}: {e}")
            return False

    def _log_unfilled_exit(self, con_id: int, symbol: str, trigger, *, fill_status,
                           close_qty, trigger_mark, bid, limit_price, order_id,
                           reason=None, placed_at=None) -> None:
        """Public API contract; production-derived narrative omitted."""
        try:




            if str(fill_status or "").strip().upper() == "FILLED":
                print(f"[UNFILLED-LOG] {symbol} con_id={con_id} order_id={order_id} "
                      "status=Filled; classification withheld while durable finalization "
                      "remains pending")
                return
            if not hasattr(self, "_unfilled_logged"):
                self._unfilled_logged = set()
            key = (con_id, order_id)
            if key in self._unfilled_logged:
                return
            self._unfilled_logged.add(key)
            je = self._journal_entries.get(con_id) or {}
            sp = je.get("spread")
            trade_capture.capture_unfilled(
                self._dataset_dir(), source="exit_manager", symbol=symbol, con_id=con_id,
                fill_status=fill_status, close_qty=close_qty, trigger_mark=trigger_mark,
                bid=bid, limit_price=limit_price, order_id=order_id, placed_at=placed_at,
                reason=reason, rule_fired=getattr(trigger, "trigger_type", None),
                spread=({"short_con_id": sp.get("short_con_id"),
                         "short_strike": sp.get("short_strike"),
                         "width": sp.get("width")} if sp else None))
            print(f"[UNFILLED-LOG] {symbol} con_id={con_id} order_id={order_id} "
                  f"status={fill_status} qty={close_qty} (non-fill recorded; continuing)")
        except Exception as e:
            try:
                print(f"[WARN] unfilled-exit log failed for con_id={con_id}: {e} (continuing)")
            except Exception:
                pass














    def _terminal_dedupe_set(self) -> Set[int]:
        """Public API contract; production-derived narrative omitted."""
        if not hasattr(self, "_terminal_logged"):
            self._terminal_logged: Set[int] = set()
        return self._terminal_logged

    def _closed_con_ids_from_exits_log(self) -> Set[int]:
        """Public API contract; production-derived narrative omitted."""
        ids: Set[int] = set()
        try:








            path = self._exits_log_path()
            if not os.path.exists(path):
                return ids
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if r.get("partial"):
                        continue
                    if r.get("superseded_20260821"):
                        continue
                    cid = _first_present(r, "con_id", "contract_id")
                    if cid is None:
                        continue
                    try:
                        ids.add(int(cid))
                    except (TypeError, ValueError):
                        continue
        except Exception as e:



            print(f"[WARN] exits.log closed-con_id scan failed ({type(e).__name__}: {e}); "
                  "treating as EMPTY -- the expiry guard is DEGRADED this cycle")
            return ids
        return ids

    def _full_close_on_disk(self) -> Set[int]:
        """Public API contract; production-derived narrative omitted."""
        ids: Set[int] = set()
        try:
            dp = self._dataset_path()
            if not os.path.exists(dp):
                return ids
            with open(dp) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if r.get("kind") != "trade":
                        continue
                    close = r.get("close") or {}
                    if close.get("partial"):
                        continue
                    if r.get("unfilled"):
                        continue
                    fill_status = close.get("fill_status")
                    terminal_event = close.get("exit_event") or close.get("reason")
                    confirmed = (
                        fill_status == "Filled"
                        or terminal_event == "expired"

                        or (fill_status is None and close.get("realized_pnl") is not None)
                    )
                    if not confirmed:
                        continue
                    cid = r.get("con_id")
                    if cid is not None:
                        try:
                            ids.add(int(cid))
                        except (TypeError, ValueError):
                            pass
        except Exception as e:
            print(f"[WARN] full-close on-disk scan failed (continuing): {e}")
        return ids

    def _emit_tool_close(self, con_id: int, je: dict, markers: dict) -> None:
        """Public API contract; production-derived narrative omitted."""
        try:
            con_id = int(con_id)
            dedupe = self._terminal_dedupe_set()
            if con_id in dedupe:
                return
            if con_id in self._full_close_on_disk():
                dedupe.add(con_id)
                return
            sp = je.get("spread") or {}
            is_spread = bool(sp.get("short_con_id"))
            long_marker = markers.get(con_id) or {}
            short_marker = {}
            if sp.get("short_con_id") is not None:
                try:
                    short_marker = markers.get(int(sp["short_con_id"])) or {}
                except (TypeError, ValueError):
                    short_marker = {}
            primary = long_marker or short_marker or {}
            tool = primary.get("tool")
            reason = "liquidated" if tool == "liquidate" else "closed_by_tool"
            client_id = primary.get("client_id")

            def _num_ok(v) -> bool:
                try:
                    return v is not None and float(v) == float(v)
                except (TypeError, ValueError):
                    return False

            qty = int(je.get("quantity", 1) or 1)
            entry_debit = je.get("debit")
            long_fill = long_marker.get("avg_fill_price")
            exit_px = None
            fill_known = False
            if long_marker.get("status") == "Filled" and _num_ok(long_fill):
                if is_spread:
                    short_fill = short_marker.get("avg_fill_price")
                    if short_marker.get("status") == "Filled" and _num_ok(short_fill):
                        exit_px = float(long_fill) - float(short_fill)
                        fill_known = True

                else:
                    exit_px = float(long_fill)
                    fill_known = True
            import types as _t
            trig = _t.SimpleNamespace(trigger_type=reason, pnl_pct=0.0,
                                      message=f"position closed by {tool or 'close tool'}")
            extra = {
                "exit_event": reason,
                "rule_fired": reason,
                "exit_reasoning": f"closed by {tool or 'close tool'} (clientId={client_id})",


                "fill_status": ("Filled" if fill_known
                                else (long_marker.get("status") or "closed_by_tool")),
                "avg_fill_price": (exit_px if fill_known else None),
                "close_client_id": client_id,
                "mfe_pct": self.state_manager.state.mfe_pct.get(str(con_id)),
            }
            if not fill_known:
                extra["realized_unknown_reason"] = (
                    "spread_net_fill_unknown" if is_spread else "tool_close_fill_unknown")
            self._log_exit(con_id, je.get("symbol"), trig,
                           exit_price_per_share=(exit_px if fill_known else None),
                           quantity=qty, reason=reason, extra=extra, entry_debit=entry_debit,
                           je=je)
            dedupe.add(con_id)
            print(f"[TERMINAL-CLOSE] {je.get('symbol')} con_id={con_id} reason={reason} "
                  f"fill_known={fill_known} (tool-close recorded; continuing)")
        except Exception as e:
            try:
                print(f"[WARN] tool-close logging failed for con_id={con_id}: {e} (continuing)")
            except Exception:
                pass

    def _emit_expiry_close(self, con_id: int, je: dict, spot: Optional[float] = None) -> None:
        """Public API contract; production-derived narrative omitted."""
        try:
            con_id = int(con_id)
            dedupe = self._terminal_dedupe_set()
            if con_id in dedupe:
                return
            if con_id in self._full_close_on_disk():
                dedupe.add(con_id)
                return
            sp = je.get("spread") or {}
            is_spread = bool(sp.get("short_con_id"))
            right = (je.get("right") or "C").upper()
            strike = je.get("strike")
            qty = int(je.get("quantity", 1) or 1)
            entry_debit = je.get("debit")




            is_short = self._is_credit_row(je, qty)

            def _intrinsic(k):
                try:
                    if k is None or spot is None:
                        return None
                    if right.startswith("C"):
                        return max(0.0, float(spot) - float(k))
                    return max(0.0, float(k) - float(spot))
                except (TypeError, ValueError):
                    return None

            exit_px = None
            value_known = False
            if spot is not None:
                li = _intrinsic(strike)
                if is_spread:
                    si = _intrinsic(sp.get("short_strike"))
                    if li is not None and si is not None:
                        exit_px = li - si
                        value_known = True
                elif li is not None:
                    exit_px = li
                    value_known = True
            import types as _t
            trig = _t.SimpleNamespace(trigger_type="expired", pnl_pct=0.0,
                                      message="option expired")
            if is_short:
                _reasoning = ("expired worthless (OTM) -- full credit kept"
                              if (value_known and not exit_px)
                              else ("expired ITM -> ASSIGNED" if (value_known and exit_px)
                                    else "expired"))
            else:
                _reasoning = ("expired worthless (OTM)"
                              if (value_known and not exit_px) else "expired")
            extra = {
                "exit_event": "expired",
                "rule_fired": "expired",
                "exit_reasoning": _reasoning,
                "dte_at_close": 0,
                "underlying_price": spot,
                "mfe_pct": self.state_manager.state.mfe_pct.get(str(con_id)),
            }
            if is_short:
                extra["side"] = "credit"


                extra["assigned"] = bool(value_known and exit_px)
                if value_known and exit_px:
                    extra["assigned_shares"] = 100 * abs(qty or 1)
                    extra["assigned_strike"] = strike
            if not value_known:

                extra["expiry_value_unknown"] = True
                extra["realized_unknown_reason"] = "expiry_value_unknown"



            self._log_exit(con_id, je.get("symbol"), trig,
                           exit_price_per_share=(exit_px if value_known else None),
                           quantity=qty, reason="expired", extra=extra, entry_debit=entry_debit,
                           je=je)
            dedupe.add(con_id)
            print(f"[TERMINAL-CLOSE] {je.get('symbol')} con_id={con_id} reason=expired "
                  f"value_known={value_known} spot={spot} (expiry recorded; continuing)")
        except Exception as e:
            try:
                print(f"[WARN] expiry logging failed for con_id={con_id}: {e} (continuing)")
            except Exception:
                pass

    async def _process_expiries(self, live_positions, live_shorts=None) -> None:
        """Public API contract; production-derived narrative omitted."""
        try:
            _on_disk = None
            _live_short_ids = set((live_shorts or {}).keys())
            for con_id, je in list(self._journal_entries.items()):
                try:
                    if con_id in live_positions or con_id in _live_short_ids:
                        continue
                    dte = days_to_expiry(je.get("expiry"))
                    if dte is None or dte >= 0:
                        continue
                    if con_id in self._terminal_dedupe_set():
                        continue
                    if _on_disk is None:



                        _on_disk = self._full_close_on_disk() | self._closed_con_ids_from_exits_log()
                    if con_id in _on_disk:

                        self._terminal_dedupe_set().add(con_id)
                        continue
                    spot = None
                    try:
                        spot = await self._spot_price(je.get("symbol"))
                    except Exception:
                        spot = None
                    self._emit_expiry_close(con_id, je, spot=spot)
                except Exception as _ie:
                    print(f"[WARN] expiry check failed for con_id={con_id}: {_ie} (continuing)")
        except Exception as e:
            print(f"[WARN] expiry processing errored (continuing): {e}")


    def _short_marks(self) -> Dict[int, float]:
        """Public API contract; production-derived narrative omitted."""


        if bool(getattr(self.config, "arm", False)):
            return self._portfolio_marks()
        out: Dict[int, float] = {}
        try:
            for p in (self.ib_conn.ib.portfolio() or []):
                try:
                    cid = int(p.contract.conId)
                    px = float(p.marketPrice)
                except (TypeError, ValueError, AttributeError):
                    continue
                if px == px and px >= 0:
                    out[cid] = px
        except Exception:
            return out
        return out

    def _short_entry_credit(self, con_id: int, je: Optional[dict],
                            contracts: int) -> Optional[float]:
        """Public API contract; production-derived narrative omitted."""
        credit = self._credit_received_usd(je, contracts)
        try:
            credit = float(credit) if credit is not None else None
        except (TypeError, ValueError):
            credit = None
        if credit is None or not (credit > 0):
            print(f"[SHORT] con_id={con_id}: NO usable entry-credit basis "
                  f"(net_credit_usd={((je or {}).get('net_credit_usd'))!r}, "
                  f"journaled={bool(je)}); refusing to evaluate any stop or target. This position "
                  "is UNMANAGED -- it is NOT protected. (A credit row's `debit` field is MAX LOSS, "
                  "not the credit, and is deliberately never used as the basis.)")
            return None
        return credit

    def _short_exit_rules(self, je: Optional[dict]):
        """Public API contract; production-derived narrative omitted."""
        from dataclasses import replace
        base = self.config.rules
        stop = None
        for src in ((je or {}).get("short_stop_pct"),
                    getattr(base, "short_stop_pct", None)):
            try:
                v = float(src) if src is not None else None
            except (TypeError, ValueError):
                v = None
            if v is not None and v > 0:
                stop = v
                break
        if stop is None:
            stop = _SHORT_STOP_BACKSTOP_PCT
        return replace(base, profit_target_pct=None, stop_pct=stop,
                       trailing=replace(base.trailing, enabled=False))

    def _short_exit_readiness(self, con_id: int, je: Optional[dict], contracts: int,
                              mark: Optional[float]) -> tuple:
        """Public API contract; production-derived narrative omitted."""
        if int(con_id) in (getattr(self, "_spread_short_legs", {}) or {}):
            return False, "spread_short_leg_closes_with_its_long"
        if self._short_entry_credit(con_id, je, contracts) is None:
            return False, "no_entry_credit_basis"
        try:
            ok_mark = mark is not None and float(mark) == float(mark) and float(mark) >= 0
        except (TypeError, ValueError):
            ok_mark = False
        if not ok_mark:
            return False, "no_mark_this_cycle"
        return True, None

    async def _report_short_positions(self) -> List[dict]:
        """Public API contract; production-derived narrative omitted."""
        rows: List[dict] = []
        try:
            shorts = dict(self._short_positions or {})
            if not shorts:
                self._short_report = []
                return []
            marks = self._short_marks()
            for cid, pd in sorted(shorts.items()):
                je = self._journal_entries.get(int(cid)) or {}
                contracts = abs(int(getattr(pd, "quantity", 0) or 0)) or 1
                px = marks.get(int(cid))
                pnl = self.short_pnl(je, px, contracts)
                strike = je.get("strike")
                if strike is None:
                    strike = getattr(pd, "strike", 0.0) or None
                collateral = je.get("collateral_usd")
                if collateral is None and strike:
                    try:
                        collateral = round(float(strike) * 100 * contracts, 2)
                    except (TypeError, ValueError):
                        collateral = None
                row = {
                    "con_id": int(cid),
                    "symbol": getattr(pd, "symbol", "") or je.get("symbol") or "",
                    "right": getattr(pd, "right", "") or je.get("right") or "",


                    "quantity": int(getattr(pd, "quantity", 0) or 0),
                    "contracts": contracts,
                    "strike": strike,
                    "expiry": getattr(pd, "expiry", "") or je.get("expiry") or "",
                    "dte": days_to_expiry(getattr(pd, "expiry", "") or je.get("expiry")),
                    "mark": px,
                    "collateral_usd": collateral,
                    "credit_usd": pnl.get("credit_usd"),
                    "cost_to_close_usd": pnl.get("cost_to_close_usd"),
                    "unrealized_pnl_usd": pnl.get("pnl_usd"),
                    "pct_of_credit": pnl.get("pct_of_credit"),
                    "journaled": bool(je),
                }
                _managed, _why = self._short_exit_readiness(int(cid), je, contracts, px)
                row["managed"] = _managed
                row["unmanaged_reason"] = _why
                rows.append(row)
                _p = row["unrealized_pnl_usd"]
                _pc = row["pct_of_credit"]
                _p_s = "n/a" if _p is None else "{:+.2f}".format(_p)
                _pc_s = "n/a" if _pc is None else "{:+.1f}%".format(_pc)
                print("[SHORT] {} {}x {}{} exp={} mark={} credit=${} pnl={} ({} of credit) {}".format(
                    row["symbol"], row["quantity"], row["strike"], row["right"],
                    row["expiry"], px, row["credit_usd"], _p_s, _pc_s,
                    ("MANAGED -- a protective buy-to-close stop is armed on this position"
                     if _managed else





                     "PAIRED ({}) -- exits with its long leg; no separate stop by design"
                     .format(_why)
                     if _why == "spread_short_leg_closes_with_its_long" else
                     "UNMANAGED ({}) -- no stop/target is armed on this position".format(_why))))
                if int(cid) not in self._short_alerted:
                    self._short_alerted.add(int(cid))
                    self._post_short_alert(row)
        except Exception as e:
            print(f"[WARN] short-position reporting errored (continuing): {e}")
        self._short_report = rows
        return rows

    def _post_short_alert(self, row: dict) -> bool:
        """Public API contract; production-derived narrative omitted."""
        try:
            channel = getattr(self.config, "alerts_channel", "") or ""
            token = os.environ.get("SLACK_BOT_TOKEN", "")
            if not (channel and token):
                return True
            from exitmgr import approval
            ts = approval.post_proposal(
                token, channel,
                f":warning: *SHORT POSITION VISIBLE — {row.get('symbol')}* "
                f"{row.get('quantity')}x {row.get('strike')}{row.get('right')} "
                f"exp {row.get('expiry')}\n"
                f"_collateral ${row.get('collateral_usd')} / credit ${row.get('credit_usd')} — "
                + (f"a protective buy-to-close stop is ARMED on this position "
                   f"(no take-profit: R2 doctrine). Watch it._"
                   if row.get("managed") else
                   f"the exit manager can SEE this position but is NOT protecting it "
                   f"({row.get('unmanaged_reason')}). Manage it manually._"))
            return bool(ts)
        except Exception as e:
            print(f"[WARN] short-position alert post failed: {e}")
            return False

    async def _manage_short_positions(self, live_shorts, live_open_orders,
                                      dry_run: bool, caps_ok: bool = True) -> int:
        """Public API contract; production-derived narrative omitted."""
        placed = 0
        self._short_managed = {}
        try:
            shorts = dict(live_shorts or {})
            if not shorts:
                return 0
            marks = self._short_marks()
            for cid in sorted(shorts):
                try:
                    if await self._manage_one_short(int(cid), shorts[cid], marks,
                                                    live_open_orders, dry_run, caps_ok):
                        placed += 1
                except Exception as _ie:
                    print(f"[WARN] short exit evaluation failed for con_id={cid}: {_ie} "
                          "(continuing; the position remains UNMANAGED this cycle)")
        except Exception as e:
            print(f"[WARN] short exit management errored (continuing): {e}")
        return placed

    async def _manage_one_short(self, con_id: int, pos_data, marks: Dict[int, float],
                                live_open_orders, dry_run: bool, caps_ok: bool) -> bool:
        """Public API contract; production-derived narrative omitted."""
        je = self._journal_entries.get(con_id) or {}
        try:
            signed_qty = int(getattr(pos_data, "quantity", 0) or 0)
        except (TypeError, ValueError):
            signed_qty = 0
        symbol = getattr(pos_data, "symbol", "") or je.get("symbol") or ""



        if signed_qty >= 0:
            print(f"[ERROR] con_id={con_id} ({symbol}) reached the SHORT exit path with "
                  f"quantity={signed_qty}; refusing (this path may only close a proven short)")
            return False
        contracts = abs(signed_qty)

        state_row = {"con_id": con_id, "symbol": symbol, "quantity": signed_qty,
                     "contracts": contracts, "managed": False, "reason": None,
                     "entry_credit_usd": None, "mark": None, "stop_pct": None,
                     "trigger": None, "placed": False}
        self._short_managed[con_id] = state_row



        if con_id in (getattr(self, "_spread_short_legs", {}) or {}):
            state_row["reason"] = "spread_short_leg_closes_with_its_long"
            return False
        if self.state_manager.state.get_in_flight(con_id) is not None:
            state_row["reason"] = "close_already_in_flight"
            return False




        px = marks.get(con_id)
        quote = None
        try:
            quote = (await self.ib_conn.fetch_quotes([con_id]) or {}).get(con_id)
        except Exception as _qe:
            print(f"[WARN] con_id={con_id} ({symbol}): short quote fetch failed ({_qe})")
            quote = None
        if px is None and quote is not None:
            px = quote.get("price")
        ask = (quote or {}).get("ask")
        try:
            ask = float(ask) if ask is not None else None
            if ask is not None and (ask != ask or ask <= 0):
                ask = None
        except (TypeError, ValueError):
            ask = None
        state_row["mark"] = px

        managed, why = self._short_exit_readiness(con_id, je, contracts, px)
        state_row["managed"] = managed
        state_row["reason"] = why
        if not managed:
            print(f"[EVAL-SHORT] con_id={con_id} ({symbol}): NOT EVALUATED ({why}) -- "
                  "no stop is armed on this position")
            return False
        credit = self._short_entry_credit(con_id, je, contracts)
        if credit is None:
            state_row["managed"] = False
            state_row["reason"] = "no_entry_credit_basis"
            return False
        state_row["entry_credit_usd"] = credit

        rules = self._short_exit_rules(je)
        state_row["stop_pct"] = rules.stop_pct
        dte = days_to_expiry(getattr(pos_data, "expiry", "") or je.get("expiry"))



        trigger = evaluate_short_position(
            con_id=con_id, symbol=symbol, quantity=signed_qty, entry_credit=credit,
            current_price=px, days_to_expiry=dte, rules=rules,
            trail_armed=False, trough_since_arm=None,
        )
        if trigger is None:
            _pnl = rules_mod.short_pnl_pct(px, credit, contracts)
            print(f"[EVAL-SHORT] con_id={con_id} ({symbol}): no trigger "
                  f"(mark={px:.4f}, credit=${credit:.2f}, pnl={_pnl:+.2f}% of credit, "
                  f"stop={rules.stop_pct:.0f}% of credit, dte={dte})")
            return False
        trigger.con_id = con_id
        state_row["trigger"] = getattr(trigger, "trigger_type", None)

        if not getattr(trigger, "is_short", False):
            print(f"[ERROR] con_id={con_id} ({symbol}): trigger {trigger.trigger_type!r} is not "
                  "short-signed (is_short is False); refusing to place a buy-to-close on it")
            return False
        print(f"[EVAL-SHORT] con_id={con_id} ({symbol}): {trigger.message} "
              f"(pnl={trigger.pnl_pct:+.2f}% of credit)")

        if dry_run:
            print(f"[EVAL-SHORT] dry run -- no buy-to-close placed for con_id={con_id}")
            return False




        _protective = self._is_protective_exit(trigger)
        _bad = self._reconcile_bad_con_ids
        if (_bad is None) or (con_id in _bad):
            print(f"[SHORT] con_id={con_id} ({symbol}): protective buy-to-close WITHHELD -- "
                  "reconcile-inconsistent")
            try:
                self._post_stops_withheld_alert([(symbol, con_id, trigger.trigger_type)])
            except Exception:
                pass
            return False
        if not (caps_ok or _protective):
            print(f"[SHORT] con_id={con_id} ({symbol}): caps exceeded and trigger is not "
                  "protective; not placing")
            return False




        market_flag = getattr(self.config.rules, "exit_market_orders", False)
        if trigger.trigger_type == "time_stop" and trigger.pnl_pct >= 0:
            market_flag = False
            print(f"[SHORT] time-stop MANAGED buy-to-close for {symbol} (green) -> LIMIT at mark")

        _exit_ctx = {
            "symbol": symbol,
            "reason": self._exit_reason(trigger),
            "trigger_type": trigger.trigger_type,
            "trigger_message": trigger.message,
            "trigger_pnl_pct": trigger.pnl_pct,
            "triggered_at": datetime.now(timezone.utc).isoformat(),
            "trigger_threshold_pct": -abs(float(rules.stop_pct)),
            "trigger_threshold_price": round(
                (credit / (100.0 * contracts)) * (1.0 + abs(float(rules.stop_pct)) / 100.0),
                6),
            "trigger_threshold_basis": "credit_received_return_pct",
            "reload": False,
            "reload_conviction": None,


            "close_qty": contracts,
            "position_qty": contracts,


            "entry_debit": credit,
            "journal_entry": dict(je),
            "campaign_binding": self._close_campaign_binding(con_id),
            "decision_id": je.get("decision_id"),
            "entry_model_identity": je.get("model_identity"),
            "entry_model_identity_source": _identity_source(
                je.get("model_identity"), je.get("model_identity_source")),
            "exit_model_identity": None,


            "exit_model_identity_source": None,
            "manual_request": False,
            "is_short": True,
            "extra": {
                "partial": False,
                "close_qty": contracts,
                "remaining_qty": 0,


                "is_short": True,
                "side": "credit",
                "entry_credit_usd": credit,
                "trigger_mark": px,
                "ask": ask,
                "limit_price": px,
                "rule_fired": trigger.trigger_type,
                "exit_reasoning": trigger.message,
                "short_stop_pct_of_credit": rules.stop_pct,
            },
        }
        result = await self.order_manager.place_close_order(
            con_id=con_id, symbol=symbol,
            quantity=signed_qty,
            limit_price=px, entry_debit=credit,
            live_open_orders=(live_open_orders or {}),
            spread=None, market=market_flag,
            right=(je.get("right") or getattr(pos_data, "right", None)),
            trigger_type=trigger.trigger_type,
            exit_context=_exit_ctx,
            short_close=True, ask=ask,
        )
        if bool(getattr(result, "ambiguous", False)):
            state_row["placed"] = True
            print(f"[SHORT-EXIT-ACK-UNKNOWN] {symbol} con_id={con_id} "
                  f"order_id={result.order_id}; durable latch retained, no blind retry")
            return True
        if not result.success:
            print(f"[SHORT] con_id={con_id} ({symbol}) buy-to-close NOT placed: {result.message}")
            return False
        state_row["placed"] = True
        _inf = self.state_manager.state.get_in_flight(con_id)
        _tr = getattr(result, "trade", None)
        if _tr is not None:
            try:
                for _ in range(12):
                    _st_now = getattr(getattr(_tr, "orderStatus", None), "status", None)
                    if _st_now in ("Filled", "Cancelled", "ApiCancelled", "Inactive"):
                        break
                    await asyncio.sleep(0.5)
            except Exception as _fe:
                print(f"[WARN] short fill capture failed for con_id={con_id}: {_fe}")
        _filled_now = bool(_inf is not None and _tr is not None
                           and self._finalize_in_flight_exit(con_id, _inf, _tr))
        if not _filled_now:
            _st = getattr(getattr(_tr, "orderStatus", None), "status", None)
            if isinstance(_st, str):
                self._log_unfilled_exit(
                    con_id, symbol, trigger, fill_status=_st, close_qty=contracts,
                    trigger_mark=px, bid=None, limit_price=px,
                    order_id=result.order_id, reason=self._exit_reason(trigger),
                    placed_at=getattr(_inf, "placed_at", None))
            print(f"[SHORT-EXIT-PENDING] {symbol} con_id={con_id} order_id={result.order_id} "
                  f"status={_st or 'unknown'}; durable context retained")
        else:
            print(f"[SHORT-EXIT] {symbol} con_id={con_id} bought to close "
                  f"({trigger.trigger_type})")
        return True

    async def _process_short_assignments(self, live_shorts, live_stocks) -> None:
        """Public API contract; production-derived narrative omitted."""
        try:
            live_short_ids = set((live_shorts or {}).keys())
            stock_by_symbol: Dict[str, int] = {}
            for pd in (live_stocks or {}).values():
                try:
                    sym = str(getattr(pd, "symbol", "") or "").upper()
                    q = int(getattr(pd, "quantity", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if sym:
                    stock_by_symbol[sym] = stock_by_symbol.get(sym, 0) + q
            for con_id in sorted(set(self._credit_con_ids or set())):
                try:
                    if con_id in live_short_ids:
                        continue
                    je = self._journal_entries.get(int(con_id))
                    if not je:
                        continue
                    dte = days_to_expiry(je.get("expiry"))
                    if dte is not None and dte < 0:
                        continue
                    if con_id in self._terminal_dedupe_set():
                        continue
                    if con_id in self._full_close_on_disk():
                        self._terminal_dedupe_set().add(con_id)
                        continue
                    sym = str(je.get("symbol") or "").upper()
                    contracts = abs(int(je.get("quantity", 1) or 1)) or 1
                    shares = stock_by_symbol.get(sym, 0)
                    right = str(je.get("right") or "P").upper()[:1]



                    assigned = (shares >= 100 * contracts) if right == "P" else (shares <= -100 * contracts)
                    if not assigned:
                        print(f"[SHORT] {sym} con_id={con_id} left the option book with "
                              f"dte={dte} and NO matching stock position (shares={shares}); "
                              "treating as an EXTERNAL close, NOT assignment -- journal row "
                              "left intact for the execution capture to reconcile")
                        continue
                    spot = None
                    try:
                        spot = await self._spot_price(je.get("symbol"))
                    except Exception:
                        spot = None
                    strike = je.get("strike")
                    intrinsic = None
                    if spot is not None and strike is not None:
                        try:
                            intrinsic = (max(0.0, float(strike) - float(spot)) if right == "P"
                                         else max(0.0, float(spot) - float(strike)))
                        except (TypeError, ValueError):
                            intrinsic = None
                    import types as _t
                    trig = _t.SimpleNamespace(trigger_type="assigned", pnl_pct=0.0,
                                              message="short option assigned")
                    extra = {
                        "exit_event": "assigned",
                        "rule_fired": "assigned",
                        "exit_reasoning": (f"short {right} assigned -> {shares} shares of {sym}"),
                        "side": "credit",
                        "assigned": True,
                        "assigned_shares": shares,
                        "assigned_strike": strike,
                        "dte_at_close": dte,
                        "underlying_price": spot,
                    }
                    if intrinsic is None:

                        extra["expiry_value_unknown"] = True
                        extra["realized_unknown_reason"] = "assignment_value_unknown"
                    self._log_exit(int(con_id), je.get("symbol"), trig,
                                   exit_price_per_share=intrinsic,
                                   quantity=int(je.get("quantity", -contracts) or -contracts),
                                   reason="assigned", extra=extra,
                                   entry_debit=je.get("debit"), je=je)
                    self._terminal_dedupe_set().add(int(con_id))


                    try:
                        self.state_manager.state.peak_prices.pop(str(con_id), None)


                        self.state_manager.state.clear_trail_state(con_id)
                    except Exception:
                        pass
                    print(f"[TERMINAL-CLOSE] {sym} con_id={con_id} reason=assigned "
                          f"shares={shares} strike={strike} spot={spot} "
                          "(ASSIGNMENT recorded; the resulting STOCK position is NOT managed "
                          "by the exit manager)")
                    self._post_short_alert({
                        "symbol": sym, "quantity": je.get("quantity"), "strike": strike,
                        "right": right, "expiry": je.get("expiry"),
                        "collateral_usd": je.get("collateral_usd"),
                        "credit_usd": self._credit_received_usd(je, contracts)})
                except Exception as _ie:
                    print(f"[WARN] assignment check failed for con_id={con_id}: {_ie} (continuing)")
        except Exception as e:
            print(f"[WARN] short assignment processing errored (continuing): {e}")

    @staticmethod
    def _exit_reason(trigger) -> str:
        """Public API contract; production-derived narrative omitted."""
        tt = (getattr(trigger, "trigger_type", "") or "").lower()


        if "scale" in tt:
            return "scale_out"
        if "profit" in tt or "take_profit" in tt:
            return "profit_target"
        if "time" in tt:
            return "time_stop"
        if "trail" in tt:
            return "stop"
        if "stop" in tt or "cut" in tt:
            return "stop"
        if "manual" in tt:
            return "manual"
        return tt or "exit"

    def _check_kill_switch(self) -> bool:
        """Public API contract; production-derived narrative omitted."""
        kill_path = Path(self.config.kill_switch.path)
        if kill_path.exists():
            print(f"[KILL SWITCH] {self.config.kill_switch.path} active: halting entries only; "
                  "protective/manual exits remain armed")
            return True
        return False

    def _shadow_resident_protection(self, con_id, symbol, quantity,
                                    entry_debit, rules, journal) -> None:
        """Public API contract; production-derived narrative omitted."""
        if self._broker_protection_mode != "shadow":
            return
        try:
            from exitmgr.resident_protection import plan_shadow
            campaign = open_campaign(self._journal_campaigns, con_id)
            if campaign is None:
                raise ValueError("no open campaign")
            binding = {"campaign_seq": campaign.campaign_seq,
                       "identity": list(campaign.identity),
                       "lot_lines": [lot.line_no for lot in campaign.lots]}
            identity = self._runtime_identity_fields or {}
            plan = plan_shadow(
                con_id=con_id, symbol=symbol, quantity=quantity,
                entry_debit=entry_debit, stop_pct=getattr(rules, "stop_pct", None),
                journal=journal, campaign=binding,
                code_version=identity.get("code_version"),
                policy_version=identity.get("policy_version"))
            key = str(con_id)
            if self.state_manager.state.resident_protection.get(key) != plan:
                self.state_manager.state.resident_protection[key] = plan
                self.state_manager.save()
                print(f"[BROKER-PROTECTION-SHADOW] {symbol} con_id={con_id}: "
                      f"{plan['sec_type']} GTC STP @ {plan['aux_price']:.2f}, "
                      f"orderRef={plan['order_ref']} — NO broker action, approval=false")
        except Exception as exc:
            print(f"[BROKER-PROTECTION-SHADOW] con_id={con_id}: plan refused ({exc})")

    def _manual_exit_path(self):
        return os.path.join(os.path.dirname(self.config.journal.path) or ".", "manual_exits.json")

    def _read_manual_exits(self) -> Dict[int, dict]:
        from exitmgr.manual_exit_queue import ManualExitQueue, ManualExitQueueError
        try:
            return ManualExitQueue(self._manual_exit_path()).read()
        except ManualExitQueueError as e:


            print(f"[MANUAL-EXIT] [ALERT] {e}; refusing all manual-exit actions until repaired")
            return {}

    def _manual_exit_fail(self, sym, cid, reason) -> None:



        try:
            with open(os.path.join(os.path.dirname(self.config.journal.path) or ".", "manual_exit_errors.log"), "a") as f:
                f.write(f"{datetime.now(timezone.utc).isoformat()} {sym} con_id={cid} FAILED: {reason}\n")
        except Exception:
            pass
        try:
            ch = getattr(self.config, "alerts_channel", "") or ""; tok = os.environ.get("SLACK_BOT_TOKEN", "")
            if ch and tok:
                from exitmgr import approval
                approval.post_proposal(tok, ch, f":warning: *Could not auto-sell {sym}* (one-tap early-exit): {reason}. The request remains pending and will retry until Filled.")
        except Exception:
            pass
        print(f"[MANUAL-EXIT] {sym} con_id={cid} FAILED/PENDING: {reason}")

    def _clear_manual_exit(self, con_id, request_id) -> bool:
        from exitmgr.manual_exit_queue import ManualExitQueue
        try:
            ManualExitQueue(self._manual_exit_path()).discard(int(con_id), str(request_id or ""))
            return True
        except Exception as e:


            print(f"[MANUAL-EXIT] [ALERT] filled con_id={con_id} but durable queue clear failed: {e}")
            return False

    def _archive_obsolete_manual_exits(self, requests, live_positions, live_shorts,
                                       live_stocks) -> Dict[int, dict]:
        """Public API contract; production-derived narrative omitted."""
        from exitmgr.manual_exit_queue import ManualExitQueue
        queue = ManualExitQueue(self._manual_exit_path())
        live_ids = set(live_positions) | set(live_shorts) | set(live_stocks)
        current = dict(requests or {})
        for parent, request in list(current.items()):
            if int(parent) in self._campaign_conflicts:
                continue
            try:
                topology = {int(leg["con_id"]) for leg in request["topology"]}
            except (KeyError, TypeError, ValueError):
                continue
            if topology & live_ids or self.state_manager.state.get_in_flight(parent) is not None:
                continue
            campaigns = list(self._journal_campaigns.get(int(parent)) or [])
            exact_index = next((i for i, campaign in enumerate(campaigns)
                                if _campaign_receipt(campaign) == request.get("campaign")), None)
            if exact_index is None:
                continue
            campaign = campaigns[exact_index]
            marker = campaign.close_row if isinstance(campaign.close_row, dict) else {}
            terminal_marker = (_terminal_tool_marker(marker)
                               or _terminal_manager_marker(marker)
                               or _terminal_flex_marker(marker))
            superseded = exact_index < len(campaigns) - 1
            if not campaign.closed or not (terminal_marker or superseded):
                continue
            evidence = {
                "broker_flat": True,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "topology_con_ids": sorted(topology),
                "queued_campaign": request.get("campaign"),
                "terminal_marker": ({"event": marker.get("event"),
                                     "status": marker.get("status")} if terminal_marker else None),
                "superseded_by": [_campaign_receipt(c) for c in campaigns[exact_index + 1:]],
            }
            try:
                current = queue.archive_terminal(
                    parent, request.get("request_id"), request.get("binding_sha"), evidence)
                print(f"[MANUAL-EXIT] archived obsolete broker-flat request for con_id={parent}")
            except Exception as exc:
                print(f"[MANUAL-EXIT] terminal archive refused for con_id={parent}: {exc}")
        return current

    def _get_scope_con_ids(self, live_positions: Dict[int, PositionData]) -> Set[int]:
        """Public API contract; production-derived narrative omitted."""
        if self.config.scope.mode == "journal":


            authorized = set(self._journal_entries) | set(self._campaign_conflicts)
            return authorized & set(live_positions)
        else:

            return set(live_positions.keys())

    def campaign_conflict_symbols(self) -> Set[str]:
        return {str(row.get("symbol") or "").strip().upper()
                for row in self._campaign_conflict_rows.values()
                if str(row.get("symbol") or "").strip()}

    def _campaign_conflict_emergency_context(self, pos, live_shorts) -> tuple:
        """Public API contract; production-derived narrative omitted."""
        raw = self._campaign_conflict_rows.get(int(pos.con_id)) or {}
        evidence = self._campaign_conflicts.get(int(pos.con_id)) or {}
        try:
            qty = int(pos.quantity)
            avg_cost = float(pos.avg_cost)
            strike = float(pos.strike)
        except (TypeError, ValueError, OverflowError):
            return None, "broker quantity/basis/strike is unavailable"
        identity = (str(pos.symbol or "").strip().upper(),
                    str(pos.right or "").strip().upper()[:1], str(pos.expiry or "").strip())
        if (str(pos.sec_type or "").upper() != "OPT" or qty <= 0
                or not math.isfinite(avg_cost) or avg_cost <= 0
                or not math.isfinite(strike) or strike <= 0
                or not all(identity) or identity[1] not in {"C", "P"}):
            return None, "exact standalone option identity and positive broker basis are unproved"
        if (raw.get("spread") or {}).get("short_con_id"):
            return None, "quarantined row claims multi-leg topology"
        for short in (live_shorts or {}).values():
            short_symbol = str(short.symbol or "").strip().upper()
            if short_symbol == identity[0]:
                return None, ("same-underlying broker short makes standalone topology "
                              "ambiguous")
        configured = getattr(self.config.rules, "stop_pct", None)
        candidates = [_STOP_BACKSTOP_PCT]


        for value in (configured,):
            try:
                value = float(value)
                if math.isfinite(value) and value > 0:
                    candidates.append(value)
            except (TypeError, ValueError, OverflowError):
                pass


        try:
            raw_snapshot = json.loads(json.dumps(raw, allow_nan=False))
            evidence_snapshot = json.loads(json.dumps(evidence, allow_nan=False))
        except (TypeError, ValueError):
            return None, "campaign-conflict evidence is not canonically serializable"
        authority = {
            "schema": "campaign_conflict_emergency_authority.v1",
            "fingerprint": evidence_snapshot.get("fingerprint"),
            "evidence": evidence_snapshot,
            "evidence_sha256": _canonical_sha256(evidence_snapshot),
            "quarantined_row": raw_snapshot,
            "quarantined_row_sha256": _canonical_sha256(raw_snapshot),
            "broker_position": {
                "source": "ibkr_reqPositions",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "con_id": int(pos.con_id), "sec_type": "OPT", "symbol": identity[0],
                "right": identity[1], "expiry": identity[2], "strike": strike,
                "quantity": qty, "avg_cost_per_share": avg_cost,
            },
            "entry_debit": avg_cost * 100.0 * qty,
            "stop_pct": min(candidates),
        }
        if not _valid_campaign_conflict_authority(authority):
            return None, "campaign-conflict emergency authority could not be proven"
        return {"symbol": identity[0], "quantity": qty,
                "entry_debit": avg_cost * 100.0 * qty,
                "stop_pct": min(candidates), "authority": authority}, None

    def _check_caps(self, dry_run: bool) -> tuple[bool, str]:
        """Public API contract; production-derived narrative omitted."""





        today = _trading_day()
        daily_stats = self.state_manager.state.daily_stats.get(today)

        if daily_stats is None:
            daily_stats = {"orders_placed": 0, "notional_closed": 0.0}
            orders_today = 0
            notional_today = 0.0
        else:
            orders_today = daily_stats.orders_placed
            notional_today = daily_stats.notional_closed


        if orders_today >= self.config.caps.max_orders_per_day:
            return False, f"Daily order cap reached: {orders_today} >= {self.config.caps.max_orders_per_day}"


        if notional_today >= self.config.caps.max_notional_per_day:
            return False, f"Daily notional cap reached: ${notional_today:.2f} >= ${self.config.caps.max_notional_per_day:.2f}"

        return True, ""

    def _post_reconcile_block_alert(self, alerts) -> None:
        """Public API contract; production-derived narrative omitted."""
        import os, json as _json, urllib.request, time
        channel = getattr(self.config, "alerts_channel", "") or ""
        token = os.environ.get("SLACK_BOT_TOKEN", "")
        if not (channel and token):
            return


        tsf = (os.environ.get("EXITMGR_RECONCILE_TS_PATH")
               or os.path.expanduser("~/exitmgr-app/.reconcile_alert_ts"))
        try:
            if os.path.exists(tsf) and (time.time() - os.path.getmtime(tsf)) < 1800:
                return
        except Exception:
            pass
        errs = [a for a in alerts if "[ERROR]" in a] or alerts
        body = (":octagonal_sign: *Exit-manager reconciliation BLOCKED -- trader cannot manage exits.*\n"
                + "\n".join(errs[:6]) + "\n_Resolve the position(s) above (journal or close) to unblock._")
        try:
            req = urllib.request.Request("https://slack.com/api/chat.postMessage",
                data=_json.dumps({"channel": channel, "text": body}).encode(),
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=8)
            open(tsf, "w").write(str(time.time()))
        except Exception as _e:
            print(f"[WARN] reconcile-block Slack alert failed: {_e}")




    _PROTECTIVE_TRIGGERS = frozenset({
        "stop", "trailing_stop", "time_stop", "profit_target", "take_profit", "model_cut"})

    def _is_protective_exit(self, trigger) -> bool:
        return getattr(trigger, "trigger_type", None) in self._PROTECTIVE_TRIGGERS

    async def _read_entry_order_view(self):
        """Public API contract; production-derived narrative omitted."""
        from exitmgr.trader import EntryOrderView, broker_entry_order_view
        ib = getattr(self.ib_conn, "ib", None)
        if ib is None:
            return EntryOrderView(False, error="no IB handle on the connection")
        try:
            return await broker_entry_order_view(
                ib, getattr(self.config, "audit_path", None) or None)
        except Exception as exc:
            return EntryOrderView(False, error=str(exc) or type(exc).__name__)

    def _entry_gate(self, con_id, je):
        """Public API contract; production-derived narrative omitted."""
        from exitmgr.trader import (EntryOrderView, ProtectiveEntryGate,
                                     protective_sell_gate)
        view = getattr(self, "_entry_order_view", None)
        if view is None:
            view = EntryOrderView(
                False, error="the entry-side open-order book was never read this cycle")
        row = dict(je or {})


        if row.get("contract_id") is None:
            row["contract_id"] = con_id





















        if not getattr(view, "readable", False) and not str(row.get("order_ref") or ""):
            _q = row.get("quantity")
            try:
                _m = int(abs(float(_q))) if _q is not None else None
            except (TypeError, ValueError):
                _m = None
            return ProtectiveEntryGate(True, (), None, _m, "no entry orderRef to be working")
        try:
            return protective_sell_gate(row, view)
        except Exception as exc:
            return ProtectiveEntryGate(
                False, ("protective-sell gate raised (%s); refusing rather than guessing"
                        % (exc,),), None, None)

    def _clear_closed_position(self, con_id) -> None:
        """Public API contract; production-derived narrative omitted."""
        try:
            k = str(con_id)
            st = self.state_manager.state


            self._clear_trail_qualification_alert(con_id)
            st.clear_position_tracking(con_id)







            _reopened = False
            if getattr(self, "_journal_load_in_progress", False):
                try:
                    _reopened = open_campaign(
                        getattr(self, "_journal_campaigns", None) or {}, con_id) is not None
                except Exception:
                    _reopened = False
            if _reopened:
                print(f"[INFO] con_id={con_id}: tool-close marker superseded by a later journal "
                      f"entry -- stale peak/trail cleared, the RE-ENTRY is kept and stays managed")
            else:
                try:
                    self._journal_entries.pop(int(con_id), None)
                except (TypeError, ValueError):
                    pass
                self._journal_entries.pop(con_id, None)
            self.state_manager.save()
        except Exception as e:
            print(f"[WARN] clear-closed-position failed for con_id={con_id}: {e}")

    def _post_unthrottled_alert(self, body: str, what: str, *, channel=None,
                                condition_key=None, repeat_seconds=0,
                                on_delivery=None) -> bool:
        """Public API contract; production-derived narrative omitted."""
        import time as _notice_time
        if not body:
            return False
        key = (str(what), condition_key if condition_key is not None else body)
        delivered_at = getattr(self, "_protective_notice_delivered_at", None)
        if delivered_at is None:
            delivered_at = self._protective_notice_delivered_at = {}
        previous = delivered_at.get(key)
        if (repeat_seconds > 0 and previous is not None
                and _notice_time.monotonic() - previous < repeat_seconds):
            return False

        def remember_delivery(delivered):
            if delivered and repeat_seconds > 0:

                if len(delivered_at) >= 128 and key not in delivered_at:
                    delivered_at.pop(min(delivered_at, key=delivered_at.get), None)
                delivered_at[key] = _notice_time.monotonic()
            if on_delivery is not None:
                try:
                    on_delivery(bool(delivered))
                except Exception as exc:
                    print(f"[WARN] {what} delivery checkpoint failed: {exc}")

        def deliver():
            try:
                from exitmgr import alerting
                target = channel or getattr(self.config, "alerts_channel", "") or alerting.alerts_channel()
                fallback = (alerting.alerts_channel() if target == alerting.error_channel()
                            else alerting.error_channel())
                delivered = alerting.post(
                    body, target, label=what, fallback_channel=fallback,
                    timeout=8, dedup=False)
                if not delivered:
                    print(f"[WARN] {what} Slack alert UNDELIVERED after verified transport/fallback")
                else:
                    print(f"[ALERT-DELIVERED] {what}")
                return bool(delivered)
            except Exception as exc:
                print(f"[WARN] {what} Slack alert failed: {exc}")
                return False

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            delivered = deliver()
            remember_delivery(delivered)
            return bool(delivered)
        pending = getattr(self, "_active_risk_alert_tasks", None)
        if pending is None:
            pending = self._active_risk_alert_tasks = {}
        if key in pending:
            return False
        if len(pending) >= 4:
            print(f"[WARN] {what} Slack alert deferred: four active-risk deliveries pending")
            remember_delivery(False)
            return False
        worker = asyncio.to_thread(deliver)
        try:
            task = loop.create_task(worker)
        except Exception as exc:
            worker.close()
            print(f"[WARN] {what} Slack alert dispatch failed: {exc}")
            remember_delivery(False)
            return False
        pending[key] = task

        def completed(done):
            if pending.get(key) is done:
                pending.pop(key, None)
            try:
                remember_delivery(done.result())
            except BaseException as exc:
                print(f"[WARN] {what} Slack alert task failed: {exc!r}")
        task.add_done_callback(completed)
        return True

    @staticmethod
    def _protective_fill_telemetry(ctx: dict, *, fill_price: float, quantity: int,
                                   basis: float, short_close: bool,
                                   fill_ts: Optional[str]) -> dict:
        """Public API contract; production-derived narrative omitted."""
        extra = dict((ctx or {}).get("extra") or {})

        def finite(value):
            try:
                value = float(value)
                return value if math.isfinite(value) else None
            except (TypeError, ValueError):
                return None

        threshold_pct = finite((ctx or {}).get("trigger_threshold_pct"))
        threshold_price = finite((ctx or {}).get("trigger_threshold_price"))
        trigger_mark = finite(extra.get("trigger_mark"))
        actual_pct = None
        realized_pnl = None
        try:
            if basis > 0 and quantity > 0:
                close_value = float(fill_price) * 100.0 * int(quantity)
                realized_pnl = ((basis - close_value) if short_close
                                else (close_value - basis))
                actual_pct = realized_pnl / basis * 100.0
        except (TypeError, ValueError, ZeroDivisionError):
            pass
        elapsed = None
        triggered_at = (ctx or {}).get("triggered_at")
        try:
            started = datetime.fromisoformat(str(triggered_at).replace("Z", "+00:00"))
            ended = datetime.fromisoformat(str(fill_ts).replace("Z", "+00:00"))
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            if ended.tzinfo is None:
                ended = ended.replace(tzinfo=timezone.utc)
            seconds = (ended.astimezone(timezone.utc) -
                       started.astimezone(timezone.utc)).total_seconds()
            if seconds >= 0:
                elapsed = round(seconds, 3)
        except (TypeError, ValueError):
            pass
        signed_price_slippage = None
        adverse_price_slippage = None
        if threshold_price is not None:
            signed_price_slippage = round(float(fill_price) - threshold_price, 6)
            adverse_price_slippage = round(
                max(0.0, signed_price_slippage if short_close else -signed_price_slippage), 6)
        signed_pct_slippage = None
        adverse_pct_slippage = None
        if threshold_pct is not None and actual_pct is not None:
            signed_pct_slippage = round(actual_pct - threshold_pct, 4)
            adverse_pct_slippage = round(max(0.0, threshold_pct - actual_pct), 4)
        return {
            "triggered_at": triggered_at,
            "trigger_mark": trigger_mark,
            "trigger_threshold_pct": threshold_pct,
            "trigger_threshold_price": threshold_price,
            "trigger_threshold_basis": (ctx or {}).get("trigger_threshold_basis"),
            "fill_ts": fill_ts,
            "fill_price": round(float(fill_price), 6),
            "trigger_to_fill_seconds": elapsed,
            "realized_loss_usd": (round(max(0.0, -realized_pnl), 2)
                                  if realized_pnl is not None else None),
            "realized_loss_pct": (round(max(0.0, -actual_pct), 4)
                                  if actual_pct is not None else None),
            "threshold_to_fill_slippage_per_share": signed_price_slippage,
            "adverse_slippage_beyond_threshold_per_share": adverse_price_slippage,
            "threshold_to_fill_slippage_pct_points": signed_pct_slippage,
            "adverse_slippage_beyond_threshold_pct_points": adverse_pct_slippage,
        }

    def _post_hard_stop_breach_incident(self, *, symbol: str, con_id: int,
                                        fill_key: str, telemetry: dict,
                                        on_delivery=None) -> bool:
        """Public API contract; production-derived narrative omitted."""
        threshold = telemetry.get("trigger_threshold_pct")
        realized_loss = telemetry.get("realized_loss_pct")
        adverse = telemetry.get("adverse_slippage_beyond_threshold_pct_points")
        if threshold is None or realized_loss is None or not adverse or adverse <= 0:
            return False
        body = (
            ":rotating_light: *HARD-STOP EXECUTION BREACH* — the fill was worse than the "
            "configured trigger threshold:\n"
            f"• *{symbol}* con_id={con_id}: trigger {float(threshold):+.2f}% of basis, "
            f"realized loss {float(realized_loss):.2f}% "
            f"({float(adverse):.2f} percentage points beyond threshold)\n"
            f"• trigger mark={telemetry.get('trigger_mark')}, "
            f"fill={telemetry.get('fill_price')}, "
            f"trigger→fill={telemetry.get('trigger_to_fill_seconds')}s\n"
            "_The stop percentage is an election threshold; gaps, quote loss and execution "
            "slippage can produce a larger realized loss. This alert does not claim a maximum "
            "realized loss._")
        return self._post_unthrottled_alert(
            body, "hard-stop-execution-breach", condition_key=str(fill_key), repeat_seconds=0,
            on_delivery=on_delivery)

    def _ensure_hard_stop_breach_alert(self, *, symbol: str, con_id: int,
                                       fill_key: str, telemetry: dict, inf) -> bool:
        """Public API contract; production-derived narrative omitted."""
        effects = inf.side_effects
        if effects.get("hard_stop_breach_alert"):
            return True
        identity = (int(con_id), getattr(inf, "client_id", None),
                    getattr(inf, "order_id", 0), getattr(inf, "perm_id", 0),
                    getattr(inf, "order_ref", None), str(fill_key))

        def checkpoint(delivered):
            if not delivered:
                return
            current = self.state_manager.state.get_in_flight(con_id)
            if current is None:
                return
            current_identity = (
                int(con_id), getattr(current, "client_id", None),
                getattr(current, "order_id", 0), getattr(current, "perm_id", 0),
                getattr(current, "order_ref", None),
                str(getattr(current, "fill_key", None) or ""))
            if current_identity != identity:
                return
            current.side_effects["hard_stop_breach_alert"] = True
            self.state_manager.save()

        self._post_hard_stop_breach_incident(
            symbol=symbol, con_id=con_id, fill_key=fill_key, telemetry=telemetry,
            on_delivery=checkpoint)
        return bool(inf.side_effects.get("hard_stop_breach_alert"))

    def _page_prior_session_day_closes(self, now: Optional[datetime] = None) -> int:
        """Public API contract; production-derived narrative omitted."""
        observed = now or datetime.now(timezone.utc)
        today = _trading_day(observed)
        candidates = []
        for raw_cid, inf in dict(self.state_manager.state.in_flight).items():
            if getattr(inf, "remaining_qty", 0) <= 0:
                continue
            snap = dict(getattr(inf, "submitted_close", {}) or {})
            if str(snap.get("tif") or "").upper() != "DAY":
                continue
            effects = getattr(inf, "side_effects", None)
            if not isinstance(effects, dict) or effects.get("next_rth_day_close_page"):
                continue
            try:
                placed = datetime.fromisoformat(str(inf.placed_at).replace("Z", "+00:00"))
                if placed.tzinfo is None:
                    placed = placed.replace(tzinfo=timezone.utc)
                if _trading_day(placed) >= today:
                    continue
            except (TypeError, ValueError):
                continue
            cid = int(raw_cid)
            sym = ((self._journal_entries.get(cid) or {}).get("symbol")
                   or (getattr(inf, "exit_context", {}) or {}).get("symbol") or "?")
            candidates.append((cid, sym, inf))
        if not candidates:
            return 0
        lines = "\n".join(
            f"• *{sym}* con_id={cid}, order_id={inf.order_id}, "
            f"remaining={inf.remaining_qty}, placed={inf.placed_at}"
            for cid, sym, inf in candidates)
        identities = tuple((cid, inf.client_id, inf.order_id, inf.perm_id, inf.order_ref)
                           for cid, _sym, inf in candidates)

        def checkpoint(delivered):
            if not delivered:
                return
            changed = False
            for cid, client_id, order_id, perm_id, order_ref in identities:
                current = self.state_manager.state.get_in_flight(cid)
                if current is None:
                    continue
                if ((current.client_id, current.order_id, current.perm_id, current.order_ref)
                        != (client_id, order_id, perm_id, order_ref)):
                    continue
                current.side_effects["next_rth_day_close_page"] = True
                changed = True
            if changed:
                self.state_manager.save()

        queued = self._post_unthrottled_alert(
            ":rotating_light: *PRIOR-SESSION DAY PROTECTIVE CLOSE STILL UNRESOLVED*:\n"
            + lines
            + "\n_The next regular session is actionable. No order was cancelled, replaced, "
              "repriced, or retransmitted; inspect the exact broker identity before acting._",
            "prior-session-day-protective-close",
            condition_key=identities, repeat_seconds=0, on_delivery=checkpoint)
        if not queued:
            return 0
        return len(candidates)

    def _duplicate_resting_closes(self, live_open_orders, live_positions=None) -> List[tuple]:
        """Public API contract; production-derived narrative omitted."""
        out: List[tuple] = []
        try:
            items = list((live_open_orders or {}).items())
        except Exception:


            return out
        for cid, od in items:
            if not isinstance(od, dict):
                continue



            count = od.get("order_count")
            if not isinstance(count, int) or isinstance(count, bool) or count <= 1:
                continue
            ids = od.get("order_ids")
            if not isinstance(ids, (list, tuple)):
                ids = ()
            _pd = (live_positions or {}).get(cid)
            _sym = getattr(_pd, "symbol", None)
            if _sym is None:
                _sym = ((self._journal_entries or {}).get(cid) or {}).get("symbol")
            out.append((_sym if _sym is not None else "?", cid, tuple(ids), count,
                        od.get("remaining")))
        return out

    def _post_duplicate_close_alert(self, items) -> None:
        """Public API contract; production-derived narrative omitted."""
        if not items:
            return
        lines = "\n".join(
            f"\u2022 *{sym}* con_id={cid} \u2014 {count} resting closes, order_ids={list(ids)}"
            f" ({rem} contract(s) working in aggregate)"
            for sym, cid, ids, count, rem in items[:10])
        body = (":heavy_exclamation_mark: *DUPLICATE RESTING CLOSE at the broker*:\n"
                + lines + "\n"
                + "_Both can fill and OVER-CLOSE the position (a long becomes a naked short with "
                  "no journal row). Nothing was cancelled: cancel the redundant order(s) by hand, "
                  "and check the remaining one still covers the position._")
        self._post_unthrottled_alert(body, "duplicate-resting-close")

    def _post_stops_withheld_alert(self, items, reason: Optional[str] = None) -> None:
        """Public API contract; production-derived narrative omitted."""
        if not items:
            return


        _cause = reason or "con_id reconcile-inconsistent"
        _fix = ("_Resolve the flagged position(s) (journal or close) to release their stops._"
                if reason is None else
                "_No position is at fault -- the broker link could not be read. Stops re-arm "
                "automatically on the next readable cycle; escalate if this persists._")
        lines = "\n".join(f"• *{sym}* con_id={cid} — {why} WITHHELD ({_cause})"
                          for sym, cid, why in items[:10])
        body = (f":octagonal_sign: *Protective exits WITHHELD this cycle* ({_cause}):\n"
                + lines + "\n" + _fix)
        self._post_unthrottled_alert(body, "stops-withheld")

    def _trail_qualification_alert_path(self) -> str:
        return (os.environ.get("EXITMGR_TRAIL_QUALIFICATION_ALERTS")
                or os.path.join(os.path.dirname(self.config.journal.path) or ".",
                                ".trail-qualification-alerts.json"))

    @staticmethod
    def _option_rth_now(now: Optional[datetime] = None) -> bool:
        """Public API contract; production-derived narrative omitted."""
        from zoneinfo import ZoneInfo
        instant = now or datetime.now(timezone.utc)
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        eastern = instant.astimezone(ZoneInfo("America/New_York"))
        minute = eastern.hour * 60 + eastern.minute
        return rules_mod.is_trading_session(eastern.date()) and 570 <= minute < 960

    def _trail_qualification_key(self, con_id: int) -> str:




        binding = self.state_manager.state.campaign_bindings.get(str(int(con_id)))
        if not isinstance(binding, dict):
            try:
                binding = _campaign_receipt(open_campaign(self._journal_campaigns, con_id))
            except Exception:
                binding = None
        if isinstance(binding, dict):
            encoded = json.dumps(binding, sort_keys=True, separators=(",", ":"),
                                 allow_nan=False).encode()
            campaign = hashlib.sha256(encoded).hexdigest()[:20]
        else:
            campaign = "unbound"
        return f"new-trail-qualification:{int(con_id)}:{campaign}"

    def _clear_trail_qualification_alert(self, con_id: int) -> None:
        from exitmgr import alert_throttle
        alert_throttle.clear(self._trail_qualification_key(con_id),
                             state_path=self._trail_qualification_alert_path())

    def _post_new_trail_qualification_alert(self, symbol: str, con_id: int,
                                            reason: str) -> bool:
        """Public API contract; production-derived narrative omitted."""
        if time.monotonic() < self._trail_qualification_alert_not_before:
            return False
        if not self._option_rth_now():
            return False
        from exitmgr import alert_throttle
        key = self._trail_qualification_key(con_id)
        body = (
            ":information_source: *New trailing-stop activation deferred*\n"
            f"• *{symbol}* con_id={int(con_id)} — live executable-quote qualification "
            f"failed: {reason}\n"
            "_No new trail was armed. Existing hard-loss protection and any already-earned "
            "trail remain active; qualification retries automatically on the next readable "
            "regular-session cycle._")
        send, _held, note = alert_throttle.consider(
            body, getattr(self.config, "alerts_channel", "") or "", key=key,
            state_path=self._trail_qualification_alert_path(), record=False)
        if not send:
            return False
        def delivery_result(delivered):
            if delivered:


                alert_throttle.record_delivery(
                    body, getattr(self.config, "alerts_channel", "") or "", key=key,
                    state_path=self._trail_qualification_alert_path())

        return self._post_unthrottled_alert(
            body + note, "new-trail-qualification", condition_key=key,
            on_delivery=delivery_result)

    def _pending_protective_recovery_alert(self, cid, inf, reason):
        ctx = getattr(inf, "exit_context", None) or {}
        if ctx.get("trigger_type") not in {"stop", "trailing_stop"} or ctx.get("manual_request") is not False:
            return
        try:
            placed = datetime.fromisoformat(str(inf.placed_at).replace("Z", "+00:00"))
            if placed.tzinfo is None:
                placed = placed.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - placed).total_seconds() < 30:
                return
        except (ValueError, TypeError):
            pass
        self._post_unthrottled_alert(
            f"Protective exit con_id={cid} has no confirmed fill and cannot safely reprice: "
            f"{reason}. Existing order latch retained; no duplicate close sent.",
            f"protective-reprice-blocked:{cid}")

    async def _recover_unfilled_protective_exits(self, live_orders, live_positions,
                                                book_tick, book_at, book_generation, *, live_shorts=None):
        """Public API contract; production-derived narrative omitted."""
        quotes = self._fresh_protective_quotes()
        quote_conn = self.quote_ib_conn or self.ib_conn
        try:
            trades = list(self.ib_conn.ib.openTrades())
        except Exception as exc:
            self._post_unthrottled_alert(
                f"Protective reprice cannot read cached broker trades: {exc}",
                "protective-reprice-unreadable")
            return
        for cid_s, inf in tuple(self.state_manager.state.in_flight.items()):
            cid = int(cid_s)
            if cid not in live_positions or cid not in live_orders:
                self._pending_protective_recovery_alert(cid, inf, "position/order evidence missing")
                continue
            bad = getattr(self, "_reconcile_bad_con_ids", None)
            if bad is None or cid in bad:
                self._pending_protective_recovery_alert(cid, inf, "reconciliation is uncertain")
                continue
            try:
                remaining = float(inf.remaining_qty)
                long_qty = float(live_positions[cid].quantity)
                if (not math.isfinite(remaining) or remaining <= 0 or remaining != int(remaining)
                        or not math.isfinite(long_qty) or long_qty != int(long_qty) or long_qty < remaining):
                    raise ValueError("close remainder exceeds verified long inventory")
                topology = (getattr(inf, "submitted_close", None) or {}).get("legs", ())
                for leg in topology:
                    ratio = float(leg.get("ratio", 1))
                    side = leg.get("expected_side")
                    if side not in {"SLD", "BOT"} or not math.isfinite(ratio) or ratio <= 0 or ratio != int(ratio):
                        raise ValueError("invalid submitted close leg direction/ratio")
                    book = live_positions if side == "SLD" else (live_shorts or {})
                    position = book.get(int(leg["con_id"]))
                    held = float(getattr(position, "quantity", 0))
                    coverage = held if side == "SLD" else -held
                    if not math.isfinite(held) or held != int(held) or coverage < remaining * ratio:
                        raise ValueError("close remainder lacks verified leg inventory")
            except (ValueError, TypeError, KeyError, OverflowError) as exc:
                self._pending_protective_recovery_alert(cid, inf, str(exc))
                continue
            matches = [t for t in trades
                       if getattr(getattr(t, "order", None), "orderId", None) == inf.order_id
                       and getattr(getattr(t, "order", None), "clientId", None) == inf.client_id
                       and getattr(getattr(t, "order", None), "permId", None) == inf.perm_id]
            if len(matches) != 1:
                self._pending_protective_recovery_alert(cid, inf, "exact working order identity unavailable")
                continue
            try:
                result = await self.order_manager.reprice_unfilled_protective_exit(
                    cid, live_order=live_orders[cid], trade=matches[0], quotes=quotes,
                    book_observed_monotonic=book_tick, book_observed_at=book_at,
                    book_generation=book_generation,
                    quote_generation=getattr(quote_conn, "_connection_generation", None),
                    quote_healthy=quote_conn.is_healthy(), quote_connection=quote_conn)
            except Exception as exc:
                result = {"outcome": "blocked", "urgent": True,
                          "reason": "recovery_error:" + type(exc).__name__}
            if result.get("outcome") not in ("not_eligible", "waiting"):
                print(f"[PROTECTIVE-REPRICE] con_id={cid}: {result}")
            if result.get("urgent"):
                self._post_unthrottled_alert(
                    f"Protective exit con_id={cid} still needs a confirmed fill: "
                    f"{result.get('reason')}. Existing order identity retained.",
                    f"protective-reprice:{cid}")

    async def _alert_unfilled_orders(self) -> None:
        """Public API contract; production-derived narrative omitted."""
        cons = getattr(self.config, "construction", None)

        mins = _cfg_num(cons, "fill_alarm_minutes", 15)
        from exitmgr import alerting
        channel = (getattr(self.config, "error_channel", "") or ""
                   ) or (getattr(self.config, "alerts_channel", "") or "") or alerting.error_channel()
        try:
            from exitmgr.trader import _market_open
            if not _market_open():
                return
        except Exception:
            pass



        self._page_prior_session_day_closes()
        stale: List[str] = []
        identities = []






        now = datetime.now(timezone.utc)

        for cid_str, inf in dict(self.state_manager.state.in_flight).items():







            _is_intent = (getattr(inf, "placement_state", "submitted") == "intent")
            _is_ambiguous = (getattr(inf, "placement_state", "submitted")
                             == "transmission_ambiguous")
            if not (inf.order_id or getattr(inf, "order_ref", None)
                    or _is_intent or _is_ambiguous):
                continue
            if not inf.placed_at or inf.remaining_qty <= 0:
                continue
            try:
                placed = datetime.fromisoformat(str(inf.placed_at).replace("Z", "+00:00"))
                if placed.tzinfo is None:
                    placed = placed.replace(tzinfo=timezone.utc)
                age_min = (now - placed).total_seconds() / 60.0
            except (ValueError, TypeError):
                continue
            if age_min >= mins:
                identities.append((str(cid_str), inf.client_id, inf.order_id,
                                   inf.perm_id, inf.order_ref, inf.placement_state))
                je = self._journal_entries.get(int(cid_str)) or {}
                _ident = (f"order_id={inf.order_id}" if inf.order_id
                          else f"order_ref={getattr(inf, 'order_ref', None) or 'none'}")
                stale.append(f"• EXIT *{je.get('symbol', '?')}* con_id={cid_str} {_ident} "
                             + ("[UNTRANSMITTED INTENT — the position may be UNPROTECTED] "
                                if _is_intent else "")
                             + ("[TRANSMISSION/ACK UNKNOWN — DO NOT BLIND-RETRY] "
                                if _is_ambiguous else "")
                             + f"— unfilled for {age_min:.0f} min (qty {inf.remaining_qty})")

        try:


            open_trades = self.ib_conn.ib.openTrades()
            for t in open_trades or []:
                o = getattr(t, "order", None)
                c = getattr(t, "contract", None)
                if not o or getattr(o, "action", "") != "BUY":
                    continue
                age_min = None
                try:
                    logs = getattr(t, "log", None) or []
                    if logs:
                        t0 = logs[0].time
                        from datetime import timezone as _tz
                        age_min = (datetime.now(_tz.utc) - t0).total_seconds() / 60.0
                except Exception:
                    age_min = None
                if age_min is not None and age_min < mins:
                    continue
                identities.append(("entry", getattr(o, "clientId", None),
                                   getattr(o, "orderId", None), getattr(o, "permId", None),
                                   getattr(o, "orderRef", None)))
                stale.append(f"• ENTRY *{getattr(c, 'symbol', '?')}* order_id={getattr(o, 'orderId', '?')} "
                             f"— BUY resting unfilled"
                             + (f" for {age_min:.0f} min" if age_min is not None else " (age unknown)"))
        except Exception as e:
            print(f"[WARN] open-order scan for fill alarm failed: {e}")
        if not stale:
            return
        try:
            self._post_unthrottled_alert(
                ":hourglass_flowing_sand: *UNFILLED ORDER ALARM* — placed but not filled:\n"
                + "\n".join(stale)
                + "\n_Exact broker evidence is required to resolve this condition; no retry or cancellation was performed._",
                "unfilled-order", channel=channel,
                condition_key=tuple(sorted(identities, key=repr)), repeat_seconds=900)
        except Exception as e:
            print(f"[WARN] unfilled-order Slack alarm failed: {e}")

    @staticmethod
    def _trade_order_id(trade):
        try:
            return int(getattr(getattr(trade, "order", None), "orderId", 0) or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _positive_int(value) -> int:
        if isinstance(value, bool):
            return 0
        if isinstance(value, int):
            return value if value > 0 else 0
        if isinstance(value, str) and value.isdigit():
            parsed = int(value)
            return parsed if parsed > 0 else 0
        return 0

    @staticmethod
    def _ib_client_id(value):
        """Public API contract; production-derived narrative omitted."""
        if isinstance(value, bool):
            return None
        if isinstance(value, int) and value >= 0:
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
        return None

    def _trade_key_con_id(self, trade) -> int:
        contract = getattr(trade, "contract", None)
        if contract is None:
            return 0
        try:
            return int(self.ib_conn._order_key_con_id(contract) or 0)
        except Exception:
            return self._positive_int(getattr(contract, "conId", 0))

    def _trade_matches_in_flight(self, trade, inf) -> bool:
        """Public API contract; production-derived narrative omitted."""
        tcid = self._trade_key_con_id(trade)
        if tcid and tcid != int(inf.con_id):
            return False
        order = getattr(trade, "order", None)
        tperm = self._positive_int(getattr(order, "permId", 0))
        iperm = self._positive_int(getattr(inf, "perm_id", 0))
        if iperm:
            return bool(tperm and tperm == iperm)
        tref = getattr(order, "orderRef", None)
        iref = getattr(inf, "order_ref", None)
        if iref:
            return bool(tref and str(tref) == str(iref))
        toid = self._positive_int(getattr(order, "orderId", 0))
        ioid = self._positive_int(getattr(inf, "order_id", 0))
        tclient = self._ib_client_id(getattr(order, "clientId", None))
        iclient = self._ib_client_id(getattr(inf, "client_id", None))
        strong_client = bool(getattr(inf, "identity_version", 0) >= 1
                             or iclient is not None)
        if strong_client:
            return bool(toid and ioid and toid == ioid
                        and tclient is not None and tclient == iclient)




        return False

    def _execution_matches_in_flight(self, fill, inf) -> bool:
        """Public API contract; production-derived narrative omitted."""
        ex = getattr(fill, "execution", None)
        if ex is None:
            return False
        eperm = self._positive_int(getattr(ex, "permId", 0))
        iperm = self._positive_int(getattr(inf, "perm_id", 0))
        if iperm:
            return bool(eperm and eperm == iperm)
        eoid = self._positive_int(getattr(ex, "orderId", 0))
        ioid = self._positive_int(getattr(inf, "order_id", 0))
        eclient = self._ib_client_id(getattr(ex, "clientId", None))
        iclient = self._ib_client_id(getattr(inf, "client_id", None))
        return bool(ioid and eoid == ioid and iclient is not None and eclient == iclient)

    @staticmethod
    def _terminal_cache_key(cid: int, inf) -> Optional[str]:
        """Public API contract; production-derived narrative omitted."""
        perm_id = int(getattr(inf, "perm_id", 0) or 0)
        order_ref = getattr(inf, "order_ref", None)
        client_id = getattr(inf, "client_id", None)
        order_id = int(getattr(inf, "order_id", 0) or 0)


        if not (perm_id > 0 or (isinstance(order_ref, str) and order_ref)
                or (getattr(inf, "identity_version", 0) >= 1
                    and client_id is not None and order_id > 0)):
            return None
        ctx = dict(getattr(inf, "exit_context", {}) or {})
        payload = {
            "con_id": int(cid),
            "order_id": int(getattr(inf, "order_id", 0) or 0),
            "perm_id": int(getattr(inf, "perm_id", 0) or 0),
            "client_id": getattr(inf, "client_id", None),
            "order_ref": getattr(inf, "order_ref", None),
            "identity_version": int(getattr(inf, "identity_version", 0) or 0),
            "close_qty": ctx.get("close_qty", "__absent__"),
            "position_qty": ctx.get("position_qty", "__absent__"),
            "submitted_close": dict(getattr(inf, "submitted_close", {}) or {}),
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         allow_nan=False, default=str).encode()
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _execution_side(value) -> Optional[str]:
        side = str(value or "").upper()
        if side in {"BOT", "BUY"}:
            return "BOT"
        if side in {"SLD", "SELL"}:
            return "SLD"
        return None

    @staticmethod
    def _fill_evidence_binding(evidence: dict) -> str:
        payload = dict(evidence)
        payload.pop("binding_sha", None)
        return hashlib.sha256(json.dumps(
            payload, sort_keys=True, separators=(",", ":"),
            allow_nan=False).encode()).hexdigest()

    def _make_reconstructed_trade(self, cid: int, inf, evidence: dict, fills):
        import types as _types
        qty = int(evidence["combo_qty"])
        planned = int(evidence["planned_combo_qty"])
        avg = float(evidence["net_fill_price"])
        return _types.SimpleNamespace(
            contract=_types.SimpleNamespace(
                conId=cid, secType=str(evidence.get("sec_type") or "OPT")),
            order=_types.SimpleNamespace(
                orderId=getattr(inf, "order_id", 0), permId=getattr(inf, "perm_id", 0),
                clientId=getattr(inf, "client_id", None),
                orderRef=getattr(inf, "order_ref", None)),
            orderStatus=_types.SimpleNamespace(
                status=str(evidence["terminal_status"]), avgFillPrice=avg,
                filled=qty, remaining=max(0, planned - qty)),
            fills=list(fills or []), execution_reconstructed=True, fill_evidence=evidence)

    def _trade_from_fill_evidence(self, cid: int, inf, evidence: dict):
        """Public API contract; production-derived narrative omitted."""
        import types as _types
        try:
            required = {"schema", "source", "sec_type", "action", "combo_qty",
                        "planned_combo_qty", "terminal_status", "net_fill_price", "formula",
                        "executions", "fill_ts", "binding_sha"}
            if (not isinstance(evidence, dict) or set(evidence) != required
                    or evidence.get("schema") != "close_fill_evidence.v1"
                    or evidence.get("source") != "ibkr_executions"
                    or evidence.get("binding_sha") != self._fill_evidence_binding(evidence)):
                return None
            rows = evidence.get("executions")
            if not isinstance(rows, list) or not rows:
                return None
            fills = []
            row_fields = {"exec_id", "con_id", "side", "quantity", "price", "perm_id",
                          "order_id", "client_id", "commission", "time"}
            for row in rows:
                if not isinstance(row, dict) or set(row) != row_fields:
                    return None
                commission = row.get("commission")
                report = (_types.SimpleNamespace(commission=float(commission))
                          if commission is not None else None)
                if report is not None and not math.isfinite(report.commission):
                    return None
                exec_time = row.get("time")
                if exec_time is not None:
                    exec_time = datetime.fromisoformat(str(exec_time).replace("Z", "+00:00"))
                fills.append(_types.SimpleNamespace(
                    contract=_types.SimpleNamespace(conId=int(row["con_id"])),
                    execution=_types.SimpleNamespace(
                        execId=str(row["exec_id"]), permId=int(row.get("perm_id") or 0),
                        orderId=int(row.get("order_id") or 0), clientId=row.get("client_id"),
                        side=str(row["side"]), shares=float(row["quantity"]),
                        price=float(row["price"]), time=exec_time),
                    commissionReport=report))
            trade, recomputed, _error = self._reconstruct_execution_trade(
                cid, inf, fills, terminal_status=evidence["terminal_status"],
                expected_filled=evidence["combo_qty"])
            if trade is None or recomputed != evidence:
                return None
            return trade
        except (KeyError, TypeError, ValueError, OverflowError):
            return None

    def _reconstruct_execution_trade(self, cid: int, inf, fills, *,
                                     terminal_status=None, expected_filled=None):
        """Public API contract; production-derived narrative omitted."""
        snap = dict(getattr(inf, "submitted_close", {}) or {})
        if snap.get("schema") != "submitted_close.v1":
            ctx = dict(getattr(inf, "exit_context", {}) or {})
            if (ctx.get("journal_entry") or {}).get("spread"):
                return None, None, "no frozen submitted-close topology"
            try:
                qty = _durable_qty(ctx, "close_qty", inf.remaining_qty,
                                   where=f"con_id={cid} legacy single execution recovery")
            except _MalformedCloseQty:
                return None, None, "no frozen submitted-close topology"
            action = "BUY" if ctx.get("is_short") else "SELL"
            snap = {"schema": "submitted_close.v1", "sec_type": "OPT", "action": action,
                    "combo_qty": qty,
                    "legs": [{"con_id": cid, "ratio": 1,
                              "expected_side": "BOT" if ctx.get("is_short") else "SLD",
                              "multiplier": 100}]}
        try:
            needed = int(snap["combo_qty"])
            action = str(snap["action"]).upper()
            legs = list(snap["legs"])
        except (KeyError, TypeError, ValueError):
            return None, None, "unusable submitted-close topology"
        if needed <= 0 or action not in {"BUY", "SELL"} or not legs:
            return None, None, "unusable submitted-close quantity/legs"
        expected = {}
        snap_sec_type = str(snap.get("sec_type") or "").upper()
        try:
            for leg in legs:
                con_id = int(leg["con_id"])
                ratio = int(leg["ratio"])
                side = self._execution_side(leg["expected_side"])
                if con_id <= 0 or ratio <= 0 or side is None or con_id in expected:
                    raise ValueError
                expected[con_id] = (ratio, side)
        except (KeyError, TypeError, ValueError):
            return None, None, "unusable frozen leg"

        by_exec = {}
        aggregate_by_exec = {}
        originals = {}
        for fill in list(fills or []):
            if not self._execution_matches_in_flight(fill, inf):
                continue
            ex = getattr(fill, "execution", None)
            contract = getattr(fill, "contract", None)
            exec_id = str(getattr(ex, "execId", "") or "").strip()
            if not exec_id:
                return None, None, "identity-matched execution has no execId"
            try:
                con_id = int(getattr(contract, "conId", 0) or 0)
                qty = float(getattr(ex, "shares", 0))
                price = float(getattr(ex, "price", float("nan")))
            except (TypeError, ValueError):
                return None, None, f"execution {exec_id} has unusable quantity/price"
            side = self._execution_side(getattr(ex, "side", None))
            if not math.isfinite(qty) or qty <= 0 or not qty.is_integer():
                return None, None, f"execution {exec_id} has non-integral quantity {qty!r}"
            if not math.isfinite(price) or price < 0:
                return None, None, f"execution {exec_id} has invalid price {price!r}"
            if con_id not in expected:




                contract_sec_type = str(getattr(contract, "secType", "") or "").upper()
                if snap_sec_type == "BAG" and contract_sec_type == "BAG":
                    aggregate_side = "BOT" if action == "BUY" else "SLD"
                    if side != aggregate_side:
                        return None, None, (f"aggregate execution {exec_id} has wrong side "
                                            f"{side!r}")
                    aggregate = {
                        "exec_id": exec_id, "side": side, "quantity": int(qty),
                        "price": price,
                    }
                    if exec_id in by_exec:
                        return None, None, f"conflicting duplicate execId {exec_id}"
                    if (exec_id in aggregate_by_exec
                            and aggregate_by_exec[exec_id] != aggregate):
                        return None, None, f"conflicting duplicate execId {exec_id}"
                    aggregate_by_exec[exec_id] = aggregate
                    continue
                return None, None, f"execution {exec_id} names unexpected leg {con_id}"
            if side != expected[con_id][1]:
                return None, None, f"execution {exec_id} has wrong side {side!r}"
            commission = None
            try:
                c = float(getattr(getattr(fill, "commissionReport", None), "commission", None))
                if math.isfinite(c) and c != 0.0:
                    commission = c
            except (TypeError, ValueError):
                pass
            row = {"exec_id": exec_id, "con_id": con_id, "side": side,
                   "quantity": int(qty), "price": price,
                   "perm_id": self._positive_int(getattr(ex, "permId", 0)),
                   "order_id": self._positive_int(getattr(ex, "orderId", 0)),
                   "client_id": self._ib_client_id(getattr(ex, "clientId", None)),
                   "commission": commission}
            try:
                _xt = getattr(ex, "time", None)
                row["time"] = _xt.isoformat() if _xt is not None else None
            except Exception:
                row["time"] = None
            if exec_id in aggregate_by_exec:
                return None, None, f"conflicting duplicate execId {exec_id}"
            if exec_id in by_exec and by_exec[exec_id] != row:
                return None, None, f"conflicting duplicate execId {exec_id}"
            by_exec[exec_id] = row
            originals.setdefault(exec_id, fill)
        if not by_exec:
            return None, None, "no identity-matched executions"

        units_by_leg, cash = {}, 0.0
        for con_id, (ratio, side) in expected.items():
            rows = [row for row in by_exec.values() if row["con_id"] == con_id]
            total_qty = sum(row["quantity"] for row in rows)
            if total_qty <= 0 or total_qty % ratio:
                return None, None, f"leg {con_id} has incomplete ratio quantity {total_qty}/{ratio}"
            units_by_leg[con_id] = total_qty // ratio
            cash += sum((1.0 if side == "SLD" else -1.0)
                        * row["quantity"] * row["price"] for row in rows)
        units = set(units_by_leg.values())
        if len(units) != 1:
            return None, None, f"leg units disagree: {units_by_leg!r}"
        filled_units = next(iter(units))
        if aggregate_by_exec:
            aggregate_units = sum(row["quantity"] for row in aggregate_by_exec.values())
            if aggregate_units != filled_units:
                return None, None, (f"aggregate BAG units {aggregate_units} disagree with leg "
                                    f"units {filled_units}")
        terminal_status = str(terminal_status or "Filled")
        if terminal_status not in {"Filled", "Cancelled", "ApiCancelled", "Inactive"}:
            return None, None, f"unsupported terminal execution status {terminal_status!r}"
        if expected_filled is not None:
            try:
                expected_filled = int(expected_filled)
            except (TypeError, ValueError):
                return None, None, "terminal filled quantity is unusable"
            if filled_units != expected_filled:
                return None, None, (f"execution units {filled_units} disagree with broker "
                                    f"terminal filled {expected_filled}")
        if filled_units <= 0 or filled_units > needed:
            return None, None, f"filled units {filled_units} exceed planned {needed}"
        if terminal_status == "Filled" and filled_units != needed:
            return None, None, f"filled units {filled_units} do not equal planned {needed}"
        net = (cash if action == "SELL" else -cash) / filled_units
        if not math.isfinite(net) or net < -1e-9:
            return None, None, f"invalid reconstructed net price {net!r}"
        rows = [by_exec[k] for k in sorted(by_exec)]
        evidence = {"schema": "close_fill_evidence.v1", "source": "ibkr_executions",
                    "sec_type": str(snap.get("sec_type") or "OPT"),
                    "action": action, "combo_qty": filled_units,
                    "planned_combo_qty": needed, "terminal_status": terminal_status,
                    "net_fill_price": max(0.0, net),
                    "formula": ("signed close cash / combo_qty; SELL uses SLD-BOT, "
                                "BUY uses BOT-SLD"),
                    "executions": rows,
                    "fill_ts": max((row.get("time") for row in rows if row.get("time")),
                                   default=None)}
        evidence["binding_sha"] = self._fill_evidence_binding(evidence)
        trade = self._make_reconstructed_trade(
            cid, inf, evidence, [originals[row["exec_id"]] for row in rows])
        return trade, evidence, None

    async def _terminal_trades_for_in_flight(
            self, infs: dict, *, persist_recovery_receipts: bool = True) -> Dict[int, object]:
        """Public API contract; production-derived narrative omitted."""
        targets = []
        for key, inf in infs.items():
            try:
                targets.append((int(key), inf))
            except (TypeError, ValueError):
                continue
        cache_keys = {cid: self._terminal_cache_key(cid, inf) for cid, inf in targets}
        found: Dict[int, object] = {}
        for cid, inf in targets:
            key = cache_keys[cid]
            trade = self._terminal_history_cache.get(key) if key is not None else None
            if trade is not None and self._trade_matches_in_flight(trade, inf):
                found[cid] = trade
        self._terminal_lookup_complete = False

        def _status(tr):
            return getattr(getattr(tr, "orderStatus", None), "status", None)

        def _valid_fill(tr):
            try:
                px = float(getattr(getattr(tr, "orderStatus", None), "avgFillPrice", None))
                return math.isfinite(px) and (px > 0 or bool(
                    getattr(tr, "execution_reconstructed", False)))
            except (TypeError, ValueError):
                return False

        def _terminal(tr):
            if _status(tr) == "Filled":
                return _valid_fill(tr)
            if _status(tr) in {"Cancelled", "ApiCancelled", "Inactive"}:



                return (trade_has_proven_zero_fill(tr)
                        or (self._trade_reported_filled(tr) > 0 and _valid_fill(tr)))
            return False

        def _rank(tr):
            if getattr(tr, "execution_reconstructed", False):
                return 5
            if _status(tr) == "Filled":
                return 4 if _valid_fill(tr) else 2
            return 3 if _status(tr) in {"Cancelled", "ApiCancelled", "Inactive"} else 1

        def _collect(items):
            try:
                seq = list(items or [])
            except Exception:
                return
            for trade in seq:
                for cid, inf in targets:
                    if self._trade_matches_in_flight(trade, inf):
                        if (persist_recovery_receipts
                                and not getattr(inf, "submitted_close", None)):
                            try:
                                ctx = dict(getattr(inf, "exit_context", {}) or {})
                                qty = _durable_qty(ctx, "close_qty", inf.remaining_qty,
                                                   where=f"con_id={cid} recovered topology")
                                inf.submitted_close = submitted_close_snapshot(
                                    getattr(trade, "contract", None),
                                    getattr(trade, "order", None),
                                    parent_con_id=cid, combo_qty=qty)
                            except Exception as exc:
                                print(f"[RECONCILE] con_id={cid}: cannot freeze completed-order "
                                      f"topology ({exc})")
                        if cid not in found or _rank(trade) >= _rank(found[cid]):
                            found[cid] = trade

        ib = getattr(self.ib_conn, "ib", None)
        if ib is None or not targets:
            return found
        for cid, inf in targets:
            trade = self._trade_from_fill_evidence(
                cid, inf, dict(getattr(inf, "fill_evidence", {}) or {}))
            if trade is not None:
                found[cid] = trade
        try:
            _collect(ib.trades())
        except Exception as exc:
            print(f"[WARN] in-flight fill poll: current trade blotter unavailable ({exc})")

        now = time.monotonic()
        timeout = 2.0
        missing = {cid for cid, _ in targets if cid not in found or not _terminal(found[cid])}
        executions_ok = False
        if (missing and now >= self._execution_lookup_next_at
                and hasattr(ib, "reqExecutionsAsync")):
            try:
                fills = list(await asyncio.wait_for(ib.reqExecutionsAsync(), timeout) or [])
                executions_ok = True
                self._execution_lookup_next_at = now + 60.0
                dirty = False
                for cid, inf in targets:
                    if cid not in missing:
                        continue
                    terminal_candidate = found.get(cid)
                    candidate_status = _status(terminal_candidate)






                    terminal_status = (candidate_status if candidate_status in {
                        "Filled", "Cancelled", "ApiCancelled", "Inactive"} else None)
                    expected_filled = (trade_reported_filled(terminal_candidate)
                                       if terminal_status in {
                                           "Cancelled", "ApiCancelled", "Inactive"} else None)
                    trade, evidence, error = self._reconstruct_execution_trade(
                        cid, inf, fills, terminal_status=terminal_status,
                        expected_filled=expected_filled)
                    if trade is None:
                        if error != "no identity-matched executions":
                            print(f"[RECONCILE] con_id={cid}: execution evidence incomplete: {error}")
                        continue
                    if persist_recovery_receipts:
                        inf.fill_evidence = evidence
                    found[cid] = trade
                    dirty = dirty or persist_recovery_receipts
                if dirty and persist_recovery_receipts:
                    self.state_manager.save()
            except Exception as exc:
                self._execution_lookup_next_at = now + 300.0
                print(f"[WARN] execution fill lookup failed ({type(exc).__name__}: "
                      f"{exc or 'no detail'}); retry throttled for 5m")

        missing = {cid for cid, _ in targets if cid not in found or not _terminal(found[cid])}
        completed_ok = False
        if (missing and now >= self._completed_lookup_next_at
                and hasattr(ib, "reqCompletedOrdersAsync")):
            try:
                try:
                    rows = await asyncio.wait_for(
                        ib.reqCompletedOrdersAsync(apiOnly=False), timeout)
                except TypeError:
                    rows = await asyncio.wait_for(ib.reqCompletedOrdersAsync(False), timeout)
                _collect(rows)
                if (persist_recovery_receipts
                        and any(getattr(inf, "submitted_close", None)
                                for _cid, inf in targets)):
                    self.state_manager.save()
                completed_ok = True
                self._completed_lookup_next_at = now + 60.0
            except Exception as exc:
                self._completed_lookup_next_at = now + 300.0
                print(f"[WARN] completed-order lookup failed ({type(exc).__name__}: "
                      f"{exc or 'no detail'}); retry throttled for 5m -- terminal history is "
                      "UNREADABLE, so untransmitted-intent release stays blocked")
        self._terminal_lookup_complete = bool(completed_ok and executions_ok)
        self._terminal_history_cache = {}
        for cid, trade in found.items():
            inf = next((candidate for candidate_cid, candidate in targets
                        if candidate_cid == cid), None)
            final_key = self._terminal_cache_key(cid, inf) if inf is not None else None
            if final_key is not None and _terminal(trade) and self._trade_matches_in_flight(
                    trade, inf):
                self._terminal_history_cache[final_key] = trade
        self._terminal_history_covered_keys = {
            key for cid, inf in targets
            if (key := self._terminal_cache_key(cid, inf)) is not None
        }
        self._terminal_history_refreshed_at = time.monotonic()
        return found

    async def refresh_terminal_history_offcycle(self) -> Dict[int, object]:
        """Public API contract; production-derived narrative omitted."""
        async with self._terminal_history_refresh_lock:
            live_infs = dict(self.state_manager.state.in_flight)
            infs = copy.deepcopy(live_infs)
            if not live_infs:
                self._terminal_history_cache = {}
                self._terminal_history_legacy_cache = {}
                self._terminal_history_covered_keys = set()
                self._terminal_lookup_complete = False
                self._terminal_history_refreshed_at = time.monotonic()
                return {}
            try:
                found = await self._terminal_trades_for_in_flight(
                    infs, persist_recovery_receipts=False)
                legacy = {}
                current = self.state_manager.state.in_flight
                for raw_cid, live_inf in live_infs.items():
                    try:
                        cid = int(raw_cid)
                    except (TypeError, ValueError):
                        continue

                    if (self._terminal_cache_key(cid, live_inf) is None
                            and current.get(str(cid)) is live_inf and cid in found):
                        legacy[cid] = (live_inf, found[cid])
                self._terminal_history_legacy_cache = legacy
                return found
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                self._terminal_lookup_complete = False
                print(f"[WARN] off-cycle terminal-history refresh failed "
                      f"({type(exc).__name__}: {exc or 'no detail'}); close latches retained")
                return {}

    def _cached_terminal_trades_for_in_flight(self, infs: dict):
        """Public API contract; production-derived narrative omitted."""
        found: Dict[int, object] = {}
        complete_for: Set[int] = set()
        age = time.monotonic() - float(self._terminal_history_refreshed_at or 0.0)
        fresh_complete = bool(
            self._terminal_lookup_complete and 0.0 <= age <= self._terminal_history_ttl_s)
        cache = dict(self._terminal_history_cache)
        legacy_cache = dict(self._terminal_history_legacy_cache)
        covered = set(self._terminal_history_covered_keys)



        try:
            current_trades = list(getattr(self.ib_conn, "ib", None).trades() or [])
        except Exception as exc:
            current_trades = []
            print(f"[WARN] cached fill poll: current trade blotter unavailable ({exc})")
        for raw_cid, inf in dict(infs or {}).items():
            try:
                cid = int(raw_cid)
            except (TypeError, ValueError):
                continue
            key = self._terminal_cache_key(cid, inf)
            candidates = [trade for trade in current_trades
                          if self._trade_matches_in_flight(trade, inf)
                          and self._terminal_trade_usable(trade)]
            if key is not None:
                cached = cache.get(key)
                if (cached is not None and self._trade_matches_in_flight(cached, inf)
                        and self._terminal_trade_usable(cached)):
                    candidates.append(cached)
                if fresh_complete and key in covered:
                    complete_for.add(cid)
            else:
                legacy = legacy_cache.get(cid)
                if (legacy is not None and legacy[0] is inf
                        and self._trade_matches_in_flight(legacy[1], inf)
                        and self._terminal_trade_usable(legacy[1])):
                    candidates.append(legacy[1])
            if candidates:
                found[cid] = max(candidates, key=self._terminal_trade_rank)
        return found, complete_for

    @classmethod
    def _terminal_trade_usable(cls, trade) -> bool:
        status = getattr(getattr(trade, "orderStatus", None), "status", None)
        try:
            price = float(getattr(getattr(trade, "orderStatus", None), "avgFillPrice", None))
            valid_fill = math.isfinite(price) and (
                price > 0 or bool(getattr(trade, "execution_reconstructed", False)))
        except (TypeError, ValueError):
            valid_fill = False
        if status == "Filled":
            return valid_fill
        if status in {"Cancelled", "ApiCancelled", "Inactive"}:
            return (trade_has_proven_zero_fill(trade)
                    or (cls._trade_reported_filled(trade) > 0 and valid_fill))
        return False

    @classmethod
    def _terminal_trade_rank(cls, trade) -> int:
        status = getattr(getattr(trade, "orderStatus", None), "status", None)
        if getattr(trade, "execution_reconstructed", False):
            return 5
        if status == "Filled":
            return 4 if cls._terminal_trade_usable(trade) else 2
        return 3 if status in {"Cancelled", "ApiCancelled", "Inactive"} else 1

    @staticmethod
    def _trade_fill_timestamp(trade) -> str:
        try:
            evidence_ts = (getattr(trade, "fill_evidence", None) or {}).get("fill_ts")
            if evidence_ts:
                return str(evidence_ts)
        except Exception:
            pass
        try:
            times = [getattr(getattr(fl, "execution", None), "time", None)
                     for fl in (getattr(trade, "fills", None) or [])]
            times = [t for t in times if t is not None]
            if times:
                return max(times).isoformat()
        except Exception:
            pass
        return datetime.now().astimezone().isoformat()

    @staticmethod
    def _broker_fill_latency_seconds(placed_at, trade) -> Optional[float]:
        """Public API contract; production-derived narrative omitted."""
        try:
            fill_ts = (getattr(trade, "fill_evidence", None) or {}).get("fill_ts")
            if not fill_ts:
                times = [getattr(getattr(fill, "execution", None), "time", None)
                         for fill in (getattr(trade, "fills", None) or [])]
                times = [value for value in times if value is not None]
                if not times:
                    return None
                fill_ts = max(times)
            placed = datetime.fromisoformat(str(placed_at).replace("Z", "+00:00"))
            filled = (fill_ts if isinstance(fill_ts, datetime)
                      else datetime.fromisoformat(str(fill_ts).replace("Z", "+00:00")))
            if placed.tzinfo is None:
                placed = placed.replace(tzinfo=timezone.utc)
            if filled.tzinfo is None:
                filled = filled.replace(tzinfo=timezone.utc)
            seconds = (filled.astimezone(timezone.utc) -
                       placed.astimezone(timezone.utc)).total_seconds()
            return round(seconds, 3) if seconds >= 0 else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _trade_reported_filled(trade) -> float:


        return trade_reported_filled(trade) or 0.0

    def _fill_identity(self, con_id: int, inf, trade) -> str:
        """Public API contract; production-derived narrative omitted."""
        existing = getattr(inf, "fill_key", None)
        if existing:
            return str(existing)
        order = getattr(trade, "order", None)
        perm_id = (self._positive_int(getattr(order, "permId", 0))
                   or self._positive_int(getattr(inf, "perm_id", 0)))
        order_id = (self._positive_int(getattr(order, "orderId", 0))
                    or self._positive_int(getattr(inf, "order_id", 0)))
        trade_client_id = self._ib_client_id(getattr(order, "clientId", None))
        stored_client_id = self._ib_client_id(getattr(inf, "client_id", None))
        client_id = (trade_client_id if trade_client_id is not None else stored_client_id)
        trade_ref = getattr(order, "orderRef", None)
        stored_ref = getattr(inf, "order_ref", None)
        order_ref = (trade_ref if isinstance(trade_ref, str) and trade_ref
                     else stored_ref if isinstance(stored_ref, str) and stored_ref else None)
        inf.perm_id = perm_id
        inf.order_id = order_id
        inf.client_id = client_id
        inf.order_ref = order_ref
        if perm_id:
            key = f"perm:{perm_id}:con:{int(con_id)}"
        elif order_ref:
            key = f"ref:{order_ref}:con:{int(con_id)}"
        elif client_id is not None and order_id:
            key = f"api:{client_id}:order:{order_id}:con:{int(con_id)}"
        else:
            key = f"legacy:order:{order_id}:con:{int(con_id)}"
        inf.fill_key = key
        if perm_id or order_ref or client_id is not None:
            inf.identity_version = 1
        return key

    def _frozen_campaign_for_close(self, con_id: int, inf) -> dict:
        """Public API contract; production-derived narrative omitted."""
        ctx = dict(getattr(inf, "exit_context", {}) or {})
        frozen = ctx.get("campaign_binding")
        if _valid_campaign_binding(frozen):
            return dict(frozen)



        journal_entry = ctx.get("journal_entry") or {}
        campaign = open_campaign(self._journal_campaigns, con_id)
        candidate = _campaign_receipt(campaign)
        if (not _valid_campaign_binding(candidate)
                or _campaign_identity(journal_entry) != tuple(candidate["identity"])
                or _norm_ts(journal_entry.get("ts")) != _norm_ts(candidate["first_lot_ts"])):
            return {}
        inf.exit_context["campaign_binding"] = candidate
        return candidate

    async def _fresh_broker_flat_evidence(self, inf) -> Optional[dict]:
        """Public API contract; production-derived narrative omitted."""
        snapshot = dict(getattr(inf, "submitted_close", {}) or {})
        if not _valid_submitted_close(snapshot):
            return None
        try:
            all_positions = await self.ib_conn.get_positions(
                include_short=True, include_stock=True)
            if not isinstance(all_positions, dict):
                return None
            topology = sorted(int(leg["con_id"]) for leg in snapshot["legs"])
            for cid in topology:
                pos = all_positions.get(cid)
                if pos is None:
                    continue
                quantity = getattr(pos, "quantity", None)
                if quantity is None or int(quantity) != 0:
                    return None
            return {"schema": "position_flat_evidence.v1", "source": "ibkr_reqPositions",
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "absent_con_ids": topology}
        except Exception as exc:
            print(f"[CAMPAIGN] broker-flat proof unavailable for con_id={inf.con_id}: "
                  f"{type(exc).__name__}: {exc or 'no detail'}")
            return None

    @staticmethod
    def _boundary_submitted_close(con_id: int, inf, quantity: int) -> dict:
        """Public API contract; production-derived narrative omitted."""
        snapshot = dict(getattr(inf, "submitted_close", {}) or {})
        if snapshot:
            return snapshot
        ctx = dict(getattr(inf, "exit_context", {}) or {})
        if (ctx.get("journal_entry") or {}).get("spread"):
            return {}
        action = "BUY" if ctx.get("is_short") else "SELL"
        return {
            "schema": "submitted_close.v1", "sec_type": "OPT", "action": action,
            "combo_qty": int(quantity),
            "legs": [{"con_id": int(con_id), "ratio": 1,
                      "expected_side": "BOT" if action == "BUY" else "SLD",
                      "multiplier": 100}],
            "order_type": "", "tif": "", "limit_price": None, "stop_price": None,
        }

    def _campaign_boundary_committed(self, con_id: int, inf, *, quantity: int,
                                     campaign: dict) -> bool:
        """Public API contract; production-derived narrative omitted."""
        fill_key = str(getattr(inf, "fill_key", None) or "")
        snapshot = self._boundary_submitted_close(con_id, inf, quantity)
        if not fill_key or not _valid_campaign_binding(campaign) or not snapshot:
            return False
        path = Path(self.config.journal.path)
        try:
            with open(path) as stream:
                for line in stream:
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    if (row.get("event") == "position_closed"
                            and row.get("fill_key") == fill_key
                            and _terminal_manager_marker(row)
                            and row.get("contract_id") == int(con_id)
                            and row.get("filled_quantity") == int(quantity)
                            and row.get("submitted_close") == snapshot
                            and row.get("campaign") == campaign
                            and row.get("order_id") == int(getattr(inf, "order_id", 0) or 0)
                            and row.get("perm_id") == int(getattr(inf, "perm_id", 0) or 0)
                            and row.get("client_id") == getattr(inf, "client_id", None)
                            and row.get("order_ref") == getattr(inf, "order_ref", None)):
                        return True
            return False
        except (OSError, json.JSONDecodeError, TypeError, ValueError, OverflowError):
            return False

    def _append_campaign_close_boundary(self, con_id: int, inf, trade, *,
                                        quantity: int, position_qty: int,
                                        symbol: str, campaign: dict,
                                        broker_flat_evidence: dict) -> bool:
        """Public API contract; production-derived narrative omitted."""
        try:
            quantity = int(quantity)
            position_qty = int(position_qty)
            if quantity <= 0 or quantity != position_qty:
                raise ValueError(
                    f"full-close boundary requires exact quantity, got {quantity}/{position_qty}")
            snapshot = self._boundary_submitted_close(con_id, inf, quantity)
            if (snapshot.get("schema") != "submitted_close.v1"
                    or int(snapshot.get("combo_qty") or 0) != quantity
                    or not list(snapshot.get("legs") or [])):
                raise ValueError("exact submitted-close topology is unavailable")
            fill_key = str(getattr(inf, "fill_key", None) or "")
            if not fill_key:
                raise ValueError("fill identity is unavailable")
            if not isinstance(campaign, dict) or not campaign:
                raise ValueError("closed journal campaign binding is unavailable")
            status = str(getattr(getattr(trade, "orderStatus", None), "status", "") or "")
            marker = {
                "event": "position_closed", "schema": "position_closed.v1",
                "ts": self._trade_fill_timestamp(trade), "contract_id": int(con_id),
                "symbol": str(symbol or "").upper(), "status": status,
                "fill_complete": True, "topology_complete": True,
                "broker_flat_confirmed": True,
                "broker_flat_evidence": broker_flat_evidence,
                "filled_quantity": quantity, "fill_key": fill_key,
                "order_id": int(getattr(inf, "order_id", 0) or 0),
                "perm_id": int(getattr(inf, "perm_id", 0) or 0),
                "client_id": getattr(inf, "client_id", None),
                "order_ref": getattr(inf, "order_ref", None),
                "submitted_close": snapshot, "campaign": campaign,
                "fill_evidence": (dict(getattr(inf, "fill_evidence", {}) or {}) or None),
            }
            marker["binding_sha"] = hashlib.sha256(json.dumps(
                marker, sort_keys=True, separators=(",", ":"),
                allow_nan=False).encode()).hexdigest()
            if not _terminal_manager_marker(marker):
                raise ValueError("candidate campaign boundary fails semantic validation")
            path = Path(self.config.journal.path)
            encoded = (json.dumps(marker, sort_keys=True, separators=(",", ":"),
                                  allow_nan=False) + "\n").encode()
            from exitmgr.journal_io import journal_lock
            with journal_lock(path):
                if path.exists():
                    with open(path) as stream:
                        for line in stream:
                            try:
                                prior = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            if (prior.get("event") == "position_closed"
                                    and prior.get("fill_key") == fill_key):
                                exact_identity = all(prior.get(key) == marker.get(key) for key in (
                                    "contract_id", "filled_quantity", "order_id", "perm_id",
                                    "client_id", "order_ref", "submitted_close", "campaign"))
                                if not exact_identity or not _terminal_manager_marker(prior):
                                    raise ValueError(
                                        "conflicting campaign boundary already exists for fill")
                                return True
                fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                try:
                    written = os.write(fd, encoded)
                    if written != len(encoded):
                        raise OSError(f"short boundary append {written}/{len(encoded)}")
                    os.fsync(fd)
                finally:
                    os.close(fd)


                with open(path) as stream:
                    verified = [json.loads(line) for line in stream if line.strip()]
                if not any(row.get("binding_sha") == marker["binding_sha"]
                           and _terminal_manager_marker(row) for row in verified):
                    raise OSError("campaign boundary append did not read back exactly")
                return True
        except Exception as exc:
            print(f"[CAMPAIGN] [ALERT] con_id={con_id}: full-fill boundary write failed "
                  f"({exc}); retaining durable close state")
            return False

    def _campaign_conflict_resolution_committed(self, con_id: int, inf, *, quantity: int,
                                                authority: dict) -> bool:
        """Public API contract; production-derived narrative omitted."""
        fill_key = str(getattr(inf, "fill_key", None) or "")
        snapshot = self._boundary_submitted_close(con_id, inf, quantity)
        if not fill_key or not _valid_campaign_conflict_authority(authority) or not snapshot:
            return False
        try:
            with open(self.config.journal.path) as stream:
                for line in stream:
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    if (row.get("event") == "campaign_conflict_position_closed"
                            and row.get("fill_key") == fill_key
                            and row.get("contract_id") == int(con_id)
                            and row.get("filled_quantity") == int(quantity)
                            and row.get("submitted_close") == snapshot
                            and row.get("conflict_authority") == authority
                            and row.get("order_id") == int(getattr(inf, "order_id", 0) or 0)
                            and row.get("perm_id") == int(getattr(inf, "perm_id", 0) or 0)
                            and row.get("client_id") == getattr(inf, "client_id", None)
                            and row.get("order_ref") == getattr(inf, "order_ref", None)
                            and _terminal_campaign_conflict_marker(row)):
                        return True
            return False
        except (OSError, json.JSONDecodeError, TypeError, ValueError, OverflowError):
            return False

    def _append_campaign_conflict_resolution(self, con_id: int, inf, trade, *,
                                             quantity: int, position_qty: int, symbol: str,
                                             authority: dict,
                                             broker_flat_evidence: dict) -> bool:
        """Public API contract; production-derived narrative omitted."""
        try:
            quantity, position_qty = int(quantity), int(position_qty)
            if quantity <= 0 or quantity != position_qty:
                raise ValueError("campaign-conflict resolution requires an exact full close")
            if not _valid_campaign_conflict_authority(authority):
                raise ValueError("campaign-conflict authority is invalid")
            if authority["fingerprint"] != (self._campaign_conflicts.get(
                    int(con_id)) or {}).get("fingerprint"):
                raise ValueError("active campaign-conflict fingerprint changed")
            snapshot = self._boundary_submitted_close(con_id, inf, quantity)
            if (not _valid_submitted_close(snapshot)
                    or int(snapshot.get("combo_qty") or 0) != quantity):
                raise ValueError("exact submitted-close topology is unavailable")
            fill_key = str(getattr(inf, "fill_key", None) or "")
            if not fill_key:
                raise ValueError("fill identity is unavailable")
            status = str(getattr(getattr(trade, "orderStatus", None), "status", "") or "")
            marker = {
                "event": "campaign_conflict_position_closed",
                "schema": "campaign_conflict_position_closed.v1",
                "source": "emergency_hard_stop",
                "ts": self._trade_fill_timestamp(trade),
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "contract_id": int(con_id), "symbol": str(symbol or "").upper(),
                "status": status, "fill_complete": True, "topology_complete": True,
                "broker_flat_confirmed": True,
                "broker_flat_evidence": broker_flat_evidence,
                "filled_quantity": quantity, "fill_key": fill_key,
                "order_id": int(getattr(inf, "order_id", 0) or 0),
                "perm_id": int(getattr(inf, "perm_id", 0) or 0),
                "client_id": getattr(inf, "client_id", None),
                "order_ref": getattr(inf, "order_ref", None),
                "submitted_close": snapshot, "conflict_authority": authority,
                "fill_evidence": (dict(getattr(inf, "fill_evidence", {}) or {}) or None),
            }
            marker["binding_sha"] = _canonical_sha256(marker)
            if not _terminal_campaign_conflict_marker(marker):
                raise ValueError("candidate campaign-conflict resolution fails validation")
            path = Path(self.config.journal.path)
            from exitmgr.journal_io import journal_lock
            with journal_lock(path):
                rows = [json.loads(line) for line in path.read_text().splitlines()
                        if line.strip()]
                matching_lots = [row for row in rows
                                 if row.get("contract_id") == int(con_id)
                                 and not row.get("event")]
                if (not matching_lots or _canonical_sha256(matching_lots[-1])
                        != authority["quarantined_row_sha256"]):
                    raise ValueError("quarantined row changed or a later re-entry exists")
                if open_campaign(build_journal_campaigns(rows), con_id) is not None:
                    raise ValueError("an open campaign now owns this contract")
                for prior in rows:
                    if (prior.get("event") == "campaign_conflict_position_closed"
                            and prior.get("fill_key") == fill_key):
                        if not (_terminal_campaign_conflict_marker(prior)
                                and prior.get("conflict_authority") == authority
                                and prior.get("submitted_close") == snapshot):
                            raise ValueError("conflicting emergency-close receipt exists")
                        return True
                encoded = (json.dumps(marker, sort_keys=True, separators=(",", ":"),
                                      allow_nan=False) + "\n").encode()
                fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                try:
                    if os.write(fd, encoded) != len(encoded):
                        raise OSError("short campaign-conflict resolution append")
                    os.fsync(fd)
                finally:
                    os.close(fd)
                verified = [json.loads(line) for line in path.read_text().splitlines()
                            if line.strip()]
                if not any(row.get("binding_sha") == marker["binding_sha"]
                           and _terminal_campaign_conflict_marker(row) for row in verified):
                    raise OSError("campaign-conflict resolution did not read back exactly")
            return True
        except Exception as exc:
            print(f"[CAMPAIGN-CONFLICT] con_id={con_id}: emergency close receipt failed "
                  f"({exc}); retaining durable close state")
            return False

    def recover_archived_close(self, receipt: dict, xml_bytes: bytes, *, source_bindings: dict,
                               positions, order_leg_con_ids, observed_at, current_account_id=None,
                               apply: bool = False, observation_provider=None) -> dict:
        """Public API contract; production-derived narrative omitted."""
        from contextlib import nullcontext
        from dataclasses import asdict
        from types import SimpleNamespace
        from exitmgr.flex_close_recovery import (
            FlexCloseRefused, digest, validate_receipt, check_commit_preconditions)
        from exitmgr.journal_io import journal_lock
        from exitmgr.order_lock import order_mutation_lock

        def need(condition, reason):
            if not condition:
                raise FlexCloseRefused(reason)

        try:
            need(type(apply) is bool, "apply must be an explicit boolean")
            if apply:
                need(self.state_manager.persist is True, "read-only manager cannot commit recovery")
                need(callable(observation_provider), "apply requires a native observation provider under the host lock")
            with (order_mutation_lock() if apply else nullcontext()):
                with (journal_lock(self.config.journal.path) if apply else nullcontext()):
                    if apply:
                        observation = observation_provider()
                        need(isinstance(observation, dict), "native observation provider returned UNKNOWN")
                        positions = observation.get("positions")
                        order_leg_con_ids = observation.get("order_leg_con_ids")
                        observed_at = observation.get("observed_at")
                        current_account_id = observation.get("current_account_id")
                    proof = validate_receipt(receipt, xml_bytes, **source_bindings)
                    need(isinstance(current_account_id, str) and current_account_id
                         and current_account_id == source_bindings.get("account_id"),
                         "current native account is missing or differs from archive account")
                    cid, qty = proof["con_id"], proof["combo_qty"]
                    fill_key = _flex_fill_identity(proof)
                    expected_entry = source_bindings["entry_snapshot"]
                    expected_campaign = source_bindings["campaign_binding"]
                    expected_inf = source_bindings.get("inflight_snapshot")
                    path = Path(self.config.journal.path)
                    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
                    campaigns = build_journal_campaigns(rows).get(cid) or []
                    need(bool(campaigns), "journal campaign is missing")
                    latest = campaigns[-1]
                    need(_campaign_receipt(latest) == expected_campaign,
                         "a different or later journal campaign owns this contract")
                    need(latest.as_journal_row() == expected_entry, "current journal entry changed")
                    prior = [row for row in rows if row.get("fill_key") == fill_key]
                    need(len(prior) <= 1, "duplicate archive campaign boundaries")
                    marker = prior[0] if prior else None
                    if marker is not None:
                        need(_terminal_flex_marker(marker)
                             and marker["fill_evidence"] == proof
                             and marker["campaign"] == expected_campaign,
                             "archive campaign boundary conflicts with this source receipt")

                    disk = json.loads(Path(self.config.state.path).read_text())
                    need((disk.get("campaign_bindings") or {}).get(str(cid)) == expected_campaign
                         and self.state_manager.state.campaign_bindings.get(str(cid)) == expected_campaign,
                         "durable or resident campaign binding changed")
                    disk_inf = (disk.get("in_flight") or {}).get(str(cid))
                    live_inf = self.state_manager.state.get_in_flight(cid)
                    memory_inf = asdict(live_inf) if live_inf is not None else None
                    need(disk_inf == memory_inf, "owner memory and durable latch differ")


                    released_replay = bool(marker is not None and expected_inf is not None
                                           and disk_inf is None)
                    need(disk_inf == expected_inf or released_replay,
                         "durable close latch changed or unexpectedly appeared")
                    if expected_inf is not None:
                        ctx = expected_inf.get("exit_context") or {}
                        need(_durable_qty(ctx, "close_qty", expected_inf["remaining_qty"],
                                          where="Flex close") == qty
                             and _durable_qty(ctx, "position_qty", qty, where="Flex close") == qty,
                             "partial or changed close context is unsupported")
                    else:
                        ctx = {}
                    check_commit_preconditions(proof, entry_snapshot=expected_entry,
                        campaign_binding=expected_campaign,
                        inflight_snapshot=(expected_inf if released_replay else disk_inf),
                        positions=positions, order_leg_con_ids=order_leg_con_ids,
                        observed_at=observed_at, now=datetime.now(timezone.utc))


                    exits_path = Path(self._exits_log_path())
                    exits = ([json.loads(line) for line in exits_path.read_text().splitlines()
                              if line.strip()] if exits_path.exists() else [])
                    exact_exits = [row for row in exits if row.get("close_identity") == fill_key]
                    need(len(exact_exits) <= 1, "duplicate archive exit ledger rows")
                    for row in exits:
                        if (str(row.get("contract_id")) == str(cid)
                                and row.get("fill_status") == "Filled"
                                and not str(row.get("order_ref") or "").strip()):
                            raise ValueError("unbound filled ledger row for this contract; adjudication required")
                        if (str(row.get("contract_id")) == str(cid)
                                and row.get("order_ref") == proof["close_order_ref"]):
                            need(row.get("close_identity") == fill_key,
                                 "this close already has a different ledger identity; adjudication required")
                    def valid_exit(row):
                        need(row.get("fill_evidence") == proof and row.get("fill_status") == "Filled"
                             and row.get("fill_evidence_source") == "ibkr_flex"
                             and row.get("contract_id") == cid
                             and row.get("order_ref") == proof["close_order_ref"]
                             and row.get("close_ts") == proof["fill_ts"]
                             and row.get("ts") == proof["fill_ts"]
                             and row.get("quantity") == qty
                             and row.get("exit_price_per_share") == round(float(proof["net_fill_price"]), 4)
                             and row.get("realized_pnl") == round(float(proof["gross_pnl"]), 2)
                             and row.get("realized_pnl_net") == round(float(proof["reconstructed_net_pnl"]), 2)
                             and row.get("entry_commission") == round(float(proof["entry_commission_cost"]), 4)
                             and row.get("exit_commission") == float(proof["exit_commission_cost"]),
                             "archive exit ledger conflicts with the verified receipt")
                    if exact_exits:
                        valid_exit(exact_exits[0])
                    need(marker is None or bool(exact_exits), "boundary has no matching durable exit ledger")
                    result = {"status": "preview", "source": "ibkr_flex", "con_id": cid,
                              "fill_key": fill_key, "fill_ts": proof["fill_ts"],
                              "quantity": qty, "net_fill_price": proof["net_fill_price"],
                              "reconstructed_net_pnl": proof["reconstructed_net_pnl"]}
                    if not apply:
                        return result



                    recorded_at = datetime.now(timezone.utc).isoformat()
                    entry = dict(expected_entry)
                    entry["entry_commission"] = float(proof["entry_commission_cost"])
                    extra = dict(close_identity=fill_key, order_ref=proof["close_order_ref"],
                        fill_status="Filled", terminal_order_status="Filled",
                        fill_evidence_source="ibkr_flex", fill_evidence=proof,
                        fill_ts=proof["fill_ts"], recovery_recorded_at=recorded_at,
                        exit_commission=float(proof["exit_commission_cost"]), partial=False,
                        close_qty=qty, remaining_qty=0,
                        code_version=ctx.get("code_version") or "",
                        policy_version=ctx.get("policy_version") or "",
                        execution_authority_source=("persisted_close_context" if expected_inf else "unknown"))
                    if expected_inf is not None:
                        extra["native_submission_context"] = {key: expected_inf.get(key)
                            for key in ("order_id", "perm_id", "client_id", "order_ref", "placed_at")}
                    trigger = SimpleNamespace(trigger_type="archive_recovery", pnl_pct=0.0,
                        message="Verified historical IBKR Flex close", reload=False)
                    need(self._log_exit(cid, entry["symbol"], trigger,
                        exit_price_per_share=float(proof["net_fill_price"]), quantity=qty,
                        reason="archive_recovery", extra=extra, entry_debit=float(proof["entry_debit"]),
                        je=entry, defer_position_clear=True, historical_fill_ts=proof["fill_ts"]),
                        "archive exit ledger/dataset checkpoint failed")
                    durable_exits = [json.loads(line) for line in exits_path.read_text().splitlines() if line.strip()]
                    durable_matches = [row for row in durable_exits if row.get("close_identity") == fill_key]
                    need(len(durable_matches) == 1, "archive exit ledger did not read back exactly once")
                    valid_exit(durable_matches[0])
                    if marker is None:
                        legs = sorted(leg["con_id"] for leg in proof["close_topology"]["legs"])
                        marker = dict(event="position_closed", schema="position_closed.flex.v1",
                            source="ibkr_flex", ts=proof["fill_ts"], recorded_at=recorded_at,
                            contract_id=cid, symbol=entry["symbol"], filled_quantity=qty,
                            fill_key=fill_key, order_ref=proof["close_order_ref"],
                            campaign=expected_campaign, close_topology=proof["close_topology"],
                            fill_evidence=proof, broker_flat_evidence=dict(
                                schema="archived_close_broker_view.v1",
                                source="ibkr_reqPositions+reqAllOpenOrders",
                                observed_at=observed_at.isoformat(), absent_con_ids=legs,
                                no_resting_order_con_ids=legs))
                        marker["binding_sha"] = digest(marker)
                        need(_terminal_flex_marker(marker), "archive boundary semantic validation failed")
                        encoded = (json.dumps(marker, sort_keys=True, separators=(",", ":"),
                                              allow_nan=False) + "\n").encode()
                        fd = os.open(path, os.O_WRONLY | os.O_APPEND)
                        try:
                            need(os.write(fd, encoded) == len(encoded), "short archive boundary append")
                            os.fsync(fd)
                        finally:
                            os.close(fd)
                    verified = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
                    need(sum(row.get("binding_sha") == marker["binding_sha"]
                             and _terminal_flex_marker(row) for row in verified) == 1,
                         "archive boundary did not read back exactly")

                    prior_state = copy.deepcopy(self.state_manager.state)
                    try:
                        self.state_manager.state.clear_position_tracking(cid, clear_campaign_binding=False)
                        if expected_inf is not None and not released_replay:
                            self.state_manager.state.remove_in_flight(cid)
                        self.state_manager.save()
                    except Exception:

                        self.state_manager._state = prior_state
                        raise
                    self._journal_entries.pop(cid, None)
                    result["status"] = "already_committed" if released_replay else "committed"
                    return result
        except Exception as exc:
            return {"status": "refused", "source": "ibkr_flex", "reason": str(exc)}

    def _finalize_in_flight_exit(self, con_id: int, inf, trade,
                                 broker_flat_evidence: Optional[dict] = None) -> bool:
        """Public API contract; production-derived narrative omitted."""
        try:
            order_status = getattr(trade, "orderStatus", None)
            status = getattr(order_status, "status", None)
            terminal_cancel = status in {"Cancelled", "ApiCancelled", "Inactive"}
            if status != "Filled" and not terminal_cancel:
                return False
            if not self._trade_matches_in_flight(trade, inf):
                print(f"[ALERT] Refusing terminal evidence that does not match the durable close "
                      f"for con_id={con_id} order_id={getattr(inf, 'order_id', 0)}")
                return False

            ctx = dict(getattr(inf, "exit_context", {}) or {})
            if not ctx:
                print(f"[ALERT] con_id={con_id}: terminal broker status has no durable exit "
                      "context; retaining the latch because price/basis/campaign cannot be proven")
                return False




            try:
                planned_qty = _durable_qty(ctx, "close_qty", inf.remaining_qty,
                                           where=f"con_id={con_id} order_id={inf.order_id}")
            except _MalformedCloseQty as _mq:
                print(f"[ALERT] {_mq} -- NOT booking this fill and NOT releasing the durable "
                      "close; an operator must correct the record")
                return False
            if planned_qty <= 0:
                print(f"[WARN] Terminal order_id={inf.order_id} has no durable close quantity; retaining")
                return False
            if status == "Filled":
                qty = planned_qty
            else:
                qty = min(planned_qty, int(self._trade_reported_filled(trade)))
                if qty <= 0:
                    return False

            fill_px = getattr(order_status, "avgFillPrice", None)
            try:
                fill_px = float(fill_px)



                _execution_proven = bool(getattr(trade, "execution_reconstructed", False))
                if fill_px != fill_px or fill_px < 0 or (fill_px == 0 and not _execution_proven):
                    raise ValueError("invalid fill price")
            except (TypeError, ValueError):
                print(f"[WARN] Filled order_id={inf.order_id} has no valid avgFillPrice; "
                      "retaining in-flight for a later broker read")
                return False

            import types as _types
            trig = _types.SimpleNamespace(
                trigger_type=ctx.get("trigger_type") or ctx.get("reason") or "exit",
                pnl_pct=float(ctx.get("trigger_pnl_pct") or 0.0),
                message=ctx.get("trigger_message") or "asynchronous exit fill",
                reload=bool(ctx.get("reload", False)),
                reload_conviction=ctx.get("reload_conviction"),
            )
            try:
                planned_basis = float(ctx.get("entry_debit", inf.entry_debit))
            except (TypeError, ValueError):
                planned_basis = float(inf.entry_debit)
            basis = planned_basis * qty / planned_qty
            je = dict(ctx.get("journal_entry") or self._journal_entries.get(con_id) or {})
            extra = dict(ctx.get("extra") or {})












            _short_close = (bool(ctx.get("is_short"))
                            or str(ctx.get("close_action") or "").upper() == "BUY"
                            or self._is_credit_row(je))
            try:
                if basis > 0:
                    trig.pnl_pct = ((basis - fill_px * 100 * qty) / basis * 100 if _short_close
                                    else (fill_px * 100 * qty - basis) / basis * 100)
            except (TypeError, ValueError):
                pass
            if _short_close:


                extra.setdefault("is_short", True)
            try:
                position_qty = _durable_qty(ctx, "position_qty", planned_qty,
                                            where=f"con_id={con_id} order_id={inf.order_id}")
            except _MalformedCloseQty as _mq:
                print(f"[ALERT] {_mq} -- NOT booking this fill; a zero recorded position "
                      "size would mark a PARTIAL close as a full exit and zero the runner")
                return False
            full_position_closed = qty >= position_qty
            partial = bool(extra.get("partial")) or not full_position_closed
            fill_key = self._fill_identity(con_id, inf, trade)
            _closed_campaign = self._frozen_campaign_for_close(con_id, inf)
            fill_ts = self._trade_fill_timestamp(trade)
            telemetry = self._protective_fill_telemetry(
                ctx, fill_price=fill_px, quantity=qty, basis=basis,
                short_close=_short_close, fill_ts=fill_ts)


            self.state_manager.save()
            extra.update({
                "order_id": inf.order_id,
                "perm_id": getattr(inf, "perm_id", 0),
                "client_id": getattr(inf, "client_id", 0),
                "order_ref": getattr(inf, "order_ref", None),
                "close_identity": fill_key,
                "fill_status": "Filled",
                "terminal_order_status": status,
                "avg_fill_price": fill_px,
                "fill_ts": fill_ts,
                "fill_latency_seconds": self._broker_fill_latency_seconds(
                    getattr(inf, "placed_at", None), trade),
                "submitted_order_type": (getattr(inf, "submitted_close", {}) or {}).get(
                    "order_type"),
                "submitted_limit_price": (getattr(inf, "submitted_close", {}) or {}).get(
                    "limit_price"),
                "fill_evidence": (dict(getattr(inf, "fill_evidence", {}) or {}) or None),
                "fill_evidence_binding": (dict(getattr(inf, "fill_evidence", {}) or {}).get(
                    "binding_sha")),
                "exit_commission": commission_from_trade(trade),
                "exit_model_identity": ctx.get("exit_model_identity"),
                "exit_model_identity_source": ctx.get("exit_model_identity_source"),
                "code_version": ctx.get("code_version"),
                "policy_version": ctx.get("policy_version"),
                "partial": partial,
                "close_qty": qty,
                "remaining_qty": max(0, position_qty - qty),
            })
            extra.update(telemetry)
            ok = self._log_exit(
                con_id, ctx.get("symbol") or je.get("symbol") or "",
                trig, exit_price_per_share=fill_px, quantity=qty,
                reason=ctx.get("reason") or self._exit_reason(trig),
                extra=extra, entry_debit=basis, je=je, defer_position_clear=True,
            )
            if not ok:
                return False

            effects = inf.side_effects
            if not effects.get("ledger"):
                effects["ledger"] = True
                self.state_manager.save()




            _hard_stop = str(ctx.get("trigger_type") or "").lower() in {
                "stop", "stop_loss", "hard_stop", "hard_loss"}
            if (_hard_stop and float(telemetry.get(
                    "adverse_slippage_beyond_threshold_pct_points") or 0.0) > 0
                    and not effects.get("hard_stop_breach_alert")):
                if not self._ensure_hard_stop_breach_alert(
                        symbol=ctx.get("symbol") or je.get("symbol") or "",
                        con_id=con_id, fill_key=fill_key, telemetry=telemetry, inf=inf):



                    return False

            _conflict_authority = ctx.get("campaign_conflict_authority")
            if _conflict_authority is not None:




                if not _hard_stop or not _valid_campaign_conflict_authority(_conflict_authority):
                    print(f"[CAMPAIGN-CONFLICT] con_id={con_id}: invalid emergency-close "
                          "authority; retaining durable close state")
                    return False
                conflict_committed = (self._campaign_conflict_resolution_committed(
                    con_id, inf, quantity=qty, authority=_conflict_authority)
                    if full_position_closed else False)
                if (full_position_closed and effects.get("campaign_conflict_resolution")
                        and not conflict_committed):
                    print(f"[CAMPAIGN-CONFLICT] con_id={con_id}: state claims a resolution "
                          "without its exact receipt; retaining durable close state")
                    return False
                if full_position_closed and not conflict_committed:
                    if status != "Filled" or not isinstance(broker_flat_evidence, dict):
                        return False
                    if not self._append_campaign_conflict_resolution(
                            con_id, inf, trade, quantity=qty, position_qty=position_qty,
                            symbol=ctx.get("symbol") or je.get("symbol") or "",
                            authority=_conflict_authority,
                            broker_flat_evidence=broker_flat_evidence):
                        return False
                    if not self._campaign_conflict_resolution_committed(
                            con_id, inf, quantity=qty, authority=_conflict_authority):
                        return False
                    effects["campaign_conflict_resolution"] = True
                    self.state_manager.save()
                elif full_position_closed and conflict_committed and not effects.get(
                        "campaign_conflict_resolution"):
                    effects["campaign_conflict_resolution"] = True
                    self.state_manager.save()
            else:
                boundary_committed = (self._campaign_boundary_committed(
                    con_id, inf, quantity=qty, campaign=_closed_campaign)
                    if full_position_closed else False)
                if (full_position_closed and effects.get("campaign_boundary")
                        and not boundary_committed):
                    print(f"[CAMPAIGN] [ALERT] con_id={con_id}: state claims a campaign boundary "
                          "that the exact journal marker does not prove; retaining all close state")
                    return False
                if full_position_closed and not boundary_committed:
                    if (status != "Filled" or not _valid_campaign_binding(_closed_campaign)
                            or not isinstance(broker_flat_evidence, dict)):
                        print(f"[CAMPAIGN] con_id={con_id}: fill booked but fresh exact broker-flat "
                              "proof/campaign binding is unavailable; retaining durable close state")
                        return False
                    if not self._append_campaign_close_boundary(
                            con_id, inf, trade, quantity=qty, position_qty=position_qty,
                            symbol=ctx.get("symbol") or je.get("symbol") or "",
                            campaign=_closed_campaign,
                            broker_flat_evidence=broker_flat_evidence):
                        return False
                    if not self._campaign_boundary_committed(
                            con_id, inf, quantity=qty, campaign=_closed_campaign):
                        print(f"[CAMPAIGN] [ALERT] con_id={con_id}: appended boundary did not "
                              "survive exact semantic re-read; retaining close state")
                        return False
                    effects["campaign_boundary"] = True
                    self.state_manager.save()
                elif full_position_closed and boundary_committed and not effects.get(
                        "campaign_boundary"):
                    effects["campaign_boundary"] = True
                    self.state_manager.save()

            if ctx.get("trigger_type") == "scale_out" and not effects.get("scale_out"):
                self.state_manager.state.scaled_out[str(con_id)] = True
                effects["scale_out"] = True
                self.state_manager.save()
            if not effects.get("reload"):
                reload_ok = self._maybe_write_reload_ticket(
                    con_id, ctx.get("symbol") or je.get("symbol") or "", trig,
                    position_qty, planned_basis, fill_px,
                    ("Filled" if full_position_closed and status == "Filled" else None),
                    fill_key=fill_key, je=je)
                if not reload_ok:
                    return False
                effects["reload"] = True
                self.state_manager.save()
            if (ctx.get("manual_request") and full_position_closed
                    and not effects.get("manual_clear")):
                if not self._clear_manual_exit(con_id, ctx.get("manual_request_id")):
                    return False
                effects["manual_clear"] = True
                self.state_manager.save()
            if full_position_closed and not effects.get("position_clear"):
                self._clear_closed_position(con_id)
                effects["position_clear"] = True
                self.state_manager.save()
            if not effects.get("alert"):
                slack_key = str(uuid.uuid5(uuid.NAMESPACE_URL, fill_key))
                if not self._post_exit_alert(
                        ctx.get("symbol") or je.get("symbol") or "", trig,
                        client_msg_id=slack_key):
                    return False
                effects["alert"] = True
                self.state_manager.save()
            self.state_manager.state.remove_in_flight(con_id)
            self.state_manager.save()
            print(f"[EXIT-FINALIZED] con_id={con_id} order_id={inf.order_id} "
                  f"{status}, filled_qty={qty} @ {fill_px:.4f} (durable, deduped)")
            return True
        except Exception as e:
            print(f"[WARN] in-flight finalization failed for con_id={con_id}: {e}; retaining state")
            return False

    def _intent_release_evidence(self, cid: int, inf, live_positions,
                                 min_age_s: float) -> Dict[str, object]:
        """Public API contract; production-derived narrative omitted."""
        ev: Dict[str, object] = {"ok": False, "unchanged": False, "old_enough": False,
                                 "age_s": 0.0, "live_qty": None, "why": ""}
        if live_positions is None:
            ev["why"] = "position book was not read this cycle"
            return ev
        if cid not in live_positions:
            ev["why"] = "position is absent from the position book (it may already be closed)"
            return ev
        pos = live_positions[cid]
        live_qty = (getattr(pos, "quantity", None) if not isinstance(pos, dict)
                    else pos.get("qty"))
        ev["live_qty"] = live_qty
        try:



            ev["unchanged"] = int(live_qty) == _durable_qty(
                (inf.exit_context or {}), "position_qty", inf.remaining_qty,
                where=f"con_id={cid} intent-release")
        except Exception as e:
            ev["why"] = f"position quantity is unusable ({type(e).__name__}: {e})"
            return ev
        try:
            placed = datetime.fromisoformat(str(inf.placed_at).replace("Z", "+00:00"))
            if placed.tzinfo is None:
                placed = placed.replace(tzinfo=timezone.utc)
            ev["age_s"] = ((datetime.now(timezone.utc) - placed.astimezone(timezone.utc))
                           .total_seconds())
        except Exception as e:
            ev["why"] = f"placed_at is unparseable ({type(e).__name__}: {e})"
            return ev
        ev["old_enough"] = bool(ev["age_s"] >= min_age_s)
        if not ev["unchanged"]:
            ev["why"] = (f"position quantity changed (live {live_qty}) -- part of this close "
                         "may have filled")
        elif not ev["old_enough"]:
            ev["why"] = f"intent is only {ev['age_s']:.0f}s old (< {min_age_s:.0f}s grace)"
        else:
            ev["why"] = "clean"
        ev["ok"] = bool(ev["unchanged"] and ev["old_enough"])
        return ev

    def _page_unresolvable_intent(self, cid: int, inf, passes: int,
                                  ev: Dict[str, object], released: bool) -> None:
        """Public API contract; production-derived narrative omitted."""
        try:
            key = int(cid)
        except (TypeError, ValueError):
            key = cid
        try:
            _ctx = getattr(inf, "exit_context", None) or {}
            sym = ((self._journal_entries.get(key) or {}).get("symbol")
                   or _ctx.get("symbol") or "?")
            ident = (f"order_id={getattr(inf, 'order_id', 0)}"
                     if getattr(inf, "order_id", 0)
                     else f"order_ref={getattr(inf, 'order_ref', None) or 'none'}")
            if released:
                head = (":rotating_light: *UNTRANSMITTED CLOSE INTENT RELEASED* — the stop is "
                        "being re-armed on positive evidence, NOT on a guess")
                tail = ("_The open-order book was READ and holds nothing for this con_id and the "
                        "position is unchanged, so there is nothing to duplicate. Verify no "
                        "duplicate close appears in TWS._")
            else:
                head = (":rotating_light: *POSITION MAY BE UNPROTECTED* — a close intent cannot "
                        "be resolved against the broker")
                tail = ("_No second close is safe while this latch remains unresolved. Recover "
                        "the exact permId/orderRef/(clientId, orderId) terminal receipt or use an "
                        "explicit adjudication path bound to that identity; never clear by conId._")
            body = (f"{head}\n"
                    f"• *{sym}* con_id={key} {ident}\n"
                    f"• placed {getattr(inf, 'placed_at', '?')} "
                    f"(age {float(ev.get('age_s') or 0.0) / 60.0:.0f} min), "
                    f"qty {getattr(inf, 'remaining_qty', '?')}\n"
                    f"• exact terminal receipt absent; clean observations {passes} "
                    f"(diagnostic only; never auto-releases); evidence: {ev.get('why')}\n"
                    f"{tail}")



            from exitmgr import alerting
            channel = ((getattr(self.config, "error_channel", "") or "")
                       or (getattr(self.config, "alerts_channel", "") or "")
                       or alerting.alerts_channel())
            if not channel:
                return
            identity = (key, getattr(inf, "client_id", None), getattr(inf, "order_id", 0),
                        getattr(inf, "perm_id", 0), getattr(inf, "order_ref", None),
                        getattr(inf, "placement_state", None), bool(released))
            queued = self._post_unthrottled_alert(
                body, "intent-unresolvable", channel=channel, condition_key=identity,
                repeat_seconds=self._INTENT_PAGE_THROTTLE_S)
            if queued:
                print(f"[INTENT-PAGE] con_id={key} {sym} released={released} passes={passes} "
                      f"why={ev.get('why')}; delivery queued, not yet confirmed")
        except Exception as e:
            print(f"[WARN] unresolvable-intent page failed for con_id={cid}: {e}")

    async def _poll_in_flight_fills(self, live_open_orders: Dict[int, dict],
                                    live_positions: Optional[Dict[int, object]] = None) -> Set[int]:
        """Public API contract; production-derived narrative omitted."""
        changed: Set[int] = set()
        try:
            infs = dict(self.state_manager.state.in_flight)
            if not infs:
                return changed



            terminal, history_complete_for = self._cached_terminal_trades_for_in_flight(infs)
            dirty = False
            for cid_str, inf in infs.items():
                try:
                    cid = int(cid_str)
                except (TypeError, ValueError):
                    continue
                live = live_open_orders.get(cid)
                if live is not None:
                    rem = live.get("remaining")
                    if isinstance(rem, (int, float)) and rem == rem and int(rem) >= 0:
                        rem = int(rem)
                        if rem != inf.remaining_qty:
                            inf.remaining_qty = rem
                            dirty = True
                trade = terminal.get(cid)
                if trade is None:











                    _is_intent = (getattr(inf, "placement_state", "submitted")
                                  in {"intent", "transmission_ambiguous"})


                    _hist_ok = cid in history_complete_for
                    if _is_intent and not _hist_ok:
                        ev = self._intent_release_evidence(
                            cid, inf, live_positions,
                            min_age_s=self._INTENT_LATCH_MIN_AGE_S)




                        qualifies = bool(live is None and ev["ok"]
                                         and self._order_book_unreadable is None)
                        if not qualifies:


                            _prev = self._intent_unreadable_observations.pop(cid, 0)
                            if _prev:
                                print(f"[RECONCILE] con_id={cid}: untransmitted-intent latch "
                                      f"counter RESET at {_prev} -- {ev['why']}")
                            if live is None:
                                self._page_unresolvable_intent(cid, inf, 0, ev, released=False)
                            continue
                        n = self._intent_unreadable_observations.get(cid, 0) + 1
                        self._intent_unreadable_observations[cid] = n
                        print(f"[RECONCILE] [WARN] con_id={cid}: untransmitted intent held, broker "
                              f"history UNREADABLE ({n} clean observations, age "
                              f"{float(ev['age_s']):.0f}s; no auto-release authority)")
                        self._page_unresolvable_intent(cid, inf, n, ev, released=False)
                        continue
                    if _is_intent and _hist_ok and live is None:
                        ev = self._intent_release_evidence(
                            cid, inf, live_positions, min_age_s=30)
                        self._intent_unreadable_observations.pop(cid, None)
                        print(f"[RECONCILE] con_id={cid}: readable broker history contains no "
                              "exact terminal receipt; intent latch HELD (negative absence is "
                              "not terminal authority)")
                        self._page_unresolvable_intent(cid, inf, 0, ev, released=False)
                    continue
                status = getattr(getattr(trade, "orderStatus", None), "status", None)
                if status == "Filled" or status in {"Cancelled", "ApiCancelled", "Inactive"}:




                    _recovered_evidence = dict(getattr(trade, "fill_evidence", {}) or {})
                    if (_recovered_evidence
                            and _recovered_evidence != dict(
                                getattr(inf, "fill_evidence", {}) or {})):
                        if self._trade_from_fill_evidence(cid, inf, _recovered_evidence) is None:
                            print(f"[ALERT] con_id={cid}: cached execution receipt failed live "
                                  "revalidation; retaining the durable close latch")
                            continue
                        inf.fill_evidence = _recovered_evidence
                        self.state_manager.save()
                    flat_evidence = (await self._fresh_broker_flat_evidence(inf)
                                     if status == "Filled" else None)
                    if self._finalize_in_flight_exit(
                            cid, inf, trade, broker_flat_evidence=flat_evidence):
                        changed.add(cid)
                        continue




                    if (status in {"Cancelled", "ApiCancelled", "Inactive"}
                            and trade_has_proven_zero_fill(trade)
                            and getattr(inf, "exit_context", None)):
                        ctx = dict(inf.exit_context or {})
                        import types as _types
                        trig = _types.SimpleNamespace(
                            trigger_type=ctx.get("trigger_type") or ctx.get("reason") or "exit")
                        extra = dict(ctx.get("extra") or {})





                        try:
                            _unfilled_qty = _durable_qty(
                                ctx, "close_qty", inf.remaining_qty,
                                where=f"con_id={cid} unfilled-exit row")
                        except _MalformedCloseQty as _mq:
                            print(f"[ALERT] {_mq} -- recording the non-fill row with an "
                                  "UNKNOWN close quantity rather than inventing one")
                            _unfilled_qty = None
                        self._log_unfilled_exit(
                            cid, ctx.get("symbol") or "", trig, fill_status=status,
                            close_qty=_unfilled_qty,
                            trigger_mark=extra.get("trigger_mark"), bid=extra.get("bid"),
                            limit_price=extra.get("limit_price", inf.order_price),
                            order_id=inf.order_id, reason=ctx.get("reason"),
                            placed_at=inf.placed_at)
                        self.state_manager.state.remove_in_flight(cid)
                        dirty = True
                        changed.add(cid)
            if dirty:
                self.state_manager.save()


            _live_keys = set()
            for _k in self.state_manager.state.in_flight:
                try:
                    _live_keys.add(int(_k))
                except (TypeError, ValueError):
                    continue
            for _stale_cid in [c for c in self._intent_unreadable_observations
                               if c not in _live_keys]:
                self._intent_unreadable_observations.pop(_stale_cid, None)
        except Exception as e:
            print(f"[WARN] in-flight fill poll errored (continuing): {e}")
        return changed

    async def refresh_entry_reconciliation(self) -> bool:
        """Public API contract; production-derived narrative omitted."""
        self._reconcile_ok = False
        self._entry_reconcile_error = None
        prior_side_effects = self._journal_side_effects
        try:
            if self.state_manager.persist:
                raise RuntimeError("entry refresh requires a read-only state manager")
            self._journal_side_effects = False

            self._load_journal(strict=True)
            safe = await self._reconcile_on_startup(
                emit_alerts=False, refresh_entry_journal=True)
            self._reconcile_ok = bool(safe and not self._unjournaled_con_ids)
            if not self._reconcile_ok:
                self._entry_reconcile_error = ("unjournaled_positions"
                    if safe and self._unjournaled_con_ids else "reconcile_unsafe")
            return self._reconcile_ok
        except Exception as exc:
            self._entry_reconcile_error = "unreadable:" + type(exc).__name__
            print(f"[ENTRY-RECONCILE] refresh failed ({type(exc).__name__}); "
                  "new entries withheld")
            return False
        finally:
            self._journal_side_effects = prior_side_effects

    async def _reconcile_on_startup(self, *, emit_alerts: bool = True,
                                    refresh_entry_journal: bool = False) -> bool:
        """Public API contract; production-derived narrative omitted."""
        print("[INFO] Starting reconciliation...")









        try:
            live_positions_raw, _short_raw, _stock_raw = await self._fetch_position_book()
            live_open_orders_raw = await self.ib_conn.get_open_orders(
                short_leg_con_ids=set(getattr(self, "_spread_short_legs", {}).keys()))
            self._note_good_broker_view()
        except Exception as e:



            _why = str(e) or ("timed out (%s)" % type(e).__name__)
            print(f"[ERROR] Could not fetch live data for reconciliation: {_why}")







            self._reconcile_bad_con_ids = None
            return False


        live_positions = {
            pd.con_id: {"qty": pd.quantity, "avg_cost": pd.avg_cost}
            for pd in live_positions_raw.values()
        }
        live_open_orders = {
            od.con_id: {"order_id": od.order_id, "remaining": od.remaining,
                        "perm_id": getattr(od, "perm_id", 0),
                        "client_id": getattr(od, "client_id", None),
                        "order_ref": getattr(od, "order_ref", None),
                        "status": getattr(od, "status", None),
                        "limit_price": getattr(od, "limit_price", None),





                        "order_ids": list(getattr(od, "order_ids", None) or (od.order_id,)),
                        "order_count": int(getattr(od, "order_count", 1))}
            for od in live_open_orders_raw.values()
        }

        if refresh_entry_journal:


            if self.state_manager.persist:
                raise RuntimeError("entry journal refresh cannot use the protective owner")
            self._load_journal(strict=True)


        journal_debits = {
            con_id: entry.get("debit", 0.0)
            for con_id, entry in self._journal_entries.items()
        }



        for _scid in getattr(self, "_spread_short_legs", {}):
            journal_debits.setdefault(int(_scid), 0.0)




        journal_qtys = {}
        for con_id, entry in self._journal_entries.items():
            try:
                journal_qtys[int(con_id)] = int(entry.get("quantity"))
            except (TypeError, ValueError):
                pass

















        if not getattr(self.state_manager, "persist", True):
            self.state_manager.reload()


        from exitmgr.state import reconcile_state
        _detail: dict = {}
        safe, alerts = reconcile_state(
            self.state_manager.state,
            live_positions,
            live_open_orders,
            journal_debits,
            journal_qtys=journal_qtys,
            detail=_detail,
        )



        self._reconcile_bad_con_ids = set(_detail.get("inconsistent") or set())




        self._unjournaled_con_ids = set(_detail.get("unjournaled") or set())



        self._unjournaled_con_ids.difference_update(self._campaign_conflicts)


        for _cc in (_detail.get("closed") or set()):
            self._clear_closed_position(_cc)


        for alert in alerts:
            print(f"[RECONCILE] {alert}")

        if safe:
            print("[RECONCILE] Reconciliation complete - safe to proceed")

            self.state_manager.save()
            return True
        else:
            print("[RECONCILE] Reconciliation found inconsistencies - ABORTING for safety")
            if emit_alerts:
                self._post_reconcile_block_alert(alerts)
            return False



    MGMT_DEFAULT_INTERVAL_S = 300.0

    MGMT_DEFAULT_MAX_AGE_S = 600.0

    def _mgmt_max_age_s(self) -> float:
        try:
            return max(0.0, float(getattr(self.config, "manage_positions_max_age_s",
                                          self.MGMT_DEFAULT_MAX_AGE_S)))
        except (TypeError, ValueError):
            return self.MGMT_DEFAULT_MAX_AGE_S

    def publish_mgmt_views(self, views, regime=None, qty_by_cid=None) -> None:
        """Public API contract; production-derived narrative omitted."""
        self._mgmt_views = list(views or [])
        self._mgmt_views_regime = regime
        self._mgmt_views_qty = dict(qty_by_cid or {})
        self._mgmt_views_ts = time.time()

    async def assess_positions_offcycle(self) -> bool:
        """Public API contract; production-derived narrative omitted."""
        if not getattr(self.config, "manage_positions", False):
            return False


        _exp = self._shadow_experiment_status()
        if _exp.get("retire"):
            self._announce_shadow_retirement(_exp)
            return False
        views = list(getattr(self, "_mgmt_views", None) or [])
        if not views:
            return False
        age = time.time() - float(getattr(self, "_mgmt_views_ts", 0.0) or 0.0)
        if age > self._mgmt_max_age_s():


            print(f"[POSMGMT] off-cycle assessment skipped: published views are {age:.0f}s stale")
            return False
        try:
            decisions, meta = await asyncio.to_thread(
                assess_positions,
                self.config.llm_endpoint, self.config.llm_model,
                views, market_regime=self._mgmt_views_regime, return_meta=True)
        except Exception as e:
            reason = f"{type(e).__name__}: {e}"[:500]
            self._mgmt_cache = None
            self._mgmt_last_attempt = {
                "attempt_ok": False, "failure_reason": reason, "ts": time.time(),
                "assessed_cids": {v.get("con_id") for v in views},
                "qty_by_cid": dict(getattr(self, "_mgmt_views_qty", {}) or {}),
            }
            print(f"[POSMGMT] off-cycle assessment failed ({e}); static exit rules remain in force")
            return False
        meta = meta if isinstance(meta, dict) else {}





        if "attempt_ok" in meta and meta.get("attempt_ok") is not True:
            reason = meta.get("failure_reason") or "unspecified_model_failure"
            self._mgmt_cache = None
            self._mgmt_last_attempt = {
                "attempt_ok": False, "failure_reason": str(reason)[:500], "ts": time.time(),
                "assessed_cids": {v.get("con_id") for v in views},
                "qty_by_cid": dict(getattr(self, "_mgmt_views_qty", {}) or {}),
            }
            print(f"[POSMGMT] off-cycle assessment failed ({reason}); "
                  "not cached; static exit rules remain in force")
            return False





        decisions, _refused = self._narrow_to_remit(decisions)
        if _refused:
            print(f"[POSMGMT] remit={list(self.MGMT_REMIT_ACTIONS)}: refused "
                  f"{len(_refused)} out-of-remit proposal(s) for con_ids {sorted(_refused)} "
                  f"-- recorded, not acted on")
        self._mgmt_cache = {
            "decisions": dict(decisions or {}),
            "raw": (meta or {}).get("raw"),











            "model_identity": ((meta or {}).get("model_identity")
                               or getattr(self.config, "llm_model", None)),



            "model_identity_source": ("meta" if (meta or {}).get("model_identity")
                                      else ("config" if getattr(self.config, "llm_model", None)
                                            else "unknown")),
            "attempt_ok": True,
            "failure_reason": None,
            "ts": time.time(),
            "assessed_cids": {v.get("con_id") for v in views},
            "qty_by_cid": dict(getattr(self, "_mgmt_views_qty", {}) or {}),
        }
        self._mgmt_last_attempt = {
            "attempt_ok": True, "failure_reason": None,
            "ts": self._mgmt_cache["ts"],
            "assessed_cids": set(self._mgmt_cache["assessed_cids"]),
            "qty_by_cid": dict(self._mgmt_cache["qty_by_cid"]),
        }
        rg = (self._mgmt_views_regime or {}).get("regime", "n/a")
        print(f"[POSMGMT] off-cycle assessment regime={rg} model decisions for con_ids "
              f"{sorted(decisions or {})} (usable for {self._mgmt_max_age_s():.0f}s)")





        self._alert_exit_decisions(decisions or {}, {v.get("con_id"): v for v in views})
        return True

















    MGMT_REMIT_ACTIONS = ("hold", "cut")







    MGMT_OUT_OF_REMIT = "out_of_remit"


    SHADOW_MAX_CLOSES = 60
    SHADOW_MAX_DAYS = 90





    SHADOW_GATE_MIN_EPISODES = 12
    SHADOW_GATE_MIN_SYMBOLS = 8
    SHADOW_GATE_MIN_ENTRY_WEEKS = 4

    _SHADOW_EXPERIMENT_ENV = "EXITMGR_SHADOW_EXPERIMENT_PATH"
    _SHADOW_EXPERIMENT_DEFAULT = os.path.expanduser(
        "~/.local/var/exitmgr/shadow-experiment.json")

    @classmethod
    def _narrow_to_remit(cls, decisions):
        """Public API contract; production-derived narrative omitted."""
        out, refused = {}, []
        for cid, dec in (decisions or {}).items():
            d = dict(dec or {})
            action = str(d.get("action", "hold")).strip().lower()
            if action == cls.MGMT_OUT_OF_REMIT:


                out[cid] = d
                continue
            if action in cls.MGMT_REMIT_ACTIONS:
                d["action"] = action
                out[cid] = d
                continue
            d["proposed_action"] = action
            d["action"] = cls.MGMT_OUT_OF_REMIT
            out[cid] = d
            refused.append(cid)
        return out, refused

    def _model_trail_state_has_authority(self) -> bool:
        """Public API contract; production-derived narrative omitted."""
        if bool(getattr(self.config, "manage_positions_shadow_only", False)):
            return False

        return "arm_trail" in self.MGMT_REMIT_ACTIONS

    @staticmethod
    def _bound_pinned_trail(pin, trailing):
        """Public API contract; production-derived narrative omitted."""
        pin = pin or {}
        act, gb = pin.get("activation_gain_pct"), pin.get("giveback_fraction")
        cur_act = getattr(trailing, "activation_gain_pct", None)
        cur_gb = getattr(trailing, "giveback_fraction", None)
        try:
            act = float(act)
            if act != act:
                raise ValueError
        except (TypeError, ValueError):
            act = cur_act
        try:
            gb = float(gb)
            if gb != gb:
                raise ValueError
        except (TypeError, ValueError):
            gb = cur_gb
        try:
            act = min(float(act), float(cur_act))
        except (TypeError, ValueError):
            pass
        try:
            gb = min(float(gb), float(cur_gb))
        except (TypeError, ValueError):
            pass
        return act, gb


    @property
    def _shadow_experiment_file(self) -> str:
        return (os.environ.get(self._SHADOW_EXPERIMENT_ENV)
                or self._SHADOW_EXPERIMENT_DEFAULT)

    def _load_shadow_experiment(self) -> dict:
        """Public API contract; production-derived narrative omitted."""
        try:
            with open(self._shadow_experiment_file) as fh:
                led = json.load(fh)
            return led if isinstance(led, dict) else {}
        except Exception:
            return {}

    def _save_shadow_experiment(self, led) -> bool:
        try:
            path = self._shadow_experiment_file
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(led, fh, indent=1, sort_keys=True, default=str)
            os.replace(tmp, path)
            return True
        except Exception as e:
            print(f"[SHADOW] experiment ledger write failed ({e}); continuing")
            return False

    def _completed_closes_since(self, started_at) -> int:
        """Public API contract; production-derived narrative omitted."""
        seen = set()
        try:
            with open(self._exits_log_path()) as fh:
                for line in fh:
                    line = line.strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    ts = str(r.get("close_ts") or r.get("ts") or "")
                    if started_at and ts and ts < str(started_at):
                        continue
                    if r.get("realized_pnl") is None:
                        continue
                    px = r.get("exit_price_per_share")
                    if (r.get("realized_pnl_pct") == -100.0 and px is not None
                            and float(px) == 0.0 and r.get("reason") != "expired"):
                        continue



                    cid = _first_present(r, "con_id", "contract_id", "conId")
                    if cid is not None:
                        seen.add(int(cid))
        except FileNotFoundError:
            return 0
        except Exception as e:
            print(f"[SHADOW] could not count completed closes ({e}); treating as 0")
            return 0
        return len(seen)

    @staticmethod
    def _entry_week(ts) -> str:
        """Public API contract; production-derived narrative omitted."""
        try:
            return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).strftime("%G-W%V")
        except Exception:
            return ""

    def _record_divergence_episode(self, con_id, symbol, je, decision, trigger) -> bool:
        """Public API contract; production-derived narrative omitted."""
        try:
            action = str((decision or {}).get("action", "")).strip().lower()
            if action != "cut" or trigger is not None:
                return False
            campaign = str((je or {}).get("decision_id") or f"con:{con_id}")
            led = self._load_shadow_experiment()
            eps = led.setdefault("episodes", {})
            if campaign in eps:
                return False
            entry_ts = (je or {}).get("fill_ts") or (je or {}).get("ts")
            eps[campaign] = {
                "con_id": int(con_id),
                "symbol": symbol,
                "entry_ts": entry_ts,
                "entry_week": self._entry_week(entry_ts),
                "first_ts": datetime.now().astimezone().isoformat(),
                "reason": str((decision or {}).get("reason") or "").strip()[:500],
            }
            led.setdefault("schema", "shadow_exit_experiment.v1")
            led.setdefault("started_at", datetime.now().astimezone().isoformat())
            self._save_shadow_experiment(led)
            print(f"[SHADOW] BINDING DIVERGENCE #{len(eps)} recorded for campaign {campaign} "
                  f"({symbol} con_id={con_id}): the model would have cut; the rules held. "
                  f"One episode per campaign -- further marks on this campaign do not count.")
            return True
        except Exception as e:
            print(f"[SHADOW] divergence bookkeeping skipped ({e})")
            return False

    def _shadow_experiment_status(self) -> dict:
        """Public API contract; production-derived narrative omitted."""
        st = {"active": False, "expired": False, "retire": False, "closes": 0, "episodes": 0,
              "symbols": 0, "entry_weeks": 0, "days": 0.0, "gate_passed": False,
              "started_at": None, "reason": ""}
        try:
            if not bool(getattr(self.config, "manage_positions_shadow_only", False)):
                st["reason"] = "not a shadow experiment (the assessor has live authority)"
                return st
            st["active"] = True
            led = self._load_shadow_experiment()
            started = led.get("started_at")
            if not started:
                started = datetime.now().astimezone().isoformat()
                led["schema"] = led.get("schema", "shadow_exit_experiment.v1")
                led["started_at"] = started
                led.setdefault("episodes", {})
                led.setdefault("gate_passed", False)
                self._save_shadow_experiment(led)
                print(f"[SHADOW] exit-assessor experiment clock started {started} "
                      f"(retires at {self.SHADOW_MAX_CLOSES} completed closes or "
                      f"{self.SHADOW_MAX_DAYS} days, whichever comes first)")
            st["started_at"] = started
            eps = led.get("episodes") or {}
            st["episodes"] = len(eps)
            st["symbols"] = len({e.get("symbol") for e in eps.values() if e.get("symbol")})
            st["entry_weeks"] = len({e.get("entry_week") for e in eps.values()
                                     if e.get("entry_week")})
            st["gate_passed"] = bool(led.get("gate_passed"))
            st["closes"] = self._completed_closes_since(started)
            try:
                _d = (datetime.now().astimezone()
                      - datetime.fromisoformat(str(started).replace("Z", "+00:00")))
                st["days"] = round(_d.total_seconds() / 86400.0, 2)
            except Exception:
                st["days"] = 0.0
            st["expired"] = (st["closes"] >= self.SHADOW_MAX_CLOSES
                             or st["days"] >= self.SHADOW_MAX_DAYS)
            if led.get("retired_at"):
                st["expired"] = True
            st["retire"] = bool(st["expired"]) and not st["gate_passed"]
            if st["retire"]:
                if st["episodes"] < self.SHADOW_GATE_MIN_EPISODES:
                    st["reason"] = (
                        f"operationally redundant: {st['episodes']} binding divergence episodes "
                        f"in {st['closes']} closes / {st['days']:.0f} days, fewer than the "
                        f"{self.SHADOW_GATE_MIN_EPISODES} the gate needs")
                else:
                    st["reason"] = (
                        f"gate review required: {st['episodes']} episodes across "
                        f"{st['symbols']} symbols and {st['entry_weeks']} entry weeks "
                        f"(gate needs >={self.SHADOW_GATE_MIN_SYMBOLS} symbols, "
                        f">={self.SHADOW_GATE_MIN_ENTRY_WEEKS} entry weeks and the paired "
                        f"statistical limbs); no live remit until that verdict is recorded")
        except Exception as e:
            print(f"[SHADOW] experiment status unavailable ({e}); the experiment continues")
            st["retire"] = False
        return st

    def _announce_shadow_retirement(self, st) -> None:
        """Public API contract; production-derived narrative omitted."""
        try:
            led = self._load_shadow_experiment()
            first = not led.get("retired_at")
            if first:
                led["retired_at"] = datetime.now().astimezone().isoformat()
                led["retired_reason"] = st.get("reason")
                led["retired_counts"] = {k: st.get(k) for k in
                                         ("closes", "days", "episodes", "symbols", "entry_weeks")}
                self._save_shadow_experiment(led)
            print(f"[SHADOW] exit-assessor experiment RETIRED -- {st.get('reason')}. "
                  f"The assessor is no longer called; deterministic rules were already the only "
                  f"authority. Evidence preserved in {self._shadow_experiment_file}")
            if first:
                try:
                    from exitmgr import alerting
                    alerting.post(
                        ":stop_sign: *Exit-assessor experiment retired*\n"
                        f"{st.get('reason')}\n"
                        f"closes {st.get('closes')}/{self.SHADOW_MAX_CLOSES}  |  "
                        f"days {st.get('days')}/{self.SHADOW_MAX_DAYS}  |  "
                        f"episodes {st.get('episodes')}  |  symbols {st.get('symbols')}  |  "
                        f"entry weeks {st.get('entry_weeks')}\n"
                        "_The assessor is no longer called. Deterministic rules are unchanged._",
                        alerting.alerts_channel(), label="shadow-retired")
                except Exception as _ae:
                    print(f"[SHADOW] retirement alert not delivered ({_ae})")
        except Exception as e:
            print(f"[SHADOW] retirement bookkeeping skipped ({e})")




    MGMT_ALERT_ACTIONS = ("arm_trail", "tighten_stop", "take_profit", "cut")







    _MGMT_ALERTED_ENV = "EXITMGR_MGMT_ALERTED_PATH"
    _MGMT_ALERTED_DEFAULT = os.path.expanduser("~/.local/var/exitmgr/mgmt-alerted.json")

    @property
    def _MGMT_ALERTED_PATH(self) -> str:
        return os.environ.get(self._MGMT_ALERTED_ENV) or self._MGMT_ALERTED_DEFAULT

    def _load_mgmt_alerted(self) -> dict:
        """Public API contract; production-derived narrative omitted."""
        try:
            with open(self._MGMT_ALERTED_PATH) as fh:
                raw = json.load(fh)
            return {int(k): tuple(v) for k, v in raw.items()}
        except Exception:
            return {}

    def _save_mgmt_alerted(self) -> None:
        """Public API contract; production-derived narrative omitted."""
        try:
            os.makedirs(os.path.dirname(self._MGMT_ALERTED_PATH), exist_ok=True)
            tmp = self._MGMT_ALERTED_PATH + ".tmp"
            with open(tmp, "w") as fh:
                json.dump({str(k): list(v) for k, v in self._mgmt_alerted.items()}, fh)
            os.replace(tmp, self._MGMT_ALERTED_PATH)
        except Exception:
            pass

    @staticmethod
    def _decision_fingerprint(decision) -> tuple:
        """Public API contract; production-derived narrative omitted."""
        d = decision or {}
        return (str(d.get("action", "hold")).strip().lower(),
                d.get("stop_pct"), d.get("trail_activation_gain_pct"),
                d.get("trail_giveback_fraction"), bool(d.get("reload")))

    def _format_exit_decision(self, con_id, decision, view) -> str:
        """Public API contract; production-derived narrative omitted."""
        d, v = decision or {}, view or {}
        action = str(d.get("action", "hold")).strip().lower()
        emoji = {"arm_trail": ":lock:", "tighten_stop": ":clamp:",
                 "take_profit": ":dart:", "cut": ":knife:"}.get(action, ":robot_face:")
        symbol = v.get("symbol") or "?"

        def _pct(x, digits=1):
            try:
                return f"{float(x):+.{digits}f}%"
            except (TypeError, ValueError):
                return "n/a"

        wf = v.get("window_fraction")
        try:
            window = f"{float(wf) * 100:.1f}%"
        except (TypeError, ValueError):
            window = "unknown"
        hold = v.get("intended_hold_days")
        elapsed = v.get("calendar_days_elapsed")
        dte = v.get("dte")

        bits = [f"{emoji} *EXIT DECISION - {symbol}* `{action}`",
                f"P&L {_pct(v.get('pnl_pct'))}  |  window {window}"
                + (f" ({elapsed}/{hold}d)" if elapsed is not None and hold else "")
                + (f"  |  DTE {dte}" if dte is not None else "")]
        if action == "arm_trail":
            act = d.get("trail_activation_gain_pct")
            gb = d.get("trail_giveback_fraction")
            bits.append(
                f"trail: activation {'default' if act is None else _pct(act, 0)}"
                f"  |  giveback {'default' if gb is None else f'{float(gb):.0%}'}")
        elif action == "tighten_stop":
            ns = d.get("stop_pct")
            bits.append(f"new stop: {'unchanged' if ns is None else f'{float(ns):.0f}%'} of debit")
        elif action == "take_profit" and d.get("reload"):
            bits.append(f"reload flagged (conviction {d.get('reload_conviction')}) "
                        f"- still gated by the normal entry path")
        reason = str(d.get("reason") or "").strip()
        if reason:
            bits.append(f"_{reason}_")
        bits.append(f"con_id {con_id}")
        return "\n".join(bits)

    def _alert_exit_decisions(self, decisions, views_by_cid) -> int:
        """Public API contract; production-derived narrative omitted."""
        posted = 0
        try:
            from exitmgr import alerting
            live = set(views_by_cid or {})
            for gone in [c for c in self._mgmt_alerted if c not in live]:
                self._mgmt_alerted.pop(gone, None)
                self._save_mgmt_alerted()

            for con_id, decision in (decisions or {}).items():
                action = str((decision or {}).get("action", "hold")).strip().lower()
                if action not in self.MGMT_ALERT_ACTIONS:
                    continue
                fingerprint = self._decision_fingerprint(decision)
                if action == "arm_trail":





                    try:
                        _rec = self.state_manager.state.trail_confirmation_for(con_id) or {}
                        _pinned = self.state_manager.state.pinned_trail_params(con_id)
                        _noop_rearm = bool(_rec.get("armed_at")) and _pinned is not None
                    except Exception:
                        _noop_rearm = False
                    if _noop_rearm:
                        print(f"[POSMGMT] con_id={con_id}: arm_trail is a no-op "
                              f"(already armed, params pinned) -- not alerting")
                        continue














                    try:
                        if self.state_manager.state.pinned_trail_params(con_id) is not None:
                            fingerprint = (fingerprint[0], fingerprint[1], None, None,
                                           fingerprint[4])
                    except Exception:
                        pass















                    fingerprint = (fingerprint[0], fingerprint[1], None, fingerprint[3],
                                   fingerprint[4])
                if self._mgmt_alerted.get(con_id) == fingerprint:
                    continue
                text = self._format_exit_decision(con_id, decision,
                                                  (views_by_cid or {}).get(con_id))
                ok = alerting.post(text, alerting.alerts_channel(), label="exit-decision")


                if ok:
                    self._mgmt_alerted[con_id] = fingerprint
                    self._save_mgmt_alerted()
                    posted += 1
                else:
                    print(f"[POSMGMT] exit-decision alert NOT delivered for con_id={con_id} "
                          f"({action}); will retry on the next assessment")
        except Exception as e:
            print(f"[POSMGMT] exit-decision alerting failed ({e}); continuing")
        return posted

    def _consume_model_decisions(self, views_by_cid, qty_by_cid=None):
        """Public API contract; production-derived narrative omitted."""






        self._mgmt_identity_source = None
        self._mgmt_attempt_ok = None
        self._mgmt_failure_reason = None
        cache = getattr(self, "_mgmt_cache", None)
        if not isinstance(cache, dict):
            failed = getattr(self, "_mgmt_last_attempt", None)
            if isinstance(failed, dict) and failed.get("attempt_ok") is False:
                age = time.time() - float(failed.get("ts") or 0.0)
                assessed = set(failed.get("assessed_cids") or ())
                seen_qty = dict(failed.get("qty_by_cid") or {})
                now_qty = dict(qty_by_cid or {})
                same_book = (age <= self._mgmt_max_age_s()
                             and all(cid in assessed for cid in views_by_cid)
                             and all(seen_qty.get(cid) == now_qty.get(cid)
                                     for cid in views_by_cid
                                     if seen_qty.get(cid) is not None
                                     and now_qty.get(cid) is not None))
                if same_book:
                    self._mgmt_attempt_ok = False
                    self._mgmt_failure_reason = failed.get("failure_reason")
            return {}, None, None
        age = time.time() - float(cache.get("ts") or 0.0)
        max_age = self._mgmt_max_age_s()
        if age > max_age:
            print(f"[POSMGMT] cached model decisions are {age:.0f}s old (limit {max_age:.0f}s) -- "
                  f"discarded; static exit rules apply")
            self._mgmt_cache = None
            return {}, None, None
        seen_qty = cache.get("qty_by_cid") or {}
        now_qty = dict(qty_by_cid or {})
        out = {}
        for cid, dec in (cache.get("decisions") or {}).items():
            if cid not in views_by_cid:
                continue
            was, now = seen_qty.get(cid), now_qty.get(cid)
            if was is not None and now is not None and was != now:
                print(f"[POSMGMT] dropping cached decision for con_id={cid}: quantity changed "
                      f"{was} -> {now} since the assessment; static rules apply to it")
                continue
            out[cid] = dec
        assessed = cache.get("assessed_cids") or set()
        covered = all(cid in assessed for cid in views_by_cid)
        if not covered:
            print("[POSMGMT] cached assessment predates a position opened this cycle -- applying "
                  "its decisions but recording no implicit holds")




        out, _refused = self._narrow_to_remit(out)
        if _refused:
            print(f"[POSMGMT] remit={list(self.MGMT_REMIT_ACTIONS)}: refused "
                  f"{len(_refused)} cached out-of-remit decision(s) for con_ids "
                  f"{sorted(_refused)} -- recorded, not acted on")
        if out:
            print(f"[POSMGMT] applying off-cycle model decisions for con_ids {sorted(out)} "
                  f"(assessed {age:.0f}s ago)")
        self._mgmt_identity_source = _identity_source(
            cache.get("model_identity"), cache.get("model_identity_source"))
        self._mgmt_attempt_ok = cache.get("attempt_ok", True)
        self._mgmt_failure_reason = cache.get("failure_reason")
        return out, (cache.get("raw") if covered else None), cache.get("model_identity")

    def _position_rules(self, je):
        """Public API contract; production-derived narrative omitted."""
        je = je or {}
        rules = self.config.rules
        _je_tp = _construction.journal_take_profit_pct(je)
        if _je_tp is not None or je.get("stop_pct"):
            from dataclasses import replace
            rules = replace(self.config.rules,
                            profit_target_pct=_je_tp,
                            stop_pct=je.get("stop_pct") or self.config.rules.stop_pct)
        return rules

    def _portfolio_marks(self):
        """Public API contract; production-derived narrative omitted."""
        try:



            if (bool(getattr(self.config, "arm", False))
                    and not self.ib_conn.is_healthy()):
                return {}
            observer = getattr(self.ib_conn, "portfolio_mark_observations", None)
            observed = (observer(max_age_s=self.PROTECTIVE_PRICE_MAX_AGE_S)
                        if callable(observer) else {})
            if observed:
                return {int(cid): float(row["price"]) for cid, row in observed.items()}


            if bool(getattr(self.config, "arm", False)):
                return {}
            return {p.contract.conId: p.marketPrice for p in self.ib_conn.ib.portfolio()
                    if p.position != 0 and p.marketPrice is not None
                    and p.marketPrice == p.marketPrice and p.marketPrice > 0}
        except Exception as _e:
            print(f"[WARN] portfolio marking unavailable ({_e}); using streaming quotes")
            return {}

    def _fresh_protective_quotes(self):
        """Public API contract; production-derived narrative omitted."""
        now = time.monotonic()
        conn = self.quote_ib_conn or self.ib_conn
        if not conn.is_healthy():
            return {}
        generation = getattr(conn, "_connection_generation", None)
        out = {}
        for cid, row in tuple(self._protective_quote_snapshot.items()):
            try:
                age = now - float(row["observed_monotonic"])
                if (0 <= age <= self.PROTECTIVE_PRICE_MAX_AGE_S
                        and row.get("generation") == generation):
                    out[int(cid)] = dict(row, age_s=age)
            except (TypeError, ValueError, KeyError):
                continue
        return out

    async def refresh_protective_quotes_offcycle(self):
        """Public API contract; production-derived narrative omitted."""
        ids = sorted(self._protective_quote_con_ids)
        if not ids:
            return 0
        conn = self.quote_ib_conn or self.ib_conn
        rows = await conn.fetch_quotes(ids)


        if rows:
            self._protective_quote_snapshot.update(
                {int(cid): dict(row) for cid, row in rows.items()})
        return len(rows or {})

    def _build_position_views(self, managed_positions, quotes, price_stats=None,
                              portfolio_marks=None):
        """Public API contract; production-derived narrative omitted."""
        views = []

        _pmarks = (self._portfolio_marks() if portfolio_marks is None
                   else dict(portfolio_marks))
        for p in managed_positions:
            cid = p.con_id
            if cid in self._campaign_conflicts:
                continue
            q = quotes.get(cid)
            if q is None and cid not in _pmarks:
                continue



            cur = _pmarks.get(cid, (q or {}).get("price"))
            je = self._journal_entries.get(cid) or {}
            sp = je.get("spread")
            if sp and sp.get("short_con_id"):
                _scid = int(sp["short_con_id"])
                short_px = _pmarks.get(_scid)
                if short_px is None:
                    sq = quotes.get(_scid)
                    if sq is None:
                        continue
                    short_px = sq["price"]
                cur = cur - short_px



            entry_debit, qty, basis_source = _resolve_entry_basis(je, p)
            current_value = cur * 100 * qty
            pnl_pct = round((current_value - entry_debit) / entry_debit * 100, 1) if entry_debit else 0.0
            peak = self.state_manager.state.peak_prices.get(str(cid), cur)
            from_peak = round((cur / peak - 1) * 100, 1) if peak else 0.0
            tc = self.config.rules.trailing
            sym = je.get("symbol", p.symbol)
            trail_info = None
            if getattr(self.config.rules.atr_levels, "dynamic_activation_enabled", False):
                levels = self._atr_levels_for(cid, sym, entry_debit, qty,
                    (q or {}).get("iv"), days_to_expiry(getattr(p, "expiry", "")), sp)
                trail_info = self._trail_activation_for(cid, levels, entry_debit, qty, quotes, sp)
                from dataclasses import replace as _view_replace
                view_rules = self._with_trail_activation(self.config.rules,
                    trail_info["activation_gain_pct"])
                if trail_info.get("qualification"):
                    view_rules = _view_replace(view_rules, trailing=_view_replace(
                        view_rules.trailing, activation_gain_pct=trail_info["activation_gain_pct"]))
                view_auto = _view_replace(self.config.rules.auto_trail,
                    activation_gain_pct=trail_info["activation_gain_pct"])
                view_rules, _ = self._apply_auto_trail(view_rules, view_auto, peak,
                    entry_debit, qty, armed=self.state_manager.state.is_trail_armed(cid),
                    peak_since_arm=self.state_manager.state.trail_peak_since_arm(cid))
                tc = view_rules.trailing
                pin = self.state_manager.state.pinned_trail_params(cid)
                if pin:
                    pa, pg = self._bound_pinned_trail(pin, tc)
                    tc = _view_replace(tc, activation_gain_pct=pa, giveback_fraction=pg)
            tstats = (price_stats or {}).get(sym)
            trend = regime_mod.trend_strength(tstats) if tstats else None
            entry_ts = je.get("fill_ts") or je.get("ts")
            elapsed = _calendar_days_elapsed(entry_ts)
            try:
                intended_hold = int(je.get("intended_hold_days"))
                if intended_hold <= 0:
                    intended_hold = None
            except (TypeError, ValueError):
                intended_hold = None
            try:
                entry_conviction = float(je.get("conviction"))
                if not 1 <= entry_conviction <= 10:
                    entry_conviction = None
            except (TypeError, ValueError):
                entry_conviction = None
            views.append({
                "con_id": cid,
                "symbol": sym,
                "structure": "spread" if sp else "single",
                "pnl_pct": pnl_pct,


                "_basis": round(float(entry_debit), 4),
                "_basis_source": basis_source,
                "_mark": round(float(cur), 4),
                "_qty": qty,
                "pct_from_peak": from_peak,
                "trend": trend,
                "dte": days_to_expiry(getattr(p, "expiry", "")),
                "entry_timestamp": entry_ts,
                "calendar_days_elapsed": elapsed,
                "intended_hold_days": intended_hold,
                "window_fraction": (round(elapsed / intended_hold, 3)
                                    if elapsed is not None and intended_hold else None),
                "entry_conviction": entry_conviction,





                "profit_target_pct": _construction.journal_take_profit_pct(je),
                "stop_pct": je.get("stop_pct") or self.config.rules.stop_pct,











                "trail_enabled": bool(tc.enabled),
                "trail_configured": str(cid) in self.state_manager.state.trail_configured,
                "trail_armed": self.state_manager.state.is_trail_armed(cid),
                "trail_confirming_closes": self.state_manager.state.trail_confirmation_for(
                    cid)["consecutive_qualifying_closes"],
                "trail_activation_gain_pct": (None if (trail_info or {}).get("qualification")
                    and not self.state_manager.state.is_trail_armed(cid)
                    and (trail_info["qualification"].get("activation_gain_pct") is None)
                    else tc.activation_gain_pct),
                "trail_activation_source": (trail_info or {}).get("source", "configured"),
                "trail_qualification": (trail_info or {}).get("qualification"),
                "trail_giveback_fraction": tc.giveback_fraction,
                "trail_protected_floor_price": self.state_manager.state.trail_protected_floor(cid),




                "thesis": je.get("thesis"),
                "entry_technical_card": je.get("technical_card"),
                "mfe_pct": self.state_manager.state.mfe_pct.get(str(cid)),


                "iv": (quotes.get(cid) or {}).get("iv"),
                "delta": (quotes.get(cid) or {}).get("delta"),



                "current_technical_card": ((price_stats or {}).get(sym) or None),


                "upcoming_events": self._events_for(sym),
            })
        return views

    def _trail_activation_for(self, con_id, levels, entry_debit, quantity,
                              quotes=None, spread=None):
        """Public API contract; production-derived narrative omitted."""
        rules = self.config.rules
        auto = getattr(rules, "auto_trail", None)
        ordinary = rules.trailing
        ceiling = float(getattr(auto, "activation_gain_pct", ordinary.activation_gain_pct))
        if ordinary.enabled:
            ceiling = min(ceiling, float(ordinary.activation_gain_pct))
        cfg = rules.atr_levels
        dynamic = bool(getattr(cfg, "dynamic_activation_enabled", False))
        activation, source, friction = ceiling, "configured_fallback:atr_unavailable", None
        if not dynamic:
            source = "legacy"
            if levels:
                activation = min(ceiling, float(levels["activation_gain_pct"]))
        else:
            try:
                raw = float(levels["per_atr_pct"]) * float(cfg.dynamic_k_arm)
                if not math.isfinite(raw) or raw <= 0:
                    raise ValueError("invalid ATR conversion")
                activation, source = raw, "underlying_atr_net_delta"
                try:
                    eps = float(entry_debit) / (100 * abs(int(quantity)))
                    ids = [int(con_id)]
                    if spread and spread.get("short_con_id"):
                        ids.append(int(spread["short_con_id"]))
                    width = 0.0
                    for cid in ids:
                        quote = (quotes or {})[cid]
                        bid, ask = float(quote["bid"]), float(quote["ask"])
                        if not all(math.isfinite(v) for v in (bid, ask)) or not 0 < bid <= ask:
                            raise ValueError("unusable spread")
                        width += ask - bid
                    if not math.isfinite(eps) or eps <= 0:
                        raise ValueError("unusable basis")
                    friction = 0.5 * width / eps * 100
                    activation = max(activation, friction)
                except (KeyError, TypeError, ValueError, ZeroDivisionError):
                    source += ":friction_unavailable"
                activation = min(ceiling, activation)
            except (KeyError, TypeError, ValueError):
                pass
        qualification = None
        if dynamic and getattr(cfg, "qualification_enabled", False):
            from exitmgr import atr_cache, trail_qualification
            from datetime import date as _quality_date
            je = self._journal_entries.get(con_id) or {}
            try:
                atr_record = atr_cache.read(je.get("symbol", ""))
                qualification = trail_qualification.qualify(
                    con_id=con_id, quotes=quotes, entry_debit=entry_debit, quantity=quantity,
                    journal=je, atr_record=atr_record, now_monotonic=time.monotonic(),
                    today=_quality_date.today(),
                    max_quote_age_s=getattr(cfg, "quote_max_age_s", 30.),
                    max_quote_skew_s=getattr(cfg, "quote_max_skew_s", 2.),
                    max_atr_age_days=getattr(cfg, "max_atr_age_days", 4),
                    k_arm=cfg.dynamic_k_arm, ceiling_pct=ceiling,
                    giveback_fraction=max(
                        float(self.config.rules.trailing.giveback_fraction),
                        float(self.config.rules.auto_trail.giveback_fraction)))
            except Exception as _quality_error:
                qualification = {"qualified": False, "quote_qualified": False,
                    "executable_price": None, "activation_gain_pct": None,
                    "reason": "qualification_error:" + type(_quality_error).__name__}
            if qualification["activation_gain_pct"] is not None:
                activation = qualification["activation_gain_pct"]
            source = "executable_qualification:" + qualification["reason"]
        pin = self.state_manager.state.pinned_trail_params(con_id)
        if dynamic and pin and pin.get("activation_gain_pct") is not None:
            pinned = float(pin["activation_gain_pct"])
            if math.isfinite(pinned) and pinned >= 0:
                activation = min(activation, pinned)
                source += ":pinned_ceiling"
        return {"activation_gain_pct": activation, "source": source,
                "friction_floor_pct": friction, "configured_ceiling_pct": ceiling,
                "dynamic": dynamic, "qualification": qualification}

    @staticmethod
    def _with_trail_activation(rules, activation):
        from dataclasses import replace
        bar = min(float(rules.trailing.activation_gain_pct), float(activation))
        return replace(rules, trailing=replace(rules.trailing, activation_gain_pct=bar))

    def _atr_levels_for(self, con_id, symbol, entry_debit, quantity, iv, dte, spread=None):
        """Public API contract; production-derived narrative omitted."""
        cfg = getattr(self.config.rules, "atr_levels", None)
        if cfg is None or not getattr(cfg, "enabled", False):
            return None
        try:
            from exitmgr import atr_cache, atr_levels





            rec = atr_cache.read(symbol)
            if not rec:
                if symbol:
                    self._atr_refresh_requested.add(str(symbol).upper())
                print(f"[ATR] con_id={con_id} ({symbol}): no ATR cache entry -- static stop")
                return None
            je = self._journal_entries.get(con_id) or {}
            long_strike = je.get("strike")
            if long_strike is None:
                print(f"[ATR] con_id={con_id} ({symbol}): journal has no long strike "
                      f"(keys={sorted(je)[:6]}...) -- static stop")
                return None
            sp = spread if spread else (je.get("spread") or {})











            _iv_eff = iv
            try:
                _iv_eff = float(_iv_eff or 0.0)
            except (TypeError, ValueError):
                _iv_eff = 0.0







            if _iv_eff != _iv_eff or _iv_eff <= 0:
                try:
                    _iv_eff = float(je.get("entry_iv") or 0.0)
                except (TypeError, ValueError):
                    _iv_eff = 0.0
                if _iv_eff != _iv_eff:
                    _iv_eff = 0.0
                if _iv_eff > 0:
                    print(f"[ATR] con_id={con_id} ({symbol}): live IV unavailable, using entry "
                          f"IV {_iv_eff:.4f} for the net-delta calc")
            net = atr_levels.net_structure_delta(
                rec["spot"], long_strike, (sp or {}).get("short_strike"), dte, _iv_eff,
                right=je.get("right", "C"))
            if net is None:
                print(f"[ATR] con_id={con_id} ({symbol}): net delta unpriceable "
                      f"(spot={rec.get('spot')} long={long_strike} "
                      f"short={(sp or {}).get('short_strike')} dte={dte} iv={_iv_eff}) "
                      f"-- static stop")
                return None
            q, ed = abs(int(quantity)), float(entry_debit)
            if q <= 0 or ed <= 0:
                print(f"[ATR] con_id={con_id} ({symbol}): unusable size/debit "
                      f"(qty={quantity} entry_debit={entry_debit}) -- static stop")
                return None
            eps = ed / (100.0 * q)
            per_atr_pct = rec["atr"] * net / eps * 100.0
















            _hold = je.get("intended_hold_days")
            try:
                _hold = float(_hold) if _hold else 0.0
            except (TypeError, ValueError):
                _hold = 0.0
            if _hold <= 0:
                _hold = float(max(1, -(-int(dte or 1) // 8)))




            _held = _calendar_days_elapsed(je.get("ts"))
            _lapse_over = None
            if (getattr(cfg, "lapse_tighten", False) and _held is not None
                    and _hold > 0 and _held > _hold):
                _SYMO = _cfg_num(cfg, "lapse_halflife_days", 10.0)
                if _SYMO > 0:
                    _lapse_over = _held - _hold
                    _floor_d = _cfg_num(cfg, "lapse_floor_days", 1.0)
                    _hold = max(_floor_d, _hold * (0.5 ** (_lapse_over / _SYMO)))





            _ws = _cfg_num(cfg, "wind_down_start_dte", 20)
            _wf = _cfg_num(self.config.rules, "time_stop_days", 10)
            _wind = None
            try:
                _dte_f = float(dte) if dte is not None else None
            except (TypeError, ValueError):
                _dte_f = None













            if _dte_f is not None:

                _room = max(0.5, _dte_f - _wf)
                if _room < _hold:
                    _hold = _room
                    if _ws <= 0 or _dte_f <= _ws:
                        _wind = _room
            _k_eff = float(cfg.k_stop)
            if getattr(cfg, "horizon_scaling", False):
                _hk = atr_levels.horizon_k(_cfg_num(cfg, "k_stop_horizon", 0.5), _hold)
                if _hk is not None:
                    _k_eff = _hk
            return {
                "atr": rec["atr"], "spot": rec["spot"], "net_delta": net,
                "entry_per_share": eps, "per_atr_pct": per_atr_pct,
                "hold_days": _hold, "k_eff": _k_eff,
                "held_days": _held, "lapse_over_days": _lapse_over,
                "wind_down_room": _wind, "dte": _dte_f,
                "activation_gain_pct": float(cfg.k_arm) * per_atr_pct,





                "stop_pct": atr_levels.atr_stop_pct(
                    eps, rec["atr"], net, _k_eff,
                    _cfg_num(self.config.rules, "stop_pct", 30.0),
                    _cfg_num(cfg, "min_stop_pct", 8.0)),
            }
        except Exception as e:
            print(f"[ATR] con_id={con_id} ({symbol}): levels unavailable ({e}); static rules")
            return None

    async def refresh_requested_atr_offcycle(self) -> dict:
        """Public API contract; production-derived narrative omitted."""
        async with self._atr_refresh_lock:
            requested = sorted(self._atr_refresh_requested)
            if not requested:
                return {"ok": [], "failed": []}
            from exitmgr import atr_cache
            try:
                result = await asyncio.to_thread(atr_cache.refresh, requested)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                print(f"[WARN] off-cycle ATR refresh failed for {requested}: {exc!r}; "
                      "static levels remain active and refresh will retry")
                return {"ok": [], "failed": requested}
            ok = {str(symbol).upper() for symbol in (result.get("ok") or [])}
            self._atr_refresh_requested.difference_update(ok)
            failed = list(result.get("failed") or [])
            print(f"[ATR-CACHE] off-cycle refresh ok={sorted(ok)} failed={failed}")
            return result

    def _apply_decision(self, rules, decision, current_price, entry_debit, quantity, con_id, symbol, regime=None):
        """Public API contract; production-derived narrative omitted."""
        from dataclasses import replace
        action = (decision or {}).get("action", "hold")
        if action in ("take_profit", "cut"):
            current_value = current_price * 100 * quantity
            pnl = (current_value - entry_debit) / entry_debit * 100 if entry_debit > 0 else 0.0
            reason = (decision.get("reason") or "").strip()




            _reload = bool(decision.get("reload")) if action == "take_profit" else False
            _reload_conv = decision.get("reload_conviction") if _reload else None
            return rules, ExitTrigger(
                con_id=con_id, trigger_type=("take_profit" if action == "take_profit" else "model_cut"),
                current_price=current_price, entry_debit=entry_debit, current_value=current_value,
                pnl_pct=pnl, message=(f"model {action}: {reason}" if reason else f"model {action}"),
                reload=_reload, reload_conviction=_reload_conv)
        if action == "arm_trail":
            tc = rules.trailing
            bull = regime_mod.is_bull(regime)
            act, gb = decision.get("trail_activation_gain_pct"), decision.get("trail_giveback_fraction")
            new_act, new_gb = tc.activation_gain_pct, tc.giveback_fraction


            free = bull or not tc.enabled
            if act is not None:
                new_act = float(act) if free else min(float(act), tc.activation_gain_pct)
            if gb is not None:
                gb = max(0.1, min(0.9, float(gb)))
                new_gb = gb if free else min(gb, tc.giveback_fraction)








            try:
                self.state_manager.state.trail_configured[str(con_id)] = True
            except Exception:
                pass
            return replace(rules, trailing=replace(tc, enabled=True, activation_gain_pct=new_act, giveback_fraction=new_gb)), None
        if action == "tighten_stop":
            ns = decision.get("stop_pct")
            if ns is not None and float(ns) > 0:
                cur = rules.stop_pct
                new_stop = min(float(ns), cur) if cur is not None else float(ns)
                return replace(rules, stop_pct=new_stop), None
        return rules, None

    @staticmethod
    def _reconcile_ceiling_backstop(rules, decision, configured=False, armed=None):
        """Public API contract; production-derived narrative omitted."""
        if armed is not None:
            configured = configured or armed
        _arm = bool(configured) or bool(decision and decision.get("action") == "arm_trail")
        if _arm and rules.profit_target_pct is not None:
            from dataclasses import replace as _rp_pt
            return _rp_pt(rules, profit_target_pct=None)
        return rules

    @staticmethod
    def _apply_auto_trail(rules, auto_cfg, peak_price, entry_debit, quantity, armed=False,
                          peak_since_arm=None):
        """Public API contract; production-derived narrative omitted."""
        from dataclasses import replace as _rp_at
        if auto_cfg is None or not getattr(auto_cfg, "enabled", False):
            return rules, False

        if not armed:
            return rules, False
        if peak_since_arm is None:
            return rules, False
        try:
            q = int(quantity)
            ed = float(entry_debit)
            pk = float(peak_since_arm)
        except (TypeError, ValueError):
            return rules, False
        if q <= 0 or ed <= 0 or pk != pk or pk <= 0:
            return rules, False
        entry_per_share = ed / (100.0 * q)
        if entry_per_share <= 0:
            return rules, False
        peak_gain_pct = (pk / entry_per_share - 1.0) * 100.0
        if peak_gain_pct < float(auto_cfg.activation_gain_pct):
            return rules, False
        tc = rules.trailing
        auto_gb = float(auto_cfg.giveback_fraction)
        if tc.enabled:

            new_gb = max(float(tc.giveback_fraction), auto_gb)
            new_act = min(float(tc.activation_gain_pct), float(auto_cfg.activation_gain_pct))
        else:
            new_gb = auto_gb
            new_act = float(auto_cfg.activation_gain_pct)
        new_gb = max(0.1, min(0.9, new_gb))
        return _rp_at(rules, trailing=_rp_at(tc, enabled=True,
                                             activation_gain_pct=new_act,
                                             giveback_fraction=new_gb)), True







    SESSION_CLOSE_START_ET_MIN = 16 * 60
    SESSION_CLOSE_END_ET_MIN = 20 * 60

    @classmethod
    def _completed_session_date(cls, now=None) -> Optional[str]:
        """Public API contract; production-derived narrative omitted."""
        from datetime import timezone as _tz
        try:
            from zoneinfo import ZoneInfo
            et = ZoneInfo("America/New_York")
            n = now or datetime.now(et)
            if getattr(n, "tzinfo", None) is None:
                n = n.replace(tzinfo=_tz.utc)
            n = n.astimezone(et)
        except Exception:
            return None
        if not rules_mod.is_trading_session(n.date()):
            return None
        mins = n.hour * 60 + n.minute
        if not (cls.SESSION_CLOSE_START_ET_MIN <= mins <= cls.SESSION_CLOSE_END_ET_MIN):
            return None
        return str(n.date())

    def _maybe_record_session_close(self, con_id, close_price, entry_debit, quantity,
                                    is_official_mark=True, now=None, activation_gain_pct=None):
        """Public API contract; production-derived narrative omitted."""
        try:
            if not is_official_mark:
                return None
            session_date = self._completed_session_date(now)
            if session_date is None:
                return None
            st = self.state_manager.state
            activation = (self.config.rules.trailing.activation_gain_pct
                          if activation_gain_pct is None else activation_gain_pct)
            before = st.is_trail_armed(con_id)
            rec = st.record_session_close(
                con_id, session_date, close_price, entry_debit, quantity,
                activation)
            if not before and rec["armed_at"] is not None:
                print(f"[TRAIL] con_id={con_id}: trail ARMED after 2 consecutive qualifying "
                      f"closes (session {session_date}, close={close_price:.4f}, "
                      f"activation=+{activation:.1f}%); "
                      f"peak_since_arm seeded from THIS close")
            return rec
        except Exception as e:
            print(f"[WARN] trail session-close bookkeeping failed for con_id={con_id}: {e} "
                  "(continuing; the position stays unarmed)")
            return None

    def _maybe_write_reload_ticket(self, con_id, symbol, trigger, quantity, entry_debit,
                                   fill_px, fill_status, *, fill_key=None, je=None) -> bool:
        """Public API contract; production-derived narrative omitted."""
        try:
            if not bool(getattr(self.config, "reload_enabled", False)):
                return True
            if getattr(trigger, "trigger_type", None) != "take_profit":
                return True
            if not bool(getattr(trigger, "reload", False)):
                return True


            if fill_status != "Filled":
                return True
            from exitmgr import reload_queue as _rq



            je = dict(je or self._journal_entries.get(con_id) or {})
            sp = je.get("spread") or {}
            _px = fill_px if fill_px is not None else getattr(trigger, "current_price", None)
            realized = None
            try:
                if _px is not None and entry_debit is not None:
                    realized = float(_px) * 100 * int(quantity) - float(entry_debit)
            except (TypeError, ValueError):
                realized = None
            _INDEX = {"SPY", "QQQ", "IWM", "DIA"}
            ticket = _rq.make_ticket(
                symbol=symbol,
                thesis=je.get("thesis") or "",
                right=je.get("right"),
                width=sp.get("width"),
                dte_target=je.get("dte_at_entry"),
                structure=("spread" if sp else "single"),
                is_index=(str(symbol).upper() in _INDEX),
                reload_conviction=getattr(trigger, "reload_conviction", None),
                realized_pnl=realized,
                original_debit=(entry_debit if je.get("debit") is None else je.get("debit")),
                ttl_cycles=int(_cfg_num(self.config, "reload_ttl_cycles", 3)),
                interval_seconds=int(_cfg_num(self.config.loop, "interval_seconds", 60)),
                source_fill_key=fill_key,
            )
            created = _rq.ReloadQueue(_rq.queue_path(self.config.journal.path)).add_once(ticket)
            print(f"[RELOAD] {'wrote' if created else 'deduped'} fill-gated reload ticket for "
                  f"{symbol} (reload_conviction={ticket.get('reload_conviction')}, "
                  f"realized={realized})")
            return True
        except Exception as e:
            print(f"[WARN] reload-ticket write skipped for con_id={con_id} ({symbol}): {e} (continuing)")
            return False

    async def _capture_external_fills_safe(self) -> None:
        """Public API contract; production-derived narrative omitted."""
        import copy as _capture_copy
        pending = getattr(self, "_external_fill_capture_task", None)
        if pending is not None and not pending.done():
            return
        now = time.time()
        if now - getattr(self, "_last_extfill_capture", 0.0) < 900:
            return
        self._last_extfill_capture = now
        try:
            ib = getattr(self.ib_conn, "ib", None)
            if ib is None or not self.ib_conn.is_healthy():
                return


            fills = _capture_copy.deepcopy(list(ib.fills()))
            if not fills:
                return
            from exitmgr import exec_capture
            capture_config = _capture_copy.deepcopy(self.config)

            class CachedFills:
                async def reqExecutionsAsync(self, *args, **kwargs):
                    return fills

            def capture():


                return asyncio.run(exec_capture.capture_external_fills(
                    ib=CachedFills(), config=capture_config, lookback_days=0))

            async def run():
                try:
                    result = await asyncio.to_thread(capture)
                    if not result.get("ok"):
                        print(f"[WARN] cached external-fill capture failed: {result.get('note')}")
                    else:
                        print("[EXT-FILL] source=partial-session-cache; no history coverage "
                              f"claim; trades+={result.get('appended', 0)} "
                              f"positions+={result.get('positions_appended', 0)}")
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:
                    print(f"[WARN] cached external-fill capture failed: {exc!r}")

            self._external_fill_capture_task = asyncio.create_task(run())
        except Exception as exc:
            print(f"[WARN] external-fill cache snapshot skipped: {exc}")


    def _events_for(self, symbol):
        """Public API contract; production-derived narrative omitted."""
        try:
            ev = (self._research_context or {}).get("events")
            if not ev:
                return None
            if isinstance(ev, dict):
                return ev.get(symbol)
            text = [ln for ln in str(ev).splitlines() if symbol in ln]
            return text[:3] or None
        except Exception:
            return None

    async def run_cycle(self, dry_run: bool, regime=None, price_stats=None,
                        defer_model: bool = False, research_context=None) -> None:
        """Public API contract; production-derived narrative omitted."""
        self._regime = regime
        self._price_stats = price_stats





        self._research_context = research_context or {}
        cycle_start = datetime.now(timezone.utc)
        print(f"\n{'='*60}")
        print(f"[CYCLE] Starting evaluation cycle at {cycle_start.isoformat()}")
        print(f"[CYCLE] Dry run: {dry_run}")



        self._load_journal()
        self._post_campaign_conflict_alerts()



        await self._capture_external_fills_safe()



        manual_exit_ids = self._read_manual_exits()
        if manual_exit_ids:
            print(f"[CYCLE] manual early-exit requests: {sorted(manual_exit_ids)}")



        if self._check_kill_switch():
            print("[CYCLE] Entry kill switch active - continuing exit management")


        caps_ok, cap_reason = self._check_caps(dry_run)
        if not caps_ok:
            print(f"[CYCLE] Caps exceeded - {cap_reason}. Skipping order placement.")







        try:
            reconcile_ok = await self._reconcile_on_startup()
        except Exception as _re:
            print(f"[CYCLE] per-cycle reconcile errored ({_re}); treating as UNSAFE (no new orders)")
            reconcile_ok = False


            self._reconcile_bad_con_ids = None






        _unjournaled = set(getattr(self, "_unjournaled_con_ids", None) or ())
        if _unjournaled and reconcile_ok:
            print(f"[CYCLE] UNJOURNALED live position(s) {sorted(_unjournaled)}: halting NEW "
                  "ENTRIES while the book cannot be fully accounted for. Their own protective "
                  "exits stay armed -- an untracked position is exactly the one whose stop is "
                  "its only protection. Journal or close them to release entries.")
        self._reconcile_ok = reconcile_ok and not _unjournaled
        if not reconcile_ok:
            print("[CYCLE] reconciliation UNSAFE - clean positions still get stops; only "
                  "reconcile-inconsistent con_ids are withheld (entries suppressed globally)")





        try:
            live_positions, live_shorts, live_stocks = await self._fetch_position_book()
        except Exception as e:
            print(f"[ERROR] Could not fetch positions: {e}")
            return
        if manual_exit_ids:
            manual_exit_ids = self._archive_obsolete_manual_exits(
                manual_exit_ids, live_positions, live_shorts, live_stocks)




        try:
            await self._alert_unfilled_orders()
        except Exception as e:
            print(f"[WARN] unfilled-order alarm errored (continuing): {e}")





        try:
            await self._process_expiries(live_positions, live_shorts=live_shorts)
        except Exception as e:
            print(f"[WARN] expiry terminal-logging errored (continuing): {e}")








        try:
            await self._process_short_assignments(live_shorts, live_stocks)
        except Exception as e:
            print(f"[WARN] short assignment check errored (continuing): {e}")
        try:
            await self._report_short_positions()
        except Exception as e:
            print(f"[WARN] short reporting errored (continuing): {e}")










        try:
            await self._manage_short_positions(live_shorts, {}, dry_run, caps_ok)
        except Exception as e:
            print(f"[WARN] short exit management errored (continuing): {e}")






        try:
            _active = set(live_positions.keys()) | set(self._journal_entries.keys())
            self.state_manager.state.prune_tracking(_active)
        except Exception as e:
            print(f"[WARN] tracking prune errored (continuing): {e}")














        _recovery_book_tick = time.monotonic()
        _recovery_book_at = datetime.now(timezone.utc).isoformat()
        _recovery_book_generation = getattr(self.ib_conn, "_connection_generation", None)
        _order_view = await self.ib_conn.get_open_orders_view(
            short_leg_con_ids=set(getattr(self, "_spread_short_legs", {}).keys()))
        if not _order_view.readable:
            self._order_book_unreadable = _order_view.error
            print(f"[ERROR] Could not fetch open orders: {_order_view.error}; the order book is "
                  "UNKNOWN this cycle -- withholding every close placement (an absent con_id in "
                  "an unread book is not evidence that nothing is resting)")

            self._reconcile_ok = False



            live_open_orders = _order_view




            terminal_changes = set()
        else:
            self._order_book_unreadable = None
            self._note_good_broker_view()
            live_open_orders = _order_view.as_state_dicts()





            try:
                _dupe_closes = self._duplicate_resting_closes(live_open_orders, live_positions)
            except Exception as _de:
                print(f"[WARN] duplicate-resting-close scan errored (continuing): {_de}")
                _dupe_closes = []
            if _dupe_closes:
                print("[CYCLE] DUPLICATE RESTING CLOSES at the broker: "
                      + str([(s, c, list(i)) for s, c, i, _n, _r in _dupe_closes]))
                try:
                    self._post_duplicate_close_alert(_dupe_closes)
                except Exception as _da:
                    print(f"[WARN] duplicate-resting-close alert errored (continuing): {_da}")
            terminal_changes = await self._poll_in_flight_fills(
                live_open_orders, live_positions=live_positions)



        self._entry_order_view = await self._read_entry_order_view()
        if not getattr(self._entry_order_view, "readable", False):
            print(f"[ENTRY-GATE] the ENTRY-side open-order book is UNKNOWN "
                  f"({getattr(self._entry_order_view, 'error', None)}); every protective SELL on "
                  "a long is withheld this cycle -- an unread book is not an empty one, and "
                  "selling against one can close the filled part of an entry whose remainder "
                  "then RECREATES the position at a worse basis with no journal row")
        if terminal_changes:



            try:
                live_positions, live_shorts, live_stocks = await self._fetch_position_book()
                live_open_orders_raw = await self.ib_conn.get_open_orders(
                    short_leg_con_ids=set(getattr(self, "_spread_short_legs", {}).keys()))
                live_open_orders = {
                    od.con_id: {"order_id": od.order_id, "remaining": od.remaining,
                                "perm_id": getattr(od, "perm_id", 0),
                                "client_id": getattr(od, "client_id", None),
                                "order_ref": getattr(od, "order_ref", None),
                                "status": getattr(od, "status", None),
                                "limit_price": getattr(od, "limit_price", None),






                                "order_ids": list(getattr(od, "order_ids", None) or (od.order_id,)),
                                "order_count": int(getattr(od, "order_count", 1))}
                    for od in live_open_orders_raw.values()
                }
                print(f"[CYCLE] Refreshed broker snapshots after terminal changes for "
                      f"con_ids={sorted(terminal_changes)}")
            except Exception as e:
                print(f"[ERROR] Could not refresh positions/orders after terminal fill: {e}; "
                      "skipping further close placement this cycle")
                return

        manual_exit_ids = self._read_manual_exits()


        scope_con_ids = self._get_scope_con_ids(live_positions)
        print(f"[CYCLE] Managing {len(scope_con_ids)} positions (scope={self.config.scope.mode})")



        _quote_ids = {int(cid) for cid in scope_con_ids if int(cid) > 0}
        for _cid in tuple(_quote_ids):
            _sp = (self._journal_entries.get(_cid) or {}).get("spread") or {}
            if _sp.get("short_con_id"):
                _quote_ids.add(int(_sp["short_con_id"]))
        for _inf in self.state_manager.state.in_flight.values():
            for _leg in (getattr(_inf, "submitted_close", None) or {}).get("legs", ()):
                try:
                    _leg_id = int(_leg["con_id"])
                    if _leg_id > 0:
                        _quote_ids.add(_leg_id)
                except (KeyError, TypeError, ValueError):
                    pass
        self._protective_quote_con_ids = _quote_ids
        if (not dry_run and getattr(self.config.rules, "protective_reprice_enabled", False)
                and _order_view.readable and not terminal_changes):
            await self._recover_unfilled_protective_exits(
                live_open_orders, live_positions, _recovery_book_tick,
                _recovery_book_at, _recovery_book_generation, live_shorts=live_shorts)
        elif (not dry_run and getattr(self.config.rules, "protective_reprice_enabled", False)
              and not _order_view.readable):
            for _cid, _inf in self.state_manager.state.in_flight.items():
                self._pending_protective_recovery_alert(_cid, _inf, "broker order book unreadable")


        managed_positions = []
        for con_id in scope_con_ids:
            pos_data = live_positions.get(con_id)
            if pos_data is None:
                continue






            try:
                if int(getattr(pos_data, "quantity", 0) or 0) < 0:
                    print(f"[ERROR] con_id={con_id} is SHORT (quantity="
                          f"{getattr(pos_data, 'quantity', None)}) and reached the long-only "
                          "management path; refusing to manage it (see _report_short_positions)")
                    continue
            except (TypeError, ValueError):
                pass


            in_flight = self.state_manager.state.get_in_flight(con_id)
            if in_flight is not None:
                print(f"[CYCLE] con_id={con_id} already has durable close state "
                      f"(order_id={in_flight.order_id}, remaining={in_flight.remaining_qty}), skipping")
                continue

            if pos_data.avg_cost is None:
                try:
                    _known_basis, _, _ = _resolve_entry_basis(
                        self._journal_entries.get(con_id), pos_data)
                    if not math.isfinite(_known_basis) or _known_basis <= 0:
                        raise ValueError("invalid journal basis")
                except (TypeError, ValueError, OverflowError):
                    print(f"[WARN] con_id={con_id} ({pos_data.symbol}): entry basis UNKNOWN; "
                          "skipping this position's percentage exits, keeping other protection active")
                    try:
                        from exitmgr.trader import audit as _basis_audit
                        _basis_audit(self.config.audit_path, "protective_basis_unknown",
                                     con_id=con_id, symbol=pos_data.symbol,
                                     reason="option cost or multiplier unavailable/unsupported")
                    except Exception:
                        pass
                    continue

            managed_positions.append(pos_data)

        if not managed_positions:
            print("[CYCLE] No positions to evaluate")
            self.state_manager.update_last_cycle()
            return


        manual_processed_ids: Set[int] = set()
        if manual_exit_ids and not dry_run:




            _manual_view = await self.ib_conn.get_open_orders_view(
                short_leg_con_ids=set(getattr(self, "_spread_short_legs", {}).keys()))
            if not _manual_view.readable:


                print(f"[MANUAL-EXIT] [ERROR] open-order book UNKNOWN ({_manual_view.error}); "
                      f"refusing {len(manual_exit_ids)} one-tap MARKET close(s) this cycle "
                      "rather than risk a duplicate SELL. The request(s) remain queued.")
                _loo = _manual_view
                manual_exit_ids = {}
            else:
                _loo = _manual_view.as_state_dicts()
            for _p in managed_positions:
                cid = _p.con_id
                if cid not in manual_exit_ids:
                    continue
                _manual_request = manual_exit_ids[cid]
                je = self._journal_entries.get(cid) or {}
                sym = je.get("symbol", _p.symbol)
                from exitmgr.manual_exit_frontend import request_matches_live
                _request_ok, _request_why = request_matches_live(
                    _manual_request, self, _p)
                if not _request_ok:
                    self._manual_exit_fail(sym, cid, _request_why)
                    continue
                qty = min(_p.quantity, je.get("quantity", _p.quantity))




                try:
                    _qty_ok = int(qty) > 0
                except (TypeError, ValueError):
                    _qty_ok = False
                if not _qty_ok:
                    print(f"[MANUAL-EXIT] [ALERT] con_id={cid} ({sym}): journal quantity "
                          f"{je.get('quantity')!r} against live {_p.quantity} resolves to a "
                          "ZERO-contract close; refusing rather than sending a nonsense "
                          "order. The request remains queued in manual_exits.json.")
                    continue







                _mgate = self._entry_gate(cid, je)
                if not _mgate.allowed:
                    print(f"[MANUAL-EXIT] [ALERT] con_id={cid} ({sym}): one-tap MARKET close "
                          f"WITHHELD -- {'; '.join(_mgate.reasons) or 'no reason given'}. The "
                          "request remains queued.")
                    continue



                if _mgate.manageable_qty is not None and 0 < _mgate.manageable_qty < qty:
                    print(f"[MANUAL-EXIT] con_id={cid} ({sym}): sizing the close to the FILLED "
                          f"quantity {_mgate.manageable_qty}, not {qty}")
                    qty = int(_mgate.manageable_qty)
                full_basis = je.get("entry_fill_debit") or je.get("debit")
                if full_basis is None:
                    full_basis = _p.avg_cost * 100 * _p.quantity




                try:
                    _jq = je.get("quantity")
                    journal_qty = int(_p.quantity if _jq is None else _jq)
                    edebit = float(full_basis) * int(qty) / journal_qty if journal_qty else float(full_basis)
                except (TypeError, ValueError, ZeroDivisionError):
                    edebit = full_basis
                _manual_ctx = {
                    "symbol": sym,
                    "reason": "manual",
                    "trigger_type": "manual",
                    "trigger_message": "early exit via book-review one-tap (market)",
                    "trigger_pnl_pct": 0.0,
                    "close_qty": qty,
                    "position_qty": qty,
                    "entry_debit": edebit,
                    "journal_entry": dict(je),
                    "campaign_binding": self._close_campaign_binding(cid),
                    "manual_request": True,
                    "manual_request_id": _manual_request.get("request_id"),
                    "manual_request_binding_sha": _manual_request.get("binding_sha"),
                    "extra": {
                        "partial": False,
                        "close_qty": qty,
                        "remaining_qty": 0,
                        "rule_fired": "manual",
                        "exit_reasoning": "early exit via book-review one-tap (market)",
                        "mfe_pct": self.state_manager.state.mfe_pct.get(str(cid)),
                    },
                }
                try:
                    res = await self.order_manager.place_close_order(
                        con_id=cid, symbol=sym, quantity=qty, limit_price=0.0, entry_debit=edebit,
                        live_open_orders=_loo, spread=je.get("spread"), market=True,
                        right=je.get("right"), trigger_type="manual",
                        exit_context=_manual_ctx)
                    if res.success:



                        manual_processed_ids.add(cid)
                        _loo[cid] = {"order_id": res.order_id, "remaining": qty}


                        _inf = self.state_manager.state.get_in_flight(cid)
                        _done = bool(_inf is not None and res.trade is not None
                                     and self._finalize_in_flight_exit(cid, _inf, res.trade))
                        print(f"[MANUAL-EXIT] {sym} con_id={cid} MARKET close "
                              f"{'Filled/finalized' if _done else 'placed; request remains pending'}")
                    elif bool(getattr(res, "ambiguous", False)):
                        manual_processed_ids.add(cid)
                        print(f"[MANUAL-EXIT-ACK-UNKNOWN] {sym} con_id={cid} "
                              f"order_id={res.order_id}; request and durable latch remain pending, "
                              "no blind retry")
                    else:
                        self._manual_exit_fail(sym, cid, res.message)
                except Exception as _e:
                    self._manual_exit_fail(sym, cid, repr(_e))

        if manual_processed_ids:
            managed_positions = [p for p in managed_positions
                                 if p.con_id not in manual_processed_ids]
            if not managed_positions:
                print("[CYCLE] All managed positions were handled by manual exits this cycle")
                self.state_manager.update_last_cycle()
                return


        con_ids_to_fetch = [p.con_id for p in managed_positions]
        for p in managed_positions:
            sp = (self._journal_entries.get(p.con_id) or {}).get("spread")
            if sp and sp.get("short_con_id"):
                con_ids_to_fetch.append(int(sp["short_con_id"]))
        self._protective_quote_con_ids.update(int(cid) for cid in con_ids_to_fetch if int(cid) > 0)
        if bool(getattr(self.config, "arm", False)):



            quotes = self._fresh_protective_quotes()
        else:
            try:
                quotes = await asyncio.wait_for(
                    (self.quote_ib_conn or self.ib_conn).fetch_quotes(con_ids_to_fetch),
                    timeout=self.PROTECTIVE_QUOTE_TIMEOUT_S)
            except asyncio.TimeoutError:
                quotes = {}
                print(f"[WARN] fetch_quotes exceeded {self.PROTECTIVE_QUOTE_TIMEOUT_S:.0f}s -- "
                      "continuing this cycle on broker portfolio marks")
            except Exception as e:
                quotes = {}
                print(f"[WARN] Could not fetch quotes ({e}); continuing this cycle on broker "
                      "portfolio marks")



        _cycle_marks = self._portfolio_marks()


        triggers: List[ExitTrigger] = []
        orders_placed_this_cycle = 0
        orders_notional_this_cycle = 0.0




        model_decisions = {}




        mgmt_raw = None
        mgmt_identity = None
        mgmt_attempt_ok = None
        mgmt_failure_reason = None
        views_by_cid = {}




        _mgmt_on = bool(getattr(self.config, "manage_positions", False)) and bool(managed_positions)





        if _mgmt_on:
            try:
                views = self._build_position_views(
                    managed_positions, quotes, self._price_stats,
                    portfolio_marks=_cycle_marks)
                views_by_cid = {v.get("con_id"): v for v in views}
                _qty_by_cid = {p.con_id: getattr(p, "quantity", None) for p in managed_positions}
                self.publish_mgmt_views(views, regime=self._regime, qty_by_cid=_qty_by_cid)
            except Exception as e:
                print(f"[POSMGMT] could not build position views ({e}); using static rules")
                views, views_by_cid, _qty_by_cid = [], {}, {}







        if _mgmt_on and defer_model:
            model_decisions, mgmt_raw, mgmt_identity = self._consume_model_decisions(
                views_by_cid, _qty_by_cid)
            mgmt_attempt_ok = getattr(self, "_mgmt_attempt_ok", None)
            mgmt_failure_reason = getattr(self, "_mgmt_failure_reason", None)
            if not model_decisions:
                print("[POSMGMT] no fresh model decisions this cycle (static exit rules apply)")
        elif _mgmt_on:
            try:




                model_decisions, _mgmt_meta = await asyncio.to_thread(
                    assess_positions,
                    self.config.llm_endpoint, self.config.llm_model,
                    views, market_regime=self._regime, return_meta=True)
                _meta = _mgmt_meta if isinstance(_mgmt_meta, dict) else {}


                mgmt_attempt_ok = _meta.get("attempt_ok", True)
                mgmt_failure_reason = _meta.get("failure_reason")
                if mgmt_attempt_ok is not True:
                    model_decisions = {}
                    print(f"[POSMGMT] assessment failed "
                          f"({mgmt_failure_reason or 'unspecified_model_failure'}); "
                          "using static rules")



                model_decisions, _refused = self._narrow_to_remit(model_decisions)
                if _refused:
                    print(f"[POSMGMT] remit={list(self.MGMT_REMIT_ACTIONS)}: refused "
                          f"{len(_refused)} out-of-remit proposal(s) for con_ids "
                          f"{sorted(_refused)} -- recorded, not acted on")
                mgmt_raw = (_mgmt_meta or {}).get("raw")
                mgmt_identity = (_mgmt_meta or {}).get("model_identity")












                if "model_identity_source" in _meta:
                    self._mgmt_identity_source = _meta["model_identity_source"]
                else:
                    self._mgmt_identity_source = (
                        _evcap.IDENTITY_SOURCE_META if mgmt_identity is not None
                        else _evcap.IDENTITY_SOURCE_UNKNOWN)
                if model_decisions:
                    rg = (self._regime or {}).get("regime", "n/a")
                    print(f"[POSMGMT] regime={rg} model decisions for con_ids {sorted(model_decisions)}")
            except Exception as e:
                print(f"[POSMGMT] assessment skipped ({e}); using static rules")
                model_decisions = {}
                self._mgmt_identity_source = None
                mgmt_attempt_ok = False
                mgmt_failure_reason = f"{type(e).__name__}: {e}"[:500]




        try:





            _mark = _cycle_marks
        except Exception as _e:
            print(f"[WARN] portfolio marking unavailable ({_e}); using streaming quotes")
            _mark = {}



        withheld_stops: List[tuple] = []





        blind_positions: List[tuple] = []



        entry_gate_withheld: List[tuple] = []
        for pos_data in managed_positions:
            con_id = pos_data.con_id
            _campaign_conflict = con_id in self._campaign_conflicts
            _conflict_emergency = None
            if _campaign_conflict:
                _conflict_emergency, _conflict_why = self._campaign_conflict_emergency_context(
                    pos_data, live_shorts)
                model_decisions.pop(con_id, None)
                if _conflict_emergency is None:
                    print(f"[CAMPAIGN-CONFLICT] con_id={con_id} ({pos_data.symbol}): "
                          f"protection is UNVERIFIABLE ({_conflict_why}); refusing every money "
                          "mutation until the campaign is repaired")
                    continue
            quote = quotes.get(con_id)













            _broker_mark = _mark.get(con_id)
            _quote_px = _usable_px((quote or {}).get("price"))
            current_price = next((p for p in (_broker_mark, _quote_px) if p is not None), None)
            if current_price is None:
                _stale_n = int(self._price_stale_cycles.get(con_id, 0)) + 1
                self._price_stale_cycles[con_id] = _stale_n
                _why = ("no streaming quote and no broker mark" if quote is None
                        else "the quote carries no usable price and there is no broker mark")
                print(f"[WARN] con_id={con_id} ({getattr(pos_data, 'symbol', '')}): UNPRICEABLE "
                      f"this cycle ({_why}) -- no stop/trail/time evaluation is possible; "
                      f"skipping")


                if _stale_n >= 2 or not bool(getattr(self.config, "arm", False)):
                    blind_positions.append(
                        (getattr(pos_data, "symbol", ""), con_id,
                         f"{_why}; {_stale_n} consecutive stale cycles"))
                continue



            _official_mark = con_id in _mark




            spread = (self._journal_entries.get(con_id) or {}).get("spread")
            if spread and spread.get("short_con_id"):
                scid = int(spread["short_con_id"])




                short_px = next(
                    (p for p in (_mark.get(scid),
                                 _usable_px((quotes.get(scid) or {}).get("price")))
                     if p is not None), None)
                if short_px is None:
                    _why = (f"spread short leg {scid} is unpriceable "
                            f"(no broker mark, no usable quote) -- the NET cannot be valued")
                    _sym = getattr(pos_data, "symbol", "")
                    _stale_n = int(self._price_stale_cycles.get(con_id, 0)) + 1
                    self._price_stale_cycles[con_id] = _stale_n
                    print(f"[WARN] con_id={con_id} ({_sym}): {_why}; skipping")
                    if _stale_n >= 2 or not bool(getattr(self.config, "arm", False)):
                        blind_positions.append(
                            (_sym, con_id,
                             f"{_why}; {_stale_n} consecutive stale cycles"))
                    continue

                _official_mark = _official_mark and (scid in _mark)
                current_price = current_price - short_px


            self._price_stale_cycles.pop(con_id, None)


            if con_id in self._journal_entries:






                journal_debit = self._journal_entries[con_id].get("debit")
                if journal_debit is None:
                    print(f"[WARN] con_id={con_id}: journal row carries NO `debit` -- entry "
                          f"basis is UNKNOWN, not zero. Every %-threshold for this position "
                          f"(target/stop/trail) is unevaluable until the row is repaired.")
                    journal_debit = 0.0






                _jefd = self._journal_entries[con_id].get("entry_fill_debit")
                _jbs = self._journal_entries[con_id].get("basis_source")
                if _jefd is not None and (_jbs == "fill" or _jefd is not None):
                    try:
                        _jefd_f = float(_jefd)
                        if _jefd_f == _jefd_f and _jefd_f > 0:
                            journal_debit = _jefd_f
                    except (TypeError, ValueError):
                        pass
                symbol = self._journal_entries[con_id].get("symbol", pos_data.symbol)
                quantity_in_journal = self._journal_entries[con_id].get("quantity", pos_data.quantity)





                if quantity_in_journal is None:
                    quantity_in_journal = pos_data.quantity

                quantity = min(pos_data.quantity, quantity_in_journal)







                if quantity_in_journal and quantity_in_journal > 0 and quantity != quantity_in_journal:
                    entry_debit = journal_debit * quantity / quantity_in_journal
                else:
                    entry_debit = journal_debit
            else:

                entry_debit = pos_data.avg_cost * 100 * pos_data.quantity
                symbol = pos_data.symbol
                quantity = pos_data.quantity
                quantity_in_journal = pos_data.quantity
            if _campaign_conflict:
                entry_debit = _conflict_emergency["entry_debit"]
                symbol = _conflict_emergency["symbol"]
                quantity = _conflict_emergency["quantity"]
                quantity_in_journal = quantity






            try:
                _v = next((v for v in (getattr(self, "_mgmt_views", None) or [])
                           if v.get("con_id") == con_id), None)
                if _v and _v.get("_basis") and entry_debit > 0:
                    _vb = float(_v["_basis"])
                    _skew = _vb / entry_debit - 1.0
                    if abs(_skew) > 0.005:
                        print(f"[BASIS-DIVERGENCE] con_id={con_id} ({symbol}): model reasoned over "
                              f"basis={_vb:.2f} (src={_v.get('_basis_source')}, mark={_v.get('_mark')}, "
                              f"qty={_v.get('_qty')}, pnl={_v.get('pnl_pct')}%) but the rules enforce "
                              f"basis={entry_debit:.2f} (qty={quantity}) -- model P&L skewed "
                              f"{_skew * 100:+.1f}%")
                        try:
                            from exitmgr.trader import audit as _audit_event
                            import os as _os
                            _ap = _os.path.join(
                                _os.path.dirname(self.config.journal.path) or ".", "audit.jsonl")
                            _audit_event(_ap, "basis_divergence", con_id=con_id, symbol=symbol,
                                         view_basis=_vb, rule_basis=round(entry_debit, 4),
                                         view_basis_source=_v.get("_basis_source"),
                                         view_mark=_v.get("_mark"), view_pnl_pct=_v.get("pnl_pct"),
                                         view_qty=_v.get("_qty"), rule_qty=quantity,
                                         skew_pct=round(_skew * 100, 3))
                        except Exception:
                            pass
            except Exception:
                pass






            peaks = self.state_manager.state.peak_prices
            k = str(con_id)

            dte = days_to_expiry(getattr(pos_data, "expiry", ""))




            _enrich = None
            try:
                _je_e = self._journal_entries.get(con_id) or {}
                _q_e = quotes.get(con_id) or {}
                _dec_e = model_decisions.get(con_id) or {}







                _mgmt_action = _dec_e.get("action")
                _mgmt_reason = _dec_e.get("reason")
                if (_mgmt_action is None and mgmt_attempt_ok is True and mgmt_raw
                        and (con_id in views_by_cid)):
                    _mgmt_action = "hold"
                    _mgmt_reason = "implicit hold (omitted from model decisions this cycle)"
                _pnl_e = ((current_price * 100 * quantity - entry_debit) / entry_debit * 100
                          if entry_debit and entry_debit > 0 else None)








                _tp_e = getattr(self._position_rules(_je_e), "profit_target_pct", None)
                _sl_e = _je_e.get("stop_pct") or getattr(self.config.rules, "stop_pct", None)
                _days_held = _calendar_days_elapsed(_je_e.get("fill_ts") or _je_e.get("ts"))
                _und = None
                try:
                    _und = ((self._price_stats or {}).get(symbol) or {}).get("last")
                except Exception:
                    _und = None






                _bid = _ask = _spw = None
                try:
                    def _f(x):
                        return float(x) if (x is not None and x == x and float(x) > 0) else None
                    _lb, _la = _f(_q_e.get("bid")), _f(_q_e.get("ask"))
                    if spread and spread.get("short_con_id"):
                        _sq = quotes.get(spread.get("short_con_id")) or {}
                        _sb, _sa = _f(_sq.get("bid")), _f(_sq.get("ask"))
                        if None not in (_lb, _la, _sb, _sa):
                            _bid, _ask = _lb - _sa, _la - _sb
                    else:
                        _bid, _ask = _lb, _la
                    if _bid is not None and _ask is not None and (_bid + _ask) > 0:
                        _spw = round((_ask - _bid) / ((_ask + _bid) / 2.0) * 100.0, 2)
                except Exception:
                    _bid = _ask = _spw = None
                _enrich = {
                    "underlying": _und,
                    "bid": _bid, "ask": _ask, "spread_width_pct": _spw,
                    "iv": _q_e.get("iv"), "delta": _q_e.get("delta"),
                    "gamma": _q_e.get("gamma"), "theta": _q_e.get("theta"), "vega": _q_e.get("vega"),
                    "dte": dte, "days_held": _days_held,
                    "dist_to_tp_pct": (round(float(_tp_e) - _pnl_e, 2)
                                       if (_tp_e is not None and _pnl_e is not None) else None),
                    "dist_to_sl_pct": (round(_pnl_e + abs(float(_sl_e)), 2)
                                       if (_sl_e is not None and _pnl_e is not None) else None),
                    "mgmt_action": _mgmt_action,





                    "mgmt_proposed_action": _dec_e.get("proposed_action"),
                    "mgmt_reason": _mgmt_reason,




                    "mgmt_raw": mgmt_raw,
                    "mgmt_attempt_ok": mgmt_attempt_ok,
                    "mgmt_failure_reason": mgmt_failure_reason,
                    "mgmt_model_identity": mgmt_identity,






                    "mgmt_model_identity_source": _identity_source(
                        mgmt_identity, getattr(self, "_mgmt_identity_source", None)),
                    "mgmt_input": views_by_cid.get(con_id),



                    "is_net_spread": bool(spread and spread.get("short_con_id")),








                    "price": round(float(current_price), 4),
                    "value": (round(float(current_price) * 100 * int(quantity), 2)
                              if quantity is not None else None),
                }
            except Exception as _ee:
                print(f"[WARN] mark enrichment build failed for con_id={con_id} (recording plain mark): {_ee}")
                _enrich = None
            _mark_pnl_pct = self.state_manager.state.record_mark(
                con_id, current_price, entry_debit, quantity, path_cap=self.MARK_PATH_CAP,
                enrich=_enrich)
            try:
                from exitmgr import event_capture as _evt
                _evt.on_position_mark(con_id, symbol=symbol, enrich=_enrich, pnl_pct=_mark_pnl_pct)
            except Exception:
                pass













            _auto_cfg_arm = getattr(self.config.rules, "auto_trail", None)










            _atr_lv = (None if _campaign_conflict else
                       self._atr_levels_for(con_id, symbol, entry_debit, quantity,
                                            (_enrich or {}).get("iv"), dte,
                                            locals().get("spread")))
            _trail_info = self._trail_activation_for(con_id, _atr_lv, entry_debit,
                                                       quantity, quotes, locals().get("spread"))
            _arm_bar = _trail_info["activation_gain_pct"]
            _qualification = _trail_info.get("qualification")
            _new_arm_allowed = (_qualification is None or _qualification["qualified"])
            _qualified_price = (_qualification or {}).get("executable_price")
            _arming_price = (_qualified_price if _qualification is not None else peaks.get(k))
            if _new_arm_allowed and not _campaign_conflict:
                self._maybe_record_session_close(con_id,
                    _qualified_price if _qualification is not None else current_price,
                    entry_debit, quantity, is_official_mark=_official_mark,
                    activation_gain_pct=_arm_bar if _trail_info["dynamic"] else None)
            elif not _campaign_conflict and not self.state_manager.state.is_trail_armed(con_id):
                _reason = _qualification["reason"]
                if (current_price * 100 * quantity > entry_debit
                        and _reason != "current_executable_gain_below_activation"):
                    self._post_new_trail_qualification_alert(symbol, con_id, _reason)
            if _qualification is not None and _qualification["qualified"]:
                self._clear_trail_qualification_alert(con_id)
            if _trail_info["dynamic"]:
                print(f"[TRAIL-LEVEL] con_id={con_id} ({symbol}) activation={_arm_bar:.4f}% "
                      f"source={_trail_info['source']} friction={_trail_info['friction_floor_pct']}")
            if (not _campaign_conflict and _new_arm_allowed and _auto_cfg_arm is not None
                    and getattr(_auto_cfg_arm, "enabled", False)):
                if self.state_manager.state.arm_on_peak_gain(
                        con_id, _arming_price, entry_debit, quantity,
                        _arm_bar):
                    _keep = 1 - max(float(self.config.rules.trailing.giveback_fraction),
                                    float(_auto_cfg_arm.giveback_fraction))
                    print(f"[EVAL] con_id={con_id} ({symbol}): BIG-GAIN TRAIL ARMED off peak "
                          f"{_arming_price} (>= {float(_arm_bar):.1f}% gain"
                          f"{' [ATR]' if _atr_lv else ''}); "
                          f"floor keeps {_keep:.0%} of the peak gain")


            if (not _campaign_conflict
                    and (_qualification is None or _qualification["quote_qualified"])):
                self.state_manager.state.record_trail_peak(con_id,
                    _qualified_price if _qualification is not None else current_price)
            _trail_armed = self.state_manager.state.is_trail_armed(con_id)
            _peak_since_arm = self.state_manager.state.trail_peak_since_arm(con_id)
            if _campaign_conflict:
                _trail_armed = False
                _peak_since_arm = None







            _trail_atr_lv = (_qualification if _qualification is not None else _atr_lv)
            if (_trail_armed and _trail_atr_lv and _peak_since_arm
                    and (_qualification is None or _qualification.get("net_delta") is not None)):
                try:
                    from exitmgr import atr_levels as _al
                    _cfgl = self.config.rules.atr_levels
                    _gb_new = _al.atr_giveback(
                        _trail_atr_lv["entry_per_share"], _peak_since_arm, _trail_atr_lv["atr"],
                        _trail_atr_lv["net_delta"], float(_cfgl.k_trail),
                        float(_cfgl.gb_min), float(_cfgl.gb_max))
                    if _gb_new is not None and self.state_manager.state.ratchet_trail_params(
                            con_id, (_arm_bar if _trail_info["dynamic"] else
                                     _atr_lv["activation_gain_pct"]), _gb_new):
                        print(f"[EVAL] con_id={con_id} ({symbol}): TRAIL RATCHETED UP -> "
                              f"giveback {_gb_new:.0%} "
                              f"({_cfgl.k_trail:.1f} ATR below peak {_peak_since_arm:.4f})")
                except Exception as _re:
                    print(f"[ATR] con_id={con_id}: trail ratchet skipped ({_re})")


            je = self._journal_entries.get(con_id) or {}
            if _campaign_conflict:



                je = {"contract_id": con_id, "symbol": symbol, "quantity": quantity}



            rules = self._position_rules(je)







            rules = _enforce_airtight_stop(rules)
            if _campaign_conflict:
                from dataclasses import replace as _conflict_replace
                rules = _conflict_replace(
                    rules, stop_pct=_conflict_emergency["stop_pct"],
                    profit_target_pct=None, time_stop_days=None,
                    trailing=_conflict_replace(rules.trailing, enabled=False),
                    scale_out=_conflict_replace(rules.scale_out, enabled=False))































            try:
                if (not _campaign_conflict and _atr_lv and _atr_lv.get("stop_pct")
                        and not self._is_credit_row(je, quantity)):
                    from dataclasses import replace as _rp_atr
                    _st_atr = self.state_manager.state
                    _eps_now = float(_atr_lv.get("entry_per_share") or 0.0)












                    _basis = atr_stop_basis(je.get("decision_id"), _eps_now)
                    if not _basis:





                        print(f"[ATR] con_id={con_id} ({symbol}): no usable entry basis "
                              f"(eps={_atr_lv.get('entry_per_share')!r}) -- static stop")
                        raise _AtrStopSkip
                    _rec = _st_atr.atr_stop_decision(con_id, _basis)
                    _atr_stop = round(float(_atr_lv["stop_pct"]), 1)



                    if _rec is not None and not _rec["grandfathered"] and _rec["stop_pct"]:
                        _inherited = float(_rec["stop_pct"])
                        _cs_now = float(getattr(rules, "stop_pct", 0.0) or 0.0)
                        if _cs_now <= 0 or _inherited < _cs_now:
                            rules = _rp_atr(rules, stop_pct=_inherited)
                            print(f"[ATR] con_id={con_id} ({symbol}): ATR stop "
                                  f"{_inherited:.1f}% INHERITED (adopted "
                                  f"{_rec.get('decided_at') or 'on an earlier cycle'})")
                    _cur_stop = float(getattr(rules, "stop_pct", 0.0) or 0.0)
                    if _cur_stop > 0 and _atr_stop >= _cur_stop:







                        _cap_cfg = float(self.config.rules.stop_pct)
                        print(f"[ATR] con_id={con_id} ({symbol}): ATR stop {_atr_stop:.1f}% is "
                              f"not tighter than {_cur_stop:.1f}% -- keeping it"
                              + (f" (pinned at the {_cap_cfg:.0f}% cap)"
                                 if abs(_atr_stop - _cap_cfg) < 0.05 else ""))
                    if _cur_stop > 0 and _atr_stop < _cur_stop:
                        _adopt = True
                        if _rec is not None and _rec["grandfathered"]:



                            _adopt = False
                            print(f"[ATR] con_id={con_id} ({symbol}): stop {_atr_stop:.1f}% "
                                  f"GRANDFATHERED PERMANENTLY at introduction "
                                  f"({_rec.get('decided_at') or 'an earlier cycle'}) -- this "
                                  f"position keeps the {_cur_stop:.1f}% it was opened under")
                        elif _rec is None:









                            _cp = None
                            try:
                                _cp = float(current_price)
                            except (TypeError, ValueError):
                                _cp = None
                            _floor = _eps_now * (1.0 - _atr_stop / 100.0)
                            if (_eps_now > 0 and _cp is not None and _cp == _cp
                                    and _cp <= _floor):
                                _adopt = False
                                _st_atr.introduce_atr_stop(con_id, _basis, grandfathered=True)
                                print(f"[ATR] con_id={con_id} ({symbol}): stop "
                                      f"{_atr_stop:.1f}% GRANDFATHERED PERMANENTLY (mark "
                                      f"{_cp:.2f} was already at/below the {_floor:.2f} ATR "
                                      f"floor when this stop was introduced) -- keeping "
                                      f"{_cur_stop:.1f}%")
                        if _adopt:




                            rules = _rp_atr(rules, stop_pct=_atr_stop)
                            if _rec is None:
                                _st_atr.introduce_atr_stop(con_id, _basis, grandfathered=False,
                                                           stop_pct=_atr_stop)
                            else:
                                _st_atr.tighten_atr_stop(con_id, _basis, _atr_stop)
                            print(f"[ATR] con_id={con_id} ({symbol}): stop {_cur_stop:.1f}% -> "
                                  f"{rules.stop_pct:.1f}%  ({float(_atr_lv['net_delta']):.3f} net "
                                  f"delta x {float(_atr_lv['atr']):.3f} ATR x "
                                  f"{float(_atr_lv.get('k_eff') or 0.0):.2f}k over "
                                  f"{float(_atr_lv.get('hold_days') or 0.0):.1f}d hold)"
                                  + (f"  [LAPSED {float(_atr_lv['lapse_over_days']):.1f}d past "
                                     f"its window -- horizon shrunk]"
                                     if _atr_lv.get("lapse_over_days") else "")
                                  + (f"  [WIND-DOWN: DTE {float(_atr_lv.get('dte') or 0):.0f}, "
                                     f"{float(_atr_lv['wind_down_room']):.1f}d of room left]"
                                     if _atr_lv.get("wind_down_room") else ""))
            except _AtrStopSkip:
                pass
            except Exception as _ase:
                print(f"[ATR] con_id={con_id}: stop sizing skipped ({_ase})")


            forced = None







            _shadow = bool(getattr(self.config, "manage_positions_shadow_only", False))
            decision = None if (_shadow or _campaign_conflict) else model_decisions.get(con_id)
            if decision and decision.get("action", "hold") != "hold":
                rules, forced = self._apply_decision(
                    rules, decision, current_price, entry_debit, quantity, con_id, symbol,
                    regime=self._regime)


















            _trail_cfg_flag = str(con_id) in self.state_manager.state.trail_configured
            _trail_state_ok = self._model_trail_state_has_authority()
            if _trail_cfg_flag and not _trail_state_ok:
                if con_id not in self._trail_state_ignored_logged:
                    self._trail_state_ignored_logged.add(con_id)
                    print(f"[SHADOW] con_id={con_id} ({symbol}): IGNORING persisted "
                          f"model-authored trail_configured -- the take-profit ceiling stands "
                          f"and the deterministic rules alone govern this position")
            rules = self._reconcile_ceiling_backstop(
                rules, decision,
                configured=(_trail_cfg_flag and _trail_state_ok))





            _effective_auto = (None if _campaign_conflict else
                               getattr(self.config.rules, "auto_trail", None))
            if _trail_info["dynamic"]:
                from dataclasses import replace as _dynamic_replace
                rules = self._with_trail_activation(rules, _arm_bar)
                if _qualification is not None:
                    rules = _dynamic_replace(rules, trailing=_dynamic_replace(
                        rules.trailing, activation_gain_pct=_arm_bar))
                if _effective_auto is not None:
                    _effective_auto = _dynamic_replace(_effective_auto, activation_gain_pct=_arm_bar)
            rules, _auto_armed = self._apply_auto_trail(
                rules, _effective_auto,
                peaks.get(k), entry_debit, quantity,
                armed=_trail_armed, peak_since_arm=_peak_since_arm)
            if _auto_armed:
                print(f"[EVAL] con_id={con_id} ({symbol}): auto-trail safety floor active "
                      f"(keep {(1 - rules.trailing.giveback_fraction):.0%} of peak gain)")






            if _trail_armed:
                from dataclasses import replace as _rp_pin
                _pin = self.state_manager.state.pinned_trail_params(con_id)
                if _pin is None:
                    if self.state_manager.state.pin_trail_params(
                            con_id, rules.trailing.activation_gain_pct,
                            rules.trailing.giveback_fraction):
                        print(f"[EVAL] con_id={con_id} ({symbol}): TRAIL PARAMS PINNED at "
                              f"activation {rules.trailing.activation_gain_pct:.0f}%, giveback "
                              f"{rules.trailing.giveback_fraction:.0%} -- fixed for this position")
                else:








                    _pin_act, _pin_gb = self._bound_pinned_trail(_pin, rules.trailing)
                    if (rules.trailing.activation_gain_pct != _pin_act
                            or rules.trailing.giveback_fraction != _pin_gb):
                        rules = _rp_pin(rules, trailing=_rp_pin(
                            rules.trailing,
                            activation_gain_pct=_pin_act,
                            giveback_fraction=_pin_gb))


            _protected_floor = self.state_manager.state.trail_protected_floor(con_id)
            if _trail_armed:
                _next_floor = self.state_manager.state.ratchet_trail_floor(
                    con_id, entry_debit, quantity, rules.trailing.giveback_fraction)
                if _next_floor != _protected_floor:
                    self.state_manager.save()
                _protected_floor = _next_floor


            already_trimmed = str(con_id) in self.state_manager.state.scaled_out



















            _bs_candidate = None
            if forced is None and not _trail_armed and not _campaign_conflict:
                try:
                    _bs_cfg = getattr(self.config.rules, "atr_levels", None)
                    if (_bs_cfg is not None
                            and getattr(_bs_cfg, "hold_backstop_enabled", False)
                            and not self._is_credit_row(je, quantity)):
                        try:
                            _bs_hold = float(je.get("intended_hold_days") or 0.0)
                        except (TypeError, ValueError):
                            _bs_hold = 0.0
                        if _bs_hold <= 0:
                            _bs_hold = float(max(1, -(-int(dte or 1) // 8)))
                        _bs_held = _calendar_days_elapsed(je.get("ts"))
                        _bs_mult = _cfg_num(_bs_cfg, "hold_backstop_multiple", 4.0)
                        if (_bs_held is not None and _bs_hold > 0 and _bs_mult > 0
                                and float(entry_debit) > 0
                                and _bs_held > _bs_hold * _bs_mult):
                            from exitmgr.rules import ExitTrigger as _ET
                            _bs_q = abs(int(quantity))
                            _bs_val = float(current_price) * 100.0 * _bs_q
                            _bs_pnl = (_bs_val - float(entry_debit)) / float(entry_debit) * 100.0
















                            if _bs_pnl > 0.0:
                                print(f"[HOLD-BACKSTOP] con_id={con_id} ({symbol}): "
                                      f"{_bs_held:.1f}d past {_bs_mult:.1f}x its {_bs_hold:.0f}d "
                                      f"window but IN PROFIT ({_bs_pnl:+.1f}%) -- not reclaiming; "
                                      f"stop and trail govern")
                            else:
                                _bs_candidate = _ET(
                                    con_id=con_id, trigger_type="time_stop",
                                    current_price=float(current_price),
                                    entry_debit=float(entry_debit),
                                    current_value=_bs_val, pnl_pct=_bs_pnl,
                                    message=(f"Hold backstop: held {_bs_held:.1f}d > "
                                             f"{_bs_mult:.1f}x the {_bs_hold:.0f}d intended hold, "
                                             f"trail never armed, not in profit "
                                             f"({_bs_pnl:+.1f}%) -- reclaiming capital"))
                                print(f"[HOLD-BACKSTOP] con_id={con_id} ({symbol}): "
                                      f"{_bs_candidate.message}")
                except Exception as _bse:
                    print(f"[HOLD-BACKSTOP] con_id={con_id}: check skipped ({_bse})")
            if forced is not None:
                trigger = forced
            else:
                if not _campaign_conflict:
                    self._shadow_resident_protection(
                        con_id, symbol, quantity, entry_debit, rules, je)
                trigger = evaluate_position(
                    con_id=con_id,
                    symbol=symbol,
                    quantity=quantity,
                    entry_debit=entry_debit,
                    current_price=current_price,
                    days_to_expiry=dte,
                    peak_price=peaks.get(k),
                    rules=rules,
                    already_trimmed=already_trimmed,
                    trail_armed=_trail_armed,
                    peak_since_arm=_peak_since_arm,
                    protected_floor_price=_protected_floor,
                )
                if (trigger is None and _trail_armed and rules.trailing.enabled
                        and _qualification is not None and _qualification["quote_qualified"]):
                    from exitmgr.rules import evaluate_trailing_stop as _executable_trail
                    trigger = _executable_trail(
                        _qualified_price, entry_debit, quantity, _peak_since_arm,
                        rules.trailing.activation_gain_pct, rules.trailing.giveback_fraction,
                        armed=True, protected_floor_price=_protected_floor)
                    if trigger is not None:
                        trigger.con_id = con_id
                        trigger.message += " [fresh executable bid-based trail]"


                if trigger is None and _bs_candidate is not None:
                    trigger = _bs_candidate
                    print(f"[HOLD-BACKSTOP] con_id={con_id} ({symbol}): no other rule fired -- "
                          f"applying the backstop")




            if _shadow:
                self._record_divergence_episode(
                    con_id, symbol, je, model_decisions.get(con_id), trigger)

            if trigger:
                trigger.con_id = con_id
                triggers.append(trigger)









                _qf = getattr(trigger, "quantity_fraction", 1.0)
                if _qf is None:
                    _qf = 1.0
                if _qf >= 1.0:
                    close_qty = quantity
                else:
                    close_qty = max(1, min(quantity - 1, round(quantity * _qf)))
                is_partial = close_qty < quantity
                print(f"[EVAL] con_id={con_id} ({symbol}): {trigger.message} (pnl={trigger.pnl_pct:.2f}%)"
                      + (f" [SCALE-OUT trim {close_qty}/{quantity}, keep {quantity - close_qty} runner]"
                         if is_partial else ""))







                _bad = self._reconcile_bad_con_ids


                _reconcile_blocked = ((_bad is None) or (con_id in _bad)
                                      or self._order_book_unreadable is not None)
                _protective = self._is_protective_exit(trigger)



                if _protective and _reconcile_blocked and not dry_run:
                    withheld_stops.append((symbol, con_id, trigger.trigger_type))
                if (not dry_run and (caps_ok or _protective) and not _reconcile_blocked):





                    _egate = self._entry_gate(con_id, je)
                    if not _egate.allowed:
                        print(f"[ENTRY-GATE] con_id={con_id} ({symbol}): protective SELL WITHHELD "
                              f"-- {'; '.join(_egate.reasons) or 'no reason given'}")
                        entry_gate_withheld.append(
                            (symbol, con_id, getattr(trigger, "trigger_type", "exit")))
                        continue











                    _mq = _egate.manageable_qty
                    if _mq is not None and _mq <= 0:
                        print(f"[ENTRY-GATE] con_id={con_id} ({symbol}): the opening order is "
                              f"terminal with ZERO manageable contracts; nothing to close")
                        entry_gate_withheld.append(
                            (symbol, con_id, getattr(trigger, "trigger_type", "exit")))
                        continue
                    if _mq is not None and _mq < close_qty:
                        print(f"[ENTRY-GATE] con_id={con_id} ({symbol}): sizing the close to the "
                              f"FILLED quantity {_mq}, not {close_qty}")
                        close_qty = int(_mq)
                        is_partial = close_qty < quantity




                    if (not _protective
                            and orders_placed_this_cycle >= self.config.caps.max_orders_per_cycle):
                        print(f"[CYCLE] Per-cycle order cap reached: {orders_placed_this_cycle} >= {self.config.caps.max_orders_per_cycle}")
                        continue


                    order_notional = current_price * 100 * close_qty
                    if (not _protective and orders_notional_this_cycle + order_notional
                            > self.config.caps.max_notional_per_day):
                        print(f"[CYCLE] Would exceed daily notional cap, skipping order for con_id={con_id}")
                        continue







                    market_flag = getattr(self.config.rules, "exit_market_orders", False)
                    if trigger.trigger_type == "time_stop":
                        _dec = model_decisions.get(con_id) or {}







                        _thesis_intact = (
                            False if bool(getattr(self.config,
                                                  "manage_positions_shadow_only", False))
                            else _dec.get("action", "hold") in ("hold", "arm_trail"))
                        if trigger.pnl_pct >= 0 or _thesis_intact:
                            market_flag = False
                            print(f"[CYCLE] time-stop MANAGED exit for {symbol} "
                                  f"(green={trigger.pnl_pct >= 0}, thesis_intact={_thesis_intact}) -> LIMIT at mark")








                    _is_spread = bool(spread and spread.get("short_con_id"))
                    _raw_bid = None if _is_spread else (quote or {}).get("bid")
                    _close_bid = (_raw_bid
                                  if (_raw_bid is not None and _raw_bid == _raw_bid and _raw_bid > 0)
                                  else None)


















                    _close_combo_bid = None
                    if _is_spread and getattr(self.config.rules, "spread_exit_bid_anchor", False):
                        try:
                            _scid = int(spread["short_con_id"])
                            _lb = (quote or {}).get("bid")
                            _sa = (quotes.get(_scid) or {}).get("ask")
                            if (_lb is not None and _sa is not None
                                    and _lb == _lb and _sa == _sa):
                                _net = round(float(_lb) - float(_sa), 6)


                                if _net > 0:
                                    _close_combo_bid = _net
                        except (KeyError, TypeError, ValueError):
                            _close_combo_bid = None



                    closed_basis = (entry_debit if close_qty == quantity
                                    else (entry_debit * close_qty / quantity
                                          if quantity else entry_debit))
                    _q = quotes.get(con_id) or {}
                    _exit_ctx = {
                        "symbol": symbol,
                        "reason": self._exit_reason(trigger),
                        "trigger_type": getattr(trigger, "trigger_type", None),
                        "trigger_message": getattr(trigger, "message", None),
                        "trigger_pnl_pct": getattr(trigger, "pnl_pct", None),
                        "triggered_at": datetime.now(timezone.utc).isoformat(),
                        "trigger_threshold_pct": (
                            -abs(float(rules.stop_pct))
                            if getattr(trigger, "trigger_type", None) == "stop"
                            else None),
                        "trigger_threshold_price": (
                            round((closed_basis / (100.0 * close_qty))
                                  * (1.0 - abs(float(rules.stop_pct)) / 100.0), 6)
                            if getattr(trigger, "trigger_type", None) == "stop" and close_qty
                            else (_protected_floor
                                  if getattr(trigger, "trigger_type", None) == "trailing_stop"
                                  else None)),
                        "trigger_threshold_basis": (
                            "long_debit_return_pct"
                            if getattr(trigger, "trigger_type", None) == "stop"
                            else ("ratcheted_trailing_floor_price"
                                  if getattr(trigger, "trigger_type", None) == "trailing_stop"
                                  else None)),
                        "reload": bool(getattr(trigger, "reload", False)),
                        "reload_conviction": getattr(trigger, "reload_conviction", None),
                        "close_qty": close_qty,
                        "position_qty": quantity,
                        "entry_debit": closed_basis,
                        "journal_entry": dict(je),
                        "campaign_binding": self._close_campaign_binding(con_id),
                        "campaign_conflict_authority": (
                            _conflict_emergency.get("authority")
                            if _campaign_conflict and _conflict_emergency else None),
                        "decision_id": je.get("decision_id"),
                        "entry_model_identity": je.get("model_identity"),
                        "entry_model_identity_source": _identity_source(
                            je.get("model_identity"), je.get("model_identity_source")),
                        "exit_model_identity": mgmt_identity,
                        "exit_model_identity_source": _identity_source(
                            mgmt_identity, getattr(self, "_mgmt_identity_source", None)),
                        "manual_request": False,
                        "extra": {
                            "partial": is_partial,
                            "close_qty": close_qty,
                            "remaining_qty": quantity - close_qty,
                            "underlying_price": ((_enrich or {}).get("underlying")
                                                 if isinstance(_enrich, dict) else None),
                            "iv": _q.get("iv"), "delta": _q.get("delta"),
                            "gamma": _q.get("gamma"), "theta": _q.get("theta"),
                            "vega": _q.get("vega"),
                            "trigger_mark": current_price,
                            "bid": _close_bid,
                            "limit_price": current_price,
                            "rule_fired": getattr(trigger, "trigger_type", None),
                            "exit_reasoning": getattr(trigger, "message", None),
                            "mfe_pct": self.state_manager.state.mfe_pct.get(str(con_id)),
                        },
                    }
                    result = await self.order_manager.place_close_order(
                        con_id=con_id,
                        symbol=symbol,
                        quantity=close_qty,
                        limit_price=current_price,
                        combo_bid=_close_combo_bid,
                        entry_debit=closed_basis,
                        live_open_orders=live_open_orders,
                        spread=spread,
                        market=market_flag,
                        right=je.get("right"),
                        bid=_close_bid,
                        trigger_type=trigger.trigger_type,
                        exit_context=_exit_ctx,
                    )

                    if result.success:
                        orders_placed_this_cycle += 1
                        orders_notional_this_cycle += order_notional

                        live_open_orders[con_id] = {"order_id": result.order_id, "remaining": close_qty}



                        _inf = self.state_manager.state.get_in_flight(con_id)
                        if _inf is None:
                            from exitmgr.state import InFlightClose
                            _result_client = self._ib_client_id(
                                getattr(result, "client_id", None))
                            _result_perm = int(getattr(result, "perm_id", 0) or 0)
                            _result_ref = getattr(result, "order_ref", None)
                            _inf = InFlightClose(
                                con_id=con_id, order_id=int(result.order_id or 0),
                                remaining_qty=close_qty, entry_debit=closed_basis,
                                order_price=current_price,
                                placed_at=datetime.now(timezone.utc).isoformat(),
                                exit_context=_exit_ctx,
                                perm_id=_result_perm,
                                client_id=_result_client,
                                order_ref=_result_ref,
                                identity_version=(1 if (_result_perm or _result_ref
                                                        or _result_client is not None) else 0),
                                placement_state="submitted")
                            self.state_manager.state.add_in_flight(_inf)
                            self.state_manager.save()




                        _tr = getattr(result, "trade", None)
                        if _tr is not None:
                            try:
                                for _ in range(12):
                                    _status_now = getattr(getattr(_tr, "orderStatus", None),
                                                          "status", None)
                                    if _status_now in ("Filled", "Cancelled", "ApiCancelled", "Inactive"):
                                        break
                                    await asyncio.sleep(0.5)
                            except Exception as _fe:
                                print(f"[WARN] fill capture failed for con_id={con_id}: {_fe}")
                        _filled_now = bool(_tr is not None
                                           and self._finalize_in_flight_exit(con_id, _inf, _tr))
                        if not _filled_now:
                            _st = getattr(getattr(_tr, "orderStatus", None), "status", None)
                            if isinstance(_st, str):
                                self._log_unfilled_exit(
                                    con_id, symbol, trigger, fill_status=_st,
                                    close_qty=close_qty, trigger_mark=current_price,
                                    bid=_close_bid, limit_price=current_price,
                                    order_id=result.order_id,
                                    reason=self._exit_reason(trigger),
                                    placed_at=getattr(_inf, "placed_at", None))
                            print(f"[EXIT-PENDING] {symbol} con_id={con_id} order_id={result.order_id} "
                                  f"status={_st or 'unknown'}; durable context retained")
                    elif bool(getattr(result, "ambiguous", False)):
                        print(f"[EXIT-ACK-UNKNOWN] {symbol} con_id={con_id} "
                              f"order_id={result.order_id}; durable intent retained, no blind retry")
                    elif result.trade is not None:









                        try:
                            _nfs = None
                            _ost = getattr(result.trade, "orderStatus", None)
                            if _ost is not None:
                                _nfs = getattr(_ost, "status", None)
                            _nfs = _nfs if isinstance(_nfs, str) else "Cancelled"
                            self._log_unfilled_exit(
                                con_id, symbol, trigger, fill_status=_nfs,
                                close_qty=close_qty, trigger_mark=current_price,
                                bid=_close_bid, limit_price=current_price,
                                order_id=result.order_id, reason=self._exit_reason(trigger),
                                placed_at=datetime.now().astimezone().isoformat())
                        except Exception as _ue:
                            print(f"[WARN] non-fill logging failed for con_id={con_id}: "
                                  f"{_ue} (continuing)")
            else:

                pnl_pct = (current_price * 100 * quantity - entry_debit) / entry_debit * 100 if entry_debit > 0 else 0
                print(f"[EVAL] con_id={con_id} ({symbol}): no trigger (price={current_price:.4f}, pnl={pnl_pct:.2f}%)")



        if blind_positions:
            print(f"[CYCLE] POSITIONS UNPRICEABLE (no broker mark and no usable quote): "
                  f"{[(s, c) for s, c, _ in blind_positions]}")
            try:



                from exitmgr.trader import _market_open
                if _market_open():
                    self._post_stops_withheld_alert(
                        blind_positions,
                        reason=("neither IBKR's portfolio mark nor the streaming quote produced a "
                                "usable price, so no stop, trail or time exit could be evaluated "
                                "for this position this cycle"))
            except Exception as _wa:
                print(f"[WARN] unpriceable-position alert errored (continuing): {_wa}")
        if withheld_stops:
            print(f"[CYCLE] PROTECTIVE EXITS WITHHELD (reconcile-inconsistent con_ids): "
                  f"{[(s, c) for s, c, _ in withheld_stops]}")
            try:
                self._post_stops_withheld_alert(
                    withheld_stops,
                    reason=(f"open-order book UNREADABLE: {self._order_book_unreadable}"
                            if self._order_book_unreadable is not None else None))
            except Exception as _wa:
                print(f"[WARN] stops-withheld alert errored (continuing): {_wa}")
        if entry_gate_withheld:
            print(f"[CYCLE] PROTECTIVE EXITS WITHHELD (opening BUY still working): "
                  f"{[(s, c) for s, c, _ in entry_gate_withheld]}")
            try:
                _ev = getattr(self, "_entry_order_view", None)
                self._post_stops_withheld_alert(
                    entry_gate_withheld,
                    reason=("the ENTRY-side open-order book is UNREADABLE: %s"
                            % (getattr(_ev, "error", None) or "unknown",)
                            if not getattr(_ev, "readable", False) else
                            "the opening BUY for this position is still working at the broker"))
            except Exception as _wa:
                print(f"[WARN] entry-gate withheld alert errored (continuing): {_wa}")
        print(f"[CYCLE] Evaluation complete. Triggers: {len(triggers)}, Orders placed: {orders_placed_this_cycle}")
        print(f"[CYCLE] Cycle finished at {datetime.now(timezone.utc).isoformat()}")
        print(f"{'='*60}\n")


        self.state_manager.update_last_cycle()

    async def run(self) -> None:
        """Public API contract; production-derived narrative omitted."""

        connected = await self.ib_conn.connect()
        if not connected:
            print("[ERROR] Could not connect to IB - exiting")
            return


        if not await self._reconcile_on_startup():
            print("[ERROR] Reconciliation failed - exiting for safety")
            await self.ib_conn.disconnect()
            sys.exit(1)


        def request_shutdown(signum, frame):
            print(f"\n[SHUTDOWN] Received signal {signum}, initiating graceful shutdown...")
            self._shutdown_requested = True
            self._running = False

        signal.signal(signal.SIGINT, request_shutdown)
        signal.signal(signal.SIGTERM, request_shutdown)

        dry_run = self.config.dry_run
        if dry_run:
            print("[INFO] Running in DRY RUN mode - no orders will be placed")
        else:
            print("[WARN] Running in LIVE mode - orders WILL be placed!")

        if self.config.loop_mode:
            print(f"[INFO] Running in LOOP mode with interval={self.config.loop.interval_seconds}s")
            self._running = True
            while self._running and not self._shutdown_requested:
                await self.run_cycle(dry_run)

                if not self._shutdown_requested:
                    await asyncio.sleep(self.config.loop.interval_seconds)
        else:
            await self.run_cycle(dry_run)


        await self.ib_conn.disconnect()
        print("[INFO] Exit manager stopped")
