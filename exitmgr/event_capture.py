"""Public API contract; production-derived narrative omitted."""
import hashlib
import json
import os
from datetime import datetime, timezone, date

SCHEMA = "trade_capture.v6"



_DEFAULT_DIR = os.path.join(os.path.expanduser("~"), "trade-capture")



def store_dir() -> str:
    """Public API contract; production-derived narrative omitted."""
    try:
        d = os.environ.get("TRADE_CAPTURE_DIR") or _DEFAULT_DIR
        os.makedirs(d, exist_ok=True)
        os.makedirs(os.path.join(d, "blobs"), exist_ok=True)
        return d
    except Exception:
        return "."


def events_path(d=None) -> str:
    return os.path.join(d or store_dir(), "events.jsonl")


def blobs_dir(d=None) -> str:
    return os.path.join(d or store_dir(), "blobs")


def labels_path(d=None) -> str:
    return os.path.join(d or store_dir(), "labels.jsonl")



def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_default(o):

    try:
        return o.__dict__
    except Exception:
        return str(o)


def _canonical(obj) -> str:
    """Public API contract; production-derived narrative omitted."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=_json_default)


def store_blob(obj, d=None):
    """Public API contract; production-derived narrative omitted."""
    try:
        if obj is None:
            return None
        payload = _canonical(obj)
        if payload in ("null", "{}", "[]", '""'):
            return None
        sha = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        d = d or store_dir()
        bp = os.path.join(blobs_dir(d), sha + ".json")
        if not os.path.exists(bp):
            tmp = bp + ".tmp"
            with open(tmp, "w") as f:
                f.write(payload)
            os.replace(tmp, bp)
        return sha
    except Exception:
        return None


def _append_event(rec: dict, d=None) -> bool:
    try:
        d = d or store_dir()
        with open(events_path(d), "a") as f:
            f.write(json.dumps(rec, default=_json_default) + "\n")
        return True
    except Exception:
        return False


def _order_ids(*vals):
    """Public API contract; production-derived narrative omitted."""
    out = []
    for v in vals:
        if v is None:
            continue
        seq = v if isinstance(v, (list, tuple, set)) else [v]
        for x in seq:
            if x in (None, "", 0, "0"):
                continue
            if x not in out:
                out.append(x)
    return out















IDENTITY_SOURCE_META = "meta"
IDENTITY_SOURCE_CONFIG = "config"
IDENTITY_SOURCE_UNKNOWN = "unknown"


def identity_source(identity, declared=None):
    """Public API contract; production-derived narrative omitted."""
    if declared:
        return str(declared)
    if identity is None:
        return None
    return IDENTITY_SOURCE_UNKNOWN


def emit(event_type: str, *, trade_uid=None, decision_id=None, symbol=None, con_id=None,
         order_ids=None, receipts=None, context=None, **fields):
    """Public API contract; production-derived narrative omitted."""
    try:
        d = store_dir()
        sha = store_blob(context, d) if context is not None else None
        rec = {
            "schema": SCHEMA,
            "event_type": event_type,
            "ts": _now_iso(),
            "trade_uid": trade_uid,
            "decision_id": decision_id,
            "symbol": symbol,
            "con_id": con_id,
            "order_ids": _order_ids(order_ids),
            "receipts": receipts or {},
            "context_sha256": sha,
        }
        for k, v in fields.items():
            if k not in rec:
                rec[k] = v
        _append_event(rec, d)
        return rec
    except Exception:
        return None



def _parse_day(v):
    """Public API contract; production-derived narrative omitted."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s[:10] if "-" in s or "/" in s else s[:8], fmt).date()
        except Exception:
            continue
    return None


def eightx_dte_check(*, dte=None, expiry=None, trade_window_days=None, ref_date=None,
                     multiple: int = 8) -> dict:
    """Public API contract; production-derived narrative omitted."""
    try:
        ref = _parse_day(ref_date) or datetime.now(timezone.utc).date()
        d = None
        try:
            d = int(dte) if dte is not None else None
        except Exception:
            d = None
        if d is None:
            ex = _parse_day(expiry)
            if ex is not None:
                d = (ex - ref).days
        tw = None
        try:
            tw = float(trade_window_days) if trade_window_days is not None else None
        except Exception:
            tw = None
        ratio = None
        rule_ok = None
        if d is not None and tw is not None and tw > 0:
            ratio = round(d / tw, 2)
            rule_ok = bool(d >= multiple * tw)
        return {
            "rule": "8x_dte",
            "multiple": multiple,
            "dte": d,
            "trade_window_days": tw,
            "ratio": ratio,
            "rule_ok": rule_ok,
            "checked_at": _now_iso(),
        }
    except Exception:
        return {"rule": "8x_dte", "multiple": multiple, "dte": None, "trade_window_days": None,
                "ratio": None, "rule_ok": None}






def _g(d, *keys, default=None):
    cur = d if isinstance(d, dict) else {}
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur if cur is not None else default


def on_decision(rec: dict):
    """Public API contract; production-derived narrative omitted."""
    try:
        if not isinstance(rec, dict):
            return None
        chosen = rec.get("chosen") or {}
        construction = rec.get("construction") or {}
        candidates = rec.get("candidates") or []
        chosen_sym = rec.get("symbol")
        rejected = [c for c in candidates
                    if isinstance(c, dict) and c.get("symbol") not in (None, chosen_sym)]
        dte_chk = eightx_dte_check(
            dte=construction.get("dte") if isinstance(construction, dict) else None,
            expiry=rec.get("expiry"),
            trade_window_days=(construction.get("trade_window_days")
                               if isinstance(construction, dict) else None))
        context = {
            "market_context": rec.get("market_context"),
            "technical_card": rec.get("technical_card"),
            "raw_strategist": rec.get("raw_strategist"),
            "cot": rec.get("cot"),
            "regime": rec.get("regime"),
            "candidates": candidates,
            "gate": rec.get("gate"),
            "sizing": rec.get("sizing"),
        }
        return emit(
            "entry_decision",
            trade_uid=rec.get("trade_uid"),
            decision_id=rec.get("decision_id"),
            symbol=chosen_sym,
            con_id=rec.get("con_id"),
            order_ids=rec.get("order_ref"),
            receipts={"decision_id": rec.get("decision_id"), "order_ref": rec.get("order_ref"),
                      "event": rec.get("event"), "revision": rec.get("revision"),
                      "source": rec.get("source")},
            context=context,
            event=rec.get("event"),
            source=rec.get("source"),
            direction=_g(chosen, "direction") or _g(chosen, "side"),
            conviction=_g(chosen, "conviction"),
            thesis=_g(chosen, "thesis"),
            structure=rec.get("structure"),
            right=rec.get("right"), strike=rec.get("strike"), expiry=rec.get("expiry"),
            construction=construction,
            sizing=rec.get("sizing"),
            gate_approved=_g(rec.get("gate") or {}, "approved"),
            per_trade_cap=_g(rec.get("gate") or {}, "per_trade_cap"),
            rejected_alternatives=rejected,
            dte_check=dte_chk,
        )
    except Exception:
        return None


def on_no_trade(rec: dict):
    """Public API contract; production-derived narrative omitted."""
    try:
        if not isinstance(rec, dict):
            return None
        context = {"raw_strategist": rec.get("raw_strategist"), "cot": rec.get("cot"),
                   "candidates": rec.get("candidates"), "regime": rec.get("regime"),
                   "market_context": rec.get("market_context")}
        return emit("no_trade", symbol=rec.get("symbol"), source=rec.get("source"),
                    reason=rec.get("reason"), context=context)
    except Exception:
        return None


def on_rejected(rec: dict):
    """Public API contract; production-derived narrative omitted."""
    try:
        if not isinstance(rec, dict):
            return None
        construction = rec.get("construction") or {}
        context = {"idea": rec.get("idea"), "gate": rec.get("gate"),
                   "construction": construction, "regime": rec.get("regime")}
        return emit("rejected", trade_uid=rec.get("trade_uid"), decision_id=rec.get("decision_id"),
                    symbol=rec.get("symbol"), source=rec.get("source"),
                    stage=rec.get("stage"), reason=rec.get("reason"),
                    structure=rec.get("structure"), right=rec.get("right"),
                    strike=rec.get("strike"), expiry=rec.get("expiry"),
                    construction=construction, context=context)
    except Exception:
        return None


def on_unfilled(rec: dict):
    """Public API contract; production-derived narrative omitted."""
    try:
        if not isinstance(rec, dict):
            return None
        close = rec.get("close") or {}
        return emit("exit_unfilled", trade_uid=rec.get("trade_uid"), symbol=rec.get("symbol"),
                    con_id=rec.get("con_id"), order_ids=close.get("order_id"),
                    receipts={"order_id": close.get("order_id"), "placed_at": close.get("placed_at")},
                    reason=close.get("reason"), rule_fired=close.get("rule_fired"),
                    fill_status=close.get("fill_status"), limit_price=close.get("limit_price"),
                    trigger_mark=close.get("trigger_mark"), bid=close.get("bid"),
                    close_qty=close.get("close_qty"))
    except Exception:
        return None


def on_exit(exit_rec: dict, je: dict, con_id, *, mfe=None, mae=None, mark_path=None):
    """Public API contract; production-derived narrative omitted."""
    try:
        exit_rec = exit_rec if isinstance(exit_rec, dict) else {}
        je = je if isinstance(je, dict) else {}
        symbol = exit_rec.get("symbol") or je.get("symbol")

        tuid = None
        try:
            from exitmgr import trade_capture as _tc
            tuid = _tc.trade_uid(con_id=con_id, symbol=symbol)
        except Exception:
            tuid = None
        context = {"entry_journal": je, "mark_path": mark_path or [], "exit_rec": exit_rec}
        return emit(
            "exit_action",
            trade_uid=tuid,
            symbol=symbol,
            con_id=int(con_id) if con_id is not None else None,
            order_ids=exit_rec.get("order_id"),
            receipts={"order_id": exit_rec.get("order_id"), "con_id": con_id},
            context=context,
            reason=exit_rec.get("reason"),
            rule_fired=exit_rec.get("rule_fired"),




            entry_model_identity=je.get("model_identity"),
            entry_model_identity_source=identity_source(
                je.get("model_identity"), je.get("model_identity_source")),
            exit_model_identity=exit_rec.get("exit_model_identity"),
            exit_model_identity_source=identity_source(
                exit_rec.get("exit_model_identity"),
                exit_rec.get("exit_model_identity_source")),
            realized_pnl=exit_rec.get("realized_pnl"),
            realized_pnl_pct=exit_rec.get("realized_pnl_pct"),
            exit_price_per_share=exit_rec.get("exit_price_per_share"),
            limit_price=exit_rec.get("limit_price"),
            trigger_mark=exit_rec.get("trigger_mark"),
            bid=exit_rec.get("bid"),
            slippage_per_share=exit_rec.get("slippage_per_share"),
            mfe_pct=mfe, mae_pct=mae, marks=len(mark_path or []),


            entry_debit=(je.get("entry_debit") if je.get("debit") is None
                         else je.get("debit")),
            entry_slippage=je.get("entry_slippage"),
            partial=bool((exit_rec.get("extra") or {}).get("partial")) if isinstance(exit_rec.get("extra"), dict) else exit_rec.get("partial"),
        )
    except Exception:
        return None


def on_position_mark(con_id, *, symbol=None, enrich=None, pnl_pct=None):
    """Public API contract; production-derived narrative omitted."""
    try:
        enrich = enrich if isinstance(enrich, dict) else {}
        tuid = None
        try:
            from exitmgr import trade_capture as _tc
            tuid = _tc.trade_uid(con_id=con_id, symbol=symbol or enrich.get("symbol"))
        except Exception:
            tuid = None
        model_attempted = enrich.get("mgmt_attempt_ok") is not None
        return emit(
            "position_path",
            trade_uid=tuid,
            symbol=symbol,
            con_id=int(con_id) if con_id is not None else None,
            pnl_pct=pnl_pct,
            underlying=enrich.get("underlying"),
            iv=enrich.get("iv"), delta=enrich.get("delta"),
            dte=enrich.get("dte"), days_held=enrich.get("days_held"),
            dist_to_tp_pct=enrich.get("dist_to_tp_pct"),
            dist_to_sl_pct=enrich.get("dist_to_sl_pct"),
            mgmt_action=enrich.get("mgmt_action"), mgmt_reason=enrich.get("mgmt_reason"),



            mgmt_attempt_ok=enrich.get("mgmt_attempt_ok"),
            mgmt_failure_reason=enrich.get("mgmt_failure_reason"),




            mgmt_input=(enrich.get("mgmt_input") if model_attempted else None),
            mgmt_model_identity=(enrich.get("mgmt_model_identity")
                                 if model_attempted else None),



            mgmt_model_identity_source=(
                identity_source(enrich.get("mgmt_model_identity"),
                                enrich.get("mgmt_model_identity_source"))
                if model_attempted else None),
            gamma=enrich.get("gamma"), theta=enrich.get("theta"), vega=enrich.get("vega"),





            price=enrich.get("price"), value=enrich.get("value"),
            is_net_spread=enrich.get("is_net_spread"),



            mgmt_raw=(enrich.get("mgmt_raw") if model_attempted else None),
        )
    except Exception:
        return None


def on_fill(row: dict):
    """Public API contract; production-derived narrative omitted."""
    try:
        if not isinstance(row, dict):
            return None
        entry = row.get("entry") or {}
        close = row.get("close") or {}
        prov = row.get("provenance") or {}
        order_ids = _order_ids(close.get("order_id"), row.get("order_id"),
                               prov.get("order_ids"))
        receipts = {"exec_ids": prov.get("exec_ids"), "order_ids": prov.get("order_ids"),
                    "perm_ids": prov.get("perm_ids"),
                    "trade_instance_uid": row.get("trade_instance_uid")}
        return emit(
            "order_fill",
            trade_uid=row.get("trade_uid"),
            symbol=row.get("symbol"),
            con_id=row.get("con_id"),
            order_ids=order_ids,
            receipts=receipts,
            kind=row.get("kind"),
            source=row.get("source"),
            manual=row.get("manual"),
            entry_debit=entry.get("debit"),
            entry_slippage=entry.get("entry_slippage"),
            exit_price_per_share=close.get("exit_price_per_share"),
            realized_pnl=close.get("realized_pnl"),
            realized_pnl_net=close.get("realized_pnl_net"),
            realized_pnl_pct=close.get("realized_pnl_pct"),
            fill_status=close.get("fill_status"),
        )
    except Exception:
        return None



def attach_label(trade_uid: str, label: dict, d=None) -> bool:
    """Public API contract; production-derived narrative omitted."""
    try:
        if not trade_uid or not isinstance(label, dict):
            return False
        rec = {"schema": "trade_capture.labels.v6", "ts": _now_iso(),
               "trade_uid": trade_uid, **label}
        d = d or store_dir()
        with open(labels_path(d), "a") as f:
            f.write(json.dumps(rec, default=_json_default) + "\n")
        return True
    except Exception:
        return False
