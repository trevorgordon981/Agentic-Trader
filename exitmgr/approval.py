"""Approve-each-entry loop over Slack.

Alfred posts a proposed trade + thesis; you approve by EITHER reacting (:white_check_mark:)
OR replying in-thread with natural language ("approved", "go for it", "send it", "do it", ...).
Text approval is read from the proposal's THREAD so it's unambiguous which trade you mean when
several are pending. NOTHING reaches IBKR without an explicit approval from an authorized user.
Rejection always beats approval (fail-safe); unapproved proposals expire.
"""
import json
import re
import time
import urllib.request
from typing import List, Optional, Set

APPROVE_EMOJI = {"white_check_mark", "heavy_check_mark", "+1", "thumbsup", "ok_hand", "rocket"}
REJECT_EMOJI = {"x", "no_entry", "-1", "thumbsdown", "no_entry_sign"}
SLACK_API = "https://slack.com/api"

# natural-language approval / rejection phrases (matched with word boundaries, case-insensitive)
APPROVE_PHRASES = [
    "approved", "approve", "go for it", "send it", "ship it", "do it", "lgtm", "lets go",
    "let's go", "green light", "greenlight", "pull the trigger", "take it", "buy it", "fire it",
    "fire", "execute", "run it", "send", "go ahead", "go", "yes", "yep", "yeah", "yup", "ok",
    "okay", "confirmed", "confirm", "approve it", "i approve", "sounds good", "do this",
]
REJECT_PHRASES = [
    "no", "nope", "nah", "skip", "pass", "reject", "rejected", "cancel", "stop", "abort",
    "deny", "denied", "kill it", "kill", "dont", "don't", "do not", "never mind", "nevermind",
    "scratch it", "scratch that", "hold off", "not this one", "no thanks", "veto",
]


def _phrase_re(phrases):
    # longest-first so multi-word phrases win; word-boundary anchored
    pat = "|".join(re.escape(p) for p in sorted(phrases, key=len, reverse=True))
    return re.compile(r"(?<!\w)(" + pat + r")(?!\w)", re.IGNORECASE)


_APPROVE_RE = _phrase_re(APPROVE_PHRASES)
_REJECT_RE = _phrase_re(REJECT_PHRASES)


_TP_RE = re.compile(r"(?:tp|take[\s-]*profit|target|pt|\bprofit)\D{0,8}?(\d{1,3})", re.IGNORECASE)
_SL_RE = re.compile(r"(?:sl|stop[\s-]*loss|\bstop)\D{0,8}?(\d{1,3})", re.IGNORECASE)


def parse_levels(text: str):
    """Pull adjusted sell levels from a reply. Returns (take_profit_pct, stop_pct), either None
    if not mentioned. e.g. 'tp 60 stop 30' -> (60, 30); 'stop 25' -> (None, 25)."""
    if not text:
        return (None, None)
    tp = sl = None
    m = _SL_RE.search(text)
    if m:
        sl = float(m.group(1))
    m = _TP_RE.search(text)
    if m:
        tp = float(m.group(1))
    return (tp, sl)


def parse_add_tickers(text: str, candidates) -> list:
    """From a reply in the discovery thread, return which scouted candidates to add. Mentioning a
    candidate ticker (or 'all') adds it. Only ever returns tickers from the candidate set."""
    if not text:
        return []
    up = " " + text.upper() + " "
    cset = [str(c).upper() for c in candidates]
    if re.search(r"[^A-Z]ALL[^A-Z]", up):
        return list(cset)
    return [c for c in cset if re.search(r"[^A-Z]" + re.escape(c) + r"[^A-Z]", up)]


def parse_structure_override(text: str) -> dict:
    """From an approval reply, detect a direction or single-vs-spread override.
    Returns e.g. {'direction':'bearish'}, {'structure':'single'}, {'direction':'flip'}, or {}."""
    if not text:
        return {}
    t = " " + text.lower() + " "
    out = {}
    if re.search(r"\b(flip|opposite|other way|reverse)\b", t):
        out["direction"] = "flip"
    elif re.search(r"\b(bear|bearish|puts?)\b", t):
        out["direction"] = "bearish"
    elif re.search(r"\b(bull|bullish|calls?)\b", t):
        out["direction"] = "bullish"
    if re.search(r"\b(single|outright|just the|no spread|long call|long put|one leg)\b", t):
        out["structure"] = "single"
    elif re.search(r"\b(spread|vertical|make it a spread)\b", t):
        out["structure"] = "spread"
    return out


def parse_size_override(text: str) -> bool:
    """Detect a deliberate 'use my full available cash on THIS trade' opt-in in an approval reply.
    Returns True for phrases like 'full size', 'go big', 'max it', 'all in'. Deliberately does NOT
    match a bare 'full' (too easy to trip accidentally)."""
    if not text:
        return False
    t = " " + text.lower() + " "
    return bool(re.search(
        r"\b(full[\s-]?size|fullsize|go full|go big|max size|max it|max out|maximi[sz]e|"
        r"all[\s-]?in|size up|size it up|use the cap|whole pot|full position)\b", t))


_QTY_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
              "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}


def parse_qty_override(text: str) -> dict:
    """From an approval reply, detect an explicit position-size override. Returns
    {'contracts': N} for 'N contract(s)' / 'qty N' / 'Nx' / 'just N' / word-numbers, or
    {'fraction': f} for 'half'/'quarter'/'third'(-size), or {} if none. Lets you size a posted
    proposal DOWN (or up) from Slack -- complements parse_size_override ('full size', up only)."""
    if not text:
        return {}
    t = " " + text.lower().strip() + " "
    if re.search(r"\b(half|half[\s-]?size)\b", t):
        return {"fraction": 0.5}
    if re.search(r"\b(quarter|quarter[\s-]?size)\b", t):
        return {"fraction": 0.25}
    if re.search(r"\bthird\b", t):
        return {"fraction": 1.0 / 3}
    m = (re.search(r"\b(?:qty|quantity)\s*(\d{1,3})\b", t)
         or re.search(r"\b(\d{1,3})\s*(?:contracts?|lots?|x)\b", t)
         or re.search(r"\bjust\s+(\d{1,3})\b", t))
    if m:
        n = int(m.group(1))
        if 1 <= n <= 100:
            return {"contracts": n}
    m2 = re.search(r"\b(" + "|".join(_QTY_WORDS) + r")\s+(?:contracts?|lots?)\b", t)
    if m2:
        return {"contracts": _QTY_WORDS[m2.group(1)]}
    return {}


def decision_from_text(text: str) -> Optional[str]:
    """Classify a free-text reply as 'approve' | 'reject' | None. Reject wins (fail-safe)."""
    if not text:
        return None
    t = text.strip()
    rej = bool(_REJECT_RE.search(t))
    app = bool(_APPROVE_RE.search(t))
    if rej:
        return "reject"
    if app:
        return "approve"
    return None


def decision_from_reactions(reactions: List[dict], approver_ids: Optional[Set[str]]) -> Optional[str]:
    approved = rejected = False
    for r in reactions or []:
        name = r.get("name", "")
        users = set(r.get("users", []) or [])
        if approver_ids and not (users & approver_ids):
            continue
        if name in APPROVE_EMOJI:
            approved = True
        if name in REJECT_EMOJI:
            rejected = True
    if rejected:
        return "reject"
    if approved:
        return "approve"
    return None


def decision_from_replies(messages: List[dict], approver_ids: Optional[Set[str]],
                          parent_ts: str) -> Optional[str]:
    """Scan thread replies (excluding the bot's parent proposal) for an approval/rejection."""
    approved = rejected = False
    for m in messages or []:
        if m.get("ts") == parent_ts:        # skip the proposal message itself
            continue
        if approver_ids and m.get("user") not in approver_ids:
            continue
        d = decision_from_text(m.get("text", ""))
        if d == "reject":
            rejected = True
        elif d == "approve":
            approved = True
    if rejected:
        return "reject"
    if approved:
        return "approve"
    return None


def _combine(*decisions) -> Optional[str]:
    if "reject" in decisions:
        return "reject"
    if "approve" in decisions:
        return "approve"
    return None


def format_proposal(idea, pot_value: float, per_trade_cap: float, expire_mins: int, order_line: str = "") -> str:
    pct = (idea.est_debit_usd / pot_value * 100.0) if pot_value else 0.0
    return (
        f":chart_with_upwards_trend: *Proposed trade* — *{idea.underlying}* {idea.direction} "
        f"{idea.structure}\n"
        f"~{idea.target_dte}DTE, ~{idea.target_delta:.2f} delta, est. debit "
        f"*${idea.est_debit_usd:,.0f}* ({pct:.1f}% of pot; cap ${per_trade_cap:,.0f})\n"
        f"Conviction *{idea.conviction}/10*"
        + (" :warning: _desperate-only_" if idea.conviction < 4 else
           " _(high confidence)_" if idea.conviction >= 8 else
           " _(medium confidence)_" if idea.conviction >= 6 else "") + "\n"
        f"_Thesis:_ {idea.thesis}\n"
        + (f"*Order:* `{order_line}`\n" if order_line else "")
        + (
        f":point_down: *Tap :white_check_mark: to APPROVE or :x: to DENY* — both are already on "
        f"this message, just tap one. (Typing 'approved' / 'no' works too.) Expires in {expire_mins}m.")
    )


def _api(method: str, token: str, params: dict, http_post: bool = True) -> dict:
    url = f"{SLACK_API}/{method}"
    if http_post:
        req = urllib.request.Request(
            url, data=json.dumps(params).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
    else:
        from urllib.parse import urlencode
        req = urllib.request.Request(
            url + "?" + urlencode(params), headers={"Authorization": f"Bearer {token}"}
        )
    # Self-healing: a transient network blip (e.g. Errno 51 Network unreachable) must NOT crash
    # the watch loop. Retry, then return a benign not-ok dict -- every caller checks .get("ok"),
    # so a failed poll simply skips this tick and retries on the next iteration.
    last_err = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except (OSError, ValueError) as e:
            last_err = e
            time.sleep(min(8, 2 * (attempt + 1)))
    return {"ok": False, "error": f"transient: {last_err}"}


def post_proposal(token: str, channel: str, text: str) -> Optional[str]:
    resp = _api("chat.postMessage", token, {"channel": channel, "text": text})
    ts = resp.get("ts") if resp.get("ok") else None
    if ts:
        # Pre-seed the approve/deny taps so the user just clicks one — no typing, no hunting for
        # the emoji picker. The bot adding these does NOT count as a decision: decision_from_
        # reactions only counts a reaction whose users include an approver_id, so it sits inert
        # until Trevor taps it (adding his user to that reaction).
        for emoji in ("white_check_mark", "x"):
            try:
                _api("reactions.add", token, {"channel": channel, "timestamp": ts, "name": emoji})
            except Exception:
                pass
    return ts


def await_approval(token: str, channel: str, ts: str, approver_ids: Optional[Set[str]],
                   timeout_s: int, poll_s: int = 10, _sleep=time.sleep, _now=time.monotonic) -> str:
    """Poll BOTH reactions and in-thread text replies until approve/reject, or expire."""
    deadline = _now() + timeout_s
    while _now() < deadline:
        rxn = _api("reactions.get", token, {"channel": channel, "timestamp": ts}, http_post=False)
        reactions = (rxn.get("message", {}) or {}).get("reactions", []) if rxn.get("ok") else []
        d_rxn = decision_from_reactions(reactions, approver_ids)

        rep = _api("conversations.replies", token, {"channel": channel, "ts": ts}, http_post=False)
        messages = rep.get("messages", []) if rep.get("ok") else []
        d_txt = decision_from_replies(messages, approver_ids, ts)

        decision = _combine(d_rxn, d_txt)
        if decision:
            return decision
        _sleep(poll_s)
    return "expired"
