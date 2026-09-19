"""Public API contract; production-derived narrative omitted."""
import json

import pytest
from unittest.mock import AsyncMock, MagicMock

from exitmgr.config import Config, RulesConfig, TrailingConfig, ScaleOutConfig
from exitmgr.connection import PositionData
from exitmgr.order import OrderResult
from exitmgr.manager import ExitManager


CON = 1000
SCID = 2000

JOURNAL = {"ts": "2026-07-01T14:00:00+00:00", "contract_id": CON, "symbol": "AAPL",
           "right": "C", "strike": 200.0, "expiry": "20261231", "quantity": 4, "debit": 2000.0,
           "conviction": 6}

SPREAD_JOURNAL = dict(JOURNAL, debit=800.0,
                      spread={"short_con_id": SCID, "short_strike": 210.0, "width": 10.0})


def _rules():
    return RulesConfig(
        profit_target_pct=30.0,
        stop_pct=30.0,
        time_stop_days=10,
        trailing=TrailingConfig(enabled=False),
        scale_out=ScaleOutConfig(enabled=False),
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


def _wire(mgr, *, quotes, positions=None):
    """Public API contract; production-derived narrative omitted."""
    if positions is None:
        positions = {CON: PositionData(con_id=CON, symbol="AAPL", right="C",
                                       quantity=4, avg_cost=5.00, expiry="20261231")}
    mgr.ib_conn.get_positions = AsyncMock(return_value=positions)
    mgr.ib_conn.get_open_orders = AsyncMock(return_value={})
    mgr.ib_conn.fetch_quotes = AsyncMock(return_value=quotes)
    mgr.ib_conn.ib = MagicMock()
    mgr.ib_conn.ib.portfolio = lambda: []
    mgr._spot_price = AsyncMock(return_value=None)

    place = AsyncMock(return_value=OrderResult(success=True, order_id=555, con_id=CON, trade=None))
    mgr.order_manager.place_close_order = place
    return place



@pytest.mark.asyncio
async def test_single_leg_stop_passes_bid_and_trigger_type(tmp_path):
    mgr, cfg = _mgr(tmp_path)

    place = _wire(mgr, quotes={CON: {"price": 3.50, "bid": 3.40, "ask": 3.60, "mark": 3.50}})
    await mgr.run_cycle(dry_run=False)

    place.assert_called_once()
    kw = place.call_args.kwargs
    assert kw["con_id"] == CON
    assert kw["quantity"] == 4
    assert kw["spread"] is None
    assert kw["bid"] == 3.40
    assert kw["trigger_type"] == "stop"


@pytest.mark.asyncio
async def test_single_leg_profit_target_passes_bid_and_trigger_type(tmp_path):
    mgr, cfg = _mgr(tmp_path)

    place = _wire(mgr, quotes={CON: {"price": 6.50, "bid": 6.40, "ask": 6.60, "mark": 6.50}})
    await mgr.run_cycle(dry_run=False)

    kw = place.call_args.kwargs
    assert kw["bid"] == 6.40
    assert kw["trigger_type"] == "profit_target"
    assert kw["spread"] is None



@pytest.mark.asyncio
async def test_spread_close_passes_bid_none(tmp_path):
    mgr, cfg = _mgr(tmp_path, journal=SPREAD_JOURNAL)



    place = _wire(mgr, quotes={
        CON:  {"price": 5.00, "bid": 4.90, "ask": 5.10, "mark": 5.00},
        SCID: {"price": 3.80, "bid": 3.70, "ask": 3.90, "mark": 3.80},
    })
    await mgr.run_cycle(dry_run=False)

    place.assert_called_once()
    kw = place.call_args.kwargs
    assert kw["spread"] is not None
    assert kw["spread"]["short_con_id"] == SCID
    assert kw["bid"] is None
    assert kw["trigger_type"] == "stop"



@pytest.mark.asyncio
async def test_single_leg_nan_bid_passes_none(tmp_path):
    mgr, cfg = _mgr(tmp_path)
    place = _wire(mgr, quotes={CON: {"price": 3.50, "bid": float("nan"),
                                     "ask": 3.60, "mark": 3.50}})
    await mgr.run_cycle(dry_run=False)

    kw = place.call_args.kwargs
    assert kw["bid"] is None
    assert kw["trigger_type"] == "stop"


@pytest.mark.asyncio
async def test_single_leg_missing_bid_passes_none(tmp_path):
    mgr, cfg = _mgr(tmp_path)
    place = _wire(mgr, quotes={CON: {"price": 3.50}})
    await mgr.run_cycle(dry_run=False)

    kw = place.call_args.kwargs
    assert kw["bid"] is None
    assert kw["trigger_type"] == "stop"


@pytest.mark.asyncio
async def test_single_leg_nonpositive_bid_passes_none(tmp_path):
    mgr, cfg = _mgr(tmp_path)
    place = _wire(mgr, quotes={CON: {"price": 3.50, "bid": 0.0, "ask": 3.60, "mark": 3.50}})
    await mgr.run_cycle(dry_run=False)

    assert place.call_args.kwargs["bid"] is None
