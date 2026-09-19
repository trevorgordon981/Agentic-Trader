"""Public API contract; production-derived narrative omitted."""

import ast
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from exitmgr.config import Config
from exitmgr.manager import ExitManager
from exitmgr.protective_clock import ProtectiveClock
from exitmgr.state import InFlightClose


CON_ID = 881122
ROOT = Path(__file__).resolve().parents[1]


def _manager(tmp_path):
    cfg = Config()
    cfg.state.path = str(tmp_path / "state.json")
    cfg.journal.path = str(tmp_path / "trades.log")
    cfg.audit_path = str(tmp_path / "audit.jsonl")
    return ExitManager(cfg, journal_side_effects=False)


def _in_flight(order_id=77, order_ref="protective-a", placement_state="submitted"):
    return InFlightClose(
        con_id=CON_ID, order_id=order_id, remaining_qty=1, entry_debit=500.0,
        client_id=42, order_ref=order_ref, identity_version=1,
        placement_state=placement_state,
        placed_at="2026-08-25T00:00:00+00:00",
        exit_context={"symbol": "AAPL", "close_qty": 1, "position_qty": 1},
    )


def test_heartbeat_is_atomic_durable_and_counts_strictly_over_30_seconds(tmp_path):
    clock = ProtectiveClock(
        tmp_path / "clock.json", deadline_seconds=30, stale_after_seconds=60,
        alert_throttle_seconds=900)
    base = datetime(2026, 8, 25, tzinfo=timezone.utc)

    seq = clock.start_cycle(
        unresolved_close_count=2,
        last_good_broker_view_at="2026-08-24T23:59:00+00:00", now=base)
    active = clock.read()
    assert active["active"] is True
    assert active["cycle_start_at"] == base.isoformat()
    assert active["cycle_end_at"] is None and active["duration_seconds"] is None
    assert active["last_good_broker_view_at"] == "2026-08-24T23:59:00+00:00"
    assert active["unresolved_close_count"] == 2

    first = clock.complete_cycle(
        seq, duration_seconds=30.000001, unresolved_close_count=1,
        now=base + timedelta(seconds=30.000001))
    assert first["deadline_missed"] is True
    assert first["consecutive_deadline_misses"] == 1

    seq = clock.start_cycle(unresolved_close_count=1, now=base + timedelta(seconds=31))
    second = clock.complete_cycle(
        seq, duration_seconds=31, unresolved_close_count=1,
        now=base + timedelta(seconds=62))
    assert second["consecutive_deadline_misses"] == 2
    assert not list(tmp_path.glob("*.tmp")), "atomic writer leaked a partial generation"
    assert (tmp_path / "clock.json").stat().st_mode & 0o777 == 0o600


def test_watchdog_claims_are_condition_scoped_and_bounded(tmp_path):
    clock = ProtectiveClock(
        tmp_path / "clock.json", deadline_seconds=30, stale_after_seconds=60,
        alert_throttle_seconds=900)
    base = datetime(2026, 8, 25, tzinfo=timezone.utc)
    seq = clock.start_cycle(unresolved_close_count=3, now=base)

    claimed = clock.claim_alert(now=base + timedelta(seconds=31))
    assert claimed and claimed[0] == "cycle_running_over_deadline"
    assert clock.claim_alert(now=base + timedelta(seconds=32)) is None
    clock.complete_cycle(
        seq, duration_seconds=31, unresolved_close_count=3,
        now=base + timedelta(seconds=33))

    seq = clock.start_cycle(unresolved_close_count=3, now=base + timedelta(seconds=34))
    clock.complete_cycle(
        seq, duration_seconds=31, unresolved_close_count=3,
        now=base + timedelta(seconds=65))
    claimed = clock.claim_alert(now=base + timedelta(seconds=66))
    assert claimed and claimed[0] == "consecutive_deadline_misses"
    assert clock.claim_alert(now=base + timedelta(seconds=67)) is None

    seq = clock.start_cycle(unresolved_close_count=0, now=base + timedelta(seconds=68))
    good = clock.complete_cycle(
        seq, duration_seconds=30, unresolved_close_count=0,
        now=base + timedelta(seconds=98))
    assert good["deadline_missed"] is False
    assert good["consecutive_deadline_misses"] == 0
    claimed = clock.claim_alert(now=base + timedelta(seconds=159))
    assert claimed and claimed[0] == "protective_cycle_stale"


def test_watchdog_pages_when_cycles_run_but_broker_truth_stops_advancing(tmp_path):
    clock = ProtectiveClock(
        tmp_path / "clock.json", deadline_seconds=30, stale_after_seconds=60,
        alert_throttle_seconds=900)
    base = datetime(2026, 8, 25, tzinfo=timezone.utc)
    seq = clock.start_cycle(
        unresolved_close_count=1, last_good_broker_view_at=base.isoformat(), now=base)
    clock.complete_cycle(
        seq, duration_seconds=1, unresolved_close_count=1,
        last_good_broker_view_at=base.isoformat(), now=base + timedelta(seconds=1))


    claimed = clock.claim_alert(now=base + timedelta(seconds=61))
    assert claimed and claimed[0] == "broker_view_stale"
    assert clock.claim_alert(now=base + timedelta(seconds=62)) is None


def test_restart_cannot_erase_an_unclaimed_overrun_with_one_fast_cycle(tmp_path):
    path = tmp_path / "clock.json"
    base = datetime(2026, 8, 25, tzinfo=timezone.utc)
    before_crash = ProtectiveClock(path, deadline_seconds=30, stale_after_seconds=60)
    before_crash.start_cycle(unresolved_close_count=4, now=base)



    restarted = ProtectiveClock(path, deadline_seconds=30, stale_after_seconds=60)
    seq = restarted.start_cycle(
        unresolved_close_count=4, now=base + timedelta(seconds=31))
    restarted.complete_cycle(
        seq, duration_seconds=1, unresolved_close_count=4,
        now=base + timedelta(seconds=32))

    recovered = restarted.read()
    assert recovered["consecutive_deadline_misses"] == 0
    assert recovered["pending_alert"] == "interrupted_cycle_overrun"
    claimed = restarted.claim_alert(now=base + timedelta(seconds=33))
    assert claimed and claimed[0] == "interrupted_cycle_overrun"
    assert restarted.read()["pending_alert"] is None
    assert restarted.claim_alert(now=base + timedelta(seconds=34)) is None


def test_restart_does_not_repage_an_overrun_the_watchdog_already_claimed(tmp_path):
    path = tmp_path / "clock.json"
    base = datetime(2026, 8, 25, tzinfo=timezone.utc)
    before_crash = ProtectiveClock(path, deadline_seconds=30, stale_after_seconds=60)
    before_crash.start_cycle(unresolved_close_count=4, now=base)
    claimed = before_crash.claim_alert(now=base + timedelta(seconds=31))
    assert claimed and claimed[0] == "cycle_running_over_deadline"

    restarted = ProtectiveClock(path, deadline_seconds=30, stale_after_seconds=60)
    seq = restarted.start_cycle(
        unresolved_close_count=4, now=base + timedelta(seconds=32))
    restarted.complete_cycle(
        seq, duration_seconds=1, unresolved_close_count=4,
        now=base + timedelta(seconds=33))

    assert restarted.read()["pending_alert"] is None
    assert restarted.claim_alert(now=base + timedelta(seconds=34)) is None


@pytest.mark.asyncio
async def test_protective_fill_poll_never_requests_terminal_history(tmp_path):
    mgr = _manager(tmp_path)
    inf = _in_flight()
    mgr.state_manager.state.add_in_flight(inf)
    mgr.ib_conn.ib = MagicMock()
    mgr.ib_conn.ib.trades.return_value = []
    mgr.ib_conn.ib.reqExecutionsAsync = AsyncMock(
        side_effect=AssertionError("history request reached protective cycle"))
    mgr.ib_conn.ib.reqCompletedOrdersAsync = AsyncMock(
        side_effect=AssertionError("history request reached protective cycle"))

    await mgr._poll_in_flight_fills(
        {CON_ID: {"remaining": 1}},
        live_positions={CON_ID: SimpleNamespace(quantity=1)})

    mgr.ib_conn.ib.reqExecutionsAsync.assert_not_awaited()
    mgr.ib_conn.ib.reqCompletedOrdersAsync.assert_not_awaited()
    assert mgr.state_manager.state.get_in_flight(CON_ID) is inf


@pytest.mark.asyncio
async def test_offcycle_history_worker_uses_a_snapshot_not_live_latch(tmp_path):
    mgr = _manager(tmp_path)
    inf = _in_flight()
    mgr.state_manager.state.add_in_flight(inf)

    async def mutate_snapshot(infs, *, persist_recovery_receipts):
        assert persist_recovery_receipts is False
        snap = infs[str(CON_ID)]
        assert snap is not inf
        snap.order_ref = "mutated-offcycle"
        return {}

    mgr._terminal_trades_for_in_flight = AsyncMock(side_effect=mutate_snapshot)
    await mgr.refresh_terminal_history_offcycle()
    assert inf.order_ref == "protective-a"


def test_history_completeness_is_bound_to_exact_covered_close_identity(tmp_path):
    mgr = _manager(tmp_path)
    old = _in_flight(order_id=77, order_ref="old-close")
    new = _in_flight(order_id=78, order_ref="new-close", placement_state="intent")
    old_key = mgr._terminal_cache_key(CON_ID, old)
    new_key = mgr._terminal_cache_key(CON_ID, new)
    mgr._terminal_lookup_complete = True
    mgr._terminal_history_refreshed_at = __import__("time").monotonic()
    mgr._terminal_history_covered_keys = {old_key}

    _found, complete = mgr._cached_terminal_trades_for_in_flight({str(CON_ID): new})
    assert new_key != old_key
    assert CON_ID not in complete, "stale history could release a newer durable intent"


def test_slow_work_is_structurally_outside_protective_loop():
    source = (ROOT / "run_trader.py").read_text()
    tree = ast.parse(source)
    functions = {node.name: node for node in ast.walk(tree)
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    protective = ast.get_source_segment(source, functions["_protective_loop"])
    assert "research.gather" not in protective
    assert "refresh_terminal_history_offcycle" not in protective
    assert "_terminal_trades_for_in_flight" not in protective
    assert "research.gather" in ast.get_source_segment(
        source, functions["_research_refresh_loop"])
    assert "refresh_terminal_history_offcycle" in ast.get_source_segment(
        source, functions["_terminal_history_loop"])

    manager_tree = ast.parse((ROOT / "exitmgr/manager.py").read_text())
    poll = next(node for node in ast.walk(manager_tree)
                if isinstance(node, ast.AsyncFunctionDef)
                and node.name == "_poll_in_flight_fills")
    called_attributes = {node.func.attr for node in ast.walk(poll)
                         if isinstance(node, ast.Call)
                         and isinstance(node.func, ast.Attribute)}
    assert "reqExecutionsAsync" not in called_attributes
    assert "reqCompletedOrdersAsync" not in called_attributes
