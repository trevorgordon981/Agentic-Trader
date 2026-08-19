#!/usr/bin/env python
"""Daily trade-recommendation slate from the configured strategist model.

The backend is CONFIGURATION, not identity: read `trading.llm_endpoint` / `trading.llm_model`
from config.yaml. Never name a specific model in code or in user-facing output -- a hardcoded
"MiniMax" outlived the model by a day and taught the assistant a false fact about its own stack.

Runs once a day (cron at market open). Asks the strategist in RECOMMEND mode for its best 1-3
option ideas scored 1-10, prices each concrete contract via IBKR (needs the OPRA subscription),
and posts each affordable one to #trading-approvals with its score and a one-tap approve/deny.
Then it watches those messages until a deadline and places the ones you approve. The MODEL picks
and scores; YOU approve; this just carries it. clientId 93 (no clash with trader 88 / status 87
/ quote 86 / manual 90).

Usage: ~/ib-grader-venv/bin/python daily_recommend.py [--watch-mins 360]
"""
import argparse
import math
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
from exitmgr.strategist import (
    propose, discover_names, propose_one, propose_intents, select_candidate, TradeIdea,
)
from exitmgr.trader import (
    Trader, ResolvedOrder, order_summary, contract_snapshot, audit, _trading_day,
    # The structure gate is IMPORTED from trader.py, which itself imports the allow-list from
    # strategist.py. One list, one gate, one place to change it -- this file declares neither.
    debit_structure_ok, _structure_implied_right, _require_allowed_structure,
    is_credit, capital_at_risk, capital_committed, credit_executable_price,
    credit_material_changes, credit_structure_ok, collateral_capacity,
    required_collateral, broker_deployed_csp_collateral, _credit_limits,
    reserve_credit_entry,
)
from exitmgr.entry_reservation import EntryReservationLedger
from exitmgr.entry_contract import StageAIntent
from exitmgr.entry_builder import (
    CandidateBinding, build_entry_candidates, bindings_for_stage_b,
    reprice_binding, select_binding,
)
from exitmgr import apewisdom, approval, construction, entry_safety, research, trade_capture, risk
from exitmgr.config import ConstructionConfig, construction_from_dict
from exitmgr.slate_lock import slate_active_guard
from exitmgr.market import fetch_universe_quotes, usable_price

CLIENT_ID = 93

# 5% cash-buffer: keep this fraction of NetLiq (account VALUE) liquid on every sizing path. Set from
# config (trading.cash_buffer_pct) in main(); the risk gate (exitmgr/risk.py) enforces the same on
# the trader loop. 2026-06-22.
CASH_BUFFER_PCT = 0.05
DAILY_ENTRY_RESERVATIONS = EntryReservationLedger()

# 2026-07-01 constructor rework: construction gates (min-DTE floor, TP/SL clamp, structure
# sanity, premium/deployed/decay budgets). Overwritten from config.yaml `construction:` in
# run(); the module-global style matches CASH_BUFFER_PCT above. CONN/JOURNAL_PATH let the
# budget gates value the OPEN book (deployed premium + portfolio decay) from live positions.
CONS = ConstructionConfig()
# (The pot-tiered TP runner ceiling that used to be read from caps.tp_tiers into a module global
# here was REMOVED 2026-07-26 -- Sol audit R5 R2 / RULING_TAKE_PROFIT.md. Account size no longer
# takes part in the exit decision at all, so there is nothing left to cache.)


# Floor-cost proxy for the cheapest realistic 0.60-delta debit spread, as a fraction
# of notional. Tunable via AFFORD_NOTIONAL_FRAC env var; see the screen below for why
# 0.02 was too aggressive at a ~$4.6k account size.
AFFORD_NOTIONAL_FRAC = float(os.environ.get("AFFORD_NOTIONAL_FRAC", "0.01"))

def _tp_level_text(tp_pct, tp_price):
    """Render the take-profit half of a Slack sell-levels line. `tp_pct` is OPTIONAL and is None
    on essentially every trade now (Sol audit R5 R2): there is no mechanical profit target, so say
    so plainly rather than printing a number the code will not act on."""
    if tp_pct is None:
        return "take profit *none* (doctrine, not a clamp — the model decides on thesis + giveback)"
    return f"take profit ~${tp_price:.2f} (+{tp_pct:.0f}%, explicit catastrophe backstop)"


def _pct_text(v):
    """`+30%`-style text for an OPTIONAL percent level; 'none' when absent."""
    return "none" if v is None else f"{v:.0f}%"
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


async def _probe_apewisdom_row(ib, row, blocked_sector_keywords, semaphore):
    """Fail-closed eligibility probe for an external social-attention candidate.

    It must be a liquid USD equity/ETF, a qualified SMART stock, and have a
    usable SMART options chain before it can enter either discovery consumer.
    """
    ticker = row["ticker"]
    try:
        async with semaphore:
            profile = await asyncio.wait_for(
                asyncio.to_thread(apewisdom.security_profile, ticker), timeout=20)
            allowed, reason = apewisdom.profile_eligible(profile, blocked_sector_keywords)
            if not allowed:
                return None, reason
            qualified = await asyncio.wait_for(
                ib.qualifyContractsAsync(Stock(ticker, "SMART", "USD")), timeout=20)
            stock = next((c for c in qualified
                          if getattr(c, "conId", None)
                          and getattr(c, "secType", None) == "STK"
                          and getattr(c, "currency", None) == "USD"), None)
            if stock is None:
                return None, "smart_usd_stock_unqualified"
            params = await asyncio.wait_for(
                ib.reqSecDefOptParamsAsync(ticker, "", "STK", stock.conId), timeout=20)
            if pick_chain(params, ticker) is None:
                return None, "no_smart_option_chain"
            return row, "eligible"
    except Exception as exc:
        return None, f"probe_error:{type(exc).__name__}"


async def _load_apewisdom_pool(ib, tr, audit_path):
    """Fetch and screen ApeWisdom. Any failure returns an empty pool; the normal slate continues."""
    cfg = tr.get("apewisdom_discovery") or {}
    if not cfg.get("enabled", False):
        return None, []
    try:
        source_limit = max(1, min(100, int(cfg.get("source_limit", 20))))
        probe_limit = max(1, min(source_limit, int(cfg.get("probe_limit", 12))))
        feed = await asyncio.to_thread(
            apewisdom.load_trends, cfg.get("filter", "all-stocks"), source_limit)
        initial = apewisdom.initial_rows(feed, blocked=tr.get("blocked_names", []))[:probe_limit]
        semaphore = asyncio.Semaphore(4)
        probed = await asyncio.gather(*[
            _probe_apewisdom_row(ib, row, tr.get("blocked_sector_keywords", []), semaphore)
            for row in initial
        ])
        eligible = [row for row, _reason in probed if row is not None]
        dropped = [{"ticker": initial[i]["ticker"], "reason": reason}
                   for i, (row, reason) in enumerate(probed) if row is None]
        audit(audit_path, "apewisdom_source_screen",
              source_url=feed.get("source_url"), stale=bool(feed.get("stale")),
              age_seconds=feed.get("age_seconds"), fetched=len(feed.get("results", [])),
              probed=len(initial), eligible=[r["ticker"] for r in eligible], dropped=dropped,
              signal_type="attention_only", trade_authority=False, training_eligible=False)
        return feed, eligible
    except Exception as exc:
        audit(audit_path, "apewisdom_source_error", error=f"{type(exc).__name__}: {exc}")
        return None, []


def _merge_discovery_candidates(*groups):
    """Stable dedupe for the ordinary and ApeWisdom new-name rounds."""
    out, seen = [], set()
    for group in groups:
        for ticker, reason in group or []:
            ticker = str(ticker).upper()
            if ticker in seen:
                continue
            seen.add(ticker)
            out.append((ticker, reason))
    return out


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
        if (w.get("credit_reservation")
                and status in {"Cancelled", "ApiCancelled", "Inactive", "Rejected"}):
            try:
                if DAILY_ENTRY_RESERVATIONS.clear_for_status(w.get("order_ref"), status):
                    audit(audit_path, "credit_entry_reservation_cleared",
                          order_ref=w.get("order_ref"), status=status)
            except Exception as exc:
                audit(audit_path, "credit_entry_reservation_clear_error",
                      order_ref=w.get("order_ref"), status=status, error=str(exc))
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
                                      "model_identity": w.get("model_identity"),
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


def user_directed_idea(args) -> TradeIdea:
    """BYPASS 1 CLOSED (2026-07-26).

    `--structure` was an ARBITRARY string handed straight to TradeIdea(): this idea is never
    parsed by the strategist, so strategist._require_allowed_structure() never ran on it.
        daily_recommend.py --ticker AAPL --structure "naked call"
    therefore built and proposed an idea LABELLED "naked call". What it actually constructed was
    a long call -- _resolve() branches on the substring "spread" and takes the option right from
    `direction`, never from the structure string -- so nothing was ever sold short on this path.
    The damage was to the RECORD: a long call filed in the journal, and thence in the training
    corpus, under the name of a short structure the account never held.

    Raises ValueError for a structure that is not on the allow-list, or one that contradicts
    --right. It is NEVER coerced back to the "long call"/"long put" default: silently swapping in
    a different structure is the same mislabelling defect wearing a different hat."""
    direction = "bullish" if args.right.upper() == "C" else "bearish"
    structure = args.structure or ("long call" if direction == "bullish" else "long put")
    # intended_hold_days was NEVER set on this route, so every user-directed fill journalled it
    # as null (observed on the live SMCI entry 2026-08-12). That field is the DENOMINATOR of the
    # 5-8x DTE doctrine check, so a null makes the position un-auditable after the fact -- and
    # post-hoc DTE compliance is exactly how the previous book's losses were diagnosed
    # (median dte_at_close was 0).
    #
    # --hold-days states it explicitly. Left unset, it is DERIVED as ceil(dte/8): the shortest
    # hold for which the chosen DTE still satisfies the 8x ceiling, which also guarantees the
    # recorded pair lands inside the 5-8x window. Derived, not invented -- it records the hold
    # the doctrine implies for the DTE the user asked for, rather than fabricating an intent.
    _hold = int(getattr(args, "hold_days", 0) or 0)
    if _hold <= 0:
        _hold = max(1, math.ceil(int(args.dte) / 8.0))
    idea = TradeIdea(underlying=args.ticker.upper(),
                     is_index=args.ticker.upper() in ("SPY", "QQQ", "IWM"),
                     direction=direction, structure=structure,
                     target_dte=args.dte, target_delta=args.delta,
                     est_debit_usd=0.0, conviction=int(args.conviction),
                     thesis=args.thesis, profit_target_pct=args.tp, stop_pct=args.stop,
                     intended_hold_days=_hold)
    ok, why = debit_structure_ok(idea)
    if not ok:
        raise ValueError("--structure %r refused. %s" % (structure, why))
    return idea


def apply_structure_override(idea, ovr):
    """BYPASS 3 CLOSED (2026-07-26). Returns (effective_idea, error, note).

    A Slack approval reply may flip/set the direction and may switch single<->spread. The result
    was written onto the idea with dataclasses.replace() and used directly, so the allow-list
    never saw it.

    The override VOCABULARY was never the hole: approval.parse_structure_override() only ever
    emits 'single' or 'spread', and both map to structures already on the allow-list. The hole was
    the DIRECTION half, which rewrites the direction and LEAVES THE STRUCTURE ALONE. An idea
    approved as a "bull call spread" came out of here as ("bull call spread", bearish); _resolve()
    then read the right off `direction` and built a PUT vertical, which the journal recorded as a
    bull call spread. Same silent mislabelling, arriving by a different door.

    RESOLUTION: when a direction override makes the structure's own noun stale, the structure is
    RELABELLED to match -- keeping its single-vs-spread shape, using the same canonical strings the
    'single'/'spread' override already uses -- and the relabel is returned as `note` so it is
    audited and shown in Slack. This is not a silent coercion and not a guess: the human has just
    said, explicitly, which direction they want, so the direction is authoritative and the stale
    noun is the error. THE ORDER IS UNCHANGED BY THIS -- the same contract was already being
    built; only the name attached to it is corrected.

    Anything still contradictory or off-list after that (a contradiction the idea arrived with,
    with no direction override to resolve it) is REFUSED via `error`; the caller must not place."""
    from dataclasses import replace as _replace
    # FAIL CLOSED ON THE WAY IN. An idea whose own structure is off the allow-list is refused
    # before ANY override is applied, so no reply can launder it: without this, ("naked call",
    # bullish) + a bare "flip" relabelled to ("long put", bearish) and passed the gate, and
    # {"structure": "single"} replaced it outright with "long call". A banned structure must not
    # be rescuable by a Slack reply -- it should never have reached approval at all.
    try:
        _require_allowed_structure("debit", idea.structure)
    except ValueError as _incoming_exc:
        return idea, ("structure override refused. STRUCTURE REFUSED: %s" % (_incoming_exc,)), ""
    nd = idea.direction
    _dir_overridden = False
    if ovr.get("direction") == "flip":
        nd = "bearish" if idea.direction == "bullish" else "bullish"
        _dir_overridden = True
    elif ovr.get("direction") in ("bullish", "bearish"):
        nd = ovr["direction"]
        _dir_overridden = True
    ns = idea.structure
    if ovr.get("structure") == "single":
        ns = "long put" if nd == "bearish" else "long call"
    elif ovr.get("structure") == "spread":
        ns = "put debit spread" if nd == "bearish" else "call debit spread"
    note = ""
    # ns is guaranteed to be on the allow-list here: it is either the incoming structure (checked
    # above) or one of the four canonical strings the 'single'/'spread' override maps to.
    if _dir_overridden and not ovr.get("structure"):
        _implied = _structure_implied_right(ns)
        _want = "C" if nd == "bullish" else "P"
        if _implied and _implied != _want:
            _relabelled = (("put debit spread" if nd == "bearish" else "call debit spread")
                           if "spread" in str(ns).lower()
                           else ("long put" if nd == "bearish" else "long call"))
            note = ("structure relabelled %r -> %r to match the direction you set (%s); the "
                    "single-vs-spread shape and the contract are unchanged" % (ns, _relabelled, nd))
            ns = _relabelled
    effective = _replace(idea, direction=nd, structure=ns)
    ok, why = debit_structure_ok(effective)
    if not ok:
        return effective, ("structure override refused. %s" % why), note
    return effective, "", note


def submit_structure_ok(r, idea) -> tuple:
    """STRUCTURE GATE AT THE MONEY BOUNDARY for this file's OWN placeOrder call -- trader.py's
    _submit_order_unlocked does not cover the slate path. Returns (ok, reason).

    Checks the same two things the trader's submit-time gate does: the idea's structure is on the
    allow-list and agrees with its direction, AND the concrete order that was resolved buys the
    right the structure names. An order carrying NO structure ("" -- nothing was ever declared) is
    not an allow-list failure; every route that HAS one is gated upstream."""
    if not getattr(r, "structure", ""):
        return True, ""
    ok, why = debit_structure_ok(idea)
    if not ok:
        return False, why
    implied = _structure_implied_right(r.structure)
    if implied and implied != str(r.right).upper()[:1]:
        return False, ("resolved order buys a %r but its structure %r names a %s -- filling it "
                       "would journal the position under a name describing a different trade"
                       % (r.right, r.structure, "call" if implied == "C" else "put"))
    return True, ""


async def _resolve(ib, idea, available, net_liq=None):
    """Pick the concrete option (nearest expiry to target DTE, strike by delta) and price it via
    OPRA. Single-leg long call/put; sizes to >=1 contract within available funds. None if it
    can't price or even one contract is unaffordable."""
    # Frozen Stage A/B route. Requote only the selected conIds; never fall through to the legacy
    # selector, which could silently substitute another strike or structure after Stage B.
    binding = getattr(idea, "_stage_b_binding", None)
    intent = getattr(idea, "_stage_a_intent", None)
    if binding is not None or intent is not None:
        if not isinstance(binding, CandidateBinding) or not isinstance(intent, StageAIntent):
            return None, "invalid Stage-B binding"
        try:
            deployed_credit = (await broker_deployed_csp_collateral(ib)
                               if intent.side == "credit" else 0.0)
            _rs = []
            refreshed = await reprice_binding(
                ib, binding, intent, net_liq=net_liq,
                available_funds=available, cons=CONS,
                deployed_credit_usd=deployed_credit,
                # All callers pass deployable_funds(), which already reserves the cash buffer.
                cash_buffer_pct=0.0, reasons=_rs)
        except Exception as exc:
            return None, f"selected-candidate requote failed: {exc}"
        if refreshed is None or refreshed.candidate.candidate_id != binding.candidate.candidate_id:
            # Say WHICH of the nine guards refused. This used to be one opaque string, so a
            # declined approval was indistinguishable from a protective block (2026-08-12).
            _why = "; ".join(_rs) if _rs else "candidate identity changed on requote"
            return None, "cannot place: %s" % _why
        idea._stage_b_binding = refreshed
        resolved = refreshed.to_resolved_order(intent)
        if resolved.side == "credit":
            idea.strike = resolved.strike
            idea.collateral_usd = resolved.collateral_usd
            idea.net_credit_usd = resolved.net_credit_usd
            idea.max_loss_usd = resolved.credit_max_loss_usd
        else:
            idea.est_debit_usd = round(
                refreshed.candidate.one_contract_cost_usd * resolved.qty, 2)
        return resolved, None
    # STRUCTURE GATE (2026-07-26): the constructor's own check, the mirror of the one at the top
    # of trader._resolve_order(). Every debit order this process builds is built here, so an idea
    # that reached construction by any route -- including one added later -- is refused before a
    # contract is qualified. A refusal is returned as an ordinary (None, reason), the same shape
    # every other constructor rejection uses.
    _ok_struct, _why_struct = debit_structure_ok(idea)
    if not _ok_struct:
        return None, _why_struct
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
    # Defined-risk long options/debit spreads are gated on executable NET DEBIT (max loss), exactly
    # like the continuous trader. Underlying share notional is not capital at risk and must not
    # pre-reject an otherwise affordable spread.
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
                                 structure=str(getattr(idea, "structure", "") or ""),
                                 **enrich), None
        # no affordable short leg -> fall through to the single long leg

    if mid * 100 > available + 1e-6:
        return None, f"one contract ${mid*100:,.0f} > available ${available:,.0f}"
    qty = max(1, int(available // (mid * 100)))
    return ResolvedOrder(idea.underlying, right, expiry, float(contract.strike), qty, round(mid, 2),
                         contract, structure=str(getattr(idea, "structure", "") or ""),
                         **enrich), None


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


async def _materialize_stage_b(ib, intents, pot, tr, audit_path):
    """Shared daily/add-name Stage B: same builder, fields, units, and decline as Trader."""
    ideas = []
    for index, intent in enumerate(intents or [], start=1):
        intent_id = f"intent_{index}"
        try:
            deployed_credit = (await broker_deployed_csp_collateral(ib, audit_path)
                               if intent.side == "credit" else 0.0)
            bindings = await build_entry_candidates(
                ib, intent, intent_id, net_liq=pot.net_liq,
                available_funds=pot.available_funds, cons=CONS,
                deployed_credit_usd=deployed_credit,
                cash_buffer_pct=CASH_BUFFER_PCT)
        except Exception as exc:
            audit(audit_path, "stage_b_candidate_error", intent_id=intent_id,
                  underlying=getattr(intent, "underlying", None), error=str(exc),
                  route="daily_or_add_name")
            continue
        bindings = bindings_for_stage_b(
            bindings, max_age_seconds=entry_safety.DEFAULT_NBBO_MAX_AGE_SECONDS)
        if len(bindings) < 3:
            audit(audit_path, "stage_b_skipped", intent_id=intent_id,
                  underlying=intent.underlying,
                  reason=f"only {len(bindings)} prefiltered candidates; requires 3",
                  route="daily_or_add_name")
            continue
        candidates = [binding.candidate for binding in bindings]
        try:
            result = await asyncio.to_thread(
                select_candidate, tr.get("llm_endpoint"), tr.get("llm_model"),
                intent, candidates, intent_id=intent_id, return_raw=True,
                return_cot=True, return_identity=True)
            selected = result
            raw_b = cot_b = identity_b = None
            if isinstance(result, tuple):
                selected = result[0] if result else None
                raw_b = result[1] if len(result) > 1 else None
                cot_b = result[2] if len(result) > 2 else None
                identity_b = result[3] if len(result) > 3 else None
        except Exception as exc:
            audit(audit_path, "stage_b_error", intent_id=intent_id,
                  underlying=intent.underlying, error=str(exc),
                  route="daily_or_add_name")
            continue
        if selected is None:
            audit(audit_path, "stage_b_declined", intent_id=intent_id,
                  underlying=intent.underlying, route="daily_or_add_name")
            continue
        selected_id = getattr(selected, "candidate_id", None)
        binding = select_binding(bindings, selected_id)
        if binding is None:
            audit(audit_path, "stage_b_invalid_selection", intent_id=intent_id,
                  underlying=intent.underlying, candidate_id=selected_id,
                  route="daily_or_add_name")
            continue
        idea = Trader._idea_from_stage_b(intent, binding)
        idea._stage_b_candidates = tuple(candidates)
        idea._stage_b_raw = raw_b
        idea._stage_b_cot = cot_b
        idea._stage_b_identity = identity_b
        ideas.append(idea)
        audit(audit_path, "stage_b_selected", intent_id=intent_id,
              underlying=intent.underlying, candidate_id=selected_id,
              candidates=len(candidates), route="daily_or_add_name")
    return ideas


async def _post_idea(ib, idea, pot, default_pct, token, channel, audit_path, pending,
                     label="Daily rec", audit_event="daily_rec_posted",
                     candidates=None, raw_strategist=None, market_context=None, regime=None,
                     technical_card=None, cot=None, model_identity=None):
    """Resolve an idea to a concrete priced order, post it to #approvals with one-tap, and append it
    to `pending` so the watch loop manages approval/execution. Returns the Slack ts (or None).
    Shared by the daily slate and the same-day 'add a name -> suggest it now' path."""
    _credit = is_credit(idea)
    deployable = deployable_funds(pot)  # available_funds minus the 5% cash reserve
    # PREMIUM CAP (2026-07-01 gate A4): no trade's premium may exceed max_premium_pct (15%)
    # of net-liq -- clamps BOTH the default slice and the full-size opt-in path (82-103% of
    # the pot was deployed at times; one gap-down = -27% account day).
    if _credit:
        _deployed_credit = await broker_deployed_csp_collateral(ib, audit_path)
        if _deployed_credit is None:
            _daily_cap_rejected("collateral", "deployed CSP collateral unverifiable", idea, None)
            return None
        deployable = min(
            deployable,
            max(0.0, 0.80 * pot.net_liq - _deployed_credit),
        )
    else:
        _max_prem = construction.max_premium_budget(pot.net_liq, CONS)
        if _max_prem > 0:
            deployable = min(deployable, _max_prem)
    _stage_b_bound = getattr(idea, "_stage_b_binding", None) is not None
    cons_budget = deployable if _stage_b_bound else min(deployable, default_pct * pot.net_liq)
    resolved, why = await _resolve(ib, idea, cons_budget, net_liq=pot.net_liq)
    over_default = False
    if not resolved and not _stage_b_bound:
        # too pricey for the default slice -> offer it at full (buffer+premium-capped) size so you can opt in
        resolved, why = await _resolve(ib, idea, deployable, net_liq=pot.net_liq)
        over_default = resolved is not None
    if resolved is not None and _credit:
        _ok_credit, _why_credit = credit_structure_ok(idea)
        if not _ok_credit:
            _daily_cap_rejected("credit_structure", _why_credit, idea, resolved)
            return None
        _capacity = collateral_capacity(
            required=required_collateral(resolved.strike, resolved.qty),
            deployed=_deployed_credit, net_liq=pot.net_liq,
            available_funds=pot.available_funds)
        if not _capacity.allowed:
            _daily_cap_rejected("collateral", _capacity.reasons, idea, resolved)
            return None
    # RISK SCREEN (mirrors the trader loop): block genuinely oversized orders. This account is
    # long-debit-only (long calls/puts + debit spreads), so the real capital at risk is the NET
    # DEBIT (max loss = limit*100*qty), NOT the strike notional. The old screen measured strike
    # notional (~1.1x/2.4x sum-of-strikes*100) vs 30x NetLiq, which structurally rejected every
    # cheap defined-risk debit spread (e.g. ORCL 162.5/160P @ $1.25 = $125 risk read as ~$32k).
    # Cap the actual max loss at 30x NetLiq; available-funds already bounds it to the pot.
    if resolved is not None and pot.net_liq:
        _eg = capital_at_risk(resolved)
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
    if resolved is not None and pot.net_liq and not _credit:
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
    if resolved is not None and not _credit:
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
            capital_committed(resolved),
            idea.is_index, risk.effective_pot(pot.net_liq, _RISK_LIMITS.pot_cap_usd), _RISK_LIMITS)
        for _txt, _akw in _cnotes:
            head += f"_{_txt}_\n"
            audit(audit_path, "concentration_warning", order=order_summary(resolved), **_akw)
    except Exception as _ce:
        print(f"[WARN] concentration check failed for {getattr(idea, 'underlying', '?')} (continuing): {_ce}")
    cost = capital_committed(resolved)
    # SELL LEVELS (2026-07-26, Sol audit R5 R2 / RULING_TAKE_PROFIT.md).
    # The stop is clamped exactly as before: default -30%, a model stop may only be TIGHTER.
    # The take-profit is now OPTIONAL and is normally None. The pot-tier ceiling that used to be
    # applied here (construction.tp_tier_for_pot on the LIVE pot, then a per-call cons copy) is
    # GONE -- at ~$1,893 it stamped +20% on every order. `tp_pct` may be None from here on; every
    # consumer below must handle that. A refused take-profit is surfaced, never silently rewritten.
    if _credit:
        tp_pct, sl_pct, tp_price, sl_price = None, 0.0, None, None
    else:
        tp_pct, sl_pct = construction.clamp_tp_sl(
            idea.profit_target_pct, idea.stop_pct, CONS)
        _tp_note = construction.optional_take_profit_pct(idea.profit_target_pct)[1]
        if _tp_note:
            print(f"[TP] {idea.underlying}: {_tp_note}")
            audit(audit_path, "take_profit_refused", underlying=idea.underlying,
                  requested_profit_target_pct=idea.profit_target_pct, reason=_tp_note)
        tp_price = (resolved.limit * (1 + tp_pct / 100.0)) if tp_pct is not None else None
        sl_price = resolved.limit * (1 - sl_pct / 100.0)
    pct_pot = (cost / pot.net_liq * 100) if pot.net_liq else 0.0
    if _credit:
        size_line = (f"Collateral ~${cost:,.0f} (*{pct_pot:.0f}% of pot*); executable credit "
                     f"~${resolved.net_credit_usd:,.0f}; max loss if assigned stock goes to zero "
                     f"~${resolved.credit_max_loss_usd:,.0f}.")
    elif over_default:
        size_line = (f":warning: ~${cost:,.0f} = *{pct_pot:.0f}% of pot* — ABOVE your {default_pct:.0%} "
                     f"default (1 contract is the smallest size). Tap :white_check_mark: only if you want this size.")
    else:
        size_line = (f"~${cost:,.0f} (*{pct_pot:.0f}% of pot*, your {default_pct:.0%} default). "
                     f"Reply `full size` to use ~${deployable:,.0f} (keeps a 5% cash buffer).")
    decision_id = entry_safety.new_decision_id()
    resolved.decision_id = decision_id
    resolved.decision_revision = 0
    resolved.model_identity = model_identity
    resolved.intended_hold_days = getattr(idea, "intended_hold_days", None)
    resolved.thesis = str(getattr(idea, "thesis", "") or "")
    if _stage_b_bound:
        action_word = "SELL the cash-secured put" if _credit else "BUY"
        msg = (head + f"*Order:* `{order_summary(resolved)}`\n"
               f"{size_line}\n"
               f":point_down: *Tap :white_check_mark: to {action_word}* or :x: to skip. "
               f"Stage-B contract/structure/size edits are not accepted; a changed quote is "
               f"shown for reapproval.\n"
               f"_Decision ID: `{decision_id}` — approval expires in 5 minutes._")
    else:
        msg = (head + f"*Order:* `{order_summary(resolved)}`\n"
               f"{size_line} Max loss = the debit.\n"
               f"*Sell levels (auto):* {_tp_level_text(tp_pct, tp_price)} | "
               f"stop ~${sl_price:.2f} (-{sl_pct:.0f}%)\n"
               f":point_down: *Tap :white_check_mark: to BUY*, or REPLY to tweak: `full size`, levels (`tp 60 stop 30`), "
               f"direction (`flip` / `make it bearish`), or `just the call` / `make it a spread`. :x: to skip.\n"
               f"_Decision ID: `{decision_id}` — approval expires in 5 minutes._")
    ts = approval.post_proposal(token, channel, msg)
    if ts:
        pending.append((ts, resolved, tp_pct, sl_pct, idea, over_default,
                        time.monotonic(), decision_id, 0, candidates, raw_strategist,
                        cot, market_context, technical_card))
        audit(audit_path, audit_event, underlying=idea.underlying,
              conviction=idea.conviction, order=order_summary(resolved),
              profit_target_pct=tp_pct, stop_pct=sl_pct, over_default=over_default,
              decision_id=decision_id)
        # DECISION CAPTURE (v2, record-only): the full DECISION -> ENTRY context for a posted
        # slate idea -- raw strategist reasoning, EVERY candidate + conviction, the chosen idea,
        # construction (clamped tp/sl, dte adjust), regime, and the RAG/news/journal brief.
        # The immutable decision_id is the only later join key; contract/symbol fallback is banned.
        try:
            trade_capture.capture_decision(
                trade_capture.dataset_dir(JOURNAL_PATH), source="daily_slate",
                symbol=idea.underlying, right=resolved.right, strike=resolved.strike,
                expiry=resolved.expiry,
                structure=("cash secured put" if is_credit(resolved) else
                           ("spread" if resolved.short_contract is not None else "single")),
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
                       "decision_id": decision_id}, decision_id=decision_id,
                revision=0, event="proposal", model_identity=model_identity,
                final_contract=contract_snapshot(resolved))
        except Exception as _dce:
            print(f"[WARN] daily-slate capture_decision failed (continuing): {_dce}")
    return ts


async def run(args):
    global CASH_BUFFER_PCT, CONS, CONN, JOURNAL_PATH, ERROR_CHANNEL, _RISK_LIMITS
    cfg = yaml.safe_load(open(args.config))
    # caps.tp_tiers is NOT read any more (2026-07-26, Sol audit R5 R2). If a tier table is ever
    # re-added to config.yaml it is inert on this path -- refuse it loudly rather than let it look
    # like it is doing something.
    if (cfg.get("caps") or {}).get("tp_tiers"):
        print("[WARN] config.yaml caps.tp_tiers is present but IGNORED — mechanical pot-tiered "
              "take-profit was removed by Sol audit R5 R2 / RULING_TAKE_PROFIT.md. Delete it.")
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
        _ape_rows, _ape_addable = [], set()
        _raw_slate = None    # verbatim strategist output (model path), for v2 decision capture
        _slate_cot = None    # [m3cot] chain-of-thought (reasoning_content), separate from the answer
        _slate_identity = None
        brief = None         # the RAG/news/journal/quote brief fed to the model (model path)
        _slate_price_stats = None  # per-name technical indicators (momentum/vol/IVR) fed to the model
        if args.ticker:
            # USER-DIRECTED: you name the trade; it runs the SAME price -> one-tap -> execute ->
            # journal -> exit-manage pipeline as the model slate (no bypass — you still tap to fire it).
            # BYPASS 1: construction moved into user_directed_idea(), which gates --structure
            # against the allow-list. Identical output for every permitted structure; a refused
            # one raises ValueError instead of being proposed under a false name.
            _ud_idea = user_directed_idea(args)
            direction, structure = _ud_idea.direction, _ud_idea.structure

            # MODEL THESIS ON A USER-DIRECTED NAME (2026-08-12).
            # This route used to journal the CLI default string ("User-directed proposal.") as the
            # thesis, discarding the reasoning the model actually had for the name. That matters
            # twice: the brief now reads the entry thesis back to the strategist when it weighs the
            # open book, so it was being handed a placeholder instead of its own reasoning; and a
            # thesis is what you judge "is this still working?" against later.
            #
            # Trevor still directs WHAT to trade -- ticker, structure, DTE, delta are untouched.
            # We only ask the model for its VIEW of the name, and record it honestly. A decline is
            # surfaced rather than hidden: "the model would not endorse this name" is information
            # you want BEFORE tapping, not a placeholder afterwards. Fail-soft: any error keeps the
            # user's thesis and the trade proceeds exactly as before.
            _ud_model_note = None
            if not getattr(args, "no_model_thesis", False):
                try:
                    # Reuse the trader's most recent FULL research brief (written every 20 min
                    # to audit.jsonl with price structure, VIX, events, headlines and the book).
                    # Rebuilding one here would duplicate the whole research plumbing for a single
                    # name; a <=20-minute-old brief is the same evidence the slate reasons from.
                    _bfile, _brief_txt = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                      "audit.jsonl"), None
                    with open(_bfile) as _bf:
                        for _bl in _bf:
                            try:
                                _be = json.loads(_bl)
                            except Exception:
                                continue
                            if _be.get("event") == "strategist_brief" and _be.get("brief"):
                                _brief_txt = _be["brief"]
                    if not _brief_txt:
                        raise RuntimeError("no research brief available yet")
                    # 2026-08-13: vLLM defaults thinking OFF, and without it the model
                    # answers in ~7 tokens with an empty trade list.
                    _mv = propose_intents(
                        tr.get("llm_endpoint"), tr.get("llm_model"), _brief_txt,
                        ticker=args.ticker.upper(), timeout=300,
                        thinking="enabled")
                    if _mv:
                        _mi = _mv[0]
                        _ud_idea.thesis = _mi.thesis or _ud_idea.thesis
                        _ud_idea.conviction = int(_mi.conviction)
                        if getattr(_mi, "intended_hold_days", None):
                            _ud_idea.intended_hold_days = int(_mi.intended_hold_days)
                        _ud_model_note = "model endorses (conviction %d)" % _mi.conviction
                    else:
                        _ud_idea.thesis = ("USER-DIRECTED. The model DECLINED to endorse %s on "
                                           "today's evidence; this trade is Trevor's call, not "
                                           "the model's. Original note: %s"
                                           % (args.ticker.upper(), args.thesis))
                        _ud_model_note = "model DECLINED to endorse this name"
                except Exception as _mte:
                    _ud_model_note = "model view unavailable (%s)" % str(_mte)[:80]
            ideas = [_ud_idea]
            audit(audit_path, "user_directed_proposal", underlying=args.ticker.upper(),
                  direction=direction, structure=structure, dte=args.dte, delta=args.delta,
                  model_view=_ud_model_note, thesis=_ud_idea.thesis[:300])
        else:
            _all = sorted({"SPY", "QQQ", "IWM"} | {n.upper() for n in tr.get("approved_names", [])})
            _core = ["SPY", "QQQ", "IWM"]; _watch = [n for n in _all if n not in _core]
            _off = datetime.now(timezone.utc).timetuple().tm_yday % max(1, len(_watch))
            _base_names = _core + (_watch[_off:] + _watch[:_off])[:35]  # rotating deep-research cap
            _ape_feed, _ape_probe_rows = await _load_apewisdom_pool(ib, tr, audit_path)
            _candidate_names = apewisdom.merge_research_universe(_base_names, _ape_probe_rows)
            today = str(datetime.now(timezone.utc).date())
            quotes = await fetch_universe_quotes(ib, _candidate_names)
            _ape_cfg = tr.get("apewisdom_discovery") or {}
            _ape_limit = max(1, min(20, int(_ape_cfg.get("research_limit", 8))))
            _ape_quoted_rows = [r for r in _ape_probe_rows
                                if usable_price((quotes.get(r["ticker"]) or {}).get("last"))]
            _ape_rows = _ape_quoted_rows[:_ape_limit]
            names = apewisdom.merge_research_universe(_base_names, _ape_rows)
            _ape_names = {r["ticker"] for r in _ape_rows}

            # AFFORDABILITY SCREEN (2026-08-14). Drop names whose cheapest plausible debit
            # spread cannot fit the per-trade cap, so a $1,600 underlying never consumes a
            # proposal slot. Fails OPEN: if the account cannot be read we keep every name
            # rather than guess a ceiling and silently starve discovery.
            _afford_dropped = []
            try:
                _screen_pot = await get_pot_snapshot(ib)
                _screen_cap = construction.max_premium_budget(
                    getattr(_screen_pot, "net_liq", 0.0), CONS)
            except Exception as _se:
                _screen_cap = 0.0
                print(f"[WARN] affordability screen skipped (account unreadable: {_se})")
            if _screen_cap and _screen_cap > 0:
                _CORE = {"SPY", "QQQ", "IWM"}
                _kept = []
                for _n in names:
                    if _n in _CORE:
                        _kept.append(_n)
                        continue
                    _last = (quotes.get(_n) or {}).get("last")
                    try:
                        _spot = float(_last)
                    except (TypeError, ValueError):
                        _kept.append(_n)          # unknown price -> let construction decide
                        continue
                    # Fraction of notional used as a floor-cost proxy for a 0.60-delta
                    # spread. 0.02 assumed width scales with share price and deleted the whole
                    # universe above ~$230 (slate went 1/3,1/1,1/1 -> 0/0,0/0,0/0). Width is
                    # chosen, not implied by spot, so 0.01 now rejects only names whose spot
                    # exceeds the cap outright. Construction enforces the REAL limit downstream
                    # against actual quoted debits via max_premium_pct.
                    if _spot > 0 and (_spot * 100.0 * AFFORD_NOTIONAL_FRAC) > _screen_cap:
                        _afford_dropped.append((_n, round(_spot, 2)))
                    else:
                        _kept.append(_n)
                if _afford_dropped:
                    print("[SCREEN] affordability dropped %d of %d name(s) over the $%.0f "
                          "per-trade cap (kept %d): %s"
                          % (len(_afford_dropped), len(_afford_dropped) + len(_kept),
                             _screen_cap, len(_kept),
                                       ", ".join("%s@$%.0f" % (t, p)
                                                 for t, p in _afford_dropped)))
                    audit(audit_path, "affordability_screen",
                          per_trade_cap=round(_screen_cap, 2),
                          dropped=[{"ticker": t, "spot": p} for t, p in _afford_dropped],
                          kept=len(_kept))
                names = _kept
                _ape_names = {t for t in _ape_names if t in set(_kept)}
            _afford_screen = True
            if _ape_feed is not None:
                audit(audit_path, "apewisdom_research_universe",
                      candidates=sorted(_ape_names), count=len(_ape_names),
                      excluded_no_live_quote=sorted(
                          r["ticker"] for r in _ape_probe_rows if r not in _ape_quoted_rows),
                      excluded_by_research_cap=[r["ticker"] for r in _ape_quoted_rows[_ape_limit:]],
                      attention_metrics_in_main_brief=False,
                      trade_authority=False, training_eligible=False)
            data = await research.gather(ib, names, single_names=[n for n in names if n not in ("SPY", "QQQ", "IWM")])
            try:
                _slate_book = await _open_positions_for_risk()
            except Exception as _book_error:
                print(f"[WARN] slate book fetch failed (brief shows no positions): {_book_error}")
                _slate_book = None
            _brief_pot = await get_pot_snapshot(ib)
            _brief_account = entry_safety.account_snapshot_valid(_brief_pot)
            if not _brief_account.allowed:
                reason = "; ".join(_brief_account.reasons)
                audit(audit_path, "strategist_skipped", reason="account_snapshot_invalid: " + reason)
                approval.post_proposal(
                    token, channel,
                    ":warning: *Daily slate skipped* — live account sizing data is invalid or "
                    f"unavailable ({reason}). No model trade was requested.")
                return 1
            brief = research.build_brief(
                today=today, quotes=quotes, universe=names,
                allow_any_name=True, book=_slate_book,
                net_liq=_brief_pot.net_liq,
                available_funds=_brief_pot.available_funds, **data)
            _slate_price_stats = data.get("price_stats")  # the technical card fed to the model this slate

            # Morning discovery: scout NEW watchlist candidates worth researching (not trades to place).
            # SOFT-MUTEX (2026-06-23): hold the slate-active flag across the discovery+propose model
            # burst so the trader defers its exit-management model call instead of colliding with us.
            _slate_gen = slate_active_guard(); _slate_gen.__enter__()
            broad_cands = []
            try:
                broad_cands = discover_names(
                    tr.get("llm_endpoint"), tr.get("llm_model"), brief,
                    exclude=set(_all) | _ape_names, timeout=400,
                    blocked=tr.get("blocked_names", []))
            except Exception as e:
                audit(audit_path, "discovery_error", source="ordinary", error=str(e))

            ape_cands = []
            _ape_new_rows = [r for r in _ape_rows if r["ticker"] not in set(_all)]
            if _ape_new_rows:
                try:
                    _ape_discovery_brief = apewisdom.discovery_context(
                        brief, _ape_new_rows, watched=_all,
                        price_stats=_slate_price_stats)
                    _ape_reviewed = discover_names(
                        tr.get("llm_endpoint"), tr.get("llm_model"), _ape_discovery_brief,
                        exclude=set(_all), timeout=400, blocked=tr.get("blocked_names", []))
                    ape_cands = apewisdom.bind_reviewed_candidates(
                        _ape_reviewed, _ape_new_rows, watched=_all,
                        price_stats=_slate_price_stats)
                    _ape_addable = {t for t, _ in ape_cands}
                    audit(audit_path, "apewisdom_discovery",
                          eligible=[r["ticker"] for r in _ape_new_rows],
                          candidates=[t for t, _ in ape_cands],
                          signal_type="attention_only", trade_authority=False,
                          training_eligible=False)
                except Exception as e:
                    audit(audit_path, "discovery_error", source="apewisdom", error=str(e))

            cands = _merge_discovery_candidates(broad_cands, ape_cands)
            if cands:
                disc_cands = [t for t, _ in cands]
                disc_ts = approval.post_proposal(token, channel,
                    ":mag: *Names to consider* (scouted this morning — NOT on your watchlist):\n"
                    + "\n".join(f"  • *{t}* — {why}" for t, why in cands)
                    + "\n_Reply *add TICKER* (or *add all*) right here to put any on the watchlist._"
                    + "\n_Anytime (even outside this window): just ask Alfred to *add TICKER*._")
                audit(audit_path, "discovery", candidates=disc_cands,
                      apewisdom_candidates=[t for t, _ in ape_cands])

            try:
                _res = propose_intents(
                    tr.get("llm_endpoint"), tr.get("llm_model"), brief,
                    timeout=400, recommend=True, return_cot=True,
                    return_identity=True,
                    # 2026-08-13: THE fix for "DeepSeek recommends no trades". vLLM launches with
                    # thinking OFF by default, so this call was returning {"trades": []} in ~7
                    # tokens -- logged as strategist_error "empty model output" -- rather than
                    # reasoning about the brief. SLATE_THINKING=disabled still overrides this.
                    thinking="enabled")
                # Stage A returns intent only. The shared IBKR builder and Stage B selection
                # below are the same ones used by continuous Trader and add-name.
                if isinstance(_res, tuple) and len(_res) == 4:
                    intents, _raw_slate, _slate_cot, _slate_identity = _res
                elif isinstance(_res, tuple) and len(_res) == 3:
                    intents, _raw_slate, _slate_cot = _res
                elif isinstance(_res, tuple) and len(_res) == 2:
                    intents, _raw_slate = _res
                else:
                    intents = _res
                ideas = await _materialize_stage_b(
                    ib, intents, _brief_pot, tr, audit_path)
            except Exception as e:
                audit(audit_path, "propose_error", error=str(e))
                approval.post_proposal(token, channel,
                    f":warning: *Daily slate* — couldn't reach the model to generate ideas ({e}). Retry with slate-now.")
                print(f"[ERROR] propose failed: {e}")
                _slate_gen.__exit__(None, None, None)
                return 1
            ideas.sort(key=lambda i: -i.conviction)
            audit(audit_path, "apewisdom_trade_consideration",
                  researched=sorted(_ape_names),
                  proposed=sorted(i.underlying for i in ideas if i.underlying in _ape_names),
                  attention_metrics_in_main_brief=False, trade_authority=False)
            audit(audit_path, "daily_recommend", count=len(ideas),
                  scores=[i.conviction for i in ideas])
            _slate_gen.__exit__(None, None, None)  # generation burst done -> release the soft-mutex
            if not ideas:
                approval.post_proposal(token, channel,
                    ":calendar: *Daily slate* — no tradeable idea today (genuinely nothing the strategist would recommend).")
                # RECORD-ONLY (v2): learn from the pass -- capture the NO_TRADE with the raw model
                # output + the brief that fed it. Never raises into the slate path.
                try:
                    trade_capture.capture_no_trade(
                        trade_capture.dataset_dir(JOURNAL_PATH), source="daily_slate",
                        reason="empty_slate", raw_strategist=_raw_slate, cot=_slate_cot,
                        market_context=brief, model_identity=_slate_identity)
                except Exception as _ce:
                    print(f"[WARN] daily-slate no_trade capture failed (continuing): {_ce}")
                print("[INFO] no ideas"); return 0

        pot = await get_pot_snapshot(ib)
        default_pct = float(tr.get("max_trade_pct", 0.12))     # conservative default slice of the pot
        cons_budget = min(pot.available_funds, default_pct * pot.net_liq)
        # (ts, resolved, tp, sl, idea, over_default, posted_monotonic, decision_id,
        #  revision, original_candidates, raw_strategist, cot, model_request_context,
        #  model_technical_card)
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
                             technical_card=_slate_price_stats, model_identity=_slate_identity)

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
                    # External-source names are rechecked against the current config, liquidity,
                    # SMART stock identity, and option chain immediately before watchlist write.
                    # A long-running slate must not rely on a hours-old social-source screen.
                    _fresh_cfg = yaml.safe_load(open(args.config)) or {}
                    _fresh_tr = _fresh_cfg.get("trading", {}) or {}
                    _fresh_blocked = {str(t).upper() for t in _fresh_tr.get("blocked_names", [])}
                    _ape_row_by_ticker = {r["ticker"]: r for r in _ape_rows}
                    _accepted_new, _rejected_new = [], []
                    for _ticker in new:
                        if _ticker in _fresh_blocked or _ticker == "TSLA":
                            _rejected_new.append((_ticker, "currently_blocked"))
                            continue
                        if _ticker in _ape_addable:
                            _row, _reason = await _probe_apewisdom_row(
                                ib, _ape_row_by_ticker[_ticker],
                                _fresh_tr.get("blocked_sector_keywords", []),
                                asyncio.Semaphore(1))
                            if _row is None:
                                _rejected_new.append((_ticker, _reason))
                                continue
                        _accepted_new.append(_ticker)
                    new = _accepted_new
                    if _rejected_new:
                        audit(audit_path, "discovery_add_rejected_recheck",
                              rejected=[{"ticker": t, "reason": r} for t, r in _rejected_new])
                        approval.post_proposal(token, channel,
                            ":warning: Not added after the fresh eligibility recheck: "
                            + ", ".join(f"*{t}* ({r})" for t, r in _rejected_new))
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
                            _one_identity = None
                            _one_brief = brief
                            _one_price_stats = _slate_price_stats
                            try:
                                _one_pot = await get_pot_snapshot(ib)
                                _one_account = entry_safety.account_snapshot_valid(_one_pot)
                                if not _one_account.allowed:
                                    raise RuntimeError(
                                        "account snapshot invalid: " + "; ".join(_one_account.reasons))
                                if tk in _ape_addable:
                                    # The social feed only nominated the ticker. Rebuild a fresh,
                                    # independent market brief before same-day trade consideration;
                                    # never reuse attention metrics or an hours-old morning snapshot.
                                    _one_names = ["SPY", "QQQ", "IWM", tk]
                                    _one_quotes = await fetch_universe_quotes(ib, _one_names)
                                    if not usable_price((_one_quotes.get(tk) or {}).get("last")):
                                        raise RuntimeError("fresh underlying quote unavailable")
                                    _one_data = await research.gather(
                                        ib, _one_names, single_names=[tk])
                                    _one_book = await _open_positions_for_risk()
                                    _one_brief = research.build_brief(
                                        today=str(datetime.now(timezone.utc).date()),
                                        quotes=_one_quotes, universe=_one_names,
                                        allow_any_name=False, book=_one_book,
                                        net_liq=_one_pot.net_liq,
                                        available_funds=_one_pot.available_funds,
                                        **_one_data)
                                    _one_price_stats = _one_data.get("price_stats")
                                else:
                                    _one_brief = research.with_account_sizing_snapshot(
                                        _one_brief, net_liq=_one_pot.net_liq,
                                        available_funds=_one_pot.available_funds)
                                with slate_active_guard():
                                    _one = propose_intents(
                                        tr.get("llm_endpoint"), tr.get("llm_model"),
                                        _one_brief, ticker=tk, timeout=400,
                                        return_cot=True, return_identity=True,
                                        # PER TICKER: this loop pays ~3-7 min per name. Enabled
                                        # anyway -- a 7-token empty answer is useless, not cheap.
                                        thinking="enabled")
                                if isinstance(_one, tuple) and len(_one) == 4:
                                    _one_intents, _one_raw, _one_cot, _one_identity = _one
                                elif isinstance(_one, tuple) and len(_one) == 3:
                                    _one_intents, _one_raw, _one_cot = _one
                                elif isinstance(_one, tuple) and len(_one) == 2:
                                    _one_intents, _one_raw = _one
                                else:
                                    _one_intents = _one
                                with slate_active_guard():
                                    _one_ideas = await _materialize_stage_b(
                                        ib, _one_intents, _one_pot, tr, audit_path)
                                idea = _one_ideas[0] if _one_ideas else None
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
                                             market_context=_one_brief, technical_card=_one_price_stats,
                                             model_identity=_one_identity)
            for (ts, r, tp_pct, sl_pct, idea, over_default, posted_at, decision_id,
                 revision, capture_candidates, capture_raw, capture_cot, capture_context,
                 capture_technical_card) in pending:
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
                _stage_b_pending = getattr(idea, "_stage_b_binding", None) is not None
                if _stage_b_pending and (ovr or full_size or qty_ovr or ov_tp or ov_sl):
                    approval.post_proposal(
                        token, channel,
                        f":no_entry: *{r.underlying}* Stage-B terms were edited; nothing placed. "
                        "Generate a fresh intent/candidate decision instead.")
                    audit(audit_path, "stage_b_override_refused", underlying=r.underlying,
                          decision_id=decision_id, structure_override=ovr,
                          quantity_override=qty_ovr, full_size=full_size,
                          tp_pct=ov_tp, sl_pct=ov_sl)
                    done.add(ts)
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
                # BYPASS 3: the override is applied by apply_structure_override(), which runs
                # the allow-list on the RESULT and relabels a structure the human's own direction
                # override has just made stale (audited + shown, never silent).
                if _stage_b_pending:
                    effective_idea, _ovr_error, _ovr_note = idea, "", ""
                else:
                    effective_idea, _ovr_error, _ovr_note = apply_structure_override(idea, ovr)
                nd, ns = effective_idea.direction, effective_idea.structure
                if _ovr_error:
                    approval.post_proposal(token, channel,
                        f":no_entry: *{r.underlying}* NOT placed — {_ovr_error}")
                    audit(audit_path, "structure_override_rejected", underlying=r.underlying,
                          decision_id=decision_id, override=dict(ovr),
                          structure=idea.structure, direction=idea.direction, reason=_ovr_error)
                    done.add(ts)
                    continue
                if _ovr_note:
                    approval.post_proposal(token, channel,
                        f":pencil2: *{r.underlying}* — {_ovr_note}")
                    audit(audit_path, "structure_relabelled", underlying=r.underlying,
                          decision_id=decision_id, override=dict(ovr),
                          was=idea.structure, now=ns, direction=nd, note=_ovr_note)

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
                if is_credit(effective_idea):
                    _deployed_now = await broker_deployed_csp_collateral(ib, audit_path)
                    if _deployed_now is None:
                        audit(audit_path, "deployed_collateral_unverifiable",
                              decision_id=decision_id)
                        done.add(ts)
                        continue
                    _dep = min(_dep, max(0.0, 0.80 * snap.net_liq - _deployed_now))
                    avail = _dep
                else:
                    _mp = construction.max_premium_budget(snap.net_liq, CONS)
                    if _mp > 0:
                        _dep = min(_dep, _mp)
                    avail = (_dep if (_stage_b_pending or full_size or over_default)
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
                fresh_r.decision_revision = revision
                fresh_r.model_identity = getattr(r, "model_identity", None)
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
                        fresh_r.decision_revision = revision
                        fresh_r.model_identity = getattr(r, "model_identity", None)
                # TAKE-PROFIT OVERRIDE (`tp 300` in the approval reply).
                #
                # 2026-07-26, TREVOR'S RULING: the 100-500% band applies to THE MODEL ONLY, not to
                # him. Sol R5 R2's band exists to stop the CODE from manufacturing a target -- his
                # ruling was "+20% is doctrine, not a clamp", and the thing being abolished is
                # MECHANICAL installation, not human judgment. A human typing `tp 60` at the
                # approval seam has just looked at the trade and decided; that is the judgment the
                # doctrine defers to, not the mechanism it removes. Refusing it would have made the
                # operator less able to act on his own book than the model is.
                #
                # So: honour any human-typed level, but make it LOUD and audited -- it is a
                # deliberate departure from "winners run", and it must never be mistakable for the
                # old automatic stamp. Sanity bounds only (>0, <=1000%); absurd input still refused.
                if ov_tp:
                    if ov_tp > 0 and ov_tp <= 1000.0:
                        eff_tp = float(ov_tp)
                        _band_note = construction.optional_take_profit_pct(ov_tp)[1]
                        _outside = f" (outside the model's {_band_note})" if _band_note else ""
                        approval.post_proposal(token, channel,
                            f":pushpin: take-profit set to `{ov_tp:g}%` by human override{_outside}"
                            f" — this trade will NOT run free.")
                        audit(audit_path, "take_profit_override_accepted_human",
                              underlying=fresh_r.underlying, profit_target_pct=eff_tp,
                              outside_model_band=bool(_band_note), decision_id=decision_id)
                    else:
                        approval.post_proposal(token, channel,
                            f":no_entry_sign: take-profit override `{ov_tp:g}` refused — "
                            f"outside sanity bounds (0 < tp <= 1000).")
                        audit(audit_path, "take_profit_override_refused",
                              underlying=fresh_r.underlying, requested_profit_target_pct=ov_tp,
                              reason="outside sanity bounds", decision_id=decision_id)
                        eff_tp = tp_pct
                else:
                    eff_tp = tp_pct
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
                    _credit_fresh = is_credit(fresh_r)
                    _cost = capital_committed(fresh_r)
                    _gate = risk.evaluate_trade(
                        risk.ProposedTrade(
                            underlying=fresh_r.underlying, notional=_cost,
                            is_index=bool(effective_idea.is_index),
                            conviction=int(getattr(effective_idea, "conviction", 1)),
                            is_long=(False if _credit_fresh else fresh_r.right == "C"),
                            profit_target_pct=(0.0 if _credit_fresh else eff_tp),
                            stop_pct=(0.0 if _credit_fresh else eff_sl)),
                        net_liq=snap.net_liq, available_funds=snap.available_funds,
                        open_positions=_risk_positions,
                        pot_day_start=(_baseline if not isinstance(_baseline, entry_safety.SafetyResult)
                                       else 0.0),
                        approved_names={str(n).upper() for n in tr.get("approved_names", [])},
                        limits=(_credit_limits(_RISK_LIMITS) if _credit_fresh else _RISK_LIMITS))
                    if not _gate.approved:
                        _block_reasons.extend(_gate.reasons)
                    if _credit_fresh:
                        _deployed_final = await broker_deployed_csp_collateral(ib, audit_path)
                        _collateral_final = collateral_capacity(
                            required=required_collateral(fresh_r.strike, fresh_r.qty),
                            deployed=_deployed_final, net_liq=snap.net_liq,
                            available_funds=snap.available_funds)
                        _block_reasons.extend(_collateral_final.reasons)
                    else:
                        okf, whyf = construction.check_budget(
                            _cost, fresh_r.dte, snap.net_liq,
                            construction.open_book(_positions, JOURNAL_PATH), CONS)
                        if not okf:
                            _block_reasons.extend(whyf)
                        _edays_final = await asyncio.to_thread(
                            research.days_to_earnings, fresh_r.underlying)
                        if _edays_final is None and not entry_safety.is_no_earnings_etf(
                                fresh_r.underlying):
                            _block_reasons.append("earnings date unavailable at approval time")
                        # An ETF has no earnings date: missing is EXPECTED, not a failure.
                        # NOT risk.INDEX_UNDERLYINGS -- that set also skips the blocklist and
                        # the approved-names universe check.
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
                    _latest_r.decision_revision = revision
                    _latest_r.model_identity = getattr(r, "model_identity", None)
                    _latest_nbbo = entry_safety.nbbo_valid(_latest_r)
                    if not _latest_nbbo.allowed:
                        raise RuntimeError("; ".join(_latest_nbbo.reasons))
                    _latest_credit = is_credit(_latest_r)
                    _latest_cost = capital_committed(_latest_r)
                    _latest_gate = risk.evaluate_trade(
                        risk.ProposedTrade(
                            underlying=_latest_r.underlying, notional=_latest_cost,
                            is_index=bool(effective_idea.is_index),
                            conviction=int(getattr(effective_idea, "conviction", 1)),
                            is_long=(False if _latest_credit else _latest_r.right == "C"),
                            profit_target_pct=(0.0 if _latest_credit else eff_tp),
                            stop_pct=(0.0 if _latest_credit else eff_sl)),
                        net_liq=snap.net_liq, available_funds=snap.available_funds,
                        open_positions=_risk_positions,
                        pot_day_start=_baseline,
                        approved_names={str(n).upper() for n in tr.get("approved_names", [])},
                        limits=(_credit_limits(_RISK_LIMITS) if _latest_credit else _RISK_LIMITS))
                    if not _latest_gate.approved:
                        raise RuntimeError("; ".join(_latest_gate.reasons))
                    if _latest_credit:
                        _latest_deployed = await broker_deployed_csp_collateral(ib, audit_path)
                        _latest_capacity = collateral_capacity(
                            required=required_collateral(_latest_r.strike, _latest_r.qty),
                            deployed=_latest_deployed, net_liq=snap.net_liq,
                            available_funds=snap.available_funds)
                        if not _latest_capacity.allowed:
                            raise RuntimeError("; ".join(_latest_capacity.reasons))
                    else:
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

                _changes = list(
                    credit_material_changes(r, fresh_r) if is_credit(r)
                    else entry_safety.material_changes(r, fresh_r))
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
                    _exec_label = ("Executable SELL credit" if is_credit(fresh_r)
                                   else "Executable BUY limit")
                    _exec_price = (credit_executable_price(fresh_r) if is_credit(fresh_r)
                                   else entry_safety.executable_price(fresh_r))
                    _remsg = (f":repeat: *Reapproval required — {fresh_r.underlying}*\n"
                              f"Refreshed order: `{order_summary(fresh_r)}`\n"
                              f"{_exec_label}: *${_exec_price:.2f}*\n"
                              f"Changed: {'; '.join(_changes)}\n"
                              f":point_down: Tap :white_check_mark: again within 5 minutes to approve these exact terms.\n"
                              f"_Decision ID: `{decision_id}`, revision {revision + 1}_")
                    _new_ts = approval.post_proposal(token, channel, _remsg)
                    done.add(ts)
                    if _new_ts:
                        fresh_r.decision_revision = revision + 1
                        pending.append((_new_ts, fresh_r, eff_tp, eff_sl, effective_idea,
                                        False, time.monotonic(), decision_id, revision + 1,
                                        capture_candidates, capture_raw, capture_cot,
                                        capture_context, capture_technical_card))
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
                # STRUCTURE GATE AT THE MONEY BOUNDARY -- this file has its own placeOrder call,
                # so it needs its own submit-time check; trader._submit_order_unlocked's does not
                # cover this path. Last line before the order is priced and captured.
                _ok_submit, _why_submit = submit_structure_ok(r, effective_idea)
                if not _ok_submit:
                    approval.post_proposal(token, channel,
                        f":no_entry: *{r.underlying}* NOT placed — structure gate at submit: "
                        f"{_why_submit}")
                    audit(audit_path, "structure_blocked_submit", decision_id=decision_id,
                          underlying=r.underlying, structure=r.structure, right=r.right,
                          reason=_why_submit)
                    done.add(ts)
                    continue
                if is_credit(r):
                    if (str(r.right).upper()[:1] != "P" or r.short_contract is not None
                            or str(r.structure).strip().lower() != "cash secured put"):
                        audit(audit_path, "credit_structure_blocked_submit",
                              decision_id=decision_id, underlying=r.underlying)
                        done.add(ts)
                        continue
                    _lmt = credit_executable_price(r)
                else:
                    _lmt = entry_safety.executable_price(r)
                try:
                    trade_capture.capture_decision(
                        trade_capture.dataset_dir(JOURNAL_PATH), source="daily_slate",
                        symbol=r.underlying, right=r.right, strike=r.strike, expiry=r.expiry,
                        structure=("cash secured put" if is_credit(r) else
                                   ("spread" if r.short_contract is not None else "single")),
                        con_id=getattr(r.contract, "conId", None), chosen_idea=effective_idea,
                        candidates=(capture_candidates or [effective_idea]),
                        raw_strategist=capture_raw, cot=capture_cot,
                        market_context=capture_context,
                        technical_card=capture_technical_card,
                        decision_id=decision_id, revision=revision, event="approved",
                        model_identity=getattr(r, "model_identity", None),
                        final_contract=contract_snapshot(r),
                        order_ref=entry_safety.decision_order_ref(decision_id),
                        human_action={"action": "approve", "structure_override": ovr,
                                      "quantity_override": qty_ovr, "full_size": full_size,
                                      "tp_pct": eff_tp, "sl_pct": eff_sl})
                except Exception as _capture_error:
                    print(f"[WARN] final decision capture failed (continuing): {_capture_error}")
                order = Order(action=("SELL" if is_credit(r) else "BUY"),
                              orderType="LMT", lmtPrice=_lmt,
                              totalQuantity=r.qty, tif="DAY")
                order.orderRef = entry_safety.decision_order_ref(decision_id)
                _credit_reservation = None
                if is_credit(r):
                    _credit_reservation, _reservation_pot, _reservation_broker = \
                        await reserve_credit_entry(
                            ib, r, order.orderRef, ledger=DAILY_ENTRY_RESERVATIONS,
                            audit_path=audit_path)
                    if not _credit_reservation.allowed or not _credit_reservation.should_place:
                        audit(audit_path, "credit_collateral_blocked_submit",
                              decision_id=decision_id,
                              status=_credit_reservation.status,
                              reasons=_credit_reservation.reasons)
                        done.add(ts)
                        continue
                _order_contract = (conn.create_combo_contract(
                    r.underlying, [(r.contract.conId, "BUY"),
                                   (r.short_contract.conId, "SELL")])
                    if r.short_contract is not None and not is_credit(r) else r.contract)
                from exitmgr.order_lock import order_mutation_lock
                _place_invoked = False
                _pre_submit_error = None
                try:
                    with order_mutation_lock():
                        if _credit_reservation is not None:
                            _markers_final = entry_safety.entry_markers_clear(
                                config_path=args.config,
                                kill_switch_path=(cfg.get("kill_switch") or {}).get("path"))
                            if not _markers_final.allowed:
                                _pre_submit_error = (
                                    "entry markers block submit after reservation: "
                                    + "; ".join(_markers_final.reasons))
                            else:
                                _quote_final = entry_safety.nbbo_valid(r)
                                if not _quote_final.allowed:
                                    _pre_submit_error = (
                                        "fresh NBBO blocks submit after reservation: "
                                        + "; ".join(_quote_final.reasons))
                        if _pre_submit_error is None:
                            # order_mutation_lock is held across this final money-boundary call.
                            _place_invoked = True
                            trade = ib.placeOrder(_order_contract, order)
                except Exception as _place_error:
                    if _credit_reservation is not None and not _place_invoked:
                        await asyncio.to_thread(
                            DAILY_ENTRY_RESERVATIONS.clear, order.orderRef)
                        audit(audit_path, "credit_entry_reservation_cleared",
                              order_ref=order.orderRef,
                              status="definite_pre_submit_failure")
                    elif _credit_reservation is not None:
                        audit(audit_path, "credit_place_ambiguous_reservation_retained",
                              order_ref=order.orderRef, error=str(_place_error))
                    raise
                if _pre_submit_error is not None:
                    await asyncio.to_thread(DAILY_ENTRY_RESERVATIONS.clear, order.orderRef)
                    audit(audit_path, "credit_entry_reservation_cleared",
                          order_ref=order.orderRef, status="definite_pre_submit_block",
                          reason=_pre_submit_error)
                    done.add(ts)
                    continue
                # Wait for IBKR to ACK (live) or REJECT — never assume it landed (Error 201 etc.).
                _reject_states = {"Cancelled", "ApiCancelled", "Inactive", "Rejected"}
                _live_states = {"PreSubmitted", "Submitted", "Filled"}
                for _ in range(16):  # up to ~8s
                    await asyncio.sleep(0.5)
                    st = trade.orderStatus.status
                    if st in _live_states or st in _reject_states:
                        break
                st = trade.orderStatus.status
                _reasons = [le.message for le in trade.log if getattr(le, "errorCode", 0)]
                if _credit_reservation is not None:
                    await asyncio.to_thread(
                        DAILY_ENTRY_RESERVATIONS.clear_for_status, order.orderRef, st)
                if st in _reject_states:
                    reason = _reasons[-1] if _reasons else f"order status {st}"
                    approval.post_proposal(token, channel,
                        f":x: *Order REJECTED by IBKR* — `{order_summary(r)}` was NOT placed.\n{reason}")
                    audit(audit_path, "daily_rec_rejected", underlying=r.underlying,
                          order=order_summary(r), status=st, reason=reason)
                    done.add(ts)
                    continue
                try:
                    trade_capture.capture_decision(
                        trade_capture.dataset_dir(JOURNAL_PATH), source="daily_slate",
                        symbol=r.underlying, right=r.right, strike=r.strike, expiry=r.expiry,
                        structure=("cash secured put" if is_credit(r) else
                                   ("spread" if r.short_contract is not None else "single")),
                        con_id=getattr(r.contract, "conId", None), chosen_idea=effective_idea,
                        candidates=(capture_candidates or [effective_idea]),
                        raw_strategist=capture_raw, cot=capture_cot,
                        market_context=capture_context,
                        technical_card=capture_technical_card,
                        decision_id=decision_id, revision=revision, event="submitted",
                        model_identity=getattr(r, "model_identity", None),
                        final_contract=contract_snapshot(r),
                        order_ref=getattr(trade.order, "orderRef", None),
                        human_action={"action": "approve", "structure_override": ovr,
                                      "quantity_override": qty_ovr, "full_size": full_size,
                                      "tp_pct": eff_tp, "sl_pct": eff_sl})
                except Exception as _capture_error:
                    print(f"[WARN] submitted decision capture failed (continuing): {_capture_error}")
                # FILL VERIFICATION (2026-07-01 audit: 5/15 "executed" entries never filled --
                # the journal recorded intent, not fills). After the ACK, wait a bit longer for
                # the actual fill so the entry record carries fill status/price/timestamp; a
                # still-unfilled order stays on placed_watch and alarms after fill_alarm_minutes.
                for _ in range(20):  # up to ~10 more seconds for the fill itself
                    if trade.orderStatus.status == "Filled":
                        break
                    await asyncio.sleep(0.5)
                st = trade.orderStatus.status
                if _credit_reservation is not None:
                    await asyncio.to_thread(
                        DAILY_ENTRY_RESERVATIONS.clear_for_status, order.orderRef, st)
                if st in _reject_states:
                    reason = _reasons[-1] if _reasons else f"order status {st} after ACK"
                    audit(audit_path, "daily_rec_rejected_after_ack",
                          underlying=r.underlying, order=order_summary(r),
                          status=st, reason=reason)
                    done.add(ts)
                    continue
                _afp = getattr(trade.orderStatus, "avgFillPrice", None)
                _fill_px = float(_afp) if (st == "Filled" and _afp and _afp == _afp) else None
                # COMMISSIONS + REAL BASIS (2026-07-03): actual entry fee + fill-based cost basis
                # so realized P&L can be reported NET of fees and entry slippage is recorded.
                # ADDITIVE; never raises into the order path; never fabricates a fee/price.
                from exitmgr.order import commission_from_trade as _comm_from_trade, compute_entry_basis as _entry_basis
                _entry_comm = _comm_from_trade(trade) if st == "Filled" else None
                if is_credit(r):
                    _est_debit = capital_at_risk(r)
                    _efd = _eslip = _eslip_pct = None
                else:
                    _est_debit = round(r.limit * 100 * r.qty, 2)
                    _efd, _eslip, _eslip_pct = _entry_basis(
                        _est_debit, _fill_px, r.qty)
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
                    _journal = {"ts": datetime.utcnow().isoformat(),
                                      "decision_id": decision_id,
                                      "decision_revision": revision,
                                      "model_identity": getattr(r, "model_identity", None),
                                      "contract_id": r.contract.conId,
                                      "symbol": r.underlying, "right": r.right, "expiry": r.expiry,
                                      "strike": r.strike, "quantity": r.qty,
                                      "debit": (capital_at_risk(r) if is_credit(r) else
                                                round(r.limit * 100 * r.qty, 2)),
                                      "profit_target_pct": (None if is_credit(r) else eff_tp),
                                      # R2 MIGRATION FLAG (2026-07-26, Sol audit R5 R2). Marks this
                                      # line as written under the doctrine-only take-profit policy,
                                      # so construction.journal_take_profit_pct will honour its
                                      # value. Journal lines WITHOUT this stamp are pre-R2 and their
                                      # take-profit (the pot-tier or global-fallback number) must
                                      # never reactivate on a restart.
                                      "tp_policy": construction.TP_POLICY_CURRENT,
                                      "stop_pct": (None if is_credit(r) else eff_sl),
                                      "conviction": getattr(idea, "conviction", -1),
                                      "intended_hold_days": getattr(r, "intended_hold_days", None),
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
                                      **spread_j}
                    if is_credit(r):
                        _journal.update({
                            "side": "credit", "structure": "cash secured put",
                            "action": "SELL", "quantity": -abs(int(r.qty)),
                            "contracts": abs(int(r.qty)),
                            "collateral_usd": capital_committed(r),
                            "net_credit_usd": round(r.net_credit_usd, 2),
                            "max_loss_usd": capital_at_risk(r),
                            "assignment_possible": True,
                        })
                    f.write(_j.dumps(_journal, default=str) + "\n")
                placed_watch.append({"trade": trade, "r": r, "t0": time.monotonic(),
                                     "decision_id": decision_id,
                                     "model_identity": getattr(r, "model_identity", None),
                                     "alerted": False, "filled_logged": st == "Filled",
                                     "credit_reservation": is_credit(r),
                                     "order_ref": order.orderRef})
                tag = " _(your levels)_" if (ov_tp or ov_sl) else ""
                if is_credit(r):
                    approval.post_proposal(
                        token, channel,
                        f":white_check_mark: *Placed* `{order_summary(r)}` — collateral and "
                        "assignment lifecycle now managed from the journal.")
                else:
                    approval.post_proposal(token, channel,
                        f":white_check_mark: *Placed* `{order_summary(r)}` — take profit "
                        f"{_pct_text(eff_tp)} / stop -{eff_sl:.0f}%{tag}")
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
                                          "model_identity": w.get("model_identity"),
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
    ap.add_argument("--tp", type=float, default=0.0,
                    help="OPTIONAL explicit catastrophe backstop, 100-500%% (0 = none, the normal "
                         "case). There is no longer a global/tiered default take-profit — see "
                         "RULING_TAKE_PROFIT.md / Sol audit R5 R2.")
    ap.add_argument("--stop", type=float, default=0.0, help="stop %% (0 = global default -50%%)")
    ap.add_argument("--no-model-thesis", action="store_true", dest="no_model_thesis",
                    help="skip asking the model for its view of a --ticker name (keeps --thesis "
                         "verbatim). Default is to ask and record the real reasoning.")
    ap.add_argument("--hold-days", type=int, default=0, dest="hold_days",
                    help="intended hold in calendar days (0 = derive as ceil(dte/8), which keeps "
                         "the DTE inside the 5-8x doctrine window). Journalled for post-hoc audit.")
    ap.add_argument("--conviction", type=int, default=6, help="conviction 1-10 (display only)")
    ap.add_argument("--thesis", default="User-directed proposal.", help="thesis line shown in the proposal")
    ap.add_argument("--client-id", type=int, default=None, dest="client_id", help="override IBKR clientId (avoid clash with the cron's 93)")
    _args = ap.parse_args()
    # FAIL FAST, BEFORE ANY IBKR CONNECTION: a refused --structure must not cost a gateway connect,
    # a slate lock and a Slack post first. This is an EARLY COPY of the same one gate that runs
    # inside run() -- not a second check with a list of its own.
    if _args.ticker:
        try:
            user_directed_idea(_args)
        except ValueError as _structure_error:
            raise SystemExit("[REFUSED] %s" % _structure_error)
    raise SystemExit(asyncio.run(run(_args)))
