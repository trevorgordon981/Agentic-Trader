#!/usr/bin/env python
"""End-of-day trading summary -> Slack. Runs at market close (cron, weekdays). Reports what was
entered today, what was exited (with realized P&L), open positions (unrealized), the day's change,
total account value, and return since inception. Read-only; clientId 95 (no clash with the
trader 88 / status 87 / quote 86 / manual 90 / daily-rec 93 / fill 94)."""
import asyncio
import json
import os
from datetime import datetime

import yaml

from exitmgr.ibkr import IB
from exitmgr import approval

CLIENT_ID = 95


def _desc(c):
    strike = getattr(c, "strike", "") or ""
    right = getattr(c, "right", "") or ""
    exp = getattr(c, "lastTradeDateOrContractMonth", "") or ""
    return f"{c.symbol} {strike:g}{right} {exp}".replace("  ", " ").strip() if strike else c.symbol


async def run():
    cfg = yaml.safe_load(open("config.yaml"))
    tr, ibc = cfg.get("trading", {}), cfg.get("ib", {})
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    channel = tr.get("summary_channel") or tr.get("slack_channel", "")  # #trading-positions
    start_cap = float(tr.get("starting_capital", 1010.0))

    ib = IB()
    await ib.connectAsync(ibc.get("host", "127.0.0.1"), ibc.get("port", 4001),
                          clientId=CLIENT_ID, timeout=15)
    try:
        ib.reqMarketDataType(ibc.get("market_data_type", 1))
        acct = {v.tag: v.value for v in await ib.accountSummaryAsync()}
        netliq = float(acct.get("NetLiquidation", 0) or 0)
        realized = float(acct.get("RealizedPnL", 0) or 0)
        unreal = float(acct.get("UnrealizedPnL", 0) or 0)
        cash = float(acct.get("TotalCashValue", 0) or 0)

        await ib.reqExecutionsAsync()
        today = datetime.now().astimezone().date()
        entered, exited = [], []
        for f in ib.fills():
            if not (f.time and f.time.astimezone().date() == today):
                continue
            ex = f.execution
            d = _desc(f.contract)
            if ex.side == "BOT":
                entered.append(f"• +{ex.shares:g}x {d} @ ${ex.price:.2f}")
            else:
                rp = getattr(getattr(f, "commissionReport", None), "realizedPNL", None)
                rstr = f"  → realized *${rp:+,.0f}*" if (rp is not None and rp == rp and rp != 0) else ""
                exited.append(f"• -{ex.shares:g}x {d} @ ${ex.price:.2f}{rstr}")

        positions = [p for p in await ib.reqPositionsAsync() if p.position != 0]

        base = netliq
        try:
            b = json.load(open(tr.get("baseline_path", "./day_baseline.json")))
            if b:
                base = float(list(b.values())[-1])
        except Exception:
            pass
        day_chg = netliq - base
        day_pct = (day_chg / base * 100.0) if base else 0.0
        # Book performance must be measured against CAPITAL CONTRIBUTED, not a fixed
        # starting balance. With start_cap pinned at $1,010 this line reported +339% on a
        # book that is down 22%, because every deposit was counted as profit.
        _bk = None
        try:
            import sys as _sys
            _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from book_return import book_return as _book_return
            _bk = _book_return(netliq)
            tot_chg = _bk["pnl_dollars"]
            tot_pct = _bk["pnl_pct"]
            start_cap = _bk["net_deposits"]
        except Exception as _e:
            print("[WARN] book_return unavailable (%s); using start_cap" % str(_e)[:60])
            tot_chg = netliq - start_cap
            tot_pct = (tot_chg / start_cap * 100.0) if start_cap else 0.0
        arrow = ":green_circle:" if day_chg >= 0 else ":red_circle:"

        L = [f":bar_chart: *Daily Trading Summary — {today.isoformat()}* {arrow}",
             f"*Account value: ${netliq:,.2f}*  (today {day_chg:+,.0f} / {day_pct:+.1f}%)",
             f"Realized today: *${realized:+,.0f}*  |  Open unrealized: *${unreal:+,.0f}*  |  Cash: ${cash:,.0f}",
             "",
             "*Entered today:*", *(entered or ["  (none)"]),
             "*Exited today:*", *(exited or ["  (none)"]),
             "*Open positions:*"]
        if positions:
            for p in positions:
                L.append(f"  • {abs(p.position):g}x {_desc(p.contract)}  (avg cost ${p.avgCost:.2f})")
        else:
            L.append("  (none — all cash)")
        L.append("")
        tarrow = ":chart_with_upwards_trend:" if tot_chg >= 0 else ":chart_with_downwards_trend:"
        L.append(f"{tarrow} *Vs capital deposited (${start_cap:,.0f}): {tot_chg:+,.0f} / {tot_pct:+.1f}%*")
        if _bk:
            L.append(f"   _time-weighted {_bk['deposit_weighted_pct']:+.1f}% on avg ${_bk['avg_capital_deployed']:,.0f} deployed over {_bk['days_since_first']}d_")

        approval.post_proposal(token, channel, "\n".join(L))
        print("daily summary posted")
        return 0
    finally:
        ib.disconnect()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
