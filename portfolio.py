#!/usr/bin/env python3
"""Public API contract; production-derived narrative omitted."""
import asyncio, json, os, sys, time as _t, urllib.error, urllib.request
from contextlib import nullcontext
from datetime import datetime, date, timezone
from zoneinfo import ZoneInfo

from exitmgr.ibkr import IB, Contract
from exitmgr.account import get_pot_snapshot
from glm_trader_lease import GLM_ENDPOINT, GLM_MODEL, trader_glm_lease

def _configured_llm():
    """Public API contract; production-derived narrative omitted."""
    try:
        from exitmgr.config import load_config
        cfg_path = os.environ.get("EXITMGR_CONFIG") or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "config.yaml")
        cfg = load_config(cfg_path)
        return getattr(cfg, "llm_endpoint", None), getattr(cfg, "llm_model", None)
    except Exception:
        return None, None


_CFG_ENDPOINT, _CFG_MODEL = _configured_llm()
ENDPOINT = os.environ.get("LLM_ENDPOINT") or _CFG_ENDPOINT or "http://127.0.0.1:8082/v1/chat/completions"
MODEL = os.environ.get("LLM_MODEL") or _CFG_MODEL or "local-model"
JOURNAL = os.environ.get("JOURNAL_PATH", os.path.expanduser("~/exitmgr-app/trades.log"))
_LEGACY_JOURNAL_TZ = ZoneInfo("America/Los_Angeles")

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
            "chat_template_kwargs": {"thinking": str(thinking).lower() in ("enabled", "adaptive")},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}


    from exitmgr.strategist import _post_json




    result = _post_json(ENDPOINT, body, timeout, retries=5)
    return result["choices"][0]["message"]["content"]

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
                rows[e["contract_id"]] = e
    except Exception:
        pass
    return rows

def _con(cid):
    c = Contract(); c.conId = int(cid); c.exchange = "SMART"; return c


def _days_held(ts, now=None):
    """Public API contract; production-derived narrative omitted."""
    if not isinstance(ts, str) or not ts.strip():
        raise ValueError("journal timestamp is missing")
    raw = ts.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        entered = datetime.fromisoformat(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid journal timestamp: {ts!r}") from exc
    if entered.tzinfo is None:
        entered = entered.replace(tzinfo=_LEGACY_JOURNAL_TZ)
    entered = entered.astimezone(timezone.utc)

    observed = now or datetime.now(timezone.utc)
    if not isinstance(observed, datetime):
        raise ValueError("review clock must be a datetime")
    if observed.tzinfo is None:
        raise ValueError("review clock must include a timezone")
    observed = observed.astimezone(timezone.utc)
    elapsed = observed - entered
    if elapsed.total_seconds() < 0:
        raise ValueError("journal timestamp is in the future")
    return elapsed.days


async def _price(ib, e):
    long_cid = int(e["contract_id"]); sp = e.get("spread") or {}
    short_cid = int(sp["short_con_id"]) if sp.get("short_con_id") else None
    cons = [_con(long_cid)] + ([_con(short_cid)] if short_cid else [])
    qc = await ib.qualifyContractsAsync(*cons)
    tks = await ib.reqTickersAsync(*[c for c in qc if getattr(c, "conId", None)])
    mid = {}
    for t in tks:
        m = (t.bid + t.ask) / 2 if (t.bid and t.ask and t.bid > 0 and t.ask > 0) else t.last
        mid[t.contract.conId] = m if (m is not None and m == m and m > 0) else 0.0
    long_mid = mid.get(long_cid, 0)
    if long_mid <= 0:
        return None
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
    unreviewable = []
    for cid, e in jrows.items():
        try:
            con_id = int(cid)
        except (TypeError, ValueError, OverflowError):
            reason = "unreviewable journal row: invalid contract id"
            unreviewable.append({"con_id": str(cid), "reason": reason})
            print(f"[REVIEW] con_id={cid}: {reason}; skipping advisory row")
            continue
        if con_id not in live:
            continue
        try:


            if e.get("event") in {"position_closed", "closed_by_tool"}:
                raise ValueError("latest journal row is a close marker, not an entry")
            held = _days_held(e.get("ts"))
            expiry = e["expiry"]
            if not isinstance(expiry, str) or len(expiry) != 8 or not expiry.isdigit():
                raise ValueError("expiry must be YYYYMMDD")
            dte = (datetime.strptime(expiry, "%Y%m%d").date() - today).days
            if not isinstance(e["symbol"], str) or not e["symbol"] or e["right"] not in ("C", "P"):
                raise ValueError("option symbol/right is missing or invalid")
            leg = f"/{e['spread']['short_strike']:g}" if e.get("spread") else ""
            structure = f"{e['symbol']} {expiry} {e['strike']:g}{leg}{e['right']}"

            if (not isinstance(e["debit"], (int, float)) or isinstance(e["debit"], bool)
                    or not 0 < abs(e["debit"]) < float("inf")
                    or not isinstance(e["quantity"], (int, float)) or isinstance(e["quantity"], bool)
                    or not 0 < abs(e["quantity"]) < float("inf")):
                raise ValueError("entry cost/quantity is missing or invalid")
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            reason = f"unreviewable journal row: {exc}"
            unreviewable.append({"con_id": int(cid), "reason": reason})
            print(f"[REVIEW] con_id={cid}: {reason}; skipping advisory row")
            continue
        try:
            priced = await _price(ib, e)
        except Exception:
            continue
        if priced is None:
            continue
        val, pnl = priced
        book.append({"symbol": e["symbol"], "con_id": int(cid), "stop_pct": e.get("stop_pct"), "structure": structure,
                     "days_held": held, "dte": dte, "cost_usd": round(e.get("debit", 0)),
                     "value_usd": round(val), "pnl_pct": round(pnl, 1),
                     "take_profit_pct": e.get("profit_target_pct"), "stop_pct": e.get("stop_pct")})
    payload = {"available_cash": round(pot.available_funds), "net_liq": round(pot.net_liq), "book": book, "unreviewable": unreviewable}


    try:
        import sys as _sys
        _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from book_return import book_return as _book_return
        payload["vs_deposits"] = _book_return(pot.net_liq)
    except Exception:
        pass
    if idea:
        payload["new_idea"] = idea
    if not book:
        return {"book": [], "reviews": [], "rotation": {"sell": None}, "pot": payload, "unreviewable": unreviewable}
    out = _last_json(_llm(REVIEW_PROMPT, json.dumps(payload), thinking=thinking))
    out["book"] = book; out["pot"] = payload; out["unreviewable"] = unreviewable
    return out

def format_synopsis(r):
    cash = r["pot"]["available_cash"]; nl = r["pot"]["net_liq"]
    vd = r["pot"].get("vs_deposits")
    head = f":clipboard: *Book review* — cash ${cash:,.0f} / NetLiq ${nl:,.0f}"
    if vd:
        head += (f"\n     vs ${vd['net_deposits']:,.0f} deposited: "
                 f"*{vd['pnl_dollars']:+,.0f} / {vd['pnl_pct']:+.1f}%*")
    lines = [head]
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
    """Public API contract; production-derived narrative omitted."""
    import yaml
    from exitmgr import approval
    cfg = yaml.safe_load(open(os.path.expanduser("~/exitmgr-app/config.yaml"))).get("trading", {})
    from exitmgr import alerting
    tok = os.environ.get("SLACK_BOT_TOKEN", ""); ch = alerting.approvals_channel()
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
        from exitmgr import alerting


        tok = os.environ.get("SLACK_BOT_TOKEN") or alerting.token()
        ch = os.environ.get("POSITIONS_CHANNEL") or alerting.positions_channel()
        if not alerting.post(format_synopsis(r), ch, tok=tok, label="portfolio",
                             timeout=15):
            sys.exit(1)

if __name__ == "__main__":
    asyncio.run(_main())
