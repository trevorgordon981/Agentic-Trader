"""Tests for the hard risk gate, including dynamic (pot-relative) sizing."""
from exitmgr.risk import (
    RiskLimits, OpenPosition, ProposedTrade, evaluate_trade, effective_pot, day_pnl_pct,
    INDEX_UNDERLYINGS,
)

POT = 1010.0  # current pot
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
    assert abs(d.per_trade_cap - 0.12 * POT) < 1e-6  # ~$121


def test_over_12pct_rejected():
    d = gate(ProposedTrade("QQQ", 200.0, True))  # >$121 cap on a $1010 pot
    assert not d.approved
    assert any("12%-of-pot" in r for r in d.reasons)


def test_dynamic_sizing_scales_with_pot():
    # SAME $200 trade: rejected at $1010, approved once the pot grows past ~$1667
    t = ProposedTrade("SPY", 200.0, True)
    assert not gate(t, net_liq=1010.0).approved        # cap ~$121
    assert gate(t, net_liq=2000.0).approved             # cap $240 -> fits
    # and tightens if the pot shrinks
    assert not gate(ProposedTrade("SPY", 100.0, True), net_liq=500.0).approved  # cap $60


def test_max_concurrent_blocks_fifth():
    pos = [OpenPosition("SPY", 50, True)] * 4
    d = gate(ProposedTrade("IWM", 50.0, True), open_pos=pos)
    assert not d.approved
    assert any("max concurrent" in r for r in d.reasons)


def test_daily_circuit_breaker_halts():
    # pot down 9% on the day -> halt new entries
    d = gate(ProposedTrade("SPY", 50.0, True), net_liq=919.0, day_start=1010.0)
    assert not d.approved
    assert any("circuit breaker" in r for r in d.reasons)


def test_daily_breaker_not_tripped_at_minus5():
    d = gate(ProposedTrade("SPY", 50.0, True), net_liq=960.0, day_start=1010.0)  # -5%
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
    # already $300 of single names on a $1010 pot (agg cap = 36% = ~$364); +$100 -> $400 > cap
    pos = [OpenPosition("NVDA", 150, False), OpenPosition("AAPL", 150, False)]
    d = gate(ProposedTrade("NVDA", 100.0, False), open_pos=pos)
    assert not d.approved
    assert any("single-name exposure" in r for r in d.reasons)


def test_index_exposure_not_capped_by_single_name_rule():
    pos = [OpenPosition("SPY", 150, True), OpenPosition("QQQ", 150, True)]
    d = gate(ProposedTrade("IWM", 100.0, True), open_pos=pos)
    assert d.approved, d.reasons


def test_pot_cap_ringfence():
    # account is $5000 but ring-fenced to $1010 -> caps computed off $1010, not $5000
    lim = RiskLimits(pot_cap_usd=1010.0)
    assert abs(effective_pot(5000.0, 1010.0) - 1010.0) < 1e-6
    d = gate(ProposedTrade("SPY", 200.0, True), net_liq=5000.0, limits=lim)
    assert not d.approved  # $200 > 12% of the ring-fenced $1010 (~$121)


def test_allow_any_name_opens_universe():
    d = gate(ProposedTrade("GME", 50.0, False), limits=RiskLimits(allow_any_name=True))
    assert d.approved, d.reasons


def test_allow_any_name_keeps_size_cap():
    d = gate(ProposedTrade("PLTR", 200.0, False), limits=RiskLimits(allow_any_name=True))
    assert not d.approved
    assert any("12%-of-pot" in r for r in d.reasons)


def test_allow_any_name_keeps_aggregate_name_cap():
    pos = [OpenPosition("NVDA", 150, False), OpenPosition("AAPL", 150, False)]
    d = gate(ProposedTrade("PLTR", 100.0, False), open_pos=pos,
             limits=RiskLimits(allow_any_name=True))
    assert not d.approved
    assert any("single-name exposure" in r for r in d.reasons)


# Confident sizing now flows through the conviction->size curve. A curve that sizes high conviction
# up toward the 25% hard ceiling lets the cap-bypass actually lift past the 12% soft cap. The cap
# bypass requires conviction >= cap_bypass_min_conviction (default 6, raised from the old 4).
CONF = RiskLimits(
    confident_full_size=True, allow_any_name=True,
    conviction_size_curve={1: 0.12, 2: 0.12, 3: 0.12, 4: 0.12, 5: 0.12,
                           6: 0.25, 7: 0.25, 8: 0.25, 9: 0.25, 10: 0.25},
)


def test_confident_clamped_to_hard_cap():
    # 2026-06-22 hardening: the hard 25%-of-NetLiq ceiling clamps even confident sizing.
    # MU $1000 on a $1010 pot is still REJECTED (no whole-pot override). conviction 8 clears the
    # bypass threshold (6) and the curve sizes it to the 25% ceiling, not the whole pot.
    d = gate(ProposedTrade("MU", 1000.0, False, conviction=8), available=1010.0, limits=CONF)
    assert not d.approved
    assert abs(d.per_trade_cap - 0.25 * POT) < 1e-6  # ~$252.50, not $1010
    assert any("25%-of-pot" in r for r in d.reasons)
    d2 = gate(ProposedTrade("MU", 200.0, False, conviction=8), available=1010.0, limits=CONF)
    assert d2.approved and abs(d2.per_trade_cap - 0.25 * POT) < 1e-6


def test_confident_still_blocked_by_buying_power():
    # even max conviction can't exceed cash on hand
    d = gate(ProposedTrade("MU", 1500.0, False, conviction=8), available=1010.0, limits=CONF)
    assert not d.approved
    assert any("available funds" in r for r in d.reasons)


def test_low_conviction_still_capped_even_when_confident_sizing_on():
    # conviction 5 < bypass threshold 6 -> normal 12% cap still applies, no curve up-size
    d = gate(ProposedTrade("MU", 1000.0, False, conviction=5), available=1010.0, limits=CONF)
    assert not d.approved
    assert any("12%-of-pot" in r for r in d.reasons)
    assert abs(d.per_trade_cap - 0.12 * POT) < 1e-6  # ~$121, not $252


def test_confident_still_respects_circuit_breaker():
    # confident sizing does NOT override the daily -8% halt
    d = gate(ProposedTrade("MU", 500.0, False, conviction=8),
             net_liq=900.0, day_start=1010.0, available=900.0, limits=CONF)
    assert not d.approved
    assert any("circuit breaker" in r for r in d.reasons)


def test_confident_off_by_default():
    # default limits have confident_full_size=False -> high conviction still capped at 12%
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
    d = gate(ProposedTrade("RKLB", 50.0, False), limits=lim)  # space name Trevor kept
    assert d.approved, d.reasons


def test_blocklist_does_not_touch_index():
    lim = RiskLimits(allow_any_name=True, blocked_names={"SPY"})  # even if mistakenly listed
    assert gate(ProposedTrade("SPY", 50.0, True), limits=lim).approved


def test_index_set():
    assert INDEX_UNDERLYINGS == {"SPY", "QQQ", "IWM"}


def test_day_pnl_helper():
    assert abs(day_pnl_pct(919.0, 1010.0) + 0.0901) < 1e-3
    assert day_pnl_pct(1010.0, 0.0) == 0.0


# ---------------------------------------------------------------------------
# 2026-06-22 risk-reducing additions: 5% cash buffer + curve-driven sizing
# ---------------------------------------------------------------------------
from exitmgr.risk import curve_fraction, DEFAULT_CONVICTION_SIZE_CURVE


def test_cash_buffer_clamps_full_size():
    # confident + whole-pot curve, but the 5% cash buffer keeps ~5% of NetLiq liquid:
    # NetLiq 1000, available 1000 -> deployable = 1000 - 50 = 950 (cap can't exceed that).
    lim = RiskLimits(confident_full_size=True, allow_any_name=True, max_trade_pct_hard=1.0,
                     cash_buffer_pct=0.05,
                     conviction_size_curve={c: 1.0 for c in range(1, 11)})
    d = gate(ProposedTrade("MU", 940.0, False, conviction=10), net_liq=1000.0, available=1000.0, limits=lim)
    assert d.approved, d.reasons
    assert abs(d.per_trade_cap - 950.0) < 1e-6           # 95% of NetLiq, never 100%
    # one dollar over the buffer-clamped cap is rejected
    d2 = gate(ProposedTrade("MU", 960.0, False, conviction=10), net_liq=1000.0, available=1000.0, limits=lim)
    assert not d2.approved


def test_cash_buffer_never_negative():
    # if available is already below the buffer floor, deployable clamps to 0 (no negative cap)
    lim = RiskLimits(confident_full_size=True, allow_any_name=True, cash_buffer_pct=0.05)
    d = gate(ProposedTrade("SPY", 10.0, True, conviction=10), net_liq=1000.0, available=30.0, limits=lim)
    assert d.per_trade_cap == 0.0
    assert not d.approved


def test_cap_bypass_threshold_blocks_below():
    # bypass min 6: conviction 5 (>= old 4, < new 6) can NOT exceed the 12% base cap
    lim = RiskLimits(confident_full_size=True, allow_any_name=True,
                     cap_bypass_min_conviction=6,
                     conviction_size_curve={c: (0.25 if c >= 6 else 0.12) for c in range(1, 11)})
    d5 = gate(ProposedTrade("MU", 200.0, False, conviction=5), available=1010.0, limits=lim)
    assert not d5.approved                                # 200 > 12% of 1010 (~121)
    assert abs(d5.per_trade_cap - 0.12 * POT) < 1e-6
    d6 = gate(ProposedTrade("MU", 200.0, False, conviction=6), available=1010.0, limits=lim)
    assert d6.approved, d6.reasons                        # 6 clears bypass -> 25% cap (~252)
    assert abs(d6.per_trade_cap - 0.25 * POT) < 1e-6


def test_curve_fraction_lookup():
    curve = {1: 0.05, 5: 0.15, 10: 0.30}
    assert curve_fraction(5, curve, 0.12) == 0.15
    assert curve_fraction(1, curve, 0.12) == 0.05
    # unmapped middle value -> fallback (never silently up-sizes)
    assert curve_fraction(3, curve, 0.12) == 0.12
    # out-of-range clamps to the nearest endpoint
    assert curve_fraction(0, curve, 0.12) == 0.05
    assert curve_fraction(99, curve, 0.12) == 0.30
    # no curve / empty -> fallback
    assert curve_fraction(7, None, 0.12) == 0.12
    assert curve_fraction(7, {}, 0.12) == 0.12


def test_default_curve_is_flat_base_cap():
    # shipped default must NOT be more aggressive than today's base cap (PENDING CALIBRATION)
    assert set(DEFAULT_CONVICTION_SIZE_CURVE.values()) == {0.12}


def test_default_curve_no_upsize_without_bypass():
    # default RiskLimits (flat-0.12 curve, bypass off): even conviction 10 stays at 12%
    d = gate(ProposedTrade("SPY", 1000.0, True, conviction=10))
    assert not d.approved
    assert abs(d.per_trade_cap - 0.12 * POT) < 1e-6
