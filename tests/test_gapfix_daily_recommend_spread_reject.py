"""Gap-fix (2026-07-03): make daily_recommend._resolve's SPREAD sizing SYMMETRIC with the trader
loop. The trader loop uses size_within_cap, which HARD-REJECTS (returns None) when even one contract
exceeds the budget; _resolve's spread path used to `max(1, ...)` and ship qty=1 OVER budget. It now
reuses size_within_cap so an unaffordable spread is rejected (the idea just isn't offered)."""
import inspect

import daily_recommend
from exitmgr.trader import size_within_cap


def test_size_within_cap_rejects_when_one_contract_exceeds_budget():
    # One spread contract costs net*100 = $1200 but only $1000 available-after-reserve -> reject.
    assert size_within_cap(1200.0, 1000.0, 1000.0) is None
    # Exactly one fits -> qty 1 (not rejected).
    assert size_within_cap(900.0, 1000.0, 1000.0) == 1
    # Two fit within budget.
    assert size_within_cap(400.0, 1000.0, 1000.0) == 2


def test_resolve_spread_path_uses_size_within_cap_not_force_clamp():
    src = inspect.getsource(daily_recommend._resolve)
    # The symmetric hard-reject wiring is present...
    assert "size_within_cap(net * 100, available, available)" in src
    assert "if qty is None:" in src
    assert "return None" in src
    # ...and the old force-to-one clamp is gone from the spread path.
    assert "qty = max(1, int(available // (net * 100)))" not in src


def test_daily_does_not_reject_defined_risk_trade_on_underlying_notional():
    src = inspect.getsource(daily_recommend._resolve)
    assert "100 * spot >" not in src
    assert "Underlying share notional is not capital at risk" in src
