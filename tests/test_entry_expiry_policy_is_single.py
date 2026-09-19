"""Public API contract; production-derived narrative omitted."""
import ast
import inspect

import pytest

from exitmgr import entry_contract


def test_the_debit_hold_floor_is_flat_across_every_conviction():
    from exitmgr.entry_contract import StageAIntent, hold_dte_floor
    floors = set()
    for conviction in range(1, 11):
        intent = StageAIntent(
            underlying="MUTX", side="debit", direction="bullish", structure="vertical",
            target_dte=180, intended_hold_days=21, target_delta=0.45,
            conviction=conviction, allocation_pct_net_liq=10.0,
            alpha="momentum", thesis="expiry-policy test intent",
        )
        floors.add(hold_dte_floor(intent))
    assert len(floors) == 1, "conviction still moves the expiry floor: %s" % sorted(floors)
    assert floors == {21 * 8}


def test_hold_dte_floor_reads_no_conviction_at_all():
    """Public API contract; production-derived narrative omitted."""
    src = inspect.getsource(entry_contract.hold_dte_floor)
    tree = ast.parse(src.lstrip())
    reads = [n for n in ast.walk(tree)
             if isinstance(n, ast.Attribute) and n.attr == "conviction"]
    assert not reads, "hold_dte_floor still reads intent.conviction"


def test_the_contract_multiple_matches_the_builder():
    """Public API contract; production-derived narrative omitted."""
    from exitmgr.entry_contract import DEBIT_HOLD_FLOOR_MULTIPLE
    from exitmgr import entry_builder
    src = inspect.getsource(entry_builder)
    assert "DEBIT_HOLD_FLOOR" in src.upper() or "* 8" in src or "8 *" in src, \
        "could not locate the builder's hold multiple to compare against"
    assert DEBIT_HOLD_FLOOR_MULTIPLE == 8


def test_the_credit_floor_is_untouched():
    from exitmgr.entry_contract import StageAIntent, hold_dte_floor
    intent = StageAIntent(
        underlying="MUTX", side="credit", direction="bullish", structure="vertical",
        target_dte=180, intended_hold_days=21, target_delta=0.45,
        conviction=9, allocation_pct_net_liq=10.0,
        alpha="momentum", thesis="expiry-policy test intent",
    )
    assert hold_dte_floor(intent) == 21
