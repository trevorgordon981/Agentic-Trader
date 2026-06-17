"""Exit rule evaluation logic."""

import math
from dataclasses import dataclass
from typing import Optional, Dict, List
from datetime import datetime

from exitmgr.config import RulesConfig


@dataclass
class ExitTrigger:
    """Represents an exit trigger for a position."""
    con_id: int
    trigger_type: str  # "profit_target", "stop", "time_stop", "trailing_stop"
    current_price: float  # per share
    entry_debit: float  # total dollars paid at entry
    current_value: float  # total current value (price * 100 * qty)
    pnl_pct: float  # profit/loss as percentage of entry cost
    message: str


def evaluate_profit_target(
    current_price: float,
    entry_debit: float,
    quantity: int,
    profit_target_pct: float,
) -> Optional[ExitTrigger]:
    """Check if profit target is hit."""
    # Entry cost per share = entry_debit / (100 * quantity)
    if quantity <= 0:
        return None

    entry_per_share = entry_debit / (100.0 * quantity)
    if entry_per_share <= 0:
        return None

    # Profit target price per share
    target_price = entry_per_share * (1 + profit_target_pct / 100.0)

    if current_price >= target_price:
        current_value = current_price * 100 * quantity
        pnl_pct = (current_value - entry_debit) / entry_debit * 100.0
        return ExitTrigger(
            con_id=0,  # Will be set by caller
            trigger_type="profit_target",
            current_price=current_price,
            entry_debit=entry_debit,
            current_value=current_value,
            pnl_pct=pnl_pct,
            message=f"Profit target hit: price={current_price:.4f} >= target={target_price:.4f} (entry={entry_per_share:.4f})",
        )
    return None


def evaluate_stop(
    current_price: float,
    entry_debit: float,
    quantity: int,
    stop_pct: float,
) -> Optional[ExitTrigger]:
    """Check if stop loss is hit."""
    if quantity <= 0:
        return None

    entry_per_share = entry_debit / (100.0 * quantity)
    if entry_per_share <= 0:
        return None

    # Stop price per share (exit at loss of stop_pct)
    stop_price = entry_per_share * (1 - stop_pct / 100.0)

    if current_price <= stop_price:
        current_value = current_price * 100 * quantity
        pnl_pct = (current_value - entry_debit) / entry_debit * 100.0
        return ExitTrigger(
            con_id=0,
            trigger_type="stop",
            current_price=current_price,
            entry_debit=entry_debit,
            current_value=current_value,
            pnl_pct=pnl_pct,
            message=f"Stop hit: price={current_price:.4f} <= stop={stop_price:.4f} (entry={entry_per_share:.4f})",
        )
    return None


def evaluate_time_stop(
    current_price: float,
    entry_debit: float,
    quantity: int,
    days_to_expiry: Optional[int],
    time_stop_days: int,
) -> Optional[ExitTrigger]:
    """Check if time stop is hit (DTE <= N)."""
    if days_to_expiry is None:
        # Can't evaluate without DTE - skip
        return None

    if days_to_expiry <= time_stop_days:
        current_value = current_price * 100 * quantity
        pnl_pct = (current_value - entry_debit) / entry_debit * 100.0 if entry_debit > 0 else 0
        return ExitTrigger(
            con_id=0,
            trigger_type="time_stop",
            current_price=current_price,
            entry_debit=entry_debit,
            current_value=current_value,
            pnl_pct=pnl_pct,
            message=f"Time stop hit: DTE={days_to_expiry} <= {time_stop_days}",
        )
    return None


def evaluate_trailing_stop(
    current_price: float,
    entry_debit: float,
    quantity: int,
    peak_price: float,
    activation_gain_pct: float,
    giveback_fraction: float,
) -> Optional[ExitTrigger]:
    """
    Evaluate trailing stop.
    Activation: after price rises by activation_gain_pct above entry.
    Trigger: if price gives back giveback_fraction of peak gain.
    """
    if quantity <= 0 or entry_debit <= 0:
        return None

    entry_per_share = entry_debit / (100.0 * quantity)
    if entry_per_share <= 0:
        return None

    # Activation threshold
    activation_price = entry_per_share * (1 + activation_gain_pct / 100.0)

    if current_price < activation_price:
        # Not yet activated
        return None

    # Peak-to-current drawdown
    if peak_price <= activation_price:
        # Peak not above activation - can't trigger
        return None

    # Max allowed retracement from peak
    max_retracement = (peak_price - activation_price) * giveback_fraction
    trigger_price = peak_price - max_retracement

    if current_price <= trigger_price:
        current_value = current_price * 100 * quantity
        pnl_pct = (current_value - entry_debit) / entry_debit * 100.0
        return ExitTrigger(
            con_id=0,
            trigger_type="trailing_stop",
            current_price=current_price,
            entry_debit=entry_debit,
            current_value=current_value,
            pnl_pct=pnl_pct,
            message=f"Trailing stop hit: price={current_price:.4f} <= trigger={trigger_price:.4f} (peak={peak_price:.4f})",
        )
    return None


def evaluate_position(
    con_id: int,
    symbol: str,
    quantity: int,
    entry_debit: float,
    current_price: float,
    days_to_expiry: Optional[int],
    peak_price: Optional[float],
    rules: RulesConfig,
) -> Optional[ExitTrigger]:
    """
    Evaluate all active exit rules for a position.
    Returns the first triggered exit, or None if no rule is triggered.
    """
    triggers: List[ExitTrigger] = []

    # Profit target
    if rules.profit_target_pct is not None and rules.profit_target_pct > 0:
        trigger = evaluate_profit_target(
            current_price, entry_debit, quantity, rules.profit_target_pct
        )
        if trigger:
            trigger.con_id = con_id
            triggers.append(trigger)

    # Stop loss
    if rules.stop_pct is not None and rules.stop_pct > 0:
        trigger = evaluate_stop(
            current_price, entry_debit, quantity, rules.stop_pct
        )
        if trigger:
            trigger.con_id = con_id
            triggers.append(trigger)

    # Time stop
    if rules.time_stop_days is not None and rules.time_stop_days > 0:
        trigger = evaluate_time_stop(
            current_price, entry_debit, quantity, days_to_expiry, rules.time_stop_days
        )
        if trigger:
            trigger.con_id = con_id
            triggers.append(trigger)

    # Trailing stop
    if rules.trailing.enabled and peak_price is not None:
        trigger = evaluate_trailing_stop(
            current_price, entry_debit, quantity, peak_price,
            rules.trailing.activation_gain_pct,
            rules.trailing.giveback_fraction,
        )
        if trigger:
            trigger.con_id = con_id
            triggers.append(trigger)

    # Return first trigger (could prioritize by type if needed)
    if triggers:
        # Sort by trigger type priority if needed (profit_target first, then trailing, then stop, then time)
        priority = {"profit_target": 1, "trailing_stop": 2, "stop": 3, "time_stop": 4}
        triggers.sort(key=lambda t: priority.get(t.trigger_type, 99))
        return triggers[0]

    return None


def calculate_pnl_pct(current_price: float, entry_debit: float, quantity: int) -> float:
    """Calculate current P&L as percentage of entry cost."""
    if entry_debit <= 0 or quantity <= 0:
        return 0.0
    current_value = current_price * 100 * quantity
    return (current_value - entry_debit) / entry_debit * 100.0


def days_to_expiry(expiry, today=None):
    """expiry = IB 'YYYYMMDD' (lastTradeDateOrContractMonth). Returns int DTE, or None."""
    if not expiry:
        return None
    from datetime import datetime, timezone
    try:
        d = datetime.strptime(str(expiry)[:8], "%Y%m%d").date()
    except (ValueError, TypeError):
        return None
    t0 = today or datetime.now(timezone.utc).date()
    return (d - t0).days
