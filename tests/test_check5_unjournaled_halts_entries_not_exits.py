"""Public API contract; production-derived narrative omitted."""
import json

import pytest
from unittest.mock import AsyncMock, MagicMock

from exitmgr.config import (Config, RulesConfig, TrailingConfig, ScaleOutConfig,
                            AutoTrailConfig)
from exitmgr.connection import PositionData
from exitmgr.manager import ExitManager
from exitmgr.order import OrderResult
from exitmgr.state import State, reconcile_state


TRACKED = 660001
UNTRACKED = 660002

JOURNAL = {"ts": "2037-08-01T14:00:00+00:00", "contract_id": TRACKED, "symbol": "TESTA",
           "right": "C", "strike": 200.0, "expiry": "20371231", "quantity": 2,
           "debit": 1000.0, "conviction": 7,
           "order_ref": "alfred-entry:1111111111111111111111111111111f"}




def _reconcile(live_positions, live_orders=None, journal=None):
    detail = {}
    safe, alerts = reconcile_state(State(), live_positions, live_orders or {},
                                   journal or {}, detail=detail)
    return safe, alerts, detail


def test_an_unjournaled_position_with_no_resting_order_is_reported_as_unjournaled():
    safe, alerts, detail = _reconcile({UNTRACKED: {"quantity": 1, "avg_cost": 5.0}})
    assert detail["unjournaled"] == {UNTRACKED}
    assert any("[WARN]" in a and str(UNTRACKED) in a for a in alerts)


def test_it_is_NEVER_added_to_inconsistent_because_that_set_only_withholds_EXITS():
    """Public API contract; production-derived narrative omitted."""
    safe, _alerts, detail = _reconcile({UNTRACKED: {"quantity": 1, "avg_cost": 5.0}})
    assert UNTRACKED not in detail["inconsistent"]
    assert detail["inconsistent"] == set()


def test_safe_stays_True_so_startup_and_post_reconnect_cycles_still_run():
    """Public API contract; production-derived narrative omitted."""
    safe, _alerts, _detail = _reconcile({UNTRACKED: {"quantity": 1, "avg_cost": 5.0}})
    assert safe is True


def test_the_FATAL_half_of_check_5_is_unchanged_when_an_order_rests_on_it():
    """Public API contract; production-derived narrative omitted."""
    safe, _alerts, detail = _reconcile(
        {UNTRACKED: {"quantity": 1, "avg_cost": 5.0}},
        live_orders={UNTRACKED: {"order_id": 9, "remaining": 1}})
    assert safe is False
    assert detail["inconsistent"] == {UNTRACKED}
    assert UNTRACKED not in detail["unjournaled"]


def test_a_journalled_position_reports_nothing():
    _safe, _alerts, detail = _reconcile({TRACKED: {"quantity": 2, "avg_cost": 5.0}},
                                        journal={TRACKED: {"debit": 1000.0}})
    assert detail["unjournaled"] == set() and detail["inconsistent"] == set()


def test_a_caller_passing_no_detail_is_byte_for_byte_unaffected():
    """Public API contract; production-derived narrative omitted."""
    safe, alerts = reconcile_state(State(), {UNTRACKED: {"quantity": 1, "avg_cost": 5.0}}, {}, {})
    assert safe is True and any(str(UNTRACKED) in a for a in alerts)




def _mgr(tmp_path):
    cfg = Config()
    cfg.dry_run = False
    cfg.loop_mode = False
    cfg.journal.path = str(tmp_path / "trades.log")
    cfg.state.path = str(tmp_path / "state.json")
    cfg.kill_switch.path = str(tmp_path / "KILL")
    cfg.audit_path = str(tmp_path / "audit.jsonl")
    cfg.manage_positions = False
    cfg.alerts_channel = ""
    cfg.error_channel = ""
    cfg.scope.mode = "all"
    cfg.rules = RulesConfig(
        profit_target_pct=300.0, stop_pct=30.0, time_stop_days=None,
        trailing=TrailingConfig(enabled=False),
        scale_out=ScaleOutConfig(enabled=False),
        auto_trail=AutoTrailConfig(enabled=False))
    (tmp_path / "trades.log").write_text(json.dumps(JOURNAL) + "\n")
    return ExitManager(cfg)


def _wire(mgr, positions):
    mgr.ib_conn.get_positions = AsyncMock(return_value=positions)
    mgr.ib_conn.get_open_orders = AsyncMock(return_value={})
    mgr.ib_conn.fetch_quotes = AsyncMock(return_value={
        cid: {"price": 3.50, "bid": 3.40, "ask": 3.60, "mark": 3.50} for cid in positions})
    mgr.ib_conn.ib = MagicMock()
    mgr.ib_conn.ib.portfolio = lambda: []
    mgr.ib_conn.ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[])
    mgr._spot_price = AsyncMock(return_value=None)
    place = AsyncMock(return_value=OrderResult(success=True, order_id=1, con_id=0, trade=None))
    mgr.order_manager.place_close_order = place
    return place


def _pos(con_id, quantity=2, avg_cost=5.0):
    return PositionData(con_id=con_id, symbol="TESTA", right="C", quantity=quantity,
                        avg_cost=avg_cost, expiry="20371231")


@pytest.mark.asyncio
async def test_the_untracked_position_HALTS_ENTRIES(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr = _mgr(tmp_path)
    _wire(mgr, {TRACKED: _pos(TRACKED), UNTRACKED: _pos(UNTRACKED)})
    await mgr.run_cycle(dry_run=False)
    assert mgr._unjournaled_con_ids == {UNTRACKED}
    assert mgr._reconcile_ok is False, "new exposure was not halted"


@pytest.mark.asyncio
async def test_and_STILL_CLOSES_the_position_it_is_about(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr = _mgr(tmp_path)
    place = _wire(mgr, {TRACKED: _pos(TRACKED), UNTRACKED: _pos(UNTRACKED)})
    await mgr.run_cycle(dry_run=False)
    closed = {c.kwargs["con_id"] for c in place.call_args_list}
    assert UNTRACKED in closed, "the untracked position lost its stop -- the naive fix's defect"
    assert TRACKED in closed, "a clean position was collaterally blocked"
    assert mgr._reconcile_bad_con_ids == set(), "an unjournaled con_id must never land here"


@pytest.mark.asyncio
async def test_a_clean_book_halts_nothing(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr = _mgr(tmp_path)
    place = _wire(mgr, {TRACKED: _pos(TRACKED)})
    await mgr.run_cycle(dry_run=False)
    assert mgr._unjournaled_con_ids == set()
    assert mgr._reconcile_ok is True
    place.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_halt_LIFTS_once_the_position_is_accounted_for(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr = _mgr(tmp_path)
    _wire(mgr, {TRACKED: _pos(TRACKED), UNTRACKED: _pos(UNTRACKED)})
    await mgr.run_cycle(dry_run=False)
    assert mgr._reconcile_ok is False

    _wire(mgr, {TRACKED: _pos(TRACKED)})
    await mgr.run_cycle(dry_run=False)
    assert mgr._reconcile_ok is True
