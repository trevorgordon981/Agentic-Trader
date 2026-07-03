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
    trigger_type: str  # "profit_target", "stop", "time_stop", "trailing_stop", "scale_out"
    current_price: float  # per share
    entry_debit: float  # total dollars paid at entry
    current_value: float  # total current value (price * 100 * qty)
    pnl_pct: float  # profit/loss as percentage of entry cost
    message: str
    # Fraction of the CURRENT position quantity this trigger asks the caller to close.
    # 1.0 == full close (the historical behavior for every existing trigger, so this is
    # BACKWARD-COMPATIBLE). A "scale_out" trim sets this < 1.0 so the manager closes only
    # part of the position and lets the runner keep going. See evaluate_scale_out().
    quantity_fraction: float = 1.0
    # TAKE-PROFIT-AND-RELOAD (2026-07-03, ADDITIVE). Set ONLY on a model take_profit that also
    # signalled a same-name re-entry (reload=true). The manager writes a fill-gated reload TICKET
    # (never fires an order itself) after the close CONFIRMS Filled; the trader later drains that
    # ticket into a normal, human-approved suggestion. Both default off => byte-identical for every
    # existing trigger. reload_conviction is the MODEL's read (1-10) of continuation strength.
    reload: bool = False
    reload_conviction: Optional[float] = None


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


def evaluate_scale_out(
    current_price: float,
    entry_debit: float,
    quantity: int,
    first_target_pct: float,
    trim_fraction: float,
    already_trimmed: bool = False,
) -> Optional[ExitTrigger]:
    """
    Partial-trim (scale-out) rule: at a FIRST target below the full profit target, take
    part of the position off the table and let the remainder run.

    Fires when:
      * gain >= first_target_pct, AND
      * this position has not already been trimmed (`already_trimmed` False), AND
      * quantity >= 2, so trimming leaves at least one contract as a runner.

    Returns an ExitTrigger with trigger_type="scale_out" and quantity_fraction=trim_fraction.
    The trigger is a PARTIAL: the caller must close round(quantity * trim_fraction) contracts
    (clamped to leave >=1 runner) and KEEP managing the remainder. `current_value`/`pnl_pct`
    are reported on the FULL current position (unchanged contract vs the other triggers); the
    caller derives the trimmed dollar amount from quantity_fraction.
    """
    if quantity < 2:
        # Can't leave a runner -- let the full profit target handle the exit instead.
        return None
    if already_trimmed:
        return None

    entry_per_share = entry_debit / (100.0 * quantity)
    if entry_per_share <= 0:
        return None

    target_price = entry_per_share * (1 + first_target_pct / 100.0)
    if current_price < target_price:
        return None

    current_value = current_price * 100 * quantity
    pnl_pct = (current_value - entry_debit) / entry_debit * 100.0
    return ExitTrigger(
        con_id=0,
        trigger_type="scale_out",
        current_price=current_price,
        entry_debit=entry_debit,
        current_value=current_value,
        pnl_pct=pnl_pct,
        message=(f"Scale-out first target hit: price={current_price:.4f} >= "
                 f"target={target_price:.4f} (entry={entry_per_share:.4f}); "
                 f"trim {trim_fraction:.0%} of {quantity}, let runner ride"),
        quantity_fraction=trim_fraction,
    )


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
    Evaluate trailing stop -- protects REALIZED gains after activation.

    Activation: arms once price has risen by activation_gain_pct above entry (measured at the
    peak, so a position that touched the activation level stays armed even if it has since
    dipped a touch -- peak_price is monotonic).

    Trigger: once armed, exit if the current price has given back more than giveback_fraction
    of the PEAK GAIN ABOVE ENTRY. i.e. the protected floor is:

        trigger_price = entry_per_share + (peak_price - entry_per_share) * (1 - giveback_fraction)

    This is the improvement over the previous version, which measured the giveback off the
    ACTIVATION price -- so it only protected the sliver of gain above activation and would
    stop out on tiny wiggles right after arming. Basing the band on (peak - entry) makes the
    trail a true percentage-of-realized-gain trail: e.g. giveback_fraction=0.4 always keeps at
    least 60% of the peak gain, whether the peak was +25% or +150%.
    """
    if quantity <= 0 or entry_debit <= 0:
        return None

    entry_per_share = entry_debit / (100.0 * quantity)
    if entry_per_share <= 0:
        return None

    # Activation threshold -- arm only after the peak cleared the activation gain.
    activation_price = entry_per_share * (1 + activation_gain_pct / 100.0)
    if peak_price < activation_price:
        # Never armed.
        return None

    # Protected floor: keep (1 - giveback) of the peak gain measured from ENTRY.
    peak_gain = peak_price - entry_per_share
    if peak_gain <= 0:
        return None
    max_retracement = peak_gain * giveback_fraction
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
            message=(f"Trailing stop hit: price={current_price:.4f} <= trigger={trigger_price:.4f} "
                     f"(peak={peak_price:.4f}, entry={entry_per_share:.4f}, "
                     f"keep {(1 - giveback_fraction):.0%} of peak gain)"),
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
    already_trimmed: bool = False,
) -> Optional[ExitTrigger]:
    """
    Evaluate all active exit rules for a position.
    Returns the highest-priority triggered exit, or None if no rule is triggered.

    `already_trimmed` should be supplied by the caller from persisted state (has this
    position already had a scale-out trim?). It defaults False for backward compatibility;
    the manager MUST pass the real flag once the scale-out hook is wired, otherwise a
    scale-out would re-fire every cycle. See the module docstring / handoff notes.
    """
    triggers: List[ExitTrigger] = []

    # Profit target (full exit)
    if rules.profit_target_pct is not None and rules.profit_target_pct > 0:
        trigger = evaluate_profit_target(
            current_price, entry_debit, quantity, rules.profit_target_pct
        )
        if trigger:
            trigger.con_id = con_id
            triggers.append(trigger)

    # Scale-out (partial trim at a first target below the full profit target)
    if (getattr(rules, "scale_out", None) is not None and rules.scale_out.enabled
            and rules.scale_out.first_target_pct is not None
            and rules.scale_out.first_target_pct > 0):
        trigger = evaluate_scale_out(
            current_price, entry_debit, quantity,
            rules.scale_out.first_target_pct,
            rules.scale_out.trim_fraction,
            already_trimmed=already_trimmed,
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

    # Return highest-priority trigger.
    if triggers:
        # Priority: full-exit / risk rules OUTRANK the partial scale-out. If a position is
        # simultaneously at the full profit target (or a stop/trailing full-exit) AND the
        # scale-out level, take the full action -- there is nothing left worth "letting run".
        # scale_out sits just above time_stop so it still fires in the band between the first
        # target and the full profit target, where it does its job.
        priority = {
            "profit_target": 1,
            "trailing_stop": 2,
            "stop": 3,
            "scale_out": 4,
            "time_stop": 5,
        }
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
