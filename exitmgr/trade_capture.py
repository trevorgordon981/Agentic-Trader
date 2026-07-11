"""Full-arc trade-decision capture (2026-07-02, trade_dataset.v2).

The v1 dataset (written by ExitManager at CLOSE) captured entry snapshot + mark path +
MFE/MAE + close + labels for every CLOSED trade. It had NO record of the DECISION that
produced the trade (the strategist's raw reasoning, the candidate ideas + convictions, the
risk-gate outcome, the construction adjustments, the regime), and it never learned from
NO-TRADE passes or gate REJECTIONS.

This module fills that gap. It is a pure, side-effect-light, NEVER-RAISING capture layer:

  * capture_decision()  -> appends the full decision context for an ENTERED idea to the
                           `decision_context.jsonl` sidecar (keyed by symbol/strike/expiry/right,
                           and con_id when known). The ExitManager joins it into the closed-trade
                           v2 record at close via load_decision_context().
  * capture_no_trade()  -> appends a light `kind:"no_trade"` row to trade_dataset.jsonl.
  * capture_rejected()  -> appends a light `kind:"rejected"` row to trade_dataset.jsonl.
  * load_decision_context() / load_review() -> best-effort read-back for the closed-trade record.

RECORD-ONLY + DEFENSIVE: every public function swallows all exceptions. A capture bug can
never raise into the trading path, alter a decision, or block an entry/exit. The sidecar is
append-only JSONL; readers tolerate partial/corrupt lines.
"""
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Optional, List

from exitmgr import dataset_integrity as _di

SCHEMA = "trade_dataset.v2"
DECISION_SCHEMA = "decision_context.v2"

# --------------------------------------------------------------------------- trade join key
# DATA-INTEGRITY (2026-07-03): entry (decision) rows and their later mgmt/close/terminal rows must
# share ONE durable key so an SFT builder can pair a decision with its REAL realized outcome. The
# closed-trade row is written by ExitManager (manager.py, owned by the trading-safety wave and NOT
# edited here), so we cannot hand a random UUID from entry to close. Instead we DERIVE a stable,
# reproducible UUID from the trade's immutable identity: both the decision row (stamped here at
# entry) and the close row (derived by the exporter/backfill from the same identity) resolve to the
# IDENTICAL uid with no write-time handoff. uuid5 (namespace + name) is deterministic, so the key is
# durable across retries, re-runs, and the entry->close boundary.
#
# Identity precedence mirrors load_decision_context's join precedence: the IB con_id when known
# (a specific option contract), else the (symbol, strike, expiry, right) leg tuple. NOTE: con_id
# identifies a CONTRACT, so re-trading the exact same contract on a later date would collide; that
# is the same limitation the existing con_id join already carries. Pass entry_day to disambiguate
# instance-level dedup where a real entry date is known (backfill/close rows).
_TRADE_UID_NAMESPACE = uuid.UUID("6f2a1c94-0b3e-5d7a-9c11-7e2f4a8b6d30")


def _norm(v) -> str:
    return "" if v is None else str(v).strip()


def trade_uid(*, con_id=None, symbol=None, strike=None, expiry=None, right=None,
              entry_day=None) -> Optional[str]:
    """Deterministic per-trade UUID (uuid5) derived from the trade's immutable identity. The SAME
    inputs ALWAYS produce the SAME uuid, so a decision row and its close row link without any
    write-time handoff. Returns None only if there is no usable identity at all. Never raises."""
    try:
        cid = _norm(con_id)
        day = _norm(entry_day)[:10]
        if cid not in ("", "0", "None"):
            ident = f"conid={cid}"
        else:
            sym, k, x, r = _norm(symbol), _norm(strike), _norm(expiry), _norm(right)
            if not sym:
                return None
            ident = f"leg={sym}|{k}|{x}|{r}"
        if day:
            ident = f"{ident}|d={day}"
        return str(uuid.uuid5(_TRADE_UID_NAMESPACE, ident))
    except Exception:
        return None

# Cap the verbatim market-context brief we store per decision so the sidecar can't grow
# without bound (the brief can be ~tens of KB). Full RAG/news/journal/technical text is
# captured up to this many chars, then truncated with a marker.
_CONTEXT_CAP = 24000
# raw_strategist can hold the model's FULL verbatim output INCLUDING chain-of-thought (when the
# serving endpoint returns CoT rather than stripping it). A long reasoning trace can exceed the old
# 24KB brief cap, so raw gets its own larger cap (2026-07-03) -- single field, no new column.
_RAW_CAP = 64000


# --------------------------------------------------------------------------- paths
def dataset_dir(journal_path: Optional[str], cfg_dataset_path: Optional[str] = None) -> str:
    """Resolve the directory holding the dataset + decision sidecar, mirroring
    ExitManager._dataset_path: a `data/` dir next to the journal, or the parent of an
    explicit config dataset path. Best-effort; falls back to '.'.

    TEST/OVERRIDE ISOLATION: if the `EXITMGR_DATASET_DIR` environment variable is set and
    non-empty, it wins over the journal/config resolution and is returned verbatim (created if
    needed). This lets the test suite (via an autouse conftest fixture) redirect ALL capture
    writes into a tmp dir so pytest never pollutes the production `data/*.jsonl` training set,
    with ZERO changes to production call-sites (daily_recommend/trader/manager pass no override
    and keep resolving the real `data/` dir as before)."""
    try:
        env_dir = os.environ.get("EXITMGR_DATASET_DIR")
        if env_dir:
            try:
                os.makedirs(env_dir, exist_ok=True)
            except Exception:
                pass
            return env_dir
        if cfg_dataset_path:
            return os.path.dirname(cfg_dataset_path) or "."
        base = os.path.dirname(journal_path) if journal_path else "."
        base = base or "."
        d = os.path.join(base, "data")
        try:
            os.makedirs(d, exist_ok=True)
            return d
        except Exception:
            return base
    except Exception:
        return "."


def decision_context_path(ddir: str) -> str:
    return os.path.join(ddir or ".", "decision_context.jsonl")


def dataset_path(ddir: str) -> str:
    return os.path.join(ddir or ".", "trade_dataset.jsonl")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dedup_key(rec: dict) -> Optional[str]:
    """Stable idempotency key for a capture row. A retried/re-run capture of the SAME event
    produces the SAME key so it is written ONCE. Keyed on the full record content with the
    volatile timestamp collapsed to DAY granularity (so a within-day retry dedups, but the same
    event on a genuinely different day is still kept). Never raises."""
    try:
        r = {k: v for k, v in rec.items() if k != "_dedup_key"}
        decision_id = r.get("decision_id")
        if decision_id:
            # Decision events are revisioned. Never collapse distinct proposal/approval/submit
            # events merely because they happened on the same day.
            blob = json.dumps(r, sort_keys=True, default=str)
            return (f"{rec.get('kind', '')}:{decision_id}:{r.get('revision', 0)}:"
                    f"{r.get('event', '')}:{hashlib.sha256(blob.encode()).hexdigest()}")
        ts = r.get("ts")
        if isinstance(ts, str) and len(ts) >= 10:
            r["ts"] = ts[:10]  # day granularity -> retries within a day collapse
        blob = json.dumps(r, sort_keys=True, default=str)
        return f"{rec.get('kind', '')}:{hashlib.sha1(blob.encode()).hexdigest()}"
    except Exception:
        return None


def _existing_dedup_keys(path: str) -> set:
    """Best-effort set of dedup keys already present in an append-only JSONL. Small files only
    (the capture corpus is a few hundred rows); guarded, never raises. Rows written before this
    change carry no `_dedup_key`, so a key is recomputed for them on the fly."""
    keys = set()
    try:
        if not os.path.exists(path):
            return keys
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                k = row.get("_dedup_key") or _dedup_key(row)
                if k:
                    keys.add(k)
    except Exception:
        pass
    return keys


def _append(path: str, rec: dict, *, dedup: bool = False) -> bool:
    """Append a capture row. When dedup=True the row is stamped with a stable `_dedup_key` and
    written ONLY if that key is not already present (idempotent re-run/retry). Never raises."""
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if dedup:
            key = _dedup_key(rec)
            if key is not None:
                rec = {**rec, "_dedup_key": key}
                if key in _existing_dedup_keys(path):
                    return False  # idempotent skip -- identical event already captured
        with open(path, "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
        return True
    except Exception as e:  # never raise into the trading path
        try:
            print(f"[WARN] trade_capture append failed ({path}): {e}")
        except Exception:
            pass
        return False


def dedupe_file(path: str) -> dict:
    """Idempotent maintenance pass: rewrite a capture JSONL in place keeping only the FIRST
    occurrence of each dedup key (and every row that has no derivable key). Preserves order.
    Returns {before, after, dropped}. Best-effort, atomic via a temp file; never raises."""
    stats = {"before": 0, "after": 0, "dropped": 0}
    try:
        if not os.path.exists(path):
            return stats
        seen = set()
        kept = []
        with open(path) as f:
            for line in f:
                line = line.rstrip("\n")
                if not line.strip():
                    continue
                stats["before"] += 1
                try:
                    row = json.loads(line)
                except Exception:
                    kept.append(line)  # keep unparseable lines verbatim (never silently drop data)
                    continue
                key = row.get("_dedup_key") or _dedup_key(row)
                if key is not None and key in seen:
                    stats["dropped"] += 1
                    continue
                if key is not None:
                    seen.add(key)
                kept.append(line)
        tmp = path + ".dedupe.tmp"
        with open(tmp, "w") as f:
            for line in kept:
                f.write(line + "\n")
        os.replace(tmp, path)
        stats["after"] = len(kept)
    except Exception as e:
        try:
            print(f"[WARN] dedupe_file failed ({path}): {e}")
        except Exception:
            pass
    return stats


def _cap(s, n: int):
    try:
        if s is None:
            return None
        s = s if isinstance(s, str) else json.dumps(s, default=str)
        if len(s) > n:
            return s[:n] + f"...[truncated {len(s) - n} chars]"
        return s
    except Exception:
        return None


# --------------------------------------------------------------------------- normalizers
def _as_dict(obj):
    """Best-effort dataclass/obj -> plain dict, JSON-safe."""
    if obj is None:
        return None
    try:
        if isinstance(obj, dict):
            d = obj
        else:
            from dataclasses import is_dataclass, asdict as _asdict
            if is_dataclass(obj):
                d = _asdict(obj)
            elif hasattr(obj, "__dict__"):
                d = {k: v for k, v in vars(obj).items() if not k.startswith("_")}
            else:
                return {"repr": _cap(repr(obj), 2000)}
        # strip un-serializable values (e.g. qualified IB contracts)
        out = {}
        for k, v in d.items():
            if isinstance(v, (str, int, float, bool)) or v is None:
                out[k] = v
            elif isinstance(v, (list, tuple)):
                out[k] = [x for x in v if isinstance(x, (str, int, float, bool)) or x is None]
            elif isinstance(v, dict):
                out[k] = {kk: vv for kk, vv in v.items()
                          if isinstance(vv, (str, int, float, bool)) or vv is None}
            else:
                out[k] = _cap(repr(v), 400)
        return out
    except Exception:
        return None


def _gate_dict(gate):
    """GateDecision -> {approved, reasons, per_trade_cap, bound_caps}. `bound_caps` = the
    reasons list (every failing cap the pure gate enumerates), so a rejected idea records
    exactly which caps bound it; for an approved idea it's [] and per_trade_cap is the binding
    size."""
    if gate is None:
        return None
    try:
        d = _as_dict(gate) or {}
        reasons = d.get("reasons") or []
        return {
            "approved": bool(d.get("approved")),
            "reasons": list(reasons),
            "bound_caps": list(reasons),
            "per_trade_cap": d.get("per_trade_cap"),
        }
    except Exception:
        return None


def _candidates(ideas):
    out = []
    try:
        for i in (ideas or []):
            d = _as_dict(i)
            if d is not None:
                out.append(d)
    except Exception:
        pass
    return out


# --------------------------------------------------------------------------- capture: ENTERED
def capture_decision(ddir: str, *, source: str, symbol: str,
                     right=None, strike=None, expiry=None, structure=None, con_id=None,
                     chosen_idea=None, candidates=None, raw_strategist=None, cot=None, gate=None,
                     construction=None, technical_card=None, regime=None,
                     market_context=None, sizing=None, extra=None, decision_id=None,
                     revision: int = 0, event: str = "proposal", model_identity=None,
                     final_contract=None, order_ref=None, human_action=None) -> Optional[dict]:
    """Append the FULL decision context for an idea that was chosen/entered. Joined into the
    closed-trade v2 record at close by load_decision_context(). Never raises."""
    try:
        rec = {
            "schema": DECISION_SCHEMA,
            "kind": "decision",
            "ts": _now(),
            "decision_id": decision_id,
            "revision": int(revision or 0),
            "event": event,
            # durable per-trade join key: the close row (written by manager) resolves to the SAME
            # uid from the same identity, so an SFT builder can pair this decision with its outcome.
            "trade_uid": trade_uid(con_id=con_id, symbol=symbol, strike=strike,
                                   expiry=expiry, right=right),
            "source": source,                       # "trader" | "daily_slate"
            "symbol": symbol,
            "right": right,
            "strike": strike,
            "expiry": expiry,
            "structure": structure,
            "con_id": con_id,                       # known in the trader path; None for daily slate
            "chosen": _as_dict(chosen_idea),        # the picked idea (conviction/thesis/direction/...)
            "candidates": _candidates(candidates),  # EVERY idea considered this cycle + its conviction
            "raw_strategist": _cap(raw_strategist, _RAW_CAP),  # clean answer (post-</mm:think>)
            "cot": _cap(cot, _RAW_CAP),  # [m3cot] chain-of-thought (reasoning_content), SEPARATE from the answer; None if absent
            "gate": _gate_dict(gate),               # risk GateDecision (approved + bound caps + cap $)
            "construction": _as_dict(construction), # tp/sl clamp, dte adjust, premium downsizing, budget
            "technical_card": _cap(technical_card, 8000),      # the technical card fed to the model
            "regime": _as_dict(regime),             # regime snapshot (bull/neutral/risk_off + trend/vix)
            "market_context": _cap(market_context, _CONTEXT_CAP),  # RAG/news/journal/quote brief
            "sizing": _as_dict(sizing),
            "model_identity": _as_dict(model_identity),
            "final_contract": _as_dict(final_contract),
            "order_ref": order_ref,
            "human_action": _as_dict(human_action),
            "extra": _as_dict(extra),
        }
        _append(decision_context_path(ddir), rec, dedup=True)
        return rec
    except Exception as e:
        try:
            print(f"[WARN] capture_decision failed for {symbol}: {e}")
        except Exception:
            pass
        return None


# --------------------------------------------------------------------------- capture: NO-TRADE
def capture_no_trade(ddir: str, *, source: str, reason=None, raw_strategist=None, cot=None,
                     candidates=None, regime=None, market_context=None, extra=None,
                     model_identity=None) -> Optional[dict]:
    """Append a light NO_TRADE row to the dataset: the model/flow declined to trade. Learns
    from passes, not just fills. Counterfactual outcome can be backfilled later. Never raises."""
    try:
        rec = {
            "schema": SCHEMA,
            "kind": "no_trade",
            "ts": _now(),
            "source": source,
            "reason": reason,                       # e.g. "market_closed" | "model_no_trade" | "empty_slate"
            "raw_strategist": _cap(raw_strategist, _RAW_CAP),  # clean answer
            "cot": _cap(cot, _RAW_CAP),             # [m3cot] chain-of-thought (reasoning_content); None if absent
            "candidates": _candidates(candidates),  # ideas the model floated but none chosen (if any)
            "regime": _as_dict(regime),
            "market_context": _cap(market_context, _CONTEXT_CAP),
            "model_identity": _as_dict(model_identity),
            "extra": _as_dict(extra),
        }
        _di.mark(rec, status=_di.CANONICAL, training=True, pnl=False,
                 reason="abstention has no realized P&L")
        _append(dataset_path(ddir), rec, dedup=True)
        return rec
    except Exception as e:
        try:
            print(f"[WARN] capture_no_trade failed: {e}")
        except Exception:
            pass
        return None


# --------------------------------------------------------------------------- capture: REJECTED
def capture_rejected(ddir: str, *, source: str, symbol: str, reason, stage,
                     idea=None, gate=None, construction=None, structure=None,
                     right=None, strike=None, expiry=None, order=None,
                     regime=None, extra=None, decision_id=None, revision: int = 0,
                     model_identity=None) -> Optional[dict]:
    """Append a light REJECTED row: an idea the model produced that a gate/constructor threw
    out (risk gate, construction gate, budget, gross-size, sector block, human decline). Records
    the reasoning + the rejected structure so the dataset learns which ideas get killed and why.
    `stage` = where it died ("risk_gate"|"construction"|"budget"|"gross"|"sector"|"approval"|...).
    Never raises."""
    try:
        rec = {
            "schema": SCHEMA,
            "kind": "rejected",
            "ts": _now(),
            "decision_id": decision_id,
            "revision": int(revision or 0),
            "trade_uid": trade_uid(con_id=None, symbol=symbol, strike=strike,
                                   expiry=expiry, right=right),
            "source": source,
            "stage": stage,
            "reason": reason,                       # str or list of reasons
            "symbol": symbol,
            "right": right,
            "strike": strike,
            "expiry": expiry,
            "structure": structure,
            "order": order,                         # human-readable resolved order, if any
            "idea": _as_dict(idea),
            "gate": _gate_dict(gate),
            "construction": _as_dict(construction),
            "regime": _as_dict(regime),
            "model_identity": _as_dict(model_identity),
            "extra": _as_dict(extra),
        }
        _di.mark(rec, status=_di.CANONICAL, training=True, pnl=False,
                 reason="rejected proposal has no realized P&L")
        _append(dataset_path(ddir), rec, dedup=True)
        return rec
    except Exception as e:
        try:
            print(f"[WARN] capture_rejected failed for {symbol}: {e}")
        except Exception:
            pass
        return None


# --------------------------------------------------------------------------- capture: UNFILLED
def capture_unfilled(ddir: str, *, source: str, symbol: str, con_id=None,
                     fill_status=None, close_qty=None, trigger_mark=None, bid=None,
                     limit_price=None, order_id=None, placed_at=None, reason=None,
                     rule_fired=None, spread=None, extra=None) -> Optional[dict]:
    """Append a lightweight NON-FILL close row for a TRIGGERED exit that did NOT fill -- a
    terminal reject/cancel at placement, or a resting order the manager abandoned unfilled.

    Emitted as a `kind:"trade"` row with ONLY a `close` block so fill_quality_report.py reads
    it exactly like any closed-trade row: `avg_fill_price=None` (=> filled=False) and a
    `fill_status` in its `_UNFILLED_STATUSES` set (submitted/presubmitted/cancelled/apicancelled/
    inactive/...) => it is counted in the UNFILLED denominator, so the too-tight fill-rate signal
    is no longer blind. No realized P&L / slippage is claimed (the exit did not realize).

    RECORD-ONLY + DEFENSIVE: swallows every exception. A logging failure can NEVER raise into or
    alter the exit path. Never emits fabricated fill data."""
    try:
        rec = {
            "schema": SCHEMA,
            "kind": "trade",                     # read by the fill-quality report like any close
            "unfilled": True,                    # explicit marker (report keys off close.* not this)
            "ts": _now(),
            # same durable join key as the entered decision/close for this contract
            "trade_uid": trade_uid(con_id=con_id, symbol=symbol),
            "source": source,
            "con_id": con_id,
            "symbol": symbol,
            # minimal entry stub so the report's symbol / is_spread lookups resolve
            "entry": {"symbol": symbol,
                      "spread": (spread if isinstance(spread, dict) else None)},
            "close": {
                "ts": _now(),
                "reason": reason,
                "rule_fired": rule_fired,            # the trigger_type that fired
                "fill_status": fill_status,          # terminal/resting status -> report "unfilled"
                "avg_fill_price": None,              # NEVER filled -> report filled=False
                "trigger_mark": trigger_mark,        # NET mark that valued/triggered the exit
                "bid": bid,                          # live bid it was priced against (if any)
                "limit_price": limit_price,          # price the SELL rested at (marketable limit)
                "close_qty": close_qty,
                "order_id": order_id,
                "placed_at": placed_at,              # dedupe key with con_id (see manager)
                "realized_pnl": None,                # no realization on a non-fill
                "realized_pnl_pct": None,
                "slippage_per_share": None,          # unmeasurable without a fill
                "slippage_pct": None,
                "partial": False,
            },
            "extra": _as_dict(extra),
        }
        _di.mark(rec, status=_di.CANONICAL, training=False, pnl=False,
                 reason="unfilled exit has no realized outcome")
        _append(dataset_path(ddir), rec)
        return rec
    except Exception as e:  # never raise into the exit path
        try:
            print(f"[WARN] capture_unfilled failed for {symbol}: {e} (continuing)")
        except Exception:
            pass
        return None


# --------------------------------------------------------------------------- read-back
def _iter_jsonl(path: str):
    try:
        if not os.path.exists(path):
            return
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except Exception:
                    continue
    except Exception:
        return


def _strike_eq(a, b) -> bool:
    try:
        return a is not None and b is not None and abs(float(a) - float(b)) < 1e-6
    except (TypeError, ValueError):
        return False


def load_decision_context(ddir: str, *, decision_id=None, con_id=None, symbol=None, strike=None,
                          expiry=None, right=None) -> Optional[dict]:
    """Return the latest revision for one immutable decision id.

    Contract/symbol fallback is intentionally forbidden: it can attach a later same-name proposal
    to an unrelated fill and manufacture a causal training example. Legacy rows without a
    decision_id remain honestly unjoined.
    """
    try:
        if not decision_id:
            return None
        path = decision_context_path(ddir)
        best = None
        best_revision = -1
        best_ts = ""
        for rec in _iter_jsonl(path):
            if rec.get("kind") != "decision" or rec.get("decision_id") != decision_id:
                continue
            try:
                revision = int(rec.get("revision") or 0)
            except (TypeError, ValueError):
                revision = 0
            ts = str(rec.get("ts") or "")
            if revision > best_revision or (revision == best_revision and ts >= best_ts):
                best, best_revision, best_ts = rec, revision, ts
        return best
    except Exception:
        return None


def load_review(ddir: str, *, decision_id=None, symbol=None, con_id=None, date=None) -> Optional[dict]:
    """Best-effort read-back of a post-trade review/coach artifact for this trade, if one was
    generated (morning_review or a coach path). Looks for a `reviews.jsonl` sidecar in the
    dataset dir whose rows carry {symbol|con_id|date, review/text/reasoning}. Returns the best
    match or None. Never raises -- a missing review simply omits the block."""
    try:
        if not decision_id:
            return None
        path = os.path.join(ddir or ".", "reviews.jsonl")
        best = None
        best_ts = ""
        for rec in _iter_jsonl(path):
            if rec.get("decision_id") != decision_id:
                continue
            ts = str(rec.get("ts") or "")
            if ts >= best_ts:
                best, best_ts = rec, ts
        return best
    except Exception:
        return None
