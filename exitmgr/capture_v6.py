"""Dataset-v6 RAW EVENT capture (2026-07-21).

Raw-event-log-NOW, render-to-training-rows-LATER. Every Alfred trade decision/action emits
an immutable, receipt-bound event to a dedicated capture store, kept SEPARATE from the v2
training dataset (exitmgr/trade_capture.py -> data/trade_dataset.jsonl). The raw event log is
the durable asset; rendering it into M3-format training rows is cheap to redo. Implements the
Alfred-side of the dataset-v6 capture spec (the operator's manual Fidelity trades are captured
separately and are NOT handled here).

Store layout (default ~/trade-capture; override with env TRADE_CAPTURE_DIR -- the test suite
points it at a tmp dir so pytest never pollutes the production store):

    events.jsonl          append-only event stream, one JSON object per line
    blobs/<sha256>.json    large context blobs, content-addressed + deduped (write-once)
    labels.jsonl          POST-HOC labels (process grade, divergence, counterfactual),
                          keyed by trade_uid -- attached LATER, never at capture time
    README.md             event schema + how labels attach (written by scripts/init_store)

Every event is a JSON object:
    {
      "schema": "trade_capture.v6",
      "event_type": <str>,          # entry_decision | no_trade | rejected | order_fill |
                                    #   exit_action | exit_unfilled | position_path
      "ts": <ISO-8601 UTC>,         # real event time, from the clock -- never reconstructed
      "trade_uid": <str|None>,      # STABLE join key (deterministic uuid5 of trade identity);
                                    #   the SAME trade's decision/fill/exit/label rows all share it
      "decision_id": <str|None>,
      "symbol": <str|None>,
      "con_id": <int|None>,
      "order_ids": [<int|str>...],   # receipts: IBKR order ids where applicable
      "receipts": {...},             # exec ids / order refs / con_id -- provenance, no reconstruction
      "context_sha256": <str|None>,  # sha256 of the large context blob (stored under blobs/)
      ... event-specific fields (direction, conviction, construction, dte_check, pnl, mfe/mae, ...)
    }

FAIL-OPEN CONTRACT: every public function swallows ALL exceptions. A capture bug can NEVER
raise into, block, or alter the trading path (mirrors trade_capture.py). Callers add their own
try/except too, so this is defence-in-depth.
"""
import hashlib
import json
import os
from datetime import datetime, timezone, date

SCHEMA = "trade_capture.v6"

# Cap a single event's inline (non-blob) size defensively; the heavy context always goes to a
# content-addressed blob, so the event line itself stays small.
_DEFAULT_DIR = os.path.join(os.path.expanduser("~"), "trade-capture")


# --------------------------------------------------------------------------- store paths
def store_dir() -> str:
    """Resolve the capture store dir: env TRADE_CAPTURE_DIR wins (test isolation), else
    ~/trade-capture. Created on demand. Best-effort; falls back to '.' and never raises."""
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


# --------------------------------------------------------------------------- primitives
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_default(o):
    # last-resort serializer for odd objects (dataclasses/namespaces already dict-ified upstream)
    try:
        return o.__dict__
    except Exception:
        return str(o)


def _canonical(obj) -> str:
    """Deterministic JSON so identical context content hashes identically (stable blob dedup)."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=_json_default)


def store_blob(obj, d=None):
    """Content-address a context blob: sha256 of its canonical JSON, write blobs/<sha>.json
    write-once (skip if it already exists), return the sha. Returns None for empty/failed.
    Never raises."""
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
            os.replace(tmp, bp)  # atomic; concurrent writers land the identical bytes
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
    """Flatten a mixed set of order-id inputs (scalars/lists/None) into a clean unique list."""
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


# --------------------------------------------------------------------------- core writer
def emit(event_type: str, *, trade_uid=None, decision_id=None, symbol=None, con_id=None,
         order_ids=None, receipts=None, context=None, **fields):
    """Append ONE raw event. `context` (any JSON-able object) is stored as a content-addressed
    blob and referenced by `context_sha256`; small scalar/field data goes inline via **fields.
    Returns the event dict, or None on any failure. NEVER raises."""
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


# --------------------------------------------------------------------------- 8x-DTE rule check
def _parse_day(v):
    """Parse an expiry/day that may be 'YYYYMMDD', 'YYYY-MM-DD', a date/datetime, or None."""
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
    """Doctrine gate (see feedback_theta_8x_dte_rule): buy ~`multiple`x longer DTE than the
    intended trade window to avoid theta bleed. Records the chosen DTE, the trade window, their
    ratio, and pass/fail -- explicitly, at decision time.

    dte: chosen days-to-expiry (used verbatim when given); else derived from `expiry` - `ref_date`.
    trade_window_days: intended holding horizon; when unknown, ratio/rule_ok are None but DTE is
    still recorded. Never raises."""
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
            "ratio": ratio,          # dte / trade_window
            "rule_ok": rule_ok,      # None when trade_window unknown
            "checked_at": _now_iso(),
        }
    except Exception:
        return {"rule": "8x_dte", "multiple": multiple, "dte": None, "trade_window_days": None,
                "ratio": None, "rule_ok": None}


# --------------------------------------------------------------------------- high-level hooks
# Each takes the dict a production capture function ALREADY built, so the call-site edit is one
# fail-open line. All swallow every exception.

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
    """Entry-decision event from trade_capture.capture_decision's rec. Captures the EXACT context
    Alfred saw (RAG/news/journal brief + technical card + raw model output + CoT + regime -> blob,
    hashed), the parsed thesis/direction/conviction, the chosen construction, EVERY candidate incl.
    rejected alternatives, sizing, the risk gate, and an explicit 8x-DTE-rule check."""
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
            "market_context": rec.get("market_context"),   # RAG/news/journal/quote brief
            "technical_card": rec.get("technical_card"),
            "raw_strategist": rec.get("raw_strategist"),    # clean model answer
            "cot": rec.get("cot"),                          # chain-of-thought (if returned)
            "regime": rec.get("regime"),
            "candidates": candidates,                       # full considered set + convictions
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
    """No-trade / abstention event (the flow declined to trade). Learns from passes."""
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
    """Gate/constructor rejection event: an idea a gate threw out + the rejected construction."""
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
    """Triggered exit that did not fill (terminal reject/cancel or abandoned resting order)."""
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
    """Exit-management action event: EVERY realized exit (TP tier, runner, stop, roll, model cut,
    manual, expiry). Captures the reason/rule that fired, order/fill receipts (limit, mid/trigger
    mark, fill price, slippage, order id), realized P&L, running MFE/MAE, and the FULL position P&L
    path (mark_path -> blob) plus the entry journal, so exits can be graded against the path."""
    try:
        exit_rec = exit_rec if isinstance(exit_rec, dict) else {}
        je = je if isinstance(je, dict) else {}
        symbol = exit_rec.get("symbol") or je.get("symbol")
        # stable join key: same uid the decision row carries (lazy import breaks the cycle).
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
            realized_pnl=exit_rec.get("realized_pnl"),
            realized_pnl_pct=exit_rec.get("realized_pnl_pct"),
            exit_price_per_share=exit_rec.get("exit_price_per_share"),
            limit_price=exit_rec.get("limit_price"),
            trigger_mark=exit_rec.get("trigger_mark"),
            bid=exit_rec.get("bid"),
            slippage_per_share=exit_rec.get("slippage_per_share"),
            mfe_pct=mfe, mae_pct=mae, marks=len(mark_path or []),
            entry_debit=je.get("debit") or je.get("entry_debit"),
            entry_slippage=je.get("entry_slippage"),
            partial=bool((exit_rec.get("extra") or {}).get("partial")) if isinstance(exit_rec.get("extra"), dict) else exit_rec.get("partial"),
        )
    except Exception:
        return None


def on_position_mark(con_id, *, symbol=None, enrich=None, pnl_pct=None):
    """Periodic position-path sample, piggybacked on the exit manager's existing per-cycle
    record_mark poll (NO new polling loop). Lightweight: no blob. Feeds MAE/MFE path grading."""
    try:
        enrich = enrich if isinstance(enrich, dict) else {}
        tuid = None
        try:
            from exitmgr import trade_capture as _tc
            tuid = _tc.trade_uid(con_id=con_id, symbol=symbol or enrich.get("symbol"))
        except Exception:
            tuid = None
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
        )
    except Exception:
        return None


def on_fill(row: dict):
    """Order/fill event from a reconciled execution row (exec_capture): limit/basis, fill price,
    slippage, and order/exec-id receipts. Covers entry + exit + manual fills the sweep reconciles."""
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


# --------------------------------------------------------------------------- post-hoc labels
def attach_label(trade_uid: str, label: dict, d=None) -> bool:
    """Attach a POST-HOC label to a trade, keyed by trade_uid, in labels.jsonl. This is the
    documented mechanism for divergence logs, PROCESS grades (Byron-style, separate from outcome),
    and counterfactual returns -- all applied LATER, never at capture time. Append-only; the latest
    row for a trade_uid wins. Never raises."""
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
