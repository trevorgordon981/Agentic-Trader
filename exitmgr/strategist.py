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
import re
import sys
import urllib.request
import urllib.error
import time
from dataclasses import dataclass, asdict
from typing import List, Optional

from exitmgr.risk import INDEX_UNDERLYINGS

# 1-10 conviction rubric (Trevor 2026-06-12): 1-3 desperate-only, 4 below-avg, 5 middle,
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
    '"target_delta": <0.0-1.0>, '
    '"est_debit_usd": <TOTAL dollars = premium_per_share * 100 * contracts, e.g. 180 not 1.80>, '
    '"conviction": <1-10>, '
    '"profit_target_pct": <SELL to take profit at +this% of premium, e.g. 75>, '
    '"stop_pct": <SELL to cut the loss at -this% of premium, e.g. 40>, '
    '"thesis": "<1-2 sentences>"}]}\n'
    "ALWAYS give profit_target_pct and stop_pct -- the levels you would sell at."
)

# Conservative mode (the 15-min loop): silence is allowed.
SYSTEM_PROMPT = (
    "You are a disciplined options swing-trading strategist for a SMALL account. Propose 0-3 trades "
    "you have genuine conviction on, or an empty list if nothing is compelling -- never force trades. "
    + _UNIVERSE + "\n" + _SCORING + "\n" + _CONTRACT
)

# Recommend mode (the daily slate): ALWAYS surface your best ideas, scored honestly.
RECOMMEND_PROMPT = (
    "You are an options swing-trading strategist for a SMALL (~$1,000) account. Recommend your "
    "BEST 1-3 option trade ideas for today. ALWAYS give at least one idea unless the market is "
    "genuinely untradeable -- it is fine to include moderate or weak ideas, just score them "
    "honestly so the human can judge. " + _UNIVERSE + "\n" + _SCORING + "\n" + _CONTRACT
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


def normalize_direction(raw_dir: str, structure: str) -> Optional[str]:
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
    """Sell-level % from the model: clamp into [lo, hi]; 0.0 means 'use the default rule'."""
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
            u = str(t["underlying"]).upper().strip()
            direction = normalize_direction(t.get("direction", ""), t.get("structure", ""))
            debit = normalize_debit(float(t["est_debit_usd"]))
            idea = TradeIdea(
                underlying=u,
                is_index=bool(t.get("is_index", u in INDEX_UNDERLYINGS)) or (u in INDEX_UNDERLYINGS),
                direction=direction or "",
                structure=str(t.get("structure", "")).strip(),
                target_dte=int(t["target_dte"]),
                target_delta=min(1.0, abs(float(t["target_delta"]))),
                est_debit_usd=debit,
                conviction=max(1, min(10, int(t["conviction"]))),  # 1-10 scale
                thesis=str(t.get("thesis", "")).strip(),
                profit_target_pct=_clamp_pct(t.get("profit_target_pct"), 20.0, 500.0),
                stop_pct=_clamp_pct(t.get("stop_pct"), 10.0, 90.0),
            )
        except (KeyError, TypeError, ValueError):
            continue
        if not idea.underlying or idea.direction not in ("bullish", "bearish"):
            continue
        if idea.target_dte <= 0 or idea.est_debit_usd <= 0 or not (0.0 < idea.target_delta <= 1.0):
            continue
        out.append(idea)
    return out


def _post_json(endpoint, body, timeout, retries=4, backoff=15):
    """POST to an OpenAI-compatible endpoint with retry on transient busy/connection errors.
    The trade brain is local (M3, single-generation): a 503 means BUSY, not broken -- it frees in
    ~70s, so we retry rather than silently drop a real-money cycle. We deliberately do NOT fall
    back to a cloud model for trade decisions: no trade is better than a trade from an unvetted
    model. If M3 is genuinely unavailable across all retries, we raise and the caller skips the
    cycle (safe)."""
    data = json.dumps(body).encode()
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(
            endpoint, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode(), strict=False)
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (502, 503, 504, 429) and attempt < retries - 1:
                time.sleep(backoff)
                continue
            raise
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            last = e
            if attempt < retries - 1:
                time.sleep(backoff)
                continue
            raise
    if last:
        raise last


def propose(endpoint: str, model: str, market_context: str, timeout: int = 300,
            recommend: bool = False, thinking: str = None) -> List[TradeIdea]:
    """recommend=False: conservative loop (silence allowed). recommend=True: daily slate (always
    surfaces its best ideas, scored honestly 1-10).
    thinking: enabled/disabled/adaptive for M3 chain-of-thought. Defaults ON for the daily recommend
    slate (deeper reasoning for new-position research), OFF for the latency-bound conservative loop."""
    think = thinking if thinking is not None else ("enabled" if recommend else "disabled")
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
    d = _post_json(endpoint, body, timeout)
    content = d["choices"][0]["message"].get("content") or ""
    return parse_ideas(content)


SINGLE_PROMPT = (
    "You are an options swing-trading strategist for a SMALL (~$1,000) account. The user just added "
    "@@TICKER@@ to their watchlist and wants your SINGLE best option trade idea on @@TICKER@@ right "
    "now -- ONLY @@TICKER@@, no other names. Decide direction (bullish/bearish) and structure "
    "yourself from the market context. Score conviction HONESTLY 1-10; if you have no real edge on "
    "@@TICKER@@ today, score it low -- do not inflate. " + _UNIVERSE + "\n" + _SCORING + "\n" + _CONTRACT
)


def propose_one(endpoint: str, model: str, market_context: str, ticker: str, timeout: int = 1800,
                thinking: str = "enabled"):
    """Best SINGLE trade idea for ONE ticker (model picks direction/structure/conviction). Returns a
    TradeIdea or None. Used when the user adds a discovered name and wants a same-day suggestion."""
    prompt = SINGLE_PROMPT.replace("@@TICKER@@", ticker.upper())
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
    d = _post_json(endpoint, body, timeout)
    ideas = [i for i in parse_ideas(d["choices"][0]["message"].get("content") or "")
             if i.underlying.upper() == ticker.upper()]
    ideas.sort(key=lambda i: -i.conviction)
    return ideas[0] if ideas else None


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
