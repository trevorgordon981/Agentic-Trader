#!/usr/bin/env python3
"""Public API contract; production-derived narrative omitted."""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import socket
import sys
import traceback

sys.path.insert(0, os.path.expanduser("~/exitmgr-app"))

import fidelity_curate as FC
from exitmgr import alerting

STATE = os.path.expanduser("~/exitmgr-app/.fidelity_freshness_state.json")


CURATION_LAG_DAYS = 2
EXPORT_STALE_DAYS = 14



MIN_ROW_RATIO = 0.90


def read_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {}


def write_state(status, detail):
    try:
        with open(STATE, "w") as f:
            json.dump({"status": status, "detail": detail,
                       "ts": dt.datetime.now().isoformat()}, f, indent=1)
    except Exception as e:
        print("state write failed:", e, file=sys.stderr)


def curated_coverage(path):
    """Public API contract; production-derived narrative omitted."""
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    newest = None
    for r in rows:
        for k in ("entry_date", "exit_date"):
            v = r.get(k)
            if v:
                d = dt.date.fromisoformat(v)
                if newest is None or d > newest:
                    newest = d
    return rows, newest


def slack(msg, label):
    ok = alerting.post(msg, alerting.alerts_channel(), label=label,
                       fallback_channel=alerting.error_channel())
    if not ok:
        print("SLACK DELIVERY FAILED for %s" % label, file=sys.stderr)
    return ok


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--exports", default=FC.DEFAULT_EXPORT_GLOB)
    ap.add_argument("--out", default=FC.DEFAULT_OUT)
    ap.add_argument("--no-write", action="store_true", help="check only; do not re-curate")
    ap.add_argument("--no-slack", action="store_true", help="print instead of posting")
    ap.add_argument("--force-alert", action="store_true",
                    help="post even if the state is unchanged (delivery test)")
    ap.add_argument("--sync-blocked", metavar="REASON", default=None,
                    help="report that the Mac->trader_host export sync cannot run, and exit")
    a = ap.parse_args(argv)

    if a.sync_blocked:




        msg = (":rotating_light: *Fidelity export sync is blocked on the Mac*\n"
               "%s\nNew Fidelity exports are NOT reaching trader_host, so the curated book will "
               "drift out of date. Fix: System Settings > Privacy & Security > Full Disk "
               "Access > add /bin/bash. Meanwhile `~/sync_fidelity_exports.sh` run from a "
               "terminal still works." % a.sync_blocked)
        ok = slack(msg, "fidelity_sync_blocked")
        print("sync-blocked alert delivered=%s" % ok)
        return 0 if ok else 1

    today = dt.date.today()
    host = socket.gethostname().split(".")[0]
    problems, notes = [], []

    prev_rows = 0
    if os.path.exists(a.out):
        try:
            prev_rows = len(curated_coverage(a.out)[0])
        except Exception as e:
            problems.append("curated file is unreadable before the run: %s" % e)


    anomalies, newest_txn, wrote = [], None, False
    try:
        rows, anomalies, txns, paths = FC.curate(a.exports, as_of=today)
        newest_txn = max((t["date"] for t in txns), default=None)
        notes.append("%d exports, %d transactions, %d trades" % (len(paths), len(txns), len(rows)))
        sp = FC.realised_split(rows)
        notes.append("realised: $%s on closed trades + $%s taken off %d still-open positions "
                     "= $%s" % ("{:,.2f}".format(sp["closed_trades"]),
                                "{:,.2f}".format(sp["partial_exits_of_open_positions"]),
                                sp["open_positions_still_held"],
                                "{:,.2f}".format(sp["total"])))
        if a.no_write:
            notes.append("check-only run; file not rewritten")
        elif prev_rows and len(rows) < prev_rows * MIN_ROW_RATIO:
            problems.append("REFUSED TO WRITE: rebuild produced %d trades against %d already on "
                            "disk (below the %.0f%% floor). A parse regression looks exactly "
                            "like this and would overwrite the source of truth."
                            % (len(rows), prev_rows, MIN_ROW_RATIO * 100))
        else:
            FC.write_atomic(rows, a.out)
            wrote = True
    except Exception as e:
        problems.append("curation FAILED: %s: %s" % (type(e).__name__, e))
        traceback.print_exc()


    curated_newest = None
    try:
        on_disk, curated_newest = curated_coverage(a.out)
        notes.append("curated file on disk: %d rows, newest %s" % (len(on_disk), curated_newest))
    except Exception as e:
        problems.append("curated file could not be read back after the run: %s" % e)

    if newest_txn is None:


        try:
            for p in glob.glob(a.exports):
                for r in FC.parse_export(p):
                    d = dt.datetime.strptime(r["Run Date"].strip(), "%m/%d/%Y").date()
                    if newest_txn is None or d > newest_txn:
                        newest_txn = d
        except Exception:
            pass

    if newest_txn is None:
        problems.append("no Fidelity exports found at %s -- there is nothing to curate from"
                        % a.exports)
    else:
        export_age = (today - newest_txn).days
        if export_age > EXPORT_STALE_DAYS:
            problems.append("NO FRESH EXPORT: newest transaction anywhere in ~/Downloads is %s, "
                            "%d days old (threshold %d). Either the manual download stopped or "
                            "the Mac->trader_host sync is broken."
                            % (newest_txn, export_age, EXPORT_STALE_DAYS))
        if curated_newest is not None:
            lag = (newest_txn - curated_newest).days
            notes.append("curation lag %d day(s) behind the newest transaction" % lag)
            if lag > CURATION_LAG_DAYS:
                problems.append("STALE CURATION: the exports carry transactions through %s but "
                                "the curated file only reaches %s -- %d days behind (threshold "
                                "%d). This is the stale curation failure."
                                % (newest_txn, curated_newest, lag, CURATION_LAG_DAYS))

    blockers = FC.blocking(anomalies)
    soft = FC.notes(anomalies)
    if blockers:
        kinds = {}
        for x in blockers:
            kinds[x.get("kind")] = kinds.get(x.get("kind"), 0) + 1
        problems.append("%d ANOMALIES the curator refused to guess at: %s"
                        % (len(blockers),
                           ", ".join("%s x%d" % kv for kv in sorted(kinds.items()))))
        for x in blockers[:10]:
            notes.append("  anomaly: %s -- %s" % (x.get("kind"), str(x.get("detail"))[:180]))
    if soft:



        notes.append("%d benign note(s): %s" % (len(soft),
                     ", ".join(sorted({x.get("kind") for x in soft}))))


    status = "PROBLEM" if problems else "OK"
    prev = read_state().get("status")
    summary = " | ".join(notes)
    for line in notes:
        print(line)
    for p in problems:
        print("PROBLEM: " + p, file=sys.stderr)

    if problems:
        msg = (":rotating_light: *Fidelity curated book needs attention* (%s)\n" % host
               + "\n".join("• " + p for p in problems)
               + "\n_" + summary + "_"
               + "\nManual rerun: `~/exitmgr-app/run_fidelity_curate.sh`")
    else:
        msg = (":white_check_mark: *Fidelity curated book is current again* (%s)\n_%s_"
               % (host, summary))

    should_post = (status != prev) or a.force_alert
    if should_post and (problems or prev == "PROBLEM"):
        if a.no_slack:
            print("\n--- would post to Slack ---\n" + msg)
        else:
            slack(msg, "fidelity_freshness")
    write_state(status, problems or summary)

    print("\nstatus=%s wrote=%s posted=%s" % (status, wrote, should_post and bool(
        problems or prev == "PROBLEM")))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
