#!/usr/bin/env python3
"""tune_exit_floor.py -- PROPOSE-ONLY tuner for the exit slippage floor (2026-07-03).

Closes the loop on fill_quality_report.py: it reads that tool's machine-readable JSON
(the `portfolio` roll-up + `by_symbol` give-up stats) and RECOMMENDS whether to keep,
raise, or lower the exit slippage floor -- the config knob `rules.exit_slippage_floor`
(order.py's EXIT_SLIPPAGE_FLOOR): a TRIGGERED bid-anchored SELL-to-close is never priced
below mark*(1 - floor).

  DEFAULT = PROPOSE ONLY. It prints a recommendation + the evidence and changes NOTHING.
  --write stages the recommended value into config.yaml (rules.exit_slippage_floor). Even
  then, to size REAL exits the value must reach a live OrderManager -- manager.py currently
  builds OrderManager WITHOUT passing exit_slippage_floor, so wiring
  `exit_slippage_floor=cfg.rules.exit_slippage_floor` there is the remaining (flagged) seam.

DIRECTION / CONVENTION (matches fill_quality_report + order.py; "floor" = the PRICE line held):
  * INSUFFICIENT (fill_quality verdict, or n_fills < min) -> HOLD at the current floor; need more
    fills. NEVER recommends a change on thin data -- it will not fabricate stats.
  * TOO_LOOSE  -> fills land chronically far BELOW the mark (big give-up). We are dumping edge into
    a wide book. Fix = RAISE the floor (hold the price line higher) so we stop dumping. In the
    config FRACTION this means a SMALLER exit_slippage_floor (tolerate less give-up) -- we defer to
    fill_quality's own computed `suggested_exit_slippage_floor` (it only ever tightens from 0.50).
  * TOO_TIGHT  -> a high fraction of protective exits rest UNFILLED/cancelled (low fill-rate). Fix =
    LOWER the floor (let the price line drop) so protective closes fill. In the config FRACTION this
    is a LARGER exit_slippage_floor (tolerate more give-up to guarantee the fill). fill_quality does
    not emit a loosen target, so we propose a bounded step.
  * OK -> KEEP the current floor.

The action word describes the FLOOR PRICE (RAISE/LOWER, matching fill_quality's prose); the
`recommended_exit_slippage_floor` FRACTION moves inversely (documented in each recommendation).

Never fabricates: all numbers come straight from fill_quality's JSON; if that JSON says
INSUFFICIENT we hold, full stop.

Run:
  python tune_exit_floor.py                # propose only, from the live dataset
  python tune_exit_floor.py --json         # machine-readable proposal
  python tune_exit_floor.py --write        # ALSO stage the value into config.yaml (opt-in)
  python tune_exit_floor.py --report-json PATH   # feed a saved fill_quality JSON instead of rebuilding
"""
import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(HERE, "config.yaml")

# --- tuner knobs -----------------------------------------------------------------------------
FLOOR_MIN = 0.10          # never propose a floor below this (order.py's own lower guard)
FLOOR_MAX = 0.90          # never propose selling below 10% of the mark, even to force a fill
LOOSEN_STEP = 0.15        # TOO_TIGHT: bounded step to LOWER the floor price (raise the fraction)


def _current_floor_from_config(config_path):
    """Read rules.exit_slippage_floor from config.yaml; fall back to 0.50 (order.py default).
    Deliberately dependency-light (no yaml import needed) but tolerant."""
    try:
        import yaml
        with open(config_path) as f:
            data = yaml.safe_load(f) or {}
        v = ((data.get("rules") or {}).get("exit_slippage_floor"))
        if v is not None:
            return float(v)
    except Exception:
        pass
    return 0.50


def load_report(dataset=None, report_json=None, min_fills=5):
    """Get the fill_quality report dict -- either from a saved --report-json file, or by
    building it fresh via fill_quality_report.build_report (never fabricated)."""
    if report_json:
        with open(report_json) as f:
            return json.load(f)
    sys.path.insert(0, HERE)
    import fill_quality_report as fq
    ds = dataset or fq.DEFAULT_DATASET
    return fq.build_report(ds, min_fills=min_fills)


def recommend(report, current_floor, min_fills=5):
    """Pure decision fn. Returns a proposal dict. PROPOSE-ONLY: never mutates anything.
    Keyed strictly off fill_quality's `portfolio` block -- no invented stats."""
    port = (report or {}).get("portfolio") or {}
    verdict = port.get("verdict", "INSUFFICIENT")
    n_fills = port.get("n_fills", 0) or 0
    fill_rate = port.get("fill_rate")
    median_giveup = port.get("median_giveup_pct")
    p90_giveup = port.get("p90_giveup_pct")
    suggested = port.get("suggested_exit_slippage_floor")

    evidence = {
        "n_fills": n_fills,
        "n_unfilled": port.get("n_unfilled"),
        "fill_rate": fill_rate,
        "median_giveup_pct": median_giveup,
        "p90_giveup_pct": p90_giveup,
        "fill_quality_suggested_floor": suggested,
        "fill_quality_verdict": verdict,
    }

    # INSUFFICIENT wins: respect fill_quality's own gate AND our own min-fills guard. Never
    # recommend a change on thin data.
    if verdict == "INSUFFICIENT" or n_fills < min_fills:
        return {
            "action": "HOLD",
            "current_floor": current_floor,
            "recommended_exit_slippage_floor": current_floor,
            "changed": False,
            "reason": (f"INSUFFICIENT data (n_fills={n_fills}, need >= {min_fills}): "
                       f"hold at current floor {current_floor}, need more fills"),
            "evidence": evidence,
        }

    if verdict == "TOO_TIGHT":
        # protective exits resting unfilled -> LOWER the floor price so they fill == RAISE the
        # fraction (tolerate more give-up). Bounded step; fill_quality gives no loosen target.
        rec = round(min(FLOOR_MAX, current_floor + LOOSEN_STEP), 2)
        return {
            "action": "LOWER",  # lower the floor PRICE line so exits fill
            "current_floor": current_floor,
            "recommended_exit_slippage_floor": rec,
            "changed": rec != current_floor,
            "reason": (f"TOO_TIGHT: fill-rate {fill_rate:.0%} of protective exits resting/cancelled "
                       f"-- LOWER the floor price (raise exit_slippage_floor {current_floor}->{rec}) "
                       f"so protective closes actually fill"),
            "evidence": evidence,
        }

    if verdict == "TOO_LOOSE":
        # fills landing far below the mark -> RAISE the floor price to stop dumping == a SMALLER
        # fraction. Defer to fill_quality's own suggested (it only ever tightens from 0.50).
        rec = suggested if suggested is not None else round(
            max(FLOOR_MIN, current_floor - LOOSEN_STEP), 2)
        rec = round(min(current_floor, max(FLOOR_MIN, rec)), 2)  # only tightens; never below MIN
        return {
            "action": "RAISE",  # raise the floor PRICE line to stop dumping edge
            "current_floor": current_floor,
            "recommended_exit_slippage_floor": rec,
            "changed": rec != current_floor,
            "reason": (f"TOO_LOOSE: median give-up {median_giveup}% (p90 {p90_giveup}%) -- RAISE the "
                       f"floor price (tighten exit_slippage_floor {current_floor}->{rec}) so we stop "
                       f"dumping edge into a wide book"),
            "evidence": evidence,
        }

    # OK
    return {
        "action": "KEEP",
        "current_floor": current_floor,
        "recommended_exit_slippage_floor": current_floor,
        "changed": False,
        "reason": (f"OK: fills clearing healthy (p90 give-up {p90_giveup}%); "
                   f"keep exit_slippage_floor at {current_floor}"),
        "evidence": evidence,
    }


def stage_into_config(config_path, value):
    """--write: set rules.exit_slippage_floor: <value> in config.yaml, in place, preserving the
    rest of the file. Backs up first. Line-oriented (no full YAML round-trip -> no comment loss).
    Only touches the exit_slippage_floor line under a top-level `rules:` block."""
    import datetime
    with open(config_path) as f:
        lines = f.readlines()
    bak = f"{config_path}.bak-tunewrite-{datetime.datetime.now():%Y%m%d-%H%M%S}"
    with open(bak, "w") as f:
        f.writelines(lines)

    in_rules = False
    replaced = False
    out = []
    for ln in lines:
        stripped = ln.rstrip("\n")
        # track top-level block headers (no leading whitespace, ends with ':')
        if re.match(r"^\S.*:\s*$", stripped):
            in_rules = (stripped.strip() == "rules:")
        if in_rules and re.match(r"^\s+exit_slippage_floor:\s*", ln) and not replaced:
            indent = ln[:len(ln) - len(ln.lstrip())]
            out.append(f"{indent}exit_slippage_floor: {value}\n")
            replaced = True
            continue
        out.append(ln)
    if not replaced:
        return False, bak  # no existing key found; do NOT blindly append (surface the miss)
    with open(config_path, "w") as f:
        f.writelines(out)
    return True, bak


def render(proposal):
    p = proposal
    ev = p["evidence"]
    L = []
    L.append("EXIT-FLOOR TUNER (PROPOSE-ONLY)")
    L.append(f"  fill_quality verdict : {ev['fill_quality_verdict']}")
    L.append(f"  evidence             : n_fills={ev['n_fills']}  unfilled={ev['n_unfilled']}  "
             f"fill_rate={ev['fill_rate']}  median_giveup={ev['median_giveup_pct']}%  "
             f"p90_giveup={ev['p90_giveup_pct']}%")
    L.append(f"  current floor        : {p['current_floor']}")
    L.append(f"  ACTION               : {p['action']}  ->  "
             f"exit_slippage_floor = {p['recommended_exit_slippage_floor']} "
             f"({'CHANGE' if p['changed'] else 'no change'})")
    L.append(f"  why                  : {p['reason']}")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="PROPOSE-ONLY exit slippage-floor tuner (reads fill_quality JSON).")
    ap.add_argument("--dataset", default=None, help="trade_dataset.jsonl (default: fill_quality's default)")
    ap.add_argument("--report-json", default=None, help="use a saved fill_quality --json file instead of rebuilding")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="config.yaml (source of the current floor)")
    ap.add_argument("--min-fills", type=int, default=5, help="min portfolio fills before a change is proposed")
    ap.add_argument("--json", action="store_true", help="emit the proposal as JSON")
    ap.add_argument("--write", action="store_true",
                    help="OPT-IN: stage the recommended floor into config.yaml (default OFF -- propose only)")
    args = ap.parse_args(argv)

    report = load_report(dataset=args.dataset, report_json=args.report_json, min_fills=args.min_fills)
    current = _current_floor_from_config(args.config)
    proposal = recommend(report, current, min_fills=args.min_fills)

    wrote = False
    if args.write:
        if not proposal["changed"]:
            proposal["write_note"] = "nothing to write (recommendation == current floor)"
        else:
            ok, bak = stage_into_config(args.config, proposal["recommended_exit_slippage_floor"])
            wrote = ok
            proposal["write_note"] = (f"staged into {args.config} (backup {bak}); "
                                      f"SEAM: also wire exit_slippage_floor=cfg.rules.exit_slippage_floor "
                                      f"in manager.py to reach a live OrderManager"
                                      if ok else f"NO exit_slippage_floor key found in {args.config}; not written")

    if args.json:
        print(json.dumps(proposal, indent=2, default=str))
    else:
        print(render(proposal))
        if args.write:
            print(f"  write                : {proposal.get('write_note')}")
        else:
            print("  (propose-only; re-run with --write to stage into config.yaml)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
