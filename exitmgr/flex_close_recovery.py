"""Public API contract; production-derived narrative omitted."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Mapping
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo


SCHEMA = "close_fill_evidence.flex.v1"


class FlexCloseRefused(ValueError):
    """Public API contract; production-derived narrative omitted."""


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _need(condition, reason):
    if not condition:
        raise FlexCloseRefused(reason)


def _number(value, *, positive=False):
    _need(not isinstance(value, bool), "boolean is not a quantity/price")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise FlexCloseRefused("invalid numeric value") from exc
    _need(result.is_finite() and (not positive or result > 0), "non-finite/non-positive numeric value")
    return result


def _integer(value):
    result = _number(value, positive=True)
    _need(result == result.to_integral_value(), "non-integral quantity/contract id")
    return int(result)


def _timestamp(value, statement_timezone):
    try:
        result = datetime.strptime(str(value), "%Y%m%d;%H%M%S").replace(
            tzinfo=ZoneInfo(statement_timezone))
    except (ValueError, TypeError, KeyError) as exc:
        raise FlexCloseRefused("unusable broker fill timestamp/timezone") from exc
    return result.astimezone(timezone.utc).isoformat()


def _bindings(entry_snapshot, campaign_binding, inflight_snapshot):
    return {"entry_sha256": digest(entry_snapshot), "campaign_sha256": digest(campaign_binding),
            "inflight_sha256": digest(inflight_snapshot) if inflight_snapshot is not None else None}


def build_receipt(xml_bytes: bytes, *, expected_xml_sha256: str, account_id: str,
                  close_order_ref: str, submitted_close: dict, entry_snapshot: dict,
                  campaign_binding: dict, inflight_snapshot: dict | None = None,
                  statement_timezone: str) -> dict:
    """Public API contract; production-derived narrative omitted."""
    _need(isinstance(xml_bytes, bytes), "XML must be exact bytes")
    xml_sha = hashlib.sha256(xml_bytes).hexdigest()
    _need(xml_sha == expected_xml_sha256, "XML digest changed")
    _need(account_id and close_order_ref and isinstance(close_order_ref, str), "account/reference missing")
    try:
        tree = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise FlexCloseRefused("invalid XML") from exc
    _need(isinstance(entry_snapshot, dict) and isinstance(campaign_binding, dict), "entry/campaign missing")
    cid = _integer(entry_snapshot.get("contract_id"))
    short = _integer((entry_snapshot.get("spread") or {}).get("short_con_id"))
    qty = _integer(entry_snapshot.get("quantity"))
    entry_ref = entry_snapshot.get("order_ref")
    _need(isinstance(entry_ref, str) and entry_ref and entry_ref != close_order_ref, "entry reference missing/conflicting")
    identity = [entry_snapshot.get("symbol"), entry_snapshot.get("right"),
                entry_snapshot.get("expiry"), entry_snapshot.get("strike")]
    _need(campaign_binding.get("identity") == identity and campaign_binding.get("first_lot_ts") == entry_snapshot.get("ts")
          and _integer(campaign_binding.get("campaign_seq")) > 0
          and _integer(campaign_binding.get("first_lot_line")) > 0, "campaign does not bind entry")
    _need(submitted_close.get("schema") == "submitted_close.v1" and submitted_close.get("sec_type") == "BAG"
          and submitted_close.get("action") == "SELL" and _integer(submitted_close.get("combo_qty")) == qty,
          "unsupported submitted close")
    expected = {cid: "SLD", short: "BOT"}
    _need(cid != short and len(submitted_close.get("legs", [])) == 2, "exact two-leg topology required")
    observed_legs = {}
    for leg in submitted_close["legs"]:
        leg_cid = _integer(leg.get("con_id"))
        _need(leg_cid not in observed_legs and _integer(leg.get("ratio")) == 1
              and _integer(leg.get("multiplier")) == 100, "unsupported/duplicate leg")
        observed_legs[leg_cid] = leg.get("expected_side")
    _need(observed_legs == expected, "submitted topology/side differs from entry")
    if inflight_snapshot is not None:
        _need(inflight_snapshot.get("con_id") == cid and inflight_snapshot.get("order_ref") == close_order_ref
              and inflight_snapshot.get("submitted_close") == submitted_close, "inflight identity/topology mismatch")
        _need(_integer(inflight_snapshot.get("remaining_qty")) == qty, "partial/changed latch is unsupported")
    rows = [dict(e.attrib) for e in tree.iter("Trade") if e.attrib.get("levelOfDetail") == "EXECUTION"
            and e.attrib.get("conid") in {str(cid), str(short)}]
    target = [r for r in rows if r.get("orderReference") in {entry_ref, close_order_ref}]
    _need(target and all(r.get("accountId") == account_id for r in target), "statement account mismatch")
    _need(all(r.get("currency") == "USD" and r.get("ibCommissionCurrency") == "USD"
              and r.get("assetCategory") == "OPT" and _integer(r.get("multiplier")) == 100 for r in target),
          "unsupported asset/currency/multiplier")
    _need(all(not r.get("origTradeID") and not r.get("notes") for r in target), "correction/annotation needs adjudication")
    exec_ids = [r.get("ibExecID") for r in target]
    _need(all(exec_ids) and len(set(exec_ids)) == len(exec_ids), "missing/duplicate execution ID")
    opening = [r for r in target if r.get("orderReference") == entry_ref]
    closing = [r for r in target if r.get("orderReference") == close_order_ref]
    _need(opening and closing, "opening/closing evidence missing")
    first_open = min(_timestamp(r.get("dateTime"), statement_timezone) for r in opening)
    last_close = max(_timestamp(r.get("dateTime"), statement_timezone) for r in closing)
    last_open = max(_timestamp(r.get("dateTime"), statement_timezone) for r in opening)
    first_close = min(_timestamp(r.get("dateTime"), statement_timezone) for r in closing)
    _need(first_close >= last_open, "close predates complete opening")

    _need(all(r.get("accountId") != account_id or _timestamp(r.get("dateTime"), statement_timezone) < first_open
              or r.get("orderReference") in {entry_ref, close_order_ref} for r in rows), "intervening/later execution needs adjudication")
    entry_cash = Decimal(0)
    close_cash = Decimal(0)
    entry_fees = Decimal(0)
    close_fees = Decimal(0)
    broker_net = Decimal(0)
    normalized = []
    entry_normalized = []
    for group, indicator in ((opening, "O"), (closing, "C")):
        totals = {cid: Decimal(0), short: Decimal(0)}
        for row in group:
            leg_cid = _integer(row["conid"])
            amount = _number(row.get("quantity"))
            is_sell = (expected[leg_cid] == "SLD") if indicator == "C" else (expected[leg_cid] == "BOT")
            _need(row.get("openCloseIndicator") == indicator and row.get("buySell") == ("SELL" if is_sell else "BUY")
                  and ((amount < 0) if is_sell else (amount > 0)), "execution action/quantity sign mismatch")
            _integer(abs(amount))
            totals[leg_cid] += abs(amount)
            price = _number(row.get("tradePrice"))
            _need(price >= 0, "negative price")
            commission = _number(row.get("ibCommission"))
            _need(commission <= 0, "positive Flex commission/rebate requires adjudication")
            cash = -amount * price * 100
            if indicator == "O":
                entry_cash += cash
                entry_fees -= commission
                entry_normalized.append({"exec_id": row["ibExecID"], "con_id": leg_cid,
                    "side": "SLD" if is_sell else "BOT", "quantity": int(abs(amount)), "price": str(price),
                    "order_ref": entry_ref, "flex_order_id": row.get("ibOrderID"),
                    "commission_cost": str(-commission), "time": _timestamp(row["dateTime"], statement_timezone),
                    "row_sha256": digest(row)})
            else:
                close_cash += cash
                close_fees -= commission
                broker_net += _number(row.get("fifoPnlRealized"))
                normalized.append({"exec_id": row["ibExecID"], "con_id": leg_cid,
                    "side": expected[leg_cid], "quantity": int(abs(amount)), "price": str(price),
                    "order_ref": close_order_ref, "flex_order_id": row.get("ibOrderID"),
                    "commission_cost": str(-commission), "time": _timestamp(row["dateTime"], statement_timezone),
                    "row_sha256": digest(row)})
        _need(all(n == qty for n in totals.values()), "incomplete/excess leg quantity")
    basis = _number(entry_snapshot.get("entry_fill_debit"), positive=True)
    _need(-entry_cash == basis, "broker opening debit differs from frozen entry basis")
    _need(close_cash >= 0, "negative closing credit")
    if inflight_snapshot is not None:
        _need(_number(inflight_snapshot.get("entry_debit")) == basis
              and _number((inflight_snapshot.get("exit_context") or {}).get("entry_debit")) == basis,
              "latch basis differs from entry")
    receipt = {"schema": SCHEMA, "source": "ibkr_flex", "xml_sha256": xml_sha,
        "account_sha256": hashlib.sha256(account_id.encode()).hexdigest(), "statement_timezone": statement_timezone,
        "bindings": _bindings(entry_snapshot, campaign_binding, inflight_snapshot),
        "close_topology": {"schema": "close_topology.v1", "sec_type": "BAG", "action": "SELL", "combo_qty": qty,
            "legs": sorted(json.loads(json.dumps(submitted_close["legs"])), key=lambda leg: leg["con_id"])},
        "topology_source": "persisted_submission" if inflight_snapshot is not None else "journal_and_archived_executions",
        "close_order_ref": close_order_ref, "entry_order_ref": entry_ref,
        "con_id": cid, "combo_qty": qty, "terminal_status": "Filled", "entry_fill_ts": first_open, "fill_ts": last_close,
        "net_fill_price": str(close_cash / Decimal(100 * qty)), "entry_debit": str(basis),
        "entry_commission_cost": str(entry_fees), "exit_commission_cost": str(close_fees),
        "gross_pnl": str(close_cash - basis), "reconstructed_net_pnl": str(close_cash - basis - entry_fees - close_fees),
        "broker_fifo_pnl_net": str(broker_net), "entry_executions": sorted(entry_normalized, key=lambda r: r["exec_id"]),
        "executions": sorted(normalized, key=lambda r: r["exec_id"])}
    if inflight_snapshot is not None:
        receipt["submitted_close"] = json.loads(json.dumps(submitted_close))
    receipt["binding_sha"] = digest(receipt)
    validate_stored_receipt(receipt)
    return receipt


def validate_receipt(receipt: dict, xml_bytes: bytes, **expected) -> dict:
    """Public API contract; production-derived narrative omitted."""
    rebuilt = build_receipt(xml_bytes, **expected)
    _need(receipt == rebuilt, "receipt does not reproduce from source/bindings")
    return rebuilt


def check_commit_preconditions(receipt: dict, *, entry_snapshot: dict, campaign_binding: dict,
                               inflight_snapshot: dict | None, positions: Mapping | None,
                               order_leg_con_ids, observed_at: datetime, now: datetime,
                               max_age_seconds: float = 5.0) -> None:
    """Public API contract; production-derived narrative omitted."""
    _need(receipt.get("schema") == SCHEMA and receipt.get("source") == "ibkr_flex", "wrong receipt schema/source")
    payload = dict(receipt); binding = payload.pop("binding_sha", None)
    _need(binding == digest(payload), "receipt digest mismatch")
    _need(receipt.get("bindings") == _bindings(entry_snapshot, campaign_binding, inflight_snapshot), "owner campaign/latch changed")
    _need(isinstance(positions, Mapping) and isinstance(order_leg_con_ids, (list, tuple, set, frozenset)),
          "broker book is unknown/malformed")
    try:
        _need(observed_at.tzinfo is not None and now.tzinfo is not None, "broker observation clock is naive")
        age = (now - observed_at).total_seconds()
        _need(math.isfinite(max_age_seconds) and 0 < max_age_seconds <= 5 and 0 <= age <= max_age_seconds,
              "broker observation stale/future")
        live = {_integer(k): _number(v) for k, v in positions.items()}
        resting = {_integer(k) for k in order_leg_con_ids}
    except (TypeError, ValueError, AttributeError) as exc:
        raise FlexCloseRefused("unusable broker observation") from exc
    validate_stored_receipt(receipt)
    topology = {leg["con_id"] for leg in receipt["close_topology"]["legs"]}
    _need(datetime.fromisoformat(receipt["fill_ts"]) <= now, "archive fill is in the future")
    _need(not any(live.get(cid, Decimal(0)) != 0 for cid in topology), "submitted leg is still live")
    _need(not topology.intersection(resting), "resting order still touches submitted leg")


def validate_stored_receipt(receipt: dict) -> bool:
    """Public API contract; production-derived narrative omitted."""
    try:
        return _validate_stored_receipt(receipt)
    except FlexCloseRefused:
        raise
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise FlexCloseRefused("malformed stored receipt") from exc


def _validate_stored_receipt(receipt: dict) -> bool:
    _need(isinstance(receipt, dict), "receipt must be mapping")
    fields = {"schema", "source", "xml_sha256", "account_sha256", "statement_timezone", "bindings",
              "close_topology", "topology_source", "close_order_ref", "entry_order_ref", "con_id",
              "combo_qty", "terminal_status", "entry_fill_ts", "fill_ts", "net_fill_price", "entry_debit",
              "entry_commission_cost", "exit_commission_cost", "gross_pnl", "reconstructed_net_pnl",
              "broker_fifo_pnl_net", "entry_executions", "executions", "binding_sha"}
    ZoneInfo(receipt["statement_timezone"])
    source = receipt.get("topology_source")
    if source == "persisted_submission":
        fields.add("submitted_close")
    _need(set(receipt) == fields and receipt.get("schema") == SCHEMA and receipt.get("source") == "ibkr_flex"
          and source in {"persisted_submission", "journal_and_archived_executions"}, "unsupported stored receipt shape/source")
    payload = dict(receipt); bound = payload.pop("binding_sha")
    _need(bound == digest(payload), "stored receipt digest mismatch")
    def hash_ok(value):
        return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)
    _need(hash_ok(receipt['xml_sha256']) and hash_ok(receipt['account_sha256']), "invalid source hash")
    bindings = receipt['bindings']
    _need(isinstance(bindings, dict) and set(bindings) == {'entry_sha256', 'campaign_sha256', 'inflight_sha256'}
          and hash_ok(bindings['entry_sha256']) and hash_ok(bindings['campaign_sha256']), "invalid campaign hashes")
    _need((hash_ok(bindings['inflight_sha256']) if source == 'persisted_submission' else bindings['inflight_sha256'] is None),
          "inflight provenance mismatch")
    top = receipt['close_topology']
    qty = _integer(receipt['combo_qty']); cid = _integer(receipt['con_id'])
    _need(isinstance(top, dict) and set(top) == {'schema', 'sec_type', 'action', 'combo_qty', 'legs'}
          and top['schema'] == 'close_topology.v1' and top['sec_type'] == 'BAG' and top['action'] == 'SELL'
          and top['combo_qty'] == qty and receipt['terminal_status'] == 'Filled', "invalid closed topology")
    expected = {}
    _need(isinstance(top['legs'], list) and len(top['legs']) == 2, "two legs required")
    for leg in top['legs']:
        _need(isinstance(leg, dict) and set(leg) == {'con_id','ratio','multiplier','expected_side'}, "invalid leg shape")
        lid = _integer(leg['con_id'])
        _need(lid not in expected and _integer(leg['ratio']) == 1 and _integer(leg['multiplier']) == 100
              and leg['expected_side'] in {'SLD','BOT'}, "invalid/duplicate leg")
        expected[lid] = leg['expected_side']
    _need(expected.get(cid) == 'SLD' and sorted(expected.values()) == ['BOT','SLD'], "wrong parent/leg side")
    if source == 'persisted_submission':
        sub = receipt['submitted_close']
        _need(isinstance(sub, dict) and sub.get('schema') == 'submitted_close.v1'
              and all(sub.get(k) == top[k] for k in ('sec_type','action','combo_qty'))
              and sorted(sub.get('legs', []), key=lambda r:r['con_id']) == top['legs'], "submitted topology mismatch")
    _need(isinstance(receipt['entry_order_ref'], str) and receipt['entry_order_ref']
          and isinstance(receipt['close_order_ref'], str) and receipt['close_order_ref']
          and receipt['entry_order_ref'] != receipt['close_order_ref'], "invalid references")
    row_fields = {'exec_id','con_id','side','quantity','price','order_ref','flex_order_id','commission_cost','time','row_sha256'}
    seen = set(); aggregates = []; times = []
    for name, opening in (('entry_executions', True), ('executions', False)):
        rows = receipt[name]
        _need(isinstance(rows, list) and rows, "execution rows missing")
        totals = {key:0 for key in expected}; cash = Decimal(0); fees = Decimal(0); group_times = []
        for row in rows:
            _need(isinstance(row, dict) and set(row) == row_fields, "invalid execution shape/native identity injection")
            eid = row['exec_id']; lid = _integer(row['con_id']); amount = _integer(row['quantity'])
            _need(isinstance(eid, str) and eid and eid not in seen and lid in expected
                  and hash_ok(row['row_sha256']) and isinstance(row['flex_order_id'], str) and row['flex_order_id'],
                  "invalid/duplicate execution identity")
            seen.add(eid)
            side = ('BOT' if expected[lid]=='SLD' else 'SLD') if opening else expected[lid]
            _need(row['side'] == side and row['order_ref'] == receipt['entry_order_ref' if opening else 'close_order_ref'],
                  "execution side/reference mismatch")
            price = _number(row['price']); fee = _number(row['commission_cost'])
            _need(price >= 0 and fee >= 0, "invalid price/commission")
            try:
                stamp = datetime.fromisoformat(row['time'])
                _need(stamp.tzinfo is not None, "execution time needs timezone")
            except (TypeError, ValueError) as exc:
                raise FlexCloseRefused("invalid execution time") from exc
            group_times.append(stamp); totals[lid] += amount; fees += fee
            cash += (1 if side == 'SLD' else -1) * Decimal(amount * 100) * price
        _need(all(n == qty for n in totals.values()), "stored executions incomplete/excess")
        aggregates.append((cash, fees)); times.append(group_times)
    (entry_cash, entry_fees), (close_cash, close_fees) = aggregates
    basis = _number(receipt['entry_debit'], positive=True)
    _need(-entry_cash == basis and close_cash >= 0
          and _number(receipt['net_fill_price']) == close_cash/Decimal(100*qty)
          and _number(receipt['entry_commission_cost']) == entry_fees
          and _number(receipt['exit_commission_cost']) == close_fees
          and _number(receipt['gross_pnl']) == close_cash-basis
          and _number(receipt['reconstructed_net_pnl']) == close_cash-basis-entry_fees-close_fees,
          "stored cash/fee arithmetic mismatch")
    _number(receipt['broker_fifo_pnl_net'])
    _need(receipt['entry_fill_ts'] == min(times[0]).astimezone(timezone.utc).isoformat()
          and receipt['fill_ts'] == max(times[1]).astimezone(timezone.utc).isoformat()
          and max(times[0]) <= min(times[1]), "stored historical timestamp mismatch")
    return True
