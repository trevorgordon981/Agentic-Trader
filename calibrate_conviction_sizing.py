#!/usr/bin/env python3
"""calibrate_conviction_sizing.py  --  fit an EMPIRICAL, GATED conviction->size multiplier
from REAL closed-trade outcomes.

THE QUESTION (distinct from gordon-gauntlet/calibration_gate.py, which asks whether the
MODEL's stated conviction tracks CORRECTNESS on the held-out eval): on our own LIVE exitmgr
trades, does higher conviction earn real P&L -- and if so, by how much should it earn more
SIZE?  This tool fits a per-conviction-bucket size MULTIPLIER (>1 up-size, <1 down-size,
1.0 flat) from realized outcomes, GATED so thin data can never up-size real money.

  * Higher conviction earns MORE size ONLY if its realized edge (mean return, positive, and
    beating the book, win-rate >= 50%) justifies it.  A bucket with no edge gets NO premium
    (1.0, flat).  A bucket that LOSES gets DOWN-sized (<1.0).
  * Every up-multiplier is bounded so that base_soft_pct * mult <= the hard per-trade ceiling
    -- the proposal can NEVER exceed the current hard cap (risk.py also clamps at the hard
    ceiling as a backstop).
  * GATED through gordon-gauntlet/calibration_gate.py (reused when importable; a byte-equivalent
    vendored fallback otherwise -- see _resolve_gate()).  Below-threshold buckets fall back to
    the current FLAT size (reported "insufficient data -- flat").  On thin/empty data the honest
    result is flat everywhere -- NEVER an invented edge.

INPUT  (read-only; no model, no IBKR, no orders)
  data/trade_dataset.jsonl -- the v2 closed-trade dataset written by ExitManager._log_trade_dataset.
  Each kind=="trade" row already carries, from the SAME join trade_capture does at close:
      entry.conviction        <- persisted entry conviction (recovered via _recover_conviction);
                                 falls back to decision.chosen.conviction (the joined decision ctx)
      close.realized_pnl_pct   <- realized return, % of debit
      lifecycle.mfe_pct / mae_pct
      labels.win / outcome
  We do NOT invent a join key -- we read the fields the dataset already joined.

OUTPUT  (PROPOSE-ONLY by default; Trevor reviews before it sizes real money)
  * prints the per-bucket table + the overall gate verdict
  * --out PATH  writes a proposed calibration JSON blob (default under data/)
  * --write-config (default OFF) would stage the mapping into config.yaml trading:
    conviction_size_multipliers.  DEFAULT is propose-only.  Real-money application ALSO needs
    the one-line wiring in run_trader.py (mirror conviction_size_curve) -- see that file.

USAGE
  python3 calibrate_conviction_sizing.py                       # live dataset, propose-only
  python3 calibrate_conviction_sizing.py --dataset data/trade_dataset.jsonl --out /tmp/prop.json
  python3 calibrate_conviction_sizing.py --selftest            # prove the mechanism on synthetic data

100% stdlib (+ optional PyYAML only for --write-config).  Read-only unless --write-config.
"""
import argparse
import json
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone

# ---- derivation tunables (conservative -- this informs REAL-money sizing) ------------------
MIN_MULT = 0.25          # down-size floor: never shrink a bucket below 25% of flat
UP_REF = 100.0           # a +100pp relative-edge bucket would earn +1.00 to its multiplier
DOWN_REF = 100.0         # a -100pp mean-return bucket would fall to the MIN_MULT floor
WIN_FLOOR_FOR_UPSIZE = 0.5   # a bucket needs >=50% win-rate to be up-sized (guards a lone
                             # fat winner dragging a losing bucket's mean positive)
SCRATCH_BAND = 1.0       # +/-1% of debit == scratch (mirrors manager._log_trade_dataset labels)


# --------------------------------------------------------------------------- conviction buckets
def bucket(conv):
    """The system's sizing buckets (identical to gordon-gauntlet/conviction_calibration.py).
    None/<0/unparseable -> 'unknown' (conviction not recoverable => not sizeable by conviction)."""
    if conv is None:
        return "unknown"
    try:
        c = float(conv)
    except (TypeError, ValueError):
        return "unknown"
    if c < 0:
        return "unknown"
    if c >= 8:
        return "high (8-10)"
    if c >= 5:
        return "mid (5-7)"
    return "low (1-4)"


# conviction levels 1..10 that each bucket owns (drives expansion to a per-level config map)
BUCKET_LEVELS = {
    "high (8-10)": [8, 9, 10],
    "mid (5-7)": [5, 6, 7],
    "low (1-4)": [1, 2, 3, 4],
}
BUCKET_ORDER = ["high (8-10)", "mid (5-7)", "low (1-4)", "unknown"]


# --------------------------------------------------------------------------- calibration gate reuse
def _resolve_gate():
    """Reuse gordon-gauntlet/calibration_gate.py when importable (its reliability()/gate() +
    MIN_TOTAL/MIN_PER_GROUP/MIN_LIFT/LOW_HI/HI_LO thresholds); otherwise a byte-equivalent vendored
    fallback so this harness (and its tests) run standalone.  Returns (module_or_shim, source_str).
    """
    try:
        import calibration_gate as cg  # already on path?
        return cg, "gordon-gauntlet/calibration_gate.py (imported)"
    except Exception:
        pass
    gg = os.path.expanduser("~/gordon-gauntlet")
    if os.path.isdir(gg) and gg not in sys.path:
        sys.path.insert(0, gg)
    try:
        import calibration_gate as cg
        return cg, "gordon-gauntlet/calibration_gate.py (imported via ~/gordon-gauntlet)"
    except Exception:
        return _VendoredGate(), "vendored fallback (calibration_gate not importable)"


class _VendoredGate:
    """Byte-equivalent copy of calibration_gate.py's sufficiency/discrimination gate (thresholds
    and math), used only when the real module can't be imported.  Same verdicts on the same data."""
    MIN_TOTAL = 30
    MIN_PER_GROUP = 8
    MIN_LIFT = 0.10
    MAX_ECE = 0.15
    LOW_HI, HI_LO = 4, 8

    def reliability(self, pairs, conf_scale=10.0):
        n = len(pairs)
        byval = defaultdict(list)
        for c, ok in pairs:
            byval[c].append(ok)
        per_value, ece, mce = {}, 0.0, 0.0
        for c in sorted(byval):
            oks = byval[c]
            acc = sum(oks) / len(oks)
            conf = c / conf_scale
            gap = abs(acc - conf)
            per_value[c] = {"n": len(oks), "acc": acc, "conf": conf, "gap": gap}
            ece += (len(oks) / n) * gap if n else 0.0
            mce = max(mce, gap)

        def _grp(pred):
            oks = [ok for c, ok in pairs if pred(c)]
            return {"n": len(oks), "acc": (sum(oks) / len(oks)) if oks else None}
        groups = {
            "low(<=%d)" % self.LOW_HI: _grp(lambda c: c <= self.LOW_HI),
            "mid": _grp(lambda c: self.LOW_HI < c < self.HI_LO),
            "high(>=%d)" % self.HI_LO: _grp(lambda c: c >= self.HI_LO),
        }
        return {"n": n, "per_value": per_value, "groups": groups, "ece": ece, "mce": mce}

    def gate(self, rel):
        n = rel["n"]
        lo = rel["groups"]["low(<=%d)" % self.LOW_HI]
        hi = rel["groups"]["high(>=%d)" % self.HI_LO]
        if n < self.MIN_TOTAL:
            return "INSUFFICIENT", ["only %d scored predictions (need >=%d)" % (n, self.MIN_TOTAL)]
        if lo["n"] < self.MIN_PER_GROUP or hi["n"] < self.MIN_PER_GROUP:
            return "INSUFFICIENT", [
                "need >=%d in BOTH groups; have low=%d high=%d" % (
                    self.MIN_PER_GROUP, lo["n"], hi["n"])]
        lift = hi["acc"] - lo["acc"]
        reasons = ["discrimination lift high-low = %+.1fpp (need >=%+.1fpp)" % (
            100 * lift, 100 * self.MIN_LIFT)]
        verdict = "PASS" if lift >= self.MIN_LIFT else "FAIL"
        return verdict, reasons


# --------------------------------------------------------------------------- dataset loading
def _iter_jsonl(path):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def load_closed_trades(dataset_path):
    """Collect (conviction, realized_return, MFE, MAE, win) tuples for CLOSED trades from the v2
    dataset, using the fields the dataset already joined (no new join key invented).

    A row qualifies iff kind=="trade" AND close.realized_pnl_pct is present (== realized/closed).
    Conviction: entry.conviction first (what the trader persists on the entry), else the joined
    decision.chosen.conviction; None => bucket 'unknown' (not sizeable).  Partial scale-out rows
    (close.partial True) are skipped -- a partial realization is not a full round-trip outcome.
    Returns list of dicts."""
    out = []
    for rec in _iter_jsonl(dataset_path):
        if not isinstance(rec, dict) or rec.get("kind") != "trade":
            continue
        close = rec.get("close") or {}
        if close.get("partial"):
            continue
        realized = close.get("realized_pnl_pct")
        if realized is None:
            continue  # still open / no realized P&L -> can't calibrate on it
        try:
            realized = float(realized)
        except (TypeError, ValueError):
            continue
        entry = rec.get("entry") or {}
        conv = entry.get("conviction")
        if conv is None:
            dec = rec.get("decision")
            if isinstance(dec, dict):
                chosen = dec.get("chosen") or {}
                if isinstance(chosen, dict):
                    conv = chosen.get("conviction")
        life = rec.get("lifecycle") or {}
        labels = rec.get("labels") or {}
        win = labels.get("win")
        if win is None:
            win = realized > SCRATCH_BAND
        out.append({
            "conviction": conv,
            "realized": realized,
            "mfe": _num(life.get("mfe_pct")),
            "mae": _num(life.get("mae_pct")),
            "win": bool(win),
        })
    return out


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- per-bucket stats
def bucket_stats(trades):
    """Aggregate closed trades into per-bucket stats. Returns {bucket: {...}} for buckets that
    have >=1 trade, plus 'overall'."""
    byb = defaultdict(list)
    for t in trades:
        byb[bucket(t["conviction"])].append(t)
    stats = {}
    for b, ts in byb.items():
        rets = [t["realized"] for t in ts]
        mfes = [t["mfe"] for t in ts if t["mfe"] is not None]
        maes = [t["mae"] for t in ts if t["mae"] is not None]
        wins = sum(1 for t in ts if t["win"])
        avg_mfe = statistics.mean(mfes) if mfes else None
        avg_mae = statistics.mean(maes) if maes else None
        # MFE:MAE on magnitudes (MAE is stored as a signed/negative adverse excursion)
        mfe_mae = None
        if avg_mfe is not None and avg_mae not in (None, 0):
            denom = abs(avg_mae)
            mfe_mae = round(avg_mfe / denom, 2) if denom else None
        stats[b] = {
            "n": len(ts),
            "mean_ret": round(statistics.mean(rets), 2),
            "median_ret": round(statistics.median(rets), 2),
            "win_rate": round(wins / len(ts), 3),
            "avg_mfe": round(avg_mfe, 2) if avg_mfe is not None else None,
            "avg_mae": round(avg_mae, 2) if avg_mae is not None else None,
            "mfe_mae": mfe_mae,
        }
    if trades:
        stats["overall"] = {
            "n": len(trades),
            "mean_ret": round(statistics.mean([t["realized"] for t in trades]), 2),
        }
    return stats


# --------------------------------------------------------------------------- multiplier derivation
def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def derive_multipliers(trades, *, soft_pct, hard_pct, gate=None):
    """Fit a GATED per-bucket size multiplier from realized outcomes.

    soft_pct / hard_pct: the current soft per-trade fraction and the ABSOLUTE hard ceiling. The
    up-multiplier is bounded by max_up = hard_pct / soft_pct so base_soft*mult can NEVER exceed
    the hard cap.  Returns (bucket_mult, conviction_map, info).

    Gating (double-gated, conservative):
      1. OVERALL gate via calibration_gate: PASS (high-conviction genuinely wins more, enough n)
         is REQUIRED before ANY non-flat multiplier is emitted.  INSUFFICIENT/FAIL => all flat.
      2. PER-BUCKET: even under overall PASS, a bucket with n < MIN_PER_GROUP falls back to flat
         (1.0, "insufficient data -- flat").
    """
    if gate is None:
        gate, _ = _resolve_gate()
    max_up = round(hard_pct / soft_pct, 4) if soft_pct > 0 else 1.0
    if max_up < 1.0:
        max_up = 1.0  # no headroom above the soft cap (soft==hard) -> up-sizing impossible

    stats = bucket_stats(trades)
    overall_mean = stats.get("overall", {}).get("mean_ret", 0.0)

    # overall discrimination/sufficiency gate on (conviction, win) pairs
    pairs = [(float(t["conviction"]), bool(t["win"]))
             for t in trades if t["conviction"] is not None and _num(t["conviction"]) is not None
             and float(t["conviction"]) >= 0]
    rel = gate.reliability(pairs) if pairs else {"n": 0,
        "groups": {"low(<=4)": {"n": 0, "acc": None}, "mid": {"n": 0, "acc": None},
                   "high(>=8)": {"n": 0, "acc": None}}}
    verdict, gate_reasons = gate.gate(rel)
    allow_nonflat = (verdict == "PASS")
    min_per_bucket = getattr(gate, "MIN_PER_GROUP", 8)

    bucket_mult = {}
    bucket_note = {}
    for b in ("high (8-10)", "mid (5-7)", "low (1-4)"):
        s = stats.get(b)
        if not allow_nonflat:
            bucket_mult[b] = 1.0
            bucket_note[b] = "flat (overall gate %s)" % verdict
            continue
        if not s or s["n"] < min_per_bucket:
            bucket_mult[b] = 1.0
            bucket_note[b] = "insufficient data -- flat (n=%d < %d)" % (
                (s["n"] if s else 0), min_per_bucket)
            continue
        mean_b = s["mean_ret"]
        rel_edge = mean_b - overall_mean
        if mean_b < 0:                                   # losing bucket -> down-size
            mult = _clamp(1.0 + mean_b / DOWN_REF, MIN_MULT, 1.0)
            note = "down-sized (mean %.1f%% < 0)" % mean_b
        elif mean_b > 0 and rel_edge > 0 and s["win_rate"] >= WIN_FLOOR_FOR_UPSIZE:
            mult = _clamp(1.0 + rel_edge / UP_REF, 1.0, max_up)  # up-size, bounded by hard ceiling
            note = ("up-sized (mean %.1f%%, +%.1fpp vs book, win %.0f%%)"
                    % (mean_b, rel_edge, 100 * s["win_rate"]))
            if abs(mult - 1.0) < 1e-9:
                note = "flat (no headroom above soft cap)" if max_up <= 1.0 else "flat (edge below threshold)"
        else:                                            # positive but not distinguished -> flat
            mult = 1.0
            note = "flat (no justified edge: mean %.1f%%, win %.0f%%)" % (mean_b, 100 * s["win_rate"])
        bucket_mult[b] = round(mult, 2)
        bucket_note[b] = note

    # expand buckets -> a per-conviction-level 1..10 map for the config representation
    conviction_map = {}
    for b, levels in BUCKET_LEVELS.items():
        for lvl in levels:
            conviction_map[lvl] = bucket_mult.get(b, 1.0)

    info = {
        "gate_verdict": verdict,
        "gate_reasons": gate_reasons,
        "gate_source": None,   # filled by caller
        "allow_nonflat": allow_nonflat,
        "min_per_bucket": min_per_bucket,
        "soft_pct": soft_pct,
        "hard_pct": hard_pct,
        "max_up_multiplier": max_up,
        "overall_mean_ret": overall_mean,
        "n_closed": len(trades),
        "n_conviction_known": len(pairs),
        "stats": stats,
        "bucket_notes": bucket_note,
        "reliability_groups": rel.get("groups"),
    }
    return bucket_mult, conviction_map, info


# --------------------------------------------------------------------------- reporting
def _fmt(v, suf="%"):
    return "-" if v is None else ("%+.1f%s" % (v, suf))


def report(bucket_mult, info):
    lines = []
    lines.append("=" * 84)
    lines.append("CONVICTION -> SIZE MULTIPLIER CALIBRATION  (empirical, GATED, PROPOSE-ONLY)")
    lines.append("=" * 84)
    lines.append("closed trades: %d   conviction-known: %d   overall mean return: %s"
                 % (info["n_closed"], info["n_conviction_known"], _fmt(info["overall_mean_ret"])))
    lines.append("soft per-trade cap: %.0f%%   hard ceiling: %.0f%%   max up-multiplier: x%.2f%s"
                 % (100 * info["soft_pct"], 100 * info["hard_pct"], info["max_up_multiplier"],
                    "  (no up-size headroom: soft==hard)" if info["max_up_multiplier"] <= 1.0 else ""))
    lines.append("gate: %s  [%s]" % (info["gate_verdict"], info.get("gate_source") or "?"))
    for r in info["gate_reasons"]:
        lines.append("   - %s" % r)
    lines.append("")
    hdr = "  %-12s %4s %9s %9s %6s %8s %8s %7s   %8s  %s" % (
        "bucket", "n", "mean%", "median%", "win%", "avgMFE%", "avgMAE%", "MFE:MAE", "MULT", "note")
    lines.append(hdr)
    lines.append("  " + "-" * (len(hdr) - 2))
    for b in ("high (8-10)", "mid (5-7)", "low (1-4)", "unknown"):
        s = info["stats"].get(b)
        if not s:
            if b == "unknown":
                continue
            lines.append("  %-12s %4d %9s %9s %6s %8s %8s %7s   x%-7.2f %s" % (
                b, 0, "-", "-", "-", "-", "-", "-", bucket_mult.get(b, 1.0),
                info["bucket_notes"].get(b, "no data -- flat")))
            continue
        mult = bucket_mult.get(b)
        mults = ("x%-7.2f" % mult) if mult is not None else "    -   "
        lines.append("  %-12s %4d %9s %9s %5.0f%% %8s %8s %7s   %s %s" % (
            b, s["n"], _fmt(s["mean_ret"]), _fmt(s["median_ret"]), 100 * s["win_rate"],
            _fmt(s["avg_mfe"]), _fmt(s["avg_mae"]),
            ("%.2f" % s["mfe_mae"]) if s["mfe_mae"] is not None else "-",
            mults, info["bucket_notes"].get(b, "") if b != "unknown" else "(no conviction -- not sizeable)"))
    lines.append("")
    lines.append("proposed conviction_size_multipliers (1..10): %s"
                 % json.dumps({int(k): v for k, v in sorted(
                     {kk: bucket_mult.get(bb, 1.0) for bb, lv in BUCKET_LEVELS.items() for kk in lv}.items())}))
    if all(abs(v - 1.0) < 1e-9 for v in bucket_mult.values()):
        lines.append("RESULT: FLAT everywhere (no gated edge) -> sizing UNCHANGED vs today.")
    lines.append("NOTE: PROPOSE-ONLY. Real-money application needs Trevor's explicit opt-in "
                 "(review this blob, set config.yaml trading.conviction_size_multipliers, and wire "
                 "run_trader.py like conviction_size_curve).")
    lines.append("=" * 84)
    return "\n".join(lines)


# --------------------------------------------------------------------------- proposal blob
def build_blob(bucket_mult, conviction_map, info, dataset_path):
    return {
        "schema": "conviction_size_calibration.v1",
        "generated_ts": datetime.now(timezone.utc).isoformat(),
        "dataset": dataset_path,
        "applied": False,                     # propose-only; never auto-applied
        "gate": {"verdict": info["gate_verdict"], "reasons": info["gate_reasons"],
                 "source": info.get("gate_source"), "min_per_bucket": info["min_per_bucket"],
                 "reliability_groups": info["reliability_groups"]},
        "caps": {"soft_pct": info["soft_pct"], "hard_pct": info["hard_pct"],
                 "max_up_multiplier": info["max_up_multiplier"]},
        "n_closed": info["n_closed"], "n_conviction_known": info["n_conviction_known"],
        "overall_mean_ret": info["overall_mean_ret"],
        "bucket_stats": {b: info["stats"].get(b) for b in BUCKET_ORDER if b in info["stats"]},
        "bucket_multipliers": bucket_mult,
        "bucket_notes": info["bucket_notes"],
        "conviction_size_multipliers": {int(k): v for k, v in conviction_map.items()},
        "note": ("PROPOSE-ONLY. Empty/flat => risk.py sizing byte-identical to today. Apply only "
                 "after Trevor reviews: set config.yaml trading.conviction_size_multipliers AND "
                 "wire it into run_trader.py's RiskLimits (mirror conviction_size_curve)."),
    }


def _load_caps_from_config(config_path):
    """Best-effort read of the current soft cap + hard ceiling from config.yaml (falls back to
    the risk.py defaults). Read-only."""
    soft, hard = 0.12, 0.25
    try:
        import yaml
        with open(config_path) as f:
            data = yaml.safe_load(f) or {}
        tr = (data.get("trading") or {})
        soft = float(tr.get("max_trade_pct", soft))
    except Exception:
        pass
    try:
        from exitmgr.risk import RiskLimits
        hard = float(RiskLimits().max_trade_pct_hard)
    except Exception:
        pass
    return soft, hard


def _write_config(config_path, conviction_map):
    """Stage the mapping into config.yaml trading.conviction_size_multipliers. Backs up first.
    GUARDED behind --write-config (default OFF). Requires PyYAML."""
    import yaml
    import shutil
    from datetime import datetime as _dt
    bak = "%s.bak-convcal-%s" % (config_path, _dt.now().strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(config_path, bak)
    with open(config_path) as f:
        data = yaml.safe_load(f) or {}
    data.setdefault("trading", {})
    data["trading"]["conviction_size_multipliers"] = {int(k): float(v) for k, v in conviction_map.items()}
    with open(config_path, "w") as f:
        yaml.safe_dump(data, f, sort_keys=False, default_flow_style=False)
    return bak


# --------------------------------------------------------------------------- selftest
def _synth(bucket_specs):
    """bucket_specs: {conviction: (n, win_rate, mean_ret)} -> deterministic trade list."""
    trades = []
    for conv, (n, wr, mean_ret) in bucket_specs.items():
        wins = round(wr * n)
        for i in range(n):
            w = i < wins
            r = abs(mean_ret) if w else -abs(mean_ret)
            # nudge the mean toward mean_ret regardless of win/loss split
            trades.append({"conviction": conv, "realized": float(mean_ret),
                           "mfe": abs(mean_ret) + 5.0, "mae": -(abs(mean_ret) / 2 + 3.0),
                           "win": bool(w)})
            _ = r
    return trades


def selftest():
    gate, src = _resolve_gate()
    print("gate source:", src)
    # high genuinely outperforms, low loses; enough n in both groups -> PASS, up/down
    trades = _synth({9: (15, 0.80, 30.0), 3: (15, 0.20, -30.0)})
    bm, cm, info = derive_multipliers(trades, soft_pct=0.12, hard_pct=0.25, gate=gate)
    info["gate_source"] = src
    print(report(bm, info))
    assert info["gate_verdict"] == "PASS", info
    assert bm["high (8-10)"] > 1.0, bm
    assert bm["low (1-4)"] < 1.0, bm
    assert bm["high (8-10)"] <= info["max_up_multiplier"] + 1e-9, bm
    # thin data -> INSUFFICIENT -> flat everywhere
    thin = _synth({9: (2, 1.0, 30.0), 3: (2, 0.0, -30.0)})
    bm2, cm2, info2 = derive_multipliers(thin, soft_pct=0.12, hard_pct=0.25, gate=gate)
    assert info2["gate_verdict"] == "INSUFFICIENT", info2
    assert all(v == 1.0 for v in bm2.values()), bm2
    print("\nSELFTEST PASSED.")


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", default="data/trade_dataset.jsonl",
                    help="v2 closed-trade dataset (default data/trade_dataset.jsonl)")
    ap.add_argument("--config", default="config.yaml",
                    help="config.yaml to read caps from (and stage into with --write-config)")
    ap.add_argument("--soft-pct", type=float, default=None,
                    help="override current soft per-trade fraction (else read from config)")
    ap.add_argument("--hard-pct", type=float, default=None,
                    help="override the hard per-trade ceiling (else RiskLimits default 0.25)")
    ap.add_argument("--out", default="data/conviction_size_calibration_proposal.json",
                    help="write the proposal JSON blob here")
    ap.add_argument("--write-config", action="store_true",
                    help="(DANGER, default OFF) stage the mapping into config.yaml. Propose-only "
                         "otherwise. Only writes on a PASS with a non-flat proposal.")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        selftest()
        return 0

    gate, src = _resolve_gate()
    soft, hard = _load_caps_from_config(args.config)
    if args.soft_pct is not None:
        soft = args.soft_pct
    if args.hard_pct is not None:
        hard = args.hard_pct

    trades = load_closed_trades(args.dataset)
    bucket_mult, conviction_map, info = derive_multipliers(
        trades, soft_pct=soft, hard_pct=hard, gate=gate)
    info["gate_source"] = src
    print(report(bucket_mult, info))

    blob = build_blob(bucket_mult, conviction_map, info, args.dataset)
    try:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w") as f:
            json.dump(blob, f, indent=2)
        print("\nwrote proposal blob -> %s" % args.out)
    except Exception as e:
        print("\n[WARN] could not write proposal blob: %s" % e)

    nonflat = any(abs(v - 1.0) > 1e-9 for v in bucket_mult.values())
    if args.write_config:
        if info["gate_verdict"] != "PASS" or not nonflat:
            print("[--write-config] refused: gate=%s, non-flat=%s -- nothing to stage (flat == "
                  "no-op). Propose-only." % (info["gate_verdict"], nonflat))
        else:
            bak = _write_config(args.config, conviction_map)
            print("[--write-config] staged conviction_size_multipliers into %s (backup %s). "
                  "STILL requires the run_trader.py wiring + --arm to size real money." % (args.config, bak))
    return 0


if __name__ == "__main__":
    sys.exit(main())
