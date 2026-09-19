#!/usr/bin/env python
"""Public API contract; production-derived narrative omitted."""
import asyncio
import datetime as dt
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.expanduser("~/exitmgr-app"))
from ib_async import IB, StartupFetch
from exitmgr.account import get_pot_snapshot
from exitmgr import alerting

HOST, PORT, CLIENT_ID = "127.0.0.1", 4001, 96
JOURNAL = os.path.expanduser("~/exitmgr-app/trades.log")


POSITIONS_CH = alerting.positions_channel()
ALERTS_CH = alerting.alerts_channel()
OPERATOR = sorted(alerting.approver_ids())[0]

NEAR_STOP_FRAC = 0.8
NEAR_TARGET_FRAC = 0.9
EXPIRING_DTE = 1


def slack(text, channel):
    """Public API contract; production-derived narrative omitted."""
    return alerting.post(text, channel, label="position_monitor")


def load_journal():
    """Public API contract; production-derived narrative omitted."""
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
                by_con[cid] = e
    except FileNotFoundError:
        pass
    return by_con


def dte(expiry):
    try:
        d = dt.datetime.strptime(str(expiry)[:8], "%Y%m%d").date()
        return (d - dt.date.today()).days
    except Exception:
        return None




_READ_TIMEOUT_S = 30
_CONNECT_TIMEOUT_S = 30
_RETRY_DELAY_S = 30
_STARTUP_FIELDS = StartupFetch.ACCOUNT_UPDATES | StartupFetch.SUB_ACCOUNT_UPDATES


async def collect_async(ib_factory=None):
    factory = IB if ib_factory is None else ib_factory
    ib = factory()
    ib.RaiseRequestErrors = True
    try:
        for attempt in range(2):
            try:
                await asyncio.wait_for(ib.connectAsync(
                    host=HOST, port=PORT, clientId=CLIENT_ID, timeout=10,
                    readonly=True, fetchFields=_STARTUP_FIELDS,
                    raiseSyncErrors=True), _CONNECT_TIMEOUT_S)
                break
            except Exception:
                ib.disconnect()
                if attempt == 1:
                    raise
                await asyncio.sleep(_RETRY_DELAY_S)
        await asyncio.sleep(1.5)
        pot = await asyncio.wait_for(get_pot_snapshot(ib), _READ_TIMEOUT_S)
        rows = await asyncio.wait_for(ib.accountSummaryAsync(), _READ_TIMEOUT_S)
        upnl_total = next((r.value for r in rows if r.tag == "UnrealizedPnL"), None)
        positions = [p for p in await asyncio.wait_for(
            ib.reqPositionsAsync(), _READ_TIMEOUT_S) if p.position != 0]

        port = {p.contract.conId: p for p in ib.portfolio() if p.position != 0}
        if not ib.isConnected():
            raise RuntimeError("IBKR disconnected during position report")
    finally:
        ib.disconnect()
    return pot, upnl_total, port, positions


def post_position_report(text):
    """Public API contract; production-derived narrative omitted."""
    delivered = alerting.post(text, POSITIONS_CH, label="position_monitor", dedup=False)
    if delivered is True:
        print("POSITION_UPDATE_DELIVERED " + json.dumps({
            "at": dt.datetime.now().astimezone().isoformat(),
            "channel": POSITIONS_CH}, sort_keys=True))
        return True
    print("ERROR: position heartbeat was not acknowledged by Slack; no retry",
          file=sys.stderr)
    return False


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
    delivered = post_position_report(head + "\n" + body)

    if alerts:
        msg = (f"<@{OPERATOR}> :rotating_light: *Thesis/level erosion — review for exit:*\n"
               + "\n".join("• " + a for a in alerts)
               + "\n\nClose one now: reply here, or I can run "
                 "`close_symbol.py --symbol XXX --confirm`.")
        delivered = bool(slack(msg, ALERTS_CH)) and delivered

    print(head + "\n" + body)
    if alerts:
        print("ALERTS:", alerts)
    return 0 if delivered else 1


if __name__ == "__main__":
    raise SystemExit(main())
