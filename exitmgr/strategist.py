#!/usr/bin/env python3
"""Public API contract; production-derived narrative omitted."""
import argparse
import http.client
import json
import math
import os
import re
import socket
import sys
import threading
import urllib.request
import urllib.error
import time
from contextlib import nullcontext
from dataclasses import dataclass, asdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import List, Optional

from exitmgr.risk import INDEX_UNDERLYINGS
from exitmgr import provenance
from exitmgr.entry_contract import (
    CONTRACT_VERSION,
    DEBIT_HOLD_FLOOR_MULTIPLE,
    EntryContractError,
    RuntimeCandidate,
    StageAIntent,
    parse_json_document,
    parse_stage_a,
    parse_stage_b,
    validate_candidates,
)
from glm_trader_lease import GLM_ENDPOINT, GLM_MODEL, LeaseError, trader_glm_lease



_SCORING = (
    "Score each idea 1-10 on its ABSOLUTE conviction -- this is NOT a rank-ordering of your picks, "
    "and you must NOT default to a 6/5/4 spread. Use the FULL range every day: 8-10 = HIGH "
    "(genuinely strong -- clear catalyst, favorable structure, good risk/reward, you would size up; "
    "use it whenever warranted and do NOT cap a strong idea at 6); 6-7 = MEDIUM (solid but with real "
    "caveats); 4-5 = MARGINAL but STILL TRADEABLE on a sound setup -- score it honestly and "
    "propose it; 1-3 = WEAK (never propose). Prefer an "
    "EMPTY slate over forcing weak ideas. Be honest BOTH ways: do not inflate a mediocre idea, and "
    "do not suppress a strong one. If two ideas are both genuinely strong, score BOTH 8+ -- no need "
    "to spread them apart."
)
_SAME_DAY_EARNINGS = (
    "EARNINGS TODAY (or 0d to earnings) means the release is scheduled for today; it does NOT "
    "mean the release has already happened. Treat the event as UPCOMING unless the supplied "
    "brief contains authoritative evidence with a reported timestamp earlier than this decision. "
    "Same-day earnings do not by themselves bar entry, but any entry thesis must explicitly "
    "distinguish the two states. BEFORE a release, underwrite an overnight gap beyond ordinary "
    "risk controls, elevated pre-event IV, and post-print IV repricing. State the consequence for "
    "the chosen structure: IV crush can hurt long premium even when direction is right; a debit "
    "spread reduces but does not erase that exposure; a short put may benefit from crush, but a "
    "downside gap and assignment risk can dominate. AFTER a verified release, reassess the actual "
    "price reaction, fresh option IV/NBBO, and stabilization; never merely assume the option is "
    "cheaper because the calendar says 0d. "
)
_UNIVERSE = (
    "Universe: SPY, QQQ, IWM, and liquid large-cap single names only. DO NOT propose "
    "Elon-Musk-linked companies (e.g. TSLA) -- they are rejected. SYMD is the "
    "ONE permitted Elon-derivative name (allowed). "








    "BINARY EVENTS: if a scheduled binary catalyst (FDA decision, trial readout, earnings) falls "
    "inside the intended hold, say so in the thesis and either decline or size for a gap THROUGH "
    "the stop -- an overnight gap fills wherever it opens, not at your stop level. This is about "
    "the event, not the sector. " + _SAME_DAY_EARNINGS +
    "STRUCTURES: long calls, long puts, DEBIT spreads ('call debit spread' / 'put debit "
    "spread'), or CASH-SECURED PUTS ('cash secured put'). For a debit you PAY the debit and "
    "that debit is your max loss. For a cash-secured put you RECEIVE a credit and must post "
    "collateral_usd = strike x 100 x contracts -- THE COLLATERAL, NOT THE CREDIT, is the capital "
    "the trade ties up, and it is what the size caps measure. A $40 credit on a $15 strike ties up "
    "$1,500. A cash-secured put must earn at least 1.25% of its collateral in premium, and you must "
    "state max_loss_usd = collateral_usd - net_credit_usd. Do NOT propose credit spreads, iron "
    "condors, naked shorts, or any margin structure -- a short that is not fully cash-secured is "
    "refused at the order layer. Prefer DEBIT SPREADS (cheaper, defined risk). For spreads, "
    "est_debit_usd is the NET debit. SIZE ONLY FROM A VALID Account sizing snapshot in this "
    "brief, not from a remembered figure; if that snapshot says unavailable, return no trades. "
    "One debit may use at most 25% of net liquidation value in premium; one "
    "cash-secured put may use at most 80% of net liquidation value in collateral. Compute the "
    "dollar limit from the net liq in this brief."
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
    "ENTRY DISCIPLINE: before any directional idea, confirm the UNDERLYING'S OWN trend -- that is "
    "the trend you are trading. The broad tape (SPY/QQQ) is CONTEXT, not a veto: it must not "
    "flatly contradict the idea, but it does NOT have to point the same way. Trade WITH the "
    "name's trend, in EITHER DIRECTION -- the trend is what must be confirmed, not the direction "
    "bullish.\n"
    "  * A confirmed UPTREND (underlying making higher highs) supports a BULLISH idea: a call "
    "debit spread or long call.\n"
    "  * A confirmed DOWNTREND IN THE NAME (lower lows, negative 5d and 20d) supports a BEARISH "
    "idea with exactly the same standing: a PUT DEBIT SPREAD or long put -- EVEN IF THE INDEX IS "
    "RISING. A name making lower lows while SPY/QQQ grind higher is RELATIVE WEAKNESS, which is "
    "one of the cleanest short setups there is, not a contradiction to be resolved in favour of "
    "the index. Requiring the index to fall too is what made the short side unreachable. A "
    "sustained downtrend is a tradeable setup, not a reason to sit out. Do not skip it merely "
    "because it is short-side, and do not downgrade it because the index disagrees.\n"
    "FADING is the error, not direction, and the test is THE NAME'S direction. Do not buy calls "
    "into a name that is falling hoping for a bounce ('oversold', 'due to bounce'), and do not "
    "buy puts into a name that is RISING hoping for a top. 'Buying a falling knife' means buying "
    "CALLS as something drops -- it does NOT mean declining to trade a downtrend. If the NAME is "
    "genuinely choppy or directionless, prefer an empty slate; and prefer a defined-risk debit "
    "spread over a naked long in either direction."
)














_EXPIRY_DOCTRINE = (
    f"DEBIT EXPIRY: target_dte and the selected candidate DTE must be at least "
    f"{DEBIT_HOLD_FLOOR_MULTIPLE} x intended_hold_days. "
    f"{DEBIT_HOLD_FLOOR_MULTIPLE}x is a minimum, not a ceiling. "
    "Choose intended_hold_days from the evidence and intended trade horizon; never shorten or "
    "lengthen it merely to make an expiry pass. For an 8-day hold the minimum is 64 DTE; "
    "a listed 71-DTE expiry is allowed by this rule and is not too long. If the exact minimum "
    "is not listed, runtime may use the next listed expiry at or above the floor; do not invent "
    "a contract or reject a supplied candidate solely because it exceeds the minimum or target_dte. "
    "Later expiries remain subject to the configured maximum DTE, live premium cap, liquidity, "
    "quote freshness, and every other construction and risk gate. Buy enough time to keep theta "
    "immaterial over the intended hold; assess the actual listed economics and exit or restructure "
    "before theta becomes material. If no compliant expiry is affordable, decline instead of "
    "weakening the floor. This rule applies only to DEBITS. CASH-SECURED PUTS retain their "
    "separate configured 3-45 DTE window and DTE no shorter than intended_hold_days. "
)

_DOCTRINE = (
    "TRADING DOCTRINE -- these are hard rules of this desk, not preferences.\n"
    "1. CONVICTION IS A SIZE INPUT, NOT A TAKE BAR. Score each sound setup 1-10; "
    "every score remains eligible, while deterministic policy maps the score to size. "
    "Use the upper tier only for setups you would deliberately size larger, and never "
    "inflate the score to obtain more capital.\n"
    "1b. AFFORDABILITY IS A SCREEN, NOT A SIZING STEP. The brief states Max debit per trade. Before you propose a name, satisfy yourself that a defined-risk spread on THAT underlying can be built for less than it. A rough test: a 0.60-delta debit spread typically costs on the order of 2% of the underlying's share price x 100, so a name roughly priced above HALF the Max debit per trade figure in this brief almost never fits. Use the figure in the brief, never a remembered one -- it moves with the account. Proposing an unfundable name is a wasted slot, not a near miss: a synthetic high-priced name can have every available spread exceed the stated budget, so no strike pair could have worked. Prefer names cheap enough that the budget buys real structure.\n"
    "2. " + _EXPIRY_DOCTRINE + "\n"
    "2b. BUY TIME, THEN DO NOT USE IT. Choose expiry from the setup, listed chain, "
    "liquidity, premium budget, and intended hold. Do not emit a constant DTE or extend "
    "the holding period to justify an expiry. The hard loss stop remains mandatory. "
    "3. CONSTRUCTION: the default build is a LONG-DATED DEBIT SPREAD in the direction the trend "
    "confirms -- a CALL debit spread on a confirmed uptrend, a PUT debit spread on a confirmed "
    "downtrend. Everything below applies identically to both; read 'bull call spread' as 'the "
    "spread on the side the trend supports'. The short leg "
    "finances part of the long, so the spread is the cheap way to enter long-dated bullish "
    "exposure -- it is a capital-efficiency choice, not an aversion to naked calls. A long-dated "
    "naked long call is also acceptable. What is NOT acceptable is choosing between them as a "
    "function of conviction: structure selection follows the trade economics and "
    "the configured risk limits. Pick the structure on its economics, never on "
    "the conviction score.\n"
    "4. LONG-LEG DELTA IS A BAND, NOT A PREFERENCE: for any DEBIT structure, target_delta must "
    "land between 0.55 and 0.65. This is enforced at runtime -- a value outside the band is "
    "CLAMPED into it, silently, before the order is built. Anything you ask for below 0.55 "
    "becomes 0.55. The reason is that far-OTM low-delta legs were the lottery tickets in the "
    "audit: a 0.55-0.65 leg is already WORKING, not hoping, and it moves with the underlying "
    "instead of needing a miracle to reach the strike. THE CONSEQUENCE YOU MUST ACT ON: price the "
    "trade for the leg you will actually be given. A 0.60-delta contract costs materially more "
    "than a 0.40-delta one, so est_debit_usd and allocation_pct_net_liq must both be computed at "
    "the IN-BAND delta. Asking for 0.40 and sizing for 0.40 produces a position roughly half the "
    "size you intended, because the fill happens at the clamped delta and the cash does not "
    "stretch. Emit target_delta inside 0.55-0.65 and size the trade to THAT contract. This governs "
    "DEBITS only: a credit structure (CSP) uses your requested delta unclamped, since selling a "
    "low-delta put is the whole point there."
)


SYSTEM_PROMPT = (
    "You are a disciplined options swing-trading strategist for a SMALL account. Propose 0-3 trades "
    "you have genuine conviction on, or an empty list if nothing is compelling -- never force trades. "
    + _UNIVERSE + "\n" + _REGIME + "\n" + _DOCTRINE + "\n" + _SCORING + "\n" + _CONTRACT
)


RECOMMEND_PROMPT = (
    "You are an options swing-trading strategist for a SMALL account whose exact net liquidation value is stated in this brief -- size every trade from THAT figure, never from a remembered one. Recommend your "
    "BEST 1-3 option trade ideas for today. ALWAYS give at least one idea unless the market is "
    "genuinely untradeable -- it is fine to include moderate or weak ideas, just score them "
    "honestly so the human can judge. " + _UNIVERSE + "\n" + _REGIME + "\n" + _DOCTRINE + "\n" + _SCORING + "\n" + _CONTRACT
)








STAGE_AB_CONTRACT_VERSION = CONTRACT_VERSION

_STAGE_A_UNIVERSE = (
    "Universe: SPY, QQQ, IWM, and liquid US single names permitted by the supplied market brief. "
    "Do not propose Elon-Musk-linked companies (for example TSLA); SYMD is the one permitted "
    "Elon-derivative name. Scheduled binary catalysts inside the intended hold must be stated in "
    "the thesis and reflected in conviction and allocation. " + _SAME_DAY_EARNINGS +
    "The only structures are exactly: long call, long put, call debit spread, put debit spread, "
    "cash secured put. A cash secured put is side credit, direction bullish. Every other structure "
    "is side debit; call structures are bullish and put debit structures are bearish."
)

_STAGE_A_CONTRACT = (
    f"STAGE A CONTRACT ({STAGE_AB_CONTRACT_VERSION}) -- respond with one complete JSON document "
    "and nothing else: no markdown, prose, comments, or trailing bytes. The top level has exactly "
    "one key, intents. Every key appears EXACTLY ONCE in the object that contains it -- a repeated "
    "key (for example alpha given twice) makes the document invalid and the entire response is "
    "discarded, so write each field once and only once. "
    "intents contains zero to three objects, each with exactly these keys: "
    "underlying, side, direction, structure, target_dte, intended_hold_days, target_delta, "
    "conviction, allocation_pct_net_liq, alpha, thesis. Use the exact lowercase enum spellings "
    "given here; the ticker itself must be uppercase. The enum values are these literal strings "
    "and no others -- SPACE-separated words, never snake_case, camelCase, hyphenated or "
    'abbreviated. side is exactly "debit" or "credit". direction is exactly "bullish" or '
    '"bearish". structure is exactly one of "long call", "long put", "call debit spread", '
    '"put debit spread", "cash secured put". A value such as "call_debit_spread", '
    '"callDebitSpread", "bull call spread" or "CSP" is rejected outright and your ENTIRE '
    "response is discarded, so copy the string byte-for-byte. "
    "target_dte is an integer from 1 through 800; "
    "intended_hold_days is an integer from 1 through 365 and may not exceed target_dte; "
    "target_delta is greater than 0 and no greater than 1; conviction is an integer from 1 "
    "through 10; allocation_pct_net_liq is a percentage greater than 0 and no greater than 100. "
    "alpha must contain 1 through 600 characters; thesis must contain 1 through 1200 characters. "
    "alpha names the expected edge, while thesis states "
    "the source-bound trade case and invalidation logic. Put any explanation of target_dte "
    "inside thesis; do not add target_dte_note or other explanatory keys. "
    "The allocation is intent only; runtime "
    "will apply the live account and risk caps. "
    "You must not author or include est_debit_usd, price, premium, strike, expiry, con_id, contract "
    "identifier, width, bid, ask, credit, collateral, max loss, quantity, or any other key. Runtime "
    "alone discovers and prices contracts after this response. If declining, emit exactly "
    '{"intents":[]} and stop; an empty Stage A result is terminal.'
)

STAGE_A_SYSTEM_PROMPT = (
    "You are a disciplined options swing-trading strategist for a small account. Choose zero to "
    "three trade intents only when the supplied evidence supports genuine alpha; never force a "
    "trade. " + _STAGE_A_UNIVERSE + "\n" + _REGIME + "\n" + _DOCTRINE + "\n" + _SCORING + "\n" + _STAGE_A_CONTRACT
)

STAGE_A_RECOMMEND_PROMPT = (
    "You are an options swing-trading strategist for a small account. Return the best one to three "
    "trade intents supported by today's supplied evidence, scored honestly; an exact empty decline "
    "is still required when the market is genuinely untradeable. "
    + _STAGE_A_UNIVERSE + "\n" + _REGIME + "\n" + _DOCTRINE + "\n" + _SCORING + "\n" + _STAGE_A_CONTRACT
)


















_STAGE_A_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "intents": {
            "type": "array",
            "minItems": 0,
            "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "underlying": {"type": "string", "pattern": "^[A-Z][A-Z0-9.]{0,7}$"},
                    "side": {"type": "string", "enum": ["debit", "credit"]},
                    "direction": {"type": "string", "enum": ["bullish", "bearish"]},
                    "structure": {"type": "string", "enum": ["long call", "long put",
                                                             "call debit spread",
                                                             "put debit spread",
                                                             "cash secured put"]},
                    "target_dte": {"type": "integer", "minimum": 1, "maximum": 800},
                    "intended_hold_days": {"type": "integer", "minimum": 1, "maximum": 365},
                    "target_delta": {"type": "number", "exclusiveMinimum": 0, "maximum": 1},
                    "conviction": {"type": "integer", "minimum": 1, "maximum": 10},
                    "allocation_pct_net_liq": {"type": "number", "exclusiveMinimum": 0,
                                               "maximum": 100},
                    "alpha": {"type": "string", "minLength": 1, "maxLength": 600},
                    "thesis": {"type": "string", "minLength": 1, "maxLength": 1200},
                },
                "required": ["underlying", "side", "direction", "structure", "target_dte",
                             "intended_hold_days", "target_delta", "conviction",
                             "allocation_pct_net_liq", "alpha", "thesis"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["intents"],
    "additionalProperties": False,
}


def structured_output_enabled() -> bool:
    """Public API contract; production-derived narrative omitted."""
    value = os.environ.get("TRADER_STRUCTURED_OUTPUT", "0").strip().lower()
    return value not in ("0", "false", "no", "off", "")


def _stage_a_response_format():
    return {"type": "json_schema",
            "json_schema": {"name": "stage_a_intents", "strict": True,
                            "schema": _STAGE_A_JSON_SCHEMA}}


_SAME_DAY_EARNINGS_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["proceed", "decline"]},
        "event_phase": {
            "type": "string",
            "enum": ["pre_release", "post_release", "unknown"],
        },
        "reason": {"type": "string", "minLength": 20, "maxLength": 2000},
    },
    "required": ["decision", "event_phase", "reason"],
    "additionalProperties": False,
}


def _same_day_earnings_response_format():
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "same_day_earnings_review",
            "strict": True,
            "schema": _SAME_DAY_EARNINGS_REVIEW_SCHEMA,
        },
    }


SAME_DAY_EARNINGS_REVIEW_PROMPT = (
    "You are the final same-day-earnings risk reviewer for an already model-authored options "
    "entry. The original decision did not know earnings were scheduled for today. Review the "
    "supplied market brief and exact order risk envelope, then either permit it or decline it. "
    "You may not change a strike, expiry, side, structure, quantity, price, or any other term. "
    "Runtime will obtain a newer executable quote after this review; the receipt remains valid "
    "only while that quote and every other term stay inside the existing strict non-material "
    "change bound, otherwise another review or a refusal is required. The calendar value 0d "
    "proves only that earnings are scheduled today; it does "
    "not prove the release has occurred. Use post_release only when the supplied evidence "
    "authoritatively and timestampedly establishes that the release preceded this review; use "
    "unknown otherwise when timing cannot be established. Explicitly weigh overnight gap risk, "
    "pre-event implied volatility and post-print IV repricing/crush. Long premium carries both "
    "directional and vega risk; a debit spread reduces but does not erase vega risk; a cash-secured "
    "put may benefit from IV crush but still carries downside-gap and assignment risk. Respond "
    "with one complete JSON document and nothing else, with exactly these keys: "
    "decision (proceed or decline), event_phase (pre_release, post_release, or unknown), and a "
    "substantive reason. When evidence is insufficient to underwrite the exact order, decline."
)


@dataclass(frozen=True)
class SameDayEarningsReview:
    decision: str
    event_phase: str
    reason: str

    @property
    def proceed(self) -> bool:
        return self.decision == "proceed"


def parse_same_day_earnings_review(raw: str) -> SameDayEarningsReview:
    """Public API contract; production-derived narrative omitted."""
    obj = parse_json_document(raw)
    if not isinstance(obj, dict):
        raise EntryContractError("same-day earnings review must be a JSON object")
    expected = {"decision", "event_phase", "reason"}
    if set(obj) != expected:
        missing = sorted(expected - set(obj))
        extra = sorted(set(obj) - expected)
        raise EntryContractError(
            f"same-day earnings review keys mismatch; missing={missing}, extra={extra}")
    decision = obj["decision"]
    phase = obj["event_phase"]
    reason = obj["reason"]
    if not isinstance(decision, str) or decision not in ("proceed", "decline"):
        raise EntryContractError("same-day earnings decision must be proceed or decline")
    if not isinstance(phase, str) or phase not in (
            "pre_release", "post_release", "unknown"):
        raise EntryContractError(
            "same-day earnings event_phase must be pre_release, post_release, or unknown")
    if not isinstance(reason, str):
        raise EntryContractError("same-day earnings reason must be a string")
    reason = reason.strip()
    if len(reason) < 20 or len(reason) > 2000:
        raise EntryContractError("same-day earnings reason must contain 20 to 2000 characters")
    return SameDayEarningsReview(decision, phase, reason)


STAGE_B_SYSTEM_PROMPT = (
    f"You are the Stage B selector for {STAGE_AB_CONTRACT_VERSION}. Runtime has already rejected "
    "stale, one-sided, structurally invalid, illiquid, over-cap, and unaffordable contracts. Review "
    "the supplied immutable Stage A intent and its three to five runtime-priced candidates. Select "
    "only a supplied candidate whose executable economics best express that intent, or decline. "
    + _EXPIRY_DOCTRINE + "\n"
    "Respond with exactly one complete JSON document and nothing else. A selection is exactly "
    '{"candidate_id":"cand_<64 lowercase hex characters>"}; copy the full candidate_id byte-for-byte '
    "from the supplied candidates. A decline is exactly {\"decline\":true}. Do not include a "
    "rationale or any second key. Do not author or alter price, strike, expiry, contract identifier, "
    "width, credit, collateral, max loss, allocation, cap, or quantity. Decline is terminal."
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
    profit_target_pct: float = 0.0
    stop_pct: float = 0.0
    intended_hold_days: Optional[int] = None




    side: str = "debit"
    collateral_usd: float = 0.0
    net_credit_usd: float = 0.0
    max_loss_usd: float = 0.0
    strike: float = 0.0


def normalize_direction(raw_dir: str, structure: str) -> Optional[str]:
    """Public API contract; production-derived narrative omitted."""
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
    """Public API contract; production-derived narrative omitted."""
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
    """Public API contract; production-derived narrative omitted."""
    if 0 < value < 25.0:
        return round(value * 100.0, 2)
    return value




CSP_STRUCTURE = "cash secured put"
_SIDES = ("debit", "credit")

























DEBIT_STRUCTURES = frozenset({

    "long call",
    "long put",
    "long option",

    "call debit spread",
    "put debit spread",
    "debit call spread",
    "debit put spread",
    "bull call spread",
    "bear put spread",
    "long call spread",
    "long put spread",
    "debit spread",
})








def _require_allowed_structure(side: str, raw_structure) -> str:
    """Public API contract; production-derived narrative omitted."""
    canon = _canonical_structure(raw_structure)
    allowed = frozenset({CSP_STRUCTURE}) if side == "credit" else DEBIT_STRUCTURES
    if canon not in allowed:
        raise ValueError(
            "structure %r is not permitted on side=%r. Permitted: %s. An unrecognised or "
            "short-premium structure is REFUSED, never guessed -- a naked short has unbounded "
            "loss and this account may not carry one." % (raw_structure, side,
                                                          ", ".join(sorted(allowed))))
    return canon















_CENT = Decimal("0.01")




_MAX_LOSS_TOL_CENTS = 1



_COLLATERAL_TOL_CENTS = 1


def _reject_bool(value, key: str):
    """Public API contract; production-derived narrative omitted."""
    if isinstance(value, bool):
        raise ValueError("%s must be a number, not the boolean %r" % (key, value))
    return value


def _cents(value, key: str) -> int:
    """Public API contract; production-derived narrative omitted."""
    _reject_bool(value, key)
    try:
        d = Decimal(str(value))
        if not d.is_finite():
            raise ValueError("%s must be finite, got %r" % (key, value))
        return int(d.quantize(_CENT, rounding=ROUND_HALF_UP) * 100)
    except (InvalidOperation, ArithmeticError, TypeError) as exc:
        raise ValueError("%s is not a usable decimal amount: %r (%s)" % (key, value, exc))


def normalize_csp_direction(raw) -> str:
    """Public API contract; production-derived narrative omitted."""
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
    """Public API contract; production-derived narrative omitted."""
    s = str(raw if raw not in (None, "") else "debit").strip().lower()
    return s if s in _SIDES else None


def _canonical_structure(raw) -> str:
    return re.sub(r"\s+", " ", str(raw if raw is not None else "").strip().lower())


def _require_finite(t: dict, key: str) -> float:
    """Public API contract; production-derived narrative omitted."""
    _reject_bool(t[key], key)
    v = float(t[key])
    if not math.isfinite(v):
        raise ValueError("%s must be a finite number, got %r" % (key, t[key]))
    return v


def _require_positive(t: dict, key: str) -> float:
    """Public API contract; production-derived narrative omitted."""
    _reject_bool(t[key], key)
    v = float(t[key])
    if not math.isfinite(v) or v <= 0:
        raise ValueError("%s must be a finite positive number, got %r" % (key, t[key]))
    return v


def _implied_contracts(strike_cents: int, collateral_cents: int) -> int:
    """Public API contract; production-derived narrative omitted."""
    unit_cents = strike_cents * 100
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
    """Public API contract; production-derived narrative omitted."""

    _require_allowed_structure("credit", t.get("structure"))

    strike = _require_positive(t, "strike")
    collateral = _require_positive(t, "collateral_usd")
    net_credit = _require_positive(t, "net_credit_usd")
    max_loss = _require_positive(t, "max_loss_usd")


    strike_c = _cents(t["strike"], "strike")
    collateral_c = _cents(t["collateral_usd"], "collateral_usd")
    net_credit_c = _cents(t["net_credit_usd"], "net_credit_usd")
    max_loss_c = _cents(t["max_loss_usd"], "max_loss_usd")





    if net_credit_c >= collateral_c:
        raise ValueError(
            "net_credit_usd $%.2f >= collateral_usd $%.2f -- impossible for a cash-secured "
            "put; the credit received is always a fraction of the collateral posted"
            % (net_credit, collateral))


    if abs(max_loss_c - (collateral_c - net_credit_c)) > _MAX_LOSS_TOL_CENTS:
        raise ValueError("max_loss_usd %.2f != collateral_usd %.2f - net_credit_usd %.2f"
                         % (max_loss, collateral, net_credit))


    contracts = _implied_contracts(strike_c, collateral_c)

    return {
        "structure": CSP_STRUCTURE,
        "strike": strike,
        "collateral_usd": collateral,
        "net_credit_usd": net_credit,
        "max_loss_usd": max_loss,

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
    """Public API contract; production-derived narrative omitted."""
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
                credit = _parse_credit_fields(t)

                debit = 0.0
                structure = credit["structure"]


                direction = normalize_csp_direction(t.get("direction", ""))
            else:
                credit = {}


                _require_allowed_structure("debit", t.get("structure"))
                debit = normalize_debit(float(_reject_bool(t["est_debit_usd"], "est_debit_usd")))

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
                target_delta=min(1.0, abs(_require_finite(t, "target_delta"))),
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


        if idea.side != "credit" and idea.est_debit_usd <= 0:
            continue
        out.append(idea)
    return out





_BUSY_BACKOFFS = (8, 16, 24, 32)





_GLM_PRIMARY_ENDPOINT = "http://127.0.0.1:18080/v1/chat/completions"
_GLM_PRIMARY_MODEL = "glm-5.3-flash-candidate"
_DSV41_PRIMARY_MODEL = "example-mtp-model"
_DEEPSEEK_FALLBACK_ENDPOINT = "http://127.0.0.1:8888/v1/chat/completions"
_DEEPSEEK_FALLBACK_MODEL = "deepseek-v4-flash-0731"


class ModelRouteUnavailable(RuntimeError):
    """Public API contract; production-derived narrative omitted."""


class ModelDeadlineExceeded(ModelRouteUnavailable):
    """Public API contract; production-derived narrative omitted."""


def _remaining_model_time(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ModelDeadlineExceeded("model call total deadline exhausted")
    return remaining


def _model_retry_wait(wait, deadline):
    remaining = _remaining_model_time(deadline)
    if wait >= remaining:
        raise ModelDeadlineExceeded("model call has no time remaining for retry")
    time.sleep(wait)
    _remaining_model_time(deadline)


def _read_model_response(response, deadline):
    """Public API contract; production-derived narrative omitted."""
    remaining = _remaining_model_time(deadline)
    sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
    expired = threading.Event()

    def expire():
        expired.set()
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    timer = threading.Timer(remaining, expire)
    timer.daemon = True
    timer.start()
    chunks = []
    size = 0
    try:
        read1 = getattr(response, "read1", None)
        while True:
            remaining = _remaining_model_time(deadline)



            if sock is not None and sock.fileno() >= 0:
                sock.settimeout(remaining)

            chunk = read1(65536) if read1 is not None else response.read()
            if expired.is_set():
                raise ModelDeadlineExceeded("model response total deadline exhausted")
            _remaining_model_time(deadline)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            size += len(chunk)
            if size > 4 * 1024 * 1024:
                raise ModelRouteUnavailable("model response exceeds 4MiB read bound")
            if read1 is None:
                return b"".join(chunks)
    except ModelDeadlineExceeded:
        expire()
        raise
    except (OSError, http.client.HTTPException) as exc:
        if expired.is_set() or time.monotonic() >= deadline:
            expire()
            raise ModelDeadlineExceeded("model response total deadline exhausted") from exc
        raise
    finally:
        timer.cancel()
        timer.join()


class _DeadlineResponse:
    """Public API contract; production-derived narrative omitted."""
    def __init__(self, response, deadline):
        self.response, self.deadline = response, deadline

    def __enter__(self):
        self.response.__enter__()
        return self

    def __exit__(self, *args):
        return self.response.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self.response, name)

    def read(self):
        return _read_model_response(self.response, self.deadline)


def _thinking_enabled(body):
    """Public API contract; production-derived narrative omitted."""
    raw = body.get("thinking")
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        value = raw.strip().lower()
        if value in ("enabled", "adaptive", "on", "true", "1", "high"):
            return True
        if value in ("disabled", "off", "false", "0", "none"):
            return False
    if isinstance(raw, dict):
        kind = str(raw.get("type", "")).strip().lower()
        if kind in ("enabled", "adaptive"):
            return True
        if kind == "disabled":
            return False
    if isinstance(body.get("enable_thinking"), bool):
        return body["enable_thinking"]
    kwargs = body.get("chat_template_kwargs")
    if isinstance(kwargs, dict):
        for key in ("thinking", "enable_thinking"):
            if isinstance(kwargs.get(key), bool):
                return kwargs[key]
    effort = body.get("reasoning_effort")
    if isinstance(effort, str):
        return effort.strip().lower() not in ("none", "off", "disabled", "false", "0")
    return None


def _glm_request_body(body):
    """Public API contract; production-derived narrative omitted."""
    enabled = _thinking_enabled(body)
    if enabled is None:
        raise ValueError("exact GLM trader request has no explicit thinking setting")
    out = dict(body)




    raw_kwargs = out.get("chat_template_kwargs")
    if raw_kwargs is not None and not isinstance(raw_kwargs, dict):
        raise ValueError("GLM chat_template_kwargs must be an object")
    template_kwargs = dict(raw_kwargs or {})
    template_kwargs.pop("thinking", None)
    template_kwargs["enable_thinking"] = enabled
    out["chat_template_kwargs"] = template_kwargs
    out["reasoning_effort"] = "high" if enabled else "none"
    out.pop("enable_thinking", None)
    out.pop("thinking", None)
    out.pop("think", None)
    return out


def _dsv41_request_body(body, *, spark_fallback=False):
    """Public API contract; production-derived narrative omitted."""
    enabled = _thinking_enabled(body)
    if enabled is None:
        raise ValueError("exact DSV4.1 trader request has no explicit thinking setting")
    raw_kwargs = body.get("chat_template_kwargs")
    if raw_kwargs is not None and not isinstance(raw_kwargs, dict):
        raise ValueError("DSV4.1 chat_template_kwargs must be an object")
    out = dict(body)
    template_kwargs = dict(raw_kwargs or {})
    for key in ("thinking", "enable_thinking", "thinking_mode", "think"):
        template_kwargs.pop(key, None)
    if spark_fallback:

        template_kwargs["thinking"] = enabled
    else:

        template_kwargs.update(enable_thinking=enabled,
                               thinking_mode="thinking" if enabled else "chat")
    out["chat_template_kwargs"] = template_kwargs
    out["reasoning_effort"] = "high" if enabled else "none"
    for key in ("thinking", "enable_thinking", "thinking_mode", "think"):
        out.pop(key, None)
    return out


def _is_empty_completion(result) -> bool:
    """Public API contract; production-derived narrative omitted."""
    if not isinstance(result, dict):
        return True
    choices = result.get("choices")
    if not isinstance(choices, list) or not choices:
        return True
    choice = choices[0]
    if not isinstance(choice, dict):
        return True
    msg = choice.get("message")
    if not isinstance(msg, dict):
        return True
    if msg.get("tool_calls"):
        return False
    return not str(msg.get("content") or "").strip()


def _strict_schema_requested(body) -> bool:
    """Public API contract; production-derived narrative omitted."""
    response_format = body.get("response_format") if isinstance(body, dict) else None
    if not isinstance(response_format, dict) or response_format.get("type") != "json_schema":
        return False
    json_schema = response_format.get("json_schema")
    return isinstance(json_schema, dict) and json_schema.get("strict") is True


def _normalize_stage_a_notes(parsed):
    """Public API contract; production-derived narrative omitted."""
    if not isinstance(parsed, dict) or set(parsed) != {"intents"}:
        raise EntryContractError("Stage A output must contain only intents")
    if not isinstance(parsed["intents"], list):
        raise EntryContractError("Stage A intents must be an array")
    note_keys = ("thesis_note", "target_dte_note")
    baseline = {"intents": []}
    normalized = {"intents": []}
    edits = []
    for index, row in enumerate(parsed["intents"]):
        if not isinstance(row, dict):
            raise EntryContractError("Stage A intent must be an object")
        original = {key: value for key, value in row.items() if key not in note_keys}
        baseline["intents"].append(original)
        final = dict(original)
        keys = [key for key in note_keys if key in row]
        if keys:
            if not isinstance(original.get("thesis"), str):
                raise EntryContractError("Stage A thesis must be a string before note folding")
            for key in keys:
                if not isinstance(row[key], str) or not row[key].strip():
                    raise EntryContractError("Stage A explanatory notes must be nonempty strings")
            final["thesis"] = original["thesis"] + "".join("\n\n" + row[key] for key in keys)
            edits.append({"intent_index": index, "folded_keys": keys})
        normalized["intents"].append(final)



    parse_stage_a(json.dumps(baseline, allow_nan=False))
    parse_stage_a(json.dumps(normalized, allow_nan=False))
    return normalized, edits


def _validate_native_schema_response(body, result):
    """Public API contract; production-derived narrative omitted."""
    import jsonschema
    contract = body.get("response_format", {}).get("json_schema", {})
    schema = contract.get("schema")
    allowed = {"$schema", "$id", "title", "description", "type", "enum", "const",
               "properties", "required", "additionalProperties", "items", "prefixItems",
               "minItems", "maxItems", "uniqueItems", "contains", "minContains", "maxContains",
               "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
               "minLength", "maxLength", "pattern", "anyOf", "allOf", "oneOf", "not",
               "minProperties", "maxProperties", "if", "then", "else"}
    def check_known(node):
        if isinstance(node, bool):
            return
        if not isinstance(node, dict) or set(node) - allowed:
            raise ValueError("unsupported JSON Schema form or keyword")
        if "$schema" in node and node["$schema"] != "https://json-schema.org/draft/2020-12/schema":
            raise ValueError("unsupported JSON Schema dialect")
        for key in ("properties",):
            for child in node.get(key, {}).values():
                check_known(child)
        for key in ("items", "additionalProperties", "contains", "not", "if", "then", "else"):
            if key in node:
                check_known(node[key])
        for key in ("prefixItems", "anyOf", "allOf", "oneOf"):
            for child in node.get(key, []):
                check_known(child)
    def unique(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError("duplicate JSON key")
            obj[key] = value
        return obj
    def no_constant(value):
        raise ValueError("nonfinite JSON constant: " + value)
    def finite_float(raw):
        import math as _finite_math
        value = float(raw)
        if not _finite_math.isfinite(value):
            raise ValueError("nonfinite JSON number")
        return value
    try:
        if contract.get("strict") is not True or not isinstance(schema, dict):
            raise ValueError("missing strict JSON Schema")
        check_known(schema)
        jsonschema.Draft202012Validator.check_schema(schema)
        choices = result.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise ValueError("ambiguous completion choices")
        choice = choices[0]
        message = choice.get("message", {})
        if choice.get("finish_reason") != "stop" or message.get("tool_calls") or message.get("refusal"):
            raise ValueError("completion was truncated or did not return plain JSON")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("empty JSON content")
        parsed = json.loads(content, object_pairs_hook=unique, parse_constant=no_constant,
                            parse_float=finite_float)
        normalization = []
        if (contract.get("name") == "stage_a_intents"
                and schema == _STAGE_A_JSON_SCHEMA):
            parsed, normalization = _normalize_stage_a_notes(parsed)
        jsonschema.Draft202012Validator(schema).validate(parsed)
    except Exception as exc:
        raise ModelRouteUnavailable("strict local JSON Schema validation failed: " + str(exc)[:500]) from exc
    if normalization:
        normalized_content = json.dumps(parsed, ensure_ascii=False, allow_nan=False)


        result["_trader_stage_a_normalization"] = {
            "mode": "verbatim_explanatory_notes_v1",
            "original_content": content,
            "original_content_sha256": provenance.sha256(content),
            "normalized_content_sha256": provenance.sha256(normalized_content),
            "edits": normalization,
        }
        message["content"] = normalized_content
    result["_trader_schema_enforcement"] = {
        "mode": "local_json_schema_validation",
        "native_grammar_constrained": False,
        "native_warning": "response_format was not enforced during generation",
        "schema_sha256": provenance.sha256(schema),
    }


def _runtime_snapshot_with_retry(endpoint, attempts=3, delay=0.2, *, deadline=None):
    """Public API contract; production-derived narrative omitted."""
    last = None
    for attempt in range(attempts):
        try:
            if deadline is None:
                return provenance.runtime_snapshot(endpoint)

            def identity_opener(request, timeout):
                response = urllib.request.urlopen(
                    request, timeout=min(timeout, _remaining_model_time(deadline)))
                return _DeadlineResponse(response, deadline)

            result = provenance.runtime_snapshot(
                endpoint, timeout=min(3.0, _remaining_model_time(deadline)),
                opener=identity_opener)
            _remaining_model_time(deadline)
            return result
        except provenance.RuntimeIdentityError as exc:
            last = exc
            if attempt < attempts - 1:
                if deadline is None:
                    time.sleep(delay)
                else:
                    _model_retry_wait(delay, deadline)
    raise last


def _post_json_one(endpoint, body, timeout, retries=5, backoff=None, return_identity=False,
                   *, _deadline=None):
    """Public API contract; production-derived narrative omitted."""
    _env_r = os.environ.get("SLATE_POST_RETRIES")
    if _env_r and _env_r.isdigit():
        retries = max(retries, int(_env_r))
    data = json.dumps(body).encode()

    editorial_deadline = (_deadline if _deadline is not None
                          else time.monotonic() + float(timeout))
    _remaining_model_time(editorial_deadline)
    before = None
    identity_error = None
    if return_identity:
        try:
            before = _runtime_snapshot_with_retry(endpoint, deadline=editorial_deadline)
        except provenance.RuntimeIdentityError as exc:
            identity_error = str(exc)
            if provenance.identity_required():
                raise
    last = None
    for attempt in range(retries):
        _remaining_model_time(editorial_deadline)
        accepted_body = body
        priority = int(os.environ.get("TRADER_LLM_PRIORITY", "1"))
        headers = {"Content-Type": "application/json"}
        headers.update(provenance.priority_headers(priority))
        req = urllib.request.Request(endpoint, data=data, headers=headers)
        try:
            lease = (trader_glm_lease(_remaining_model_time(editorial_deadline))
                     if endpoint == GLM_ENDPOINT and body.get("model") in (GLM_MODEL, _DSV41_PRIMARY_MODEL)
                     else nullcontext())
            with lease:
                with urllib.request.urlopen(
                        req, timeout=_remaining_model_time(editorial_deadline)) as r:
                    warning_headers = getattr(r, "headers", None)
                    warning = (warning_headers.get("Warning", "")
                               if warning_headers is not None else "")
                    payload = _read_model_response(r, editorial_deadline)
                result = json.loads(payload.decode(), strict=False)
                if _strict_schema_requested(body):
                    if endpoint == _GLM_PRIMARY_ENDPOINT and body.get("model") == _DSV41_PRIMARY_MODEL:
                        try:
                            _validate_native_schema_response(body, result)
                        except ModelRouteUnavailable:
                            from exitmgr import native_text_recovery


                            def post_editorial_once(revision, remaining):
                                revision_req = urllib.request.Request(
                                    endpoint, data=json.dumps(revision).encode(), headers=headers)
                                with urllib.request.urlopen(revision_req, timeout=remaining) as reply:
                                    raw = _read_model_response(reply, editorial_deadline)
                                return json.loads(raw.decode(), strict=False)
                            try:
                                result, accepted_body = native_text_recovery.recover(
                                    body, result, _STAGE_A_JSON_SCHEMA, parse_stage_a,
                                    _validate_native_schema_response, post_editorial_once,
                                    editorial_deadline, runtime_before=before)
                            except (native_text_recovery.RecoveryError, OSError, ValueError) as exc:


                                raise ModelRouteUnavailable(str(exc)) from exc
                    elif "not enforced" in str(warning).lower():
                        raise ModelRouteUnavailable(
                            f"{endpoint} did not enforce the requested strict JSON schema")



                if _is_empty_completion(result):
                    if attempt < retries - 1:
                        wait = backoff if backoff is not None else _BUSY_BACKOFFS[
                            min(attempt, len(_BUSY_BACKOFFS) - 1)]
                        print(f"[strategist] EMPTY reply (no content, no tool_calls), "
                              f"retry {attempt + 1}/{retries - 1} in {wait}s")
                        _model_retry_wait(wait, editorial_deadline)
                        continue
                    raise ModelRouteUnavailable(
                        f"{endpoint} returned an empty completion after {retries} attempt(s)")
                if not return_identity:
                    return result
                if before is not None:
                    try:
                        after = _runtime_snapshot_with_retry(endpoint, deadline=editorial_deadline)
                        return result, provenance.request_identity(
                            endpoint=endpoint, body=accepted_body, response=result, before=before, after=after)
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
                    "request_sha256": provenance.sha256(accepted_body),
                    "response_sha256": provenance.sha256(result),
                }
        except (LeaseError, FileExistsError) as e:


            _remaining_model_time(editorial_deadline)
            raise ModelRouteUnavailable("shared GLM slot is already owned") from e
        except urllib.error.HTTPError as e:
            last = e
            retryable_status = e.code == 429 or 500 <= e.code <= 599
            if retryable_status and attempt < retries - 1:
                wait = backoff if backoff is not None else _BUSY_BACKOFFS[min(attempt, len(_BUSY_BACKOFFS) - 1)]
                print(f"[strategist] model busy ({e.code}), retry {attempt + 1}/{retries - 1} in {wait}s")
                _model_retry_wait(wait, editorial_deadline)
                continue
            if retryable_status:
                raise ModelRouteUnavailable(
                    f"{endpoint} remained unavailable with HTTP {e.code}") from e
            raise
        except (json.JSONDecodeError, UnicodeDecodeError, http.client.HTTPException) as e:
            last = e
            if attempt < retries - 1:
                wait = backoff if backoff is not None else _BUSY_BACKOFFS[
                    min(attempt, len(_BUSY_BACKOFFS) - 1)]
                print(f"[strategist] invalid/truncated model response "
                      f"({type(e).__name__}), retry {attempt + 1}/{retries - 1} in {wait}s")
                _model_retry_wait(wait, editorial_deadline)
                continue
            raise ModelRouteUnavailable(
                f"{endpoint} returned an invalid/truncated response "
                f"({type(e).__name__})") from e
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            last = e
            if attempt < retries - 1:
                wait = backoff if backoff is not None else _BUSY_BACKOFFS[min(attempt, len(_BUSY_BACKOFFS) - 1)]
                print(f"[strategist] connection error ({type(e).__name__}), retry {attempt + 1}/{retries - 1} in {wait}s")
                _model_retry_wait(wait, editorial_deadline)
                continue
            raise ModelRouteUnavailable(
                f"{endpoint} remained unavailable ({type(e).__name__})") from e
    if last:
        raise ModelRouteUnavailable(f"{endpoint} remained unavailable") from last


def _post_json(endpoint, body, timeout, retries=5, backoff=None, return_identity=False,
               response_validator=None):
    """Public API contract; production-derived narrative omitted."""
    deadline = time.monotonic() + float(timeout)
    _remaining_model_time(deadline)
    primary_body = ( _glm_request_body(body)
                    if endpoint == _GLM_PRIMARY_ENDPOINT
                    and body.get("model") == _GLM_PRIMARY_MODEL
                    else dict(body))
    if endpoint == _GLM_PRIMARY_ENDPOINT and body.get("model") == _DSV41_PRIMARY_MODEL:
        primary_body = _dsv41_request_body(body)
    routes = [(endpoint, primary_body, "primary")]




    glm_only = os.environ.get("TRADER_GLM_ONLY", "").strip().lower() in (
        "1", "true", "yes", "on",
    )
    if (endpoint == _GLM_PRIMARY_ENDPOINT
            and body.get("model") in (_GLM_PRIMARY_MODEL, _DSV41_PRIMARY_MODEL)
            and not glm_only):
        fallback_body = dict(body)
        fallback_body["model"] = _DEEPSEEK_FALLBACK_MODEL
        if body.get("model") == _DSV41_PRIMARY_MODEL:
            fallback_body = _dsv41_request_body(fallback_body, spark_fallback=True)
        routes.append((_DEEPSEEK_FALLBACK_ENDPOINT, fallback_body, "deepseek-fallback"))

    route_errors = []

    def route_failure(route, error):
        message = str(error).replace("\n", " ")[:500]
        return f"{route}: {type(error).__name__}: {message}"

    for route_endpoint, route_body, label in routes:
        try:
            posted = _post_json_one(
                route_endpoint, route_body, _remaining_model_time(deadline),
                retries=retries, backoff=backoff,
                return_identity=return_identity, _deadline=deadline)
            if response_validator is not None:
                try:
                    response_validator(posted)
                except EntryContractError as exc:
                    raise ModelRouteUnavailable(
                        "exact model returned contract-invalid output: "
                        f"{str(exc)[:500]}"
                    ) from exc
            _remaining_model_time(deadline)
            return posted
        except ModelDeadlineExceeded:
            raise
        except (ModelRouteUnavailable, provenance.RuntimeIdentityError) as exc:
            route_errors.append((label, exc))
            if label == "primary" and len(routes) > 1:
                print("[strategist] exact trader host route unavailable; using exact DeepSeek Spark fallback")
                continue
            if len(route_errors) == 1:
                raise
            detail = "; ".join(
                route_failure(route, error)
                for route, error in route_errors
            )
            raise ModelRouteUnavailable(
                f"all exact trader model routes unavailable ({detail})"
            ) from exc
        except urllib.error.HTTPError as exc:




            if label == "primary":
                raise
            route_errors.append((label, exc))
            detail = "; ".join(
                route_failure(route, error)
                for route, error in route_errors
            )
            raise ModelRouteUnavailable(
                f"all exact trader model routes unavailable ({detail})"
            ) from exc
    raise ModelRouteUnavailable("all exact trader model routes unavailable")


def _resolve_thinking(default):
    """Public API contract; production-derived narrative omitted."""
    v = os.environ.get("SLATE_THINKING")
    return v if v in ("enabled", "disabled", "adaptive") else default


def _thinking_kwargs(think):
    """Public API contract; production-derived narrative omitted."""
    on = str(think).strip().lower() in ("enabled", "true", "1", "on", "adaptive")
    return {"chat_template_kwargs": {"thinking": on}}


def _read_cot(message):
    """Public API contract; production-derived narrative omitted."""
    if not isinstance(message, dict):
        return None
    return message.get("reasoning_content") or message.get("reasoning") or None


def _two_stage_message(posted, return_identity):
    """Public API contract; production-derived narrative omitted."""
    d, identity = posted if return_identity else (posted, None)
    try:
        message = d["choices"][0]["message"]
        content = message.get("content") or ""
        cot = _read_cot(message)
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise EntryContractError(f"malformed model response envelope: {exc}") from exc
    if not isinstance(content, str):
        raise EntryContractError("model response content must be a string")
    return content, cot, identity


def _two_stage_return(value, content, cot, identity, *, return_raw, return_cot,
                      return_identity):
    """Public API contract; production-derived narrative omitted."""
    if return_identity:
        return value, content, cot, identity
    if return_cot:
        return value, content, cot
    if return_raw:
        return value, content
    return value


def reconsider_same_day_earnings(endpoint: str, model: str, market_context: str,
                                 trade_context: dict, timeout: int = 300,
                                 thinking: str = "disabled", return_raw: bool = False,
                                 return_cot: bool = False, return_identity: bool = False):
    """Public API contract; production-derived narrative omitted."""
    if not isinstance(market_context, str) or not market_context.strip():
        raise EntryContractError("same-day earnings review requires market context")
    if not isinstance(trade_context, dict) or not trade_context:
        raise EntryContractError("same-day earnings review requires exact trade context")
    try:
        user_content = json.dumps(
            {"market_context": market_context, "trade": trade_context},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise EntryContractError(
            f"same-day earnings review input is not canonical JSON: {exc}") from exc
    think = _resolve_thinking(thinking)
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SAME_DAY_EARNINGS_REVIEW_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": 12000 if think == "enabled" else 700,
        "temperature": 0.0,
        "thinking": think,
        **_thinking_kwargs(think),
    }
    if structured_output_enabled():
        body["response_format"] = _same_day_earnings_response_format()

    def validate_review(posted):
        candidate_content, _candidate_cot, _candidate_identity = _two_stage_message(
            posted, return_identity)
        parse_same_day_earnings_review(candidate_content)

    posted = _post_json(
        endpoint, body, timeout, return_identity=return_identity,
        response_validator=validate_review)
    content, cot, identity = _two_stage_message(posted, return_identity)
    review = parse_same_day_earnings_review(content)
    return _two_stage_return(
        review, content, cot, identity, return_raw=return_raw, return_cot=return_cot,
        return_identity=return_identity)


def _stage_a_prompt(*, recommend: bool, ticker: Optional[str]) -> str:
    if ticker is None:
        return STAGE_A_RECOMMEND_PROMPT if recommend else STAGE_A_SYSTEM_PROMPT
    symbol = str(ticker).strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9.]{0,7}", symbol):
        raise EntryContractError(f"invalid Stage A ticker constraint: {ticker!r}")
    return (
        "You are an options swing-trading strategist for a small account. Return at most one trade "
        f"intent, and it must be for {symbol}; do not emit any other underlying. If the evidence "
        "does not support real alpha on that name, return the exact terminal decline. "
        + _STAGE_A_UNIVERSE + "\n" + _REGIME + "\n" + _DOCTRINE + "\n" + _SCORING + "\n" + _STAGE_A_CONTRACT
    )


def propose_intents(endpoint: str, model: str, market_context: str, timeout: int = 300,
                    recommend: bool = False, thinking: str = None,
                    return_raw: bool = False, return_cot: bool = False,
                    return_identity: bool = False, ticker: Optional[str] = None):
    """Public API contract; production-derived narrative omitted."""
    symbol = str(ticker).strip().upper() if ticker is not None else None
    prompt = _stage_a_prompt(recommend=recommend, ticker=symbol)







    think = thinking if thinking is not None else "enabled"
    think = _resolve_thinking(think)











    max_tokens = ((24000 if think == "enabled" else 2000) if (recommend or symbol)
                  else (16000 if think == "enabled" else 1400))
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": market_context},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.4,
        "thinking": think,
        **_thinking_kwargs(think),
    }
    if structured_output_enabled():
        body["response_format"] = _stage_a_response_format()
    def validate_stage_a(posted):
        candidate_content, _candidate_cot, _candidate_identity = _two_stage_message(
            posted, return_identity)
        candidate_intents = parse_stage_a(candidate_content)
        if symbol is not None:
            if len(candidate_intents) > 1:
                raise EntryContractError(
                    f"ticker-constrained Stage A returned {len(candidate_intents)} intents; "
                    "maximum is one")
            if any(intent.underlying != symbol for intent in candidate_intents):
                got = ", ".join(intent.underlying for intent in candidate_intents)
                raise EntryContractError(
                    f"ticker-constrained Stage A requested {symbol} but returned {got}")

    posted = _post_json(
        endpoint, body, timeout, return_identity=return_identity,
        response_validator=validate_stage_a,
    )
    content, cot, identity = _two_stage_message(posted, return_identity)
    intents = parse_stage_a(content)
    if symbol is not None:
        if len(intents) > 1:
            raise EntryContractError(
                f"ticker-constrained Stage A returned {len(intents)} intents; maximum is one")
        if any(intent.underlying != symbol for intent in intents):
            got = ", ".join(intent.underlying for intent in intents)
            raise EntryContractError(
                f"ticker-constrained Stage A requested {symbol} but returned {got}")
    return _two_stage_return(
        intents, content, cot, identity, return_raw=return_raw, return_cot=return_cot,
        return_identity=return_identity)


def _stage_b_payload(intent: StageAIntent, candidates: List[RuntimeCandidate],
                     intent_id: Optional[str]):
    """Public API contract; production-derived narrative omitted."""
    if not isinstance(intent, StageAIntent):
        raise EntryContractError("Stage B intent must be a StageAIntent")
    candidate_list = list(candidates or [])
    if not 3 <= len(candidate_list) <= 5:
        raise EntryContractError(
            f"Stage B requires 3 to 5 runtime candidates, got {len(candidate_list)}")
    if any(not isinstance(candidate, RuntimeCandidate) for candidate in candidate_list):
        raise EntryContractError("every Stage B candidate must be a RuntimeCandidate")

    intent_dict = intent.to_dict()
    candidate_dicts = [candidate.to_dict() for candidate in candidate_list]
    candidate_ids = [candidate["candidate_id"] for candidate in candidate_dicts]
    if len(set(candidate_ids)) != len(candidate_ids):
        raise EntryContractError("Stage B candidate_id values must be unique")
    candidate_intent_ids = {candidate["intent_id"] for candidate in candidate_dicts}
    if len(candidate_intent_ids) != 1:
        raise EntryContractError("all Stage B candidates must share one intent_id")
    supplied_intent_id = next(iter(candidate_intent_ids))
    if intent_id is not None and str(intent_id) != supplied_intent_id:
        raise EntryContractError(
            f"Stage B intent_id mismatch: {intent_id!r} != {supplied_intent_id!r}")




    checked_candidates = validate_candidates(supplied_intent_id, intent, candidate_list)
    candidate_dicts = [candidate.to_dict() for candidate in checked_candidates]

    for candidate in candidate_dicts:
        for key in ("underlying", "side", "direction", "structure"):
            if candidate[key] != intent_dict[key]:
                raise EntryContractError(
                    f"Stage B candidate {candidate['candidate_id']} {key} does not match intent")
    payload = {
        "intent_id": supplied_intent_id,
        "intent": intent_dict,
        "candidates": candidate_dicts,
    }
    return payload, candidate_list, dict(zip(candidate_ids, candidate_list))


def select_candidate(endpoint: str, model: str, intent: StageAIntent,
                     candidates: List[RuntimeCandidate], timeout: int = 300,
                     thinking: str = "enabled",
                     return_raw: bool = False,
                     return_cot: bool = False, return_identity: bool = False,
                     intent_id: Optional[str] = None):
    """Public API contract; production-derived narrative omitted."""
    payload, candidate_list, by_id = _stage_b_payload(intent, candidates, intent_id)
    try:
        user_content = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise EntryContractError(f"Stage B input is not canonical JSON: {exc}") from exc
    think = _resolve_thinking(thinking)
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": STAGE_B_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": 12000 if think == "enabled" else 400,
        "temperature": 0.0,
        "thinking": think,
        **_thinking_kwargs(think),
    }
    def validate_stage_b(posted):
        candidate_content, _candidate_cot, _candidate_identity = _two_stage_message(
            posted, return_identity)
        parse_stage_b(candidate_content, candidate_list)

    posted = _post_json(
        endpoint, body, timeout, return_identity=return_identity,
        response_validator=validate_stage_b,
    )
    content, cot, identity = _two_stage_message(posted, return_identity)
    selected_id = parse_stage_b(content, candidate_list)
    selected = None if selected_id is None else by_id.get(selected_id)
    if selected_id is not None and selected is None:


        raise EntryContractError("Stage B parser returned an unsupplied candidate_id")
    return _two_stage_return(
        selected, content, cot, identity, return_raw=return_raw, return_cot=return_cot,
        return_identity=return_identity)


def propose(endpoint: str, model: str, market_context: str, timeout: int = 300,
            recommend: bool = False, thinking: str = None, return_raw: bool = False,
            return_cot: bool = False, return_identity: bool = False):
    """Public API contract; production-derived narrative omitted."""
    think = thinking if thinking is not None else ("enabled" if recommend else "disabled")
    think = _resolve_thinking(think)


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
        **_thinking_kwargs(think),
    }
    posted = _post_json(endpoint, body, timeout, return_identity=return_identity)
    d, identity = posted if return_identity else (posted, None)
    _msg = d["choices"][0]["message"]
    content = _msg.get("content") or ""
    cot = _read_cot(_msg)
    ideas = parse_ideas(content)
    if return_identity:
        return (ideas, content, cot, identity)
    if return_cot:
        return (ideas, content, cot)
    return (ideas, content) if return_raw else ideas


SINGLE_PROMPT = (
    "You are an options swing-trading strategist for a SMALL account whose exact net liquidation value is stated in this brief -- size every trade from THAT figure, never from a remembered one. The user just added "
    "@@TICKER@@ to their watchlist and wants your SINGLE best option trade idea on @@TICKER@@ right "
    "now -- ONLY @@TICKER@@, no other names. Decide direction (bullish/bearish) and structure "
    "yourself from the market context. Score conviction HONESTLY 1-10; if you have no real edge on "
    "@@TICKER@@ today, score it low -- do not inflate. " + _UNIVERSE + "\n" + _REGIME + "\n" + _DOCTRINE + "\n" + _SCORING + "\n" + _CONTRACT
)


def propose_one(endpoint: str, model: str, market_context: str, ticker: str, timeout: int = 1800,
                thinking: str = "enabled", return_raw: bool = False, return_cot: bool = False,
                return_identity: bool = False):
    """Public API contract; production-derived narrative omitted."""
    prompt = SINGLE_PROMPT.replace("@@TICKER@@", ticker.upper())
    thinking = _resolve_thinking(thinking)
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": market_context},
        ],






        "max_tokens": 24000 if thinking == "enabled" else 6000,
        "temperature": 0.4,
        "thinking": thinking,
        **_thinking_kwargs(thinking),
    }
    posted = _post_json(endpoint, body, timeout, return_identity=return_identity)
    d, identity = posted if return_identity else (posted, None)
    _msg = d["choices"][0]["message"]
    content = _msg.get("content") or ""
    cot = _read_cot(_msg)
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
    "any name already being watched (listed below), and avoid Elon-Musk-linked "
    "names (SYMD is the ONE permitted Elon-derivative name). Flag any name with a scheduled "
    "binary catalyst (FDA decision, trial readout) in its reason. One short reason each.\n"
    'Respond with ONLY this JSON: {"candidates":[{"ticker":"<SYM>","reason":"<short>"}]}'
)
_TICKER_RE = re.compile(r"^[A-Z]{1,5}$")


def discover_names(endpoint: str, model: str, market_context: str, exclude, timeout: int = 600,
                   thinking: str = "enabled", blocked=None):
    """Public API contract; production-derived narrative omitted."""
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
        **_thinking_kwargs(thinking),
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





















def _is_gemma(model_name: str) -> bool:
    """Public API contract; production-derived narrative omitted."""
    return "gemma" in (model_name or "").lower()


def gemma_signal(endpoint: str, model: str, ticker: str, horizon_label: str = "~2 weeks",
                 timeout: int = 120, vix_series=None):
    """Public API contract; production-derived narrative omitted."""
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
