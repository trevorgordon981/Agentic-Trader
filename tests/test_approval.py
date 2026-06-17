"""Tests for the Slack approve-each loop: reactions + natural-language replies (no real Slack)."""
import exitmgr.approval as ap
from exitmgr.approval import (
    decision_from_reactions, decision_from_text, decision_from_replies,
    format_proposal, await_approval,
)
from exitmgr.strategist import TradeIdea

IDEA = TradeIdea("SPY", True, "bullish", "long call", 7, 0.35, 90.0, 4, "trend continuation")


def test_format_contains_key_facts_and_text_hint():
    msg = format_proposal(IDEA, 1010.0, 121.0, 15)
    assert "SPY" in msg and "$90" in msg and "Conviction *4/10*" in msg
    assert "APPROVE" in msg and "DENY" in msg  # one-tap instruction
    assert "approved" in msg  # text approval still mentioned as fallback


# --- natural-language classification ---
def test_text_approvals():
    for t in ["approved", "go for it", "send it", "do it", "lgtm", "yep", "ship it",
              "ok send it", "yeah go ahead", "green light", "pull the trigger", "buy it"]:
        assert decision_from_text(t) == "approve", t


def test_text_rejections():
    for t in ["no", "skip", "pass", "reject", "abort", "nah", "cancel", "not this one", "hold off"]:
        assert decision_from_text(t) == "reject", t


def test_text_reject_wins_when_mixed():
    assert decision_from_text("yeah but no, skip it") == "reject"


def test_text_neutral_is_none():
    assert decision_from_text("what's the thesis again?") is None
    assert decision_from_text("") is None


def test_text_word_boundary_no_false_positive():
    # "now" must not match "no"; "broker" must not match "ok"; "goes" not "go"
    assert decision_from_text("looking at this now") is None
    assert decision_from_text("the broker is slow") is None


# --- reactions ---
def test_decision_reject_wins_over_approve():
    rx = [{"name": "white_check_mark", "users": ["U1"]}, {"name": "x", "users": ["U1"]}]
    assert decision_from_reactions(rx, None) == "reject"


def test_decision_respects_approver_allowlist():
    assert decision_from_reactions([{"name": "white_check_mark", "users": ["STRANGER"]}], {"OWNER"}) is None
    assert decision_from_reactions([{"name": "white_check_mark", "users": ["OWNER"]}], {"OWNER"}) == "approve"


# --- thread replies ---
def test_replies_skip_parent_and_respect_approver():
    msgs = [
        {"ts": "P", "user": "BOT", "text": "Proposed trade ..."},   # parent, ignored
        {"ts": "1", "user": "STRANGER", "text": "send it"},          # not approver, ignored
        {"ts": "2", "user": "OWNER", "text": "go for it"},           # approver -> approve
    ]
    assert decision_from_replies(msgs, {"OWNER"}, "P") == "approve"


def test_replies_reject_wins():
    msgs = [{"ts": "1", "user": "OWNER", "text": "go for it"},
            {"ts": "2", "user": "OWNER", "text": "actually no, skip it"}]
    assert decision_from_replies(msgs, {"OWNER"}, "P") == "reject"


# --- combined polling loop ---
def _fake_api(reactions=None, replies=None):
    def api(method, token, params, http_post=True):
        if method == "reactions.get":
            return {"ok": True, "message": {"reactions": reactions or []}}
        if method == "conversations.replies":
            return {"ok": True, "messages": replies or []}
        return {"ok": True}
    return api


def test_await_approval_via_text_reply(monkeypatch):
    state = {"replies": []}
    def api(method, token, params, http_post=True):
        if method == "reactions.get":
            return {"ok": True, "message": {"reactions": []}}
        return {"ok": True, "messages": state["replies"]}
    monkeypatch.setattr(ap, "_api", api)
    clock = {"t": 0.0}
    def slp(s):
        clock["t"] += s
        if clock["t"] >= 20:  # after two empty polls, the user replies
            state["replies"] = [{"ts": "1", "user": "OWNER", "text": "send it"}]
    d = await_approval("tok", "C1", "P", {"OWNER"}, timeout_s=300, poll_s=10,
                       _sleep=slp, _now=lambda: clock["t"])
    assert d == "approve"


def test_await_approval_expires(monkeypatch):
    monkeypatch.setattr(ap, "_api", _fake_api())
    clock = {"t": 0.0}
    d = await_approval("tok", "C1", "P", {"OWNER"}, timeout_s=30, poll_s=10,
                       _sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
                       _now=lambda: clock["t"])
    assert d == "expired"


def test_await_approval_text_reject_beats_emoji_approve(monkeypatch):
    monkeypatch.setattr(ap, "_api", _fake_api(
        reactions=[{"name": "white_check_mark", "users": ["OWNER"]}],
        replies=[{"ts": "1", "user": "OWNER", "text": "no, skip it"}]))
    clock = {"t": 0.0}
    d = await_approval("tok", "C1", "P", {"OWNER"}, timeout_s=30, poll_s=10,
                       _sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
                       _now=lambda: clock["t"])
    assert d == "reject"


def test_parse_levels():
    from exitmgr.approval import parse_levels
    assert parse_levels("tp 60 stop 30") == (60.0, 30.0)
    assert parse_levels("take profit 80%, stop loss 35%") == (80.0, 35.0)
    assert parse_levels("stop 25") == (None, 25.0)
    assert parse_levels("tp 90") == (90.0, None)
    assert parse_levels("looks good") == (None, None)
    assert parse_levels("") == (None, None)


def test_parse_add_tickers():
    from exitmgr.approval import parse_add_tickers
    cands = ["ANET", "VRT", "SMH", "XLRE", "MSFT"]
    assert set(parse_add_tickers("add ANET and VRT", cands)) == {"ANET", "VRT"}
    assert set(parse_add_tickers("add all of them", cands)) == set(cands)
    assert parse_add_tickers("ANET", cands) == ["ANET"]
    assert parse_add_tickers("none of these", cands) == []
    assert parse_add_tickers("add NVDA", cands) == []   # not a candidate -> ignored


def test_parse_structure_override():
    from exitmgr.approval import parse_structure_override
    assert parse_structure_override("flip it") == {"direction": "flip"}
    assert parse_structure_override("make it bearish") == {"direction": "bearish"}
    assert parse_structure_override("just the call") == {"direction": "bullish", "structure": "single"}
    assert parse_structure_override("make it a spread") == {"structure": "spread"}
    assert parse_structure_override("go for it") == {}
    assert parse_structure_override("stop 30") == {}


def test_parse_size_override():
    from exitmgr.approval import parse_size_override
    assert parse_size_override("full size") is True
    assert parse_size_override("go big") is True
    assert parse_size_override("max it") is True
    assert parse_size_override("all in") is True
    assert parse_size_override("size up") is True
    assert parse_size_override("use the cap") is True
    # must NOT trip on incidental words / a bare 'full'
    assert parse_size_override("looks good, send it") is False
    assert parse_size_override("the tank is full") is False
    assert parse_size_override("flip it") is False
    assert parse_size_override("") is False


def test_parse_qty_override():
    from exitmgr.approval import parse_qty_override
    assert parse_qty_override("1 contract") == {"contracts": 1}
    assert parse_qty_override("just 2") == {"contracts": 2}
    assert parse_qty_override("qty 3") == {"contracts": 3}
    assert parse_qty_override("2x") == {"contracts": 2}
    assert parse_qty_override("one contract") == {"contracts": 1}
    assert parse_qty_override("execute 1 contract") == {"contracts": 1}
    assert parse_qty_override("half size") == {"fraction": 0.5}
    assert parse_qty_override("quarter") == {"fraction": 0.25}
    assert parse_qty_override("looks good") == {}
    assert parse_qty_override("full size") == {}
    assert parse_qty_override("") == {}
