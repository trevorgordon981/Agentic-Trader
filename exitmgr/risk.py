"""Hard risk gate for the LLM trading system.

This is the SOLVENCY layer and is NOT the LLM -- every limit is enforced here in code.
The LLM proposes; this gate disposes. All caps are computed off the LIVE pot value passed
in each evaluation, so position sizing scales automatically with the account.

Confirmed limits (2026-06-11): <=12% of pot per trade, <=4 concurrent positions,
halt new entries at -8% on the day, index = SPY/QQQ/IWM, single names must be user-approved.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from exitmgr import regime as regime_mod

INDEX_UNDERLYINGS: Set[str] = {"SPY", "QQQ", "IWM"}

# PENDING CONVICTION CALIBRATION -- do not steepen until journal calibration justifies it.
# Conviction (1-10) -> fraction-of-pot the trade may use, BEFORE the hard ceiling + cash-buffer
# clamp. Defaulted FLAT at the base 12% cap so installing the mechanism does NOT increase risk
# vs today; we just edit these numbers once the journal says higher conviction actually earns more.
DEFAULT_CONVICTION_SIZE_CURVE: Dict[int, float] = {
    1: 0.12, 2: 0.12, 3: 0.12, 4: 0.12, 5: 0.12,
    6: 0.12, 7: 0.12, 8: 0.12, 9: 0.12, 10: 0.12,
}


@dataclass
class RiskLimits:
    max_trade_pct: float = 0.12            # <=12% of the pot per single trade (soft, regime-scalable)
    max_trade_pct_hard: float = 0.25       # ABSOLUTE ceiling: no single trade may EVER exceed this
                                           # % of NetLiq -- binds on every path incl. confident +
                                           # regime size-up (Trevor 2026-06-22 risk-gate hardening)
    max_concurrent: int = 4                # <=4 open positions at once
    daily_halt_pct: float = 0.08           # halt NEW entries when pot is down >=8% on the day
    max_single_name_agg_pct: float = 0.36  # aggregate cap on single-name (non-index) exposure
    pot_cap_usd: Optional[float] = None    # optional ring-fence ceiling for sizing
    cash_buffer_pct: float = 0.05          # ALWAYS keep this fraction of NetLiq (account VALUE,
                                           # not buying power) liquid. The final $ size is clamped to
                                           # available_funds - cash_buffer_pct*NetLiq, so even a
                                           # whole-pot conviction-10 trade leaves ~5% cash. (2026-06-22)
    allow_any_name: bool = False           # model may propose names beyond approved_names
                                           # (all other caps + per-entry approval still bind)
    confident_full_size: bool = False      # if a high-conviction idea may use the WHOLE pot,
                                           # bypassing the % size caps (Trevor's call 2026-06-12)
    cap_bypass_min_conviction: int = 6     # conviction >= this may EXCEED the soft 12% cap (toward the
                                           # hard ceiling). Raised 2026-06-22 from 4 -> 6: low-conviction
                                           # ideas (3-5) can NOT exceed the base cap. Still bounded by
                                           # max_trade_pct_hard + the cash buffer on every path.
    conviction_size_curve: Optional[Dict[int, float]] = None
                                           # PENDING CONVICTION CALIBRATION -- conviction ->
                                           # fraction-of-pot lookup. Default None => fall back to
                                           # max_trade_pct for EVERY conviction (exactly today's soft
                                           # cap, no behavior change). Set it (config or
                                           # DEFAULT_CONVICTION_SIZE_CURVE) to drive sizing by the
                                           # curve; ship it FLAT at the base cap until calibrated.
    blocked_names: Set[str] = field(default_factory=set)  # single-name tickers to always reject
                                           # (Elon/space/etc); sector-level biotech block is in trader.py


@dataclass
class OpenPosition:
    underlying: str
    notional: float        # current $ at risk
    is_index: bool


@dataclass
class ProposedTrade:
    underlying: str
    notional: float        # $ debit to open the trade
    is_index: bool
    conviction: int = 1    # 1-5, from the strategist; drives the confident-sizing bypass
    is_long: bool = True   # bullish (long call / call debit) vs bearish; gates bull size-up
    profit_target_pct: float = 0.0  # SELL-to-take-profit at +this% of premium (0 = use default)
    stop_pct: float = 0.0           # SELL-to-cut-loss at -this% of premium (0 = use default)
                                    # reward:risk gate rejects if stop_pct > profit_target_pct


@dataclass
class GateDecision:
    approved: bool
    reasons: List[str] = field(default_factory=list)  # why blocked (empty == approved)
    pot_value: float = 0.0
    per_trade_cap: float = 0.0


def effective_pot(net_liq: float, pot_cap_usd: Optional[float]) -> float:
    """Pot used for % caps: live net-liq, optionally ring-fenced by a configured ceiling."""
    if pot_cap_usd and pot_cap_usd > 0:
        return min(net_liq, pot_cap_usd)
    return net_liq


def day_pnl_pct(pot_now: float, pot_day_start: float) -> float:
    if pot_day_start <= 0:
        return 0.0
    return (pot_now - pot_day_start) / pot_day_start


def curve_fraction(conviction: int, curve: Optional[Dict[int, float]], fallback: float) -> float:
    """Fraction-of-pot this conviction may use, per the configured conviction_size_curve.
    PENDING CONVICTION CALIBRATION -- the shipped curve is flat at the base cap, so this is a no-op
    sizing-wise today; it's the MECHANISM that lets us later steepen sizing by editing config only.
    Out-of-range / missing convictions fall back to the base cap (never to a larger value)."""
    if not curve:
        return fallback
    try:
        c = int(conviction)
    except (TypeError, ValueError):
        return fallback
    if c in curve:
        return float(curve[c])
    # clamp to the nearest defined endpoint so 0 / 11 / unmapped don't silently up-size
    keys = sorted(int(k) for k in curve.keys())
    if not keys:
        return fallback
    if c < keys[0]:
        return float(curve[keys[0]])
    if c > keys[-1]:
        return float(curve[keys[-1]])
    return fallback


def evaluate_trade(
    trade: ProposedTrade,
    *,
    net_liq: float,
    available_funds: float,
    open_positions: List[OpenPosition],
    pot_day_start: float,
    approved_names: Set[str],
    limits: RiskLimits,
    regime_info: Optional[dict] = None,
) -> GateDecision:
    """Pure risk gate. Returns approved + every reason it was blocked. ALL checks run so the
    caller can log the full picture (we don't short-circuit on the first failure)."""
    reasons: List[str] = []
    pot = effective_pot(net_liq, limits.pot_cap_usd)
    EPS = 1e-9
    u = trade.underlying.upper()

    # CONVICTION->SIZE CURVE (PENDING CONVICTION CALIBRATION -- do not steepen until journal
    # calibration justifies it). The conviction_size_curve maps conviction -> fraction-of-pot; it
    # ships FLAT at the base 12% cap, so this is sizing-equivalent to today. The cap-bypass below
    # only lets genuine mid/high conviction (>= cap_bypass_min_conviction) lift PAST the soft 12%
    # cap toward the hard ceiling -- low conviction (e.g. 3-5) can never exceed the base cap.
    # Every path still binds on: buying power (#3), the cash buffer, the concurrent cap (#4), the
    # daily circuit breaker (#5), the per-trade HARD ceiling (#2 below), and human approval.
    confident = (limits.confident_full_size and trade.conviction >= limits.cap_bypass_min_conviction)
    curve_pct = curve_fraction(trade.conviction, limits.conviction_size_curve, limits.max_trade_pct)
    # the soft per-trade fraction = the curve value, but never above the base cap UNLESS this idea
    # has earned the bypass (confident). This is what keeps low conviction from up-sizing even if
    # a future curve edit set a high number for it without also clearing the bypass threshold.
    soft_pct = curve_pct if confident else min(curve_pct, limits.max_trade_pct)
    # Regime-aware sizing: lean BIGGER into a bullish idea in a confirmed bull (mult > 1 only for
    # is_long in a bull; 1.0 otherwise). Caps below still bind -- buying power, concurrent, breaker,
    # and the scaled % cap is floored at available_funds so it can never exceed cash.
    size_mult = regime_mod.size_multiplier(regime_info, getattr(trade, "is_long", True))
    per_trade_cap = min(soft_pct * pot * size_mult, available_funds)
    # HARD per-trade ceiling -- clamps EVERY path (confident + regime size-up + curve included). A
    # single trade can never exceed max_trade_pct_hard of NetLiq, no matter the conviction or regime.
    hard_cap = limits.max_trade_pct_hard * pot
    per_trade_cap = min(per_trade_cap, hard_cap, available_funds)
    # 5%-CASH-BUFFER CLAMP (applied LAST, after the curve + hard ceiling): always keep a slice of
    # NetLiq (account VALUE) liquid. deployable = available_funds - cash_buffer_pct*NetLiq. A
    # whole-pot conviction-10 trade therefore caps near 95% of cash, never literally $0 -- intended.
    cash_floor = max(0.0, limits.cash_buffer_pct) * pot
    deployable = max(0.0, available_funds - cash_floor)
    per_trade_cap = min(per_trade_cap, deployable)

    # 1a. blocklist -- specific single-name tickers are always rejected (index ETFs exempt)
    if u not in INDEX_UNDERLYINGS and u in {n.upper() for n in limits.blocked_names}:
        reasons.append(f"{u} is on the blocklist (excluded single name)")

    # 1b. universe -- index ETFs always allowed; single names must be pre-approved
    #     unless allow_any_name opens the universe (checks 2-6 and per-entry approval still bind)
    if (not limits.allow_any_name
            and u not in INDEX_UNDERLYINGS
            and u not in {n.upper() for n in approved_names}):
        reasons.append(f"{u} not in allowed universe (SPY/QQQ/IWM or an approved single name)")

    # 2. per-trade size cap (computed off the LIVE pot) -- ALWAYS enforced now, even when confident:
    #    per_trade_cap is already clamped to the hard 25%-of-NetLiq ceiling above, so no override
    #    (confident or regime size-up) can spend more than that. The "12%-of-pot" wording is kept
    #    for the soft path; when the hard ceiling is what's binding it reads as that %.
    if trade.notional > per_trade_cap + EPS:
        binding_pct = limits.max_trade_pct if not confident else limits.max_trade_pct_hard
        reasons.append(f"notional ${trade.notional:,.0f} exceeds {binding_pct:.0%}-of-pot cap ${per_trade_cap:,.0f}")
        # note: per_trade_cap may also be bound here by the 5% cash buffer or the 25% hard ceiling,
        # whichever is tightest -- the wording above reflects the soft/confident % path.

    # 3. buying power -- ALWAYS enforced (can't spend cash you don't have), confident or not
    if trade.notional > available_funds + EPS:
        reasons.append(f"notional ${trade.notional:,.0f} exceeds available funds ${available_funds:,.0f}")

    # 4. concurrent position cap
    if len(open_positions) >= limits.max_concurrent:
        reasons.append(f"at max concurrent positions ({len(open_positions)}/{limits.max_concurrent})")

    # 5. daily-loss circuit breaker
    dp = day_pnl_pct(pot, pot_day_start) if pot_day_start > 0 else 0.0
    if dp <= -limits.daily_halt_pct + EPS:
        reasons.append(f"daily circuit breaker: pot down {dp:.1%} (halt at -{limits.daily_halt_pct:.0%}) - no new entries")

    # 6. aggregate single-name exposure cap -- skipped for a confident override
    if not confident and not trade.is_index:
        name_exposure = sum(p.notional for p in open_positions if not p.is_index) + trade.notional
        name_cap = limits.max_single_name_agg_pct * pot
        if name_exposure > name_cap + EPS:
            reasons.append(f"single-name exposure ${name_exposure:,.0f} would exceed {limits.max_single_name_agg_pct:.0%}-of-pot cap ${name_cap:,.0f}")

    # 7. reward:risk gate -- never take a trade risking more than it targets. Only enforced when
    #    BOTH levels are present (>0); a 0 means "use the default rule" so we don't second-guess it.
    tp = getattr(trade, "profit_target_pct", 0.0) or 0.0
    sl = getattr(trade, "stop_pct", 0.0) or 0.0
    if tp > 0 and sl > 0 and sl > tp + EPS:
        reasons.append(f"reward:risk inverted: stop {sl:.0f}% exceeds profit target {tp:.0f}% (need target >= stop)")

    return GateDecision(approved=not reasons, reasons=reasons, pot_value=pot, per_trade_cap=per_trade_cap)
