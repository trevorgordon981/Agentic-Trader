"""Pytest fixtures and mocks for IB connection."""

import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from typing import Dict, Optional
from dataclasses import dataclass, field

from exitmgr.config import Config, RulesConfig, TrailingConfig


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
