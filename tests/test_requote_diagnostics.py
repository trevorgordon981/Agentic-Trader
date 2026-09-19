"""Public API contract; production-derived narrative omitted."""
from types import SimpleNamespace as N

import pytest

from exitmgr import entry_builder as builder


class FakeIB:
    def __init__(self, legs):
        self.legs = legs

    async def reqTickersAsync(self, *contracts):
        return [N(contract=N(conId=leg.con_id), leg=leg) for leg in self.legs]


@pytest.fixture
def reprice(monkeypatch):
    async def spot(*args):
        return 100.0

    monkeypatch.setattr(builder, "RuntimeCandidate", N)
    monkeypatch.setattr(builder, "CandidateBinding", N)
    monkeypatch.setattr(builder, "_candidate_id", lambda *args: "same-candidate")
    monkeypatch.setattr(builder, "_dte", lambda value: 30)
    monkeypatch.setattr(builder, "underlying_price", spot)
    monkeypatch.setattr(builder, "_leg", lambda ticker, *args: ticker.leg)
    monkeypatch.setattr(builder, "_structure_ok", lambda *args, **kwargs: True)
    monkeypatch.setattr(builder, "combo_spread_ceiling", lambda *args: 45.0)
    monkeypatch.setattr(builder, "entry_policy", lambda *args, **kwargs:
                        N(allocation_budget_usd=442.0, live_cap_usd=442.0))
    monkeypatch.setattr(builder, "validate_runtime_candidate", lambda *args: None)

    async def call(kind, reasons):
        quotes = {
            "wide": (10, 11, 7, 8), "unafford": (10, 10.1, 4.9, 5),
            "bad_combo": (10, 11, 5, 6), "pass": (10, 10.1, 6.9, 7),
        }[kind]
        legs = [
            N(con_id=1, action="buy", right="call", strike=100,
              bid_per_share=quotes[0], ask_per_share=quotes[1], delta=.6, iv=.3),
            N(con_id=2, action="sell", right="call", strike=110 if kind == "unafford" else 105,
              bid_per_share=quotes[2], ask_per_share=quotes[3], delta=.5, iv=.3),
        ]
        binding = N(candidate=N(intent_id="test", candidate_id="same-candidate",
                                expiry="20990101", legs=legs),
                    underlying_contract=N(), primary_contract=N(), secondary_contract=N())
        intent = N(side="debit", underlying="TEST", direction="bullish", structure="call debit spread")
        return await builder.reprice_binding(
            FakeIB(legs), binding, intent, net_liq=4420, available_funds=2000,
            cons=N(max_entry_spread_pct=25), reasons=reasons)

    return call


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "expected"), [
    ("wide", "requote rejected: wide(spread=66.7 max=45.0 cost=400.00)"),
    ("unafford", "requote rejected: unafford(cost=520.00 live_cap=442.00)"),
    ("bad_combo", "requote rejected: bad_combo(bid=4.00 ask=6.00 width=5.00)"),
])
async def test_rejected_requote_retains_actual_guard_reason(reprice, kind, expected):
    reasons = []
    assert await reprice(kind, reasons) is None
    assert reasons == [expected]


@pytest.mark.asyncio
async def test_success_preserves_candidate_identity_and_economics(reprice):
    reasons = []
    refreshed = await reprice("pass", reasons)
    assert reasons == []
    candidate = refreshed.candidate
    assert candidate.candidate_id == "same-candidate"
    assert candidate.one_contract_cost_usd == pytest.approx(320.0)
    assert candidate.max_affordable_quantity == 1
    assert candidate.combo_bid_per_share == pytest.approx(3.0)
    assert candidate.combo_ask_per_share == pytest.approx(3.2)
    assert candidate.live_cap_usd == 442.0


@pytest.mark.asyncio
async def test_optional_reasons_still_rejects_wide_quote(reprice):
    assert await reprice("wide", None) is None


@pytest.mark.asyncio
async def test_missing_guard_detail_stays_fail_closed(reprice, monkeypatch):
    monkeypatch.setattr(builder, "_make_candidate", lambda **kwargs: None)
    reasons = []
    assert await reprice("pass", reasons) is None
    assert reasons == ["requote rejected: candidate validation failed without a reason"]
