#!/usr/bin/env python3
"""Add ticker(s) to the trading watchlist (trading.approved_names in config.yaml) — anytime.

Standalone, dependency-free. Does NOT import daily_recommend.py (that module pulls in IBKR/heavy
deps at import time and would connect to things). Instead it replicates the same minimal,
comment-preserving regex edit `_append_watchlist` uses on the flow-style `approved_names: [...]`
list, so the rest of config.yaml (inline comments, block lists) is left byte-for-byte intact.

Why this exists: the morning slate's reply-watcher can only add that day's scouted candidates
during its ~6h window. Ad-hoc "add TICKER" outside that window had no path — chat-Alfred would
thrash through files. This is that path: one command, anytime.

Usage:
    python3 ~/exitmgr-app/watchlist_add.py MU NVDA DELL
    python3 ~/exitmgr-app/watchlist_add.py "MU, NVDA DELL"     # comma and/or space separated

It uppercases, validates each looks like a ticker, de-dupes against the current list, refuses any
ticker in trading.blocked_names (warns instead), writes a timestamped .bak of config.yaml before
editing, and re-parses the YAML afterward to confirm it's still valid. Prints added / already-present
/ skipped-blocked and the new count.

NOTE: a config change only takes effect on the trader after a restart:
    launchctl kickstart -k gui/$(id -u)/ai.alfred.trader
This script does NOT restart anything (additive + safe by design). Mention the restart if the change
needs to be live immediately; otherwise the next slate/loop picks it up on its own config reload.
"""
import argparse
import os
import re
import shutil
import sys
from datetime import datetime

CONFIG_PATH = os.path.expanduser("~/exitmgr-app/config.yaml")
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,6}$")
APPROVED_RE = re.compile(r"(  approved_names: \[)([^\]]*)(\])")


def parse_tickers(args):
    """Split args on whitespace/commas, uppercase, dedupe (preserve order)."""
    raw = []
    for a in args:
        raw.extend(p for p in re.split(r"[,\s]+", a) if p)
    out, seen = [], set()
    for t in (x.strip().upper() for x in raw):
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def current_approved(s):
    m = APPROVED_RE.search(s)
    if not m:
        return None, None
    cur = [x.strip() for x in m.group(2).split(",") if x.strip()]
    return cur, m


def current_blocked(s):
    """Read the block-style `blocked_names:` list (one `- TICKER` per line)."""
    blocked = []
    in_block = False
    for line in s.splitlines():
        if re.match(r"^\s*blocked_names:\s*$", line):
            in_block = True
            continue
        if in_block:
            m = re.match(r"^\s*-\s*([A-Za-z0-9.\-]+)\s*$", line)
            if m:
                blocked.append(m.group(1).strip().upper())
            elif line.strip() and not line.lstrip().startswith("#"):
                break  # left the list
    return set(blocked)


def main():
    ap = argparse.ArgumentParser(description="Add ticker(s) to the trading watchlist (approved_names).")
    ap.add_argument("tickers", nargs="+", help="Tickers, space- or comma-separated (e.g. MU NVDA DELL)")
    ap.add_argument("--config", default=CONFIG_PATH, help="Path to config.yaml")
    args = ap.parse_args()

    config_path = os.path.expanduser(args.config)
    requested = parse_tickers(args.tickers)
    if not requested:
        print("No tickers given.")
        return 1

    s = open(config_path).read()
    cur, m = current_approved(s)
    if cur is None:
        print(f"ERROR: could not find `approved_names: [...]` in {config_path}", file=sys.stderr)
        return 2
    cur_up = {c.upper() for c in cur}
    blocked = current_blocked(s)

    added, already, blocked_skip, invalid = [], [], [], []
    to_add = []
    for t in requested:
        if not TICKER_RE.match(t):
            invalid.append(t)
        elif t in blocked:
            blocked_skip.append(t)
        elif t in cur_up:
            already.append(t)
        else:
            to_add.append(t)
            added.append(t)
            cur_up.add(t)  # guard against dup within the same invocation

    if to_add:
        bak = config_path + ".bak." + datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(config_path, bak)
        new_inner = ", ".join(cur + to_add)
        s2 = s[:m.start()] + "  approved_names: [" + new_inner + "]" + s[m.end():]
        open(config_path, "w").write(s2)
        # Re-parse to confirm the YAML is still valid.
        try:
            import yaml
            with open(config_path) as f:
                yaml.safe_load(f)
        except Exception as e:
            shutil.copy2(bak, config_path)  # roll back
            print(f"ERROR: edit produced invalid YAML, rolled back from {bak}: {e}", file=sys.stderr)
            return 3
        print(f"Backed up config -> {bak}")

    new_count = len(cur) + len(to_add)
    print("Watchlist update:")
    if added:
        print(f"  added ({len(added)}):           {', '.join(added)}")
    if already:
        print(f"  already present ({len(already)}): {', '.join(already)}")
    if blocked_skip:
        print(f"  SKIPPED (blocked) ({len(blocked_skip)}): {', '.join(blocked_skip)}")
    if invalid:
        print(f"  SKIPPED (not a valid ticker) ({len(invalid)}): {', '.join(invalid)}")
    print(f"  approved_names count: {len(cur)} -> {new_count}")
    if added:
        print("  (config reloads on the next slate/loop; to apply now: "
              "launchctl kickstart -k gui/$(id -u)/ai.alfred.trader)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
