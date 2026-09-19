"""Public API contract; production-derived narrative omitted."""
from types import SimpleNamespace
import time
import pytest
from exitmgr.connection import IBConnection
from exitmgr.manager import ExitManager


def test_short_marks_require_actual_recent_event_and_current_socket(monkeypatch):
    contract = SimpleNamespace(conId=10)
    item = SimpleNamespace(contract=contract, position=-1, marketPrice=2.0)
    conn = IBConnection('127.0.0.1', 4002, 118)
    conn.ib = SimpleNamespace(portfolio=lambda: [item])
    conn._price_events_accepted = True
    monkeypatch.setattr(conn, 'is_healthy', lambda: True)
    manager = ExitManager.__new__(ExitManager)
    manager.config = SimpleNamespace(arm=True)
    manager.ib_conn = conn
    assert manager._short_marks() == {}
    conn._on_update_portfolio(item)
    assert manager._short_marks() == {10: 2.0}
    conn._portfolio_observations[10]['observed_monotonic'] = time.monotonic() - 46
    assert manager._short_marks() == {}
    conn._on_update_portfolio(item)
    conn._connection_generation += 1
    assert manager._short_marks() == {}


@pytest.mark.parametrize('price', [float('inf'), float('-inf'), float('nan')])
def test_nonfinite_broker_price_revokes_previous_valid_mark(price):
    item = SimpleNamespace(contract=SimpleNamespace(conId=10), position=-1, marketPrice=2.0)
    conn = IBConnection('127.0.0.1', 4002, 118)
    conn._price_events_accepted = True
    conn._on_update_portfolio(item)
    assert 10 in conn.portfolio_mark_observations()
    item.marketPrice = price
    conn._on_update_portfolio(item)
    assert conn.portfolio_mark_observations() == {}
