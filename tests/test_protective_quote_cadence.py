"""Public API contract; production-derived narrative omitted."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize('closed_first',[False,True])
async def test_snapshot_duration_consumes_cadence_and_keeps_quotes_fresh(closed_first):
    source=Path.cwd()/'run_trader.py'
    tree=ast.parse(source.read_text())
    node=next(n for n in ast.walk(tree) if isinstance(n,ast.AsyncFunctionDef)
              and n.name=='_quote_connection_loop')
    now=[0.]; sleeps=[]; starts=[]; published=[]; plans=[0]
    def plan():
        plans[0]+=1
        return (False,300.) if closed_first and plans[0]==1 else (True,10.)
    async def sleep(seconds):
        sleeps.append(seconds);now[0]+=seconds
    async def ensure(conn):
        return True
    async def refresh():
        starts.append(now[0])
        if len(starts)==4:
            raise asyncio.CancelledError()

        stamp=now[0];now[0]+=12.;published.append((now[0],stamp))
    fake_asyncio=SimpleNamespace(get_running_loop=lambda:SimpleNamespace(time=lambda:now[0]),
                                 sleep=sleep,CancelledError=asyncio.CancelledError)
    ns=dict(asyncio=fake_asyncio,quote_ib_conn=SimpleNamespace(is_healthy=lambda:True),
            _protective_quote_lane_plan=plan,_ensure_quote_connection=ensure,
            exit_mgr=SimpleNamespace(refresh_protective_quotes_offcycle=refresh))
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node],type_ignores=[])),
                 'actual_quote_worker','exec'),ns)
    with pytest.raises(asyncio.CancelledError):
        await ns['_quote_connection_loop']()
    if closed_first:
        assert sleeps[0]==300. and starts[0]==300.
        sleeps=sleeps[1:]
    assert sleeps==pytest.approx([.1,.1,.1])
    assert [b-a for a,b in zip(starts,starts[1:])]==pytest.approx([12.1]*3)

    maximum_ages=[published[i+1][0]-published[i][1] for i in range(2)]
    assert maximum_ages==pytest.approx([24.1,24.1])
    assert max(maximum_ages)<30.
