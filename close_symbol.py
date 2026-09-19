#!/usr/bin/env python3
"""Public API contract; production-derived narrative omitted."""

import argparse
import asyncio
import os
import subprocess
import sys
import re

APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)

from exitmgr import alerting
from exitmgr.config import load_config
from exitmgr.connection import IBConnection
from exitmgr.manual_exit_frontend import ManualExitPreflightError, bound_requests
from exitmgr.manual_exit_queue import ManualExitQueue, ManualExitQueueError
from exitmgr.runtime_identity import freeze_runtime_identity, RuntimeIdentityError


def slack(message):
    return alerting.post(message, alerting.alerts_channel(), label="close_symbol",
                         fallback_channel=alerting.error_channel())


def _protective_running():
    label = f"gui/{os.getuid()}/ai.alfred.protective"
    result = subprocess.run(["launchctl", "print", label], capture_output=True, text=True)
    text = result.stdout or ""
    return (result.returncode == 0
            and re.search(r"(?m)^\s*state = running\s*$", text) is not None
            and re.search(r"(?m)^\s*pid = [1-9][0-9]*\s*$", text) is not None)


async def run(confirm, client_id, symbol=None, con_id=None, config_path=None, *,
              slack_status=True):
    if not symbol and con_id is None:
        print("--symbol or --con-id is required; use liquidate.py for the supported long-debit book")
        return 2
    cfg = load_config(config_path or os.path.join(APP_DIR, "config.yaml"))
    try:
        runtime_identity = freeze_runtime_identity(cfg, APP_DIR, require_clean_code=True)
    except RuntimeIdentityError as exc:
        print(f"REFUSED: cannot freeze exact code/policy identity: {exc}")
        return 2
    conn = IBConnection(cfg.ib.host, cfg.ib.port, client_id,
                        market_data_type=cfg.ib.market_data_type)
    if not await conn.connect():
        print("connect failed")
        if slack_status:
            slack(f":x: close_symbol: broker preflight failed for {symbol}; no request queued.")
        return 1
    try:
        positions = list(await conn.ib.reqPositionsAsync())
        requests = bound_requests(
            cfg, positions, symbol=symbol, parent_con_id=con_id,
            runtime_identity=runtime_identity)
        targets = [request["parent_con_id"] for request in requests]
        if not requests:
            print(f"no matching open position for symbol={symbol!r} con_id={con_id!r}")
            return 0
        label = symbol.upper() if symbol else f"conId {con_id}"
        print(f"exact protective-owner targets for {label}: {sorted(targets)}")
        if not confirm:
            print("DRY-RUN — no request queued. Run with --confirm to queue the BAG/single close.")
            return 0
        queue_path = os.path.join(os.path.dirname(cfg.journal.path) or ".", "manual_exits.json")
        ManualExitQueue(queue_path).add(requests)
        running = _protective_running()
        if slack_status:
            slack(f":hourglass_flowing_sand: *{label} exit queued* — parent conIds "
                  f"{sorted(targets)}. The protective owner will submit BAG/single close(s); "
                  f"this is pending until IBKR confirms the fill. Protective service "
                  f"{'has a running process' if running else 'IS NOT RUNNING — operator action required'}.")
        print("QUEUED/PENDING — no broker order was placed by this tool; protective="
              + ("RUNNING" if running else "NOT_RUNNING"))
        return 0 if running else 75
    except (ManualExitPreflightError, ManualExitQueueError) as e:
        print(f"REFUSED: {e}")
        label = symbol if symbol else con_id
        if slack_status:
            slack(f":x: close_symbol refused {label}: {e}. No broker order changed.")
        return 2
    finally:
        await conn.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--client-id", type=int, default=91)
    parser.add_argument("--symbol")
    parser.add_argument("--con-id", type=int)
    parser.add_argument("--config", default=os.path.join(APP_DIR, "config.yaml"))
    parser.add_argument(
        "--suppress-slack-status", action="store_true",
        help="caller renders the queue outcome; suppress this tool's duplicate Slack status")
    args = parser.parse_args()
    sys.exit(asyncio.run(run(
        args.confirm, args.client_id, args.symbol, args.con_id, args.config,
        slack_status=not args.suppress_slack_status)))
