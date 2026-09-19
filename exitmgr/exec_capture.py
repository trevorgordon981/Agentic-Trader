"""Public API contract; production-derived narrative omitted."""
import asyncio
import json
import os
import random
import shutil
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from typing import Optional, List, Dict, Any


from exitmgr import trade_capture as _tc
from exitmgr import dataset_integrity as _di


_UNSET_DOUBLE = 1.0e300

_OPT_MULT = 100.0
_PNL_TOLERANCE_USD = 0.05

WATERMARK_NAME = ".exec_watermark.json"
WATERMARK_SCHEMA = "exec_watermark.v1"









_KNOWN_APP_CLIENT_IDS = {1, 91, 189}


_ROTATION_BAND = (150, 900)


def _reserved_client_ids(config=None) -> set:
    """Public API contract; production-derived narrative omitted."""
    reserved = {1, 189}
    ib = getattr(config, "ib", None)
    for attr in ("client_id", "protective_client_id"):
        try:
            reserved.add(int(getattr(ib, attr)))
        except (AttributeError, TypeError, ValueError):
            pass
    return reserved


def _rotation_candidates(config=None) -> List[int]:
    """Public API contract; production-derived narrative omitted."""
    lo, hi = _ROTATION_BAND
    reserved = _reserved_client_ids(config)
    return [cid for cid in range(lo, hi + 1) if cid not in reserved]


def _rotation_client_id(config=None, rng=None) -> int:
    """Public API contract; production-derived narrative omitted."""
    candidates = _rotation_candidates(config)
    if not candidates:
        raise RuntimeError("no free clientId in rotation band %s" % (_ROTATION_BAND,))
    return (rng or random).choice(candidates)



def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _iso(t) -> Optional[str]:
    """Public API contract; production-derived narrative omitted."""
    if t is None:
        return None
    try:
        if isinstance(t, datetime):
            return t.isoformat()
        return str(t)
    except Exception:
        return None


def _real_realized(v):
    """Public API contract; production-derived narrative omitted."""
    x = _num(v)
    if x is None or abs(x) >= _UNSET_DOUBLE:
        return None
    return x



def normalize_fill(fill) -> Optional[Dict[str, Any]]:
    """Public API contract; production-derived narrative omitted."""
    try:
        ex = getattr(fill, "execution", None)
        c = getattr(fill, "contract", None)
        cr = getattr(fill, "commissionReport", None)
        if ex is None:
            return None
        exec_id = getattr(ex, "execId", None)
        if not exec_id:
            return None
        sec_type = getattr(c, "secType", "") or ""
        mult = _OPT_MULT if sec_type == "OPT" else 1.0

        try:
            m = getattr(c, "multiplier", None)
            if m:
                mult = float(m)
        except (TypeError, ValueError):
            pass
        return {
            "exec_id": str(exec_id),
            "order_id": int(getattr(ex, "orderId", 0) or 0),
            "perm_id": int(getattr(ex, "permId", 0) or 0),
            "client_id": int(getattr(ex, "clientId", 0) or 0),
            "acct": getattr(ex, "acctNumber", None),
            "con_id": int(getattr(c, "conId", 0) or 0) if c is not None else 0,
            "symbol": getattr(c, "symbol", None) if c is not None else None,
            "sec_type": sec_type,
            "right": getattr(c, "right", "") if c is not None else "",
            "strike": _num(getattr(c, "strike", None)) if c is not None else None,
            "expiry": (getattr(c, "lastTradeDateOrContractMonth", "") or "") if c is not None else "",



            "order_ref": str(getattr(ex, "orderRef", "") or ""),
            "side": getattr(ex, "side", "") or "",
            "shares": _num(getattr(ex, "shares", 0)) or 0.0,
            "price": _num(getattr(ex, "price", 0)) or 0.0,
            "time": _iso(getattr(ex, "time", None)),
            "mult": mult,
            "commission": _num(getattr(cr, "commission", None)) if cr is not None else None,
            "commission_ccy": getattr(cr, "currency", None) if cr is not None else None,
            "realized_pnl_ib": _real_realized(getattr(cr, "realizedPNL", None)) if cr is not None else None,
        }
    except Exception:
        return None


















ENTRY_DEAD_STATUSES = frozenset({"Cancelled", "ApiCancelled", "Inactive", "Rejected"})
ENTRY_TERMINAL_STATUSES = frozenset(ENTRY_DEAD_STATUSES | {"Filled"})


OBSERVED_QTY_SOURCES = frozenset({"order_status", "order_status_zero"})


def _clean_float(value):
    """Public API contract; production-derived narrative omitted."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")) or abs(f) >= _UNSET_DOUBLE:
        return None
    return f


@dataclass(frozen=True)
class FillFacts:
    """Public API contract; production-derived narrative omitted."""

    status: str
    terminal: bool
    filled_qty: Optional[int]
    filled_qty_source: str
    remaining_qty: Optional[int]
    remaining_qty_source: str
    requested_qty: Optional[int]
    avg_fill_price: Optional[float]
    price_source: str
    commission_usd: Optional[float]
    commission_source: str
    fill_ts: Optional[str]
    fill_ts_source: str
    execution_count: int = 0

    @property
    def journalable(self) -> bool:
        """Public API contract; production-derived narrative omitted."""
        return (self.terminal
                and self.filled_qty_source in OBSERVED_QTY_SOURCES
                and self.filled_qty is not None and self.filled_qty > 0)

    @property
    def partial_working(self) -> bool:
        """Public API contract; production-derived narrative omitted."""
        return (not self.terminal and self.filled_qty_source in OBSERVED_QTY_SOURCES
                and (self.filled_qty or 0) > 0)

    @property
    def exposure_unsized(self) -> bool:
        """Public API contract; production-derived narrative omitted."""
        return self.execution_count > 0 and not self.journalable

    @property
    def remainder_working(self):
        """Public API contract; production-derived narrative omitted."""
        if self.terminal:
            return False
        if self.remaining_qty is None:
            return None
        return self.remaining_qty > 0




def _unobserved_facts(status: str = "") -> "FillFacts":
    return FillFacts(
        status=status, terminal=False,
        filled_qty=None, filled_qty_source="unobserved",
        remaining_qty=None, remaining_qty_source="unobserved",
        requested_qty=None, avg_fill_price=None, price_source="unobserved",
        commission_usd=None, commission_source="unreported",
        fill_ts=None, fill_ts_source="unfilled", execution_count=0)


def fill_facts_from_trade(trade, *, requested_qty=None) -> FillFacts:
    """Public API contract; production-derived narrative omitted."""
    try:
        return _fill_facts_from_trade(trade, requested_qty=requested_qty)
    except Exception:
        return _unobserved_facts()


def _fill_facts_from_trade(trade, *, requested_qty=None) -> FillFacts:
    status = ""
    try:
        status = str(getattr(getattr(trade, "orderStatus", None), "status", "") or "")
    except Exception:
        status = ""
    terminal = status in ENTRY_TERMINAL_STATUSES
    st = getattr(trade, "orderStatus", None)

    req = None
    for source in (requested_qty,
                   getattr(getattr(trade, "order", None), "totalQuantity", None)):
        value = _clean_float(source)
        if value is not None and value > 0:
            req = int(round(abs(value)))
            break

    filled_raw = _clean_float(getattr(st, "filled", None)) if st is not None else None
    if filled_raw is None:
        filled_qty, filled_src = None, "unobserved"
    elif filled_raw > 0:
        filled_qty, filled_src = int(round(abs(filled_raw))), "order_status"
    else:


        filled_qty, filled_src = 0, "order_status_zero"

    remaining_raw = _clean_float(getattr(st, "remaining", None)) if st is not None else None
    if remaining_raw is not None and remaining_raw >= 0:
        remaining_qty, remaining_src = int(round(remaining_raw)), "order_status"
    elif terminal:

        remaining_qty, remaining_src = 0, "terminal"
    elif req is not None and filled_qty is not None and filled_src in OBSERVED_QTY_SOURCES:
        remaining_qty, remaining_src = max(0, req - filled_qty), "derived"
    else:
        remaining_qty, remaining_src = None, "unobserved"

    price = _clean_float(getattr(st, "avgFillPrice", None)) if st is not None else None
    if price is not None and price > 0 and filled_src in OBSERVED_QTY_SOURCES and (filled_qty or 0) > 0:
        price_src = "order_status"
    else:
        price, price_src = None, "unobserved"

    executions = 0
    try:
        executions = sum(1 for _f in (getattr(trade, "fills", None) or [])
                         if getattr(_f, "execution", None) is not None)
    except Exception:
        executions = 0

    commission = None
    try:
        from exitmgr.order import commission_from_trade
        commission = commission_from_trade(trade)
    except Exception:
        commission = None
    commission_src = "broker" if commission is not None else "unreported"

    if filled_src in OBSERVED_QTY_SOURCES and (filled_qty or 0) > 0:
        try:
            from exitmgr.order import fill_timestamp_from_trade
            fill_ts, fill_ts_src = fill_timestamp_from_trade(trade)
        except Exception:
            fill_ts, fill_ts_src = None, "unfilled"
    else:
        fill_ts, fill_ts_src = None, "unfilled"

    return FillFacts(
        status=status, terminal=terminal,
        filled_qty=filled_qty, filled_qty_source=filled_src,
        remaining_qty=remaining_qty, remaining_qty_source=remaining_src,
        requested_qty=req,
        avg_fill_price=price, price_source=price_src,
        commission_usd=commission, commission_source=commission_src,
        fill_ts=fill_ts, fill_ts_source=fill_ts_src,
        execution_count=executions)


def entry_journal_fields(trade, *, requested_qty, estimated_debit,
                         credit: bool = False) -> tuple:
    """Public API contract; production-derived narrative omitted."""
    facts = fill_facts_from_trade(trade, requested_qty=requested_qty)
    fields = {
        "order_status": facts.status,
        "order_terminal": facts.terminal,
        "quantity_requested": (int(abs(requested_qty)) if requested_qty else facts.requested_qty),
        "quantity_source": facts.filled_qty_source,
        "entry_remaining_qty": facts.remaining_qty,
        "entry_remaining_qty_source": facts.remaining_qty_source,
        "entry_executions_seen": facts.execution_count,
        "avg_fill_price": facts.avg_fill_price,
        "fill_ts": facts.fill_ts,
        "fill_ts_source": facts.fill_ts_source,
        "entry_commission": facts.commission_usd,
        "entry_commission_source": facts.commission_source,
    }
    if not facts.journalable:
        return False, fields, facts

    filled = int(facts.filled_qty)
    fields["quantity"] = -filled if credit else filled
    if credit:
        fields["contracts"] = filled

    est = _clean_float(estimated_debit)


    req = fields["quantity_requested"]
    if req is None or int(req) <= 0:
        req = filled
    prorated = None
    if est is not None and req:
        prorated = round(est * (float(filled) / float(req)), 2)
    try:
        from exitmgr.order import compute_entry_basis
        efd, eslip, eslip_pct = compute_entry_basis(prorated, facts.avg_fill_price, filled)
    except Exception:
        efd = eslip = eslip_pct = None
    fields["entry_fill_debit"] = efd
    fields["entry_slippage"] = eslip
    fields["entry_slippage_pct"] = eslip_pct
    if credit:


        fields["debit"] = prorated if prorated is not None else _clean_float(estimated_debit)
        fields["basis_source"] = ("estimate_prorated" if filled != req else "estimate")
    elif efd is not None:
        fields["debit"] = efd
        fields["basis_source"] = "fill"
    else:





        fields["debit"] = prorated if prorated is not None else _clean_float(estimated_debit)
        fields["basis_source"] = ("estimate_prorated" if filled != req else "estimate")
        fields["basis_error"] = True
    return True, fields, facts

def entry_fill_fields_from_executions(fills, *, primary_con_id, requested_qty,
                                      estimated_debit, credit: bool = False,
                                      leg_con_ids=None) -> tuple:
    """Public API contract; production-derived narrative omitted."""
    rows = [f for f in (fills or []) if isinstance(f, dict)]
    want_side = "SLD" if credit else "BOT"
    primary = int(primary_con_id or 0)
    expected_legs = tuple(dict.fromkeys(int(x) for x in (leg_con_ids or (primary,))))
    expected_leg_set = set(expected_legs)
    leg_quantities = {leg: 0.0 for leg in expected_legs}
    qty = 0.0
    basis = 0.0
    commission = 0.0
    commission_seen = False
    times = []
    for f in rows:
        con_id = int(f.get("con_id") or 0)
        if con_id not in expected_leg_set:
            continue
        shares = _clean_float(f.get("shares")) or 0.0
        price = _clean_float(f.get("price"))
        mult = _clean_float(f.get("mult")) or 1.0
        side = str(f.get("side") or "").upper()
        expected_side = (want_side if con_id == primary
                         else ("BOT" if want_side == "SLD" else "SLD"))
        if side == expected_side:
            leg_quantities[con_id] += abs(shares)
        if con_id == primary and side == want_side:
            qty += abs(shares)
        if price is not None:
            basis += (1.0 if side == "BOT" else -1.0) * price * abs(shares) * mult
        c = _clean_float(f.get("commission"))
        if c is not None and c != 0.0:
            commission += c
            commission_seen = True
        t = f.get("time")
        if t:
            times.append(str(t))
    filled = int(round(qty))
    req = int(abs(requested_qty)) if requested_qty else None
    fields = {
        "quantity_requested": req,
        "quantity_source": ("executions" if filled > 0 else "executions_zero"),
        "entry_executions_seen": len(rows),
        "entry_commission": (round(commission, 4) if commission_seen else None),
        "entry_commission_source": ("broker" if commission_seen else "unreported"),
        "fill_ts": (max(times) if times else None),
        "fill_ts_source": ("broker_execution" if times else "unfilled"),
        "entry_leg_quantities": {str(k): v for k, v in leg_quantities.items()},
    }
    if filled <= 0:
        return False, fields
    if len(expected_legs) > 1 and (
            any(value <= 0 for value in leg_quantities.values())
            or any(abs(value - qty) > 1e-9 for value in leg_quantities.values())):
        fields["recovery_error"] = "incomplete_or_unequal_combo_leg_executions"
        return False, fields
    fields["quantity"] = -filled if credit else filled
    if credit:
        fields["contracts"] = filled
    est = _clean_float(estimated_debit)
    prorated = (round(est * (float(filled) / float(req)), 2)
                if (est is not None and req) else est)
    if not credit and abs(basis) > 0:
        fields["debit"] = round(abs(basis), 2)
        fields["entry_fill_debit"] = round(abs(basis), 2)
        fields["basis_source"] = "fill"
        fields["avg_fill_price"] = round(abs(basis) / (100.0 * filled), 4)
        if prorated is not None:
            fields["entry_slippage"] = round(abs(basis) - prorated, 2)
            fields["entry_slippage_pct"] = (round(fields["entry_slippage"] / abs(prorated) * 100, 2)
                                            if prorated else None)
    else:
        fields["debit"] = prorated
        fields["basis_source"] = ("estimate_prorated" if (req and filled != req) else "estimate")
        fields["avg_fill_price"] = None
    return True, fields


def load_app_origin_index(dataset_path: str) -> Dict[str, set]:
    """Public API contract; production-derived narrative omitted."""
    idx = {"exec_ids": set(), "order_ids": set(), "perm_ids": set(), "trade_uids": set()}
    try:
        if not os.path.exists(dataset_path):
            return idx
        with open(dataset_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                _harvest_ids(row, idx)
    except Exception:
        pass
    return idx


def _harvest_ids(obj, idx, depth=0):
    """Public API contract; production-derived narrative omitted."""
    if depth > 6:
        return
    try:
        if isinstance(obj, dict):
            for k, v in obj.items():
                kl = str(k).lower()
                if kl in ("exec_id", "execid"):
                    if v:
                        idx["exec_ids"].add(str(v))
                elif kl in ("exec_ids", "execids") and isinstance(v, (list, tuple)):
                    for e in v:
                        if e:
                            idx["exec_ids"].add(str(e))
                elif kl in ("order_id", "orderid"):
                    iv = _int(v)
                    if iv:
                        idx["order_ids"].add(iv)
                elif kl in ("order_ids", "orderids") and isinstance(v, (list, tuple)):
                    for e in v:
                        iv = _int(e)
                        if iv:
                            idx["order_ids"].add(iv)
                elif kl in ("perm_id", "permid"):
                    iv = _int(v)
                    if iv:
                        idx["perm_ids"].add(iv)
                elif kl in ("perm_ids", "permids") and isinstance(v, (list, tuple)):
                    for e in v:
                        iv = _int(e)
                        if iv:
                            idx["perm_ids"].add(iv)
                elif kl in ("trade_uid", "trade_instance_uid"):
                    if v:
                        idx["trade_uids"].add(str(v))
                else:
                    _harvest_ids(v, idx, depth + 1)
        elif isinstance(obj, (list, tuple)):
            for e in obj:
                _harvest_ids(e, idx, depth + 1)
    except Exception:
        pass


def _int(v):
    try:
        i = int(v)
        return i if i else 0
    except (TypeError, ValueError):
        return 0


def is_app_origin(fill: dict, idx: dict, app_client_ids: set):
    """Public API contract; production-derived narrative omitted."""
    if fill.get("exec_id") and str(fill["exec_id"]) in idx.get("exec_ids", ()):
        return True, "exec_id_in_dataset"
    oid = fill.get("order_id") or 0
    if oid and oid in idx.get("order_ids", ()):
        return True, "order_id_in_dataset"
    pid = fill.get("perm_id") or 0
    if pid and pid in idx.get("perm_ids", ()):
        return True, "perm_id_in_dataset"
    cid = fill.get("client_id", 0) or 0
    if cid and cid in app_client_ids:
        return True, "app_client_id"
    if cid == 0:
        return False, "manual_tws_clientid0"
    return True, "nonzero_clientid_assumed_app"



def watermark_path(ddir: str) -> str:
    return os.path.join(ddir or ".", WATERMARK_NAME)


def load_watermark(ddir: str) -> Dict[str, Any]:
    p = watermark_path(ddir)
    wm = {"schema": WATERMARK_SCHEMA, "processed_exec_ids": [], "runs": 0, "updated": None}
    try:
        if os.path.exists(p):
            with open(p) as f:
                data = json.load(f)
            if isinstance(data, dict):
                wm.update(data)
    except Exception:
        pass

    wm["_processed"] = set(str(x) for x in (wm.get("processed_exec_ids") or []))
    return wm


def save_watermark(ddir: str, wm: Dict[str, Any]) -> None:
    p = watermark_path(ddir)
    try:
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        out = {
            "schema": WATERMARK_SCHEMA,
            "processed_exec_ids": sorted(wm.get("_processed", set())),
            "runs": int(wm.get("runs", 0)),
            "updated": _now(),
        }
        tmp = p + ".tmp"
        with open(tmp, "w") as f:
            json.dump(out, f, indent=0)
        os.replace(tmp, p)
    except Exception as e:
        try:
            print(f"[WARN] save_watermark failed ({p}): {e}")
        except Exception:
            pass



def _wavg(fills, key_price="price", key_qty="shares"):
    tot_q = sum((f.get(key_qty) or 0.0) for f in fills)
    if tot_q <= 0:
        return 0.0, 0.0
    tot = sum((f.get(key_price) or 0.0) * (f.get(key_qty) or 0.0) for f in fills)
    return (tot / tot_q), tot_q


def _sum_commissions(fills):
    """Public API contract; production-derived narrative omitted."""
    have = [f.get("commission") for f in fills if f.get("commission") is not None]
    all_present = len(have) == len(fills) and len(fills) > 0
    total = round(sum(have), 4) if have else None
    return total, all_present


def _outcome(pnl):
    if pnl is None:
        return None
    if pnl > 0:
        return "win"
    if pnl < 0:
        return "loss"
    return "scratch"


def _holding_days(open_ts, close_ts):
    try:
        a = datetime.fromisoformat(str(open_ts)) if open_ts else None
        b = datetime.fromisoformat(str(close_ts)) if close_ts else None
        if a and b:
            return round((b - a).total_seconds() / 86400.0, 4)
    except Exception:
        pass
    return None


def build_rows_for_contract(con_id: int, fills: List[dict]) -> Dict[str, Any]:
    """Public API contract; production-derived narrative omitted."""
    fills = sorted(fills, key=lambda f: str(f.get("time") or ""))
    exec_ids = [f["exec_id"] for f in fills]
    opens = [f for f in fills if f.get("realized_pnl_ib") is None]
    closes = [f for f in fills if f.get("realized_pnl_ib") is not None]

    sample = fills[0]
    symbol = sample.get("symbol")
    right = sample.get("right") or ""
    strike = sample.get("strike")
    expiry = sample.get("expiry") or ""
    mult = sample.get("mult") or _OPT_MULT

    open_ts = opens[0]["time"] if opens else (closes[0]["time"] if closes else None)
    entry_day = str(open_ts)[:10] if open_ts else None
    uid = _tc.trade_uid(con_id=con_id, symbol=symbol, strike=strike, expiry=expiry, right=right)
    uid_inst = _tc.trade_uid(con_id=con_id, symbol=symbol, strike=strike, expiry=expiry,
                             right=right, entry_day=entry_day)

    open_qty = sum((f.get("shares") or 0.0) for f in opens)
    close_qty = sum((f.get("shares") or 0.0) for f in closes)

    result = {"trade": None, "position": None, "exec_ids": exec_ids, "terminal": False}


    if close_qty <= 0:
        if open_qty <= 0:
            return result
        avg_open, _ = _wavg(opens)
        oc, all_pres = _sum_commissions(opens)
        direction = "long" if (opens[0].get("side") == "BOT") else "short"
        result["position"] = {
            "schema": "trade_dataset.v2",
            "kind": "position",
            "source": "manual",
            "manual": True,
            "reasoning_available": False,
            "ts": _now(),
            "trade_uid": uid,
            "trade_instance_uid": uid_inst,
            "con_id": int(con_id),
            "symbol": symbol,
            "status": "open",
            "decision": None,
            "position": {
                "direction": direction,
                "opened_ts": open_ts,
                "right": right,
                "strike": strike,
                "expiry": expiry,
                "quantity": int(open_qty) if open_qty == int(open_qty) else open_qty,
                "avg_open_price": round(avg_open, 6),
                "open_cost": round(avg_open * open_qty * mult, 2),
                "open_commission": oc,
                "commission_unknown": (not all_pres),
                "thesis": None, "conviction": None,
                "basis_source": "ibkr executions (real fills); position still open",
            },
            "provenance": _provenance(fills),
        }


        result["position"]["_dedup_key"] = _position_dedup_key(uid_inst, opens)
        _di.mark(result["position"], status=_di.CANONICAL, training=False, pnl=False,
                 reason="open position snapshot has no terminal outcome")
        return result


    avg_close, _ = _wavg(closes)
    close_ts = closes[-1]["time"] if closes else None
    matched_qty = min(open_qty, close_qty) if open_qty > 0 else close_qty
    entry_visible = open_qty > 0

    open_side = str(opens[0].get("side") or "").upper() if opens else None
    close_side = str(closes[0].get("side") or "").upper() if closes else None
    side_valid = (open_side, close_side) in (("BOT", "SLD"), ("SLD", "BOT"))
    direction = "long" if open_side == "BOT" else ("short" if open_side == "SLD" else None)
    ib_realized = round(sum(f["realized_pnl_ib"] for f in closes), 2) if closes else None

    if entry_visible:
        avg_open, _ = _wavg(opens)
        entry_premium = round(avg_open * matched_qty * mult, 2)
        close_premium = round(avg_close * matched_qty * mult, 2)



        entry_cashflow = -entry_premium if open_side == "BOT" else (
            entry_premium if open_side == "SLD" else None)
        close_cashflow = close_premium if close_side == "SLD" else (
            -close_premium if close_side == "BOT" else None)
        debit = entry_premium if direction == "long" else None
        credit = entry_premium if direction == "short" else None
        proceeds = close_premium if close_side == "SLD" else None
        close_cost = close_premium if close_side == "BOT" else None
        realized_gross = (round(entry_cashflow + close_cashflow, 2)
                          if entry_cashflow is not None and close_cashflow is not None else None)
        comm, all_pres = _sum_commissions(fills)
        computed_net = (round(realized_gross - comm, 2)
                        if (realized_gross is not None and comm is not None and all_pres) else None)
        commission_unknown = (not all_pres)
        pnl_difference = (round(computed_net - ib_realized, 2)
                          if computed_net is not None and ib_realized is not None else None)
        pnl_valid = bool(side_valid and (
            pnl_difference is None or abs(pnl_difference) <= _PNL_TOLERANCE_USD))


        realized_net = ib_realized if (ib_realized is not None and pnl_valid and all_pres) else (
            computed_net if ib_realized is None and pnl_valid else None)
        realized_pct_basis = ib_realized if pnl_valid and ib_realized is not None else realized_net
        realized_pct = (round(realized_pct_basis / abs(entry_premium) * 100, 2)
                        if realized_pct_basis is not None and entry_premium else None)
        entry_ts = open_ts
        basis_source = "ibkr executions + side-aware cashflows; IB realized P&L authoritative"
        entry_outside_window = False
    else:


        debit = None
        credit = None
        close_premium = round(avg_close * matched_qty * mult, 2)
        proceeds = close_premium if close_side == "SLD" else None
        close_cost = close_premium if close_side == "BOT" else None
        entry_cashflow = None
        close_cashflow = None
        realized_gross = ib_realized
        comm, all_pres = _sum_commissions(closes)


        realized_net = realized_gross if all_pres else None
        computed_net = None
        pnl_difference = None
        pnl_valid = ib_realized is not None
        commission_unknown = (not all_pres)
        realized_pct = None
        entry_ts = None
        basis_source = ("ibkr realizedPNL (opener predates reqExecutions ~7d window; entry basis "
                        "unavailable -- needs a Flex Query)")
        entry_outside_window = True

    net_open_remaining = open_qty - close_qty
    partial = matched_qty < max(open_qty, close_qty) or net_open_remaining > 0

    outcome_pnl = ((ib_realized if ib_realized is not None else realized_net) if pnl_valid else None)
    outcome = _outcome(outcome_pnl)

    trade = {
        "schema": "trade_dataset.v2",
        "kind": "trade",
        "source": "manual",
        "manual": True,
        "reasoning_available": False,
        "ts": _now(),
        "trade_uid": uid,
        "trade_instance_uid": uid_inst,
        "con_id": int(con_id),
        "symbol": symbol,
        "decision": None,
        "entry": {
            "ts": entry_ts,
            "symbol": symbol,
            "right": right,
            "strike": strike,
            "expiry": expiry,
            "structure": "single",
            "spread": None,
            "direction": direction,
            "open_side": open_side,
            "close_side": close_side,
            "quantity": int(matched_qty) if matched_qty == int(matched_qty) else matched_qty,
            "debit": debit,
            "credit": credit,
            "entry_cashflow": entry_cashflow,
            "profit_target_pct": None,
            "stop_pct": None,
            "conviction": None,
            "thesis": None,
            "entry_outside_window": entry_outside_window,
            "basis_source": basis_source,
        },
        "lifecycle": {
            "mark_path": [], "marks": 0, "mfe_pct": None, "mae_pct": None,
            "drawdown_from_peak_pct": None,
        },
        "close": {
            "ts": close_ts,
            "reason": "manual_close",
            "rule_fired": None,
            "exit_price_per_share": round(avg_close, 6),
            "proceeds": proceeds,
            "close_cost": close_cost,
            "close_cashflow": close_cashflow,
            "realized_pnl": realized_gross,
            "realized_pnl_net": realized_net,
            "entry_commission": None if not entry_visible else _entry_commission(opens),
            "exit_commission": _sum_commissions(closes)[0],
            "commission_unknown": commission_unknown,
            "pnl_is_estimate": bool(partial),
            "realized_pnl_pct": realized_pct,
            "holding_days": _holding_days(entry_ts, close_ts),
            "realized_pnl_ib": ib_realized,
            "pnl_valid": pnl_valid,
            "pnl_quarantined": not pnl_valid,
            "pnl_validation": {
                "status": "valid" if pnl_valid else "ib_disagreement",
                "tolerance_usd": _PNL_TOLERANCE_USD,
                "computed_net": computed_net,
                "ib_realized": ib_realized,
                "difference": pnl_difference,
                "side_valid": side_valid,
            },
            "fill_status": "filled",
            "tp_hit": None,
            "sl_hit": None,
            "partial": bool(partial),
            "basis_source": basis_source,
        },
        "labels": {
            "outcome": outcome,
            "win": (outcome == "win") if outcome is not None else None,
            "round_trip": None,
        },
        "review": None,
        "provenance": _provenance(fills),
    }
    trade["_dedup_key"] = f"trade_instance:{uid_inst}" if uid_inst else (_tc._dedup_key(trade) or "")
    if pnl_valid:
        _di.mark(trade, status=_di.CANONICAL, training=False, pnl=True,
                 reason="manual execution has no attributable model decision")
    else:
        _di.mark(trade, status=_di.INVALID, training=False, pnl=False,
                 reason="computed P&L disagrees with authoritative IB realized P&L")

    result["trade"] = trade


    result["terminal"] = (net_open_remaining <= 0)

    if net_open_remaining > 0:

        avg_open, _ = _wavg(opens)
        oc, all_pres = _sum_commissions(opens)
        result["position"] = {
            "schema": "trade_dataset.v2",
            "kind": "position", "source": "manual", "manual": True,
            "reasoning_available": False, "ts": _now(),
            "trade_uid": uid, "trade_instance_uid": uid_inst,
            "con_id": int(con_id), "symbol": symbol, "status": "open", "decision": None,
            "position": {
                "direction": "long" if opens[0].get("side") == "BOT" else "short",
                "opened_ts": open_ts, "right": right, "strike": strike, "expiry": expiry,
                "quantity": net_open_remaining,
                "avg_open_price": round(avg_open, 6),
                "open_cost": round(avg_open * net_open_remaining * mult, 2),
                "open_commission": oc, "commission_unknown": (not all_pres),
                "thesis": None, "conviction": None,
                "basis_source": "ibkr executions (real fills); runner open after partial close",
            },
            "provenance": _provenance(fills),
        }
        result["position"]["_dedup_key"] = _position_dedup_key(uid_inst, opens) + ":runner"
        _di.mark(result["position"], status=_di.CANONICAL, training=False, pnl=False,
                 reason="open runner snapshot has no terminal outcome")
    return result


def _entry_commission(opens):
    c, _ = _sum_commissions(opens)
    return c


def _provenance(fills):
    return {
        "capture_source": "ibkr_executions",
        "capture_ts": _now(),
        "exec_ids": [f["exec_id"] for f in fills],
        "order_ids": sorted({f["order_id"] for f in fills if f.get("order_id")}),
        "perm_ids": sorted({f["perm_id"] for f in fills if f.get("perm_id")}),
        "client_ids": sorted({f["client_id"] for f in fills}),
        "sides": [f.get("side") for f in fills],
    }


def _position_dedup_key(uid_inst, opens):
    ids = ",".join(sorted(f["exec_id"] for f in opens))
    return f"position_open:{uid_inst}:{ids}"



def process_fills(all_fills: List[dict], idx: dict, watermark: dict,
                  app_client_ids: set) -> Dict[str, Any]:
    """Public API contract; production-derived narrative omitted."""
    processed = set(watermark.get("_processed", set()))
    stats = {"fills_seen": len(all_fills), "app_origin": 0, "manual": 0,
             "already_watermarked": 0, "contracts": 0, "trades": 0, "positions": 0}
    manual = []
    for f in all_fills:
        app, _reason = is_app_origin(f, idx, app_client_ids)
        if app:
            stats["app_origin"] += 1
            continue
        stats["manual"] += 1
        manual.append(f)


    by_con: Dict[int, List[dict]] = {}
    for f in manual:
        by_con.setdefault(int(f.get("con_id") or 0), []).append(f)
    stats["contracts"] = len(by_con)

    trade_rows, position_rows = [], []
    new_processed = set()
    for con_id, fills in by_con.items():

        remaining_new = [e for e in (fl["exec_id"] for fl in fills) if e not in processed]
        built = build_rows_for_contract(con_id, fills)
        if built["trade"] is not None:

            if built["terminal"] and not remaining_new:
                stats["already_watermarked"] += 1
            else:
                trade_rows.append(built["trade"])
                stats["trades"] += 1
                if built["terminal"]:
                    new_processed.update(built["exec_ids"])
        if built["position"] is not None:
            position_rows.append(built["position"])
            stats["positions"] += 1

    return {"trade_rows": trade_rows, "position_rows": position_rows,
            "new_processed": new_processed, "stats": stats, "manual_fills": manual}



def _exec_filter(lookback_days: int):
    """Public API contract; production-derived narrative omitted."""
    from exitmgr.ibkr import _ib
    ExecutionFilter = _ib.ExecutionFilter
    ef = ExecutionFilter()
    try:
        since = datetime.now() - timedelta(days=max(1, int(lookback_days)))
        ef.time = since.strftime("%Y%m%d-%H:%M:%S")
    except Exception:
        pass
    return ef


def _raw_fill_order_ref(fill) -> Optional[str]:
    """Public API contract; production-derived narrative omitted."""
    for owner_name in ("execution", "order"):
        try:
            owner = (fill.get(owner_name) if isinstance(fill, dict)
                     else getattr(fill, owner_name, None))
            value = (owner.get("orderRef") if isinstance(owner, dict)
                     else getattr(owner, "orderRef", None))
        except Exception:
            value = None
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


async def fetch_fills(ib, lookback_days: int = 7, *, strict: bool = False,
                      relevant_order_refs=None) -> List[dict]:
    """Public API contract; production-derived narrative omitted."""
    try:
        ef = _exec_filter(lookback_days)



        _FILLS_TIMEOUT_S = 30
        fills = await asyncio.wait_for(ib.reqExecutionsAsync(ef), _FILLS_TIMEOUT_S)
        if strict and fills is None:
            raise RuntimeError("execution feed returned None")
    except Exception as e:
        try:
            print(f"[WARN] reqExecutionsAsync failed: {str(e) or type(e).__name__}")
        except Exception:
            pass
        if strict:
            raise RuntimeError(
                "execution feed unreadable: %s" % (str(e) or type(e).__name__)) from e
        return []
    out = []
    relevant = (None if relevant_order_refs is None
                else {str(ref) for ref in relevant_order_refs if str(ref)})
    for fl in (fills or []):
        n = normalize_fill(fl)
        if n is None:
            if strict:
                raw_ref = _raw_fill_order_ref(fl)
                if relevant is None or raw_ref is None:
                    scope = ("unknown" if raw_ref is None else raw_ref)
                    raise RuntimeError(
                        "execution feed contained an unnormalizable fill relevant to %s" % scope)
                if raw_ref in relevant:


                    out.append({"order_ref": raw_ref, "_normalization_error": True})
            continue
        out.append(n)
    return out



def _append_rows(dataset_path: str, rows: List[dict], dry_run: bool) -> int:
    """Public API contract; production-derived narrative omitted."""
    if not rows:
        return 0
    existing = _tc._existing_dedup_keys(dataset_path)
    fresh = [r for r in rows if r.get("_dedup_key") and r["_dedup_key"] not in existing]

    seen, batch = set(), []
    for r in fresh:
        k = r.get("_dedup_key")
        if k in seen:
            continue
        seen.add(k)
        batch.append(r)
    if dry_run or not batch:
        return len(batch)
    if os.path.exists(dataset_path):
        bak = dataset_path + ".bak-execcapture-" + datetime.now().strftime("%Y%m%d-%H%M%S")
        try:
            shutil.copy2(dataset_path, bak)
        except Exception:
            pass
    os.makedirs(os.path.dirname(dataset_path) or ".", exist_ok=True)
    with open(dataset_path, "a") as f:
        for r in batch:
            f.write(json.dumps(r, default=str) + "\n")
    try:
        from exitmgr import event_capture as _evt
        for r in batch:
            _evt.on_fill(r)
    except Exception:
        pass
    return len(batch)



async def capture_external_fills(*, ib=None, config=None, ddir: Optional[str] = None,
                                 host: str = "127.0.0.1", port: int = 4001,
                                 client_id: int = 170, lookback_days: int = 7,
                                 dry_run: bool = False,
                                 app_client_ids: Optional[set] = None) -> Dict[str, Any]:
    """Public API contract; production-derived narrative omitted."""
    summary = {"ok": False, "appended": 0, "positions_appended": 0, "manual_fills": 0,
               "stats": {}, "note": None, "clientId": None, "lookback_days": lookback_days}
    owns_conn = False
    conn = None
    try:

        if ddir is None:
            journal = getattr(getattr(config, "journal", None), "path", None) if config else None
            ddir = _tc.dataset_dir(journal)
        dpath = _tc.dataset_path(ddir)


        acids = set(_KNOWN_APP_CLIENT_IDS)
        if app_client_ids:
            acids |= set(app_client_ids)
        if config is not None:
            for _attr in ("client_id", "protective_client_id"):
                try:
                    acids.add(int(getattr(config.ib, _attr)))
                except Exception:
                    pass


        if ib is None:
            from exitmgr.connection import IBConnection
            if config is not None:
                host = getattr(config.ib, "host", host)
                port = getattr(config.ib, "port", port)
            conn = IBConnection(host=host, port=port, client_id=client_id, market_data_type=3)
            owns_conn = True
            connected = False
            for attempt in range(4):
                connected = await conn.connect()
                if connected:
                    break
                conn.client_id = client_id = _rotation_client_id(config)
                print(f"[INFO] exec_capture: retrying with fresh clientId={conn.client_id}")
            if not connected:
                summary["note"] = "could not connect to IB gateway"
                return summary
            ib = conn.ib
            summary["clientId"] = conn.client_id
        else:
            summary["clientId"] = "reused-existing-connection"


        fills = await fetch_fills(ib, lookback_days=lookback_days)

        idx = load_app_origin_index(dpath)
        wm = load_watermark(ddir)
        wm["runs"] = int(wm.get("runs", 0)) + 1

        res = process_fills(fills, idx, wm, acids)
        appended = _append_rows(dpath, res["trade_rows"], dry_run)
        pos_appended = _append_rows(dpath, res["position_rows"], dry_run)

        if not dry_run:
            wm["_processed"] |= res["new_processed"]
            save_watermark(ddir, wm)

        summary.update({
            "ok": True,
            "appended": appended,
            "positions_appended": pos_appended,
            "manual_fills": res["stats"]["manual"],
            "stats": res["stats"],
            "dataset_path": dpath,
            "manual_detail": [
                {"symbol": f.get("symbol"), "con_id": f.get("con_id"), "side": f.get("side"),
                 "qty": f.get("shares"), "price": f.get("price"), "time": f.get("time"),
                 "client_id": f.get("client_id"), "realized_pnl_ib": f.get("realized_pnl_ib")}
                for f in res["manual_fills"]
            ],
        })
        if lookback_days >= 7:
            summary["note"] = ("reqExecutions reaches only ~7 days; deeper history needs an IBKR "
                               "Flex Query (not built here).")
        return summary
    except Exception as e:
        summary["note"] = f"exception: {e}"
        try:
            print(f"[WARN] capture_external_fills failed: {e}")
        except Exception:
            pass
        return summary
    finally:
        if owns_conn and conn is not None:
            try:
                await conn.disconnect()
            except Exception:
                pass


def capture_external_fills_blocking(**kwargs) -> Dict[str, Any]:
    """Public API contract; production-derived narrative omitted."""
    import asyncio
    try:
        return asyncio.run(capture_external_fills(**kwargs))
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(capture_external_fills(**kwargs))
        finally:
            loop.close()



def _main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Capture manual/external IBKR fills into the dataset.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4001)
    ap.add_argument("--client-id", type=int, default=170)
    ap.add_argument("--lookback-days", type=int, default=7)
    ap.add_argument("--ddir", default=None, help="dataset dir (default: resolved from config/journal)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)









    import sys as _sys
    import yaml as _yaml
    from exitmgr.config import Config, ConfigError

    cfg, _p = None, None
    try:
        for _p in ("config.yaml", os.path.join(os.path.dirname(__file__), "..", "config.yaml")):
            if os.path.exists(_p):
                cfg = Config.from_yaml(_p)
                break



    except (ConfigError, ValueError, _yaml.YAMLError) as exc:
        _sys.stderr.write("config REFUSED (%s): %s\n" % (_p, exc))
        _sys.stderr.write("refusing to capture against defaults while a broken config is on "
                          "disk; fix the key above or move config.yaml aside.\n")
        return 2

    s = capture_external_fills_blocking(config=cfg, ddir=args.ddir, host=args.host, port=args.port,
                                        client_id=args.client_id, lookback_days=args.lookback_days,
                                        dry_run=args.dry_run)
    print(json.dumps(s, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
