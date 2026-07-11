#!/usr/bin/env python3
"""Backfill historical CLOSED trades into the exitmgr training dataset from the raw trade logs
(2026-07-03).

Before the trade_dataset.v2 capture layer existed, the ONLY durable record of a closed trade was
`exits.log` (one JSON line per close: entry_debit, proceeds, realized_pnl, reason, entry_ts, ...)
plus the entry journal `trades.log` (debit, expiry, profit_target/stop, thesis, conviction). This
script reconstructs a `kind:"trade"` v2 row for every historical close whose cost basis is REAL and
recoverable, so those trades are not lost to the fine-tuning corpus.

INTEGRITY CONTRACT (this is training data for a live money model -- accuracy over volume):
  * NEVER fabricates a field. exits.log carries GROSS realized P&L (proceeds - entry_debit, from
    actual fills) but NO commissions, so `realized_pnl_net` is left NULL with commission_unknown=True
    and `pnl_is_estimate` semantics -- a commission-unadjusted number is never passed off as net.
  * A close with no recoverable basis (entry_debit is null OR realized_pnl is null -- e.g. a
    worthless-residual sweep that never had a real entry) is SKIPPED and logged, never emitted with
    an invented P&L.
  * Every emitted row is tagged `backfilled:true` + `backfill_source` + `backfill_ts` so it is
    always distinguishable from a live-captured row.
  * IDEMPOTENT: rows carry a stable trade_capture dedup key; re-running appends nothing new. The
    same worthless-residual close logged 5x collapses to ONE row.
  * The join key is the SAME durable trade_uid the live capture stamps, so backfilled decisions and
    outcomes pair exactly like live ones.

Usage:
  backfill_from_exits_log.py [--exits exits.log] [--trades trades.log]
                             [--dataset-dir data] [--out DATASET.jsonl]
                             [--dry-run] [--decision-dir DIR]

Default --out is the dataset dir's trade_dataset.jsonl (appended, deduped, backed up first).
"""
import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone

# reuse the app's durable join key + dedup + decision join (read-only)
_HAVE_TC = False
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from exitmgr import trade_capture as _tc  # noqa: E402
    from exitmgr import dataset_integrity as _di  # noqa: E402
    _HAVE_TC = True
except Exception:
    _tc = None
    _di = None


BACKFILL_SOURCE = "backfill:exits.log+trades.log"


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
    """contract_id -> list of ENTRY journal rows (have a real `debit`, not an event marker)."""
    by_cid = {}
    for _ln, row in _iter_jsonl(trades_path):
        if not isinstance(row, dict):
            continue
        if row.get("event"):            # e.g. closed_by_tool status markers -- not an entry
            continue
        if row.get("debit") is None:    # only real entries carry a debit
            continue
        cid = row.get("contract_id") or row.get("conId")
        if cid is None:
            continue
        by_cid.setdefault(int(cid), []).append(row)
    return by_cid


def _match_entry(entries, entry_ts):
    """Pick the entry journal row for a close: the one whose ts is the latest at or before the
    close's entry_ts; else the latest available. Returns {} if none."""
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
    """Reconstruct a trade_dataset.v2 `kind:"trade"` row from a real close event. Returns
    (row, None) on success or (None, skip_reason) when the basis is not recoverable."""
    entry_debit = _num(close_ev.get("entry_debit"))
    realized_pnl = _num(close_ev.get("realized_pnl"))
    proceeds = _num(close_ev.get("proceeds"))
    if entry_debit is None or realized_pnl is None or proceeds is None:
        return None, "no_recoverable_basis (entry_debit/realized_pnl/proceeds null)"

    con_id = close_ev.get("contract_id") or close_ev.get("conId")
    symbol = close_ev.get("symbol")
    right = close_ev.get("right")
    strike = _num(close_ev.get("strike"))
    entry_ts = close_ev.get("entry_ts") or entry_j.get("ts")
    expiry = entry_j.get("expiry")  # exits.log lacks expiry; recovered from the entry journal only
    reason = close_ev.get("reason")
    realized_pct = _num(close_ev.get("realized_pnl_pct"))
    entry_day = str(entry_ts)[:10] if entry_ts else None

    uid = _trade_uid(con_id, symbol, strike, expiry, right)
    # instance-level uid (adds entry day) disambiguates the same contract re-traded on a later date
    uid_inst = _trade_uid(con_id, symbol, strike, expiry, right, entry_day=entry_day)

    spread = close_ev.get("spread") or entry_j.get("spread")
    outcome = _outcome(realized_pnl)

    row = {
        "schema": "trade_dataset.v2",
        "kind": "trade",
        "ts": _now(),
        "trade_uid": uid,
        "trade_instance_uid": uid_inst,
        # ---- provenance: this row was reconstructed, not captured live ----
        "backfilled": True,
        "backfill_source": BACKFILL_SOURCE,
        "backfill_ts": _now(),
        "con_id": int(con_id) if con_id is not None else None,
        "symbol": symbol,
        "decision": decision,  # best-effort join from decision_context.jsonl; None if unavailable
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
        # NO mark path / greeks / MFE / MAE were logged pre-capture -> NULL, never fabricated.
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
            # GROSS realized P&L from actual fills (proceeds - entry_debit). REAL.
            "realized_pnl": realized_pnl,
            # commissions were NOT logged pre-capture -> the true NET basis is unknown. Never
            # fabricated: left null with the flags a consumer keys off to avoid treating gross as net.
            "realized_pnl_net": None,
            "entry_commission": None,
            "exit_commission": None,
            "commission_unknown": True,
            "realized_pnl_pct": realized_pct,
            "holding_days": _num(close_ev.get("holding_days")),
            "fill_status": "filled",           # proceeds realized => the exit filled
            "tp_hit": (reason in ("profit_target", "tp", "take_profit")),
            "sl_hit": (reason in ("stop", "sl", "stop_loss")),
            "partial": False,
            "basis_source": "backfill: gross realized from exits.log; net unknown (no commissions)",
        },
        "labels": {
            "outcome": outcome,
            "win": (outcome == "win") if outcome is not None else None,
            "round_trip": None,   # not derivable without the mark path
        },
        "review": None,
    }
    # DEDUP UNIT = the trade INSTANCE (contract + entry date). The reconciliation/double-close loop
    # logged the same worthless residual close many times (identical entry_ts + realized, differing
    # only in retry timestamp / holding_days); those collapse to ONE reconstructed trade rather than
    # N phantom -$2 losses. Falls back to the content hash only if no instance identity exists.
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

    # existing dedup keys in the target so re-runs are idempotent
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
        cid = ev.get("contract_id") or ev.get("conId")
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

    # -------- report --------
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
        # back up the target before mutating (integrity: never clobber the dataset silently)
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
