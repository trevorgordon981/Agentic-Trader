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


CONF = RiskLimits(confident_full_size=True, allow_any_name=True)


def test_confident_trade_bypasses_size_cap_up_to_funds():
    # MU-style: $1000 debit on a $1010 pot (99%), conviction 5 -> allowed when confident-sizing on
    d = gate(ProposedTrade("MU", 1000.0, False, conviction=5), available=1010.0, limits=CONF)
    assert d.approved, d.reasons
    assert abs(d.per_trade_cap - 1010.0) < 1e-6  # cap lifted to available funds


def test_confident_still_blocked_by_buying_power():
    # even max conviction can't exceed cash on hand
    d = gate(ProposedTrade("MU", 1500.0, False, conviction=5), available=1010.0, limits=CONF)
    assert not d.approved
    assert any("available funds" in r for r in d.reasons)


def test_low_conviction_still_capped_even_when_confident_sizing_on():
    # conviction 3 < threshold 4 -> normal 12% cap still applies
    d = gate(ProposedTrade("MU", 1000.0, False, conviction=3), available=1010.0, limits=CONF)
    assert not d.approved
    assert any("12%-of-pot" in r for r in d.reasons)


def test_confident_still_respects_circuit_breaker():
    # confident sizing does NOT override the daily -8% halt
    d = gate(ProposedTrade("MU", 500.0, False, conviction=5),
             net_liq=900.0, day_start=1010.0, available=900.0, limits=CONF)
    assert not d.approved
    assert any("circuit breaker" in r for r in d.reasons)


def test_confident_off_by_default():
    # default limits have confident_full_size=False -> high conviction still capped
    d = gate(ProposedTrade("SPY", 1000.0, True, conviction=5))
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
