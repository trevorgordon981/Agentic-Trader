from pathlib import Path
from types import SimpleNamespace

import pytest
import typer
from unittest.mock import MagicMock

import run_trader
import exitmgr.trader as trader_mod
from exitmgr.config import Config, load_config
from exitmgr.risk import RiskLimits
from exitmgr.runtime_identity import RuntimeIdentity, resolved_config_sha256


def test_protective_client_id_is_distinct_and_validated():
    cfg = Config()
    cfg.ib.client_id = 88
    cfg.ib.protective_client_id = 189
    assert run_trader._selected_client_id(cfg, "entry", None) == 88
    assert run_trader._selected_client_id(cfg, "protective", None) == 189
    assert run_trader._selected_client_id(cfg, "protective", 190) == 190
    with pytest.raises(typer.BadParameter):
        run_trader._selected_client_id(cfg, "protective", 88)


def test_protective_identity_matches_manual_frontend_baseline(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        "loop:\n  interval_seconds: 60\n"
        "caps:\n  max_orders_per_day: 20\n  max_notional_per_day: 1000\n"
        "construction:\n  max_entry_spread_pct: 25.0\n"
    )
    frontend = load_config(str(config))
    protective = load_config(
        str(config), arm=True, loop=True,
        interval=run_trader._config_interval_for_mode("protective", 900),
    )
    entry = load_config(
        str(config), arm=True, loop=True,
        interval=run_trader._config_interval_for_mode("entry", 1200),
    )

    assert resolved_config_sha256(protective) == resolved_config_sha256(frontend)
    assert resolved_config_sha256(entry) != resolved_config_sha256(frontend)


def test_protective_one_shot_has_no_entry_or_model_path(tmp_path, monkeypatch):
    cfg = Config()
    cfg.ib.client_id = 88
    cfg.ib.protective_client_id = 189
    cfg.journal.path = str(tmp_path / "trades.log")
    cfg.audit_path = str(tmp_path / "audit.jsonl")
    marker = tmp_path / "TRADING_DOWN"
    marker.write_text("stand down")
    monkeypatch.setattr(run_trader, "TRADING_DOWN_MARKER", str(marker))
    monkeypatch.setattr(run_trader, "load_config", lambda **kwargs: cfg)
    monkeypatch.setattr(run_trader, "freeze_runtime_identity",
                        lambda *_args, **_kwargs: RuntimeIdentity("a" * 40, "b" * 64))
    calls = {"entry": 0, "protective": 0, "client_ids": [], "defer_model": None,
             "entry_ledger_required": None}

    class FakeConnection:
        def __init__(self, **kwargs):
            calls["client_ids"].append(kwargs["client_id"])
            self.client_id = kwargs["client_id"]
            self.ib = object()

        async def connect(self, **kwargs):
            return True

        async def disconnect(self):
            return None

    class FakeManager:
        def __init__(self, _cfg, runtime_identity=None, state_persist=True,
                     journal_side_effects=True):
            self.ib_conn = None
            self.quote_ib_conn = None
            self.order_manager = SimpleNamespace(ib_conn=None)
            self.state_manager = SimpleNamespace(state=object())
            assert runtime_identity == RuntimeIdentity("a" * 40, "b" * 64)
            assert state_persist is True
            assert journal_side_effects is True

        async def _reconcile_on_startup(self):
            return True

        async def run_cycle(self, dry_run, **kwargs):
            calls["protective"] += 1
            calls["defer_model"] = kwargs.get("defer_model")

    class FailEntryTrader:
        def __init__(self, **kwargs):
            calls["entry_ledger_required"] = kwargs.get("entry_ledger_required")
            self._regime = None
            self._price_stats = {}

        async def run_once(self, *args, **kwargs):
            calls["entry"] += 1
            raise AssertionError("protective mode reached entry/model path")

    monkeypatch.setattr(run_trader, "IBConnection", FakeConnection)
    monkeypatch.setattr(run_trader, "ExitManager", FakeManager)
    monkeypatch.setattr(run_trader, "Trader", FailEntryTrader)
    run_trader.main(config="unused", arm=True, loop=False, interval=900,
                    protective_interval=30, mode="protective", client_id=189,
                    quote_client_id=118)
    assert calls == {"entry": 0, "protective": 1, "client_ids": [189, 118],
                     "defer_model": True, "entry_ledger_required": False}


def test_real_protective_trader_never_constructs_or_migrates_entry_ledger(tmp_path, monkeypatch):
    cfg = Config()
    identity = RuntimeIdentity("a" * 40, "b" * 64)
    monkeypatch.setattr(trader_mod, "freeze_runtime_identity", lambda _cfg: identity)
    monkeypatch.setattr(
        trader_mod, "EntryReservationLedger",
        lambda: pytest.fail("protective-only startup touched the entry ledger"))

    instance = trader_mod.Trader(
        ib_conn=MagicMock(), exit_manager=MagicMock(), limits=RiskLimits(), approved_names=set(),
        endpoint="http://unused", model="unused", slack_token="", slack_channel="",
        approver_ids=set(), baseline_path=str(tmp_path / "baseline.json"),
        audit_path=str(tmp_path / "audit.jsonl"), config_path=str(tmp_path / "config.yaml"),
        resolved_config=cfg, runtime_identity=identity, entry_ledger_required=False)

    assert instance.entry_reservation_ledger is None


def test_service_contract_has_launchagent_distinct_ids_and_shared_process_lock():
    root = Path(__file__).resolve().parents[1]
    entry = (root / "run_trader_service.sh").read_text()
    protective = (root / "run_protective_service.sh").read_text()
    plist = (root / "ops" / "ai.alfred.protective.plist").read_text()
    assert "--mode entry" in entry
    assert "--mode protective" in protective and "--client-id" in protective
    assert "PROTECTIVE_IB_CLIENT_ID:-189" in protective
    assert "PROTECTIVE_QUOTE_IB_CLIENT_ID:-118" in protective
    lock_line = 'EXITMGR_ORDER_LOCK="${EXITMGR_ORDER_LOCK:-$HOME/.local/var/exitmgr/order-mutation.lock}"'
    assert lock_line in entry and lock_line in protective
    assert "ai.alfred.protective" in plist and "run_protective_service.sh" in plist
    assert "StandardOutPath" in plist and "protective-service.log" in plist
    assert "StandardErrorPath" in plist and "protective-service.error.log" in plist

    assert "order_mutation_lock" in (root / "exitmgr" / "connection.py").read_text()
    assert "order_mutation_lock" in (root / "exitmgr" / "trader.py").read_text()
