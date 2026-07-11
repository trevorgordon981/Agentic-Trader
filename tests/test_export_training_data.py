"""Tests for the standalone training-data exporter (export_training_data.py).

Builds a synthetic dataset dir with:
  * a closed WINNING trade with a full embedded decision + review,
  * a closed LOSING trade whose review is MISSING (degrades gracefully),
  * a no_trade row,
  * a rejected row,
  * one MALFORMED line (must be skipped, not crash).

Asserts: both output formats produce correctly joined examples; the missing-review case notes
completeness; the malformed line is skipped; no_trade/rejected are flagged negative; output is
deterministic across runs.
"""
import importlib.util
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
_MOD_PATH = os.path.join(os.path.dirname(HERE), "export_training_data.py")
_spec = importlib.util.spec_from_file_location("export_training_data", _MOD_PATH)
etd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(etd)


def _write_dataset(tmpdir):
    ds = os.path.join(tmpdir, "trade_dataset.jsonl")
    dc = os.path.join(tmpdir, "decision_context.jsonl")
    rv = os.path.join(tmpdir, "reviews.jsonl")

    winner = {
        "schema": "trade_dataset.v2", "kind": "trade", "con_id": 111, "symbol": "AAPL",
        "decision": {
            "schema": "decision_context.v2", "kind": "decision", "source": "trader",
            "symbol": "AAPL", "regime": {"regime": "bull", "vix": 14.0},
            "market_context": "AAPL breakout brief", "technical_card": "RSI 61, above 20DMA",
            "candidates": [{"underlying": "AAPL", "conviction": 7}, {"underlying": "MSFT", "conviction": 4}],
            "raw_strategist": "Take AAPL calls, momentum + earnings drift.",
            "chosen": {"underlying": "AAPL", "conviction": 7, "direction": "bullish"},
            "gate": {"approved": True, "reasons": [], "bound_caps": [], "per_trade_cap": 500.0},
            "construction": {"dte_adjusted": True}, "sizing": {"qty": 2},
        },
        "entry": {
            "ts": "2026-07-01T14:30:00+00:00", "symbol": "AAPL", "right": "C", "strike": 200.0,
            "expiry": "20260718", "structure": "long call", "quantity": 2, "debit": 300.0,
            "dte_at_entry": 17, "profit_target_pct": 40.0, "stop_pct": 50.0, "conviction": 7,
            "thesis": "momentum", "entry_delta": 0.4, "entry_iv": 0.28,
        },
        "lifecycle": {"mark_path": [], "marks": 3, "mfe_pct": 55.0, "mae_pct": -8.0,
                      "drawdown_from_peak_pct": 15.0},
        "close": {
            "ts": "2026-07-02T18:00:00+00:00", "reason": "profit_target", "rule_fired": "tp",
            "exit_reasoning": "hit TP", "realized_pnl": 120.0, "realized_pnl_pct": 40.0,
            "holding_days": 1, "fill_status": "filled", "avg_fill_price": 4.2,
            "trigger_mark": 4.2, "slippage_per_share": 0.0, "slippage_pct": 0.0,
            "tp_hit": True, "sl_hit": False, "partial": False,
        },
        "labels": {"outcome": "win", "win": True, "round_trip": False},
        "review": {"schema": "trade_review.v1", "symbol": "AAPL", "con_id": 111,
                   "date": "2026-07-01", "review": "Good entry, sized right."},
    }

    # loser: review is None (missing); must degrade gracefully
    loser = {
        "schema": "trade_dataset.v2", "kind": "trade", "con_id": 222, "symbol": "TSLA",
        "decision": {
            "schema": "decision_context.v2", "kind": "decision", "source": "trader",
            "symbol": "TSLA", "regime": {"regime": "neutral"},
            "market_context": "TSLA chop", "candidates": [{"underlying": "TSLA", "conviction": 5}],
            "raw_strategist": "TSLA puts on breakdown.", "chosen": {"underlying": "TSLA", "conviction": 5},
            "gate": {"approved": True, "reasons": [], "bound_caps": [], "per_trade_cap": 300.0},
        },
        "entry": {
            "ts": "2026-07-01T15:00:00+00:00", "symbol": "TSLA", "right": "P", "strike": 250.0,
            "expiry": "20260718", "structure": "long put", "quantity": 1, "debit": 250.0,
            "dte_at_entry": 17, "profit_target_pct": 40.0, "stop_pct": 50.0, "conviction": 5,
        },
        "lifecycle": {"mark_path": [], "marks": 2, "mfe_pct": 5.0, "mae_pct": -60.0},
        "close": {
            "ts": "2026-07-02T19:00:00+00:00", "reason": "stop_loss", "rule_fired": "sl",
            "realized_pnl": -140.0, "realized_pnl_pct": -56.0, "holding_days": 1,
            "fill_status": "filled", "tp_hit": False, "sl_hit": True, "partial": False,
        },
        "labels": {"outcome": "loss", "win": False, "round_trip": False},
        "review": None,
    }

    no_trade = {
        "schema": "trade_dataset.v2", "kind": "no_trade", "ts": "2026-07-01T13:00:00+00:00",
        "source": "daily_slate", "reason": "model_no_trade",
        "raw_strategist": "Nothing clears the bar today.",
        "candidates": [{"underlying": "SPY", "conviction": 3}], "regime": {"regime": "risk_off"},
        "market_context": "choppy tape",
    }

    rejected = {
        "schema": "trade_dataset.v2", "kind": "rejected", "ts": "2026-07-01T13:30:00+00:00",
        "source": "trader", "stage": "risk_gate", "reason": ["per_trade_cap exceeded"],
        "symbol": "NVDA", "right": "C", "strike": 1200.0, "expiry": "20260718",
        "structure": "long call", "order": "BUY 1x NVDA 20260718 1200C",
        "idea": {"underlying": "NVDA", "conviction": 6}, "regime": {"regime": "bull"},
        "gate": {"approved": False, "reasons": ["cap"], "bound_caps": ["cap"], "per_trade_cap": 0.0},
    }

    for row in (winner, loser):
        row.update({"record_status": "CANONICAL", "canonical": True,
                    "usable_for_training": True, "usable_for_pnl": True})
    for row in (no_trade, rejected):
        row.update({"record_status": "CANONICAL", "canonical": True,
                    "usable_for_training": True, "usable_for_pnl": False,
                    "not_for_pnl_reason": "no realized outcome"})

    with open(ds, "w") as f:
        f.write(json.dumps(winner) + "\n")
        f.write("{this is not valid json,,,\n")  # MALFORMED line
        f.write(json.dumps(loser) + "\n")
        f.write(json.dumps(no_trade) + "\n")
        f.write(json.dumps(rejected) + "\n")

    # sidecars: winner's review lives here too (embedded already), loser has none.
    with open(dc, "w") as f:
        f.write(json.dumps(winner["decision"] | {"con_id": 111}) + "\n")
    with open(rv, "w") as f:
        f.write(json.dumps(winner["review"]) + "\n")
    return ds


def test_flat_format_joins_and_labels(tmp_path):
    _write_dataset(str(tmp_path))
    out = str(tmp_path / "flat.jsonl")
    counts = etd.export(str(tmp_path), out, "jsonl-flat", include_open=False, min_realized=None)

    assert counts["trade"] == 2
    assert counts["no_trade"] == 1
    assert counts["rejected"] == 1
    assert counts["malformed"] == 1  # the bad line skipped, not crashed
    assert counts["emitted"] == 4

    rows = [json.loads(l) for l in open(out)]
    by_sym = {r.get("symbol"): r for r in rows if r.get("example_kind") == "trade"}

    # winner: full join, win label, review present
    w = by_sym["AAPL"]
    assert w["example_kind"] == "trade" and w["is_negative"] is False
    assert w["input"]["chosen"]["conviction"] == 7
    assert w["input"]["raw_strategist"].startswith("Take AAPL")
    assert w["label"]["outcome"] == "win"
    assert w["label"]["realized_pnl_pct"] == 40.0
    assert w["label"]["mfe_pct"] == 55.0 and w["label"]["mae_pct"] == -8.0
    assert w["label"]["scaled_out"] is False
    assert w["order"]["structure"] == "long call"
    assert w["review"]["review"].startswith("Good entry")
    assert w["completeness"]["has_decision"] is True
    assert w["completeness"]["has_review"] is True
    assert w["completeness"]["decision_source"] == "embedded"

    # loser: missing review degrades gracefully with completeness noting it
    l = by_sym["TSLA"]
    assert l["label"]["outcome"] == "loss"
    assert l["review"] is None
    assert l["completeness"]["has_review"] is False
    assert l["completeness"]["review_source"] is None
    assert l["completeness"]["has_decision"] is True

    # negatives flagged distinctly
    nt = [r for r in rows if r["example_kind"] == "no_trade"][0]
    assert nt["is_negative"] is True and nt["negative_type"] == "abstain"
    assert nt["input"]["raw_strategist"].startswith("Nothing clears")
    rj = [r for r in rows if r["example_kind"] == "rejected"][0]
    assert rj["is_negative"] is True and rj["negative_type"] == "rejected"
    assert rj["stage"] == "risk_gate"
    assert rj["symbol"] == "NVDA"

    # win rate over decided trades
    assert counts["wins"] == 1 and counts["losses"] == 1


def test_chat_format_shape(tmp_path):
    _write_dataset(str(tmp_path))
    out = str(tmp_path / "chat.jsonl")
    etd.export(str(tmp_path), out, "chat", include_open=False, min_realized=None)
    rows = [json.loads(l) for l in open(out)]

    for r in rows:
        assert "messages" in r and "metadata" in r
        roles = [m["role"] for m in r["messages"]]
        assert roles == ["system", "user", "assistant"]

    trades = [r for r in rows if r["metadata"]["example_kind"] == "trade"]
    # a trade chat example carries the realized outcome + review in metadata
    aapl = [r for r in trades if r["metadata"]["symbol"] == "AAPL"][0]
    assert aapl["metadata"]["label"]["outcome"] == "win"
    assert aapl["metadata"]["review"]["review"].startswith("Good entry")
    assistant = json.loads(aapl["messages"][2]["content"])
    assert assistant["decision"] == "TRADE"
    assert assistant["chosen"]["conviction"] == 7

    nt = [r for r in rows if r["metadata"]["example_kind"] == "no_trade"][0]
    assert nt["metadata"]["is_negative"] is True
    assert json.loads(nt["messages"][2]["content"])["decision"] == "NO_TRADE"

    rj = [r for r in rows if r["metadata"]["example_kind"] == "rejected"][0]
    assert json.loads(rj["messages"][2]["content"])["decision"] == "REJECTED"


def test_deterministic_output(tmp_path):
    _write_dataset(str(tmp_path))
    o1 = str(tmp_path / "a.jsonl")
    o2 = str(tmp_path / "b.jsonl")
    etd.export(str(tmp_path), o1, "jsonl-flat", include_open=False, min_realized=None)
    etd.export(str(tmp_path), o2, "jsonl-flat", include_open=False, min_realized=None)
    assert open(o1).read() == open(o2).read()
    # stable ascending ts ordering
    rows = [json.loads(l) for l in open(o1)]
    ts = [r["ts"] for r in rows]
    assert ts == sorted(ts)


def test_min_realized_filter(tmp_path):
    _write_dataset(str(tmp_path))
    out = str(tmp_path / "filt.jsonl")
    counts = etd.export(str(tmp_path), out, "jsonl-flat", include_open=False, min_realized=0.0)
    # loser (-56%) filtered out; winner (+40%) kept
    assert counts["trade"] == 1
    assert counts["skipped_filter"] == 1
    syms = {json.loads(l).get("symbol") for l in open(out)
            if json.loads(l)["example_kind"] == "trade"}
    assert syms == {"AAPL"}


def test_export_drops_noncanonical_and_ib_disagreement_rows(tmp_path):
    ds = _write_dataset(str(tmp_path))
    legacy = {
        "schema": "trade_dataset.v2", "kind": "trade", "source": "flex_history",
        "record_status": "LEGACY", "usable_for_training": False, "usable_for_pnl": False,
        "symbol": "OLD", "entry": {"ts": "2026-07-01T00:00:00Z"},
        "close": {"realized_pnl_net": 10, "realized_pnl_ib": 10},
        "labels": {"outcome": "win", "win": True},
    }
    disagreement = {
        "schema": "trade_dataset.v2", "kind": "trade", "source": "app",
        "record_status": "CANONICAL", "canonical": True,
        "usable_for_training": True, "usable_for_pnl": True,
        "symbol": "BAD", "entry": {"ts": "2026-07-01T00:00:01Z"},
        "close": {"realized_pnl_net": -100, "realized_pnl_ib": 100},
        "labels": {"outcome": "loss", "win": False},
    }
    unmarked = {
        "schema": "trade_dataset.v2", "kind": "trade", "source": "app",
        "symbol": "UNMARKED", "entry": {"ts": "2026-07-01T00:00:02Z"},
        "close": {"realized_pnl_net": 1, "realized_pnl_ib": 1},
        "labels": {"outcome": "win", "win": True},
    }
    with open(ds, "a") as stream:
        stream.write(json.dumps(legacy) + "\n")
        stream.write(json.dumps(disagreement) + "\n")
        stream.write(json.dumps(unmarked) + "\n")
    out = str(tmp_path / "guarded.jsonl")
    counts = etd.export(str(tmp_path), out, "jsonl-flat", include_open=False, min_realized=None)
    assert counts["noncanonical_dropped"] == 2
    assert counts["invalid_pnl_dropped"] == 1
    assert counts["emitted"] == 4
    assert os.path.exists(out + ".manifest.json")
