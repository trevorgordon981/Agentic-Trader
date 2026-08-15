"""Pytest fixtures and mocks for IB connection."""

import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from typing import Dict, Optional
from dataclasses import dataclass, field

from exitmgr.config import Config, RulesConfig, TrailingConfig


# --------------------------------------------------------------------------------------------
# DATASET ISOLATION (2026-07-03): a pytest run once wrote 141 synthetic rows into the PRODUCTION
# data/*.jsonl fine-tuning corpus (SPY 50C / net_liq 1010 / "quotes unavailable" / raw_strategist
# null). trade_capture.dataset_dir() now honors $EXITMGR_DATASET_DIR; this autouse fixture points
# EVERY test at a fresh tmp dir so no capture (trader / daily_slate / manager / morning_review)
# can ever touch the real data/ dir again. monkeypatch auto-restores the env after each test.
@pytest.fixture(autouse=True)
def isolate_dataset_dir(tmp_path, monkeypatch):
    ddir = tmp_path / "dataset_isolated"
    ddir.mkdir(exist_ok=True)
    monkeypatch.setenv("EXITMGR_DATASET_DIR", str(ddir))
    # v6 RAW-EVENT store isolation (2026-07-21): the capture_v6 hooks (entry_decision / exit_action /
    # order_fill / position_path / ...) fire via trade_capture + manager + exec_capture during tests.
    # Point TRADE_CAPTURE_DIR at a fresh tmp dir so no test can ever write into the real
    # ~/trade-capture store (same rationale as EXITMGR_DATASET_DIR above).
    v6dir = tmp_path / "trade_capture_isolated"
    v6dir.mkdir(exist_ok=True)
    monkeypatch.setenv("TRADE_CAPTURE_DIR", str(v6dir))
    yield str(ddir)



# Mock IB and ib_async before importing manager modules
@pytest.fixture(autouse=True)
def mock_ib_async():
    """Mock ib_async module completely."""
    mock_ib = MagicMock()
    mock_contract = MagicMock()
    mock_order = MagicMock()
    mock_ticker = MagicMock()

    # Make IB instance methods async
    mock_ib.connect = AsyncMock(return_value=None)
    mock_ib.disconnect = MagicMock()
    mock_ib.reqPositionsAsync = AsyncMock(return_value=[])
    mock_ib.reqOpenOrdersAsync = AsyncMock(return_value=[])
    mock_ib.reqTickersAsync = AsyncMock(return_value=[])
    mock_ib.placeOrderAsync = AsyncMock(return_value=mock_order)

    # Configure mock order
    mock_order.orderId = 12345
    mock_order.action = "SELL"
    mock_order.orderType = "LMT"
    mock_order.totalQuantity = 1
    mock_order.lmtPrice = 5.0
    mock_order.filled = 0

    # Configure mock contract
    mock_contract.conId = 123456
    mock_contract.symbol = "AAPL"
    mock_contract.right = "C"
    mock_contract.secType = "OPT"

    # Configure mock ticker
    mock_ticker.contract = mock_contract
    mock_ticker.bid = 4.5
    mock_ticker.ask = 5.5
    mock_ticker.last = 5.0
    mock_ticker.mark = 5.0

    with patch.dict('sys.modules', {'ib_async': MagicMock()}):
        # Create module mocks
        import sys
        ib_async_mock = MagicMock()
        ib_async_mock.IB.return_value = mock_ib
        ib_async_mock.Contract = MagicMock(return_value=mock_contract)
        ib_async_mock.Order = MagicMock(return_value=mock_order)
        ib_async_mock.Position = MagicMock()
        ib_async_mock.Ticker = MagicMock()
        sys.modules['ib_async'] = ib_async_mock

        yield {
            'ib': mock_ib,
            'contract': mock_contract,
            'order': mock_order,
            'ticker': mock_ticker,
        }


@pytest.fixture
def sample_config():
    """Create a sample configuration for testing."""
    cfg = Config()
    cfg.dry_run = True
    cfg.loop_mode = False
    cfg.ib.host = "127.0.0.1"
    cfg.ib.port = 7497
    cfg.ib.client_id = 42
    cfg.journal.path = "./test_trades.log"
    cfg.state.path = "./test_state.json"
    cfg.kill_switch.path = "./TEST_KILL_SWITCH"
    cfg.loop.interval_seconds = 60
    cfg.scope.mode = "journal"
    cfg.caps.max_orders_per_cycle = 5
    cfg.caps.max_orders_per_day = 20
    cfg.caps.max_notional_per_day = 50000.0
    cfg.rules.profit_target_pct = 100.0
    cfg.rules.stop_pct = 50.0
    cfg.rules.time_stop_days = 3
    cfg.rules.trailing = TrailingConfig(
        enabled=False,
        activation_gain_pct=50.0,
        giveback_fraction=0.5,
    )
    return cfg


@pytest.fixture
def temp_state_file(tmp_path):
    """Create a temporary state file path."""
    return str(tmp_path / "test_state.json")


@pytest.fixture
def temp_journal_file(tmp_path):
    """Create a temporary journal file with sample entries."""
    journal_path = tmp_path / "test_trades.log"
    journal_content = """{"contract_id": 123456, "symbol": "AAPL", "right": "C", "quantity": 1, "debit": 500.0}
{"contract_id": 234567, "symbol": "TSLA", "right": "C", "quantity": 2, "debit": 1200.0}
{"contract_id": 345678, "symbol": "SPY", "right": "C", "quantity": 1, "debit": 300.0}
"""
    journal_path.write_text(journal_content)
    return str(journal_path)


# ---------------------------------------------------------------- Slack containment (2026-08-13)
# Running the exit regression posted FABRICATED "EXIT DECISION" cards to Trevor's real Slack --
# SMCI at -39.8%, SPCX at -366.1% (impossible: a debit spread bottoms at -100%). They carried real
# con_ids, so they read as though live positions had been cut. Individual tests do patch
# alerting.post, and cards still escaped, so the guard belongs at the SUITE boundary.
#
# Session-scoped + autouse: no test can reach the network. Tests that assert on posting apply
# their own function-scoped monkeypatch, which takes precedence over this.
import pytest as _pytest


@_pytest.fixture(autouse=True, scope="session")
def _block_real_slack():
    """Refuse every real Slack post for the whole session unless ALLOW_TEST_SLACK=1."""
    import os as _os
    if _os.environ.get("ALLOW_TEST_SLACK") == "1":
        yield
        return
    try:
        from exitmgr import alerting as _alerting
    except Exception:
        yield
        return

    _real = getattr(_alerting, "post", None)
    _blocked = []

    def _refuse(text, channel_id=None, *a, **k):
        _blocked.append((channel_id, (text or "")[:120]))
        return True          # mimic a successful post so dedupe/retry logic behaves normally

    _alerting.post = _refuse
    try:
        yield
    finally:
        if _real is not None:
            _alerting.post = _real
        if _blocked:
            print("\n[conftest] blocked %d real Slack post(s) during the test run "
                  "(set ALLOW_TEST_SLACK=1 to permit)" % len(_blocked))
