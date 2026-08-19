"""ATR-normalised exit levels for option positions.

WHY (2026-08-19).  Both the protective stop (`stop_pct`, a flat % of debit) and the
trailing floor (`giveback_fraction`, a flat % of peak gain) are expressed in PREMIUM
space.  Premium space is not comparable across positions: the same 30% of debit is a
very different underlying move for a 0.10-delta vertical than for a 0.85-delta LEAP.
Measured on the live book, a flat -30% sat 3.99 ATR away on PFE and 3.49 ATR on HL --
i.e. effectively unreachable -- while the same rule on a high-delta structure can sit
well inside one daily range.

The fix is to make the ATR distance the CONTROLLED variable and let the premium
percentage fall out of it, per position:

    premium_move = k_atr * ATR14_underlying * net_delta_of_structure

Everything here is a PURE FUNCTION of numbers already available at decision time.  No
I/O, no clock, no config -- so it is fully testable and cannot fail open.

TWO INVARIANTS THIS MODULE MUST NEVER BREAK, both pre-existing:
  * stops only ever TIGHTEN.  `atr_stop_pct` is therefore capped by the configured
    stop_pct and can only return something <= it.  A rising-ATR name must not be
    granted a wider stop.
  * a floor that moves on its own is not a floor.  These values are computed ONCE, at
    arming/entry, and pinned by the caller; they are not recomputed per cycle.

Every function returns None when its inputs cannot support an honest answer, so the
caller falls back to the configured static value rather than to a fabricated one.
"""
from __future__ import annotations

import math
from typing import Optional


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_call_delta(spot: float, strike: float, years: float, iv: float,
                  rate: float = 0.04) -> Optional[float]:
    """Black-Scholes call delta.  None on unusable inputs (never a guess)."""
    try:
        S, K, T, s = float(spot), float(strike), float(years), float(iv)
    except (TypeError, ValueError):
        return None
    if not all(v == v for v in (S, K, T, s)):      # NaN
        return None
    if S <= 0 or K <= 0:
        return None
    if T <= 0 or s <= 0:                            # expired / no vol -> intrinsic
        return 1.0 if S > K else 0.0
    d1 = (math.log(S / K) + (rate + s * s / 2.0) * T) / (s * math.sqrt(T))
    return _norm_cdf(d1)


def net_structure_delta(spot: float, long_strike: float, short_strike: Optional[float],
                        dte: int, iv: float, rate: float = 0.04,
                        right: str = "C") -> Optional[float]:
    """MAGNITUDE of the structure's net delta -- a long call/put or a debit vertical.

    Returns |dPremium / dUnderlying|, which is all the ATR conversion needs; direction
    is irrelevant because ATR is a distance.

    THE BUG THIS EXISTS TO PREVENT: a vertical's delta is long_delta - short_delta.
    Using the long leg alone overstated PFE's leverage by ~6x (0.681 vs a true 0.106)
    and made a 0.89-ATR trail look like a 0.14-ATR one.  Always net the legs.

    PUTS: put_delta = call_delta - 1.  For a put DEBIT spread the long leg sits at the
    HIGHER strike, so the magnitude is N(d1_short) - N(d1_long).  Taking abs() of the
    netted value handles calls and puts with one expression -- the book holds both
    (INTC 105P, MARA 10P are put verticals).
    """
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
    # ORIENTATION GATE. This module prices DEBIT structures only. A call vertical is a
    # debit when long < short; a put vertical when long > short. The reverse is a CREDIT
    # spread with inverted risk, and taking abs() of its delta would hand back a
    # confident number for a structure whose stop/trail semantics are upside down.
    if is_put:
        if not float(long_strike) > float(short_strike):
            return None
    elif not float(long_strike) < float(short_strike):
        return None
    # call vertical: dl - ds ; put vertical: (dl-1) - (ds-1) = dl - ds. Same expression,
    # opposite sign, so the magnitude is what carries across both.
    net = abs(dl - ds)
    return net if net > 0 else None


def premium_move_for_atrs(k_atr: float, atr_abs: float, net_delta: float) -> Optional[float]:
    """Convert k underlying ATRs into a per-share premium move for this structure."""
    try:
        k, a, d = float(k_atr), float(atr_abs), float(net_delta)
    except (TypeError, ValueError):
        return None
    if not all(v == v for v in (k, a, d)) or k <= 0 or a <= 0 or d <= 0:
        return None
    return k * a * d


def atr_stop_pct(entry_per_share: float, atr_abs: float, net_delta: float,
                 k_atr: float, max_stop_pct: float, min_stop_pct: float = 8.0) -> Optional[float]:
    """Protective stop as a % of debit, sized so it sits `k_atr` ATRs below entry.

    CAPPED by `max_stop_pct` (the configured stop, doctrine 30%) so this can only ever
    TIGHTEN an existing stop -- never widen one.  Floored by `min_stop_pct` so a very
    low-delta structure cannot produce an absurdly tight stop that exits on a tick.
    """
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
    """Trail giveback fraction sized so the floor sits `k_atr` ATRs below the peak.

    Returned as a FRACTION OF PEAK GAIN because that is the unit the existing trail
    speaks; the ATR distance is what actually determined it.  Clamped so a huge gain
    cannot produce a giveback that hands back nearly everything, and a tiny gain cannot
    produce one so tight it exits on noise.
    """
    move = premium_move_for_atrs(k_atr, atr_abs, net_delta)
    if move is None:
        return None
    try:
        eps, peak = float(entry_per_share), float(peak_per_share)
    except (TypeError, ValueError):
        return None
    # NaN slips through every ordering comparison (nan <= 0 is False, peak <= nan is
    # False), so it must be rejected explicitly or it fabricates a clamped giveback.
    if eps != eps or peak != peak or eps <= 0 or peak <= eps:
        return None
    gain = peak - eps
    return max(float(gb_min), min(move / gain, float(gb_max)))


def describe(entry_per_share: float, peak_per_share: Optional[float], atr_abs: float,
             net_delta: float, stop_pct: float, giveback: Optional[float]) -> dict:
    """Express a set of levels in ATRs, for logging and for the dry-run report."""
    out = {}
    move_per_atr = atr_abs * net_delta
    if move_per_atr > 0:
        out["stop_atr"] = (entry_per_share * stop_pct / 100.0) / move_per_atr
        if peak_per_share and giveback is not None and peak_per_share > entry_per_share:
            out["trail_atr"] = (giveback * (peak_per_share - entry_per_share)) / move_per_atr
    return out
