"""Public API contract; production-derived narrative omitted."""
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Optional, List

from exitmgr import dataset_integrity as _di

SCHEMA = "trade_dataset.v2"
DECISION_SCHEMA = "decision_context.v2"
















_TRADE_UID_NAMESPACE = uuid.UUID("6f2a1c94-0b3e-5d7a-9c11-7e2f4a8b6d30")


def _norm(v) -> str:
    return "" if v is None else str(v).strip()


def trade_uid(*, con_id=None, symbol=None, strike=None, expiry=None, right=None,
              entry_day=None) -> Optional[str]:
    """Public API contract; production-derived narrative omitted."""
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




_CONTEXT_CAP = 24000



_RAW_CAP = 64000



def dataset_dir(journal_path: Optional[str], cfg_dataset_path: Optional[str] = None) -> str:
    """Public API contract; production-derived narrative omitted."""
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
    """Public API contract; production-derived narrative omitted."""
    try:
        r = {k: v for k, v in rec.items() if k != "_dedup_key"}
        decision_id = r.get("decision_id")
        if decision_id:


            blob = json.dumps(r, sort_keys=True, default=str)
            return (f"{rec.get('kind', '')}:{decision_id}:{r.get('revision', 0)}:"
                    f"{r.get('event', '')}:{hashlib.sha256(blob.encode()).hexdigest()}")
        ts = r.get("ts")
        if isinstance(ts, str) and len(ts) >= 10:
            r["ts"] = ts[:10]
        blob = json.dumps(r, sort_keys=True, default=str)
        return f"{rec.get('kind', '')}:{hashlib.sha1(blob.encode()).hexdigest()}"
    except Exception:
        return None


def _existing_dedup_keys(path: str) -> set:
    """Public API contract; production-derived narrative omitted."""
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
    """Public API contract; production-derived narrative omitted."""
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if dedup:
            key = _dedup_key(rec)
            if key is not None:
                rec = {**rec, "_dedup_key": key}
                if key in _existing_dedup_keys(path):
                    return False
        with open(path, "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
        return True
    except Exception as e:
        try:
            print(f"[WARN] trade_capture append failed ({path}): {e}")
        except Exception:
            pass
        return False


def dedupe_file(path: str) -> dict:
    """Public API contract; production-derived narrative omitted."""
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
                    kept.append(line)
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



def _as_dict(obj):
    """Public API contract; production-derived narrative omitted."""
    if obj is None:
        return None


    if isinstance(obj, (str, bool, int, float)):
        return obj
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




        if not isinstance(obj, dict):
            _model_dte = getattr(obj, "model_target_dte", None)
            if _model_dte is not None:
                d = dict(d)
                d["model_target_dte"] = _model_dte
                d["model_target_delta"] = getattr(obj, "model_target_delta", None)
                d["construction_overridden"] = True


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
    """Public API contract; production-derived narrative omitted."""
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



def _identity_source(identity, declared=None):
    """Public API contract; production-derived narrative omitted."""
    try:
        from exitmgr.event_capture import identity_source as _fn
        return _fn(identity, declared)
    except Exception:
        if declared:
            return str(declared)
        return None if identity is None else "unknown"



def capture_decision(ddir: str, *, source: str, symbol: str,
                     right=None, strike=None, expiry=None, structure=None, con_id=None,
                     chosen_idea=None, candidates=None, raw_strategist=None, cot=None, gate=None,
                     construction=None, technical_card=None, regime=None,
                     market_context=None, sizing=None, extra=None, decision_id=None,
                     revision: int = 0, event: str = "proposal", model_identity=None,
                     model_identity_source=None,
                     final_contract=None, order_ref=None, human_action=None,
                     execution_authority=None) -> Optional[dict]:
    """Public API contract; production-derived narrative omitted."""
    try:
        rec = {
            "schema": DECISION_SCHEMA,
            "kind": "decision",
            "ts": _now(),
            "decision_id": decision_id,
            "revision": int(revision or 0),
            "event": event,


            "trade_uid": trade_uid(con_id=con_id, symbol=symbol, strike=strike,
                                   expiry=expiry, right=right),
            "source": source,
            "symbol": symbol,
            "right": right,
            "strike": strike,
            "expiry": expiry,
            "structure": structure,
            "con_id": con_id,
            "chosen": _as_dict(chosen_idea),
            "candidates": _candidates(candidates),
            "raw_strategist": _cap(raw_strategist, _RAW_CAP),
            "cot": _cap(cot, _RAW_CAP),
            "gate": _gate_dict(gate),
            "construction": _as_dict(construction),
            "technical_card": _cap(technical_card, 8000),
            "regime": _as_dict(regime),
            "market_context": _cap(market_context, _CONTEXT_CAP),
            "sizing": _as_dict(sizing),
            "model_identity": _as_dict(model_identity),





            "model_identity_source": _identity_source(model_identity,
                                                      model_identity_source),
            "final_contract": _as_dict(final_contract),
            "order_ref": order_ref,
            "execution_authority": execution_authority,
            "human_action": _as_dict(human_action),
            "extra": _as_dict(extra),
        }
        _append(decision_context_path(ddir), rec, dedup=True)
        try:
            from exitmgr import event_capture as _evt
            _evt.on_decision(rec)
        except Exception:
            pass
        return rec
    except Exception as e:
        try:
            print(f"[WARN] capture_decision failed for {symbol}: {e}")
        except Exception:
            pass
        return None



def capture_no_trade(ddir: str, *, source: str, reason=None, raw_strategist=None, cot=None,
                     candidates=None, regime=None, market_context=None, extra=None,
                     model_identity=None, model_identity_source=None,
                     training_eligible: bool = True) -> Optional[dict]:
    """Public API contract; production-derived narrative omitted."""
    try:
        rec = {
            "schema": SCHEMA,
            "kind": "no_trade",
            "ts": _now(),
            "source": source,
            "reason": reason,
            "raw_strategist": _cap(raw_strategist, _RAW_CAP),
            "cot": _cap(cot, _RAW_CAP),
            "candidates": _candidates(candidates),
            "regime": _as_dict(regime),
            "market_context": _cap(market_context, _CONTEXT_CAP),
            "model_identity": _as_dict(model_identity),





            "model_identity_source": _identity_source(model_identity,
                                                      model_identity_source),
            "extra": _as_dict(extra),
        }
        _di.mark(rec, status=_di.CANONICAL, training=bool(training_eligible), pnl=False,
                 reason=("abstention has no realized P&L" if training_eligible else
                         "pipeline/construction outcome is not model abstention"))
        _append(dataset_path(ddir), rec, dedup=True)
        try:
            from exitmgr import event_capture as _evt
            _evt.on_no_trade(rec)
        except Exception:
            pass
        return rec
    except Exception as e:
        try:
            print(f"[WARN] capture_no_trade failed: {e}")
        except Exception:
            pass
        return None



def capture_rejected(ddir: str, *, source: str, symbol: str, reason, stage,
                     idea=None, gate=None, construction=None, structure=None,
                     right=None, strike=None, expiry=None, order=None,
                     regime=None, extra=None, decision_id=None, revision: int = 0,
                     model_identity=None, model_identity_source=None) -> Optional[dict]:
    """Public API contract; production-derived narrative omitted."""
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
            "reason": reason,
            "symbol": symbol,
            "right": right,
            "strike": strike,
            "expiry": expiry,
            "structure": structure,
            "order": order,
            "idea": _as_dict(idea),
            "gate": _gate_dict(gate),
            "construction": _as_dict(construction),
            "regime": _as_dict(regime),
            "model_identity": _as_dict(model_identity),





            "model_identity_source": _identity_source(model_identity,
                                                      model_identity_source),
            "extra": _as_dict(extra),
        }
        _di.mark(rec, status=_di.CANONICAL, training=True, pnl=False,
                 reason="rejected proposal has no realized P&L")
        _append(dataset_path(ddir), rec, dedup=True)
        try:
            from exitmgr import event_capture as _evt
            _evt.on_rejected(rec)
        except Exception:
            pass
        return rec
    except Exception as e:
        try:
            print(f"[WARN] capture_rejected failed for {symbol}: {e}")
        except Exception:
            pass
        return None



def capture_unfilled(ddir: str, *, source: str, symbol: str, con_id=None,
                     fill_status=None, close_qty=None, trigger_mark=None, bid=None,
                     limit_price=None, order_id=None, placed_at=None, reason=None,
                     rule_fired=None, spread=None, extra=None) -> Optional[dict]:
    """Public API contract; production-derived narrative omitted."""
    try:
        rec = {
            "schema": SCHEMA,
            "kind": "trade",
            "unfilled": True,
            "ts": _now(),

            "trade_uid": trade_uid(con_id=con_id, symbol=symbol),
            "source": source,
            "con_id": con_id,
            "symbol": symbol,

            "entry": {"symbol": symbol,
                      "spread": (spread if isinstance(spread, dict) else None)},
            "close": {
                "ts": _now(),
                "reason": reason,
                "rule_fired": rule_fired,
                "fill_status": fill_status,
                "avg_fill_price": None,
                "trigger_mark": trigger_mark,
                "bid": bid,
                "limit_price": limit_price,
                "close_qty": close_qty,
                "order_id": order_id,
                "placed_at": placed_at,
                "realized_pnl": None,
                "realized_pnl_pct": None,
                "slippage_per_share": None,
                "slippage_pct": None,
                "partial": False,
            },
            "extra": _as_dict(extra),
        }
        _di.mark(rec, status=_di.CANONICAL, training=False, pnl=False,
                 reason="unfilled exit has no realized outcome")
        _append(dataset_path(ddir), rec)
        try:
            from exitmgr import event_capture as _evt
            _evt.on_unfilled(rec)
        except Exception:
            pass
        return rec
    except Exception as e:
        try:
            print(f"[WARN] capture_unfilled failed for {symbol}: {e} (continuing)")
        except Exception:
            pass
        return None



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
    """Public API contract; production-derived narrative omitted."""
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
    """Public API contract; production-derived narrative omitted."""
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
