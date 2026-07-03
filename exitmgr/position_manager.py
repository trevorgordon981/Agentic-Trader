"""Model-driven position management.

Each exit cycle, ask the LLM to assess every open position from its live state and
decide how to manage it: arm/tune a trailing stop once it's up and the trend holds,
tighten the stop into weakness, or exit (take-profit / cut). Returns a dict keyed by
con_id; the ExitManager applies the decisions to each position's RulesConfig with
MONOTONIC guardrails (only ever tighten/arm, never loosen) and falls back to the
static rules on any error. READ-ONLY: this never touches IBKR.

Decision schema (per con_id):
  {"action": "hold"|"arm_trail"|"tighten_stop"|"take_profit"|"cut",
   "trail_activation_gain_pct": float,   # arm_trail: gain% at which the trail activates
   "trail_giveback_fraction": float,     # arm_trail: fraction of peak gain it may give back (0-1)
   "stop_pct": float,                    # tighten_stop: new (tighter) stop %
   "reason": "<one line>"}
"""
import json
import re
import urllib.request
import urllib.error

SYSTEM = (
    "You are a disciplined risk manager for an options swing-trading book. Each cycle you are given the "
    "current state of every OPEN position and you decide, per position, how to manage it. You are NOT picking "
    "new trades — only managing existing ones. These are long options / debit spreads (defined risk).\n\n"
    "Your bias is HOLD. Only act on a MATERIAL change. For each position choose exactly one action:\n"
    "- \"hold\": leave the current rules unchanged (the default; use it unless there is a clear reason).\n"
    "- \"arm_trail\": the position is UP and the trend is intact — arm/tighten a trailing stop so the winner can "
    "run while locking in gains. Give trail_activation_gain_pct (the gain% it has cleared) and "
    "trail_giveback_fraction (how much of the peak gain it may retrace before exiting, 0.2-0.5 typical). "
    "Because these are options (volatile), keep the trail WIDE — do not use a stock-tight trail or it stops out on noise.\n"
    "- \"tighten_stop\": momentum is weakening but not broken — raise the stop to a TIGHTER stop_pct (smaller loss) to cut risk.\n"
    "- \"take_profit\": momentum has stalled at a strong gain — exit now and bank it. YOU decide the "
    "level worthy of selling from momentum/trend/MFE, not a fixed % ceiling: while a winner is still "
    "running prefer arm_trail (let it run); choose take_profit when the run has stalled.\n"
    "- \"take_profit\" with \"reload\": true — bank the gain AND you would immediately RE-ENTER a fresh "
    "position in the SAME name at today's level because momentum is strong and the thesis still has room "
    "to run. This signals a fresh same-name entry on continued conviction; give \"reload_conviction\" "
    "(1-10) as your read of the continuation. Only set reload=true when you'd genuinely put fresh capital "
    "in NOW — do NOT reload into exhaustion (weak/rolling-over trend, faded MFE, or a spent thesis).\n"
    "- \"cut\": the thesis is broken / it is bleeding — exit now.\n\n"
    "You are also given the current market_regime (bull / neutral / risk_off) and each position's trend. ADAPT to it:\n"
    "- BULL regime + a strong-uptrending winner: LET IT RUN. Prefer arm_trail with a WIDE giveback (0.4-0.5); you MAY "
    "widen an existing trail to give it room; AVOID take_profit on a strong winner — do not choke your biggest winner.\n"
    "- NEUTRAL or RISK_OFF: manage TIGHT — tighter trails, quicker take_profit, and never loosen a stop or widen a trail.\n"
    "- ASYMMETRIC in every regime: cut LOSERS fast; stops only ever tighten. When uncertain, HOLD.\n\n"
    "Reply with ONE JSON object and nothing after it:\n"
    "{\"decisions\": {\"<con_id>\": {\"action\": \"...\", \"trail_activation_gain_pct\": 40, "
    "\"trail_giveback_fraction\": 0.35, \"stop_pct\": 30, \"reload\": false, \"reload_conviction\": 7, "
    "\"reason\": \"...\"}, ...}}\n"
    "(reload / reload_conviction apply ONLY to take_profit; omit or leave reload=false otherwise.)\n"
    "Include only positions you are changing PLUS any you explicitly hold; omitted positions are treated as hold."
)


def _post_json(endpoint, model, system, user, timeout=120, retries=3, backoff=8):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0,
        "max_tokens": 1400,
    }).encode()
    last = ""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(endpoint, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read().decode())
            return d["choices"][0]["message"].get("content") or ""
        except Exception as e:  # noqa: BLE001 - transient busy/connection; retry then give up
            last = str(e)
            if attempt < retries - 1:
                import time
                time.sleep(backoff * (attempt + 1))
    raise RuntimeError(last or "llm call failed")


def _extract_json(raw):
    """Return the last balanced {...} object in the text, parsed."""
    if not raw:
        return None
    depth = 0
    start = None
    last = None
    for i, ch in enumerate(raw):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    last = raw[start:i + 1]
    if not last:
        return None
    try:
        return json.loads(last)
    except Exception:
        return None


_VALID = {"hold", "arm_trail", "tighten_stop", "take_profit", "cut"}


def assess_positions(endpoint, model, positions, market_regime=None, timeout=75, return_meta=False):
    """positions: list of per-position view dicts (see ExitManager._build_position_views).
    market_regime: dict from regime.classify_regime (bull/neutral/risk_off + trend_score/vix).
    Returns {int(con_id): {action, ...}}; {} on any error (caller falls back to static rules).
    Tuned to fail FAST to the static-rules fallback (retries=2) so a slow/down model never
    stalls the exit cycle -- protecting stop execution is more important than the assessment."""
    # return_meta (2026-07-03, ADDITIVE dataset capture): when True, return
    # (decisions, {"raw": <full raw model response or None>}) so the ExitManager can persist the
    # COMPLETE per-cycle model reasoning (untruncated reason + the raw response) onto each mark for
    # fine-tuning. Default False keeps the historical dict-only return byte-identical for every
    # existing caller/test. The exit DECISIONS are unchanged either way.
    def _ret(out, raw=None):
        return (out, {"raw": raw}) if return_meta else out

    if not positions:
        return _ret({})
    if not model:
        # model name unset -> skip model management, stay on static rules
        return _ret({})
    try:
        user = json.dumps({"market_regime": market_regime, "positions": positions}, default=str)
        raw = _post_json(endpoint, model, SYSTEM, user, timeout=timeout, retries=2, backoff=5)
        data = _extract_json(raw)
        if not isinstance(data, dict):
            return _ret({}, raw)
        decs = data.get("decisions") or {}
        out = {}
        for k, v in decs.items():
            if not isinstance(v, dict):
                continue
            try:
                cid = int(k)
            except (TypeError, ValueError):
                continue
            action = str(v.get("action", "hold")).strip().lower()
            if action not in _VALID:
                continue
            # TAKE-PROFIT-AND-RELOAD (2026-07-03, ADDITIVE): parse an optional reload verb on
            # take_profit. reload=true means "bank it AND re-enter fresh in the same name on
            # continued conviction"; reload_conviction (1-10) is the model's continuation read.
            # Meaningful ONLY on take_profit -- forced off for every other action so a stray flag
            # on hold/arm_trail/cut can never spawn a re-entry. Fully backward-compatible: absent
            # keys => reload False / reload_conviction None (today's behavior).
            _reload = bool(v.get("reload")) if action == "take_profit" else False
            _reload_conv = _num(v.get("reload_conviction")) if _reload else None
            out[cid] = {
                "action": action,
                "trail_activation_gain_pct": _num(v.get("trail_activation_gain_pct")),
                "trail_giveback_fraction": _num(v.get("trail_giveback_fraction")),
                "stop_pct": _num(v.get("stop_pct")),
                "reload": _reload,
                "reload_conviction": _reload_conv,
                # UN-TRUNCATED (2026-07-03): was [:200], which clamped the management reasoning we
                # now want in full for fine-tuning. Keep the FULL reason; the 8KB cap only bounds a
                # pathological response (max_tokens=1400 keeps this far below the cap in practice).
                "reason": str(v.get("reason", ""))[:8000],
            }
        return _ret(out, raw)
    except Exception as e:  # noqa: BLE001
        print(f"[POSMGMT] assess_positions failed ({e}); falling back to static exit rules")
        return _ret({})


def _num(x):
    try:
        if x is None:
            return None
        return float(x)
    except (TypeError, ValueError):
        return None
