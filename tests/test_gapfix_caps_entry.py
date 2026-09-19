"""Public API contract; production-derived narrative omitted."""
import pytest
from unittest.mock import AsyncMock, MagicMock

import exitmgr.trader as trader
from exitmgr.trader import Trader, ResolvedOrder
from exitmgr.risk import RiskLimits
from exitmgr.strategist import TradeIdea
from exitmgr.account import PotSnapshot
from exitmgr.state import StateManager
from tests._stage_stub import stub_stage_a

LIM = RiskLimits(max_concurrent=8)

IDEA = TradeIdea("SPY", True, "bullish", "long call", 7, 0.35, 90.0, 4, "trend")


def _trader(tmp_path, *, n_ideas, state_manager=None, **caps):
    ibc = MagicMock(); ibc.ib = MagicMock()
    ibc.ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[])
    ibc.get_positions = AsyncMock(return_value={})
    em = MagicMock(); em.run_cycle = AsyncMock()
    if state_manager is not None:
        em.state_manager = state_manager
    t = Trader(ib_conn=ibc, exit_manager=em, limits=LIM, approved_names=set(),
               endpoint="http://x", model="m", slack_token="tok", slack_channel="C1",
               approver_ids={"OWNER"}, baseline_path=str(tmp_path / "b.json"),
               audit_path=str(tmp_path / "a.jsonl"), approve_timeout_s=60,
               trading_down_path=str(tmp_path / "TRADING_DOWN"), **caps)

    resolved = ResolvedOrder(
        "SPY", "C", "20260620", 50.0, 1, 1.20, MagicMock(conId=123),
        entry_bid=1.15, entry_ask=1.25,
        quote_observed_at=__import__("time").monotonic(),
        decision_id="decision-" + "a" * 32)
    t._resolve_order = AsyncMock(return_value=resolved)
    t._refresh_approved_entry = AsyncMock(
        side_effect=lambda idea, original, baseline: (
            original, PotSnapshot(1010.0, 9000.0, 1010.0), ()))
    t._submit_order = AsyncMock(return_value=("Filled", []))
    t._n_ideas = n_ideas
    return t


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    monkeypatch.setattr(trader.research, "gather", AsyncMock(return_value={}))
    monkeypatch.setattr(trader.research, "days_to_earnings", lambda *a, **k: None)
    monkeypatch.setattr(trader.research, "days_to_ex_dividend", lambda *a, **k: None)
    monkeypatch.setattr(trader, "_market_open", lambda: True)
    monkeypatch.setattr(trader, "get_pot_snapshot", AsyncMock(return_value=PotSnapshot(1010.0, 9000.0, 1010.0)))
    monkeypatch.setattr(trader.approval, "post_proposal", lambda *a, **k: "ts1")
    monkeypatch.setattr(trader.approval, "await_approval", lambda *a, **k: "approve")


def _propose_n(n):
    return lambda *a, **k: [IDEA] * n


@pytest.mark.asyncio
async def test_per_cycle_order_cap_blocks_extra_entries(tmp_path, monkeypatch):
    stub_stage_a(monkeypatch, _propose_n(3))
    t = _trader(tmp_path, n_ideas=3, max_orders_per_cycle=2,
                max_orders_per_day=99, max_notional_per_day=1e9)
    await t.run_once(dry_run=False)
    assert t._submit_order.await_count == 2


@pytest.mark.asyncio
async def test_per_day_order_cap_blocks_extra_entries(tmp_path, monkeypatch):
    stub_stage_a(monkeypatch, _propose_n(3))
    sm = StateManager(str(tmp_path / "state.json"))
    t = _trader(tmp_path, n_ideas=3, state_manager=sm, max_orders_per_cycle=99,
                max_orders_per_day=2, max_notional_per_day=1e9)
    await t.run_once(dry_run=False)
    assert t._submit_order.await_count == 2


@pytest.mark.asyncio
async def test_per_day_notional_cap_blocks_extra_entries(tmp_path, monkeypatch):
    stub_stage_a(monkeypatch, _propose_n(3))
    sm = StateManager(str(tmp_path / "state.json"))

    t = _trader(tmp_path, n_ideas=3, state_manager=sm, max_orders_per_cycle=99,
                max_orders_per_day=99, max_notional_per_day=250.0)
    await t.run_once(dry_run=False)
    assert t._submit_order.await_count == 2


@pytest.mark.asyncio
async def test_caps_none_means_no_throttle(tmp_path, monkeypatch):

    stub_stage_a(monkeypatch, _propose_n(3))
    t = _trader(tmp_path, n_ideas=3)
    await t.run_once(dry_run=False)
    assert t._submit_order.await_count == 3


@pytest.mark.asyncio
async def test_same_cycle_submitted_buys_count_against_deployed_budget(tmp_path, monkeypatch):


    stub_stage_a(monkeypatch, _propose_n(4))
    t = _trader(tmp_path, n_ideas=4)
    await t.run_once(dry_run=False)
    assert t._submit_order.await_count == 3


@pytest.mark.asyncio
async def test_working_order_snapshot_failure_blocks_entries(tmp_path, monkeypatch):
    stub_stage_a(monkeypatch, _propose_n(1))
    t = _trader(tmp_path, n_ideas=1)
    t.ib_conn.ib.reqAllOpenOrdersAsync = AsyncMock(side_effect=RuntimeError("broker snapshot down"))
    await t.run_once(dry_run=False)
    assert t._submit_order.await_count == 0


@pytest.mark.asyncio
async def test_per_day_opened_notional_persisted_to_state(tmp_path, monkeypatch):
    stub_stage_a(monkeypatch, _propose_n(2))
    sm = StateManager(str(tmp_path / "state.json"))
    t = _trader(tmp_path, n_ideas=2, state_manager=sm, max_orders_per_cycle=99,
                max_orders_per_day=99, max_notional_per_day=1e9)
    await t.run_once(dry_run=False)
    from exitmgr.order import _trading_day
    ds = sm.state.daily_stats.get(_trading_day())
    assert ds is not None
    assert ds.orders_opened == 2
    assert ds.notional_opened == pytest.approx(240.0)
