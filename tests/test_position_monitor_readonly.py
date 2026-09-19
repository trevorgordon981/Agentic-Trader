"""Public API contract; production-derived narrative omitted."""
import asyncio
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest
import ib_async as _real_ib_async

@pytest.fixture
def monitor(monkeypatch):
    monkeypatch.setitem(sys.modules, 'ib_async', _real_ib_async)
    path = Path(__file__).resolve().parents[1] / 'position_monitor.py'
    spec = importlib.util.spec_from_file_location('_position_monitor_readonly', path)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    monkeypatch.setattr(m, '_RETRY_DELAY_S', 0)
    monkeypatch.setattr(m.asyncio, 'sleep', AsyncMock())
    return m

def fake_ib():
    p = SimpleNamespace(position=2.3908, contract=SimpleNamespace(conId=6001001))
    return SimpleNamespace(connectAsync=AsyncMock(), disconnect=Mock(),
        accountSummaryAsync=AsyncMock(return_value=[SimpleNamespace(tag='UnrealizedPnL', value='-20.10')]),
        reqPositionsAsync=AsyncMock(return_value=[p]), portfolio=Mock(return_value=[p]),
        isConnected=Mock(return_value=True),
        reqOpenOrdersAsync=AsyncMock(side_effect=AssertionError('orders forbidden')),
        reqCompletedOrdersAsync=AsyncMock(side_effect=AssertionError('history forbidden')),
        reqExecutionsAsync=AsyncMock(side_effect=AssertionError('executions forbidden')),
        placeOrder=Mock(side_effect=AssertionError('orders forbidden')),
        cancelOrder=Mock(side_effect=AssertionError('cancels forbidden')))

@pytest.fixture
def pot(monitor, monkeypatch):
    p = SimpleNamespace(net_liq=4200, available_funds=1200, cash=1400)
    monkeypatch.setattr(monitor, 'get_pot_snapshot', AsyncMock(return_value=p)); return p

def test_strict_readonly_account_positions_only(monitor, pot):
    ib=fake_ib(); result=asyncio.run(monitor.collect_async(lambda:ib))
    assert ib.connectAsync.await_args.kwargs == dict(host='127.0.0.1',port=4001,clientId=96,timeout=10,
        readonly=True,fetchFields=_real_ib_async.StartupFetch.ACCOUNT_UPDATES |
        _real_ib_async.StartupFetch.SUB_ACCOUNT_UPDATES,raiseSyncErrors=True)
    assert result[0] is pot and result[1]=='-20.10' and result[3][0].position==2.3908
    assert ib.RaiseRequestErrors is True
    for n in ['reqOpenOrdersAsync','reqCompletedOrdersAsync','reqExecutionsAsync','placeOrder','cancelOrder']:
        getattr(ib,n).assert_not_called()
    ib.disconnect.assert_called_once()

def test_real_installed_bootstrap_does_not_request_hanging_completed_orders(monitor,pot,monkeypatch):
    async def scenario():
        ib=_real_ib_async.IB()
        for n,value in [('getAccounts',['DU_TEST']),('serverVersion',178),('isReady',True)]:
            monkeypatch.setattr(ib.client,n,Mock(return_value=value))
        monkeypatch.setattr(ib.client,'connectAsync',AsyncMock())
        monkeypatch.setattr(ib,'isConnected',Mock(return_value=True));monkeypatch.setattr(ib,'disconnect',Mock())
        for n in ['reqOpenOrdersAsync','reqCompletedOrdersAsync','reqExecutionsAsync']:
            monkeypatch.setattr(ib,n,AsyncMock(side_effect=AssertionError('unneeded request')))
        for n in ['reqPositionsAsync','reqAccountUpdatesAsync','reqAccountUpdatesMultiAsync','accountSummaryAsync']:
            monkeypatch.setattr(ib,n,AsyncMock(return_value=[]))
        monkeypatch.setattr(ib,'portfolio',Mock(return_value=[]))
        await monitor.collect_async(lambda:ib)
        assert ib.reqPositionsAsync.await_count==2
        ib.reqAccountUpdatesAsync.assert_awaited_once_with('DU_TEST')
        ib.reqAccountUpdatesMultiAsync.assert_awaited_once_with('DU_TEST')
        for n in ['reqOpenOrdersAsync','reqCompletedOrdersAsync','reqExecutionsAsync']:getattr(ib,n).assert_not_called()
    asyncio.run(scenario())

@pytest.mark.parametrize('failure',[ConnectionError('positions sync'),TimeoutError('account sync')])
def test_required_sync_failure_never_reports_flat(monitor,pot,failure):
    ib=fake_ib();ib.connectAsync.side_effect=failure
    with pytest.raises(type(failure)):asyncio.run(monitor.collect_async(lambda:ib))
    assert ib.connectAsync.await_count==2;ib.accountSummaryAsync.assert_not_called()
    assert ib.disconnect.call_count>=2

def test_startup_retries_once_without_weakening_flags(monitor,pot):
    ib=fake_ib();ib.connectAsync.side_effect=[ConnectionError('transient'),None]
    asyncio.run(monitor.collect_async(lambda:ib));assert ib.connectAsync.await_count==2
    assert all(c.kwargs['readonly'] is True and c.kwargs['raiseSyncErrors'] is True for c in ib.connectAsync.await_args_list)

@pytest.mark.parametrize('method',['get_pot_snapshot','accountSummaryAsync','reqPositionsAsync'])
def test_required_read_deadline(monitor,monkeypatch,pot,method):
    async def hangs(*a,**k):await asyncio.Event().wait()
    ib=fake_ib();monkeypatch.setattr(monitor,'_READ_TIMEOUT_S',.01)
    monkeypatch.setattr(monitor if method=='get_pot_snapshot' else ib,method,hangs)
    with pytest.raises(TimeoutError):asyncio.run(monitor.collect_async(lambda:ib))
    ib.disconnect.assert_called_once()

def test_connect_backstop(monitor,monkeypatch,pot):
    async def hangs(**kw):await asyncio.Event().wait()
    ib=fake_ib();ib.connectAsync=hangs;monkeypatch.setattr(monitor,'_CONNECT_TIMEOUT_S',.01)
    with pytest.raises(TimeoutError):asyncio.run(monitor.collect_async(lambda:ib))
    assert ib.disconnect.call_count==3

def test_disconnect_during_required_read(monitor,pot):
    ib=fake_ib();ib.isConnected.return_value=False
    with pytest.raises(RuntimeError,match='disconnected'):asyncio.run(monitor.collect_async(lambda:ib))
    ib.disconnect.assert_called_once()

def test_heartbeat_no_backoff_ack_logged(monitor,monkeypatch,capsys):
    post=Mock(return_value=True);monkeypatch.setattr(monitor.alerting,'post',post)
    assert monitor.post_position_report('unchanged report') is True
    post.assert_called_once_with('unchanged report',monitor.POSITIONS_CH,label='position_monitor',dedup=False)
    assert 'POSITION_UPDATE_DELIVERED' in capsys.readouterr().out

@pytest.mark.parametrize('result',[False,None,'true',1])
def test_failed_ack_no_retry_no_success_log(monitor,monkeypatch,capsys,result):
    post=Mock(return_value=result);monkeypatch.setattr(monitor.alerting,'post',post)
    assert monitor.post_position_report('report') is False
    post.assert_called_once();out=capsys.readouterr()
    assert 'POSITION_UPDATE_DELIVERED' not in out.out and 'not acknowledged' in out.err

@pytest.mark.parametrize('heartbeat,alert,alerts,status',[(True,True,[],0),(False,True,[],1),(True,False,['near stop'],1),(False,True,['near stop'],1)])
def test_exit_status_and_escalation_when_heartbeat_fails(monitor,monkeypatch,heartbeat,alert,alerts,status):
    monkeypatch.setattr(monitor,'build_report',Mock(return_value=(4200,1200,'-20',['position'],alerts)))
    hp=Mock(return_value=heartbeat);ap=Mock(return_value=alert)
    monkeypatch.setattr(monitor,'post_position_report',hp);monkeypatch.setattr(monitor,'slack',ap)
    assert monitor.main()==status;hp.assert_called_once();assert ap.call_count==bool(alerts)

def test_collection_error_never_posts_flat_heartbeat(monitor,monkeypatch):
    monkeypatch.setattr(monitor,'build_report',Mock(side_effect=TimeoutError('positions unavailable')))
    hp=Mock();ap=Mock(return_value=True)
    monkeypatch.setattr(monitor,'post_position_report',hp);monkeypatch.setattr(monitor,'slack',ap)
    assert monitor.main()==1;hp.assert_not_called();ap.assert_called_once()
