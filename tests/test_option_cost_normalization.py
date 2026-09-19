"""Public API contract; production-derived narrative omitted."""
import asyncio
from types import SimpleNamespace

import pytest

from exitmgr.connection import _option_cost_per_share
from exitmgr.construction import open_book_items
from exitmgr.entry_reflection import build_debit_risk_book
from exitmgr.manager import _resolve_entry_basis
from exitmgr.rules import evaluate_stop
from tests.test_credit_wiring import _raw, _conn, _mgr, _capture_closes, LONG_JOURNAL


def test_real_SYMD_contract_cost_does_not_trigger_phantom_stop():
    raw = _raw(5001001, "C", 1, symbol="SYMD")
    raw.avgCost = 1590.04028
    pos = asyncio.run(_conn([raw]).get_positions())[5001001]
    assert pos.avg_cost == pytest.approx(15.9004028)
    basis, qty, source = _resolve_entry_basis({}, pos)
    assert basis == pytest.approx(1590.04028)
    assert source == "avg_cost" and qty == 1
    assert evaluate_stop(15.86015605, basis, qty, 30) is None
    assert evaluate_stop(10, basis, qty, 30).trigger_type == "stop"


def test_short_cost_normalizes_without_changing_quantity_or_stock():
    rows = [_raw(1, "P", -3, avg_cost=1.75),
            _raw(2, "", 10, avg_cost=498.25, sec_type="STK")]
    out = asyncio.run(_conn(rows).get_positions(include_short=True, include_stock=True))
    assert out[1].quantity == -3 and out[1].avg_cost == 1.75
    assert out[2].quantity == 10 and out[2].avg_cost == 498.25


@pytest.mark.parametrize("multiplier", [None, "", "bad", "50", "10", 0, -1,
                                         True, float("nan"), float("inf")])
def test_unsupported_units_leave_visible_position_and_unknown_basis(multiplier, tmp_path):
    raw = _raw(1, "C", 1)
    raw.contract.multiplier = multiplier
    out = asyncio.run(_conn([raw]).get_positions())
    assert out[1].quantity == 1 and out[1].avg_cost is None
    with pytest.raises((TypeError, ValueError)):
        _resolve_entry_basis({}, out[1])
    with pytest.raises((TypeError, ValueError)):
        open_book_items(out, str(tmp_path / "absent-journal"))
    with pytest.raises((TypeError, ValueError)):
        build_debit_risk_book(out, SimpleNamespace(campaign_entries={}, debits={}))


@pytest.mark.parametrize("cost", [None, "bad", True, 0, float("nan"), float("inf")])
def test_invalid_cost_is_unknown_not_invented_zero(cost):
    row = _raw(1, "C", 1)
    row.avgCost = cost
    assert _option_cost_per_share(row) is None


def test_fop_is_not_mislabeled_as_standard_option_cost():
    row = _raw(1, "C", 1, sec_type="FOP")
    row.contract.multiplier = "100"
    assert _option_cost_per_share(row) is None


def test_unknown_basis_does_not_abort_sibling_protection(tmp_path, capsys):
    unknown = _raw(999, "C", 1, symbol="UNKNOWN")
    unknown.contract.multiplier = ""
    healthy = _raw(101, "C", 2, symbol="RKLB", avg_cost=4.10)
    mgr, cfg = _mgr(tmp_path, [LONG_JOURNAL], rows=[unknown, healthy],
                    quotes={999: {"price": 1.0}, 101: {"price": 1.0}})
    cfg.scope.mode = "all_longs"
    calls = _capture_closes(mgr)
    asyncio.run(mgr.run_cycle(dry_run=False))
    assert "entry basis UNKNOWN" in capsys.readouterr().out
    assert [c["con_id"] for c in calls] == [101]


def test_known_journal_basis_remains_available_when_wire_cost_unknown():
    row = _raw(1, "C", 1)
    row.contract.multiplier = ""
    pos = asyncio.run(_conn([row]).get_positions())[1]
    assert _resolve_entry_basis({"entry_fill_debit": 175, "quantity": 1}, pos) == (
        175, 1, "entry_fill_debit")
