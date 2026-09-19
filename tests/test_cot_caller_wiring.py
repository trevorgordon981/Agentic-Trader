"""Public API contract; production-derived narrative omitted."""
import json
import os

import pytest
from unittest.mock import AsyncMock, MagicMock

import exitmgr.trader as trader
from exitmgr.trader import Trader, ResolvedOrder
from exitmgr.risk import RiskLimits
from exitmgr.strategist import TradeIdea
from exitmgr.account import PotSnapshot
from exitmgr import trade_capture

import daily_recommend as dr
from tests._stage_stub import stub_stage_a


LIM = RiskLimits()
IDEA = TradeIdea("SPY", True, "bullish", "long call", 7, 0.35, 90.0, 4, "trend")
_ANSWER = ('{"trades":[{"underlying":"SPY","is_index":true,"direction":"bullish",'
           '"structure":"long call","target_dte":30,"target_delta":0.35,'
           '"est_debit_usd":90,"conviction":4,"thesis":"trend"}]}')
_COT = "Step 1: SPY trend up. Step 2: low VIX favors calls. Step 3: buy a call."


def _ddir():
    return trade_capture.dataset_dir(None)


def _read_decisions():
    p = trade_capture.decision_context_path(_ddir())
    if not os.path.exists(p):
        return []
    with open(p) as f:
        return [json.loads(l) for l in f if l.strip()]


def _read_dataset():
    p = trade_capture.dataset_path(_ddir())
    if not os.path.exists(p):
        return []
    with open(p) as f:
        return [json.loads(l) for l in f if l.strip()]



def _trader(tmp_path):
    ibc = MagicMock()
    ibc.ib = MagicMock()
    ibc.ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[])
    ibc.get_positions = AsyncMock(return_value={})
    em = MagicMock(); em.run_cycle = AsyncMock()
    t = Trader(ib_conn=ibc, exit_manager=em, limits=LIM, approved_names=set(),
               endpoint="http://x", model="m", slack_token="tok", slack_channel="C1",
               approver_ids={"OWNER"}, baseline_path=str(tmp_path / "b.json"),
               audit_path=str(tmp_path / "a.jsonl"), approve_timeout_s=60)
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
    return t


def _wire_trader(monkeypatch, propose_ret):
    monkeypatch.setattr(trader.research, "gather", AsyncMock(return_value={}))
    monkeypatch.setattr(trader, "_market_open", lambda: True)
    monkeypatch.setattr(trader, "get_pot_snapshot",
                        AsyncMock(return_value=PotSnapshot(1010.0, 9000.0, 1010.0)))
    stub_stage_a(monkeypatch, lambda *a, **k: propose_ret)
    monkeypatch.setattr(trader.approval, "post_proposal", lambda *a, **k: "ts1")
    monkeypatch.setattr(trader.approval, "await_approval", lambda *a, **k: "approve")


@pytest.mark.asyncio
async def test_trader_threads_cot_into_decision(tmp_path, monkeypatch):

    _wire_trader(monkeypatch, ([IDEA], _ANSWER, _COT))
    t = _trader(tmp_path)
    await t.run_once(dry_run=False)
    rows = _read_decisions()
    assert len(rows) == 2
    assert {row["event"] for row in rows} == {"proposal", "submitted"}
    assert all(row["cot"] == _COT for row in rows)
    assert all(row["raw_strategist"] == _ANSWER for row in rows)
    assert all(row["chosen"]["underlying"] == "SPY" for row in rows)


@pytest.mark.asyncio
async def test_trader_cot_none_is_safe(tmp_path, monkeypatch):

    _wire_trader(monkeypatch, ([IDEA], _ANSWER, None))
    t = _trader(tmp_path)
    await t.run_once(dry_run=False)
    r = _read_decisions()[-1]
    assert r["cot"] is None and r["raw_strategist"] == _ANSWER


@pytest.mark.asyncio
async def test_trader_bare_list_propose_still_works(tmp_path, monkeypatch):

    _wire_trader(monkeypatch, [IDEA])
    t = _trader(tmp_path)
    await t.run_once(dry_run=False)
    r = _read_decisions()[-1]
    assert r["cot"] is None
    assert r["chosen"]["underlying"] == "SPY"


@pytest.mark.asyncio
async def test_trader_no_trade_captures_cot(tmp_path, monkeypatch):

    _wire_trader(monkeypatch, ([], _ANSWER, _COT))
    t = _trader(tmp_path)
    await t.run_once(dry_run=False)
    nts = [r for r in _read_dataset() if r.get("kind") == "no_trade"]
    assert nts and nts[-1]["cot"] == _COT and nts[-1]["reason"] == "empty_slate"



def _wire_slate(monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    resolved = ResolvedOrder("SPY", "C", "20260620", 500.0, 1, 1.20, object())
    monkeypatch.setattr(dr, "_resolve", AsyncMock(return_value=(resolved, "ok")))
    monkeypatch.setattr(dr, "_open_book", AsyncMock(return_value=[]))
    monkeypatch.setattr(dr, "_open_positions_for_risk", AsyncMock(return_value=[]))
    monkeypatch.setattr(dr.approval, "post_proposal", lambda *a, **k: "ts1")
    monkeypatch.setattr(dr.research, "days_to_earnings", lambda s: None)
    monkeypatch.setattr(dr.research, "days_to_ex_dividend", lambda s: None)

    monkeypatch.setattr(dr.construction, "max_premium_budget", lambda *a, **k: 0)
    monkeypatch.setattr(dr.construction, "check_budget", lambda *a, **k: (True, []))
    monkeypatch.setattr(dr.construction, "earnings_ok", lambda *a, **k: (True, ""))
    monkeypatch.setattr(dr.construction, "assignment_risk_ok", lambda *a, **k: (True, ""))
    monkeypatch.setattr(dr.construction, "clamp_tp_sl", lambda *a, **k: (30.0, 30.0))
    return resolved


@pytest.mark.asyncio
async def test_slate_post_idea_threads_cot(monkeypatch):
    _wire_slate(monkeypatch)
    pot = PotSnapshot(1010.0, 9000.0, 1010.0)
    pending = []
    ts = await dr._post_idea(MagicMock(), IDEA, pot, 0.12, "tok", "C1", "/dev/null", pending,
                             candidates=[IDEA], raw_strategist=_ANSWER, market_context="brief",
                             technical_card={"rsi": 55}, cot=_COT)
    assert ts == "ts1"
    r = _read_decisions()[-1]
    assert r["cot"] == _COT
    assert r["raw_strategist"] == _ANSWER
    assert r["source"] == "daily_slate"
    assert r["chosen"]["underlying"] == "SPY"


@pytest.mark.asyncio
async def test_slate_post_idea_cot_defaults_none(monkeypatch):

    _wire_slate(monkeypatch)
    pot = PotSnapshot(1010.0, 9000.0, 1010.0)
    pending = []
    await dr._post_idea(MagicMock(), IDEA, pot, 0.12, "tok", "C1", "/dev/null", pending,
                        candidates=[IDEA], raw_strategist=_ANSWER, market_context="brief")
    r = _read_decisions()[-1]
    assert r["cot"] is None and r["raw_strategist"] == _ANSWER
