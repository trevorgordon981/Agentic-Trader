"""Public API contract; production-derived narrative omitted."""
import asyncio
import json
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest

from exitmgr import exec_capture
from exitmgr.manager import ExitManager
from tests.test_exec_capture import _fake_fill
from tests.test_shadow_remit_routing import _mgr, _assert_evaluated
from tests.test_untransmitted_intent_latch import _mgr as simple_manager


async def finish_capture(manager):
    task = getattr(manager, "_external_fill_capture_task", None)
    if task is not None:
        await task
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_blocked_capture_does_not_delay_real_cycle_or_pile_up(tmp_path, monkeypatch):
    manager = _mgr(tmp_path, monkeypatch, "capture", shadow=True, dte=90,
                   mark=7.0, manage=False)
    del manager._capture_external_fills_safe
    manager.ib_conn.is_healthy = lambda: True
    manager.ib_conn.ib.fills.return_value = [_fake_fill(exec_id="cached-1")]
    started, release = threading.Event(), threading.Event()
    calls = []
    async def blocked(*, ib, **kwargs):
        calls.append(ib)
        started.set()
        release.wait(2)
        return {"ok": True, "appended": 0, "positions_appended": 0}
    monkeypatch.setattr(exec_capture, "capture_external_fills", blocked)
    try:
        await asyncio.wait_for(manager.run_cycle(True, defer_model=True), timeout=0.75)
        _assert_evaluated(manager)
        await asyncio.to_thread(started.wait, 0.5)
        assert started.is_set()
        first = manager._external_fill_capture_task
        for _ in range(10):
            manager._last_extfill_capture = 0
            await manager._capture_external_fills_safe()
            assert manager._external_fill_capture_task is first
        assert len(calls) == 1 and calls[0] is not manager.ib_conn.ib
    finally:
        release.set()
        await finish_capture(manager)


@pytest.mark.asyncio
async def test_cached_capture_keeps_exec_id_dedup_without_history_cursor(tmp_path, monkeypatch):
    manager = simple_manager(tmp_path)
    manager.ib_conn.is_healthy = lambda: True
    ib = manager.ib_conn.ib = MagicMock()
    ib.reqExecutionsAsync = AsyncMock(side_effect=AssertionError("no live history request"))
    ib.fills.return_value = [
        _fake_fill(exec_id="cache-open", shares=1, price=5, side="BOT"),
        _fake_fill(exec_id="cache-close", shares=1, price=6, side="SLD", realized=98),
    ]
    ddir = tmp_path / "captured"
    monkeypatch.setenv("EXITMGR_DATASET_DIR", str(ddir))
    monkeypatch.setattr(exec_capture._tc, "dataset_dir", lambda journal: str(ddir))
    await manager._capture_external_fills_safe()
    await finish_capture(manager)
    watermark = exec_capture.load_watermark(str(ddir))
    assert watermark["_processed"] == {"cache-open", "cache-close"}
    assert set(json.loads(open(exec_capture.watermark_path(str(ddir))).read())) == {
        "schema", "processed_exec_ids", "runs", "updated"}
    dataset = exec_capture._tc.dataset_path(str(ddir))
    original = open(dataset).read()
    manager._last_extfill_capture = 0
    await manager._capture_external_fills_safe()
    await finish_capture(manager)
    assert open(dataset).read() == original
    ib.reqExecutionsAsync.assert_not_awaited()


@pytest.mark.asyncio
async def test_capture_exception_is_observed_and_cadence_stays_bounded(tmp_path, monkeypatch, capsys):
    manager = simple_manager(tmp_path)
    manager.ib_conn.is_healthy = lambda: True
    manager.ib_conn.ib = MagicMock()
    manager.ib_conn.ib.fills.return_value = [_fake_fill(exec_id="cached")]
    capture = AsyncMock(side_effect=RuntimeError("fixture capture failure"))
    monkeypatch.setattr(exec_capture, "capture_external_fills", capture)
    await manager._capture_external_fills_safe()
    await finish_capture(manager)
    assert "fixture capture failure" in capsys.readouterr().out
    await manager._capture_external_fills_safe()
    assert capture.await_count == 1
