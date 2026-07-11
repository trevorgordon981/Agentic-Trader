#!/usr/bin/env python
"""Morning portfolio THESIS REVIEW for the live IBKR options book.

Fires every weekday ~6:35 AM PT (launchd ai.alfred.morning-review), a few minutes
BEFORE the daily-recommend slate (6:40). It re-judges the trades you're ALREADY in,
not new ones:

  1. Pull LIVE open positions (read-only, clientId 97).
  2. Recall each position's ENTRY THESIS from audit.jsonl (matched by underlying).
  3. Re-ground in TODAY's research brief (reuses research.gather/build_brief).
  4. Ask the strategist LLM (M3) per position: does the thesis still HOLD, or has
     it materially ERODED?  -> JSON {eroded, reason, action}.
  5. For ERODED theses, post a ONE-TAP SELL offer to #trading-approvals so Master
     Gordon can close on a single tap. Intact theses get a one-line "still holds".

RED LINE: this NEVER sells on its own. Every close needs the human tap. When a SELL
offer is approved, this process calls the sanctioned close tool
(`close_symbol.py --symbol XXX --confirm`) -- the same market-close path the exit
manager and liquidate-listener use. Until then it is strictly read-only.

DRY-RUN: `--dry-run` does the full pipeline (positions, theses, brief, M3) and PRINTS
what it would post + which closes it would offer, but posts NOTHING to Slack and
places NO orders. Safe to run anytime, including against a live gateway.

Client IDs in use elsewhere: status=87, quote=86, trader=88, close=91, daily-rec=93,
daily-summary=95, position-monitor=96. This uses 97.
"""
import argparse
import asyncio
import datetime as dt
import json
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.expanduser("~/exitmgr-app"))

from exitmgr import approval, research, trade_capture
from exitmgr.account import get_pot_snapshot
from exitmgr.connection import IBConnection
from exitmgr.market import fetch_universe_quotes
from exitmgr.strategist import _post_json  # reuse the retrying OpenAI-compatible POST

import yaml

APP_DIR = os.path.expanduser("~/exitmgr-app")
CLIENT_ID = 97
CORE = ["SPY", "QQQ", "IWM"]

# How much erosion conviction we require before bugging the user with a SELL offer.
# The LLM returns action in {HOLD, CONSIDER_SELL}; only CONSIDER_SELL + eroded=True offers a sell.

REVIEW_SYSTEM_PROMPT = (
    "You are a risk-focused options swing-trading strategist managing a SMALL (~$1,000) account. "
    "You are NOT proposing new trades. You are re-judging ONE position the account is ALREADY in. "
    "You are given: the position, the ORIGINAL entry thesis (why it was opened), and TODAY's market "
    "context. Decide ONLY whether the original thesis still holds or has MATERIALLY ERODED.\n"
    "Erosion = the specific reason the trade was opened is no longer true or has reversed (trend "
    "broke, catalyst passed/failed, momentum flipped, regime changed against it, news invalidated "
    "the call). A position merely being down is NOT automatically erosion -- judge the THESIS, not "
    "the mark. Be conservative about telling the user to sell: only flag CONSIDER_SELL when the "
    "reasoning that justified the position is genuinely gone.\n"
    "Reply with STRICT JSON and nothing else:\n"
    '{"eroded": true|false, "reason": "<one tight sentence>", "action": "HOLD"|"CONSIDER_SELL"}'
)


# ---------------------------------------------------------------------------
# Slack token (same source/format as position_monitor.py / close_symbol.py)
# ---------------------------------------------------------------------------
def slack_token():
    tok = os.environ.get("SLACK_BOT_TOKEN", "")
    if tok:
        return tok
    try:
        for l in open(os.path.expanduser("~/.hermes/.env")):
            if l.startswith("SLACK_BOT_TOKEN="):
                return l.split("=", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return ""


def slack_post(token, channel, text):
    """Plain message (used for the intact-theses summary)."""
    try:
        urllib.request.urlopen(urllib.request.Request(
            "https://slack.com/api/chat.postMessage",
            data=json.dumps({"channel": channel, "text": text}).encode(),
            headers={"Authorization": "Bearer " + token,
                     "Content-Type": "application/json"}), timeout=15)
    except Exception as e:
        print("slack post fail:", e)


# ---------------------------------------------------------------------------
# Journal (trades.log) -- newest entry per long contract_id, exactly like
# position_monitor.load_journal(): later lines overwrite, so newest wins.
# ---------------------------------------------------------------------------
def load_journal(path):
    by_con = {}
    try:
        for line in open(path):
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            cid = e.get("contract_id")
            if cid is not None:
                by_con[cid] = e
    except FileNotFoundError:
        pass
    return by_con


# ---------------------------------------------------------------------------
# Entry-thesis recall from audit.jsonl.
#
# VERIFIED format (2026-06-22): `gated` events carry idea.underlying + idea.thesis;
# `user_directed_proposal` events carry underlying (no thesis text). We scan the
# whole file and keep, per UNDERLYING, the NEWEST event that actually has a thesis
# string. We match a live position to a thesis by its underlying symbol -- the audit
# log keys on underlying, not contract_id, so per-contract precision isn't available.
# Degrades gracefully (returns None) when no thesis is found for a symbol.
# ---------------------------------------------------------------------------
def load_theses(path):
    """{UNDERLYING -> {'thesis', 'ts', 'event', 'conviction'}} newest-thesis-wins."""
    out = {}
    try:
        for line in open(path):
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            idea = e.get("idea") or {}
            sym = (idea.get("underlying") or e.get("underlying") or "").upper()
            thesis = idea.get("thesis")
            if not (sym and thesis):
                continue
            ts = e.get("ts", "")
            prev = out.get(sym)
            if prev is None or ts >= prev["ts"]:
                out[sym] = {"thesis": thesis, "ts": ts,
                            "event": e.get("event", ""),
                            "conviction": idea.get("conviction")}
    except FileNotFoundError:
        pass
    return out


def dte(expiry):
    try:
        d = dt.datetime.strptime(str(expiry)[:8], "%Y%m%d").date()
        return (d - dt.date.today()).days
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Collect live positions -- mirrors position_monitor.collect_async: group spread
# legs under the long leg via the journal, take uPnL from ib.portfolio() (IBKR's
# own server-side marking, most reliable for options vs flaky bid/ask).
# ---------------------------------------------------------------------------
async def collect_positions(conn, jrnl):
    ib = conn.ib
    await asyncio.sleep(1.5)  # let account/portfolio updates arrive
    pot = await get_pot_snapshot(ib)
    positions = [p for p in await ib.reqPositionsAsync() if p.position != 0]
    port = {p.contract.conId: p for p in ib.portfolio() if p.position != 0}

    live_conids = {p.contract.conId for p in positions}
    seen = set()
    items = []
    for p in positions:
        c = p.contract
        cid = c.conId
        if cid in seen:
            continue
        # skip the SHORT leg of a managed spread -> reported under its long leg
        if cid not in jrnl and any(
                (je.get("spread") or {}).get("short_con_id") == cid
                and je.get("contract_id") in live_conids
                for je in jrnl.values()):
            seen.add(cid)
            continue

        e = jrnl.get(cid)
        right = getattr(c, "right", "") or ""
        expiry = getattr(c, "lastTradeDateOrContractMonth", "") or ""
        upnl = port[cid].unrealizedPNL if cid in port else None
        debit = None
        label = f"{c.symbol} {getattr(c, 'strike', '')}{right}"
        if e:
            expiry = e.get("expiry", expiry)
            debit = e.get("debit")
            sp = e.get("spread") or {}
            short_con = sp.get("short_con_id")
            if short_con and short_con in port:
                upnl = (upnl or 0.0) + port[short_con].unrealizedPNL
                seen.add(short_con)
            label = (f"{c.symbol} {e.get('strike', '')}/{sp.get('short_strike', '')}{right} spread"
                     if sp else f"{c.symbol} {e.get('strike', '')}{right}")
        seen.add(cid)

        d = dte(expiry)
        pct = (upnl / debit * 100.0) if (upnl is not None and debit) else None
        items.append({
            "symbol": c.symbol, "label": label, "right": right, "con_id": cid,
            "decision_id": (e.get("decision_id") if e else None),
            "expiry": expiry, "dte": d, "upnl": upnl, "debit": debit, "pct": pct,
        })
    return pot, items


# ---------------------------------------------------------------------------
# Per-position thesis judgement via M3 (reuses strategist._post_json + endpoint).
# ---------------------------------------------------------------------------
def judge_thesis(endpoint, model, position_blurb, thesis, brief, timeout=240):
    user = (
        "=== OPEN POSITION ===\n" + position_blurb + "\n\n"
        "=== ORIGINAL ENTRY THESIS ===\n" + (thesis or "(no recorded thesis)") + "\n\n"
        "=== CURRENT MARKET CONTEXT (today's brief) ===\n" + brief + "\n\n"
        "Has the ORIGINAL thesis materially eroded? Reply with the strict JSON only."
    )
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        "max_tokens": 600,
        "temperature": 0.2,
        "thinking": "disabled",
    }
    d = _post_json(endpoint, body, timeout)
    content = d["choices"][0]["message"].get("content") or ""
    return _parse_verdict(content)


def _parse_verdict(raw):
    """Pull the {eroded, reason, action} object out of the model reply; degrade safely."""
    if not raw:
        return {"eroded": False, "reason": "(empty model reply)", "action": "HOLD", "_ok": False}
    s = raw
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j != -1 and j > i:
        s = s[i:j + 1]
    try:
        o = json.loads(s)
    except Exception:
        return {"eroded": False, "reason": "(unparseable model reply -> defaulting to HOLD)",
                "action": "HOLD", "_ok": False}
    action = str(o.get("action", "HOLD")).upper().strip()
    if action not in ("HOLD", "CONSIDER_SELL"):
        action = "CONSIDER_SELL" if o.get("eroded") else "HOLD"
    return {
        "eroded": bool(o.get("eroded")),
        "reason": str(o.get("reason", "")).strip() or "(no reason given)",
        "action": action,
        "_ok": True,
    }


def position_blurb(it):
    bits = [it["label"]]
    if it["pct"] is not None:
        bits.append(f"{it['pct']:+.0f}% uPnL")
    elif it["upnl"] is not None:
        bits.append(f"${it['upnl']:+.0f} uPnL")
    if it["dte"] is not None:
        bits.append(f"{it['dte']}DTE")
    return "  |  ".join(bits)


# ---------------------------------------------------------------------------
# Post-trade REVIEW sidecar emit. This is the ONLY writer of `reviews.jsonl`
# in the dataset dir; ExitManager.load_review() (trade_capture.load_review)
# reads it back at CLOSE to embed a `review` block under the closed-trade v2
# record -- otherwise that block is always empty.
#
# We reuse the per-position coaching this script ALREADY produced (the M3
# thesis verdict: eroded/holds + reason + action) -- NO second LLM call. One
# row per reviewed position, keyed by con_id (reliable join) + symbol + date.
#
# NOTE (needs Trevor's eyes): morning_review runs on OPEN positions, so the
# real close date is unknown here. We stamp `date` = review date (today, ET).
# load_review's con_id path ignores date, so the join is robust; its symbol
# path only matches when the caller's date equals this review date. con_id is
# always present for journaled positions, so the con_id path is what carries.
#
# RECORD-ONLY + DEFENSIVE: mirrors trade_capture -- never raises into the
# review flow, append-only JSONL, dedup on read-back so a same-day re-run
# won't double-write the same (con_id|symbol, date).
# ---------------------------------------------------------------------------
def _review_text(it, v, review_date):
    verdict = "ERODED" if v.get("eroded") else "holds"
    reason = v.get("reason") or "(no reason given)"
    action = v.get("action") or "HOLD"
    txt = f"[{review_date} morning thesis review] {verdict}: {reason} (action={action})"
    if v.get("_no_thesis"):
        txt += " (no recorded entry thesis; judged on market context)"
    return txt


def emit_reviews(journal_path, reviewed, review_date, dataset_cfg_path=None):
    """Append one reviews.jsonl row per reviewed position (skipping same-date dups).
    `reviewed` is an iterable of (item, verdict) pairs -- exactly what the report
    already iterates. Returns the number of rows written. Never raises."""
    written = 0
    try:
        ddir = trade_capture.dataset_dir(journal_path, dataset_cfg_path)
        path = os.path.join(ddir or ".", "reviews.jsonl")
        review_date = str(review_date)[:10]

        # Read-back existing keys for idempotency (same (con_id|symbol, date)).
        seen_decision, seen_con, seen_sym = set(), set(), set()
        try:
            if os.path.exists(path):
                for line in open(path):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    d = str(r.get("date") or "")[:10]
                    if r.get("decision_id"):
                        seen_decision.add((str(r.get("decision_id")), d))
                    if r.get("con_id") is not None:
                        seen_con.add((str(r.get("con_id")), d))
                    if r.get("symbol") is not None:
                        seen_sym.add((str(r.get("symbol")), d))
        except Exception:
            pass

        for it, v in reviewed:
            con_id = it.get("con_id")
            symbol = it.get("symbol")
            decision_id = it.get("decision_id")
            if decision_id and (str(decision_id), review_date) in seen_decision:
                continue
            if con_id is not None and (str(con_id), review_date) in seen_con:
                continue
            if con_id is None and (str(symbol), review_date) in seen_sym:
                continue
            rec = {
                "schema": "trade_review.v1",
                "kind": "review",
                "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                "source": "morning_review",
                "decision_id": decision_id,
                "con_id": con_id,
                "symbol": symbol,
                "date": review_date,
                "review": _review_text(it, v, review_date),
                "eroded": bool(v.get("eroded")),
                "action": v.get("action") or "HOLD",
            }
            try:
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                with open(path, "a") as f:
                    f.write(json.dumps(rec, default=str) + "\n")
                written += 1
                if decision_id:
                    seen_decision.add((str(decision_id), review_date))
                if con_id is not None:
                    seen_con.add((str(con_id), review_date))
                if symbol is not None:
                    seen_sym.add((str(symbol), review_date))
            except Exception as e:
                print(f"[WARN] review sidecar write failed for {symbol}: {e!r} (continuing)")
    except Exception as e:
        print(f"[WARN] review sidecar emit failed: {e!r} (continuing)")
    return written


# ---------------------------------------------------------------------------
# SELL-offer message. We reuse approval.post_proposal so the user gets the SAME
# one-tap UX as entry approvals: the bot pre-seeds :white_check_mark:/:x: and the
# watch loop counts only the approver's tap.
# ---------------------------------------------------------------------------
def sell_offer_text(it, verdict, approver_mention):
    return (
        f":warning: *Thesis eroded -- SELL offer* {approver_mention}\n"
        f"*{it['label']}*  ({position_blurb(it)})\n"
        f"_Why I'd consider closing:_ {verdict['reason']}\n\n"
        f":point_down: *Tap :white_check_mark: to CLOSE {it['symbol']} at market, or :x: to HOLD.* "
        f"Both are already on this message -- just tap one. (Typing 'sell'/'close' or 'hold' works "
        f"too.) Nothing closes without your tap."
    )


def run_close(symbol):
    """Sanctioned close on approval: the symbol-filtered market close, the SAME path the exit
    manager / liquidate-listener use. Runs synchronously; returns (ok, output)."""
    py = os.path.expanduser("~/ib-grader-venv/bin/python")
    if not os.path.exists(py):
        py = sys.executable
    cmd = [py, os.path.join(APP_DIR, "close_symbol.py"),
           "--symbol", symbol, "--confirm", "--client-id", "91"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180, cwd=APP_DIR)
        return (r.returncode == 0, (r.stdout or "") + (r.stderr or ""))
    except Exception as e:
        return (False, f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def run(args):
    cfg = yaml.safe_load(open(args.config))
    ibc, tr = cfg.get("ib", {}), cfg.get("trading", {})
    token = slack_token()
    channel = tr.get("slack_channel", "")           # #trading-approvals = C0XXXXXXXXX
    approver_ids = set(tr.get("approver_ids", []))   # {U0XXXXXXXXX}
    approver_mention = "<@" + next(iter(approver_ids)) + ">" if approver_ids else ""
    endpoint = tr.get("llm_endpoint")
    model = tr.get("llm_model")
    journal_path = cfg.get("journal", {}).get("path", os.path.join(APP_DIR, "trades.log"))
    audit_path = tr.get("audit_path", os.path.join(APP_DIR, "audit.jsonl"))
    if journal_path.startswith("./"):
        journal_path = os.path.join(APP_DIR, journal_path[2:])
    if audit_path.startswith("./"):
        audit_path = os.path.join(APP_DIR, audit_path[2:])

    jrnl = load_journal(journal_path)
    theses = load_theses(audit_path)

    conn = IBConnection(host=ibc.get("host", "127.0.0.1"), port=ibc.get("port", 4001),
                        client_id=(getattr(args, "client_id", None) or CLIENT_ID),
                        market_data_type=ibc.get("market_data_type", 1))
    if not await conn.connect(retries=(0 if args.dry_run else 6), retry_delay=20):
        msg = (":warning: *Morning thesis-review skipped -- IBKR gateway unreachable.* "
               "Likely the weekly forced 2FA (auto-restart can't bypass). Do 2FA via "
               "~/studio-screen.sh; the daily slate will tell you the same.")
        print("[ERROR] no IBKR connection")
        if token and channel and not args.dry_run:
            slack_post(token, channel, msg)
        return 1

    try:
        pot, items = await collect_positions(conn, jrnl)
        if not items:
            print("No open positions -- nothing to review.")
            if token and channel and not args.dry_run:
                slack_post(token, channel,
                           ":sunrise: *Morning thesis review* -- no open positions, book is flat.")
            return 0

        # Build TODAY's brief once, reusing the exact daily_recommend reuse path.
        symbols = sorted({it["symbol"] for it in items} | set(CORE))
        names = sorted(set(symbols))
        today = str(dt.datetime.now(dt.timezone.utc).date())
        try:
            quotes = await fetch_universe_quotes(conn.ib, names)
            data = await research.gather(
                conn.ib, names,
                single_names=[n for n in names if n not in CORE])
            brief = research.build_brief(today=today, quotes=quotes, universe=names,
                                         allow_any_name=True, **data)
        except Exception as e:
            print("[WARN] brief build failed, judging on thesis alone:", repr(e))
            brief = "(today's market brief unavailable)"
    finally:
        await conn.disconnect()

    # Judge each position. (LLM calls are I/O-bound HTTP; M3 is single-generation so
    # we go sequentially -- firing concurrent long prompts at it triggers 503/backpressure.)
    eroded, intact = [], []
    for it in items:
        th = theses.get(it["symbol"].upper())
        thesis = th["thesis"] if th else None
        blurb = position_blurb(it)
        try:
            v = judge_thesis(endpoint, model, blurb, thesis, brief)
        except Exception as e:
            print(f"[WARN] M3 judge failed for {it['symbol']}: {e!r} -> treating as HOLD")
            v = {"eroded": False, "reason": f"(judge error: {type(e).__name__})",
                 "action": "HOLD", "_ok": False}
        v["_no_thesis"] = thesis is None
        if v["action"] == "CONSIDER_SELL" and v["eroded"]:
            eroded.append((it, v))
        else:
            intact.append((it, v))

    # ---- Report -------------------------------------------------------------
    print(f"=== Morning thesis review ({today}) — {len(items)} positions ===")
    for it, v in eroded:
        print(f"  ERODED  {it['label']}: {v['reason']}"
              + ("  [no recorded thesis]" if v["_no_thesis"] else ""))
    for it, v in intact:
        print(f"  holds   {it['label']}: {v['reason']}"
              + ("  [no recorded thesis]" if v["_no_thesis"] else ""))

    # Emit the post-trade REVIEW sidecar (reviews.jsonl) so ExitManager can embed
    # this coaching under each closed-trade v2 record. Capture-layer: never raises.
    # Skipped in dry-run to keep that path side-effect-free.
    if not args.dry_run:
        try:
            n_rev = emit_reviews(journal_path, [*eroded, *intact], today,
                                 cfg.get("dataset", {}).get("path"))
            print(f"[review-emit] wrote {n_rev} reviews.jsonl row(s).")
        except Exception as _re:
            print(f"[WARN] morning-review review emit failed (continuing): {_re!r}")

    if args.dry_run:
        print("\n--dry-run: NOTHING posted to Slack, NO sell offers, NO orders.")
        if eroded:
            print("Would post SELL offers for:", ", ".join(it["symbol"] for it, _ in eroded))
        return 0

    if not (token and channel):
        print("[WARN] no Slack token/channel -- cannot post. (dry output above.)")
        return 0

    # Intact summary: one plain message, one line per holding.
    if intact:
        lines = []
        for it, v in intact:
            tag = " _(no recorded thesis — judged on market context)_" if v["_no_thesis"] else ""
            lines.append(f"• *{it['label']}* — still holds: {v['reason']}{tag}")
        slack_post(token, channel,
                   ":sunrise: *Morning thesis review* — these positions still hold:\n"
                   + "\n".join(lines))

    # Eroded: one ONE-TAP SELL offer per position, then watch each for the tap.
    offers = []
    for it, v in eroded:
        ts = approval.post_proposal(token, channel,
                                    sell_offer_text(it, v, approver_mention))
        if ts:
            offers.append((it, ts))
        else:
            print(f"[WARN] failed to post sell offer for {it['symbol']}")

    if not offers:
        return 0

    # Watch for the tap. Reuses approval.await_approval (polls BOTH reactions and
    # in-thread text, approver-gated, reject/HOLD wins). On 'approve' we run the
    # sanctioned close; on 'reject'/'expired' we leave the position alone.
    timeout_s = args.watch_minutes * 60
    for it, ts in offers:
        decision = approval.await_approval(token, channel, ts, approver_ids,
                                           timeout_s=timeout_s, poll_s=15)
        if decision == "approve":
            slack_post(token, channel,
                       f":hourglass_flowing_sand: Closing *{it['symbol']}* at market (your tap)…")
            ok, out = run_close(it["symbol"])
            tail = "\n".join(out.strip().splitlines()[-4:]) if out else ""
            slack_post(token, channel,
                       (":white_check_mark:" if ok else ":x:")
                       + f" close_symbol {it['symbol']} "
                       + ("done" if ok else "FAILED")
                       + (f"\n```{tail}```" if tail else ""))
        else:
            print(f"{it['symbol']}: {decision} -> holding.")

    return 0


def main():
    ap = argparse.ArgumentParser(description="Morning portfolio thesis review (read-mostly).")
    ap.add_argument("--config", default=os.path.join(APP_DIR, "config.yaml"))
    ap.add_argument("--dry-run", action="store_true",
                    help="full pipeline but post nothing and place no orders.")
    ap.add_argument("--client-id", type=int, default=None,
                    help="override IBKR clientId (default 97).")
    ap.add_argument("--watch-minutes", type=int, default=120,
                    help="how long to watch each SELL offer for the approver's tap.")
    args = ap.parse_args()
    try:
        return asyncio.run(run(args))
    except Exception as e:
        print("FATAL:", repr(e))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
