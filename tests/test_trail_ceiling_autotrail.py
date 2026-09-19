"""Public API contract; production-derived narrative omitted."""
import os
from dataclasses import replace

import pytest

from exitmgr.config import (Config, RulesConfig, TrailingConfig, AutoTrailConfig,
                            StateConfig, JournalConfig)
from exitmgr.manager import ExitManager
from exitmgr.state import StateManager
from exitmgr.rules import evaluate_position, evaluate_trailing_stop


def _mgr(tmp_path, stop_pct=30.0):
    cfg = Config()
    cfg.state = StateConfig(path=os.path.join(str(tmp_path), "state.json"))
    cfg.journal = JournalConfig(path=os.path.join(str(tmp_path), "trades.log"))
    cfg.rules = RulesConfig(profit_target_pct=30.0, stop_pct=stop_pct,
                            trailing=TrailingConfig(enabled=True, activation_gain_pct=20.0,
                                                    giveback_fraction=0.4))
    return ExitManager(cfg)



ED, QTY = 500.0, 1



class TestCeilingSuppressionPersists:
    def test_arm_trail_persists_configured_flag(self, tmp_path):
        m = _mgr(tmp_path)
        _, forced = m._apply_decision(m.config.rules, {"action": "arm_trail"},
                                      7.0, ED, QTY, 111, "X")
        assert forced is None
        assert m.state_manager.state.trail_configured.get("111") is True

    def test_configured_flag_survives_save_load(self, tmp_path):
        m = _mgr(tmp_path)
        m._apply_decision(m.config.rules, {"action": "arm_trail"}, 7.0, ED, QTY, 111, "X")
        m.state_manager.save()
        reloaded = StateManager(m.config.state.path)
        assert reloaded.state.trail_configured.get("111") is True

    def test_ceiling_stays_suppressed_on_subsequent_hold_when_armed(self, tmp_path):
        m = _mgr(tmp_path)

        m._apply_decision(m.config.rules, {"action": "arm_trail"}, 7.0, ED, QTY, 111, "X")

        armed = "111" in m.state_manager.state.trail_configured
        out = m._reconcile_ceiling_backstop(m.config.rules, {"action": "hold"}, armed=armed)
        assert out.profit_target_pct is None

    def test_armed_runner_not_clipped_by_ceiling_on_hold(self, tmp_path):
        """Public API contract; production-derived narrative omitted."""
        m = _mgr(tmp_path)
        m._apply_decision(m.config.rules, {"action": "arm_trail"}, 7.0, ED, QTY, 111, "X")
        armed = "111" in m.state_manager.state.trail_configured
        rules = m._reconcile_ceiling_backstop(m.config.rules, {"action": "hold"}, armed=armed)

        trig = evaluate_position(con_id=111, symbol="X", quantity=QTY, entry_debit=ED,
                                 current_price=7.0, days_to_expiry=20, peak_price=7.0, rules=rules)
        assert trig is None

    def test_ceiling_fires_when_no_trail_armed(self, tmp_path):
        m = _mgr(tmp_path)
        armed = "222" in m.state_manager.state.trail_configured
        out = m._reconcile_ceiling_backstop(m.config.rules, {"action": "hold"}, armed=armed)
        assert out.profit_target_pct == 30.0

        trig = evaluate_position(con_id=222, symbol="X", quantity=QTY, entry_debit=ED,
                                 current_price=7.0, days_to_expiry=20, peak_price=7.0, rules=out)
        assert trig is not None and trig.trigger_type == "profit_target"

    def test_prune_clears_configured_flag_on_close(self, tmp_path):
        m = _mgr(tmp_path)
        m._apply_decision(m.config.rules, {"action": "arm_trail"}, 7.0, ED, QTY, 111, "X")
        m.state_manager.state.prune_tracking(active_con_ids=[999])
        assert "111" not in m.state_manager.state.trail_configured

    def test_stop_never_altered_by_ceiling_suppression(self, tmp_path):
        m = _mgr(tmp_path)
        out = m._reconcile_ceiling_backstop(m.config.rules, {"action": "arm_trail"}, armed=True)
        assert out.stop_pct == 30.0


        trig = evaluate_position(con_id=1, symbol="X", quantity=QTY, entry_debit=ED,
                                 current_price=3.00, days_to_expiry=20, peak_price=None, rules=out)
        assert trig is not None and trig.trigger_type == "stop"



class TestAutoTrailSafetyFloor:
    AUTO = AutoTrailConfig(enabled=True, activation_gain_pct=25.0, giveback_fraction=0.5)

    def test_winner_past_activation_auto_arms_even_with_trailing_off(self, tmp_path):
        m = _mgr(tmp_path)

        base = replace(m.config.rules, trailing=TrailingConfig(enabled=False))
        out, armed = m._apply_auto_trail(base, self.AUTO, peak_price=7.0,
                                         entry_debit=ED, quantity=QTY,
                                         armed=True, peak_since_arm=7.0)
        assert armed is True
        assert out.trailing.enabled is True
        assert out.trailing.giveback_fraction == 0.5

    def test_below_activation_is_noop(self, tmp_path):
        m = _mgr(tmp_path)
        base = replace(m.config.rules, trailing=TrailingConfig(enabled=False))
        out, armed = m._apply_auto_trail(base, self.AUTO, peak_price=6.0,
                                         entry_debit=ED, quantity=QTY,
                                         armed=True, peak_since_arm=6.0)
        assert armed is False
        assert out is base

    def test_disabled_flag_is_exact_noop(self, tmp_path):
        m = _mgr(tmp_path)
        off = AutoTrailConfig(enabled=False)
        base = replace(m.config.rules, trailing=TrailingConfig(enabled=False))
        out, armed = m._apply_auto_trail(base, off, peak_price=9.0, entry_debit=ED, quantity=QTY,
                                         armed=True, peak_since_arm=9.0)
        assert armed is False
        assert out is base

    def test_widen_only_never_tightens_config_trail(self, tmp_path):
        m = _mgr(tmp_path)

        base = replace(m.config.rules,
                       trailing=TrailingConfig(enabled=True, activation_gain_pct=20.0,
                                               giveback_fraction=0.6))
        out, armed = m._apply_auto_trail(base, self.AUTO, peak_price=7.0,
                                         entry_debit=ED, quantity=QTY,
                                         armed=True, peak_since_arm=7.0)
        assert armed is True
        assert out.trailing.giveback_fraction == 0.6
        assert out.trailing.activation_gain_pct == 20.0

    def test_auto_trail_locks_a_gain_above_cost_basis(self, tmp_path):
        m = _mgr(tmp_path)
        base = replace(m.config.rules, trailing=TrailingConfig(enabled=False))
        out, _ = m._apply_auto_trail(base, self.AUTO, peak_price=7.0, entry_debit=ED, quantity=QTY,
                                     armed=True, peak_since_arm=7.0)

        trig = evaluate_position(con_id=1, symbol="X", quantity=QTY, entry_debit=ED,
                                 current_price=5.90, days_to_expiry=20, peak_price=7.0, rules=out,
                                 trail_armed=True, peak_since_arm=7.0)
        assert trig is not None and trig.trigger_type == "trailing_stop"
        assert trig.pnl_pct > 0

    def test_trail_ratchets_up_with_peak(self, tmp_path):
        m = _mgr(tmp_path)
        base = replace(m.config.rules, trailing=TrailingConfig(enabled=False))
        out, _ = m._apply_auto_trail(base, self.AUTO, peak_price=9.0, entry_debit=ED, quantity=QTY,
                                     armed=True, peak_since_arm=9.0)


        low = evaluate_trailing_stop(6.99, ED, QTY, peak_since_arm=7.0,
                                     activation_gain_pct=25.0, giveback_fraction=0.5, armed=True)
        high = evaluate_trailing_stop(6.99, ED, QTY, peak_since_arm=9.0,
                                      activation_gain_pct=25.0, giveback_fraction=0.5, armed=True)

        assert low is None and high is not None

    def test_model_can_still_take_profit_and_widen(self, tmp_path):
        m = _mgr(tmp_path)

        _, forced = m._apply_decision(m.config.rules, {"action": "take_profit", "reason": "bank"},
                                      9.0, ED, QTY, 1, "X")
        assert forced is not None and forced.trigger_type == "take_profit"

        r2, _ = m._apply_decision(
            replace(m.config.rules, trailing=TrailingConfig(enabled=False)),
            {"action": "arm_trail", "trail_giveback_fraction": 0.7},
            7.0, ED, QTY, 1, "X", regime={"regime": "bull"})
        assert r2.trailing.enabled is True and r2.trailing.giveback_fraction == 0.7

    def test_auto_trail_does_not_suppress_ceiling(self, tmp_path):
        """Public API contract; production-derived narrative omitted."""
        m = _mgr(tmp_path)
        base = replace(m.config.rules, trailing=TrailingConfig(enabled=False))
        out, armed = m._apply_auto_trail(base, self.AUTO, peak_price=7.0, entry_debit=ED, quantity=QTY,
                                         armed=True, peak_since_arm=7.0)
        assert armed is True
        assert out.profit_target_pct == 30.0
