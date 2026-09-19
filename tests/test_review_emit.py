"""Public API contract; production-derived narrative omitted."""
import json
import os

import pytest

import morning_review as mr
from exitmgr import trade_capture


def _item(symbol, con_id, label=None):
    return {
        "symbol": symbol, "con_id": con_id, "label": label or f"{symbol} test",
        "decision_id": f"decision-{symbol}-{con_id}",
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



def test_emit_writes_row_and_load_review_reads_it(tmp_path):
    journal_path = str(tmp_path / "trades.log")
    (tmp_path / "trades.log").write_text("")
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

    for k in ("con_id", "symbol", "date", "review", "ts"):
        assert k in r
    assert r["con_id"] == 111
    assert r["symbol"] == "SPY"
    assert r["date"] == date
    assert "trend broke" in r["review"]

    ddir = trade_capture.dataset_dir(journal_path)


    got = trade_capture.load_review(ddir, decision_id="decision-SPY-111", con_id=111)
    assert got is not None
    assert "trend broke" in got["review"]


    got2 = trade_capture.load_review(
        ddir, decision_id="decision-QQQ-222", symbol="QQQ", date=date)
    assert got2 is not None
    assert "breakout thesis" in got2["review"]


    assert trade_capture.load_review(ddir, decision_id="wrong", con_id=999) is None



def test_same_day_rerun_does_not_double_write(tmp_path):
    journal_path = str(tmp_path / "trades.log")
    date = "2026-07-03"
    reviewed = [(_item("SPY", 111), _verdict(True, "catalyst passed", "CONSIDER_SELL"))]

    assert mr.emit_reviews(journal_path, reviewed, date) == 1
    assert mr.emit_reviews(journal_path, reviewed, date) == 0
    assert len(_read_reviews(journal_path)) == 1


    assert mr.emit_reviews(journal_path, reviewed, "2026-07-04") == 1
    assert len(_read_reviews(journal_path)) == 2



def test_symbol_only_when_con_id_unknown(tmp_path):
    journal_path = str(tmp_path / "trades.log")
    date = "2026-07-03"
    it = _item("IWM", None)
    reviewed = [(it, _verdict(False, "range intact", "HOLD"))]

    assert mr.emit_reviews(journal_path, reviewed, date) == 1

    assert mr.emit_reviews(journal_path, reviewed, date) == 0

    ddir = trade_capture.dataset_dir(journal_path)
    got = trade_capture.load_review(
        ddir, decision_id="decision-IWM-None", symbol="IWM", date=date)
    assert got is not None and got["con_id"] is None
    assert "range intact" in got["review"]
