"""Public API contract; production-derived narrative omitted."""
from types import SimpleNamespace

import pytest

from exitmgr import entry_safety
from exitmgr.trader import ResolvedOrder


def _resolved(*, tp=0.0, sl=0.0, ask=1.10, bid=1.00, qty=1, con_id=11):
    return ResolvedOrder(
        "SPY", "C", "20270115", 600.0, qty, 1.05,
        SimpleNamespace(conId=con_id), entry_bid=bid, entry_ask=ask,
        quote_observed_at=100.0, decision_id="decision-" + "a" * 32,
        tp_pct=tp, sl_pct=sl)


def _changes(original, refreshed):
    return entry_safety.material_changes(original, refreshed)


def test_stop_tightened_from_30_to_15_forces_reapproval():
    changes = _changes(_resolved(tp=30.0, sl=30.0), _resolved(tp=30.0, sl=15.0))
    assert any("stop-loss changed 30.0% -> 15.0%" in c for c in changes)


def test_take_profit_change_forces_reapproval():
    changes = _changes(_resolved(tp=30.0, sl=30.0), _resolved(tp=35.0, sl=30.0))
    assert any("take-profit changed 30.0% -> 35.0%" in c for c in changes)


def test_unchanged_protective_terms_do_not_retap():
    assert _changes(_resolved(tp=30.0, sl=30.0), _resolved(tp=30.0, sl=30.0)) == ()


def test_orders_carrying_no_protective_terms_do_not_retap():
    """Public API contract; production-derived narrative omitted."""
    assert _changes(_resolved(), _resolved()) == ()


@pytest.mark.parametrize("delta, material", [
    (0.4, False),
    (0.5, False),
    (0.6, True),
    (-15.0, True),
])
def test_threshold_is_half_a_percentage_point(delta, material):
    assert entry_safety.DEFAULT_MATERIAL_PROTECTIVE_PTS == 0.5
    changes = _changes(_resolved(tp=30.0, sl=30.0), _resolved(tp=30.0, sl=30.0 + delta))
    assert bool(any("stop-loss" in c for c in changes)) is material


def test_threshold_is_absolute_points_not_relative_to_the_value():
    """Public API contract; production-derived narrative omitted."""
    small = _changes(_resolved(tp=30.0, sl=10.0), _resolved(tp=30.0, sl=10.6))
    large = _changes(_resolved(tp=30.0, sl=34.0), _resolved(tp=30.0, sl=34.6))
    assert any("stop-loss" in c for c in small)
    assert any("stop-loss" in c for c in large)


def test_band_is_overridable_and_actually_read():
    a, b = _resolved(tp=30.0, sl=30.0), _resolved(tp=30.0, sl=32.0)
    assert entry_safety.material_changes(a, b, max_protective_change_pts=5.0) == ()
    assert entry_safety.material_changes(a, b, max_protective_change_pts=0.0)


@pytest.mark.parametrize("original_sl, refreshed_sl, expect", [
    (30.0, 0.0, "stop-loss changed 30.0% -> default rule"),
    (0.0, 30.0, "stop-loss changed default rule -> 30.0%"),
])
def test_appearing_or_disappearing_protection_is_always_material(original_sl, refreshed_sl, expect):
    """Public API contract; production-derived narrative omitted."""
    changes = _changes(_resolved(tp=30.0, sl=original_sl), _resolved(tp=30.0, sl=refreshed_sl))
    assert any(expect in c for c in changes)


@pytest.mark.parametrize("bad", ["thirty", float("nan"), object()])
def test_uncomparable_protective_terms_fail_closed(bad):
    original = _resolved(tp=30.0, sl=30.0)
    refreshed = SimpleNamespace(**{**original.__dict__, "sl_pct": bad})
    changes = _changes(original, refreshed)
    assert changes, "an uncomparable stop must never read as 'unchanged'"


def test_protective_check_does_not_mask_the_original_three():
    original = _resolved(tp=30.0, sl=30.0)
    assert any("quantity changed" in c
               for c in _changes(original, _resolved(tp=30.0, sl=30.0, qty=2)))
    assert any("contract/structure" in c
               for c in _changes(original, _resolved(tp=30.0, sl=30.0, con_id=99)))
    assert any("price changed" in c
               for c in _changes(original, _resolved(tp=30.0, sl=30.0, ask=1.30)))
