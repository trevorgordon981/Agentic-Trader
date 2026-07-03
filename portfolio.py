#!/usr/bin/env python3
"""Portfolio review (2026-06-18): price the open book, ask the model HOLD/TRIM/SELL per position,
and — when a new idea needs more cash than is available — recommend which position to sell to fund it.
Advisory: posts a synopsis; execution stays human-approved. Run standalone or import review_positions()."""
import asyncio, json, os, sys, urllib.request
from datetime import datetime, date

from exitmgr.ibkr import IB, Contract
from exitmgr.account import get_pot_snapshot

ENDPOINT = os.environ.get("LLM_ENDPOINT", "http://127.0.0.1:8082/v1/chat/completions")
MODEL = os.environ.get("LLM_MODEL", "/path/to/model")
JOURNAL = os.environ.get("JOURNAL_PATH", os.path.expanduser("~/exitmgr-app/trades.log"))

REVIEW_PROMPT = (
    "You are reviewing an open options book to decide what to KEEP, TRIM, or SELL. For each position you "
    "get: symbol, structure, days held, days-to-expiry (DTE), entry cost, current value, unrealized P&L %, "
    "and the take-profit / stop levels. Decide HOLD, TRIM, or SELL with one concise reason. Lean SELL when: "
    "thesis broken or near the stop; a large gain worth banking; an option decaying with little time left "
    "(low DTE and out-of-the-money); or capital is needed for a clearly better setup. HOLD winners that still "
    "have time and an intact thesis. If a NEW IDEA is given that needs more cash than AVAILABLE_CASH, choose "
    "the SINGLE position whose sale best funds it — prefer the weakest/most-decayed, never a strong winner. "
    'Respond with ONLY JSON: {"reviews":[{"symbol":"X","verdict":"hold|trim|sell","reason":"..."}], '
    '"rotation":{"sell":"SYMBOL or null","frees_usd":<number>,"reason":"..."}}'
)

def _llm(system, user, timeout=300, thinking="enabled"):
    body = {"model": MODEL, "temperature": 0,
            "max_tokens": 24000 if thinking == "enabled" else 900, "thinking": thinking,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    req = urllib.request.Request(ENDPOINT, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    import urllib.error, time as _t
    last = None
    for attempt in range(5):                       # M3 is single-gen -> retry 503 backpressure
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504):
                raise
            last = e.code
        except OSError as e:
            last = str(e)
        _t.sleep(8 * (attempt + 1))
    raise RuntimeError(f"llm failed after retries: {last}")

def _last_json(txt):
    import re
    txt = re.sub(r"<think>.*?</think>", "", txt, flags=re.S)
    end = txt.rfind("}")
    while end != -1:
        depth = 0
        for j in range(end, -1, -1):
            if txt[j] == "}": depth += 1
            elif txt[j] == "{":
                depth -= 1
                if depth == 0:
                    try: return json.loads(txt[j:end + 1])
                    except Exception: break
        end = txt.rfind("}", 0, end)
    return {}

def _load_journal(path):
    rows = {}
    try:
        for line in open(path):
            line = line.strip()
            if line:
                e = json.loads(line)
                rows[e["contract_id"]] = e   # latest entry per contract wins
    except Exception:
        pass
    return rows

def _con(cid):
    c = Contract(); c.conId = int(cid); c.exchange = "SMART"; return c

async def _price(ib, e):
    long_cid = int(e["contract_id"]); sp = e.get("spread") or {}
    short_cid = int(sp["short_con_id"]) if sp.get("short_con_id") else None
    cons = [_con(long_cid)] + ([_con(short_cid)] if short_cid else [])
    qc = await ib.qualifyContractsAsync(*cons)
    tks = await ib.reqTickersAsync(*[c for c in qc if getattr(c, "conId", None)])
    mid = {}
    for t in tks:
        m = (t.bid + t.ask) / 2 if (t.bid and t.ask and t.bid > 0 and t.ask > 0) else t.last
        mid[t.contract.conId] = m if (m is not None and m == m and m > 0) else 0.0   # NaN-safe
    long_mid = mid.get(long_cid, 0)
    if long_mid <= 0:
        return None   # could not price the long leg -> skip this position
    net = long_mid - (mid.get(short_cid, 0) if short_cid else 0)
    val = net * 100 * e.get("quantity", 1)
    debit = e.get("debit", 0)
    pnl = ((val - debit) / debit * 100) if debit else 0
    return val, pnl

async def review_positions(ib, idea=None, thinking="enabled"):
    pot = await get_pot_snapshot(ib)
    jrows = _load_journal(JOURNAL)
    live = {abs(p.contract.conId): p.position for p in await ib.reqPositionsAsync() if p.position}
    today = date.today()
    book = []
    for cid, e in jrows.items():
        if int(cid) not in live:        # only review still-open positions
            continue
        try:
            priced = await _price(ib, e)
        except Exception:
            continue
        if priced is None:
            continue
        val, pnl = priced
        held = (datetime.now() - datetime.fromisoformat(e["ts"])).days
        dte = (datetime.strptime(e["expiry"], "%Y%m%d").date() - today).days
        leg = f"/{e['spread']['short_strike']:g}" if e.get("spread") else ""
        book.append({"symbol": e["symbol"], "con_id": int(cid), "stop_pct": e.get("stop_pct"), "structure": f"{e['symbol']} {e['expiry']} {e['strike']:g}{leg}{e['right']}",
                     "days_held": held, "dte": dte, "cost_usd": round(e.get("debit", 0)),
                     "value_usd": round(val), "pnl_pct": round(pnl, 1),
                     "take_profit_pct": e.get("profit_target_pct"), "stop_pct": e.get("stop_pct")})
    payload = {"available_cash": round(pot.available_funds), "net_liq": round(pot.net_liq), "book": book}
    if idea:
        payload["new_idea"] = idea
    if not book:
        return {"book": [], "reviews": [], "rotation": {"sell": None}, "pot": payload}
    out = _last_json(_llm(REVIEW_PROMPT, json.dumps(payload), thinking=thinking))
    out["book"] = book; out["pot"] = payload
    return out

def format_synopsis(r):
    cash = r["pot"]["available_cash"]; nl = r["pot"]["net_liq"]
    lines = [f":clipboard: *Book review* — cash ${cash:,.0f} / NetLiq ${nl:,.0f}"]
    bym = {b["symbol"]: b for b in r.get("book", [])}
    emoji = {"hold": ":green_circle:", "trim": ":large_yellow_circle:", "sell": ":red_circle:"}
    for rv in r.get("reviews", []):
        b = bym.get(rv["symbol"], {})
        pnl = b.get("pnl_pct"); pnls = f" ({pnl:+.0f}%)" if isinstance(pnl, (int, float)) else ""
        lines.append(f"{emoji.get(rv.get('verdict'), '•')} *{rv['symbol']}*{pnls} — {rv.get('verdict','').upper()}: {rv.get('reason','')}")
    rot = r.get("rotation") or {}
    if rot.get("sell"):
        lines.append(f":moneybag: *To free cash:* sell *{rot['sell']}* (~${rot.get('frees_usd',0):,.0f}) — {rot.get('reason','')}")
    return "\n".join(lines)



def arm_sell_approvals(r, mins=30):
    """Post a one-tap SELL approval per SELL-verdict position (+ rotation sell) and write a pending
    map for review_watch.py. Tapping :white_check_mark: -> the watcher queues a real market early-exit."""
    import yaml
    from exitmgr import approval
    cfg = yaml.safe_load(open(os.path.expanduser("~/exitmgr-app/config.yaml"))).get("trading", {})
    tok = os.environ.get("SLACK_BOT_TOKEN", ""); ch = cfg.get("slack_channel", "C0XXXXXXXXX")
    if not tok:
        return
    bym = {b["symbol"]: b for b in r.get("book", [])}
    want = {rv["symbol"] for rv in r.get("reviews", []) if rv.get("verdict") == "sell"}
    rot = (r.get("rotation") or {}).get("sell")
    if rot:
        want.add(rot)
    pending = {}
    for sym in want:
        b = bym.get(sym)
        if not b:
            continue
        rv = next((x for x in r.get("reviews", []) if x["symbol"] == sym), {})
        reason = rv.get("reason") or ((r.get("rotation") or {}).get("reason", "") if sym == rot else "")
        stp = b.get("stop_pct")
        stop_s = f" (ahead of the -{stp:.0f}% stop)" if isinstance(stp, (int, float)) else ""
        msg = chr(10).join([
            f":red_circle: *Early exit: {sym}* ({b.get('pnl_pct',0):+.0f}%, {b.get('dte','?')} DTE)",
            f"_{reason}_",
            f":point_down: Tap :white_check_mark: to SELL NOW at market{stop_s}.",
        ])
        ts = approval.post_proposal(tok, ch, msg)
        if ts:
            pending[ts] = {"symbol": sym, "con_id": b["con_id"]}
    with open(os.path.expanduser("~/exitmgr-app/review_pending.json"), "w") as f:
        json.dump({"channel": ch, "expires_min": mins, "pending": pending}, f)
    print(f"armed {len(pending)} SELL approval(s) -> #trading-approvals")

async def _main():
    ib = IB(); await ib.connectAsync("127.0.0.1", 4001, clientId=94, timeout=15)
    idea = None
    if "--idea" in sys.argv:
        idea = json.loads(sys.argv[sys.argv.index("--idea") + 1])
    r = await review_positions(ib, idea=idea)
    ib.disconnect()
    print(format_synopsis(r))
    if "--arm-sells" in sys.argv:
        arm_sell_approvals(r)
    if "--post" in sys.argv:
        tok = os.environ.get("SLACK_BOT_TOKEN"); ch = os.environ.get("POSITIONS_CHANNEL", "C0XXXXXXXXX")
        try:
            urllib.request.urlopen(urllib.request.Request(
                "https://slack.com/api/chat.postMessage",
                data=json.dumps({"channel": ch, "text": format_synopsis(r)}).encode(),
                headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}), timeout=15)
        except Exception as e:
            print("slack post failed:", e)

if __name__ == "__main__":
    asyncio.run(_main())
