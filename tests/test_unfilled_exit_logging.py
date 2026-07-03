"""Tests for NON-FILL exit logging (2026-07-03).

The v2 fill-quality report (fill_quality_report.py) reads the fill-rate as
filled / (filled + unfilled) over `kind:"trade"` rows: a row counts as UNFILLED when
close.avg_fill_price is None AND close.fill_status is a resting/cancelled IBKR status. Before
this change a TRIGGERED exit that was REJECTED/CANCELLED at placement (OrderResult.success=False)
produced NO row at all, so a too-tight exit floor read as ~100% fill-rate and the TOO_TIGHT
signal was blind.

These tests prove:
  * a REJECTED exit (success=False WITH a terminal trade) emits ONE unfilled row with the right
    fields (fill_status, trigger_mark, bid, limit_price, close_qty, rule_fired, avg_fill_price=None,
    realized P&L None) and the report counts it as unfilled;
  * an idempotency SKIP (success=False, trade=None -- a prior close still working) emits NO row
    (no double-count against the prior order's own row);
  * a NORMAL fill emits exactly ONE filled row and NO unfilled row (fill path byte-identical);
  * a RESTING exit (success=True, never fills) emits exactly ONE unfilled row (via the existing
    fill-verification path) and the new branch does NOT add a duplicate;
  * dedupe by (con_id, order_id): re-observing the SAME rejected order across cycles (order.py
    deliberately retries) emits the row only ONCE.

run_cycle is driven with the broker + order layer fully mocked (no IBKR, no orders placed); we
control the OrderResult and inspect data/trade_dataset.jsonl.
"""
import json
import os

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from exitmgr.config import Config, RulesConfig, TrailingConfig, ScaleOutConfig
from exitmgr.connection import PositionData
from exitmgr.order import OrderResult
from exitmgr.manager import ExitManager

import fill_quality_report as fqr


CON = 1000
# Single-leg: 5.00/share x 4 = $2000 debit. Far expiry so the DTE<=10 time-stop never fires.
JOURNAL = {"ts": "2026-07-01T14:00:00+00:00", "contract_id": CON, "symbol": "AAPL",
           "right": "C", "strike": 200.0, "expiry": "20261231", "quantity": 4, "debit": 2000.0,
           "conviction": 6}


def _rules():
    return RulesConfig(
        profit_target_pct=30.0,
        stop_pct=30.0,
        time_stop_days=10,
        trailing=TrailingConfig(enabled=False),
        scale_out=ScaleOutConfig(enabled=False),   # isolate: no partial-trim noise
    )


def _mgr(tmp_path, journal=JOURNAL):
    cfg = Config()
    cfg.dry_run = False
    cfg.loop_mode = False
    cfg.journal.path = str(tmp_path / "trades.log")
    cfg.state.path = str(tmp_path / "state.json")
    cfg.kill_switch.path = str(tmp_path / "KILL")   # absent -> kill switch inactive
    cfg.audit_path = str(tmp_path / "audit.jsonl")
    cfg.manage_positions = False                    # skip the LLM assessment call
    cfg.alerts_channel = ""
    cfg.error_channel = ""
    cfg.rules = _rules()
    (tmp_path / "trades.log").write_text(json.dumps(journal) + "\n")
    return ExitManager(cfg), cfg


def _wire(mgr, *, quotes, place_result):
    """Mock broker + order layer for run_cycle. `place_result` is the OrderResult (or a callable
    returning one) place_close_order yields. Returns the AsyncMock standing in for it."""
    positions = {CON: PositionData(con_id=CON, symbol="AAPL", right="C",
                                   quantity=4, avg_cost=5.00, expiry="20261231")}
    mgr.ib_conn.get_positions = AsyncMock(return_value=positions)
    mgr.ib_conn.get_open_orders = AsyncMock(return_value={})
    mgr.ib_conn.fetch_quotes = AsyncMock(return_value=quotes)
    mgr.ib_conn.ib = MagicMock()
    mgr.ib_conn.ib.portfolio = lambda: []           # no server marking -> use the quote
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
    """A trade object whose orderStatus reports a terminal (rejected) status."""
    tr = MagicMock()
    tr.orderStatus.status = status
    return tr


def _filled_trade(px=3.40):
    tr = MagicMock()
    tr.orderStatus.status = "Filled"
    tr.orderStatus.avgFillPrice = px
    return tr


def _resting_trade(status="Submitted"):
    tr = MagicMock()
    tr.orderStatus.status = status              # never becomes "Filled"
    tr.orderStatus.avgFillPrice = None
    return tr


# STOP quote: 3.50 vs 5.00 basis = -30% -> hard stop, full exit; live bid 3.40.
_STOP_QUOTES = {CON: {"price": 3.50, "bid": 3.40, "ask": 3.60, "mark": 3.50}}


# --------------------------------------------------------------- rejected -> ONE unfilled row
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

    # the report must COUNT it as unfilled (denominator no longer blind)
    closes = fqr.extract_closes(rows)
    assert len(closes) == 1
    assert closes[0]["filled"] is False
    assert closes[0]["unfilled"] is True


# --------------------------------------------------------- idempotency skip -> NO row
@pytest.mark.asyncio
async def test_idempotency_skip_emits_no_row(tmp_path):
    mgr, cfg = _mgr(tmp_path)
    # success=False with trade=None == a can_place_close skip (a prior close is still working).
    _wire(mgr, quotes=_STOP_QUOTES,
          place_result=OrderResult(success=False, order_id=None, con_id=CON, trade=None,
                                   message="con_id=1000 already has in-flight order"))
    await mgr.run_cycle(dry_run=False)

    rows = _read_dataset(cfg)
    assert [r for r in rows if r.get("kind") == "trade"] == []


# --------------------------------------------------------- normal fill -> ONE filled, NO unfilled
@pytest.mark.asyncio
async def test_normal_fill_emits_filled_not_unfilled(tmp_path):
    mgr, cfg = _mgr(tmp_path)
    _wire(mgr, quotes=_STOP_QUOTES,
          place_result=OrderResult(success=True, order_id=555, con_id=CON,
                                   trade=_filled_trade(3.40)))
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


# --------------------------------------------------------- resting -> exactly ONE unfilled row
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
    assert len(trade_rows) == 1                      # exactly one, no duplicate from the new branch
    closes = fqr.extract_closes(rows)
    assert len(closes) == 1
    assert closes[0]["filled"] is False
    assert closes[0]["unfilled"] is True             # counted in the denominator


# --------------------------------------------------------- dedupe: same order twice -> ONE row
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
    # order.py's reject path clears in-flight and retries next cycle; a persistently-rejecting
    # order (same order_id) must be logged ONCE, not once per cycle.
    _wire(mgr, quotes=_STOP_QUOTES,
          place_result=OrderResult(success=False, order_id=555, con_id=CON,
                                   trade=_dead_trade("ApiCancelled"),
                                   message="order ApiCancelled"))
    await mgr.run_cycle(dry_run=False)
    await mgr.run_cycle(dry_run=False)

    rows = _read_dataset(cfg)
    assert len([r for r in rows if r.get("kind") == "trade"]) == 1
