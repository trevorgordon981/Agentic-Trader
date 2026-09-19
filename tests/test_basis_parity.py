"""Public API contract; production-derived narrative omitted."""
import pytest

from exitmgr.manager import _resolve_entry_basis


class _Pos:
    def __init__(self, quantity=1, avg_cost=2.0):
        self.quantity = quantity
        self.avg_cost = avg_cost


def test_prefers_actual_fill_over_order_limit():
    """Public API contract; production-derived narrative omitted."""
    debit, qty, src = _resolve_entry_basis(
        {"debit": 300.0, "entry_fill_debit": 250.0, "basis_source": "fill", "quantity": 1},
        _Pos(quantity=1))
    assert debit == 250.0, "regressed to the order's limit debit -- this is the SYMM bug"
    assert qty == 1
    assert src == "entry_fill_debit"


def test_synthetic_basis_regression():
    """Public API contract; production-derived narrative omitted."""
    je = {"debit": 300.0, "entry_fill_debit": 250.0, "basis_source": "fill", "quantity": 1}
    basis, qty, _ = _resolve_entry_basis(je, _Pos(quantity=1))

    mark = 2.144
    pnl = (mark * 100 * qty - basis) / basis * 100
    assert pnl == pytest.approx(-14.24, abs=0.05)


    phantom = (mark * 100 * 1 - 300.0) / 300.0 * 100
    assert phantom < -27.0
    assert pnl > -30.0, "the position never breached the 30% stop the model cited"


def test_scale_out_prorates_the_basis():
    """Public API contract; production-derived narrative omitted."""
    debit, qty, src = _resolve_entry_basis(
        {"debit": 900.0, "entry_fill_debit": 800.0, "quantity": 4}, _Pos(quantity=1))
    assert qty == 1
    assert debit == 200.0, "full-size basis against a trimmed position skews every % threshold"
    assert "prorated" in src


def test_untrimmed_position_is_not_prorated():
    debit, qty, src = _resolve_entry_basis(
        {"debit": 900.0, "entry_fill_debit": 800.0, "quantity": 4}, _Pos(quantity=4))
    assert (debit, qty) == (800.0, 4)
    assert "prorated" not in src


@pytest.mark.parametrize("je", [
    {},
    None,
    {"debit": 0, "entry_fill_debit": None},
    {"debit": None, "entry_fill_debit": None},
])
def test_falls_back_to_avg_cost_when_no_usable_basis(je):
    """Public API contract; production-derived narrative omitted."""
    debit, qty, src = _resolve_entry_basis(je, _Pos(quantity=2, avg_cost=1.5))
    assert debit == 300.0
    assert qty == 2
    assert src == "avg_cost"


def test_garbage_fill_value_does_not_displace_the_debit():
    debit, _, src = _resolve_entry_basis(
        {"debit": 300.0, "entry_fill_debit": "n/a", "quantity": 1}, _Pos(quantity=1))
    assert debit == 300.0
    assert src == "debit"
