"""Public API contract; production-derived narrative omitted."""
import asyncio
import json

import pytest
from unittest.mock import AsyncMock, MagicMock

from exitmgr import trader
from exitmgr.config import (Config, RulesConfig, TrailingConfig, ScaleOutConfig,
                            AutoTrailConfig)
from exitmgr.connection import IBConnection, PositionData
from exitmgr.manager import ExitManager
from exitmgr import manual_exit_frontend
from exitmgr.manual_exit_queue import binding_sha
from exitmgr.order import OrderResult


CON = 771001
REF = "alfred-entry:0f8f0d1f4e2b4d0f9c1a6b5e4d3c2b1a"




JOURNAL = {"ts": "2026-08-01T14:00:00+00:00", "contract_id": CON, "symbol": "AAPL",
           "right": "C", "strike": 200.0, "expiry": "20261231", "quantity": 2,
           "debit": 1000.0, "conviction": 7, "order_ref": REF}


def _bound_manual_request(mgr):
    """Public API contract; production-derived narrative omitted."""
    authority = dict(getattr(mgr, "_runtime_identity_fields", None) or {})
    request = {
        "request_id": "gate-test", "parent_con_id": CON, "symbol": "AAPL",
        "quantity": 2,
        "campaign": manual_exit_frontend._campaign_binding(mgr, CON),
        "journal_identity": manual_exit_frontend._journal_identity(mgr._journal_entries[CON]),
        "topology": [{"con_id": CON, "position": 2, "role": "long"}],
        "code_version": authority.get("code_version"),
        "policy_version": authority.get("policy_version"),
        "action": "SELL", "order_type": "MARKET", "sec_type": "OPT",
    }
    request["binding_sha"] = binding_sha(request)
    return request



class _OrderStatus:
    def __init__(self, status, remaining=None, filled=None):
        self.status, self.remaining, self.filled = status, remaining, filled


class _Order:
    def __init__(self, action, ref, total=2):
        self.action, self.orderRef, self.totalQuantity = action, ref, total
        self.orderId, self.permId, self.lmtPrice, self.clientId = 91, 0, 5.0, 7


class _Contract:
    def __init__(self, con_id, sec_type="OPT", combo_legs=()):
        self.conId, self.secType = con_id, sec_type
        self.comboLegs = list(combo_legs)
        self.lastTradeDateOrContractMonth = "20261231"


class _Leg:
    def __init__(self, con_id, action="BUY"):
        self.conId, self.action = con_id, action


class _Trade:
    def __init__(self, contract, order, status):
        self.contract, self.order, self.orderStatus = contract, order, status


def _working_entry_buy(con_id=CON, ref=REF, status="PreSubmitted", remaining=1.0, filled=1.0):
    """Public API contract; production-derived narrative omitted."""
    return _Trade(_Contract(con_id), _Order("BUY", ref),
                  _OrderStatus(status, remaining=remaining, filled=filled))


def _working_spread_entry_buy(long_con_id=CON, ref=REF):
    """Public API contract; production-derived narrative omitted."""
    return _Trade(_Contract(0, "BAG", (_Leg(long_con_id, "BUY"), _Leg(999002, "SELL"))),
                  _Order("BUY", ref), _OrderStatus("PreSubmitted", remaining=1.0, filled=1.0))



def _mgr(tmp_path, journal=JOURNAL, *, stop_pct=30.0):
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
        profit_target_pct=300.0, stop_pct=stop_pct, time_stop_days=None,
        trailing=TrailingConfig(enabled=False),
        scale_out=ScaleOutConfig(enabled=False),
        auto_trail=AutoTrailConfig(enabled=False))
    (tmp_path / "trades.log").write_text(json.dumps(journal) + "\n")
    return ExitManager(cfg)


def _wire(mgr, *, entry_orders, quantity=2, quote=None):
    """Public API contract; production-derived narrative omitted."""
    pos = {CON: PositionData(con_id=CON, symbol="AAPL", right="C", quantity=quantity,
                             avg_cost=5.0, expiry="20261231")}
    mgr.ib_conn.get_positions = AsyncMock(return_value=pos)


    mgr.ib_conn.get_open_orders = AsyncMock(return_value={})
    mgr.ib_conn.fetch_quotes = AsyncMock(
        return_value={CON: quote or {"price": 3.50, "bid": 3.40, "ask": 3.60, "mark": 3.50}})
    mgr.ib_conn.ib = MagicMock()
    mgr.ib_conn.ib.portfolio = lambda: []
    if isinstance(entry_orders, Exception):
        mgr.ib_conn.ib.reqAllOpenOrdersAsync = AsyncMock(side_effect=entry_orders)
    else:
        mgr.ib_conn.ib.reqAllOpenOrdersAsync = AsyncMock(return_value=entry_orders)
    mgr._spot_price = AsyncMock(return_value=None)
    place = AsyncMock(return_value=OrderResult(success=True, order_id=555, con_id=CON, trade=None))
    mgr.order_manager.place_close_order = place
    return place






@pytest.mark.asyncio
async def test_the_stop_is_WITHHELD_while_the_opening_BUY_is_still_working(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr = _mgr(tmp_path)
    place = _wire(mgr, entry_orders=[_working_entry_buy()], quantity=1)
    await mgr.run_cycle(dry_run=False)
    place.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_SAME_stop_transmits_once_the_entry_is_terminal_sized_to_the_filled_quantity(
        tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr = _mgr(tmp_path)
    place = _wire(mgr, entry_orders=[], quantity=2)
    await mgr.run_cycle(dry_run=False)
    place.assert_awaited_once()
    kw = place.call_args.kwargs
    assert kw["con_id"] == CON
    assert kw["quantity"] == 2, "the close was not sized to the filled quantity"
    assert kw["trigger_type"] == "stop"


@pytest.mark.asyncio
async def test_a_TERMINAL_entry_order_still_in_the_book_does_not_withhold(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr = _mgr(tmp_path)
    place = _wire(mgr, entry_orders=[
        _Trade(_Contract(CON), _Order("BUY", REF), _OrderStatus("Filled", remaining=0.0, filled=2.0))])
    await mgr.run_cycle(dry_run=False)
    place.assert_awaited_once()


@pytest.mark.asyncio
async def test_an_UNREADABLE_entry_book_withholds_the_stop_it_could_be_about(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr = _mgr(tmp_path)
    place = _wire(mgr, entry_orders=asyncio.TimeoutError())
    await mgr.run_cycle(dry_run=False)
    place.assert_not_awaited()
    assert mgr._entry_order_view.readable is False


@pytest.mark.asyncio
async def test_a_None_response_is_UNKNOWN_and_withholds_too(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr = _mgr(tmp_path)
    place = _wire(mgr, entry_orders=None)
    await mgr.run_cycle(dry_run=False)
    place.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_in_flight_SPREAD_entry_is_seen_through_its_COMBO_LEGS(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr = _mgr(tmp_path)
    place = _wire(mgr, entry_orders=[_working_spread_entry_buy()], quantity=1)
    await mgr.run_cycle(dry_run=False)
    place.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_legacy_row_with_NO_orderRef_is_still_protected_by_the_CONTRACT_fallback(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr = _mgr(tmp_path, journal={k: v for k, v in JOURNAL.items() if k != "order_ref"})
    place = _wire(mgr, entry_orders=[_working_entry_buy(ref="")], quantity=1)
    await mgr.run_cycle(dry_run=False)
    place.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_resting_SELL_on_the_same_contract_never_withholds(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr = _mgr(tmp_path)
    place = _wire(mgr, entry_orders=[
        _Trade(_Contract(CON), _Order("SELL", "alfred-exit:1"),
               _OrderStatus("PreSubmitted", remaining=1.0, filled=0.0))])
    await mgr.run_cycle(dry_run=False)
    place.assert_awaited_once()






@pytest.mark.asyncio
async def test_the_manual_one_tap_MARKET_close_is_gated_too(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr = _mgr(tmp_path)
    place = _wire(mgr, entry_orders=[_working_entry_buy()], quantity=1)
    request = _bound_manual_request(mgr)
    mgr._read_manual_exits = lambda: {CON: request}
    await mgr.run_cycle(dry_run=False)
    place.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_one_tap_close_goes_out_once_the_entry_is_terminal(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr = _mgr(tmp_path, journal=dict(JOURNAL, debit=1000.0))
    place = _wire(mgr, entry_orders=[], quantity=2,
                  quote={"price": 5.00, "bid": 4.90, "ask": 5.10, "mark": 5.00})
    request = _bound_manual_request(mgr)
    mgr._read_manual_exits = lambda: {CON: request}
    await mgr.run_cycle(dry_run=False)
    place.assert_awaited_once()
    assert place.call_args.kwargs["market"] is True
    assert place.call_args.kwargs["quantity"] == 2






@pytest.mark.asyncio
async def test_the_close_oriented_read_DROPS_the_entry_BUY_so_it_could_not_have_been_the_source():
    """Public API contract; production-derived narrative omitted."""
    ib = MagicMock()
    ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[_working_entry_buy()])
    conn = IBConnection(host="127.0.0.1", port=4001, client_id=1)
    conn.ib = ib
    conn._connected = True

    close_view = await conn.get_open_orders_view()
    assert close_view.readable is True, "the read itself succeeded -- this is not a failure case"
    assert close_view.orders == {}, "the long ENTRY is filtered out of the close-oriented view"

    entry_view = await trader.broker_entry_order_view(ib)
    assert entry_view.readable is True
    assert len(entry_view.working) == 1
    assert entry_view.working_for_ref(REF) is not None
    assert entry_view.working_buys_for_con_id(CON)


    row = dict(JOURNAL)
    assert trader.protective_sell_gate(row, entry_view).allowed is False
    assert trader.protective_sell_gate(
        row, trader.EntryOrderView(True, (), frozenset(), 0, None, ())).allowed is True


def test_EntryOrderView_is_not_connection_OrderView_and_has_no_truth_value_shortcut():
    """Public API contract; production-derived narrative omitted."""
    from exitmgr.connection import OrderView
    unknown = trader.EntryOrderView(False, error="timed out")
    assert not isinstance(unknown, OrderView)
    assert bool(unknown) is True, "a falsy check would read UNKNOWN as 'nothing resting'"
    assert unknown.readable is False
