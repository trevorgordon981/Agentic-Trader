#!/usr/bin/env python
"""Constant position monitor for the live IBKR options book.

Fires at MARKET OPEN, MARKET CLOSE, and every 30 min in between (launchd
ai.alfred.position-monitor). READ-ONLY: it never places, modifies, or cancels
an order. Its job is to watch every open position and tell Master Gordon how
they're doing — a heartbeat to #trading-positions every run, and an escalation
to #trading-alerts the moment a position's thesis/level looks eroded.

Erosion = level + time signals computed from the journal (trades.log):
  - NEAR STOP : unrealized P&L <= -0.8 * the position's own stop_pct
  - NEAR TARGET: unrealized P&L >= 0.9 * the position's own profit_target_pct
  - EXPIRING  : <= 1 day to expiry (theta cliff / time-stop imminent)
Actual exits remain the exit-manager's job (auto stop/target/time-stop) or
Master Gordon's explicit call — this monitor only watches and reports.

Uses the app's own IBConnection (clientId 96; status=87, quote=86, trader=88,
close=91, daily-rec=93, daily-summary=95 are all taken).
"""
import asyncio
import datetime as dt
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.expanduser("~/exitmgr-app"))
from exitmgr.connection import IBConnection
from exitmgr.account import get_pot_snapshot

HOST, PORT, CLIENT_ID = "127.0.0.1", 4001, 96
JOURNAL = os.path.expanduser("~/exitmgr-app/trades.log")
POSITIONS_CH = "C0XXXXXXXXX"   # #trading-positions  (heartbeat)
ALERTS_CH = "C0XXXXXXXXX"      # #trading-alerts     (escalation)
TREVOR = "U0XXXXXXXXX"

NEAR_STOP_FRAC = 0.8    # flag when uPnL <= -0.8 * stop_pct
NEAR_TARGET_FRAC = 0.9  # flag when uPnL >= 0.9 * profit_target_pct
EXPIRING_DTE = 1        # flag when <= 1 day to expiry


def slack(text, channel):
    try:
        tok = None
        for l in open(os.path.expanduser("~/.hermes/.env")):
            if l.startswith("SLACK_BOT_TOKEN="):
                tok = l.split("=", 1)[1].strip().strip('"').strip("'"); break
        if not tok:
            print("no slack token"); return
        urllib.request.urlopen(urllib.request.Request(
            "https://slack.com/api/chat.postMessage",
            data=json.dumps({"channel": channel, "text": text}).encode(),
            headers={"Authorization": "Bearer " + tok,
                     "Content-Type": "application/json"}), timeout=10)
    except Exception as e:
        print("slack fail:", e)


def load_journal():
    """Most-recent journal entry per long contract_id (positions can reopen)."""
    by_con = {}
    try:
        for line in open(JOURNAL):
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            cid = e.get("contract_id")
            if cid is not None:
                by_con[cid] = e   # later lines overwrite -> newest wins
    except FileNotFoundError:
        pass
    return by_con


def dte(expiry):
    try:
        d = dt.datetime.strptime(str(expiry)[:8], "%Y%m%d").date()
        return (d - dt.date.today()).days
    except Exception:
        return None


async def collect_async():
    conn = IBConnection(HOST, PORT, CLIENT_ID, market_data_type=1)
    if not await conn.connect(retries=1):
        raise RuntimeError("IBKR connect failed")
    ib = conn.ib
    try:
        await asyncio.sleep(1.5)   # let account/portfolio updates arrive
        pot = await get_pot_snapshot(ib)
        rows = await ib.accountSummaryAsync()
        upnl_total = next((r.value for r in rows if r.tag == "UnrealizedPnL"), None)
        positions = [p for p in await ib.reqPositionsAsync() if p.position != 0]
        # portfolio() carries IBKR's own server-side marking (most reliable for
        # option uPnL given delayed/flaky bid-ask).
        port = {p.contract.conId: p for p in ib.portfolio() if p.position != 0}
    finally:
        await conn.disconnect()
    return pot, upnl_total, port, positions


def build_report():
    pot, upnl_total, port, positions = asyncio.run(collect_async())
    jrnl = load_journal()

    live_conids = {p.contract.conId for p in positions}
    seen = set()
    lines, alerts = [], []

    for p in positions:
        c = p.contract
        cid = c.conId
        if cid in seen:
            continue
        # skip short legs of a managed spread -> they're reported under the long leg
        if cid not in jrnl and any(
                (je.get("spread") or {}).get("short_con_id") == cid
                and je.get("contract_id") in live_conids
                for je in jrnl.values()):
            seen.add(cid)
            continue

        e = jrnl.get(cid)
        upnl = port[cid].unrealizedPNL if cid in port else None
        expiry = getattr(c, "lastTradeDateOrContractMonth", "") or ""
        right = getattr(c, "right", "") or ""
        label = f"{c.symbol} {getattr(c,'strike','')}{right}"
        debit = tgt = stop = None

        if e:
            expiry = e.get("expiry", expiry)
            debit = e.get("debit")
            tgt = e.get("profit_target_pct")
            stop = e.get("stop_pct")
            sp = e.get("spread") or {}
            short_con = sp.get("short_con_id")
            if short_con and short_con in port:
                upnl = (upnl or 0.0) + port[short_con].unrealizedPNL
                seen.add(short_con)
            label = (f"{c.symbol} {e.get('strike','')}/{sp.get('short_strike','')}{right} spread"
                     if sp else f"{c.symbol} {e.get('strike','')}{right}")
        seen.add(cid)

        d = dte(expiry)
        pct = (upnl / debit * 100.0) if (upnl is not None and debit) else None
        pieces = [f"*{label}*"]
        if pct is not None:
            pieces.append(f"{pct:+.0f}%")
        if upnl is not None:
            pieces.append(f"(${upnl:+.0f})")
        if d is not None:
            pieces.append(f"{d}DTE")
        if tgt and stop:
            pieces.append(f"[tgt +{tgt:.0f}/stop -{stop:.0f}]")

        flags = []
        if d is not None and d <= EXPIRING_DTE:
            flags.append("EXPIRING")
        if pct is not None and stop and pct <= -NEAR_STOP_FRAC * stop:
            flags.append("NEAR STOP")
        near_target = pct is not None and tgt and pct >= NEAR_TARGET_FRAC * tgt
        if near_target:
            flags.append("NEAR TARGET")
        if flags:
            pieces.append("⚠ " + ", ".join(flags))
            erosion = [f for f in flags if f != "NEAR TARGET"]
            if erosion:
                alerts.append(f"*{label}* — {', '.join(erosion)}: "
                              + (f"{pct:+.0f}% " if pct is not None else "")
                              + (f"{d}DTE" if d is not None else ""))
        lines.append("  ".join(pieces))

    nl = pot.net_liq if pot else None
    av = pot.available_funds if pot else None
    return nl, av, upnl_total, lines, alerts


def main():
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M %Z")
    try:
        nl, av, upnl_total, lines, alerts = build_report()
    except Exception as ex:
        slack(f":warning: position-monitor failed {now}: {type(ex).__name__}: {ex}", ALERTS_CH)
        print("ERROR:", repr(ex))
        return 1

    def money(x):
        try:
            return f"${float(x):,.0f}"
        except (TypeError, ValueError):
            return f"${x}"

    head = (f":satellite_antenna: *Position monitor* — {now}\n"
            f"NetLiq {money(nl)} · Avail {money(av)} · uPnL {money(upnl_total)}")
    body = "\n".join("• " + l for l in lines) if lines else "_No open positions — flat._"
    slack(head + "\n" + body, POSITIONS_CH)

    if alerts:
        msg = (f"<@{TREVOR}> :rotating_light: *Thesis/level erosion — review for exit:*\n"
               + "\n".join("• " + a for a in alerts)
               + "\n\nClose one now: reply here, or I can run "
                 "`close_symbol.py --symbol XXX --confirm`.")
        slack(msg, ALERTS_CH)

    print(head + "\n" + body)
    if alerts:
        print("ALERTS:", alerts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
