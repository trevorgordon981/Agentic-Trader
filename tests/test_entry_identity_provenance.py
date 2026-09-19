"""Public API contract; production-derived narrative omitted."""
import json
import os
from dataclasses import replace as _dc_replace

import pytest
from unittest.mock import AsyncMock, MagicMock

import exitmgr.trader as trader
from exitmgr.trader import Trader, ResolvedOrder
from exitmgr.risk import RiskLimits
from exitmgr.strategist import TradeIdea
from exitmgr.account import PotSnapshot
from exitmgr import trade_capture
from exitmgr import event_capture as ec

import daily_recommend as dr
from tests._stage_stub import stub_stage_a


LIM = RiskLimits()
IDEA = TradeIdea("SPY", True, "bullish", "long call", 7, 0.35, 90.0, 4, "trend")
_ANSWER = ('{"trades":[{"underlying":"SPY","is_index":true,"direction":"bullish",'
           '"structure":"long call","target_dte":30,"target_delta":0.35,'
           '"est_debit_usd":90,"conviction":4,"thesis":"trend"}]}')
_COT = "Step 1: SPY trend up. Step 2: buy a call."



SERVED = {"artifact_sha256": "cafe" * 16, "runtime": "vllm-0.11", "served_model": "deepseek-v4"}


def _ddir():
    return trade_capture.dataset_dir(None)


def _rows(path):
    if not os.path.exists(path):
        return []
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _decisions():
    return _rows(trade_capture.decision_context_path(_ddir()))


def _no_trades():
    return [r for r in _rows(trade_capture.dataset_path(_ddir())) if r.get("kind") == "no_trade"]


def _shadow_events():
    return [r for r in _rows(ec.events_path())
            if r.get("event_type") == "entry_reasoning_shadow"]



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


def _wire_trader(monkeypatch, propose_ret, *, market_open=True):
    monkeypatch.setattr(trader.research, "gather", AsyncMock(return_value={}))
    monkeypatch.setattr(trader, "_market_open", lambda: market_open)
    monkeypatch.setattr(trader, "get_pot_snapshot",
                        AsyncMock(return_value=PotSnapshot(1010.0, 9000.0, 1010.0)))
    stub_stage_a(monkeypatch, lambda *a, **k: propose_ret)
    monkeypatch.setattr(trader.approval, "post_proposal", lambda *a, **k: "ts1")
    monkeypatch.setattr(trader.approval, "await_approval", lambda *a, **k: "approve")


@pytest.mark.asyncio
async def test_a_served_identity_is_recorded_meta_on_every_entry_row(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    _wire_trader(monkeypatch, ([IDEA], _ANSWER, _COT, SERVED))
    await _trader(tmp_path).run_once(dry_run=False)

    rows = _decisions()
    assert {r["event"] for r in rows} == {"proposal", "submitted"}
    assert all(r["model_identity"] == SERVED for r in rows)
    assert all(r["model_identity_source"] == "meta" for r in rows)


@pytest.mark.asyncio
async def test_a_model_that_did_not_name_itself_is_unknown_not_absent(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    _wire_trader(monkeypatch, ([IDEA], _ANSWER, _COT))
    await _trader(tmp_path).run_once(dry_run=False)

    rows = _decisions()
    assert rows
    assert all(r["model_identity"] is None for r in rows)
    assert all(r["model_identity_source"] == "unknown" for r in rows)


@pytest.mark.asyncio
async def test_a_cycle_that_asked_no_model_records_None_not_unknown(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    _wire_trader(monkeypatch, ([IDEA], _ANSWER, _COT, SERVED), market_open=False)
    await _trader(tmp_path).run_once(dry_run=False)

    rows = _no_trades()
    assert rows and rows[-1]["reason"] == "market_closed"
    assert rows[-1]["model_identity"] is None
    assert rows[-1]["model_identity_source"] is None
    assert not _decisions()


@pytest.mark.asyncio
async def test_an_empty_slate_keeps_the_attribution_of_the_model_that_passed(
        tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    _wire_trader(monkeypatch, ([], _ANSWER, _COT, SERVED))
    await _trader(tmp_path).run_once(dry_run=False)

    rows = _no_trades()
    assert rows and rows[-1]["reason"] == "empty_slate"
    assert rows[-1]["model_identity"] == SERVED
    assert rows[-1]["model_identity_source"] == "meta"


@pytest.mark.asyncio
async def test_an_unnamed_model_that_passed_is_unknown(tmp_path, monkeypatch):
    _wire_trader(monkeypatch, ([], _ANSWER, _COT))
    await _trader(tmp_path).run_once(dry_run=False)

    rows = _no_trades()
    assert rows and rows[-1]["reason"] == "empty_slate"
    assert rows[-1]["model_identity"] is None
    assert rows[-1]["model_identity_source"] == "unknown"


@pytest.mark.asyncio
async def test_a_strategist_that_RAISED_is_still_unknown_because_it_was_asked(
        tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    def _boom(*a, **k):
        raise RuntimeError("model server down")
    _wire_trader(monkeypatch, ([], _ANSWER, _COT))
    monkeypatch.setattr(trader, "propose_intents", _boom)
    await _trader(tmp_path).run_once(dry_run=False)

    rows = _no_trades()
    assert rows and rows[-1]["model_identity_source"] == "unknown"


@pytest.mark.asyncio
async def test_a_reload_is_never_attributed_to_this_cycles_strategist(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    reload_idea = TradeIdea("SPY", True, "bullish", "debit spread", 7, 0.35, 90.0, 4, "runner")
    reload_idea.is_reload = True
    reload_idea.reload_conviction = 7
    monkeypatch.setattr(trader.reload_queue, "reload_friction_ok",
                        lambda *a, **k: (True, "", {}))
    _wire_trader(monkeypatch, ([reload_idea], _ANSWER, _COT, SERVED))
    await _trader(tmp_path).run_once(dry_run=False)

    rows = _decisions()
    assert rows, "the reload must still be proposed -- only its attribution changes"
    assert all(r["model_identity"] is None for r in rows)
    assert all(r["model_identity_source"] == "unknown" for r in rows)


@pytest.mark.asyncio
async def test_a_strategist_idea_in_the_same_cycle_keeps_its_meta(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    reload_idea = TradeIdea("IWM", True, "bearish", "debit spread", 7, 0.35, 90.0, 4, "runner")
    reload_idea.is_reload = True
    reload_idea.reload_conviction = 7
    monkeypatch.setattr(trader.reload_queue, "reload_friction_ok",
                        lambda *a, **k: (True, "", {}))
    _wire_trader(monkeypatch, ([reload_idea, IDEA], _ANSWER, _COT, SERVED))
    await _trader(tmp_path).run_once(dry_run=False)

    by_symbol = {}
    for r in _decisions():
        by_symbol.setdefault(r["chosen"]["underlying"], []).append(r)
    assert set(by_symbol) == {"IWM", "SPY"}
    assert all(r["model_identity_source"] == "unknown" for r in by_symbol["IWM"])
    assert all(r["model_identity"] is None for r in by_symbol["IWM"])
    assert all(r["model_identity_source"] == "meta" for r in by_symbol["SPY"])
    assert all(r["model_identity"] == SERVED for r in by_symbol["SPY"])



@pytest.mark.asyncio
async def test_shadow_skip_reuses_served_foreground_identity_without_training_duplicate(tmp_path, monkeypatch):
    _wire_trader(monkeypatch, ([], _ANSWER, None, SERVED))
    t = _trader(tmp_path)
    await t.run_once(dry_run=False)
    skips = [r for r in _rows(t.audit_path) if r.get("event") == "entry_reasoning_shadow_skipped"]
    assert len(skips) == 1 and skips[0]["model_identity"] == SERVED
    assert skips[0]["model_identity_source"] == "meta"
    assert skips[0]["thinking"] == "disabled"
    assert skips[0]["foreground_data_ref"]["cot_sha256"] is None
    assert len(_no_trades()) == 1
    assert not _shadow_events(), "OFF data must not be duplicated as an ON shadow training row"


@pytest.mark.asyncio
async def test_shadow_skip_keeps_unnamed_foreground_model_unknown(tmp_path, monkeypatch):
    _wire_trader(monkeypatch, ([], _ANSWER, None))
    t = _trader(tmp_path)
    await t.run_once(dry_run=False)
    skips = [r for r in _rows(t.audit_path) if r.get("event") == "entry_reasoning_shadow_skipped"]
    assert len(skips) == 1 and skips[0]["model_identity"] is None
    assert skips[0]["model_identity_source"] == "unknown"
    assert not _shadow_events()


@pytest.mark.asyncio
async def test_shadow_skip_emits_nothing_when_no_model_was_asked(tmp_path, monkeypatch):
    calls = []
    def record(*args, **kwargs):
        calls.append((args, kwargs))
        return ([], _ANSWER, _COT, SERVED)
    monkeypatch.setattr(trader, "propose_intents", record)
    t = _trader(tmp_path)
    t.model = None
    await t._shadow_cot_capture({"model_was_asked": False})
    assert calls == []
    assert not [r for r in _rows(t.audit_path) if r.get("event") == "entry_reasoning_shadow_skipped"]
    assert not _shadow_events()



def test_the_source_is_a_dataclass_field_so_replace_carries_it():
    """Public API contract; production-derived narrative omitted."""
    r = ResolvedOrder("SPY", "C", "20260620", 50.0, 1, 1.20, MagicMock(conId=1),
                      model_identity=SERVED, model_identity_source="meta")
    assert _dc_replace(r, qty=2).model_identity_source == "meta"
    assert ResolvedOrder("SPY", "C", "20260620", 50.0, 1, 1.20,
                         MagicMock(conId=1)).model_identity_source is None



def _wire_slate(monkeypatch):
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


async def _post(monkeypatch, **kw):
    resolved = _wire_slate(monkeypatch)
    pending = []
    ts = await dr._post_idea(MagicMock(), IDEA, PotSnapshot(1010.0, 9000.0, 1010.0), 0.12,
                             "tok", "C1", "/dev/null", pending, candidates=[IDEA],
                             raw_strategist=_ANSWER, market_context="brief", **kw)
    assert ts == "ts1"
    return _decisions()[-1], resolved


@pytest.mark.asyncio
async def test_slate_a_served_identity_is_recorded_meta(monkeypatch):
    row, resolved = await _post(monkeypatch, model_identity=SERVED,
                                model_identity_source="meta")
    assert row["model_identity"] == SERVED
    assert row["model_identity_source"] == "meta"

    assert resolved.model_identity_source == "meta"


@pytest.mark.asyncio
async def test_slate_an_undeclared_identity_is_unknown_not_promoted(monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    row, _ = await _post(monkeypatch, model_identity=SERVED)
    assert row["model_identity"] == SERVED
    assert row["model_identity_source"] == "unknown"


@pytest.mark.asyncio
async def test_slate_an_unnamed_model_is_unknown(monkeypatch):
    row, resolved = await _post(monkeypatch, model_identity=None,
                                model_identity_source="unknown")
    assert row["model_identity"] is None
    assert row["model_identity_source"] == "unknown"
    assert resolved.model_identity_source == "unknown"


@pytest.mark.asyncio
async def test_slate_a_row_no_model_authored_is_None_not_unknown(monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    row, resolved = await _post(monkeypatch)
    assert row["model_identity"] is None
    assert row["model_identity_source"] is None
    assert resolved.model_identity_source is None
