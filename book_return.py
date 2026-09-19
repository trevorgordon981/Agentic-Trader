"""Public API contract; production-derived narrative omitted."""
import datetime as _dt
import json
import os

LEDGER = os.path.expanduser("~/contributions.jsonl")
CAPITAL_TYPES = ("deposit", "withdrawal", "transfer", "contribution")


def load_contributions(path=LEDGER):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def book_return(net_liq, path=LEDGER, as_of=None):
    """Public API contract; production-derived narrative omitted."""
    rows = load_contributions(path)
    deposits = withdrawals = 0.0
    fees = 0.0
    dated = []
    for r in rows:
        kind = str(r.get("type") or r.get("kind") or "").lower()
        amt = float(r.get("amount") or 0.0)
        if kind in CAPITAL_TYPES:
            if amt >= 0:
                deposits += amt
            else:
                withdrawals += -amt
            dated.append((str(r.get("date") or "")[:10], amt))
        else:
            fees += amt

    net_deposits = deposits - withdrawals
    pnl = float(net_liq) - net_deposits
    pct = (pnl / net_deposits * 100.0) if net_deposits else 0.0


    as_of = as_of or _dt.date.today()
    wsum = wcap = 0.0
    first = None
    for d, amt in dated:
        try:
            day = _dt.date.fromisoformat(d)
        except ValueError:
            continue
        first = day if first is None or day < first else first
        days = max((as_of - day).days, 0)
        wsum += amt * days
        wcap += amt
    span = max((as_of - first).days, 1) if first else 1
    avg_capital = wsum / span if span else net_deposits
    wpct = (pnl / avg_capital * 100.0) if avg_capital else 0.0

    return {
        "net_liq": round(float(net_liq), 2),
        "deposits": round(deposits, 2),
        "withdrawals": round(withdrawals, 2),
        "net_deposits": round(net_deposits, 2),
        "fees_and_other": round(fees, 2),
        "pnl_dollars": round(pnl, 2),
        "pnl_pct": round(pct, 2),
        "avg_capital_deployed": round(avg_capital, 2),
        "deposit_weighted_pct": round(wpct, 2),
        "first_contribution": first.isoformat() if first else None,
        "days_since_first": span,
        "n_contributions": len(dated),
    }


def format_book_return(b):
    sign = "+" if b["pnl_dollars"] >= 0 else ""
    return (
        "Book vs capital contributed\n"
        "  deposited      ${deposits:,.2f}"
        "{wd}\n"
        "  net deposits   ${net_deposits:,.2f}\n"
        "  portfolio now  ${net_liq:,.2f}\n"
        "  P&L            {s}${pnl_dollars:,.2f}  ({s}{pnl_pct:.2f}%)\n"
        "  time-weighted  {s}{deposit_weighted_pct:.2f}%  "
        "(avg ${avg_capital_deployed:,.0f} deployed over {days_since_first}d)"
    ).format(s=sign, wd=("" if not b["withdrawals"] else
                         "   withdrawn ${:,.2f}".format(b["withdrawals"])), **b)


if __name__ == "__main__":
    import sys
    nl = float(sys.argv[1]) if len(sys.argv) > 1 else None
    if nl is None:

        nl = 0.0
        p = os.path.expanduser("~/exitmgr-app/audit.jsonl")
        for line in open(p):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("event") == "cycle_start" and r.get("net_liq"):
                nl = float(r["net_liq"])
    b = book_return(nl)
    print(format_book_return(b))
    print()
    print(json.dumps(b, indent=2))
