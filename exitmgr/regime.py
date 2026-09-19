"""Public API contract; production-derived narrative omitted."""


def _vix_risk(vix):
    """Public API contract; production-derived narrative omitted."""
    if vix is None:
        return None
    if vix < 19:
        return "calm"
    if vix < 26:
        return "elevated"
    return "stressed"


def _finite(value):
    """Public API contract; production-derived narrative omitted."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def trend_strength(stats):
    """Public API contract; production-derived narrative omitted."""
    if not stats:
        return {"score": 0, "label": "unknown"}
    r20 = _finite(stats.get("ret_20d"))
    r5 = _finite(stats.get("ret_5d"))
    parts = []

    if r20 is not None:
        parts.append((0.6, max(-1.0, min(1.0, r20 / 6.0))))

    if r5 is not None:
        parts.append((0.4, max(-1.0, min(1.0, r5 / 4.0))))
    if not parts:
        return {"score": 0, "label": "unknown"}
    wsum = sum(w for w, _ in parts)
    score = round(sum(w * v for w, v in parts) / wsum * 100)
    if score >= 45:
        label = "strong_up"
    elif score >= 15:
        label = "up"
    elif score <= -45:
        label = "strong_down"
    elif score <= -15:
        label = "down"
    else:
        label = "flat"
    return {"score": score, "label": label}


def classify_regime(index_stats, vix):
    """Public API contract; production-derived narrative omitted."""
    scores = [trend_strength(s)["score"] for s in (index_stats or []) if s]
    tscore = round(sum(scores) / len(scores)) if scores else None
    vstate = _vix_risk(vix)

    if tscore is None and vstate is None:
        return {"regime": "unknown", "trend_score": None, "vix": vix, "vix_state": None}

    regime = "neutral"
    if vstate == "stressed":

        regime = "risk_off"
    elif tscore is not None:
        if tscore >= 25 and vstate in ("calm", "elevated", None):
            regime = "bull"
        elif tscore <= -25:
            regime = "risk_off"
    return {"regime": regime, "trend_score": tscore, "vix": vix, "vix_state": vstate}


def is_bull(regime_info):
    """Public API contract; production-derived narrative omitted."""
    return bool(regime_info) and regime_info.get("regime") == "bull"


BULL_LONG_SIZE_MULT = 1.5


def size_multiplier(regime_info, is_long):
    """Public API contract; production-derived narrative omitted."""
    if is_bull(regime_info) and is_long:
        return BULL_LONG_SIZE_MULT
    return 1.0
