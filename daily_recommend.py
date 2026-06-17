#!/usr/bin/env python
"""Daily trade-recommendation slate from MiniMax.

Runs once a day (cron at market open). Asks the strategist in RECOMMEND mode for its best 1-3
option ideas scored 1-10, prices each concrete contract via IBKR (needs the OPRA subscription),
and posts each affordable one to #trading-approvals with its score and a one-tap approve/deny.
Then it watches those messages until a deadline and places the ones you approve. The MODEL picks
and scores; YOU approve; this just carries it. clientId 93 (no clash with trader 88 / status 87
/ quote 86 / manual 90).

Usage: ~/ib-grader-venv/bin/python daily_recommend.py [--watch-mins 360]
"""
import argparse
import asyncio
import os
import subprocess
import time
from datetime import date, datetime, timezone

import yaml

from exitmgr.account import get_pot_snapshot
from exitmgr.connection import IBConnection
from exitmgr.ibkr import Stock, Option, Order
from exitmgr.strategist import propose, discover_names, propose_one, TradeIdea
from exitmgr.trader import ResolvedOrder, order_summary, audit
from exitmgr import approval, research
from exitmgr.market import fetch_universe_quotes

CLIENT_ID = 93


def _append_watchlist(config_path, tickers):
    """Append tickers to trading.approved_names in config.yaml (preserving the file). Returns the
    ones actually added (not already present)."""
    import re
    s = open(config_path).read()
    m = re.search(r"(  approved_names: \[)([^\]]*)(\])", s)
    if not m:
        return []
    cur = [x.strip() for x in m.group(2).split(",") if x.strip()]
    cur_up = {c.upper() for c in cur}
    add = [t for t in tickers if t.upper() not in cur_up]
    if not add:
        return []
    new_inner = ", ".join(cur + add)
    s = s[:m.start()] + "  approved_names: [" + new_inner + "]" + s[m.end():]
    open(config_path, "w").write(s)
    return add


def _score_tag(s):
    if s < 4:
        return ":warning: *desperate-only*"
    if s >= 8:
        return "*high confidence*"
    if s >= 6:
        return "*medium confidence*"
    return "_below-average / middle_"


async def _resolve(ib, idea, available):
    """Pick the concrete option (nearest expiry to target DTE, strike by delta) and price it via
    OPRA. Single-leg long call/put; sizes to >=1 contract within available funds. None if it
    can't price or even one contract is unaffordable."""
    right = "C" if idea.direction == "bullish" else "P"
    stk = (await ib.qualifyContractsAsync(Stock(idea.underlying, "SMART", "USD")))[0]
    params = await ib.reqSecDefOptParamsAsync(idea.underlying, "", "STK", stk.conId)
    if not params:
        return None, "no option chain"
    p = params[0]
    today = datetime.now(timezone.utc).date()
    expiry = min(p.expirations, key=lambda e:
                 abs((datetime.strptime(e, "%Y%m%d").date() - today).days - idea.target_dte))
    cands = [Option(idea.underlying, expiry, k, right, p.exchange or "SMART") for k in sorted(p.strikes)]
    qualified = await ib.qualifyContractsAsync(*cands)
    tickers = await ib.reqTickersAsync(*[c for c in qualified if getattr(c, "conId", None)])
    best, best_err = None, 1e9
    by_strike = {}
    for tk in tickers:
        mid = (tk.bid + tk.ask) / 2 if (tk.bid and tk.ask and tk.bid > 0 and tk.ask > 0) else (tk.last or 0)
        if not (mid == mid and mid > 0):
            continue
        k = float(getattr(tk.contract, "strike", 0) or 0)
        if k:
            by_strike[k] = (mid, tk.contract)
        g = getattr(tk, "modelGreeks", None) or getattr(tk, "lastGreeks", None)
        if g and g.delta is not None:
            err = abs(abs(g.delta) - idea.target_delta)
            if err < best_err:
                best, best_err = (tk.contract, mid), err
    if not best:
        return None, "no priced strike (OPRA active?)"
    contract, mid = best

    # DEBIT SPREAD: buy the delta-selected leg, sell a further-OTM same-type leg to cut the cost.
    if "spread" in (idea.structure or "").lower():
        from exitmgr.trader import pick_spread_short
        pick = pick_spread_short([(k, m) for k, (m, _) in by_strike.items()],
                                 float(contract.strike), mid, right, available)
        if pick:
            short_strike, net = pick
            short_contract = by_strike[short_strike][1]
            qty = max(1, int(available // (net * 100)))
            if net * 100 * qty > available + 1e-6:
                qty = max(1, qty - 1)
            return ResolvedOrder(idea.underlying, right, expiry, float(contract.strike), qty, net,
                                 contract, short_strike=short_strike, short_contract=short_contract), None
        # no affordable short leg -> fall through to the single long leg

    if mid * 100 > available + 1e-6:
        return None, f"one contract ${mid*100:,.0f} > available ${available:,.0f}"
    qty = max(1, int(available // (mid * 100)))
    return ResolvedOrder(idea.underlying, right, expiry, float(contract.strike), qty, round(mid, 2), contract), None


async def _post_idea(ib, idea, pot, default_pct, token, channel, audit_path, pending,
                     label="Daily rec", audit_event="daily_rec_posted"):
    """Resolve an idea to a concrete priced order, post it to #approvals with one-tap, and append it
    to `pending` so the watch loop manages approval/execution. Returns the Slack ts (or None).
    Shared by the daily slate and the same-day 'add a name -> suggest it now' path."""
    cons_budget = min(pot.available_funds, default_pct * pot.net_liq)
    resolved, why = await _resolve(ib, idea, cons_budget)
    over_default = False
    if not resolved:
        # too pricey for the default slice -> offer it at full size so you can opt in
        resolved, why = await _resolve(ib, idea, pot.available_funds)
        over_default = resolved is not None
    head = (f":calendar: *{label} — {idea.underlying}* {idea.direction} {idea.structure}\n"
            f"Conviction *{idea.conviction}/10* — {_score_tag(idea.conviction)}\n"
            f"_Thesis:_ {idea.thesis}\n")
    if not resolved:
        approval.post_proposal(token, channel, head + f"_(not placeable: {why})_")
        return None
    cost = resolved.limit * 100 * resolved.qty
    tp_pct = idea.profit_target_pct or 100.0   # fall back to the global default rule
    sl_pct = idea.stop_pct or 50.0
    tp_price = resolved.limit * (1 + tp_pct / 100.0)
    sl_price = resolved.limit * (1 - sl_pct / 100.0)
    pct_pot = (cost / pot.net_liq * 100) if pot.net_liq else 0.0
    if over_default:
        size_line = (f":warning: ~${cost:,.0f} = *{pct_pot:.0f}% of pot* — ABOVE your {default_pct:.0%} "
                     f"default (1 contract is the smallest size). Tap :white_check_mark: only if you want this size.")
    else:
        size_line = (f"~${cost:,.0f} (*{pct_pot:.0f}% of pot*, your {default_pct:.0%} default). "
                     f"Reply `full size` to use all ~${pot.available_funds:,.0f} available.")
    msg = (head + f"*Order:* `{order_summary(resolved)}`\n"
           f"{size_line} Max loss = the debit.\n"
           f"*Sell levels (auto):* take profit ~${tp_price:.2f} (+{tp_pct:.0f}%) | "
           f"stop ~${sl_price:.2f} (-{sl_pct:.0f}%)\n"
           f":point_down: *Tap :white_check_mark: to BUY*, or REPLY to tweak: `full size`, levels (`tp 60 stop 30`), "
           f"direction (`flip` / `make it bearish`), or `just the call` / `make it a spread`. :x: to skip.")
    ts = approval.post_proposal(token, channel, msg)
    if ts:
        pending.append((ts, resolved, tp_pct, sl_pct, idea, over_default))
        audit(audit_path, audit_event, underlying=idea.underlying,
              conviction=idea.conviction, order=order_summary(resolved),
              profit_target_pct=tp_pct, stop_pct=sl_pct, over_default=over_default)
    return ts


async def run(args):
    cfg = yaml.safe_load(open(args.config))
    ibc, tr = cfg.get("ib", {}), cfg.get("trading", {})
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    channel = tr.get("slack_channel", "")
    approver_ids = set(tr.get("approver_ids", []))
    audit_path = tr.get("audit_path", "./audit.jsonl")
    journal_path = cfg.get("journal", {}).get("path", "./trades.log")

    conn = IBConnection(host=ibc.get("host", "127.0.0.1"), port=ibc.get("port", 4001),
                        client_id=(getattr(args, "client_id", None) or CLIENT_ID),
                        market_data_type=ibc.get("market_data_type", 1))
    if not await conn.connect(retries=10, retry_delay=30):
        # B: gateway looks down -> kick IBC to restart it, wait for auto-login, try once more.
        print("[WARN] no IBKR connection -- attempting IBC gateway restart")
        try:
            subprocess.run(["launchctl", "kickstart", "-k", "gui/%d/ai.alfred.ibgateway" % os.getuid()],
                           timeout=30, check=False)
        except Exception as e:
            print("[WARN] gateway kickstart failed: %s" % e)
        await asyncio.sleep(90)  # IBC auto-login ~60-90s (cannot bypass IBKR weekly forced 2FA)
        if not await conn.connect(retries=4, retry_delay=20):
            # A: tell the user in Slack instead of failing silently in a log they never see.
            print("[ERROR] no IBKR connection after restart")
            if token and channel:
                approval.post_proposal(token, channel,
                    ":warning: *Daily slate skipped -- IBKR gateway unreachable.* An auto-restart was tried; "
                    "it is likely logged out (IBKR's ~weekly forced 2FA, which auto-restart cannot bypass). "
                    "Do 2FA via ~/studio-screen.sh, then run slate-now (or ask Claude) for today's slate.")
            return 1
    ib = conn.ib
    try:
        disc_ts, disc_cands = None, []
        if args.ticker:
            # USER-DIRECTED: you name the trade; it runs the SAME price -> one-tap -> execute ->
            # journal -> exit-manage pipeline as the model slate (no bypass — you still tap to fire it).
            direction = "bullish" if args.right.upper() == "C" else "bearish"
            structure = args.structure or ("long call" if direction == "bullish" else "long put")
            ideas = [TradeIdea(underlying=args.ticker.upper(),
                               is_index=args.ticker.upper() in ("SPY", "QQQ", "IWM"),
                               direction=direction, structure=structure,
                               target_dte=args.dte, target_delta=args.delta,
                               est_debit_usd=0.0, conviction=int(args.conviction),
                               thesis=args.thesis, profit_target_pct=args.tp, stop_pct=args.stop)]
            audit(audit_path, "user_directed_proposal", underlying=args.ticker.upper(),
                  direction=direction, structure=structure, dte=args.dte, delta=args.delta)
        else:
            names = sorted({"SPY", "QQQ", "IWM"} | {n.upper() for n in tr.get("approved_names", [])})
            today = str(datetime.now(timezone.utc).date())
            quotes = await fetch_universe_quotes(ib, names)
            data = await research.gather(ib, names, single_names=[n for n in names if n not in ("SPY", "QQQ", "IWM")])
            brief = research.build_brief(today=today, quotes=quotes, universe=names, allow_any_name=True, **data)

            # Morning discovery: scout NEW watchlist candidates worth researching (not trades to place).
            try:
                cands = discover_names(tr.get("llm_endpoint"), tr.get("llm_model"), brief,
                                       exclude=set(names), timeout=1200,
                                       blocked=tr.get("blocked_names", []))
                if cands:
                    disc_cands = [t for t, _ in cands]
                    disc_ts = approval.post_proposal(token, channel,
                        ":mag: *Names to consider* (scouted this morning — NOT on your watchlist):\n"
                        + "\n".join(f"  • *{t}* — {why}" for t, why in cands)
                        + "\n_Reply *add TICKER* (or *add all*) right here to put any on the watchlist._")
                    audit(audit_path, "discovery", candidates=disc_cands)
            except Exception as e:
                audit(audit_path, "discovery_error", error=str(e))

            ideas = propose(tr.get("llm_endpoint"), tr.get("llm_model"), brief, timeout=1800, recommend=True)
            ideas.sort(key=lambda i: -i.conviction)
            audit(audit_path, "daily_recommend", count=len(ideas),
                  scores=[i.conviction for i in ideas])
            if not ideas:
                approval.post_proposal(token, channel,
                    ":calendar: *Daily slate* — MiniMax has no tradeable idea today (genuinely nothing it would recommend).")
                print("[INFO] no ideas"); return 0

        pot = await get_pot_snapshot(ib)
        default_pct = float(tr.get("max_trade_pct", 0.12))     # conservative default slice of the pot
        cons_budget = min(pot.available_funds, default_pct * pot.net_liq)
        pending = []  # (ts, ResolvedOrder, tp_pct, sl_pct, idea, over_default)
        for idea in ideas:
            # CONSERVATIVE DEFAULT: size to ~default_pct of the pot; go full only on an opt-in reply.
            await _post_idea(ib, idea, pot, default_pct, token, channel, audit_path, pending)

        # Watch the posted recs and place the ones you approve, until the deadline.
        deadline = time.monotonic() + args.watch_mins * 60
        done = set()
        added_watch = set()
        while (pending or disc_cands) and time.monotonic() < deadline \
                and (len(done) < len(pending) or len(added_watch) < len(disc_cands)):
            # Discovery thread: 'add TICKER' / 'add all' replies -> append to the watchlist
            if disc_ts and disc_cands and len(added_watch) < len(disc_cands):
                rep = approval._api("conversations.replies", token, {"channel": channel, "ts": disc_ts}, http_post=False)
                want = set()
                for m in (rep.get("messages", []) if rep.get("ok") else []):
                    if m.get("ts") == disc_ts:
                        continue
                    if approver_ids and m.get("user") not in approver_ids:
                        continue
                    want |= set(approval.parse_add_tickers(m.get("text", ""), disc_cands))
                new = sorted(want - added_watch)
                if new:
                    really = _append_watchlist(args.config, new)
                    added_watch |= set(new)
                    if really:
                        approval.post_proposal(token, channel,
                            f":white_check_mark: Added to watchlist: *{', '.join(really)}*")
                        audit(audit_path, "watchlist_added", tickers=really)
                        # SAME-DAY SUGGESTION: for each name you just added, ask the model for its single
                        # best idea on THAT name now; if conviction clears the bar, propose it (one-tap)
                        # immediately instead of waiting for tomorrow's slate.
                        min_conv = float(tr.get("add_suggest_min_conviction", 6))
                        for tk in really:
                            try:
                                idea = propose_one(tr.get("llm_endpoint"), tr.get("llm_model"), brief, tk, timeout=1800)
                            except Exception as e:
                                audit(audit_path, "add_suggest_error", ticker=tk, error=str(e))
                                continue
                            if not idea:
                                continue
                            if idea.conviction < min_conv:
                                approval.post_proposal(token, channel,
                                    f":information_source: *{tk}*: best idea today is {idea.direction} "
                                    f"{idea.structure} at conviction *{idea.conviction}/10* — below your same-day "
                                    f"bar ({min_conv:.0f}). It'll ride the daily slate.")
                                audit(audit_path, "add_suggest_below_bar", ticker=tk, conviction=idea.conviction)
                                continue
                            snap = await get_pot_snapshot(ib)
                            await _post_idea(ib, idea, snap, default_pct, token, channel, audit_path, pending,
                                             label="Added & suggested", audit_event="add_suggest_posted")
            for ts, r, tp_pct, sl_pct, idea, over_default in pending:
                if ts in done:
                    continue
                rxn = approval._api("reactions.get", token, {"channel": channel, "timestamp": ts}, http_post=False)
                reactions = (rxn.get("message", {}) or {}).get("reactions", []) if rxn.get("ok") else []
                rep = approval._api("conversations.replies", token, {"channel": channel, "ts": ts}, http_post=False)
                replies = [m for m in rep.get("messages", []) if m.get("ts") != ts] if rep.get("ok") else []
                if approval.decision_from_reactions(reactions, approver_ids) == "reject" \
                        or approval.decision_from_replies(replies, approver_ids, ts) == "reject":
                    done.add(ts); continue
                # adjusted sell levels + direction/structure + SIZE override from replies (latest wins)
                ov_tp = ov_sl = None
                ovr = {}
                full_size = False
                qty_ovr = {}
                for m in replies:
                    if approver_ids and m.get("user") not in approver_ids:
                        continue
                    a, b = approval.parse_levels(m.get("text", ""))
                    if a: ov_tp = a
                    if b: ov_sl = b
                    ovr.update(approval.parse_structure_override(m.get("text", "")))
                    qty_ovr.update(approval.parse_qty_override(m.get("text", "")))
                    if approval.parse_size_override(m.get("text", "")):
                        full_size = True
                approved = (approval.decision_from_reactions(reactions, approver_ids) == "approve"
                            or approval.decision_from_replies(replies, approver_ids, ts) == "approve"
                            or ov_tp or ov_sl or ovr or full_size or qty_ovr)
                if not approved:
                    continue
                # direction/structure change OR a 'full size' opt-in -> re-resolve before placing.
                # Budget = full available funds only when opted in (or it was already an over-default
                # full-size proposal); otherwise the conservative default slice.
                if ovr or full_size:
                    from dataclasses import replace as _replace
                    nd = idea.direction
                    if ovr.get("direction") == "flip":
                        nd = "bearish" if idea.direction == "bullish" else "bullish"
                    elif ovr.get("direction") in ("bullish", "bearish"):
                        nd = ovr["direction"]
                    ns = idea.structure
                    if ovr.get("structure") == "single":
                        ns = "long put" if nd == "bearish" else "long call"
                    elif ovr.get("structure") == "spread":
                        ns = "put debit spread" if nd == "bearish" else "call debit spread"
                    snap = await get_pot_snapshot(ib)
                    avail = (snap.available_funds if (full_size or over_default)
                             else min(snap.available_funds, default_pct * snap.net_liq))
                    r2, why2 = await _resolve(ib, _replace(idea, direction=nd, structure=ns), avail)
                    if r2 is not None:
                        r = r2
                    else:
                        approval.post_proposal(token, channel,
                            f":warning: couldn't build your override ({why2}) — placing the original `{order_summary(r)}`")
                # explicit position-size override ("1 contract", "half size") -> set qty on the resolved order
                if qty_ovr:
                    from dataclasses import replace as _replace
                    newq = qty_ovr["contracts"] if "contracts" in qty_ovr else max(1, round(r.qty * qty_ovr["fraction"]))
                    snap2 = await get_pot_snapshot(ib)
                    if r.limit * 100 * newq > snap2.available_funds + 1e-6:
                        affordable = max(1, int(snap2.available_funds // (r.limit * 100)))
                        approval.post_proposal(token, channel,
                            f":warning: {newq}x ~${r.limit*100*newq:,.0f} > available ${snap2.available_funds:,.0f} — placing {affordable}x instead.")
                        newq = affordable
                    if newq != r.qty:
                        r = _replace(r, qty=newq)
                eff_tp = max(20.0, min(500.0, ov_tp)) if ov_tp else tp_pct
                eff_sl = max(10.0, min(90.0, ov_sl)) if ov_sl else sl_pct
                buf_pct = float(tr.get("entry_limit_buffer_pct", 0.05))
                lmt = round(r.limit * (1 + buf_pct), 2)   # marketable -> entry fills instead of resting at mid
                order = Order(action="BUY", orderType="LMT", totalQuantity=r.qty, lmtPrice=lmt, tif="DAY")
                if r.short_contract is not None:
                    combo = conn.create_combo_contract(
                        r.underlying, [(r.contract.conId, "BUY"), (r.short_contract.conId, "SELL")])
                    ib.placeOrder(combo, order)
                else:
                    ib.placeOrder(r.contract, order)
                await asyncio.sleep(1.5)  # let it transmit before we move on
                with open(journal_path, "a") as f:
                    import json as _j
                    spread_j = ({"spread": {"short_con_id": r.short_contract.conId,
                                            "short_strike": r.short_strike,
                                            "width": abs(r.short_strike - r.strike)}}
                                if r.short_contract is not None else {})
                    f.write(_j.dumps({"ts": datetime.utcnow().isoformat(), "contract_id": r.contract.conId,
                                      "symbol": r.underlying, "right": r.right, "expiry": r.expiry,
                                      "strike": r.strike, "quantity": r.qty,
                                      "debit": round(r.limit * 100 * r.qty, 2),
                                      "profit_target_pct": eff_tp, "stop_pct": eff_sl,
                                      **spread_j}, default=str) + "\n")
                tag = " _(your levels)_" if (ov_tp or ov_sl) else ""
                approval.post_proposal(token, channel,
                    f":white_check_mark: *Placed* `{order_summary(r)}` — exits +{eff_tp:.0f}% / -{eff_sl:.0f}%{tag}")
                audit(audit_path, "daily_rec_executed", underlying=r.underlying, order=order_summary(r),
                      profit_target_pct=eff_tp, stop_pct=eff_sl)
                done.add(ts)
            await asyncio.sleep(15)
        print(f"[INFO] daily slate done — {len(done)}/{len(pending)} decided")
        return 0
    finally:
        await conn.disconnect()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch-mins", type=int, default=360, help="how long to watch for your taps")
    ap.add_argument("--config", default="config.yaml")
    # USER-DIRECTED proposal: when --ticker is set, skip the model slate and propose THIS trade
    # through the same one-tap approve -> execute -> journal -> exit-manage pipeline.
    ap.add_argument("--ticker", default=None, help="user-directed: underlying symbol (e.g. QQQ). Skips the model slate.")
    ap.add_argument("--right", default="C", choices=["C", "P", "c", "p"], help="C=call (bullish), P=put (bearish)")
    ap.add_argument("--dte", type=int, default=10, help="target days-to-expiry")
    ap.add_argument("--delta", type=float, default=0.35, help="target option delta (strike selection)")
    ap.add_argument("--structure", default="", help="override structure, e.g. 'call debit spread' (default: long call/put)")
    ap.add_argument("--tp", type=float, default=0.0, help="take-profit %% (0 = global default +100%%)")
    ap.add_argument("--stop", type=float, default=0.0, help="stop %% (0 = global default -50%%)")
    ap.add_argument("--conviction", type=int, default=6, help="conviction 1-10 (display only)")
    ap.add_argument("--thesis", default="User-directed proposal.", help="thesis line shown in the proposal")
    ap.add_argument("--client-id", type=int, default=None, dest="client_id", help="override IBKR clientId (avoid clash with the cron's 93)")
    raise SystemExit(asyncio.run(run(ap.parse_args())))
