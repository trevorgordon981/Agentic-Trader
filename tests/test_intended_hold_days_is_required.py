"""Public API contract; production-derived narrative omitted."""
import ast
import os
import sys
from argparse import Namespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import daily_recommend
from daily_recommend import (
    carry_intended_hold,
    doctrine_stamp,
    positive_hold_days,
    user_directed_idea,
)
from exitmgr.entry_builder import DEBIT_HOLD_FLOOR_MULTIPLE
from exitmgr.trader import ResolvedOrder


def _args(**kw):
    d = dict(ticker="SYMG", right="C", structure="call debit spread", dte=280, delta=0.45,
             conviction=7, thesis="User-directed proposal.", tp=None, stop=30.0, hold_days=0)
    d.update(kw)
    return Namespace(**d)


class _Order:
    """Public API contract; production-derived narrative omitted."""

    def __init__(self, hold=None):
        self.intended_hold_days = hold



@pytest.mark.parametrize("value", [None, 0, -1, "", "abc", True, False, 0.0])
def test_a_hold_that_is_not_a_positive_integer_is_no_hold_at_all(value):
    """Public API contract; production-derived narrative omitted."""
    assert positive_hold_days(value) is None


@pytest.mark.parametrize("value,expect", [(1, 1), (20, 20), (35, 35), ("21", 21), (21.0, 21)])
def test_a_positive_hold_is_taken_as_stated(value, expect):
    assert positive_hold_days(value) == expect



def test_an_explicit_hold_is_used_and_labelled_explicit():
    idea = user_directed_idea(_args(hold_days=25))
    assert idea.intended_hold_days == 25
    assert idea._intended_hold_days_source == "explicit"


def test_no_hold_and_no_configured_fallback_refuses():
    """Public API contract; production-derived narrative omitted."""
    with pytest.raises(ValueError) as exc:
        user_directed_idea(_args(), fallback_hold_days=None)
    assert "intended_hold_days is REQUIRED" in str(exc.value)
    assert "--hold-days" in str(exc.value)


def test_the_dte_is_never_used_to_manufacture_a_hold():
    """Public API contract; production-derived narrative omitted."""
    for dte in (80, 280, 720):
        with pytest.raises(ValueError):
            user_directed_idea(_args(dte=dte), fallback_hold_days=None)
        idea = user_directed_idea(_args(dte=dte), fallback_hold_days=20)
        assert idea.intended_hold_days == 20, "the fallback, not a function of --dte"
    assert -(-280 // 8) == 35


def test_the_configured_fallback_is_used_and_labelled_as_defaulted(capsys):
    idea = user_directed_idea(_args(), fallback_hold_days=20)
    assert idea.intended_hold_days == 20
    assert idea._intended_hold_days_source == "config_fallback"
    assert "intended_hold_days_fallback=20" in capsys.readouterr().out


def test_the_fallback_value_comes_from_config_not_from_code():
    """Public API contract; production-derived narrative omitted."""
    for configured in (5, 20, 40):
        assert user_directed_idea(_args(), fallback_hold_days=configured
                                  ).intended_hold_days == configured


def test_the_shipped_config_value_is_registered_and_readable():
    """Public API contract; production-derived narrative omitted."""
    from exitmgr.config import TRADING_DEFAULTS, Config
    declared = dict(TRADING_DEFAULTS)
    assert "intended_hold_days_fallback" in declared
    assert declared["intended_hold_days_fallback"] is None, "code default FAILS CLOSED"
    live = Config.from_yaml(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml"))
    assert live.intended_hold_days_fallback == 20


def test_the_shipped_fallback_implies_a_floor_the_current_book_clears():
    """Public API contract; production-derived narrative omitted."""
    assert 20 * DEBIT_HOLD_FLOOR_MULTIPLE == 160
    assert 40 * DEBIT_HOLD_FLOOR_MULTIPLE == 320



def test_a_re_resolved_order_starts_with_no_hold():
    """Public API contract; production-derived narrative omitted."""
    order = ResolvedOrder("SYMG", "C", "20270617", 44.0, 1, 4.30, object())
    assert getattr(order, "intended_hold_days", "missing") is None


def test_the_hold_is_carried_onto_a_re_resolved_order():
    """Public API contract; production-derived narrative omitted."""
    fresh = _Order()
    assert carry_intended_hold(fresh, _Order(None), _Order(38)) == 38
    assert fresh.intended_hold_days == 38


def test_the_carry_prefers_the_first_source_that_states_a_hold():
    fresh = _Order()
    carry_intended_hold(fresh, _Order(None), _Order(0), _Order(14), _Order(99))
    assert fresh.intended_hold_days == 14


def test_the_carry_reports_failure_rather_than_inventing_a_hold():
    fresh = _Order()
    assert carry_intended_hold(fresh, _Order(None), _Order(0), _Order(None)) is None
    assert fresh.intended_hold_days is None


def test_an_already_held_order_is_not_overwritten_by_a_later_source():
    fresh = _Order(30)
    assert carry_intended_hold(fresh, fresh, _Order(7)) == 30
    assert fresh.intended_hold_days == 30



def test_the_stamp_records_the_multiple_the_entry_path_enforced():
    stamp = doctrine_stamp(20, credit=False)
    assert stamp["doctrine_hold_multiple"] == DEBIT_HOLD_FLOOR_MULTIPLE
    assert stamp["doctrine_dte_floor"] == 20 * DEBIT_HOLD_FLOOR_MULTIPLE
    assert stamp["doctrine_side"] == "debit"


def test_the_stamp_is_not_a_second_copy_of_the_number(monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    monkeypatch.setattr(daily_recommend, "DEBIT_HOLD_FLOOR_MULTIPLE", 5)
    stamp = doctrine_stamp(20, credit=False)
    assert stamp["doctrine_hold_multiple"] == 5
    assert stamp["doctrine_dte_floor"] == 100


def test_the_credit_stamp_is_the_hold_itself_not_a_multiple_of_it():
    """Public API contract; production-derived narrative omitted."""
    stamp = doctrine_stamp(20, credit=True)
    assert stamp == {"doctrine_hold_multiple": 1, "doctrine_dte_floor": 20,
                     "doctrine_side": "credit"}


def test_a_missing_hold_stamps_a_null_floor_rather_than_a_plausible_one():
    """Public API contract; production-derived narrative omitted."""
    stamp = doctrine_stamp(None, credit=False)
    assert stamp["doctrine_dte_floor"] is None



def test_the_hard_gate_refuses_an_entry_with_no_hold():
    """Public API contract; production-derived narrative omitted."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "daily_recommend.py")).read()
    gate = "if carry_intended_hold(fresh_r, fresh_r, r, effective_idea, idea) is None:"
    assert gate in src
    tail = src.split(gate, 1)[1][:400]
    assert "_block_reasons.append(" in tail
    assert "intended_hold_days is not stated" in tail


def _statement_blocks(tree):
    """Public API contract; production-derived narrative omitted."""
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if isinstance(block, list) and block and all(isinstance(x, ast.stmt) for x in block):
                yield block


def _own_nodes(stmt):
    """Public API contract; production-derived narrative omitted."""
    stack = [stmt]
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                continue
            stack.append(child)


def test_every_re_resolved_order_carries_the_hold():
    """Public API contract; production-derived narrative omitted."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "daily_recommend.py")).read()
    tree = ast.parse(src)
    offenders, stamps_seen = [], 0
    for block in _statement_blocks(tree):
        stamped, carried = set(), set()
        for stmt in block:
            for node in _own_nodes(stmt):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if not (isinstance(target, ast.Attribute)
                                and isinstance(target.value, ast.Name)):
                            continue
                        if target.attr == "model_identity":
                            stamped.add(target.value.id)
                        elif target.attr == "intended_hold_days":
                            carried.add(target.value.id)
                elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == "carry_intended_hold" and node.args
                        and isinstance(node.args[0], ast.Name)):
                    carried.add(node.args[0].id)
        stamps_seen += len(stamped)
        for name in sorted(stamped - carried):
            offenders.append((name, block[0].lineno))
    assert stamps_seen >= 3, (
        "anti-vacuous: expected at least three provenance-stamping blocks (the posted order and "
        "both re-resolves); found %d" % stamps_seen)
    assert not offenders, (
        "these blocks stamp an order with the entry's provenance but never give it the hold, "
        "which is the SYMG leak: %s" % offenders)


def test_the_journal_stamps_the_hold_and_the_doctrine_without_guessing():
    """Public API contract; production-derived narrative omitted."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "daily_recommend.py")).read()
    journal = src.split('"intended_hold_days": positive_hold_days(', 1)
    assert len(journal) == 2, "journal no longer stamps the hold through positive_hold_days"
    assert '"intended_hold_days_source"' in journal[1][:400]
    assert "**doctrine_stamp(" in journal[1][:600]
    assert "// 8))" not in journal[1][:600], "the ceil(dte/8) guess must not come back"
