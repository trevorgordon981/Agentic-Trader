"""Trade-CONSTRUCTION gates (2026-07-01 rework, evidence-backed).

Journal audit (n=9 closed trades, 6/12-7/1) found the loss pool was mostly construction +
execution, not direction: median 17.5 DTE at entry bled 5.9-12.5%/day theta (both
direction-RIGHT losers died of short DTE); +75-100% profit targets were touched 0/9 times
while +20-35% MFE was reached by 4/8; far-OTM short strikes were lottery tickets (NOK 14/25
on a $13.62 stock); and 82-103% of the pot was in premium at times.

These are PURE functions (no IBKR, offline-testable) enforcing the rulebook wherever orders
are constructed (daily_recommend.py + exitmgr/trader.py). Every threshold lives in
config.yaml under `construction:` (see exitmgr.config.ConstructionConfig) so Trevor tunes
numbers there, never here.
"""
import json
import math
from datetime import date as _date, datetime, timedelta, timezone
from pathlib import Path

from exitmgr.rules import days_to_expiry


# --------------------------------------------------------------- DTE floor (gate A1)

def pick_expiry(expirations, target_dte, min_dte=25, prefer_dte_max=45, today=None):
    """Choose the entry expiry honoring the min-DTE floor (default 25, prefer 25-45).

    Nearest expiry to the idea's target_dte among expiries with DTE >= min_dte; a target
    below the floor (or above prefer_dte_max) is ADJUSTED into the band rather than
    rejected -- the trade idea is often sound, the model's expiry choice was the problem.
    Returns (expiry_str, dte, adjusted). (None, None, False) when NO expiry clears the
    floor (reject: no reasonable expiry exists).
    """
    t0 = today or datetime.now(timezone.utc).date()
    cands = []
    for e in expirations or []:
        try:
            d = (datetime.strptime(str(e)[:8], "%Y%m%d").date() - t0).days
        except (ValueError, TypeError):
            continue
        if d >= int(min_dte):
            cands.append((str(e), d))
    if not cands:
        return None, None, False
    try:
        tgt = int(target_dte or 0)
    except (TypeError, ValueError):
        tgt = 0
    eff_target = min(max(tgt, int(min_dte)), max(int(prefer_dte_max), int(min_dte)))
    exp, dte = min(cands, key=lambda x: abs(x[1] - eff_target))
    return exp, dte, (eff_target != tgt)


# --------------------------------------------------------------- TP/SL clamp (gate A2)

def tp_tier_for_pot(net_liq, tiers, fallback_max, fallback_default):
    """Pot-tiered take-profit ceiling + default (2026-07-03). Returns (tp_max_pct, tp_pct) in
    FRACTION units (0.35 == 35%), matching cons.tp_max_pct / cons.tp_pct.

    Picks the HIGHEST tier row whose min_pot <= net_liq; below the lowest tier the lowest row is
    used (the `< $2,500` floor row). `tiers` is an ordered list of dicts
    {min_pot, tp_max_pct, tp_pct}. Scales ONLY the take-profit side -- the caller must leave
    tp_min_pct and sl_pct (the protective stop) untouched.

    FAIL-SAFE, never up-sizes on missing data: empty/None tiers, or a missing/None/NaN net_liq,
    returns the flat (fallback_max, fallback_default) unchanged. Never raises -- a malformed row is
    skipped so a config typo can never widen a runner or crash entry.
    """
    if not tiers:
        return fallback_max, fallback_default
    try:
        nl = float(net_liq)
    except (TypeError, ValueError):
        return fallback_max, fallback_default
    if nl != nl:  # NaN net_liq -> flat (never up-size on garbage)
        return fallback_max, fallback_default
    rows = []
    for t in tiers:
        try:
            rows.append((float(t["min_pot"]), float(t["tp_max_pct"]), float(t["tp_pct"])))
        except (TypeError, ValueError, KeyError, IndexError):
            continue  # skip a malformed row rather than crash/widen
    if not rows:
        return fallback_max, fallback_default
    rows.sort(key=lambda r: r[0])
    chosen = rows[0]  # below the lowest tier -> the floor (`< $2,500`) row
    for mp, tmax, tdef in rows:
        if nl >= mp:
            chosen = (mp, tmax, tdef)
        else:
            break
    return chosen[1], chosen[2]


def clamp_tp_sl(tp_pct, sl_pct, cons):
    """Clamp sell levels into the audit-backed band. PERCENT units in/out (30.0 = 30%).

    TP: model value clamped into [tp_min_pct, tp_max_pct]; missing/0 -> default tp_pct.
    (75-100% targets were touched 0/9 times; +25-35% would have recovered ~$590 of ~$800.)
    SL: missing/0 -> default |sl_pct|; a model stop may be TIGHTER (smaller) than the
    default but never looser (tightened from the old -50%).

    ACCEPTED INVERTED REWARD:RISK ON SMALL POTS (INTENTIONAL -- do NOT "fix"): once the pot-tier
    default TP (config caps.tp_tiers, applied by the caller via tp_tier_for_pot) is 0.20-0.25 on a
    sub-$5k pot, the resulting default TP can sit BELOW the 30% stop, so reward < risk by design.
    Trevor's deliberate call: bank fast to climb out of down days while the stop still holds at 30%.
    Intentionally NO post-clamp R:R re-check here -- this clamp must not reject or re-widen a trade
    just because the accepted small-pot TP is below the stop. (See also config.yaml caps.tp_tiers.)
    """
    lo = float(cons.tp_min_pct) * 100.0
    hi = float(cons.tp_max_pct) * 100.0
    tp_def = float(cons.tp_pct) * 100.0
    sl_def = abs(float(cons.sl_pct)) * 100.0
    try:
        tp_in = float(tp_pct or 0.0)
    except (TypeError, ValueError):
        tp_in = 0.0
    try:
        sl_in = float(sl_pct or 0.0)
    except (TypeError, ValueError):
        sl_in = 0.0
    tp = tp_def if tp_in <= 0 else max(lo, min(hi, tp_in))
    sl = sl_def if sl_in <= 0 else min(sl_def, sl_in)
    sl = max(5.0, sl)  # never a hair-trigger stop from a garbage model value
    return round(tp, 1), round(sl, 1)


# --------------------------------------------------------------- structure sanity (gate A3)

def expected_move(spot, iv, dte):
    """1-sigma expected move ($) of the underlying over `dte` days at annualized IV.
    None when spot/IV are unavailable (caller applies the conservative fallback)."""
    try:
        s, v = float(spot or 0), float(iv or 0)
    except (TypeError, ValueError):
        return None
    if s <= 0 or v <= 0 or v != v:
        return None
    return s * v * math.sqrt(max(int(dte or 0), 1) / 365.0)


def effective_delta(target_delta, cons):
    """Long-leg target delta clamped into the [delta_min, delta_max] band (~0.55-0.65).
    Far-OTM low-delta legs were the audit's lottery tickets; a 0.55-0.65 leg is already
    working, not hoping. Garbage/missing -> band midpoint."""
    try:
        d = abs(float(target_delta))
    except (TypeError, ValueError):
        d = 0.0
    if d <= 0 or d != d:
        return (float(cons.delta_min) + float(cons.delta_max)) / 2.0
    return max(float(cons.delta_min), min(float(cons.delta_max), d))


def long_strike_ok(strike, spot, right, dte, atm_iv, cons):
    """Reject lottery LONG legs: the long strike may not sit further OTM than ~1 expected
    move for the holding horizon (SNDK needed +3.5% in 15d just to REACH its long strike).
    Conservative fallback when IV is unavailable: within strike_near_spot_pct of spot.
    Returns (ok, reason). Missing/invalid spot (or strike) is NOT a silent pass: it returns
    (False, "INSUFFICIENT_DATA: ...") so a data-feed gap HOLDS the trade rather than
    disabling this anti-lottery gate. Callers already treat `not ok` as skip/hold, so a
    transient quote gap merely delays the entry -- it cannot slip a lottery leg through.
    (Exits do not use this gate, so a spot outage never blocks closing a position.)"""
    try:
        s, k = float(spot or 0), float(strike or 0)
    except (TypeError, ValueError):
        return False, "INSUFFICIENT_DATA: spot/strike unavailable -- cannot judge lottery-long structure (holding)"
    if s <= 0 or k <= 0:
        return False, "INSUFFICIENT_DATA: spot/strike unavailable -- cannot judge lottery-long structure (holding)"
    otm = (k - s) if right == "C" else (s - k)
    if otm <= 0:
        return True, ""  # ITM/ATM long leg is fine
    em = expected_move(s, atm_iv, dte)
    lim = em if em is not None else float(cons.strike_near_spot_pct) * s
    if otm > lim + 1e-9:
        basis = "~1 expected move" if em is not None else f"{float(cons.strike_near_spot_pct):.0%} of spot (no IV; conservative)"
        return False, (f"long strike {k:g} is ${otm:,.2f} OTM of spot ${s:,.2f} "
                       f"(> {basis} ${lim:,.2f}) -- lottery-ticket structure")
    return True, ""


def spread_structure_ok(long_strike, short_strike, spot, right, dte, atm_iv, cons):
    """Debit-vertical sanity: the SHORT strike must be within ~1 expected move of spot for
    the holding horizon (the NOK 14/25 vertical on a $13.62 stock could never realize its
    width). Conservative fallback when IV is unavailable: width <= spread_width_max_pct of
    spot AND long strike within strike_near_spot_pct of spot. Returns (ok, reason).
    Missing/invalid spot (or either strike) is NOT a silent pass: it returns
    (False, "INSUFFICIENT_DATA: ...") so a data-feed gap HOLDS the trade rather than
    disabling this gate. In pick_spread_short this simply skips the candidate short leg;
    with no spot every candidate is held, so no unrealizable vertical can be constructed
    during a quote gap (a transient gap only delays the entry)."""
    try:
        s = float(spot or 0)
        lk, sk = float(long_strike or 0), float(short_strike or 0)
    except (TypeError, ValueError):
        return False, "INSUFFICIENT_DATA: spot/strikes unavailable -- cannot judge spread structure (holding)"
    if s <= 0 or lk <= 0 or sk <= 0:
        return False, "INSUFFICIENT_DATA: spot/strikes unavailable -- cannot judge spread structure (holding)"
    em = expected_move(s, atm_iv, dte)
    if em is not None:
        dist = abs(sk - s)
        if dist > em + 1e-9:
            return False, (f"short strike {sk:g} is ${dist:,.2f} from spot ${s:,.2f} "
                           f"(> ~1 expected move ${em:,.2f} for {dte} DTE)")
        return True, ""
    width = abs(sk - lk)
    if width > float(cons.spread_width_max_pct) * s + 1e-9:
        return False, (f"spread width {width:g} > {float(cons.spread_width_max_pct):.0%} of spot "
                       f"${s:,.2f} (no IV available; conservative gate)")
    if abs(lk - s) > float(cons.strike_near_spot_pct) * s + 1e-9:
        return False, (f"long strike {lk:g} > {float(cons.strike_near_spot_pct):.0%} from spot "
                       f"${s:,.2f} (no IV available; conservative gate)")
    return True, ""


# --------------------------------------------------------------- budget gates (gate A4)

def max_premium_budget(net_liq, cons):
    """Hard $ ceiling on a single trade's premium: max_premium_pct of net-liq (<=15%).
    0.0 when net-liq is unknown (caller must not use it as 'unlimited')."""
    try:
        nl = float(net_liq or 0)
    except (TypeError, ValueError):
        nl = 0.0
    return max(0.0, float(cons.max_premium_pct) * nl)


def check_budget(debit_usd, dte, net_liq, open_book, cons):
    """Budget gates on a candidate trade. Returns (ok, [reasons]).

      * premium per trade <= max_premium_pct of net-liq (<=15%);
      * TOTAL deployed premium (open book + this trade) <= max_deployed_pct (<=40%;
        the audit found 82-103% of the pot in premium -> one gap-down = -27% day);
      * theta-decay budget: (debit/DTE)/net-liq <= max_decay_pct_per_day per trade (1%/day)
        and the portfolio total <= max_portfolio_decay_pct_per_day (4%/day).

    open_book = [(net_debit_usd, dte), ...] for the OPEN positions (see open_book()).
    net-liq unknown -> pass (other gates and human approval still bind)."""
    reasons = []
    try:
        nl = float(net_liq or 0)
    except (TypeError, ValueError):
        nl = 0.0
    if nl <= 0:
        return True, []
    debit = float(debit_usd or 0)
    try:
        d = int(dte or 0)
    except (TypeError, ValueError):
        d = 0
    prem_cap = float(cons.max_premium_pct) * nl
    if debit > prem_cap + 1e-6:
        reasons.append(f"premium ${debit:,.0f} > {float(cons.max_premium_pct):.0%}-of-net-liq cap ${prem_cap:,.0f}")
    deployed = sum(float(b or 0) for b, _ in (open_book or []))
    dep_cap = float(cons.max_deployed_pct) * nl
    if deployed + debit > dep_cap + 1e-6:
        reasons.append(f"total deployed premium ${deployed + debit:,.0f} (open ${deployed:,.0f} + this ${debit:,.0f}) "
                       f"> {float(cons.max_deployed_pct):.0%}-of-net-liq cap ${dep_cap:,.0f}")
    # Decay checks need a real horizon: dte<=0 means UNKNOWN (legacy/mock construction),
    # so skip them rather than assume 1 day and false-reject. Book positions with unknown
    # DTE likewise contribute premium (above) but no decay estimate.
    decay = (debit / d) / nl if d > 0 else 0.0
    if d > 0 and decay > float(cons.max_decay_pct_per_day) + 1e-9:
        reasons.append(f"theta-decay budget {decay:.2%}/day (${debit:,.0f} over {d} DTE) "
                       f"> {float(cons.max_decay_pct_per_day):.1%}/day per-trade cap")
    port_decay = decay + sum((float(b or 0) / int(x)) for b, x in (open_book or [])
                             if x and int(x) > 0) / nl
    if port_decay > float(cons.max_portfolio_decay_pct_per_day) + 1e-9:
        reasons.append(f"portfolio theta-decay {port_decay:.2%}/day would exceed the "
                       f"{float(cons.max_portfolio_decay_pct_per_day):.0%}/day total cap")
    return (not reasons), reasons


def open_book(positions, journal_path):
    """[(net_debit_usd, dte), ...] for the open long legs -- the deployed-premium/decay book.

    positions: {con_id: PositionData} from IBConnection.get_positions() (long legs only;
    spread short legs are already excluded there). The journaled NET debit (max loss) is
    preferred, keyed by the long leg's contract_id, newest entry wins -- same convention as
    trader._load_journal_debits. Fallback: gross long-leg value (conservative)."""
    rows = {}
    p = Path(journal_path)
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("event"):  # lifecycle records (closed_by_tool etc.), not entries
                continue
            cid, d = e.get("contract_id"), e.get("debit")
            if cid is None or d is None:
                continue
            try:
                rows[int(cid)] = (float(d), e.get("expiry"))
            except (TypeError, ValueError):
                continue
    book = []
    for cid, pos in (positions or {}).items():
        r = rows.get(int(cid))
        if r is not None:
            debit, exp = r
            dte = days_to_expiry(exp)
        else:
            debit = abs(getattr(pos, "avg_cost", 0.0)) * 100 * abs(getattr(pos, "quantity", 0))
            dte = days_to_expiry(getattr(pos, "expiry", ""))
        book.append((float(debit), dte if (dte and dte > 0) else 0))  # 0 = unknown horizon
    return book


# --------------------------------------------------------------- earnings blackout (gate A5)

def _as_date(x):
    """Coerce a date/datetime/'YYYYMMDD'/'YYYY-MM-DD'/ISO string to a date; None if unparseable."""
    if x is None:
        return None
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, _date):
        return x
    s = str(x).strip()
    if not s:
        return None
    # IBKR expiry form 'YYYYMMDD' (first 8 digits)
    digits = s[:8]
    if len(digits) == 8 and digits.isdigit():
        try:
            return datetime.strptime(digits, "%Y%m%d").date()
        except ValueError:
            pass
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        return None


def earnings_ok(entry_date, expiry, earnings_date, cons):
    """Earnings-blackout gate for DEBIT structures (long options / debit verticals).

    A debit trade held THROUGH an earnings print is an IV-crush loser by construction: once
    the event passes, implied vol collapses and the long premium loses value even when the
    directional call was right. This gate blocks entering a debit whose holding horizon
    straddles a KNOWN next-earnings date.

    Block (return (False, reason)) when a known `earnings_date` falls inside the holding
    window: on/before `expiry`, extended by an optional `earnings_blackout_days` cushion
    beyond expiry (0 => block iff earnings <= expiry; a positive cushion also blocks earnings
    that land just after nominal expiry, covering rolls / hold-to-expiry). Earnings strictly
    BEFORE `entry_date` are in the past and never block.

    FAIL-OPEN on missing data: if `earnings_date` is None/unknown (or unparseable), return
    (True, "") -- an unknown earnings date must NOT hard-block a trade. The CALLER is
    responsible for surfacing an "earnings date unknown -- unchecked" flag so an unchecked
    trade is never presented as if it were verified 'clear' of earnings.

    Disabled (cons.earnings_blackout_enabled False) is a no-op: (True, "").

    Only DEBIT structures should be passed here (credit structures WANT the crush); callers
    apply it exactly where the other construction A-gates fire, and this account is
    long-debit-only, so every constructed order is a debit.
    """
    if not bool(getattr(cons, "earnings_blackout_enabled", True)):
        return True, ""
    if earnings_date is None:
        return True, ""  # FAIL-OPEN: unknown earnings -> pass, caller flags 'unchecked'
    ea = _as_date(earnings_date)
    xp = _as_date(expiry)
    en = _as_date(entry_date)
    if ea is None or xp is None:
        return True, ""  # can't judge -> fail open (never hard-block on bad data)
    if en is not None and ea < en:
        return True, ""  # earnings already past at entry -> can't be held through
    try:
        buf = int(getattr(cons, "earnings_blackout_days", 0) or 0)
    except (TypeError, ValueError):
        buf = 0
    cutoff = xp + timedelta(days=max(0, buf))
    if ea <= cutoff:
        window = f"on/before expiry {xp.isoformat()}" if buf == 0 else \
                 f"on/before expiry {xp.isoformat()} +{buf}d cushion ({cutoff.isoformat()})"
        return False, (f"earnings {ea.isoformat()} falls within the holding horizon "
                       f"({window}) -- debit held through earnings = IV-crush loser")
    return True, ""


# --------------------------------------------------------------- ex-div assignment risk (gate A6)

def assignment_risk_ok(short_strike, spot, right, expiry, ex_div_date, dte, cons):
    """Early-assignment / ex-dividend risk gate for the SHORT leg of a DEBIT VERTICAL.

    A debit vertical's SHORT leg can be assigned EARLY when it is ITM heading into an
    ex-dividend date: a counterparty exercises the ITM short to capture the dividend
    (classically a short CALL just before ex-div), you are assigned, forfeit the dividend, and
    the spread converts/closes early on an unfavorable leg. This gate SURFACES that risk.

    DISPOSITION (deliberate): early assignment is MANAGEABLE, not a guaranteed loss (the long
    leg still defines max risk; assignment merely closes the short early, often near max spread
    value). So the DEFAULT is WARN, not hard-block:
      * risk present, cons.assignment_block_hard FALSE (default) -> (True, reason)  [warn: the
        caller SURFACES `reason` as a :warning: note but still lets the trade through];
      * risk present, cons.assignment_block_hard TRUE            -> (False, reason) [hard block].
    A clean pass (no risk / not applicable / unknown data) is ALWAYS (True, "").

    Risk is present only when ALL hold:
      * there IS a short leg (short_strike > 0; a single long leg has nothing to be assigned);
      * the short leg is ITM: short CALL with spot >= short_strike, or short PUT with
        spot <= short_strike (right is the shared right of a debit vertical's two legs);
      * a KNOWN ex_div_date falls on/before `expiry`, extended by an optional
        cons.assignment_cushion_days cushion beyond expiry (0 => on/before expiry).

    FAIL-OPEN (return (True, "")) on: gate disabled; no short leg; missing/invalid spot or
    short_strike; unknown/unparseable ex_div_date or expiry. An unknown ex-div date must NEVER
    hard-block a trade -- like the earnings gate, the CALLER surfaces an "unchecked" flag when
    the ex-div date could not be determined. `ex_div_date` is computed by the caller as
    today + research.days_to_ex_dividend(...), so it is inherently future (never past entry).
    `dte` is accepted for signature parity with the sibling gates; the window is judged on the
    concrete expiry date. NEVER raises.
    """
    if not bool(getattr(cons, "assignment_check_enabled", True)):
        return True, ""
    try:
        k = float(short_strike or 0)
    except (TypeError, ValueError):
        return True, ""
    if k <= 0:
        return True, ""  # no short leg (single long option) -> nothing to be assigned
    try:
        s = float(spot or 0)
    except (TypeError, ValueError):
        return True, ""
    if s <= 0:
        return True, ""  # FAIL-OPEN: no usable spot -> can't judge ITM
    if ex_div_date is None:
        return True, ""  # FAIL-OPEN: unknown ex-div -> pass, caller flags 'unchecked'
    xd = _as_date(ex_div_date)
    xp = _as_date(expiry)
    if xd is None or xp is None:
        return True, ""  # can't judge -> fail open (never hard-block on bad data)
    itm = (right == "C" and s >= k) or (right == "P" and s <= k)
    if not itm:
        return True, ""  # OTM short leg -> not worth exercising for the dividend
    try:
        buf = int(getattr(cons, "assignment_cushion_days", 0) or 0)
    except (TypeError, ValueError):
        buf = 0
    cutoff = xp + timedelta(days=max(0, buf))
    if xd > cutoff:
        return True, ""  # ex-div lands after we are out of the position -> no assignment risk
    window = f"on/before expiry {xp.isoformat()}" if buf == 0 else \
             f"on/before expiry {xp.isoformat()} +{buf}d cushion ({cutoff.isoformat()})"
    reason = (f"early-assignment risk: ITM short {right} {k:g} (spot {s:g}) heading into "
              f"ex-dividend {xd.isoformat()} ({window}) -- the short leg may be exercised early "
              f"to capture the dividend, converting the spread")
    if bool(getattr(cons, "assignment_block_hard", False)):
        return False, reason
    return True, reason  # WARN (default): pass but SURFACE the risk (caller flags :warning:)
