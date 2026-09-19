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
    return alerting.post(message, alerting.alerts_channel(), label="liquidate",
                         fallback_channel=alerting.error_channel())


def _protective_running():
    label = f"gui/{os.getuid()}/ai.alfred.protective"
    result = subprocess.run(["launchctl", "print", label], capture_output=True, text=True)
    text = result.stdout or ""
    return (result.returncode == 0
            and re.search(r"(?m)^\s*state = running\s*$", text) is not None
            and re.search(r"(?m)^\s*pid = [1-9][0-9]*\s*$", text) is not None)


async def run(confirm, client_id, config_path=None):
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
        slack(":x: liquidate: broker preflight failed; no request queued.")
        return 1
    try:
        positions = list(await conn.ib.reqPositionsAsync())
        requests = bound_requests(cfg, positions, runtime_identity=runtime_identity)
        targets = [request["parent_con_id"] for request in requests]
        if not requests:
            print("no open positions")
            return 0
        print(f"exact protective-owner targets: {sorted(targets)}")
        if not confirm:
            print("DRY-RUN — no request queued. Run with --confirm to queue the supported "
                  "long-debit book; unsupported credit/stock positions fail closed.")
            return 0
        queue_path = os.path.join(os.path.dirname(cfg.journal.path) or ".", "manual_exits.json")
        ManualExitQueue(queue_path).add(requests)
        running = _protective_running()
        slack(":rotating_light: *Supported long-debit book exit queued* — parent conIds "
              f"{sorted(targets)}. The protective owner will submit BAG/single close(s); "
              f"this is pending until every IBKR fill is confirmed. Protective service "
              f"{'has a running process' if running else 'IS NOT RUNNING — operator action required'}.")
        print("QUEUED/PENDING — no broker order was placed by this tool; protective="
              + ("RUNNING" if running else "NOT_RUNNING"))
        return 0 if running else 75
    except (ManualExitPreflightError, ManualExitQueueError) as e:
        print(f"REFUSED: {e}")
        slack(f":x: liquidate refused: {e}. No broker order changed.")
        return 2
    finally:
        await conn.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--client-id", type=int, default=91)
    parser.add_argument("--config", default=os.path.join(APP_DIR, "config.yaml"))
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args.confirm, args.client_id, args.config)))
