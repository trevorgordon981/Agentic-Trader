"""Public API contract; production-derived narrative omitted."""
import json
import os
import sys

from unittest.mock import MagicMock

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import fill_quality_report as fq
import tune_exit_floor as tef
from exitmgr.order import OrderManager, DEFAULT_EXIT_SLIPPAGE_FLOOR
from exitmgr.config import Config, RulesConfig
from exitmgr.state import StateManager



def _close_row(symbol, *, trigger_mark, avg_fill_price, fill_status="Filled", rule_fired="stop"):
    """Public API contract; production-derived narrative omitted."""
    slip = slip_pct = None
    if avg_fill_price is not None and trigger_mark is not None:
        slip = round(float(avg_fill_price) - float(trigger_mark), 4)
        if float(trigger_mark) != 0:
            slip_pct = round(slip / abs(float(trigger_mark)) * 100, 2)
    return {
        "schema": "trade_dataset.v2", "record_status": "CANONICAL", "canonical": True,
        "usable_for_training": False, "usable_for_pnl": False,
        "kind": "trade", "con_id": 1000, "symbol": symbol,
        "entry": {"symbol": symbol, "quantity": 1, "spread": None},
        "close": {"fill_status": fill_status, "avg_fill_price": avg_fill_price,
                  "trigger_mark": trigger_mark, "slippage_per_share": slip,
                  "slippage_pct": slip_pct, "rule_fired": rule_fired, "close_qty": 1},
    }


def _report_from_rows(tmp_path, rows, min_fills=5):
    p = tmp_path / "ds.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return fq.build_report(str(p), min_fills=min_fills)









_CONSTRUCTION = ("construction:\n  max_entry_spread_pct: 25.0\n"
                 "caps:\n  max_orders_per_day: 20\n  max_notional_per_day: 50000.0\n")

def test_too_loose_recommends_raise(tmp_path):


    rows = [_close_row("SPY", trigger_mark=2.00, avg_fill_price=1.60) for _ in range(6)]
    rep = _report_from_rows(tmp_path, rows)
    assert rep["portfolio"]["verdict"] == "TOO_LOOSE"
    prop = tef.recommend(rep, current_floor=0.50)
    assert prop["action"] == "RAISE"

    assert prop["recommended_exit_slippage_floor"] == rep["portfolio"]["suggested_exit_slippage_floor"]
    assert prop["recommended_exit_slippage_floor"] < 0.50
    assert prop["changed"] is True


def test_too_tight_recommends_lower(tmp_path):


    filled = [_close_row("SPY", trigger_mark=2.00, avg_fill_price=1.98) for _ in range(6)]
    unfilled = [_close_row("SPY", trigger_mark=2.00, avg_fill_price=None,
                           fill_status="Cancelled") for _ in range(6)]
    rep = _report_from_rows(tmp_path, filled + unfilled)
    assert rep["portfolio"]["verdict"] == "TOO_TIGHT"
    prop = tef.recommend(rep, current_floor=0.50)
    assert prop["action"] == "LOWER"
    assert prop["recommended_exit_slippage_floor"] > 0.50
    assert prop["changed"] is True


def test_insufficient_holds(tmp_path):

    rows = [_close_row("SPY", trigger_mark=2.00, avg_fill_price=1.90) for _ in range(2)]
    rep = _report_from_rows(tmp_path, rows)
    assert rep["portfolio"]["verdict"] == "INSUFFICIENT"
    prop = tef.recommend(rep, current_floor=0.50)
    assert prop["action"] == "HOLD"
    assert prop["changed"] is False
    assert prop["recommended_exit_slippage_floor"] == 0.50
    assert "need more fills" in prop["reason"].lower()


def test_empty_dataset_holds(tmp_path):

    rep = _report_from_rows(tmp_path, [])
    prop = tef.recommend(rep, current_floor=0.50)
    assert prop["action"] == "HOLD"
    assert prop["changed"] is False



def _floor_px(exit_slippage_floor):
    """Public API contract; production-derived narrative omitted."""
    ib = MagicMock()
    sm = StateManager("/tmp/_tef_state_ignore.json")
    kwargs = {} if exit_slippage_floor is None else {"exit_slippage_floor": exit_slippage_floor}
    om = OrderManager(ib, sm, **kwargs)
    om._build_close_order(1, 2.00, True, bid=0.02, trigger_type="stop")
    action, qty, px = ib.create_limit_order.call_args[0]
    assert action == "SELL"
    return om, px


def test_absent_value_is_byte_identical_default():


    om, px = _floor_px(None)
    assert om.EXIT_SLIPPAGE_FLOOR == 0.50 == DEFAULT_EXIT_SLIPPAGE_FLOOR
    assert px == round(2.00 * (1 - 0.50), 2) == 1.00


def test_config_value_reaches_floor_logic():

    om, px = _floor_px(0.30)
    assert om.EXIT_SLIPPAGE_FLOOR == 0.30
    assert px == round(2.00 * (1 - 0.30), 2) == 1.40


def test_config_round_trip_present_and_absent(tmp_path):

    cfg_present = tmp_path / "present.yaml"
    cfg_present.write_text(_CONSTRUCTION + "rules:\n  exit_slippage_floor: 0.30\n")
    cfg = Config.from_yaml(str(cfg_present))
    assert cfg.rules.exit_slippage_floor == 0.30
    om, px = _floor_px(cfg.rules.exit_slippage_floor)
    assert px == 1.40


    cfg_absent = tmp_path / "absent.yaml"
    cfg_absent.write_text(_CONSTRUCTION + "rules:\n  stop_pct: 30.0\n")
    cfg2 = Config.from_yaml(str(cfg_absent))
    assert cfg2.rules.exit_slippage_floor == 0.50
    _om2, px2 = _floor_px(cfg2.rules.exit_slippage_floor)
    assert px2 == 1.00


    assert RulesConfig().exit_slippage_floor == 0.50



def test_write_stages_into_config_yaml(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_CONSTRUCTION +
                   "rules:\n  stop_pct: 30.0\n  exit_slippage_floor: 0.50\n"
                   "  time_stop_days: 10\n")
    ok, bak = tef.stage_into_config(str(cfg), 0.30)
    assert ok
    reloaded = Config.from_yaml(str(cfg))
    assert reloaded.rules.exit_slippage_floor == 0.30
    assert reloaded.rules.stop_pct == 30.0
    assert os.path.exists(bak)
