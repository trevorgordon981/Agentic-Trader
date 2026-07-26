#!/usr/bin/env python3
"""options-strategist skill -- canonical, prompt-independent trade proposer.

The output contract lives HERE, not in whatever prompt is fed to Alfred:
  * a locked system prompt,
  * hard validation, and
  * normalization that repairs common model deviations (direction synonyms, per-share vs
    total-dollar debit) into one canonical schema.

So invoking the skill always yields the same structured shape regardless of phrasing.
READ-ONLY: this proposes ideas only. It never touches IBKR -- execution is the gated
approval+exit system's job.

Usage:
  echo "<market context>" | python strategist.py --endpoint URL --model NAME
  python strategist.py --context "SPY +0.4% ..." --endpoint URL --model NAME
Output: JSON {"trades": [<canonical idea>, ...]} to stdout.
"""
import argparse
import json
import math
import os
import re
import sys
import urllib.request
import urllib.error
import time
from dataclasses import dataclass, asdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import List, Optional

from exitmgr.risk import INDEX_UNDERLYINGS
from exitmgr import provenance

# 1-10 conviction rubric (the operator 2026-06-12): 1-3 desperate-only, 4 below-avg, 5 middle,
# 6-8 medium confidence, 8-10 high confidence. Score HONESTLY -- a low score is fine and useful.
_SCORING = (
    "Score each idea 1-10 on its ABSOLUTE conviction -- this is NOT a rank-ordering of your picks, "
    "and you must NOT default to a 6/5/4 spread. Use the FULL range every day: 8-10 = HIGH "
    "(genuinely strong -- clear catalyst, favorable structure, good risk/reward, you would size up; "
    "use it whenever warranted and do NOT cap a strong idea at 6); 6-7 = MEDIUM (solid but with real "
    "caveats); 4-5 = MARGINAL (take-it-or-leave-it); 1-3 = WEAK (only if desperate -- and prefer an "
    "EMPTY slate over forcing weak ideas). Be honest BOTH ways: do not inflate a mediocre idea, and "
    "do not suppress a strong one. If two ideas are both genuinely strong, score BOTH 8+ -- no need "
    "to spread them apart."
)
_UNIVERSE = (
    "Universe: SPY, QQQ, IWM, and liquid large-cap single names only. DO NOT propose biotech / "
    "pharma names or Elon-Musk-linked companies (e.g. TSLA) -- they are rejected. SPCX is the "
    "ONE permitted Elon-derivative name (allowed). "
    "STRUCTURES: use ONLY long calls, long puts, or DEBIT spreads ('call debit spread' / "
    "'put debit spread') -- you PAY a debit and that debit is your max loss. Do NOT propose credit "
    "spreads, cash-secured puts, iron condors, or any short-premium / margin structure: the ~$1,000 "
    "account can't post the collateral and the system only manages long-debit positions. Prefer "
    "DEBIT SPREADS (cheaper, defined risk). For spreads, est_debit_usd is the NET debit. Keep "
    "est_debit AFFORDABLE for a ~$1,000 account."
)
_CONTRACT = (
    "OUTPUT CONTRACT -- respond with ONLY this JSON object, no markdown, no prose:\n"
    '{"trades": [{'
    '"underlying": "<TICKER>", '
    '"is_index": <true|false>, '
    '"direction": "bullish" | "bearish", '
    '"structure": "<e.g. long call, call debit spread>", '
    '"target_dte": <int days>, '
    '"intended_hold_days": <positive int CALENDAR days>, '
    '"target_delta": <0.0-1.0>, '
    '"est_debit_usd": <TOTAL dollars = premium_per_share * 100 * contracts, e.g. 180 not 1.80>, '
    '"conviction": <1-10>, '
    '"profit_target_pct": <SELL to take profit at +this% of premium, e.g. 75>, '
    '"stop_pct": <SELL to cut the loss at -this% of premium, e.g. 40>, '
    '"thesis": "<1-2 sentences>"}]}\n'
    "ALWAYS give profit_target_pct and stop_pct -- the levels you would sell at. "
    "Make exits ASYMMETRIC -- cut losers fast, let winners run: set profit_target_pct meaningfully "
    "WIDER than stop_pct (roughly 1.5-2x the stop), UNLESS it is a short-dated catalyst you would "
    "take profit on quickly. Mind theta -- do not hold a thesis-broken option hoping; the stop "
    "protects the account."
)

_REGIME = (
    "ENTRY DISCIPLINE: before any directional idea, confirm the underlying's trend and the broad "
    "tape (SPY/QQQ) agree with it. Trade WITH the trend -- a bullish call/spread needs the "
    "underlying confirming higher, not just 'oversold' or 'due to bounce.' Do not fade strong "
    "momentum or buy falling knives. If the tape is choppy or against you, prefer an empty slate "
    "or a defined-risk debit spread over a naked long."
)

# Conservative mode (the 15-min loop): silence is allowed.
SYSTEM_PROMPT = (
    "You are a disciplined options swing-trading strategist for a SMALL account. Propose 0-3 trades "
    "you have genuine conviction on, or an empty list if nothing is compelling -- never force trades. "
    + _UNIVERSE + "\n" + _REGIME + "\n" + _SCORING + "\n" + _CONTRACT
)

# Recommend mode (the daily slate): ALWAYS surface your best ideas, scored honestly.
RECOMMEND_PROMPT = (
    "You are an options swing-trading strategist for a SMALL (~$1,000) account. Recommend your "
    "BEST 1-3 option trade ideas for today. ALWAYS give at least one idea unless the market is "
    "genuinely untradeable -- it is fine to include moderate or weak ideas, just score them "
    "honestly so the human can judge. " + _UNIVERSE + "\n" + _REGIME + "\n" + _SCORING + "\n" + _CONTRACT
)

_DIRECTION = {
    "bullish": "bullish", "bull": "bullish", "long": "bullish", "up": "bullish",
    "call": "bullish", "calls": "bullish", "buy": "bullish", "up trend": "bullish", "uptrend": "bullish",
    "bearish": "bearish", "bear": "bearish", "short": "bearish", "down": "bearish",
    "put": "bearish", "puts": "bearish", "sell": "bearish", "downtrend": "bearish",
}


@dataclass
class TradeIdea:
    underlying: str
    is_index: bool
    direction: str
    structure: str
    target_dte: int
    target_delta: float
    est_debit_usd: float
    conviction: int
    thesis: str
    profit_target_pct: float = 0.0   # SELL to take profit at +this% of premium (0 = use default)
    stop_pct: float = 0.0            # SELL to cut loss at -this% of premium (0 = use default)
    intended_hold_days: Optional[int] = None  # source-bound calendar underwriting window
    # -- CREDIT LIMB (2026-07-26, CREDIT_PATH_SPEC.md S1). Absent/"debit" == the historical
    # contract. The only permitted short structure is a cash-secured put; every other short
    # (naked call, strangle, condor) is UNREPRESENTABLE ON EITHER SIDE -- the allow-list in
    # _require_allowed_structure() is enforced on the debit path too (audit R4 / S2 hole 1).
    side: str = "debit"              # "debit" | "credit"
    collateral_usd: float = 0.0      # credit only: strike * 100 * contracts
    net_credit_usd: float = 0.0      # credit only: TOTAL dollars received (never normalize_debit'd)
    max_loss_usd: float = 0.0        # credit only: collateral_usd - net_credit_usd
    strike: float = 0.0              # credit only: a CSP is defined by its strike


def normalize_direction(raw_dir: str, structure: str) -> Optional[str]:
    """GENERIC direction mapping -- UNCHANGED. An ordinary long put is bearish and stays
    bearish. Cash-secured puts do NOT come through here: see normalize_csp_direction()."""
    d = _DIRECTION.get(str(raw_dir).lower().strip())
    if d:
        return d
    s = str(structure).lower()
    if "put" in s:
        return "bearish"
    if "call" in s:
        return "bullish"
    return None


def _clamp_pct(value, lo: float, hi: float) -> float:
    """Sell-level % from the model: clamp into [lo, hi]; 0.0 means 'use the default rule'.
    A boolean is not a percentage (S2 hole 4): `"stop_pct": true` used to become float(True)
    == 1.0 and then clamp UP to the 10% floor, inventing a sell level. It now falls back to
    0.0 == "use the default rule", which is the same outcome as omitting the field."""
    if isinstance(value, bool):
        return 0.0
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    if v <= 0:
        return 0.0
    return max(lo, min(hi, v))


def normalize_debit(value: float) -> float:
    """Repair the common per-share-vs-total mix-up. A 'debit' under ~$25 is almost certainly a
    per-share premium for a real option, so scale it to a total-dollar figure (x100/contract)."""
    if 0 < value < 25.0:
        return round(value * 100.0, 2)
    return value


# The ONLY short structure this system may ever express. Invariant 1 ("never sell a naked
# call") is enforced by making an unbounded-loss structure unparseable, not merely discouraged.
CSP_STRUCTURE = "cash secured put"
_SIDES = ("debit", "credit")

# ============================================================================================
# STRUCTURE ALLOW-LIST  (audit R4 / S2 hole 1, reproduced live 2026-07-26 on strategist.py
# d54b571c4543da5d7930cc5a0cf90b4c7e420de8e01d7c93a31654018be52237)
#
# THE DEFECT: the CSP structure gate lived only inside the credit branch, so
#   {"structure": "naked call", "est_debit_usd": 500}          (side absent or "debit")
#   {"structure": "short strangle", "est_debit_usd": 500}      (side "debit")
# both PARSED. An unbounded-loss structure was expressible on the path that has no structure
# gate at all -- the single most dangerous defect in this file.
#
# THE FIX: an ALLOW-LIST enforced on EVERY path, debit and credit alike, through the one
# choke point `_require_allowed_structure()`. Allow-list, not deny-list: a deny-list can only
# ban the shorts somebody remembered to name, and the space of ways to write "sell a naked
# call" is unbounded. An unrecognised structure is REFUSED (fail closed).
#
# WHY THESE ENTRIES: every one is (a) a bounded-loss LONG-DEBIT structure whose maximum loss
# is the debit paid, and (b) routed correctly by the executors, which branch on the substring
# "spread" (exitmgr/trader.py:1926, daily_recommend.py:402): a name containing "spread" builds
# a two-leg vertical via pick_spread_short(); anything else builds a single long leg whose
# right (C/P) comes from `direction`. Nothing here can produce a short naked leg.
#
# NOT normalisation: membership is tested on the whitespace/case-canonical form, but the
# ORIGINAL string is what gets stored on the TradeIdea, so no downstream consumer sees a
# rewritten structure and the debit path stays byte-identical for everything it still accepts.
DEBIT_STRUCTURES = frozenset({
    # single long leg -- max loss = the debit paid
    "long call",
    "long put",
    "long option",              # trader.py:1658's own canonical single-leg string
    # vertical DEBIT spreads -- max loss = the net debit paid
    "call debit spread",        # daily_recommend.py:1066 canonical
    "put debit spread",         # daily_recommend.py:1066 canonical
    "debit call spread",
    "debit put spread",
    "bull call spread",         # the standard trade name for a call debit spread
    "bear put spread",          # the standard trade name for a put debit spread
    "long call spread",
    "long put spread",
    "debit spread",             # trader.py:1658's own canonical spread string
})

# Deliberately ABSENT and therefore refused on every path: naked/short call, short put,
# strangle, straddle, iron condor, ratio spread, "credit spread", "bull put spread",
# "bear call spread", covered call, and every undefined placeholder ("x", "", "options
# trade"). A short-premium name arriving on the DEBIT path is the worst case of all: the
# executor would have silently BOUGHT the leg the model meant to SELL.


def _require_allowed_structure(side: str, raw_structure) -> str:
    """THE structure choke point. Runs on EVERY parsed idea, debit and credit alike.

    Returns the canonical form (for the credit path, which stores the canonical CSP string).
    Raises ValueError -- caught by parse_ideas' except-clause -- for anything not on the
    allow-list for that side."""
    canon = _canonical_structure(raw_structure)
    allowed = frozenset({CSP_STRUCTURE}) if side == "credit" else DEBIT_STRUCTURES
    if canon not in allowed:
        raise ValueError(
            "structure %r is not permitted on side=%r. Permitted: %s. An unrecognised or "
            "short-premium structure is REFUSED, never guessed -- a naked short has unbounded "
            "loss and this account may not carry one." % (raw_structure, side,
                                                          ", ".join(sorted(allowed))))
    return canon


# --------------------------------------------------------------------------------------------
# CENT-SAFE MONEY  (audit R4 / S2 hole 4)
#
# THE DEFECT: money was compared in binary floats. `abs(11815.01 - (12000.0 - 185.0))` is
# 0.010000000000218279, so the nominal EXACT "$0.01" boundary the spec promises to accept was
# REJECTED, while $0.009 passed. And JSON booleans are Python ints, so `float(True) == 1.0`
# silently fabricated a value for EVERY numeric field: `"est_debit_usd": true` became a $100
# debit, `"strike": true` a $1 strike, `"collateral_usd": true` a $1 collateral.
#
# THE FIX: booleans are rejected BEFORE any numeric coercion, and every money comparison is
# done on exact integer CENTS with a DECLARED INCLUSIVE boundary (see _MAX_LOSS_TOL_CENTS).
# --------------------------------------------------------------------------------------------

_CENT = Decimal("0.01")

# The spec says max_loss_usd must equal collateral - credit "within $0.01". DECLARED BOUNDARY:
# INCLUSIVE -- a difference of exactly one cent is ACCEPTED, two cents is refused. In integer
# cents that boundary is exactly representable, which is the whole point.
_MAX_LOSS_TOL_CENTS = 1

# Same inclusive one-cent slop for the collateral/strike multiple, to absorb a model rounding
# its own arithmetic, and nothing more.
_COLLATERAL_TOL_CENTS = 1


def _reject_bool(value, key: str):
    """`True`/`False` are ints in Python: float(True) == 1.0. Every numeric field must refuse a
    boolean BEFORE coercion or the parser invents a number the model never stated."""
    if isinstance(value, bool):
        raise ValueError("%s must be a number, not the boolean %r" % (key, value))
    return value


def _cents(value, key: str) -> int:
    """Exact integer cents. Rejects booleans, non-numerics, NaN and +/-Inf.

    Decimal(str(v)) reads the DECIMAL the model wrote rather than its binary approximation, so
    $0.01 is exactly one cent here and stays exact through every comparison."""
    _reject_bool(value, key)
    try:
        d = Decimal(str(value))
        if not d.is_finite():
            raise ValueError("%s must be finite, got %r" % (key, value))
        return int(d.quantize(_CENT, rounding=ROUND_HALF_UP) * 100)
    except (InvalidOperation, ArithmeticError, TypeError) as exc:
        raise ValueError("%s is not a usable decimal amount: %r (%s)" % (key, value, exc))


def normalize_csp_direction(raw) -> str:
    """Audit R4 / S4 ruling: A CASH-SECURED PUT'S DIRECTION IS `bullish`.

    A short put has POSITIVE delta -- you profit if the underlying holds or rises, and the
    worst case is being assigned long stock at the strike. That is a bullish/neutral thesis;
    the accepted enum has no neutral value, so it is `bullish`.

    STRICTLY CREDIT-SCOPED. normalize_direction()'s generic `"put" -> bearish` mapping is
    UNTOUCHED: an ordinary LONG put is still bearish. This function is only ever reached after
    _parse_credit_fields() has already proved the idea is a fully specified cash-secured put.

      * direction omitted / blank / unrecognised + valid CSP -> infer `bullish`
      * explicit `bullish` (or a bullish synonym)  + valid CSP -> accept
      * explicit `bearish` (or a bearish synonym, incl. the literal "put") + CSP -> REJECT

    The bearish case FAILS CLOSED rather than being silently corrected: a model that asks to
    sell a put while calling it bearish does not understand the position it is proposing, and
    the rest of its numbers cannot be trusted either."""
    d = _DIRECTION.get(str(raw if raw is not None else "").strip().lower())
    if d is None:
        return "bullish"
    if d != "bullish":
        raise ValueError(
            "direction %r is inconsistent with a cash-secured put: a short put has POSITIVE "
            "delta and expresses a bullish/neutral thesis. Failing closed rather than "
            "correcting the model." % (raw,))
    return "bullish"


def normalize_side(raw) -> Optional[str]:
    """Absent/blank -> "debit" (the historical contract). "credit" only when explicitly asked
    for. Anything else -> None, which drops the idea: an unrecognised side must never silently
    fall through to a live order path."""
    s = str(raw if raw not in (None, "") else "debit").strip().lower()
    return s if s in _SIDES else None


def _canonical_structure(raw) -> str:
    return re.sub(r"\s+", " ", str(raw if raw is not None else "").strip().lower())


def _require_positive(t: dict, key: str) -> float:
    """Required, finite and > 0. Raises so parse_ideas' existing except-clause drops the idea.
    Booleans are refused BEFORE coercion (S2 hole 4) -- float(True) == 1.0 would otherwise pass
    this gate as a legitimate $1.00 / 1-point value."""
    _reject_bool(t[key], key)    # KeyError propagates == dropped
    v = float(t[key])            # TypeError / ValueError propagate == dropped
    if not math.isfinite(v) or v <= 0:
        raise ValueError("%s must be a finite positive number, got %r" % (key, t[key]))
    return v


def _implied_contracts(strike_cents: int, collateral_cents: int) -> int:
    """S2 hole 2. A cash-secured put's collateral IS strike x 100 x contracts -- that is what
    makes it "cash secured" at all. The old parser checked only that collateral was positive,
    so `strike=120, collateral_usd=1.00, net_credit_usd=0.50, max_loss_usd=0.50` was ACCEPTED:
    a $12,000 obligation declared as a $1 one, with the max_loss arithmetic self-consistently
    agreeing because it was computed from the same fictional collateral.

    THE STRONGEST CHECK THIS SCHEMA SUPPORTS: TradeIdea has no `contracts` field, so the parser
    cannot verify a specific contract count. It CAN verify that the declared collateral is a
    POSITIVE INTEGER MULTIPLE of one contract's collateral (strike x 100), which rejects every
    impossibility -- a fraction of a contract, a rounded-off collateral, an unrelated number --
    and implies collateral >= strike x 100 for at least one contract.

    RESIDUAL, MUST BE RE-VERIFIED AT SUBMIT TIME: this proves collateral == strike x 100 x N
    for SOME integer N >= 1; it cannot prove N equals the quantity actually transmitted. Before
    any order is placed, the submit path must re-check
        collateral_usd == strike * 100 * order.totalQuantity
    against the real order object, and against available cash. See trader.credit_structure_ok()
    for the second (post-parse) enforcement point."""
    unit_cents = strike_cents * 100          # one contract = 100 shares at the strike
    if unit_cents <= 0:
        raise ValueError("strike must be positive to bound collateral")
    whole, rem = divmod(collateral_cents, unit_cents)
    if rem > _COLLATERAL_TOL_CENTS and (unit_cents - rem) > _COLLATERAL_TOL_CENTS:
        raise ValueError(
            "collateral_usd $%s is not a whole multiple of one contract's collateral "
            "(strike x 100 = $%s) -- a cash-secured put must post the full assignment cost"
            % (collateral_cents / 100.0, unit_cents / 100.0))
    contracts = whole + (1 if rem > _COLLATERAL_TOL_CENTS else 0)
    if contracts < 1:
        raise ValueError(
            "collateral_usd $%s covers less than one contract (strike x 100 = $%s)"
            % (collateral_cents / 100.0, unit_cents / 100.0))
    return contracts


def _parse_credit_fields(t: dict) -> dict:
    """Validate the credit limb of a raw idea. Raises ValueError for anything that is not a
    fully specified, collateral-backed cash-secured put.

    NOTE: normalize_debit() is deliberately NOT applied to any field here. It rescales values
    under $25 by x100 to repair a per-share/total mix-up on DEBITS; these are already total
    dollars and a sub-$25 net credit is perfectly legitimate. Applying it would inflate a
    position 100x."""
    # Gate 1 (S2 hole 1): the credit allow-list is exactly one structure.
    _require_allowed_structure("credit", t.get("structure"))
    # Gate 2: every credit field present, finite, positive, not a boolean.
    strike = _require_positive(t, "strike")
    collateral = _require_positive(t, "collateral_usd")
    net_credit = _require_positive(t, "net_credit_usd")
    max_loss = _require_positive(t, "max_loss_usd")

    # From here on money is compared in EXACT INTEGER CENTS (S2 hole 4), never binary floats.
    strike_c = _cents(t["strike"], "strike")
    collateral_c = _cents(t["collateral_usd"], "collateral_usd")
    net_credit_c = _cents(t["net_credit_usd"], "net_credit_usd")
    max_loss_c = _cents(t["max_loss_usd"], "max_loss_usd")

    # Gate 3 (S2 hole 3): a CSP's credit can NEVER reach its collateral -- the credit is a
    # fraction of the strike, and max_loss = collateral - credit must stay strictly positive.
    # `collateral=100, credit=100.005, max_loss=0.001` used to be accepted: the absolute
    # difference tolerance masked an economically impossible, near-zero declared risk.
    if net_credit_c >= collateral_c:
        raise ValueError(
            "net_credit_usd $%.2f >= collateral_usd $%.2f -- impossible for a cash-secured "
            "put; the credit received is always a fraction of the collateral posted"
            % (net_credit, collateral))

    # Gate 4 (S2 hole 4): max_loss == collateral - credit, INCLUSIVE $0.01 boundary, in cents.
    if abs(max_loss_c - (collateral_c - net_credit_c)) > _MAX_LOSS_TOL_CENTS:
        raise ValueError("max_loss_usd %.2f != collateral_usd %.2f - net_credit_usd %.2f"
                         % (max_loss, collateral, net_credit))

    # Gate 5 (S2 hole 2): collateral is bound to the strike, not merely positive.
    contracts = _implied_contracts(strike_c, collateral_c)

    return {
        "structure": CSP_STRUCTURE,
        "strike": strike,
        "collateral_usd": collateral,
        "net_credit_usd": net_credit,
        "max_loss_usd": max_loss,
        # not a TradeIdea field -- returned so callers/tests can see what the collateral implies
        "implied_contracts": contracts,
    }


def _extract_json(raw: str) -> Optional[dict]:
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def parse_ideas(raw: str) -> List[TradeIdea]:
    """Validate + NORMALIZE model output into canonical TradeIdeas. Drops the unrecoverable."""
    out: List[TradeIdea] = []
    obj = _extract_json(raw)
    if not isinstance(obj, dict):
        return out
    for t in obj.get("trades", []) or []:
        if not isinstance(t, dict):
            continue
        try:
            side = normalize_side(t.get("side"))
            if side is None:
                raise ValueError("unrecognised side %r" % (t.get("side"),))
            if side == "credit":
                credit = _parse_credit_fields(t)   # raises -> idea dropped
                # est_debit_usd is NOT required for a credit idea and must not be invented.
                debit = 0.0
                structure = credit["structure"]
                # S4: credit-scoped CSP carve-out. Only reachable once the idea has been
                # proved to be a valid cash-secured put, so it can never touch a long put.
                direction = normalize_csp_direction(t.get("direction", ""))
            else:
                credit = {}
                # S2 hole 1: the allow-list now runs on the DEBIT path too. This is the gate
                # that was missing entirely -- "naked call" / "short strangle" parsed cleanly.
                _require_allowed_structure("debit", t.get("structure"))
                debit = normalize_debit(float(_reject_bool(t["est_debit_usd"], "est_debit_usd")))
                # UNCHANGED: the ORIGINAL string is stored, not the canonical form.
                structure = str(t.get("structure", "")).strip()
                direction = normalize_direction(t.get("direction", ""), t.get("structure", ""))
            u = str(t["underlying"]).upper().strip()
            _hold = t.get("intended_hold_days")
            idea = TradeIdea(
                underlying=u,
                is_index=bool(t.get("is_index", u in INDEX_UNDERLYINGS)) or (u in INDEX_UNDERLYINGS),
                direction=direction or "",
                structure=structure,
                target_dte=int(_reject_bool(t["target_dte"], "target_dte")),
                target_delta=min(1.0, abs(float(_reject_bool(t["target_delta"], "target_delta")))),
                est_debit_usd=debit,
                conviction=max(1, min(10, int(_reject_bool(t["conviction"], "conviction")))),
                thesis=str(t.get("thesis", "")).strip(),
                profit_target_pct=_clamp_pct(t.get("profit_target_pct"), 20.0, 500.0),
                stop_pct=_clamp_pct(t.get("stop_pct"), 10.0, 90.0),
                intended_hold_days=(int(_reject_bool(_hold, "intended_hold_days"))
                                    if _hold is not None
                                    and int(_reject_bool(_hold, "intended_hold_days")) > 0
                                    else None),
                side=side,
                collateral_usd=credit.get("collateral_usd", 0.0),
                net_credit_usd=credit.get("net_credit_usd", 0.0),
                max_loss_usd=credit.get("max_loss_usd", 0.0),
                strike=credit.get("strike", 0.0),
            )
        except (KeyError, TypeError, ValueError):
            continue
        if not idea.underlying or idea.direction not in ("bullish", "bearish"):
            continue
        if idea.target_dte <= 0 or not (0.0 < idea.target_delta <= 1.0):
            continue
        # Unchanged debit rejection (was the single :201 condition): a debit idea with no
        # positive debit is unrecoverable. A credit idea legitimately has no debit at all.
        if idea.side != "credit" and idea.est_debit_usd <= 0:
            continue
        out.append(idea)
    return out


# Escalating backoff for transient busy: m3_serve.py is single-threaded behind GEN_LOCK
# (LOCK_WAIT_S=45s) and returns HTTP 503 when busy. The live trader loop (every 900s) and the
# daily slate collide on the one model, so a thinking-on trader generation can hold the lock
# longer than a single 45s wait -- the old flat 4x15s=45s window matched exactly ONE lock wait
# and let 503s slip through. Escalating 8/16/24/32 = ~80s total added wait outlasts a finite
# trader gen while staying WELL under the 900s trader interval (safe for the trader hot path,
# which shares this helper).
_BUSY_BACKOFFS = (8, 16, 24, 32)


def _post_json(endpoint, body, timeout, retries=5, backoff=None, return_identity=False):
    _env_r = os.environ.get("SLATE_POST_RETRIES")
    if _env_r and _env_r.isdigit():
        retries = max(retries, int(_env_r))
    """POST to an OpenAI-compatible endpoint with bounded retry on transient busy/connection errors.
    The trade brain is local (M3, single-generation): a 503 means BUSY, not broken -- it frees once
    the holder's generation finishes, so we retry rather than silently drop a real-money cycle. We
    deliberately do NOT fall back to a cloud model for trade decisions: no trade is better than a
    trade from an unvetted model. If M3 is genuinely unavailable across all retries, we raise and the
    caller skips the cycle (safe). Total added wait is bounded (~80s) so it can never stall the
    trader's 900s loop."""
    data = json.dumps(body).encode()
    before = None
    identity_error = None
    if return_identity:
        try:
            before = provenance.runtime_snapshot(endpoint)
        except provenance.RuntimeIdentityError as exc:
            identity_error = str(exc)
            if provenance.identity_required():
                raise
    last = None
    for attempt in range(retries):
        priority = int(os.environ.get("TRADER_LLM_PRIORITY", "1"))
        headers = {"Content-Type": "application/json"}
        headers.update(provenance.priority_headers(priority))
        req = urllib.request.Request(endpoint, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                result = json.loads(r.read().decode(), strict=False)
                if not return_identity:
                    return result
                if before is not None:
                    try:
                        after = provenance.runtime_snapshot(endpoint)
                        return result, provenance.request_identity(
                            endpoint=endpoint, body=body, response=result, before=before, after=after)
                    except provenance.RuntimeIdentityError as exc:
                        if provenance.identity_required():
                            raise
                        identity_error = str(exc)
                return result, {
                    "schema": provenance.IDENTITY_SCHEMA,
                    "verified": False,
                    "identity_error": identity_error,
                    "endpoint": endpoint,
                    "system_prompt_sha256": provenance.sha256(body["messages"][0].get("content") or ""),
                    "context_sha256": provenance.sha256(body["messages"][1].get("content") or ""),
                    "request_sha256": provenance.sha256(body),
                    "response_sha256": provenance.sha256(result),
                }
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (502, 503, 504, 429) and attempt < retries - 1:
                wait = backoff if backoff is not None else _BUSY_BACKOFFS[min(attempt, len(_BUSY_BACKOFFS) - 1)]
                print(f"[strategist] model busy ({e.code}), retry {attempt + 1}/{retries - 1} in {wait}s")
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            last = e
            if attempt < retries - 1:
                wait = backoff if backoff is not None else _BUSY_BACKOFFS[min(attempt, len(_BUSY_BACKOFFS) - 1)]
                print(f"[strategist] connection error ({type(e).__name__}), retry {attempt + 1}/{retries - 1} in {wait}s")
                time.sleep(wait)
                continue
            raise
    if last:
        raise last


def _resolve_thinking(default):
    """Env override for the daily slate. SLATE_THINKING=disabled forces thinking-OFF for ALL
    slate model calls (fast, short generations) so they do not hold the single :8082 GEN_LOCK
    for minutes and 503 everyone else. Unset/invalid => keep the passed default."""
    v = os.environ.get("SLATE_THINKING")
    return v if v in ("enabled", "disabled", "adaptive") else default


def propose(endpoint: str, model: str, market_context: str, timeout: int = 300,
            recommend: bool = False, thinking: str = None, return_raw: bool = False,
            return_cot: bool = False, return_identity: bool = False):
    """recommend=False: conservative loop (silence allowed). recommend=True: daily slate (always
    surfaces its best ideas, scored honestly 1-10).
    thinking: enabled/disabled/adaptive for M3 chain-of-thought. Defaults ON for the daily recommend
    slate (deeper reasoning for new-position research), OFF for the latency-bound conservative loop.
    return_raw (ADDITIVE, record-only 2026-07-02): when True, returns (ideas, raw_content) so the
    caller can capture the strategist's VERBATIM reasoning into the decision dataset. Default False
    preserves the original `List[TradeIdea]` return -- every existing caller is unchanged.
    return_cot (ADDITIVE, 2026-07-03): when True, returns (ideas, content, cot) -- `cot` is the
    model's chain-of-thought read from message.reasoning_content (the m3_serve additive field), or
    None when the endpoint returned no CoT. `content`/parsing are byte-identical; cot is captured
    into the decision record's distinct `cot` field, SEPARATE from raw_strategist (the clean answer).
    Takes precedence over return_raw when both are set."""
    think = thinking if thinking is not None else ("enabled" if recommend else "disabled")
    think = _resolve_thinking(think)
    # Thinking emits CoT BEFORE the answer and m3_serve.strip_think drops up to </mm:think>; give it
    # headroom or the answer gets strangled / returns raw CoT (the MAXTOK length-strangling trap).
    mt = (24000 if think == "enabled" else 2000) if recommend else 1400
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": RECOMMEND_PROMPT if recommend else SYSTEM_PROMPT},
            {"role": "user", "content": market_context},
        ],
        "max_tokens": mt,
        "temperature": 0.4,
        "thinking": think,
    }
    posted = _post_json(endpoint, body, timeout, return_identity=return_identity)
    d, identity = posted if return_identity else (posted, None)
    _msg = d["choices"][0]["message"]
    content = _msg.get("content") or ""
    cot = _msg.get("reasoning_content") or None  # [m3cot] separate CoT field; None if endpoint stripped it
    ideas = parse_ideas(content)  # PARSING UNCHANGED: runs on the clean `content` exactly as before
    if return_identity:
        return (ideas, content, cot, identity)
    if return_cot:
        return (ideas, content, cot)
    return (ideas, content) if return_raw else ideas


SINGLE_PROMPT = (
    "You are an options swing-trading strategist for a SMALL (~$1,000) account. The user just added "
    "@@TICKER@@ to their watchlist and wants your SINGLE best option trade idea on @@TICKER@@ right "
    "now -- ONLY @@TICKER@@, no other names. Decide direction (bullish/bearish) and structure "
    "yourself from the market context. Score conviction HONESTLY 1-10; if you have no real edge on "
    "@@TICKER@@ today, score it low -- do not inflate. " + _UNIVERSE + "\n" + _REGIME + "\n" + _SCORING + "\n" + _CONTRACT
)


def propose_one(endpoint: str, model: str, market_context: str, ticker: str, timeout: int = 1800,
                thinking: str = "enabled", return_raw: bool = False, return_cot: bool = False,
                return_identity: bool = False):
    """Best SINGLE trade idea for ONE ticker (model picks direction/structure/conviction). Returns a
    TradeIdea or None. Used when the user adds a discovered name and wants a same-day suggestion.
    return_raw (ADDITIVE, record-only 2026-07-03): when True, returns (idea_or_None, raw_content) so
    the caller can capture the strategist's VERBATIM output -- the full text the endpoint returns
    (chain-of-thought included, IF the serving endpoint returns CoT) -- into the decision dataset.
    Default False preserves the original TradeIdea|None return: every existing caller is unchanged.
    PARSING IS BYTE-IDENTICAL either way: parse_ideas() runs on the SAME full `content`; return_raw
    only ALSO hands that content back for capture (it never alters which idea is chosen)."""
    prompt = SINGLE_PROMPT.replace("@@TICKER@@", ticker.upper())
    thinking = _resolve_thinking(thinking)
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": market_context},
        ],
        "max_tokens": 24000 if thinking == "enabled" else 2000,
        "temperature": 0.4,
        "thinking": thinking,
    }
    posted = _post_json(endpoint, body, timeout, return_identity=return_identity)
    d, identity = posted if return_identity else (posted, None)
    _msg = d["choices"][0]["message"]
    content = _msg.get("content") or ""          # clean answer (parsing target, unchanged)
    cot = _msg.get("reasoning_content") or None  # [m3cot] separate CoT field; None if endpoint stripped it
    ideas = [i for i in parse_ideas(content)
             if i.underlying.upper() == ticker.upper()]
    ideas.sort(key=lambda i: -i.conviction)
    best = ideas[0] if ideas else None
    if return_identity:
        return (best, content, cot, identity)
    if return_cot:
        return (best, content, cot)
    return (best, content) if return_raw else best


DISCOVER_PROMPT = (
    "You are scouting NEW options swing-trade CANDIDATES for a small US account -- names to put on "
    "a watchlist to research, NOT trades to place now. From today's market context, suggest up to 5 "
    "LIQUID US large-cap stocks or ETFs worth a look (momentum, catalyst, sector rotation). EXCLUDE "
    "any name already being watched (listed below), and avoid biotech / pharma and Elon-Musk-linked "
    "names (SPCX is the ONE permitted Elon-derivative name). One short reason each.\n"
    'Respond with ONLY this JSON: {"candidates":[{"ticker":"<SYM>","reason":"<short>"}]}'
)
_TICKER_RE = re.compile(r"^[A-Z]{1,5}$")


def discover_names(endpoint: str, model: str, market_context: str, exclude, timeout: int = 240,
                   thinking: str = "enabled", blocked=None):
    """Ask the model for NEW watchlist candidates (not trades). Returns [(ticker, reason), ...].
    thinking defaults ON: this is the daily web-research scout for new names, where deeper reasoning
    over the brief pays off and the once-a-morning latency cost is acceptable.
    blocked: hard code-side drop (blocked_names) -- the model is told to avoid these, but we also
    filter them out here so prompt non-adherence (e.g. ARKK as a SpaceX play) can't leak through."""
    exclude_up = {str(e).upper() for e in exclude}
    blocked_up = {str(b).upper() for b in (blocked or [])}
    thinking = _resolve_thinking(thinking)
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": DISCOVER_PROMPT + "\nAlready watched: " + ", ".join(sorted(exclude_up))},
            {"role": "user", "content": market_context},
        ],
        "max_tokens": 12000 if thinking == "enabled" else 700,
        "temperature": 0.6,
        "thinking": thinking,
    }
    d = _post_json(endpoint, body, timeout)
    obj = _extract_json(d["choices"][0]["message"].get("content") or "") or {}
    out, seen = [], set()
    for c in obj.get("candidates", []) or []:
        if not isinstance(c, dict):
            continue
        t = str(c.get("ticker", "")).upper().strip()
        if not _TICKER_RE.match(t) or t in exclude_up or t in blocked_up or t in seen:
            continue
        seen.add(t)
        out.append((t, str(c.get("reason", "")).strip()[:120]))
    return out[:5]


# ============================================================================================
# FINE-TUNED GEMMA integration hook (technical-card path).
#
# The QLoRA Gemma fine-tune was trained on byte-exact technical cards (see exitmgr.technical_card,
# ported verbatim from gordon-gauntlet/trading/gen_train_huge3.py). It emits a DIRECTIONAL signal
# -- {"call":"BULLISH|BEARISH|NEUTRAL","conviction":1-10} -- NOT a full TradeIdea. The existing
# structure-building / sizing path then turns that signal into an actual contract.
#
# This is intentionally SEPARATE from propose()/propose_one() (the MiniMax brief path). It is only
# wired in when config.trading.llm_model points at the Gemma fine-tune. Nothing here touches IBKR,
# restarts services, or alters the live MiniMax brief. READ-ONLY signal generation.
#
# CALL SITE (where it plugs in): daily_recommend.main(), around the
#   ideas = propose(tr.get("llm_endpoint"), tr.get("llm_model"), brief, ...)
# block. When _is_gemma(tr.get("llm_model")) is true, call gemma_signal(endpoint, model, ticker)
# per name to get its directional call+conviction (built from the byte-exact technical card), then
# feed that into the same structure/pricing path the slate already uses. The MiniMax propose()
# call stays as the default branch -- this is additive.
# ============================================================================================
def _is_gemma(model_name: str) -> bool:
    """True when the configured model is the fine-tuned Gemma (so we feed it technical cards
    rather than the MiniMax market brief)."""
    return "gemma" in (model_name or "").lower()


def gemma_signal(endpoint: str, model: str, ticker: str, horizon_label: str = "~2 weeks",
                 timeout: int = 120, vix_series=None):
    """Query the fine-tuned Gemma with the BYTE-EXACT technical card for `ticker`'s latest bar.

    Returns dict {"ticker","call","conviction","card"} or None if the model/parse fails or there's
    insufficient history to build the card. `call` in {BULLISH,BEARISH,NEUTRAL}, conviction 1-10.

    This is the model-query path for the fine-tune: the user content MUST be the exact card string
    the model trained on -- that byte-exactness is the whole point (test_card_match.py proves it).
    """
    from exitmgr.technical_card import fetch_card, card_messages, InsufficientHistory
    try:
        card = fetch_card(ticker, vix_series=vix_series, horizon_label=horizon_label)
    except InsufficientHistory:
        return None
    body = {
        "model": model,
        "messages": card_messages(ticker, card, horizon_label=horizon_label),
        "max_tokens": 64,
        "temperature": 0.0,
    }
    d = _post_json(endpoint, body, timeout)
    obj = _extract_json(d["choices"][0]["message"].get("content") or "") or {}
    call = str(obj.get("call", "")).upper().strip()
    if call not in ("BULLISH", "BEARISH", "NEUTRAL"):
        return None
    try:
        conv = int(obj.get("conviction", 0))
    except (TypeError, ValueError):
        return None
    conv = min(10, max(1, conv))
    return {"ticker": ticker.upper(), "call": call, "conviction": conv, "card": card}
