"""Market-regime + per-underlying trend-strength signals for position management.

Pure functions over data the system ALREADY fetches each cycle: the per-symbol
`research.momentum_stats` dicts ({last, ret_5d, ret_20d, from_high_pct, from_low_pct,
vol_20d_ann, ivr}) and the VIX level. No new data sources and no fabrication -- if the
inputs are missing we return "unknown"/neutral and callers fall back to regime-neutral
(the original static) behavior.

The point: let the position manager behave differently by regime -- in a confirmed
uptrend let strong winners RUN (wide trails, no early profit-taking); in chop/risk-off
keep the strict, defensive exits. Cut losers fast in every regime.
"""


def _vix_risk(vix):
    """Coarse risk axis from VIX (mirrors research.vix_regime buckets)."""
    if vix is None:
        return None
    if vix < 19:
        return "calm"        # risk-on friendly
    if vix < 26:
        return "elevated"
    return "stressed"        # high/extreme -> risk-off


def trend_strength(stats):
    """Per-underlying trend score in [-100, 100] from momentum_stats. Higher = stronger
    uptrend. Returns {"score": int, "label": str}. label in
    strong_up / up / flat / down / strong_down / unknown."""
    if not stats:
        return {"score": 0, "label": "unknown"}
    r20 = stats.get("ret_20d")
    r5 = stats.get("ret_5d")
    parts = []
    # 20d return is the trend backbone (~+6%/20d on an index = a strong leg -> full weight)
    if r20 is not None:
        parts.append((0.6, max(-1.0, min(1.0, r20 / 6.0))))
    # 5d return is the recent impulse
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
    """Classify the market regime from index momentum + VIX.

    index_stats: list of momentum_stats dicts for the index proxies (SPY/QQQ/IWM); any
                 that are None/missing are ignored.
    Returns {"regime": "bull"|"neutral"|"risk_off"|"unknown", "trend_score": int|None,
             "vix": float|None, "vix_state": str|None}.
    """
    scores = [trend_strength(s)["score"] for s in (index_stats or []) if s]
    tscore = round(sum(scores) / len(scores)) if scores else None
    vstate = _vix_risk(vix)

    if tscore is None and vstate is None:
        return {"regime": "unknown", "trend_score": None, "vix": vix, "vix_state": None}

    regime = "neutral"
    if vstate == "stressed":
        # stress dominates: defend regardless of trend
        regime = "risk_off"
    elif tscore is not None:
        if tscore >= 25 and vstate in ("calm", "elevated", None):
            regime = "bull"
        elif tscore <= -25:
            regime = "risk_off"
    return {"regime": regime, "trend_score": tscore, "vix": vix, "vix_state": vstate}


def is_bull(regime_info):
    """True only on a confirmed bull -- gates the 'let winners run' relaxations."""
    return bool(regime_info) and regime_info.get("regime") == "bull"


BULL_LONG_SIZE_MULT = 1.5  # how much bigger to size a bullish idea in a confirmed bull


def size_multiplier(regime_info, is_long):
    """Per-trade size multiplier. Lean BIGGER into BULLISH ideas in a confirmed bull; leave
    everything else at 1.0 -- bearish ideas are NOT enlarged in a bull tape, and we don't shrink
    in other regimes here (the risk caps stay the ceiling, applied after this). Always >= 1.0."""
    if is_bull(regime_info) and is_long:
        return BULL_LONG_SIZE_MULT
    return 1.0
