"""Tests for tune_exit_floor.py -- the PROPOSE-ONLY exit slippage-floor tuner (2026-07-03).

Covers:
  * recommend() on real fill_quality JSON built from synthetic close rows:
      - TOO_TIGHT (protective exits resting)  -> action LOWER  (raise the fraction so exits fill)
      - TOO_LOOSE (fills far below the mark)   -> action RAISE  (tighten toward fill_quality's suggested)
      - INSUFFICIENT (too few fills)           -> action HOLD   (never changes on thin data)
  * config round-trip: rules.exit_slippage_floor loads via Config.from_yaml AND reaches order.py's
    _build_close_order floor logic; an ABSENT value == the 0.50 default == BYTE-IDENTICAL limit
    price to today's hardcoded behavior.
"""
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


# --------------------------------------------------------------------------- synthetic rows
def _close_row(symbol, *, trigger_mark, avg_fill_price, fill_status="Filled", rule_fired="stop"):
    """A v2 kind=='trade' row, slippage computed exactly as the logger does."""
    slip = slip_pct = None
    if avg_fill_price is not None and trigger_mark is not None:
        slip = round(float(avg_fill_price) - float(trigger_mark), 4)
        if float(trigger_mark) != 0:
            slip_pct = round(slip / abs(float(trigger_mark)) * 100, 2)
    return {
        "schema": "trade_dataset.v2", "kind": "trade", "con_id": 1000, "symbol": symbol,
        "entry": {"symbol": symbol, "quantity": 1, "spread": None},
        "close": {"fill_status": fill_status, "avg_fill_price": avg_fill_price,
                  "trigger_mark": trigger_mark, "slippage_per_share": slip,
                  "slippage_pct": slip_pct, "rule_fired": rule_fired, "close_qty": 1},
    }


def _report_from_rows(tmp_path, rows, min_fills=5):
    p = tmp_path / "ds.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return fq.build_report(str(p), min_fills=min_fills)


# --------------------------------------------------------------------------- recommend() logic
def test_too_loose_recommends_raise(tmp_path):
    # 6 filled well BELOW the mark (mark 2.00, fill 1.60 => 20% give-up), 0 unfilled -> fill_rate
    # 100% but median give-up 20% > 10% => TOO_LOOSE. Fix = RAISE the floor price (tighten).
    rows = [_close_row("SPY", trigger_mark=2.00, avg_fill_price=1.60) for _ in range(6)]
    rep = _report_from_rows(tmp_path, rows)
    assert rep["portfolio"]["verdict"] == "TOO_LOOSE"
    prop = tef.recommend(rep, current_floor=0.50)
    assert prop["action"] == "RAISE"
    # tighter floor == a SMALLER fraction; equals fill_quality's own suggested, and < current
    assert prop["recommended_exit_slippage_floor"] == rep["portfolio"]["suggested_exit_slippage_floor"]
    assert prop["recommended_exit_slippage_floor"] < 0.50
    assert prop["changed"] is True


def test_too_tight_recommends_lower(tmp_path):
    # 6 filled (>= MIN so not INSUFFICIENT) but 6 unfilled/cancelled -> fill_rate 50% < 80% =>
    # TOO_TIGHT. Fix = LOWER the floor price so exits fill == RAISE the config fraction.
    filled = [_close_row("SPY", trigger_mark=2.00, avg_fill_price=1.98) for _ in range(6)]
    unfilled = [_close_row("SPY", trigger_mark=2.00, avg_fill_price=None,
                           fill_status="Cancelled") for _ in range(6)]
    rep = _report_from_rows(tmp_path, filled + unfilled)
    assert rep["portfolio"]["verdict"] == "TOO_TIGHT"
    prop = tef.recommend(rep, current_floor=0.50)
    assert prop["action"] == "LOWER"
    assert prop["recommended_exit_slippage_floor"] > 0.50   # bigger fraction => lower price line
    assert prop["changed"] is True


def test_insufficient_holds(tmp_path):
    # only 2 fills (< MIN_FILLS 5) => INSUFFICIENT => HOLD, never a change.
    rows = [_close_row("SPY", trigger_mark=2.00, avg_fill_price=1.90) for _ in range(2)]
    rep = _report_from_rows(tmp_path, rows)
    assert rep["portfolio"]["verdict"] == "INSUFFICIENT"
    prop = tef.recommend(rep, current_floor=0.50)
    assert prop["action"] == "HOLD"
    assert prop["changed"] is False
    assert prop["recommended_exit_slippage_floor"] == 0.50
    assert "need more fills" in prop["reason"].lower()


def test_empty_dataset_holds(tmp_path):
    # no rows at all -> the live-today case; must HOLD, not crash.
    rep = _report_from_rows(tmp_path, [])
    prop = tef.recommend(rep, current_floor=0.50)
    assert prop["action"] == "HOLD"
    assert prop["changed"] is False


# --------------------------------------------------------------------------- config -> order.py
def _floor_px(exit_slippage_floor):
    """Build an OrderManager (default when arg is None) and return the LIMIT price of a triggered
    close whose bid (0.02) is below the floor, so the floor engages."""
    ib = MagicMock()
    sm = StateManager("/tmp/_tef_state_ignore.json")
    kwargs = {} if exit_slippage_floor is None else {"exit_slippage_floor": exit_slippage_floor}
    om = OrderManager(ib, sm, **kwargs)
    om._build_close_order(1, 2.00, True, bid=0.02, trigger_type="stop")
    action, qty, px = ib.create_limit_order.call_args[0]
    assert action == "SELL"
    return om, px


def test_absent_value_is_byte_identical_default():
    # No exit_slippage_floor arg -> module default 0.50 -> floor px == mark*(1-0.50) == 1.00,
    # EXACTLY today's hardcoded behavior.
    om, px = _floor_px(None)
    assert om.EXIT_SLIPPAGE_FLOOR == 0.50 == DEFAULT_EXIT_SLIPPAGE_FLOOR
    assert px == round(2.00 * (1 - 0.50), 2) == 1.00


def test_config_value_reaches_floor_logic():
    # A tuned 0.30 floor -> px == mark*(1-0.30) == 1.40, differs from the 1.00 default.
    om, px = _floor_px(0.30)
    assert om.EXIT_SLIPPAGE_FLOOR == 0.30
    assert px == round(2.00 * (1 - 0.30), 2) == 1.40


def test_config_round_trip_present_and_absent(tmp_path):
    # present in YAML -> loads onto RulesConfig -> threads into OrderManager -> floor uses it.
    cfg_present = tmp_path / "present.yaml"
    cfg_present.write_text("rules:\n  exit_slippage_floor: 0.30\n")
    cfg = Config.from_yaml(str(cfg_present))
    assert cfg.rules.exit_slippage_floor == 0.30
    om, px = _floor_px(cfg.rules.exit_slippage_floor)
    assert px == 1.40

    # absent from YAML -> default 0.50 -> byte-identical 1.00 floor price.
    cfg_absent = tmp_path / "absent.yaml"
    cfg_absent.write_text("rules:\n  stop_pct: 30.0\n")
    cfg2 = Config.from_yaml(str(cfg_absent))
    assert cfg2.rules.exit_slippage_floor == 0.50
    _om2, px2 = _floor_px(cfg2.rules.exit_slippage_floor)
    assert px2 == 1.00

    # RulesConfig default itself is 0.50
    assert RulesConfig().exit_slippage_floor == 0.50


# --------------------------------------------------------------------------- --write staging
def test_write_stages_into_config_yaml(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("rules:\n  stop_pct: 30.0\n  exit_slippage_floor: 0.50\n  time_stop_days: 10\n")
    ok, bak = tef.stage_into_config(str(cfg), 0.30)
    assert ok
    reloaded = Config.from_yaml(str(cfg))
    assert reloaded.rules.exit_slippage_floor == 0.30
    assert reloaded.rules.stop_pct == 30.0  # other keys preserved
    assert os.path.exists(bak)
