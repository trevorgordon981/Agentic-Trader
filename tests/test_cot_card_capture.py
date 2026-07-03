"""CoT + technical_card capture (2026-07-03).

Two entry-reasoning gaps closed, both ADDITIVE (never change which trade is parsed/placed):
  * the strategist's FULL verbatim output -- chain-of-thought INCLUDED when the serving endpoint
    returns it -- reaches raw_strategist, while parsing still extracts the same idea; and
  * the technical-indicator card fed to the model is persisted in the decision record.

Uses the autouse EXITMGR_DATASET_DIR isolation (conftest) so NOTHING touches the prod data/ dir.
"""
import json
import os

from exitmgr import strategist, trade_capture


_COT = ("<think>Step 1: SPY momentum is up 5d and 20d. Step 2: VIX low, favorable for calls. "
        "Step 3: pick a call debit spread for defined risk.</think>")
_ANSWER = ('{"trades":[{"underlying":"SPY","is_index":true,"direction":"bullish",'
           '"structure":"call debit spread","target_dte":30,"target_delta":0.6,'
           '"est_debit_usd":300,"conviction":8,"thesis":"trend"}]}')


def _fake_post(content):
    def _p(endpoint, body, timeout, **kw):
        return {"choices": [{"message": {"content": content}}]}
    return _p


def _ddir():
    return trade_capture.dataset_dir(None)  # honors EXITMGR_DATASET_DIR (autouse conftest)


def _read_decisions():
    p = trade_capture.decision_context_path(_ddir())
    if not os.path.exists(p):
        return []
    with open(p) as f:
        return [json.loads(l) for l in f if l.strip()]


# --------------------------------------------------------------- Gap 1: propose keeps CoT for capture
def test_propose_return_raw_preserves_cot(monkeypatch):
    full = _COT + "\n" + _ANSWER
    monkeypatch.setattr(strategist, "_post_json", _fake_post(full))
    res = strategist.propose("http://x", "m3", "ctx", return_raw=True)
    assert isinstance(res, tuple) and len(res) == 2
    ideas, raw = res
    # PARSING UNCHANGED: the single bullish SPY idea is still extracted despite the CoT prefix
    assert len(ideas) == 1 and ideas[0].underlying == "SPY" and ideas[0].conviction == 8
    # CAPTURE gets the FULL text INCLUDING the hidden chain-of-thought
    assert "<think>" in raw and "Step 1" in raw and "call debit spread" in raw
    # default call (no return_raw) is byte-identical parsing + the original bare-list return
    ideas2 = strategist.propose("http://x", "m3", "ctx")
    assert not isinstance(ideas2, tuple)
    assert [i.underlying for i in ideas2] == [i.underlying for i in ideas]


# --------------------------------------------------------------- Gap 1: propose_one now returns raw
def test_propose_one_return_raw_preserves_cot(monkeypatch):
    full = _COT + "\n" + _ANSWER
    monkeypatch.setattr(strategist, "_post_json", _fake_post(full))
    res = strategist.propose_one("http://x", "m3", "ctx", "SPY", return_raw=True)
    assert isinstance(res, tuple) and len(res) == 2
    idea, raw = res
    assert idea is not None and idea.underlying == "SPY" and idea.conviction == 8
    assert "<think>" in raw and "Step 2" in raw
    # backward compatible: default return is still a bare TradeIdea (not a tuple)
    bare = strategist.propose_one("http://x", "m3", "ctx", "SPY")
    assert bare is not None and not isinstance(bare, tuple) and bare.underlying == "SPY"


# --------------------------------------------------------------- Gap 1+2: persisted record has both
def test_capture_decision_records_cot_raw_and_technical_card():
    card = {"SPY": {"last": 500.0, "ret_5d": 1.2, "ret_20d": 3.4, "ivr": 40, "vol_20d_ann": 12.0}}
    full = _COT + "\n" + _ANSWER
    rec = trade_capture.capture_decision(
        _ddir(), source="daily_slate", symbol="SPY", right="C", strike=500, expiry="20260220",
        raw_strategist=full, technical_card=card)
    assert rec is not None
    rows = _read_decisions()
    assert len(rows) == 1
    r = rows[0]
    # CoT survives into the persisted raw_strategist
    assert "<think>" in r["raw_strategist"] and "Step 1" in r["raw_strategist"]
    # technical_card is NON-NULL and carries the indicators (JSON-serialized + capped by _cap)
    assert r["technical_card"] is not None
    assert "ret_5d" in r["technical_card"] and "SPY" in r["technical_card"]


# --------------------------------------------------------------- Gap 1: long CoT not truncated
def test_long_cot_not_truncated_under_raised_cap():
    assert trade_capture._RAW_CAP >= 64000
    long_cot = "<think>" + ("reasoning token " * 2000) + "</think>\n" + _ANSWER  # ~32KB, > old 24KB cap
    assert len(long_cot) > 24000
    trade_capture.capture_decision(_ddir(), source="trader", symbol="SPY", raw_strategist=long_cot)
    rows = _read_decisions()
    assert rows and rows[-1]["raw_strategist"].count("reasoning token") == 2000  # nothing dropped
    assert "truncated" not in rows[-1]["raw_strategist"]


# --------------------------------------------------------------- isolation sanity
def test_capture_writes_land_in_isolated_tmp_dir():
    trade_capture.capture_no_trade(_ddir(), source="trader", reason="empty_slate",
                                   raw_strategist=_COT)
    # the resolved dataset dir is the conftest tmp path, never the repo's data/
    assert os.environ.get("EXITMGR_DATASET_DIR") and _ddir() == os.environ["EXITMGR_DATASET_DIR"]
    assert os.path.isfile(trade_capture.dataset_path(_ddir()))
