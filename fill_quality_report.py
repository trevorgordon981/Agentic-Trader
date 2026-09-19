#!/usr/bin/env python3
"""Public API contract; production-derived narrative omitted."""
import argparse
import json
import os
import sys

from exitmgr import dataset_integrity


MIN_FILLS = 5
FILL_RATE_TOO_TIGHT = 0.80
SLIP_TOO_LOOSE_PCT = 10.0
FLOOR_MARGIN = 0.10





FLOOR_MIN = 0.10
FLOOR_MAX = 0.90
LOOSEN_STEP = 0.15


DEFAULT_EXIT_SLIPPAGE_FLOOR = 0.50


CURRENT_EXIT_SLIPPAGE_FLOOR = DEFAULT_EXIT_SLIPPAGE_FLOOR
CURRENT_MARKETABLE_BUFFER = 0.05


_UNFILLED_STATUSES = {
    "submitted", "presubmitted", "pendingsubmit", "pendingcancel",
    "cancelled", "apicancelled", "inactive",
}

DEFAULT_DATASET = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "trade_dataset.jsonl"
)
DEFAULT_CONFIG = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "config.yaml"
)


def current_exit_slippage_floor(config_path=None):
    """Public API contract; production-derived narrative omitted."""
    path = config_path or DEFAULT_CONFIG
    try:
        import yaml
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        v = (data.get("rules") or {}).get("exit_slippage_floor")
        if v is not None:
            return float(v)
    except Exception:
        pass
    return DEFAULT_EXIT_SLIPPAGE_FLOOR


def _suggested_floor(giveup_pct, current_floor):
    """Public API contract; production-derived narrative omitted."""
    if giveup_pct is None:
        return None
    return round(min(current_floor, max(FLOOR_MIN, giveup_pct / 100.0 + FLOOR_MARGIN)), 2)


def _loosen_target(current_floor):
    """Public API contract; production-derived narrative omitted."""
    return round(min(FLOOR_MAX, current_floor + LOOSEN_STEP), 2)



def _percentile(vals, q):
    """Public API contract; production-derived narrative omitted."""
    xs = sorted(v for v in vals if v is not None)
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    pos = (q / 100.0) * (len(xs) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    frac = pos - lo
    return xs[lo] + (xs[hi] - xs[lo]) * frac


def _median(vals):
    return _percentile(vals, 50)


def _round(x, n=2):
    return round(x, n) if isinstance(x, (int, float)) else x



def iter_rows(path):
    """Public API contract; production-derived narrative omitted."""
    if not os.path.exists(path):
        return
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    status = str(row.get("record_status") or "").upper()
                    if status == dataset_integrity.CANONICAL and row.get("canonical") is True:
                        yield row
                except Exception:
                    continue
    except Exception:
        return


def extract_closes(rows):
    """Public API contract; production-derived narrative omitted."""
    out = []
    for r in rows:
        if not isinstance(r, dict) or r.get("kind") != "trade":
            continue
        close = r.get("close") or {}
        entry = r.get("entry") or {}
        symbol = r.get("symbol") or entry.get("symbol") or "?"
        fill_status = close.get("fill_status")
        avg_fill = close.get("avg_fill_price")
        slip_ps = close.get("slippage_per_share")
        slip_pct = close.get("slippage_pct")
        status_l = (fill_status or "").strip().lower()
        filled = avg_fill is not None
        unfilled = (not filled) and (status_l in _UNFILLED_STATUSES)
        out.append({
            "symbol": symbol,
            "con_id": r.get("con_id"),
            "ts": close.get("ts"),
            "rule_fired": close.get("rule_fired"),
            "fill_status": fill_status,
            "avg_fill_price": avg_fill,
            "trigger_mark": close.get("trigger_mark"),
            "slippage_per_share": slip_ps,
            "slippage_pct": slip_pct,

            "close_qty": (entry.get("quantity") if close.get("close_qty") is None
                          else close.get("close_qty")),
            "is_spread": bool(entry.get("spread")),
            "partial": bool(close.get("partial", False)),
            "filled": filled,
            "unfilled": unfilled,
            "measured": (slip_pct is not None and slip_ps is not None),
        })
    return out



def _giveup_pct(slip_pct):
    """Public API contract; production-derived narrative omitted."""
    return -slip_pct if slip_pct is not None else None


def aggregate_symbol(recs, current_floor=None):
    """Public API contract; production-derived narrative omitted."""
    if current_floor is None:
        current_floor = CURRENT_EXIT_SLIPPAGE_FLOOR
    filled = [r for r in recs if r["filled"]]
    unfilled = [r for r in recs if r["unfilled"]]
    measured = [r for r in recs if r["measured"]]

    slip_pcts = [r["slippage_pct"] for r in measured]
    slip_dollars = [r["slippage_per_share"] for r in measured]
    giveups = [_giveup_pct(r["slippage_pct"]) for r in measured]

    n_fills = len(filled)
    n_unfilled = len(unfilled)
    denom = n_fills + n_unfilled
    fill_rate = (n_fills / denom) if denom else None

    median_slip_pct = _median(slip_pcts)
    p90_slip_pct = _percentile(slip_pcts, 90)
    median_slip_usd = _median(slip_dollars)
    p90_slip_usd = _percentile(slip_dollars, 90)
    median_giveup = _median(giveups)

    p90_giveup = _percentile(giveups, 90)


    worst = sorted(measured, key=lambda r: (r["slippage_pct"] if r["slippage_pct"] is not None else 0))[:3]
    worst_out = [{
        "ts": r["ts"], "rule_fired": r["rule_fired"], "is_spread": r["is_spread"],
        "slippage_pct": r["slippage_pct"], "slippage_per_share": r["slippage_per_share"],
        "trigger_mark": r["trigger_mark"], "avg_fill_price": r["avg_fill_price"],
    } for r in worst]

    verdict, action = _recommend(n_fills, fill_rate, median_giveup, current_floor)

    return {
        "symbol": recs[0]["symbol"],
        "n_closes": len(recs),
        "n_fills": n_fills,
        "n_unfilled": n_unfilled,
        "n_measured": len(measured),
        "fill_rate": _round(fill_rate, 3),
        "median_slippage_pct": _round(median_slip_pct),
        "p90_slippage_pct": _round(p90_slip_pct),
        "median_slippage_usd": _round(median_slip_usd, 4),
        "p90_slippage_usd": _round(p90_slip_usd, 4),
        "median_giveup_pct": _round(median_giveup),
        "p90_giveup_pct": _round(p90_giveup),
        "worst_fills": worst_out,
        "verdict": verdict,
        "recommendation": action,

        "_p90_giveup_frac": (p90_giveup / 100.0) if p90_giveup is not None else None,
    }


def _recommend(n_fills, fill_rate, median_giveup, current_floor=None):
    """Public API contract; production-derived narrative omitted."""
    if current_floor is None:
        current_floor = CURRENT_EXIT_SLIPPAGE_FLOOR
    if n_fills < MIN_FILLS:
        return "INSUFFICIENT", f"insufficient filled closes (n={n_fills}); need >= {MIN_FILLS}"
    if fill_rate is not None and fill_rate < FILL_RATE_TOO_TIGHT:
        loosen_target = _loosen_target(current_floor)
        return ("TOO_TIGHT",
                f"fill-rate {fill_rate:.0%} < {FILL_RATE_TOO_TIGHT:.0%}: exits resting/cancelled -- "
                f"LOOSEN: raise EXIT_SLIPPAGE_FLOOR {current_floor:.2f} -> ~{loosen_target:.2f} "
                f"(a LARGER fraction below mark lowers the price line / crosses more) so exits fill")
    if median_giveup is not None and median_giveup > SLIP_TOO_LOOSE_PCT:
        tighten_target = _suggested_floor(median_giveup, current_floor)
        return ("TOO_LOOSE",
                f"median give-up {median_giveup:.1f}% > {SLIP_TOO_LOOSE_PCT:.0f}%: crossing too far -- "
                f"TIGHTEN: lower EXIT_SLIPPAGE_FLOOR {current_floor:.2f} -> ~{tighten_target:.2f} "
                f"(a SMALLER fraction below mark; refuse fills worse than ~{tighten_target*100:.0f}% "
                f"under mark) to stop dumping edge")
    return "OK", "fills clearing at a healthy price; leave the buffer/floor as-is"



def portfolio_summary(sym_summaries, all_closes, current_floor=None):
    if current_floor is None:
        current_floor = CURRENT_EXIT_SLIPPAGE_FLOOR
    filled = [r for r in all_closes if r["filled"]]
    unfilled = [r for r in all_closes if r["unfilled"]]
    measured = [r for r in all_closes if r["measured"]]
    n_fills = len(filled)
    denom = n_fills + len(unfilled)
    fill_rate = (n_fills / denom) if denom else None

    giveups = [_giveup_pct(r["slippage_pct"]) for r in measured]
    slip_dollars = [r["slippage_per_share"] for r in measured]
    median_giveup = _median(giveups)
    p90_giveup = _percentile(giveups, 90)




    suggested_floor = _suggested_floor(p90_giveup, current_floor)


    if n_fills < MIN_FILLS:
        verdict = "INSUFFICIENT"
        action = f"insufficient filled closes (n={n_fills}); need >= {MIN_FILLS} before tuning knobs"
    elif fill_rate is not None and fill_rate < FILL_RATE_TOO_TIGHT:
        verdict = "TOO_TIGHT"
        loosen_target = _loosen_target(current_floor)
        action = (f"portfolio fill-rate {fill_rate:.0%}: LOOSEN -- raise EXIT_SLIPPAGE_FLOOR "
                  f"{current_floor:.2f} -> ~{loosen_target:.2f} (a LARGER fraction below mark lowers "
                  f"the price line so protective exits fill)")
    elif median_giveup is not None and median_giveup > SLIP_TOO_LOOSE_PCT:
        verdict = "TOO_LOOSE"
        action = (f"portfolio median give-up {median_giveup:.1f}%: TIGHTEN -- lower EXIT_SLIPPAGE_FLOOR "
                  f"{current_floor:.2f} -> ~{suggested_floor:.2f} (a SMALLER fraction; refuse fills "
                  f"worse than ~{p90_giveup:.1f}% below mark) to stop dumping edge")
    else:
        verdict = "OK"
        action = (f"fills healthy; EXIT_SLIPPAGE_FLOOR {current_floor:.2f} is fine "
                  f"(p90 give-up only {p90_giveup:.1f}%)" if p90_giveup is not None
                  else "fills healthy; leave knobs as-is")

    return {
        "n_closes": len(all_closes),
        "n_fills": n_fills,
        "n_unfilled": len(unfilled),
        "n_measured": len(measured),
        "fill_rate": _round(fill_rate, 3),
        "median_giveup_pct": _round(median_giveup),
        "p90_giveup_pct": _round(p90_giveup),
        "median_slippage_usd": _round(_median(slip_dollars), 4),
        "p90_slippage_usd": _round(_percentile(slip_dollars, 90), 4),
        "current_exit_slippage_floor": current_floor,
        "current_marketable_buffer": CURRENT_MARKETABLE_BUFFER,
        "suggested_exit_slippage_floor": suggested_floor,
        "suggested_marketable_buffer_pct": _round(p90_giveup),
        "verdict": verdict,
        "recommendation": action,
    }



def build_report(path, min_fills=MIN_FILLS, config_path=None):
    global MIN_FILLS
    MIN_FILLS = min_fills
    current_floor = current_exit_slippage_floor(config_path)
    closes = extract_closes(iter_rows(path))
    by_symbol = {}
    for r in closes:
        by_symbol.setdefault(r["symbol"], []).append(r)
    sym_summaries = [aggregate_symbol(recs, current_floor) for recs in by_symbol.values()]
    sym_summaries.sort(key=lambda s: (s["n_fills"], s["symbol"]), reverse=True)
    port = portfolio_summary(sym_summaries, closes, current_floor)

    for s in sym_summaries:
        s.pop("_p90_giveup_frac", None)
    return {
        "dataset": path,
        "total_closes": len(closes),
        "total_fills": port["n_fills"],
        "portfolio": port,
        "by_symbol": sym_summaries,
    }



def _fmt(x, suffix="", nd=2):
    if x is None:
        return "-"
    if isinstance(x, float):
        return f"{x:.{nd}f}{suffix}"
    return f"{x}{suffix}"


def render_table(report):
    lines = []
    n_fills = report["total_fills"]
    lines.append(f"FILL-QUALITY REPORT  ({report['dataset']})")
    lines.append(f"  total closed-trade rows: {report['total_closes']}   filled closes: {n_fills}")

    if n_fills == 0:
        lines.append("")
        lines.append(f"  insufficient filled closes (n={n_fills}) -- nothing to assess.")
        lines.append("  (trader likely down; dataset holds only no_trade/rejected rows.)")
        return "\n".join(lines)

    lines.append("")
    hdr = (f"  {'SYM':<6} {'fills':>5} {'unfl':>4} {'fill%':>6} "
           f"{'med_slip%':>9} {'p90_slip%':>9} {'med_$':>8} {'p90_$':>8} "
           f"{'med_giveup%':>11}  VERDICT")
    lines.append(hdr)
    lines.append("  " + "-" * (len(hdr) - 2))
    for s in report["by_symbol"]:
        lines.append(
            f"  {s['symbol']:<6} {s['n_fills']:>5} {s['n_unfilled']:>4} "
            f"{_fmt((s['fill_rate'] or 0)*100 if s['fill_rate'] is not None else None,'',0):>6} "
            f"{_fmt(s['median_slippage_pct']):>9} {_fmt(s['p90_slippage_pct']):>9} "
            f"{_fmt(s['median_slippage_usd'],'',4):>8} {_fmt(s['p90_slippage_usd'],'',4):>8} "
            f"{_fmt(s['median_giveup_pct']):>11}  {s['verdict']}"
        )
        lines.append(f"         -> {s['recommendation']}")

    p = report["portfolio"]
    lines.append("")
    lines.append("  PORTFOLIO")
    lines.append(f"    fill-rate           : {_fmt((p['fill_rate'] or 0)*100 if p['fill_rate'] is not None else None,'%',0)}")
    lines.append(f"    median give-up      : {_fmt(p['median_giveup_pct'],'%')}   p90 give-up: {_fmt(p['p90_giveup_pct'],'%')}")
    lines.append(f"    median slip $/sh    : {_fmt(p['median_slippage_usd'],'',4)}   p90: {_fmt(p['p90_slippage_usd'],'',4)}")
    lines.append(f"    EXIT_SLIPPAGE_FLOOR : current {p['current_exit_slippage_floor']}  ->  suggested {p['suggested_exit_slippage_floor']}")
    lines.append(f"    marketable buffer   : suggested ~{_fmt(p['suggested_marketable_buffer_pct'],'%')} through the mark")
    lines.append(f"    VERDICT             : {p['verdict']} -- {p['recommendation']}")
    return "\n".join(lines)



def main(argv=None):
    ap = argparse.ArgumentParser(description="Per-symbol fill-quality / slippage report + exit-knob recommendation.")
    ap.add_argument("--dataset", default=DEFAULT_DATASET, help="path to trade_dataset.jsonl")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="config.yaml (source of the live EXIT_SLIPPAGE_FLOOR)")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON instead of a table")
    ap.add_argument("--min-fills", type=int, default=MIN_FILLS, help="min filled closes per symbol before a verdict")
    args = ap.parse_args(argv)

    report = build_report(args.dataset, min_fills=args.min_fills, config_path=args.config)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(render_table(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
