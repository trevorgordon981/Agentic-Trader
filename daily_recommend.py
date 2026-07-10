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
import json
import os
import subprocess
import time
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

from exitmgr.account import get_pot_snapshot
from exitmgr.connection import IBConnection
from exitmgr.ibkr import Stock, Option, Order, pick_chain, strikes_near, underlying_price
from exitmgr.strategist import propose, discover_names, propose_one, TradeIdea
from exitmgr.trader import ResolvedOrder, order_summary, audit, _trading_day
from exitmgr import approval, construction, entry_safety, research, trade_capture, risk
from exitmgr.config import ConstructionConfig, construction_from_dict
from exitmgr.slate_lock import slate_active_guard
from exitmgr.market import fetch_universe_quotes, usable_price

CLIENT_ID = 93

# 5% cash-buffer: keep this fraction of NetLiq (account VALUE) liquid on every sizing path. Set from
# config (trading.cash_buffer_pct) in main(); the risk gate (exitmgr/risk.py) enforces the same on
# the trader loop. 2026-06-22.
CASH_BUFFER_PCT = 0.05

# 2026-07-01 constructor rework: construction gates (min-DTE floor, TP/SL clamp, structure
# sanity, premium/deployed/decay budgets). Overwritten from config.yaml `construction:` in
# run(); the module-global style matches CASH_BUFFER_PCT above. CONN/JOURNAL_PATH let the
# budget gates value the OPEN book (deployed premium + portfolio decay) from live positions.
CONS = ConstructionConfig()
# POT-TIERED TP RUNNER CEILING rows (caps.tp_tiers); set in run(). Empty => flat no-op.
CAPS_TP_TIERS = []
CONN = None            # the IBConnection, set in run()
JOURNAL_PATH = "./trades.log"
ERROR_CHANNEL = ""     # #error-logs -- unfilled-entry alarms; set from config in run()
FILLS_PATH = "./fills.log"  # entry-fill confirmations (SEPARATE file: trades.log consumers
                            # key newest-line-per-contract_id, so lifecycle lines can't go there)

# 2026-07-03 gate H2: the sector/correlation cap (risk.py #6b) + single-name-agg cap (#6) only
# ran on the autonomous trader path. The daily slate -- Trevor's PRIMARY entry path -- never
# called risk.evaluate_trade, so the flagship concentration protection was dormant where trades
# actually originate. We now SURFACE a warning (never hard-block; the human tap decides) if a
# candidate would breach either cap. _RISK_LIMITS is loaded from config.yaml `trading:` in run()
# the same minimal way run_trader.py builds RiskLimits (max_single_name_agg_pct keeps its 0.36
# dataclass default -- matching the trader, which never overrides it).
_RISK_LIMITS = risk.RiskLimits()


async def _open_book(ib_unused=None):
    """(net_debit, dte) pairs for the open long legs -- the budget gates' book. Best-effort:
    an IBKR hiccup yields an empty book (the per-trade caps still bind)."""
    try:
        return construction.open_book(await CONN.get_positions(), JOURNAL_PATH)
    except Exception as e:
        print(f"[WARN] open-book fetch failed (budget gates see an empty book): {e}")
        return []


async def _open_positions_for_risk(positions=None):
    """List[risk.OpenPosition] for the concentration warning (gate H2) -- mirrors
    trader._open_positions EXACTLY: journaled NET debit (max loss) per long leg keyed by con_id
    (newest wins), gross long-leg value as the conservative fallback, SPY/QQQ/IWM flagged index.
    This is the SAME $ premium-at-risk basis risk.py #6/#6b sum. Best-effort: any hiccup yields []
    (the warning simply can't fire -- it never blocks a proposal)."""
    positions = await CONN.get_positions() if positions is None else positions
    debits = {}
    p = Path(JOURNAL_PATH)
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("event"):  # lifecycle records (closed_by_tool etc.), not entries
                continue
            cid, d = rec.get("contract_id"), rec.get("debit")
            if cid is None or d is None:
                continue
            try:
                debits[int(cid)] = float(d)
            except (TypeError, ValueError):
                continue
    out = []
    for pd_ in (positions or {}).values():
        sym = (getattr(pd_, "symbol", "") or "").upper()
        is_index = sym in {"SPY", "QQQ", "IWM"}
        con_id = getattr(pd_, "con_id", None)
        gross = abs(getattr(pd_, "avg_cost", 0.0)) * 100 * abs(getattr(pd_, "quantity", 0))
        nd = debits.get(int(con_id)) if con_id is not None else None
        notional = float(nd) if nd is not None else gross
        out.append(risk.OpenPosition(sym, notional, is_index))
    return out


def _concentration_notes(open_positions, underlying, candidate_debit, is_index, pot, limits):
    """PURE (no I/O, never mutates, never raises on normal inputs): would ADDING this candidate
    breach the single-name-agg cap (#6) or the sector/correlated-cluster cap (#6b)? Returns a list
    of (warning_text, audit_kwargs); EMPTY when nothing breaches (so no head note is added and there
    is NO behavior change on an under-cap idea). Mirrors risk.py #6/#6b math exactly -- same $
    premium-at-risk basis, same caps, same 'index ETFs exempt' rule -- but is SURFACE-ONLY: the
    slate has a human tap, so we warn and let Trevor decide rather than hard-block."""
    notes = []
    u = (underlying or "").upper()
    if is_index or pot <= 0:
        return notes
    _EPS = 1e-9  # matches the EPS tolerance risk.evaluate_trade uses on these same comparisons
    # (a) aggregate single-name exposure cap (risk.py #6)
    name_exposure = sum(p.notional for p in open_positions if not p.is_index) + candidate_debit
    name_cap = limits.max_single_name_agg_pct * pot
    if name_exposure > name_cap + _EPS:
        notes.append((
            f":warning: concentration — single-name book would reach ${name_exposure:,.0f} "
            f"({name_exposure / pot * 100:.0f}% of pot, cap {limits.max_single_name_agg_pct:.0%})",
            dict(kind="single_name_agg", underlying=u, exposure=round(name_exposure, 2),
                 cap=round(name_cap, 2), pot=round(pot, 2)),
        ))
    # (b) aggregate SECTOR / correlated-cluster exposure cap (risk.py #6b)
    if limits.sector_map and limits.max_sector_agg_pct > 0:
        sec = risk.sector_of(u, limits.sector_map)
        sec_exposure = risk.sector_exposure(
            open_positions, u, candidate_debit, limits.sector_map).get(sec, 0.0)
        sec_cap = limits.max_sector_agg_pct * pot
        if sec_exposure > sec_cap + _EPS:
            notes.append((
                f":warning: concentration — sector '{sec}' would reach ${sec_exposure:,.0f} "
                f"({sec_exposure / pot * 100:.0f}% of pot, cap {limits.max_sector_agg_pct:.0%})",
                dict(kind="sector_agg", underlying=u, sector=sec,
                     exposure=round(sec_exposure, 2), cap=round(sec_cap, 2), pot=round(pot, 2)),
            ))
    return notes


def _watch_entry_fills(placed_watch, token, audit_path):
    """FILL VERIFICATION + alarm for entries placed this session (2026-07-01 audit: 5/15
    'executed' entries never filled -- the journal recorded intent, not fills). Called each
    watch-loop pass. On an observed fill -> append a confirmation to fills.log and audit it.
    Unfilled past construction.fill_alarm_minutes -> Slack #error-logs once from here (the
    exit manager's every-cycle alarm keeps escalating after this process exits)."""
    import json as _j
    for w in placed_watch:
        try:
            st = w["trade"].orderStatus
            status = st.status
        except Exception:
            continue
        if status == "Filled" and not w["filled_logged"]:
            _afp = getattr(st, "avgFillPrice", None)
            fill_px = float(_afp) if (_afp and _afp == _afp) else None
            try:
                from exitmgr.order import commission_from_trade as _comm_from_trade
                _late_comm = _comm_from_trade(w["trade"])
            except Exception:
                _late_comm = None
            try:
                with open(FILLS_PATH, "a") as f:
                    f.write(_j.dumps({"ts": datetime.utcnow().isoformat(), "event": "entry_fill",
                                      "decision_id": w.get("decision_id"),
                                      "contract_id": getattr(w["r"].contract, "conId", None),
                                      "symbol": w["r"].underlying,
                                      "order_id": getattr(getattr(w["trade"], "order", None), "orderId", None),
                                      "order_ref": getattr(getattr(w["trade"], "order", None), "orderRef", None),
                                      "status": status, "avg_fill_price": fill_px,
                                      "entry_commission": _late_comm,
                                      "quantity": w["r"].qty}) + "\n")
            except Exception as e:
                print(f"[WARN] fills.log write failed: {e}")
            audit(audit_path, "entry_filled", underlying=w["r"].underlying,
                  decision_id=w.get("decision_id"),
                  order_id=getattr(getattr(w["trade"], "order", None), "orderId", None),
                  avg_fill_price=fill_px)
            w["filled_logged"] = True
        elif (status != "Filled" and not w["alerted"]
                and time.monotonic() - w["t0"] > float(CONS.fill_alarm_minutes) * 60):
            w["alerted"] = True
            audit(audit_path, "entry_unfilled_alarm", underlying=w["r"].underlying,
                  status=status, minutes=CONS.fill_alarm_minutes)
            if token and ERROR_CHANNEL:
                approval.post_proposal(token, ERROR_CHANNEL,
                    f":hourglass_flowing_sand: *ENTRY UNFILLED* — `{order_summary(w['r'])}` has not filled "
                    f"after {CONS.fill_alarm_minutes:.0f} min (status {status}). Check the gateway; the "
                    f"trader's cycle alarm will keep escalating.")


def deployable_funds(pot):
    """Buying power we may actually deploy = available_funds minus a 5% cash reserve on NetLiq.
    Clamped at 0 so we never go negative. Whole-pot sizing therefore caps near 95%, never $0 cash."""
    floor = max(0.0, CASH_BUFFER_PCT) * (pot.net_liq or 0.0)
    return max(0.0, (pot.available_funds or 0.0) - floor)


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


async def _resolve(ib, idea, available, net_liq=None):
    """Pick the concrete option (nearest expiry to target DTE, strike by delta) and price it via
    OPRA. Single-leg long call/put; sizes to >=1 contract within available funds. None if it
    can't price or even one contract is unaffordable."""
    right = "C" if idea.direction == "bullish" else "P"
    stk = (await ib.qualifyContractsAsync(Stock(idea.underlying, "SMART", "USD")))[0]
    params = await ib.reqSecDefOptParamsAsync(idea.underlying, "", "STK", stk.conId)
    if not params:
        return None, "no option chain"
    p = pick_chain(params, idea.underlying)
    if p is None:
        return None, "no SMART option chain"
    # MIN-DTE FLOOR (2026-07-01 gate A1): median 17.5 DTE at entry bled 5.9-12.5%/day theta
    # and killed both direction-RIGHT losers. Nearest expiry to target among those >= min_dte
    # (prefer 25-45); a too-short model target is ADJUSTED up (annotated in journal + Slack),
    # rejected only when no valid expiry exists at all.
    expiry, chosen_dte, dte_adjusted = construction.pick_expiry(
        p.expirations, idea.target_dte, CONS.min_dte, CONS.prefer_dte_max)
    if expiry is None:
        return None, f"no expiry >= {CONS.min_dte} DTE (min-DTE floor)"
    spot = await underlying_price(ib, stk)
    # IBKR gross-position rule: ~100*spot per contract must not exceed 30x net_liq. High-priced
    # underlyings (NVDA ~$233, MU ~$989) blow this cap on a small account -> reject before IBKR Error 201.
    if net_liq and spot and 100 * spot > 0.9 * 30 * net_liq:
        return None, (f"underlying ${spot:,.0f}/sh too high-priced for ${net_liq:,.0f} acct: "
                      f"1-contract notional ${100*spot:,.0f} exceeds 30x net_liq cap ${30*net_liq:,.0f}")
    cands = [Option(idea.underlying, expiry, k, right, "SMART") for k in strikes_near(p.strikes, spot)]
    qualified = await ib.qualifyContractsAsync(*cands)
    tickers = await ib.reqTickersAsync(*[c for c in qualified if getattr(c, "conId", None)])
    # LONG-LEG DELTA BAND (2026-07-01 gate A3): target ~0.55-0.65 delta -- a leg that is
    # already working, not a lottery ticket. The model's target_delta is clamped into the band.
    tgt_delta = construction.effective_delta(idea.target_delta, CONS)
    best, best_err, best_greeks = None, 1e9, None
    best_bidask = (None, None)
    by_strike = {}
    quote_by_strike = {}
    for tk in tickers:
        # -1 SENTINEL GUARD (2026-07-03): use market.usable_price so an IB -1.0/NaN "no quote"
        # sentinel on bid/ask/last can never leak into the mid (the old `tk.last or 0` let a -1
        # last through). One-sided junk falls back to last; junk last falls back to 0 (skipped).
        if usable_price(tk.bid) and usable_price(tk.ask):
            mid = (tk.bid + tk.ask) / 2
        else:
            mid = tk.last if usable_price(tk.last) else 0
        if not (mid == mid and mid > 0):
            continue
        k = float(getattr(tk.contract, "strike", 0) or 0)
        if k:
            by_strike[k] = (mid, tk.contract)
            quote_by_strike[k] = (getattr(tk, "bid", None), getattr(tk, "ask", None))
        g = getattr(tk, "modelGreeks", None) or getattr(tk, "lastGreeks", None)
        if g and g.delta is not None:
            err = abs(abs(g.delta) - tgt_delta)
            if err < best_err:
                best, best_err, best_greeks = (tk.contract, mid), err, g
                best_bidask = (getattr(tk, "bid", None), getattr(tk, "ask", None))
    if not best and by_strike and spot:
        # No greeks streaming -> conservative fallback (gate A3): nearest-to-spot priced
        # strike, only if within the near-spot band; else reject rather than guess.
        k_near = min(by_strike, key=lambda k: abs(k - spot))
        if abs(k_near - spot) <= CONS.strike_near_spot_pct * spot:
            best = (by_strike[k_near][1], by_strike[k_near][0])
            best_bidask = quote_by_strike.get(k_near, (None, None))
    if not best:
        return None, "no priced strike (OPRA active?)"
    contract, mid = best
    atm_iv = getattr(best_greeks, "impliedVol", None) if best_greeks else None
    # LOTTERY-LONG check (gate A3): the long strike may not sit further OTM than ~1 expected
    # move for the horizon (SNDK needed +3.5% in 15d just to REACH its long strike).
    ok, why = construction.long_strike_ok(float(contract.strike), spot, right, chosen_dte, atm_iv, CONS)
    if not ok:
        return None, why
    def _quote_value(value):
        try:
            value = float(value)
            return value if value == value and value > 0 else 0.0
        except (TypeError, ValueError):
            return 0.0

    _long_bid, _long_ask = (_quote_value(x) for x in best_bidask)
    enrich = dict(spot=float(spot or 0.0),
                  entry_delta=float(abs(best_greeks.delta)) if (best_greeks and best_greeks.delta is not None) else 0.0,
                  entry_iv=float(atm_iv) if (atm_iv and atm_iv == atm_iv) else 0.0,
                  dte=int(chosen_dte), dte_adjusted=bool(dte_adjusted),
                  entry_bid=_long_bid, entry_ask=_long_ask,
                  entry_spread_pct=(round((_long_ask - _long_bid) / mid * 100, 2)
                                    if mid > 0 and _long_ask >= _long_bid > 0 else 0.0),
                  quote_observed_at=time.monotonic())

    # DEBIT SPREAD: buy the delta-selected leg, sell a further-OTM same-type leg to cut the cost.
    if "spread" in (idea.structure or "").lower():
        from exitmgr.trader import pick_spread_short, size_within_cap
        # STRUCTURE SANITY (gate A3): the short leg is constrained to ~1 expected move of
        # spot (conservative width fallback without IV) inside pick_spread_short -- the NOK
        # 14/25-on-a-$13.62-stock lottery vertical can no longer be constructed.
        pick = pick_spread_short([(k, m) for k, (m, _) in by_strike.items()],
                                 float(contract.strike), mid, right, available,
                                 spot=spot, dte=chosen_dte, atm_iv=atm_iv, cons=CONS)
        if pick:
            short_strike, net = pick
            short_contract = by_strike[short_strike][1]
            _short_bid, _short_ask = (_quote_value(x) for x in quote_by_strike[short_strike])
            if _long_bid > 0 and _long_ask > 0 and _short_bid > 0 and _short_ask > 0:
                enrich["entry_bid"] = round(_long_bid - _short_ask, 4)
                enrich["entry_ask"] = round(_long_ask - _short_bid, 4)
                _net_mid = (enrich["entry_bid"] + enrich["entry_ask"]) / 2
                enrich["entry_spread_pct"] = (
                    round((enrich["entry_ask"] - enrich["entry_bid"]) / _net_mid * 100, 2)
                    if _net_mid > 0 and enrich["entry_ask"] >= enrich["entry_bid"] else 0.0)
            else:
                enrich["entry_bid"] = enrich["entry_ask"] = 0.0
                enrich["entry_spread_pct"] = 0.0
            # SYMMETRIC HARD-REJECT (2026-07-03 gap-fix): mirror the trader loop's size_within_cap
            # -- if even ONE spread contract exceeds the available cash-after-reserve budget, REJECT
            # the idea (it just isn't offered) instead of the old `max(1, ...)` that force-shipped
            # qty=1 OVER budget. `available` already reflects the 5% cash reserve + premium cap.
            qty = size_within_cap(net * 100, available, available)
            if qty is None:
                return None, (f"one spread contract ${net*100:,.0f} > available ${available:,.0f}")
            return ResolvedOrder(idea.underlying, right, expiry, float(contract.strike), qty, net,
                                 contract, short_strike=short_strike, short_contract=short_contract,
                                 **enrich), None
        # no affordable short leg -> fall through to the single long leg

    if mid * 100 > available + 1e-6:
        return None, f"one contract ${mid*100:,.0f} > available ${available:,.0f}"
    qty = max(1, int(available // (mid * 100)))
    return ResolvedOrder(idea.underlying, right, expiry, float(contract.strike), qty, round(mid, 2),
                         contract, **enrich), None


def _daily_cap_rejected(stage, reason, idea, resolved):
    """Record-only (v2): append a REJECTED row for a slate idea killed by a gate/constructor.
    Never raises into the slate path."""
    try:
        trade_capture.capture_rejected(
            trade_capture.dataset_dir(JOURNAL_PATH), source="daily_slate",
            symbol=getattr(idea, "underlying", None), reason=reason, stage=stage,
            idea=idea, structure=getattr(idea, "structure", None),
            right=getattr(resolved, "right", None), strike=getattr(resolved, "strike", None),
            expiry=getattr(resolved, "expiry", None),
            order=(order_summary(resolved) if resolved is not None else None))
    except Exception as _re:
        print(f"[WARN] daily-slate capture_rejected failed (continuing): {_re}")


async def _post_idea(ib, idea, pot, default_pct, token, channel, audit_path, pending,
                     label="Daily rec", audit_event="daily_rec_posted",
                     candidates=None, raw_strategist=None, market_context=None, regime=None,
                     technical_card=None, cot=None):
    """Resolve an idea to a concrete priced order, post it to #approvals with one-tap, and append it
    to `pending` so the watch loop manages approval/execution. Returns the Slack ts (or None).
    Shared by the daily slate and the same-day 'add a name -> suggest it now' path."""
    deployable = deployable_funds(pot)  # available_funds minus the 5% cash reserve
    # PREMIUM CAP (2026-07-01 gate A4): no trade's premium may exceed max_premium_pct (15%)
    # of net-liq -- clamps BOTH the default slice and the full-size opt-in path (82-103% of
    # the pot was deployed at times; one gap-down = -27% account day).
    _max_prem = construction.max_premium_budget(pot.net_liq, CONS)
    if _max_prem > 0:
        deployable = min(deployable, _max_prem)
    cons_budget = min(deployable, default_pct * pot.net_liq)
    resolved, why = await _resolve(ib, idea, cons_budget, net_liq=pot.net_liq)
    over_default = False
    if not resolved:
        # too pricey for the default slice -> offer it at full (buffer+premium-capped) size so you can opt in
        resolved, why = await _resolve(ib, idea, deployable, net_liq=pot.net_liq)
        over_default = resolved is not None
    # RISK SCREEN (mirrors the trader loop): block genuinely oversized orders. This account is
    # long-debit-only (long calls/puts + debit spreads), so the real capital at risk is the NET
    # DEBIT (max loss = limit*100*qty), NOT the strike notional. The old screen measured strike
    # notional (~1.1x/2.4x sum-of-strikes*100) vs 30x NetLiq, which structurally rejected every
    # cheap defined-risk debit spread (e.g. ORCL 162.5/160P @ $1.25 = $125 risk read as ~$32k).
    # Cap the actual max loss at 30x NetLiq; available-funds already bounds it to the pot.
    if resolved is not None and pot.net_liq:
        _eg = resolved.limit * 100 * resolved.qty  # max loss / net debit = capital at risk
        if _eg > 0.95 * 30 * pot.net_liq:
            audit(audit_path, "daily_rec_gross_rejected", underlying=idea.underlying,
                  order=order_summary(resolved), est_gross=round(_eg), cap=round(30 * pot.net_liq))
            _daily_cap_rejected("gross", f"capital at risk ${_eg:,.0f} exceeds 30x-NetLiq cap",
                                idea, resolved)
            approval.post_proposal(token, channel,
                f":no_entry: *{idea.underlying}* {order_summary(resolved)} skipped — capital at risk "
                f"${_eg:,.0f} exceeds the 30x-NetLiq cap (${30*pot.net_liq:,.0f}). Too large for this ${pot.net_liq:,.0f} pot.")
            return None
    # BUDGET GATES (2026-07-01 gate A4): deployed premium <=40% of net-liq and theta decay
    # <=1%/day per trade / <=4%/day portfolio, valued against the live open book.
    if resolved is not None and pot.net_liq:
        _debit = resolved.limit * 100 * resolved.qty
        ok_b, why_b = construction.check_budget(_debit, resolved.dte, pot.net_liq,
                                                await _open_book(), CONS)
        if not ok_b:
            audit(audit_path, "budget_rejected", underlying=idea.underlying,
                  order=order_summary(resolved), reasons=why_b)
            _daily_cap_rejected("budget", why_b, idea, resolved)
            approval.post_proposal(token, channel,
                f":no_entry: *{idea.underlying}* {order_summary(resolved)} skipped — budget gate: "
                + "; ".join(why_b))
            return None
    # EARNINGS BLACKOUT (2026-07-03 gate A5): a DEBIT held THROUGH an earnings print is an
    # IV-crush loser by construction (IV collapses post-print; the long premium bleeds even
    # when direction is right). Block if a KNOWN next-earnings date (research.days_to_earnings
    # via yfinance) lands within the holding horizon (on/before expiry + cushion). FAIL-OPEN
    # on unknown earnings: never hard-block, but flag it 'unchecked' below (not silent-clear).
    _earn_unchecked = False
    if resolved is not None:
        _entry = datetime.now(timezone.utc).date()
        try:
            _edays = research.days_to_earnings(idea.underlying)
        except Exception as _ee:
            print(f"[WARN] earnings lookup failed for {idea.underlying} (fail-open, unchecked): {_ee}")
            _edays = None
        _earn_date = (_entry + timedelta(days=_edays)) if _edays is not None else None
        _earn_unchecked = _earn_date is None
        ok_e, why_e = construction.earnings_ok(_entry, resolved.expiry, _earn_date, CONS)
        if not ok_e:
            audit(audit_path, "earnings_blackout_rejected", underlying=idea.underlying,
                  order=order_summary(resolved), reason=why_e)
            _daily_cap_rejected("earnings_blackout", why_e, idea, resolved)
            approval.post_proposal(token, channel,
                f":no_entry: *{idea.underlying}* {order_summary(resolved)} skipped — {why_e}")
            return None
    # EARLY-ASSIGNMENT / EX-DIV RISK (2026-07-03 gate A6): a DEBIT SPREAD whose ITM short leg
    # heads into an ex-dividend date can be assigned EARLY (a counterparty exercises the ITM
    # short to grab the dividend, converting the spread). Applies ONLY to spreads -- a single
    # long leg has no short to be assigned. Default disposition is WARN (surface the risk, still
    # allow); a hard block only when construction.assignment_block_hard is set. FAIL-OPEN on an
    # unknown ex-div date (flagged 'unchecked' below, never silent-clear).
    _assign_warn = ""
    _assign_unchecked = False
    if resolved is not None and resolved.short_contract is not None:
        _entry_a = datetime.now(timezone.utc).date()
        try:
            _xdays = research.days_to_ex_dividend(idea.underlying)
        except Exception as _xe:
            print(f"[WARN] ex-div lookup failed for {idea.underlying} (fail-open, unchecked): {_xe}")
            _xdays = None
        _exdiv_date = (_entry_a + timedelta(days=_xdays)) if _xdays is not None else None
        _assign_unchecked = _exdiv_date is None
        ok_a, why_a = construction.assignment_risk_ok(
            resolved.short_strike, resolved.spot, resolved.right, resolved.expiry,
            _exdiv_date, resolved.dte, CONS)
        if not ok_a:
            audit(audit_path, "assignment_risk_rejected", underlying=idea.underlying,
                  order=order_summary(resolved), reason=why_a)
            _daily_cap_rejected("assignment_risk", why_a, idea, resolved)
            approval.post_proposal(token, channel,
                f":no_entry: *{idea.underlying}* {order_summary(resolved)} skipped — {why_a}")
            return None
        if why_a:
            _assign_warn = why_a
            audit(audit_path, "assignment_risk_warn", underlying=idea.underlying,
                  order=order_summary(resolved), reason=why_a)
    head = (f":calendar: *{label} — {idea.underlying}* {idea.direction} {idea.structure}\n"
            f"Conviction *{idea.conviction}/10* — {_score_tag(idea.conviction)}\n"
            f"_Thesis:_ {idea.thesis}\n")
    if not resolved:
        _daily_cap_rejected("not_placeable", why, idea, None)
        approval.post_proposal(token, channel, head + f"_(not placeable: {why})_")
        return None
    if resolved.dte_adjusted:
        # A1 annotation: the model's expiry was ADJUSTED up to the min-DTE floor.
        head += (f"_:calendar: expiry adjusted to *{resolved.dte} DTE* (model asked ~{idea.target_dte}; "
                 f"min-DTE floor {CONS.min_dte} — short DTE was the audit's biggest theta killer)_\n")
        audit(audit_path, "dte_adjusted", underlying=idea.underlying,
              requested_dte=idea.target_dte, adjusted_dte=resolved.dte, min_dte=CONS.min_dte)
    if _earn_unchecked:
        # A5: no earnings date available -- the IV-crush blackout could NOT be checked.
        # Surfaced so an unchecked trade is never presented as verified-clear of earnings.
        head += "_:grey_question: earnings date unknown — IV-crush blackout UNCHECKED_\n"
    if _assign_warn:
        # A6 (warn disposition): ITM short leg into ex-div -- surface the early-assignment risk
        # but still allow the trade (it's manageable; hard-block only when configured).
        head += f"_:warning: {_assign_warn}_\n"
    elif _assign_unchecked:
        # A6: no ex-div date available for a spread -- assignment risk could NOT be checked.
        head += "_:grey_question: ex-dividend date unknown — early-assignment risk UNCHECKED_\n"
    # CONCENTRATION / CORRELATION WARNING (2026-07-03 gate H2, SURFACE-ONLY): the daily slate is
    # Trevor's PRIMARY entry path but never called risk.evaluate_trade, so the sector/correlation
    # cap (risk.py #6b) and single-name-agg cap (#6) never ran where trades originate. SURFACE (do
    # NOT hard-block) if ADDING this trade would breach either -- the human tap still decides,
    # mirroring the earnings-unchecked / ex-div-warn disposition above. FAIL-SAFE: any error logs
    # and continues; a computation hiccup never blocks a proposal.
    try:
        _cnotes = _concentration_notes(
            await _open_positions_for_risk(), idea.underlying,
            resolved.limit * 100 * resolved.qty,   # max loss = capital at risk (same $ basis as risk.py)
            idea.is_index, risk.effective_pot(pot.net_liq, _RISK_LIMITS.pot_cap_usd), _RISK_LIMITS)
        for _txt, _akw in _cnotes:
            head += f"_{_txt}_\n"
            audit(audit_path, "concentration_warning", order=order_summary(resolved), **_akw)
    except Exception as _ce:
        print(f"[WARN] concentration check failed for {getattr(idea, 'underlying', '?')} (continuing): {_ce}")
    cost = resolved.limit * 100 * resolved.qty
    # TP/SL CLAMP (2026-07-01 gate A2): +75-100% targets were touched 0/9 times while +20-35%
    # MFE was reached by 4/8 -- clamp any model TP into the 25-35 band (default +30%); the
    # stop defaults -30% (was -50%) and a model stop may only be TIGHTER.
    # POT-TIERED TP CEILING (2026-07-03): scale ONLY the runner ceiling + default target with the
    # LIVE pot; the model's TP still clamps into [tp_min, tp_max]. A per-call cons copy so the shared
    # CONS singleton is never mutated. Stop (sl_pct) and tp_min are untouched. Empty tiers => no-op.
    _tp_max, _tp_def = construction.tp_tier_for_pot(
        pot.net_liq, CAPS_TP_TIERS, CONS.tp_max_pct, CONS.tp_pct)
    _cons_tp = replace(CONS, tp_max_pct=_tp_max, tp_pct=_tp_def)
    tp_pct, sl_pct = construction.clamp_tp_sl(idea.profit_target_pct, idea.stop_pct, _cons_tp)
    tp_price = resolved.limit * (1 + tp_pct / 100.0)
    sl_price = resolved.limit * (1 - sl_pct / 100.0)
    pct_pot = (cost / pot.net_liq * 100) if pot.net_liq else 0.0
    if over_default:
        size_line = (f":warning: ~${cost:,.0f} = *{pct_pot:.0f}% of pot* — ABOVE your {default_pct:.0%} "
                     f"default (1 contract is the smallest size). Tap :white_check_mark: only if you want this size.")
    else:
        size_line = (f"~${cost:,.0f} (*{pct_pot:.0f}% of pot*, your {default_pct:.0%} default). "
                     f"Reply `full size` to use ~${deployable:,.0f} (keeps a 5% cash buffer).")
    decision_id = entry_safety.new_decision_id()
    resolved.decision_id = decision_id
    msg = (head + f"*Order:* `{order_summary(resolved)}`\n"
           f"{size_line} Max loss = the debit.\n"
           f"*Sell levels (auto):* take profit ~${tp_price:.2f} (+{tp_pct:.0f}%) | "
           f"stop ~${sl_price:.2f} (-{sl_pct:.0f}%)\n"
           f":point_down: *Tap :white_check_mark: to BUY*, or REPLY to tweak: `full size`, levels (`tp 60 stop 30`), "
           f"direction (`flip` / `make it bearish`), or `just the call` / `make it a spread`. :x: to skip.\n"
           f"_Decision ID: `{decision_id}` — approval expires in 5 minutes._")
    ts = approval.post_proposal(token, channel, msg)
    if ts:
        pending.append((ts, resolved, tp_pct, sl_pct, idea, over_default,
                        time.monotonic(), decision_id, 0))
        audit(audit_path, audit_event, underlying=idea.underlying,
              conviction=idea.conviction, order=order_summary(resolved),
              profit_target_pct=tp_pct, stop_pct=sl_pct, over_default=over_default,
              decision_id=decision_id)
        # DECISION CAPTURE (v2, record-only): the full DECISION -> ENTRY context for a posted
        # slate idea -- raw strategist reasoning, EVERY candidate + conviction, the chosen idea,
        # construction (clamped tp/sl, dte adjust), regime, and the RAG/news/journal brief.
        # con_id is unknown here (nothing placed yet); the record is joined to the closed trade
        # at close by symbol+strike+expiry+right. Never raises into the slate path.
        try:
            trade_capture.capture_decision(
                trade_capture.dataset_dir(JOURNAL_PATH), source="daily_slate",
                symbol=idea.underlying, right=resolved.right, strike=resolved.strike,
                expiry=resolved.expiry,
                structure=("spread" if resolved.short_contract is not None else "single"),
                con_id=None, chosen_idea=idea, candidates=candidates,
                raw_strategist=raw_strategist, cot=cot, market_context=market_context, regime=regime,
                technical_card=technical_card,
                construction={"tp_pct": tp_pct, "sl_pct": sl_pct, "dte": resolved.dte,
                              "dte_adjusted": resolved.dte_adjusted, "qty": resolved.qty,
                              "limit": resolved.limit, "over_default": over_default,
                              "short_strike": resolved.short_strike},
                sizing={"cost": cost, "pct_pot": pct_pot, "net_liq": pot.net_liq,
                        "available_funds": pot.available_funds},
                extra={"label": label, "order": order_summary(resolved),
                       "decision_id": decision_id})
        except Exception as _dce:
            print(f"[WARN] daily-slate capture_decision failed (continuing): {_dce}")
    return ts


async def run(args):
    global CASH_BUFFER_PCT, CONS, CONN, JOURNAL_PATH, ERROR_CHANNEL, _RISK_LIMITS, CAPS_TP_TIERS
    cfg = yaml.safe_load(open(args.config))
    CAPS_TP_TIERS = (cfg.get("caps") or {}).get("tp_tiers") or []   # 2026-07-03 pot-tiered TP ceiling
    ibc, tr = cfg.get("ib", {}), cfg.get("trading", {})
    CASH_BUFFER_PCT = float(tr.get("cash_buffer_pct", 0.05))  # keep this % of NetLiq liquid
    CONS = construction_from_dict(cfg.get("construction"))    # 2026-07-01 constructor-rework gates
    _RISK_LIMITS = entry_safety.risk_limits_from_config(tr)
    ERROR_CHANNEL = tr.get("error_channel", "") or tr.get("alerts_channel", "")
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    channel = tr.get("slack_channel", "")
    approver_ids = set(tr.get("approver_ids", []))
    audit_path = tr.get("audit_path", "./audit.jsonl")
    journal_path = cfg.get("journal", {}).get("path", "./trades.log")
    JOURNAL_PATH = journal_path

    # Entry stand-down is checked before any broker/model/network activity and again immediately
    # before every placeOrder. Config/marker I/O errors block rather than silently clearing a halt.
    _markers = entry_safety.entry_markers_clear(
        config_path=args.config,
        kill_switch_path=(cfg.get("kill_switch") or {}).get("path"))
    if not _markers.allowed:
        print("[BLOCKED] " + "; ".join(_markers.reasons))
        return 2

    conn = IBConnection(host=ibc.get("host", "127.0.0.1"), port=ibc.get("port", 4001),
                        client_id=(getattr(args, "client_id", None) or CLIENT_ID),
                        market_data_type=ibc.get("market_data_type", 1))
    CONN = conn  # budget gates value the open book through this connection
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
        _raw_slate = None    # verbatim strategist output (model path), for v2 decision capture
        _slate_cot = None    # [m3cot] chain-of-thought (reasoning_content), separate from the answer
        brief = None         # the RAG/news/journal/quote brief fed to the model (model path)
        _slate_price_stats = None  # per-name technical indicators (momentum/vol/IVR) fed to the model
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
            _all = sorted({"SPY", "QQQ", "IWM"} | {n.upper() for n in tr.get("approved_names", [])})
            _core = ["SPY", "QQQ", "IWM"]; _watch = [n for n in _all if n not in _core]
            _off = datetime.now(timezone.utc).timetuple().tm_yday % max(1, len(_watch))
            names = _core + (_watch[_off:] + _watch[:_off])[:35]  # rotating deep-research cap; all approved names still tradeable via open universe
            today = str(datetime.now(timezone.utc).date())
            quotes = await fetch_universe_quotes(ib, names)
            data = await research.gather(ib, names, single_names=[n for n in names if n not in ("SPY", "QQQ", "IWM")])
            brief = research.build_brief(today=today, quotes=quotes, universe=names, allow_any_name=True, **data)
            _slate_price_stats = data.get("price_stats")  # the technical card fed to the model this slate

            # Morning discovery: scout NEW watchlist candidates worth researching (not trades to place).
            # SOFT-MUTEX (2026-06-23): hold the slate-active flag across the discovery+propose model
            # burst so the trader defers its exit-management model call instead of colliding with us.
            _slate_gen = slate_active_guard(); _slate_gen.__enter__()
            try:
                cands = discover_names(tr.get("llm_endpoint"), tr.get("llm_model"), brief,
                                       exclude=set(names), timeout=400,
                                       blocked=tr.get("blocked_names", []))
                if cands:
                    disc_cands = [t for t, _ in cands]
                    disc_ts = approval.post_proposal(token, channel,
                        ":mag: *Names to consider* (scouted this morning — NOT on your watchlist):\n"
                        + "\n".join(f"  • *{t}* — {why}" for t, why in cands)
                        + "\n_Reply *add TICKER* (or *add all*) right here to put any on the watchlist._"
                        + "\n_Anytime (even outside this window): just ask Alfred to *add TICKER*._")
                    audit(audit_path, "discovery", candidates=disc_cands)
            except Exception as e:
                audit(audit_path, "discovery_error", error=str(e))

            try:
                _res = propose(tr.get("llm_endpoint"), tr.get("llm_model"), brief,
                               timeout=400, recommend=True, return_cot=True)
                # robust to all shapes: (ideas, raw, cot) 3-tuple, (ideas, raw) 2-tuple, or a
                # bare list (older/mocked propose). cot is optional/None-safe.
                if isinstance(_res, tuple) and len(_res) == 3:
                    ideas, _raw_slate, _slate_cot = _res
                elif isinstance(_res, tuple) and len(_res) == 2:
                    ideas, _raw_slate = _res
                else:
                    ideas = _res
            except Exception as e:
                audit(audit_path, "propose_error", error=str(e))
                approval.post_proposal(token, channel,
                    f":warning: *Daily slate* — couldn't reach the model to generate ideas ({e}). Retry with slate-now.")
                print(f"[ERROR] propose failed: {e}")
                _slate_gen.__exit__(None, None, None)
                return 1
            ideas.sort(key=lambda i: -i.conviction)
            audit(audit_path, "daily_recommend", count=len(ideas),
                  scores=[i.conviction for i in ideas])
            _slate_gen.__exit__(None, None, None)  # generation burst done -> release the soft-mutex
            if not ideas:
                approval.post_proposal(token, channel,
                    ":calendar: *Daily slate* — MiniMax has no tradeable idea today (genuinely nothing it would recommend).")
                # RECORD-ONLY (v2): learn from the pass -- capture the NO_TRADE with the raw model
                # output + the brief that fed it. Never raises into the slate path.
                try:
                    trade_capture.capture_no_trade(
                        trade_capture.dataset_dir(JOURNAL_PATH), source="daily_slate",
                        reason="empty_slate", raw_strategist=_raw_slate, cot=_slate_cot, market_context=brief)
                except Exception as _ce:
                    print(f"[WARN] daily-slate no_trade capture failed (continuing): {_ce}")
                print("[INFO] no ideas"); return 0

        pot = await get_pot_snapshot(ib)
        default_pct = float(tr.get("max_trade_pct", 0.12))     # conservative default slice of the pot
        cons_budget = min(pot.available_funds, default_pct * pot.net_liq)
        # (ts, resolved, tp, sl, idea, over_default, posted_monotonic, decision_id, revision)
        pending = []
        placed_watch = []  # fill-verification watch for orders placed this session (2026-07-01)

        # Book review + funding rotation (2026-06-18): synopsis of hold/trim/sell per open position,
        # and if the top idea outruns available cash, which position to sell to fund it. Advisory.
        try:
            from portfolio import review_positions, format_synopsis
            _top = ideas[0]
            _rev = await review_positions(ib, idea={"symbol": _top.underlying, "structure": _top.structure,
                                                    "est_cost_usd": round(_top.est_debit_usd), "conviction": _top.conviction})
            if _rev.get("book"):
                approval.post_proposal(token, channel, format_synopsis(_rev))
                audit(audit_path, "book_review", reviews=_rev.get("reviews"), rotation=_rev.get("rotation"))
        except Exception as _e:
            audit(audit_path, "review_error", error=str(_e))
        for idea in ideas:
            # CONSERVATIVE DEFAULT: size to ~default_pct of the pot; go full only on an opt-in reply.
            await _post_idea(ib, idea, pot, default_pct, token, channel, audit_path, pending,
                             candidates=ideas, raw_strategist=_raw_slate, cot=_slate_cot, market_context=brief,
                             technical_card=_slate_price_stats)

        # Watch the posted recs and place the ones you approve, until the deadline.
        deadline = time.monotonic() + args.watch_mins * 60
        done = set()
        added_watch = set()
        # 2026-07-01: the loop also stays alive while a PLACED order is still unfilled, so the
        # fill watch + unfilled alarm keep running until the fill (or the deadline).
        while time.monotonic() < deadline and (
                ((pending or disc_cands)
                 and (len(done) < len(pending) or len(added_watch) < len(disc_cands)))
                or any(not w["filled_logged"] for w in placed_watch)):
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
                            _one_raw = None
                            _one_cot = None
                            try:
                                with slate_active_guard():
                                    _one = propose_one(tr.get("llm_endpoint"), tr.get("llm_model"), brief, tk,
                                                       timeout=400, return_cot=True)
                                # robust to all shapes: (idea, raw, cot) 3-tuple, (idea, raw) 2-tuple,
                                # or a bare TradeIdea|None (older/mocked). cot is optional/None-safe.
                                if isinstance(_one, tuple) and len(_one) == 3:
                                    idea, _one_raw, _one_cot = _one
                                elif isinstance(_one, tuple) and len(_one) == 2:
                                    idea, _one_raw = _one
                                else:
                                    idea = _one
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
                                             label="Added & suggested", audit_event="add_suggest_posted",
                                             candidates=[idea], raw_strategist=_one_raw, cot=_one_cot,
                                             market_context=brief, technical_card=_slate_price_stats)
            for ts, r, tp_pct, sl_pct, idea, over_default, posted_at, decision_id, revision in pending:
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
                # C5 (2026-07-09): a BUY fires ONLY on an EXPLICIT approve (✅ reaction / "approve"
                # reply). A bare TP/SL/qty/structure tweak modifies the PENDING order but must NOT by
                # itself place the trade -- previously any tweak set approved=True and bought. The
                # tweak replies persist in the thread and are re-parsed every poll, so they are still
                # applied to the order once the explicit approve lands.
                approved = (approval.decision_from_reactions(reactions, approver_ids) == "approve"
                            or approval.decision_from_replies(replies, approver_ids, ts) == "approve")
                if not approved:
                    continue
                _age = entry_safety.approval_expired(posted_at)
                if not _age.allowed:
                    approval.post_proposal(token, channel,
                        f":hourglass: Approval expired — *{r.underlying}* was NOT placed. "
                        "Generate a fresh slate to approve a current quote.")
                    audit(audit_path, "approval_expired", underlying=r.underlying,
                          decision_id=decision_id, reasons=_age.reasons)
                    done.add(ts)
                    continue

                # Always reconstruct the effective idea and re-resolve its contract from a new
                # chain/NBBO/account snapshot. Overrides never fall back to the stale original.
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
                effective_idea = _replace(idea, direction=nd, structure=ns)

                _block_reasons = []
                try:
                    _markers = entry_safety.entry_markers_clear(
                        config_path=args.config,
                        kill_switch_path=(cfg.get("kill_switch") or {}).get("path"))
                    _block_reasons.extend(_markers.reasons)
                    snap = await get_pot_snapshot(ib)
                    _acct = entry_safety.account_snapshot_valid(snap)
                    _block_reasons.extend(_acct.reasons)
                except Exception as _account_error:
                    snap = None
                    _block_reasons.append(f"fresh account/stand-down check failed: {_account_error}")
                if _block_reasons:
                    approval.post_proposal(token, channel,
                        f":no_entry: *{r.underlying}* NOT placed — final safety gate: "
                        + "; ".join(_block_reasons))
                    audit(audit_path, "final_entry_gate_blocked", underlying=r.underlying,
                          decision_id=decision_id, reasons=_block_reasons)
                    done.add(ts)
                    continue

                _dep = deployable_funds(snap)
                _mp = construction.max_premium_budget(snap.net_liq, CONS)
                if _mp > 0:
                    _dep = min(_dep, _mp)
                avail = (_dep if (full_size or over_default)
                         else min(_dep, default_pct * snap.net_liq))
                try:
                    fresh_r, why2 = await _resolve(
                        ib, effective_idea, avail, net_liq=snap.net_liq)
                except Exception as _resolve_error:
                    fresh_r, why2 = None, f"fresh contract/NBBO resolution failed: {_resolve_error}"
                if fresh_r is None:
                    approval.post_proposal(token, channel,
                        f":no_entry: *{r.underlying}* NOT placed — {why2}")
                    audit(audit_path, "fresh_resolve_blocked", underlying=r.underlying,
                          decision_id=decision_id, reason=why2)
                    done.add(ts)
                    continue
                fresh_r.decision_id = decision_id
                # explicit position-size override ("1 contract", "half size") -> set qty on the resolved order
                if qty_ovr:
                    newq = (qty_ovr["contracts"] if "contracts" in qty_ovr
                            else max(1, round(fresh_r.qty * qty_ovr["fraction"])))
                    _unit_cost = entry_safety.executable_price(fresh_r) * 100
                    if _unit_cost * newq > snap.available_funds + 1e-6:
                        # C4 (2026-07-09): floor-divide -- if even ONE contract does not fit, REFUSE
                        # the override rather than silently shipping the old max(1, ...) = 1 contract
                        # that itself blows the budget.
                        affordable = int(snap.available_funds // _unit_cost)
                        if affordable < 1:
                            approval.post_proposal(token, channel,
                                f":no_entry: even 1 contract (~${_unit_cost:,.0f}) exceeds available "
                                f"funds ${snap.available_funds:,.0f} — not placing.")
                            audit(audit_path, "qty_override_refused_funds", underlying=fresh_r.underlying,
                                  order=order_summary(fresh_r), available=snap.available_funds,
                                  decision_id=decision_id)
                            done.add(ts)
                            continue
                        approval.post_proposal(token, channel,
                            f":warning: {newq}x ~${_unit_cost*newq:,.0f} > available ${snap.available_funds:,.0f} — revised to {affordable}x.")
                        newq = affordable
                    _mp2 = construction.max_premium_budget(snap.net_liq, CONS)
                    if _mp2 > 0 and _unit_cost * newq > _mp2 + 1e-6:
                        # C4: same refuse-don't-clamp-to-1 rule against the premium cap.
                        capq = int(_mp2 // _unit_cost)
                        if capq < 1:
                            approval.post_proposal(token, channel,
                                f":no_entry: even 1 contract (~${_unit_cost:,.0f}) exceeds the "
                                f"{CONS.max_premium_pct:.0%}-of-net-liq premium cap (${_mp2:,.0f}) — not placing.")
                            audit(audit_path, "qty_override_refused_premium_cap", underlying=fresh_r.underlying,
                                  order=order_summary(fresh_r), premium_cap=_mp2,
                                  decision_id=decision_id)
                            done.add(ts)
                            continue
                        approval.post_proposal(token, channel,
                            f":warning: {newq}x ~${_unit_cost*newq:,.0f} exceeds the {CONS.max_premium_pct:.0%}-of-net-liq "
                            f"premium cap (${_mp2:,.0f}) — revised to {capq}x.")
                        newq = capq
                    if newq != fresh_r.qty:
                        fresh_r = _replace(fresh_r, qty=newq)
                        fresh_r.decision_id = decision_id
                eff_tp = max(20.0, min(500.0, ov_tp)) if ov_tp else tp_pct
                # M4 (2026-07-09): a manual SL override may only TIGHTEN the stop (smaller max-loss
                # %), never LOOSEN it past the 30% default. Ceiling 30 (was 90); floor 10.
                eff_sl = max(10.0, min(30.0, ov_sl)) if ov_sl else sl_pct
                # Full fail-closed gate on the refreshed account, open book, universe,
                # concentration, daily breaker, construction budget, earnings and two-sided NBBO.
                try:
                    _positions = await CONN.get_positions()
                    _risk_positions = await _open_positions_for_risk(_positions)
                    _baseline_path = Path(args.config).resolve().parent / tr.get("baseline_path", "./day_baseline.json")
                    _baseline = entry_safety.day_start_value(_baseline_path, _trading_day())
                    if isinstance(_baseline, entry_safety.SafetyResult):
                        _block_reasons.extend(_baseline.reasons)
                    _nbbo = entry_safety.nbbo_valid(fresh_r)
                    _block_reasons.extend(_nbbo.reasons)
                    _cost = entry_safety.executable_price(fresh_r) * fresh_r.qty * 100
                    _gate = risk.evaluate_trade(
                        risk.ProposedTrade(
                            underlying=fresh_r.underlying, notional=_cost,
                            is_index=bool(effective_idea.is_index),
                            conviction=int(getattr(effective_idea, "conviction", 1)),
                            is_long=(fresh_r.right == "C"),
                            profit_target_pct=eff_tp, stop_pct=eff_sl),
                        net_liq=snap.net_liq, available_funds=snap.available_funds,
                        open_positions=_risk_positions,
                        pot_day_start=(_baseline if not isinstance(_baseline, entry_safety.SafetyResult)
                                       else 0.0),
                        approved_names={str(n).upper() for n in tr.get("approved_names", [])},
                        limits=_RISK_LIMITS)
                    if not _gate.approved:
                        _block_reasons.extend(_gate.reasons)
                    okf, whyf = construction.check_budget(
                        _cost, fresh_r.dte, snap.net_liq,
                        construction.open_book(_positions, JOURNAL_PATH), CONS)
                    if not okf:
                        _block_reasons.extend(whyf)
                    _edays_final = await asyncio.to_thread(
                        research.days_to_earnings, fresh_r.underlying)
                    if _edays_final is None:
                        _block_reasons.append("earnings date unavailable at approval time")
                    else:
                        _entry_final = datetime.now(timezone.utc).date()
                        _earn_final = _entry_final + timedelta(days=_edays_final)
                        _earn_ok, _earn_why = construction.earnings_ok(
                            _entry_final, fresh_r.expiry, _earn_final, CONS)
                        if not _earn_ok:
                            _block_reasons.append(_earn_why)
                except Exception as _be:
                    _block_reasons.append(f"final risk/NBBO/earnings gate failed: {_be}")
                if _block_reasons:
                    approval.post_proposal(token, channel,
                        f":no_entry: *{fresh_r.underlying}* `{order_summary(fresh_r)}` NOT placed — "
                        "final hard gate: " + "; ".join(_block_reasons))
                    audit(audit_path, "final_entry_gate_blocked", underlying=fresh_r.underlying,
                          order=order_summary(fresh_r), decision_id=decision_id,
                          reasons=_block_reasons)
                    done.add(ts)
                    continue

                # The earnings/account/book checks above may take long enough to stale the first
                # refresh. Request the exact chain/NBBO again as the final network read, then rerun
                # the pure dollar gates against that executable ask.
                try:
                    _latest_r, _latest_why = await _resolve(
                        ib, effective_idea, avail, net_liq=snap.net_liq)
                    if _latest_r is None:
                        raise RuntimeError(_latest_why or "final NBBO refresh returned no order")
                    if qty_ovr:
                        _latest_r = _replace(_latest_r, qty=fresh_r.qty)
                    _latest_r.decision_id = decision_id
                    _latest_nbbo = entry_safety.nbbo_valid(_latest_r)
                    if not _latest_nbbo.allowed:
                        raise RuntimeError("; ".join(_latest_nbbo.reasons))
                    _latest_cost = entry_safety.executable_price(_latest_r) * 100 * _latest_r.qty
                    _latest_gate = risk.evaluate_trade(
                        risk.ProposedTrade(
                            underlying=_latest_r.underlying, notional=_latest_cost,
                            is_index=bool(effective_idea.is_index),
                            conviction=int(getattr(effective_idea, "conviction", 1)),
                            is_long=(_latest_r.right == "C"),
                            profit_target_pct=eff_tp, stop_pct=eff_sl),
                        net_liq=snap.net_liq, available_funds=snap.available_funds,
                        open_positions=_risk_positions,
                        pot_day_start=_baseline,
                        approved_names={str(n).upper() for n in tr.get("approved_names", [])},
                        limits=_RISK_LIMITS)
                    if not _latest_gate.approved:
                        raise RuntimeError("; ".join(_latest_gate.reasons))
                    _latest_budget, _latest_budget_reasons = construction.check_budget(
                        _latest_cost, _latest_r.dte, snap.net_liq,
                        construction.open_book(_positions, JOURNAL_PATH), CONS)
                    if not _latest_budget:
                        raise RuntimeError("; ".join(_latest_budget_reasons))
                    fresh_r = _latest_r
                except Exception as _latest_error:
                    approval.post_proposal(token, channel,
                        f":no_entry: *{fresh_r.underlying}* NOT placed — final NBBO refresh/gate: {_latest_error}")
                    audit(audit_path, "final_nbbo_gate_blocked", decision_id=decision_id,
                          underlying=fresh_r.underlying, error=str(_latest_error))
                    done.add(ts)
                    continue

                _changes = list(entry_safety.material_changes(r, fresh_r))
                if revision == 0 and (ovr or full_size or qty_ovr or ov_tp or ov_sl):
                    _changes.append("human override changed approved terms")
                if _changes:
                    if revision >= 2:
                        approval.post_proposal(token, channel,
                            f":no_entry: *{fresh_r.underlying}* kept moving after two refreshes — "
                            "nothing placed; generate a new slate.")
                        audit(audit_path, "reapproval_churn_blocked", decision_id=decision_id,
                              underlying=fresh_r.underlying, changes=_changes)
                        done.add(ts)
                        continue
                    _remsg = (f":repeat: *Reapproval required — {fresh_r.underlying}*\n"
                              f"Refreshed order: `{order_summary(fresh_r)}`\n"
                              f"Executable BUY limit: *${entry_safety.executable_price(fresh_r):.2f}*\n"
                              f"Changed: {'; '.join(_changes)}\n"
                              f":point_down: Tap :white_check_mark: again within 5 minutes to approve these exact terms.\n"
                              f"_Decision ID: `{decision_id}`, revision {revision + 1}_")
                    _new_ts = approval.post_proposal(token, channel, _remsg)
                    done.add(ts)
                    if _new_ts:
                        pending.append((_new_ts, fresh_r, eff_tp, eff_sl, effective_idea,
                                        False, time.monotonic(), decision_id, revision + 1))
                    audit(audit_path, "reapproval_required", decision_id=decision_id,
                          underlying=fresh_r.underlying, changes=_changes,
                          revision=revision + 1)
                    continue

                r = fresh_r
                # Final marker stat is adjacent to the only BUY placeOrder call: a halt flipped
                # during account/quote/risk I/O cannot slip through.
                _markers_now = entry_safety.entry_markers_clear(
                    config_path=args.config,
                    kill_switch_path=(cfg.get("kill_switch") or {}).get("path"))
                if not _markers_now.allowed:
                    audit(audit_path, "marker_blocked_submit", decision_id=decision_id,
                          reasons=_markers_now.reasons)
                    done.add(ts)
                    continue
                _quote_now = entry_safety.nbbo_valid(r)
                if not _quote_now.allowed:
                    audit(audit_path, "stale_nbbo_blocked_submit", decision_id=decision_id,
                          reasons=_quote_now.reasons)
                    done.add(ts)
                    continue
                _lmt = entry_safety.executable_price(r)
                order = Order(action="BUY", orderType="LMT", lmtPrice=_lmt,
                              totalQuantity=r.qty, tif="DAY")
                order.orderRef = entry_safety.decision_order_ref(decision_id)
                if r.short_contract is not None:
                    combo = conn.create_combo_contract(
                        r.underlying, [(r.contract.conId, "BUY"), (r.short_contract.conId, "SELL")])
                    from exitmgr.order_lock import order_mutation_lock
                    with order_mutation_lock():
                        trade = ib.placeOrder(combo, order)
                else:
                    from exitmgr.order_lock import order_mutation_lock
                    with order_mutation_lock():
                        trade = ib.placeOrder(r.contract, order)
                # Wait for IBKR to ACK (live) or REJECT — never assume it landed (Error 201 etc.).
                _reject_states = {"Cancelled", "ApiCancelled", "Inactive"}
                _live_states = {"PreSubmitted", "Submitted", "Filled"}
                for _ in range(16):  # up to ~8s
                    await asyncio.sleep(0.5)
                    st = trade.orderStatus.status
                    if st in _live_states or st in _reject_states:
                        break
                st = trade.orderStatus.status
                _reasons = [le.message for le in trade.log if getattr(le, "errorCode", 0)]
                if st in _reject_states:
                    reason = _reasons[-1] if _reasons else f"order status {st}"
                    approval.post_proposal(token, channel,
                        f":x: *Order REJECTED by IBKR* — `{order_summary(r)}` was NOT placed.\n{reason}")
                    audit(audit_path, "daily_rec_rejected", underlying=r.underlying,
                          order=order_summary(r), status=st, reason=reason)
                    done.add(ts)
                    continue
                # FILL VERIFICATION (2026-07-01 audit: 5/15 "executed" entries never filled --
                # the journal recorded intent, not fills). After the ACK, wait a bit longer for
                # the actual fill so the entry record carries fill status/price/timestamp; a
                # still-unfilled order stays on placed_watch and alarms after fill_alarm_minutes.
                for _ in range(20):  # up to ~10 more seconds for the fill itself
                    if trade.orderStatus.status == "Filled":
                        break
                    await asyncio.sleep(0.5)
                st = trade.orderStatus.status
                _afp = getattr(trade.orderStatus, "avgFillPrice", None)
                _fill_px = float(_afp) if (st == "Filled" and _afp and _afp == _afp) else None
                # COMMISSIONS + REAL BASIS (2026-07-03): actual entry fee + fill-based cost basis
                # so realized P&L can be reported NET of fees and entry slippage is recorded.
                # ADDITIVE; never raises into the order path; never fabricates a fee/price.
                from exitmgr.order import commission_from_trade as _comm_from_trade, compute_entry_basis as _entry_basis
                _est_debit = round(r.limit * 100 * r.qty, 2)
                _entry_comm = _comm_from_trade(trade) if st == "Filled" else None
                _efd, _eslip, _eslip_pct = _entry_basis(_est_debit, _fill_px, r.qty)
                # Capture the strategist entry thesis for the durable journal record.
                # Non-blocking: thesis capture must never interfere with placing the order.
                try:
                    _thesis_str = str(getattr(idea, "thesis", "") or "")
                except Exception as _te:
                    print(f"[WARN] thesis capture failed (continuing): {_te}")
                    _thesis_str = ""
                with open(journal_path, "a") as f:
                    import json as _j
                    spread_j = ({"spread": {"short_con_id": r.short_contract.conId,
                                            "short_strike": r.short_strike,
                                            "width": abs(r.short_strike - r.strike)}}
                                if r.short_contract is not None else {})
                    f.write(_j.dumps({"ts": datetime.utcnow().isoformat(),
                                      "decision_id": decision_id,
                                      "contract_id": r.contract.conId,
                                      "symbol": r.underlying, "right": r.right, "expiry": r.expiry,
                                      "strike": r.strike, "quantity": r.qty,
                                      "debit": round(r.limit * 100 * r.qty, 2),
                                      "profit_target_pct": eff_tp, "stop_pct": eff_sl,
                                      "conviction": getattr(idea, "conviction", -1),
                                      "thesis": _thesis_str,
                                      # 2026-07-01 ADDITIVE fields: fill verification + construction annotations
                                      "order_id": getattr(trade.order, "orderId", None),
                                      "order_ref": getattr(trade.order, "orderRef", None),
                                      "order_status": st,
                                      "avg_fill_price": _fill_px,
                                      "fill_ts": (datetime.utcnow().isoformat() if st == "Filled" else None),
                                      "entry_commission": _entry_comm,
                                      "entry_fill_debit": _efd,
                                      "entry_slippage": _eslip,
                                      "entry_slippage_pct": _eslip_pct,
                                      "basis_source": ("fill" if _efd is not None else "estimate"),
                                      "underlying_price_at_entry": (r.spot or None),
                                      "entry_delta": (r.entry_delta or None),
                                      "entry_iv": (r.entry_iv or None),
                                      "dte_at_entry": (r.dte or None),
                                      "dte_adjusted": bool(r.dte_adjusted),
                                      **spread_j}, default=str) + "\n")
                placed_watch.append({"trade": trade, "r": r, "t0": time.monotonic(),
                                     "decision_id": decision_id,
                                     "alerted": False, "filled_logged": st == "Filled"})
                tag = " _(your levels)_" if (ov_tp or ov_sl) else ""
                approval.post_proposal(token, channel,
                    f":white_check_mark: *Placed* `{order_summary(r)}` — exits +{eff_tp:.0f}% / -{eff_sl:.0f}%{tag}")
                audit(audit_path, "daily_rec_executed", underlying=r.underlying, order=order_summary(r),
                      decision_id=decision_id,
                      profit_target_pct=eff_tp, stop_pct=eff_sl)
                done.add(ts)
            # FILL VERIFICATION sweep (2026-07-01): confirm fills of placed orders into
            # fills.log; alarm #error-logs on anything unfilled past fill_alarm_minutes.
            _watch_entry_fills(placed_watch, token, audit_path)
            await asyncio.sleep(15)
        # final fill sweep + a durable last-known status for anything still unfilled
        _watch_entry_fills(placed_watch, token, audit_path)
        for w in placed_watch:
            if not w["filled_logged"]:
                try:
                    import json as _j
                    with open(FILLS_PATH, "a") as f:
                        f.write(_j.dumps({"ts": datetime.utcnow().isoformat(),
                                          "event": "entry_fill_final",
                                          "decision_id": w.get("decision_id"),
                                          "contract_id": getattr(w["r"].contract, "conId", None),
                                          "symbol": w["r"].underlying,
                                          "order_id": getattr(getattr(w["trade"], "order", None), "orderId", None),
                                          "order_ref": getattr(getattr(w["trade"], "order", None), "orderRef", None),
                                          "status": w["trade"].orderStatus.status,
                                          "note": "still unfilled when the slate watcher exited"}) + "\n")
                except Exception as e:
                    print(f"[WARN] final fill-status write failed: {e}")
        print(f"[INFO] daily slate done — {len(done)}/{len(pending)} decided")
        return 0
    finally:
        from exitmgr.slate_lock import clear_slate_active
        clear_slate_active()  # never leave the soft-mutex flag set after the slate exits
        await conn.disconnect()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch-mins", type=int, default=360, help="how long to watch for your taps")
    ap.add_argument("--config", default="config.yaml")
    # USER-DIRECTED proposal: when --ticker is set, skip the model slate and propose THIS trade
    # through the same one-tap approve -> execute -> journal -> exit-manage pipeline.
    ap.add_argument("--ticker", default=None, help="user-directed: underlying symbol (e.g. QQQ). Skips the model slate.")
    ap.add_argument("--right", default="C", choices=["C", "P", "c", "p"], help="C=call (bullish), P=put (bearish)")
    ap.add_argument("--dte", type=int, default=30, help="target days-to-expiry (min-DTE floor 25 applies; prefer 25-45)")
    ap.add_argument("--delta", type=float, default=0.60, help="target option delta (clamped into the 0.55-0.65 band)")
    ap.add_argument("--structure", default="", help="override structure, e.g. 'call debit spread' (default: long call/put)")
    ap.add_argument("--tp", type=float, default=0.0, help="take-profit %% (0 = global default +100%%)")
    ap.add_argument("--stop", type=float, default=0.0, help="stop %% (0 = global default -50%%)")
    ap.add_argument("--conviction", type=int, default=6, help="conviction 1-10 (display only)")
    ap.add_argument("--thesis", default="User-directed proposal.", help="thesis line shown in the proposal")
    ap.add_argument("--client-id", type=int, default=None, dest="client_id", help="override IBKR clientId (avoid clash with the cron's 93)")
    raise SystemExit(asyncio.run(run(ap.parse_args())))
