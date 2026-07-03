"""Hard risk gate for the LLM trading system.

This is the SOLVENCY layer and is NOT the LLM -- every limit is enforced here in code.
The LLM proposes; this gate disposes. All caps are computed off the LIVE pot value passed
in each evaluation, so position sizing scales automatically with the account.

LIVE production limits (2026-07-03, from config.yaml -> run_trader.py): <=25% of pot per trade
(soft cap == the 25% hard ceiling in the current config), <=8 concurrent positions, halt new
entries at -20% on the day, index = SPY/QQQ/IWM, single names must be user-approved. (The RiskLimits
dataclass DEFAULTS below are the older 12% / 4 / -8% fallback and are OVERRIDDEN by config at
construction; the values above are what actually binds in production.)
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
    max_trade_pct: float = 0.12            # dataclass default; LIVE config sets 0.25 (<=25% of the
                                           # pot per single trade, soft/regime-scalable)
    max_trade_pct_hard: float = 0.25       # ABSOLUTE ceiling: no single trade may EVER exceed this
                                           # % of NetLiq -- binds on every path incl. confident +
                                           # regime size-up (Trevor 2026-06-22 risk-gate hardening)
    max_concurrent: int = 4                # dataclass default; LIVE config sets 8 open positions
    daily_halt_pct: float = 0.08           # dataclass default; LIVE config sets 0.20 -- halt NEW
                                           # entries when the pot is down >=20% on the day
    max_single_name_agg_pct: float = 0.36  # aggregate cap on single-name (non-index) exposure
    max_sector_agg_pct: float = 0.25       # aggregate cap on a CORRELATED sector/cluster (a SUBSET
                                           # of the single-name book -- e.g. NVDA+AMD+MU are one
                                           # semis macro-bet). Deliberately BELOW max_single_name_agg_pct
                                           # (0.36) so it actually BINDS: a value >= 0.36 would be dead
                                           # code, since the whole non-index book is already capped at
                                           # 0.36. At ~2x the 0.12 soft per-trade cap it lets ~two
                                           # soft-sized correlated names coexist but blocks a third
                                           # concentrating the same macro bet. Tune in config.yaml.
    sector_map: Dict[str, str] = field(default_factory=dict)  # UPPERCASE symbol -> sector/cluster
                                           # id. STATIC, Trevor-editable map from config.yaml -- the
                                           # pragmatic no-network source (a live correlation/beta feed
                                           # is a future upgrade). Unmapped symbols key to their own
                                           # name => no clustering (behaves like today). EMPTY map =>
                                           # the whole sector check is a no-op, so old configs are
                                           # unchanged (backward compatible).
    pot_cap_usd: Optional[float] = None    # optional ring-fence ceiling for sizing
    cash_buffer_pct: float = 0.05          # ALWAYS keep this fraction of NetLiq (account VALUE,
                                           # not buying power) liquid. The final $ size is clamped to
                                           # available_funds - cash_buffer_pct*NetLiq, so even a
                                           # whole-pot conviction-10 trade leaves ~5% cash. (2026-06-22)
    allow_any_name: bool = False           # model may propose names beyond approved_names
                                           # (all other caps + per-entry approval still bind)
    confident_full_size: bool = False      # if a high-conviction idea may use the WHOLE pot,
                                           # bypassing the % size caps (Trevor's call 2026-06-12)
    cap_bypass_min_conviction: int = 6     # conviction >= this may EXCEED the soft base cap (25% live) toward the
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
    conviction_size_multipliers: Optional[Dict[int, float]] = None
                                           # EMPIRICAL, GATED size multiplier per conviction, fit
                                           # from REAL closed-trade outcomes by
                                           # calibrate_conviction_sizing.py (conviction -> multiplier
                                           # on the per-trade SOFT size). PROPOSE-ONLY / opt-in.
                                           # None/empty => 1.0 for EVERY conviction => sizing is
                                           # byte-identical to today (pure no-op read). A PURE size
                                           # scale: it changes NO risk gate (caps/breaker/
                                           # concentration). Up-multipliers still bind on the hard
                                           # ceiling (max_trade_pct_hard) + cash buffer below.
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
    conviction: int = 1    # 1-10, from the strategist (see strategist.py: clamped to 1..10, the
                           # documented 1-10 rubric); drives the confident-sizing bypass. The
                           # cap-bypass threshold (cap_bypass_min_conviction=6) and the 1-10-keyed
                           # DEFAULT_CONVICTION_SIZE_CURVE are on this SAME 1-10 scale, so >=6
                           # ("medium"/"high") genuinely arrives and the bypass is reachable --
                           # it is NOT dead code. (Was mis-documented as "1-5" pre-2026-07-02.)
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


def sector_of(symbol: str, sector_map: Optional[Dict[str, str]]) -> str:
    """Cluster id for a symbol. Falls back to the symbol's own (uppercased) name when the map is
    empty or the symbol is unmapped -- so an unmapped name is its own singleton cluster and is
    NOT grouped with anything else (i.e. no clustering => behaves like today)."""
    s = (symbol or "").upper()
    if not sector_map:
        return s
    return sector_map.get(s, s)


def sector_exposure(
    open_positions: List["OpenPosition"],
    candidate_symbol: str,
    candidate_debit: float,
    sector_map: Optional[Dict[str, str]],
) -> Dict[str, float]:
    """Pure: aggregate non-index premium-at-risk ($ notional), grouped by sector/cluster, INCLUDING
    the candidate trade's debit added to its own cluster. 'Aggregate' == premium at risk (same
    $ notional the single-name-agg cap sums), NOT net-liq %. Index positions are excluded (index
    exposure is not a single-name/sector concentration concern). Unmapped symbols key to their own
    uppercased name, so they only ever cluster with other positions in the SAME name.

    Returns {sector_id: aggregate_$}. The caller compares the candidate's own sector's aggregate
    against max_sector_agg_pct * pot -- mirroring the single-name-agg enforcement."""
    agg: Dict[str, float] = {}
    for p in open_positions:
        if getattr(p, "is_index", False):
            continue
        sec = sector_of(p.underlying, sector_map)
        agg[sec] = agg.get(sec, 0.0) + p.notional
    if candidate_symbol:
        sec = sector_of(candidate_symbol, sector_map)
        agg[sec] = agg.get(sec, 0.0) + candidate_debit
    return agg


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


def conviction_multiplier(conviction: int, mult_map: Optional[Dict[int, float]]) -> float:
    """Empirical, GATED per-trade SIZE multiplier for this conviction, read from the optional
    calibrated `conviction_size_multipliers` map (fit from REAL closed-trade outcomes by
    calibrate_conviction_sizing.py). Returns 1.0 (flat / unchanged) when the map is empty/None or
    the conviction is unmapped -- an unmapped conviction NEVER up-sizes. Out-of-range convictions
    clamp to the nearest defined endpoint. This is a PURE size scale: it alters no risk gate, and
    an empty/None map makes the caller's sizing byte-identical to today."""
    if not mult_map:
        return 1.0
    try:
        c = int(conviction)
    except (TypeError, ValueError):
        return 1.0
    if c in mult_map:
        return float(mult_map[c])
    keys = sorted(int(k) for k in mult_map.keys())
    if not keys:
        return 1.0
    if c < keys[0]:
        return float(mult_map[keys[0]])
    if c > keys[-1]:
        return float(mult_map[keys[-1]])
    return 1.0  # unmapped middle value -> flat (never silently up-sizes)


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
    # ships FLAT at the soft base cap (limits.max_trade_pct; 25% in live config), so this is
    # sizing-equivalent to today. The cap-bypass below only lets genuine mid/high conviction
    # (>= cap_bypass_min_conviction) lift PAST the soft base cap toward the hard ceiling -- low
    # conviction (e.g. 3-5) can never exceed the base cap.
    # Every path still binds on: buying power (#3), the cash buffer, the concurrent cap (#4), the
    # daily circuit breaker (#5), the per-trade HARD ceiling (#2 below), and human approval.
    confident = (limits.confident_full_size and trade.conviction >= limits.cap_bypass_min_conviction)
    curve_pct = curve_fraction(trade.conviction, limits.conviction_size_curve, limits.max_trade_pct)
    # the soft per-trade fraction = the curve value, but never above the base cap UNLESS this idea
    # has earned the bypass (confident). This is what keeps low conviction from up-sizing even if
    # a future curve edit set a high number for it without also clearing the bypass threshold.
    soft_pct = curve_pct if confident else min(curve_pct, limits.max_trade_pct)
    # CONVICTION SIZE MULTIPLIER (empirical, GATED calibration -- PROPOSE-ONLY until opt-in). A
    # PURE READ of the optional conviction_size_multipliers map (fit from REAL closed-trade
    # outcomes; below-threshold buckets stay 1.0). Scales ONLY the soft per-trade fraction; the
    # hard ceiling (#2), cash buffer, buying power (#3), concurrent cap (#4), daily breaker (#5)
    # and concentration caps (#6/#6b) below ALL still bind unchanged. Down-multipliers shrink size;
    # up-multipliers can lift the soft fraction toward -- but never past -- the hard ceiling
    # (clamped below). Empty/None map => this block is a no-op and sizing is byte-identical to today.
    conv_size_mult = conviction_multiplier(
        trade.conviction, getattr(limits, "conviction_size_multipliers", None))
    if conv_size_mult != 1.0:
        soft_pct = soft_pct * conv_size_mult
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
    #    (confident or regime size-up) can spend more than that. The reject message formats the
    #    binding % dynamically from limits.max_trade_pct (25% live) / max_trade_pct_hard.
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

    # 6. aggregate single-name exposure cap (max_single_name_agg_pct, default 36% of pot).
    #    INTENTIONALLY skipped when `confident` (confident_full_size on AND conviction >=
    #    cap_bypass_min_conviction): a confident override relaxes BOTH the per-trade % cap AND this
    #    concentration cap. Consequence to be aware of: two confident single-name trades can each
    #    reach the 25%-of-NetLiq hard per-trade ceiling (#2), so confident single-name exposure can
    #    stack toward ~50% of NetLiq -- ABOVE the 36% aggregate cap that binds in non-confident mode.
    #    It is still bounded on every path by: the 25% hard per-trade ceiling (#2), buying power (#3),
    #    the 5% cash buffer, the concurrent-position cap (#4, default 4), the daily breaker (#5), and
    #    human approval. NOTE: this relaxation only takes effect when confident_full_size is enabled
    #    in config (default False in config.py -> today this branch always runs). Do NOT re-scope this
    #    to also honor concentration under confident mode without Trevor's sign-off (that TIGHTENS
    #    risk, but it is still a deliberate behavior change on a real-money gate).
    if not confident and not trade.is_index:
        name_exposure = sum(p.notional for p in open_positions if not p.is_index) + trade.notional
        name_cap = limits.max_single_name_agg_pct * pot
        if name_exposure > name_cap + EPS:
            reasons.append(f"single-name exposure ${name_exposure:,.0f} would exceed {limits.max_single_name_agg_pct:.0%}-of-pot cap ${name_cap:,.0f}")

    # 6b. aggregate SECTOR / correlated-cluster exposure cap (max_sector_agg_pct, default 25% of
    #     pot). Mirrors the single-name-agg cap (#6) exactly, but groups CORRELATED names into one
    #     macro bet (e.g. NVDA + AMD + MU sail through #6 as three names while being one semis bet).
    #     The grouping comes from a STATIC, Trevor-editable sector_map (symbol -> cluster) in
    #     config.yaml -- the pragmatic no-network source; a live correlation/beta feed is a future
    #     upgrade. Unmapped symbols key to their own name (no clustering => same as today), and an
    #     EMPTY sector_map (or a non-positive cap) makes this whole check a no-op so old configs are
    #     unchanged. Skipped for index ETFs and -- like #6 -- when `confident` (a confident override
    #     relaxes the concentration caps; every other gate in this function still binds). See
    #     sector_exposure() for the pure aggregation.
    if (not confident and not trade.is_index
            and limits.sector_map and limits.max_sector_agg_pct > 0):
        sec = sector_of(u, limits.sector_map)
        sec_exposure = sector_exposure(open_positions, u, trade.notional, limits.sector_map).get(sec, 0.0)
        sec_cap = limits.max_sector_agg_pct * pot
        if sec_exposure > sec_cap + EPS:
            reasons.append(f"sector '{sec}' exposure ${sec_exposure:,.0f} would exceed {limits.max_sector_agg_pct:.0%}-of-pot cap ${sec_cap:,.0f}")

    # 7. reward:risk gate -- never take a trade risking more than it targets. Only enforced when
    #    BOTH levels are present (>0); a 0 means "use the default rule" so we don't second-guess it.
    #    ACCEPTED INVERTED R:R ON SMALL POTS (INTENTIONAL -- do NOT extend this gate): this checks
    #    only the MODEL-PROVIDED tp/sl. It does NOT (and must not) re-check the pot-tier DEFAULT tp
    #    applied later in construction.clamp_tp_sl, where a sub-$5k pot deliberately runs a 20-25% TP
    #    below the 30% stop (bank fast out of down days; stop still holds at 30%). Leaving both levels
    #    at 0 (use-the-default path) intentionally bypasses this gate -- that accepted case is by design.
    tp = getattr(trade, "profit_target_pct", 0.0) or 0.0
    sl = getattr(trade, "stop_pct", 0.0) or 0.0
    if tp > 0 and sl > 0 and sl > tp + EPS:
        reasons.append(f"reward:risk inverted: stop {sl:.0f}% exceeds profit target {tp:.0f}% (need target >= stop)")

    return GateDecision(approved=not reasons, reasons=reasons, pot_value=pot, per_trade_cap=per_trade_cap)
