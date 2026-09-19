"""Public API contract; production-derived narrative omitted."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace as N
from unittest.mock import AsyncMock, MagicMock

import pytest

from exitmgr.ibkr import Contract, Order
from exitmgr.order import OrderManager, submitted_close_snapshot
from exitmgr.exit_recovery import KEY
from exitmgr.state import InFlightClose

NOW = datetime(2026, 9, 18, 19, 0, tzinfo=timezone.utc)


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.setenv("EXITMGR_ORDER_LOCK", str(tmp_path / "order.lock"))
    contract = Contract(conId=100, secType="OPT", symbol="SYMS", exchange="SMART", currency="USD")
    order = Order(orderId=55, permId=555, clientId=17, orderRef="exitmgr-100-unique",
        action="SELL", totalQuantity=3, orderType="LMT", lmtPrice=4.0, tif="DAY")
    trade = N(order=order, contract=contract,
        orderStatus=N(status="Submitted", filled=0, remaining=3))
    inf = InFlightClose(con_id=100, order_id=55, perm_id=555, client_id=17,
        order_ref=order.orderRef, identity_version=1, placement_state="submitted",
        remaining_qty=3, entry_debit=1500, order_price=4,
        placed_at=(NOW-timedelta(seconds=31)).isoformat(),
        exit_context={"trigger_type":"stop", "manual_request":False},
        submitted_close=submitted_close_snapshot(contract, order, parent_con_id=100, combo_qty=3))
    path = tmp_path / "state.json"
    state = N(get_in_flight=lambda cid: inf if cid == 100 else None)
    def save():
        path.write_text(json.dumps(inf.exit_context))
    sm = N(state=state, save=MagicMock(side_effect=save))
    ib = N(placeOrder=MagicMock(return_value=trade))
    conn = N(client_id=17, _connection_generation=4, ib=ib,
        is_healthy=lambda: True, ensure_connected=AsyncMock(return_value=True))
    manager = OrderManager(conn, sm)
    live = dict(con_id=100, order_id=55, perm_id=555, client_id=17,
        order_ref=order.orderRef, order_count=1, order_ids=(55,),
        status="Submitted", remaining=3, limit_price=4.0)
    quotes = {100:dict(bid=3.0, ask=3.2, observed_monotonic=995., generation=8)}
    kw = dict(live_order=live, trade=trade, quotes=quotes,
        book_observed_monotonic=998., book_observed_at=(NOW-timedelta(seconds=2)).isoformat(),
        book_generation=4, quote_generation=8,
        quote_healthy=True, now=NOW, monotonic_now=1000.)
    return N(manager=manager, conn=conn, sm=sm, inf=inf, trade=trade,
        live=live, quotes=quotes, kw=kw, path=path)


async def invoke(c):
    return await c.manager.reprice_unfilled_protective_exit(100, **c.kw)


@pytest.mark.asyncio
async def test_same_identity_and_durable_intent_before_send(case):
    c=case
    def send(contract, order):
        saved=json.loads(c.path.read_text())[KEY]
        assert saved["phase"]=="intent"
        assert saved["target_price"]==3
        assert contract is c.trade.contract
        assert (order.orderId,order.permId,order.clientId,order.orderRef)==(55,555,17,c.inf.order_ref)
        assert order.totalQuantity==3
        assert order.lmtPrice==3
    c.conn.ib.placeOrder.side_effect=send
    r=await invoke(c)
    assert r["outcome"]=="modified"
    assert json.loads(c.path.read_text())[KEY]["phase"]=="awaiting_echo"
    assert c.trade.order.lmtPrice==4
    assert c.inf.order_id==55 and c.inf.remaining_qty==3


@pytest.mark.asyncio
async def test_partial_fill_preserves_original_total_not_remaining(case):
    c=case
    c.trade.orderStatus.filled=1;c.trade.orderStatus.remaining=2
    c.live["remaining"]=2;c.inf.remaining_qty=2
    r=await invoke(c)
    assert r["outcome"]=="modified" and r["remaining"]==2
    sent=c.conn.ib.placeOrder.call_args.args[1]
    assert sent.totalQuantity==3


@pytest.mark.asyncio
async def test_combo_uses_exact_both_legs_and_never_replaces_contract(case):
    c=case
    c.trade.contract.secType="BAG"
    c.trade.contract.comboLegs=[N(conId=100,ratio=1,action="BUY"),N(conId=200,ratio=1,action="SELL")]
    c.inf.submitted_close=submitted_close_snapshot(c.trade.contract,c.trade.order,parent_con_id=100,combo_qty=3)
    c.quotes[100].update(bid=9,ask=9.2)
    c.quotes[200]=dict(bid=5.8,ask=6,observed_monotonic=995,generation=8)
    r=await invoke(c)
    assert r["outcome"]=="modified" and r["limit_price"]==3
    assert c.conn.ib.placeOrder.call_args.args[0] is c.trade.contract


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [
    lambda c: setattr(c.conn,"client_id",18),
    lambda c: setattr(c.trade.order,"permId",999),
    lambda c: c.live.update(order_count=2,order_ids=(55,56)),
    lambda c: setattr(c.trade.orderStatus,"status","Filled"),
    lambda c: c.live.update(status="PendingCancel"),
    lambda c: c.live.update(remaining=2),
    lambda c: setattr(c.trade.order,"totalQuantity",4),
    lambda c: setattr(c.trade.contract,"conId",101),
    lambda c: setattr(c.trade.order,"ocaGroup","protection"),
    lambda c: c.kw.update(book_generation=3),
    lambda c: c.kw.update(book_observed_monotonic=970),
    lambda c: c.quotes[100].update(generation=7),
    lambda c: c.quotes[100].update(observed_monotonic=970),
    lambda c: c.quotes[100].update(bid=0),
    lambda c: c.quotes[100].update(bid=4,ask=3),
    lambda c: c.quotes[100].update(ask=float("nan")),
    lambda c: c.quotes.clear(),
    lambda c: c.kw.update(quote_healthy=False),
    lambda c: setattr(c.inf,"placement_state","transmission_ambiguous"),
])
async def test_unknown_or_conflicting_evidence_never_modifies(case,mutation):
    mutation(case)
    r=await invoke(case)
    assert r["outcome"]=="blocked" and r["urgent"]
    case.conn.ib.placeOrder.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger,manual",[("take_profit",False),("stop",True),("manual",False),("trailing_stop",None)])
async def test_manual_and_nonprotective_not_repriced(case,trigger,manual):
    case.inf.exit_context.update(trigger_type=trigger,manual_request=manual)
    assert (await invoke(case))["outcome"]=="not_eligible"
    case.conn.ib.placeOrder.assert_not_called()


@pytest.mark.asyncio
async def test_failed_durable_intent_sends_nothing(case):
    case.sm.save.side_effect=OSError("disk full")
    assert (await invoke(case))["outcome"]=="blocked"
    case.conn.ib.placeOrder.assert_not_called()
    assert KEY not in case.inf.exit_context


@pytest.mark.asyncio
async def test_fill_during_probe_cannot_be_reissued(case):
    async def probe(**kw):
        case.trade.orderStatus.status="Filled"
        return True
    case.conn.ensure_connected.side_effect=probe
    assert (await invoke(case))["outcome"]=="blocked"
    case.conn.ib.placeOrder.assert_not_called()


@pytest.mark.asyncio
async def test_ambiguous_send_survives_restart_no_blind_retry_then_exact_echo(case):
    c=case;c.conn.ib.placeOrder.side_effect=RuntimeError("socket reset")
    assert (await invoke(c))["outcome"]=="ambiguous"
    assert c.conn.ib.placeOrder.call_count==1
    c.inf.exit_context=json.loads(c.path.read_text())
    c.kw["now"]=NOW+timedelta(seconds=35)
    c.kw["book_observed_at"]=(NOW+timedelta(seconds=33)).isoformat()
    assert (await invoke(c))["outcome"]=="ambiguous"
    assert c.conn.ib.placeOrder.call_count==1

    c.live["limit_price"]=3;c.trade.order.lmtPrice=3;c.quotes[100]["bid"]=2.9
    c.conn.ib.placeOrder.side_effect=None
    assert (await invoke(c))["outcome"]=="modified"
    assert c.conn.ib.placeOrder.call_count==2
    assert c.inf.exit_context[KEY]["attempts"]==2


@pytest.mark.asyncio
async def test_three_attempts_backoff_then_continue_without_new_identity(case):
    c=case
    c.inf.exit_context[KEY]=dict(phase="confirmed",attempts=3,attempted_at=(NOW-timedelta(seconds=40)).isoformat())
    assert (await invoke(c))["outcome"]=="waiting"
    c.conn.ib.placeOrder.assert_not_called()
    c.kw["now"]=NOW+timedelta(seconds=21)
    c.kw["book_observed_at"]=(NOW+timedelta(seconds=20)).isoformat()
    r=await invoke(c)
    assert r["outcome"]=="modified" and r["urgent"] and r["attempts"]==4


@pytest.mark.asyncio
async def test_initial_thirty_second_grace_and_already_marketable_escalation(case):
    case.inf.placed_at=(NOW-timedelta(seconds=15)).isoformat()
    assert (await invoke(case))["outcome"]=="waiting"
    case.inf.placed_at=(NOW-timedelta(seconds=40)).isoformat()
    case.quotes[100].update(bid=4.1,ask=4.2)
    r=await invoke(case)
    assert r["outcome"]=="waiting" and r["urgent"]
    case.conn.ib.placeOrder.assert_not_called()


@pytest.mark.asyncio
async def test_local_price_change_and_pre_attempt_book_are_not_broker_ack(case):
    c=case
    assert (await invoke(c))["outcome"]=="modified"
    c.trade.order.lmtPrice=3
    c.kw["now"]=NOW+timedelta(seconds=5)

    assert (await invoke(c))["outcome"]=="blocked"
    c.live["limit_price"]=3

    assert (await invoke(c))["outcome"]=="ambiguous"
    assert c.conn.ib.placeOrder.call_count==1


@pytest.mark.asyncio
async def test_quote_connection_disconnect_during_probe_blocks_send(case):
    c=case
    qconn=N(_connection_generation=8,is_healthy=MagicMock(return_value=True))
    c.kw["quote_connection"]=qconn
    async def probe(**kw):
        qconn.is_healthy.return_value=False
        return True
    c.conn.ensure_connected.side_effect=probe
    assert (await invoke(c))["outcome"]=="blocked"
    c.conn.ib.placeOrder.assert_not_called()


@pytest.mark.asyncio
async def test_partial_fill_arriving_during_probe_requires_new_book(case):
    c=case
    async def probe(**kw):
        c.trade.orderStatus.filled=1;c.trade.orderStatus.remaining=2
        return True
    c.conn.ensure_connected.side_effect=probe
    assert (await invoke(c))["outcome"]=="blocked"
    c.conn.ib.placeOrder.assert_not_called()


@pytest.mark.asyncio
async def test_short_protective_buy_close_reprices_up_to_fresh_ask(case):
    c=case
    c.trade.order.action="BUY"
    c.inf.submitted_close=submitted_close_snapshot(c.trade.contract,c.trade.order,parent_con_id=100,combo_qty=3)
    c.quotes[100].update(bid=4.1,ask=4.3)
    r=await invoke(c)
    assert r["outcome"]=="modified" and r["limit_price"]==4.3
    assert c.conn.ib.placeOrder.call_args.args[1].action=="BUY"


@pytest.mark.asyncio
async def test_recovery_does_not_reuse_old_mark_slippage_floor(case):
    c=case
    c.quotes[100].update(bid=1.25,ask=1.3)
    r=await invoke(c)
    assert r["outcome"]=="modified" and r["limit_price"]==1.25
    assert c.inf.submitted_close["limit_price"]==4
