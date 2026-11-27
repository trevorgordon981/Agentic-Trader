"""Paper-to-live graduation evaluator and neutral-before-fees alert formatter.

Graduation design:
  - Excludes synthetic/proxy exits (only broker-verified fills qualify).
  - Checks sample size, trading-day span, split-half stability, outlier-stripped
    profit factor, commission-adjusted live-ticket economics, and realized
    execution slippage.
  - NEVER auto-promotes a strategy to live; always returns
    ``requires_human_approval=True``.

Alert truth contract:
  - Exit alerts NEVER render a win/loss color or polarity tag while round-trip
    broker commissions/fees are pending (``fees_usd is None``). Pending-fee
    exits always emit a neutral status with explicit ``gross (fees pending)``
    text so gross-before-fees never misleads an operator.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Optional, Sequence


@dataclass(frozen=True)
class ClosedPaperTrade:
    """Single closed paper trade record for graduation evaluation."""

    trade_key: str
    symbol: str
    strategy_id: str
    direction: str
    session_day: str
    entry_price: float
    exit_price: float
    signal_price: Optional[float] = None
    entry_spread_bps: Optional[float] = None
    is_proxy_exit: bool = False
    live_rail_eligible: bool = True


@dataclass(frozen=True)
class GraduationContract:
    """Parameterized paper-to-live graduation thresholds."""

    min_closed_trades: int = 40
    min_trading_days: int = 30
    split_half_min_pf: float = 1.15
    outlier_drop_count: int = 2
    outlier_stripped_min_pf: float = 1.25
    min_live_equiv_pf: float = 1.60
    min_live_equiv_wr_pct: float = 60.0
    min_live_rail_trades: int = 20
    min_live_rail_pf: float = 1.50
    max_spread_bps: float = 20.0
    max_median_slippage_bps: float = 25.0
    slot_fraction: float = 0.20
    round_trip_commission_usd: float = 0.70
    probation_trades: int = 10
    probation_size_multiplier: float = 0.50
    probation_unlock_min_pf: float = 1.30
    probation_demote_min_trades: int = 15
    probation_demote_pf_floor: float = 1.00
    probation_max_dd_pct: float = 5.0


@dataclass(frozen=True)
class GraduationReport:
    """Result of evaluating a strategy against the paper-to-live contract."""

    strategy_id: str
    verified_trades: int
    proxy_exits_excluded: int
    trading_days: int
    live_equiv_pf: Optional[float]
    live_equiv_wr_pct: Optional[float]
    first_half_pf: Optional[float]
    second_half_pf: Optional[float]
    outlier_stripped_pf: Optional[float]
    live_rail_trades: int
    live_rail_pf: Optional[float]
    median_slippage_bps: Optional[float]
    passed_gates: dict[str, bool]
    eligible_for_human_review: bool
    requires_human_approval: bool = True
    auto_promoted: bool = False


@dataclass(frozen=True)
class ProbationDecision:
    """Live probationary sizing and auto-demote decision."""

    status: str  # "PROBATION_HALF" | "FULL_UNLOCKED" | "PROBATION_DEMOTED"
    size_multiplier: float
    allow_new_entries: bool
    reason: str
    live_trades: int
    live_pf: Optional[float]
    peak_to_trough_dd_pct: float


def _profit_factor(pnls: Sequence[float]) -> Optional[float]:
    if not pnls:
        return None
    gross_win = sum(p for p in pnls if p > 0.0)
    gross_loss = abs(sum(p for p in pnls if p < 0.0))
    if gross_loss == 0.0:
        return float("inf") if gross_win > 0.0 else 0.0
    return round(gross_win / gross_loss, 4)


def _live_equiv_trade(trade: ClosedPaperTrade, *, live_net_liq_usd: float, contract: GraduationContract) -> dict[str, object]:
    slot_notional = max(100.0, float(live_net_liq_usd) * contract.slot_fraction)
    qty = max(1, int(slot_notional // max(trade.entry_price, 0.01)))
    if trade.direction.upper() == "LONG":
        gross = (trade.exit_price - trade.entry_price) * qty
    else:
        gross = (trade.entry_price - trade.exit_price) * qty
    net = gross - contract.round_trip_commission_usd
    slippage_bps: Optional[float] = None
    if trade.signal_price is not None and trade.signal_price > 0:
        if trade.direction.upper() == "LONG":
            slippage_bps = ((trade.entry_price - trade.signal_price) / trade.signal_price) * 10000.0
        else:
            slippage_bps = ((trade.signal_price - trade.entry_price) / trade.signal_price) * 10000.0
    spread_ok = trade.entry_spread_bps is None or trade.entry_spread_bps <= contract.max_spread_bps
    return {
        "net_usd": net,
        "slippage_bps": slippage_bps,
        "live_rail_eligible": bool(trade.live_rail_eligible and spread_ok),
    }


def evaluate_paper_graduation(
    strategy_id: str,
    trades: Sequence[ClosedPaperTrade],
    *,
    live_net_liq_usd: float = 10000.0,
    contract: GraduationContract = GraduationContract(),
) -> GraduationReport:
    """Evaluate a candidate strategy's broker-verified paper track record."""
    if isinstance(live_net_liq_usd, bool) or not math.isfinite(live_net_liq_usd) or live_net_liq_usd <= 0:
        raise ValueError(f"live_net_liq_usd must be positive and finite, got {live_net_liq_usd!r}")

    verified = [t for t in trades if not t.is_proxy_exit]
    excluded = len(trades) - len(verified)
    ordered = sorted(verified, key=lambda t: (t.session_day, t.trade_key))
    evaluated = [_live_equiv_trade(t, live_net_liq_usd=live_net_liq_usd, contract=contract) for t in ordered]

    n = len(evaluated)
    days = len({t.session_day for t in ordered if t.session_day})
    pnls = [float(e["net_usd"]) for e in evaluated]
    pf = _profit_factor(pnls)
    wr = round((sum(1 for p in pnls if p > 0.0) / n) * 100.0, 2) if n > 0 else None

    first_pf = _profit_factor(pnls[: n // 2]) if n >= 4 else None
    second_pf = _profit_factor(pnls[n // 2 :]) if n >= 4 else None
    split_ok = (
        first_pf is not None
        and second_pf is not None
        and first_pf >= contract.split_half_min_pf
        and second_pf >= contract.split_half_min_pf
    )

    if n > contract.outlier_drop_count:
        top_wins = sorted([i for i, p in enumerate(pnls) if p > 0.0], key=lambda i: pnls[i], reverse=True)[
            : contract.outlier_drop_count
        ]
        drop_idx = set(top_wins)
        stripped_pf = _profit_factor([p for i, p in enumerate(pnls) if i not in drop_idx])
    else:
        stripped_pf = None
    outlier_ok = stripped_pf is not None and stripped_pf >= contract.outlier_stripped_min_pf

    rail_pnls = [float(e["net_usd"]) for e in evaluated if e["live_rail_eligible"]]
    rail_pf = _profit_factor(rail_pnls)
    slips = [float(e["slippage_bps"]) for e in evaluated if e["slippage_bps"] is not None]
    med_slip = round(statistics.median(slips), 2) if slips else None

    gate_sample = n >= contract.min_closed_trades and days >= contract.min_trading_days and split_ok and outlier_ok
    gate_econ = pf is not None and pf >= contract.min_live_equiv_pf and wr is not None and wr >= contract.min_live_equiv_wr_pct
    gate_rails = (
        len(rail_pnls) >= contract.min_live_rail_trades
        and rail_pf is not None
        and rail_pf >= contract.min_live_rail_pf
        and (med_slip is None or med_slip <= contract.max_median_slippage_bps)
    )

    passed = {
        "gate_1_sample_and_stability": gate_sample,
        "gate_2_live_ticket_economics": gate_econ,
        "gate_3_execution_rails": gate_rails,
    }
    return GraduationReport(
        strategy_id=strategy_id,
        verified_trades=n,
        proxy_exits_excluded=excluded,
        trading_days=days,
        live_equiv_pf=pf,
        live_equiv_wr_pct=wr,
        first_half_pf=first_pf,
        second_half_pf=second_pf,
        outlier_stripped_pf=stripped_pf,
        live_rail_trades=len(rail_pnls),
        live_rail_pf=rail_pf,
        median_slippage_bps=med_slip,
        passed_gates=passed,
        eligible_for_human_review=all(passed.values()),
        requires_human_approval=True,
        auto_promoted=False,
    )


def evaluate_live_probation(
    live_net_pnls_usd: Sequence[float],
    *,
    live_net_liq_usd: float,
    contract: GraduationContract = GraduationContract(),
) -> ProbationDecision:
    """Compute live probationary size multiplier or auto-demotion from realized net P&Ls."""
    if isinstance(live_net_liq_usd, bool) or not math.isfinite(live_net_liq_usd) or live_net_liq_usd <= 0:
        raise ValueError(f"live_net_liq_usd must be positive and finite, got {live_net_liq_usd!r}")
    pnls = [float(p) for p in live_net_pnls_usd]
    n = len(pnls)
    pf = _profit_factor(pnls)

    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        cum += p
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd
    dd_pct = round((max_dd / live_net_liq_usd) * 100.0, 2)

    if dd_pct > contract.probation_max_dd_pct:
        return ProbationDecision(
            status="PROBATION_DEMOTED",
            size_multiplier=0.0,
            allow_new_entries=False,
            reason=f"drawdown_exceeded({dd_pct:.2f}%>{contract.probation_max_dd_pct:.2f}%)",
            live_trades=n,
            live_pf=pf,
            peak_to_trough_dd_pct=dd_pct,
        )
    if n >= contract.probation_demote_min_trades and (pf is None or pf < contract.probation_demote_pf_floor):
        return ProbationDecision(
            status="PROBATION_DEMOTED",
            size_multiplier=0.0,
            allow_new_entries=False,
            reason=f"pf_below_floor({pf}<{contract.probation_demote_pf_floor})",
            live_trades=n,
            live_pf=pf,
            peak_to_trough_dd_pct=dd_pct,
        )
    if n >= contract.probation_trades and pf is not None and pf >= contract.probation_unlock_min_pf:
        return ProbationDecision(
            status="FULL_UNLOCKED",
            size_multiplier=1.0,
            allow_new_entries=True,
            reason=f"probation_passed(trades={n},pf={pf})",
            live_trades=n,
            live_pf=pf,
            peak_to_trough_dd_pct=dd_pct,
        )
    return ProbationDecision(
        status="PROBATION_HALF",
        size_multiplier=contract.probation_size_multiplier,
        allow_new_entries=True,
        reason=f"probation_active(trades={n}/{contract.probation_trades})",
        live_trades=n,
        live_pf=pf,
        peak_to_trough_dd_pct=dd_pct,
    )


def format_entry_alert(
    *,
    symbol: str,
    qty: int,
    fill_price: float,
    stop_price: float,
    strategy_id: str,
) -> str:
    """Format a structured entry fill alert."""
    return (
        f"[ENTRY] {symbol.upper()} BUY {qty} @ ${fill_price:.2f} | "
        f"stop=${stop_price:.2f} | strategy={strategy_id}"
    )


def format_stop_sync_alert(
    *,
    symbol: str,
    old_stop: float,
    new_stop: float,
    remaining_qty: int,
) -> str:
    """Format a structured protective stop tighten/sync alert."""
    return (
        f"[STOP_SYNC] {symbol.upper()} stop ${old_stop:.2f} -> ${new_stop:.2f} | "
        f"remaining_qty={remaining_qty}"
    )


def format_exit_alert(
    *,
    symbol: str,
    qty: int,
    entry_price: float,
    exit_price: float,
    strategy_id: str,
    reason: str,
    direction: str = "LONG",
    fees_usd: Optional[float] = None,
) -> dict[str, object]:
    """Format an exit alert adhering to the neutral-before-fees truth contract.

    When ``fees_usd`` is ``None`` (pending/unverified), ``tone`` is strictly
    ``"NEUTRAL"`` and ``text`` never classifies the trade as a win or loss.
    Only when verified finite non-negative ``fees_usd`` is provided does this
    compute net P&L and emit ``"WIN"`` / ``"LOSS"`` / ``"FLAT"``.
    """
    if qty <= 0:
        raise ValueError(f"qty must be positive, got {qty!r}")
    if isinstance(entry_price, bool) or not math.isfinite(entry_price) or entry_price <= 0:
        raise ValueError(f"entry_price must be positive and finite, got {entry_price!r}")
    if isinstance(exit_price, bool) or not math.isfinite(exit_price) or exit_price <= 0:
        raise ValueError(f"exit_price must be positive and finite, got {exit_price!r}")

    dir_u = direction.upper()
    gross_pnl = (exit_price - entry_price) * qty if dir_u == "LONG" else (entry_price - exit_price) * qty
    basis = entry_price * qty
    gross_pct = (gross_pnl / basis) * 100.0

    fees_verified = (
        fees_usd is not None
        and not isinstance(fees_usd, bool)
        and isinstance(fees_usd, (int, float))
        and math.isfinite(float(fees_usd))
        and float(fees_usd) >= 0.0
    )

    if fees_verified:
        fees_f = float(fees_usd)  # type: ignore[arg-type]
        net_pnl = round(gross_pnl - fees_f, 2)
        net_pct = round((net_pnl / basis) * 100.0, 2)
        tone = "WIN" if net_pnl > 0.0 else ("LOSS" if net_pnl < 0.0 else "FLAT")
        badge = "[EXIT_WIN]" if tone == "WIN" else ("[EXIT_LOSS]" if tone == "LOSS" else "[EXIT_FLAT]")
        pnl_text = (
            f"Net ${net_pnl:+.2f} ({net_pct:+.2f}%) "
            f"[gross ${gross_pnl:+.2f}, fees ${fees_f:.2f}]"
        )
        return {
            "tone": tone,
            "fees_verified": True,
            "gross_pnl_usd": round(gross_pnl, 2),
            "fees_usd": round(fees_f, 2),
            "net_pnl_usd": net_pnl,
            "text": (
                f"{badge} {symbol.upper()} {qty} sh @ ${exit_price:.2f} "
                f"(entry ${entry_price:.2f}) | {pnl_text} | {reason} | strategy={strategy_id}"
            ),
        }

    # Neutral-before-fees contract: never color or classify gross P&L as win/loss.
    pnl_text = f"Gross ${gross_pnl:+.2f} ({gross_pct:+.2f}%) -- gross (fees pending)"
    return {
        "tone": "NEUTRAL",
        "fees_verified": False,
        "gross_pnl_usd": round(gross_pnl, 2),
        "fees_usd": None,
        "net_pnl_usd": None,
        "text": (
            f"[EXIT_PENDING_FEES] {symbol.upper()} {qty} sh @ ${exit_price:.2f} "
            f"(entry ${entry_price:.2f}) | {pnl_text} | {reason} | strategy={strategy_id}"
        ),
    }
