"""Public API contract; production-derived narrative omitted."""
import inspect

from exitmgr.risk import (
    RiskLimits, OpenPosition, ProposedTrade, evaluate_trade, effective_pot, day_pnl_pct,
    INDEX_UNDERLYINGS,
)

POT = 1010.0
LIM = RiskLimits()
NAMES = {"NVDA", "AAPL"}


def gate(trade, net_liq=POT, available=10_000.0, open_pos=None, day_start=POT, names=NAMES, limits=LIM):
    return evaluate_trade(
        trade, net_liq=net_liq, available_funds=available,
        open_positions=open_pos or [], pot_day_start=day_start,
        approved_names=names, limits=limits,
    )


def test_small_index_trade_approved():
    d = gate(ProposedTrade("SPY", 100.0, True))
    assert d.approved, d.reasons
    assert abs(d.per_trade_cap - 0.12 * POT) < 1e-6


def test_over_12pct_rejected():
    d = gate(ProposedTrade("QQQ", 200.0, True))
    assert not d.approved
    assert any("12%-of-pot" in r for r in d.reasons)


def test_dynamic_sizing_scales_with_pot():

    t = ProposedTrade("SPY", 200.0, True)
    assert not gate(t, net_liq=1010.0).approved
    assert gate(t, net_liq=2000.0).approved

    assert not gate(ProposedTrade("SPY", 100.0, True), net_liq=500.0).approved


def test_max_concurrent_blocks_fifth():
    pos = [OpenPosition("SPY", 50, True)] * 4
    d = gate(ProposedTrade("IWM", 50.0, True), open_pos=pos)
    assert not d.approved
    assert any("max concurrent" in r for r in d.reasons)


def test_daily_circuit_breaker_halts():

    d = gate(ProposedTrade("SPY", 50.0, True), net_liq=919.0, day_start=1010.0)
    assert not d.approved
    assert any("circuit breaker" in r for r in d.reasons)


def test_daily_breaker_not_tripped_at_minus5():
    d = gate(ProposedTrade("SPY", 50.0, True), net_liq=960.0, day_start=1010.0)
    assert d.approved, d.reasons


def test_unapproved_name_rejected():
    d = gate(ProposedTrade("GME", 50.0, False))
    assert not d.approved
    assert any("allowed universe" in r for r in d.reasons)


def test_approved_name_allowed():
    d = gate(ProposedTrade("NVDA", 50.0, False))
    assert d.approved, d.reasons


def test_insufficient_buying_power():
    d = gate(ProposedTrade("SPY", 100.0, True), available=40.0)
    assert not d.approved
    assert any("available funds" in r for r in d.reasons)


def test_aggregate_single_name_cap():




    pos = [OpenPosition("NVDA", 150, False), OpenPosition("NVDA", 150, False)]
    d = gate(ProposedTrade("NVDA", 100.0, False), open_pos=pos)
    assert not d.approved
    assert any("single-name exposure" in r for r in d.reasons)


def test_positions_in_OTHER_names_do_not_aggregate_into_this_one():
    """Public API contract; production-derived narrative omitted."""
    pos = [OpenPosition("NVDA", 150, False), OpenPosition("AAPL", 150, False)]
    d = gate(ProposedTrade("SYMB", 100.0, False), open_pos=pos)
    assert not any("single-name exposure" in r for r in d.reasons), d.reasons


def test_index_exposure_not_capped_by_single_name_rule():
    pos = [OpenPosition("SPY", 150, True), OpenPosition("QQQ", 150, True)]
    d = gate(ProposedTrade("IWM", 100.0, True), open_pos=pos)
    assert d.approved, d.reasons


def test_pot_cap_ringfence():

    lim = RiskLimits(pot_cap_usd=1010.0)
    assert abs(effective_pot(5000.0, 1010.0) - 1010.0) < 1e-6
    d = gate(ProposedTrade("SPY", 200.0, True), net_liq=5000.0, limits=lim)
    assert not d.approved


def test_allow_any_name_opens_universe():
    d = gate(ProposedTrade("GME", 50.0, False), limits=RiskLimits(allow_any_name=True))
    assert d.approved, d.reasons


def test_allow_any_name_keeps_size_cap():
    d = gate(ProposedTrade("SYMB", 200.0, False), limits=RiskLimits(allow_any_name=True))
    assert not d.approved
    assert any("12%-of-pot" in r for r in d.reasons)


def test_allow_any_name_keeps_aggregate_name_cap():


    pos = [OpenPosition("SYMB", 150, False), OpenPosition("SYMB", 150, False)]
    d = gate(ProposedTrade("SYMB", 100.0, False), open_pos=pos,
             limits=RiskLimits(allow_any_name=True))
    assert not d.approved
    assert any("single-name exposure" in r for r in d.reasons)





CONF = RiskLimits(
    confident_full_size=True, allow_any_name=True,
    conviction_size_curve={1: 0.12, 2: 0.12, 3: 0.12, 4: 0.12, 5: 0.12,
                           6: 0.25, 7: 0.25, 8: 0.25, 9: 0.25, 10: 0.25},
)


def test_confident_clamped_to_hard_cap():



    d = gate(ProposedTrade("MUTX", 1000.0, False, conviction=8), available=1010.0, limits=CONF)
    assert not d.approved
    assert abs(d.per_trade_cap - 0.25 * POT) < 1e-6
    assert any("25%-of-pot" in r for r in d.reasons)
    d2 = gate(ProposedTrade("MUTX", 200.0, False, conviction=8), available=1010.0, limits=CONF)
    assert d2.approved and abs(d2.per_trade_cap - 0.25 * POT) < 1e-6


def test_confident_still_blocked_by_buying_power():

    d = gate(ProposedTrade("MUTX", 1500.0, False, conviction=8), available=1010.0, limits=CONF)
    assert not d.approved
    assert any("available funds" in r for r in d.reasons)


def test_low_conviction_still_capped_even_when_confident_sizing_on():

    d = gate(ProposedTrade("MUTX", 1000.0, False, conviction=5), available=1010.0, limits=CONF)
    assert not d.approved
    assert any("12%-of-pot" in r for r in d.reasons)
    assert abs(d.per_trade_cap - 0.12 * POT) < 1e-6


def test_confident_still_respects_circuit_breaker():

    d = gate(ProposedTrade("MUTX", 500.0, False, conviction=8),
             net_liq=900.0, day_start=1010.0, available=900.0, limits=CONF)
    assert not d.approved
    assert any("circuit breaker" in r for r in d.reasons)


def test_confident_off_by_default():

    d = gate(ProposedTrade("SPY", 1000.0, True, conviction=8))
    assert not d.approved
    assert any("12%-of-pot" in r for r in d.reasons)


def test_blocked_name_rejected_even_with_open_universe():
    lim = RiskLimits(allow_any_name=True, blocked_names={"TSLA"})
    d = gate(ProposedTrade("TSLA", 50.0, False), limits=lim)
    assert not d.approved
    assert any("blocklist" in r for r in d.reasons)


def test_blocked_name_case_insensitive():
    lim = RiskLimits(allow_any_name=True, blocked_names={"tsla"})
    assert not gate(ProposedTrade("TSLA", 50.0, False), limits=lim).approved


def test_non_blocked_name_still_allowed():
    lim = RiskLimits(allow_any_name=True, blocked_names={"TSLA"})
    d = gate(ProposedTrade("RKLB", 50.0, False), limits=lim)
    assert d.approved, d.reasons


def test_blocklist_does_not_touch_index():
    lim = RiskLimits(allow_any_name=True, blocked_names={"SPY"})
    assert gate(ProposedTrade("SPY", 50.0, True), limits=lim).approved


def test_index_set():
    assert INDEX_UNDERLYINGS == {"SPY", "QQQ", "IWM"}


def test_day_pnl_helper():
    assert abs(day_pnl_pct(919.0, 1010.0) + 0.0901) < 1e-3
    assert day_pnl_pct(1010.0, 0.0) == 0.0





from exitmgr.risk import curve_fraction, DEFAULT_CONVICTION_SIZE_CURVE


def test_cash_buffer_clamps_full_size():


    lim = RiskLimits(confident_full_size=True, allow_any_name=True, max_trade_pct_hard=1.0,
                     cash_buffer_pct=0.05,
                     conviction_size_curve={c: 1.0 for c in range(1, 11)})


    d = gate(ProposedTrade("SPY", 940.0, True, conviction=10), net_liq=1000.0, available=1000.0, limits=lim)
    assert d.approved, d.reasons
    assert abs(d.per_trade_cap - 950.0) < 1e-6

    d2 = gate(ProposedTrade("SPY", 960.0, True, conviction=10), net_liq=1000.0, available=1000.0, limits=lim)
    assert not d2.approved


def test_cash_buffer_never_negative():

    lim = RiskLimits(confident_full_size=True, allow_any_name=True, cash_buffer_pct=0.05)
    d = gate(ProposedTrade("SPY", 10.0, True, conviction=10), net_liq=1000.0, available=30.0, limits=lim)
    assert d.per_trade_cap == 0.0
    assert not d.approved


def test_cap_bypass_threshold_blocks_below():

    lim = RiskLimits(confident_full_size=True, allow_any_name=True,
                     cap_bypass_min_conviction=6,
                     conviction_size_curve={c: (0.25 if c >= 6 else 0.12) for c in range(1, 11)})
    d5 = gate(ProposedTrade("MUTX", 200.0, False, conviction=5), available=1010.0, limits=lim)
    assert not d5.approved
    assert abs(d5.per_trade_cap - 0.12 * POT) < 1e-6
    d6 = gate(ProposedTrade("MUTX", 200.0, False, conviction=6), available=1010.0, limits=lim)
    assert d6.approved, d6.reasons
    assert abs(d6.per_trade_cap - 0.25 * POT) < 1e-6


def test_curve_fraction_lookup():
    curve = {1: 0.05, 5: 0.15, 10: 0.30}
    assert curve_fraction(5, curve, 0.12) == 0.15
    assert curve_fraction(1, curve, 0.12) == 0.05

    assert curve_fraction(3, curve, 0.12) == 0.12

    assert curve_fraction(0, curve, 0.12) == 0.05
    assert curve_fraction(99, curve, 0.12) == 0.30

    assert curve_fraction(7, None, 0.12) == 0.12
    assert curve_fraction(7, {}, 0.12) == 0.12


def test_default_curve_is_flat_base_cap():

    assert set(DEFAULT_CONVICTION_SIZE_CURVE.values()) == {0.12}


def test_default_curve_no_upsize_without_bypass():

    d = gate(ProposedTrade("SPY", 1000.0, True, conviction=10))
    assert not d.approved
    assert abs(d.per_trade_cap - 0.12 * POT) < 1e-6










def test_conviction_scale_is_1_to_10():

    assert set(DEFAULT_CONVICTION_SIZE_CURVE.keys()) == set(range(1, 11))


def test_cap_bypass_threshold_is_reachable_on_strategist_scale():


    thr = RiskLimits().cap_bypass_min_conviction
    assert 1 <= thr <= 10, f"cap_bypass_min_conviction={thr} is off the 1-10 strategist scale"

    assert thr in DEFAULT_CONVICTION_SIZE_CURVE


def test_strategist_clamps_to_1_to_10():

    from exitmgr import strategist
    src = inspect.getsource(strategist)
    assert "min(10" in src and "max(1" in src, "strategist no longer clamps conviction to 1..10"


def test_confident_bypass_is_live_at_threshold_conviction():


    thr = RiskLimits().cap_bypass_min_conviction
    lim = RiskLimits(confident_full_size=True, allow_any_name=True,
                     conviction_size_curve={c: (0.25 if c >= thr else 0.12) for c in range(1, 11)})


    d = gate(ProposedTrade("MUTX", 200.0, False, conviction=thr), available=1010.0, limits=lim)
    assert d.approved, d.reasons
    assert abs(d.per_trade_cap - 0.25 * POT) < 1e-6
