"""Regression tests verifying audit findings AT01 through AT05."""

from __future__ import annotations

import asyncio
import json
import math
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from typer.testing import CliRunner

import main as cli_main
from exitmgr.config import Config, ConfigError, load_config
from exitmgr.entry_reservation import EntryReservationError, EntryReservationLedger
from exitmgr.entry_throttle import (
    EntryThrottleStore,
    EntryThrottleUnreadable,
    entry_day_open_counts,
)
from exitmgr.manager import BrokerStartupError, ExitManager
from exitmgr.state import DailyStats, StateManager

REPO_ROOT = Path(__file__).resolve().parents[1]
PAPER_CONFIG = REPO_ROOT / "config.paper.yaml"
LIVE_CONFIG = REPO_ROOT / "config.yaml"


def _write_valid_cfg(tmp_path: Path) -> Path:
    data = yaml.safe_load(PAPER_CONFIG.read_text(encoding="utf-8"))
    data["state"]["path"] = str(tmp_path / "state.json")
    data["journal"]["path"] = str(tmp_path / "trades.log")
    data["kill_switch"]["path"] = str(tmp_path / "KILL_SWITCH")
    data["trading"]["baseline_path"] = str(tmp_path / "baseline.json")
    data["trading"]["audit_path"] = str(tmp_path / "audit.jsonl")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# AT01: Armed startup MUST reject missing explicit config paths (fail closed)
# ---------------------------------------------------------------------------


def test_at01_armed_load_config_refuses_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.yaml"
    with pytest.raises(ConfigError) as exc_info:
        load_config(str(missing), arm=True)
    assert exc_info.value.key_path == "config"
    assert "does_not_exist.yaml" in str(exc_info.value)


def test_at01_armed_load_config_refuses_directory_path(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc_info:
        load_config(str(tmp_path), arm=True)
    assert exc_info.value.key_path == "config"


def test_at01_cli_armed_missing_config_exits_nonzero_before_broker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker_touched = MagicMock()
    monkeypatch.setattr(cli_main, "ExitManager", broker_touched)
    monkeypatch.setattr(cli_main, "freeze_runtime_identity", MagicMock())

    runner = CliRunner()
    result = runner.invoke(cli_main.app, ["--config", str(tmp_path / "missing.yaml"), "--arm"])
    assert result.exit_code == 2
    assert "Startup configuration refused" in result.output
    broker_touched.assert_not_called()


def test_at01_paper_profile_is_valid_and_safe() -> None:
    cfg = Config.from_yaml(str(PAPER_CONFIG))
    assert cfg.ib.port == 7497
    assert cfg.auto_approve_within_gates is False
    assert cfg.caps.max_orders_per_day >= 1
    assert cfg.caps.max_notional_per_day > 0


# ---------------------------------------------------------------------------
# AT02: CLI cap & interval overrides MUST pass the same domain checks as YAML
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs,expected_key",
    [
        ({"max_orders_day": -1}, "caps.max_orders_per_day"),
        ({"max_orders_day": 0}, "caps.max_orders_per_day"),
        ({"max_orders_day": True}, "caps.max_orders_per_day"),
        ({"max_orders_cycle": -3}, "caps.max_orders_per_cycle"),
        ({"max_orders_cycle": 0}, "caps.max_orders_per_cycle"),
        ({"max_notional_day": -50.0}, "caps.max_notional_per_day"),
        ({"max_notional_day": 0.0}, "caps.max_notional_per_day"),
        ({"max_notional_day": float("nan")}, "caps.max_notional_per_day"),
        ({"max_notional_day": float("inf")}, "caps.max_notional_per_day"),
        ({"max_notional_day": float("-inf")}, "caps.max_notional_per_day"),
        ({"interval": 0}, "loop.interval_seconds"),
        ({"interval": -10}, "loop.interval_seconds"),
    ],
)
def test_at02_cli_overrides_enforced_with_yaml_domain_rules(
    tmp_path: Path, kwargs: dict, expected_key: str
) -> None:
    cfg_path = _write_valid_cfg(tmp_path)
    with pytest.raises(ConfigError) as exc_info:
        load_config(str(cfg_path), **kwargs)
    assert exc_info.value.key_path == expected_key


@pytest.mark.parametrize(
    "cli_args",
    [
        ["--max-orders-day", "-1"],
        ["--max-orders-cycle", "0"],
        ["--max-notional-day", "-100"],
        ["--max-notional-day", "nan"],
        ["--max-notional-day", "inf"],
        ["--interval", "0"],
    ],
)
def test_at02_cli_malformed_overrides_exit_nonzero_before_broker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_args: list[str]
) -> None:
    cfg_path = _write_valid_cfg(tmp_path)
    broker_touched = MagicMock()
    monkeypatch.setattr(cli_main, "ExitManager", broker_touched)
    monkeypatch.setattr(cli_main, "freeze_runtime_identity", MagicMock())

    runner = CliRunner()
    result = runner.invoke(cli_main.app, ["--config", str(cfg_path), *cli_args])
    assert result.exit_code == 2
    broker_touched.assert_not_called()


def test_at02_manager_check_caps_fails_closed_on_invalid_caps(tmp_path: Path) -> None:
    cfg_path = _write_valid_cfg(tmp_path)
    cfg = load_config(str(cfg_path))
    mgr = ExitManager(cfg)

    cfg.caps.max_notional_per_day = float("nan")
    ok, reason = mgr._check_caps(dry_run=True)
    assert ok is False
    assert "Invalid daily notional cap" in reason

    cfg.caps.max_notional_per_day = 1000.0
    cfg.caps.max_orders_per_day = -1
    ok, reason = mgr._check_caps(dry_run=True)
    assert ok is False
    assert "Invalid daily order cap" in reason


# ---------------------------------------------------------------------------
# AT03: Persisted throttle counters & ledgers reject partial/invalid records
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw_json",
    [
        '{"2026-08-12": {"orders_opened": 3}}',
        '{"2026-08-12": {"notional_opened": 100.0}}',
        '{"2026-08-12": {"orders_opened": 3, "notional_opened": 100.0, "extra": 1}}',
        '{"2026-08-12": {"orders_opened": -3, "notional_opened": 100.0}}',
        '{"2026-08-12": {"orders_opened": 3, "notional_opened": -1.0}}',
        '{"2026-08-12": {"orders_opened": 3, "notional_opened": NaN}}',
        '{"2026-08-12": {"orders_opened": 3, "notional_opened": Infinity}}',
        '{"2026-08-12": {"orders_opened": 3, "notional_opened": -Infinity}}',
        '{"2026-08-12": {"orders_opened": null, "notional_opened": 10.0}}',
        '{"2026-08-12": {"orders_opened": true, "notional_opened": 10.0}}',
        '{"2026-08-12": {"orders_opened": 1.5, "notional_opened": 10.0}}',
        '{"2026-08-12": {"orders_opened": 1, "notional_opened": false}}',
        '{"2026-08-12": {"orders_opened": 1, "orders_opened": 2, "notional_opened": 10.0}}',
        '{"2026-08-12": ["not", "an", "object"]}',
        '{"2026-08-12": null}',
        '{"2026-08-11": null}',
        '{"2026-08-11": {"orders_opened": -1, "notional_opened": 10.0}}',
    ],
)
def test_at03_throttle_store_refuses_malformed_records_and_preserves_evidence(
    tmp_path: Path, raw_json: str
) -> None:
    day = "2026-08-12"
    store = EntryThrottleStore(tmp_path / "entry_throttle.json")
    store.path.write_text(raw_json, encoding="utf-8")

    with pytest.raises(EntryThrottleUnreadable):
        store.read_day(day)
    with pytest.raises(EntryThrottleUnreadable):
        entry_day_open_counts(None, store, day)

    # Attempting to add MUST fail closed and NEVER overwrite corrupted evidence.
    assert store.add(day, 1, 50.0) is False
    assert store.path.read_text(encoding="utf-8") == raw_json


def test_at03_null_current_day_record_never_reads_as_zero_and_blocks_pre_submit(tmp_path: Path) -> None:
    day = "2026-01-02"
    raw = '{"2026-01-02": null}'
    store = EntryThrottleStore(tmp_path / "null.json")
    store.path.write_text(raw, encoding="utf-8")

    with pytest.raises(EntryThrottleUnreadable):
        store.read_day(day)
    with pytest.raises(EntryThrottleUnreadable):
        entry_day_open_counts(None, store, day)
    assert store.add(day, 1, 100.0) is False
    assert store.path.read_text(encoding="utf-8") == raw

    sm = StateManager(tmp_path / "state.json")
    sm.state.daily_stats[day] = None  # type: ignore[assignment]
    with pytest.raises(EntryThrottleUnreadable):
        entry_day_open_counts(sm, None, day)


@pytest.mark.parametrize(
    "order_count,notional",
    [
        (-1, 100.0),
        (True, 100.0),
        (1.5, 100.0),
        (1, -5.0),
        (1, float("nan")),
        (1, float("inf")),
        (1, False),
    ],
)
def test_at03_throttle_add_refuses_invalid_inputs(
    tmp_path: Path, order_count, notional
) -> None:
    day = "2026-08-12"
    store = EntryThrottleStore(tmp_path / "entry_throttle.json")
    assert store.add(day, 1, 250.0) is True
    before = store.path.read_text(encoding="utf-8")

    assert store.add(day, order_count, notional) is False
    assert store.path.read_text(encoding="utf-8") == before
    assert store.read_day(day) == (1, 250.0)


def test_at03_in_memory_corrupted_counters_refused(tmp_path: Path) -> None:
    day = "2026-08-12"
    sm = StateManager(tmp_path / "state.json")
    sm.state.daily_stats[day] = DailyStats(date=day, orders_opened=-2, notional_opened=100.0)
    with pytest.raises(EntryThrottleUnreadable):
        entry_day_open_counts(sm, None, day)

    sm.state.daily_stats[day] = DailyStats(date=day, orders_opened=1, notional_opened=float("nan"))
    with pytest.raises(EntryThrottleUnreadable):
        entry_day_open_counts(sm, None, day)


def test_at03_reservation_ledger_rejects_nan_and_duplicate_keys(tmp_path: Path) -> None:
    ledger_path = tmp_path / "reservations.json"
    ledger = EntryReservationLedger(
        ledger_path=ledger_path,
        lock_path=tmp_path / "reservations.lock",
    )

    ledger_path.write_text('{"version": 1, "reservations": {}, "intents": {}, "bad": NaN}', encoding="utf-8")
    with pytest.raises(EntryReservationError):
        ledger._load_state()

    ledger_path.write_text('{"version": 1, "version": 1, "reservations": {}, "intents": {}}', encoding="utf-8")
    with pytest.raises(EntryReservationError):
        ledger._load_state()


# ---------------------------------------------------------------------------
# AT04: Broker startup failure MUST raise / exit nonzero (not exit 0)
# ---------------------------------------------------------------------------


def test_at04_manager_run_raises_broker_startup_error_on_connect_failure(tmp_path: Path) -> None:
    cfg_path = _write_valid_cfg(tmp_path)
    cfg = load_config(str(cfg_path))
    mgr = ExitManager(cfg)

    class _FailingIB:
        async def connect(self) -> bool:
            return False

    mgr.ib_conn = _FailingIB()
    with pytest.raises(BrokerStartupError):
        asyncio.run(mgr.run())


def test_at04_cli_exits_nonzero_on_broker_connect_failure_vs_zero_on_keyboard_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path = _write_valid_cfg(tmp_path)
    monkeypatch.setattr(cli_main, "freeze_runtime_identity", lambda cfg, require_clean_code=True: MagicMock())

    class _FailManager:
        def __init__(self, cfg, runtime_identity=None):
            self.cfg = cfg

        async def run(self):
            raise BrokerStartupError("Could not connect to IB")

    monkeypatch.setattr(cli_main, "ExitManager", _FailManager)
    runner = CliRunner()
    res_fail = runner.invoke(cli_main.app, ["--config", str(cfg_path)])
    assert res_fail.exit_code == 1
    assert "Broker startup failed" in res_fail.output

    class _InterruptManager:
        def __init__(self, cfg, runtime_identity=None):
            self.cfg = cfg

        async def run(self):
            raise KeyboardInterrupt

    monkeypatch.setattr(cli_main, "ExitManager", _InterruptManager)
    res_interrupt = runner.invoke(cli_main.app, ["--config", str(cfg_path)])
    assert res_interrupt.exit_code == 0


# ---------------------------------------------------------------------------
# AT05: Offline broker-free proposal-to-close demo runs cleanly end-to-end
# ---------------------------------------------------------------------------


def test_at05_offline_demo_runs_cleanly_without_broker_or_network() -> None:
    demo = REPO_ROOT / "scripts" / "demo_offline_simulation.py"
    proc = subprocess.run(
        [sys.executable, str(demo), "--json"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, f"demo failed: stdout={proc.stdout}\nstderr={proc.stderr}"
    payload = json.loads(proc.stdout)
    assert payload["mode"] == "offline_broker_free_simulation"
    assert payload["signal_provider"]["is_production_strategy"] is False
    assert "at01_missing_armed_config_refused" in payload["exitmgr_fail_closed_checks"]
    assert "at02_negative_cli_cap_refused" in payload["exitmgr_fail_closed_checks"]
    assert "at03_partial_counter_refused" in payload["exitmgr_fail_closed_checks"]
    assert "at04_broker_startup_error" in payload["exitmgr_fail_closed_checks"]

    swing = payload["swing_lifecycle"]
    assert swing["timeout_fail_closed"]["uncertain_holdings"] is True
    assert swing["timeout_fail_closed"]["rearm_deferred_uncertain_qty"] is True
    assert swing["exit_alert_before_fees"]["tone"] == "NEUTRAL"
    assert swing["exit_alert_after_fees"]["tone"] == "WIN"
    assert swing["graduation_gate"]["requires_human_approval"] is True
    assert swing["graduation_gate"]["auto_promoted"] is False
