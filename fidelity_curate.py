#!/usr/bin/env python3
"""Public API contract; production-derived narrative omitted."""
from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import glob
import itertools
import json
import os
import re
import sys
import tempfile

APP_DIR = os.path.expanduser("~/exitmgr-app")



















DEFAULT_EXPORT_GLOB = os.path.expanduser("~/fidelity-exports/Accounts_History*.csv")
DEFAULT_OUT = os.path.join(APP_DIR, "data", "fidelity_gold_trades.jsonl")



TCC_PROTECTED = ("/Downloads", "/Desktop", "/Documents", "/Library/Mobile Documents")





SCHEMA = [
    "source", "manual", "reasoning_available", "strategy", "account", "acct_no", "ticker",
    "right", "strike", "expiry", "direction", "entry_date", "exit_date", "hold_days",
    "dte_at_entry", "dte_at_close", "contracts", "entry_px", "exit_px", "cost_basis",
    "proceeds", "realized_pnl", "outcome_pct", "close_type", "mfe_pct", "mae_pct",
    "max_drawdown_pct",
]

MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}






RIGHT_RE = re.compile(r"\b(CALL|PUT)\s*\(([A-Z][A-Z0-9.]{0,5})\)")


TERMS_RE = re.compile(r"(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\s+"
                      r"(\d{1,2})\s+(\d{2})\s+\$\s*([0-9,]*\.?[0-9]+)")

OCC_RE = re.compile(r"^-([A-Z][A-Z0-9.]{0,5})(\d{6})([CP])([0-9]*\.?[0-9]+)$")
DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")


ASOF_ISO_RE = re.compile(r"as of (\d{4})-(\d{2})-(\d{2})", re.I)
ASOF_MON_RE = re.compile(r"as of ([A-Z][a-z]{2})-(\d{2})-(\d{4})", re.I)



SHS_RE = re.compile(r"\((\d+)\s+SHS\)")






EQUITY_LEG_RE = re.compile(r"\b(ASSIGNED|EXERCISED)\s+(PUTS|CALLS)\b")


class Anomaly(Exception):
    pass


def blocking(anomalies):
    """Public API contract; production-derived narrative omitted."""
    return [a for a in anomalies if a.get("severity", "block") == "block"]


def notes(anomalies):
    return [a for a in anomalies if a.get("severity", "block") == "note"]


def _f(v, default=0.0):
    """Public API contract; production-derived narrative omitted."""
    s = (v or "").replace(",", "").replace("$", "").strip().strip('"').strip()
    if not s:
        return default
    return float(s)


def parse_export(path):
    """Public API contract; production-derived narrative omitted."""
    with open(path, encoding="utf-8-sig") as f:
        lines = f.read().splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.startswith("Run Date,Account"))
    except StopIteration:
        raise Anomaly("%s: no 'Run Date,Account' header found" % os.path.basename(path))
    out = []
    for r in csv.DictReader(lines[start:]):
        if not DATE_RE.match((r.get("Run Date") or "").strip()):
            break
        out.append(r)
    return out


def normalize_action(action):
    """Public API contract; production-derived narrative omitted."""
    a = re.sub(r"\s+", " ", action or "").strip()
    a = ASOF_ISO_RE.sub(lambda m: "as of %s-%s-%s" % m.groups(), a)

    def _mon(m):
        mon = MONTHS.get(m.group(1).upper())
        return "as of %s-%02d-%s" % (m.group(3), mon, m.group(2)) if mon else m.group(0)

    return ASOF_MON_RE.sub(_mon, a)


def contract_from_action(action):
    """Public API contract; production-derived narrative omitted."""
    rm = RIGHT_RE.search(action)
    if not rm:
        return None
    right_word, ticker = rm.group(1), rm.group(2)
    tm = TERMS_RE.search(action, rm.end())
    if not tm:
        raise Anomaly("option row with no parseable expiry/strike: %r" % action[:160])
    mon, day, yy, strike = tm.groups()
    expiry = dt.date(2000 + int(yy), MONTHS[mon], int(day))
    return ticker, right_word[0], expiry, float(strike.replace(",", ""))


def check_symbol(symbol, contract):
    """Public API contract; production-derived narrative omitted."""
    m = OCC_RE.match((symbol or "").strip())
    if not m:
        return None
    tick, ymd, right, strike = m.groups()
    got = (tick, right, dt.date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:6])), float(strike))
    if got != contract:
        return "symbol %s says %s but action text says %s" % (symbol, got, contract)
    return None




def classify(action_u):
    """Public API contract; production-derived narrative omitted."""
    if "DISTRIBUTION" in action_u and ("SPLIT" in action_u or "REVERSE" in action_u):
        return "split"
    if "CXL" in action_u or "CANCEL" in action_u:
        return "cancel"
    if "ASSIGNED" in action_u:
        return "assigned"
    if "EXERCISED" in action_u:
        return "exercised"
    if "EXPIRED" in action_u:
        return "expired"
    if "OPENING TRANSACTION" in action_u:
        return "open"
    if "CLOSING TRANSACTION" in action_u:
        return "close"
    return None


def load_transactions(paths):
    """Public API contract; production-derived narrative omitted."""
    anomalies = []
    best = collections.Counter()
    sample = {}
    amount = {}

    for path in sorted(paths, key=lambda p: (os.path.getmtime(p), p)):
        try:
            rows = parse_export(path)
        except Anomaly as e:
            anomalies.append({"kind": "unreadable_export", "detail": str(e), "file": path})
            continue
        here = collections.Counter()
        for r in rows:
            act = normalize_action(r.get("Action"))
            kind = classify(act.upper())
            if kind is None:
                continue
            try:
                contract = contract_from_action(act)
            except Anomaly as e:
                anomalies.append({"kind": "unparseable_option_row", "detail": str(e),
                                  "file": os.path.basename(path),
                                  "run_date": (r.get("Run Date") or "").strip()})
                continue
            if contract is None:




                if kind == "split":
                    continue
                if EQUITY_LEG_RE.search(act.upper()):
                    continue
                anomalies.append({"kind": "option_event_without_contract",
                                  "detail": act[:200], "file": os.path.basename(path)})
                continue
            shs = SHS_RE.search(act)
            mult = int(shs.group(1)) if shs else 100






















            key = (
                (r.get("Run Date") or "").strip(),
                (r.get("Account Number") or "").strip(),
                kind, contract, mult,
                round(_f(r.get("Quantity")), 6),
                round(_f(r.get("Price")), 6),
            )
            here[key] += 1
            sample[key] = r
            amount[key] = round(_f(r.get("Amount")), 6)
        for k, n in here.items():
            if n > best[k]:
                best[k] = n

    txns = []
    for key, n in best.items():
        (rd, acct_no, kind, contract, mult, qty, px) = key
        amt = amount[key]
        acct_name = (sample[key].get("Account") or "").strip()
        act = normalize_action(sample[key].get("Action"))
        if mult != 100:
            anomalies.append({"kind": "non_standard_multiplier", "detail":
                              "%s SHS on %s" % (mult, act[:120])})
        sym_err = check_symbol(sample[key].get("Symbol"), contract)
        if sym_err:
            anomalies.append({"kind": "symbol_terms_mismatch", "detail": sym_err,
                              "run_date": rd})
        for _ in range(n):
            txns.append({
                "date": dt.datetime.strptime(rd, "%m/%d/%Y").date(),
                "acct_no": acct_no, "account": acct_name,
                "ticker": contract[0], "right": contract[1],
                "expiry": contract[2], "strike": contract[3],
                "mult": mult, "kind": kind, "qty": qty, "px": px, "amt": amt,
                "action": act,
            })


    order = {"open": 0, "split": 1, "cancel": 2, "close": 3,
             "exercised": 4, "assigned": 5, "expired": 6}
    txns.sort(key=lambda t: (t["date"], order.get(t["kind"], 9), t["ticker"], t["strike"]))
    return txns, anomalies




def resolve_splits(txns, anomalies):
    """Public API contract; production-derived narrative omitted."""
    by_event = collections.defaultdict(list)
    for t in txns:
        if t["kind"] == "split":
            by_event[(t["date"], t["acct_no"], t["ticker"])].append(t)
    if not by_event:
        return txns, {}

    transfers = {}
    drop = set()
    for (date, acct_no, ticker), legs in sorted(by_event.items()):





        held = collections.defaultdict(float)
        for t in txns:
            if t["kind"] != "split" and t["acct_no"] == acct_no and t["date"] < date:
                held[(t["ticker"], t["right"], t["strike"], t["expiry"])] += t["qty"]
        outgoing, incoming = [], []
        for l in legs:
            pos = held.get((l["ticker"], l["right"], l["strike"], l["expiry"]), 0.0)

            if abs(pos) > 1e-9 and abs(pos + l["qty"]) < abs(pos) - 1e-9:
                outgoing.append(l)
            else:
                incoming.append(l)
        if len(outgoing) != len(incoming) or not outgoing:
            anomalies.append({"kind": "unpaired_corporate_action",
                              "detail": "%s %s on %s: %d outgoing vs %d incoming legs -- NOT "
                                        "applied, left for manual review"
                                        % (ticker, "split", date, len(outgoing), len(incoming)),
                              "legs": [l["action"][:110] for l in legs]})
            continue
        used = set()
        for old in outgoing:
            match = None
            for i, new in enumerate(incoming):
                if i in used or new["expiry"] != old["expiry"]:
                    continue
                if abs(new["qty"]) % abs(old["qty"]) == 0 and abs(new["qty"]) >= abs(old["qty"]):
                    match = (i, new)
                    break
            if match is None:
                anomalies.append({"kind": "unpaired_corporate_action",
                                  "detail": "%s %s on %s: no incoming leg matches outgoing %s "
                                            "x%g -- NOT applied"
                                            % (ticker, "split", date, old["strike"], old["qty"]),
                                  "legs": [l["action"][:110] for l in legs]})
                continue
            i, new = match
            used.add(i)
            ratio = abs(new["qty"]) / abs(old["qty"])
            okey = (acct_no, ticker, old["right"], old["strike"], old["expiry"])
            nkey = (acct_no, ticker, new["right"], new["strike"], new["expiry"])
            transfers.setdefault(nkey, []).append({"from": okey, "ratio": ratio, "date": date,
                                                   "detail": "%s %g -> %s %g (%gx) on %s"
                                                   % (old["strike"], old["qty"], new["strike"],
                                                      new["qty"], ratio, date)})
            drop.add(id(old))
            drop.add(id(new))
    kept = [t for t in txns if id(t) not in drop]
    stranded = [t for t in kept if t["kind"] == "split"]
    for t in stranded:
        anomalies.append({"kind": "unapplied_corporate_action", "detail": t["action"][:180]})
    return [t for t in kept if t["kind"] != "split"], transfers


























CURATED_ACCOUNTS = {"100000001", "X00000000", "100000002"}


def scope_to_curated_accounts(txns, anomalies):
    """Public API contract; production-derived narrative omitted."""
    if CURATED_ACCOUNTS is None:
        return txns
    kept, dropped = [], {}
    for t in txns:




        acct = next((str(t[k]).strip() for k in ("acct_no", "account_number")
                     if t.get(k) is not None), "")
        if not acct or acct in CURATED_ACCOUNTS:
            kept.append(t)
        else:
            dropped.setdefault(acct, {"n": 0, "label": t.get("account") or "?"})
            dropped[acct]["n"] += 1
    for acct, d in sorted(dropped.items()):
        anomalies.append({
            "kind": "account_out_of_scope",



            "severity": "note",
            "detail": "%s (%s): %d transactions excluded by CURATED_ACCOUNTS"
                      % (acct, d["label"], d["n"]),
        })
    return kept




def build_campaigns(txns, transfers, anomalies):
    """Public API contract; production-derived narrative omitted."""
    groups = collections.defaultdict(list)
    for t in txns:
        groups[(t["acct_no"], t["ticker"], t["right"], t["strike"], t["expiry"])].append(t)

    campaigns = []
    carried = {}



    order, seen = [], set()

    def visit(k):
        if k in seen:
            return
        seen.add(k)
        for src in transfers.get(k, []):
            visit(src["from"])
        order.append(k)

    for k in sorted(groups, key=lambda k: (k[0], k[1], k[4], k[3], k[2])):
        visit(k)
    for k in sorted(transfers):
        visit(k)

    for key in order:
        rows = sorted(groups.get(key, []), key=lambda t: (t["date"], t["kind"] != "open"))
        acct_no, ticker, right, strike, expiry = key
        cur = carried.pop(key, None)
        last = None
        for day, day_rows in itertools.groupby(rows, key=lambda t: t["date"]):
            for t in day_rows:
                if cur is None:
                    started_by_close = t["kind"] == "close"
                    if t["kind"] == "cancel" and last is not None:





                        cur = last
                        campaigns.remove(last)
                        last = None
                    elif not opens_position(t, 0.0):


                        anomalies.append({
                            "kind": "unmatched_close", "severity": "block",
                            "detail": "%s %s %s %s %s: %s of %g with no open position"
                                      % (acct_no, ticker, right, strike, expiry,
                                         t["kind"], t["qty"]),
                            "date": str(t["date"]), "action": t["action"][:160]})
                        continue
                    else:
                        cur = new_campaign(t, key)







                        cur["started_by_close"] = started_by_close
                apply_txn(cur, t)







            if cur is not None and abs(cur["net"]) < 1e-9:
                campaigns.append(cur)
                last = cur
                cur = None
        if cur is None:
            continue




        dest = [(d, s["ratio"]) for d, srcs in transfers.items()
                for s in srcs if s["from"] == key]
        if dest:
            d, ratio = dest[0]
            carried[d] = rescale(cur, d, ratio)
            continue
        campaigns.append(cur)

    for key, c in carried.items():
        anomalies.append({"kind": "corporate_action_dead_end", "severity": "block",
                          "detail": "basis carried to %s but that contract has no transactions "
                                    "of its own" % (key,)})
        campaigns.append(c)




    for c in campaigns:
        if not c.get("started_by_close"):
            continue
        reconciled = abs(c["net"]) < 1e-9
        anomalies.append({
            "kind": "closing_label_opened_position" if reconciled else "unmatched_close",
            "severity": "note" if reconciled else "block",
            "detail": "%s %s %s %s %s entered %s on a fill Fidelity labelled CLOSING; %s"
                      % (c["acct_no"], c["ticker"], c["right"], c["strike"], c["expiry"],
                         c["entry_date"],
                         "the position ran back to flat, so the label was wrong and the trade "
                         "is complete" if reconciled else
                         "the position never returned to flat -- the OPENING FILL IS MISSING "
                         "from every export and this trade's cost basis is understated")})
    return campaigns


def rescale(c, dest_key, ratio):
    """Public API contract; production-derived narrative omitted."""
    acct_no, ticker, right, strike, expiry = dest_key
    out = dict(c)
    out.update({"key": dest_key, "ticker": ticker, "right": right, "strike": strike,
                "expiry": expiry, "from_corporate_action": True,
                "net": c["net"] * ratio,
                "open_qty": c["open_qty"] * ratio,
                "close_qty": c["close_qty"] * ratio})
    return out


def new_campaign(t, key):
    acct_no, ticker, right, strike, expiry = key
    return {
        "key": key, "acct_no": acct_no, "account": t["account"], "ticker": ticker,
        "right": right, "strike": strike, "expiry": expiry,
        "entry_date": t["date"], "exit_date": None,
        "net": 0.0, "open_qty": 0.0, "close_qty": 0.0,
        "cost_basis": 0.0, "proceeds": 0.0,
        "open_notional": 0.0, "close_notional": 0.0,
        "terminal": None, "from_corporate_action": False,
    }


TERMINAL = ("expired", "assigned", "exercised")


def opens_position(t, net):
    """Public API contract; production-derived narrative omitted."""
    if t["kind"] in TERMINAL:
        return False
    if abs(net) < 1e-9:
        return True
    return (net > 0) == (t["qty"] > 0)


def apply_txn(c, t):
    net = c["net"]
    c["net"] += t["qty"]
    if t["kind"] == "cancel":


        c["proceeds"] += t["amt"]
        c["close_qty"] -= abs(t["qty"])
        c["close_notional"] -= abs(t["qty"]) * t["px"] * t["mult"]
    elif opens_position(t, net):
        c["cost_basis"] += t["amt"]
        c["open_qty"] += abs(t["qty"])
        c["open_notional"] += abs(t["qty"]) * t["px"] * t["mult"]
    elif t["kind"] not in TERMINAL:
        c["proceeds"] += t["amt"]
        c["close_qty"] += abs(t["qty"])
        c["close_notional"] += abs(t["qty"]) * t["px"] * t["mult"]
        c["exit_date"] = t["date"]
    else:



        c["proceeds"] += t["amt"]
        c["close_qty"] += abs(t["qty"])
        c["exit_date"] = t["date"]
        c["terminal"] = t["kind"]


def finalize(c, as_of):
    """Public API contract; production-derived narrative omitted."""
    strategy = "short_premium" if c["cost_basis"] > 0 else "long_debit"
    long_side = c["cost_basis"] < 0
    if c["right"] == "C":
        direction = "bullish" if long_side else "bearish"
    else:
        direction = "bearish" if long_side else "bullish"

    open_qty = c["open_qty"]
    entry_px = round(c["open_notional"] / (open_qty * 100), 4) if open_qty else None
    closed_out = abs(c["net"]) < 1e-9

    if not closed_out:
        close_type = "open"
    elif c["terminal"] == "assigned":
        close_type = "assigned"
    elif c["terminal"] == "exercised":
        close_type = "exercised"
    elif c["terminal"] == "expired":
        close_type = "expired"
    else:
        close_type = "closed"

    if not closed_out and c["expiry"] < as_of:


        close_type = "expired_unresolved"

    exit_date = c["exit_date"] if close_type not in ("open", "expired_unresolved") else None
    if close_type in ("expired", "assigned", "exercised"):
        exit_px = 0.0
    elif c["close_qty"] > 1e-9:
        exit_px = round(c["close_notional"] / (c["close_qty"] * 100), 4)
    else:
        exit_px = None

    if close_type != "open":



        basis = c["cost_basis"]
        realized = round(basis + c["proceeds"], 2)
    elif c["close_qty"] > 1e-9:










        basis = c["cost_basis"] * (c["close_qty"] / open_qty) if open_qty else 0.0
        realized = round(basis + c["proceeds"], 2)
    else:

        basis, realized = None, None

    outcome = (round(realized / abs(basis) * 100, 2) if realized is not None and basis else None)
    hold = (exit_date - c["entry_date"]).days if exit_date else None
    dte_close = ((c["expiry"] - exit_date).days if exit_date
                 else (c["expiry"] - as_of).days)

    return {
        "source": "fidelity_manual", "manual": True, "reasoning_available": False,
        "strategy": strategy, "account": c["account"], "acct_no": c["acct_no"],
        "ticker": c["ticker"], "right": c["right"], "strike": float(c["strike"]),
        "expiry": c["expiry"].isoformat(), "direction": direction,
        "entry_date": c["entry_date"].isoformat(),
        "exit_date": exit_date.isoformat() if exit_date else None,
        "hold_days": hold,
        "dte_at_entry": (c["expiry"] - c["entry_date"]).days,
        "dte_at_close": dte_close,
        "contracts": float(round(open_qty, 6)),
        "entry_px": entry_px, "exit_px": exit_px,
        "cost_basis": round(c["cost_basis"], 2),
        "proceeds": round(c["proceeds"], 2),
        "realized_pnl": realized, "outcome_pct": outcome, "close_type": close_type,
        "mfe_pct": None, "mae_pct": None, "max_drawdown_pct": None,
    }


def curate(export_glob=DEFAULT_EXPORT_GLOB, as_of=None, until=None):
    """Public API contract; production-derived narrative omitted."""
    as_of = as_of or dt.date.today()
    home = os.path.expanduser("~")
    d = os.path.dirname(os.path.abspath(export_glob))
    if any(d == home + p or d.startswith(home + p + "/") for p in TCC_PROTECTED):
        raise Anomaly(
            "refusing to read exports from %s: macOS guards that directory with a consent "
            "prompt, and a scheduled job opening it HANGS IN open() instead of failing. Use "
            "~/fidelity-exports." % d)
    paths = sorted(glob.glob(export_glob))
    if not paths:
        raise Anomaly("no exports matched %s" % export_glob)
    txns, anomalies = load_transactions(paths)
    if until:
        txns = [t for t in txns if t["date"] <= until]
    txns, transfers = resolve_splits(txns, anomalies)
    txns = scope_to_curated_accounts(txns, anomalies)
    campaigns = build_campaigns(txns, transfers, anomalies)
    rows = [finalize(c, as_of) for c in campaigns]
    rows.sort(key=lambda r: (r["entry_date"], r["ticker"], r["expiry"], r["strike"], r["right"]))
    return rows, anomalies, txns, paths


def realised_split(rows):
    """Public API contract; production-derived narrative omitted."""
    closed = sum(r["realized_pnl"] or 0 for r in rows if r["close_type"] != "open")
    partial = sum(r["realized_pnl"] or 0 for r in rows if r["close_type"] == "open")
    return {
        "closed_trades": round(closed, 2),
        "partial_exits_of_open_positions": round(partial, 2),
        "total": round(closed + partial, 2),
        "open_positions_still_held": sum(1 for r in rows if r["close_type"] == "open"),
    }


def write_atomic(rows, out_path):
    """Public API contract; production-derived narrative omitted."""
    d = os.path.dirname(out_path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".fidelity_gold_trades.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            for r in rows:
                f.write(json.dumps({k: r[k] for k in SCHEMA}) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, out_path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--exports", default=DEFAULT_EXPORT_GLOB)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--as-of", default=None, help="YYYY-MM-DD; default today")
    ap.add_argument("--until", default=None,
                    help="YYYY-MM-DD; ignore transactions after this date (replay/control)")
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    ap.add_argument("--json-report", default=None)
    a = ap.parse_args(argv)
    as_of = dt.date.fromisoformat(a.as_of) if a.as_of else dt.date.today()

    until = dt.date.fromisoformat(a.until) if a.until else None
    rows, anomalies, txns, paths = curate(a.exports, as_of, until)
    newest_txn = max((t["date"] for t in txns), default=None)
    report = {
        "exports": len(paths), "transactions": len(txns), "trades": len(rows),
        "newest_transaction": newest_txn.isoformat() if newest_txn else None,
        "blocking_anomalies": len(blocking(anomalies)),
        "notes": len(notes(anomalies)),
        "anomalies": anomalies,
        "by_close_type": dict(collections.Counter(r["close_type"] for r in rows)),
        "realised": realised_split(rows),
    }
    if not a.dry_run:
        write_atomic(rows, a.out)
        report["written"] = a.out
    if a.json_report:
        with open(a.json_report, "w") as f:
            json.dump(report, f, indent=1, default=str)
    print(json.dumps({k: v for k, v in report.items() if k != "anomalies"}, indent=1))
    if anomalies:
        print("\nANOMALIES (%d blocking, %d notes) -- surfaced, never dropped:"
              % (len(blocking(anomalies)), len(notes(anomalies))), file=sys.stderr)
        for x in anomalies[:40]:
            print("  [%-5s] %-32s %s" % (x.get("severity", "block"), x.get("kind"),
                                         str(x.get("detail"))[:150]), file=sys.stderr)
    return 1 if blocking(anomalies) else 0


if __name__ == "__main__":
    sys.exit(main())
