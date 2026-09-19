"""Public API contract; production-derived narrative omitted."""
import importlib.util
import os

import pytest

from exitmgr.risk import (
    RiskLimits, ProposedTrade, evaluate_trade, conviction_multiplier,
)


_HARNESS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "calibrate_conviction_sizing.py")
_spec = importlib.util.spec_from_file_location("calibrate_conviction_sizing", _HARNESS)
cc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cc)


POT = 1000.0
NAMES = {"MUTX", "NVDA", "AAPL"}


def gate_eval(trade, limits, net_liq=POT, available=10_000.0, open_pos=None):
    return evaluate_trade(
        trade, net_liq=net_liq, available_funds=available,
        open_positions=open_pos or [], pot_day_start=net_liq,
        approved_names=NAMES, limits=limits,
    )



def _synth(specs):
    return cc._synth(specs)


def test_high_conviction_outperforms_gets_upsize():

    trades = _synth({9: (15, 0.80, 30.0), 3: (15, 0.20, -30.0)})
    bm, cm, info = cc.derive_multipliers(trades, soft_pct=0.12, hard_pct=0.25)
    assert info["gate_verdict"] == "PASS", info
    assert bm["high (8-10)"] > 1.0, bm
    assert bm["high (8-10)"] <= info["max_up_multiplier"] + 1e-9

    assert cm[9] == bm["high (8-10)"] and cm[10] == bm["high (8-10)"]


def test_losing_bucket_is_downsized():
    trades = _synth({9: (15, 0.80, 30.0), 3: (15, 0.20, -30.0)})
    bm, cm, info = cc.derive_multipliers(trades, soft_pct=0.12, hard_pct=0.25)
    assert bm["low (1-4)"] < 1.0, bm
    assert bm["low (1-4)"] >= cc.MIN_MULT - 1e-9
    assert cm[1] == bm["low (1-4)"] and cm[4] == bm["low (1-4)"]


def test_thin_data_gated_to_flat():

    trades = _synth({9: (2, 1.0, 30.0), 3: (2, 0.0, -30.0)})
    bm, cm, info = cc.derive_multipliers(trades, soft_pct=0.12, hard_pct=0.25)
    assert info["gate_verdict"] == "INSUFFICIENT", info
    assert all(v == 1.0 for v in bm.values()), bm
    assert all(v == 1.0 for v in cm.values()), cm


def test_no_edge_bucket_stays_flat():

    trades = _synth({9: (15, 0.50, 0.0), 3: (15, 0.50, 0.0)})
    bm, cm, info = cc.derive_multipliers(trades, soft_pct=0.12, hard_pct=0.25)
    assert info["gate_verdict"] in ("FAIL", "INSUFFICIENT"), info
    assert all(v == 1.0 for v in bm.values()), bm


def test_upsize_never_proposes_above_hard_ceiling():

    trades = _synth({9: (20, 1.0, 90.0), 3: (20, 0.0, -30.0)})
    bm, cm, info = cc.derive_multipliers(trades, soft_pct=0.12, hard_pct=0.25)
    max_up = 0.25 / 0.12
    assert bm["high (8-10)"] <= max_up + 1e-9, bm

    assert 0.12 * bm["high (8-10)"] <= 0.25 + 1e-9


def test_no_upsize_headroom_when_soft_equals_hard():

    trades = _synth({9: (15, 0.80, 30.0), 3: (15, 0.20, -30.0)})
    bm, cm, info = cc.derive_multipliers(trades, soft_pct=0.25, hard_pct=0.25)
    assert info["max_up_multiplier"] == 1.0
    assert bm["high (8-10)"] == 1.0, bm
    assert bm["low (1-4)"] < 1.0, bm



def test_conviction_multiplier_helper_defaults_flat():
    assert conviction_multiplier(9, None) == 1.0
    assert conviction_multiplier(9, {}) == 1.0
    m = {8: 1.5, 9: 1.5, 10: 1.5, 1: 0.5}
    assert conviction_multiplier(9, m) == 1.5
    assert conviction_multiplier(1, m) == 0.5

    assert conviction_multiplier(5, m) == 1.0
    assert conviction_multiplier(0, m) == 0.5
    assert conviction_multiplier(99, m) == 1.5


def test_empty_map_is_byte_identical_to_today():

    base = RiskLimits(max_trade_pct=0.12, max_trade_pct_hard=0.25)
    withmap = RiskLimits(max_trade_pct=0.12, max_trade_pct_hard=0.25,
                         conviction_size_multipliers={})
    t = ProposedTrade("MUTX", 100.0, False, conviction=9)
    d0 = gate_eval(t, base)
    d1 = gate_eval(t, withmap)
    assert d0.per_trade_cap == d1.per_trade_cap

    assert RiskLimits().conviction_size_multipliers is None
    d2 = gate_eval(t, RiskLimits(max_trade_pct=0.12, max_trade_pct_hard=0.25,
                                 conviction_size_multipliers=None))
    assert d0.per_trade_cap == d2.per_trade_cap


def test_config_multiplier_scales_risk_sizing():

    lim = RiskLimits(max_trade_pct=0.12, max_trade_pct_hard=0.25, allow_any_name=True,
                     conviction_size_multipliers={c: (1.5 if c >= 8 else 1.0) for c in range(1, 11)})
    d = gate_eval(ProposedTrade("MUTX", 100.0, False, conviction=9), lim)
    assert abs(d.per_trade_cap - 0.18 * POT) < 1e-6, d.per_trade_cap

    lim2 = RiskLimits(max_trade_pct=0.12, max_trade_pct_hard=0.25, allow_any_name=True,
                      conviction_size_multipliers={c: (0.5 if c <= 4 else 1.0) for c in range(1, 11)})
    d2 = gate_eval(ProposedTrade("MUTX", 100.0, False, conviction=2), lim2)
    assert abs(d2.per_trade_cap - 0.06 * POT) < 1e-6, d2.per_trade_cap


def test_multiplier_still_clamped_by_hard_ceiling_in_risk():

    lim = RiskLimits(max_trade_pct=0.12, max_trade_pct_hard=0.25, allow_any_name=True,
                     conviction_size_multipliers={c: 10.0 for c in range(1, 11)})
    d = gate_eval(ProposedTrade("MUTX", 100.0, False, conviction=9), lim)
    assert d.per_trade_cap <= 0.25 * POT + 1e-6


def test_multiplier_does_not_relax_any_gate():


    lim = RiskLimits(max_trade_pct=0.12, max_trade_pct_hard=0.25, allow_any_name=False,
                     conviction_size_multipliers={c: 1.5 for c in range(1, 11)})
    d = gate_eval(ProposedTrade("ZZZZ", 10.0, False, conviction=9), lim)
    assert not d.approved
    assert any("allowed universe" in r for r in d.reasons)



def test_loader_skips_open_and_partial(tmp_path):
    import json
    p = tmp_path / "ds.jsonl"
    rows = [
        {"kind": "trade", "entry": {"conviction": 9}, "close": {"realized_pnl_pct": 25.0},
         "lifecycle": {"mfe_pct": 40.0, "mae_pct": -10.0}, "labels": {"win": True}},
        {"kind": "trade", "entry": {"conviction": 3}, "close": {"realized_pnl_pct": None}},
        {"kind": "trade", "entry": {"conviction": 7}, "close": {"realized_pnl_pct": 5.0, "partial": True}},
        {"kind": "no_trade", "reason": "model_no_trade"},
        {"kind": "rejected", "symbol": "MUTX", "stage": "risk_gate"},

        {"kind": "trade", "entry": {}, "decision": {"chosen": {"conviction": 6}},
         "close": {"realized_pnl_pct": 12.0}, "lifecycle": {}, "labels": {}},
    ]
    for row in rows:
        if row.get("kind") == "trade":
            row.update({"record_status": "CANONICAL", "canonical": True,
                        "usable_for_training": bool(row.get("decision")),
                        "usable_for_pnl": True})
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    trades = cc.load_closed_trades(str(p))
    assert len(trades) == 2, trades
    convs = sorted(t["conviction"] for t in trades)
    assert convs == [6, 9]


def test_loader_rejects_unmarked_trade(tmp_path):
    import json
    path = tmp_path / "legacy.jsonl"
    path.write_text(json.dumps({"kind": "trade", "entry": {"conviction": 9},
                                "close": {"realized_pnl_pct": 25.0},
                                "labels": {"win": True}}) + "\n")
    assert cc.load_closed_trades(str(path)) == []
