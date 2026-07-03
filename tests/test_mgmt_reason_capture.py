"""Manager-level tests for FULL per-cycle position-management reasoning capture (2026-07-03).

The per-cycle position-management LLM assessment is now recorded onto each mark IN FULL for the
fine-tuning corpus: the UN-truncated reason, the raw model response (mgmt_raw), and the exact
per-position view fed to the model (mgmt_input). Silent hold-by-omission is also recorded so a
position the model implicitly holds is not invisible to the dataset. ADDITIVE + record-only:
no orders are placed and the exit decisions are unchanged. run_cycle is driven with the broker /
order layer mocked (mirrors tests/test_scale_out_hook.py).
"""
import json

import pytest
from unittest.mock import AsyncMock, MagicMock

from exitmgr.config import Config, RulesConfig, TrailingConfig, ScaleOutConfig
from exitmgr.connection import PositionData
from exitmgr.order import OrderResult
from exitmgr.manager import ExitManager
from exitmgr import manager as manager_mod

CON = 2000
# Far expiry so the DTE<=10 time-stop never fires; entry 5.00/share x 4 = $2000 debit.
JOURNAL = {"ts": "2026-07-01T14:00:00+00:00", "contract_id": CON, "symbol": "AAPL",
           "right": "C", "strike": 200.0, "expiry": "20261231", "quantity": 4, "debit": 2000.0,
           "conviction": 6}


def _mgr(tmp_path):
    cfg = Config()
    cfg.dry_run = False
    cfg.loop_mode = False
    cfg.journal.path = str(tmp_path / "trades.log")
    cfg.state.path = str(tmp_path / "state.json")
    cfg.kill_switch.path = str(tmp_path / "KILL")     # absent -> kill switch inactive
    cfg.audit_path = str(tmp_path / "audit.jsonl")
    cfg.manage_positions = True                        # exercise the model-assessment path
    cfg.alerts_channel = ""
    cfg.error_channel = ""
    # Wide targets so a FLAT position (0% P&L) never triggers an exit -> mark is recorded, held.
    cfg.rules = RulesConfig(profit_target_pct=100.0, stop_pct=90.0, time_stop_days=10,
                            trailing=TrailingConfig(enabled=False),
                            scale_out=ScaleOutConfig(enabled=False))
    (tmp_path / "trades.log").write_text(json.dumps(JOURNAL) + "\n")
    return ExitManager(cfg), cfg


def _wire(mgr, *, price=5.00):
    """Flat position (price == entry 5.00/share) -> no exit fires; the mark IS recorded."""
    pos = {CON: PositionData(con_id=CON, symbol="AAPL", right="C",
                             quantity=4, avg_cost=5.00, expiry="20261231")}
    mgr.ib_conn.get_positions = AsyncMock(return_value=pos)
    mgr.ib_conn.get_open_orders = AsyncMock(return_value={})
    mgr.ib_conn.fetch_quotes = AsyncMock(return_value={CON: {"price": price}})
    mgr.ib_conn.ib = MagicMock()
    mgr.ib_conn.ib.portfolio = lambda: []              # no server marking -> use the quote
    mgr._spot_price = AsyncMock(return_value=None)
    mgr.order_manager.place_close_order = AsyncMock(
        return_value=OrderResult(success=True, order_id=1, con_id=CON, trade=None))


def _last_mark(mgr):
    return mgr.state_manager.state.mark_path[str(CON)][-1]


@pytest.mark.asyncio
async def test_full_reason_raw_and_input_on_mark(tmp_path, monkeypatch):
    """Explicit decision: the FULL (untruncated) reason, raw response, and per-position input
    fed to the model are all persisted on the mark."""
    long_reason = "up and trending; hold the runner. " * 30   # ~1000 chars, >> old 200 clamp
    raw = '{"decisions": {"%d": {"action": "hold", "reason": "%s"}}}' % (CON, long_reason)
    decisions = {CON: {"action": "hold", "trail_activation_gain_pct": None,
                       "trail_giveback_fraction": None, "stop_pct": None, "reason": long_reason}}
    monkeypatch.setattr(manager_mod, "assess_positions", lambda *a, **k: (decisions, {"raw": raw}))
    mgr, cfg = _mgr(tmp_path)
    _wire(mgr)
    await mgr.run_cycle(dry_run=False)
    m = _last_mark(mgr)
    assert m["mgmt_action"] == "hold"                                  # existing behavior preserved
    assert m["mgmt_reason"] == long_reason and len(m["mgmt_reason"]) > 200   # NOT truncated at 200
    assert m["mgmt_raw"] == raw                                        # full raw model response
    assert m["mgmt_input"]["con_id"] == CON                            # exact view fed to the model


@pytest.mark.asyncio
async def test_hold_by_omission_recorded(tmp_path, monkeypatch):
    """The model RAN (raw present) but omitted the position -> a lightweight implicit-hold row is
    recorded so the silent hold is not invisible to the dataset."""
    raw = '{"decisions": {}}'
    monkeypatch.setattr(manager_mod, "assess_positions", lambda *a, **k: ({}, {"raw": raw}))
    mgr, cfg = _mgr(tmp_path)
    _wire(mgr)
    await mgr.run_cycle(dry_run=False)
    m = _last_mark(mgr)
    assert m["mgmt_action"] == "hold"
    assert "implicit hold" in m["mgmt_reason"]
    assert m["mgmt_raw"] == raw
    assert m["mgmt_input"]["con_id"] == CON


@pytest.mark.asyncio
async def test_no_fabricated_hold_when_model_down(tmp_path, monkeypatch):
    """Model down / deferred (raw is None) -> NO fabricated hold and no mgmt_raw on the mark."""
    monkeypatch.setattr(manager_mod, "assess_positions", lambda *a, **k: ({}, {"raw": None}))
    mgr, cfg = _mgr(tmp_path)
    _wire(mgr)
    await mgr.run_cycle(dry_run=False)
    m = _last_mark(mgr)
    assert "mgmt_action" not in m        # None is not merged -> no fabricated hold
    assert "mgmt_raw" not in m           # None is not merged
