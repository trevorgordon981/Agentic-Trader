"""Public API contract; production-derived narrative omitted."""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest

from exitmgr.config import Config
from exitmgr.connection import IBConnection
from exitmgr.ibkr import Contract
from exitmgr.manager import ExitManager
from exitmgr.order import OrderManager, trade_has_proven_zero_fill, trade_reported_filled
from exitmgr.state import StateManager, InFlightClose


CID = 5001001
SHORT = 5001005


def _trade(filled=0, fills=(), status="Cancelled"):
    return NS(order=NS(orderId=77, clientId=42, permId=70077, orderRef="exitmgr-proof"),
              orderStatus=NS(status=status, filled=filled, remaining=1, avgFillPrice=0),
              fills=list(fills), log=[])


def _connection(trade, *, short_qty=-1, right="C"):
    conn = IBConnection.__new__(IBConnection)
    long = Contract(conId=CID, symbol="SYMD", secType="OPT", currency="USD", right=right)
    short = Contract(conId=SHORT, symbol="SYMD", secType="OPT", currency="USD", right=right)
    rows = [NS(contract=long, position=1), NS(contract=short, position=short_qty)]
    conn.ib = NS(positions=lambda: rows, portfolio=lambda: rows)
    conn._qualified_quote_contracts = {}
    conn.client_id = 42
    conn.reserve_order_id = lambda: 77

    async def place(contract, order):
        trade.contract = contract
        trade.order = order
        order.clientId, order.permId = 42, 70077
        return trade

    conn.place_order = AsyncMock(side_effect=place)
    return conn


def _place(tmp_path, trade, monkeypatch, *, spread=True, short_qty=-1, right="C"):
    monkeypatch.setattr("exitmgr.order._alert_ambiguous_close", lambda *_args: None)
    conn = _connection(trade, short_qty=short_qty, right=right)
    sm = StateManager(str(tmp_path / "state.json"))
    om = OrderManager(conn, sm)
    result = asyncio.run(om.place_close_order(
        CID, "SYMD", 1, 2.84, 228, {},
        spread={"short_con_id": SHORT} if spread else None,
        right=right, market=True, trigger_type="trailing_stop",
        exit_context={"close_qty": 1, "position_qty": 1, "symbol": "SYMD"}))
    return om, conn, sm, result


@pytest.mark.parametrize("filled", [None, float("nan"), float("inf"), -1, "0", False])
def test_unknown_filled_count_is_never_zero_fill_evidence(filled):
    trade = _trade(filled=filled)
    assert trade_reported_filled(trade) is None
    assert not trade_has_proven_zero_fill(trade)
    assert not ExitManager._terminal_trade_usable(trade)


@pytest.mark.parametrize("filled", [None, float("nan"), float("inf"), -1, "0", False])
def test_terminal_unknown_count_preserves_durable_close(tmp_path, monkeypatch, filled):
    om, conn, sm, result = _place(tmp_path, _trade(filled), monkeypatch)
    assert not result.success and result.ambiguous and result.acknowledged
    assert sm.state.get_in_flight(CID).placement_state == "transmission_ambiguous"
    assert StateManager(sm.state_path).state.get_in_flight(CID) is not None
    assert not asyncio.run(om.can_place_close(CID, 1, {}))[0]
    assert conn.place_order.await_count == 1


def test_terminal_zero_aggregate_with_one_leg_execution_keeps_whole_spread_latch(tmp_path, monkeypatch):
    fill = NS(contract=NS(conId=CID), execution=NS(shares=1, price=19.9, side="SLD"))
    om, conn, sm, result = _place(tmp_path, _trade(0, [fill]), monkeypatch)
    assert not result.success and result.ambiguous
    frozen = sm.state.get_in_flight(CID).submitted_close
    assert frozen["sec_type"] == "BAG"
    assert [(r["con_id"], r["expected_side"]) for r in frozen["legs"]] == [
        (CID, "SLD"), (SHORT, "BOT")]
    assert not asyncio.run(om.can_place_close(CID, 1, {}))[0]
    assert not ExitManager._terminal_trade_usable(_trade(0, [fill]))


@pytest.mark.parametrize("status", ["Cancelled", "ApiCancelled", "Inactive"])
def test_explicit_terminal_zero_empty_fills_releases_only_this_close(tmp_path, monkeypatch, status):
    _, conn, sm, result = _place(tmp_path, _trade(0, status=status), monkeypatch)
    assert not result.success and result.acknowledged and not result.ambiguous
    assert sm.state.get_in_flight(CID) is None
    assert conn.place_order.await_count == 1


def test_terminal_partial_fill_retains_identity_for_finalization(tmp_path, monkeypatch):
    trade = _trade(1, status="Cancelled")
    trade.orderStatus.avgFillPrice = 2.84
    _, _, sm, result = _place(tmp_path, trade, monkeypatch)
    assert result.success and result.acknowledged
    assert sm.state.get_in_flight(CID).submitted_close["sec_type"] == "BAG"


def test_missing_exchange_validation_error_is_not_a_terminal_receipt(tmp_path, monkeypatch):
    trade = _trade(0, status="PendingSubmit")
    trade.log = [NS(errorCode=321, message="Missing order exchange")]
    monkeypatch.setattr("exitmgr.order.asyncio.sleep", AsyncMock())
    om, conn, sm, result = _place(tmp_path, trade, monkeypatch)
    assert not result.success and result.ambiguous and not result.acknowledged
    assert sm.state.get_in_flight(CID).placement_state == "transmission_ambiguous"
    assert conn.place_order.await_count == 1
    assert not asyncio.run(om.can_place_close(CID, 1, {}))[0]


@pytest.mark.parametrize("spread,short_qty,expected_type", [(False, -1, "OPT"), (True, 0, "OPT"), (True, -1, "BAG")])
@pytest.mark.parametrize("right", ["C", "P"])
def test_native_close_contracts_have_explicit_routes_and_correct_topology(tmp_path, monkeypatch,
                                                                       spread, short_qty, expected_type, right):
    _, conn, sm, result = _place(tmp_path, _trade(0, status="Submitted"), monkeypatch,
                                spread=spread, short_qty=short_qty, right=right)
    assert result.success
    contract, order = conn.place_order.await_args.args
    assert contract.exchange == "SMART" and contract.currency == "USD"
    assert contract.secType == expected_type and order.action == "SELL"
    assert sm.state.get_in_flight(CID).submitted_close["sec_type"] == expected_type
    if expected_type == "OPT":
        assert contract.right == right
    if expected_type == "BAG":
        assert [(leg.conId, leg.action, leg.exchange) for leg in contract.comboLegs] == [
            (CID, "BUY", "SMART"), (SHORT, "SELL", "SMART")]


@pytest.mark.parametrize("filled,fills", [(None, []), (float("nan"), []), (0, [NS(execution=NS(shares=1))])])
def test_reconciliation_never_releases_unknown_or_contradictory_terminal_fill(tmp_path, filled, fills):
    cfg = Config()
    cfg.journal.path = str(tmp_path / "trades.log")
    cfg.state.path = str(tmp_path / "state.json")
    cfg.audit_path = str(tmp_path / "audit.jsonl")
    cfg.alerts_channel = cfg.error_channel = ""
    (tmp_path / "trades.log").write_text("")
    mgr = ExitManager(cfg)
    inf = InFlightClose(CID, 77, 1, 228,
                        exit_context={"symbol": "SYMD", "close_qty": 1, "position_qty": 1},
                        client_id=42, order_ref="exitmgr-proof", identity_version=1)
    mgr.state_manager.state.add_in_flight(inf)
    trade = _trade(filled, fills)


    mgr._cached_terminal_trades_for_in_flight = lambda _: ({CID: trade}, {CID})
    mgr._finalize_in_flight_exit = MagicMock(return_value=False)
    mgr._log_unfilled_exit = MagicMock()
    asyncio.run(mgr._poll_in_flight_fills({}, live_positions={CID: object()}))
    assert mgr.state_manager.state.get_in_flight(CID) is inf
    mgr._log_unfilled_exit.assert_not_called()
