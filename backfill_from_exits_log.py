#!/usr/bin/env python3
"""Public API contract; production-derived narrative omitted."""
import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone


_HAVE_TC = False
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from exitmgr import trade_capture as _tc
    from exitmgr import dataset_integrity as _di
    _HAVE_TC = True
except Exception:
    _tc = None
    _di = None


BACKFILL_SOURCE = "backfill:exits.log+trades.log"



def _first_present(mapping, *keys):
    """Public API contract; production-derived narrative omitted."""
    for k in keys:
        v = mapping.get(k)
        if v is not None:
            return v
    return None


def _iter_jsonl(path):
    if not path or not os.path.exists(path):
        return
    with open(path) as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield i, json.loads(line)
            except Exception:
                yield i, None


def _now():
    return datetime.now(timezone.utc).isoformat()


def _num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _trade_uid(con_id, symbol, strike, expiry, right, entry_day=None):
    if _HAVE_TC:
        return _tc.trade_uid(con_id=con_id, symbol=symbol, strike=strike,
                             expiry=expiry, right=right, entry_day=entry_day)
    return None


def _dedup_key(rec):
    if _HAVE_TC:
        return _tc._dedup_key(rec)
    return None


def _load_entry_journal(trades_path):
    """Public API contract; production-derived narrative omitted."""
    by_cid = {}
    for _ln, row in _iter_jsonl(trades_path):
        if not isinstance(row, dict):
            continue
        if row.get("event"):
            continue
        if row.get("debit") is None:
            continue
        cid = _first_present(row, "contract_id", "conId")
        if cid is None:
            continue
        by_cid.setdefault(int(cid), []).append(row)
    return by_cid


def _match_entry(entries, entry_ts):
    """Public API contract; production-derived narrative omitted."""
    if not entries:
        return {}
    if entry_ts:
        le = [e for e in entries if str(e.get("ts") or "") <= str(entry_ts)]
        if le:
            return max(le, key=lambda e: str(e.get("ts") or ""))
    return max(entries, key=lambda e: str(e.get("ts") or ""))


def _outcome(realized_pnl):
    if realized_pnl is None:
        return None
    if realized_pnl > 0.0:
        return "win"
    if realized_pnl < 0.0:
        return "loss"
    return "scratch"


def build_backfill_row(close_ev, entry_j, decision):
    """Public API contract; production-derived narrative omitted."""
    entry_debit = _num(close_ev.get("entry_debit"))
    realized_pnl = _num(close_ev.get("realized_pnl"))
    proceeds = _num(close_ev.get("proceeds"))
    if entry_debit is None or realized_pnl is None or proceeds is None:
        return None, "no_recoverable_basis (entry_debit/realized_pnl/proceeds null)"

    con_id = _first_present(close_ev, "contract_id", "conId")
    symbol = close_ev.get("symbol")
    right = close_ev.get("right")
    strike = _num(close_ev.get("strike"))
    entry_ts = close_ev.get("entry_ts") or entry_j.get("ts")
    expiry = entry_j.get("expiry")
    reason = close_ev.get("reason")
    realized_pct = _num(close_ev.get("realized_pnl_pct"))
    entry_day = str(entry_ts)[:10] if entry_ts else None

    uid = _trade_uid(con_id, symbol, strike, expiry, right)

    uid_inst = _trade_uid(con_id, symbol, strike, expiry, right, entry_day=entry_day)

    spread = close_ev.get("spread") or entry_j.get("spread")
    outcome = _outcome(realized_pnl)

    row = {
        "schema": "trade_dataset.v2",
        "kind": "trade",
        "ts": _now(),
        "trade_uid": uid,
        "trade_instance_uid": uid_inst,

        "backfilled": True,
        "backfill_source": BACKFILL_SOURCE,
        "backfill_ts": _now(),
        "con_id": int(con_id) if con_id is not None else None,
        "symbol": symbol,
        "decision": decision,
        "entry": {
            "ts": entry_ts,
            "symbol": symbol,
            "right": right,
            "strike": strike,
            "expiry": expiry,
            "structure": close_ev.get("structure") or entry_j.get("structure"),
            "spread": ({"short_con_id": spread.get("short_con_id"),
                        "short_strike": spread.get("short_strike"),
                        "width": spread.get("width")} if isinstance(spread, dict) else None),
            "quantity": close_ev.get("quantity") if close_ev.get("quantity") is not None
                        else entry_j.get("quantity"),
            "debit": entry_debit,
            "profit_target_pct": entry_j.get("profit_target_pct"),
            "stop_pct": entry_j.get("stop_pct"),
            "conviction": close_ev.get("conviction") if close_ev.get("conviction") is not None
                          else entry_j.get("conviction"),
            "thesis": entry_j.get("thesis"),
            "basis_source": "exits.log entry_debit (real fill debit)",
        },

        "lifecycle": {
            "mark_path": [],
            "marks": 0,
            "mfe_pct": None,
            "mae_pct": None,
            "drawdown_from_peak_pct": None,
        },
        "close": {
            "ts": close_ev.get("close_ts") or close_ev.get("ts"),
            "reason": reason,
            "rule_fired": reason,
            "exit_price_per_share": _num(close_ev.get("exit_price_per_share")),
            "proceeds": proceeds,

            "realized_pnl": realized_pnl,


            "realized_pnl_net": None,
            "entry_commission": None,
            "exit_commission": None,
            "commission_unknown": True,
            "realized_pnl_pct": realized_pct,
            "holding_days": _num(close_ev.get("holding_days")),
            "fill_status": "filled",
            "tp_hit": (reason in ("profit_target", "tp", "take_profit")),
            "sl_hit": (reason in ("stop", "sl", "stop_loss")),
            "partial": False,
            "basis_source": "backfill: gross realized from exits.log; net unknown (no commissions)",
        },
        "labels": {
            "outcome": outcome,
            "win": (outcome == "win") if outcome is not None else None,
            "round_trip": None,
        },
        "review": None,
    }




    row["_dedup_key"] = f"trade_instance:{uid_inst}" if uid_inst else (_dedup_key(row) or "")
    if _di is not None:
        _di.mark(
            row, status=_di.ESTIMATE, training=False, pnl=False,
            reason="legacy exits.log reconstruction lacks authoritative commissions/decision lineage")
    else:
        row.update({"record_status": "ESTIMATE", "canonical": False,
                    "usable_for_training": False, "usable_for_pnl": False,
                    "not_for_training_reason": "legacy estimate",
                    "not_for_pnl_reason": "legacy estimate"})
    return row, None


def backfill(exits_path, trades_path, out_path, decision_dir, dry_run):
    stats = {"close_events": 0, "malformed": 0, "reconstructed": 0,
             "skipped_no_basis": 0, "deduped_in_batch": 0, "already_present": 0, "appended": 0}
    entries = _load_entry_journal(trades_path)
    skipped = []


    existing = set()
    if _HAVE_TC:
        try:
            existing = _tc._existing_dedup_keys(out_path)
        except Exception:
            existing = set()

    batch_seen = set()
    new_rows = []
    for lineno, ev in _iter_jsonl(exits_path):
        if ev is None:
            stats["malformed"] += 1
            continue
        stats["close_events"] += 1
        cid = _first_present(ev, "contract_id", "conId")
        entry_j = _match_entry(entries.get(int(cid), []) if cid is not None else [],
                               ev.get("entry_ts"))
        decision = None
        if _HAVE_TC and decision_dir:
            try:
                strike = _num(ev.get("strike"))
                decision = _tc.load_decision_context(
                    decision_dir, con_id=cid, symbol=ev.get("symbol"),
                    strike=strike, expiry=entry_j.get("expiry"), right=ev.get("right"))
            except Exception:
                decision = None
        row, skip = build_backfill_row(ev, entry_j, decision)
        if skip:
            stats["skipped_no_basis"] += 1
            skipped.append({"line": lineno, "symbol": ev.get("symbol"),
                            "con_id": cid, "reason": skip})
            continue
        stats["reconstructed"] += 1
        key = row.get("_dedup_key")
        if key and key in batch_seen:
            stats["deduped_in_batch"] += 1
            continue
        if key:
            batch_seen.add(key)
        if key and key in existing:
            stats["already_present"] += 1
            continue
        new_rows.append(row)


    print("=== backfill_from_exits_log summary ===", file=sys.stderr)
    print(f"exits={exits_path}  trades={trades_path}  out={out_path}", file=sys.stderr)
    print(f"join_module={'exitmgr.trade_capture' if _HAVE_TC else 'UNAVAILABLE'}", file=sys.stderr)
    print(f"close_events={stats['close_events']}  malformed={stats['malformed']}", file=sys.stderr)
    print(f"reconstructed={stats['reconstructed']}  skipped_no_basis={stats['skipped_no_basis']}  "
          f"deduped_in_batch={stats['deduped_in_batch']}  already_present={stats['already_present']}",
          file=sys.stderr)
    print(f"NEW rows to append={len(new_rows)}", file=sys.stderr)
    for s in skipped:
        print(f"  [SKIP no-basis] line {s['line']} {s['symbol']} con_id={s['con_id']}: {s['reason']}",
              file=sys.stderr)
    for r in new_rows:
        print(f"  [NEW] {r['symbol']} con_id={r['con_id']} pnl={r['close']['realized_pnl']} "
              f"pct={r['close']['realized_pnl_pct']} outcome={r['labels']['outcome']} uid={r['trade_uid']}",
              file=sys.stderr)

    if dry_run:
        print("DRY-RUN: nothing written.", file=sys.stderr)
        return stats, new_rows

    if new_rows:

        if os.path.exists(out_path):
            bak = out_path + ".bak-backfill-" + datetime.now().strftime("%Y%m%d-%H%M%S")
            shutil.copy2(out_path, bak)
            print(f"backed up {out_path} -> {bak}", file=sys.stderr)
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "a") as f:
            for r in new_rows:
                f.write(json.dumps(r, default=str) + "\n")
                stats["appended"] += 1
        print(f"appended {stats['appended']} backfilled trade rows to {out_path}", file=sys.stderr)
    else:
        print("nothing new to append (idempotent no-op).", file=sys.stderr)
    return stats, new_rows


def main(argv=None):
    ap = argparse.ArgumentParser(description="Backfill closed trades into the exitmgr dataset.")
    ap.add_argument("--exits", default="exits.log")
    ap.add_argument("--trades", default="trades.log")
    ap.add_argument("--dataset-dir", default="data")
    ap.add_argument("--out", default=None, help="target JSONL (default: <dataset-dir>/trade_dataset.jsonl)")
    ap.add_argument("--decision-dir", default=None,
                    help="dir with decision_context.jsonl to join (default: --dataset-dir)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    out_path = args.out or os.path.join(args.dataset_dir, "trade_dataset.jsonl")
    decision_dir = args.decision_dir or args.dataset_dir
    backfill(args.exits, args.trades, out_path, decision_dir, args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
