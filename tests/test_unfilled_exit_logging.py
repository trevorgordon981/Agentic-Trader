"""Public API contract; production-derived narrative omitted."""
import json
import os
from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from exitmgr.config import Config, RulesConfig, TrailingConfig, ScaleOutConfig
from exitmgr.connection import PositionData
from exitmgr.order import OrderResult
from exitmgr.manager import ExitManager

import fill_quality_report as fqr


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


def _wire(mgr, *, quotes, place_result):
    """Public API contract; production-derived narrative omitted."""
    positions = {CON: PositionData(con_id=CON, symbol="AAPL", right="C",
                                   quantity=4, avg_cost=5.00, expiry="20261231")}
    mgr.ib_conn.get_positions = AsyncMock(return_value=positions)
    mgr.ib_conn.get_open_orders = AsyncMock(return_value={})
    mgr.ib_conn.fetch_quotes = AsyncMock(return_value=quotes)
    mgr.ib_conn.ib = MagicMock()
    mgr.ib_conn.ib.portfolio = lambda: []
    mgr._spot_price = AsyncMock(return_value=None)
    place = AsyncMock(return_value=place_result)
    mgr.order_manager.place_close_order = place
    return place


def _read_dataset(cfg):
    ddir = os.environ.get("EXITMGR_DATASET_DIR") or os.path.join(
        os.path.dirname(cfg.journal.path) or ".", "data")
    path = os.path.join(ddir, "trade_dataset.jsonl")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]


def _dead_trade(status="Cancelled"):
    """Public API contract; production-derived narrative omitted."""
    tr = MagicMock()
    tr.orderStatus.status = status
    return tr


def _filled_trade(px=3.40):
    """Public API contract; production-derived narrative omitted."""
    return SimpleNamespace(
        contract=SimpleNamespace(conId=CON, secType="OPT", comboLegs=[]),
        order=SimpleNamespace(
            action="SELL", orderId=555, clientId=42, permId=700555,
            orderRef="exitmgr-test-normal-fill", orderType="LMT", tif="DAY",
            lmtPrice=3.50, auxPrice=None,
        ),
        orderStatus=SimpleNamespace(
            status="Filled", avgFillPrice=px, filled=4, remaining=0),
        fills=[],
    )


def _resting_trade(status="Submitted"):
    tr = MagicMock()
    tr.orderStatus.status = status
    tr.orderStatus.avgFillPrice = None
    return tr



_STOP_QUOTES = {CON: {"price": 3.50, "bid": 3.40, "ask": 3.60, "mark": 3.50}}



@pytest.mark.asyncio
async def test_rejected_exit_emits_unfilled_row(tmp_path):
    mgr, cfg = _mgr(tmp_path)
    _wire(mgr, quotes=_STOP_QUOTES,
          place_result=OrderResult(success=False, order_id=555, con_id=CON,
                                    trade=_dead_trade("Cancelled"),
                                    message="order Cancelled: rejected"))
    await mgr.run_cycle(dry_run=False)

    rows = _read_dataset(cfg)
    trade_rows = [r for r in rows if r.get("kind") == "trade"]
    assert len(trade_rows) == 1
    r = trade_rows[0]
    c = r["close"]
    assert c["fill_status"] == "Cancelled"
    assert c["avg_fill_price"] is None
    assert c["trigger_mark"] == 3.50
    assert c["bid"] == 3.40
    assert c["limit_price"] == 3.50
    assert c["close_qty"] == 4
    assert c["rule_fired"] == "stop"
    assert c["realized_pnl"] is None
    assert c["order_id"] == 555
    assert r.get("unfilled") is True


    closes = fqr.extract_closes(rows)
    assert len(closes) == 1
    assert closes[0]["filled"] is False
    assert closes[0]["unfilled"] is True



@pytest.mark.asyncio
async def test_idempotency_skip_emits_no_row(tmp_path):
    mgr, cfg = _mgr(tmp_path)

    _wire(mgr, quotes=_STOP_QUOTES,
          place_result=OrderResult(success=False, order_id=None, con_id=CON, trade=None,
                                   message="con_id=1000 already has in-flight order"))
    await mgr.run_cycle(dry_run=False)

    rows = _read_dataset(cfg)
    assert [r for r in rows if r.get("kind") == "trade"] == []



@pytest.mark.asyncio
async def test_normal_fill_emits_filled_not_unfilled(tmp_path):
    mgr, cfg = _mgr(tmp_path)
    _wire(mgr, quotes=_STOP_QUOTES,
          place_result=OrderResult(success=True, order_id=555, con_id=CON,
                                   trade=_filled_trade(3.40), perm_id=700555,
                                   client_id=42, order_ref="exitmgr-test-normal-fill"))
    with patch("exitmgr.manager.asyncio.sleep", new=AsyncMock()):
        await mgr.run_cycle(dry_run=False)

    rows = _read_dataset(cfg)
    trade_rows = [r for r in rows if r.get("kind") == "trade"]
    assert len(trade_rows) == 1
    assert trade_rows[0].get("unfilled") is not True
    assert trade_rows[0]["close"]["avg_fill_price"] == 3.40

    closes = fqr.extract_closes(rows)
    assert len(closes) == 1
    assert closes[0]["filled"] is True
    assert closes[0]["unfilled"] is False



@pytest.mark.asyncio
async def test_resting_exit_emits_one_unfilled_row(tmp_path):
    mgr, cfg = _mgr(tmp_path)
    _wire(mgr, quotes=_STOP_QUOTES,
          place_result=OrderResult(success=True, order_id=555, con_id=CON,
                                   trade=_resting_trade("Submitted")))
    with patch("exitmgr.manager.asyncio.sleep", new=AsyncMock()):
        await mgr.run_cycle(dry_run=False)

    rows = _read_dataset(cfg)
    trade_rows = [r for r in rows if r.get("kind") == "trade"]
    assert len(trade_rows) == 1
    closes = fqr.extract_closes(rows)
    assert len(closes) == 1
    assert closes[0]["filled"] is False
    assert closes[0]["unfilled"] is True



def test_dedupe_direct_helper(tmp_path):
    mgr, cfg = _mgr(tmp_path)
    trg = MagicMock()
    trg.trigger_type = "stop"
    for _ in range(2):
        mgr._log_unfilled_exit(CON, "AAPL", trg, fill_status="Cancelled", close_qty=4,
                               trigger_mark=3.50, bid=3.40, limit_price=3.50, order_id=555,
                               reason="stop", placed_at="2026-07-03T00:00:00+00:00")
    rows = _read_dataset(cfg)
    assert len([r for r in rows if r.get("kind") == "trade"]) == 1


@pytest.mark.asyncio
async def test_dedupe_across_two_reject_cycles(tmp_path):
    mgr, cfg = _mgr(tmp_path)


    _wire(mgr, quotes=_STOP_QUOTES,
          place_result=OrderResult(success=False, order_id=555, con_id=CON,
                                   trade=_dead_trade("ApiCancelled"),
                                   message="order ApiCancelled"))
    await mgr.run_cycle(dry_run=False)
    await mgr.run_cycle(dry_run=False)

    rows = _read_dataset(cfg)
    assert len([r for r in rows if r.get("kind") == "trade"]) == 1
