"""Public API contract; production-derived narrative omitted."""
import json

import pytest

from exitmgr import position_manager as pm
from exitmgr.manager import ExitManager


def _assess(monkeypatch, decisions):
    raw = json.dumps({"decisions": decisions})
    monkeypatch.setattr(pm, "_post_json", lambda *a, **k: raw)
    return pm.assess_positions("ep", "m", [{"con_id": 1}])



def test_the_two_remit_boundaries_are_the_same_tuple():
    """Public API contract; production-derived narrative omitted."""
    assert pm.REMIT_ACTIONS == ExitManager.MGMT_REMIT_ACTIONS
    assert pm.OUT_OF_REMIT == ExitManager.MGMT_OUT_OF_REMIT


def test_every_remit_verb_has_a_prompt_clause():
    assert set(pm.REMIT_ACTIONS) <= set(pm.KNOWN_ACTIONS)
    with pytest.raises(ValueError):
        pm.build_system(("hold", "liquidate_everything"))



def test_the_prompt_offers_only_the_remit():
    for verb in pm.REMIT_ACTIONS:
        assert ('"%s"' % verb) in pm.SYSTEM
    for verb in set(pm.KNOWN_ACTIONS) - set(pm.REMIT_ACTIONS):
        assert verb not in pm.SYSTEM, "the prompt still asks for a verb that will be refused: %s" % verb


def test_the_reply_schema_carries_no_field_the_remit_cannot_use():
    """Public API contract; production-derived narrative omitted."""
    for dead in ("trail_activation_gain_pct", "trail_giveback_fraction", "stop_pct",
                 "reload_conviction"):
        assert dead not in pm.SYSTEM


def test_the_prompt_says_who_owns_the_withheld_verbs():
    """Public API contract; production-derived narrative omitted."""
    assert "NOT YOURS THIS CYCLE" in pm.SYSTEM
    assert "BROKEN THESIS" in pm.SYSTEM
    assert "is a HOLD even when you would prefer to bank it" in pm.SYSTEM


    assert "Default to closing." not in pm.SYSTEM


def test_widening_the_remit_is_one_edit_not_an_archaeology_dig():
    """Public API contract; production-derived narrative omitted."""
    full = pm.build_system(pm.KNOWN_ACTIONS)
    for verb in pm.KNOWN_ACTIONS:
        assert ('"%s"' % verb) in full
    assert "reload_conviction" in full and "trail_giveback_fraction" in full
    assert "stop_pct is at most 30% of debit" in full
    assert "NOT YOURS THIS CYCLE" not in full



def test_an_out_of_remit_verb_is_neutered_not_dropped(monkeypatch):
    out = _assess(monkeypatch, {"1": {"action": "arm_trail", "trail_giveback_fraction": 0.9,
                                     "reason": "up"}})
    assert 1 in out, "dropping the entry fabricates a HOLD: an omitted con_id reads as one"
    assert out[1]["action"] == pm.OUT_OF_REMIT
    assert out[1]["proposed_action"] == "arm_trail"
    assert out[1]["trail_giveback_fraction"] == 0.9
    assert out[1]["reason"] == "up"


def test_an_unrecognised_verb_is_also_neutered_not_dropped(monkeypatch):
    out = _assess(monkeypatch, {"1": {"action": "YOLO"}})
    assert out[1]["action"] == pm.OUT_OF_REMIT and out[1]["proposed_action"] == "yolo"


def test_an_in_remit_verb_is_untouched_and_carries_no_proposed_action(monkeypatch):
    out = _assess(monkeypatch, {"1": {"action": "cut", "reason": "thesis broke"},
                                "2": {"action": "hold"}})
    assert out[1]["action"] == "cut" and "proposed_action" not in out[1]
    assert out[2]["action"] == "hold" and "proposed_action" not in out[2]


def test_a_refused_take_profit_can_never_carry_a_reload(monkeypatch):
    out = _assess(monkeypatch, {"1": {"action": "take_profit", "reload": True,
                                     "reload_conviction": 9}})
    assert out[1]["action"] == pm.OUT_OF_REMIT
    assert out[1]["reload"] is False and out[1]["reload_conviction"] is None


def test_a_neutered_decision_is_inert_at_the_manager(monkeypatch, tmp_path):
    """Public API contract; production-derived narrative omitted."""
    import os
    from exitmgr.config import Config, RulesConfig, TrailingConfig, StateConfig, JournalConfig
    cfg = Config()
    cfg.state = StateConfig(path=os.path.join(str(tmp_path), "state.json"))
    cfg.journal = JournalConfig(path=os.path.join(str(tmp_path), "trades.log"))
    cfg.rules = RulesConfig(stop_pct=50.0, trailing=TrailingConfig())
    m = ExitManager(cfg)
    dec = _assess(monkeypatch, {"5": {"action": "arm_trail", "trail_activation_gain_pct": 5,
                                     "trail_giveback_fraction": 0.9}})[5]
    r2, forced = m._apply_decision(m.config.rules, dec, 9.0, 800.0, 1, 5, "X")
    assert forced is None
    assert r2.trailing.enabled is cfg.rules.trailing.enabled
    assert pm.OUT_OF_REMIT not in ExitManager.MGMT_ALERT_ACTIONS


def test_the_consumption_boundary_does_not_re_refuse_an_already_refused_proposal(monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    decs = _assess(monkeypatch, {"7": {"action": "tighten_stop", "stop_pct": 12}})
    narrowed, refused = ExitManager._narrow_to_remit(decs)
    assert narrowed[7]["proposed_action"] == "tighten_stop"
    assert narrowed[7]["action"] == pm.OUT_OF_REMIT
    assert refused == []


def test_the_refusal_stays_visible_in_the_operator_log(monkeypatch, capsys):
    """Public API contract; production-derived narrative omitted."""
    _assess(monkeypatch, {"1": {"action": "arm_trail"}, "2": {"action": "arm_trail"},
                          "3": {"action": "cut"}})
    log = capsys.readouterr().out
    assert "refused 2 out-of-remit proposal(s)" in log
    assert "arm_trail -> con_ids [1, 2]" in log


def test_a_fully_in_remit_cycle_logs_nothing(monkeypatch, capsys):
    _assess(monkeypatch, {"1": {"action": "hold"}, "2": {"action": "cut"}})
    assert "refused" not in capsys.readouterr().out
