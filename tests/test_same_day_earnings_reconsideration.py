import json

import pytest

import exitmgr.strategist as strategist
import exitmgr.trader as trader
from exitmgr.entry_contract import EntryContractError


def _raw(**overrides):
    obj = {
        "decision": "proceed",
        "event_phase": "pre_release",
        "reason": "Defined risk remains acceptable after explicit event underwriting.",
    }
    obj.update(overrides)
    return json.dumps(obj)


def test_strict_same_day_review_parser_accepts_exact_contract():
    review = strategist.parse_same_day_earnings_review(_raw())
    assert review.proceed is True
    assert review.event_phase == "pre_release"


@pytest.mark.parametrize("raw", [
    _raw(extra=True),
    json.dumps({"decision": "proceed", "event_phase": "pre_release"}),
    _raw(decision="PROCEED"),
    _raw(event_phase="reported"),
    _raw(reason="too short"),
    '{"decision":"proceed","decision":"decline","event_phase":"unknown",'
    '"reason":"This duplicate decision must be rejected by the parser."}',
    _raw() + " trailing",
])
def test_strict_same_day_review_parser_rejects_ambiguous_or_malformed_output(raw):
    with pytest.raises(EntryContractError):
        strategist.parse_same_day_earnings_review(raw)


def test_same_day_review_wrapper_validates_before_return(monkeypatch):
    seen = {}

    def fake_post(endpoint, body, timeout, **kwargs):
        seen.update(endpoint=endpoint, body=body, timeout=timeout)
        response = {"choices": [{"message": {"content": _raw()}}]}
        kwargs["response_validator"]((response, {"verified": True}))
        return response, {"verified": True}

    monkeypatch.setattr(strategist, "_post_json", fake_post)
    result = strategist.reconsider_same_day_earnings(
        "http://model", "model", "market brief", {"exact_order": {"quantity": 1}},
        return_identity=True)
    review, raw, cot, identity = result
    assert review.proceed is True
    assert raw == _raw()
    assert cot is None
    assert identity == {"verified": True}
    assert seen["body"]["thinking"] == "disabled"
    assert seen["body"]["temperature"] == 0.0


def test_review_receipt_is_bound_to_material_order_envelope():
    order = trader.ResolvedOrder(
        "AVGO", "C", "20270115", 400.0, 1, 1.00, object(),
        short_strike=410.0, short_contract=object(), structure="call debit spread",
        tp_pct=None, sl_pct=30.0, earnings_date="2026-09-03",
        earnings_day_warning=True, earnings_reconsidered=True,
        earnings_reconsideration_decision="proceed")
    order.entry_bid = 0.95
    order.entry_ask = 1.00
    order.earnings_reconsideration_order_snapshot = (
        trader.earnings_reconsideration_order_snapshot(order))
    order.earnings_reconsideration_order_sha256 = (
        trader.earnings_reconsideration_snapshot_sha256(
            order.earnings_reconsideration_order_snapshot))
    assert trader.earnings_reconsideration_receipt_valid(order)
    order.entry_ask = 1.03
    assert trader.earnings_reconsideration_receipt_valid(order)
    order.entry_ask = 1.04
    assert not trader.earnings_reconsideration_receipt_valid(order)
    order.entry_ask = 1.00
    order.qty = 2
    assert not trader.earnings_reconsideration_receipt_valid(order)


def test_review_receipt_rejects_tampered_snapshot_digest():
    order = trader.ResolvedOrder(
        "AVGO", "C", "20270115", 400.0, 1, 1.00, object(),
        short_strike=410.0, short_contract=object(), structure="call debit spread",
        tp_pct=None, sl_pct=30.0, earnings_date="2026-09-03",
        earnings_day_warning=True, earnings_reconsidered=True,
        earnings_reconsideration_decision="proceed")
    order.entry_bid = 0.95
    order.entry_ask = 1.00
    order.earnings_reconsideration_order_snapshot = (
        trader.earnings_reconsideration_order_snapshot(order))
    order.earnings_reconsideration_order_sha256 = "0" * 64
    assert not trader.earnings_reconsideration_receipt_valid(order)
