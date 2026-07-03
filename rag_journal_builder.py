#!/usr/bin/env python3
"""
Build Trevor's daily journal for the Alfred RAG "memory" domain by extracting
key facts from Alfred's conversation history.

Every run, for each target day, it reads that day's messages from
~/.hermes/sessions/*.jsonl, asks the local M3 model to distill concrete
facts/events/decisions, and writes journal-YYYY-MM-DD.md. Idempotent: the whole
day is regenerated each run, so a 2-hourly cadence keeps refining the same file
with the day's full picture. The files are synced to rag-host and re-indexed so
"what happened on June 3rd" becomes answerable from RAG.

Usage:
    python3 rag_journal_builder.py                # today
    python3 rag_journal_builder.py --date 2026-06-03
    python3 rag_journal_builder.py --backfill 7   # today + previous 6 days
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
import urllib.request
import urllib.error
from datetime import date, datetime, timedelta

STATE_DB = os.path.expanduser("~/.hermes/state.db")
OUT_DIR = os.path.expanduser("~/rag-journal-stage")
LLM_ENDPOINT = "http://127.0.0.1:8082/v1/chat/completions"
LLM_MODEL = "/path/to/model"

MAX_MSG_CHARS = 2000           # truncate any single message
MAX_TRANSCRIPT_CHARS = 150000  # total budget sent to the model (keep most recent if over)

def _date_block(target):
    d = datetime.strptime(target, '%Y-%m-%d').date()
    weekday = d.strftime('%A')
    month = d.strftime('%B')
    long_date = f'{weekday}, {month} {d.day}, {d.year}'
    short = f'{month} {d.day}'
    us = f'{d.month:02d}/{d.day:02d}/{d.year}'
    iso = target
    h1 = f'# Journal \u2014 {long_date}'
    dateline = f'**Date:** {long_date} \u00b7 {short} \u00b7 {us} \u00b7 {iso}. (All entries below occurred on this day.)'
    return h1, dateline


SYSTEM_PROMPT = (
    "You maintain Trevor's daily journal. You are given timestamped excerpts from "
    "his assistant Alfred's conversations on a single day. Extract the concrete "
    "facts, events, decisions, and changes that actually happened — what Trevor "
    "did, decided, learned, built, traded, or experienced. Be specific and factual.\n\n"
    "Output GitHub-flavored markdown grouped under only the relevant headings from: "
    "## Health, ## Trading, ## Infra & Homelab, ## Decisions, ## Personal, ## Learnings. "
    "Each item is a bullet; lead with the time (e.g. '- 15:31 — ...') when known. "
    "Record durable facts, not greetings, banter, or Alfred's persona chatter. "
    "Do not invent anything not supported by the excerpts. If the day has nothing "
    "notable, output exactly: No notable events."
)


def collect_messages(target: str):
    """Return ordered (hm, source, role, content) tuples for the given local day."""
    lo = datetime.strptime(target, "%Y-%m-%d").timestamp()
    hi = lo + 86400
    con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT m.timestamp, COALESCE(s.source, '?'), m.role, m.content "
            "FROM messages m JOIN sessions s ON m.session_id = s.id "
            "WHERE m.role IN ('user','assistant') AND m.content IS NOT NULL "
            "AND m.content != '' AND m.timestamp >= ? AND m.timestamp < ? "
            "ORDER BY m.timestamp",
            (lo, hi),
        ).fetchall()
    finally:
        con.close()
    out = []
    for ts, source, role, content in rows:
        hm = datetime.fromtimestamp(ts).strftime("%H:%M")
        out.append((hm, source, role, content.strip()))
    return out


def build_transcript(msgs):
    parts = []
    for hm, source, role, content in msgs:
        if len(content) > MAX_MSG_CHARS:
            content = content[:MAX_MSG_CHARS] + " …[truncated]"
        parts.append(f"[{hm} {source}] {role}: {content}")
    text = "\n".join(parts)
    if len(text) > MAX_TRANSCRIPT_CHARS:
        text = text[-MAX_TRANSCRIPT_CHARS:]  # keep the most recent of the day
    return text


def extract(target: str, transcript: str) -> str:
    body = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Day: {target}\n\nConversation excerpts:\n{transcript}"},
        ],
        "max_tokens": 4000,
        "temperature": 0.2,
        "thinking": "disabled",
    }
    data = json.dumps(body).encode()
    for attempt in range(4):
        try:
            req = urllib.request.Request(LLM_ENDPOINT, data=data,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=300) as r:
                d = json.loads(r.read().decode(), strict=False)
            return d["choices"][0]["message"]["content"].strip()
        except (urllib.error.HTTPError, urllib.error.URLError, ConnectionError, TimeoutError):
            if attempt < 3:
                time.sleep(15)
                continue
            raise


def write_day(target: str) -> str | None:
    msgs = collect_messages(target)
    if not msgs:
        return None
    body = extract(target, build_transcript(msgs))
    if body.strip() == "No notable events.":
        return None
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"journal-{target}.md")
    with open(path, "w") as f:
        f.write(f"---\ndate: {target}\ntype: daily-journal\nsource: alfred-conversations\n---\n\n")
        h1, dateline = _date_block(target)
        f.write(f"{h1}\n{dateline}\n\n{body}\n")
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=date.today().isoformat())
    ap.add_argument("--backfill", type=int, default=1,
                    help="number of days ending at --date to process (default 1)")
    args = ap.parse_args()

    end = datetime.strptime(args.date, "%Y-%m-%d").date()
    written = []
    for i in range(args.backfill):
        day = (end - timedelta(days=i)).isoformat()
        try:
            p = write_day(day)
            if p:
                written.append(p)
                print(f"  wrote {p}")
            else:
                print(f"  {day}: no notable events / no messages")
        except Exception as e:
            print(f"  {day}: FAILED ({e})")
    print(f"Journal builder wrote {len(written)} file(s) to {OUT_DIR}")


if __name__ == "__main__":
    main()
