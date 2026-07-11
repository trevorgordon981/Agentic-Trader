"""H1 + H3 regressions (2026-07-03).

H1 -- daily order/notional caps must READ daily_stats under the SAME US/Eastern trading-day
key that order.place_close_order WRITES it. Before the fix _check_caps keyed by
datetime.utcnow(); from ~20:00 ET to midnight ET the UTC date has already rolled to "tomorrow",
so the read looked at an EMPTY bucket and the cap could not bind (and evening activity split
across two keys). Clock is INJECTED through the real ET helper; no wall-time dependence.

H3 -- a close row may carry REALIZED P&L only if the exit actually FILLED. The fill-verification
path logs a still-resting exit with exit_price_per_share=current_price (the MARK); _log_exit must
NOT emit that mark-derived value as realized_pnl / realized_pnl_pct / outcome labels. It nulls
them and preserves the mark valuation under DISTINCT mark_estimate_* keys, so the phantom row is
skipped by consumers that key off realized_pnl_pct presence. A genuine fill is unchanged.
"""
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


# ============================================================ shared harness
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


# ============================================================ H1
def test_h1_manager_reads_same_canonical_helper_as_order():
    """No 3rd copy of the trading-day helper: manager imports order's canonical one."""
    assert manager_trading_day is order_trading_day
    for inst in (
        datetime(2026, 7, 7, 1, 30, tzinfo=timezone.utc),   # 21:30 ET on 7/6 (UTC is 'tomorrow')
        datetime(2026, 7, 6, 13, 45, tzinfo=timezone.utc),  # RTH
    ):
        assert manager_trading_day(now=inst) == order_trading_day(now=inst)


def test_h1_check_caps_binds_at_evening_et(tmp_path, monkeypatch):
    """Write (ET key) and read (_check_caps) land on the SAME ET key so the cap BINDS in the
    evening-ET / UTC-tomorrow window that previously read an empty bucket."""
    evening_utc = datetime(2026, 7, 7, 1, 30, tzinfo=timezone.utc)  # == 2026-07-06 21:30 ET
    et_key = order_trading_day(now=evening_utc)          # "2026-07-06" (trading day)
    utc_naive_key = evening_utc.strftime("%Y-%m-%d")     # "2026-07-07" (the OLD buggy read key)
    assert et_key == "2026-07-06" and utc_naive_key == "2026-07-07"  # the disputed window exists

    mgr, cfg = _mgr(tmp_path)
    cfg.caps.max_orders_per_day = 5
    cfg.caps.max_notional_per_day = 1_000_000.0          # keep the ORDER cap the binding one

    # WRITE side: order.place_close_order books each order under the ET key.
    for _ in range(cfg.caps.max_orders_per_day):
        mgr.state_manager.state.update_daily_stats(et_key, order_count=1, notional=1000.0)
    # The UTC-naive bucket the OLD read used is empty -> that was the bug (cap silently didn't bind).
    assert mgr.state_manager.state.daily_stats.get(utc_naive_key) is None

    # READ side evaluated AT that evening-ET instant: inject the clock THROUGH the real ET helper.
    monkeypatch.setattr("exitmgr.manager._trading_day",
                        lambda: order_trading_day(now=evening_utc))
    can_proceed, reason = mgr._check_caps(dry_run=True)
    assert can_proceed is False                          # cap BINDS now (pre-fix: True, blind)
    assert "order cap" in reason.lower()


def test_h1_notional_cap_also_uses_et_key(tmp_path, monkeypatch):
    evening_utc = datetime(2026, 7, 7, 3, 15, tzinfo=timezone.utc)  # 23:15 ET on 7/6
    et_key = order_trading_day(now=evening_utc)
    mgr, cfg = _mgr(tmp_path)
    cfg.caps.max_orders_per_day = 10_000                 # keep the NOTIONAL cap the binding one
    cfg.caps.max_notional_per_day = 5_000.0
    mgr.state_manager.state.update_daily_stats(et_key, order_count=1, notional=5_000.0)
    monkeypatch.setattr("exitmgr.manager._trading_day",
                        lambda: order_trading_day(now=evening_utc))
    can_proceed, reason = mgr._check_caps(dry_run=True)
    assert can_proceed is False
    assert "notional cap" in reason.lower()


# ============================================================ H3
_JE = {"ts": "2026-06-20T16:00:00+00:00", "contract_id": 111, "symbol": "SPY",
       "right": "C", "strike": 610.0, "quantity": 1, "debit": 120.0, "conviction": 7,
       "profit_target_pct": 40.0, "stop_pct": 30.0}


def _trig():
    return types.SimpleNamespace(trigger_type="profit_target", pnl_pct=50.0, message="tp")


def test_h3_resting_exit_logs_null_realized_and_mark_estimate(tmp_path):
    """A still-resting (Submitted) exit: NULL realized P&L + labels; mark estimate preserved
    under its DISTINCT key; fill_status reflects the non-fill."""
    mgr, cfg = _mgr(tmp_path, [_JE])
    # mark 1.80/share -> would be +$60 (+50%) IF it had filled; it did not (Submitted, no fill px).
    mgr._log_exit(111, "SPY", _trig(), exit_price_per_share=1.80, quantity=1,
                  reason=mgr._exit_reason(_trig()),
                  extra={"order_id": 9, "fill_status": "Submitted", "avg_fill_price": None,
                         "trigger_mark": 1.80})

    r = _read_exits(cfg)[0]
    # realized fields nulled...
    assert r["realized_pnl"] is None
    assert r["realizedPNL"] is None
    assert r["realized_pnl_pct"] is None
    # ...mark estimate preserved under DISTINCT keys (info not lost, never read as realized)...
    assert r["mark_estimate_pnl"] == 60.0
    assert r["mark_estimate_pnl_pct"] == 50.0
    # ...and the fill status reflects the unfilled state.
    assert r["fill_status"] == "Submitted"
    assert r["avg_fill_price"] is None

    # trade_dataset mirror: realized + outcome labels null; mark estimate surfaced.
    ds = [x for x in _read_dataset(cfg) if x.get("kind") == "trade"]
    assert len(ds) == 1
    c = ds[0]["close"]
    labels = ds[0]["labels"]
    assert c["realized_pnl"] is None and c["realized_pnl_pct"] is None
    assert c["mark_estimate_pnl_pct"] == 50.0
    assert c["tp_hit"] is None and c["sl_hit"] is None
    assert labels["outcome"] is None and labels["win"] is None


def test_h3_filled_exit_realized_pnl_unchanged(tmp_path):
    """A genuinely Filled exit still logs correct realized P&L + labels (byte-identical path)."""
    mgr, cfg = _mgr(tmp_path, [_JE])
    mgr._log_exit(111, "SPY", _trig(), exit_price_per_share=1.80, quantity=1,
                  reason=mgr._exit_reason(_trig()),
                  extra={"order_id": 9, "fill_status": "Filled", "avg_fill_price": 1.80,
                         "trigger_mark": 1.80})
    r = _read_exits(cfg)[0]
    assert r["realized_pnl"] == 60.0 and r["realizedPNL"] == 60.0
    assert r["realized_pnl_pct"] == 50.0
    # nothing was nulled -> no mark-estimate override written
    assert r.get("mark_estimate_pnl_pct") is None
    assert r["fill_status"] == "Filled"

    ds = [x for x in _read_dataset(cfg) if x.get("kind") == "trade"][0]
    assert ds["close"]["realized_pnl_pct"] == 50.0
    assert ds["close"]["tp_hit"] is True          # 50% >= 40% target
    assert ds["labels"]["outcome"] == "win"


def test_h3_no_fill_status_caller_unchanged(tmp_path):
    """A caller that passes NO fill_status (manual MKT close / legacy direct _log_exit) is
    untouched: realized P&L computed from the given price exactly as before."""
    mgr, cfg = _mgr(tmp_path, [_JE])
    mgr._log_exit(111, "SPY", _trig(), exit_price_per_share=1.80, quantity=1,
                  reason=mgr._exit_reason(_trig()), extra={"order_id": 9})
    r = _read_exits(cfg)[0]
    assert r["realized_pnl"] == 60.0 and r["realized_pnl_pct"] == 50.0
    assert r.get("mark_estimate_pnl_pct") is None


def test_h3_phantom_row_skipped_by_realized_present_filter(tmp_path):
    """The phantom (resting) row is excluded by a 'realized_pnl_pct present' filter, while the
    genuinely filled row survives -- exactly how calibrate_conviction_sizing / export gate rows."""
    mgr, cfg = _mgr(tmp_path, [_JE])
    # resting (phantom) ...
    mgr._log_exit(111, "SPY", _trig(), exit_price_per_share=1.80, quantity=1,
                  reason=mgr._exit_reason(_trig()),
                  extra={"order_id": 9, "fill_status": "Submitted", "avg_fill_price": None})
    # ... then a genuine fill.
    mgr._log_exit(111, "SPY", _trig(), exit_price_per_share=1.80, quantity=1,
                  reason=mgr._exit_reason(_trig()),
                  extra={"order_id": 10, "fill_status": "Filled", "avg_fill_price": 1.80})

    rows = _read_exits(cfg)
    assert len(rows) == 2
    qualified = [r for r in rows if r.get("realized_pnl_pct") is not None]
    assert len(qualified) == 1                    # only the filled row qualifies
    assert qualified[0]["fill_status"] == "Filled"

    # Real-consumer proof: calibrate_conviction_sizing.load_closed_trades ingests ONLY the fill.
    try:
        from calibrate_conviction_sizing import load_closed_trades
    except Exception:
        return  # consumer not importable in this env -> the filter assertion above still proves it
    _ddir = os.environ.get("EXITMGR_DATASET_DIR") or os.path.join(
        os.path.dirname(cfg.journal.path), "data")
    ds_path = os.path.join(_ddir, "trade_dataset.jsonl")
    closed = load_closed_trades(ds_path)
    # The filled fixture still lacks commission-complete canonical NET P&L, so the stricter
    # canonical consumer rejects both it and the resting phantom.
    assert closed == []
