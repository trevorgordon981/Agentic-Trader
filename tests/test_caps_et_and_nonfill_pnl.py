"""Public API contract; production-derived narrative omitted."""
import json
import os
import types

import pytest

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from exitmgr.config import Config
from exitmgr.manager import ExitManager
from exitmgr.order import _trading_day as order_trading_day
from exitmgr.manager import _trading_day as manager_trading_day

ET = ZoneInfo("America/New_York")



def _mgr(tmp_path, journal_lines=()):
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


def _read_exits(cfg):
    path = os.path.join(os.path.dirname(cfg.journal.path) or ".", "exits.log")
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]


def _read_dataset(cfg):
    ddir = os.environ.get("EXITMGR_DATASET_DIR") or os.path.join(
        os.path.dirname(cfg.journal.path) or ".", "data")
    path = os.path.join(ddir, "trade_dataset.jsonl")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]



def test_h1_manager_reads_same_canonical_helper_as_order():
    """Public API contract; production-derived narrative omitted."""
    assert manager_trading_day is order_trading_day
    for inst in (
        datetime(2026, 7, 7, 1, 30, tzinfo=timezone.utc),
        datetime(2026, 7, 6, 13, 45, tzinfo=timezone.utc),
    ):
        assert manager_trading_day(now=inst) == order_trading_day(now=inst)


def test_h1_check_caps_binds_at_evening_et(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    evening_utc = datetime(2026, 7, 7, 1, 30, tzinfo=timezone.utc)
    et_key = order_trading_day(now=evening_utc)
    utc_naive_key = evening_utc.strftime("%Y-%m-%d")
    assert et_key == "2026-07-06" and utc_naive_key == "2026-07-07"

    mgr, cfg = _mgr(tmp_path)
    cfg.caps.max_orders_per_day = 5
    cfg.caps.max_notional_per_day = 1_000_000.0


    for _ in range(cfg.caps.max_orders_per_day):
        mgr.state_manager.state.update_daily_stats(et_key, order_count=1, notional=1000.0)

    assert mgr.state_manager.state.daily_stats.get(utc_naive_key) is None


    monkeypatch.setattr("exitmgr.manager._trading_day",
                        lambda: order_trading_day(now=evening_utc))
    can_proceed, reason = mgr._check_caps(dry_run=True)
    assert can_proceed is False
    assert "order cap" in reason.lower()


def test_h1_notional_cap_also_uses_et_key(tmp_path, monkeypatch):
    evening_utc = datetime(2026, 7, 7, 3, 15, tzinfo=timezone.utc)
    et_key = order_trading_day(now=evening_utc)
    mgr, cfg = _mgr(tmp_path)
    cfg.caps.max_orders_per_day = 10_000
    cfg.caps.max_notional_per_day = 5_000.0
    mgr.state_manager.state.update_daily_stats(et_key, order_count=1, notional=5_000.0)
    monkeypatch.setattr("exitmgr.manager._trading_day",
                        lambda: order_trading_day(now=evening_utc))
    can_proceed, reason = mgr._check_caps(dry_run=True)
    assert can_proceed is False
    assert "notional cap" in reason.lower()



_JE = {"ts": "2026-06-20T16:00:00+00:00", "contract_id": 111, "symbol": "SPY",
       "right": "C", "strike": 610.0, "quantity": 1, "debit": 120.0, "conviction": 7,
       "profit_target_pct": 40.0, "stop_pct": 30.0}


def _trig():
    return types.SimpleNamespace(trigger_type="profit_target", pnl_pct=50.0, message="tp")


def test_h3_resting_exit_logs_null_realized_and_mark_estimate(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr, cfg = _mgr(tmp_path, [_JE])

    mgr._log_exit(111, "SPY", _trig(), exit_price_per_share=1.80, quantity=1,
                  reason=mgr._exit_reason(_trig()),
                  extra={"order_id": 9, "fill_status": "Submitted", "avg_fill_price": None,
                         "trigger_mark": 1.80})

    r = _read_exits(cfg)[0]

    assert r["realized_pnl"] is None
    assert r["realizedPNL"] is None
    assert r["realized_pnl_pct"] is None

    assert r["mark_estimate_pnl"] == 60.0
    assert r["mark_estimate_pnl_pct"] == 50.0

    assert r["fill_status"] == "Submitted"
    assert r["avg_fill_price"] is None


    ds = [x for x in _read_dataset(cfg) if x.get("kind") == "trade"]
    assert len(ds) == 1
    c = ds[0]["close"]
    labels = ds[0]["labels"]
    assert c["realized_pnl"] is None and c["realized_pnl_pct"] is None
    assert c["mark_estimate_pnl_pct"] == 50.0
    assert c["tp_hit"] is None and c["sl_hit"] is None
    assert labels["outcome"] is None and labels["win"] is None


def test_h3_filled_exit_realized_pnl_unchanged(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr, cfg = _mgr(tmp_path, [_JE])
    mgr._log_exit(111, "SPY", _trig(), exit_price_per_share=1.80, quantity=1,
                  reason=mgr._exit_reason(_trig()),
                  extra={"order_id": 9, "fill_status": "Filled", "avg_fill_price": 1.80,
                         "trigger_mark": 1.80})
    r = _read_exits(cfg)[0]
    assert r["realized_pnl"] == 60.0 and r["realizedPNL"] == 60.0
    assert r["realized_pnl_pct"] == 50.0

    assert r.get("mark_estimate_pnl_pct") is None
    assert r["fill_status"] == "Filled"

    ds = [x for x in _read_dataset(cfg) if x.get("kind") == "trade"][0]
    assert ds["close"]["realized_pnl_pct"] == 50.0
    assert ds["close"]["tp_hit"] is True
    assert ds["labels"]["outcome"] == "win"


def test_h3_no_fill_status_caller_unchanged(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr, cfg = _mgr(tmp_path, [_JE])
    mgr._log_exit(111, "SPY", _trig(), exit_price_per_share=1.80, quantity=1,
                  reason=mgr._exit_reason(_trig()), extra={"order_id": 9})
    r = _read_exits(cfg)[0]
    assert r["realized_pnl"] == 60.0 and r["realized_pnl_pct"] == 50.0
    assert r.get("mark_estimate_pnl_pct") is None


def test_h3_phantom_row_skipped_by_realized_present_filter(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr, cfg = _mgr(tmp_path, [_JE])

    mgr._log_exit(111, "SPY", _trig(), exit_price_per_share=1.80, quantity=1,
                  reason=mgr._exit_reason(_trig()),
                  extra={"order_id": 9, "fill_status": "Submitted", "avg_fill_price": None})

    mgr._log_exit(111, "SPY", _trig(), exit_price_per_share=1.80, quantity=1,
                  reason=mgr._exit_reason(_trig()),
                  extra={"order_id": 10, "fill_status": "Filled", "avg_fill_price": 1.80})

    rows = _read_exits(cfg)
    assert len(rows) == 2
    qualified = [r for r in rows if r.get("realized_pnl_pct") is not None]
    assert len(qualified) == 1
    assert qualified[0]["fill_status"] == "Filled"


    try:
        from calibrate_conviction_sizing import load_closed_trades
    except Exception:
        return
    _ddir = os.environ.get("EXITMGR_DATASET_DIR") or os.path.join(
        os.path.dirname(cfg.journal.path), "data")
    ds_path = os.path.join(_ddir, "trade_dataset.jsonl")
    closed = load_closed_trades(ds_path)


    assert closed == []
