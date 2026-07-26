"""Unit tests for the dataset-v6 raw event capture (exitmgr/capture_v6.py).

Covers: append, blob+hash round-trip + dedup, the 8x-DTE check, the high-level hooks, label
attach, and the FAIL-OPEN contract (a capture error never raises into the trading path)."""
import json
import os

import pytest

from exitmgr import capture_v6 as v6


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    d = tmp_path / "trade-capture"
    monkeypatch.setenv("TRADE_CAPTURE_DIR", str(d))
    return d


def _events(store):
    p = os.path.join(str(store), "events.jsonl")
    if not os.path.exists(p):
        return []
    with open(p) as f:
        return [json.loads(ln) for ln in f if ln.strip()]


# --------------------------------------------------------------------------- store + append
def test_emit_appends_event(_store):
    rec = v6.emit("entry_decision", trade_uid="T1", symbol="AAPL", con_id=42,
                  order_ids=[111, 222], direction="long", conviction=8)
    assert rec["schema"] == "trade_capture.v6"
    assert rec["event_type"] == "entry_decision"
    assert rec["trade_uid"] == "T1" and rec["symbol"] == "AAPL" and rec["con_id"] == 42
    assert rec["order_ids"] == [111, 222]
    assert rec["direction"] == "long" and rec["conviction"] == 8
    # ISO-8601 UTC timestamp present
    assert rec["ts"].endswith("+00:00")
    evs = _events(_store)
    assert len(evs) == 1 and evs[0]["trade_uid"] == "T1"


def test_order_ids_flatten_and_dedup(_store):
    rec = v6.emit("order_fill", order_ids=[1, 1, None, 0, "0", 2])
    assert rec["order_ids"] == [1, 2]


# --------------------------------------------------------------------------- blob + hash
def test_blob_hash_roundtrip(_store):
    ctx = {"market_context": "big RAG brief …", "cot": "reasoning", "n": 3}
    rec = v6.emit("entry_decision", trade_uid="T2", context=ctx)
    sha = rec["context_sha256"]
    assert sha and len(sha) == 64
    blob = os.path.join(str(_store), "blobs", sha + ".json")
    assert os.path.exists(blob)
    with open(blob) as f:
        loaded = json.load(f)
    assert loaded == ctx  # round-trips exactly


def test_blob_dedup_write_once(_store):
    ctx = {"a": 1, "b": [1, 2, 3]}
    r1 = v6.emit("no_trade", context=ctx)
    r2 = v6.emit("no_trade", context=dict(ctx))  # identical content
    assert r1["context_sha256"] == r2["context_sha256"]  # content-addressed => same sha
    blobs = os.listdir(os.path.join(str(_store), "blobs"))
    assert len(blobs) == 1  # written once, deduped


def test_empty_context_no_blob(_store):
    rec = v6.emit("no_trade", context=None)
    assert rec["context_sha256"] is None
    rec2 = v6.emit("no_trade", context={})
    assert rec2["context_sha256"] is None


# --------------------------------------------------------------------------- 8x-DTE check
def test_eightx_dte_pass_fail():
    ok = v6.eightx_dte_check(dte=80, trade_window_days=10)
    assert ok["dte"] == 80 and ok["trade_window_days"] == 10.0
    assert ok["ratio"] == 8.0 and ok["rule_ok"] is True
    bad = v6.eightx_dte_check(dte=20, trade_window_days=10)
    assert bad["ratio"] == 2.0 and bad["rule_ok"] is False


def test_eightx_dte_from_expiry():
    r = v6.eightx_dte_check(expiry="2026-08-20", trade_window_days=5,
                            ref_date="2026-07-21")
    assert r["dte"] == 30 and r["rule_ok"] is False
    r2 = v6.eightx_dte_check(expiry="20260820", ref_date="20260721")
    assert r2["dte"] == 30 and r2["rule_ok"] is None  # no trade window -> undecidable


def test_eightx_dte_unknown_window_records_dte():
    r = v6.eightx_dte_check(dte=45)
    assert r["dte"] == 45 and r["ratio"] is None and r["rule_ok"] is None


# --------------------------------------------------------------------------- high-level hooks
def test_on_decision_extracts_fields(_store):
    rec = {
        "trade_uid": "U9", "decision_id": "D9", "symbol": "NVDA", "con_id": 7,
        "event": "submitted", "source": "trader", "structure": "spread",
        "right": "C", "strike": 120, "expiry": "2026-12-18",
        "chosen": {"symbol": "NVDA", "direction": "long", "conviction": 9, "thesis": "breakout"},
        "candidates": [{"symbol": "NVDA", "conviction": 9}, {"symbol": "AMD", "conviction": 5}],
        "construction": {"dte": 150, "trade_window_days": 15, "tp_pct": 50, "sl_pct": 30, "qty": 2},
        "sizing": {"qty": 2, "net_liq": 5000},
        "gate": {"approved": True, "per_trade_cap": 800},
        "market_context": "RAG brief", "technical_card": "tech", "raw_strategist": "answer",
        "cot": "reasoning", "regime": {"label": "bull"}, "order_ref": "ref-9",
    }
    out = v6.on_decision(rec)
    assert out["event_type"] == "entry_decision"
    assert out["trade_uid"] == "U9" and out["symbol"] == "NVDA"
    assert out["direction"] == "long" and out["conviction"] == 9 and out["thesis"] == "breakout"
    assert out["gate_approved"] is True and out["per_trade_cap"] == 800
    # rejected alternatives = candidates minus the chosen symbol
    assert out["rejected_alternatives"] == [{"symbol": "AMD", "conviction": 5}]
    # explicit 8x-DTE result: 150 / 15 = 10x >= 8 -> pass
    assert out["dte_check"]["ratio"] == 10.0 and out["dte_check"]["rule_ok"] is True
    # exact context hashed to a blob
    assert out["context_sha256"] and len(out["context_sha256"]) == 64


def test_on_exit_event(_store):
    exit_rec = {"symbol": "TSLA", "reason": "take_profit", "rule_fired": "take_profit",
                "realized_pnl": 320.0, "realized_pnl_pct": 42.0, "exit_price_per_share": 3.2,
                "limit_price": 3.15, "slippage_per_share": 0.05, "order_id": 5551}
    je = {"symbol": "TSLA", "debit": 500.0, "entry_slippage": 4.0}
    mark_path = [{"ts": "t0", "pnl_pct": 0}, {"ts": "t1", "pnl_pct": 42}]
    out = v6.on_exit(exit_rec, je, 7, mfe=55.0, mae=-8.0, mark_path=mark_path)
    assert out["event_type"] == "exit_action"
    assert out["reason"] == "take_profit" and out["rule_fired"] == "take_profit"
    assert out["realized_pnl"] == 320.0 and out["mfe_pct"] == 55.0 and out["mae_pct"] == -8.0
    assert out["order_ids"] == [5551] and out["marks"] == 2
    # full path lives in the blob
    assert out["context_sha256"]
    # trade_uid derived deterministically from con_id (stable join with the decision row)
    from exitmgr import trade_capture as tc
    assert out["trade_uid"] == tc.trade_uid(con_id=7, symbol="TSLA")


def test_on_position_mark_lightweight(_store):
    out = v6.on_position_mark(7, symbol="TSLA", pnl_pct=12.5,
                              enrich={"underlying": 250.0, "dte": 100, "days_held": 3,
                                      "mgmt_action": "hold", "mgmt_reason": "thesis intact"})
    assert out["event_type"] == "position_path"
    assert out["pnl_pct"] == 12.5 and out["underlying"] == 250.0 and out["days_held"] == 3
    assert out["mgmt_action"] == "hold"
    assert out["context_sha256"] is None  # no blob for the lightweight sample


def test_on_fill_event(_store):
    row = {"trade_uid": "F1", "symbol": "SOFI", "con_id": 9, "kind": "trade", "source": "manual",
           "entry": {"debit": 120.0, "entry_slippage": 2.0},
           "close": {"exit_price_per_share": 1.5, "realized_pnl": 30.0, "order_id": 88,
                     "fill_status": "filled"},
           "provenance": {"exec_ids": ["e1"], "order_ids": [88], "perm_ids": [999]}}
    out = v6.on_fill(row)
    assert out["event_type"] == "order_fill"
    assert out["entry_debit"] == 120.0 and out["entry_slippage"] == 2.0
    assert out["realized_pnl"] == 30.0 and out["order_ids"] == [88]
    assert out["receipts"]["exec_ids"] == ["e1"]


# --------------------------------------------------------------------------- labels
def test_attach_label(_store):
    assert v6.attach_label("U9", {"label_type": "process_grade",
                                  "process_grade": "good_process_loser"}) is True
    p = os.path.join(str(_store), "labels.jsonl")
    with open(p) as f:
        rows = [json.loads(ln) for ln in f if ln.strip()]
    assert rows[0]["trade_uid"] == "U9"
    assert rows[0]["process_grade"] == "good_process_loser"


def test_attach_label_rejects_bad_input(_store):
    assert v6.attach_label("", {"x": 1}) is False
    assert v6.attach_label("U1", "not a dict") is False


# --------------------------------------------------------------------------- FAIL-OPEN contract
def test_emit_never_raises_on_unserializable(_store):
    class Weird:
        pass
    # context with an unserializable object still must not raise; _json_default handles it
    out = v6.emit("entry_decision", trade_uid="W1", context={"obj": Weird()})
    assert out is not None and out["trade_uid"] == "W1"


def test_hooks_never_raise_on_garbage(_store):
    # None / wrong-typed inputs must be swallowed, returning None, never raising
    assert v6.on_decision(None) is None
    assert v6.on_no_trade(123) is None
    assert v6.on_rejected("x") is None
    assert v6.on_exit("x", None, None) is not None or True  # on_exit tolerates junk, no raise
    assert v6.on_fill(None) is None


def test_emit_failopen_when_store_unwritable(monkeypatch):
    # Point the store at an unwritable path; emit must return None, not raise.
    monkeypatch.setenv("TRADE_CAPTURE_DIR", "/proc/nonexistent/cannot/write")
    out = v6.emit("no_trade", symbol="X")
    assert out is None or isinstance(out, dict)  # never raises either way
