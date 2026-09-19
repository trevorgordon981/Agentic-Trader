from exitmgr.config import ConstructionConfig
from exitmgr.entry_builder import _make_candidate, combo_spread_ceiling
from exitmgr.entry_contract import RuntimeLeg, StageAIntent
from exitmgr.entry_safety import LEVERAGED_ETFS, is_leveraged_etf


def _intent(symbol):
    return StageAIntent(
        underlying=symbol, side="debit", direction="bullish",
        structure="call debit spread", target_dte=90, intended_hold_days=10,
        target_delta=0.60, conviction=7, allocation_pct_net_liq=8,
        alpha="test", thesis="test",
    )


def _legs():

    return [
        RuntimeLeg(con_id=1, action="buy", right="call", strike=19.0,
                   bid_per_share=4.00, ask_per_share=5.00, delta=0.60, iv=0.80),
        RuntimeLeg(con_id=2, action="sell", right="call", strike=26.0,
                   bid_per_share=2.25, ask_per_share=2.34, delta=0.35, iv=0.80),
    ]


def _candidate(symbol, ceiling):
    return _make_candidate(
        intent_id="intent_1", intent=_intent(symbol), expiry="20261218", dte=113,
        legs=_legs(), allocation=400.0, live_cap=400.0,
        quote_utc="2026-08-27T00:00:00Z", quote_mono=1.0,
        max_spread_pct=25.0, max_combo_spread_pct=ceiling,
    )


def test_only_explicit_leveraged_etfs_receive_50_percent_ceiling():
    cfg = ConstructionConfig()
    assert LEVERAGED_ETFS == frozenset({"NVDX", "SOXL", "SYMX", "USD"})
    for symbol in LEVERAGED_ETFS:
        assert is_leveraged_etf(symbol)
        assert combo_spread_ceiling(cfg, symbol) == 50.0
        assert _candidate(symbol, combo_spread_ceiling(cfg, symbol)) is not None


def test_identical_wide_quote_still_fails_for_every_nonleveraged_name():
    cfg = ConstructionConfig()
    for symbol in ("CRM", "NVDA", "SPY", "QQQ", "DRAQ"):
        assert not is_leveraged_etf(symbol)
        assert combo_spread_ceiling(cfg, symbol) == 45.0
        reasons = []
        assert _make_candidate(
            intent_id="intent_1", intent=_intent(symbol), expiry="20261218", dte=113,
            legs=_legs(), allocation=400.0, live_cap=400.0,
            quote_utc="2026-08-27T00:00:00Z", quote_mono=1.0,
            max_spread_pct=25.0, max_combo_spread_pct=45.0, why=reasons,
        ) is None
        assert any("max=45.0" in reason for reason in reasons)


def test_leveraged_exception_is_bounded_at_50_not_unlimited():
    cfg = ConstructionConfig()
    assert _candidate("NVDX", combo_spread_ceiling(cfg, "NVDX")) is not None
    assert _candidate("NVDX", 49.0) is None
