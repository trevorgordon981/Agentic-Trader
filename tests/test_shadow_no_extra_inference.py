"""Public API contract; production-derived narrative omitted."""
import asyncio

import pytest
from tests.test_entry_identity_provenance import (
    _trader, _wire_trader, _rows, _no_trades, _shadow_events, _ANSWER, SERVED,
)
import exitmgr.trader as trader_module


@pytest.mark.asyncio
async def test_two_successive_real_cycles_ask_model_once_each_and_reuse_capture(tmp_path, monkeypatch):
    _wire_trader(monkeypatch, ([], _ANSWER, None, SERVED))
    calls = []
    def propose(*args, **kwargs):
        calls.append(kwargs)
        return ([], _ANSWER, None, SERVED)
    monkeypatch.setattr(trader_module, "propose_intents", propose)
    t = _trader(tmp_path)
    before = set(asyncio.all_tasks())
    await t.run_once(dry_run=True)
    assert len(calls) == 1
    await t.run_once(dry_run=True)
    assert len(calls) == 2
    assert all(c["thinking"] == "disabled" for c in calls)
    assert set(asyncio.all_tasks()) == before


    assert len(_no_trades()) == 1
    assert all(r.get("cot") is None for r in _no_trades())
    assert not _shadow_events()
    skips = [r for r in _rows(t.audit_path) if r.get("event") == "entry_reasoning_shadow_skipped"]
    assert len(skips) == 2
    assert all(r["model_identity"] == SERVED and r["model_identity_source"] == "meta" for r in skips)


@pytest.mark.asyncio
async def test_failed_real_foreground_call_does_not_spawn_shadow_retry(tmp_path, monkeypatch):
    _wire_trader(monkeypatch, ([], _ANSWER, None, SERVED))
    calls = []
    def propose(*args, **kwargs):
        calls.append(kwargs)
        raise RuntimeError("synthetic model failure")
    monkeypatch.setattr(trader_module, "propose_intents", propose)
    t = _trader(tmp_path)
    before = set(asyncio.all_tasks())
    await t.run_once(dry_run=True)
    await asyncio.sleep(0)
    assert len(calls) == 1 and calls[0]["thinking"] == "disabled"
    assert set(asyncio.all_tasks()) == before
    assert not _shadow_events()
    assert not [r for r in _rows(t.audit_path) if r.get("event") == "entry_reasoning_shadow_skipped"]
    assert len(_no_trades()) == 1 and _no_trades()[0]["reason"] == "entry_pipeline_error"
