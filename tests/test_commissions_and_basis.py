"""Public API contract; production-derived narrative omitted."""
import json
import os
import types

import pytest

from exitmgr.config import Config
from exitmgr.manager import ExitManager
from exitmgr.order import commission_from_trade, compute_entry_basis



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


def _fill(commission):
    return types.SimpleNamespace(commissionReport=types.SimpleNamespace(commission=commission))


def _trade(*commissions):
    """Public API contract; production-derived narrative omitted."""
    return types.SimpleNamespace(fills=[_fill(c) for c in commissions])


def _trig(kind="profit_target", msg="tp"):
    return types.SimpleNamespace(trigger_type=kind, pnl_pct=50.0, message=msg)



def test_commission_single_leg():
    assert commission_from_trade(_trade(0.65)) == 0.65


def test_commission_spread_sums_legs():

    assert commission_from_trade(_trade(0.65, 0.65)) == 1.3


def test_commission_unknown_when_no_fills():
    assert commission_from_trade(_trade()) is None
    assert commission_from_trade(types.SimpleNamespace(fills=None)) is None


def test_commission_zero_or_nan_treated_unknown():

    assert commission_from_trade(_trade(0.0)) is None
    assert commission_from_trade(_trade(float("nan"))) is None

    assert commission_from_trade(_trade(0.65, 0.0)) is None



def test_entry_basis_single():
    efd, slip, slip_pct = compute_entry_basis(120.0, 1.30, 1)
    assert efd == 130.0 and slip == 10.0 and slip_pct == 8.33


def test_entry_basis_spread_net_and_qty():

    efd, slip, slip_pct = compute_entry_basis(247.0, 2.50, 1)
    assert efd == 250.0 and slip == 3.0 and slip_pct == 1.21


def test_entry_basis_unknown_fill():
    assert compute_entry_basis(120.0, None, 1) == (None, None, None)



def test_entry_and_exit_commission_and_net(tmp_path):
    je = {"ts": "2026-06-20T16:00:00+00:00", "contract_id": 111, "symbol": "SPY",
          "right": "C", "strike": 610.0, "quantity": 1, "debit": 120.0, "conviction": 7,
          "avg_fill_price": 1.21, "entry_commission": 0.65, "entry_fill_debit": 121.0,
          "entry_slippage": 1.0, "entry_slippage_pct": 0.83, "basis_source": "fill"}
    mgr, cfg = _mgr(tmp_path, [je])
    mgr._log_exit(111, "SPY", _trig(), exit_price_per_share=1.80, quantity=1,
                  reason="profit_target",
                  extra={"fill_status": "Filled", "avg_fill_price": 1.80,
                         "exit_commission": 0.65})
    r = _read_exits(cfg)[0]


    assert r["realized_pnl"] == 59.0
    assert r["entry_commission"] == 0.65
    assert r["exit_commission"] == 0.65
    assert r["commission_unknown"] is False
    assert r["realized_pnl_net"] == 57.70
    assert r["basis_source"] == "fill" and r["entry_fill_debit"] == 121.0

    ds = _read_dataset(cfg)[0]
    assert ds["close"]["realized_pnl"] == 59.0
    assert ds["close"]["realized_pnl_net"] == 57.70
    assert ds["close"]["entry_commission"] == 0.65
    assert ds["close"]["exit_commission"] == 0.65
    assert ds["close"]["commission_unknown"] is False
    assert ds["entry"]["entry_commission"] == 0.65
    assert ds["entry"]["entry_fill_debit"] == 121.0
    assert ds["entry"]["entry_slippage"] == 1.0
    assert ds["entry"]["basis_source"] == "fill"


def test_exit_commission_unknown_nulls_net(tmp_path):
    je = {"ts": "2026-06-20T16:00:00+00:00", "contract_id": 112, "symbol": "SPY",
          "right": "C", "strike": 610.0, "quantity": 1, "debit": 120.0, "conviction": 7,
          "entry_commission": 0.65}
    mgr, cfg = _mgr(tmp_path, [je])

    mgr._log_exit(112, "SPY", _trig(), exit_price_per_share=1.80, quantity=1,
                  reason="profit_target",
                  extra={"fill_status": "Filled", "avg_fill_price": 1.80})
    r = _read_exits(cfg)[0]
    assert r["realized_pnl"] == 60.0
    assert r["exit_commission"] is None
    assert r["commission_unknown"] is True
    assert r["realized_pnl_net"] is None


def test_entry_commission_unknown_nulls_net(tmp_path):
    je = {"ts": "2026-06-20T16:00:00+00:00", "contract_id": 113, "symbol": "SPY",
          "right": "C", "strike": 610.0, "quantity": 1, "debit": 120.0}
    mgr, cfg = _mgr(tmp_path, [je])
    mgr._log_exit(113, "SPY", _trig(), exit_price_per_share=1.80, quantity=1,
                  reason="profit_target",
                  extra={"fill_status": "Filled", "avg_fill_price": 1.80,
                         "exit_commission": 0.65})
    r = _read_exits(cfg)[0]
    assert r["entry_commission"] is None
    assert r["commission_unknown"] is True
    assert r["realized_pnl_net"] is None


def test_estimate_basis_fallback_labeled(tmp_path):

    je = {"ts": "2026-06-20T16:00:00+00:00", "contract_id": 114, "symbol": "SPY",
          "right": "C", "strike": 610.0, "quantity": 1, "debit": 120.0,
          "entry_commission": 0.65, "basis_source": "estimate"}
    mgr, cfg = _mgr(tmp_path, [je])
    mgr._log_exit(114, "SPY", _trig(), exit_price_per_share=1.80, quantity=1,
                  reason="profit_target",
                  extra={"fill_status": "Filled", "avg_fill_price": 1.80,
                         "exit_commission": 0.65})
    r = _read_exits(cfg)[0]
    assert r["basis_source"] == "estimate"
    assert r["entry_fill_debit"] is None
    assert r["realized_pnl_net"] == 58.70


def test_scale_out_prorates_entry_commission(tmp_path):

    je = {"ts": "2026-06-20T16:00:00+00:00", "contract_id": 115, "symbol": "NVDA",
          "right": "C", "strike": 500.0, "quantity": 2, "debit": 800.0, "conviction": 6,
          "entry_commission": 1.30}
    mgr, cfg = _mgr(tmp_path, [je])
    mgr._log_exit(115, "NVDA", _trig(kind="scale_out", msg="trim"),
                  exit_price_per_share=5.0, quantity=1, reason="scale_out",
                  entry_debit=400.0,
                  extra={"fill_status": "Filled", "avg_fill_price": 5.0,
                         "exit_commission": 0.65, "partial": True,
                         "close_qty": 1, "remaining_qty": 1})
    r = _read_exits(cfg)[0]
    assert r["realized_pnl"] == 100.0
    assert r["entry_commission"] == 0.65
    assert r["exit_commission"] == 0.65
    assert r["realized_pnl_net"] == 98.70


def test_late_fill_join_from_fills_log(tmp_path):


    je = {"ts": "2026-06-18T16:00:00+00:00", "contract_id": 222, "symbol": "MUTX",
          "right": "C", "strike": 1120.0, "quantity": 1, "debit": 247.0, "conviction": 5}
    (tmp_path / "fills.log").write_text(json.dumps(
        {"event": "entry_fill", "contract_id": 222, "avg_fill_price": 2.50,
         "entry_commission": 1.30, "quantity": 1}) + "\n")
    mgr, cfg = _mgr(tmp_path, [je])
    mgr._log_exit(222, "MUTX", _trig(kind="stop", msg="stop"), exit_price_per_share=2.0,
                  quantity=1, reason="stop",
                  extra={"fill_status": "Filled", "avg_fill_price": 2.0,
                         "exit_commission": 0.65})
    ds = _read_dataset(cfg)[0]
    assert ds["entry"]["entry_avg_fill_price"] == 2.50
    assert ds["entry"]["entry_commission"] == 1.30
    assert ds["entry"]["entry_fill_debit"] == 250.0
    assert ds["entry"]["entry_slippage"] == 3.0
    assert ds["entry"]["basis_source"] == "fill"
    r = _read_exits(cfg)[0]



    assert r["realized_pnl"] == -50.0
    assert r["realized_pnl_net"] == -51.95


def test_non_fill_exit_has_null_net(tmp_path):

    je = {"ts": "2026-06-20T16:00:00+00:00", "contract_id": 116, "symbol": "SPY",
          "right": "C", "strike": 610.0, "quantity": 1, "debit": 120.0,
          "entry_commission": 0.65}
    mgr, cfg = _mgr(tmp_path, [je])
    mgr._log_exit(116, "SPY", _trig(), exit_price_per_share=1.80, quantity=1,
                  reason="profit_target",
                  extra={"fill_status": "Submitted", "avg_fill_price": None,
                         "exit_commission": 0.65})
    r = _read_exits(cfg)[0]
    assert r["realized_pnl"] is None
    assert r["realized_pnl_net"] is None
    assert r["mark_estimate_pnl_pct"] is not None
