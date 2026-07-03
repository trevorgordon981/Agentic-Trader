"""Tests for fill_quality_report.py -- the v2 close-record fill-quality consumer.

Synthetic v2 close rows exercise:
  * per-symbol aggregation math (fill-rate, median/p90 slippage, give-up, worst fills)
  * the too-tight (fills resting/unfilled) vs too-loose (crossing too far) recommendation logic
  * the empty / too-few-rows path (must say "insufficient", never crash)
  * H4: the human-facing advice strings print the CORRECT floor value + direction (not the old
    inverted `1 - giveup` = 0.90), and the current-floor display reflects live config (no stale 0.50)
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fill_quality_report as fq
import tune_exit_floor as tef  # cross-tool consistency: advice strings must AGREE with the tuner


# --------------------------------------------------------------------------- row factories
def _close_row(symbol, *, trigger_mark, avg_fill_price, fill_status="Filled",
               rule_fired="stop", is_spread=False, close_qty=1, partial=False):
    """Build a v2 kind=='trade' row, computing slippage the SAME way the logger does
    (slippage_per_share = fill - trigger_mark; slippage_pct = slip/|mark|*100)."""
    slip = None
    slip_pct = None
    if avg_fill_price is not None and trigger_mark is not None:
        slip = round(float(avg_fill_price) - float(trigger_mark), 4)
        if float(trigger_mark) != 0:
            slip_pct = round(slip / abs(float(trigger_mark)) * 100, 2)
    return {
        "schema": "trade_dataset.v2",
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
    """A close that never filled (rested / cancelled): no avg_fill_price, no slippage."""
    return _close_row(symbol, trigger_mark=trigger_mark, avg_fill_price=None,
                      fill_status=fill_status)


def _write(tmp_path, rows):
    p = tmp_path / "trade_dataset.jsonl"
    with open(p, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return str(p)


# --------------------------------------------------------------------------- aggregation math
def test_per_symbol_aggregation_math(tmp_path):
    # narrow book: fills essentially AT the mark (tiny favorable/adverse), all filled.
    # slippage_pct here: fill 1.01 vs mark 1.00 => +1.0% ; 0.99 => -1.0% ; 1.00 => 0
    rows = [
        _close_row("GOOD", trigger_mark=1.00, avg_fill_price=1.01),
        _close_row("GOOD", trigger_mark=1.00, avg_fill_price=0.99),
        _close_row("GOOD", trigger_mark=1.00, avg_fill_price=1.00),
        _close_row("GOOD", trigger_mark=2.00, avg_fill_price=2.00),
        _close_row("GOOD", trigger_mark=2.00, avg_fill_price=2.02),  # +1.0%
    ]
    path = _write(tmp_path, rows)
    rep = fq.build_report(path, min_fills=5)
    g = rep["by_symbol"][0]
    assert g["symbol"] == "GOOD"
    assert g["n_fills"] == 5
    assert g["n_unfilled"] == 0
    assert g["fill_rate"] == 1.0
    # median of [-1.0, 0, 1.0, 0, 1.0] sorted [-1,0,0,1,1] => 0.0
    assert g["median_slippage_pct"] == 0.0
    # worst fill = most negative slippage_pct (-1.0%)
    assert g["worst_fills"][0]["slippage_pct"] == -1.0
    assert g["verdict"] == "OK"


def test_too_loose_recommendation(tmp_path):
    # WIDE book: every SELL fills well BELOW the mark => large adverse give-up => TOO_LOOSE.
    # mark 1.00, fills ~0.80 => -20% slippage (give-up 20%).
    rows = [_close_row("WIDE", trigger_mark=1.00, avg_fill_price=0.80) for _ in range(6)]
    path = _write(tmp_path, rows)
    rep = fq.build_report(path, min_fills=5)
    w = rep["by_symbol"][0]
    assert w["n_fills"] == 6
    assert w["median_slippage_pct"] == -20.0
    assert w["median_giveup_pct"] == 20.0
    assert w["verdict"] == "TOO_LOOSE"
    assert "TIGHTEN" in w["recommendation"]
    # portfolio should also flag too loose and suggest a TIGHTER floor than 0.50
    p = rep["portfolio"]
    assert p["verdict"] == "TOO_LOOSE"
    assert p["suggested_exit_slippage_floor"] <= fq.CURRENT_EXIT_SLIPPAGE_FLOOR
    assert p["suggested_exit_slippage_floor"] == pytest.approx(0.30, abs=0.02)  # 20% + 10% margin


def test_too_tight_recommendation(tmp_path):
    # Mostly UNFILLED (resting/cancelled) with a couple of clean fills => low fill-rate => TOO_TIGHT.
    rows = [_close_row("TIGHT", trigger_mark=1.00, avg_fill_price=1.00) for _ in range(2)]
    rows += [_unfilled_row("TIGHT") for _ in range(8)]  # fill-rate = 2/10 = 20%
    path = _write(tmp_path, rows)
    rep = fq.build_report(path, min_fills=2)  # allow the 2 fills to count
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
    # the live-right-now shape: no kind=='trade' rows at all -> must not crash, insufficient.
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
    assert fq.render_table(rep)  # renders without raising


def test_insufficient_below_min_fills(tmp_path):
    # a few good fills but under MIN_FILLS => per-symbol INSUFFICIENT, no false OK/loose/tight.
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
    blob = json.dumps(rep, default=str)  # must round-trip
    back = json.loads(blob)
    assert back["by_symbol"][0]["symbol"] == "SPY"
    assert "portfolio" in back


# --------------------------------------------------------------------------- H4: advice-string correctness
def test_suggested_floor_helper_worked_example():
    # THE canonical worked example: 10% give-up => floor 0.20 (giveup/100 + 0.10 margin),
    # NOT the old inverted 0.90 (= 1 - 0.10) the advice string used to print.
    assert fq._suggested_floor(10.0, 0.50) == 0.20
    assert fq._suggested_floor(10.0, 0.50) != 0.90
    # only ever tightens: never proposes a fraction ABOVE the current floor, never below FLOOR_MIN.
    assert fq._suggested_floor(80.0, 0.50) == 0.50   # capped at current
    assert fq._suggested_floor(0.0, 0.50) == 0.10    # floored at FLOOR_MIN
    # loosen target matches tune's current + LOOSEN_STEP, capped at FLOOR_MAX.
    assert fq._loosen_target(0.50) == 0.65
    assert fq._loosen_target(0.85) == 0.90


def test_too_loose_advice_matches_tune_and_prints_correct_floor(tmp_path):
    # 20% give-up on every fill => TOO_LOOSE; correct tightened floor = 0.20 + 0.10 = 0.30
    # (the old code printed the inverted 1 - 0.20 = 0.80). Assert value + direction, and that the
    # per-symbol AND portfolio strings AGREE with what tune_exit_floor would stage on this data.
    rows = [_close_row("WIDE", trigger_mark=1.00, avg_fill_price=0.80) for _ in range(6)]
    path = _write(tmp_path, rows)
    rep = fq.build_report(path, min_fills=5)

    w = rep["by_symbol"][0]
    assert w["verdict"] == "TOO_LOOSE"
    # correct value present; the inverted 0.80/0.90 must be ABSENT.
    assert "0.30" in w["recommendation"]
    assert "0.80" not in w["recommendation"]
    assert "0.90" not in w["recommendation"]
    # correct direction: TIGHTEN by LOWERING the fraction (not "raise ... 0.90").
    assert "TIGHTEN" in w["recommendation"]
    assert "lower EXIT_SLIPPAGE_FLOOR" in w["recommendation"]

    p = rep["portfolio"]
    assert p["verdict"] == "TOO_LOOSE"
    assert "0.30" in p["recommendation"]
    assert "TIGHTEN" in p["recommendation"]
    assert "lower EXIT_SLIPPAGE_FLOOR" in p["recommendation"]

    # tune_exit_floor, fed the SAME report, must stage the SAME floor + RAISE-the-price direction.
    proposal = tef.recommend(rep, current_floor=0.50, min_fills=5)
    assert proposal["action"] == "RAISE"                       # raise the floor PRICE line
    assert proposal["recommended_exit_slippage_floor"] == pytest.approx(0.30, abs=1e-9)
    assert proposal["recommended_exit_slippage_floor"] == p["suggested_exit_slippage_floor"]


def test_too_tight_advice_matches_tune_direction(tmp_path):
    # low fill-rate => TOO_TIGHT; loosen = RAISE the fraction toward 0.50 + 0.15 step = 0.65,
    # matching tune_exit_floor (which LOWERS the price line by raising the fraction).
    rows = [_close_row("TIGHT", trigger_mark=1.00, avg_fill_price=1.00) for _ in range(5)]
    rows += [_unfilled_row("TIGHT") for _ in range(5)]  # fill-rate = 5/10 = 50% < 80%
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
    assert proposal["action"] == "LOWER"   # lower the floor PRICE == raise the fraction
    assert proposal["recommended_exit_slippage_floor"] == pytest.approx(0.65, abs=1e-9)


def test_current_floor_display_reflects_config(tmp_path):
    # A NON-default config floor must show through the report + display -- no stale 0.50 hardcode.
    cfg = tmp_path / "config.yaml"
    cfg.write_text("rules:\n  exit_slippage_floor: 0.30\n")
    assert fq.current_exit_slippage_floor(str(cfg)) == 0.30

    rows = [_close_row("SPY", trigger_mark=1.00, avg_fill_price=1.00) for _ in range(5)]
    path = _write(tmp_path, rows)
    rep = fq.build_report(path, min_fills=5, config_path=str(cfg))
    assert rep["portfolio"]["current_exit_slippage_floor"] == 0.30
    txt = fq.render_table(rep)
    assert "current 0.3" in txt        # display no longer stuck at 0.50

    # a missing/broken config falls back to the order.py default, never crashes.
    assert fq.current_exit_slippage_floor(str(tmp_path / "nope.yaml")) == 0.50
