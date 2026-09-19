"""Public API contract; production-derived narrative omitted."""

import os
import sys

import pytest







if sys.version_info < (3, 12) and not os.environ.get("EXITMGR_ALLOW_ANY_PYTHON"):
    raise RuntimeError(
        "This suite must run on the interpreter production uses (>=3.12). Detected %d.%d.%d at %s.\n"
        "Use: /opt/agentic-trader/ib-grader-venv/bin/python -m pytest tests/ -q -p no:cacheprovider"
        % (sys.version_info[0], sys.version_info[1], sys.version_info[2], sys.executable))
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from typing import Dict, Optional
from dataclasses import dataclass, field

from exitmgr.config import Config, RulesConfig, TrailingConfig








@pytest.fixture(autouse=True)
def isolate_dataset_dir(tmp_path, monkeypatch):
    ddir = tmp_path / "dataset_isolated"
    ddir.mkdir(exist_ok=True)
    monkeypatch.setenv("EXITMGR_DATASET_DIR", str(ddir))
    monkeypatch.setenv("EXITMGR_SCHEMA_REJECTION_DIR", str(tmp_path / "schema-rejections"))




    capdir = tmp_path / "trade_capture_isolated"
    capdir.mkdir(exist_ok=True)
    monkeypatch.setenv("TRADE_CAPTURE_DIR", str(capdir))





    monkeypatch.setenv("EXITMGR_MGMT_ALERTED_PATH", str(tmp_path / "mgmt-alerted.json"))
    monkeypatch.setenv("EXITMGR_RECONCILE_TS_PATH", str(tmp_path / "reconcile_alert_ts"))




    monkeypatch.setenv("EXITMGR_LINK_ROTATIONS_PATH", str(tmp_path / "link-rotations.json"))
    monkeypatch.setenv("EXITMGR_ENRICH_CACHE_DIR", str(tmp_path / "enrich-cache"))
    monkeypatch.setenv("EXITMGR_SHADOW_LOG", str(tmp_path / "shadow-decisions.jsonl"))




    monkeypatch.setenv("EXITMGR_SHADOW_EXPERIMENT_PATH", str(tmp_path / "shadow-experiment.json"))
    monkeypatch.setenv("EXITMGR_EXITS_LOG", str(tmp_path / "exits.log"))





    monkeypatch.setenv("EXITMGR_FLEX_ARCHIVE", str(tmp_path / "flex-archive"))


    monkeypatch.setenv("EXITMGR_ALERT_THROTTLE", str(tmp_path / "alert-throttle.json"))






    monkeypatch.setenv("EXITMGR_ENTRY_RESERVATIONS", str(tmp_path / "entry-reservations.json"))
    monkeypatch.setenv("EXITMGR_ENTRY_RESERVATION_LOCK", str(tmp_path / "entry-reservations.lock"))
    monkeypatch.setenv("EXITMGR_ORDER_LOCK", str(tmp_path / "order-mutation.lock"))
    monkeypatch.setenv("EXITMGR_FLEX_ENV", str(tmp_path / "flex.env"))



    from exitmgr import trader as trader_mod
    monkeypatch.setattr(
        trader_mod, "active_campaign_conflict_symbols", lambda _path: frozenset())
    yield str(ddir)




@pytest.fixture(autouse=True)
def mock_ib_async():
    """Public API contract; production-derived narrative omitted."""
    mock_ib = MagicMock()
    mock_contract = MagicMock()
    mock_order = MagicMock()
    mock_ticker = MagicMock()


    mock_ib.connect = AsyncMock(return_value=None)
    mock_ib.disconnect = MagicMock()
    mock_ib.reqPositionsAsync = AsyncMock(return_value=[])
    mock_ib.reqOpenOrdersAsync = AsyncMock(return_value=[])
    mock_ib.reqTickersAsync = AsyncMock(return_value=[])
    mock_ib.placeOrderAsync = AsyncMock(return_value=mock_order)


    mock_order.orderId = 12345
    mock_order.action = "SELL"
    mock_order.orderType = "LMT"
    mock_order.totalQuantity = 1
    mock_order.lmtPrice = 5.0
    mock_order.filled = 0


    mock_contract.conId = 123456
    mock_contract.symbol = "AAPL"
    mock_contract.right = "C"
    mock_contract.secType = "OPT"


    mock_ticker.contract = mock_contract
    mock_ticker.bid = 4.5
    mock_ticker.ask = 5.5
    mock_ticker.last = 5.0
    mock_ticker.mark = 5.0

    with patch.dict('sys.modules', {'ib_async': MagicMock()}):

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
    """Public API contract; production-derived narrative omitted."""
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
    """Public API contract; production-derived narrative omitted."""
    return str(tmp_path / "test_state.json")


@pytest.fixture
def temp_journal_file(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    journal_path = tmp_path / "test_trades.log"
    journal_content = """{"contract_id": 123456, "symbol": "AAPL", "right": "C", "quantity": 1, "debit": 500.0}
{"contract_id": 234567, "symbol": "TSLA", "right": "C", "quantity": 2, "debit": 1200.0}
{"contract_id": 345678, "symbol": "SPY", "right": "C", "quantity": 1, "debit": 300.0}
"""
    journal_path.write_text(journal_content)
    return str(journal_path)










import pytest as _pytest


@_pytest.fixture(autouse=True, scope="session")
def _block_real_slack():
    """Public API contract; production-derived narrative omitted."""
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
    from exitmgr import pipeline_notice as _pipeline_notice
    _real_notice = _pipeline_notice._post
    _pipeline_notice._post = lambda *a, **k: "test-notice-receipt"
    _blocked = []

    def _refuse(text, channel_id=None, *a, **k):
        _blocked.append((channel_id, (text or "")[:120]))
        return True

    _alerting.post = _refuse
    try:
        yield
    finally:
        _pipeline_notice._post = _real_notice
        if _real is not None:
            _alerting.post = _real
        if _blocked:
            print("\n[conftest] blocked %d real Slack post(s) during the test run "
                  "(set ALLOW_TEST_SLACK=1 to permit)" % len(_blocked))
