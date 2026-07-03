"""Tests for BID WIRING into manager.run_cycle's eval-loop place_close_order call (2026-07-03).

order.py's place_close_order gained two optional, backward-compatible params -- `bid` and
`trigger_type` -- so a TRIGGERED single-leg exit can rest a MARKETABLE LIMIT at the bid (fills on
wide books) instead of a mark-anchored limit that rests above the bid and never protects the
position. These tests prove the MANAGER now threads both values into that call correctly:

  * a single-leg triggered close passes the single-leg bid + the trigger's trigger_type through;
  * a SPREAD (combo) close passes bid=None -- a per-leg bid is NOT the net combo price, so a
    spread must NEVER be bid-anchored (order.py's _eff_bid guards this too; the manager must not
    even hand it a bid);
  * a NaN bid and a missing bid both pass bid=None -> order.py keeps its guaranteed-fill fallback.

run_cycle is driven with the broker + order layer mocked (no IBKR, no orders placed); we capture
the kwargs handed to place_close_order.
"""
import json

import pytest
from unittest.mock import AsyncMock, MagicMock

from exitmgr.config import Config, RulesConfig, TrailingConfig, ScaleOutConfig
from exitmgr.connection import PositionData
from exitmgr.order import OrderResult
from exitmgr.manager import ExitManager


CON = 1000
SCID = 2000
# Single-leg: 5.00/share x 4 = $2000 debit. Far expiry so the DTE<=10 time-stop never fires.
JOURNAL = {"ts": "2026-07-01T14:00:00+00:00", "contract_id": CON, "symbol": "AAPL",
           "right": "C", "strike": 200.0, "expiry": "20261231", "quantity": 4, "debit": 2000.0,
           "conviction": 6}
# Debit spread: NET 2.00/share x 4 = $800 debit; long CON, short SCID.
SPREAD_JOURNAL = dict(JOURNAL, debit=800.0,
                      spread={"short_con_id": SCID, "short_strike": 210.0, "width": 10.0})


def _rules():
    return RulesConfig(
        profit_target_pct=30.0,
        stop_pct=30.0,
        time_stop_days=10,
        trailing=TrailingConfig(enabled=False),
        scale_out=ScaleOutConfig(enabled=False),   # isolate: no partial-trim noise here
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


def _wire(mgr, *, quotes, positions=None):
    """Mock broker + order layer for one run_cycle. `quotes` is the con_id->quote-dict map
    fetch_quotes returns (include a 'bid' key to exercise the wiring). Returns the AsyncMock
    standing in for place_close_order (inspect .call_args.kwargs)."""
    if positions is None:
        positions = {CON: PositionData(con_id=CON, symbol="AAPL", right="C",
                                       quantity=4, avg_cost=5.00, expiry="20261231")}
    mgr.ib_conn.get_positions = AsyncMock(return_value=positions)
    mgr.ib_conn.get_open_orders = AsyncMock(return_value={})
    mgr.ib_conn.fetch_quotes = AsyncMock(return_value=quotes)
    mgr.ib_conn.ib = MagicMock()
    mgr.ib_conn.ib.portfolio = lambda: []           # no server marking -> use the quote
    mgr._spot_price = AsyncMock(return_value=None)

    place = AsyncMock(return_value=OrderResult(success=True, order_id=555, con_id=CON, trade=None))
    mgr.order_manager.place_close_order = place
    return place


# ---------------------------------------------------------- single-leg: bid + trigger_type passed
@pytest.mark.asyncio
async def test_single_leg_stop_passes_bid_and_trigger_type(tmp_path):
    mgr, cfg = _mgr(tmp_path)
    # -30% (3.50 vs 5.00 basis) -> hard STOP, full exit; live bid 3.40 on the leg.
    place = _wire(mgr, quotes={CON: {"price": 3.50, "bid": 3.40, "ask": 3.60, "mark": 3.50}})
    await mgr.run_cycle(dry_run=False)

    place.assert_called_once()
    kw = place.call_args.kwargs
    assert kw["con_id"] == CON
    assert kw["quantity"] == 4                        # full close (scale-out disabled)
    assert kw["spread"] is None                       # single leg
    assert kw["bid"] == 3.40                          # the single-leg bid threaded through
    assert kw["trigger_type"] == "stop"               # from trigger.trigger_type


@pytest.mark.asyncio
async def test_single_leg_profit_target_passes_bid_and_trigger_type(tmp_path):
    mgr, cfg = _mgr(tmp_path)
    # +30% (6.50 vs 5.00) -> profit_target; live bid 6.40.
    place = _wire(mgr, quotes={CON: {"price": 6.50, "bid": 6.40, "ask": 6.60, "mark": 6.50}})
    await mgr.run_cycle(dry_run=False)

    kw = place.call_args.kwargs
    assert kw["bid"] == 6.40
    assert kw["trigger_type"] == "profit_target"
    assert kw["spread"] is None


# ------------------------------------------------------------------ spread: bid MUST be None
@pytest.mark.asyncio
async def test_spread_close_passes_bid_none(tmp_path):
    mgr, cfg = _mgr(tmp_path, journal=SPREAD_JOURNAL)
    # NET mark 5.00 - 3.80 = 1.20 vs 2.00 basis = -40% -> stop (well past the -30 threshold,
    # avoids a float-boundary miss). Both legs carry a live bid, but a per-leg bid is NOT the
    # net combo price -> the manager must pass bid=None.
    place = _wire(mgr, quotes={
        CON:  {"price": 5.00, "bid": 4.90, "ask": 5.10, "mark": 5.00},
        SCID: {"price": 3.80, "bid": 3.70, "ask": 3.90, "mark": 3.80},
    })
    await mgr.run_cycle(dry_run=False)

    place.assert_called_once()
    kw = place.call_args.kwargs
    assert kw["spread"] is not None
    assert kw["spread"]["short_con_id"] == SCID       # it really is the combo close path
    assert kw["bid"] is None                          # NEVER bid-anchor a combo
    assert kw["trigger_type"] == "stop"


# ------------------------------------------------------------------ NaN / missing bid -> None
@pytest.mark.asyncio
async def test_single_leg_nan_bid_passes_none(tmp_path):
    mgr, cfg = _mgr(tmp_path)
    place = _wire(mgr, quotes={CON: {"price": 3.50, "bid": float("nan"),
                                     "ask": 3.60, "mark": 3.50}})
    await mgr.run_cycle(dry_run=False)

    kw = place.call_args.kwargs
    assert kw["bid"] is None                          # NaN guarded -> None
    assert kw["trigger_type"] == "stop"


@pytest.mark.asyncio
async def test_single_leg_missing_bid_passes_none(tmp_path):
    mgr, cfg = _mgr(tmp_path)
    place = _wire(mgr, quotes={CON: {"price": 3.50}})   # no 'bid' key at all
    await mgr.run_cycle(dry_run=False)

    kw = place.call_args.kwargs
    assert kw["bid"] is None
    assert kw["trigger_type"] == "stop"


@pytest.mark.asyncio
async def test_single_leg_nonpositive_bid_passes_none(tmp_path):
    mgr, cfg = _mgr(tmp_path)
    place = _wire(mgr, quotes={CON: {"price": 3.50, "bid": 0.0, "ask": 3.60, "mark": 3.50}})
    await mgr.run_cycle(dry_run=False)

    assert place.call_args.kwargs["bid"] is None        # bid<=0 (stub) guarded -> None
