import asyncio

import pytest

from exitmgr import atr_cache
from exitmgr.config import Config
from exitmgr.manager import ExitManager


def _manager(tmp_path):
    cfg = Config()
    cfg.journal.path = str(tmp_path / "trades.log")
    cfg.state.path = str(tmp_path / "state.json")
    cfg.kill_switch.path = str(tmp_path / "KILL")
    (tmp_path / "trades.log").write_text("")
    return ExitManager(cfg)


@pytest.mark.asyncio
async def test_missing_live_atr_requests_one_offcycle_refresh_and_never_blocks(
        tmp_path, monkeypatch):
    mgr = _manager(tmp_path)
    monkeypatch.setattr(atr_cache, "read", lambda _symbol: None)
    calls = []

    def _refresh(symbols):
        calls.append(list(symbols))
        return {"ok": list(symbols), "failed": [], "path": str(tmp_path / "atr.json")}

    monkeypatch.setattr(atr_cache, "refresh", _refresh)


    assert mgr._atr_levels_for(1, "CRM", 100.0, 1, 0.30, 60) is None
    assert mgr._atr_refresh_requested == {"CRM"}
    result = await asyncio.wait_for(mgr.refresh_requested_atr_offcycle(), timeout=0.5)

    assert calls == [["CRM"]]
    assert result["ok"] == ["CRM"]
    assert mgr._atr_refresh_requested == set()


@pytest.mark.asyncio
async def test_failed_offcycle_atr_refresh_keeps_request_for_retry(tmp_path, monkeypatch):
    mgr = _manager(tmp_path)
    mgr._atr_refresh_requested.add("NVDX")
    monkeypatch.setattr(
        atr_cache, "refresh",
        lambda symbols: {"ok": [], "failed": list(symbols),
                         "path": str(tmp_path / "atr.json")})

    await mgr.refresh_requested_atr_offcycle()

    assert mgr._atr_refresh_requested == {"NVDX"}
