import os
import pytest
from exitmgr import regime
from exitmgr.risk import ProposedTrade, RiskLimits, evaluate_trade
from exitmgr.config import Config, RulesConfig, TrailingConfig, StateConfig, JournalConfig
from exitmgr.manager import ExitManager


# ---------- regime classification ----------
def test_classify_bull():
    assert regime.classify_regime([{"ret_20d": 5, "ret_5d": 2}, {"ret_20d": 4, "ret_5d": 1.5}], 14)["regime"] == "bull"

def test_classify_riskoff_on_vix():
    assert regime.classify_regime([{"ret_20d": 1, "ret_5d": 0}], 32)["regime"] == "risk_off"

def test_classify_riskoff_on_trend():
    assert regime.classify_regime([{"ret_20d": -6, "ret_5d": -3}], 18)["regime"] == "risk_off"

def test_classify_neutral():
    assert regime.classify_regime([{"ret_20d": 0.5, "ret_5d": 0.2}], 17)["regime"] == "neutral"

def test_classify_unknown_when_no_data():
    assert regime.classify_regime([], None)["regime"] == "unknown"

def test_bull_needs_calm_vix():
    # strong uptrend but stressed VIX -> stress dominates -> risk_off, not bull
    assert regime.classify_regime([{"ret_20d": 6, "ret_5d": 3}], 30)["regime"] == "risk_off"

def test_trend_strength_labels():
    assert regime.trend_strength({"ret_20d": 8, "ret_5d": 5})["label"] == "strong_up"
    assert regime.trend_strength({"ret_20d": -8, "ret_5d": -5})["label"] == "strong_down"
    assert regime.trend_strength(None)["label"] == "unknown"

def test_size_multiplier():
    assert regime.size_multiplier({"regime": "bull"}, True) == 1.5
    assert regime.size_multiplier({"regime": "bull"}, False) == 1.0
    assert regime.size_multiplier({"regime": "neutral"}, True) == 1.0
    assert regime.size_multiplier(None, True) == 1.0


# ---------- regime-aware sizing in the risk gate ----------
def _limits(**kw):
    base = dict(max_trade_pct=0.30, max_concurrent=8, daily_halt_pct=0.20,
                confident_full_size=True, confident_conviction=7, pot_cap_usd=None,
                allow_any_name=True, blocked_names=set(), max_single_name_agg_pct=0.36)
    base.update(kw)
    return RiskLimits(**base)

def _gate(trade, available=1000.0, **kw):
    return evaluate_trade(trade, net_liq=1000.0, available_funds=available, open_positions=[],
                          pot_day_start=1000.0, approved_names=set(), limits=_limits(), **kw)

def test_bull_scales_long_size():
    t = ProposedTrade("SPY", 400.0, True, conviction=5, is_long=True)
    assert _gate(t).per_trade_cap == pytest.approx(300.0)                                  # 30% base
    assert _gate(t, regime_info={"regime": "bull"}).per_trade_cap == pytest.approx(450.0)  # 30% * 1.5

def test_bull_does_not_scale_short():
    t = ProposedTrade("SPY", 400.0, True, conviction=5, is_long=False)
    assert _gate(t, regime_info={"regime": "bull"}).per_trade_cap == pytest.approx(300.0)

def test_scaled_cap_floored_at_available_funds():
    t = ProposedTrade("SPY", 100.0, True, conviction=5, is_long=True)
    g = _gate(t, available=400.0, regime_info={"regime": "bull"})  # 450 wanted, 400 cash
    assert g.per_trade_cap == pytest.approx(400.0)

def test_confident_unaffected_by_regime():
    t = ProposedTrade("SPY", 100.0, True, conviction=8, is_long=True)  # >= confident_conviction
    assert _gate(t, regime_info={"regime": "bull"}).per_trade_cap == pytest.approx(1000.0)


# ---------- regime-aware trail widening in the position manager ----------
def _mgr(tmp_path):
    cfg = Config()
    cfg.state = StateConfig(path=os.path.join(str(tmp_path), "s.json"))
    cfg.journal = JournalConfig(path=os.path.join(str(tmp_path), "t.log"))
    cfg.rules = RulesConfig(stop_pct=50.0,
                            trailing=TrailingConfig(enabled=True, activation_gain_pct=50, giveback_fraction=0.3))
    return ExitManager(cfg)

def test_bull_allows_widening_existing_trail(tmp_path):
    m = _mgr(tmp_path)  # trail armed, giveback 0.3
    r2, _ = m._apply_decision(m.config.rules,
        {"action": "arm_trail", "trail_giveback_fraction": 0.5}, 5.0, 800.0, 1, 1, "X",
        regime={"regime": "bull"})
    assert r2.trailing.giveback_fraction == 0.5  # WIDENED -> winner can run

def test_neutral_keeps_trail_monotonic(tmp_path):
    m = _mgr(tmp_path)  # giveback 0.3
    r2, _ = m._apply_decision(m.config.rules,
        {"action": "arm_trail", "trail_giveback_fraction": 0.5}, 5.0, 800.0, 1, 1, "X",
        regime={"regime": "neutral"})
    assert r2.trailing.giveback_fraction == 0.3  # NOT widened (monotonic tighten only)

def test_stop_always_monotonic_regardless_of_regime(tmp_path):
    m = _mgr(tmp_path)  # stop_pct 50
    r2, _ = m._apply_decision(m.config.rules, {"action": "tighten_stop", "stop_pct": 70},
                              5.0, 800.0, 1, 1, "X", regime={"regime": "bull"})
    assert r2.stop_pct == 50  # cannot loosen a stop even in a bull
