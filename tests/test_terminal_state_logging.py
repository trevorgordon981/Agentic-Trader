"""Terminal-state closed-trade logging (2026-07-03).

Two whole classes of trade ENDING used to be invisible to the fine-tuning dataset:

  1. TOOL-CLOSES -- close_symbol.py / liquidate.py (clientId 91) flatten a position; the
     journal-drop path dropped the ENTIRE entry+mark-path arc with NO exits.log / dataset row.
  2. EXPIRIES    -- a position that expires worthless / auto-exercises just vanished from live
     positions with no close record at all.

Both now emit ONE complete closed-trade row via the same _log_exit machinery a placed exit uses
(full entry snapshot + accumulated mark path/MFE/MAE + close block + labels), honoring the H3
realized-vs-mark convention and NEVER fabricating a price/P&L. These tests place no orders and
touch no broker (ib_async is mocked by conftest). They also prove the manager dataset-dir env
override (EXITMGR_DATASET_DIR) now routes exit-path writes to tmp.
"""
import json
import os
import types

import pytest
from unittest.mock import AsyncMock

from exitmgr.config import Config
from exitmgr.connection import PositionData
from exitmgr.manager import ExitManager


# --------------------------------------------------------------------------- helpers
def _mgr(tmp_path, journal_lines):
    cfg = Config()
    cfg.dry_run = True
    cfg.loop_mode = False
    cfg.journal.path = str(tmp_path / "trades.log")
    cfg.state.path = str(tmp_path / "state.json")
    cfg.kill_switch.path = str(tmp_path / "KILL")
    cfg.audit_path = str(tmp_path / "audit.jsonl")
    (tmp_path / "trades.log").write_text(
        "".join(json.dumps(x) + "\n" for x in journal_lines))
    return ExitManager(cfg), cfg


def _read_dataset(cfg):
    ddir = os.environ.get("EXITMGR_DATASET_DIR") or os.path.join(
        os.path.dirname(cfg.journal.path) or ".", "data")
    path = os.path.join(ddir, "trade_dataset.jsonl")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]


def _append_marker(cfg, marker):
    with open(cfg.journal.path, "a") as f:
        f.write(json.dumps(marker) + "\n")


# ===========================================================================================
# TOOL-CLOSES
# ===========================================================================================
def test_tool_close_single_emits_full_row_with_realized(tmp_path):
    """A close-tool flatten of a journaled single emits ONE full closed-trade row: entry snapshot
    + accumulated mark path + a close block with real realized P&L (from the marker fill)."""
    entry = {"ts": "2026-07-01T14:00:00+00:00", "contract_id": 700, "symbol": "RKLB",
             "right": "C", "strike": 20.0, "expiry": "20260918", "quantity": 2, "debit": 800.0,
             "conviction": 6, "thesis": "post-earnings breakout"}
    mgr, cfg = _mgr(tmp_path, [entry])
    assert _read_dataset(cfg) == []                       # no marker yet -> nothing emitted
    # marks accumulate exactly as run_cycle would each cycle (qty 2, basis $800 -> BE px 4.00)
    for ts, px in [("t0", 4.00), ("t1", 5.50), ("t2", 5.00)]:   # peak 5.50 -> +37.5%
        mgr.state_manager.state.record_mark(700, px, 800.0, 2, ts=ts)
    # the tool closes the position and journals an enriched marker; next _load_journal emits.
    _append_marker(cfg, {"contract_id": 700, "symbol": "RKLB", "event": "closed_by_tool",
                         "status": "Filled", "avg_fill_price": 5.00, "tool": "close_symbol",
                         "client_id": 91})
    mgr._load_journal()

    rows = _read_dataset(cfg)
    assert len(rows) == 1
    r = rows[0]
    assert r["kind"] == "trade" and r["con_id"] == 700 and r["symbol"] == "RKLB"
    # entry arc preserved (was previously orphaned)
    assert r["entry"]["debit"] == 800.0 and r["entry"]["thesis"].startswith("post-earnings")
    # full mark path carried
    assert r["lifecycle"]["marks"] == 3
    assert r["lifecycle"]["mfe_pct"] == pytest.approx(37.5)
    # close: realized from the tool fill -> proceeds 5.00*100*2=1000, realized 1000-800=200 (+25%)
    c = r["close"]
    assert c["reason"] == "closed_by_tool" and c["exit_event"] == "closed_by_tool"
    assert c["fill_status"] == "Filled"
    assert c["realized_pnl"] == pytest.approx(200.0)
    assert c["realized_pnl_pct"] == pytest.approx(25.0)
    assert c["close_client_id"] == 91
    assert r["labels"]["outcome"] == "win"
    # the journal-drop still happened (trader won't re-close it)
    assert 700 not in mgr._journal_entries


def test_tool_close_dedupes_across_reload(tmp_path):
    """Re-observing the same marker across cycles (each cycle re-reads trades.log) emits ONE row."""
    entry = {"ts": "2026-07-01T14:00:00+00:00", "contract_id": 700, "symbol": "RKLB",
             "right": "C", "strike": 20.0, "expiry": "20260918", "quantity": 1, "debit": 400.0}
    mgr, cfg = _mgr(tmp_path, [entry])
    _append_marker(cfg, {"contract_id": 700, "symbol": "RKLB", "event": "closed_by_tool",
                         "status": "Filled", "avg_fill_price": 5.00, "tool": "close_symbol",
                         "client_id": 91})
    mgr._load_journal()
    mgr._load_journal()
    mgr._load_journal()
    assert len(_read_dataset(cfg)) == 1


def test_tool_close_null_realized_without_price(tmp_path):
    """No fill price on the marker -> realized P&L is NULL (never fabricated), reason recorded."""
    entry = {"ts": "2026-07-01T14:00:00+00:00", "contract_id": 701, "symbol": "MU",
             "right": "C", "strike": 100.0, "expiry": "20260918", "quantity": 1, "debit": 500.0}
    mgr, cfg = _mgr(tmp_path, [entry])
    _append_marker(cfg, {"contract_id": 701, "symbol": "MU", "event": "closed_by_tool",
                         "status": "Filled", "avg_fill_price": None, "tool": "close_symbol",
                         "client_id": 91})
    mgr._load_journal()
    r = _read_dataset(cfg)[0]
    assert r["close"]["reason"] == "closed_by_tool"
    assert r["close"]["realized_pnl"] is None
    assert r["close"]["realized_pnl_pct"] is None
    assert r["close"]["realized_unknown_reason"] == "tool_close_fill_unknown"
    assert r["labels"]["outcome"] is None                 # no realized -> no label


def test_liquidate_tool_sets_liquidated_reason(tmp_path):
    """The liquidate.py marker (tool='liquidate') tags exit_reason='liquidated'."""
    entry = {"ts": "2026-07-01T14:00:00+00:00", "contract_id": 702, "symbol": "AA",
             "right": "C", "strike": 40.0, "expiry": "20260918", "quantity": 1, "debit": 300.0}
    mgr, cfg = _mgr(tmp_path, [entry])
    _append_marker(cfg, {"contract_id": 702, "symbol": "AA", "event": "closed_by_tool",
                         "status": "Filled", "avg_fill_price": 4.00, "tool": "liquidate",
                         "client_id": 91})
    mgr._load_journal()
    r = _read_dataset(cfg)[0]
    assert r["close"]["reason"] == "liquidated" and r["close"]["exit_event"] == "liquidated"
    assert r["close"]["realized_pnl"] == pytest.approx(100.0)   # 400-300


def test_tool_close_spread_nets_both_legs(tmp_path):
    """A spread tool-close nets the long-leg SALE and the short-leg BUY-to-close from both legs'
    per-leg markers -> one row with correct NET realized P&L."""
    entry = {"ts": "2026-07-01T14:00:00+00:00", "contract_id": 800, "symbol": "NOK",
             "right": "C", "strike": 14.0, "expiry": "20260918", "quantity": 1, "debit": 150.0,
             "spread": {"short_con_id": 801, "short_strike": 25.0, "width": 11.0}}
    mgr, cfg = _mgr(tmp_path, [entry])
    mgr.state_manager.state.record_mark(800, 1.50, 150.0, 1, ts="t0",
                                        enrich={"is_net_spread": True})
    # both legs journal a marker (the tool closes each leg)
    _append_marker(cfg, {"contract_id": 800, "symbol": "NOK", "event": "closed_by_tool",
                         "status": "Filled", "avg_fill_price": 2.00, "tool": "liquidate",
                         "client_id": 91})
    _append_marker(cfg, {"contract_id": 801, "symbol": "NOK", "event": "closed_by_tool",
                         "status": "Filled", "avg_fill_price": 0.40, "tool": "liquidate",
                         "client_id": 91})
    mgr._load_journal()
    rows = _read_dataset(cfg)
    assert len(rows) == 1                                  # ONE row for the spread arc
    r = rows[0]
    assert r["entry"]["structure"] == "spread"
    assert r["entry"]["spread"]["short_con_id"] == 801
    # net exit px = 2.00 - 0.40 = 1.60 -> proceeds 160 -> realized 160 - 150 = 10
    assert r["close"]["realized_pnl"] == pytest.approx(10.0)
    assert r["close"]["reason"] == "liquidated"


def test_tool_close_spread_null_realized_when_short_fill_unknown(tmp_path):
    """Missing short-leg fill -> the net cannot be valued -> realized NULL with a reason."""
    entry = {"ts": "2026-07-01T14:00:00+00:00", "contract_id": 810, "symbol": "NOK",
             "right": "C", "strike": 14.0, "expiry": "20260918", "quantity": 1, "debit": 150.0,
             "spread": {"short_con_id": 811, "short_strike": 25.0, "width": 11.0}}
    mgr, cfg = _mgr(tmp_path, [entry])
    _append_marker(cfg, {"contract_id": 810, "symbol": "NOK", "event": "closed_by_tool",
                         "status": "Filled", "avg_fill_price": 2.00, "tool": "liquidate",
                         "client_id": 91})
    _append_marker(cfg, {"contract_id": 811, "symbol": "NOK", "event": "closed_by_tool",
                         "status": "Filled", "avg_fill_price": None, "tool": "liquidate",
                         "client_id": 91})
    mgr._load_journal()
    r = _read_dataset(cfg)[0]
    assert r["close"]["realized_pnl"] is None
    assert r["close"]["realized_unknown_reason"] == "spread_net_fill_unknown"


# ===========================================================================================
# EXPIRIES
# ===========================================================================================
def test_expiry_otm_worthless_is_minus_100pct(tmp_path):
    """An OTM long call expiring worthless -> realized = -100% of debit (a real, definitional
    outcome -- NOT nulled by the H3 non-fill convention)."""
    entry = {"ts": "2026-06-01T14:00:00+00:00", "contract_id": 900, "symbol": "SPY",
             "right": "C", "strike": 650.0, "expiry": "20260601", "quantity": 1, "debit": 500.0}
    mgr, cfg = _mgr(tmp_path, [entry])
    mgr.state_manager.state.record_mark(900, 5.0, 500.0, 1, ts="t0")
    # spot 600 < strike 650 -> OTM -> intrinsic 0
    mgr._emit_expiry_close(900, mgr._journal_entries[900], spot=600.0)
    r = _read_dataset(cfg)[0]
    assert r["close"]["reason"] == "expired" and r["close"]["exit_event"] == "expired"
    assert r["close"]["realized_pnl"] == pytest.approx(-500.0)
    assert r["close"]["realized_pnl_pct"] == pytest.approx(-100.0)
    assert r["close"]["dte_at_close"] == 0
    assert r["labels"]["outcome"] == "loss" and r["labels"]["win"] is False
    assert r["lifecycle"]["marks"] == 1                    # final mark path retained


def test_expiry_itm_uses_intrinsic_value(tmp_path):
    """An ITM long call auto-exercised -> realized from intrinsic value at the settlement spot."""
    entry = {"ts": "2026-06-01T14:00:00+00:00", "contract_id": 910, "symbol": "MU",
             "right": "C", "strike": 100.0, "expiry": "20260601", "quantity": 1, "debit": 200.0}
    mgr, cfg = _mgr(tmp_path, [entry])
    # spot 105 > strike 100 -> intrinsic 5.00 -> proceeds 500 -> realized 500-200 = 300 (+150%)
    mgr._emit_expiry_close(910, mgr._journal_entries[910], spot=105.0)
    r = _read_dataset(cfg)[0]
    assert r["close"]["realized_pnl"] == pytest.approx(300.0)
    assert r["close"]["realized_pnl_pct"] == pytest.approx(150.0)
    assert r["labels"]["outcome"] == "win"


def test_expiry_value_unknown_without_spot_is_flagged_not_assumed(tmp_path):
    """No settlement spot -> the value is NOT assumed worthless: realized NULL + a clear flag."""
    entry = {"ts": "2026-06-01T14:00:00+00:00", "contract_id": 920, "symbol": "AA",
             "right": "C", "strike": 40.0, "expiry": "20260601", "quantity": 1, "debit": 300.0}
    mgr, cfg = _mgr(tmp_path, [entry])
    mgr._emit_expiry_close(920, mgr._journal_entries[920], spot=None)
    r = _read_dataset(cfg)[0]
    assert r["close"]["reason"] == "expired"
    assert r["close"]["realized_pnl"] is None
    assert r["close"]["realized_pnl_pct"] is None
    assert r["close"]["expiry_value_unknown"] is True
    assert r["close"]["realized_unknown_reason"] == "expiry_value_unknown"


@pytest.mark.asyncio
async def test_process_expiries_detects_and_dedupes_across_cycles(tmp_path):
    """End-to-end detection: a journaled position past expiry AND gone from live positions emits
    exactly ONE 'expired' row, and re-running the check next cycle does NOT double-log."""
    entry = {"ts": "2026-06-01T14:00:00+00:00", "contract_id": 950, "symbol": "SPY",
             "right": "C", "strike": 650.0, "expiry": "20260601", "quantity": 1, "debit": 500.0}
    mgr, cfg = _mgr(tmp_path, [entry])
    mgr._spot_price = AsyncMock(return_value=600.0)        # OTM -> worthless
    await mgr._process_expiries({})                        # no live positions
    await mgr._process_expiries({})                        # next cycle: must NOT re-emit
    rows = [r for r in _read_dataset(cfg) if r["close"]["reason"] == "expired"]
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_process_expiries_skips_still_live_and_not_yet_expired(tmp_path):
    """No expiry row for a position that is still live, nor for one not yet past expiry."""
    live = {"ts": "2026-06-01T14:00:00+00:00", "contract_id": 960, "symbol": "SPY",
            "right": "C", "strike": 650.0, "expiry": "20260601", "quantity": 1, "debit": 500.0}
    future = {"ts": "2026-07-01T14:00:00+00:00", "contract_id": 961, "symbol": "MU",
              "right": "C", "strike": 100.0, "expiry": "20261231", "quantity": 1, "debit": 500.0}
    mgr, cfg = _mgr(tmp_path, [live, future])
    mgr._spot_price = AsyncMock(return_value=600.0)
    # 960 is past expiry but STILL LIVE; 961 is gone but NOT yet expired -> neither emits
    await mgr._process_expiries({960: PositionData(con_id=960, symbol="SPY", right="C",
                                                   quantity=1, avg_cost=5.0, expiry="20260601")})
    assert [r for r in _read_dataset(cfg) if r["close"]["reason"] == "expired"] == []


# ===========================================================================================
# DATASET-DIR ENV FOLD-IN
# ===========================================================================================
def test_manager_dataset_dir_env_override_routes_exit_writes_to_tmp(tmp_path, monkeypatch):
    """The EXITMGR_DATASET_DIR override now reaches the EXIT-MANAGER's own dataset writes (fold-in):
    a logged exit lands in the override dir, NOT in data/ next to the journal."""
    override = tmp_path / "override_ds"
    override.mkdir()
    monkeypatch.setenv("EXITMGR_DATASET_DIR", str(override))
    entry = {"ts": "2026-07-01T14:00:00+00:00", "contract_id": 970, "symbol": "SPY",
             "right": "C", "strike": 610.0, "expiry": "20260918", "quantity": 1, "debit": 800.0}
    mgr, cfg = _mgr(tmp_path, [entry])
    assert mgr._dataset_dir() == str(override)
    mgr.state_manager.state.record_mark(970, 8.0, 800.0, 1, ts="t0")
    trig = types.SimpleNamespace(trigger_type="stop", pnl_pct=-10.0, message="stop")
    mgr._log_exit(970, "SPY", trig, exit_price_per_share=7.2, quantity=1, reason="stop")
    # write landed in the override dir...
    assert os.path.exists(str(override / "trade_dataset.jsonl"))
    # ...and NOT in data/ next to the journal (the pre-fold-in location)
    assert not os.path.exists(
        os.path.join(os.path.dirname(cfg.journal.path), "data", "trade_dataset.jsonl"))
