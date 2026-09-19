"""Public API contract; production-derived narrative omitted."""
import json

import pytest
from unittest.mock import AsyncMock, MagicMock

from exitmgr.config import Config, RulesConfig, TrailingConfig, ScaleOutConfig
from exitmgr.connection import PositionData
from exitmgr.order import OrderResult
from exitmgr.manager import ExitManager
from exitmgr import construction as _construction


CON = 5100
BASE = {"ts": "2026-07-01T14:00:00+00:00", "contract_id": CON, "symbol": "AAPL",
        "right": "C", "strike": 200.0, "expiry": "20261231", "quantity": 4, "debit": 2000.0,
        "conviction": 6}


LEGACY_TIER = dict(BASE, profit_target_pct=30.0, stop_pct=25.0)

STAMPED = dict(BASE, profit_target_pct=150.0, stop_pct=25.0,
               tp_policy=_construction.TP_POLICY_CURRENT)

BARE = dict(BASE)


def _mgr(tmp_path, journal, *, config_tp=None):
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
    cfg.rules = RulesConfig(profit_target_pct=config_tp, stop_pct=90.0, time_stop_days=10,
                            trailing=TrailingConfig(enabled=False),
                            scale_out=ScaleOutConfig(enabled=False))
    (tmp_path / "trades.log").write_text(json.dumps(journal) + "\n")
    return ExitManager(cfg), cfg


def _wire(mgr, *, price):
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


def _last_path_row():
    from exitmgr import event_capture as _evt
    rows = []
    try:
        with open(_evt.events_path()) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("event_type") == "position_path":
                    rows.append(r)
    except FileNotFoundError:
        return None
    return rows[-1] if rows else None



@pytest.mark.parametrize("journal,config_tp,expected", [
    (LEGACY_TIER, None, None),
    (LEGACY_TIER, 60.0, None),
    (STAMPED, None, 150.0),
    (STAMPED, 60.0, 150.0),
    (BARE, 60.0, 60.0),
    (BARE, None, None),
])
def test_position_rules_is_the_take_profit_actually_in_force(tmp_path, journal, config_tp, expected):
    mgr, cfg = _mgr(tmp_path, journal, config_tp=config_tp)
    assert mgr._position_rules(journal).profit_target_pct == expected


    assert mgr._position_rules(journal).stop_pct == (journal.get("stop_pct")
                                                     or cfg.rules.stop_pct)


def test_a_refused_tier_value_is_not_revived_by_the_config_fallback(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr, _ = _mgr(tmp_path, LEGACY_TIER, config_tp=60.0)
    assert LEGACY_TIER["profit_target_pct"] == 30.0
    assert _construction.journal_take_profit_pct(LEGACY_TIER) is None
    assert mgr._position_rules(LEGACY_TIER).profit_target_pct is None



@pytest.mark.asyncio
async def test_capture_records_no_distance_to_a_refused_target(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr, _ = _mgr(tmp_path, LEGACY_TIER, config_tp=None)
    _wire(mgr, price=5.00)
    await mgr.run_cycle(dry_run=False)

    row = _last_path_row()
    assert row is not None, "no position_path row was written"
    assert row["dist_to_tp_pct"] is None
    assert row["dist_to_sl_pct"] is not None, "the STOP limb must be unaffected by this change"


@pytest.mark.asyncio
async def test_capture_records_the_distance_to_a_target_that_is_in_force(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr, _ = _mgr(tmp_path, STAMPED, config_tp=None)
    _wire(mgr, price=5.00)
    await mgr.run_cycle(dry_run=False)

    row = _last_path_row()
    assert row["dist_to_tp_pct"] == 150.0


@pytest.mark.asyncio
async def test_capture_follows_the_config_rule_when_nothing_is_journaled(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr, _ = _mgr(tmp_path, BARE, config_tp=60.0)
    _wire(mgr, price=5.00)
    await mgr.run_cycle(dry_run=False)

    row = _last_path_row()
    assert row["dist_to_tp_pct"] == 60.0


@pytest.mark.asyncio
async def test_the_captured_distance_matches_the_live_resolution_exactly(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr, _ = _mgr(tmp_path, STAMPED, config_tp=60.0)
    _wire(mgr, price=6.00)
    await mgr.run_cycle(dry_run=False)

    row = _last_path_row()
    live_tp = mgr._position_rules(STAMPED).profit_target_pct
    assert row["dist_to_tp_pct"] == round(float(live_tp) - row["pnl_pct"], 2)
