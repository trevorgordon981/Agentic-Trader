"""Exit rule evaluation logic."""

import math
from dataclasses import dataclass
from typing import Optional, Dict, List, Union
from datetime import datetime, date, timedelta

from exitmgr.config import RulesConfig


# ------------------------------------------------------------------ EXCHANGE SESSION CALENDAR
# (2026-07-26, Sol audit R5 section R1.)  The confirmed-trail contract counts "two CONSECUTIVE
# completed regular trading sessions".  Consecutiveness is an EXCHANGE-CALENDAR question, not a
# calendar-day one: Friday -> Monday is consecutive, Friday -> Tuesday is not, and a session that
# produced no official closing mark is a GAP that breaks the streak (it is never inferred).
#
# Computed rather than table-driven so it does not silently rot at the end of a year.  Holidays
# NYSE observes: New Year's Day, MLK Day, Washington's Birthday, Good Friday, Memorial Day,
# Juneteenth, Independence Day, Labor Day, Thanksgiving, Christmas.  Ad-hoc full-day closures
# (national days of mourning, weather) have no rule -- add them to EXTRA_MARKET_CLOSURES.
#
# Half sessions (day after Thanksgiving, Christmas Eve) are REAL sessions with an official close;
# they are deliberately NOT excluded.  The recorder reads the closing mark well after 13:00 ET, so
# a half day is handled by the same path as a full day.
EXTRA_MARKET_CLOSURES: frozenset = frozenset()


def _easter_sunday(year: int) -> date:
    """Gregorian Easter (anonymous/Meeus algorithm).  Good Friday is two days earlier."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-th `weekday` (Mon=0) of `month`.  n<0 counts back from the end (-1 == last)."""
    if n > 0:
        d = date(year, month, 1)
        d += timedelta(days=(weekday - d.weekday()) % 7)
        return d + timedelta(days=7 * (n - 1))
    d = date(year, month, 28)
    while (d + timedelta(days=1)).month == month:
        d += timedelta(days=1)
    d -= timedelta(days=(d.weekday() - weekday) % 7)
    return d + timedelta(days=7 * (n + 1))


def _observed(d: date, *, roll_saturday_back: bool = True) -> Optional[date]:
    """Weekend observance.  Saturday -> the preceding Friday, Sunday -> the following Monday.
    New Year's Day is the exception the NYSE does NOT roll back into the prior year, so it passes
    roll_saturday_back=False and simply is not observed."""
    if d.weekday() == 5:
        return d - timedelta(days=1) if roll_saturday_back else None
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def nyse_holidays(year: int) -> frozenset:
    """Full-day NYSE closures observed in `year`."""
    out = set()

    def _add(d, **kw):
        o = _observed(d, **kw) if d is not None else None
        if o is not None and o.year == year:
            out.add(o)

    _add(date(year, 1, 1), roll_saturday_back=False)   # New Year's Day
    out.add(_nth_weekday(year, 1, 0, 3))               # MLK Day (3rd Monday, January)
    out.add(_nth_weekday(year, 2, 0, 3))               # Washington's Birthday (3rd Monday, Feb)
    out.add(_easter_sunday(year) - timedelta(days=2))  # Good Friday
    out.add(_nth_weekday(year, 5, 0, -1))              # Memorial Day (last Monday, May)
    _add(date(year, 6, 19))                            # Juneteenth
    _add(date(year, 7, 4))                             # Independence Day
    out.add(_nth_weekday(year, 9, 0, 1))               # Labor Day (1st Monday, September)
    out.add(_nth_weekday(year, 11, 3, 4))              # Thanksgiving (4th Thursday, November)
    _add(date(year, 12, 25))                           # Christmas
    # A Jan 1 that falls on a Sunday is observed on Jan 2 of the SAME year (covered above); a
    # Dec 31 Saturday observance for the NEXT year's New Year is deliberately not taken.
    return frozenset(out)


def _as_date(value: Union[str, date, datetime, None]) -> Optional[date]:
    """Coerce 'YYYY-MM-DD' / date / datetime to a date.  None on anything unusable -- callers
    treat an unusable session date as 'this session does not count'."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def is_trading_session(value) -> bool:
    """True when `value` is a regular NYSE trading session (weekday, not a full-day closure)."""
    d = _as_date(value)
    if d is None:
        return False
    if d.weekday() >= 5:
        return False
    if d in EXTRA_MARKET_CLOSURES:
        return False
    return d not in nyse_holidays(d.year)


def next_trading_session(value) -> Optional[date]:
    """The next regular trading session strictly after `value` (which need not be one itself)."""
    d = _as_date(value)
    if d is None:
        return None
    for _ in range(1, 15):   # the longest NYSE gap is a holiday-extended weekend
        d = d + timedelta(days=1)
        if is_trading_session(d):
            return d
    return None


def is_next_trading_session(prev, cur) -> bool:
    """True IFF `cur` is the very next trading session after `prev` -- i.e. the two sessions are
    CONSECUTIVE on the exchange calendar with no session skipped between them.  Friday -> Monday
    is True; Friday -> Tuesday is False (Monday was skipped)."""
    p, c = _as_date(prev), _as_date(cur)
    if p is None or c is None:
        return False
    return next_trading_session(p) == c


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
    peak_since_arm: Optional[float] = None,
    activation_gain_pct: float = 0.0,
    giveback_fraction: float = 0.0,
    *,
    armed: bool = False,
) -> Optional[ExitTrigger]:
    """
    Evaluate trailing stop -- protects REALIZED gains after the trail is ARMED.

    ARMING IS NOT DECIDED HERE (2026-07-26, Sol audit R5 R1).  This function REQUIRES the caller
    to pass the explicit, persisted armed state (`armed=True`, from
    ``State.is_trail_armed(con_id)``) and the post-arm ratchet price (`peak_since_arm`, from
    ``State.trail_peak_since_arm(con_id)``).  It may NO LONGER infer arming from a lifetime peak.

    Why: arming used to be "the monotonic lifetime peak once touched activation", i.e. a single
    INTRADAY tick.  A 365-DTE 0.60-delta option moves ~10-12% on an average day, so a tick-armed
    trail at +20% with giveback 0.4 sits well inside one day of noise and exits on wiggles rather
    than on a thesis change.  The trail now arms only after TWO CONSECUTIVE completed regular
    sessions CLOSED at or above the activation gain (see State.record_session_close).

    `peak_since_arm` starts at the SECOND qualifying CLOSE and ratchets up only thereafter, so a
    one-day pre-confirmation spike can never poison the floor and cause an immediate noise exit.
    The lifetime `state.peak_prices` remains audit/MFE evidence and is deliberately NOT read here.

    Trigger: once armed, exit if the current price has given back more than giveback_fraction
    of the PEAK GAIN ABOVE ENTRY. i.e. the protected floor is:

        trigger_price = entry_per_share + (peak_since_arm - entry_per_share) * (1 - giveback)

    Basing the band on (peak - entry) makes the trail a true percentage-of-realized-gain trail:
    giveback_fraction=0.4 always keeps at least 60% of the peak gain, whether the peak was +25%
    or +150%.
    """
    # ARMED GATE -- first and unconditional.  Unarmed means NO trailing exit exists, whatever the
    # price has done.  Fail-closed: the -30% protective stop is untouched and still governs.
    if not armed:
        return None
    if peak_since_arm is None:
        return None
    if quantity <= 0 or entry_debit <= 0:
        return None

    entry_per_share = entry_debit / (100.0 * quantity)
    if entry_per_share <= 0:
        return None

    # Secondary price guard.  peak_since_arm is seeded from a CLOSE that already cleared
    # activation, so this only bites if the activation was raised after arming.
    activation_price = entry_per_share * (1 + activation_gain_pct / 100.0)
    if peak_since_arm < activation_price:
        return None

    # Protected floor: keep (1 - giveback) of the post-arm peak gain measured from ENTRY.
    peak_gain = peak_since_arm - entry_per_share
    if peak_gain <= 0:
        return None
    max_retracement = peak_gain * giveback_fraction
    trigger_price = peak_since_arm - max_retracement

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
                     f"(peak_since_arm={peak_since_arm:.4f}, entry={entry_per_share:.4f}, "
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
    trail_armed: bool = False,
    peak_since_arm: Optional[float] = None,
) -> Optional[ExitTrigger]:
    """
    Evaluate all active exit rules for a position.
    Returns the highest-priority triggered exit, or None if no rule is triggered.

    `already_trimmed` should be supplied by the caller from persisted state (has this
    position already had a scale-out trim?). It defaults False for backward compatibility;
    the manager MUST pass the real flag once the scale-out hook is wired, otherwise a
    scale-out would re-fire every cycle. See the module docstring / handoff notes.

    `trail_armed` / `peak_since_arm` (2026-07-26, Sol audit R5 R1) come from persisted state --
    ``State.is_trail_armed(con_id)`` and ``State.trail_peak_since_arm(con_id)``.  They default
    to unarmed/None so a caller that has not been wired up can never accidentally fire a trail.
    `peak_price` is the monotonic LIFETIME peak: it is kept for audit/MFE reporting and is
    deliberately NOT used to arm the trail or to set its floor.
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

    # Trailing stop.  THREE separate conditions, deliberately not collapsed (Sol R5 R1):
    #   rules.trailing.enabled -- the FEATURE may be used (config/model supplied the knobs);
    #   trail_armed            -- price AND multi-session confirmation actually passed;
    #   peak_since_arm         -- the post-arm ratchet the floor is measured from.
    # The lifetime `peak_price` is NOT part of this decision any more.
    if rules.trailing.enabled and trail_armed and peak_since_arm is not None:
        trigger = evaluate_trailing_stop(
            current_price, entry_debit, quantity, peak_since_arm,
            rules.trailing.activation_gain_pct,
            rules.trailing.giveback_fraction,
            armed=True,
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
