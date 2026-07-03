"""Trail-ceiling persistence (Part 1) + gain-protecting auto-trail safety floor (Part 2).

Part 1 -- a MODEL-armed trail must keep the fixed pot-tier take-profit CEILING suppressed across
SUBSEQUENT cycles (incl. plain 'hold'), so the ceiling can't snap back and force-close (clip) the
runner the trail was meant to let RUN. The armed state is PERSISTED per position.

Part 2 -- a winner whose PEAK gain clears auto_trail.activation_gain_pct auto-arms a WIDE trailing
stop EVEN IF the model never says arm_trail and EVEN IF the global rules.trailing toggle is off, so
gains are locked automatically. It never suppresses the ceiling and NEVER touches the stop.
"""
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


# entry_debit=500, qty=1 -> entry per share = 5.00; stop 30% -> stop price 3.50
ED, QTY = 500.0, 1


# ----------------------------- PART 1 -----------------------------
class TestCeilingSuppressionPersists:
    def test_arm_trail_persists_armed_flag(self, tmp_path):
        m = _mgr(tmp_path)
        _, forced = m._apply_decision(m.config.rules, {"action": "arm_trail"},
                                      7.0, ED, QTY, 111, "X")
        assert forced is None
        assert m.state_manager.state.trail_armed.get("111") is True

    def test_armed_flag_survives_save_load(self, tmp_path):
        m = _mgr(tmp_path)
        m._apply_decision(m.config.rules, {"action": "arm_trail"}, 7.0, ED, QTY, 111, "X")
        m.state_manager.save()
        reloaded = StateManager(m.config.state.path)
        assert reloaded.state.trail_armed.get("111") is True

    def test_ceiling_stays_suppressed_on_subsequent_hold_when_armed(self, tmp_path):
        m = _mgr(tmp_path)
        # cycle N: model arms a trail (persists the flag)
        m._apply_decision(m.config.rules, {"action": "arm_trail"}, 7.0, ED, QTY, 111, "X")
        # cycle N+1: model returns plain 'hold' -- ceiling must STILL be suppressed via persisted flag
        armed = "111" in m.state_manager.state.trail_armed
        out = m._reconcile_ceiling_backstop(m.config.rules, {"action": "hold"}, armed=armed)
        assert out.profit_target_pct is None  # suppressed across the hold cycle (the bug fix)

    def test_armed_runner_not_clipped_by_ceiling_on_hold(self, tmp_path):
        """The whole point: a +40% runner on a 'hold' cycle after arming is NOT force-closed by the
        30% profit_target ceiling; the (armed) trailing stop governs instead."""
        m = _mgr(tmp_path)
        m._apply_decision(m.config.rules, {"action": "arm_trail"}, 7.0, ED, QTY, 111, "X")
        armed = "111" in m.state_manager.state.trail_armed
        rules = m._reconcile_ceiling_backstop(m.config.rules, {"action": "hold"}, armed=armed)
        # price 7.00 (+40%) sits ABOVE the +30% ceiling but the trail is nowhere near giving back:
        trig = evaluate_position(con_id=111, symbol="X", quantity=QTY, entry_debit=ED,
                                 current_price=7.0, days_to_expiry=20, peak_price=7.0, rules=rules)
        assert trig is None  # runner lives -- ceiling did not clip it

    def test_ceiling_fires_when_no_trail_armed(self, tmp_path):
        m = _mgr(tmp_path)
        armed = "222" in m.state_manager.state.trail_armed  # never armed -> False
        out = m._reconcile_ceiling_backstop(m.config.rules, {"action": "hold"}, armed=armed)
        assert out.profit_target_pct == 30.0  # backstop intact
        # and a +40% winner is force-closed at the profit_target ceiling
        trig = evaluate_position(con_id=222, symbol="X", quantity=QTY, entry_debit=ED,
                                 current_price=7.0, days_to_expiry=20, peak_price=7.0, rules=out)
        assert trig is not None and trig.trigger_type == "profit_target"

    def test_prune_clears_armed_flag_on_close(self, tmp_path):
        m = _mgr(tmp_path)
        m._apply_decision(m.config.rules, {"action": "arm_trail"}, 7.0, ED, QTY, 111, "X")
        m.state_manager.state.prune_tracking(active_con_ids=[999])  # 111 no longer active
        assert "111" not in m.state_manager.state.trail_armed

    def test_stop_never_altered_by_ceiling_suppression(self, tmp_path):
        m = _mgr(tmp_path)
        out = m._reconcile_ceiling_backstop(m.config.rules, {"action": "arm_trail"}, armed=True)
        assert out.stop_pct == 30.0  # suppressing the ceiling never touches the protective stop
        # and the 30% stop still fires when price craters (peak_price=None isolates it from the
        # trail, which would otherwise legitimately outrank the stop for an up-then-down runner)
        trig = evaluate_position(con_id=1, symbol="X", quantity=QTY, entry_debit=ED,
                                 current_price=3.00, days_to_expiry=20, peak_price=None, rules=out)
        assert trig is not None and trig.trigger_type == "stop"


# ----------------------------- PART 2 -----------------------------
class TestAutoTrailSafetyFloor:
    AUTO = AutoTrailConfig(enabled=True, activation_gain_pct=25.0, giveback_fraction=0.5)

    def test_winner_past_activation_auto_arms_even_with_trailing_off(self, tmp_path):
        m = _mgr(tmp_path)
        # global trail OFF -- auto-trail must still guarantee protection
        base = replace(m.config.rules, trailing=TrailingConfig(enabled=False))
        out, armed = m._apply_auto_trail(base, self.AUTO, peak_price=7.0,  # +40% peak
                                         entry_debit=ED, quantity=QTY)
        assert armed is True
        assert out.trailing.enabled is True
        assert out.trailing.giveback_fraction == 0.5  # wide floor from auto_trail

    def test_below_activation_is_noop(self, tmp_path):
        m = _mgr(tmp_path)
        base = replace(m.config.rules, trailing=TrailingConfig(enabled=False))
        out, armed = m._apply_auto_trail(base, self.AUTO, peak_price=6.0,  # +20% peak < +25%
                                         entry_debit=ED, quantity=QTY)
        assert armed is False
        assert out is base  # untouched

    def test_disabled_flag_is_exact_noop(self, tmp_path):
        m = _mgr(tmp_path)
        off = AutoTrailConfig(enabled=False)
        base = replace(m.config.rules, trailing=TrailingConfig(enabled=False))
        out, armed = m._apply_auto_trail(base, off, peak_price=9.0, entry_debit=ED, quantity=QTY)
        assert armed is False
        assert out is base

    def test_widen_only_never_tightens_config_trail(self, tmp_path):
        m = _mgr(tmp_path)
        # config trail already wider (0.6) than auto (0.5): auto must NOT tighten it
        base = replace(m.config.rules,
                       trailing=TrailingConfig(enabled=True, activation_gain_pct=20.0,
                                               giveback_fraction=0.6))
        out, armed = m._apply_auto_trail(base, self.AUTO, peak_price=7.0,
                                         entry_debit=ED, quantity=QTY)
        assert armed is True
        assert out.trailing.giveback_fraction == 0.6  # kept the roomier giveback
        assert out.trailing.activation_gain_pct == 20.0  # never raised the activation

    def test_auto_trail_locks_a_gain_above_cost_basis(self, tmp_path):
        m = _mgr(tmp_path)
        base = replace(m.config.rules, trailing=TrailingConfig(enabled=False))
        out, _ = m._apply_auto_trail(base, self.AUTO, peak_price=7.0, entry_debit=ED, quantity=QTY)
        # peak 7.00 (+40%), giveback 0.5 -> protected floor = 7 - 0.5*(7-5) = 6.00 (+20% locked)
        trig = evaluate_position(con_id=1, symbol="X", quantity=QTY, entry_debit=ED,
                                 current_price=5.90, days_to_expiry=20, peak_price=7.0, rules=out)
        assert trig is not None and trig.trigger_type == "trailing_stop"
        assert trig.pnl_pct > 0  # exits protecting a gain, NOT a round-trip to breakeven

    def test_trail_ratchets_up_with_peak(self, tmp_path):
        m = _mgr(tmp_path)
        base = replace(m.config.rules, trailing=TrailingConfig(enabled=False))
        out, _ = m._apply_auto_trail(base, self.AUTO, peak_price=9.0, entry_debit=ED, quantity=QTY)
        # higher peak 9.00 -> floor = 9 - 0.5*(9-5) = 7.00 (+40% locked), strictly above the +20%
        # floor of the lower-peak case -> the protected floor only ratchets UP
        low = evaluate_trailing_stop(6.99, ED, QTY, peak_price=7.0,
                                     activation_gain_pct=25.0, giveback_fraction=0.5)
        high = evaluate_trailing_stop(6.99, ED, QTY, peak_price=9.0,
                                      activation_gain_pct=25.0, giveback_fraction=0.5)
        # at price 6.99: below the 7.00 floor of the higher peak (fires) but above 6.00 (does not)
        assert low is None and high is not None

    def test_model_can_still_take_profit_and_widen(self, tmp_path):
        m = _mgr(tmp_path)
        # take_profit still forces an immediate exit even with an auto-armed winner
        _, forced = m._apply_decision(m.config.rules, {"action": "take_profit", "reason": "bank"},
                                      9.0, ED, QTY, 1, "X")
        assert forced is not None and forced.trigger_type == "take_profit"
        # and the model can WIDEN the trail (bull regime / not-yet-armed accepts a wider giveback)
        r2, _ = m._apply_decision(
            replace(m.config.rules, trailing=TrailingConfig(enabled=False)),
            {"action": "arm_trail", "trail_giveback_fraction": 0.7},
            7.0, ED, QTY, 1, "X", regime={"regime": "bull"})
        assert r2.trailing.enabled is True and r2.trailing.giveback_fraction == 0.7

    def test_auto_trail_does_not_suppress_ceiling(self, tmp_path):
        """Auto-arm protects the downside; it must NOT remove the take-profit ceiling (that is only
        the model's arm_trail job). The ceiling must remain to bank the winner at its target."""
        m = _mgr(tmp_path)
        base = replace(m.config.rules, trailing=TrailingConfig(enabled=False))
        out, armed = m._apply_auto_trail(base, self.AUTO, peak_price=7.0, entry_debit=ED, quantity=QTY)
        assert armed is True
        assert out.profit_target_pct == 30.0  # ceiling untouched by auto-trail
