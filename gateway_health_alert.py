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
clientId 96 (no clash with 88/93/95/87/90)."""
import argparse, asyncio, json, os, sys, urllib.request
from datetime import datetime, time as dtime
sys.path.insert(0, os.path.expanduser('~/exitmgr-app'))
from exitmgr.connection import IBConnection

CHANNEL = 'C0XXXXXXXXX'  # #trading-alerts
STATE = os.path.expanduser('~/exitmgr-app/.gateway_health_state.json')

def slack(msg):
    tok = None
    try:
        for l in open(os.path.expanduser('~/.hermes/.env')):
            if l.startswith('SLACK_BOT_TOKEN='):
                tok = l.split('=', 1)[1].strip().strip('"').strip("'"); break
        if not tok: return
        urllib.request.urlopen(urllib.request.Request(
            'https://slack.com/api/chat.postMessage',
            data=json.dumps({'channel': CHANNEL, 'text': msg}).encode(),
            headers={'Authorization': 'Bearer ' + tok, 'Content-Type': 'application/json'}), timeout=10)
    except Exception as e:
        print('slack post failed:', e)

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
    conn = IBConnection(host='127.0.0.1', port=4001, client_id=96)
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
            slack(':white_check_mark: *IBKR Gateway back UP* and serving accounts. Trading can resume.')
            print('RECOVERY posted')
        else:
            print('OK:', detail)
        write_state('up'); return 0
    # unhealthy
    if prev != 'down':
        slack(':rotating_light: *IBKR Gateway DOWN* (%s). Do 2FA / restart now via `~/studio-screen.sh` -- '
              'no trades can fill until it is back. (Likely IBKR forced 2FA; auto-restart cannot bypass it.)' % detail)
        print('ALERT posted:', detail)
    else:
        print('still down (de-duped):', detail)
    write_state('down'); return 1

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--intraday', action='store_true', help='self-gate to market hours; for the 10-min schedule')
    a = ap.parse_args()
    sys.exit(asyncio.run(main(a.intraday)))
