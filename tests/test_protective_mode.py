from pathlib import Path
from types import SimpleNamespace

import pytest
import typer

import run_trader
from exitmgr.config import Config


def test_protective_client_id_is_distinct_and_validated():
    cfg = Config()
    cfg.ib.client_id = 88
    cfg.ib.protective_client_id = 189
    assert run_trader._selected_client_id(cfg, "entry", None) == 88
    assert run_trader._selected_client_id(cfg, "protective", None) == 189
    assert run_trader._selected_client_id(cfg, "protective", 190) == 190
    with pytest.raises(typer.BadParameter):
        run_trader._selected_client_id(cfg, "protective", 88)


def test_protective_one_shot_has_no_entry_or_model_path(tmp_path, monkeypatch):
    cfg = Config()
    cfg.ib.client_id = 88
    cfg.ib.protective_client_id = 189
    cfg.journal.path = str(tmp_path / "trades.log")
    marker = tmp_path / "TRADING_DOWN"
    marker.write_text("stand down")
    monkeypatch.setattr(run_trader, "TRADING_DOWN_MARKER", str(marker))
    monkeypatch.setattr(run_trader, "load_config", lambda **kwargs: cfg)
    calls = {"entry": 0, "protective": 0, "client_id": None, "defer_model": None}

    class FakeConnection:
        def __init__(self, **kwargs):
            calls["client_id"] = kwargs["client_id"]
            self.ib = object()

        async def connect(self, **kwargs):
            return True

        async def disconnect(self):
            return None

    class FakeManager:
        def __init__(self, _cfg):
            self.ib_conn = None

        async def _reconcile_on_startup(self):
            return True

        async def run_cycle(self, dry_run, **kwargs):
            calls["protective"] += 1
            calls["defer_model"] = kwargs.get("defer_model")

    class FailEntryTrader:
        def __init__(self, **kwargs):
            self._regime = None
            self._price_stats = {}

        async def run_once(self, *args, **kwargs):
            calls["entry"] += 1
            raise AssertionError("protective mode reached entry/model path")

    monkeypatch.setattr(run_trader, "IBConnection", FakeConnection)
    monkeypatch.setattr(run_trader, "ExitManager", FakeManager)
    monkeypatch.setattr(run_trader, "Trader", FailEntryTrader)
    run_trader.main(config="unused", arm=True, loop=False, interval=900,
                    protective_interval=30, mode="protective", client_id=189)
    assert calls == {"entry": 0, "protective": 1, "client_id": 189, "defer_model": True}


def test_service_contract_has_launchagent_distinct_ids_and_shared_process_lock():
    root = Path(__file__).resolve().parents[1]
    entry = (root / "run_trader_service.sh").read_text()
    protective = (root / "run_protective_service.sh").read_text()
    plist = (root / "ops" / "ai.alfred.protective.plist").read_text()
    assert "--mode entry" in entry
    assert "--mode protective" in protective and "--client-id" in protective
    assert "PROTECTIVE_IB_CLIENT_ID:-189" in protective
    lock_line = 'EXITMGR_ORDER_LOCK="${EXITMGR_ORDER_LOCK:-$HOME/.local/var/exitmgr/order-mutation.lock}"'
    assert lock_line in entry and lock_line in protective
    assert "ai.alfred.protective" in plist and "run_protective_service.sh" in plist
    assert "StandardOutPath" in plist and "protective-service.log" in plist
    assert "StandardErrorPath" in plist and "protective-service.error.log" in plist
    # Both mutation implementations—not just the in-process asyncio lock—hold the host lock.
    assert "order_mutation_lock" in (root / "exitmgr" / "connection.py").read_text()
    assert "order_mutation_lock" in (root / "exitmgr" / "trader.py").read_text()
