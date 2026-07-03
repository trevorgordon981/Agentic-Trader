"""External-fill capture (2026-07-03): fold EVERY IBKR account execution -- including Trevor's
MANUAL / direct-in-TWS trades -- into the trade_dataset.v2 training corpus.

WHY THIS EXISTS
---------------
The v2 capture layer (trade_capture.py + ExitManager._log_trade_dataset) only records trades that
flow through Alfred: an entered decision or a manager-placed close. Trevor's real edge is MANUAL
trading (orders he punches straight into TWS / the mobile app). Those fills never touch Alfred, so
they were INVISIBLE to the dataset. This module pulls the account's executions directly from IBKR
(READ-ONLY: reqExecutionsAsync + the bundled commissionReport) and appends any NON-app-origin fill
as a `source:"manual"` row -- facts + real P&L only, reasoning left honestly NULL.

SAFETY (LIVE real-money account)
--------------------------------
  * READ-ONLY. The ONLY IB call is reqExecutionsAsync (executions + commissions). This module NEVER
    constructs, places, cancels, modifies, or transmits an order. There is no placeOrder path here.
  * The standalone runner connects with a FRESH, dedicated high clientId (default 170, >=150) that
    the trader / close-tool never use, and rotates on Error 326 ("clientId in use").

DESIGN
------
  * classification: a fill is MANUAL iff it did NOT originate from the app. App-origin is proven by
    (a) execId/orderId/permId already present in the dataset, or (b) a non-zero clientId (every API
    order Alfred places -- including the trader's rotated ids -- carries a non-zero clientId). A
    TWS/mobile MANUAL order always reports clientId==0, so clientId==0 (with no dataset match) is the
    honest, robust manual signal. Non-zero unknown clientIds are treated as app-origin so an Alfred
    fill is NEVER mis-tagged as Trevor's manual trade.
  * pairing: manual fills are grouped by contract (conId) and paired open<->close using IBKR's own
    per-fill realizedPNL as the authoritative "this fill CLOSED something" signal. A fully-closed
    round trip becomes ONE `kind:"trade"` row with a REAL entry debit (opening fills) + realized P&L
    (from fills + commissions). A close whose opener predates the ~7d reqExecutions window is still
    recorded with IBKR's real realizedPNL but a NULL basis (flagged; needs a Flex Query). Unpaired
    opens become a `kind:"position"` open-position snapshot.
  * honesty: thesis / chain-of-thought / technical_card / conviction / decision are NULL on a manual
    row (it never went through Alfred). NOTHING is fabricated. commission_unknown / pnl_is_estimate
    are preserved wherever a fee is missing, exactly like the backfill path.
  * idempotency: a watermark (data/.exec_watermark.json) persists the execId set already folded into
    a terminal trade row; re-runs never double-append. A second dataset-level guard dedups by the
    same `_dedup_key` the rest of the corpus uses.

Entry points:
  * capture_external_fills(...)          -- async; reuse an existing connected IB handle OR connect
                                            standalone with a fresh clientId. Appends + returns a
                                            summary dict. Runnable now (gateway up) and wired to run
                                            each exit cycle going forward.
  * capture_external_fills_blocking(...) -- thin sync wrapper for CLI / cron use.
"""
import json
import os
import shutil
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any

# reuse the app's durable join key + dedup + dataset paths (read-only helpers)
from exitmgr import trade_capture as _tc

# IBKR UNSET_DOUBLE sentinel: realizedPNL is set to ~1.8e308 on OPENING fills (no realization yet).
_UNSET_DOUBLE = 1.0e300
# options multiplier (100 shares/contract). Non-option secTypes fall back to 1.
_OPT_MULT = 100.0

WATERMARK_NAME = ".exec_watermark.json"
WATERMARK_SCHEMA = "exec_watermark.v1"

# clientIds the app is KNOWN to use for placing orders (config trader id is added at runtime). Used
# only as a positive app-origin hint; the decisive manual signal is clientId==0 (see is_app_origin).
_KNOWN_APP_CLIENT_IDS = {42, 88, 91}


# --------------------------------------------------------------------------- small utils
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _iso(t) -> Optional[str]:
    """datetime/str -> iso string; None-safe."""
    if t is None:
        return None
    try:
        if isinstance(t, datetime):
            return t.isoformat()
        return str(t)
    except Exception:
        return None


def _real_realized(v):
    """IBKR realizedPNL, filtering the UNSET_DOUBLE sentinel written on opening fills -> None."""
    x = _num(v)
    if x is None or abs(x) >= _UNSET_DOUBLE:
        return None
    return x


# --------------------------------------------------------------------------- normalize a Fill
def normalize_fill(fill) -> Optional[Dict[str, Any]]:
    """ib_async Fill (contract, execution, commissionReport, time) -> a plain, JSON-safe dict.
    Accepts either a real Fill or any object exposing .contract/.execution/.commissionReport (so
    tests can pass lightweight fakes). Returns None if there is no usable execId. Never raises."""
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
        # ib_async sometimes carries an explicit contract multiplier string
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
            "side": getattr(ex, "side", "") or "",          # 'BOT' | 'SLD'
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


# --------------------------------------------------------------------------- app-origin index
def load_app_origin_index(dataset_path: str) -> Dict[str, set]:
    """Scan the existing dataset for identifiers that mark a row as APP-ORIGIN (already captured by
    Alfred / the manager / a prior manual run), so a fill matching any of them is not re-recorded.
    Collects exec_ids, order_ids, perm_ids anywhere in a row, plus trade_uid/trade_instance_uid.
    Small file; guarded; never raises."""
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
    """Recursively pull exec_id/order_id/perm_id/trade_uid identifiers out of a row (they live at
    varying depths: top-level, close.order_id, extra.order_id, provenance.exec_ids, ...)."""
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
    """Return (is_app_origin: bool, reason: str). A fill is app-origin (=> NOT recorded as manual)
    when it can be shown to have come from Alfred/the app; else it is a manual/external fill.

    Precedence (belt-and-suspenders):
      1. execId/orderId/permId already in the dataset -> already captured -> app-origin.
      2. clientId in the known app allowlist -> app-origin.
      3. clientId == 0 -> a TWS/mobile MANUAL order -> NOT app-origin (this is the manual edge).
      4. non-zero clientId not in the allowlist -> assume an app API order (e.g. the trader's rotated
         clientId) and treat as app-origin, so an Alfred fill is NEVER mis-tagged as manual.
    """
    if fill.get("exec_id") and str(fill["exec_id"]) in idx.get("exec_ids", ()):  # 1
        return True, "exec_id_in_dataset"
    oid = fill.get("order_id") or 0
    if oid and oid in idx.get("order_ids", ()):
        return True, "order_id_in_dataset"
    pid = fill.get("perm_id") or 0
    if pid and pid in idx.get("perm_ids", ()):
        return True, "perm_id_in_dataset"
    cid = fill.get("client_id", 0) or 0
    if cid and cid in app_client_ids:                                            # 2
        return True, "app_client_id"
    if cid == 0:                                                                 # 3
        return False, "manual_tws_clientid0"
    return True, "nonzero_clientid_assumed_app"                                  # 4


# --------------------------------------------------------------------------- watermark
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
    # normalize to a set for use
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


# --------------------------------------------------------------------------- pairing (PURE)
def _wavg(fills, key_price="price", key_qty="shares"):
    tot_q = sum((f.get(key_qty) or 0.0) for f in fills)
    if tot_q <= 0:
        return 0.0, 0.0
    tot = sum((f.get(key_price) or 0.0) * (f.get(key_qty) or 0.0) for f in fills)
    return (tot / tot_q), tot_q


def _sum_commissions(fills):
    """(total_commission, all_present). total is None if NONE present; all_present False if any
    fill is missing its commissionReport (=> commission_unknown / pnl_is_estimate downstream)."""
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
    """Pure pairing for ONE contract's manual fills. Returns
        {"trade": <row|None>, "position": <row|None>, "exec_ids": [...], "terminal": bool}
    where `terminal` True means the trade is complete and every exec_id can be watermarked.

    open vs close is decided by IBKR's per-fill realizedPNL (set only on CLOSING fills). This is
    robust to a closing fill whose opener predates the reqExecutions window (opener not visible)."""
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

    # ---- CASE: nothing closed -> pure OPEN position snapshot ----
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
            "reasoning_available": False,          # never went through Alfred -> no thesis/CoT
            "ts": _now(),
            "trade_uid": uid,
            "trade_instance_uid": uid_inst,
            "con_id": int(con_id),
            "symbol": symbol,
            "status": "open",
            "decision": None,                      # honest: no decision context for a manual open
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
                "thesis": None, "conviction": None,   # honest nulls
                "basis_source": "ibkr executions (real fills); position still open",
            },
            "provenance": _provenance(fills),
        }
        # open exec_ids are NOT terminally watermarked (they'll be paired when the close appears);
        # the position snapshot is deduped at the dataset level by its content _dedup_key.
        result["position"]["_dedup_key"] = _position_dedup_key(uid_inst, opens)
        return result

    # ---- CASE: something closed -> terminal TRADE row ----
    avg_close, _ = _wavg(closes)
    close_ts = closes[-1]["time"] if closes else None
    matched_qty = min(open_qty, close_qty) if open_qty > 0 else close_qty
    entry_visible = open_qty > 0

    if entry_visible:
        avg_open, _ = _wavg(opens)
        debit = round(avg_open * matched_qty * mult, 2)          # REAL basis from opening fills
        proceeds = round(avg_close * matched_qty * mult, 2)
        realized_gross = round((avg_close - avg_open) * matched_qty * mult, 2)
        comm, all_pres = _sum_commissions(fills)
        realized_net = round(realized_gross - comm, 2) if (comm is not None and all_pres) else None
        commission_unknown = (not all_pres)
        realized_pct = round(realized_gross / abs(debit) * 100, 2) if debit else None
        entry_ts = open_ts
        basis_source = "ibkr executions (real open+close fills)"
        entry_outside_window = False
    else:
        # opener predates the ~7d reqExecutions window -> no recoverable basis. Use IBKR's REAL
        # realizedPNL for the close; NEVER fabricate an entry debit/pct.
        debit = None
        proceeds = round(avg_close * matched_qty * mult, 2)
        realized_gross = round(sum(f["realized_pnl_ib"] for f in closes), 2)
        comm, all_pres = _sum_commissions(closes)
        # IBKR realizedPNL is already net of commissions -> expose it as realized_pnl_net when the
        # closing fees are known, and keep the gross field = the same IB figure (best available).
        realized_net = realized_gross if all_pres else None
        commission_unknown = (not all_pres)
        realized_pct = None
        entry_ts = None
        basis_source = ("ibkr realizedPNL (opener predates reqExecutions ~7d window; entry basis "
                        "unavailable -- needs a Flex Query)")
        entry_outside_window = True

    net_open_remaining = open_qty - close_qty  # >0 => runner still open after a partial close
    partial = matched_qty < max(open_qty, close_qty) or net_open_remaining > 0

    outcome_pnl = realized_net if realized_net is not None else realized_gross
    outcome = _outcome(outcome_pnl)

    trade = {
        "schema": "trade_dataset.v2",
        "kind": "trade",
        "source": "manual",
        "manual": True,
        "reasoning_available": False,              # honest: no Alfred thesis/CoT/technical_card
        "ts": _now(),
        "trade_uid": uid,
        "trade_instance_uid": uid_inst,
        "con_id": int(con_id),
        "symbol": symbol,
        "decision": None,                          # NEVER fabricate a decision for a manual trade
        "entry": {
            "ts": entry_ts,
            "symbol": symbol,
            "right": right,
            "strike": strike,
            "expiry": expiry,
            "structure": "single",                 # per-leg; combos are recorded leg-by-leg
            "spread": None,
            "quantity": int(matched_qty) if matched_qty == int(matched_qty) else matched_qty,
            "debit": debit,
            "profit_target_pct": None,
            "stop_pct": None,
            "conviction": None,                    # honest nulls -- no decision arc
            "thesis": None,
            "entry_outside_window": entry_outside_window,
            "basis_source": basis_source,
        },
        "lifecycle": {                             # no marks -- a manual trade never had a mark path
            "mark_path": [], "marks": 0, "mfe_pct": None, "mae_pct": None,
            "drawdown_from_peak_pct": None,
        },
        "close": {
            "ts": close_ts,
            "reason": "manual_close",
            "rule_fired": None,
            "exit_price_per_share": round(avg_close, 6),
            "proceeds": proceeds,
            "realized_pnl": realized_gross,        # REAL (fill-derived, or IBKR realizedPNL)
            "realized_pnl_net": realized_net,      # net of fees when known; else None
            "entry_commission": None if not entry_visible else _entry_commission(opens),
            "exit_commission": _sum_commissions(closes)[0],
            "commission_unknown": commission_unknown,
            "pnl_is_estimate": bool(partial),      # a partial-close allocation is approximate
            "realized_pnl_pct": realized_pct,
            "holding_days": _holding_days(entry_ts, close_ts),
            "realized_pnl_ib": round(sum(f["realized_pnl_ib"] for f in closes), 2) if closes else None,
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

    result["trade"] = trade
    # terminal (watermark every exec_id) only when the position is fully flat -- a still-open runner
    # after a partial close is NOT terminal, so the remaining opens can pair with a later close.
    result["terminal"] = (net_open_remaining <= 0)

    if net_open_remaining > 0:
        # emit the runner as an open-position snapshot alongside the partial-close trade
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


# --------------------------------------------------------------------------- orchestration (PURE)
def process_fills(all_fills: List[dict], idx: dict, watermark: dict,
                  app_client_ids: set) -> Dict[str, Any]:
    """PURE core: given normalized fills + the app-origin index + the watermark, classify manual
    fills, pair them, and return {rows, position_rows, new_processed, stats, manual_fills}. No I/O.
    Fully unit-testable. Never raises into the caller (best-effort per fill)."""
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

    # group manual fills by contract
    by_con: Dict[int, List[dict]] = {}
    for f in manual:
        by_con.setdefault(int(f.get("con_id") or 0), []).append(f)
    stats["contracts"] = len(by_con)

    trade_rows, position_rows = [], []
    new_processed = set()
    for con_id, fills in by_con.items():
        # skip a contract whose every exec is already folded into a terminal trade
        remaining_new = [e for e in (fl["exec_id"] for fl in fills) if e not in processed]
        built = build_rows_for_contract(con_id, fills)
        if built["trade"] is not None:
            # only (re)emit a terminal trade if it carries at least one un-watermarked exec_id
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


# --------------------------------------------------------------------------- IB fetch (READ-ONLY)
def _exec_filter(lookback_days: int):
    """Build an ExecutionFilter windowed to `lookback_days` back. reqExecutions only reaches ~7
    days regardless; deeper history needs a Flex Query."""
    from exitmgr.ibkr import _ib  # the resolved backend module (ib_async|ib_insync)
    ExecutionFilter = _ib.ExecutionFilter
    ef = ExecutionFilter()
    try:
        since = datetime.now() - timedelta(days=max(1, int(lookback_days)))
        ef.time = since.strftime("%Y%m%d-%H:%M:%S")  # ib_async accepts 'yyyymmdd-HH:MM:SS'
    except Exception:
        pass
    return ef


async def fetch_fills(ib, lookback_days: int = 7) -> List[dict]:
    """READ-ONLY: pull account executions (+ bundled commissionReport) and normalize. The ONLY IB
    call in this module. Returns normalized fill dicts. Never raises (returns [] on error)."""
    try:
        ef = _exec_filter(lookback_days)
        fills = await ib.reqExecutionsAsync(ef)
    except Exception as e:
        try:
            print(f"[WARN] reqExecutionsAsync failed: {e}")
        except Exception:
            pass
        return []
    out = []
    for fl in (fills or []):
        n = normalize_fill(fl)
        if n is not None:
            out.append(n)
    return out


# --------------------------------------------------------------------------- append + persist
def _append_rows(dataset_path: str, rows: List[dict], dry_run: bool) -> int:
    """Append rows, deduping against existing _dedup_keys; back up the dataset first. Returns the
    number actually written."""
    if not rows:
        return 0
    existing = _tc._existing_dedup_keys(dataset_path)
    fresh = [r for r in rows if r.get("_dedup_key") and r["_dedup_key"] not in existing]
    # also dedup within this batch
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
    return len(batch)


# --------------------------------------------------------------------------- public entry point
async def capture_external_fills(*, ib=None, config=None, ddir: Optional[str] = None,
                                 host: str = "127.0.0.1", port: int = 4001,
                                 client_id: int = 170, lookback_days: int = 7,
                                 dry_run: bool = False,
                                 app_client_ids: Optional[set] = None) -> Dict[str, Any]:
    """Capture every NON-app-origin (manual) IBKR execution into the trade dataset.

    Reuses an already-connected `ib` handle when passed (the exit-cycle hook path -- zero extra
    sockets); otherwise connects STANDALONE read-only with a fresh dedicated clientId. READ-ONLY:
    never places/cancels/modifies an order. Returns a summary dict; never raises."""
    summary = {"ok": False, "appended": 0, "positions_appended": 0, "manual_fills": 0,
               "stats": {}, "note": None, "clientId": None, "lookback_days": lookback_days}
    owns_conn = False
    conn = None
    try:
        # resolve dataset dir + paths
        if ddir is None:
            journal = getattr(getattr(config, "journal", None), "path", None) if config else None
            ddir = _tc.dataset_dir(journal)
        dpath = _tc.dataset_path(ddir)

        # app-origin allowlist (config trader id + known ids + reader id)
        acids = set(_KNOWN_APP_CLIENT_IDS)
        if app_client_ids:
            acids |= set(app_client_ids)
        if config is not None:
            try:
                acids.add(int(config.ib.client_id))
            except Exception:
                pass

        # ---- obtain a READ-ONLY IB handle ----
        if ib is None:
            from exitmgr.connection import IBConnection
            if config is not None:
                host = getattr(config.ib, "host", host)
                port = getattr(config.ib, "port", port)
            conn = IBConnection(host=host, port=port, client_id=client_id, market_data_type=3)
            owns_conn = True
            connected = False
            for attempt in range(4):  # rotate clientId on Error 326 collisions
                connected = await conn.connect()
                if connected:
                    break
                import random
                conn.client_id = client_id = random.randint(150, 900)
                print(f"[INFO] exec_capture: retrying with fresh clientId={conn.client_id}")
            if not connected:
                summary["note"] = "could not connect to IB gateway"
                return summary
            ib = conn.ib
            summary["clientId"] = conn.client_id
        else:
            summary["clientId"] = "reused-existing-connection"

        # ---- READ-ONLY pull ----
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
    """Sync wrapper for CLI/cron: run capture_external_fills on a fresh event loop."""
    import asyncio
    try:
        return asyncio.run(capture_external_fills(**kwargs))
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(capture_external_fills(**kwargs))
        finally:
            loop.close()


# --------------------------------------------------------------------------- CLI
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

    cfg = None
    try:
        from exitmgr.config import Config
        for _p in ("config.yaml", os.path.join(os.path.dirname(__file__), "..", "config.yaml")):
            if os.path.exists(_p):
                cfg = Config.from_yaml(_p)
                break
    except Exception:
        cfg = None

    s = capture_external_fills_blocking(config=cfg, ddir=args.ddir, host=args.host, port=args.port,
                                        client_id=args.client_id, lookback_days=args.lookback_days,
                                        dry_run=args.dry_run)
    print(json.dumps(s, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
