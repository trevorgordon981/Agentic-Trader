"""Public API contract; production-derived narrative omitted."""
from __future__ import annotations

import math
from typing import Optional


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_call_delta(spot: float, strike: float, years: float, iv: float,
                  rate: float = 0.04) -> Optional[float]:
    """Public API contract; production-derived narrative omitted."""
    try:
        S, K, T, s = float(spot), float(strike), float(years), float(iv)
    except (TypeError, ValueError):
        return None
    if not all(v == v for v in (S, K, T, s)):
        return None
    if S <= 0 or K <= 0:
        return None
    if T <= 0 or s <= 0:
        return 1.0 if S > K else 0.0
    d1 = (math.log(S / K) + (rate + s * s / 2.0) * T) / (s * math.sqrt(T))
    return _norm_cdf(d1)


def net_structure_delta(spot: float, long_strike: float, short_strike: Optional[float],
                        dte: int, iv: float, rate: float = 0.04,
                        right: str = "C") -> Optional[float]:
    """Public API contract; production-derived narrative omitted."""
    if dte is None:
        return None
    T = float(dte) / 365.0
    dl = bs_call_delta(spot, long_strike, T, iv, rate)
    if dl is None:
        return None
    is_put = str(right or "C").strip().upper().startswith("P")
    if short_strike is None:
        d = (dl - 1.0) if is_put else dl
        return abs(d) if abs(d) > 0 else None
    ds = bs_call_delta(spot, short_strike, T, iv, rate)
    if ds is None:
        return None




    if is_put:
        if not float(long_strike) > float(short_strike):
            return None
    elif not float(long_strike) < float(short_strike):
        return None


    net = abs(dl - ds)
    return net if net > 0 else None


def horizon_k(k_base: float, hold_days, min_days: float = 1.0) -> Optional[float]:
    """Public API contract; production-derived narrative omitted."""
    try:
        k, d = float(k_base), float(hold_days)
    except (TypeError, ValueError):
        return None
    if k != k or d != d or k <= 0:
        return None




    if d < 0:
        return None
    return k * math.sqrt(max(float(min_days), d))


def premium_move_for_atrs(k_atr: float, atr_abs: float, net_delta: float) -> Optional[float]:
    """Public API contract; production-derived narrative omitted."""
    try:
        k, a, d = float(k_atr), float(atr_abs), float(net_delta)
    except (TypeError, ValueError):
        return None
    if not all(v == v for v in (k, a, d)) or k <= 0 or a <= 0 or d <= 0:
        return None
    return k * a * d


def atr_stop_pct(entry_per_share: float, atr_abs: float, net_delta: float,
                 k_atr: float, max_stop_pct: float, min_stop_pct: float = 8.0) -> Optional[float]:
    """Public API contract; production-derived narrative omitted."""
    move = premium_move_for_atrs(k_atr, atr_abs, net_delta)
    if move is None:
        return None
    try:
        eps = float(entry_per_share)
    except (TypeError, ValueError):
        return None
    if eps <= 0 or eps != eps:
        return None
    pct = move / eps * 100.0
    return max(float(min_stop_pct), min(pct, float(max_stop_pct)))


def atr_giveback(entry_per_share: float, peak_per_share: float, atr_abs: float,
                 net_delta: float, k_atr: float,
                 gb_min: float = 0.25, gb_max: float = 0.75) -> Optional[float]:
    """Public API contract; production-derived narrative omitted."""
    move = premium_move_for_atrs(k_atr, atr_abs, net_delta)
    if move is None:
        return None
    try:
        eps, peak = float(entry_per_share), float(peak_per_share)
    except (TypeError, ValueError):
        return None


    if eps != eps or peak != peak or eps <= 0 or peak <= eps:
        return None
    gain = peak - eps
    return max(float(gb_min), min(move / gain, float(gb_max)))


def describe(entry_per_share: float, peak_per_share: Optional[float], atr_abs: float,
             net_delta: float, stop_pct: float, giveback: Optional[float]) -> dict:
    """Public API contract; production-derived narrative omitted."""
    out = {}
    move_per_atr = atr_abs * net_delta
    if move_per_atr > 0:
        out["stop_atr"] = (entry_per_share * stop_pct / 100.0) / move_per_atr
        if peak_per_share and giveback is not None and peak_per_share > entry_per_share:
            out["trail_atr"] = (giveback * (peak_per_share - entry_per_share)) / move_per_atr
    return out
