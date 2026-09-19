"""Public API contract; production-derived narrative omitted."""
import inspect

import daily_recommend
from exitmgr.trader import size_within_cap


def test_size_within_cap_rejects_when_one_contract_exceeds_budget():

    assert size_within_cap(1200.0, 1000.0, 1000.0) is None

    assert size_within_cap(900.0, 1000.0, 1000.0) == 1

    assert size_within_cap(400.0, 1000.0, 1000.0) == 2


def test_resolve_spread_path_uses_size_within_cap_not_force_clamp():
    src = inspect.getsource(daily_recommend._resolve)

    assert "size_within_cap(net * 100, available, available)" in src
    assert "if qty is None:" in src
    assert "return None" in src

    assert "qty = max(1, int(available // (net * 100)))" not in src


def test_daily_does_not_reject_defined_risk_trade_on_underlying_notional():
    src = inspect.getsource(daily_recommend._resolve)
    assert "100 * spot >" not in src
    assert "Underlying share notional is not capital at risk" in src
