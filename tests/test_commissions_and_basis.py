"""Commissions (both sides) + real entry basis/slippage + realized_pnl_net (2026-07-03).

Audit gaps #2/#3: realized P&L was recorded GROSS of IBKR fees (systematically overstated on
~$100-500 debits where ~$0.65/contract/leg is material), and the cost basis was a pre-fill MID
ESTIMATE never reconciled to avg_fill_price. These tests prove:
  * entry + exit commissions are summed across legs and persisted,
  * realized_pnl_net = gross realized - entry_commission - exit_commission (gross kept intact),
  * an unknown commission -> net null + commission_unknown flag (never fabricated),
  * the real fill-based basis + entry slippage are recorded (with basis_source),
  * a LATE entry fill in fills.log is joined into the dataset,
  * a scale-out pro-rates the entry fee to the contracts closed.
No orders placed; broker mocked by conftest.
"""
import json
import os
import types

import pytest

from exitmgr.config import Config
from exitmgr.manager import ExitManager
from exitmgr.order import commission_from_trade, compute_entry_basis


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
    """A Trade-like object whose .fills carry commissionReport.commission (one per execution)."""
    return types.SimpleNamespace(fills=[_fill(c) for c in commissions])


def _trig(kind="profit_target", msg="tp"):
    return types.SimpleNamespace(trigger_type=kind, pnl_pct=50.0, message=msg)


# =========================================================================== commission_from_trade
def test_commission_single_leg():
    assert commission_from_trade(_trade(0.65)) == 0.65


def test_commission_spread_sums_legs():
    # a combo fills BOTH legs -> two commissionReports; the round-trip capture must SUM them
    assert commission_from_trade(_trade(0.65, 0.65)) == 1.3


def test_commission_unknown_when_no_fills():
    assert commission_from_trade(_trade()) is None
    assert commission_from_trade(types.SimpleNamespace(fills=None)) is None


def test_commission_zero_or_nan_treated_unknown():
    # an un-reported CommissionReport defaults to 0.0 -> must be UNKNOWN, never a fabricated $0 fee
    assert commission_from_trade(_trade(0.0)) is None
    assert commission_from_trade(_trade(float("nan"))) is None
    # one real + one not-yet-reported -> the real one still counts
    assert commission_from_trade(_trade(0.65, 0.0)) == 0.65


# =========================================================================== compute_entry_basis
def test_entry_basis_single():
    efd, slip, slip_pct = compute_entry_basis(120.0, 1.30, 1)
    assert efd == 130.0 and slip == 10.0 and slip_pct == 8.33


def test_entry_basis_spread_net_and_qty():
    # avg_fill_price is the combo NET; qty scales it. est 247 vs real 250 -> +3 slippage
    efd, slip, slip_pct = compute_entry_basis(247.0, 2.50, 1)
    assert efd == 250.0 and slip == 3.0 and slip_pct == 1.21


def test_entry_basis_unknown_fill():
    assert compute_entry_basis(120.0, None, 1) == (None, None, None)


# =========================================================================== net P&L end-to-end
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
    assert r["realized_pnl"] == 60.0                       # gross UNCHANGED (proceeds 180 - 120)
    assert r["entry_commission"] == 0.65
    assert r["exit_commission"] == 0.65
    assert r["commission_unknown"] is False
    assert r["realized_pnl_net"] == 58.70                  # 60 - 0.65 - 0.65
    assert r["basis_source"] == "fill" and r["entry_fill_debit"] == 121.0
    # ... and it flows into the fine-tuning dataset
    ds = _read_dataset(cfg)[0]
    assert ds["close"]["realized_pnl"] == 60.0
    assert ds["close"]["realized_pnl_net"] == 58.70
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
    # exit filled but the commissionReport hasn't landed -> exit_commission absent
    mgr._log_exit(112, "SPY", _trig(), exit_price_per_share=1.80, quantity=1,
                  reason="profit_target",
                  extra={"fill_status": "Filled", "avg_fill_price": 1.80})
    r = _read_exits(cfg)[0]
    assert r["realized_pnl"] == 60.0                       # gross still recorded
    assert r["exit_commission"] is None
    assert r["commission_unknown"] is True
    assert r["realized_pnl_net"] is None                   # never fabricated


def test_entry_commission_unknown_nulls_net(tmp_path):
    je = {"ts": "2026-06-20T16:00:00+00:00", "contract_id": 113, "symbol": "SPY",
          "right": "C", "strike": 610.0, "quantity": 1, "debit": 120.0}  # no entry_commission
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
    # entry journaled with NO fill known -> basis_source estimate, no real fill debit
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
    assert r["realized_pnl_net"] == 58.70                  # net still computable from gross+fees


def test_scale_out_prorates_entry_commission(tmp_path):
    # 2 contracts entered (entry fee 1.30 for both legs*qty); trim 1 -> entry fee alloc 0.65
    je = {"ts": "2026-06-20T16:00:00+00:00", "contract_id": 115, "symbol": "NVDA",
          "right": "C", "strike": 500.0, "quantity": 2, "debit": 800.0, "conviction": 6,
          "entry_commission": 1.30}
    mgr, cfg = _mgr(tmp_path, [je])
    mgr._log_exit(115, "NVDA", _trig(kind="scale_out", msg="trim"),
                  exit_price_per_share=5.0, quantity=1, reason="scale_out",
                  entry_debit=400.0,  # pro-rated basis for the 1 contract closed
                  extra={"fill_status": "Filled", "avg_fill_price": 5.0,
                         "exit_commission": 0.65, "partial": True,
                         "close_qty": 1, "remaining_qty": 1})
    r = _read_exits(cfg)[0]
    assert r["realized_pnl"] == 100.0                      # 500 proceeds - 400 basis
    assert r["entry_commission"] == 0.65                   # 1.30 * 1/2
    assert r["exit_commission"] == 0.65
    assert r["realized_pnl_net"] == 98.70                  # 100 - 0.65 - 0.65


def test_late_fill_join_from_fills_log(tmp_path):
    # entry journaled BEFORE its fill: avg_fill_price/entry_commission absent in trades.log; the
    # real fill later landed in fills.log -- the close-time join must backfill it into the dataset.
    je = {"ts": "2026-06-18T16:00:00+00:00", "contract_id": 222, "symbol": "MU",
          "right": "C", "strike": 1120.0, "quantity": 1, "debit": 247.0, "conviction": 5}
    (tmp_path / "fills.log").write_text(json.dumps(
        {"event": "entry_fill", "contract_id": 222, "avg_fill_price": 2.50,
         "entry_commission": 1.30, "quantity": 1}) + "\n")
    mgr, cfg = _mgr(tmp_path, [je])
    mgr._log_exit(222, "MU", _trig(kind="stop", msg="stop"), exit_price_per_share=2.0,
                  quantity=1, reason="stop",
                  extra={"fill_status": "Filled", "avg_fill_price": 2.0,
                         "exit_commission": 0.65})
    ds = _read_dataset(cfg)[0]
    assert ds["entry"]["entry_avg_fill_price"] == 2.50     # JOINED from fills.log
    assert ds["entry"]["entry_commission"] == 1.30
    assert ds["entry"]["entry_fill_debit"] == 250.0        # 2.50*100*1
    assert ds["entry"]["entry_slippage"] == 3.0            # 250 - 247
    assert ds["entry"]["basis_source"] == "fill"
    r = _read_exits(cfg)[0]
    # gross = 200 proceeds - 247 basis = -47 ; net = -47 - 1.30 - 0.65
    assert r["realized_pnl"] == -47.0
    assert r["realized_pnl_net"] == -48.95


def test_non_fill_exit_has_null_net(tmp_path):
    # H3: an exit that did NOT fill carries no realized -> net must also be null
    je = {"ts": "2026-06-20T16:00:00+00:00", "contract_id": 116, "symbol": "SPY",
          "right": "C", "strike": 610.0, "quantity": 1, "debit": 120.0,
          "entry_commission": 0.65}
    mgr, cfg = _mgr(tmp_path, [je])
    mgr._log_exit(116, "SPY", _trig(), exit_price_per_share=1.80, quantity=1,
                  reason="profit_target",
                  extra={"fill_status": "Submitted", "avg_fill_price": None,
                         "exit_commission": 0.65})
    r = _read_exits(cfg)[0]
    assert r["realized_pnl"] is None                       # H3 nulled it (non-fill)
    assert r["realized_pnl_net"] is None                   # net follows gross
    assert r["mark_estimate_pnl_pct"] is not None          # H3 mark preserved
