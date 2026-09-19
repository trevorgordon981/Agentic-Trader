"""Public API contract; production-derived narrative omitted."""
import json
import os
from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock, MagicMock

from exitmgr.config import Config, RulesConfig, TrailingConfig, ScaleOutConfig
from exitmgr.connection import PositionData
from exitmgr.order import OrderResult
from exitmgr.state import State, StateManager
from exitmgr.manager import ExitManager


CON = 1000

JOURNAL = {"ts": "2026-07-01T14:00:00+00:00", "contract_id": CON, "symbol": "AAPL",
           "right": "C", "strike": 200.0, "expiry": "20261231", "quantity": 4, "debit": 2000.0,
           "conviction": 6}


def _rules():
    return RulesConfig(
        profit_target_pct=30.0,
        stop_pct=30.0,
        time_stop_days=10,
        trailing=TrailingConfig(enabled=False),
        scale_out=ScaleOutConfig(enabled=True, first_target_pct=20.0, trim_fraction=0.5),
    )


def _mgr(tmp_path, journal=JOURNAL):
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
    cfg.rules = _rules()
    (tmp_path / "trades.log").write_text(json.dumps(journal) + "\n")
    return ExitManager(cfg), cfg


def _wire(mgr, *, qty, price, expiry="20261231"):
    """Public API contract; production-derived narrative omitted."""
    pos = {CON: PositionData(con_id=CON, symbol="AAPL", right="C",
                             quantity=qty, avg_cost=5.00, expiry=expiry)}
    mgr.ib_conn.get_positions = AsyncMock(return_value=pos)
    mgr.ib_conn.get_open_orders = AsyncMock(return_value={})
    mgr.ib_conn.fetch_quotes = AsyncMock(return_value={CON: {"price": price}})
    mgr.ib_conn.ib = MagicMock()
    mgr.ib_conn.ib.portfolio = lambda: []
    mgr._spot_price = AsyncMock(return_value=None)



    trade = SimpleNamespace(
        order=SimpleNamespace(orderId=555, clientId=0),
        orderStatus=SimpleNamespace(
            status="Filled", avgFillPrice=price, filled=qty, remaining=0),
        fills=[],
    )
    place = AsyncMock(return_value=OrderResult(
        success=True, order_id=555, con_id=CON, trade=trade, client_id=0))
    mgr.order_manager.place_close_order = place
    return place


def _read_exits(cfg):
    path = os.path.join(os.path.dirname(cfg.journal.path) or ".", "exits.log")
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]



def test_close_qty_formula_matches_spec():
    """Public API contract; production-derived narrative omitted."""
    def close_qty(quantity, qf):
        if qf >= 1.0:
            return quantity
        return max(1, min(quantity - 1, round(quantity * qf)))
    assert close_qty(4, 0.5) == 2
    assert close_qty(2, 0.5) == 1
    assert close_qty(3, 0.5) == 2
    assert close_qty(5, 0.5) == 2
    assert close_qty(2, 0.9) == 1
    assert close_qty(4, 1.0) == 4
    assert close_qty(1, 0.5) == 1



def test_scaled_out_survives_save_reload(tmp_path):
    p = str(tmp_path / "state.json")
    sm = StateManager(p)
    sm.state.scaled_out[str(CON)] = True
    sm.save()
    sm2 = StateManager(p)
    assert sm2.state.scaled_out.get(str(CON)) is True


def test_old_state_file_without_scaled_out_loads_empty(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"in_flight": {}, "daily_stats": {}, "last_cycle": None,
                             "peak_prices": {}}))
    sm = StateManager(str(p))
    assert sm.state.scaled_out == {}



@pytest.mark.asyncio
async def test_first_target_trims_to_runner(tmp_path):
    mgr, cfg = _mgr(tmp_path)
    place = _wire(mgr, qty=4, price=6.00)
    await mgr.run_cycle(dry_run=False)

    place.assert_called_once()
    kw = place.call_args.kwargs
    assert kw["con_id"] == CON
    assert kw["quantity"] == 2

    assert mgr.state_manager.state.scaled_out.get(str(CON)) is True
    reloaded = StateManager(cfg.state.path).state
    assert reloaded.scaled_out.get(str(CON)) is True

    r = _read_exits(cfg)[0]
    assert r["reason"] == "scale_out"
    assert r["quantity"] == 2 and r["partial"] is True and r["remaining_qty"] == 2


@pytest.mark.asyncio
async def test_qty2_trim_leaves_one_runner(tmp_path):
    je = dict(JOURNAL, quantity=2, debit=1000.0)
    mgr, cfg = _mgr(tmp_path, journal=je)
    place = _wire(mgr, qty=2, price=6.00)
    await mgr.run_cycle(dry_run=False)
    assert place.call_args.kwargs["quantity"] == 1
    assert mgr.state_manager.state.scaled_out.get(str(CON)) is True



@pytest.mark.asyncio
async def test_already_trimmed_does_not_retrim(tmp_path):
    mgr, cfg = _mgr(tmp_path)

    mgr.state_manager.state.scaled_out[str(CON)] = True
    mgr.state_manager.save()
    place = _wire(mgr, qty=2, price=6.00)
    await mgr.run_cycle(dry_run=False)
    place.assert_not_called()



@pytest.mark.asyncio
async def test_runner_full_close_uses_prorated_basis(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr, cfg = _mgr(tmp_path)
    mgr.state_manager.state.scaled_out[str(CON)] = True
    mgr.state_manager.save()
    place = _wire(mgr, qty=2, price=6.50)
    await mgr.run_cycle(dry_run=False)

    place.assert_called_once()
    assert place.call_args.kwargs["quantity"] == 2
    r = _read_exits(cfg)[0]
    assert r["reason"] == "profit_target"
    assert r["quantity"] == 2
    assert r["partial"] is False
    assert r["entry_debit"] == pytest.approx(1000.0)
    assert r["proceeds"] == pytest.approx(1300.0)
    assert r["realized_pnl"] == pytest.approx(300.0)
    assert r["realized_pnl_pct"] == pytest.approx(30.0)



@pytest.mark.asyncio
async def test_full_risk_exit_closes_full_qty(tmp_path):
    mgr, cfg = _mgr(tmp_path)
    place = _wire(mgr, qty=4, price=3.50)
    await mgr.run_cycle(dry_run=False)

    place.assert_called_once()
    assert place.call_args.kwargs["quantity"] == 4
    assert str(CON) not in mgr.state_manager.state.scaled_out
    r = _read_exits(cfg)[0]
    assert r["reason"] == "stop"
    assert r["quantity"] == 4 and r["partial"] is False
    assert r["entry_debit"] == pytest.approx(2000.0)
    assert r["realized_pnl"] == pytest.approx(3.50 * 100 * 4 - 2000.0)


@pytest.mark.asyncio
async def test_full_profit_target_outranks_scale_out(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr, cfg = _mgr(tmp_path)
    place = _wire(mgr, qty=4, price=6.50)
    await mgr.run_cycle(dry_run=False)
    assert place.call_args.kwargs["quantity"] == 4
    assert str(CON) not in mgr.state_manager.state.scaled_out
    assert _read_exits(cfg)[0]["reason"] == "profit_target"
