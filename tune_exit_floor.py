#!/usr/bin/env python3
"""Public API contract; production-derived narrative omitted."""
import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(HERE, "config.yaml")


FLOOR_MIN = 0.10
FLOOR_MAX = 0.90
LOOSEN_STEP = 0.15


def _current_floor_from_config(config_path):
    """Public API contract; production-derived narrative omitted."""
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
    """Public API contract; production-derived narrative omitted."""
    if report_json:
        with open(report_json) as f:
            return json.load(f)
    sys.path.insert(0, HERE)
    import fill_quality_report as fq
    ds = dataset or fq.DEFAULT_DATASET
    return fq.build_report(ds, min_fills=min_fills)


def recommend(report, current_floor, min_fills=5):
    """Public API contract; production-derived narrative omitted."""
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


        rec = round(min(FLOOR_MAX, current_floor + LOOSEN_STEP), 2)
        return {
            "action": "LOWER",
            "current_floor": current_floor,
            "recommended_exit_slippage_floor": rec,
            "changed": rec != current_floor,
            "reason": (f"TOO_TIGHT: fill-rate {fill_rate:.0%} of protective exits resting/cancelled "
                       f"-- LOWER the floor price (raise exit_slippage_floor {current_floor}->{rec}) "
                       f"so protective closes actually fill"),
            "evidence": evidence,
        }

    if verdict == "TOO_LOOSE":


        rec = suggested if suggested is not None else round(
            max(FLOOR_MIN, current_floor - LOOSEN_STEP), 2)
        rec = round(min(current_floor, max(FLOOR_MIN, rec)), 2)
        return {
            "action": "RAISE",
            "current_floor": current_floor,
            "recommended_exit_slippage_floor": rec,
            "changed": rec != current_floor,
            "reason": (f"TOO_LOOSE: median give-up {median_giveup}% (p90 {p90_giveup}%) -- RAISE the "
                       f"floor price (tighten exit_slippage_floor {current_floor}->{rec}) so we stop "
                       f"dumping edge into a wide book"),
            "evidence": evidence,
        }


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
    """Public API contract; production-derived narrative omitted."""
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

        if re.match(r"^\S.*:\s*$", stripped):
            in_rules = (stripped.strip() == "rules:")
        if in_rules and re.match(r"^\s+exit_slippage_floor:\s*", ln) and not replaced:
            indent = ln[:len(ln) - len(ln.lstrip())]
            out.append(f"{indent}exit_slippage_floor: {value}\n")
            replaced = True
            continue
        out.append(ln)
    if not replaced:
        return False, bak
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
