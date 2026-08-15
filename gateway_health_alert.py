#!/usr/bin/env python3
"""IBKR-gateway readiness check (launchd ai.alfred.ibkr-gateway-alert + ...-intraday).
Does a REAL API connect: if it fails, the gateway is logged-OUT (TCP open but handshake
times out -- IBKR's ~weekly forced 2FA that auto-restart can't bypass) or down, so it posts
a Slack alert to #trading-alerts.

Two schedules share this one script:
  * pre-market (no flag): weekday 6:05 & 6:20 -- catch forced-2FA before the 6:40 slate.
  * intraday (--intraday): every 10 min via StartInterval; self-gates to market hours
    (weekday 06:30-13:05 PT) so it does NOT fire overnight/weekends during IBKR maintenance.

De-dup: a state file makes alerts edge-triggered -- one ':rotating_light:' when the gateway
goes DOWN, one ':white_check_mark:' when it comes back UP. No repeat spam while it stays down.
clientId 930 -- 96 was ALSO position_monitor.py's, and that collision made the
10-min intraday probe lose the race with IBKR error 326 ('client id is already in
use'), which this script could not distinguish from a logged-out gateway. It was
emitting FALSE gateway-DOWN alerts. 930-939 is reserved for health probes
(trader=1, protective=2500+, dd_consider=972, shadow=947)."""
import argparse, asyncio, json, os, sys, urllib.request
from datetime import datetime, time as dtime
sys.path.insert(0, os.path.expanduser('~/exitmgr-app'))
from exitmgr.connection import IBConnection
from exitmgr import alerting

CLIENT_ID = 930
STATE = os.path.expanduser('~/exitmgr-app/.gateway_health_state.json')

def slack(msg):
    """Post to #trading-alerts, verifying delivery. Returns True only if Slack
    said ok:true. Falls back to #error-logs so a problem with the alerts channel
    cannot mute a live-money gateway alarm outright."""
    return alerting.post(msg, alerting.alerts_channel(), label='gateway_health',
                         fallback_channel=alerting.error_channel())

def read_state():
    try:
        with open(STATE) as f: return json.load(f)
    except Exception:
        return {}

def write_state(status):
    try:
        with open(STATE, 'w') as f:
            json.dump({'status': status, 'ts': datetime.now().isoformat()}, f)
    except Exception as e:
        print('state write failed:', e)

def market_open_now():
    n = datetime.now()
    if n.weekday() >= 5:  # Sat/Sun
        return False
    return dtime(6, 30) <= n.time() < dtime(13, 5)

async def probe():
    """Return (healthy: bool, detail: str)."""
    conn = IBConnection(host='127.0.0.1', port=4001, client_id=CLIENT_ID)
    ok = await conn.connect()  # single attempt, 10s handshake timeout
    if not ok:
        return False, 'API connect failed (logged out / unreachable)'
    try:
        accts = conn.ib.managedAccounts()
    except Exception:
        accts = []
    await conn.disconnect()
    if accts:
        return True, 'healthy, accounts: %s' % accts
    return False, 'connected but returned NO accounts'

async def main(intraday):
    if intraday and not market_open_now():
        print('skip: outside market hours'); return 0
    healthy, detail = await probe()
    prev = read_state().get('status')
    if healthy:
        if prev == 'down':
            if slack(':white_check_mark: *IBKR Gateway back UP* and serving accounts. Trading can resume.'):
                print('RECOVERY posted')
            else:
                print('RECOVERY ALERT UNDELIVERED', file=sys.stderr)
        else:
            print('OK:', detail)
        write_state('up'); return 0
    # unhealthy
    if prev != 'down':
        delivered = slack(':rotating_light: *IBKR Gateway DOWN* (%s). Do 2FA / restart now via `~/studio-screen.sh` -- '
              'no trades can fill until it is back. (Likely IBKR forced 2FA; auto-restart cannot bypass it.)' % detail)
        if delivered:
            print('ALERT posted:', detail)
        else:
            # Do NOT record 'down': the de-dup state would suppress every future
            # retry of an alert that was never actually delivered.
            print('GATEWAY DOWN BUT ALERT UNDELIVERED:', detail, file=sys.stderr)
            return 1
    else:
        print('still down (de-duped):', detail)
    write_state('down'); return 1

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--intraday', action='store_true', help='self-gate to market hours; for the 10-min schedule')
    a = ap.parse_args()
    sys.exit(asyncio.run(main(a.intraday)))
