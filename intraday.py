#!/usr/bin/env python3
"""Intraday brain (2026-06-18): every ~20 min during market hours, re-read the day's events
(enriched brief) and dynamically (a) flag discretionary EXITS as theses break and (b) scout new
ENTRIES at conviction >=7. Everything stays one-tap (human-gated); mechanical stops are the backstop.
Reuses the slate's resolve/post/place path and the portfolio review. Run --dry-run to see decisions
without posting or trading."""
import asyncio, json, os, sys, time
from datetime import datetime, timezone
import yaml

from exitmgr.ibkr import IB
from exitmgr.account import get_pot_snapshot
from exitmgr import research, strategist
import daily_recommend as DR
import portfolio as PF

CFG = yaml.safe_load(open(os.path.expanduser("~/exitmgr-app/config.yaml")))
TR = CFG.get("trading", {})
STATE = os.path.expanduser("~/exitmgr-app/intraday_state.json")
ENTRY_MIN_CONVICTION = float(os.environ.get("INTRADAY_MIN_CONVICTION", "7"))
REPOST_COOLDOWN_S = int(os.environ.get("INTRADAY_REPOST_COOLDOWN_S", "10800"))  # 3h: don't re-pitch
DRY = "--dry-run" in sys.argv

def _now(): return datetime.now(timezone.utc)

def _market_open():
    # US RTH ~ 13:30-20:00 UTC, Mon-Fri (ignores holidays; gateway maint handled elsewhere)
    t = _now()
    if t.weekday() >= 5: return False
    mins = t.hour * 60 + t.minute
    return 13 * 60 + 30 <= mins <= 20 * 60

def _load_state():
    try:
        with open(STATE) as f: return json.load(f)
    except Exception:
        return {"posted": {}, "flagged": {}}

def _save_state(s):
    try:
        with open(STATE, "w") as f: json.dump(s, f)
    except Exception: pass

def _fresh(state, key, kind):
    last = state.get(kind, {}).get(key)
    return (not last) or (time.time() - last > REPOST_COOLDOWN_S)

async def cycle(ib):
    st = _load_state()
    pot = await get_pot_snapshot(ib)
    names = list(TR.get("approved_names", []))
    held_syms = {abs_sym for abs_sym in {p.contract.symbol for p in await ib.reqPositionsAsync() if p.position}}
    # the day's events -> enriched brief
    data = await research.gather(ib, names, single_names=names)
    brief = research.build_brief(today=_now().date().isoformat(), quotes={}, universe=names,
                                 allow_any_name=True, **data)
    out = {"exits": [], "entries": [], "cash": round(pot.available_funds), "net_liq": round(pot.net_liq)}

    # (1) DISCRETIONARY EXITS — review the book against today's tape
    try:
        rev = await PF.review_positions(ib, idea=None)
    except Exception as e:
        rev = {"reviews": [], "book": []}; out["review_error"] = str(e)
    for rv in rev.get("reviews", []):
        if rv.get("verdict") == "sell":
            b = next((x for x in rev["book"] if x["symbol"] == rv["symbol"]), {})
            key = str(b.get("con_id"))
            if _fresh(st, key, "flagged"):
                out["exits"].append({"symbol": rv["symbol"], "pnl": b.get("pnl_pct"), "reason": rv["reason"]})
                st.setdefault("flagged", {})[key] = time.time()
    if out["exits"] and not DRY:
        PF.arm_sell_approvals(rev)   # posts one-tap SELLs; review_watch executes the taps

    # (2) NEW ENTRIES — scout the day's events for high-conviction setups
    ideas = []
    import time as _t
    for _att in range(3):                          # M3 single-gen contention -> be patient, not latency-critical
        try:
            ideas = strategist.propose(TR.get("llm_endpoint"), TR.get("llm_model"), brief, timeout=1800, recommend=True)
            out.pop("propose_error", None)
            break
        except Exception as e:
            out["propose_error"] = str(e)
            _t.sleep(45)
    for idea in sorted(ideas, key=lambda i: -i.conviction):
        if idea.conviction < ENTRY_MIN_CONVICTION:
            continue
        if idea.underlying in held_syms:           # don't double up on an open name
            continue
        key = f"{idea.underlying}:{idea.direction}:{idea.structure}".lower()
        if not _fresh(st, key, "posted"):           # don't re-pitch the same idea within cooldown
            continue
        out["entries"].append({"symbol": idea.underlying, "direction": idea.direction,
                               "structure": idea.structure, "conviction": idea.conviction,
                               "thesis": idea.thesis[:140]})
        st.setdefault("posted", {})[key] = time.time()
    if not DRY:
        _save_state(st)
    return out

def _print(out):
    ts = _now().strftime("%H:%M UTC")
    print(f"[{ts}] cash ${out['cash']:,} / NetLiq ${out['net_liq']:,}")
    if out.get("propose_error"): print("  propose_error:", out["propose_error"])
    print(f"  EXITS flagged ({len(out['exits'])}):")
    for e in out["exits"]: print(f"    - {e['symbol']} ({e['pnl']:+.0f}%): {e['reason']}")
    print(f"  NEW ENTRIES >= {ENTRY_MIN_CONVICTION:.0f} conviction ({len(out['entries'])}):")
    for e in out["entries"]: print(f"    + {e['symbol']} {e['direction']} {e['structure']} (conv {e['conviction']}): {e['thesis']}")

async def main():
    once = "--once" in sys.argv or DRY
    ib = IB(); await ib.connectAsync("127.0.0.1", 4001, clientId=92, timeout=15)
    try:
        while True:
            if _market_open() or DRY:
                try:
                    _print(await cycle(ib))
                except Exception as e:
                    print(f"[cycle error] {e}")
            else:
                print(f"[{_now().strftime('%H:%M UTC')}] market closed — idle")
            if once:
                break
            await asyncio.sleep(20 * 60)
    finally:
        ib.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
