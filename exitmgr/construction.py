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
from collections import namedtuple
from datetime import date as _date, datetime, timedelta, timezone
from pathlib import Path

from exitmgr.rules import days_to_expiry


# --------------------------------------------------------------- DTE floor (gate A1)
#
# STRUCTURE-AWARE DTE DOCTRINE (2026-07-26; CREDIT_PATH_SPEC.md S3, authority SOL_AUDIT_R3.md
# S4:117-125). ONE global `min_dte` cannot serve two opposite mechanisms:
#
#   * DEBIT / long premium -- the buyer PAYS theta, so short-dated is the documented failure
#     mode (median 17.5 DTE at entry bled 5.9-12.5%/day; -$1,521). Floor stays
#     `construction.min_dte` (25); `construction.prefer_dte_max` (800 since 2026-07-26) stays a
#     SOFT target clamp so a 365/497/638/795-DTE request reaches its expiry instead of
#     collapsing to ~170.
#   * CREDIT / cash-secured put -- the writer COLLECTS theta, so a weekly is the point, not the
#     bug. Own window `construction.credit_min_dte` (3) .. `construction.credit_max_dte` (45),
#     and that ceiling is HARD: a 60-DTE write is out of doctrine, not "close enough".
#
# THE INVARIANT THIS SECTION EXISTS TO ENFORCE: **the credit floor must be UNREACHABLE from
# the debit path.** `min_dte: 3` applied globally would license a 3-DTE LONG CALL -- maximum
# theta bleed, the exact structure the doctrine abolishes. So:
#   - `pick_expiry` is a dumb primitive: it takes explicit bounds and reads NO globals;
#   - `dte_bounds_for_side` is the ONLY place a side picks its numbers. It touches the
#     `credit_*` keys ONLY when side == "credit", and fails SAFE -- anything unrecognised,
#     missing or malformed resolves to the STRICT debit doctrine;
#   - `pick_expiry_for_side` is the sanctioned one-call entry point and deliberately has NO
#     floor/ceiling argument, so no caller and no argument combination can hand a debit idea
#     a shorter floor than `min_dte`.
# Never plumb `credit_min_dte` into a debit call site.

DEBIT_SIDE = "debit"
CREDIT_SIDE = "credit"

# Fallbacks used ONLY when no ConstructionConfig is supplied (or it predates these keys).
# They mirror config.yaml `construction:` as of 2026-07-26.
DEBIT_MIN_DTE_DEFAULT = 25
DEBIT_PREFER_DTE_MAX_DEFAULT = 800
CREDIT_MIN_DTE_DEFAULT = 3
CREDIT_MAX_DTE_DEFAULT = 45

DteBounds = namedtuple("DteBounds", "min_dte prefer_dte_max max_dte")


def _int_or(value, fallback):
    """int(value) or `fallback` for None/garbage. Never raises (a config typo must not crash
    entry construction, and must never silently widen a floor)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(fallback)


def normalize_side(side):
    """Map a trade's side to "credit" or "debit".

    ONLY the exact token "credit" (any case, surrounding whitespace ignored) is credit.
    EVERYTHING else -- None, "", "Debit", "csp", a typo, a non-string -- resolves to
    "debit". That asymmetry is deliberate and load-bearing: a mangled/absent side must fall
    into the STRICT long-premium doctrine, never into the 3-DTE credit window.
    """
    try:
        s = str(side or "").strip().lower()
    except Exception:  # pragma: no cover -- defensive: an exotic __str__ must not open the floor
        return DEBIT_SIDE
    return CREDIT_SIDE if s == CREDIT_SIDE else DEBIT_SIDE


def dte_bounds_for_side(side, cons=None):
    """Resolve the DTE bounds belonging to a trade's `side`.

    Returns DteBounds(min_dte, prefer_dte_max, max_dte):
      * DEBIT  -> (cons.min_dte [25], cons.prefer_dte_max [800], None)
                  max_dte is None = NO hard ceiling; prefer_dte_max stays the SOFT target clamp
                  it has been since 2026-07-01, so the long-dated clamp fix is untouched.
      * CREDIT -> (cons.credit_min_dte [3], cons.credit_max_dte [45], cons.credit_max_dte)
                  the ceiling is HARD -- a write outside the 3-45 window is refused, not
                  rounded to the nearest available expiry.

    `cons` is a ConstructionConfig (or any object with those attributes); None/missing
    attributes fall back to the module defaults above. Fails SAFE on garbage: a non-positive
    or unparseable debit floor reverts to 25 rather than becoming "anything goes", and an
    inverted credit window degenerates to a single-day window rather than an open ceiling.
    """
    if normalize_side(side) == CREDIT_SIDE:
        lo = max(1, _int_or(getattr(cons, "credit_min_dte", None), CREDIT_MIN_DTE_DEFAULT))
        hi = _int_or(getattr(cons, "credit_max_dte", None), CREDIT_MAX_DTE_DEFAULT)
        hi = max(lo, hi)  # inverted config -> degenerate window, never an open ceiling
        return DteBounds(lo, hi, hi)
    # DEBIT: the credit_* keys are not read on this path AT ALL. This is the enforcement point.
    lo = _int_or(getattr(cons, "min_dte", None), DEBIT_MIN_DTE_DEFAULT)
    if lo <= 0:
        lo = DEBIT_MIN_DTE_DEFAULT  # a 0/negative floor is a typo, not a licence for 3-DTE longs
    hi = max(lo, _int_or(getattr(cons, "prefer_dte_max", None), DEBIT_PREFER_DTE_MAX_DEFAULT))
    return DteBounds(lo, hi, None)


def pick_expiry(expirations, target_dte, min_dte=25, prefer_dte_max=45, today=None, max_dte=None):
    """Choose the entry expiry honoring an EXPLICIT DTE floor/ceiling (no globals read here).

    Nearest expiry to the idea's target_dte among expiries with DTE >= min_dte; a target
    below the floor (or above prefer_dte_max) is ADJUSTED into the band rather than
    rejected -- the trade idea is often sound, the model's expiry choice was the problem.
    Returns (expiry_str, dte, adjusted). (None, None, False) when NO expiry clears the
    floor (reject: no reasonable expiry exists).

    `prefer_dte_max` is a SOFT clamp on the TARGET: it pulls an over-long request down, but if
    every available expiry sits beyond it the nearest one is still returned (unchanged
    behaviour -- do not make this hard, that is what collapsed 365-DTE requests to ~170).
    `max_dte` (default None = no ceiling) is the HARD ceiling: expiries beyond it are removed
    from the candidate set entirely, so an out-of-window expiry can never be selected. The
    credit path passes credit_max_dte here; the debit path passes None.

    CALLERS SELECTING BY SIDE MUST USE :func:`pick_expiry_for_side`. This primitive obeys
    whatever bounds it is handed -- it is the side dispatcher, not this function, that
    guarantees a debit idea can never be given the credit floor.
    """
    t0 = today or datetime.now(timezone.utc).date()
    floor = _int_or(min_dte, DEBIT_MIN_DTE_DEFAULT)
    # A malformed ceiling collapses to 0, i.e. NO candidate qualifies -> the trade is refused.
    # Refusal is the fail-safe direction; silently dropping the ceiling is not.
    hard_max = None if max_dte is None else _int_or(max_dte, 0)
    cands = []
    for e in expirations or []:
        try:
            d = (datetime.strptime(str(e)[:8], "%Y%m%d").date() - t0).days
        except (ValueError, TypeError):
            continue
        if d < floor:
            continue
        if hard_max is not None and d > hard_max:
            continue  # HARD ceiling: out-of-window expiry is not a candidate at all
        cands.append((str(e), d))
    if not cands:
        return None, None, False
    try:
        tgt = int(target_dte or 0)
    except (TypeError, ValueError):
        tgt = 0
    soft_ceiling = max(_int_or(prefer_dte_max, floor), floor)
    if hard_max is not None:
        soft_ceiling = min(soft_ceiling, max(hard_max, floor))
    eff_target = min(max(tgt, floor), soft_ceiling)
    exp, dte = min(cands, key=lambda x: abs(x[1] - eff_target))
    return exp, dte, (eff_target != tgt)


def pick_expiry_for_side(expirations, target_dte, side=None, cons=None, today=None):
    """Choose an entry expiry under the doctrine that belongs to `side`. SANCTIONED ENTRY POINT.

    side="credit" (a cash-secured put) gets the 3-45 DTE write window; ANYTHING ELSE -- debit,
    absent, misspelled, None -- gets the strict long-premium floor (min_dte, 25). There is
    deliberately NO floor/ceiling parameter on this function: the bounds come from
    :func:`dte_bounds_for_side`, so there is no argument combination by which a debit idea can
    reach a sub-min_dte expiry. Returns (expiry_str, dte, adjusted), same contract as
    :func:`pick_expiry`.
    """
    b = dte_bounds_for_side(side, cons)
    return pick_expiry(expirations, target_dte, min_dte=b.min_dte,
                       prefer_dte_max=b.prefer_dte_max, today=today, max_dte=b.max_dte)


# ------------------------------------------------- take-profit / stop (gate A2)
#
# DOCTRINE-ONLY TAKE-PROFIT (2026-07-26, Sol audit R5 section R2; ruling
# dataset-v8-build-20260725/RULING_TAKE_PROFIT.md).
#
#   Trevor: "+20% is doctrine, not a clamp -- a potential exit when the thesis has changed AND
#   value has been given back, never a mechanical target."
#
# THIS MODULE NO LONGER MANUFACTURES A TAKE-PROFIT. Three mechanical paths were removed together
# (Sol is explicit that killing the tier table alone was insufficient):
#   1. caps.tp_tiers -> tp_tier_for_pot -> clamp_tp_sl, which at the live net-liq (~$1,893)
#      resolved the floor tier and stamped tp_pct 0.20 onto EVERY order;
#   2. the global rules.profit_target_pct fallback (was 30.0, now null in config.yaml);
#   3. the mechanical +20% rules.scale_out partial exit (now disabled in config.yaml).
#
# +20% is now PROMPT EVIDENCE for the model, not a consumer threshold. A green, thesis-intact
# trade is a hold/trail; a broken thesis at a loss is a cut (governed by the UNCHANGED -30%
# stop); a changed thesis plus a real giveback from a positive peak may take profit -- and that
# is a model-selected action with its own contract, never arithmetic installed at entry.
#
# The ONLY numeric automatic take-profit that may still exist is an EXPLICITLY supplied, distant
# catastrophe backstop in the band below. It is validated, never manufactured.
TP_BACKSTOP_MIN_PCT = 100.0
TP_BACKSTOP_MAX_PCT = 500.0

# JOURNAL MIGRATION / VERSION FLAG (R2 requirement 4). Journal entries written BEFORE this change
# carry a mechanical tier/global target (the live book held 15/20/40/60/75/100). Those numbers must
# never silently reactivate when the manager reloads an open position after a restart, so a
# journalled take-profit is honoured ONLY when the entry is stamped with the current policy AND
# the value is an explicit distant backstop. Anything else reads as "no target".
# See journal_take_profit_pct().
TP_POLICY_CURRENT = "explicit-only-v2"


def tp_tier_for_pot(net_liq, tiers, fallback_max, fallback_default):
    """SUPERSEDED / INERT (2026-07-26, Sol audit R5 R2). Kept only so the historical entry wiring
    keeps importing and so its arithmetic stays under test; it can no longer install a target.

    Two independent things now defang it:
      * `caps.tp_tiers` has been REMOVED from config.yaml, so every production caller passes an
        empty list and receives the flat fallback;
      * `clamp_tp_sl` / `optional_take_profit_pct` no longer read `cons.tp_pct` or
        `cons.tp_max_pct` at all, so even a re-added tier table cannot synthesize a take-profit.
    DO NOT re-wire this into a take-profit. Account size is not an exit signal.

    Historical contract, unchanged: returns (tp_max_pct, tp_pct) in FRACTION units (0.35 == 35%).
    Picks the HIGHEST tier row whose min_pot <= net_liq; below the lowest tier the lowest row is
    used. Scales ONLY the take-profit side -- it never touched, and still never touches, sl_pct.

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


def clamp_stop_pct(sl_pct, cons):
    """Clamp the PROTECTIVE STOP. PERCENT units in/out (30.0 == 30%), positive magnitude.

    THIS IS THE AIRTIGHT LIMB AND IT IS UNCHANGED. The body below is a byte-for-byte lift of the
    stop half of the pre-R2 `clamp_tp_sl`; R2 removed the take-profit half and nothing else.
    `tests/test_no_mechanical_tp.py::TestThirtyPercentStopIsByteForByteUnchanged` pins both the
    source bytes and the behaviour against a frozen reference over an exhaustive input grid.

    SL: missing/0 -> default |cons.sl_pct|; a model stop may be TIGHTER (smaller) than the
    default but never looser (tightened from the old -50%). A garbage/hair-trigger value floors
    at 5%. Never raises.
    """
    sl_def = abs(float(cons.sl_pct)) * 100.0
    try:
        sl_in = float(sl_pct or 0.0)
    except (TypeError, ValueError):
        sl_in = 0.0
    sl = sl_def if sl_in <= 0 else min(sl_def, sl_in)
    sl = max(5.0, sl)  # never a hair-trigger stop from a garbage model value
    return round(sl, 1)


def optional_take_profit_pct(tp_pct):
    """Validate an OPTIONAL, EXPLICIT take-profit. Returns (tp_or_None, refusal_note).

    R2 requirement 3: "keep the airtight stop clamp unchanged, but do not synthesize a TP. If an
    explicit distant catastrophe backstop remains permitted, validate only an explicitly supplied
    100-500% value; never manufacture one."

    Therefore:
      * None / absent / 0 / "" / False        -> (None, "")           no target, nothing to log
      * a value inside [100, 500]             -> (value, "")          explicit distant backstop
      * ANY other number (incl. 20, 30, 75)   -> (None, <reason>)     REFUSED, and the caller
        must log the reason -- a rejected take-profit is logged explicitly, never silently
        rewritten into something else.
      * garbage / NaN                         -> (None, <reason>)

    Note what is deliberately absent: `cons` is not a parameter. There is no configuration, pot
    size, tier row or default anywhere in this function that could conjure a number. The only way
    out with a value is to have supplied one.
    """
    if tp_pct is None:
        return None, ""
    try:
        v = float(tp_pct)
    except (TypeError, ValueError):
        return None, f"take_profit refused: non-numeric value {tp_pct!r} (no target installed)"
    if v != v:  # NaN
        return None, "take_profit refused: NaN (no target installed)"
    if v <= 0:
        return None, ""
    if v < TP_BACKSTOP_MIN_PCT or v > TP_BACKSTOP_MAX_PCT:
        return None, (
            f"take_profit +{v:g}% NOT installed -- doctrine-only take-profit (Sol audit R5 R2 / "
            f"RULING_TAKE_PROFIT.md). A percentage is not an exit signal; a take-profit needs a "
            f"changed thesis AND a giveback from peak. Only an explicit "
            f"{TP_BACKSTOP_MIN_PCT:g}-{TP_BACKSTOP_MAX_PCT:g}% catastrophe backstop may be a "
            f"numeric automatic exit.")
    return round(v, 1), ""


def journal_take_profit_pct(journal_entry):
    """Read the take-profit of an OPEN-position journal entry, through the R2 migration gate.

    R2 requirement 4: "Existing open-journal tier values need an audited migration/version flag so
    they cannot silently reactivate after restart."

    Journal lines written before R2 carry a MECHANICAL target -- the pot-tier stamp at entry, or
    the old global 30% fallback (the live book held 15 / 20 / 40 / 60 / 75 / 100). Those lines are
    unstamped, so this returns None for every one of them: on a restart they reload with NO
    automatic profit target, exactly as if they had been opened today. No journal data is
    rewritten; the migration is read-side and therefore cannot corrupt the audit record.

    A value survives ONLY if BOTH hold:
      * the entry is stamped `tp_policy == TP_POLICY_CURRENT` (written by the post-R2 entry path);
      * the value passes `optional_take_profit_pct` (an explicit 100-500% distant backstop).

    Returns Optional[float] in PERCENT units.
    """
    je = journal_entry or {}
    try:
        policy = je.get("tp_policy")
    except AttributeError:
        return None
    if policy != TP_POLICY_CURRENT:
        return None
    tp, _note = optional_take_profit_pct(je.get("profit_target_pct"))
    return tp


def clamp_tp_sl(tp_pct, sl_pct, cons):
    """Resolve the sell levels for an entry. Returns (Optional[take_profit_pct], stop_pct).

    SPLIT under Sol audit R5 R2: this is now a thin composition of the two independent limbs.
      * the STOP goes through `clamp_stop_pct` -- unchanged, always a number, airtight;
      * the TAKE-PROFIT goes through `optional_take_profit_pct` -- and is **None** unless the
        caller explicitly supplied a distant 100-500% catastrophe backstop.

    The first element is therefore OPTIONAL and callers must handle None. Nothing here reads
    `cons.tp_pct` / `cons.tp_min_pct` / `cons.tp_max_pct` any more, so neither a pot tier nor a
    construction default can put a profit target on an order. PERCENT units in/out.

    (The superseded "accepted inverted reward:risk on small pots" note that used to live here is
    gone with the mechanism it defended: there is no longer a manufactured small-pot TP to sit
    below the stop. See config.yaml and RULING_TAKE_PROFIT.md.)
    """
    tp, _note = optional_take_profit_pct(tp_pct)
    return tp, clamp_stop_pct(sl_pct, cons)


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


def open_book_items(positions, journal_path, open_orders=None):
    """Stable-keyed deployed-premium/decay book for filled positions and working BUYs.

    positions: {con_id: PositionData} from IBConnection.get_positions() (long legs only;
    spread short legs are already excluded there). The journaled NET debit (max loss) is
    preferred, keyed by the long leg's contract_id, newest entry wins -- same convention as
    trader._load_journal_debits. ``open_orders`` is reqAllOpenOrdersAsync() output; active BUYs
    are keyed by immutable decision/order identity so a partial fill cannot be double-counted."""
    rows = {}
    rows_by_ref = {}
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
                row = (float(d), e.get("expiry"), e.get("decision_id"), int(cid))
                rows[int(cid)] = row
                if e.get("order_ref"):
                    rows_by_ref[str(e["order_ref"])] = row
            except (TypeError, ValueError):
                continue
    items = {}
    for cid, pos in (positions or {}).items():
        r = rows.get(int(cid))
        if r is not None:
            debit, exp, decision_id, _ = r
            dte = days_to_expiry(exp)
            key = f"decision:{decision_id}" if decision_id else f"position:{int(cid)}"
        else:
            debit = abs(getattr(pos, "avg_cost", 0.0)) * 100 * abs(getattr(pos, "quantity", 0))
            dte = days_to_expiry(getattr(pos, "expiry", ""))
            key = f"position:{int(cid)}"
        items[key] = (float(debit), dte if (dte and dte > 0) else 0)

    terminal = {"Cancelled", "ApiCancelled", "Inactive", "Filled"}
    for trade in open_orders or []:
        order = getattr(trade, "order", None)
        contract = getattr(trade, "contract", None)
        status = getattr(getattr(trade, "orderStatus", None), "status", None)
        if order is None or contract is None or str(getattr(order, "action", "")).upper() != "BUY":
            continue
        if status in terminal:
            continue
        ref = str(getattr(order, "orderRef", "") or "")
        row = rows_by_ref.get(ref)
        if row is not None:
            debit, exp, decision_id, long_cid = row
            key = f"decision:{decision_id}" if decision_id else f"position:{long_cid}"
            dte = days_to_expiry(exp)
        else:
            price = getattr(order, "lmtPrice", 0) or 0
            remaining = getattr(getattr(trade, "orderStatus", None), "remaining", None)
            qty = remaining if isinstance(remaining, (int, float)) and remaining > 0 \
                else (getattr(order, "totalQuantity", 0) or 0)
            try:
                debit = float(price) * 100 * float(qty)
            except (TypeError, ValueError):
                debit = 0.0
            dte = days_to_expiry(getattr(contract, "lastTradeDateOrContractMonth", ""))
            oid = getattr(order, "permId", None) or getattr(order, "orderId", None) or ref
            key = f"order:{oid}"
        items[key] = (float(debit), dte if (dte and dte > 0) else 0)
    return items


def open_book(positions, journal_path, open_orders=None):
    """Backward-compatible list view of :func:`open_book_items`."""
    return list(open_book_items(positions, journal_path, open_orders).values())


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


def earnings_ok(entry_date, expiry, earnings_date, cons, hold_days=None):
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
    window = f"on/before expiry {xp.isoformat()}" if buf == 0 else \
             f"on/before expiry {xp.isoformat()} +{buf}d cushion ({cutoff.isoformat()})"

    # HOLD-WINDOW MODE (2026-08-16). Under the long-dated doctrine expiry is ~8x the hold, so
    # comparing earnings to EXPIRY rejects almost every name. What the gate protects against is
    # being OPEN through the crush, so judge the window the position is actually open for.
    # hold * slip_mult, because the trade that is not working is the one that gets held. Falls
    # back to the strict expiry cutoff whenever the hold is missing or unusable.
    if bool(getattr(cons, "earnings_use_hold_window", False)) and en is not None:
        try:
            _hold = int(hold_days) if hold_days is not None else None
        except (TypeError, ValueError):
            _hold = None
        if _hold and _hold > 0:
            try:
                _mult = float(getattr(cons, "earnings_hold_slip_mult", 2.0) or 2.0)
            except (TypeError, ValueError):
                _mult = 2.0
            _mult = max(1.0, _mult)
            _hold_cut = en + timedelta(days=int(round(_hold * _mult)) + max(0, buf))
            if _hold_cut < cutoff:          # shrink the window: earnings after the HOLD ends
                                            # are irrelevant, so a nearer cutoff = more permissive
                cutoff = _hold_cut
                window = (f"within {_hold}d intended hold x{_mult:g} slip "
                          f"(through {cutoff.isoformat()}; expiry {xp.isoformat()})")

    if ea <= cutoff:
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


# --------------------------------------------------------------------------- #
# Deterministic construction (2026-08-16). See DETERMINISTIC_CONSTRUCTION note.
# --------------------------------------------------------------------------- #
DOCTRINE_HOLD_MULTIPLE = 8       # measured winner: ~8x the intended hold
DOCTRINE_LONG_DELTA = 0.60       # doctrine_real's long-leg delta
DOCTRINE_MAX_DTE = 800           # the schema's ceiling; nothing caps below this
DOCTRINE_MIN_HOLD = 5            # a hold under this is almost always a typo or a scalp


def deterministic_expiry(intended_hold_days, min_dte, max_dte=DOCTRINE_MAX_DTE):
    """target_dte from the hold, not from the model.

    Returns None when the hold is unusable, so the caller can leave the model's own number
    alone rather than inventing one from a bad input.
    """
    try:
        hold = int(intended_hold_days)
    except (TypeError, ValueError):
        return None
    if hold < DOCTRINE_MIN_HOLD:
        return None
    dte = hold * DOCTRINE_HOLD_MULTIPLE
    return int(max(int(min_dte), min(int(max_dte), dte)))


def apply_deterministic_construction(idea, min_dte, enabled=True):
    """Overwrite expiry/delta on a parsed idea, preserving what the model chose.

    Mutates and returns the idea. A credit-side idea is returned untouched: the CSP path has
    its own 3-45 DTE write window where theta works FOR the writer, and the debit doctrine
    must never be plumbed into it.
    """
    if not enabled or idea is None:
        return idea
    if str(getattr(idea, "side", "debit") or "debit").lower() == "credit":
        return idea

    dte = deterministic_expiry(getattr(idea, "intended_hold_days", None), min_dte)
    if dte is None:
        return idea

    # keep the model's numbers for the audit trail and future A/Bs
    if not hasattr(idea, "model_target_dte") or idea.model_target_dte is None:
        try:
            object.__setattr__(idea, "model_target_dte", getattr(idea, "target_dte", None))
            object.__setattr__(idea, "model_target_delta", getattr(idea, "target_delta", None))
        except Exception:
            pass
    try:
        object.__setattr__(idea, "target_dte", dte)
        object.__setattr__(idea, "target_delta", DOCTRINE_LONG_DELTA)
    except Exception:
        return idea
    return idea
