"""Tests for the orchestrator: pure helpers + the two hard safety invariants."""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

import exitmgr.trader as trader
from exitmgr.trader import audit, day_start_pot, plan_idea, Trader
from exitmgr.risk import RiskLimits, OpenPosition
from exitmgr.strategist import TradeIdea
from exitmgr.account import PotSnapshot

LIM = RiskLimits()
IDEA = TradeIdea("SPY", True, "bullish", "long call", 7, 0.35, 90.0, 4, "trend")


def test_audit_appends_jsonl(tmp_path):
    p = tmp_path / "audit.jsonl"
    audit(str(p), "cycle_start", net_liq=1010.0)
    audit(str(p), "executed", underlying="SPY")
    rows = [json.loads(l) for l in p.read_text().splitlines()]
    assert len(rows) == 2 and rows[0]["event"] == "cycle_start" and rows[1]["underlying"] == "SPY"


def test_day_start_snapshots_and_resets():
    b, nb = day_start_pot({}, "2026-06-11", 1010.0)
    assert b == 1010.0 and nb == {"2026-06-11": 1010.0}
    # same day keeps the original baseline even as pot moves
    b2, _ = day_start_pot(nb, "2026-06-11", 950.0)
    assert b2 == 1010.0
    # new day resets baseline and drops the stale one
    b3, nb3 = day_start_pot(nb, "2026-06-12", 980.0)
    assert b3 == 980.0 and "2026-06-11" not in nb3


def test_plan_idea_gate_rejects_oversized():
    big = TradeIdea("SPY", True, "bullish", "long call", 7, 0.35, 500.0, 4, "too big")
    plan = plan_idea(big, net_liq=1010.0, available_funds=10000.0, positions=[],
                     baseline=1010.0, approved_names=set(), limits=LIM)
    assert plan.action == "gate_rejected" and not plan.gate.approved


def test_plan_idea_needs_approval_when_valid():
    plan = plan_idea(IDEA, net_liq=1010.0, available_funds=10000.0, positions=[],
                     baseline=1010.0, approved_names=set(), limits=LIM)
    assert plan.action == "needs_approval" and plan.gate.approved


def _trader(tmp_path, **over):
    ibc = MagicMock()
    ibc.ib = MagicMock()
    ibc.get_positions = AsyncMock(return_value={})
    em = MagicMock(); em.run_cycle = AsyncMock()
    t = Trader(ib_conn=ibc, exit_manager=em, limits=LIM, approved_names=set(),
               endpoint="http://x", model="m", slack_token="tok", slack_channel="C1",
               approver_ids={"OWNER"}, baseline_path=str(tmp_path / "b.json"),
               audit_path=str(tmp_path / "a.jsonl"), approve_timeout_s=60)
    from exitmgr.trader import ResolvedOrder
    import time
    resolved = ResolvedOrder(
        "SPY", "C", "20260620", 50.0, 1, 1.20, MagicMock(conId=123),
        entry_bid=1.15, entry_ask=1.25, quote_observed_at=time.monotonic(),
        decision_id="decision-" + "a" * 32)
    t._resolve_order = AsyncMock(return_value=resolved)
    t._refresh_approved_entry = AsyncMock(
        side_effect=lambda idea, original, baseline: (original, PotSnapshot(1010.0, 9000.0, 1010.0), ()))
    t._submit_order = AsyncMock(return_value=("Filled", []))
    return t


@pytest.mark.asyncio
async def test_dry_run_never_executes(tmp_path, monkeypatch):
    monkeypatch.setattr(trader.research, "gather", AsyncMock(return_value={}))  # no network in tests
    monkeypatch.setattr(trader, "_market_open", lambda: True)  # deterministic: the 6/28 market-closed
    # guard skips propose() outside RTH, which made this test time-of-day dependent (pre-existing)
    monkeypatch.setattr(trader, "get_pot_snapshot", AsyncMock(return_value=PotSnapshot(1010.0, 9000.0, 1010.0)))
    monkeypatch.setattr(trader, "propose", lambda *a, **k: [IDEA])
    posts = []
    monkeypatch.setattr(trader.approval, "post_proposal", lambda tok, ch, txt: posts.append(txt) or "ts1")
    monkeypatch.setattr(trader.approval, "await_approval", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not await approval in dry run")))
    t = _trader(tmp_path)
    await t.run_once(dry_run=True)
    t._submit_order.assert_not_called()                 # INVARIANT: dry run places nothing
    assert posts and posts[0].startswith("[DRY RUN")     # but it DOES show what it would do


@pytest.mark.asyncio
async def test_executes_only_on_approval(tmp_path, monkeypatch):
    monkeypatch.setattr(trader.research, "gather", AsyncMock(return_value={}))  # no network in tests
    monkeypatch.setattr(trader, "_market_open", lambda: True)  # deterministic (see note above)
    monkeypatch.setattr(trader, "get_pot_snapshot", AsyncMock(return_value=PotSnapshot(1010.0, 9000.0, 1010.0)))
    monkeypatch.setattr(trader, "propose", lambda *a, **k: [IDEA])
    monkeypatch.setattr(trader.approval, "post_proposal", lambda *a, **k: "ts1")
    # case 1: approved -> executes
    monkeypatch.setattr(trader.approval, "await_approval", lambda *a, **k: "approve")
    t = _trader(tmp_path)
    await t.run_once(dry_run=False)
    t._submit_order.assert_awaited_once()
    # case 2: expired -> does NOT execute
    monkeypatch.setattr(trader.approval, "await_approval", lambda *a, **k: "expired")
    t2 = _trader(tmp_path)
    await t2.run_once(dry_run=False)
    t2._submit_order.assert_not_called()


@pytest.mark.asyncio
async def test_circuit_breaker_blocks_after_8pct_drop(tmp_path, monkeypatch):
    # baseline file says today started at 1010; pot now 920 (-8.9%) -> gate halts, no approval sought
    import json as _j
    (tmp_path / "b.json").write_text(_j.dumps({str(__import__("datetime").datetime.now(__import__("datetime").timezone.utc).date()): 1010.0}))
    monkeypatch.setattr(trader.research, "gather", AsyncMock(return_value={}))  # no network in tests
    monkeypatch.setattr(trader, "get_pot_snapshot", AsyncMock(return_value=PotSnapshot(920.0, 9000.0, 920.0)))
    monkeypatch.setattr(trader, "propose", lambda *a, **k: [IDEA])
    monkeypatch.setattr(trader.approval, "await_approval", lambda *a, **k: (_ for _ in ()).throw(AssertionError("breaker should stop us first")))
    monkeypatch.setattr(trader.approval, "post_proposal", lambda *a, **k: "ts1")
    t = _trader(tmp_path)
    await t.run_once(dry_run=False)
    t._submit_order.assert_not_called()


@pytest.mark.asyncio
async def test_trader_defers_model_when_slate_active(tmp_path, monkeypatch):
    """FIX 3 (2026-06-23) soft-mutex: when the daily slate is mid-generation (slate_active flag set),
    the trader DEFERS its exit-management model call -- it passes defer_model=True to run_cycle so the
    manager skips assess_positions this tick (static rules still run). When the flag is clear it passes
    defer_model=False (model assessment proceeds as before)."""
    monkeypatch.setattr(trader.research, "gather", AsyncMock(return_value={}))
    monkeypatch.setattr(trader, "get_pot_snapshot", AsyncMock(return_value=PotSnapshot(1010.0, 9000.0, 1010.0)))
    monkeypatch.setattr(trader, "propose", lambda *a, **k: [])  # no entries; we only care about the exit-mgr call

    # slate ACTIVE -> defer
    monkeypatch.setattr(trader.slate_lock, "slate_active", lambda *a, **k: True)
    t = _trader(tmp_path)
    await t.run_once(dry_run=False)
    t.exit_manager.run_cycle.assert_awaited_once()
    assert t.exit_manager.run_cycle.await_args.kwargs.get("defer_model") is True

    # slate CLEAR -> do not defer
    monkeypatch.setattr(trader.slate_lock, "slate_active", lambda *a, **k: False)
    t2 = _trader(tmp_path)
    await t2.run_once(dry_run=False)
    t2.exit_manager.run_cycle.assert_awaited_once()
    assert t2.exit_manager.run_cycle.await_args.kwargs.get("defer_model") is False


def test_slate_lock_flag_set_clear_and_staleness(tmp_path, monkeypatch):
    """slate_lock flag-file semantics: set -> active; clear -> inactive; a flag older than the
    staleness window is treated as inactive (a crashed slate can't block the trader forever)."""
    from exitmgr import slate_lock
    flag = str(tmp_path / ".slate_active")
    monkeypatch.setenv("SLATE_ACTIVE_FLAG", flag)
    assert slate_lock.slate_active() is False          # nothing written yet
    slate_lock.mark_slate_active()
    assert slate_lock.slate_active() is True            # fresh flag -> active
    # a flag older than the window is stale -> inactive (and auto-removed)
    assert slate_lock.slate_active(stale_s=0) is False
    slate_lock.mark_slate_active()
    slate_lock.clear_slate_active()
    assert slate_lock.slate_active() is False           # cleared
