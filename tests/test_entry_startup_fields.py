"""Public API contract; production-derived narrative omitted."""
import asyncio
import sys
import ib_async as real_ib_async
import ib_async.ib as real_ib_module
from unittest.mock import AsyncMock, Mock
import pytest
from ib_async import StartupFetch
from ib_async.ib import StartupFetchALL
from exitmgr import connection, ibkr

@pytest.fixture(autouse=True)
def actual_library(monkeypatch):
    monkeypatch.setitem(sys.modules, "ib_async", real_ib_async)
    monkeypatch.setitem(sys.modules, "ib_async.ib", real_ib_module)

@pytest.mark.parametrize('value',[None,0,1,'false',[],{}])
def test_selector_rejects_nonboolean(value):
    with pytest.raises(ValueError):connection.IBConnection('127.0.0.1',4001,289,startup_completed_orders=value)

@pytest.mark.parametrize('completed',[True,False])
def test_initial_and_reconnect_keep_exact_selection(monkeypatch,completed):
    objects=[]
    def factory():
        ib=Mock();ib.connectAsync=AsyncMock();objects.append(ib);return ib
    monkeypatch.setattr(connection,'IB',factory);monkeypatch.setattr(ibkr,'BACKEND','ib_async')
    c=connection.IBConnection('127.0.0.1',4001,289,startup_completed_orders=completed)
    monkeypatch.setattr(c,'_next_rotation_id',lambda:290);monkeypatch.setattr(c,'_record_rotation',lambda:1)
    async def run():
        assert await c.connect()
        assert await c.reconnect(retries=0)
    asyncio.run(run());assert len(objects)==2
    for ib in objects:
        kw=ib.connectAsync.await_args.kwargs
        assert kw['raiseSyncErrors'] is True and kw['timeout']==10 and 'readonly' not in kw
        if completed:assert 'fetchFields' not in kw
        else:assert kw['fetchFields']==StartupFetchALL & ~StartupFetch.ORDERS_COMPLETE
        ib.placeOrder.assert_not_called();ib.cancelOrder.assert_not_called()

def test_default_retains_all_startup_reads():
    assert connection.IBConnection('127.0.0.1',4001,189).startup_completed_orders is True

def test_unknown_backend_refuses_without_connect(monkeypatch):
    ib=Mock();ib.connectAsync=AsyncMock();monkeypatch.setattr(connection,'IB',lambda:ib)
    monkeypatch.setattr(ibkr,'BACKEND','ib_insync')
    c=connection.IBConnection('127.0.0.1',4001,289,startup_completed_orders=False)
    assert asyncio.run(c.connect()) is False
    ib.connectAsync.assert_not_called()

def test_entry_mode_is_only_caller_optout():
    import ast
    from pathlib import Path
    tree=ast.parse((Path(__file__).parents[1]/'run_trader.py').read_text())
    assignments=[n for n in ast.walk(tree) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='_entry_startup' for t in n.targets)]
    expr=ast.Expression(assignments[0].value)
    for mode in ('entry','protective','combined'):
        assert eval(compile(expr,'<selector>','eval'),{'mode':mode})==({'startup_completed_orders':False} if mode=='entry' else {})


def test_daily_explicit_entry_profile():
    import ast
    from pathlib import Path
    tree=ast.parse((Path(__file__).parents[1]/'daily_recommend.py').read_text())
    calls=[n for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='IBConnection']
    assert len(calls)==1
    option=next(k.value for k in calls[0].keywords if k.arg=='startup_completed_orders')
    assert isinstance(option,ast.Constant) and option.value is False
