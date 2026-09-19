#!/usr/bin/env python3
"""Public API contract; production-derived narrative omitted."""
import json, os, subprocess, sys, time, yaml
from exitmgr import approval

MINS = int(sys.argv[1]) if len(sys.argv) > 1 else 30
PEND = os.path.expanduser("~/exitmgr-app/review_pending.json")
MEX = os.path.expanduser("~/exitmgr-app/manual_exits.json")
cfg = yaml.safe_load(open(os.path.expanduser("~/exitmgr-app/config.yaml"))).get("trading", {})
TOK = os.environ.get("SLACK_BOT_TOKEN", ""); APPROVERS = set(cfg.get("approver_ids", []))

def _queue_symbol(symbol, con_id):
    cmd = [sys.executable, os.path.expanduser("~/exitmgr-app/close_symbol.py"),
           "--confirm", "--symbol", str(symbol), "--con-id", str(int(con_id))]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)

def main():
    try:
        d = json.load(open(PEND))
    except Exception:
        return
    ch = d["channel"]; pending = d.get("pending", {})
    if not pending or not TOK:
        return
    done = set(); deadline = time.monotonic() + MINS * 60
    while pending and time.monotonic() < deadline and len(done) < len(pending):
        for ts, info in pending.items():
            if ts in done:
                continue
            rxn = approval._api("reactions.get", TOK, {"channel": ch, "timestamp": ts}, http_post=False)
            reactions = (rxn.get("message", {}) or {}).get("reactions", []) if rxn.get("ok") else []
            dec = approval.decision_from_reactions(reactions, APPROVERS)
            if dec == "approve":
                result = _queue_symbol(info["symbol"], info["con_id"])
                if result.returncode == 0:
                    approval.post_proposal(TOK, ch, f":hourglass_flowing_sand: *{info['symbol']}* exit is queued with an exact campaign binding — pending the protective owner and IBKR fill.")
                elif result.returncode == 75:
                    approval.post_proposal(
                        TOK, ch, f":warning: *{info['symbol']}* exit IS queued, but the "
                        "protective service has no running process. The request remains durable; operator "
                        "action is required before it can reach IBKR.")
                else:
                    detail = (result.stderr or result.stdout or "unknown refusal")[-500:]
                    approval.post_proposal(TOK, ch, f":x: *{info['symbol']}* exit request was NOT queued: {detail}")
                done.add(ts)
            elif dec == "reject":
                approval.post_proposal(TOK, ch, f":x: Keeping *{info['symbol']}* — no early exit.")
                done.add(ts)
        time.sleep(10)

if __name__ == "__main__":
    main()
