#!/usr/bin/env python3
"""Public API contract; production-derived narrative omitted."""
import argparse, asyncio, json, os, sys, urllib.request
from datetime import datetime, time as dtime
sys.path.insert(0, os.path.expanduser('~/exitmgr-app'))
from exitmgr.ibkr import IB
from exitmgr import alerting

CLIENT_ID = 930
STATE = os.path.expanduser('~/exitmgr-app/.gateway_health_state.json')

def slack(msg):
    """Public API contract; production-derived narrative omitted."""
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
    if n.weekday() >= 5:
        return False
    return dtime(6, 30) <= n.time() < dtime(13, 5)

async def probe(client_id=CLIENT_ID):
    """Public API contract; production-derived narrative omitted."""
    ib = IB()

    async def readiness():

        await ib.connectAsync(host='127.0.0.1', port=4001, clientId=client_id,
                              timeout=10, raiseSyncErrors=True)
        accts = ib.managedAccounts()
        if not accts:
            return False, 'connected but returned NO accounts'
        if not ib.accountValues():
            return False, 'connected but returned NO account values'
        positions = await asyncio.wait_for(ib.reqPositionsAsync(), 6)
        orders = await asyncio.wait_for(ib.reqAllOpenOrdersAsync(), 6)
        if positions is None or orders is None:
            return False, 'position/order book UNKNOWN (no completed result)'
        return True, 'healthy, accounts: %s' % accts

    try:
        return await asyncio.wait_for(readiness(), 30)
    except Exception as exc:
        return False, 'API readiness failed: %s: %s' % (type(exc).__name__, exc)
    finally:
        try:
            ib.disconnect()
        except Exception as exc:
            print('probe disconnect failed: %s' % exc, file=sys.stderr)

async def main(intraday, client_id=CLIENT_ID):
    if intraday and not market_open_now():
        print('skip: outside market hours'); return 0
    healthy, detail = await probe(client_id)
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

    if prev != 'down':
        delivered = slack(':rotating_light: *IBKR Gateway DOWN* (%s). Do 2FA / restart now via `~/trader_host-screen.sh` -- '
              'no trades can fill until it is back. (Likely IBKR forced 2FA; auto-restart cannot bypass it.)' % detail)
        if delivered:
            print('ALERT posted:', detail)
        else:


            print('GATEWAY DOWN BUT ALERT UNDELIVERED:', detail, file=sys.stderr)
            return 1
    else:
        print('still down (de-duped):', detail)
    write_state('down'); return 1

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--intraday', action='store_true', help='self-gate to market hours; for the 10-min schedule')
    ap.add_argument('--client-id', type=int, default=CLIENT_ID, dest='client_id',
                    help='IBKR clientId; the two schedules MUST NOT share one (intraday passes 116)')
    a = ap.parse_args()
    sys.exit(asyncio.run(main(a.intraday, a.client_id)))
