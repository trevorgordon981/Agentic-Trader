"""Public API contract; production-derived narrative omitted."""

import math
from dataclasses import dataclass
from typing import Optional, Dict, List, Union
from datetime import datetime, date, timedelta

from exitmgr.config import RulesConfig
















EXTRA_MARKET_CLOSURES: frozenset = frozenset()




















EXIT_TRIGGER_PRIORITY: Dict[str, int] = {
    "profit_target": 1,
    "stop": 2,
    "trailing_stop": 3,
    "scale_out": 4,
    "time_stop": 5,
}


def _easter_sunday(year: int) -> date:
    """Public API contract; production-derived narrative omitted."""
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
    """Public API contract; production-derived narrative omitted."""
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
    """Public API contract; production-derived narrative omitted."""
    if d.weekday() == 5:
        return d - timedelta(days=1) if roll_saturday_back else None
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def nyse_holidays(year: int) -> frozenset:
    """Public API contract; production-derived narrative omitted."""
    out = set()

    def _add(d, **kw):
        o = _observed(d, **kw) if d is not None else None
        if o is not None and o.year == year:
            out.add(o)

    _add(date(year, 1, 1), roll_saturday_back=False)
    out.add(_nth_weekday(year, 1, 0, 3))
    out.add(_nth_weekday(year, 2, 0, 3))
    out.add(_easter_sunday(year) - timedelta(days=2))
    out.add(_nth_weekday(year, 5, 0, -1))
    _add(date(year, 6, 19))
    _add(date(year, 7, 4))
    out.add(_nth_weekday(year, 9, 0, 1))
    out.add(_nth_weekday(year, 11, 3, 4))
    _add(date(year, 12, 25))


    return frozenset(out)


def _as_date(value: Union[str, date, datetime, None]) -> Optional[date]:
    """Public API contract; production-derived narrative omitted."""
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
    """Public API contract; production-derived narrative omitted."""
    d = _as_date(value)
    if d is None:
        return False
    if d.weekday() >= 5:
        return False
    if d in EXTRA_MARKET_CLOSURES:
        return False
    return d not in nyse_holidays(d.year)


def next_trading_session(value) -> Optional[date]:
    """Public API contract; production-derived narrative omitted."""
    d = _as_date(value)
    if d is None:
        return None
    for _ in range(1, 15):
        d = d + timedelta(days=1)
        if is_trading_session(d):
            return d
    return None


def is_next_trading_session(prev, cur) -> bool:
    """Public API contract; production-derived narrative omitted."""
    p, c = _as_date(prev), _as_date(cur)
    if p is None or c is None:
        return False
    return next_trading_session(p) == c


@dataclass
class ExitTrigger:
    """Public API contract; production-derived narrative omitted."""
    con_id: int
    trigger_type: str
    current_price: float
    entry_debit: float
    current_value: float
    pnl_pct: float
    message: str




    quantity_fraction: float = 1.0





    reload: bool = False
    reload_conviction: Optional[float] = None










    is_short: bool = False
    entry_credit: Optional[float] = None


def evaluate_profit_target(
    current_price: float,
    entry_debit: float,
    quantity: int,
    profit_target_pct: float,
) -> Optional[ExitTrigger]:
    """Public API contract; production-derived narrative omitted."""

    if quantity <= 0:
        return None

    entry_per_share = entry_debit / (100.0 * quantity)
    if entry_per_share <= 0:
        return None


    target_price = entry_per_share * (1 + profit_target_pct / 100.0)

    if current_price >= target_price:
        current_value = current_price * 100 * quantity
        pnl_pct = (current_value - entry_debit) / entry_debit * 100.0
        return ExitTrigger(
            con_id=0,
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
    """Public API contract; production-derived narrative omitted."""
    if quantity < 2:

        return None
    if already_trimmed:
        return None
















    try:
        frac = float(trim_fraction)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(frac) or not (0.0 < frac < 1.0):
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
                 f"trim {frac:.0%} of {quantity}, let runner ride"),
        quantity_fraction=frac,
    )


def evaluate_stop(
    current_price: float,
    entry_debit: float,
    quantity: int,
    stop_pct: float,
) -> Optional[ExitTrigger]:
    """Public API contract; production-derived narrative omitted."""
    if quantity <= 0:
        return None

    entry_per_share = entry_debit / (100.0 * quantity)
    if entry_per_share <= 0:
        return None


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
    """Public API contract; production-derived narrative omitted."""
    if days_to_expiry is None:

        return None
































    try:
        contracts = int(quantity)
    except (TypeError, ValueError):
        return None
    if contracts <= 0:
        return None
    try:
        basis = float(entry_debit)
        px = float(current_price)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(basis) or basis <= 0:
        return None
    if not math.isfinite(px):
        return None

    if days_to_expiry <= time_stop_days:
        current_value = px * 100 * contracts
        pnl_pct = (current_value - basis) / basis * 100.0
        return ExitTrigger(
            con_id=0,
            trigger_type="time_stop",
            current_price=px,
            entry_debit=basis,
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
    protected_floor_price: Optional[float] = None,
) -> Optional[ExitTrigger]:
    """Public API contract; production-derived narrative omitted."""


    if not armed:
        return None


    try:
        floor = float(protected_floor_price)
        if not math.isfinite(floor) or floor <= 0:
            floor = None
    except (TypeError, ValueError, OverflowError):
        floor = None
    if floor is not None and quantity > 0 and entry_debit > 0:
        if current_price <= floor:
            value = current_price * 100 * quantity
            return ExitTrigger(
                con_id=0, trigger_type="trailing_stop", current_price=current_price,
                entry_debit=entry_debit, current_value=value,
                pnl_pct=(value-entry_debit)/entry_debit*100,
                message=f"Trailing stop hit: price={current_price:.4f} <= earned floor={floor:.4f}")
        return None
    if peak_since_arm is None:
        return None
    if quantity <= 0 or entry_debit <= 0:
        return None

    entry_per_share = entry_debit / (100.0 * quantity)
    if entry_per_share <= 0:
        return None



    activation_price = entry_per_share * (1 + activation_gain_pct / 100.0)
    if peak_since_arm < activation_price:
        return None


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








































def is_short_quantity(quantity) -> bool:
    """Public API contract; production-derived narrative omitted."""
    try:
        return int(quantity) < 0
    except (TypeError, ValueError):
        return False


def _short_credit_per_share(entry_credit, quantity) -> Optional[float]:
    """Public API contract; production-derived narrative omitted."""
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
    """Public API contract; production-derived narrative omitted."""
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
    """Public API contract; production-derived narrative omitted."""
    contracts = abs(int(quantity))
    px = float(current_price)
    credit = float(entry_credit)
    return ExitTrigger(
        con_id=0,
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
    """Public API contract; production-derived narrative omitted."""
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
    """Public API contract; production-derived narrative omitted."""
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
    """Public API contract; production-derived narrative omitted."""
    if days_to_expiry is None:
        return None
    if _short_credit_per_share(entry_credit, quantity) is None:
        return None












    try:
        px = float(current_price)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(px) or px < 0:
        return None
    if days_to_expiry <= time_stop_days:
        return _short_trigger(
            "time_stop", px, entry_credit, quantity,
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
    """Public API contract; production-derived narrative omitted."""
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
    """Public API contract; production-derived narrative omitted."""
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




        triggers.sort(key=lambda t: EXIT_TRIGGER_PRIORITY.get(t.trigger_type, 99))
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
    protected_floor_price: Optional[float] = None,
) -> Optional[ExitTrigger]:
    """Public API contract; production-derived narrative omitted."""
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












    _q_ok = True
    try:
        _q_ok = int(quantity) > 0
    except (TypeError, ValueError):
        _q_ok = False
    _d_ok = True
    try:
        _d = float(entry_debit)
        _d_ok = math.isfinite(_d) and _d > 0
    except (TypeError, ValueError):
        _d_ok = False
    if not (_q_ok and _d_ok):
        print(f"[WARN] con_id={con_id} ({symbol}): LONG position qty={quantity!r} has no usable "
              f"entry_debit basis (got {entry_debit!r}); NO exit rule can be evaluated. "
              f"This position is UNMANAGED -- it is NOT protected by a stop.")
        return None

    triggers: List[ExitTrigger] = []


    if rules.profit_target_pct is not None and rules.profit_target_pct > 0:
        trigger = evaluate_profit_target(
            current_price, entry_debit, quantity, rules.profit_target_pct
        )
        if trigger:
            trigger.con_id = con_id
            triggers.append(trigger)


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


    if rules.stop_pct is not None and rules.stop_pct > 0:
        trigger = evaluate_stop(
            current_price, entry_debit, quantity, rules.stop_pct
        )
        if trigger:
            trigger.con_id = con_id
            triggers.append(trigger)


    if rules.time_stop_days is not None and rules.time_stop_days > 0:
        trigger = evaluate_time_stop(
            current_price, entry_debit, quantity, days_to_expiry, rules.time_stop_days
        )
        if trigger:
            trigger.con_id = con_id
            triggers.append(trigger)






    if (rules.trailing.enabled and trail_armed
            and (peak_since_arm is not None or protected_floor_price is not None)):
        trigger = evaluate_trailing_stop(
            current_price, entry_debit, quantity, peak_since_arm,
            rules.trailing.activation_gain_pct,
            rules.trailing.giveback_fraction,
            armed=True, protected_floor_price=protected_floor_price,
        )
        if trigger:
            trigger.con_id = con_id
            triggers.append(trigger)


    if triggers:








        triggers.sort(key=lambda t: EXIT_TRIGGER_PRIORITY.get(t.trigger_type, 99))
        return triggers[0]

    return None


def calculate_pnl_pct(current_price: float, entry_debit: float, quantity: int) -> float:
    """Public API contract; production-derived narrative omitted."""
    if entry_debit <= 0 or quantity <= 0:
        return 0.0
    current_value = current_price * 100 * quantity
    return (current_value - entry_debit) / entry_debit * 100.0


def days_to_expiry(expiry, today=None):
    """Public API contract; production-derived narrative omitted."""
    if not expiry:
        return None
    from datetime import datetime, timezone
    try:
        d = datetime.strptime(str(expiry)[:8], "%Y%m%d").date()
    except (ValueError, TypeError):
        return None
    t0 = today or datetime.now(timezone.utc).date()
    return (d - t0).days
