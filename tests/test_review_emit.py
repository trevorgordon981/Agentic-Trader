"""Tests for the post-trade REVIEW sidecar emit in morning_review.py (2026-07-03).

morning_review.emit_reviews() is the ONLY writer of `reviews.jsonl` -- the sidecar that
trade_capture.load_review() reads at CLOSE to embed a `review` block under a closed-trade v2
record. Before this, that block was always empty. These tests prove:
  (a) emit writes a reviews.jsonl row with the keys load_review expects, into the dir
      trade_capture.dataset_dir() resolves (data/ next to the journal), and
  (b) load_review() then returns that review for the matching con_id / symbol.
Plus idempotency (same-day re-run doesn't double-write) and the no-con_id (symbol-only) path.
RECORD-ONLY -- no orders, no broker.
"""
import json
import os

import pytest

import morning_review as mr
from exitmgr import trade_capture


def _item(symbol, con_id, label=None):
    return {
        "symbol": symbol, "con_id": con_id, "label": label or f"{symbol} test",
        "right": "C", "expiry": "20260815", "dte": 40,
        "upnl": 12.0, "debit": 800.0, "pct": 1.5,
    }


def _verdict(eroded, reason, action, no_thesis=False):
    return {"eroded": eroded, "reason": reason, "action": action,
            "_ok": True, "_no_thesis": no_thesis}


def _read_reviews(journal_path):
    ddir = trade_capture.dataset_dir(journal_path)
    path = os.path.join(ddir, "reviews.jsonl")
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]


# --------------------------------------------------------------------------- (a) + (b)
def test_emit_writes_row_and_load_review_reads_it(tmp_path):
    journal_path = str(tmp_path / "trades.log")
    (tmp_path / "trades.log").write_text("")  # journal need not exist for dataset_dir
    date = "2026-07-03"

    reviewed = [
        (_item("SPY", 111), _verdict(True, "trend broke below the 50DMA", "CONSIDER_SELL")),
        (_item("QQQ", 222), _verdict(False, "breakout thesis still intact", "HOLD")),
    ]
    n = mr.emit_reviews(journal_path, reviewed, date)
    assert n == 2

    rows = _read_reviews(journal_path)
    assert len(rows) == 2
    r = rows[0]
    # keys load_review keys on / returns
    for k in ("con_id", "symbol", "date", "review", "ts"):
        assert k in r
    assert r["con_id"] == 111
    assert r["symbol"] == "SPY"
    assert r["date"] == date
    assert "trend broke" in r["review"]

    ddir = trade_capture.dataset_dir(journal_path)

    # (b) load_review resolves by con_id (date-independent path)
    got = trade_capture.load_review(ddir, con_id=111)
    assert got is not None
    assert "trend broke" in got["review"]

    # ...and by symbol + close date
    got2 = trade_capture.load_review(ddir, symbol="QQQ", date=date)
    assert got2 is not None
    assert "breakout thesis" in got2["review"]

    # wrong con_id -> None
    assert trade_capture.load_review(ddir, con_id=999) is None


# --------------------------------------------------------------------------- idempotency
def test_same_day_rerun_does_not_double_write(tmp_path):
    journal_path = str(tmp_path / "trades.log")
    date = "2026-07-03"
    reviewed = [(_item("SPY", 111), _verdict(True, "catalyst passed", "CONSIDER_SELL"))]

    assert mr.emit_reviews(journal_path, reviewed, date) == 1
    assert mr.emit_reviews(journal_path, reviewed, date) == 0  # dedup on (con_id, date)
    assert len(_read_reviews(journal_path)) == 1

    # a new day writes a fresh row (newest ts wins in load_review)
    assert mr.emit_reviews(journal_path, reviewed, "2026-07-04") == 1
    assert len(_read_reviews(journal_path)) == 2


# --------------------------------------------------------------------------- symbol-only path
def test_symbol_only_when_con_id_unknown(tmp_path):
    journal_path = str(tmp_path / "trades.log")
    date = "2026-07-03"
    it = _item("IWM", None)
    reviewed = [(it, _verdict(False, "range intact", "HOLD"))]

    assert mr.emit_reviews(journal_path, reviewed, date) == 1
    # dedup on (symbol, date) when con_id is None
    assert mr.emit_reviews(journal_path, reviewed, date) == 0

    ddir = trade_capture.dataset_dir(journal_path)
    got = trade_capture.load_review(ddir, symbol="IWM", date=date)
    assert got is not None and got["con_id"] is None
    assert "range intact" in got["review"]
