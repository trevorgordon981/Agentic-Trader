"""Public API contract; production-derived narrative omitted."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as N
from unittest.mock import AsyncMock, MagicMock

import pytest

from exitmgr.connection import OrderData, OrderView, PositionData
from exitmgr.state import InFlightClose
from tests.test_broker_mark_when_quotes_fail import _mgr, _wire, CON, SCID


def setup(tmp_path):
    mgr,cfg=_mgr(tmp_path)
    cfg.rules.protective_reprice_enabled=True
    place,alert=_wire(mgr,quotes={},portfolio=[])
    mgr._capture_external_fills_safe=AsyncMock()
    mgr._alert_unfilled_orders=AsyncMock()
    mgr._poll_in_flight_fills=AsyncMock(return_value=set())
    mgr._read_entry_order_view=AsyncMock(return_value=OrderView.known({}))
    mgr._reconcile_bad_con_ids=set()
    mgr._reconcile_on_startup=AsyncMock(return_value=True)
    mgr._post_unthrottled_alert=MagicMock(return_value=True)
    mgr._fresh_protective_quotes=MagicMock(return_value={})
    mgr.ib_conn._connection_generation=5
    mgr.ib_conn.is_healthy=MagicMock(return_value=True)
    mgr.order_manager.reprice_unfilled_protective_exit=AsyncMock(
        return_value={"outcome":"modified","urgent":False})
    return mgr,cfg,place


def inflight(mgr,cid,short=None):
    oid=cid+100
    inf=InFlightClose(con_id=cid,order_id=oid,remaining_qty=4,entry_debit=2000,
        placed_at=(datetime.now(timezone.utc)-timedelta(seconds=90)).isoformat(),
        perm_id=oid+1000,client_id=189,order_ref=f"exitmgr-{cid}-test",
        exit_context={"trigger_type":"stop","manual_request":False},
        submitted_close={"sec_type":"BAG" if short else "OPT", "action":"SELL",
            "combo_qty":4,"legs":[{"con_id":cid,"ratio":1,"expected_side":"SLD"}]})
    if short:
        inf.submitted_close["legs"].append({"con_id":short,"ratio":1,"expected_side":"BOT"})
    mgr.state_manager.state.add_in_flight(inf)
    row=OrderData(con_id=cid,order_id=oid,remaining=4,limit_price=3,
        perm_id=inf.perm_id,client_id=189,order_ref=inf.order_ref,status="Submitted")
    trade=N(order=N(orderId=oid,clientId=189,permId=inf.perm_id))
    pos=PositionData(con_id=cid,symbol="AAPL",right="C",quantity=4,avg_cost=5,expiry="20261231")
    return row,trade,pos


@pytest.mark.asyncio
async def test_all_inflight_recovery_runs_before_no_positions_early_return(tmp_path):
    mgr,cfg,new_order=setup(tmp_path)
    row,trade,pos=inflight(mgr,CON)
    mgr.ib_conn.ib.openTrades=lambda:[trade]
    mgr._fetch_position_book=AsyncMock(return_value=({CON:pos},{},{}))
    mgr.ib_conn.get_open_orders_view=AsyncMock(return_value=OrderView.known({CON:row}))
    await mgr.run_cycle(dry_run=False)
    recover=mgr.order_manager.reprice_unfilled_protective_exit
    recover.assert_awaited_once()
    assert recover.call_args.args==(CON,)
    assert recover.call_args.kwargs["live_order"]["limit_price"]==3
    assert recover.call_args.kwargs["quote_connection"] is mgr.ib_conn
    assert CON in mgr._protective_quote_con_ids
    new_order.assert_not_called()


@pytest.mark.asyncio
async def test_all_durable_submitted_legs_stay_subscribed_even_without_journal(tmp_path):
    mgr,cfg,new_order=setup(tmp_path)
    other=9900;short=9901
    row,trade,pos=inflight(mgr,other,short)
    mgr.ib_conn.ib.openTrades=lambda:[trade]
    shortpos=PositionData(con_id=short,symbol="AAPL",right="C",quantity=-4,avg_cost=2,expiry="20261231")
    mgr._fetch_position_book=AsyncMock(return_value=({other:pos},{short:shortpos},{}))
    mgr.ib_conn.get_open_orders_view=AsyncMock(return_value=OrderView.known({other:row}))
    await mgr.run_cycle(dry_run=False)
    assert {other,short}<=mgr._protective_quote_con_ids
    mgr.order_manager.reprice_unfilled_protective_exit.assert_awaited_once()
    new_order.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure",["unknown_book","bad_reconcile","unknown_reconcile","dry_run","disabled"])
async def test_unknown_or_blocked_cycle_never_modifies_existing_order(tmp_path,failure):
    mgr,cfg,new_order=setup(tmp_path)
    row,trade,pos=inflight(mgr,CON)
    mgr.ib_conn.ib.openTrades=lambda:[trade]
    mgr._fetch_position_book=AsyncMock(return_value=({CON:pos},{},{}))
    view=OrderView.unknown("timeout") if failure=="unknown_book" else OrderView.known({CON:row})
    mgr.ib_conn.get_open_orders_view=AsyncMock(return_value=view)
    if failure=="bad_reconcile":
        mgr._reconcile_bad_con_ids={CON}
    if failure=="unknown_reconcile":
        mgr._reconcile_bad_con_ids=None
    if failure=="disabled":
        cfg.rules.protective_reprice_enabled=False
    await mgr.run_cycle(dry_run=failure=="dry_run")
    mgr.order_manager.reprice_unfilled_protective_exit.assert_not_called()
    new_order.assert_not_called()


@pytest.mark.asyncio
async def test_urgent_recovery_result_uses_offcycle_alert_queue(tmp_path):
    mgr,cfg,new_order=setup(tmp_path)
    row,trade,pos=inflight(mgr,CON)
    mgr.ib_conn.ib.openTrades=lambda:[trade]
    mgr.order_manager.reprice_unfilled_protective_exit.return_value={
        "outcome":"ambiguous","urgent":True,"reason":"exact broker echo missing"}
    await mgr._recover_unfilled_protective_exits({CON:row},{CON:pos},1,
        datetime.now(timezone.utc).isoformat(),5)
    mgr._post_unthrottled_alert.assert_called_once()
    assert "exact broker echo missing" in mgr._post_unthrottled_alert.call_args.args[0]
    assert mgr._post_unthrottled_alert.call_args.args[1]==f"protective-reprice:{CON}"


@pytest.mark.asyncio
async def test_missing_exact_cached_trade_is_reported_without_modification(tmp_path):
    mgr,cfg,new_order=setup(tmp_path)
    row,trade,pos=inflight(mgr,CON)
    mgr.ib_conn.ib.openTrades=lambda:[]
    await mgr._recover_unfilled_protective_exits({CON:row},{CON:pos},1,
        datetime.now(timezone.utc).isoformat(),5)
    mgr.order_manager.reprice_unfilled_protective_exit.assert_not_called()
    mgr._post_unthrottled_alert.assert_called_once()


@pytest.mark.asyncio
async def test_fresh_position_smaller_than_resting_remainder_blocks_acceleration(tmp_path):
    mgr,cfg,new_order=setup(tmp_path)
    row,trade,pos=inflight(mgr,CON)
    pos.quantity=2
    mgr.ib_conn.ib.openTrades=lambda:[trade]
    await mgr._recover_unfilled_protective_exits({CON:row},{CON:pos},1,
        datetime.now(timezone.utc).isoformat(),5)
    mgr.order_manager.reprice_unfilled_protective_exit.assert_not_called()
    mgr._post_unthrottled_alert.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("quantity",[None,-2,4,float("nan")])
async def test_combo_needs_actual_remaining_short_leg_coverage(tmp_path,quantity):
    mgr,cfg,new_order=setup(tmp_path)
    row,trade,pos=inflight(mgr,CON,SCID)
    mgr.ib_conn.ib.openTrades=lambda:[trade]
    shorts={} if quantity is None else {SCID:N(quantity=quantity)}
    await mgr._recover_unfilled_protective_exits({CON:row},{CON:pos},1,
        datetime.now(timezone.utc).isoformat(),5,live_shorts=shorts)
    mgr.order_manager.reprice_unfilled_protective_exit.assert_not_called()
    mgr._post_unthrottled_alert.assert_called_once()


@pytest.mark.asyncio
async def test_combo_valid_remaining_short_coverage_allows_same_order_recovery(tmp_path):
    mgr,cfg,new_order=setup(tmp_path)
    row,trade,pos=inflight(mgr,CON,SCID)
    mgr.ib_conn.ib.openTrades=lambda:[trade]
    await mgr._recover_unfilled_protective_exits({CON:row},{CON:pos},1,
        datetime.now(timezone.utc).isoformat(),5,live_shorts={SCID:N(quantity=-4)})
    mgr.order_manager.reprice_unfilled_protective_exit.assert_awaited_once()


@pytest.mark.asyncio
async def test_each_submitted_sell_leg_ratio_must_be_covered(tmp_path):
    mgr,cfg,new_order=setup(tmp_path)
    row,trade,pos=inflight(mgr,CON,SCID)
    mgr.state_manager.state.get_in_flight(CON).submitted_close["legs"][0]["ratio"]=2
    mgr.ib_conn.ib.openTrades=lambda:[trade]
    await mgr._recover_unfilled_protective_exits({CON:row},{CON:pos},1,
        datetime.now(timezone.utc).isoformat(),5,live_shorts={SCID:N(quantity=-4)})
    mgr.order_manager.reprice_unfilled_protective_exit.assert_not_called()
    mgr._post_unthrottled_alert.assert_called_once()
