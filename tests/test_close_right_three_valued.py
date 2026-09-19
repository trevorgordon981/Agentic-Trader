"""Public API contract; production-derived narrative omitted."""
import os
import sys
from unittest.mock import MagicMock

import pytest

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP not in sys.path:
    sys.path.insert(0, APP)

from exitmgr.order import CloseRight, OrderManager


class _Contract:
    def __init__(self, con_id, right):
        self.conId = con_id
        self.right = right


class _Row:
    def __init__(self, con_id, right):
        self.contract = _Contract(con_id, right)


def om(portfolio):
    """Public API contract; production-derived narrative omitted."""
    m = OrderManager.__new__(OrderManager)
    m.ib_conn = MagicMock()
    if isinstance(portfolio, Exception):
        m.ib_conn.ib.portfolio.side_effect = portfolio
    else:
        m.ib_conn.ib.portfolio.return_value = portfolio
    return m


def test_the_callers_right_wins_and_is_reported_as_known():
    r = om([]) ._resolve_close_right(1, "P")
    assert r.resolved and r.right == "P" and r.for_contract == "P"


def test_the_portfolio_answers_when_the_caller_does_not():
    r = om([_Row(7, "P"), _Row(8, "C")])._resolve_close_right(7, None)
    assert r.resolved and r.right == "P" and r.for_contract == "P"


def test_an_unreadable_portfolio_is_UNKNOWN_not_a_call():
    """Public API contract; production-derived narrative omitted."""
    r = om(RuntimeError("Not connected to IB"))._resolve_close_right(7, None)
    assert r.resolved is False
    assert r.right is None
    assert "portfolio unreadable" in r.error
    assert r.for_contract == "", "an unknown right must assert nothing, not 'C'"


def test_a_con_id_absent_from_the_portfolio_is_UNKNOWN_not_a_call():
    r = om([_Row(999, "C")])._resolve_close_right(7, None)
    assert r.resolved is False and r.for_contract == ""


def test_an_empty_portfolio_is_UNKNOWN_not_a_call():
    r = om([])._resolve_close_right(7, None)
    assert r.resolved is False and r.for_contract == ""


def test_unknown_never_claims_to_be_a_put_either():
    """Public API contract; production-derived narrative omitted."""
    r = om([])._resolve_close_right(7, None)
    assert r.right not in ("C", "P")


def test_the_truth_test_raises_like_OrderView_does():
    """Public API contract; production-derived narrative omitted."""
    with pytest.raises(TypeError):
        bool(CloseRight.unknown("x"))
    with pytest.raises(TypeError):
        bool(CloseRight.known("C"))
    with pytest.raises(TypeError):
        if om([])._resolve_close_right(7, None):
            pass


def test_unknown_is_not_constructible_from_a_value_that_looks_resolved():
    assert CloseRight.unknown("").error
    with pytest.raises(AssertionError):
        CloseRight.known("X")
    with pytest.raises(AssertionError):
        CloseRight.known("")


def test_the_resolver_no_longer_returns_a_bare_string():
    """Public API contract; production-derived narrative omitted."""
    for arg in (None, "", "junk"):
        assert isinstance(om([])._resolve_close_right(7, arg), CloseRight)


def test_the_source_carries_no_C_fallback_any_more():
    with open(os.path.join(APP, "exitmgr", "order.py"), encoding="utf-8") as fh:
        body = fh.read().split("def _resolve_close_right", 1)[1].split("\n    def ", 1)[0]
    assert 'return "C"' not in body and "return 'C'" not in body
