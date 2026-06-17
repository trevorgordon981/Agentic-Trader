"""Hard risk gate for the LLM trading system.

This is the SOLVENCY layer and is NOT the LLM -- every limit is enforced here in code.
The LLM proposes; this gate disposes. All caps are computed off the LIVE pot value passed
in each evaluation, so position sizing scales automatically with the account.

Confirmed limits (2026-06-11): <=12% of pot per trade, <=4 concurrent positions,
halt new entries at -8% on the day, index = SPY/QQQ/IWM, single names must be user-approved.
"""
from dataclasses import dataclass, field
from typing import List, Optional, Set

INDEX_UNDERLYINGS: Set[str] = {"SPY", "QQQ", "IWM"}


@dataclass
class RiskLimits:
    max_trade_pct: float = 0.12            # <=12% of the pot per single trade
    max_concurrent: int = 4                # <=4 open positions at once
    daily_halt_pct: float = 0.08           # halt NEW entries when pot is down >=8% on the day
    max_single_name_agg_pct: float = 0.36  # aggregate cap on single-name (non-index) exposure
    pot_cap_usd: Optional[float] = None    # optional ring-fence ceiling for sizing
    allow_any_name: bool = False           # model may propose names beyond approved_names
                                           # (all other caps + per-entry approval still bind)
    confident_full_size: bool = False      # if a high-conviction idea may use the WHOLE pot,
                                           # bypassing the % size caps (Trevor's call 2026-06-12)
    confident_conviction: int = 4          # conviction >= this counts as "confident"
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


def evaluate_trade(
    trade: ProposedTrade,
    *,
    net_liq: float,
    available_funds: float,
    open_positions: List[OpenPosition],
    pot_day_start: float,
    approved_names: Set[str],
    limits: RiskLimits,
) -> GateDecision:
    """Pure risk gate. Returns approved + every reason it was blocked. ALL checks run so the
    caller can log the full picture (we don't short-circuit on the first failure)."""
    reasons: List[str] = []
    pot = effective_pot(net_liq, limits.pot_cap_usd)
    EPS = 1e-9
    u = trade.underlying.upper()

    # CONFIDENT OVERRIDE: a high-conviction idea may use the whole pot when enabled. This lifts
    # ONLY the % SIZE caps (#2 per-trade, #6 single-name aggregate). Buying power (#3), the
    # concurrent cap (#4), the daily circuit breaker (#5), and the mandatory human approval on
    # every entry all still apply -- you remain the sizing control by seeing the % of pot before
    # you tap. (Trevor 2026-06-12: "don't care about sizing if the model is confident.")
    confident = (limits.confident_full_size and trade.conviction >= limits.confident_conviction)
    per_trade_cap = available_funds if confident else limits.max_trade_pct * pot

    # 1a. blocklist -- specific single-name tickers are always rejected (index ETFs exempt)
    if u not in INDEX_UNDERLYINGS and u in {n.upper() for n in limits.blocked_names}:
        reasons.append(f"{u} is on the blocklist (excluded single name)")

    # 1b. universe -- index ETFs always allowed; single names must be pre-approved
    #     unless allow_any_name opens the universe (checks 2-6 and per-entry approval still bind)
    if (not limits.allow_any_name
            and u not in INDEX_UNDERLYINGS
            and u not in {n.upper() for n in approved_names}):
        reasons.append(f"{u} not in allowed universe (SPY/QQQ/IWM or an approved single name)")

    # 2. per-trade size cap (computed off the LIVE pot) -- skipped for a confident override
    if not confident and trade.notional > per_trade_cap + EPS:
        reasons.append(f"notional ${trade.notional:,.0f} exceeds {limits.max_trade_pct:.0%}-of-pot cap ${per_trade_cap:,.0f}")

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

    return GateDecision(approved=not reasons, reasons=reasons, pot_value=pot, per_trade_cap=per_trade_cap)
