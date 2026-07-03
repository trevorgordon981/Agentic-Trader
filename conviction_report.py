#!/usr/bin/env python
"""Conviction -> outcome report for the exitmgr options bot.

Reads the JSONL journal (trades.log) -- each entry now carries the entry `conviction`
(1-10) -- and correlates every closed position to its realized P&L from IBKR fills.
Prints a per-trade table and a conviction-bucket summary so you can answer:
"do high-conviction trades actually win?"

READ-ONLY. Places no orders. Uses ib_async on a dedicated clientId (85).

Run:  ~/ib-grader-venv/bin/python ~/exitmgr-app/conviction_report.py
      [--journal trades.log] [--host 127.0.0.1] [--port 4001] [--no-ib]

Degrades gracefully:
  * trades still open (no closing fill) are marked OPEN and excluded from win-rate math.
  * if IBKR is unreachable (or --no-ib), every trade is marked P&L=n/a and only the
    counts-per-bucket are shown -- the journal half of the report still works.
"""
import argparse
import json
import os
from collections import defaultdict

DEFAULT_JOURNAL = os.path.expanduser("~/exitmgr-app/trades.log")
HOST, PORT, CLIENT_ID = "127.0.0.1", 4001, 85


# ----------------------------------------------------------------------------- buckets
def bucket(conv):
    """Map a conviction score to a labelled bucket. -1/None => unknown (pre-feature trades)."""
    try:
        c = float(conv)
    except (TypeError, ValueError):
        return "unknown"
    if c < 0:
        return "unknown"
    if c >= 8:
        return "high (8-10)"
    if c >= 6:
        return "medium (6-7)"
    if c >= 4:
        return "low (4-5)"
    return "desperate (<4)"


# the order buckets print in (best conviction first), unknown last
BUCKET_ORDER = ["high (8-10)", "medium (6-7)", "low (4-5)", "desperate (<4)", "unknown"]


# ----------------------------------------------------------------------------- journal
def load_journal(path):
    """Return list of entry dicts. One line per entry-trade; tolerate blank/corrupt lines."""
    out = []
    if not os.path.exists(path):
        print(f"[WARN] journal not found: {path}")
        return out
    with open(path) as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                print(f"[WARN] skipping unparseable journal line {i}")
    return out


# ----------------------------------------------------------------------------- IBKR P&L
def realized_by_conid(host, port):
    """Return ({conId: realized_pnl_usd}, {set of conIds with an open position}).

    Sums realizedPNL across every SELL (close) fill per conId, so partial closes add up.
    Returns (None, None) if IBKR can't be reached -- caller treats P&L as unavailable.
    """
    try:
        from ib_async import IB
    except Exception as e:  # ib_async not installed in this interpreter
        print(f"[WARN] ib_async unavailable ({e}); running journal-only")
        return None, None

    ib = IB()
    realized = defaultdict(float)
    closed_any = set()
    open_conids = set()
    try:
        ib.connect(host, port, clientId=CLIENT_ID, timeout=15)
    except Exception as e:
        print(f"[WARN] could not connect to IBKR at {host}:{port} ({e}); running journal-only")
        return None, None
    try:
        ib.reqExecutions()  # populates ib.fills() with commissionReport.realizedPNL
        for fl in ib.fills():
            ex = fl.execution
            con = fl.contract
            cid = getattr(con, "conId", None)
            if cid is None:
                continue
            if ex.side == "SLD":  # a closing leg
                rp = getattr(getattr(fl, "commissionReport", None), "realizedPNL", None)
                if rp is not None and rp == rp:  # not NaN
                    realized[cid] += float(rp)
                    closed_any.add(cid)
        for p in ib.reqPositions():
            if p.position != 0:
                cid = getattr(p.contract, "conId", None)
                if cid is not None:
                    open_conids.add(cid)
    finally:
        ib.disconnect()

    # only keep realized for conIds that have actually closed (and aren't still open)
    result = {cid: realized[cid] for cid in closed_any if cid not in open_conids}
    return result, open_conids


# ----------------------------------------------------------------------------- report
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--journal", default=DEFAULT_JOURNAL)
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--no-ib", action="store_true", help="skip IBKR; journal counts only")
    args = ap.parse_args()

    entries = load_journal(args.journal)
    if not entries:
        print("No journal entries. Nothing to report.")
        return 0

    if args.no_ib:
        realized, open_conids = None, set()
    else:
        realized, open_conids = realized_by_conid(args.host, args.port)
    have_pnl = realized is not None
    open_conids = open_conids or set()

    # ---- per-trade rows
    rows = []
    for e in entries:
        cid = e.get("contract_id")
        debit = float(e.get("debit") or 0) or None
        conv = e.get("conviction", -1)
        status, pnl_usd, pnl_pct = "n/a", None, None
        if have_pnl:
            if cid in open_conids:
                status = "OPEN"
            elif cid in realized:
                pnl_usd = realized[cid]
                pnl_pct = (pnl_usd / debit * 100.0) if debit else None
                status = "WIN" if pnl_usd > 0 else ("LOSS" if pnl_usd < 0 else "FLAT")
            else:
                status = "OPEN?"  # in journal, no close fill seen and not currently held
        rows.append({
            "ts": (e.get("ts") or "")[:16],
            "symbol": e.get("symbol", "?"),
            "right": e.get("right", "?"),
            "strike": e.get("strike", ""),
            "spread": "Y" if e.get("spread") else "",
            "conv": conv,
            "debit": debit,
            "status": status,
            "pnl_usd": pnl_usd,
            "pnl_pct": pnl_pct,
        })

    # ---- per-trade table
    print(f"\nTrades from {args.journal}  ({len(rows)} entries)\n")
    hdr = f"{'date':16} {'sym':6} {'r':1} {'strike':>8} {'sp':2} {'conv':>4} " \
          f"{'debit':>8} {'status':6} {'P&L$':>9} {'P&L%':>7}"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        pnl_usd = f"{r['pnl_usd']:+,.0f}" if r["pnl_usd"] is not None else "-"
        pnl_pct = f"{r['pnl_pct']:+.0f}%" if r["pnl_pct"] is not None else "-"
        debit = f"{r['debit']:,.0f}" if r["debit"] else "-"
        strike = f"{r['strike']:g}" if isinstance(r["strike"], (int, float)) else str(r["strike"])
        print(f"{r['ts']:16} {r['symbol']:6} {r['right']:1} {strike:>8} {r['spread']:2} "
              f"{str(r['conv']):>4} {debit:>8} {r['status']:6} {pnl_usd:>9} {pnl_pct:>7}")

    # ---- bucket summary
    agg = defaultdict(lambda: {"n": 0, "closed": 0, "wins": 0, "open": 0,
                               "pnl_pcts": [], "pnl_usd": 0.0})
    for r in rows:
        b = bucket(r["conv"])
        a = agg[b]
        a["n"] += 1
        if r["status"] in ("OPEN", "OPEN?"):
            a["open"] += 1
        elif r["status"] in ("WIN", "LOSS", "FLAT"):
            a["closed"] += 1
            if r["status"] == "WIN":
                a["wins"] += 1
            if r["pnl_pct"] is not None:
                a["pnl_pcts"].append(r["pnl_pct"])
            if r["pnl_usd"] is not None:
                a["pnl_usd"] += r["pnl_usd"]

    print("\nConviction bucket -> outcome\n")
    if not have_pnl:
        print("  (IBKR P&L unavailable -- showing trade counts per bucket only)\n")
    hdr2 = f"{'bucket':16} {'trades':>6} {'closed':>6} {'open':>5} " \
           f"{'win%':>6} {'avg P&L%':>9} {'total $':>10}"
    print(hdr2)
    print("-" * len(hdr2))
    for b in BUCKET_ORDER:
        if b not in agg:
            continue
        a = agg[b]
        win = f"{(a['wins'] / a['closed'] * 100):.0f}%" if a["closed"] else "-"
        avg = f"{(sum(a['pnl_pcts']) / len(a['pnl_pcts'])):+.0f}%" if a["pnl_pcts"] else "-"
        tot = f"{a['pnl_usd']:+,.0f}" if a["closed"] else "-"
        print(f"{b:16} {a['n']:>6} {a['closed']:>6} {a['open']:>5} "
              f"{win:>6} {avg:>9} {tot:>10}")

    # ---- one-line verdict on the actual question
    hi = agg.get("high (8-10)")
    lo_keys = [k for k in ("low (4-5)", "desperate (<4)") if k in agg]
    if have_pnl and hi and hi["closed"]:
        hi_win = hi["wins"] / hi["closed"] * 100
        lo_closed = sum(agg[k]["closed"] for k in lo_keys)
        lo_wins = sum(agg[k]["wins"] for k in lo_keys)
        lo_win = (lo_wins / lo_closed * 100) if lo_closed else None
        msg = f"\nHigh-conviction (>=8): {hi_win:.0f}% win on {hi['closed']} closed."
        if lo_win is not None:
            msg += f"  Low-conviction (<6): {lo_win:.0f}% win on {lo_closed} closed."
        print(msg)
    elif have_pnl:
        print("\nNot enough closed high-conviction trades yet to judge the edge.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
