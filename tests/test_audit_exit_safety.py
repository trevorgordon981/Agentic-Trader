"""Public API contract; production-derived narrative omitted."""

import asyncio
import json
import stat
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from exitmgr.config import Config, RulesConfig, ScaleOutConfig, TrailingConfig
from exitmgr.connection import PositionData
from exitmgr.manager import ExitManager, build_journal_campaigns
from exitmgr.manual_exit_frontend import bound_requests
from exitmgr.manual_exit_queue import ManualExitQueue
from exitmgr.order import OrderResult, _trading_day
from exitmgr import reload_queue
from exitmgr.runtime_identity import RuntimeIdentity
from exitmgr.state import DailyStats, InFlightClose, State, StateManager, reconcile_state


CON = 991001
IDENTITY = RuntimeIdentity("1" * 40, "2" * 64)
JOURNAL = {
    "ts": "2026-07-01T14:00:00+00:00",
    "contract_id": CON,
    "symbol": "AAPL",
    "right": "C",
    "strike": 200.0,
    "expiry": "20261231",
    "quantity": 4,
    "debit": 2000.0,
    "conviction": 6,
}


def _manager(tmp_path):
    cfg = Config()
    cfg.dry_run = False
    cfg.loop_mode = False
    cfg.journal.path = str(tmp_path / "trades.log")
    cfg.state.path = str(tmp_path / "state.json")
    cfg.kill_switch.path = str(tmp_path / "KILL")
    cfg.audit_path = str(tmp_path / "audit.jsonl")
    cfg.manage_positions = False
    cfg.alerts_channel = ""
    cfg.error_channel = ""
    cfg.rules = RulesConfig(
        profit_target_pct=30.0,
        stop_pct=30.0,
        time_stop_days=10,
        trailing=TrailingConfig(enabled=False),
        scale_out=ScaleOutConfig(enabled=False),
    )
    (tmp_path / "trades.log").write_text(json.dumps(JOURNAL) + "\n")
    return ExitManager(cfg, runtime_identity=IDENTITY), cfg


def _campaign_conflict_manager(tmp_path):
    mgr, cfg = _manager(tmp_path)
    terminal = {
        "contract_id": CON, "symbol": "AAPL", "event": "closed_by_tool",
        "status": "Filled", "topology_complete": True,
    }
    with (tmp_path / "trades.log").open("a") as handle:
        handle.write(json.dumps(terminal) + "\n")
    mgr._journal_campaigns = build_journal_campaigns([JOURNAL, terminal])
    raw = dict(mgr._journal_entries.pop(CON))
    mgr._campaign_conflict_rows[CON] = raw
    mgr._campaign_conflicts[CON] = mgr._campaign_conflict_evidence(
        CON, raw, mgr._journal_campaigns)
    mgr._persist_campaign_conflicts()
    return mgr, cfg


def _context(*, manual=False, close_qty=4, position_qty=4, entry_debit=2000.0):
    return {
        "symbol": "AAPL",
        "reason": "manual" if manual else "stop",
        "trigger_type": "manual" if manual else "stop",
        "trigger_message": "audit regression",
        "trigger_pnl_pct": -30.0,
        "close_qty": close_qty,
        "position_qty": position_qty,
        "entry_debit": entry_debit,
        "journal_entry": dict(JOURNAL),
        "manual_request": manual,
        "extra": {
            "partial": close_qty < position_qty,
            "close_qty": close_qty,
            "remaining_qty": position_qty - close_qty,
            "trigger_mark": 3.5,
            "bid": 3.4,
            "limit_price": 3.5,
        },
    }


def _trade(order_id, status, *, avg_fill=0.0, filled=0, remaining=0, fills=None,
           client_id=0, perm_id=0, order_ref=None, con_id=None):
    trade = SimpleNamespace(
        order=SimpleNamespace(orderId=order_id, clientId=client_id, permId=perm_id,
                              orderRef=order_ref),
        orderStatus=SimpleNamespace(
            status=status, avgFillPrice=avg_fill, filled=filled, remaining=remaining),
        fills=list(fills or []),
    )
    if con_id is not None:
        trade.contract = SimpleNamespace(conId=con_id, secType="OPT")
    return trade


def _fill(order_id, shares, price, *, client_id=0, perm_id=0, con_id=CON, exec_id="E1",
          sec_type="OPT", side="SLD"):
    return SimpleNamespace(
        contract=SimpleNamespace(conId=con_id, secType=sec_type),
        execution=SimpleNamespace(orderId=order_id, clientId=client_id, permId=perm_id,
                                  side=side, shares=shares, price=price, execId=exec_id),
        commissionReport=SimpleNamespace(commission=1.0),
    )


def _read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _flat_evidence(*con_ids):
    """Public API contract; production-derived narrative omitted."""
    return {
        "schema": "position_flat_evidence.v1",
        "source": "ibkr_reqPositions",
        "observed_at": "2026-08-25T17:00:00+00:00",
        "absent_con_ids": list(con_ids or (CON,)),
    }


def _queue_manual_exit(mgr, tmp_path, *, approved_qty=4):
    position = SimpleNamespace(
        contract=SimpleNamespace(conId=CON, symbol="AAPL", secType="OPT"),
        position=approved_qty,
    )
    requests = bound_requests(
        mgr.config, [position], symbol="AAPL", runtime_identity=IDENTITY)
    assert len(requests) == 1
    ManualExitQueue(tmp_path / "manual_exits.json").add(requests)
    return requests[0]


def test_exit_context_round_trips_and_old_state_remains_compatible(tmp_path):
    sm = StateManager(str(tmp_path / "state.json"))
    sm.state.add_in_flight(InFlightClose(
        con_id=CON, order_id=77, remaining_qty=4, entry_debit=2000.0,
        exit_context=_context()))
    sm.save()
    loaded = StateManager(str(tmp_path / "state.json")).state.get_in_flight(CON)
    assert loaded.exit_context["symbol"] == "AAPL"
    assert loaded.exit_context["close_qty"] == 4
    assert stat.S_IMODE((tmp_path / "state.json").stat().st_mode) == 0o600

    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"in_flight": {str(CON): {
        "con_id": CON, "order_id": 88, "remaining_qty": 1, "entry_debit": 500.0
    }}, "daily_stats": {}, "last_cycle": None, "peak_prices": {}}))
    assert StateManager(str(legacy)).state.get_in_flight(CON).exit_context == {}


def test_reconcile_never_discards_context_before_terminal_broker_lookup():
    state = State()
    state.add_in_flight(InFlightClose(
        con_id=CON, order_id=77, remaining_qty=4, entry_debit=2000.0,
        exit_context=_context()))
    safe, _ = reconcile_state(
        state,
        live_positions={CON: {"qty": 4, "avg_cost": 5.0}},
        live_open_orders={},
        journal_entries={CON: {"debit": 2000.0}},
        journal_qtys={CON: 4},
    )
    assert safe
    assert state.get_in_flight(CON) is not None


@pytest.mark.asyncio
async def test_completed_fill_wins_over_stale_submitted_trade(tmp_path):
    mgr, _ = _manager(tmp_path)
    submitted = _trade(77, "Submitted")
    filled = _trade(77, "Filled", avg_fill=3.0, filled=4)
    mgr.ib_conn.ib = MagicMock()
    mgr.ib_conn.ib.trades.return_value = [submitted]
    mgr.ib_conn.ib.reqCompletedOrdersAsync = AsyncMock(return_value=[filled])
    mgr.ib_conn.ib.reqExecutionsAsync = AsyncMock(return_value=[])
    inf = InFlightClose(
        CON, 77, 4, 2000.0, exit_context=_context(),
        client_id=0, identity_version=1)
    found = await mgr._terminal_trades_for_in_flight({str(CON): inf})
    assert found[CON].orderStatus.status == "Filled"


@pytest.mark.asyncio
async def test_restart_partial_executions_never_fabricate_terminal_cancel(tmp_path):
    mgr, _ = _manager(tmp_path)
    mgr.ib_conn.ib = MagicMock()
    mgr.ib_conn.ib.trades.return_value = []
    mgr.ib_conn.ib.reqCompletedOrdersAsync = AsyncMock(return_value=[])
    mgr.ib_conn.ib.reqExecutionsAsync = AsyncMock(
        return_value=[_fill(77, 2, 6.0, exec_id="partial")])
    inf = InFlightClose(
        CON, 77, 4, 2000.0, exit_context=_context(),
        client_id=0, identity_version=1)
    found = await mgr._terminal_trades_for_in_flight({str(CON): inf})
    assert CON not in found


@pytest.mark.asyncio
async def test_restart_full_executions_finalize_weighted_actual_fill(tmp_path):
    mgr, _ = _manager(tmp_path)
    mgr.ib_conn.ib = MagicMock()
    mgr.ib_conn.ib.trades.return_value = []
    mgr.ib_conn.ib.reqCompletedOrdersAsync = AsyncMock(return_value=[])
    mgr.ib_conn.ib.reqExecutionsAsync = AsyncMock(return_value=[
        _fill(77, 1, 5.0, exec_id="a"), _fill(77, 3, 7.0, exec_id="b")])
    inf = InFlightClose(
        CON, 77, 4, 2000.0, exit_context=_context(), client_id=0,
        identity_version=1,
        submitted_close={
            "schema": "submitted_close.v1", "sec_type": "OPT", "action": "SELL",
            "combo_qty": 4, "legs": [{"con_id": CON, "ratio": 1,
                                        "expected_side": "SLD", "multiplier": 100}],
            "order_type": "LMT", "tif": "DAY", "limit_price": 3.5,
            "stop_price": None,
        })
    found = await mgr._terminal_trades_for_in_flight({str(CON): inf})
    assert found[CON].orderStatus.status == "Filled"
    assert found[CON].orderStatus.avgFillPrice == pytest.approx(6.5)
    assert mgr._finalize_in_flight_exit(
        CON, inf, found[CON], broker_flat_evidence=_flat_evidence())
    marker = [row for row in _read_jsonl(tmp_path / "trades.log")
              if row.get("event") == "position_closed"][0]
    assert marker["fill_evidence"]["binding_sha"] == inf.fill_evidence["binding_sha"]


def _combo_in_flight():
    return InFlightClose(
        CON, 77, 1, 331.0, exit_context=_context(close_qty=1, position_qty=1,
                                                  entry_debit=331.0),
        client_id=42, identity_version=1,
        submitted_close={
            "schema": "submitted_close.v1", "sec_type": "BAG", "action": "SELL",
            "combo_qty": 1,
            "legs": [
                {"con_id": CON, "ratio": 1, "expected_side": "SLD", "multiplier": 100},
                {"con_id": CON + 1, "ratio": 1, "expected_side": "BOT",
                 "multiplier": 100},
            ],
            "order_type": "MKT", "tif": "DAY", "limit_price": None,
            "stop_price": None,
        })


def _combo_leg_fills():
    return [
        _fill(77, 1, 10.35, client_id=42, con_id=CON, exec_id="long"),
        _fill(77, 1, 8.69, client_id=42, con_id=CON + 1, exec_id="short",
              side="BOT"),
    ]


@pytest.mark.asyncio
async def test_full_combo_executions_override_stale_submitted_and_degenerate_completed_fill(
        tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr, _ = _manager(tmp_path)
    inf = _combo_in_flight()
    mgr.state_manager.state.add_in_flight(inf)
    mgr.ib_conn.ib = MagicMock()
    mgr.ib_conn.ib.trades.return_value = [
        _trade(77, "Submitted", client_id=42, con_id=CON)]
    mgr.ib_conn.ib.reqExecutionsAsync = AsyncMock(return_value=_combo_leg_fills())
    mgr.ib_conn.ib.reqCompletedOrdersAsync = AsyncMock(return_value=[
        _trade(77, "Filled", avg_fill=0.0, filled=0, remaining=0,
               client_id=42, con_id=CON)])

    found = await mgr._terminal_trades_for_in_flight({str(CON): inf})

    trade = found[CON]
    assert trade.orderStatus.status == "Filled"
    assert trade.orderStatus.avgFillPrice == pytest.approx(1.66)
    assert trade.execution_reconstructed is True
    assert inf.fill_evidence["source"] == "ibkr_executions"
    key = mgr._terminal_cache_key(CON, inf)
    assert mgr._terminal_history_cache[key] is trade
    assert mgr._finalize_in_flight_exit(
        CON, inf, trade, broker_flat_evidence=_flat_evidence(CON, CON + 1))
    assert mgr.state_manager.state.get_in_flight(CON) is None


@pytest.mark.asyncio
async def test_stale_submitted_with_incomplete_combo_executions_stays_latched(tmp_path):
    mgr, _ = _manager(tmp_path)
    inf = _combo_in_flight()
    mgr.state_manager.state.add_in_flight(inf)
    mgr.ib_conn.ib = MagicMock()
    mgr.ib_conn.ib.trades.return_value = [
        _trade(77, "Submitted", client_id=42, con_id=CON)]
    mgr.ib_conn.ib.reqExecutionsAsync = AsyncMock(return_value=_combo_leg_fills()[:1])
    mgr.ib_conn.ib.reqCompletedOrdersAsync = AsyncMock(return_value=[
        _trade(77, "Filled", avg_fill=0.0, filled=0, remaining=0,
               client_id=42, con_id=CON)])

    found = await mgr._terminal_trades_for_in_flight({str(CON): inf})

    assert not mgr._terminal_trade_usable(found[CON])
    assert mgr._terminal_history_cache == {}
    assert inf.fill_evidence == {}
    assert mgr.state_manager.state.get_in_flight(CON) is inf


def test_combo_recovery_validates_bag_summary_but_prices_only_exact_legs(tmp_path):
    mgr, _ = _manager(tmp_path)
    fills = [
        _fill(77, 1, 1.66, client_id=42, con_id=28812380, exec_id="bag",
              sec_type="BAG"),
        *_combo_leg_fills(),
    ]

    trade, evidence, error = mgr._reconstruct_execution_trade(
        CON, _combo_in_flight(), fills)

    assert error is None
    assert trade.orderStatus.status == "Filled"
    assert trade.orderStatus.avgFillPrice == pytest.approx(1.66)
    assert evidence["net_fill_price"] == pytest.approx(1.66)
    assert {row["con_id"] for row in evidence["executions"]} == {CON, CON + 1}
    assert len(evidence["executions"]) == 2


def test_combo_recovery_still_rejects_an_unexpected_option_leg(tmp_path):
    mgr, _ = _manager(tmp_path)
    fills = [
        _fill(77, 1, 1.66, client_id=42, con_id=28812380, exec_id="bag",
              sec_type="BAG"),
        *_combo_leg_fills(),
        _fill(77, 1, 1.0, client_id=42, con_id=CON + 2, exec_id="alien"),
    ]

    trade, evidence, error = mgr._reconstruct_execution_trade(
        CON, _combo_in_flight(), fills)

    assert trade is None and evidence is None
    assert "unexpected leg" in error


@pytest.mark.parametrize(
    "bag_qty,bag_side,error_text",
    [(1, "BOT", "wrong side"), (2, "SLD", "aggregate BAG units")],
)
def test_combo_recovery_rejects_incoherent_bag_summary(
        tmp_path, bag_qty, bag_side, error_text):
    mgr, _ = _manager(tmp_path)
    fills = [
        _fill(77, bag_qty, 1.66, client_id=42, con_id=28812380, exec_id="bag",
              sec_type="BAG", side=bag_side),
        *_combo_leg_fills(),
    ]

    trade, evidence, error = mgr._reconstruct_execution_trade(
        CON, _combo_in_flight(), fills)

    assert trade is None and evidence is None
    assert error_text in error


def test_combo_recovery_never_uses_bag_summary_without_complete_legs(tmp_path):
    mgr, _ = _manager(tmp_path)
    bag_only = [_fill(
        77, 1, 1.66, client_id=42, con_id=28812380, exec_id="bag", sec_type="BAG")]

    trade, evidence, error = mgr._reconstruct_execution_trade(
        CON, _combo_in_flight(), bag_only)

    assert trade is None and evidence is None
    assert error == "no identity-matched executions"


@pytest.mark.asyncio
async def test_same_order_id_other_client_or_contract_is_ignored(tmp_path):
    mgr, _ = _manager(tmp_path)
    mgr.ib_conn.ib = MagicMock()
    mgr.ib_conn.ib.trades.return_value = [
        _trade(77, "Filled", avg_fill=6, filled=4, client_id=99, con_id=CON),
        _trade(77, "Filled", avg_fill=6, filled=4, client_id=42, con_id=CON + 1),
    ]
    mgr.ib_conn.ib.reqCompletedOrdersAsync = AsyncMock(return_value=[])
    mgr.ib_conn.ib.reqExecutionsAsync = AsyncMock(return_value=[])
    inf = InFlightClose(CON, 77, 4, 2000.0, exit_context=_context(), client_id=42)
    found = await mgr._terminal_trades_for_in_flight({str(CON): inf})
    assert CON not in found


@pytest.mark.asyncio
async def test_v1_order_ref_mismatch_cannot_fall_back_to_same_order_id(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr, _ = _manager(tmp_path)
    mgr.ib_conn.ib = MagicMock()
    mgr.ib_conn.ib.trades.return_value = [_trade(
        77, "Filled", avg_fill=6, filled=4, client_id=42,
        order_ref="different-close", con_id=CON)]
    mgr.ib_conn.ib.reqCompletedOrdersAsync = AsyncMock(return_value=[])
    mgr.ib_conn.ib.reqExecutionsAsync = AsyncMock(return_value=[])
    inf = InFlightClose(
        CON, 77, 4, 2000.0, exit_context=_context(), client_id=42,
        order_ref="exact-close", identity_version=1)

    found = await mgr._terminal_trades_for_in_flight({str(CON): inf})

    assert CON not in found


@pytest.mark.asyncio
async def test_perm_id_match_survives_client_rotation(tmp_path):
    mgr, _ = _manager(tmp_path)
    mgr.ib_conn.ib = MagicMock()
    mgr.ib_conn.ib.trades.return_value = [
        _trade(900, "Filled", avg_fill=6, filled=4, client_id=99,
               perm_id=123456, con_id=CON)]
    mgr.ib_conn.ib.reqCompletedOrdersAsync = AsyncMock(return_value=[])
    mgr.ib_conn.ib.reqExecutionsAsync = AsyncMock(return_value=[])
    inf = InFlightClose(CON, 77, 4, 2000.0, exit_context=_context(),
                        client_id=42, perm_id=123456)
    found = await mgr._terminal_trades_for_in_flight({str(CON): inf})
    assert found[CON].order.orderId == 900


@pytest.mark.asyncio
async def test_client_zero_is_strong_identity_not_legacy_order_id_fallback(tmp_path):
    mgr, _ = _manager(tmp_path)
    mgr.ib_conn.ib = MagicMock()
    mgr.ib_conn.ib.trades.return_value = []
    mgr.ib_conn.ib.reqCompletedOrdersAsync = AsyncMock(return_value=[])
    inf = InFlightClose(CON, 77, 4, 2000.0, exit_context=_context(),
                        client_id=0, identity_version=1)
    mgr.ib_conn.ib.reqExecutionsAsync = AsyncMock(return_value=[
        _fill(77, 4, 6.0, client_id=1, exec_id="wrong-client")])
    found = await mgr._terminal_trades_for_in_flight({str(CON): inf})
    assert CON not in found

    mgr._execution_lookup_next_at = 0.0
    mgr.ib_conn.ib.reqExecutionsAsync = AsyncMock(return_value=[
        _fill(77, 4, 6.0, client_id=0, exec_id="right-client")])
    found = await mgr._terminal_trades_for_in_flight({str(CON): inf})
    assert found[CON].orderStatus.status == "Filled"


def test_reconcile_client_zero_rejects_same_order_id_from_other_client():
    state = State()
    state.add_in_flight(InFlightClose(
        CON, 77, 4, 2000.0, exit_context=_context(),
        client_id=0, identity_version=1))
    safe, alerts = reconcile_state(
        state,
        live_positions={CON: {"qty": 4, "avg_cost": 5}},
        live_open_orders={CON: {"order_id": 77, "remaining": 4, "client_id": 1}},
        journal_entries={CON: {"debit": 2000}}, journal_qtys={CON: 4})
    assert not safe
    assert any("identity mismatch" in alert for alert in alerts)


def test_confirmed_fill_books_actual_price_once_and_clears_state(tmp_path):
    mgr, _ = _manager(tmp_path)
    inf = InFlightClose(
        CON, 77, 4, 2000.0, exit_context=_context(),
        client_id=0, identity_version=1)
    mgr.state_manager.state.add_in_flight(inf)
    trade = _trade(77, "Filled", avg_fill=6.0, filled=4)

    flat = _flat_evidence()
    assert mgr._finalize_in_flight_exit(CON, inf, trade, broker_flat_evidence=flat)

    assert mgr._finalize_in_flight_exit(CON, inf, trade, broker_flat_evidence=flat)
    rows = _read_jsonl(tmp_path / "exits.log")
    assert len(rows) == 1
    assert rows[0]["order_id"] == 77
    assert rows[0]["avg_fill_price"] == 6.0
    assert rows[0]["realized_pnl"] == pytest.approx(400.0)
    assert mgr.state_manager.state.get_in_flight(CON) is None


def test_full_fill_without_flat_proof_preserves_protective_authority(tmp_path):
    mgr, _ = _manager(tmp_path)
    mgr.state_manager.state.peak_prices[str(CON)] = 8.0
    mgr.state_manager.state.trail_configured[str(CON)] = True
    inf = InFlightClose(CON, 77, 4, 2000.0, exit_context=_context())
    mgr.state_manager.state.add_in_flight(inf)

    assert not mgr._finalize_in_flight_exit(
        CON, inf, _trade(77, "Filled", avg_fill=6.0, filled=4))
    assert mgr.state_manager.state.get_in_flight(CON) is inf
    assert mgr.state_manager.state.peak_prices[str(CON)] == 8.0
    assert mgr.state_manager.state.trail_configured[str(CON)] is True
    assert CON in mgr._journal_entries
    assert not any(row.get("event") == "position_closed"
                   for row in _read_jsonl(tmp_path / "trades.log"))


def test_campaign_conflict_emergency_fill_uses_distinct_resolution_receipt(tmp_path):
    mgr, cfg = _campaign_conflict_manager(tmp_path)
    assert CON in mgr._campaign_conflicts
    assert mgr._close_campaign_binding(CON) is None
    emergency, why = mgr._campaign_conflict_emergency_context(
        PositionData(CON, "AAPL", "C", 4, 5.0, "20261231", strike=200.0), {})
    assert why is None
    ctx = _context()
    ctx["journal_entry"] = {"contract_id": CON, "symbol": "AAPL", "quantity": 4}
    ctx["campaign_binding"] = None
    ctx["campaign_conflict_authority"] = emergency["authority"]
    inf = InFlightClose(CON, 77, 4, 2000.0, exit_context=ctx,
                        client_id=0, identity_version=1)
    mgr.state_manager.state.add_in_flight(inf)

    assert mgr._finalize_in_flight_exit(
        CON, inf, _trade(77, "Filled", avg_fill=3.0, filled=4, client_id=0),
        broker_flat_evidence=_flat_evidence())
    assert mgr.state_manager.state.get_in_flight(CON) is None
    rows = _read_jsonl(tmp_path / "trades.log")
    receipt = rows[-1]
    assert receipt["event"] == "campaign_conflict_position_closed"
    assert "campaign" not in receipt
    assert receipt["conflict_authority"]["fingerprint"] == (
        emergency["authority"]["fingerprint"])

    replay = ExitManager(cfg, runtime_identity=IDENTITY)
    assert replay._close_campaign_binding(CON) is None
    assert CON not in replay._journal_entries
    assert CON not in replay._campaign_conflicts


def test_delayed_campaign_conflict_receipt_cannot_erase_later_reentry(tmp_path):
    mgr, cfg = _campaign_conflict_manager(tmp_path)
    emergency, _ = mgr._campaign_conflict_emergency_context(
        PositionData(CON, "AAPL", "C", 4, 5.0, "20261231", strike=200.0), {})
    ctx = _context()
    ctx.update(journal_entry={"contract_id": CON, "symbol": "AAPL", "quantity": 4},
               campaign_binding=None,
               campaign_conflict_authority=emergency["authority"])
    inf = InFlightClose(CON, 77, 4, 2000.0, exit_context=ctx,
                        client_id=0, identity_version=1)
    mgr.state_manager.state.add_in_flight(inf)
    assert mgr._finalize_in_flight_exit(
        CON, inf, _trade(77, "Filled", avg_fill=3.0, filled=4, client_id=0),
        broker_flat_evidence=_flat_evidence())
    rows = _read_jsonl(tmp_path / "trades.log")
    receipt = rows.pop()
    later = dict(JOURNAL, ts="2026-09-18T21:20:00+00:00", decision_id="later-reentry")
    (tmp_path / "trades.log").write_text(
        "".join(json.dumps(row) + "\n" for row in [*rows, later, receipt]))

    replay = ExitManager(cfg, runtime_identity=IDENTITY)
    assert replay._journal_entries[CON]["decision_id"] == "later-reentry"
    assert replay._close_campaign_binding(CON) is not None


@pytest.mark.parametrize("mutation", ["fingerprint", "raw_digest", "flat_topology"])
def test_campaign_conflict_emergency_receipt_mismatch_retains_latch(tmp_path, mutation):
    mgr, _ = _campaign_conflict_manager(tmp_path)
    emergency, why = mgr._campaign_conflict_emergency_context(
        PositionData(CON, "AAPL", "C", 4, 5.0, "20261231", strike=200.0), {})
    assert why is None
    authority = json.loads(json.dumps(emergency["authority"]))
    flat = _flat_evidence()
    if mutation == "fingerprint":
        authority["fingerprint"] = "0" * 64
    elif mutation == "raw_digest":
        authority["quarantined_row_sha256"] = "0" * 64
    else:
        flat["absent_con_ids"] = [CON + 1]
    ctx = _context()
    ctx.update(journal_entry={"contract_id": CON, "symbol": "AAPL", "quantity": 4},
               campaign_binding=None, campaign_conflict_authority=authority)
    inf = InFlightClose(CON, 77, 4, 2000.0, exit_context=ctx,
                        client_id=0, identity_version=1)
    mgr.state_manager.state.add_in_flight(inf)
    assert not mgr._finalize_in_flight_exit(
        CON, inf, _trade(77, "Filled", avg_fill=3.0, filled=4, client_id=0),
        broker_flat_evidence=flat)
    assert mgr.state_manager.state.get_in_flight(CON) is inf
    assert not any(row.get("event") == "campaign_conflict_position_closed"
                   for row in _read_jsonl(tmp_path / "trades.log"))


def test_false_campaign_checkpoint_cannot_clear_without_exact_marker(tmp_path):
    mgr, _ = _manager(tmp_path)
    inf = InFlightClose(CON, 77, 4, 2000.0, exit_context=_context(),
                        side_effects={"campaign_boundary": True})
    mgr.state_manager.state.add_in_flight(inf)
    assert not mgr._finalize_in_flight_exit(
        CON, inf, _trade(77, "Filled", avg_fill=6.0, filled=4),
        broker_flat_evidence=_flat_evidence())
    assert mgr.state_manager.state.get_in_flight(CON) is inf
    assert CON in mgr._journal_entries


def test_contextless_filled_latch_is_retained_not_silently_deleted(tmp_path):
    mgr, _ = _manager(tmp_path)
    inf = InFlightClose(CON, 77, 4, 2000.0, client_id=0, identity_version=1)
    mgr.state_manager.state.add_in_flight(inf)
    assert not mgr._finalize_in_flight_exit(
        CON, inf, _trade(77, "Filled", avg_fill=6.0, filled=4, client_id=0))
    assert mgr.state_manager.state.get_in_flight(CON) is inf
    assert _read_jsonl(tmp_path / "exits.log") == []


def test_direct_finalizer_rejects_mismatched_broker_identity(tmp_path):
    mgr, _ = _manager(tmp_path)
    inf = InFlightClose(CON, 77, 4, 2000.0, exit_context=_context(),
                        client_id=42, identity_version=1)
    mgr.state_manager.state.add_in_flight(inf)
    assert not mgr._finalize_in_flight_exit(
        CON, inf, _trade(77, "Filled", avg_fill=6.0, filled=4, client_id=41),
        broker_flat_evidence=_flat_evidence())
    assert mgr.state_manager.state.get_in_flight(CON) is inf
    assert _read_jsonl(tmp_path / "exits.log") == []


@pytest.mark.asyncio
async def test_partial_terminal_cancel_recovers_exact_execution_price(tmp_path):
    mgr, _ = _manager(tmp_path)
    inf = InFlightClose(
        CON, 77, 4, 2000.0, exit_context=_context(), client_id=0,
        identity_version=1,
        submitted_close={
            "schema": "submitted_close.v1", "sec_type": "OPT", "action": "SELL",
            "combo_qty": 4, "legs": [{"con_id": CON, "ratio": 1,
                                        "expected_side": "SLD", "multiplier": 100}],
            "order_type": "LMT", "tif": "DAY", "limit_price": 3.5,
            "stop_price": None,
        })
    mgr.ib_conn.ib = MagicMock()
    mgr.ib_conn.ib.trades.return_value = [
        _trade(77, "Cancelled", avg_fill=0.0, filled=2, remaining=2, client_id=0)]
    mgr.ib_conn.ib.reqExecutionsAsync = AsyncMock(return_value=[
        _fill(77, 1, 5.0, client_id=0, exec_id="p1"),
        _fill(77, 1, 7.0, client_id=0, exec_id="p2"),
    ])
    mgr.ib_conn.ib.reqCompletedOrdersAsync = AsyncMock(return_value=[])
    found = await mgr._terminal_trades_for_in_flight({str(CON): inf})
    assert found[CON].orderStatus.status == "Cancelled"
    assert found[CON].orderStatus.filled == 2
    assert found[CON].orderStatus.avgFillPrice == pytest.approx(6.0)
    assert mgr._finalize_in_flight_exit(CON, inf, found[CON])
    row = _read_jsonl(tmp_path / "exits.log")[0]
    assert row["close_qty"] == 2 and row["realized_pnl"] == pytest.approx(200.0)


def test_terminal_partial_cancel_books_partial_pnl_and_keeps_manual_request(tmp_path):
    mgr, _ = _manager(tmp_path)
    request = _queue_manual_exit(mgr, tmp_path)
    inf = InFlightClose(
        CON, 77, 4, 2000.0,
        exit_context=dict(_context(manual=True, close_qty=4, position_qty=4),
                          manual_request_id=request["request_id"]),
        client_id=0, identity_version=1)
    mgr.state_manager.state.add_in_flight(inf)
    trade = _trade(77, "Cancelled", avg_fill=6.0, filled=2, remaining=2)

    assert mgr._finalize_in_flight_exit(CON, inf, trade)
    row = _read_jsonl(tmp_path / "exits.log")[0]
    assert row["realized_pnl"] == pytest.approx(200.0)
    assert row["close_qty"] == 2
    assert row["partial"] is True
    assert CON in ManualExitQueue(tmp_path / "manual_exits.json").read()
    assert mgr.state_manager.state.get_in_flight(CON) is None


def test_same_order_id_on_different_clients_commits_distinct_fills(tmp_path):
    mgr, _ = _manager(tmp_path)
    for client_id, price in ((41, 5.0), (42, 6.0)):
        inf = InFlightClose(CON, 77, 4, 2000.0, exit_context=_context(),
                            client_id=client_id)
        mgr.state_manager.state.add_in_flight(inf)
        assert mgr._finalize_in_flight_exit(
            CON, inf, _trade(77, "Filled", avg_fill=price, filled=4,
                             client_id=client_id, con_id=CON),
            broker_flat_evidence=_flat_evidence())
    rows = _read_jsonl(tmp_path / "exits.log")
    assert len(rows) == 2
    assert len({row["close_identity"] for row in rows}) == 2


def test_fill_replay_does_not_repeat_reload_or_alert_and_uses_frozen_entry(tmp_path):
    mgr, _ = _manager(tmp_path)
    mgr.config.reload_enabled = True
    ctx = _context()
    ctx.update({
        "reason": "take_profit", "trigger_type": "take_profit",
        "reload": True, "reload_conviction": 8,
    })
    ctx["journal_entry"].update({
        "thesis": "frozen continuation", "dte_at_entry": 30,
    })
    inf = InFlightClose(CON, 77, 4, 2000.0, exit_context=ctx,
                        client_id=42, order_ref="replay-safe-ref")
    mgr.state_manager.state.add_in_flight(inf)
    mgr._post_exit_alert = MagicMock(return_value=True)
    trade = _trade(77, "Filled", avg_fill=6.0, filled=4,
                   client_id=42, order_ref="replay-safe-ref", con_id=CON)

    flat = _flat_evidence()
    assert mgr._finalize_in_flight_exit(CON, inf, trade, broker_flat_evidence=flat)
    assert mgr._finalize_in_flight_exit(CON, inf, trade, broker_flat_evidence=flat)
    q = reload_queue.ReloadQueue(reload_queue.queue_path(mgr.config.journal.path))
    assert len(q.tickets) == 1
    assert q.tickets[0]["thesis"] == "frozen continuation"
    assert q.tickets[0]["source_fill_key"] == inf.fill_key
    assert mgr._post_exit_alert.call_count == 1


def test_full_manual_fill_clears_request_with_owner_only_atomic_file(tmp_path):
    mgr, _ = _manager(tmp_path)
    manual_path = tmp_path / "manual_exits.json"
    request = _queue_manual_exit(mgr, tmp_path)
    inf = InFlightClose(
        CON, 77, 4, 2000.0,
        exit_context=dict(_context(manual=True, close_qty=4, position_qty=4),
                          manual_request_id=request["request_id"]),
        client_id=0, identity_version=1)
    mgr.state_manager.state.add_in_flight(inf)
    assert mgr._finalize_in_flight_exit(
        CON, inf, _trade(77, "Filled", avg_fill=6.0, filled=4),
        broker_flat_evidence=_flat_evidence())
    assert ManualExitQueue(manual_path).read() == {}
    assert stat.S_IMODE(manual_path.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_zero_fill_cancel_releases_context_but_keeps_manual_retry(tmp_path):
    mgr, _ = _manager(tmp_path)
    request = _queue_manual_exit(mgr, tmp_path)
    inf = InFlightClose(
        CON, 77, 4, 2000.0,
        exit_context=dict(_context(manual=True),
                          manual_request_id=request["request_id"]),
        client_id=0, identity_version=1)
    mgr.state_manager.state.add_in_flight(inf)
    mgr.ib_conn.ib = MagicMock()
    mgr.ib_conn.ib.trades.return_value = [_trade(77, "Cancelled", filled=0)]
    mgr.ib_conn.ib.reqCompletedOrdersAsync = AsyncMock(return_value=[])
    mgr.ib_conn.ib.reqExecutionsAsync = AsyncMock(return_value=[])

    await mgr._poll_in_flight_fills({}, live_positions={CON: object()})
    assert mgr.state_manager.state.get_in_flight(CON) is None
    assert CON in ManualExitQueue(tmp_path / "manual_exits.json").read()


@pytest.mark.asyncio
async def test_zero_fill_cancel_empty_position_snapshot_still_keeps_manual_request(tmp_path):
    mgr, _ = _manager(tmp_path)
    request = _queue_manual_exit(mgr, tmp_path)
    inf = InFlightClose(CON, 77, 4, 2000.0,
                        exit_context=dict(_context(manual=True),
                                          manual_request_id=request["request_id"]))
    mgr.state_manager.state.add_in_flight(inf)
    mgr.ib_conn.ib = MagicMock()
    mgr.ib_conn.ib.trades.return_value = [_trade(77, "Cancelled", filled=0)]
    mgr.ib_conn.ib.reqCompletedOrdersAsync = AsyncMock(return_value=[])
    mgr.ib_conn.ib.reqExecutionsAsync = AsyncMock(return_value=[])
    await mgr._poll_in_flight_fills({}, live_positions={})
    assert CON in ManualExitQueue(tmp_path / "manual_exits.json").read()


@pytest.mark.asyncio
async def test_manual_retry_prorates_original_basis_to_live_remainder(tmp_path):
    mgr, _ = _manager(tmp_path)
    _queue_manual_exit(mgr, tmp_path)
    positions = {CON: PositionData(
        con_id=CON, symbol="AAPL", right="C", quantity=2,
        avg_cost=5.0, expiry="20261231")}
    mgr.ib_conn.get_positions = AsyncMock(return_value=positions)
    mgr.ib_conn.get_open_orders = AsyncMock(return_value={})
    mgr.ib_conn.fetch_quotes = AsyncMock(return_value={})
    mgr.ib_conn.ib = MagicMock()
    mgr.ib_conn.ib.trades.return_value = []
    mgr.ib_conn.ib.reqCompletedOrdersAsync = AsyncMock(return_value=[])
    mgr.ib_conn.ib.reqExecutionsAsync = AsyncMock(return_value=[])
    place = AsyncMock(return_value=OrderResult(success=False, message="test stop"))
    mgr.order_manager.place_close_order = place

    await mgr.run_cycle(dry_run=False)
    place.assert_called_once()
    assert place.call_args.kwargs["quantity"] == 2
    assert place.call_args.kwargs["entry_debit"] == pytest.approx(1000.0)
    assert place.call_args.kwargs["exit_context"]["entry_debit"] == pytest.approx(1000.0)


def test_reload_queue_dedupes_replay_even_after_ticket_was_drained(tmp_path):
    path = str(tmp_path / "reload.json")
    ticket = reload_queue.make_ticket(
        symbol="AAPL", thesis="t", right="C", width=None, dte_target=30,
        structure="single", is_index=False, reload_conviction=8,
        realized_pnl=100, original_debit=500, source_fill_key="fill-123")
    q = reload_queue.ReloadQueue(path)
    assert q.add_once(ticket)
    ready, _ = q.drain(today="2026-07-10", max_per_name=2)
    assert len(ready) == 1
    q2 = reload_queue.ReloadQueue(path)
    assert not q2.add_once(ticket)
    assert q2.tickets == []


def test_unfilled_dataset_row_is_not_terminal_close_dedupe(tmp_path):
    mgr, _ = _manager(tmp_path)
    trigger = SimpleNamespace(trigger_type="stop")
    mgr._log_unfilled_exit(
        CON, "AAPL", trigger, fill_status="Cancelled", close_qty=4,
        trigger_mark=3.5, bid=3.4, limit_price=3.4, order_id=77,
        reason="stop", placed_at="2026-07-10T00:00:00+00:00")
    assert CON not in mgr._full_close_on_disk()


@pytest.mark.asyncio
async def test_fill_poll_runs_before_flat_book_early_return(tmp_path):
    mgr, _ = _manager(tmp_path)
    inf = InFlightClose(
        CON, 77, 4, 2000.0, exit_context=_context(),
        client_id=0, identity_version=1,
        submitted_close={
            "schema": "submitted_close.v1", "sec_type": "OPT", "action": "SELL",
            "combo_qty": 4, "legs": [{"con_id": CON, "ratio": 1,
                                        "expected_side": "SLD", "multiplier": 100}],
            "order_type": "LMT", "tif": "DAY", "limit_price": 3.5,
            "stop_price": None,
        })
    mgr.state_manager.state.add_in_flight(inf)
    mgr.ib_conn.get_positions = AsyncMock(return_value={})
    mgr.ib_conn.get_open_orders = AsyncMock(return_value={})
    mgr.ib_conn.ib = MagicMock()
    mgr.ib_conn.ib.trades.return_value = []
    mgr.ib_conn.ib.reqCompletedOrdersAsync = AsyncMock(
        return_value=[_trade(77, "Filled", avg_fill=6.0, filled=4)])
    mgr.ib_conn.ib.reqExecutionsAsync = AsyncMock(return_value=[])



    await mgr.refresh_terminal_history_offcycle()
    await mgr.run_cycle(dry_run=False)
    assert mgr.state_manager.state.get_in_flight(CON) is None
    assert mgr.ib_conn.ib.reqCompletedOrdersAsync.await_count == 1
    assert _read_jsonl(tmp_path / "exits.log")[0]["avg_fill_price"] == 6.0


@pytest.mark.asyncio
async def test_terminal_poll_refetches_positions_before_any_second_close(tmp_path):
    mgr, _ = _manager(tmp_path)
    pos = PositionData(CON, "AAPL", "C", 4, 5.0, "20261231")
    mgr.state_manager.state.add_in_flight(InFlightClose(
        CON, 77, 4, 2000.0, exit_context=_context(),
        client_id=0, identity_version=1,
        submitted_close={
            "schema": "submitted_close.v1", "sec_type": "OPT", "action": "SELL",
            "combo_qty": 4, "legs": [{"con_id": CON, "ratio": 1,
                                        "expected_side": "SLD", "multiplier": 100}],
            "order_type": "LMT", "tif": "DAY", "limit_price": 3.5,
            "stop_price": None,
        }))

    mgr.ib_conn.get_positions = AsyncMock(side_effect=[{CON: pos}, {CON: pos}, {}, {}])
    mgr.ib_conn.get_open_orders = AsyncMock(return_value={})
    mgr.ib_conn.ib = MagicMock()
    mgr.ib_conn.ib.trades.return_value = [
        _trade(77, "Filled", avg_fill=6.0, filled=4)]
    mgr.ib_conn.ib.reqCompletedOrdersAsync = AsyncMock(return_value=[])
    mgr.ib_conn.ib.reqExecutionsAsync = AsyncMock(return_value=[])
    mgr.order_manager.place_close_order = AsyncMock()

    await mgr.run_cycle(dry_run=False)
    assert mgr.ib_conn.get_positions.await_count == 4
    mgr.order_manager.place_close_order.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,filled", [("Filled", 4), ("Cancelled", 2)])
async def test_immediate_manual_fill_cannot_trigger_second_same_cycle_close(
        tmp_path, status, filled):
    mgr, _ = _manager(tmp_path)
    _queue_manual_exit(mgr, tmp_path)
    positions = {CON: PositionData(CON, "AAPL", "C", 4, 5.0, "20261231")}
    mgr.ib_conn.get_positions = AsyncMock(return_value=positions)
    mgr.ib_conn.get_open_orders = AsyncMock(return_value={})
    mgr.ib_conn.fetch_quotes = AsyncMock(return_value={CON: {
        "price": 3.5, "bid": 3.4, "ask": 3.6, "mark": 3.5}})
    mgr.ib_conn.ib = MagicMock()

    async def place_once(**kwargs):
        inf = InFlightClose(
            CON, 88, kwargs["quantity"], kwargs["entry_debit"],
            exit_context=kwargs["exit_context"], client_id=42,
            order_ref="manual-once")
        mgr.state_manager.state.add_in_flight(inf)
        mgr.state_manager.save()
        return OrderResult(
            success=True, order_id=88, con_id=CON,
            trade=_trade(88, status, avg_fill=3.4, filled=filled,
                         remaining=4 - filled, client_id=42,
                         order_ref="manual-once", con_id=CON))

    place = AsyncMock(side_effect=place_once)
    mgr.order_manager.place_close_order = place
    await mgr.run_cycle(dry_run=False)
    assert place.await_count == 1
    mgr.ib_conn.fetch_quotes.assert_not_awaited()


@pytest.mark.asyncio
async def test_entry_kill_switch_and_all_caps_do_not_block_protective_stop(tmp_path):
    mgr, cfg = _manager(tmp_path)
    (tmp_path / "KILL").write_text("STOP ENTRIES")
    cfg.caps.max_orders_per_cycle = 0
    cfg.caps.max_orders_per_day = 0
    cfg.caps.max_notional_per_day = 0.0
    mgr.state_manager.state.daily_stats[_trading_day()] = DailyStats(
        date=_trading_day(), orders_placed=99, notional_closed=999999.0)
    positions = {CON: PositionData(
        con_id=CON, symbol="AAPL", right="C", quantity=4,
        avg_cost=5.0, expiry="20261231")}
    mgr.ib_conn.get_positions = AsyncMock(return_value=positions)
    mgr.ib_conn.get_open_orders = AsyncMock(return_value={})
    mgr.ib_conn.fetch_quotes = AsyncMock(return_value={CON: {
        "price": 3.5, "bid": 3.4, "ask": 3.6, "mark": 3.5}})
    mgr.ib_conn.ib = MagicMock()
    mgr.ib_conn.ib.portfolio.return_value = []
    place = AsyncMock(return_value=OrderResult(
        success=True, order_id=55, con_id=CON, trade=None))
    mgr.order_manager.place_close_order = place

    await mgr.run_cycle(dry_run=False)
    place.assert_called_once()
    assert place.call_args.kwargs["trigger_type"] == "stop"
