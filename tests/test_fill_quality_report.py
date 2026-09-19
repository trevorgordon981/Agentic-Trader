"""Public API contract; production-derived narrative omitted."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fill_quality_report as fq
import tune_exit_floor as tef



def _close_row(symbol, *, trigger_mark, avg_fill_price, fill_status="Filled",
               rule_fired="stop", is_spread=False, close_qty=1, partial=False):
    """Public API contract; production-derived narrative omitted."""
    slip = None
    slip_pct = None
    if avg_fill_price is not None and trigger_mark is not None:
        slip = round(float(avg_fill_price) - float(trigger_mark), 4)
        if float(trigger_mark) != 0:
            slip_pct = round(slip / abs(float(trigger_mark)) * 100, 2)
    return {
        "schema": "trade_dataset.v2",
        "record_status": "CANONICAL", "canonical": True,
        "usable_for_training": False, "usable_for_pnl": False,
        "kind": "trade",
        "con_id": 1000,
        "symbol": symbol,
        "entry": {"symbol": symbol, "quantity": close_qty,
                  "spread": {"short_con_id": 9} if is_spread else None},
        "close": {
            "fill_status": fill_status,
            "avg_fill_price": avg_fill_price,
            "trigger_mark": trigger_mark,
            "slippage_per_share": slip,
            "slippage_pct": slip_pct,
            "rule_fired": rule_fired,
            "close_qty": close_qty,
            "partial": partial,
            "ts": "2026-07-03T00:00:00+00:00",
        },
    }


def _unfilled_row(symbol, *, trigger_mark=1.00, fill_status="Cancelled"):
    """Public API contract; production-derived narrative omitted."""
    return _close_row(symbol, trigger_mark=trigger_mark, avg_fill_price=None,
                      fill_status=fill_status)


def _write(tmp_path, rows):
    p = tmp_path / "trade_dataset.jsonl"
    with open(p, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return str(p)


def test_unmarked_rows_are_not_consumed(tmp_path):
    row = _close_row("OLD", trigger_mark=1.0, avg_fill_price=0.9)
    row.pop("record_status")
    row.pop("canonical")
    path = _write(tmp_path, [row])
    assert list(fq.iter_rows(path)) == []



def test_per_symbol_aggregation_math(tmp_path):


    rows = [
        _close_row("GOOD", trigger_mark=1.00, avg_fill_price=1.01),
        _close_row("GOOD", trigger_mark=1.00, avg_fill_price=0.99),
        _close_row("GOOD", trigger_mark=1.00, avg_fill_price=1.00),
        _close_row("GOOD", trigger_mark=2.00, avg_fill_price=2.00),
        _close_row("GOOD", trigger_mark=2.00, avg_fill_price=2.02),
    ]
    path = _write(tmp_path, rows)
    rep = fq.build_report(path, min_fills=5)
    g = rep["by_symbol"][0]
    assert g["symbol"] == "GOOD"
    assert g["n_fills"] == 5
    assert g["n_unfilled"] == 0
    assert g["fill_rate"] == 1.0

    assert g["median_slippage_pct"] == 0.0

    assert g["worst_fills"][0]["slippage_pct"] == -1.0
    assert g["verdict"] == "OK"


def test_too_loose_recommendation(tmp_path):


    rows = [_close_row("WIDE", trigger_mark=1.00, avg_fill_price=0.80) for _ in range(6)]
    path = _write(tmp_path, rows)
    rep = fq.build_report(path, min_fills=5)
    w = rep["by_symbol"][0]
    assert w["n_fills"] == 6
    assert w["median_slippage_pct"] == -20.0
    assert w["median_giveup_pct"] == 20.0
    assert w["verdict"] == "TOO_LOOSE"
    assert "TIGHTEN" in w["recommendation"]

    p = rep["portfolio"]
    assert p["verdict"] == "TOO_LOOSE"
    assert p["suggested_exit_slippage_floor"] <= fq.CURRENT_EXIT_SLIPPAGE_FLOOR
    assert p["suggested_exit_slippage_floor"] == pytest.approx(0.30, abs=0.02)


def test_too_tight_recommendation(tmp_path):

    rows = [_close_row("TIGHT", trigger_mark=1.00, avg_fill_price=1.00) for _ in range(2)]
    rows += [_unfilled_row("TIGHT") for _ in range(8)]
    path = _write(tmp_path, rows)
    rep = fq.build_report(path, min_fills=2)
    t = rep["by_symbol"][0]
    assert t["n_fills"] == 2
    assert t["n_unfilled"] == 8
    assert t["fill_rate"] == 0.2
    assert t["verdict"] == "TOO_TIGHT"
    assert "LOOSEN" in t["recommendation"]
    assert rep["portfolio"]["verdict"] == "TOO_TIGHT"


def test_empty_input(tmp_path):
    path = _write(tmp_path, [])
    rep = fq.build_report(path)
    assert rep["total_closes"] == 0
    assert rep["total_fills"] == 0
    assert rep["portfolio"]["verdict"] == "INSUFFICIENT"
    txt = fq.render_table(rep)
    assert "insufficient filled closes (n=0)" in txt


def test_only_no_trade_and_rejected_rows(tmp_path):

    rows = [
        {"schema": "trade_dataset.v2", "kind": "no_trade", "reason": "market_closed"},
        {"schema": "trade_dataset.v2", "kind": "rejected", "stage": "approval", "symbol": "SPY"},
    ]
    path = _write(tmp_path, rows)
    rep = fq.build_report(path)
    assert rep["total_closes"] == 0
    assert rep["portfolio"]["verdict"] == "INSUFFICIENT"
    assert "insufficient" in fq.render_table(rep)


def test_missing_file_does_not_crash():
    rep = fq.build_report("/nonexistent/path/trade_dataset.jsonl")
    assert rep["total_closes"] == 0
    assert fq.render_table(rep)


def test_insufficient_below_min_fills(tmp_path):

    rows = [_close_row("THIN", trigger_mark=1.00, avg_fill_price=1.00) for _ in range(3)]
    path = _write(tmp_path, rows)
    rep = fq.build_report(path, min_fills=5)
    s = rep["by_symbol"][0]
    assert s["n_fills"] == 3
    assert s["verdict"] == "INSUFFICIENT"
    assert "insufficient filled closes (n=3)" in s["recommendation"]


def test_json_mode_is_serializable(tmp_path):
    rows = [_close_row("SPY", trigger_mark=1.00, avg_fill_price=0.95) for _ in range(5)]
    path = _write(tmp_path, rows)
    rep = fq.build_report(path)
    blob = json.dumps(rep, default=str)
    back = json.loads(blob)
    assert back["by_symbol"][0]["symbol"] == "SPY"
    assert "portfolio" in back



def test_suggested_floor_helper_worked_example():


    assert fq._suggested_floor(10.0, 0.50) == 0.20
    assert fq._suggested_floor(10.0, 0.50) != 0.90

    assert fq._suggested_floor(80.0, 0.50) == 0.50
    assert fq._suggested_floor(0.0, 0.50) == 0.10

    assert fq._loosen_target(0.50) == 0.65
    assert fq._loosen_target(0.85) == 0.90


def test_too_loose_advice_matches_tune_and_prints_correct_floor(tmp_path):



    rows = [_close_row("WIDE", trigger_mark=1.00, avg_fill_price=0.80) for _ in range(6)]
    path = _write(tmp_path, rows)
    rep = fq.build_report(path, min_fills=5)

    w = rep["by_symbol"][0]
    assert w["verdict"] == "TOO_LOOSE"

    assert "0.30" in w["recommendation"]
    assert "0.80" not in w["recommendation"]
    assert "0.90" not in w["recommendation"]

    assert "TIGHTEN" in w["recommendation"]
    assert "lower EXIT_SLIPPAGE_FLOOR" in w["recommendation"]

    p = rep["portfolio"]
    assert p["verdict"] == "TOO_LOOSE"
    assert "0.30" in p["recommendation"]
    assert "TIGHTEN" in p["recommendation"]
    assert "lower EXIT_SLIPPAGE_FLOOR" in p["recommendation"]


    proposal = tef.recommend(rep, current_floor=0.50, min_fills=5)
    assert proposal["action"] == "RAISE"
    assert proposal["recommended_exit_slippage_floor"] == pytest.approx(0.30, abs=1e-9)
    assert proposal["recommended_exit_slippage_floor"] == p["suggested_exit_slippage_floor"]


def test_too_tight_advice_matches_tune_direction(tmp_path):


    rows = [_close_row("TIGHT", trigger_mark=1.00, avg_fill_price=1.00) for _ in range(5)]
    rows += [_unfilled_row("TIGHT") for _ in range(5)]
    path = _write(tmp_path, rows)
    rep = fq.build_report(path, min_fills=5)

    t = rep["by_symbol"][0]
    assert t["verdict"] == "TOO_TIGHT"
    assert "LOOSEN" in t["recommendation"]
    assert "raise EXIT_SLIPPAGE_FLOOR" in t["recommendation"]
    assert "0.65" in t["recommendation"]

    p = rep["portfolio"]
    assert p["verdict"] == "TOO_TIGHT"
    assert "raise EXIT_SLIPPAGE_FLOOR" in p["recommendation"]
    assert "0.65" in p["recommendation"]

    proposal = tef.recommend(rep, current_floor=0.50, min_fills=5)
    assert proposal["action"] == "LOWER"
    assert proposal["recommended_exit_slippage_floor"] == pytest.approx(0.65, abs=1e-9)


def test_current_floor_display_reflects_config(tmp_path):

    cfg = tmp_path / "config.yaml"
    cfg.write_text("rules:\n  exit_slippage_floor: 0.30\n")
    assert fq.current_exit_slippage_floor(str(cfg)) == 0.30

    rows = [_close_row("SPY", trigger_mark=1.00, avg_fill_price=1.00) for _ in range(5)]
    path = _write(tmp_path, rows)
    rep = fq.build_report(path, min_fills=5, config_path=str(cfg))
    assert rep["portfolio"]["current_exit_slippage_floor"] == 0.30
    txt = fq.render_table(rep)
    assert "current 0.3" in txt


    assert fq.current_exit_slippage_floor(str(tmp_path / "nope.yaml")) == 0.50
