#!/usr/bin/env python3
"""
Generate dated trading-journal markdown for the Alfred RAG corpus from the
live exitmgr audit trail.

Reads ~/exitmgr-app/audit.jsonl (decisions: briefs, proposals, gated ideas,
discovery, recs, executions) and ~/exitmgr-app/trades.log (actual fills), then
emits one markdown file per UTC day into a staging dir. Idempotent: regenerates
every file each run, so re-running after new activity just refreshes the corpus.

The emitted files give the RAG "trading" domain real "what did I trade and why"
content — including the thesis behind ideas that were proposed or rejected —
which the manual Fidelity journals never captured.
"""

import json
import os
from collections import defaultdict
from datetime import datetime, timezone

APP_DIR = os.path.expanduser("~/exitmgr-app")
AUDIT = os.path.join(APP_DIR, "audit.jsonl")
TRADES = os.path.join(APP_DIR, "trades.log")
OUT_DIR = os.path.expanduser("~/rag-trading-stage")


def _date(ts: str) -> str:
    """UTC date (YYYY-MM-DD) from an ISO timestamp, tolerant of formats."""
    try:
        s = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return ts[:10]


def _hm(ts: str) -> str:
    try:
        s = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%H:%M")
    except Exception:
        return "??:??"


def _date_block(day):
    d = datetime.strptime(day, '%Y-%m-%d').date()
    weekday = d.strftime('%A')
    month = d.strftime('%B')
    long_date = f'{weekday}, {month} {d.day}, {d.year}'
    short = f'{month} {d.day}'
    us = f'{d.month:02d}/{d.day:02d}/{d.year}'
    iso = day
    h1 = f'# Trading journal \u2014 {long_date}'
    dateline = f'**Date:** {long_date} \u00b7 {short} \u00b7 {us} \u00b7 {iso}. (All trades and ideas below are for this day.)'
    return h1, dateline


def load_jsonl(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    audit = load_jsonl(AUDIT)
    fills = load_jsonl(TRADES)

    by_day = defaultdict(lambda: {
        "fills": [], "recs": [], "gated": [], "discovery": [],
        "brief": None, "directed": [],
    })

    for e in audit:
        d = _date(e.get("ts", ""))
        ev = e.get("event")
        slot = by_day[d]
        if ev == "daily_rec_posted":
            slot["recs"].append(e)
        elif ev == "gated":
            slot["gated"].append(e)
        elif ev == "discovery":
            slot["discovery"].append(e)
        elif ev == "user_directed_proposal":
            slot["directed"].append(e)
        elif ev == "strategist_brief" and slot["brief"] is None:
            slot["brief"] = e.get("brief", "")

    for t in fills:
        by_day[_date(t.get("ts", ""))]["fills"].append(t)

    written = 0
    for day in sorted(by_day):
        s = by_day[day]
        if not any([s["fills"], s["recs"], s["gated"], s["discovery"], s["directed"]]):
            continue
        lines = [
            "---",
            f"date: {day}",
            "type: trading-journal",
            "source: exitmgr-audit",
            "---",
            "",
            _date_block(day)[0],
            _date_block(day)[1],
            "",
            "Live IBKR options account (exitmgr). Auto-generated from the audit trail.",
            "",
        ]

        if s["fills"]:
            lines.append("## Trades executed")
            for t in s["fills"]:
                sym = t.get("symbol", "?")
                right = "call" if t.get("right") == "C" else "put"
                strike = t.get("strike")
                exp = t.get("expiry", "")
                debit = t.get("debit")
                tp = t.get("profit_target_pct")
                stop = t.get("stop_pct")
                spread = t.get("spread")
                if spread:
                    desc = (f"{sym} {exp} {strike}/{spread.get('short_strike')} {right} "
                            f"debit spread (width {spread.get('width')})")
                else:
                    desc = f"{sym} {exp} {strike}{('C' if right=='call' else 'P')} (long {right})"
                lines.append(
                    f"- **{_hm(t.get('ts',''))} UTC** — {desc}, debit ${debit}, "
                    f"profit target +{tp}% / stop -{stop}%."
                )
            lines.append("")

        if s["recs"]:
            lines.append("## Recommendations posted to #trading-approvals")
            for r in s["recs"]:
                conv = r.get("conviction")
                order = r.get("order", "")
                extra = f" (conviction {conv}/10)" if conv is not None else ""
                lines.append(f"- {order}{extra}")
            lines.append("")

        if s["directed"]:
            lines.append("## User-directed ideas")
            for d_ in s["directed"]:
                lines.append(
                    f"- {d_.get('underlying','?')} {d_.get('direction','')} "
                    f"{d_.get('structure','')} (DTE {d_.get('dte')}, delta {d_.get('delta')})"
                )
            lines.append("")

        if s["gated"]:
            lines.append("## Ideas considered but not taken")
            for g in s["gated"]:
                idea = g.get("idea", {})
                reasons = "; ".join(g.get("reasons", [])) or "gated"
                thesis = idea.get("thesis", "")
                lines.append(
                    f"- **{idea.get('underlying','?')}** {idea.get('direction','')} "
                    f"{idea.get('structure','')} (conviction {idea.get('conviction')}): "
                    f"_{thesis}_  \n  → **Not taken:** {reasons}."
                )
            lines.append("")

        if s["discovery"]:
            cands = []
            for d_ in s["discovery"]:
                cands.extend(d_.get("candidates", []))
            if cands:
                lines.append("## Names scouted by discovery")
                lines.append("- " + ", ".join(dict.fromkeys(cands)))
                lines.append("")

        if s["brief"]:
            brief = s["brief"].strip()
            if len(brief) > 1800:
                brief = brief[:1800] + "\n…(truncated)"
            lines.append("## Market brief snapshot")
            lines.append("```")
            lines.append(brief)
            lines.append("```")
            lines.append("")

        path = os.path.join(OUT_DIR, f"trades-{day}.md")
        with open(path, "w") as f:
            f.write("\n".join(lines))
        written += 1

    print(f"Wrote {written} trading-journal files to {OUT_DIR}")


if __name__ == "__main__":
    main()
