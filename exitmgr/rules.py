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
    # SHORT (CREDIT) POSITIONS (2026-07-26, ADDITIVE -- both default to the LONG reading, so every
    # existing trigger construction and every existing consumer is byte-for-byte unchanged).
    #   is_short     -- True IFF this trigger came from the evaluate_short_* family. A consumer
    #                   that signs P&L, sizes an order, or picks a BUY/SELL action MUST branch on
    #                   this; `pnl_pct` below is already short-signed, and closing this position
    #                   is a BUY, not a SELL.
    #   entry_credit -- the CREDIT RECEIVED at entry, in dollars, always positive. It is the
    #                   short's basis. `entry_debit` above carries the SAME number for a short
    #                   (it is the dataclass's generic "basis dollars" field) -- but a reader must
    #                   not infer "we paid this" from it. When is_short is True, cash moved IN.
    is_short: bool = False
    entry_credit: Optional[float] = None


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


# =========================================================== SHORT (CREDIT) POSITIONS
# (2026-07-26.)  A SOLD option is the P&L MIRROR of a bought one, and every bug this section
# exists to prevent descends from one fact:
#
#       A SHORT POSITION GAINS WHEN THE OPTION PRICE **FALLS**.
#
# Every rule above reads "price up = good".  Handing a short to them does not merely fail, it
# INVERTS.  A cash-secured put that has decayed $4.00 -> $0.40 is a +90% WIN (the textbook
# "buy it back at 10% of credit" close); evaluate_stop() reads that same tape as a -90% LOSS
# and fires the protective stop -- buying a winner back at the worst moment and paying the
# spread to do it.  That inversion is the single highest-risk detail in the short path.
#
# These are therefore SEPARATE, EXPLICITLY-NAMED functions rather than a `short=True` flag
# threaded through the long ones.  A caller cannot reach them by accident, a reviewer cannot
# mistake which direction a given function reasons in, and the long rules stay byte-identical.
#
# BASIS.  A long's basis is the DEBIT PAID (cash out).  A short's basis is the CREDIT RECEIVED
# (cash in): `entry_credit`, ALWAYS A POSITIVE DOLLAR AMOUNT.  Per share it is
# entry_credit / (100 * contracts).  It is NEVER the journal's `debit` field -- reading a debit
# off a credit row is exactly the mistake that made construction.open_book_items() add a CSP's
# max loss to the long-premium book.  A missing/non-positive credit yields None (no trigger),
# never a fabricated basis.
#
# P&L.   gain_per_share = credit_per_share - current_price          (price falls -> gain)
#        cost_to_close  = current_price * 100 * contracts           (cash OUT to buy it back)
#        pnl_pct        = (entry_credit - cost_to_close) / entry_credit * 100
#
# QUANTITY.  Every function here accepts the position quantity in EITHER sign and uses
# abs(quantity) as the contract count.  Direction is carried by the FUNCTION IDENTITY, never by
# an argument's sign, so a caller passing the broker's -1 and a caller passing 1 get the same
# correct answer.  Zero is still "no position" -> None.
#
# NOT IMPLEMENTED HERE, deliberately: scale-out.  A partial buy-to-close is mechanically valid
# but the manager's trim bookkeeping (state.scaled_out, runner sizing) is long-shaped, and an
# untested partial path on real money is worse than no partial path.  A short takes full-exit
# rules only.


def is_short_quantity(quantity) -> bool:
    """True when `quantity` denotes a SOLD position.  The one predicate every caller that might
    be handed either book should branch on before choosing a rule family or an order action."""
    try:
        return int(quantity) < 0
    except (TypeError, ValueError):
        return False


def _short_credit_per_share(entry_credit, quantity) -> Optional[float]:
    """Positive per-share credit received, or None when the basis is unusable.

    None is a REFUSAL, not a zero: with no honest basis there is no honest stop or target, and
    fabricating one would arm a trigger against a number nobody received."""
    try:
        contracts = abs(int(quantity))
    except (TypeError, ValueError):
        return None
    if contracts <= 0:
        return None
    try:
        credit = float(entry_credit)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(credit) or credit <= 0:
        return None
    cps = credit / (100.0 * contracts)
    return cps if cps > 0 and math.isfinite(cps) else None


def short_pnl_pct(current_price: float, entry_credit: float, quantity: int) -> float:
    """Short P&L as a percentage of the credit received.

    POSITIVE when the option price has FALLEN (we can buy it back for less than we sold it).
    This is the exact sign inversion of calculate_pnl_pct(); the two must never be swapped.
    0.0 when the basis or price is unusable (mirrors calculate_pnl_pct's unusable-input return)."""
    cps = _short_credit_per_share(entry_credit, quantity)
    if cps is None:
        return 0.0
    try:
        px = float(current_price)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(px):
        return 0.0
    contracts = abs(int(quantity))
    credit = float(entry_credit)
    cost_to_close = px * 100 * contracts
    return (credit - cost_to_close) / credit * 100.0


def _short_trigger(trigger_type: str, current_price: float, entry_credit: float,
                   quantity: int, message: str) -> ExitTrigger:
    """Build a short ExitTrigger with the basis/value/P&L fields filled in the SHORT sense.

    `current_value` is the COST TO CLOSE (cash out to buy it back), not a mark-to-market asset
    value -- for a short there is no asset, there is an obligation.  `entry_debit` carries the
    credit because it is the dataclass's generic basis field; `is_short`/`entry_credit` are what
    tell a consumer which way round it is."""
    contracts = abs(int(quantity))
    px = float(current_price)
    credit = float(entry_credit)
    return ExitTrigger(
        con_id=0,  # set by the caller, exactly like the long family
        trigger_type=trigger_type,
        current_price=px,
        entry_debit=credit,
        current_value=px * 100 * contracts,
        pnl_pct=short_pnl_pct(px, credit, contracts),
        message=message,
        is_short=True,
        entry_credit=credit,
    )


def evaluate_short_profit_target(
    current_price: float,
    entry_credit: float,
    quantity: int,
    profit_target_pct: float,
) -> Optional[ExitTrigger]:
    """Buy-to-close at a profit: fires once enough of the credit has DECAYED AWAY.

    target_price = credit_per_share * (1 - profit_target_pct/100); fires when the price has
    fallen TO OR BELOW it.  profit_target_pct=80 on a $4.00 credit targets a $0.80 buyback.
    Note the direction: `<=`, the mirror of the long target's `>=`."""
    cps = _short_credit_per_share(entry_credit, quantity)
    if cps is None:
        return None
    try:
        px = float(current_price)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(px) or px < 0:
        return None
    target_price = max(0.0, cps * (1 - float(profit_target_pct) / 100.0))
    if px <= target_price:
        return _short_trigger(
            "profit_target", px, entry_credit, quantity,
            f"Short profit target hit: price={px:.4f} <= target={target_price:.4f} "
            f"(credit={cps:.4f}/sh; the credit has decayed {float(profit_target_pct):.0f}%)",
        )
    return None


def evaluate_short_stop(
    current_price: float,
    entry_credit: float,
    quantity: int,
    stop_pct: float,
) -> Optional[ExitTrigger]:
    """Protective stop for a short: fires when buying it back has grown EXPENSIVE.

    stop_price = credit_per_share * (1 + stop_pct/100); fires when the price has RISEN to or
    above it.  stop_pct=100 on a $4.00 credit stops out at an $8.00 buyback (the classic
    "close at 2x credit" CSP stop).  Direction is `>=`, the mirror of the long stop's `<=` --
    and a profitable short (price BELOW the credit) can never satisfy it."""
    cps = _short_credit_per_share(entry_credit, quantity)
    if cps is None:
        return None
    try:
        px = float(current_price)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(px) or px < 0:
        return None
    stop_price = cps * (1 + float(stop_pct) / 100.0)
    if px >= stop_price:
        return _short_trigger(
            "stop", px, entry_credit, quantity,
            f"Short stop hit: price={px:.4f} >= stop={stop_price:.4f} "
            f"(credit={cps:.4f}/sh; buyback costs {float(stop_pct):.0f}% more than collected)",
        )
    return None


def evaluate_short_time_stop(
    current_price: float,
    entry_credit: float,
    quantity: int,
    days_to_expiry: Optional[int],
    time_stop_days: int,
) -> Optional[ExitTrigger]:
    """DTE-based close for a short.  The DTE test itself is direction-free; only the reported
    P&L had to be inverted (which is why this cannot just reuse evaluate_time_stop)."""
    if days_to_expiry is None:
        return None
    if _short_credit_per_share(entry_credit, quantity) is None:
        return None
    if days_to_expiry <= time_stop_days:
        return _short_trigger(
            "time_stop", current_price, entry_credit, quantity,
            f"Short time stop hit: DTE={days_to_expiry} <= {time_stop_days}",
        )
    return None


def evaluate_short_trailing_stop(
    current_price: float,
    entry_credit: float,
    quantity: int,
    trough_since_arm: Optional[float] = None,
    activation_gain_pct: float = 0.0,
    giveback_fraction: float = 0.0,
    *,
    armed: bool = False,
) -> Optional[ExitTrigger]:
    """MIRROR of evaluate_trailing_stop for a short -- protects DECAY already banked.

    A long's favourable extreme is a price HIGH, so its ratchet is a PEAK that only moves UP and
    its protected level is a FLOOR the price falls INTO.  A short's favourable extreme is a price
    LOW, so the ratchet is a TROUGH that only moves DOWN and the protected level is a CEILING the
    price rises INTO.  Every comparison is flipped:

        best_gain     = credit_per_share - trough_since_arm         (long: peak - entry)
        trigger_price = trough_since_arm + best_gain * giveback     (long: peak - gain*giveback)
        fires when      current_price >= trigger_price              (long: <=)

    giveback_fraction=0.4 keeps at least 60% of the banked decay either way.

    ARMING IS NOT DECIDED HERE, exactly as on the long side (Sol audit R5 R1): the caller passes
    the persisted `armed` flag and the post-arm ratchet.  Unarmed => no trailing exit exists and
    the protective stop alone governs.  `trough_since_arm` is the LOWEST price seen since arming;
    a caller that passes a lifetime peak, or a long's peak_since_arm, will get nonsense -- the
    parameter is named `trough` so that mistake is visible at the call site."""
    if not armed:
        return None
    if trough_since_arm is None:
        return None
    cps = _short_credit_per_share(entry_credit, quantity)
    if cps is None:
        return None
    try:
        px = float(current_price)
        trough = float(trough_since_arm)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(px) and math.isfinite(trough)) or px < 0 or trough < 0:
        return None

    # Secondary price guard, mirrored: the trough must be at or BELOW the activation price
    # (i.e. enough of the credit had decayed), where the long requires peak >= activation.
    activation_price = cps * (1 - float(activation_gain_pct) / 100.0)
    if trough > activation_price:
        return None

    best_gain = cps - trough
    if best_gain <= 0:
        return None
    max_giveback = best_gain * float(giveback_fraction)
    trigger_price = trough + max_giveback

    if px >= trigger_price:
        return _short_trigger(
            "trailing_stop", px, entry_credit, quantity,
            f"Short trailing stop hit: price={px:.4f} >= trigger={trigger_price:.4f} "
            f"(trough_since_arm={trough:.4f}, credit={cps:.4f}/sh, "
            f"keep {(1 - float(giveback_fraction)):.0%} of banked decay)",
        )
    return None


def evaluate_short_position(
    con_id: int,
    symbol: str,
    quantity: int,
    entry_credit: float,
    current_price: float,
    days_to_expiry: Optional[int],
    rules: RulesConfig,
    trail_armed: bool = False,
    trough_since_arm: Optional[float] = None,
) -> Optional[ExitTrigger]:
    """Evaluate every active exit rule for a SHORT position; highest-priority trigger or None.

    Structurally identical to evaluate_position (same rule gates, same priority map) so the two
    stay reviewable side by side -- but every rule it calls is from the evaluate_short_* family,
    and the basis is `entry_credit`, not a debit.  Scale-out is deliberately absent (see the
    section header).  Returns None when the credit basis is unusable: no honest basis, no
    trigger, and the caller is expected to surface that as UNMANAGED rather than as protected."""
    triggers: List[ExitTrigger] = []

    if rules.profit_target_pct is not None and rules.profit_target_pct > 0:
        trigger = evaluate_short_profit_target(
            current_price, entry_credit, quantity, rules.profit_target_pct
        )
        if trigger:
            trigger.con_id = con_id
            triggers.append(trigger)

    if rules.stop_pct is not None and rules.stop_pct > 0:
        trigger = evaluate_short_stop(
            current_price, entry_credit, quantity, rules.stop_pct
        )
        if trigger:
            trigger.con_id = con_id
            triggers.append(trigger)

    if rules.time_stop_days is not None and rules.time_stop_days > 0:
        trigger = evaluate_short_time_stop(
            current_price, entry_credit, quantity, days_to_expiry, rules.time_stop_days
        )
        if trigger:
            trigger.con_id = con_id
            triggers.append(trigger)

    if rules.trailing.enabled and trail_armed and trough_since_arm is not None:
        trigger = evaluate_short_trailing_stop(
            current_price, entry_credit, quantity, trough_since_arm,
            rules.trailing.activation_gain_pct,
            rules.trailing.giveback_fraction,
            armed=True,
        )
        if trigger:
            trigger.con_id = con_id
            triggers.append(trigger)

    if triggers:
        # Same priority map as the long side; scale_out simply never appears.
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
    entry_credit: Optional[float] = None,
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

    SHORT ROUTING (2026-07-26).  A NEGATIVE `quantity` is a SOLD position and is routed to
    evaluate_short_position, whose rules are P&L-inverted (a short gains when the price FALLS).
    Before this, a short fell through every `quantity <= 0` guard below and this function
    returned None -- which the manager's _enforce_airtight_stop then reported as "protected".
    That was a FALSE SAFETY SIGNAL: nothing was armed at all.  `quantity > 0` reaches the
    original long body below completely unchanged, and `quantity == 0` still returns None.
    A short REQUIRES `entry_credit` (dollars collected, positive); without it there is no honest
    basis, so this refuses LOUDLY and returns None rather than silently pretending to protect.
    `peak_since_arm` is reinterpreted as the post-arm TROUGH for a short -- the favourable
    extreme of a short is a price LOW.
    """
    if is_short_quantity(quantity):
        if _short_credit_per_share(entry_credit, quantity) is None:
            print(f"[WARN] con_id={con_id} ({symbol}): SHORT position qty={quantity} has no usable "
                  f"entry_credit basis (got {entry_credit!r}); NO exit rule can be evaluated. "
                  f"This position is UNMANAGED -- it is NOT protected by a stop.")
            return None
        return evaluate_short_position(
            con_id=con_id,
            symbol=symbol,
            quantity=quantity,
            entry_credit=entry_credit,
            current_price=current_price,
            days_to_expiry=days_to_expiry,
            rules=rules,
            trail_armed=trail_armed,
            trough_since_arm=peak_since_arm,
        )

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
