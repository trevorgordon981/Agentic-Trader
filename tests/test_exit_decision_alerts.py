"""Public API contract; production-derived narrative omitted."""

import asyncio
import os

import pytest

from exitmgr import manager as manager_mod
from exitmgr.config import Config, JournalConfig, StateConfig
from exitmgr.connection import PositionData
from exitmgr.manager import ExitManager

SYMC, SYMC_SHORT = 4001001, 4001002


@pytest.fixture()
def posts(monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    sent = []

    def _fake_post(text, channel_id, tok=None, label="", **kwargs):
        sent.append({"text": text, "channel": channel_id, "label": label})
        return True

    from exitmgr import alerting
    monkeypatch.setattr(alerting, "post", _fake_post)
    monkeypatch.setattr(alerting, "alerts_channel", lambda: "ALERTS_CHANNEL_PLACEHOLDER")
    return sent


def _mgr(tmp_path):
    cfg = Config()
    cfg.state = StateConfig(path=os.path.join(str(tmp_path), "exitmgr_state.json"))
    cfg.journal = JournalConfig(path=os.path.join(str(tmp_path), "trades.log"))
    cfg.manage_positions = True
    cfg.llm_endpoint = "http://127.0.0.1:9/v1/chat/completions"
    cfg.llm_model = "test-model"
    mgr = ExitManager(cfg)
    mgr._journal_entries = {
        SYMC: {"symbol": "SYMC", "debit": 800.0, "quantity": 4, "stop_pct": 30.0,
               "conviction": 6, "intended_hold_days": 7, "fill_ts": "2037-08-05T14:06:31",
               "thesis": "momentum", "spread": {"short_con_id": SYMC_SHORT}},
    }
    return mgr


def _publish(mgr):
    positions = [PositionData(con_id=SYMC, symbol="SYMC", right="C", quantity=4,
                              avg_cost=200.0, expiry="20300117", strike=40.0)]
    views = mgr._build_position_views(positions, {SYMC: {"price": 2.80},
                                                  SYMC_SHORT: {"price": 1.20}})
    mgr.publish_mgmt_views(views, regime={"regime": "bull"}, qty_by_cid={SYMC: 4})
    return views


def _assess(mgr, monkeypatch, decisions):
    def _fake(endpoint, model, positions, market_regime=None, timeout=75, return_meta=False):
        return (dict(decisions), {"raw": "{}", "model_identity": None})
    monkeypatch.setattr(manager_mod, "assess_positions", _fake)
    return asyncio.run(mgr.assess_positions_offcycle())


def _alert(mgr, decisions):
    """Public API contract; production-derived narrative omitted."""
    views = _publish(mgr)
    return mgr._alert_exit_decisions(dict(decisions), {v["con_id"]: v for v in views})




def test_an_in_remit_cut_is_announced_with_its_reasoning(tmp_path, monkeypatch, posts):
    """Public API contract; production-derived narrative omitted."""
    mgr = _mgr(tmp_path)
    _publish(mgr)
    _assess(mgr, monkeypatch, {SYMC: {
        "action": "cut",
        "reason": "Thesis broken: guidance cut and the base has failed."}})
    assert len(posts) == 1
    text = posts[0]["text"]
    assert posts[0]["label"] == "exit-decision"
    assert "SYMC" in text and "cut" in text
    assert "Thesis broken" in text, "the model's own reason must be verbatim"
    assert "window" in text and str(SYMC) in text


def test_an_out_of_remit_proposal_never_pages(tmp_path, monkeypatch, posts):
    """Public API contract; production-derived narrative omitted."""
    mgr = _mgr(tmp_path)
    _publish(mgr)
    for action in ("arm_trail", "tighten_stop", "take_profit"):
        _publish(mgr)
        _assess(mgr, monkeypatch, {SYMC: {"action": action, "stop_pct": 20.0,
                                          "trail_giveback_fraction": 0.4, "reason": "x"}})
    assert posts == []


def test_a_non_hold_decision_is_announced_with_its_reasoning(tmp_path, monkeypatch, posts):
    mgr = _mgr(tmp_path)
    _publish(mgr)
    _alert(mgr, {SYMC: {
        "action": "arm_trail", "trail_activation_gain_pct": 20.0,
        "trail_giveback_fraction": 0.4,
        "reason": "Up 18.0% with MFE 25.0%; bull regime, let the winner run."}})

    assert len(posts) == 1
    text, label = posts[0]["text"], posts[0]["label"]
    assert label == "exit-decision"
    assert posts[0]["channel"] == "ALERTS_CHANNEL_PLACEHOLDER"
    assert "SYMC" in text and "arm_trail" in text
    assert "Up 18.0% with MFE 25.0%" in text, "the model's own reason must be verbatim"
    assert "window" in text
    assert "activation" in text and "giveback" in text
    assert str(SYMC) in text


def test_a_plain_hold_is_never_announced(tmp_path, monkeypatch, posts):
    mgr = _mgr(tmp_path)
    _publish(mgr)
    _assess(mgr, monkeypatch, {SYMC: {"action": "hold", "reason": "nothing material"}})
    assert posts == [], "holds would be a firehose on a 30s loop"


def test_tighten_stop_carries_the_new_stop(tmp_path, monkeypatch, posts):
    mgr = _mgr(tmp_path)
    _publish(mgr)
    _alert(mgr, {SYMC: {"action": "tighten_stop", "stop_pct": 20.0,
                        "reason": "momentum weakening"}})
    assert "20% of debit" in posts[0]["text"]


def test_take_profit_with_reload_says_so(tmp_path, monkeypatch, posts):
    mgr = _mgr(tmp_path)
    _publish(mgr)
    _alert(mgr, {SYMC: {"action": "take_profit", "reload": True,
                        "reload_conviction": 7, "reason": "run stalled"}})
    text = posts[0]["text"]
    assert "reload flagged" in text and "7" in text
    assert "gated by the normal entry path" in text, "a reload must not read as an auto re-entry"




def test_the_same_decision_is_announced_exactly_once_across_many_cycles(tmp_path, monkeypatch,
                                                                        posts):
    """Public API contract; production-derived narrative omitted."""
    mgr = _mgr(tmp_path)
    decision = {SYMC: {"action": "cut", "reason": "thesis broken"}}

    for _ in range(6):
        _publish(mgr)
        _assess(mgr, monkeypatch, decision)
        for _ in range(10):
            views = _publish(mgr)
            mgr._consume_model_decisions({v["con_id"]: v for v in views}, {SYMC: 3})

    assert len(posts) == 1, f"expected exactly one alert, got {len(posts)}"


def test_a_changed_decision_re_announces(tmp_path, monkeypatch, posts):
    mgr = _mgr(tmp_path)
    _alert(mgr, {SYMC: {"action": "tighten_stop", "stop_pct": 25.0}})
    _alert(mgr, {SYMC: {"action": "tighten_stop", "stop_pct": 25.0}})
    assert len(posts) == 1
    _alert(mgr, {SYMC: {"action": "tighten_stop", "stop_pct": 15.0}})
    assert len(posts) == 2, "a tighter stop is a NEW decision and must be announced"
    _alert(mgr, {SYMC: {"action": "cut", "reason": "done"}})
    assert len(posts) == 3


def test_a_hold_after_an_action_does_not_re_announce_the_action(tmp_path, monkeypatch, posts):
    mgr = _mgr(tmp_path)
    _alert(mgr, {SYMC: {"action": "arm_trail"}})
    _alert(mgr, {SYMC: {"action": "hold"}})
    _alert(mgr, {SYMC: {"action": "arm_trail"}})
    assert len(posts) == 1, "an unchanged arm_trail must not re-post because a hold came between"


def test_unarmed_arm_trail_does_not_re_page_on_drifting_activation(
        tmp_path, monkeypatch, posts):
    """Public API contract; production-derived narrative omitted."""
    mgr = _mgr(tmp_path)


    drift = [20.8, 19.7, 19.5, 23.5, 20.0, 23.1, 21.9, 21.4, 20.7, 20.5, 21.7, 24.1, 22.0]

    _alert(mgr, {SYMC: {"action": "arm_trail", "trail_activation_gain_pct": drift[0],
                        "trail_giveback_fraction": 0.4}})
    assert len(posts) == 1, "the FIRST arm_trail is real news and must page"



    assert not mgr.state_manager.state.is_trail_armed(SYMC)
    assert mgr.state_manager.state.pinned_trail_params(SYMC) is None

    for act in drift[1:]:
        _alert(mgr, {SYMC: {"action": "arm_trail", "trail_activation_gain_pct": act,
                            "trail_giveback_fraction": 0.4}})
    assert len(posts) == 1, (
        "a re-issued arm_trail whose only change is the echoed live gain is not a "
        "new decision and must not re-page (got %d posts)" % len(posts))


    _alert(mgr, {SYMC: {"action": "arm_trail", "trail_activation_gain_pct": 22.0,
                        "trail_giveback_fraction": 0.5}})
    assert len(posts) == 2, "a WIDER giveback changes the protection and must page"


    _alert(mgr, {SYMC: {"action": "take_profit", "reason": "stalled"}})
    assert len(posts) == 3, "a changed ACTION must still page"


def test_a_closed_position_forgets_its_fingerprint(tmp_path, monkeypatch, posts):
    """Public API contract; production-derived narrative omitted."""
    mgr = _mgr(tmp_path)
    _publish(mgr)
    _assess(mgr, monkeypatch, {SYMC: {"action": "cut", "reason": "x"}})
    assert len(posts) == 1

    mgr.publish_mgmt_views([], regime=None, qty_by_cid={})
    mgr._alert_exit_decisions({}, {})
    assert mgr._mgmt_alerted == {}

    _publish(mgr)
    _assess(mgr, monkeypatch, {SYMC: {"action": "cut", "reason": "x"}})
    assert len(posts) == 2




def test_a_slack_failure_never_breaks_the_assessment(tmp_path, monkeypatch):
    from exitmgr import alerting
    monkeypatch.setattr(alerting, "post", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("slack exploded")))
    mgr = _mgr(tmp_path)
    _publish(mgr)
    assert _assess(mgr, monkeypatch, {SYMC: {"action": "cut"}}) is True
    assert mgr._mgmt_cache["decisions"][SYMC]["action"] == "cut"


def test_an_undelivered_alert_is_retried_next_assessment(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    from exitmgr import alerting
    outcome = {"ok": False}
    sent = []

    def _fake_post(text, channel_id, tok=None, label="", **kwargs):
        sent.append(text)
        return outcome["ok"]

    monkeypatch.setattr(alerting, "post", _fake_post)
    monkeypatch.setattr(alerting, "alerts_channel", lambda: "ALERTS_CHANNEL_PLACEHOLDER")

    mgr = _mgr(tmp_path)
    _publish(mgr)
    _assess(mgr, monkeypatch, {SYMC: {"action": "cut"}})
    assert len(sent) == 1 and mgr._mgmt_alerted == {}

    outcome["ok"] = True
    _publish(mgr)
    _assess(mgr, monkeypatch, {SYMC: {"action": "cut"}})
    assert len(sent) == 2, "the failed alert must be retried"
    _publish(mgr)
    _assess(mgr, monkeypatch, {SYMC: {"action": "cut"}})
    assert len(sent) == 2, "...and then settle back to once"


def test_alerting_lives_in_the_assessor_not_the_stop_path():
    """Public API contract; production-derived narrative omitted."""
    import ast
    from pathlib import Path
    src = Path(manager_mod.__file__).read_text()
    tree = ast.parse(src)

    def _fn(name):
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
                return n
        raise AssertionError(name)

    def _attr_calls(node):
        return {n.func.attr for n in ast.walk(node)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}

    assert "_alert_exit_decisions" in _attr_calls(_fn("assess_positions_offcycle"))
    assert "_alert_exit_decisions" not in _attr_calls(_fn("run_cycle")), (
        "exit-decision alerting moved onto the 30s stop path")
    assert "_alert_exit_decisions" not in _attr_calls(_fn("_consume_model_decisions"))
