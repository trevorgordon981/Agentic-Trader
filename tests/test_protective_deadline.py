"""Public API contract; production-derived narrative omitted."""
import asyncio
from unittest.mock import AsyncMock, MagicMock
import pytest
from exitmgr.connection import IBConnection
from run_trader import _invalidate_protective_deadline


@pytest.mark.asyncio
async def test_outer_deadline_revokes_wedged_lane_without_cancelling_orders():
    conn=IBConnection('127.0.0.1',4001,189,market_data_type=1)
    conn.ib=MagicMock()
    conn.ib.isConnected.return_value=True
    conn._connected=True
    conn._uplink_ok=True
    conn._link_fault=False
    conn._price_events_accepted=True
    conn._portfolio_observations[1]={'price':5.}
    generation=conn._connection_generation
    entered=asyncio.Event()
    async def wedged_positions():
        entered.set()
        await asyncio.Event().wait()
    conn.ib.reqPositionsAsync=AsyncMock(side_effect=wedged_positions)
    conn.ib.reqCurrentTimeAsync=AsyncMock(return_value=1)
    with pytest.raises(asyncio.TimeoutError):
        async with asyncio.timeout(.01):
            await conn.get_positions()
    assert entered.is_set()


    assert conn._link_fault is False
    _invalidate_protective_deadline(conn)
    assert conn._link_fault is True
    assert conn._connection_generation==generation+1
    assert conn._portfolio_observations=={}
    assert not await conn.ensure_connected()
    conn.ib.reqCurrentTimeAsync.assert_not_called()
    conn.ib.cancelOrder.assert_not_called()
    conn.ib.disconnect.assert_not_called()

    conn._on_error(0,2104,'farm connection OK')
    assert not await conn.ensure_connected()


@pytest.mark.asyncio
async def test_actual_protective_loop_deadline_cancels_ensure_before_alert():
    """Public API contract; production-derived narrative omitted."""
    import ast
    from pathlib import Path
    from types import SimpleNamespace
    tree=ast.parse(Path('run_trader.py').read_text())
    loop_node=next(n for n in ast.walk(tree)
                   if isinstance(n,ast.AsyncFunctionDef) and n.name=='_protective_loop')
    module=ast.Module(body=[loop_node],type_ignores=[])
    events=[]
    async def ensure():
        try:
            await asyncio.Event().wait()
        finally:
            events.append('ensure_cancelled')
    class StopProbe(BaseException):
        pass
    def alert(*args,**kwargs):
        events.append('timeout_alert')
        raise StopProbe()
    conn=SimpleNamespace(_link_fault=False,_invalidate_price_observations=lambda:events.append('invalidate'))
    status={'unresolved_close_count':0,'last_good_broker_view_at':None}
    manager=SimpleNamespace(protective_clock_status=lambda:status,
                            _post_unthrottled_alert=alert,run_cycle=AsyncMock())
    ns=dict(asyncio=asyncio,protective_interval=15,
            cfg=SimpleNamespace(loop=SimpleNamespace(protective_cycle_timeout_seconds=.01,protective_poll_seconds=15)),
            exit_mgr=manager,ib_conn=conn,
            protective_clock=SimpleNamespace(start_cycle=lambda **kw:1,complete_cycle=lambda *a,**kw:None),
            trader=SimpleNamespace(_exit_fail_streak=0,_regime=None,_price_stats=None),
            _ensure_live_connection=ensure,broker_order_lock=asyncio.Lock(),dry_run=False,
            _invalidate_protective_deadline=_invalidate_protective_deadline)
    exec(compile(ast.fix_missing_locations(module),'actual_protective_loop','exec'),ns)
    with pytest.raises(StopProbe):
        await asyncio.wait_for(ns['_protective_loop'](),timeout=1.)
    assert events==['ensure_cancelled','invalidate','timeout_alert']
    assert conn._link_fault is True
    manager.run_cycle.assert_not_called()
