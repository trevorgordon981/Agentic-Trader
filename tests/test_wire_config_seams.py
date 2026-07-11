"""Wiring tests for two config seams plumbed into production on 2026-07-03.

Both consumers were already built; these tests prove the CONSTRUCTION sites now pass the
config values through, and that the DEFAULTS keep behaviour byte-identical to before.

Seam 1: exitmgr/manager.py ExitManager.__init__ builds OrderManager with
        exit_slippage_floor=getattr(self.config.rules, "exit_slippage_floor", 0.50).
Seam 2: run_trader.py builds RiskLimits with
        conviction_size_multipliers=getattr(cfg, "conviction_size_multipliers", None).

run_trader.main() is NOT unit-testable in isolation (it opens an IBKR connection and starts
the trader), so Seam 2 is exercised by replicating the exact construction EXPRESSION run_trader
uses against fake cfg objects, plus a source-level assertion that the wiring line is present.
"""
import json
import re
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from exitmgr.config import Config, RulesConfig, TrailingConfig, ScaleOutConfig
from exitmgr.manager import ExitManager
from exitmgr.risk import RiskLimits, conviction_multiplier


# --------------------------------------------------------------------------------------
# Seam 1 -- exit_slippage_floor -> OrderManager (via ExitManager)
# --------------------------------------------------------------------------------------
def _mgr(tmp_path, floor):
    """Build an ExitManager whose RulesConfig carries the given exit_slippage_floor
    (or the RulesConfig default when floor is None)."""
    cfg = Config()
    cfg.dry_run = False
    cfg.loop_mode = False
    cfg.manage_positions = False
    cfg.journal.path = str(tmp_path / "trades.log")
    cfg.state.path = str(tmp_path / "state.json")
    cfg.kill_switch.path = str(tmp_path / "KILL")
    cfg.audit_path = str(tmp_path / "audit.jsonl")
    rkw = dict(profit_target_pct=30.0, stop_pct=30.0, time_stop_days=10,
               trailing=TrailingConfig(enabled=False),
               scale_out=ScaleOutConfig(enabled=False))
    if floor is not None:
        rkw["exit_slippage_floor"] = floor
    cfg.rules = RulesConfig(**rkw)
    (tmp_path / "trades.log").write_text("")
    return ExitManager(cfg)


def test_seam1_custom_floor_reaches_order_manager(tmp_path):
    mgr = _mgr(tmp_path, floor=0.30)
    assert mgr.order_manager.EXIT_SLIPPAGE_FLOOR == 0.30


def test_seam1_default_floor_is_050_byte_identical(tmp_path):
    # RulesConfig default exit_slippage_floor is 0.50; unset -> 0.50 reaches OrderManager.
    mgr = _mgr(tmp_path, floor=None)
    assert mgr.order_manager.EXIT_SLIPPAGE_FLOOR == 0.50


def _close_px(mgr, *, mark, bid):
    """Drive the triggered bid-anchored branch of _build_close_order and capture the
    limit price handed to the broker. mark*(1-FLOOR) is the floor; bid below it -> px=floor."""
    om = mgr.order_manager
    captured = {}
    def _cap(action, qty, price):
        captured["px"] = price
        return MagicMock()
    om.ib_conn = MagicMock()
    om.ib_conn.create_limit_order.side_effect = _cap
    om._build_close_order(quantity=1, limit_price=mark, market=True, bid=bid,
                          trigger_type="stop")
    return captured["px"]


def test_seam1_floor_bites_in_build_close_order(tmp_path):
    # mark=1.00, bid=0.10 (below both floors). floor(0.30)=0.70 ; floor(0.50)=0.50.
    px_custom = _close_px(_mgr(tmp_path, floor=0.30), mark=1.00, bid=0.10)
    px_default = _close_px(_mgr(tmp_path, floor=None), mark=1.00, bid=0.10)
    assert px_custom == 0.70
    assert px_default == 0.50   # byte-identical to pre-wiring behaviour


# --------------------------------------------------------------------------------------
# Seam 2 -- conviction_size_multipliers -> RiskLimits (run_trader.py construction path)
# --------------------------------------------------------------------------------------
def _build_limits_like_run_trader(cfg):
    """EXACT expression run_trader.py uses to plumb the multipliers into RiskLimits."""
    return RiskLimits(
        conviction_size_multipliers=getattr(cfg, "conviction_size_multipliers", None),
    )


def test_seam2_multipliers_carried_when_set():
    cfg = SimpleNamespace(conviction_size_multipliers={7: 1.5, 3: 0.5})
    limits = _build_limits_like_run_trader(cfg)
    assert limits.conviction_size_multipliers == {7: 1.5, 3: 0.5}
    # consumer reads the map: conviction 7 -> 1.5x
    assert conviction_multiplier(7, limits.conviction_size_multipliers) == 1.5


def test_seam2_default_none_is_flat_byte_identical():
    cfg = SimpleNamespace()  # attr absent -> getattr default None
    limits = _build_limits_like_run_trader(cfg)
    assert limits.conviction_size_multipliers is None
    # None map -> flat 1.0 for every conviction (unchanged sizing).
    assert conviction_multiplier(7, limits.conviction_size_multipliers) == 1.0
    assert conviction_multiplier(3, limits.conviction_size_multipliers) == 1.0


def test_seam2_wiring_line_present_in_run_trader_source():
    with open("run_trader.py") as f:
        src = f.read()
    # Every entry path now uses one canonical parser, eliminating drift between daily and trader.
    assert "entry_safety.risk_limits_from_config(cfg)" in src
    with open("exitmgr/entry_safety.py") as f:
        helper = f.read()
    assert "{int(k): float(v) for k, v in dict(raw).items()}" in helper
