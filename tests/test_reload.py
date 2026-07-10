"""Take-profit-and-reload loop (2026-07-03).

Encodes Trevor's serial-reload exit style as a fill-gated, human-approved SUGGESTION (never an
auto-fire). Covered here:
  * reload verb parse (position_manager) -- only on take_profit, backward-compatible.
  * ExitTrigger carries reload metadata; _apply_decision attaches it only to take_profit.
  * ticket written ONLY on a CONFIRMED Filled close (double-exposure guard); never on resting/reject.
  * a drained ticket is routed through the NORMAL gate/construct/approve/submit path.
  * the just-vacated slot is free (no self-block / no double-count) via the G1 fresh post-exit book.
  * a reload is DEFERRED while a same-underlying close is still in flight (G2 interlock).
  * the friction gate rejects a churn.
  * anti-churn: per-name-per-day depth cap + TTL expiry, consume-once.
  * reloads inherit the entry throttle (count against max_orders_per_cycle).
  * ceiling->backstop reconciliation: arm_trail suppresses the fixed tier profit_target.
  * feature OFF (reload_enabled=False) == EXACT no-op (queue untouched, no suggestion).
"""
import json
import os
import time

import pytest
from unittest.mock import AsyncMock, MagicMock

import exitmgr.trader as trader
from exitmgr.trader import Trader, ResolvedOrder
from exitmgr.risk import RiskLimits
from exitmgr.strategist import TradeIdea
from exitmgr.rules import ExitTrigger
from exitmgr.account import PotSnapshot
from exitmgr.config import Config, RulesConfig, TrailingConfig, StateConfig, JournalConfig, LoopConfig
from exitmgr.manager import ExitManager
from exitmgr import position_manager, reload_queue


# ============================ 1. reload verb parse (position_manager) ============================

def _assess(monkeypatch, decisions):
    raw = json.dumps({"decisions": decisions})
    monkeypatch.setattr(position_manager, "_post_json", lambda *a, **k: raw)
    return position_manager.assess_positions("http://x", "m", [{"con_id": 1}])


def test_reload_parsed_on_take_profit(monkeypatch):
    out = _assess(monkeypatch, {"1": {"action": "take_profit", "reload": True,
                                      "reload_conviction": 7, "reason": "runs on"}})
    assert out[1]["action"] == "take_profit"
    assert out[1]["reload"] is True
    assert out[1]["reload_conviction"] == 7.0


def test_reload_ignored_off_take_profit(monkeypatch):
    # a stray reload flag on arm_trail/hold/cut must never carry through
    out = _assess(monkeypatch, {"1": {"action": "arm_trail", "reload": True, "reload_conviction": 9},
                                "2": {"action": "cut", "reload": True}})
    assert out[1]["reload"] is False and out[1]["reload_conviction"] is None
    assert out[2]["reload"] is False


def test_reload_absent_is_backward_compatible(monkeypatch):
    out = _assess(monkeypatch, {"1": {"action": "take_profit", "reason": "stall"}})
    assert out[1]["reload"] is False and out[1]["reload_conviction"] is None


# ============================ 2. _apply_decision attaches reload metadata ============================

def _mgr(tmp_path, **kw):
    cfg = Config()
    cfg.state = StateConfig(path=os.path.join(str(tmp_path), "state.json"))
    cfg.journal = JournalConfig(path=os.path.join(str(tmp_path), "trades.log"))
    cfg.loop = LoopConfig(interval_seconds=60)
    cfg.rules = RulesConfig(stop_pct=30.0, profit_target_pct=35.0, trailing=TrailingConfig())
    for k, v in kw.items():
        setattr(cfg, k, v)
    return ExitManager(cfg)


def test_apply_decision_carries_reload(tmp_path):
    m = _mgr(tmp_path)
    _, f = m._apply_decision(m.config.rules,
                             {"action": "take_profit", "reload": True, "reload_conviction": 8},
                             10.0, 800.0, 1, 5, "NVDA")
    assert isinstance(f, ExitTrigger) and f.trigger_type == "take_profit"
    assert f.reload is True and f.reload_conviction == 8


def test_apply_decision_cut_never_reloads(tmp_path):
    m = _mgr(tmp_path)
    _, f = m._apply_decision(m.config.rules, {"action": "cut", "reload": True}, 3.0, 800.0, 1, 5, "X")
    assert f.trigger_type == "model_cut" and f.reload is False


# ============================ 3. ceiling -> backstop reconciliation ============================

def test_arm_trail_suppresses_fixed_profit_target(tmp_path):
    m = _mgr(tmp_path)
    rules = m.config.rules  # profit_target_pct = 35
    out = m._reconcile_ceiling_backstop(rules, {"action": "arm_trail"})
    assert out.profit_target_pct is None            # ceiling relaxed -> model let-it-run wins
    assert out.stop_pct == 30.0                      # protective stop NEVER touched


def test_ceiling_intact_when_not_arm_trail(tmp_path):
    m = _mgr(tmp_path)
    for dec in ({"action": "hold"}, {"action": "take_profit"}, None, {"action": "cut"}):
        out = m._reconcile_ceiling_backstop(m.config.rules, dec)
        assert out.profit_target_pct == 35.0         # backstop stays for non-arm_trail / no response


# ============================ 4. ticket written ONLY on confirmed Filled ============================

def _seed_journal_entry(m, con_id=5, symbol="NVDA"):
    m._journal_entries[con_id] = {
        "symbol": symbol, "right": "C", "debit": 800.0, "quantity": 1,
        "thesis": "breakout continuation", "dte_at_entry": 30,
    }


def _tp_trigger(reload=True, conv=8):
    return ExitTrigger(con_id=5, trigger_type="take_profit", current_price=12.0,
                       entry_debit=800.0, current_value=1200.0, pnl_pct=50.0,
                       message="model take_profit", reload=reload, reload_conviction=conv)


def test_ticket_written_on_filled(tmp_path):
    m = _mgr(tmp_path, reload_enabled=True)
    _seed_journal_entry(m)
    m._maybe_write_reload_ticket(
        5, "NVDA", _tp_trigger(), 1, 800.0, fill_px=12.0,
        fill_status="Filled", fill_key="perm:ticket-written")
    q = reload_queue.ReloadQueue(reload_queue.queue_path(m.config.journal.path))
    assert len(q.tickets) == 1
    t = q.tickets[0]
    assert t["symbol"] == "NVDA" and t["reload_conviction"] == 8 and t["right"] == "C"
    assert t["dte_target"] == 30 and t["realized_pnl"] == pytest.approx(12.0 * 100 - 800.0)


def test_reload_queue_add_once_dedupes_fill_identity(tmp_path):
    path = tmp_path / "reload.json"
    ticket = reload_queue.make_ticket(
        symbol="NVDA", thesis="x", right="C", width=None, dte_target=30,
        structure="single", is_index=False, reload_conviction=8,
        realized_pnl=100, original_debit=500, source_fill_key="perm:123")
    q = reload_queue.ReloadQueue(str(path))
    assert q.add_once(ticket)
    assert not q.add_once(ticket)
    persisted = reload_queue.ReloadQueue(str(path)).tickets
    assert len(persisted) == 1
    assert persisted[0]["source_fill_key"] == "perm:123"


@pytest.mark.parametrize("status", ["Submitted", "PreSubmitted", "Cancelled", None])
def test_no_ticket_on_non_filled_close(tmp_path, status):
    m = _mgr(tmp_path, reload_enabled=True)
    _seed_journal_entry(m)
    m._maybe_write_reload_ticket(
        5, "NVDA", _tp_trigger(), 1, 800.0, fill_px=None,
        fill_status=status, fill_key=f"status:{status}")
    assert not os.path.exists(reload_queue.queue_path(m.config.journal.path))  # double-exposure guard


def test_no_ticket_when_feature_off(tmp_path):
    m = _mgr(tmp_path, reload_enabled=False)
    _seed_journal_entry(m)
    m._maybe_write_reload_ticket(
        5, "NVDA", _tp_trigger(), 1, 800.0, fill_px=12.0,
        fill_status="Filled", fill_key="perm:disabled")
    assert not os.path.exists(reload_queue.queue_path(m.config.journal.path))


def test_no_ticket_without_reload_flag(tmp_path):
    m = _mgr(tmp_path, reload_enabled=True)
    _seed_journal_entry(m)
    m._maybe_write_reload_ticket(
        5, "NVDA", _tp_trigger(reload=False), 1, 800.0,
        fill_px=12.0, fill_status="Filled", fill_key="perm:no-reload")
    assert not os.path.exists(reload_queue.queue_path(m.config.journal.path))


# ============================ 5. queue drain: consume-once, TTL, depth cap ============================

def _q(tmp_path):
    return reload_queue.ReloadQueue(str(tmp_path / "rq.json"))


def _ticket(sym="NVDA", conv=8, ttl_cycles=3, now=None):
    return reload_queue.make_ticket(symbol=sym, thesis="t", right="C", width=None, dte_target=30,
                                    structure="single", is_index=False, reload_conviction=conv,
                                    realized_pnl=10.0, original_debit=800.0, now_ts=now,
                                    ttl_cycles=ttl_cycles, interval_seconds=60)


def test_drain_consumes_once(tmp_path):
    q = _q(tmp_path)
    q.add(_ticket())
    ready, summ = q.drain(today="2026-07-03", max_per_name=2)
    assert len(ready) == 1 and summ["ready"] == 1
    # consumed: a second drain yields nothing
    ready2, _ = _q(tmp_path).drain(today="2026-07-03", max_per_name=2)
    assert ready2 == []


def test_drain_ttl_expiry(tmp_path):
    q = _q(tmp_path)
    q.add(_ticket(now=time.time() - 10_000))   # expires_after_ts already in the past
    ready, summ = q.drain(today="2026-07-03", max_per_name=2)
    assert ready == [] and summ["expired"] == 1


def test_drain_depth_cap(tmp_path):
    q = _q(tmp_path)
    for _ in range(4):
        q.add(_ticket(sym="NVDA"))
    ready, summ = q.drain(today="2026-07-03", max_per_name=2)
    assert len(ready) == 2 and summ["capped"] == 2   # only 2/day for the name; rest dropped


# ============================ 6. friction gate (pure function) ============================

def test_friction_rejects_low_conviction():
    ok, why, _ = reload_queue.reload_friction_ok(
        reload_conviction=4, conviction_min=6, tp_pct=30.0, new_debit=800.0, qty=1,
        is_spread=False, theta_per_share=-0.1, entry_spread_pct=2.0, k=1.5)
    assert not ok and "conviction" in why


def test_friction_rejects_bad_economics():
    # tiny expected continuation vs. heavy theta -> churn, rejected
    ok, why, _ = reload_queue.reload_friction_ok(
        reload_conviction=9, conviction_min=6, tp_pct=1.0, new_debit=100.0, qty=1,
        is_spread=False, theta_per_share=-5.0, entry_spread_pct=1.0, k=1.5)
    assert not ok and "continuation" in why


def test_friction_passes_good_reload():
    ok, why, detail = reload_queue.reload_friction_ok(
        reload_conviction=8, conviction_min=6, tp_pct=30.0, new_debit=800.0, qty=1,
        is_spread=False, theta_per_share=-0.05, entry_spread_pct=1.0, k=1.5)
    assert ok and detail["expected_continuation"] == pytest.approx(240.0)


# ============================ 7. trader routing / gates ============================

LIM = RiskLimits(max_concurrent=8)
NVDA_IDEA = TradeIdea("NVDA", False, "bullish", "long call", 7, 0.35, 90.0, 4, "strategist idea")


def _trader(tmp_path, **kw):
    ibc = MagicMock(); ibc.ib = MagicMock()
    em = MagicMock(); em.run_cycle = AsyncMock()
    t = Trader(ib_conn=ibc, exit_manager=em, limits=LIM, approved_names={"NVDA", "AMD"},
               endpoint="http://x", model="m", slack_token="tok", slack_channel="C1",
               approver_ids={"OWNER"}, baseline_path=str(tmp_path / "b.json"),
               audit_path=str(tmp_path / "a.jsonl"), journal_path=str(tmp_path / "trades.log"),
               approve_timeout_s=60, **kw)
    resolved = ResolvedOrder(
        "NVDA", "C", "20260620", 50.0, 1, 0.90, MagicMock(conId=123),
        entry_bid=0.85, entry_ask=0.95, quote_observed_at=time.monotonic(),
        decision_id="decision-" + "a" * 32)
    t._resolve_order = AsyncMock(return_value=resolved)
    t._refresh_approved_entry = AsyncMock(
        side_effect=lambda idea, original, baseline: (
            original, PotSnapshot(1010.0, 9000.0, 1010.0), ()))
    t._submit_order = AsyncMock(return_value=("Filled", []))
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


def _seed_reload(tmp_path, sym="NVDA", conv=8):
    q = reload_queue.ReloadQueue(reload_queue.queue_path(str(tmp_path / "trades.log")))
    q.add(reload_queue.make_ticket(symbol=sym, thesis="continuation", right="C", width=None,
                                   dte_target=30, structure="single", is_index=False,
                                   reload_conviction=conv, realized_pnl=100.0, original_debit=90.0))


@pytest.mark.asyncio
async def test_reload_routed_through_normal_path(tmp_path, monkeypatch):
    monkeypatch.setattr(trader, "propose", lambda *a, **k: [])   # no strategist ideas
    _seed_reload(tmp_path)
    t, ibc, em = _trader(tmp_path, reload_enabled=True)
    ibc.get_positions = AsyncMock(return_value={})              # just-vacated slot: book empty
    ibc.ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[])
    await t.run_once(dry_run=False)
    assert t._submit_order.await_count == 1                     # reload submitted via normal path
    # consumed exactly once
    assert reload_queue.ReloadQueue(reload_queue.queue_path(str(tmp_path / "trades.log"))).tickets == []


@pytest.mark.asyncio
async def test_reload_deferred_while_close_in_flight(tmp_path, monkeypatch):
    monkeypatch.setattr(trader, "propose", lambda *a, **k: [])
    _seed_reload(tmp_path)
    t, ibc, em = _trader(tmp_path, reload_enabled=True)
    ibc.get_positions = AsyncMock(return_value={})
    # a resting SELL-to-close on NVDA is still working -> G2 defers the reload
    tr = MagicMock(); tr.order = MagicMock(action="SELL")
    tr.contract = MagicMock(symbol="NVDA"); tr.orderStatus = MagicMock(status="Submitted")
    ibc.ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[tr])
    await t.run_once(dry_run=False)
    assert t._submit_order.await_count == 0
    assert t._resolve_order.await_count == 0                    # deferred before any construction


@pytest.mark.asyncio
async def test_reload_friction_rejects_churn(tmp_path, monkeypatch):
    monkeypatch.setattr(trader, "propose", lambda *a, **k: [])
    _seed_reload(tmp_path, conv=3)                              # below reload_conviction_min (6)
    t, ibc, em = _trader(tmp_path, reload_enabled=True)
    ibc.get_positions = AsyncMock(return_value={})
    ibc.ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[])
    await t.run_once(dry_run=False)
    assert t._submit_order.await_count == 0                     # friction gate rejected the churn


@pytest.mark.asyncio
async def test_reload_inherits_entry_throttle(tmp_path, monkeypatch):
    # one reload + one strategist idea, per-cycle cap = 1. The reload runs FIRST and consumes the
    # single allowed order; the strategist idea is then throttled -> proves the reload counts
    # against max_orders_per_cycle (no cap bypass).
    monkeypatch.setattr(trader, "propose", lambda *a, **k: [NVDA_IDEA])
    _seed_reload(tmp_path)
    t, ibc, em = _trader(tmp_path, reload_enabled=True, max_orders_per_cycle=1)
    ibc.get_positions = AsyncMock(return_value={})
    ibc.ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[])
    await t.run_once(dry_run=False)
    assert t._submit_order.await_count == 1                     # only 1 order allowed this cycle


@pytest.mark.asyncio
async def test_feature_off_is_exact_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(trader, "propose", lambda *a, **k: [])
    _seed_reload(tmp_path)
    t, ibc, em = _trader(tmp_path)                              # reload_enabled defaults False
    ibc.get_positions = AsyncMock(return_value={})
    ibc.ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[])
    await t.run_once(dry_run=False)
    assert t._submit_order.await_count == 0                     # no suggestion made
    # queue is UNTOUCHED: the ticket is still there (drain never ran)
    q = reload_queue.ReloadQueue(reload_queue.queue_path(str(tmp_path / "trades.log")))
    assert len(q.tickets) == 1
