"""Public API contract; production-derived narrative omitted."""
import json
import os

from exitmgr import entry_safety, strategist, trade_capture


_COT = ("Step 1: SPY momentum up 5d & 20d. Step 2: VIX low, favorable for calls. "
        "Step 3: pick a call debit spread for defined risk.")
_ANSWER = ('{"trades":[{"underlying":"SPY","is_index":true,"direction":"bullish",'
           '"structure":"call debit spread","target_dte":30,"target_delta":0.6,'
           '"est_debit_usd":300,"conviction":8,"thesis":"trend"}]}')


def _fake_post(content, reasoning=None):
    """Public API contract; production-derived narrative omitted."""
    def _p(endpoint, body, timeout, **kw):
        msg = {"content": content}
        if reasoning is not None:
            msg["reasoning_content"] = reasoning
        return {"choices": [{"message": msg}]}
    return _p


def _ddir():
    return trade_capture.dataset_dir(None)


def _read_decisions():
    p = trade_capture.decision_context_path(_ddir())
    if not os.path.exists(p):
        return []
    with open(p) as f:
        return [json.loads(l) for l in f if l.strip()]


def _read_dataset():
    p = trade_capture.dataset_path(_ddir())
    if not os.path.exists(p):
        return []
    with open(p) as f:
        return [json.loads(l) for l in f if l.strip()]



def test_propose_return_cot_reads_reasoning_content(monkeypatch):
    monkeypatch.setattr(strategist, "_post_json", _fake_post(_ANSWER, reasoning=_COT))
    res = strategist.propose("http://x", "m3", "ctx", return_cot=True)
    assert isinstance(res, tuple) and len(res) == 3
    ideas, content, cot = res

    assert len(ideas) == 1 and ideas[0].underlying == "SPY" and ideas[0].conviction == 8

    assert content == _ANSWER and "Step 1" not in content

    assert cot == _COT and "call debit spread" in cot


def test_propose_return_cot_none_when_absent(monkeypatch):

    monkeypatch.setattr(strategist, "_post_json", _fake_post(_ANSWER, reasoning=None))
    ideas, content, cot = strategist.propose("http://x", "m3", "ctx", return_cot=True)
    assert cot is None and content == _ANSWER
    assert len(ideas) == 1 and ideas[0].underlying == "SPY"


def test_propose_backward_compatible(monkeypatch):

    monkeypatch.setattr(strategist, "_post_json", _fake_post(_ANSWER, reasoning=_COT))
    two = strategist.propose("http://x", "m3", "ctx", return_raw=True)
    assert isinstance(two, tuple) and len(two) == 2
    ideas_raw, content_raw = two
    assert content_raw == _ANSWER
    bare = strategist.propose("http://x", "m3", "ctx")
    assert not isinstance(bare, tuple)
    assert [i.underlying for i in bare] == [i.underlying for i in ideas_raw]



def test_propose_one_return_cot(monkeypatch):
    monkeypatch.setattr(strategist, "_post_json", _fake_post(_ANSWER, reasoning=_COT))
    res = strategist.propose_one("http://x", "m3", "ctx", "SPY", return_cot=True)
    assert isinstance(res, tuple) and len(res) == 3
    idea, content, cot = res
    assert idea is not None and idea.underlying == "SPY" and idea.conviction == 8
    assert content == _ANSWER and cot == _COT

    bare = strategist.propose_one("http://x", "m3", "ctx", "SPY")
    assert bare is not None and not isinstance(bare, tuple) and bare.underlying == "SPY"
    two = strategist.propose_one("http://x", "m3", "ctx", "SPY", return_raw=True)
    assert isinstance(two, tuple) and len(two) == 2 and two[1] == _ANSWER



def test_capture_decision_records_cot_field():
    rec = trade_capture.capture_decision(
        _ddir(), source="daily_slate", symbol="SPY", right="C", strike=500, expiry="20260220",
        raw_strategist=_ANSWER, cot=_COT)
    assert rec is not None
    rows = _read_decisions()
    assert len(rows) == 1
    r = rows[0]
    assert r["raw_strategist"] == _ANSWER
    assert r["cot"] == _COT
    assert "Step 1" not in r["raw_strategist"]


def test_capture_decision_cot_none_when_absent():
    trade_capture.capture_decision(_ddir(), source="trader", symbol="SPY",
                                   raw_strategist=_ANSWER, cot=None)
    r = _read_decisions()[-1]
    assert r["cot"] is None and r["raw_strategist"] == _ANSWER


def test_capture_decision_records_execution_authority_at_top_level():
    autonomous = trade_capture.capture_decision(
        _ddir(), source="daily_slate", symbol="SPY",
        decision_id="decision-autonomous-provenance", event="approved",
        execution_authority=entry_safety.AUTONOMOUS_WITHIN_RISK_GATES,
        human_action=None,
    )
    human = trade_capture.capture_decision(
        _ddir(), source="daily_slate", symbol="QQQ",
        decision_id="decision-human-provenance", event="approved",
        execution_authority=entry_safety.HUMAN_SLACK_APPROVAL,
        human_action={"action": "approve", "quantity_override": 2},
    )

    assert autonomous["execution_authority"] == (
        entry_safety.AUTONOMOUS_WITHIN_RISK_GATES
    )
    assert autonomous["human_action"] is None
    assert human["execution_authority"] == entry_safety.HUMAN_SLACK_APPROVAL
    assert human["human_action"] == {"action": "approve", "quantity_override": 2}

    persisted = {
        row["decision_id"]: row
        for row in _read_decisions()
        if row.get("decision_id") in {
            "decision-autonomous-provenance", "decision-human-provenance"
        }
    }
    assert persisted["decision-autonomous-provenance"]["human_action"] is None
    assert persisted["decision-human-provenance"]["execution_authority"] == (
        entry_safety.HUMAN_SLACK_APPROVAL
    )


def test_capture_no_trade_records_cot_field():
    trade_capture.capture_no_trade(_ddir(), source="trader", reason="empty_slate",
                                   raw_strategist="{}", cot=_COT)
    rows = _read_dataset()
    nt = [r for r in rows if r.get("kind") == "no_trade"]
    assert nt and nt[-1]["cot"] == _COT and nt[-1]["raw_strategist"] == "{}"



def test_long_cot_capped():
    long_cot = "reason " * 20000
    assert len(long_cot) > trade_capture._RAW_CAP
    trade_capture.capture_decision(_ddir(), source="trader", symbol="SPY",
                                   raw_strategist=_ANSWER, cot=long_cot)
    r = _read_decisions()[-1]
    assert len(r["cot"]) <= trade_capture._RAW_CAP + 64
    assert "truncated" in r["cot"]



def test_end_to_end_propose_to_capture(monkeypatch):
    monkeypatch.setattr(strategist, "_post_json", _fake_post(_ANSWER, reasoning=_COT))
    ideas, content, cot = strategist.propose("http://x", "m3", "ctx", return_cot=True)
    trade_capture.capture_decision(_ddir(), source="daily_slate", symbol=ideas[0].underlying,
                                   chosen_idea=ideas[0], candidates=ideas,
                                   raw_strategist=content, cot=cot)
    r = _read_decisions()[-1]
    assert r["raw_strategist"] == _ANSWER and r["cot"] == _COT
    assert r["chosen"]["underlying"] == "SPY" and r["chosen"]["conviction"] == 8
