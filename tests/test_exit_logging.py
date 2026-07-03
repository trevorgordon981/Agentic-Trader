"""Tests for FIX #2: durable realized-exit logging to exits.log (JSONL).

The exit manager previously recorded realized P&L NOWHERE on disk (only in IBKR).
_log_exit appends an append-only JSONL record consumable by
conviction_calibration.py --fills (keyed contract_id -> realized_pnl + conviction).
These tests assert the schema + values without placing any orders.
"""
import json
import types

import pytest

from exitmgr.config import Config
from exitmgr.manager import ExitManager


def _mgr(tmp_path, journal_lines, audit_lines=None):
    cfg = Config()
    cfg.dry_run = True
    cfg.loop_mode = False
    cfg.journal.path = str(tmp_path / "trades.log")
    cfg.state.path = str(tmp_path / "state.json")
    cfg.kill_switch.path = str(tmp_path / "KILL")
    cfg.audit_path = str(tmp_path / "audit.jsonl")
    (tmp_path / "trades.log").write_text(
        "".join(json.dumps(x) + "\n" for x in journal_lines))
    if audit_lines is not None:
        (tmp_path / "audit.jsonl").write_text(
            "".join(json.dumps(x) + "\n" for x in audit_lines))
    return ExitManager(cfg), cfg


def _read_exits(cfg):
    import os
    path = os.path.join(os.path.dirname(cfg.journal.path) or ".", "exits.log")
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]


def test_log_exit_single_leg_realized_pnl_and_conviction(tmp_path):
    je = {"ts": "2026-06-20T16:00:00+00:00", "contract_id": 111, "symbol": "SPY",
          "right": "C", "strike": 610.0, "quantity": 1, "debit": 120.0, "conviction": 7}
    mgr, cfg = _mgr(tmp_path, [je])
    trig = types.SimpleNamespace(trigger_type="profit_target", pnl_pct=50.0, message="tp")
    # exit at $1.80/share -> proceeds 180, entry 120 -> +60 (+50%)
    mgr._log_exit(111, "SPY", trig, exit_price_per_share=1.80, quantity=1,
                  reason=mgr._exit_reason(trig))
    rows = _read_exits(cfg)
    assert len(rows) == 1
    r = rows[0]
    assert r["contract_id"] == 111 and r["conId"] == 111
    assert r["structure"] == "single"
    assert r["proceeds"] == 180.0
    assert r["realized_pnl"] == 60.0 and r["realizedPNL"] == 60.0
    assert r["realized_pnl_pct"] == 50.0
    assert r["reason"] == "profit_target"
    assert r["conviction"] == 7.0
    assert r["entry_ts"] == "2026-06-20T16:00:00+00:00"
    assert r["holding_days"] is not None and r["holding_days"] > 0


def test_log_exit_spread_records_legs_and_loss(tmp_path):
    je = {"ts": "2026-06-18T16:09:33+00:00", "contract_id": 222, "symbol": "MU",
          "right": "C", "strike": 1120.0, "quantity": 1, "debit": 247.0, "conviction": 5,
          "spread": {"short_con_id": 999, "short_strike": 1125.0, "width": 5.0}}
    mgr, cfg = _mgr(tmp_path, [je])
    trig = types.SimpleNamespace(trigger_type="stop", pnl_pct=-40.0, message="stop")
    # net exit $1.48/share -> proceeds 148, entry 247 -> -99
    mgr._log_exit(222, "MU", trig, exit_price_per_share=1.48, quantity=1,
                  reason=mgr._exit_reason(trig))
    r = _read_exits(cfg)[0]
    assert r["structure"] == "spread"
    assert r["spread"]["short_con_id"] == 999 and r["spread"]["width"] == 5.0
    assert r["proceeds"] == 148.0
    assert r["realized_pnl"] == -99.0
    assert r["reason"] == "stop"
    assert r["conviction"] == 5.0


def test_log_exit_conviction_falls_back_to_audit(tmp_path):
    # journal entry has NO conviction; recover it from audit daily_rec_posted by symbol+strike
    je = {"ts": "2026-06-22T17:00:00+00:00", "contract_id": 333, "symbol": "AA",
          "right": "P", "strike": 57.0, "quantity": 1, "debit": 160.0}
    audit = [{"ts": "2026-06-22T13:50:00+00:00", "event": "daily_rec_posted",
              "underlying": "AA", "conviction": 6,
              "order": "BUY 1x AA 20260702 57/49.5P debit spread @ $1.60 LMT"}]
    mgr, cfg = _mgr(tmp_path, [je], audit_lines=audit)
    trig = types.SimpleNamespace(trigger_type="time_stop", pnl_pct=-10.0, message="t")
    mgr._log_exit(333, "AA", trig, exit_price_per_share=1.44, quantity=1,
                  reason=mgr._exit_reason(trig))
    r = _read_exits(cfg)[0]
    assert r["conviction"] == 6.0       # recovered from audit, not the journal
    assert r["reason"] == "time_stop"


def test_log_exit_manual_market_close_null_proceeds(tmp_path):
    # manual MARKET close: fill price unknown synchronously -> proceeds/realized null, reason=manual
    je = {"ts": "2026-06-20T16:00:00+00:00", "contract_id": 444, "symbol": "IWM",
          "right": "C", "strike": 290.0, "quantity": 1, "debit": 829.05, "conviction": 4}
    mgr, cfg = _mgr(tmp_path, [je])
    mgr._log_exit(444, "IWM", types.SimpleNamespace(trigger_type="manual"),
                  exit_price_per_share=None, quantity=1, reason="manual")
    r = _read_exits(cfg)[0]
    assert r["reason"] == "manual"
    assert r["proceeds"] is None and r["realized_pnl"] is None
    assert r["conviction"] == 4.0
    assert r["contract_id"] == 444


def test_exit_reason_mapping():
    cfg = Config()
    cfg.journal.path = "./nonexistent_trades.log"
    mgr = ExitManager(cfg)
    R = lambda tt: mgr._exit_reason(types.SimpleNamespace(trigger_type=tt))
    assert R("profit_target") == "profit_target"
    assert R("take_profit") == "profit_target"
    assert R("time_stop") == "time_stop"
    assert R("stop") == "stop"
    assert R("trailing_stop") == "stop"
    assert R("model_cut") == "stop"
    assert R("manual") == "manual"
