#!/usr/bin/env python3
"""Public API contract; production-derived narrative omitted."""
import argparse
import hashlib
import json
import os
import sys
import time


_HAVE_TC = False
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from exitmgr import trade_capture as _tc
    _HAVE_TC = True
except Exception:
    _tc = None
try:
    from exitmgr import dataset_integrity as _di
except Exception:
    _di = None


SYSTEM_PROMPT = (
    "You are the options strategist for a live swing-trading system. Given the market regime, "
    "the research brief, and the candidate ideas, decide whether to trade and, if so, which idea "
    "to take and how to construct it. If no idea clears the bar, abstain (NO_TRADE)."
)







_OUTCOME_KEYS = frozenset({
    "realized_pnl", "realizedpnl", "realized_pnl_net", "realized_pnl_pct", "realized_pct",
    "proceeds", "exit_price_per_share", "mark_estimate_pnl", "mark_estimate_pnl_pct",
    "mfe_pct", "mae_pct", "mfe_ts", "mae_ts", "drawdown_from_peak_pct", "peak_price",
    "outcome", "win", "round_trip", "tp_hit", "sl_hit", "holding_days",
    "exit_reason", "exit_reasoning", "close_ts", "rule_fired",
    "slippage_per_share", "slippage_pct", "fill_status", "avg_fill_price",
    "exit_commission", "exit_delta", "exit_gamma", "exit_theta", "exit_vega", "exit_iv",
    "dte_at_close", "expiry_value_unknown", "realized_unknown_reason", "exit_event",

    "close", "lifecycle", "labels", "label", "review",
})


def _scan_contamination(obj, path="input", _depth=0):
    """Public API contract; production-derived narrative omitted."""
    hits = []
    if _depth > 40:
        return hits
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() in _OUTCOME_KEYS:
                hits.append(f"{path}.{k}")
            hits.extend(_scan_contamination(v, f"{path}.{k}", _depth + 1))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            hits.extend(_scan_contamination(v, f"{path}[{i}]", _depth + 1))
    return hits


def _derive_trade_uid(rec, entry):
    """Public API contract; production-derived narrative omitted."""
    uid = rec.get("trade_uid")
    if uid:
        return uid
    d = rec.get("decision") or {}
    if d.get("trade_uid"):
        return d["trade_uid"]
    if not _HAVE_TC:
        return None
    e = entry or {}
    try:
        return _tc.trade_uid(
            con_id=rec.get("con_id"),
            symbol=e.get("symbol") or rec.get("symbol"),
            strike=e.get("strike") or rec.get("strike"),
            expiry=e.get("expiry") or rec.get("expiry"),
            right=e.get("right") or rec.get("right"),
        )
    except Exception:
        return None


_PNL_TOLERANCE_USD = 0.05


def _pnl_integrity(cl):
    """Public API contract; production-derived narrative omitted."""
    cl = cl or {}
    if cl.get("pnl_valid") is False or cl.get("pnl_quarantined") is True:
        return False, "capture marked P&L invalid/quarantined"
    net, ib = cl.get("realized_pnl_net"), cl.get("realized_pnl_ib")
    try:
        if net is not None and ib is not None and abs(float(net) - float(ib)) > _PNL_TOLERANCE_USD:
            return False, (f"computed net {float(net):.2f} disagrees with "
                           f"IB realized {float(ib):.2f}")
    except (TypeError, ValueError):
        return False, "non-numeric realized P&L"
    return True, None


def _pnl_from_close(cl):
    """Public API contract; production-derived narrative omitted."""
    cl = cl or {}
    valid, invalid_reason = _pnl_integrity(cl)
    net = cl.get("realized_pnl_net")
    ib = cl.get("realized_pnl_ib")
    gross = cl.get("realized_pnl")
    pct = cl.get("realized_pnl_pct")
    commission_unknown = bool(cl.get("commission_unknown"))
    if not valid:
        return None, gross, None, True, f"INVALID P&L: {invalid_reason}"
    if ib is not None:
        return ib, gross, pct, False, "IB realized P&L (authoritative)"
    if net is not None and not commission_unknown:
        return net, gross, pct, False, "fills+commissions (net)"
    if gross is not None:

        return None, gross, pct, True, "gross realized; commissions unknown -> net is an estimate"
    mark = cl.get("mark_estimate_pnl")
    if mark is not None:
        return None, None, cl.get("mark_estimate_pnl_pct"), True, "mark-derived (NON-FILL); not realized"
    return None, None, None, True, "no recoverable basis"



def _iter_jsonl(path):
    """Public API contract; production-derived narrative omitted."""
    if not path or not os.path.exists(path):
        return
    with open(path, "rb") as f:
        for i, raw in enumerate(f, 1):
            if not raw.strip():
                continue
            digest = _di.raw_jsonl_line_sha256(raw) if _di is not None else None
            try:
                row = json.loads(raw.decode("utf-8"))
                yield i, row if isinstance(row, dict) else None, digest
            except Exception:
                yield i, None, digest


def _default_dataset_dir():
    """Public API contract; production-derived narrative omitted."""
    here = os.path.dirname(os.path.abspath(__file__))
    cand = os.path.join(here, "data")
    return cand if os.path.isdir(cand) else here



def _resolve_decision(rec, ddir, entry):
    """Public API contract; production-derived narrative omitted."""
    d = rec.get("decision")
    decision_id = rec.get("decision_id") or (entry or {}).get("decision_id")
    if d and (not decision_id or d.get("decision_id") == decision_id):
        return d, "embedded"
    if _HAVE_TC and entry is not None:
        try:
            d = _tc.load_decision_context(
                ddir,
                decision_id=decision_id,
                con_id=rec.get("con_id"),
                symbol=entry.get("symbol") or rec.get("symbol"),
                strike=entry.get("strike"),
                expiry=entry.get("expiry"),
                right=entry.get("right"),
            )
            if d:
                return d, "rejoined"
        except Exception:
            pass
    return None, None


def _resolve_review(rec, ddir, entry):
    """Public API contract; production-derived narrative omitted."""
    rv = rec.get("review")
    decision_id = rec.get("decision_id") or (entry or {}).get("decision_id")
    if rv and (not decision_id or rv.get("decision_id") == decision_id):
        return rv, "embedded"
    if _HAVE_TC:
        try:
            ets = (entry or {}).get("ts")
            rv = _tc.load_review(
                ddir,
                decision_id=rec.get("decision_id") or (entry or {}).get("decision_id"),
                con_id=rec.get("con_id"),
                symbol=(entry or {}).get("symbol") or rec.get("symbol"),
                date=(str(ets)[:10] if ets else None),
            )
            if rv:
                return rv, "rejoined"
        except Exception:
            pass
    return None, None



def _decision_input(decision, entry, rec_regime=None):
    """Public API contract; production-derived narrative omitted."""
    d = decision or {}
    return {
        "regime": d.get("regime") if d else rec_regime,
        "market_context": d.get("market_context"),
        "technical_card": d.get("technical_card"),
        "candidates": d.get("candidates"),
        "raw_strategist": d.get("raw_strategist"),
        "chosen": d.get("chosen"),
        "gate": d.get("gate"),
        "construction": d.get("construction"),
        "sizing": d.get("sizing"),
        "source": d.get("source"),
    }


def _constructed_order(entry):
    """Public API contract; production-derived narrative omitted."""
    if not entry:
        return None
    return {
        "structure": entry.get("structure"),
        "symbol": entry.get("symbol"),
        "right": entry.get("right"),
        "strike": entry.get("strike"),
        "expiry": entry.get("expiry"),
        "spread": entry.get("spread"),
        "quantity": entry.get("quantity"),
        "debit": entry.get("debit"),
        "dte_at_entry": entry.get("dte_at_entry"),
        "profit_target_pct": entry.get("profit_target_pct"),
        "stop_pct": entry.get("stop_pct"),
        "conviction": entry.get("conviction"),
        "thesis": entry.get("thesis"),
    }


def _trade_label(rec):
    """Public API contract; production-derived narrative omitted."""
    lc = rec.get("lifecycle") or {}
    cl = rec.get("close") or {}
    lb = rec.get("labels") or {}
    pnl_net, pnl_gross, pnl_pct, pnl_is_estimate, basis_note = _pnl_from_close(cl)
    return {
        "realized_pnl_pct": pnl_pct,

        "realized_pnl_net": pnl_net,

        "realized_pnl": pnl_gross,


        "pnl_is_estimate": pnl_is_estimate,
        "pnl_basis": basis_note,
        "commission_unknown": bool(cl.get("commission_unknown")),
        "outcome": lb.get("outcome"),
        "win": lb.get("win"),
        "round_trip": lb.get("round_trip"),
        "mfe_pct": lc.get("mfe_pct"),
        "mae_pct": lc.get("mae_pct"),
        "drawdown_from_peak_pct": lc.get("drawdown_from_peak_pct"),
        "holding_days": cl.get("holding_days"),
        "exit_reason": cl.get("reason"),
        "rule_fired": cl.get("rule_fired"),
        "exit_reasoning": cl.get("exit_reasoning"),
        "tp_hit": cl.get("tp_hit"),
        "sl_hit": cl.get("sl_hit"),
        "fill_status": cl.get("fill_status"),
        "slippage_per_share": cl.get("slippage_per_share"),
        "slippage_pct": cl.get("slippage_pct"),
        "scaled_out": bool(cl.get("partial", False)),
        "close_qty": cl.get("close_qty"),
        "remaining_qty": cl.get("remaining_qty"),
    }


def _resolve_ts(rec):
    """Public API contract; production-derived narrative omitted."""
    if rec.get("ts"):
        return rec["ts"]
    e = rec.get("entry") or {}
    if e.get("ts"):
        return e["ts"]
    c = rec.get("close") or {}
    return c.get("ts") or ""


def build_trade_example(rec, ddir, fmt):
    entry = rec.get("entry") or {}
    decision, dsrc = _resolve_decision(rec, ddir, entry)
    review, rsrc = _resolve_review(rec, ddir, entry)
    inp = _decision_input(decision, entry)
    order = _constructed_order(entry)
    label = _trade_label(rec)
    completeness = {
        "example_kind": "trade",
        "has_decision": decision is not None,
        "decision_source": dsrc,
        "has_review": review is not None,
        "review_source": rsrc,
        "has_lifecycle": bool(rec.get("lifecycle")),
        "has_close": bool(rec.get("close")),
        "has_entry": bool(entry),
    }
    ts = _resolve_ts(rec)
    con_id = rec.get("con_id")
    symbol = rec.get("symbol") or entry.get("symbol")

    if fmt == "chat":
        user = {
            "regime": inp["regime"],
            "market_context": inp["market_context"],
            "technical_card": inp["technical_card"],
            "candidates": inp["candidates"],
        }
        assistant = {
            "decision": "TRADE",
            "chosen": inp["chosen"],
            "order": order,
            "gate": inp["gate"],
            "construction": inp["construction"],
            "reasoning": inp["raw_strategist"],
        }
        return {
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(user, default=str, sort_keys=True)},
                {"role": "assistant", "content": json.dumps(assistant, default=str, sort_keys=True)},
            ],
            "metadata": {
                "example_kind": "trade",
                "is_negative": False,
                "ts": ts,
                "con_id": con_id,
                "symbol": symbol,
                "label": label,
                "review": review,
                "completeness": completeness,
            },
        }


    return {
        "example_kind": "trade",
        "is_negative": False,
        "ts": ts,
        "con_id": con_id,
        "symbol": symbol,
        "input": inp,
        "order": order,
        "label": label,
        "review": review,
        "entry": entry,
        "lifecycle": rec.get("lifecycle"),
        "close": rec.get("close"),
        "completeness": completeness,
    }


def build_no_trade_example(rec, fmt):
    ts = rec.get("ts") or ""
    inp = {
        "regime": rec.get("regime"),
        "market_context": rec.get("market_context"),
        "candidates": rec.get("candidates"),
        "raw_strategist": rec.get("raw_strategist"),
        "source": rec.get("source"),
    }
    completeness = {
        "example_kind": "no_trade",
        "has_regime": rec.get("regime") is not None,
        "has_market_context": rec.get("market_context") is not None,
        "has_candidates": bool(rec.get("candidates")),
        "has_raw_strategist": rec.get("raw_strategist") is not None,
    }
    if fmt == "chat":
        user = {
            "regime": inp["regime"],
            "market_context": inp["market_context"],
            "candidates": inp["candidates"],
        }
        assistant = {
            "decision": "NO_TRADE",
            "reason": rec.get("reason"),
            "reasoning": rec.get("raw_strategist"),
        }
        return {
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(user, default=str, sort_keys=True)},
                {"role": "assistant", "content": json.dumps(assistant, default=str, sort_keys=True)},
            ],
            "metadata": {
                "example_kind": "no_trade",
                "is_negative": True,
                "negative_type": "abstain",
                "reason": rec.get("reason"),
                "ts": ts,
                "completeness": completeness,
            },
        }
    return {
        "example_kind": "no_trade",
        "is_negative": True,
        "negative_type": "abstain",
        "ts": ts,
        "symbol": None,
        "reason": rec.get("reason"),
        "input": inp,
        "label": {"decision": "NO_TRADE"},
        "completeness": completeness,
    }


def build_rejected_example(rec, fmt):
    ts = rec.get("ts") or ""
    idea = rec.get("idea")
    inp = {
        "regime": rec.get("regime"),
        "idea": idea,
        "gate": rec.get("gate"),
        "construction": rec.get("construction"),
        "source": rec.get("source"),
    }
    proposed_order = {
        "structure": rec.get("structure"),
        "symbol": rec.get("symbol"),
        "right": rec.get("right"),
        "strike": rec.get("strike"),
        "expiry": rec.get("expiry"),
        "order": rec.get("order"),
    }
    completeness = {
        "example_kind": "rejected",
        "has_regime": rec.get("regime") is not None,
        "has_idea": idea is not None,
        "has_gate": rec.get("gate") is not None,
        "has_order": rec.get("order") is not None,
    }
    if fmt == "chat":
        user = {"regime": inp["regime"], "idea": idea}
        assistant = {
            "decision": "REJECTED",
            "stage": rec.get("stage"),
            "reason": rec.get("reason"),
            "proposed_order": proposed_order,
        }
        return {
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(user, default=str, sort_keys=True)},
                {"role": "assistant", "content": json.dumps(assistant, default=str, sort_keys=True)},
            ],
            "metadata": {
                "example_kind": "rejected",
                "is_negative": True,
                "negative_type": "rejected",
                "stage": rec.get("stage"),
                "reason": rec.get("reason"),
                "ts": ts,
                "symbol": rec.get("symbol"),
                "completeness": completeness,
            },
        }
    return {
        "example_kind": "rejected",
        "is_negative": True,
        "negative_type": "rejected",
        "ts": ts,
        "symbol": rec.get("symbol"),
        "stage": rec.get("stage"),
        "reason": rec.get("reason"),
        "input": inp,
        "proposed_order": proposed_order,
        "label": {"decision": "REJECTED", "stage": rec.get("stage")},
        "completeness": completeness,
    }



def _input_side(ex, fmt):
    """Public API contract; production-derived narrative omitted."""
    if fmt == "chat":
        try:
            return json.loads((ex.get("messages") or [{}, {}])[1].get("content") or "{}")
        except Exception:
            return {}
    return {"input": ex.get("input"), "order": ex.get("order"),
            "proposed_order": ex.get("proposed_order")}


def _set_uid(ex, fmt, uid):
    if uid is None:
        return
    if fmt == "chat":
        ex.setdefault("metadata", {})["trade_uid"] = uid
    else:
        ex["trade_uid"] = uid


def _dedup_sig(ex, fmt):
    """Public API contract; production-derived narrative omitted."""
    meta = ex.get("metadata") or {}
    kind = ex.get("example_kind") or meta.get("example_kind")
    uid = ex.get("trade_uid") or meta.get("trade_uid") or ""
    ts = ex.get("ts") or meta.get("ts") or ""
    sym = ex.get("symbol") or meta.get("symbol") or ""
    disc = ex.get("stage") or meta.get("stage") or ex.get("reason") or meta.get("reason") or ""
    return (str(kind), str(uid), str(ts), str(sym), str(disc))


def export(dataset_dir, out_path, fmt, include_open, min_realized):
    if _di is None:
        raise RuntimeError("dataset integrity module unavailable; refusing training export")
    ds_path = os.path.join(dataset_dir, "trade_dataset.jsonl")
    with open(ds_path, "rb") as source_stream:
        source_dataset_sha256 = hashlib.sha256(source_stream.read()).hexdigest()
    source_rows = list(_iter_jsonl(ds_path))
    exclusions = _di.load_training_exclusions(ds_path, source_rows)
    counts = {
        "trade": 0, "no_trade": 0, "rejected": 0,
        "malformed": 0, "skipped_open": 0, "skipped_filter": 0, "emitted": 0,
        "wins": 0, "losses": 0, "scratch": 0,
        "missing_decision": 0, "missing_review": 0,
        "contaminated_dropped": 0, "dedup_dropped": 0, "pnl_estimate": 0,
        "invalid_pnl_dropped": 0,
        "noncanonical_dropped": 0,
        "training_excluded": 0,
    }
    examples = []
    ts_seen = []
    dropped_log = []

    excluded_source_hashes = []
    for _lineno, rec, _raw_sha in source_rows:
        if rec is None:
            counts["malformed"] += 1
            continue
        exclusion = exclusions["records_by_target_sha256"].get(_raw_sha)
        if exclusion is not None:
            counts["training_excluded"] += 1
            excluded_source_hashes.append(_raw_sha)
            dropped_log.append({
                "reason": "historical_training_exclusion",
                "source_line_sha256": _raw_sha,
                "source_dedup_key": rec.get("_dedup_key"),
                "exclusion_record_line_sha256": exclusions[
                    "record_line_sha256_by_target_sha256"][_raw_sha],
                "reason_code": exclusion["reason_code"],
                "ts": rec.get("ts"), "source": rec.get("source"),
                "kind": rec.get("kind"),
            })
            continue
        try:
            _canonical_ok, _canonical_reason = _di.allowed(rec, "training")
            if not _canonical_ok:
                counts["noncanonical_dropped"] += 1
                dropped_log.append({
                    "reason": "not_canonical_for_training", "detail": _canonical_reason,
                    "record_status": rec.get("record_status"), "ts": _resolve_ts(rec),
                    "symbol": rec.get("symbol"), "con_id": rec.get("con_id"),
                })
                continue
            kind = rec.get("kind")
            if kind == "trade":
                has_close = rec.get("close") is not None
                if not has_close and not include_open:
                    counts["skipped_open"] += 1
                    continue
                _pnl_ok, _pnl_reason = _pnl_integrity(rec.get("close") or {})
                if not _pnl_ok:
                    counts["invalid_pnl_dropped"] += 1
                    dropped_log.append({
                        "reason": "invalid_realized_pnl",
                        "detail": _pnl_reason,
                        "ts": _resolve_ts(rec),
                        "symbol": rec.get("symbol"),
                        "con_id": rec.get("con_id"),
                    })
                    continue
                if min_realized is not None:
                    rp = (rec.get("close") or {}).get("realized_pnl_pct")
                    if rp is None or rp < min_realized:
                        counts["skipped_filter"] += 1
                        continue
                ex = build_trade_example(rec, dataset_dir, fmt)
                counts["trade"] += 1
                if not (ex.get("completeness") or (ex.get("metadata") or {}).get("completeness") or {}).get("has_decision", True):
                    pass
                comp = ex.get("completeness") or (ex.get("metadata") or {}).get("completeness") or {}
                if not comp.get("has_decision"):
                    counts["missing_decision"] += 1
                if not comp.get("has_review"):
                    counts["missing_review"] += 1
                outcome = ((rec.get("labels") or {}).get("outcome"))
                if outcome == "win":
                    counts["wins"] += 1
                elif outcome == "loss":
                    counts["losses"] += 1
                elif outcome == "scratch":
                    counts["scratch"] += 1
            elif kind == "no_trade":
                ex = build_no_trade_example(rec, fmt)
                counts["no_trade"] += 1
            elif kind == "rejected":
                ex = build_rejected_example(rec, fmt)
                counts["rejected"] += 1
            else:
                counts["malformed"] += 1
                continue
        except Exception as e:

            counts["malformed"] += 1
            print(f"[WARN] row build failed (line {_lineno}): {e}", file=sys.stderr)
            continue


        hits = _scan_contamination(_input_side(ex, fmt))
        if hits:
            counts["contaminated_dropped"] += 1
            dropped_log.append({
                "reason": "outcome_field_in_input",
                "leaked_keys": hits,
                "example_kind": ex.get("example_kind") or (ex.get("metadata") or {}).get("example_kind"),
                "ts": _resolve_ts(rec),
                "symbol": rec.get("symbol"),
                "con_id": rec.get("con_id"),
            })
            print(f"[DROP] contamination in input ({rec.get('symbol')} line {_lineno}): {hits}",
                  file=sys.stderr)
            continue


        _set_uid(ex, fmt, _derive_trade_uid(rec, rec.get("entry") or {}))
        _target = ex.setdefault("metadata", {}) if fmt == "chat" else ex
        _target.update({"record_status": "CANONICAL", "canonical": True,
                        "usable_for_training": True,
                        "source_record_status": rec.get("record_status") or "UNMARKED_LEGACY_APP"})


        _lbl = ex.get("label") or (ex.get("metadata") or {}).get("label") or {}
        if (ex.get("example_kind") or (ex.get("metadata") or {}).get("example_kind")) == "trade" \
                and _lbl.get("pnl_is_estimate"):
            counts["pnl_estimate"] += 1

        ts = _resolve_ts(rec)
        if ts:
            ts_seen.append(ts)
        sort_key = (ts or "", str(rec.get("symbol") or ""), str(rec.get("con_id") or ""))
        examples.append((sort_key, ex))


    examples.sort(key=lambda x: x[0])


    seen_sig = set()
    deduped = []
    for sk, ex in examples:
        sig = _dedup_sig(ex, fmt)
        if sig in seen_sig:
            counts["dedup_dropped"] += 1
            continue
        seen_sig.add(sig)
        deduped.append((sk, ex))
    examples = deduped

    if os.path.exists(out_path):
        legacy_path = f"{out_path}.LEGACY_NOT_FOR_TRAINING.{time.time_ns()}"
        os.replace(out_path, legacy_path)
        with open(legacy_path + ".manifest.json", "w") as marker:
            json.dump({"record_status": "LEGACY", "canonical": False,
                       "usable_for_training": False, "usable_for_pnl": False,
                       "reason": "superseded by a canonical validated export"}, marker, sort_keys=True)
    with open(out_path, "w") as f:
        for _k, ex in examples:
            f.write(json.dumps(ex, default=str, sort_keys=True) + "\n")
            counts["emitted"] += 1
    with open(out_path, "rb") as exported:
        export_sha256 = hashlib.sha256(exported.read()).hexdigest()
    with open(out_path + ".manifest.json", "w") as manifest:
        json.dump({"schema": "trade-training-export.v2", "record_status": "CANONICAL",
                   "canonical": True, "usable_for_training": True, "usable_for_pnl": False,
                   "source_dataset": os.path.abspath(ds_path), "format": fmt,
                   "source_dataset_sha256": source_dataset_sha256,
                   "training_exclusion_ledger": os.path.abspath(exclusions["path"]),
                   "training_exclusion_ledger_sha256": exclusions["ledger_sha256"],
                   "training_exclusion_records": len(exclusions["records_by_target_sha256"]),
                   "training_excluded_records": counts["training_excluded"],
                   "training_excluded_source_line_sha256": sorted(excluded_source_hashes),
                   "sha256": export_sha256, "records": len(examples)}, manifest, sort_keys=True)


    if dropped_log:
        try:
            with open(out_path + ".dropped.jsonl", "w") as f:
                for d in dropped_log:
                    f.write(json.dumps(d, default=str, sort_keys=True) + "\n")
        except Exception as _de:
            print(f"[WARN] could not write dropped log: {_de}", file=sys.stderr)

    decided = counts["wins"] + counts["losses"]
    win_rate = (counts["wins"] / decided) if decided else None
    date_lo = min(ts_seen)[:10] if ts_seen else None
    date_hi = max(ts_seen)[:10] if ts_seen else None

    print("=== export_training_data summary ===", file=sys.stderr)
    print(f"format={fmt}  out={out_path}", file=sys.stderr)
    print(f"dataset={ds_path}  join_module={'exitmgr.trade_capture' if _HAVE_TC else 'UNAVAILABLE(embedded-only)'}",
          file=sys.stderr)
    print(f"trades={counts['trade']}  no_trade={counts['no_trade']}  rejected={counts['rejected']}",
          file=sys.stderr)
    print(f"emitted={counts['emitted']}  malformed_skipped={counts['malformed']}  "
          f"skipped_open={counts['skipped_open']}  skipped_min_realized={counts['skipped_filter']}",
          file=sys.stderr)
    print(f"trade_wins={counts['wins']}  losses={counts['losses']}  scratch={counts['scratch']}  "
          f"win_rate={'n/a' if win_rate is None else round(win_rate, 4)}", file=sys.stderr)
    print(f"trades_missing_decision_join={counts['missing_decision']}  "
          f"trades_missing_review_join={counts['missing_review']}", file=sys.stderr)
    print(f"contamination_dropped={counts['contaminated_dropped']}  "
          f"training_excluded={counts['training_excluded']}  "
          f"dedup_dropped={counts['dedup_dropped']}  "
          f"trades_pnl_is_estimate={counts['pnl_estimate']}", file=sys.stderr)
    if dropped_log:
        print(f"  -> dropped examples logged to {out_path}.dropped.jsonl", file=sys.stderr)
    print(f"date_range={date_lo}..{date_hi}", file=sys.stderr)
    return counts


def main(argv=None):
    ap = argparse.ArgumentParser(description="Export exitmgr trade dataset to training-ready JSONL.")
    ap.add_argument("--out", required=True, help="output JSONL path")
    ap.add_argument("--format", choices=["jsonl-flat", "chat"], default="jsonl-flat",
                    help="jsonl-flat = one flat joined record per example; chat = SFT messages shape")
    ap.add_argument("--dataset-dir", default=None,
                    help="dir holding trade_dataset.jsonl + decision_context.jsonl + reviews.jsonl")
    ap.add_argument("--include-open", action="store_true",
                    help="also emit trade rows that have no close block (default off)")
    ap.add_argument("--min-realized", type=float, default=None,
                    help="only emit trades with realized_pnl_pct >= this value")
    args = ap.parse_args(argv)

    ddir = args.dataset_dir or _default_dataset_dir()
    export(ddir, args.out, args.format, args.include_open, args.min_realized)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
