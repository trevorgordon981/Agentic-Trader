"""Public API contract; production-derived narrative omitted."""
import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock
import pytest
import ib_async as actual_ib_async
import ib_async.ib as actual_ib_module
from ib_async import StartupFetch
from ib_async.ib import StartupFetchALL
import morning_review as review
from exitmgr import connection, ibkr

@pytest.mark.parametrize('other_startup_error',[False,True])
def test_review_avoids_completed_history_but_preserves_other_sync_failures(monkeypatch,tmp_path,other_startup_error):
    monkeypatch.setitem(sys.modules,'ib_async',actual_ib_async)
    monkeypatch.setitem(sys.modules,'ib_async.ib',actual_ib_module)
    monkeypatch.setattr(ibkr,'BACKEND','ib_async')
    fake=Mock()
    async def connect(**kwargs):
        if kwargs.get('fetchFields',StartupFetchALL) & StartupFetch.ORDERS_COMPLETE:
            raise TimeoutError('completed orders request timed out')
        if other_startup_error: raise RuntimeError('current positions unavailable')
    fake.connectAsync=AsyncMock(side_effect=connect)
    monkeypatch.setattr(connection,'IB',lambda:fake)
    monkeypatch.setattr(review,'IBConnection',connection.IBConnection)
    monkeypatch.setattr(review,'slack_token',lambda:'')
    monkeypatch.setattr(review,'load_journal',lambda path:[])
    monkeypatch.setattr(review,'load_theses',lambda path:{})
    positions=AsyncMock(return_value=(SimpleNamespace(),[]))
    monkeypatch.setattr(review,'collect_positions',positions)
    def forbidden(*args,**kwargs): raise AssertionError('side effect is forbidden')
    monkeypatch.setattr(review,'slack_post',forbidden)
    monkeypatch.setattr(review,'judge_thesis',forbidden)
    monkeypatch.setattr(review,'run_close',forbidden)
    cfg=tmp_path/'config.yaml';cfg.write_text('ib: {host: 127.0.0.1, port: 4001}\ntrading: {}\n')
    args=SimpleNamespace(config=str(cfg),dry_run=True,client_id=197,watch_minutes=0)
    assert asyncio.run(review.run(args)) == (1 if other_startup_error else 0)
    kwargs=fake.connectAsync.await_args.kwargs
    assert kwargs['fetchFields']==StartupFetchALL & ~StartupFetch.ORDERS_COMPLETE
    assert kwargs['raiseSyncErrors'] is True
    if other_startup_error:positions.assert_not_awaited()
    else:positions.assert_awaited_once()
    fake.placeOrder.assert_not_called();fake.cancelOrder.assert_not_called()
