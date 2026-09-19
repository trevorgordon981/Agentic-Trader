"""Public API contract; production-derived narrative omitted."""
import json

import pytest
from unittest.mock import AsyncMock, MagicMock

from exitmgr.config import Config, RulesConfig, TrailingConfig, ScaleOutConfig
from exitmgr.connection import PositionData
from exitmgr.order import OrderResult
from exitmgr.manager import ExitManager
from exitmgr import manager as manager_mod

CON = 2000

JOURNAL = {"ts": "2026-07-01T14:00:00+00:00", "contract_id": CON, "symbol": "AAPL",
           "right": "C", "strike": 200.0, "expiry": "20261231", "quantity": 4, "debit": 2000.0,
           "conviction": 6}


def _mgr(tmp_path):
    cfg = Config()
    cfg.dry_run = False
    cfg.loop_mode = False
    cfg.journal.path = str(tmp_path / "trades.log")
    cfg.state.path = str(tmp_path / "state.json")
    cfg.kill_switch.path = str(tmp_path / "KILL")
    cfg.audit_path = str(tmp_path / "audit.jsonl")
    cfg.manage_positions = True
    cfg.alerts_channel = ""
    cfg.error_channel = ""

    cfg.rules = RulesConfig(profit_target_pct=100.0, stop_pct=90.0, time_stop_days=10,
                            trailing=TrailingConfig(enabled=False),
                            scale_out=ScaleOutConfig(enabled=False))
    (tmp_path / "trades.log").write_text(json.dumps(JOURNAL) + "\n")
    return ExitManager(cfg), cfg


def _wire(mgr, *, price=5.00):
    """Public API contract; production-derived narrative omitted."""
    pos = {CON: PositionData(con_id=CON, symbol="AAPL", right="C",
                             quantity=4, avg_cost=5.00, expiry="20261231")}
    mgr.ib_conn.get_positions = AsyncMock(return_value=pos)
    mgr.ib_conn.get_open_orders = AsyncMock(return_value={})
    mgr.ib_conn.fetch_quotes = AsyncMock(return_value={CON: {"price": price}})
    mgr.ib_conn.ib = MagicMock()
    mgr.ib_conn.ib.portfolio = lambda: []
    mgr._spot_price = AsyncMock(return_value=None)
    mgr.order_manager.place_close_order = AsyncMock(
        return_value=OrderResult(success=True, order_id=1, con_id=CON, trade=None))


def _last_mark(mgr):
    return mgr.state_manager.state.mark_path[str(CON)][-1]


def _last_ledger_row(event_type="position_path"):
    """Public API contract; production-derived narrative omitted."""
    import json as _json
    from exitmgr import event_capture as _evt
    rows = []
    try:
        with open(_evt.events_path()) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = _json.loads(line)
                except Exception:
                    continue
                if r.get("event_type") == event_type:
                    rows.append(r)
    except FileNotFoundError:
        return None
    return rows[-1] if rows else None


@pytest.mark.asyncio
async def test_full_reason_raw_and_input_on_mark(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    long_reason = "up and trending; hold the runner. " * 30
    raw = '{"decisions": {"%d": {"action": "hold", "reason": "%s"}}}' % (CON, long_reason)
    decisions = {CON: {"action": "hold", "trail_activation_gain_pct": None,
                       "trail_giveback_fraction": None, "stop_pct": None, "reason": long_reason}}
    monkeypatch.setattr(manager_mod, "assess_positions", lambda *a, **k: (decisions, {"raw": raw}))
    mgr, cfg = _mgr(tmp_path)
    _wire(mgr)
    await mgr.run_cycle(dry_run=False)
    m = _last_mark(mgr)
    assert m["mgmt_action"] == "hold"
    assert m["mgmt_reason"] == long_reason and len(m["mgmt_reason"]) > 200
    assert "mgmt_input" not in m
    assert "mgmt_raw" not in m
    led = _last_ledger_row()
    assert led is not None, "no position_path row was written to the capture ledger"
    assert led["mgmt_raw"] == raw
    assert led["mgmt_input"]["con_id"] == CON
    assert led["price"] is not None and led["value"] is not None


@pytest.mark.asyncio
async def test_hold_by_omission_recorded(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    raw = '{"decisions": {}}'
    monkeypatch.setattr(manager_mod, "assess_positions", lambda *a, **k: ({}, {"raw": raw}))
    mgr, cfg = _mgr(tmp_path)
    _wire(mgr)
    await mgr.run_cycle(dry_run=False)
    m = _last_mark(mgr)
    assert m["mgmt_action"] == "hold"
    assert "implicit hold" in m["mgmt_reason"]
    assert "mgmt_input" not in m
    led = _last_ledger_row()
    assert led is not None and led["mgmt_raw"] == raw
    assert led["mgmt_input"]["con_id"] == CON


@pytest.mark.asyncio
async def test_failed_attempt_is_not_captured_as_an_implicit_hold(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    monkeypatch.setattr(
        manager_mod, "assess_positions",
        lambda *a, **k: ({}, {"raw": "not-json", "attempt_ok": False,
                               "failure_reason": "ModelDeadlineExceeded: timed out",
                               "model_identity_source": "unknown"}))
    mgr, _ = _mgr(tmp_path)
    mgr.config.rules.stop_pct = 30.0
    _wire(mgr, price=3.0)
    await mgr.run_cycle(dry_run=False)

    mark = _last_mark(mgr)
    assert "mgmt_action" not in mark
    assert "mgmt_input" not in mark and "mgmt_raw" not in mark
    row = _last_ledger_row()
    assert row["mgmt_action"] is None
    assert row["mgmt_attempt_ok"] is False
    assert row["mgmt_failure_reason"] == "ModelDeadlineExceeded: timed out"
    assert row["mgmt_input"]["con_id"] == CON
    assert row["mgmt_raw"] == "not-json"
    mgr.order_manager.place_close_order.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_fabricated_hold_when_model_down(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    monkeypatch.setattr(manager_mod, "assess_positions", lambda *a, **k: ({}, {"raw": None}))
    mgr, cfg = _mgr(tmp_path)
    _wire(mgr)
    await mgr.run_cycle(dry_run=False)
    m = _last_mark(mgr)
    assert "mgmt_action" not in m
    assert "mgmt_raw" not in m



    led = _last_ledger_row()
    assert led is None or led.get("mgmt_raw") is None
