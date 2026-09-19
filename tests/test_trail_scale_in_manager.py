"""Public API contract; production-derived narrative omitted."""
import json
from unittest.mock import MagicMock
import pytest
from exitmgr.manager import ExitManager
from exitmgr.connection import PositionData
from tests.test_trail_qualification import manager, live_quote
from tests.test_broker_mark_when_quotes_fail import _wire, _PortRow, CON, JOURNAL


@pytest.mark.asyncio
async def test_manager_high_cost_scale_in_restart_preserves_and_executes_floor(tmp_path,monkeypatch):
    mgr,cfg=manager(tmp_path,monkeypatch)
    place,_=_wire(mgr,quotes={CON:live_quote(5.9)},portfolio=[_PortRow(CON,5.9)])
    await mgr.run_cycle(dry_run=False)
    place.assert_not_called()
    earned=mgr.state_manager.state.trail_protected_floor(CON)
    assert earned is not None and earned >5.2


    with open(cfg.journal.path,'a') as f:
        f.write(json.dumps(dict(JOURNAL,ts='2026-09-18T19:00:00+00:00',quantity=4,debit=3200.))+'\n')
    restarted=ExitManager(cfg)
    restarted._atr_levels_for=MagicMock(return_value=None)
    assert restarted.state_manager.state.trail_protected_floor(CON)==earned
    positions={CON:PositionData(con_id=CON,symbol='AAPL',right='C',quantity=8,
                                avg_cost=6.5,expiry='20261231')}
    close,_=_wire(restarted,quotes={CON:live_quote(5.2)},portfolio=[_PortRow(CON,5.2,8)],positions=positions)
    await restarted.run_cycle(dry_run=False)
    close.assert_called_once()
    assert close.call_args.kwargs['trigger_type']=='trailing_stop'
    assert close.call_args.kwargs['quantity']==8
    assert close.call_args.kwargs['entry_debit']==5200.
