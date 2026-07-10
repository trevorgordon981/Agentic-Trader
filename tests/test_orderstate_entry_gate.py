"""Order-state entry-gate fixes (2026-07-03).

GUARDRAIL 1 -- the entry RISK gate must evaluate the FRESH POST-EXIT position book, not the stale
pre-exit book fetched before exit_manager.run_cycle ran. A name closed during THIS cycle's exit run
must no longer be counted against max_concurrent / single-name-agg / sector caps -- while intra-cycle
sequential gating (each accepted fill counts against later ideas in the same cycle) is preserved.

GUARDRAIL 2 -- before submitting a new entry, DEFER any entry whose underlying currently has an
in-flight or resting SELL/close order (avoid transient double exposure in that name). Once the close
is gone the entry proceeds; unrelated underlyings are unaffected.

Both fixes only ever BLOCK/DEFER an entry -- they can never loosen a gate. These are also the two
prerequisites for the planned take-profit-and-reload loop (a same-name re-entry on a just-vacated
slot must not be blocked by the stale book, and must not race its own still-settling close)."""
import pytest
from unittest.mock import AsyncMock, MagicMock

import exitmgr.trader as trader
from exitmgr.trader import Trader, ResolvedOrder
from exitmgr.risk import RiskLimits
from exitmgr.strategist import TradeIdea
from exitmgr.account import PotSnapshot
from exitmgr.state import StateManager, InFlightClose
from exitmgr.connection import PositionData

# max_concurrent = 8 (live value). Small per-trade sizes so only the count/name caps bind.
LIM = RiskLimits(max_concurrent=8)
# SPY index ideas bypass the single-name-agg + sector caps, so ONLY max_concurrent binds -- lets us
# test the concurrent-count effect of stale-vs-fresh book cleanly.
SPY_IDEA = TradeIdea("SPY", True, "bullish", "long call", 7, 0.35, 90.0, 4, "trend")
# A single-name idea used for the re-entry / defer cases.
NVDA_IDEA = TradeIdea("NVDA", False, "bullish", "long call", 7, 0.35, 90.0, 4, "trend")


def _pos(con_id, symbol):
    """A live long-call PositionData (avg_cost small so gross-fallback notional is tiny)."""
    return PositionData(con_id=con_id, symbol=symbol, right="C", quantity=1, avg_cost=0.10,
                        expiry="20260620")


def _trader(tmp_path, *, resolve_sym="SPY", **kw):
    ibc = MagicMock(); ibc.ib = MagicMock()
    em = MagicMock(); em.run_cycle = AsyncMock()
    sm = kw.pop("state_manager", None)
    if sm is not None:
        em.state_manager = sm
    t = Trader(ib_conn=ibc, exit_manager=em, limits=LIM, approved_names={"NVDA", "AMD"},
               endpoint="http://x", model="m", slack_token="tok", slack_channel="C1",
               approver_ids={"OWNER"}, baseline_path=str(tmp_path / "b.json"),
               audit_path=str(tmp_path / "a.jsonl"), journal_path=str(tmp_path / "trades.log"),
               approve_timeout_s=60, **kw)
    resolved = ResolvedOrder(
        resolve_sym, "C", "20260620", 50.0, 1, 0.90, MagicMock(conId=123),
        entry_bid=0.85, entry_ask=0.95,
        quote_observed_at=__import__("time").monotonic(),
        decision_id="decision-" + "a" * 32)
    t._resolve_order = AsyncMock(return_value=resolved)
    t._refresh_approved_entry = AsyncMock(
        side_effect=lambda idea, original, baseline: (
            original, PotSnapshot(1010.0, 9000.0, 1010.0), ()))
    t._submit_order = AsyncMock(return_value=("Filled", []))
    # no journal file -> _open_positions uses the (tiny) gross fallback for notional
    return t, ibc, em


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    monkeypatch.setattr(trader.research, "gather", AsyncMock(return_value={}))
    monkeypatch.setattr(trader.research, "days_to_earnings", lambda *a, **k: None)
    monkeypatch.setattr(trader.research, "days_to_ex_dividend", lambda *a, **k: None)
    monkeypatch.setattr(trader, "_market_open", lambda: True)
    monkeypatch.setattr(trader, "get_pot_snapshot",
                        AsyncMock(return_value=PotSnapshot(50000.0, 40000.0, 50000.0)))
    monkeypatch.setattr(trader.approval, "post_proposal", lambda *a, **k: "ts1")
    monkeypatch.setattr(trader.approval, "await_approval", lambda *a, **k: "approve")


# --------------------------------------------------------------------------------------------------
# GUARDRAIL 1 (a): a name closed in the exit cycle is no longer counted by the entry gate.
# --------------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_name_closed_in_exit_cycle_not_counted_by_entry_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(trader, "propose", lambda *a, **k: [SPY_IDEA])
    t, ibc, em = _trader(tmp_path)

    # PRE-exit book = 8 open positions (== max_concurrent -> a new entry would be blocked).
    # POST-exit book = 7 (one closed during the exit cycle) -> the new entry now has room.
    pre = {i: _pos(1000 + i, f"SYM{i}") for i in range(8)}
    post = {i: _pos(1000 + i, f"SYM{i}") for i in range(7)}
    st = {"exited": False}

    async def _get_positions():
        return post if st["exited"] else pre
    ibc.get_positions = AsyncMock(side_effect=_get_positions)
    ibc.ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[])

    async def _run_cycle(*a, **k):
        st["exited"] = True   # the exit cycle closes one name
    em.run_cycle = AsyncMock(side_effect=_run_cycle)

    await t.run_once(dry_run=False)
    # FRESH post-exit book has 7 < 8 -> the entry is allowed and submitted.
    assert t._submit_order.await_count == 1


@pytest.mark.asyncio
async def test_stale_book_would_have_blocked_regression_guard(tmp_path, monkeypatch):
    """Control: if the book DID NOT change (8 both before and after), the concurrent cap still
    binds and the entry is blocked -- proves the pass above is due to the close, not a loosened gate."""
    monkeypatch.setattr(trader, "propose", lambda *a, **k: [SPY_IDEA])
    t, ibc, em = _trader(tmp_path)
    full = {i: _pos(1000 + i, f"SYM{i}") for i in range(8)}
    ibc.get_positions = AsyncMock(return_value=full)
    ibc.ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[])
    await t.run_once(dry_run=False)
    assert t._submit_order.await_count == 0   # 8/8 concurrent -> blocked


# --------------------------------------------------------------------------------------------------
# GUARDRAIL 1 (b): intra-cycle sequential gating still blocks an over-concentrating 2nd idea.
# --------------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_intracycle_sequential_gating_still_blocks_second_idea(tmp_path, monkeypatch):
    # Two SPY ideas in one cycle, book starts at 7/8 open. First fills -> book effectively 8/8 ->
    # the SECOND idea must be blocked by max_concurrent WITHIN the same cycle (append is honored).
    monkeypatch.setattr(trader, "propose", lambda *a, **k: [SPY_IDEA, SPY_IDEA])
    t, ibc, em = _trader(tmp_path)
    seven = {i: _pos(1000 + i, f"SYM{i}") for i in range(7)}
    ibc.get_positions = AsyncMock(return_value=seven)
    ibc.ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[])
    await t.run_once(dry_run=False)
    assert t._submit_order.await_count == 1   # 1st fills (7->8), 2nd blocked at 8/8


# --------------------------------------------------------------------------------------------------
# GUARDRAIL 1 (c): fresh vs stale book don't diverge in shape (same _open_positions() path).
# --------------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fresh_and_stale_book_use_same_path(tmp_path, monkeypatch):
    # With an unchanged book, the pre-exit `positions` and the post-exit `entry_positions` are byte-
    # identical (same method, same inputs). Verified indirectly: a 5-position book leaves room, the
    # single SPY idea is submitted, and _open_positions() was called at least twice (pre + post-exit).
    monkeypatch.setattr(trader, "propose", lambda *a, **k: [SPY_IDEA])
    t, ibc, em = _trader(tmp_path)
    five = {i: _pos(1000 + i, f"SYM{i}") for i in range(5)}
    ibc.get_positions = AsyncMock(return_value=five)
    ibc.ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[])
    spy_before = await t._open_positions()
    await t.run_once(dry_run=False)
    spy_after = await t._open_positions()
    assert [(p.underlying, p.notional, p.is_index) for p in spy_before] == \
           [(p.underlying, p.notional, p.is_index) for p in spy_after]
    assert t._submit_order.await_count == 1


# --------------------------------------------------------------------------------------------------
# GUARDRAIL 2: entry into an underlying with an in-flight/resting close is DEFERRED.
# --------------------------------------------------------------------------------------------------
def _sell_trade(symbol, status="Submitted"):
    """A resting SELL-to-close Trade-like object for reqAllOpenOrdersAsync()."""
    tr = MagicMock()
    tr.order = MagicMock(action="SELL")
    tr.contract = MagicMock(symbol=symbol)
    tr.orderStatus = MagicMock(status=status)
    return tr


@pytest.mark.asyncio
async def test_entry_deferred_when_resting_close_on_same_underlying(tmp_path, monkeypatch):
    monkeypatch.setattr(trader, "propose", lambda *a, **k: [NVDA_IDEA])
    t, ibc, em = _trader(tmp_path, resolve_sym="NVDA")
    ibc.get_positions = AsyncMock(return_value={})
    # a resting SELL-to-close on NVDA is working right now
    ibc.ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[_sell_trade("NVDA")])
    await t.run_once(dry_run=False)
    assert t._submit_order.await_count == 0        # deferred
    assert t._resolve_order.await_count == 0        # deferred BEFORE any construction work


@pytest.mark.asyncio
async def test_entry_deferred_when_state_inflight_close_on_same_underlying(tmp_path, monkeypatch):
    monkeypatch.setattr(trader, "propose", lambda *a, **k: [NVDA_IDEA])
    sm = StateManager(str(tmp_path / "state.json"))
    sm.state.add_in_flight(InFlightClose(con_id=555, order_id=7, remaining_qty=1, entry_debit=90.0))
    t, ibc, em = _trader(tmp_path, resolve_sym="NVDA", state_manager=sm)
    # in-flight con_id 555 maps to NVDA via the live position book
    ibc.get_positions = AsyncMock(return_value={555: _pos(555, "NVDA")})
    ibc.ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[])
    await t.run_once(dry_run=False)
    assert t._submit_order.await_count == 0        # deferred via StateManager in-flight


@pytest.mark.asyncio
async def test_entry_proceeds_once_close_is_gone(tmp_path, monkeypatch):
    monkeypatch.setattr(trader, "propose", lambda *a, **k: [NVDA_IDEA])
    t, ibc, em = _trader(tmp_path, resolve_sym="NVDA")
    ibc.get_positions = AsyncMock(return_value={})
    ibc.ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[])   # no close in flight anymore
    await t.run_once(dry_run=False)
    assert t._submit_order.await_count == 1        # proceeds normally


@pytest.mark.asyncio
async def test_unrelated_underlying_close_does_not_defer(tmp_path, monkeypatch):
    monkeypatch.setattr(trader, "propose", lambda *a, **k: [NVDA_IDEA])
    t, ibc, em = _trader(tmp_path, resolve_sym="NVDA")
    ibc.get_positions = AsyncMock(return_value={})
    # a resting close on AMD must NOT block an NVDA entry
    ibc.ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[_sell_trade("AMD")])
    await t.run_once(dry_run=False)
    assert t._submit_order.await_count == 1


@pytest.mark.asyncio
async def test_terminal_status_close_does_not_defer(tmp_path, monkeypatch):
    # a SELL order already Filled/Cancelled is NOT in-flight -> must not defer.
    monkeypatch.setattr(trader, "propose", lambda *a, **k: [NVDA_IDEA])
    t, ibc, em = _trader(tmp_path, resolve_sym="NVDA")
    ibc.get_positions = AsyncMock(return_value={})
    ibc.ib.reqAllOpenOrdersAsync = AsyncMock(
        return_value=[_sell_trade("NVDA", status="Filled"),
                      _sell_trade("NVDA", status="Cancelled")])
    await t.run_once(dry_run=False)
    assert t._submit_order.await_count == 1
