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
from exitmgr.entry_contract import (
    CONTRACT_VERSION,
    EntryContractError,
    RuntimeCandidate,
    StageAIntent,
    parse_stage_a,
    parse_stage_b,
    validate_candidates,
)

# 1-10 conviction rubric (Trevor 2026-06-12): 1-3 desperate-only, 4 below-avg, 5 middle,
# 6-8 medium confidence, 8-10 high confidence. Score HONESTLY -- a low score is fine and useful.
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
_UNIVERSE = (
    "Universe: SPY, QQQ, IWM, and liquid large-cap single names only. DO NOT propose "
    "Elon-Musk-linked companies (e.g. TSLA) -- they are rejected. SPCX is the "
    "ONE permitted Elon-derivative name (allowed). "
    # 2026-07-26 (Trevor): the blanket biotech/pharma ban is REPLACED by the mechanism it was
    # standing in for. The ban existed because biotech gaps had hurt him. Measured from the price
    # cache: large-cap pharma averages 0.1 down-10% sessions/yr with a worst day of -10.5%, while
    # the names already on the approved list average 5.7/yr at -22.2% -- i.e. the ban blocked the
    # safer half of the sector while permitting the riskier names outright. The real hazard is a
    # SCHEDULED BINARY EVENT that gaps through a stop overnight, which is a pipeline/market-cap
    # property, not a sector. Stated as a sizing-and-timing rule below rather than an exclusion,
    # deliberately: Trevor wants the high-volatility names and a gap gate would have cut them.
    "BINARY EVENTS: if a scheduled binary catalyst (FDA decision, trial readout, earnings) falls "
    "inside the intended hold, say so in the thesis and either decline or size for a gap THROUGH "
    "the stop -- an overnight gap fills wherever it opens, not at your stop level. This is about "
    "the event, not the sector. "
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

# SYMMETRY FIX 2026-08-11 (Trevor: "let it trade puts on downtrends").
#
# The previous wording was bull-only in effect. Measured over 150 byron setups, the model traded
# 93% of strong-uptrend tapes and 0 of 56 DOWNTRENDS -- it passed every falling tape rather than
# buying a put, even though `long put` and `put debit spread` are valid structures. The cause was
# this paragraph: "trade WITH the trend" was illustrated only with a bullish example, and "do not
# buy falling knives" reads as "do not act on a downtrend" rather than its actual meaning, "do not
# buy CALLS into a downtrend." Both directions now carry the same evidentiary bar.
_REGIME = (
    "ENTRY DISCIPLINE: before any directional idea, confirm the underlying's trend and the broad "
    "tape (SPY/QQQ) agree with it. Trade WITH the trend, in EITHER DIRECTION -- the trend is what "
    "must be confirmed, not the direction bullish.\n"
    "  * A confirmed UPTREND (underlying making higher highs, tape agreeing) supports a BULLISH "
    "idea: a call debit spread or long call.\n"
    "  * A confirmed DOWNTREND (underlying making lower lows, tape agreeing) supports a BEARISH "
    "idea with exactly the same standing: a PUT DEBIT SPREAD or long put. A sustained downtrend is "
    "a tradeable setup, not a reason to sit out. Do not skip it merely because it is short-side.\n"
    "FADING is the error, not direction. Do not buy calls into a falling tape hoping for a bounce "
    "('oversold', 'due to bounce'), and do not buy puts into a rising tape hoping for a top. "
    "'Buying a falling knife' means buying CALLS as something drops -- it does NOT mean declining "
    "to trade a downtrend. If the tape is genuinely choppy or directionless, prefer an empty slate; "
    "and prefer a defined-risk debit spread over a naked long in either direction."
)

# TREVOR'S DOCTRINE, STATED IN-PROMPT (2026-08-10).
#
# These three rules were previously carried by the fine-tuned model's own weights (the v6
# curriculum encodes them) and by downstream code that CLAMPS a violating idea. Neither applies
# to a general-purpose serving model such as deepseek-v4-flash, which arrives knowing none of
# them: it proposes off a generic rubric and the runtime silently repairs the result, so the
# journal records a trade nobody actually reasoned about. Stating them here makes the model
# reason INSIDE the doctrine instead of being corrected after the fact.
#
# Sources: conviction bar = the 629-case byron calibration; DTE scale = the 4-8x sliding rule;
# construction = the n=524 construction bake-off plus the 2024-26 book A/B.
_DOCTRINE = (
    "TREVOR'S DOCTRINE -- these are hard rules of this desk, not preferences.\n"
    "1. CONVICTION IS A SIZE INPUT, NOT A TAKE BAR. Score each idea 1-10 on absolute "
    "conviction and propose it whenever the setup is sound -- there is no minimum. The old "
    "conviction-6 floor was A/B tested on 1,365 paired setups and it DISCARDED trades that "
    "outperformed the ones it kept (mean GNA 0.491 and 0.595 thrown away, against 0.480 and "
    "0.574 retained), so it cost a quarter of the trade count and bought nothing. What "
    "conviction DOES predict is worth using: 7 and above returned 42.1% mean against 5.8% "
    "below it, at a 41.7% win rate against 31.4%, and that edge survives trimming the top and "
    "bottom decile -- so a 7+ is automatically SIZED UP by the risk layer. Reserve 7+ for "
    "setups you would genuinely bet more on; inflating conviction to buy size is the one way "
    "to abuse this. A 4 or 5 on a sound setup is a NORMAL trade, not a pass. Declining a "
    "genuinely good setup is as much an error as taking a marginal one.\n"
    "1b. AFFORDABILITY IS A SCREEN, NOT A SIZING STEP. The brief states Max debit per trade. Before you propose a name, satisfy yourself that a defined-risk spread on THAT underlying can be built for less than it. A rough test: a 0.60-delta debit spread typically costs on the order of 2% of the underlying's share price x 100, so a name roughly priced above HALF the Max debit per trade figure in this brief almost never fits. Use the figure in the brief, never a remembered one -- it moves with the account. Proposing an unfundable name is a wasted slot, not a near miss: a $1,627/share name was proposed on 2026-08-14 and its CHEAPEST real spread priced at $1,400, so no strike pair could have worked. Prefer names cheap enough that the budget buys real structure.\n"
    "2. EXPIRY IS A 5-8x WINDOW ON YOUR OWN INTENDED HOLD, and you must do the arithmetic before "
    "you answer. For any DEBIT structure: target_dte must land between 5x and 8x "
    "intended_hold_days. Compute both ends explicitly -- 5 x intended_hold_days and "
    "8 x intended_hold_days -- and place target_dte inside that window before emitting the intent. "
    "8x is the default; the tighter end of the window is EARNED, never assumed. NOTHING below 5x, "
    "ever: if your target_dte is under 5 x intended_hold_days, RAISE it (or shorten "
    "intended_hold_days) rather than emit the smaller number. Worked example: intended_hold_days 8 "
    "means target_dte belongs in 40-64; a 30 is a VIOLATION and is rejected. You buy far more time "
    "than you intend to use because you are paying theta the whole way -- being long-dated relative "
    "to the hold carries the large majority of the measured edge, more than which structure you "
    "pick. State in the thesis the multiple you used. This rule governs DEBITS only: a "
    "CASH-SECURED PUT is the deliberate inverse -- theta works for the seller there, so weeklies "
    "(3-45 DTE) are correct process, never a violation. "
    "REACH FOR THE LONGEST HOLD THE BUDGET ALLOWS. The 5-8x window is a floor on expiry, not a target for the hold: nothing caps target_dte below 800, so when a name is cheap enough that a long-dated spread still fits under Max debit per trade, intend a LONGER hold and let the multiple carry target_dte with it -- an intended_hold_days of 45-90 puts target_dte in LEAPS territory (300-720). Long-dated is where the measured edge lives, and on a cheap underlying it costs little to buy far more time than you expect to use. Only fall back to a shorter hold when the premium for a long-dated structure will not fit the cap.\n"
    "2b. BUY TIME, THEN DO NOT USE IT -- PUSH target_dte WELL PAST YOUR HOLD. Theta is what "
    "you pay for being near-dated and you pay it whether or not the move arrives, so buy far "
    "more time than you intend to use and close into strength rather than riding to the "
    "horizon. Keep intended_hold_days SHORT; never lengthen the hold to justify the expiry. "
    "This exact instruction was A/B tested on 759 paired setups against the identical model "
    "with only this guidance differing: it moved grid-normalized alpha +10.7 points "
    "(p<0.0001) and win rate 31.5% -> 44.8%, while direction skill did not change at all "
    "(p=0.77) -- the entire gain was expiry choice, not better forecasting. In the live "
    "manual book the trades that carried the P&L ran a median 590 DTE held a median 8 days. "
    "CHOOSE THE EXPIRY PER SETUP -- DO NOT EMIT A CONSTANT. A run where every trade carries "
    "the same target_dte is a FAILURE, not compliance: it means you stopped pricing the "
    "trade and started copying a number. Liquidity, the chain actually listed, premium "
    "against Max debit per trade, and how far out the thesis plausibly runs all differ by "
    "name, so the expiry should differ too. Reason to a figure; do not multiply your way to "
    "one. "
    "CAUTION the A/B could not see: the eight WORST trades in that book share this profile "
    "(long calls, median 197 DTE, worst -$13,693). The construction produces both tails, so "
    "the -30% stop is what makes it survivable and is never optional. "
    "3. CONSTRUCTION: the default build is a LONG-DATED DEBIT SPREAD in the direction the trend "
    "confirms -- a CALL debit spread on a confirmed uptrend, a PUT debit spread on a confirmed "
    "downtrend. Everything below applies identically to both; read 'bull call spread' as 'the "
    "spread on the side the trend supports'. The short leg "
    "finances part of the long, so the spread is the cheap way to enter long-dated bullish "
    "exposure -- it is a capital-efficiency choice, not an aversion to naked calls. A long-dated "
    "naked long call is also acceptable. What is NOT acceptable is choosing between them as a "
    "function of conviction: conviction-gated structure mixing was tested on the 2024-26 book and "
    "was dominated by both pure doctrines. Pick the structure on the trade's economics, never on "
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

# Conservative mode (the 15-min loop): silence is allowed.
SYSTEM_PROMPT = (
    "You are a disciplined options swing-trading strategist for a SMALL account. Propose 0-3 trades "
    "you have genuine conviction on, or an empty list if nothing is compelling -- never force trades. "
    + _UNIVERSE + "\n" + _REGIME + "\n" + _DOCTRINE + "\n" + _SCORING + "\n" + _CONTRACT
)

# Recommend mode (the daily slate): ALWAYS surface your best ideas, scored honestly.
RECOMMEND_PROMPT = (
    "You are an options swing-trading strategist for a SMALL account whose exact net liquidation value is stated in this brief -- size every trade from THAT figure, never from a remembered one. Recommend your "
    "BEST 1-3 option trade ideas for today. ALWAYS give at least one idea unless the market is "
    "genuinely untradeable -- it is fine to include moderate or weak ideas, just score them "
    "honestly so the human can judge. " + _UNIVERSE + "\n" + _REGIME + "\n" + _DOCTRINE + "\n" + _SCORING + "\n" + _CONTRACT
)


# --------------------------------------------------------------------- TWO-STAGE ENTRY CONTRACT
#
# This is deliberately additive.  The legacy TradeIdea contract above remains the compatibility
# surface for intraday scouting, reload tickets, CLI/manual routes, and their existing tests.  New
# live-entry callers use Stage A/B below; they never route a Stage A answer through parse_ideas(),
# whose historical normalisation intentionally repairs fields that stage-ab.v3 must instead reject.
STAGE_AB_CONTRACT_VERSION = CONTRACT_VERSION

_STAGE_A_UNIVERSE = (
    "Universe: SPY, QQQ, IWM, and liquid US single names permitted by the supplied market brief. "
    "Do not propose Elon-Musk-linked companies (for example TSLA); SPCX is the one permitted "
    "Elon-derivative name. Scheduled binary catalysts inside the intended hold must be stated in "
    "the thesis and reflected in conviction and allocation. "
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
    "alpha and thesis must both be non-empty: alpha names the expected edge, while thesis states "
    "the source-bound trade case and invalidation logic. The allocation is intent only; runtime "
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

# STAGE A GUIDED DECODING (2026-08-10) -- opt-in via TRADER_STRUCTURED_OUTPUT=1.
#
# Measured on deepseek-v4-flash over 10 slate cycles: 8 produced fully doctrine-compliant
# intents and 2 died on a DUPLICATE JSON KEY ("underlying" once, "conviction" once). The Stage A
# parser rejects duplicate keys deliberately, so each of those cost a whole cycle. Restating the
# rule in the prompt did NOT fix it -- the repeated key simply moved -- because it is a decoding
# artifact, not a comprehension failure.
#
# vLLM constrains generation to a JSON Schema (xgrammar) when the request carries response_format,
# which makes a duplicate key structurally unrepresentable rather than merely forbidden. The schema
# below mirrors entry_contract.parse_stage_a EXACTLY, including additionalProperties: false, so it
# forbids the same model-authored execution fields (est_debit_usd, strike, expiry, ...) the parser
# rejects. It is a decode-time restatement of the contract, never a relaxation of it: the parser
# still runs afterwards and remains the authority.
#
# Gated behind an env flag because it is server-dependent -- the custom m3_serve path has no such
# support and must keep sending an unconstrained body.
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
                    "alpha": {"type": "string", "minLength": 1},
                    "thesis": {"type": "string", "minLength": 1},
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
    """True when the serving engine should be asked to constrain Stage A to the schema."""
    value = os.environ.get("TRADER_STRUCTURED_OUTPUT", "0").strip().lower()
    return value not in ("0", "false", "no", "off", "")


def _stage_a_response_format():
    return {"type": "json_schema",
            "json_schema": {"name": "stage_a_intents", "strict": True,
                            "schema": _STAGE_A_JSON_SCHEMA}}


STAGE_B_SYSTEM_PROMPT = (
    f"You are the Stage B selector for {STAGE_AB_CONTRACT_VERSION}. Runtime has already rejected "
    "stale, one-sided, structurally invalid, illiquid, over-cap, and unaffordable contracts. Review "
    "the supplied immutable Stage A intent and its three to five runtime-priced candidates. Select "
    "only a supplied candidate whose executable economics best express that intent, or decline. "
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


def _require_finite(t: dict, key: str) -> float:
    """Required and finite. Sign is PRESERVED -- the caller applies abs().

    CLAUDE FIX 2026-07-26. `json.loads` accepts a bare `NaN` token, and `min(1.0, abs(nan))`
    returns 1.0 because `nan < 1.0` is False and min() keeps its first argument. A NaN
    target_delta therefore became delta 1.0 -- the deepest ITM, most expensive contract on the
    board -- indistinguishable from the model deliberately asking for it. Verified empirically
    before this fix. Raising here routes it into parse_ideas' existing except-clause, which
    drops the idea, matching how every other malformed numeric field in this file behaves.

    NOT _require_positive: puts quote a negative delta and the caller takes abs(); requiring
    positivity would silently drop every put idea.
    """
    _reject_bool(t[key], key)    # KeyError propagates == dropped
    v = float(t[key])            # TypeError / ValueError propagate == dropped
    if not math.isfinite(v):
        raise ValueError("%s must be a finite number, got %r" % (key, t[key]))
    return v


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


def _is_empty_completion(result) -> bool:
    """True when a 200 carried no usable answer: no choices, or blank content AND no tool calls.

    Blank content WITH tool_calls is ordinary function calling and is NOT empty -- retrying that
    would re-issue a valid tool call. Reasoning-only replies (content blank, reasoning_content
    populated, no tool call) DO count as empty: the parser cannot use them either.
    """
    try:
        choices = (result or {}).get("choices") or []
        if not choices:
            return True
        msg = (choices[0] or {}).get("message") or {}
        if msg.get("tool_calls"):
            return False
        return not str(msg.get("content") or "").strip()
    except Exception:
        return False        # never let the guard itself fail a good response


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
                # EMPTY-REPLY RETRY (2026-08-14). A 200 with no content and no tool call is a
                # silent nothing -- indistinguishable downstream from "the model had no ideas".
                # Treated like the 503-busy case: retry within the existing bounded backoff.
                if _is_empty_completion(result) and attempt < retries - 1:
                    wait = backoff if backoff is not None else _BUSY_BACKOFFS[
                        min(attempt, len(_BUSY_BACKOFFS) - 1)]
                    print(f"[strategist] EMPTY reply (no content, no tool_calls), "
                          f"retry {attempt + 1}/{retries - 1} in {wait}s")
                    time.sleep(wait)
                    continue
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


def _thinking_kwargs(think):
    """Request fields that actually switch model thinking on/off, for BOTH serving stacks.

    The bare top-level `"thinking"` key is m3_serve_batched's private extension. vLLM ACCEPTS
    unknown body fields silently (no 400), so after the 2026-08-10 cutover to deepseek-v4-flash
    every "thinking: enabled" call was a no-op: vLLM was launched with
    `--default-chat-template-kwargs {"thinking": false}`, so the slate and research paths -- the
    ones designed to reason deeply -- ran flat, and `reasoning` came back null on every request,
    so the captured CoT was null for every decision. vLLM's real switch is
    `chat_template_kwargs`, verified live 2026-08-11: with it set, DeepSeek returns a full
    reasoning trace; without it, nothing.

    Emitting BOTH keys keeps the custom M3 server working unchanged if it is ever served again.
    """
    on = str(think).strip().lower() in ("enabled", "true", "1", "on", "adaptive")
    return {"chat_template_kwargs": {"thinking": on}}


def _read_cot(message):
    """Chain-of-thought from either serving stack, or None.

    m3_serve_batched publishes it as `reasoning_content`; vLLM's reasoning parser
    (`--reasoning-parser deepseek_v4`) publishes it as `reasoning`. Reading only the former
    silently dropped every DeepSeek trace.
    """
    if not isinstance(message, dict):
        return None
    return message.get("reasoning_content") or message.get("reasoning") or None


def _two_stage_message(posted, return_identity):
    """Return (content, reasoning_content, identity) from one OpenAI-compatible response.

    The legacy callers below retain their historical indexing behavior.  The stage-ab.v3 path is
    fail-closed: a malformed envelope is a contract failure, never an empty/declined trade.
    """
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
    """Preserve the additive capture tuple convention used by propose()/propose_one()."""
    if return_identity:
        return value, content, cot, identity
    if return_cot:
        return value, content, cot
    if return_raw:
        return value, content
    return value


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
    """Stage A: request strictly validated, price-free trade intents under stage-ab.v3.

    This is a new entry-only surface.  It intentionally does not call parse_ideas(), normalize an
    enum, repair a number, or derive a missing field.  Any extra/model-authored execution field is
    rejected by entry_contract.parse_stage_a().  ``ticker`` constrains the add-name route to zero or
    one intent for exactly that uppercase symbol.

    Return/capture shapes mirror propose(): a bare list by default, then (value, raw),
    (value, raw, cot), or (value, raw, cot, identity) as the corresponding flag is enabled.
    """
    symbol = str(ticker).strip().upper() if ticker is not None else None
    prompt = _stage_a_prompt(recommend=recommend, ticker=symbol)
    think = thinking if thinking is not None else (
        "enabled" if (recommend or symbol is not None) else "disabled")
    think = _resolve_thinking(think)
    # The continuous entry loop historically got 1400 tokens because it never reasoned. With
    # thinking ON the trace is emitted BEFORE the answer, so a 1400 budget would truncate the JSON
    # behind its own reasoning and fail the contract. 6000 covers an entry-sized trace plus the
    # answer with room to spare, and the loop runs every 1200s so the extra decode time is free.
    max_tokens = ((24000 if think == "enabled" else 2000) if (recommend or symbol)
                  else (6000 if think == "enabled" else 1400))
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
    posted = _post_json(endpoint, body, timeout, return_identity=return_identity)
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
    """Build the exact Stage B input and the immutable ID->object selection map."""
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

    # Do the full frozen arithmetic/identity/quote/affordability validation before a byte of the
    # candidate payload reaches the model.  RuntimeCandidate is a dataclass and can be instantiated
    # directly, so isinstance()/to_dict() alone are not a safety boundary.
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
                     thinking: str = "disabled", return_raw: bool = False,
                     return_cot: bool = False, return_identity: bool = False,
                     intent_id: Optional[str] = None):
    """Stage B: select one supplied runtime candidate, or return None for terminal decline.

    The model sees only the immutable intent plus 3-5 already filtered runtime candidates.  The
    strict parser returns a supplied ID or None; this wrapper resolves that ID back to the original
    RuntimeCandidate object so downstream code stays bound to its exact ordered conIds.  It never
    reconstructs or substitutes a contract.
    """
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
    posted = _post_json(endpoint, body, timeout, return_identity=return_identity)
    content, cot, identity = _two_stage_message(posted, return_identity)
    selected_id = parse_stage_b(content, candidate_list)
    selected = None if selected_id is None else by_id.get(selected_id)
    if selected_id is not None and selected is None:
        # parse_stage_b owns this membership check; keep the wrapper fail-closed if its contract ever
        # regresses instead of returning an unbound identifier to the money path.
        raise EntryContractError("Stage B parser returned an unsupplied candidate_id")
    return _two_stage_return(
        selected, content, cot, identity, return_raw=return_raw, return_cot=return_cot,
        return_identity=return_identity)


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
        **_thinking_kwargs(think),
    }
    posted = _post_json(endpoint, body, timeout, return_identity=return_identity)
    d, identity = posted if return_identity else (posted, None)
    _msg = d["choices"][0]["message"]
    content = _msg.get("content") or ""
    cot = _read_cot(_msg)  # [m3cot] separate CoT field; None if endpoint stripped it
    ideas = parse_ideas(content)  # PARSING UNCHANGED: runs on the clean `content` exactly as before
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
        **_thinking_kwargs(thinking),
    }
    posted = _post_json(endpoint, body, timeout, return_identity=return_identity)
    d, identity = posted if return_identity else (posted, None)
    _msg = d["choices"][0]["message"]
    content = _msg.get("content") or ""          # clean answer (parsing target, unchanged)
    cot = _read_cot(_msg)  # [m3cot] separate CoT field; None if endpoint stripped it
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
    "names (SPCX is the ONE permitted Elon-derivative name). Flag any name with a scheduled "
    "binary catalyst (FDA decision, trial readout) in its reason. One short reason each.\n"
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
