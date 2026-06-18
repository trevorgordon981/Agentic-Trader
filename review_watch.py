#!/usr/bin/env python3
"""Watch the book-review SELL approvals; on the approver's tap, queue a real market early-exit
(manual_exits.json) and kick the trader to execute it. Slack-only (no IBKR). Single executor = trader."""
import json, os, subprocess, sys, time, yaml
from exitmgr import approval

MINS = int(sys.argv[1]) if len(sys.argv) > 1 else 30
PEND = os.path.expanduser("~/exitmgr-app/review_pending.json")
MEX = os.path.expanduser("~/exitmgr-app/manual_exits.json")
cfg = yaml.safe_load(open(os.path.expanduser("~/exitmgr-app/config.yaml"))).get("trading", {})
TOK = os.environ.get("SLACK_BOT_TOKEN", ""); APPROVERS = set(cfg.get("approver_ids", []))

def _queue(cid):
    cur = set()
    try: cur = set(json.load(open(MEX)))
    except Exception: pass
    cur.add(int(cid)); json.dump(sorted(cur), open(MEX, "w"))

def _kick():
    try: subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/ai.alfred.trader"], timeout=10)
    except Exception: pass

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
                _queue(info["con_id"]); _kick()
                approval.post_proposal(TOK, ch, f":white_check_mark: Selling *{info['symbol']}* now (early exit) — watch #trading-alerts for the fill.")
                done.add(ts)
            elif dec == "reject":
                approval.post_proposal(TOK, ch, f":x: Keeping *{info['symbol']}* — no early exit.")
                done.add(ts)
        time.sleep(10)

if __name__ == "__main__":
    main()
