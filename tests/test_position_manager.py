import os
import pytest
from exitmgr.config import Config, RulesConfig, TrailingConfig, StateConfig, JournalConfig
from exitmgr.rules import ExitTrigger
from exitmgr.manager import ExitManager
from exitmgr import position_manager


def _mgr(tmp_path, stop_pct=50.0):
    cfg = Config()
    cfg.state = StateConfig(path=os.path.join(str(tmp_path), "state.json"))
    cfg.journal = JournalConfig(path=os.path.join(str(tmp_path), "trades.log"))
    cfg.rules = RulesConfig(stop_pct=stop_pct, trailing=TrailingConfig())
    return ExitManager(cfg)


def test_arm_trail_enables_and_is_monotonic(tmp_path):
    m = _mgr(tmp_path)
    r2, forced = m._apply_decision(
        m.config.rules,
        {"action": "arm_trail", "trail_activation_gain_pct": 30, "trail_giveback_fraction": 0.4},
        5.0, 800.0, 1, 111, "X")
    assert forced is None
    assert r2.trailing.enabled is True
    assert r2.trailing.activation_gain_pct == 30
    assert r2.trailing.giveback_fraction == 0.4
    # monotonic: cannot raise activation (arm later) or widen giveback once armed
    r3, _ = m._apply_decision(
        r2,
        {"action": "arm_trail", "trail_activation_gain_pct": 60, "trail_giveback_fraction": 0.7},
        5.0, 800.0, 1, 111, "X")
    assert r3.trailing.activation_gain_pct == 30
    assert r3.trailing.giveback_fraction <= 0.4


def test_tighten_stop_only_reduces(tmp_path):
    m = _mgr(tmp_path, stop_pct=50.0)
    r2, _ = m._apply_decision(m.config.rules, {"action": "tighten_stop", "stop_pct": 30}, 5, 800, 1, 1, "X")
    assert r2.stop_pct == 30
    r3, _ = m._apply_decision(r2, {"action": "tighten_stop", "stop_pct": 70}, 5, 800, 1, 1, "X")
    assert r3.stop_pct == 30  # loosen attempt rejected


def test_take_profit_and_cut_force_exit(tmp_path):
    m = _mgr(tmp_path)
    _, f1 = m._apply_decision(m.config.rules, {"action": "take_profit", "reason": "stall"}, 10.0, 800.0, 1, 5, "X")
    assert isinstance(f1, ExitTrigger) and f1.trigger_type == "take_profit"
    assert f1.pnl_pct == pytest.approx((10.0 * 100 * 1 - 800.0) / 800.0 * 100)
    _, f2 = m._apply_decision(m.config.rules, {"action": "cut"}, 3.0, 800.0, 1, 5, "X")
    assert f2.trigger_type == "model_cut"


def test_hold_no_change(tmp_path):
    m = _mgr(tmp_path)
    r2, forced = m._apply_decision(m.config.rules, {"action": "hold"}, 5, 800, 1, 1, "X")
    assert forced is None
    assert r2 is m.config.rules


def test_assess_parses_and_filters(monkeypatch):
    raw = ('note {"decisions": {"111": {"action":"arm_trail","trail_activation_gain_pct":40,'
           '"trail_giveback_fraction":0.3,"reason":"up"}, "222": {"action":"bogus"}, '
           '"333": {"action":"cut"}}}')
    monkeypatch.setattr(position_manager, "_post_json", lambda *a, **k: raw)
    out = position_manager.assess_positions("ep", "m", [{"con_id": 111}, {"con_id": 222}, {"con_id": 333}])
    assert set(out) == {111, 333}  # bogus action filtered out
    assert out[111]["action"] == "arm_trail" and out[333]["action"] == "cut"


def test_assess_empty_on_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(position_manager, "_post_json", boom)
    assert position_manager.assess_positions("ep", "m", [{"con_id": 1}]) == {}


def test_assess_empty_when_no_model_or_no_positions():
    assert position_manager.assess_positions("ep", "", [{"con_id": 1}]) == {}
    assert position_manager.assess_positions("ep", "m", []) == {}
