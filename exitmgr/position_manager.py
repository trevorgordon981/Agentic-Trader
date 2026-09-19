"""Public API contract; production-derived narrative omitted."""
import json
import re
import urllib.request
import os

from exitmgr import provenance
from exitmgr import strategist
from exitmgr import event_capture as _evcap
import urllib.error
















REMIT_ACTIONS = ("hold", "cut")






KNOWN_ACTIONS = ("hold", "arm_trail", "tighten_stop", "take_profit", "cut")





OUT_OF_REMIT = "out_of_remit"

_ACTION_CLAUSES = {
    "hold":
        '- "hold": leave the current rules unchanged (the default; use it unless there is a '
        'clear reason).',
    "arm_trail":
        '- "arm_trail": the position is UP and the trend is intact — arm or tighten an '
        'evidence-driven trailing stop so the winner can run while protecting a material share of '
        'its peak gain. Give trail_activation_gain_pct (the observed gain% it has cleared) and '
        'trail_giveback_fraction (how much of the peak gain it may retrace before exiting, 0.2-0.5 '
        'typical). Because these are options (volatile), keep the trail wide enough for the '
        'observed volatility — do not use a stock-tight trail or a fixed profit ceiling.',
    "tighten_stop":
        '- "tighten_stop": momentum is weakening but not broken — raise the stop to a TIGHTER '
        'stop_pct (smaller loss) to cut risk.',
    "take_profit":
        '- "take_profit": current evidence says the run has stalled, the thesis has resolved, or '
        'momentum has materially broken — exit now and bank it. Do not take profit merely because '
        'an ordinary percentage threshold was reached; while a winner is still running prefer '
        'arm_trail.\n'
        '- "take_profit" with "reload": true — exit now AND flag the same name for a separate '
        'fresh-entry evaluation. This is never immediate re-entry and never bypasses the normal '
        'new-entry gates. A reload requires a fresh qualified base plus current quote, underlying '
        'and broad-tape confirmation, valid long-debit structure, DTE/theta fit, risk/size checks, '
        'and normal human approval. If those fresh-entry facts are absent from the visible '
        'evidence, set reload=false. Give reload_conviction (1-10) only as the strength of the '
        'candidate for that separate evaluation; do not reload into exhaustion, a weak or '
        'rolling-over trend, faded MFE, or a spent thesis.',
    "cut":
        '- "cut": the thesis is broken / it is bleeding — exit now.',
}






_RULES_OWN_IT = (
    'NOT YOURS THIS CYCLE. Trailing stops, profit-taking and stop tightening are performed by '
    'deterministic rules that run every cycle without you: an entry-anchored protective stop, an '
    'auto-arming trail on any winner, an ATR-normalised stop that ratchets in as the intended hold '
    'is spent, and a hard close near expiry. Do not ask for them, and do not smuggle them into a '
    'cut. A position that is still working is a HOLD even when you would prefer to bank it — the '
    'rules will bank it. "cut" is reserved for a BROKEN THESIS: the reason you entered is no '
    'longer true, or the visible evidence has turned against it. Merely stale, merely flat, merely '
    'up, or merely past a threshold is NOT a broken thesis.'
)

_HEADER = (
    'You are a disciplined risk manager for an options swing-trading book. Each cycle you are '
    'given the current state of every OPEN position and you decide, per position, how to manage '
    'it. You are NOT picking new trades — only managing existing ones. These are long options / '
    'debit spreads (defined risk).\n\nYour bias is HOLD. Only act on a MATERIAL change. For each '
    'position choose exactly one action:'
)


_STOP_RULE = (
    'For every ordinary long call, long put, or debit spread, stop_pct is at most 30% of debit. '
    'Thesis failure or adverse evidence may require an earlier/tighter exit, never a later or '
    'wider stop. If an existing ordinary stop exceeds 30, tighten it to 30 or less; never output '
    'or preserve a wider stop.'
)

_REGIME_INTRO = ('You are also given the current market_regime (bull / neutral / risk_off) and '
                 "each position's trend. ADAPT to it:")
_REGIME_BULL = {
    True: '- BULL regime + a strong-uptrending winner: LET IT RUN. Prefer arm_trail with a wide '
          'evidence-supported giveback (0.4-0.5); you MAY widen an existing trail only when the '
          'visible trend and volatility justify it, never widen the ordinary loss stop; AVOID '
          'take_profit on a strong winner.',
    False: '- BULL regime + a strong-uptrending winner: LET IT RUN — and "let it run" is a HOLD. '
           'Do not cut a position whose thesis is still working; the rules carry its trail.',
}
_REGIME_TIGHT = {
    True: '- NEUTRAL or RISK_OFF: manage TIGHT — tighter trails, quicker evidence-driven '
          'take_profit, and never loosen a stop or widen a trail.',
    False: '- NEUTRAL or RISK_OFF: judge the thesis HARDER — a thesis that has stopped working in '
           'a hostile tape is a cut, not a wait. A hostile tape alone is not a broken thesis.',
}
_REGIME_ASYM = {
    True: '- ASYMMETRIC in every regime: cut LOSERS fast; ordinary loss stops only ever tighten. '
          'When uncertain, HOLD.',
    False: '- ASYMMETRIC in every regime: cut LOSERS fast. When uncertain, HOLD.',
}

_WINDOW_HEAD = (
    'UNDERWRITING WINDOW — elapsed time is evidence, not just a clock.\n'
    'When intended_hold_days is non-null, the position was entered with that CALENDAR-day hold, '
    'and its expiry was dated to a multiple of that hold inside a 5-8x WINDOW (8x is the default; '
    'the tighter end is earned, never assumed; 5x is a hard floor and nothing goes under it). That '
    'long expiry is INSURANCE for the intended hold. It is not permission to hold to expiry, and '
    'it is never a reason to date the trade shorter. The buffer is recycled by CLOSING near the '
    'intended hold, which leaves the unspent expiry available to the book.\n\n'
    'Judge every open position by where it sits in the window it was underwritten for:\n\n'
    '    window fraction = calendar days elapsed / intended_hold_days\n'
)
_WINDOW_EARLY = {
    True: '- under 40% (early): elapsed time is not yet evidence about the thesis. Act only on '
          'the thesis itself or on the stop.',
    False: '- under 40% (early): elapsed time is not yet evidence about the thesis. Act only on '
           'the thesis itself.',
}
_WINDOW_MID = {
    True: '- 40-80% (mid): target proximity and trail management become live considerations; the '
          'calendar still is not the argument.',
    False: '- 40-80% (mid): the calendar still is not the argument. Judge the thesis.',
}
_WINDOW_LATE = {
    True: '- 80-100% (late): the thesis was underwritten to pay INSIDE this window, so bias '
          'toward closing.',
    False: '- 80-100% (late): the thesis was underwritten to pay INSIDE this window. Ask whether '
           'it still can; if it can, that is a HOLD and the rules do the banking.',
}
_WINDOW_PAST = {
    True: '- over 100% (past the window): the thesis did not pay on the schedule it was '
          'underwritten for. That is itself evidence. Default to closing. Keep the position only '
          'if you state, from the evidence supplied in this cycle, a live reason to be in it that '
          'is not the original schedule.',
    False: '- over 100% (past the window): the thesis did not pay on the schedule it was '
           'underwritten for. That is EVIDENCE that it may be broken — weigh it with everything '
           'else supplied this cycle, and cut if the thesis itself no longer holds. On its own it '
           'is not an instruction to close: the deterministic rules already ratchet the stop in on '
           'a lapsed hold and force-close genuinely abandoned capital.',
}
_WINDOW_TAIL = (
    'Calendar days and trading days are different units. The intended hold is stated in calendar '
    'days and must be judged in calendar days; do not compare trading days held against a '
    'calendar-day hold.'
)

_INPUT_COMPLETENESS = (
    'INPUT COMPLETENESS: each position view supplies entry_timestamp, calendar_days_elapsed, '
    'intended_hold_days, window_fraction, and entry_conviction. Legacy values may be null. If '
    'intended_hold_days or calendar_days_elapsed is null, the underwriting-window state is '
    'UNKNOWN: do not infer it from DTE, conviction, thesis, or any other field. Manage that '
    'position only from the other evidence supplied.'
)


def _reply_block(remit):
    """Public API contract; production-derived narrative omitted."""
    fields = ['"action": "..."']
    if "arm_trail" in remit:
        fields += ['"trail_activation_gain_pct": 40', '"trail_giveback_fraction": 0.35']
    if "tighten_stop" in remit:
        fields += ['"stop_pct": 30']
    if "take_profit" in remit:
        fields += ['"reload": false', '"reload_conviction": 7']
    fields += ['"reason": "..."']
    lines = ['Reply with ONE JSON object and nothing after it:',
             '{"decisions": {"<con_id>": {%s}, ...}}' % ', '.join(fields)]
    if "take_profit" in remit:
        lines.append('(reload / reload_conviction apply ONLY to take_profit; omit or leave '
                     'reload=false otherwise.)')
    lines.append('Include only positions you are changing PLUS any you explicitly hold; omitted '
                 'positions are treated as hold.')
    return '\n'.join(lines)


def build_system(remit=REMIT_ACTIONS):
    """Public API contract; production-derived narrative omitted."""
    remit = tuple(remit)
    unknown = [a for a in remit if a not in KNOWN_ACTIONS]
    if unknown:
        raise ValueError("remit contains verbs with no prompt clause: %s" % unknown)
    menu = '\n'.join(_ACTION_CLAUSES[a] for a in KNOWN_ACTIONS if a in remit)
    parts = [_HEADER, menu]
    if len(remit) < len(KNOWN_ACTIONS):
        parts.append(_RULES_OWN_IT)
    if "tighten_stop" in remit:
        parts.append(_STOP_RULE)
    parts.append('\n'.join([_REGIME_INTRO,
                            _REGIME_BULL["arm_trail" in remit],
                            _REGIME_TIGHT["take_profit" in remit],
                            _REGIME_ASYM["tighten_stop" in remit]]))
    parts.append(_reply_block(remit))
    _wide = "take_profit" in remit
    parts.append('\n'.join([_WINDOW_HEAD, _WINDOW_EARLY[_wide], _WINDOW_MID[_wide], _WINDOW_LATE[_wide],
                            _WINDOW_PAST[_wide], '', _WINDOW_TAIL]))
    parts.append(_INPUT_COMPLETENESS)
    return '\n\n'.join(parts)


SYSTEM = build_system(REMIT_ACTIONS)


def _post_json(endpoint, model, system, user, timeout=120, retries=3, backoff=8,
               return_identity=False):
    request_body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0,
        "max_tokens": 1400,

        "thinking": "disabled",
        **strategist._thinking_kwargs("disabled"),
    }
    posted = strategist._post_json(
        endpoint, request_body, timeout, retries=retries, backoff=backoff,
        return_identity=return_identity)
    data, identity = posted if return_identity else (posted, None)
    content = data["choices"][0]["message"].get("content") or ""
    return (content, identity) if return_identity else content


def _extract_json(raw):
    """Public API contract; production-derived narrative omitted."""
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




_VALID = frozenset(REMIT_ACTIONS)


def _identity_provenance(model_identity):
    """Public API contract; production-derived narrative omitted."""
    return (_evcap.IDENTITY_SOURCE_META if model_identity is not None
            else _evcap.IDENTITY_SOURCE_UNKNOWN)


def assess_positions(endpoint, model, positions, market_regime=None, timeout=75, return_meta=False):
    """Public API contract; production-derived narrative omitted."""












    def _ret(out, raw=None, model_identity=None, source=None, *, attempt_ok=None,
             failure_reason=None):
        if not return_meta:
            return out





        meta = {"raw": raw, "model_identity_source": source,
                "attempt_ok": attempt_ok, "failure_reason": failure_reason}
        if model_identity is not None:
            meta["model_identity"] = model_identity
        return out, meta

    if not positions:
        return _ret({}, failure_reason="not_attempted:no_positions")
    if not model:

        return _ret({}, failure_reason="not_attempted:model_not_configured")
    try:
        user = json.dumps({"market_regime": market_regime, "positions": positions}, default=str)
        capture_identity = os.environ.get("TRADER_CAPTURE_EXIT_IDENTITY", "0").lower() \
            not in ("0", "false", "no", "off")
        posted = _post_json(endpoint, model, SYSTEM, user, timeout=timeout, retries=2, backoff=5,
                            return_identity=capture_identity)
        raw, model_identity = posted if isinstance(posted, tuple) else (posted, None)
        data = _extract_json(raw)
        if not isinstance(data, dict):
            return _ret({}, raw, model_identity, source=_identity_provenance(model_identity),
                        attempt_ok=False, failure_reason="invalid_response_json")
        decs = data.get("decisions")
        if not isinstance(decs, dict):
            return _ret({}, raw, model_identity, source=_identity_provenance(model_identity),
                        attempt_ok=False, failure_reason="invalid_decisions_object")
        out, refused = {}, []
        for k, v in decs.items():
            if not isinstance(v, dict):
                return _ret({}, raw, model_identity,
                            source=_identity_provenance(model_identity), attempt_ok=False,
                            failure_reason="invalid_decision_entry")
            try:
                cid = int(k)
            except (TypeError, ValueError):
                return _ret({}, raw, model_identity,
                            source=_identity_provenance(model_identity), attempt_ok=False,
                            failure_reason="invalid_decision_con_id")
            action = str(v.get("action", "hold")).strip().lower()







            proposed = None
            if action not in _VALID:
                proposed = action
                action = OUT_OF_REMIT
                refused.append((cid, proposed))






            _reload = bool(v.get("reload")) if action == "take_profit" else False
            _reload_conv = _num(v.get("reload_conviction")) if _reload else None
            out[cid] = {
                "action": action,


                **({"proposed_action": proposed} if proposed is not None else {}),
                "trail_activation_gain_pct": _num(v.get("trail_activation_gain_pct")),
                "trail_giveback_fraction": _num(v.get("trail_giveback_fraction")),
                "stop_pct": _num(v.get("stop_pct")),
                "reload": _reload,
                "reload_conviction": _reload_conv,



                "reason": str(v.get("reason", ""))[:8000],
            }
        if refused:




            _by_verb = {}
            for _cid, _verb in refused:
                _by_verb.setdefault(_verb, []).append(_cid)
            print("[POSMGMT] remit=%s: refused %d out-of-remit proposal(s) at the model boundary: "
                  "%s (recorded as %r with the original under 'proposed_action'; nothing acts on "
                  "them)" % (list(REMIT_ACTIONS), len(refused),
                             "; ".join("%s -> con_ids %s" % (v, sorted(c))
                                        for v, c in sorted(_by_verb.items())), OUT_OF_REMIT))
        return _ret(out, raw, model_identity, source=_identity_provenance(model_identity),
                    attempt_ok=True)
    except Exception as e:
        print(f"[POSMGMT] assess_positions failed ({e}); falling back to static exit rules")



        reason = f"{type(e).__name__}: {e}"[:500]
        return _ret({}, source=_evcap.IDENTITY_SOURCE_UNKNOWN, attempt_ok=False,
                    failure_reason=reason)


def _num(x):
    try:
        if x is None:
            return None
        return float(x)
    except (TypeError, ValueError):
        return None
