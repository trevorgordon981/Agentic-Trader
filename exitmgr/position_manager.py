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
import os

from exitmgr import provenance
import urllib.error

SYSTEM = 'You are a disciplined risk manager for an options swing-trading book. Each cycle you are given the current state of every OPEN position and you decide, per position, how to manage it. You are NOT picking new trades — only managing existing ones. These are long options / debit spreads (defined risk).\n\nYour bias is HOLD. Only act on a MATERIAL change. For each position choose exactly one action:\n- "hold": leave the current rules unchanged (the default; use it unless there is a clear reason).\n- "arm_trail": the position is UP and the trend is intact — arm or tighten an evidence-driven trailing stop so the winner can run while protecting a material share of its peak gain. Give trail_activation_gain_pct (the observed gain% it has cleared) and trail_giveback_fraction (how much of the peak gain it may retrace before exiting, 0.2-0.5 typical). Because these are options (volatile), keep the trail wide enough for the observed volatility — do not use a stock-tight trail or a fixed profit ceiling.\n- "tighten_stop": momentum is weakening but not broken — raise the stop to a TIGHTER stop_pct (smaller loss) to cut risk.\n- "take_profit": current evidence says the run has stalled, the thesis has resolved, or momentum has materially broken — exit now and bank it. Do not take profit merely because an ordinary percentage threshold was reached; while a winner is still running prefer arm_trail.\n- "take_profit" with "reload": true — exit now AND flag the same name for a separate fresh-entry evaluation. This is never immediate re-entry and never bypasses the normal new-entry gates. A reload requires a fresh qualified base plus current quote, underlying and broad-tape confirmation, valid long-debit structure, DTE/theta fit, risk/size checks, and normal human approval. If those fresh-entry facts are absent from the visible evidence, set reload=false. Give reload_conviction (1-10) only as the strength of the candidate for that separate evaluation; do not reload into exhaustion, a weak or rolling-over trend, faded MFE, or a spent thesis.\n- "cut": the thesis is broken / it is bleeding — exit now.\n\nFor every ordinary long call, long put, or debit spread, stop_pct is at most 30% of debit. Thesis failure or adverse evidence may require an earlier/tighter exit, never a later or wider stop. If an existing ordinary stop exceeds 30, tighten it to 30 or less; never output or preserve a wider stop.\n\nYou are also given the current market_regime (bull / neutral / risk_off) and each position\'s trend. ADAPT to it:\n- BULL regime + a strong-uptrending winner: LET IT RUN. Prefer arm_trail with a wide evidence-supported giveback (0.4-0.5); you MAY widen an existing trail only when the visible trend and volatility justify it, never widen the ordinary loss stop; AVOID take_profit on a strong winner.\n- NEUTRAL or RISK_OFF: manage TIGHT — tighter trails, quicker evidence-driven take_profit, and never loosen a stop or widen a trail.\n- ASYMMETRIC in every regime: cut LOSERS fast; ordinary loss stops only ever tighten. When uncertain, HOLD.\n\nReply with ONE JSON object and nothing after it:\n{"decisions": {"<con_id>": {"action": "...", "trail_activation_gain_pct": 40, "trail_giveback_fraction": 0.35, "stop_pct": 30, "reload": false, "reload_conviction": 7, "reason": "..."}, ...}}\n(reload / reload_conviction apply ONLY to take_profit; omit or leave reload=false otherwise.)\nInclude only positions you are changing PLUS any you explicitly hold; omitted positions are treated as hold.\n\n\nUNDERWRITING WINDOW — elapsed time is evidence, not just a clock.\nEvery position was entered with an intended hold stated in CALENDAR days, and its expiry was dated to a multiple of that hold keyed to entry conviction (6 -> at least 8x the intended hold, 7 -> 6x, 8 -> 5x, 9-10 -> 4x; 4x is a hard floor and nothing goes under it). That long expiry is INSURANCE for the intended hold. It is not permission to hold to expiry, and it is never a reason to date the trade shorter. The buffer is recycled by CLOSING near the intended hold, which leaves the unspent expiry available to the book.\n\nJudge every open position by where it sits in the window it was underwritten for:\n\n    window fraction = calendar days elapsed / intended_hold_days\n\n- under 40% (early): elapsed time is not yet evidence about the thesis. Act only on the thesis itself or on the stop.\n- 40-80% (mid): target proximity and trail management become live considerations; the calendar still is not the argument.\n- 80-100% (late): the thesis was underwritten to pay INSIDE this window, so bias toward closing.\n- over 100% (past the window): the thesis did not pay on the schedule it was underwritten for. That is itself evidence. Default to closing. Keep the position only if you state, from the evidence supplied in this cycle, a live reason to be in it that is not the original schedule.\n\nCalendar days and trading days are different units. The intended hold is stated in calendar days and must be judged in calendar days; do not compare trading days held against a calendar-day hold.'
SYSTEM = SYSTEM.replace(
    'Every position was entered with an intended hold stated in CALENDAR days, and its expiry was '
    'dated to a multiple of that hold keyed to entry conviction (6 -> at least 8x the intended '
    'hold, 7 -> 6x, 8 -> 5x, 9-10 -> 4x; 4x is a hard floor and nothing goes under it).',
    'When intended_hold_days is non-null, the position was entered with that CALENDAR-day hold, '
    'and its expiry was dated to a multiple of that hold keyed to entry conviction (6 -> at least '
    '8x the intended hold, 7 -> 6x, 8 -> 5x, 9-10 -> 4x; 4x is a hard floor and nothing goes under it).')
SYSTEM += ('\n\nINPUT COMPLETENESS: each position view supplies entry_timestamp, '
           'calendar_days_elapsed, intended_hold_days, window_fraction, and entry_conviction. '
           'Legacy values may be null. If intended_hold_days or calendar_days_elapsed is null, '
           'the underwriting-window state is UNKNOWN: do not infer it from DTE, conviction, thesis, '
           'or any other field. Manage that position only from the other evidence supplied.')
def _post_json(endpoint, model, system, user, timeout=120, retries=3, backoff=8,
               return_identity=False):
    request_body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0,
        "max_tokens": 1400,
    }
    body = json.dumps(request_body).encode()
    before = None
    identity_error = None
    if return_identity:
        try:
            before = provenance.runtime_snapshot(endpoint)
        except provenance.RuntimeIdentityError as exc:
            identity_error = str(exc)
            if provenance.identity_required():
                raise
    last = ""
    for attempt in range(retries):
        try:
            headers = {"Content-Type": "application/json"}
            headers.update(provenance.priority_headers(
                int(os.environ.get("TRADER_LLM_PRIORITY", "1"))))
            req = urllib.request.Request(endpoint, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read().decode())
            content = d["choices"][0]["message"].get("content") or ""
            if not return_identity:
                return content
            if before is not None:
                try:
                    after = provenance.runtime_snapshot(endpoint)
                    return content, provenance.request_identity(
                        endpoint=endpoint, body=request_body, response=d,
                        before=before, after=after)
                except provenance.RuntimeIdentityError as exc:
                    if provenance.identity_required():
                        raise
                    identity_error = str(exc)
            return content, {
                "schema": provenance.IDENTITY_SCHEMA, "verified": False,
                "identity_error": identity_error, "endpoint": endpoint,
                "system_prompt_sha256": provenance.sha256(system),
                "context_sha256": provenance.sha256(user),
                "request_sha256": provenance.sha256(request_body),
                "response_sha256": provenance.sha256(d),
            }
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
    def _ret(out, raw=None, model_identity=None):
        if not return_meta:
            return out
        meta = {"raw": raw}
        if model_identity is not None:
            meta["model_identity"] = model_identity
        return out, meta

    if not positions:
        return _ret({})
    if not model:
        # model name unset -> skip model management, stay on static rules
        return _ret({})
    try:
        user = json.dumps({"market_regime": market_regime, "positions": positions}, default=str)
        capture_identity = os.environ.get("TRADER_CAPTURE_EXIT_IDENTITY", "0").lower() \
            not in ("0", "false", "no", "off")
        posted = _post_json(endpoint, model, SYSTEM, user, timeout=timeout, retries=2, backoff=5,
                            return_identity=capture_identity)
        raw, model_identity = posted if isinstance(posted, tuple) else (posted, None)
        data = _extract_json(raw)
        if not isinstance(data, dict):
            return _ret({}, raw, model_identity)
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
        return _ret(out, raw, model_identity)
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
