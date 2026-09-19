"""Public API contract; production-derived narrative omitted."""
from unittest.mock import AsyncMock, MagicMock

import pytest

import daily_recommend as dr
from exitmgr.account import PotSnapshot
from exitmgr.strategist import TradeIdea
from exitmgr.trader import (
    ResolvedOrder,
    contract_snapshot,
    earnings_reconsideration_order_snapshot,
    earnings_reconsideration_receipt_valid,
    earnings_reconsideration_snapshot_sha256,
)


def test_daily_protective_terms_are_order_authority_and_receipt_bound():
    resolved = ResolvedOrder(
        "AVGO", "C", "20261016", 400.0, 1, 3.20,
        MagicMock(conId=456, right="C", strike=400.0),
        entry_bid=3.10, entry_ask=3.20,
    )

    dr.bind_protective_terms(resolved, None, 30.0)
    assert resolved.tp_pct is None
    assert resolved.sl_pct == 30.0
    assert contract_snapshot(resolved)["profit_target_pct"] is None
    assert contract_snapshot(resolved)["stop_pct"] == 30.0

    reviewed = earnings_reconsideration_order_snapshot(resolved)
    resolved.earnings_reconsidered = True
    resolved.earnings_reconsideration_decision = "proceed"
    resolved.earnings_reconsideration_order_snapshot = reviewed
    resolved.earnings_reconsideration_order_sha256 = (
        earnings_reconsideration_snapshot_sha256(reviewed)
    )
    assert earnings_reconsideration_receipt_valid(resolved)



    dr.bind_protective_terms(resolved, 40.0, 25.0)
    assert resolved.tp_pct == 40.0
    assert resolved.sl_pct == 25.0
    assert not earnings_reconsideration_receipt_valid(resolved)


@pytest.mark.asyncio
async def test_daily_csp_gets_same_day_warning_without_becoming_blocked(tmp_path, monkeypatch):
    contract = MagicMock(conId=123, right="P", strike=50.0)
    resolved = ResolvedOrder(
        "AVGO", "P", "20261016", 50.0, 1, 2.00, contract,
        dte=43, intended_hold_days=10, side="credit",
        structure="cash secured put", collateral_usd=5000.0,
        net_credit_usd=200.0, credit_max_loss_usd=4800.0,
    )
    idea = TradeIdea(
        "AVGO", False, "bullish", "cash secured put", 43, 0.20, 0.0, 7,
        "same-day event risk explicitly underwritten", intended_hold_days=10,
        side="credit", collateral_usd=5000.0, net_credit_usd=200.0,
        max_loss_usd=4800.0, strike=50.0,
    )


    idea._stage_b_binding = object()
    posts = []
    monkeypatch.setattr(dr, "_resolve", AsyncMock(return_value=(resolved, None)))
    monkeypatch.setattr(dr, "broker_deployed_csp_collateral",
                        AsyncMock(return_value=0.0))
    monkeypatch.setattr(dr, "_short_option_entry_authority",
                        lambda _obj: MagicMock(allowed=True, reasons=()))
    monkeypatch.setattr(dr.research, "days_to_earnings", lambda *_a, **_k: 0)
    monkeypatch.setattr(dr.research, "days_to_ex_dividend", lambda *_a, **_k: None)
    monkeypatch.setattr(dr.approval, "post_proposal",
                        lambda _t, _c, text, **_kw: posts.append(text) or "ts1")
    monkeypatch.setattr(dr.trade_capture, "capture_decision", lambda *a, **k: None)

    pending = []
    ts = await dr._post_idea(
        MagicMock(), idea, PotSnapshot(10_000.0, 9_000.0, 10_000.0), 0.12,
        "tok", "C1", str(tmp_path / "audit.jsonl"), pending,
        candidates=[idea], raw_strategist="{}", market_context="brief",
    )

    assert ts == "ts1"
    assert resolved.earnings_day_warning is True
    assert resolved.earnings_warn.startswith("EARNINGS TODAY")
    assert posts and posts[0].startswith(
        ":rotating_light: *EARNINGS TODAY — ENTRY WARNING*")
    assert len(pending) == 1
    audit_text = (tmp_path / "audit.jsonl").read_text()
    assert "earnings_same_day_warning" in audit_text
    assert "earnings_blackout_rejected" not in audit_text
