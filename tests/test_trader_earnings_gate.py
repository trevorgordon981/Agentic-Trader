"""Public API contract; production-derived narrative omitted."""
from datetime import datetime, timedelta, timezone

import pytest
from unittest.mock import AsyncMock, MagicMock

import exitmgr.trader as trader
from exitmgr.trader import Trader, ResolvedOrder
from exitmgr.risk import RiskLimits
from exitmgr.strategist import SameDayEarningsReview, TradeIdea
from exitmgr.account import PotSnapshot
from exitmgr.config import ConstructionConfig
from exitmgr.state import StateManager
from exitmgr.entry_safety import (
    AUTONOMOUS_WITHIN_RISK_GATES,
    HUMAN_SLACK_APPROVAL,
    EntrySubmissionAuthority,
    SafetyResult,
)

LIM = RiskLimits()
IDEA = TradeIdea("SPY", True, "bullish", "long call", 7, 0.35, 90.0, 4, "trend")


def _expiry_str(days_from_today: int) -> str:
    d = datetime.now(timezone.utc).date() + timedelta(days=days_from_today)
    return d.strftime("%Y%m%d")


def _trader(tmp_path, resolved, *, hard=False, auto=False):
    ibc = MagicMock()
    ibc.ib = MagicMock()
    ibc.ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[])
    ibc.get_positions = AsyncMock(return_value={})
    em = MagicMock(); em.run_cycle = AsyncMock()



    em.state_manager = StateManager(str(tmp_path / "state.json"))
    t = Trader(ib_conn=ibc, exit_manager=em, limits=LIM, approved_names={"AVGO"},
               endpoint="http://x", model="m", slack_token="tok", slack_channel="C1",
               approver_ids={"OWNER"}, baseline_path=str(tmp_path / "b.json"),
               audit_path=str(tmp_path / "a.jsonl"), approve_timeout_s=60,
               auto_approve_within_gates=auto,
               kill_switch_path=str(tmp_path / "NO_KILL_SWITCH"),
               construction_cfg=ConstructionConfig(earnings_blackout_enabled=True,
                                                     earnings_block_hard=hard))
    t._resolve_order = AsyncMock(return_value=resolved)
    resolved.entry_bid = 1.15
    resolved.entry_ask = 1.25
    resolved.quote_observed_at = __import__("time").monotonic()
    resolved.decision_id = "decision-" + "a" * 32
    t._refresh_approved_entry = AsyncMock(
        side_effect=lambda idea, original, baseline: (
            original, PotSnapshot(1010.0, 9000.0, 1010.0), ()))
    t._submit_order = AsyncMock(return_value=("Filled", []))
    return t


def _assert_submitted_with(t, resolved, expected_mode):
    t._submit_order.assert_awaited_once()
    call = t._submit_order.await_args
    assert call.args == (resolved,)
    authority = call.kwargs.get("submission_authority")
    assert isinstance(authority, EntrySubmissionAuthority)
    assert authority.mode == expected_mode


def _wire(monkeypatch, posts, days_to_earnings):
    monkeypatch.setattr(trader.research, "gather", AsyncMock(return_value={}))
    monkeypatch.setattr(trader, "_market_open", lambda: True)
    monkeypatch.setattr(trader, "get_pot_snapshot",
                        AsyncMock(return_value=PotSnapshot(1010.0, 9000.0, 1010.0)))
    monkeypatch.setattr(trader, "propose", lambda *a, **k: [IDEA])



    monkeypatch.setattr(trader, "propose_intents",
                        lambda *a, **k: ([IDEA], "", None, None))
    async def _stage_b(self, intents, pot):
        return list(intents or [])
    monkeypatch.setattr(trader.Trader, "_materialize_stage_b", _stage_b)
    monkeypatch.setattr(trader.approval, "post_proposal",
                        lambda tok, ch, txt, **kwargs: posts.append(txt) or "ts1")
    monkeypatch.setattr(trader.approval, "await_approval", lambda *a, **k: "approve")

    monkeypatch.setattr(trader.research, "days_to_earnings", lambda *a, **k: days_to_earnings)


@pytest.mark.asyncio
async def test_trader_allows_and_discloses_earnings_overlap(tmp_path, monkeypatch):

    resolved = ResolvedOrder("SPY", "C", _expiry_str(120), 50.0, 1, 1.20, object(), dte=120)
    posts = []
    _wire(monkeypatch, posts, days_to_earnings=30)
    t = _trader(tmp_path, resolved)
    await t.run_once(dry_run=False)
    t._submit_order.assert_awaited_once()
    assert any("holding through earnings is allowed" in p.lower() for p in posts), posts
    audit_rows = (tmp_path / "a.jsonl").read_text()
    assert "earnings_overlap_disclosed" in audit_rows
    assert "earnings_blackout_rejected" not in audit_rows
    assert resolved.earnings_warn
    assert resolved.earnings_date


@pytest.mark.asyncio
async def test_trader_allows_and_prominently_flags_same_day_earnings(tmp_path, monkeypatch):
    resolved = ResolvedOrder("AVGO", "C", _expiry_str(120), 400.0, 1, 1.00,
                             object(), short_strike=410.0,
                             short_contract=object(), dte=120)
    posts = []
    _wire(monkeypatch, posts, days_to_earnings=0)
    avgo_idea = TradeIdea("AVGO", False, "bullish", "call debit spread", 7,
                          0.35, 100.0, 6, "same-day event risk explicitly underwritten")
    monkeypatch.setattr(trader, "propose", lambda *a, **k: [avgo_idea])
    monkeypatch.setattr(trader, "propose_intents",
                        lambda *a, **k: ([avgo_idea], "", None, None))
    t = _trader(tmp_path, resolved, hard=True)
    await t.run_once(dry_run=False)

    t._submit_order.assert_awaited_once()
    assert resolved.earnings_day_warning is True
    assert resolved.earnings_date == datetime.now(timezone.utc).date().isoformat()
    assert resolved.earnings_warn.startswith("EARNINGS TODAY")
    assert any(p.startswith(":rotating_light: *EARNINGS TODAY — ENTRY WARNING*")
               for p in posts), posts
    audit_rows = (tmp_path / "a.jsonl").read_text()
    assert "earnings_same_day_warning" in audit_rows
    assert "earnings_blackout_rejected" not in audit_rows
    entry = t._entry_record(resolved)
    assert entry["earnings_day_warning"] is True
    assert entry["earnings_overlap_warning"].startswith("EARNINGS TODAY")
    snapshot = trader.contract_snapshot(resolved)
    assert snapshot["earnings_day_warning"] is True
    assert snapshot["earnings_date"] == resolved.earnings_date


@pytest.mark.asyncio
async def test_final_refresh_rechecks_and_persists_same_day_warning(tmp_path, monkeypatch):
    original = ResolvedOrder("AVGO", "C", _expiry_str(120), 400.0, 1, 1.00,
                             object(), short_strike=410.0,
                             short_contract=object(), dte=120,
                             intended_hold_days=15)
    fresh = ResolvedOrder("AVGO", "C", original.expiry, 400.0, 1, 1.00,
                          object(), short_strike=410.0,
                          short_contract=object(), dte=120)
    fresh.entry_bid = 0.95
    fresh.entry_ask = 1.05
    fresh.quote_observed_at = __import__("time").monotonic()
    posts = []
    _wire(monkeypatch, posts, days_to_earnings=0)
    idea = TradeIdea("AVGO", False, "bullish", "call debit spread", 7,
                     0.35, 100.0, 6, "same-day event risk explicitly underwritten")
    t = _trader(tmp_path, original, hard=True)
    t._resolve_order = AsyncMock(return_value=fresh)
    t._open_positions = AsyncMock(return_value=[])
    monkeypatch.setattr(t, "_entry_markers_clear", lambda: MagicMock(reasons=()))

    refreshed, _pot, reasons = await Trader._refresh_approved_entry(
        t, idea, original, baseline=1010.0)

    assert reasons == ()
    assert refreshed.earnings_day_warning is True
    assert refreshed.earnings_warn.startswith("EARNINGS TODAY")
    audit_rows = (tmp_path / "a.jsonl").read_text()
    assert '"phase": "post_approval"' in audit_rows
    assert "earnings_same_day_warning" in audit_rows


@pytest.mark.asyncio
async def test_trader_legacy_hard_disposition_requires_explicit_opt_in(tmp_path, monkeypatch):
    resolved = ResolvedOrder("SPY", "C", _expiry_str(120), 50.0, 1, 1.20, object(), dte=120)
    posts = []
    _wire(monkeypatch, posts, days_to_earnings=30)
    t = _trader(tmp_path, resolved, hard=True)
    await t.run_once(dry_run=False)
    t._submit_order.assert_not_called()
    assert "earnings_blackout_rejected" in (tmp_path / "a.jsonl").read_text()


@pytest.mark.asyncio
async def test_trader_passes_debit_with_earnings_after_expiry(tmp_path, monkeypatch):

    resolved = ResolvedOrder("SPY", "C", _expiry_str(20), 50.0, 1, 1.20, object(), dte=20)
    posts = []
    _wire(monkeypatch, posts, days_to_earnings=90)
    t = _trader(tmp_path, resolved)
    await t.run_once(dry_run=False)
    t._submit_order.assert_awaited_once()

    assert resolved.earnings_unchecked is False
    assert not any("UNCHECKED" in p for p in posts), posts
    assert "earnings_blackout_rejected" not in (tmp_path / "a.jsonl").read_text()


@pytest.mark.asyncio
async def test_trader_proposal_flags_unknown_before_mocked_final_refresh(tmp_path, monkeypatch):



    resolved = ResolvedOrder("SPY", "C", _expiry_str(120), 50.0, 1, 1.20, object(), dte=120)
    posts = []
    _wire(monkeypatch, posts, days_to_earnings=None)
    t = _trader(tmp_path, resolved)
    await t.run_once(dry_run=False)
    t._submit_order.assert_awaited_once()
    assert resolved.earnings_unchecked is True
    assert any("UNCHECKED" in p for p in posts), posts
    assert "earnings_unchecked" in (tmp_path / "a.jsonl").read_text()


def _late_today(original):
    fresh = ResolvedOrder(
        original.underlying, original.right, original.expiry, original.strike,
        original.qty, original.limit, original.contract,
        short_strike=original.short_strike, short_contract=original.short_contract,
        dte=original.dte, intended_hold_days=original.intended_hold_days)
    fresh.decision_id = original.decision_id
    fresh.entry_bid = original.entry_bid or 1.15
    fresh.entry_ask = original.entry_ask or 1.25
    fresh.tp_pct = None
    fresh.sl_pct = 30.0
    fresh.quote_observed_at = __import__("time").monotonic()
    fresh.earnings_date = datetime.now(timezone.utc).date().isoformat()
    fresh.earnings_day_warning = True
    fresh.earnings_warn = "EARNINGS TODAY — exact release timing remains unverified"
    return fresh


@pytest.mark.asyncio
async def test_late_same_day_model_proceeds_then_forces_new_refresh_before_submit(
        tmp_path, monkeypatch):
    original = ResolvedOrder("AVGO", "C", _expiry_str(120), 400.0, 1, 1.00,
                             object(), short_strike=410.0, short_contract=object(),
                             dte=120, intended_hold_days=15)
    fresh = _late_today(original)
    posts = []
    _wire(monkeypatch, posts, days_to_earnings=None)
    idea = TradeIdea("AVGO", False, "bullish", "call debit spread", 120,
                     0.60, 100.0, 6, "trend", intended_hold_days=15)
    monkeypatch.setattr(trader, "propose_intents",
                        lambda *a, **k: ([idea], "", None, None))
    model_review = MagicMock(return_value=(
        SameDayEarningsReview("proceed", "pre_release",
                              "Defined risk remains acceptable despite the binary event."),
        "{}", None, {"verified": True}))
    monkeypatch.setattr(trader, "reconsider_same_day_earnings", model_review)
    t = _trader(tmp_path, original)
    t._refresh_approved_entry = AsyncMock(side_effect=[
        (fresh, PotSnapshot(1010.0, 9000.0, 1010.0), ()),
        (fresh, PotSnapshot(1010.0, 9000.0, 1010.0), ()),
    ])

    await t.run_once(dry_run=False)

    assert t._refresh_approved_entry.await_count == 2
    model_review.assert_called_once()
    _assert_submitted_with(t, fresh, HUMAN_SLACK_APPROVAL)
    assert fresh.earnings_reconsidered is True
    assert fresh.earnings_reconsideration_decision == "proceed"
    assert any("MODEL RECONSIDERATION PASSED" in p for p in posts)


@pytest.mark.asyncio
async def test_human_approval_expiring_during_late_review_never_submits(
        tmp_path, monkeypatch):
    original = ResolvedOrder("AVGO", "C", _expiry_str(120), 400.0, 1, 1.00,
                             object(), short_strike=410.0, short_contract=object(),
                             dte=120, intended_hold_days=15)
    reviewed = _late_today(original)
    posts = []
    _wire(monkeypatch, posts, days_to_earnings=None)
    idea = TradeIdea("AVGO", False, "bullish", "call debit spread", 120,
                     0.60, 100.0, 6, "trend", intended_hold_days=15)
    monkeypatch.setattr(trader, "propose_intents",
                        lambda *a, **k: ([idea], "", None, None))
    monkeypatch.setattr(trader, "reconsider_same_day_earnings", MagicMock(return_value=(
        SameDayEarningsReview("proceed", "pre_release",
                              "Defined risk remains acceptable despite the binary event."),
        "{}", None, {"verified": True})))
    ages = iter((SafetyResult(True),
                 SafetyResult(False, ("approval expired during model review",))))
    monkeypatch.setattr(trader.entry_safety, "approval_expired",
                        lambda *a, **k: next(ages))
    t = _trader(tmp_path, original)
    t._refresh_approved_entry = AsyncMock(side_effect=[
        (reviewed, PotSnapshot(1010.0, 9000.0, 1010.0), ()),
        (reviewed, PotSnapshot(1010.0, 9000.0, 1010.0), ()),
    ])

    await t.run_once(dry_run=False)

    t._submit_order.assert_not_called()
    assert any("Approval expired during final review" in p for p in posts)
    assert "approval_expired_pre_submit" in (tmp_path / "a.jsonl").read_text()


@pytest.mark.asyncio
async def test_material_post_review_move_is_reconsidered_again_before_submit(
        tmp_path, monkeypatch):
    original = ResolvedOrder("AVGO", "C", _expiry_str(120), 400.0, 1, 1.00,
                             object(), short_strike=410.0, short_contract=object(),
                             dte=120, intended_hold_days=15)
    reviewed_a = _late_today(original)
    moved_b = _late_today(original)
    moved_b.limit = 1.20
    moved_b.entry_bid = 1.35
    moved_b.entry_ask = 1.45
    moved_b.earnings_reconsidered = True
    moved_b.earnings_reconsideration_decision = "proceed"
    moved_b.earnings_reconsideration_order_snapshot = (
        trader.earnings_reconsideration_order_snapshot(reviewed_a))
    moved_b.earnings_reconsideration_order_sha256 = (
        trader.earnings_reconsideration_snapshot_sha256(
            moved_b.earnings_reconsideration_order_snapshot))
    posts = []
    _wire(monkeypatch, posts, days_to_earnings=None)
    idea = TradeIdea("AVGO", False, "bullish", "call debit spread", 120,
                     0.60, 100.0, 6, "trend", intended_hold_days=15)
    monkeypatch.setattr(trader, "propose_intents",
                        lambda *a, **k: ([idea], "", None, None))
    model_review = MagicMock(return_value=(
        SameDayEarningsReview("proceed", "pre_release",
                              "Defined risk remains acceptable despite the binary event."),
        "{}", None, {"verified": True}))
    monkeypatch.setattr(trader, "reconsider_same_day_earnings", model_review)
    approvals = MagicMock(return_value="approve")
    monkeypatch.setattr(trader.approval, "await_approval", approvals)
    t = _trader(tmp_path, original)


    t._refresh_approved_entry = AsyncMock(side_effect=[
        (reviewed_a, PotSnapshot(1010.0, 9000.0, 1010.0), ()),
        (moved_b, PotSnapshot(1010.0, 9000.0, 1010.0), ()),
        (moved_b, PotSnapshot(1010.0, 9000.0, 1010.0), ()),
        (moved_b, PotSnapshot(1010.0, 9000.0, 1010.0), ()),
    ])

    await t.run_once(dry_run=False)

    assert model_review.call_count == 2
    assert approvals.call_count == 2
    _assert_submitted_with(t, moved_b, HUMAN_SLACK_APPROVAL)
    assert trader.earnings_reconsideration_receipt_valid(moved_b)
    assert moved_b.earnings_reconsideration_order_snapshot["executable_limit"] == 1.45


@pytest.mark.asyncio
async def test_autonomous_late_earnings_refresh_never_waits_for_human_approval(
        tmp_path, monkeypatch):
    original = ResolvedOrder("AVGO", "C", _expiry_str(120), 400.0, 1, 1.00,
                             object(), short_strike=410.0, short_contract=object(),
                             dte=120, intended_hold_days=15)
    reviewed_a = _late_today(original)
    moved_b = _late_today(original)
    moved_b.limit = 1.20
    moved_b.entry_bid = 1.35
    moved_b.entry_ask = 1.45
    moved_b.earnings_reconsidered = True
    moved_b.earnings_reconsideration_decision = "proceed"
    moved_b.earnings_reconsideration_order_snapshot = (
        trader.earnings_reconsideration_order_snapshot(reviewed_a))
    moved_b.earnings_reconsideration_order_sha256 = (
        trader.earnings_reconsideration_snapshot_sha256(
            moved_b.earnings_reconsideration_order_snapshot))
    posts = []
    _wire(monkeypatch, posts, days_to_earnings=None)
    idea = TradeIdea("AVGO", False, "bullish", "call debit spread", 120,
                     0.60, 100.0, 6, "trend", intended_hold_days=15)
    monkeypatch.setattr(trader, "propose_intents",
                        lambda *a, **k: ([idea], "", None, None))
    model_review = MagicMock(return_value=(
        SameDayEarningsReview("proceed", "pre_release",
                              "Defined risk remains acceptable despite the binary event."),
        "{}", None, {"verified": True}))
    monkeypatch.setattr(trader, "reconsider_same_day_earnings", model_review)
    approvals = MagicMock(side_effect=AssertionError("autonomous route must not await a tap"))
    monkeypatch.setattr(trader.approval, "await_approval", approvals)
    t = _trader(tmp_path, original, auto=True)
    t.limits.sector_map["AVGO"] = "semiconductors"
    t._price_stats = {"AVGO": {"close": 400.0}}
    t._market_context = AsyncMock(return_value="grounded market brief")
    t._refresh_approved_entry = AsyncMock(side_effect=[
        (reviewed_a, PotSnapshot(1010.0, 9000.0, 1010.0), ()),
        (moved_b, PotSnapshot(1010.0, 9000.0, 1010.0), ()),
        (moved_b, PotSnapshot(1010.0, 9000.0, 1010.0), ()),
    ])

    await t.run_once(dry_run=False)

    approvals.assert_not_called()
    assert model_review.call_count == 2
    _assert_submitted_with(t, moved_b, AUTONOMOUS_WITHIN_RISK_GATES)
    assert trader.earnings_reconsideration_receipt_valid(moved_b)
    assert any("AUTO-APPROVED TERMS REFRESHED" in p for p in posts)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["decline", "failure"])
async def test_late_same_day_decline_or_provider_failure_never_submits(
        tmp_path, monkeypatch, outcome):
    original = ResolvedOrder("AVGO", "C", _expiry_str(120), 400.0, 1, 1.00,
                             object(), short_strike=410.0, short_contract=object(),
                             dte=120, intended_hold_days=15)
    fresh = _late_today(original)
    posts = []
    _wire(monkeypatch, posts, days_to_earnings=None)
    idea = TradeIdea("AVGO", False, "bullish", "call debit spread", 120,
                     0.60, 100.0, 6, "trend", intended_hold_days=15)
    monkeypatch.setattr(trader, "propose_intents",
                        lambda *a, **k: ([idea], "", None, None))
    if outcome == "decline":
        monkeypatch.setattr(trader, "reconsider_same_day_earnings", MagicMock(return_value=(
            SameDayEarningsReview("decline", "unknown",
                                  "Release timing is unknown and gap risk is not underwritten."),
            "{}", None, {"verified": True})))
    else:
        monkeypatch.setattr(
            trader, "reconsider_same_day_earnings",
            MagicMock(side_effect=RuntimeError("provider details must stay internal")))
    t = _trader(tmp_path, original)
    t._refresh_approved_entry = AsyncMock(return_value=(
        fresh, PotSnapshot(1010.0, 9000.0, 1010.0), ()))

    await t.run_once(dry_run=False)

    t._submit_order.assert_not_called()
    assert any("NOT placed" in p for p in posts)
    if outcome == "failure":
        assert all("provider details" not in p for p in posts)


@pytest.mark.asyncio
async def test_already_known_same_day_does_not_reconsider_twice(tmp_path, monkeypatch):
    original = ResolvedOrder("AVGO", "C", _expiry_str(120), 400.0, 1, 1.00,
                             object(), short_strike=410.0, short_contract=object(),
                             dte=120, intended_hold_days=15,
                             earnings_day_warning=True)
    fresh = _late_today(original)
    review = MagicMock(side_effect=AssertionError("must not re-review known event"))
    monkeypatch.setattr(trader, "reconsider_same_day_earnings", review)
    posts = []
    allowed, triggered = await trader.reconsider_new_same_day_earnings(
        endpoint="http://x", model="m", market_context="brief", idea=IDEA,
        previous=original, fresh=fresh, slack_token="tok", slack_channel="C1",
        audit_path=str(tmp_path / "a.jsonl"), decision_id="decision-" + "b" * 32)
    assert (allowed, triggered) == (True, False)
    review.assert_not_called()
