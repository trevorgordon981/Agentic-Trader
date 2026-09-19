"""Public API contract; production-derived narrative omitted."""
import copy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib, json, socket
from pathlib import Path
from unittest.mock import Mock
import pytest
from exitmgr.config import Config
from exitmgr.manager import ExitManager, build_journal_campaigns, _campaign_receipt, _terminal_flex_marker, _terminal_manager_marker
from exitmgr.state import InFlightClose
from exitmgr.flex_close_recovery import build_receipt

@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.setattr(socket.socket,'connect',Mock(side_effect=AssertionError('no network')))
    monkeypatch.setattr(socket.socket,'connect_ex',Mock(side_effect=AssertionError('no network')))
    def make(latch=True):

        import importlib.util
        spec=importlib.util.spec_from_file_location('flex_fixture',Path(__file__).with_name('test_flex_close_recovery.py'))
        fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
        rows,kw=fixture.case.__wrapped__()
        entry=kw['entry_snapshot']; entry['debit']=220.0; entry['entry_commission']=2.0
        entry['spread'].update(short_strike=105.0,width=5.0)
        cfg=Config();cfg.journal.path=str(tmp_path/'journal');cfg.state.path=str(tmp_path/'state')
        cfg.kill_switch.path=str(tmp_path/'kill');cfg.manage_positions=False
        cfg.alerts_channel=cfg.error_channel=''
        Path(cfg.journal.path).write_text(json.dumps(entry)+'\n')
        mgr=ExitManager(cfg)
        mgr.ib_conn.place_order=Mock(side_effect=AssertionError('no broker order'))
        mgr._post_exit_alert=Mock(side_effect=AssertionError('no Slack'))
        mgr._maybe_write_reload_ticket=Mock(side_effect=AssertionError('no reload'))
        kw['campaign_binding']=_campaign_receipt(build_journal_campaigns([entry])[1001][0])
        kw['submitted_close'].update(order_type='MKT',tif='DAY',limit_price=None,stop_price=None)
        inf=None
        if latch:
            inf=InFlightClose(con_id=1001,order_id=6,remaining_qty=1,entry_debit=220.0,
                placed_at='2026-08-25T13:29:59+00:00',perm_id=777,client_id=42,
                order_ref='close-fixture',identity_version=1,submitted_close=kw['submitted_close'],
                exit_context=dict(entry_debit=220.0,close_qty=1,position_qty=1,symbol='FIXTURE',journal_entry=entry))
            mgr.state_manager.state.add_in_flight(inf)
        kw['inflight_snapshot']=asdict(inf) if inf else None
        mgr.state_manager.state.campaign_bindings['1001']=kw['campaign_binding']
        mgr.state_manager.state.peak_prices['1001']=3.0;mgr.state_manager.save()
        raw,sha=fixture.encode(rows);kw['expected_xml_sha256']=sha
        receipt=build_receipt(raw,**kw)
        def run(**changes):
            args=dict(source_bindings=kw,positions={},order_leg_con_ids=[],
                      observed_at=datetime.now(timezone.utc),current_account_id='fixture-account')
            args.update(changes)
            if args.get('apply') and 'observation_provider' not in args:
                args['observation_provider']=lambda:{k:args[k] for k in ('positions','order_leg_con_ids','observed_at','current_account_id')}
            return mgr.recover_archived_close(receipt,raw,**args)
        return mgr,receipt,kw,run
    return make

@pytest.mark.parametrize('latch',[True,False])
def test_preview_never_writes(case,latch):
    mgr,r,kw,run=case(latch)
    paths=[Path(mgr.config.state.path),Path(mgr.config.journal.path)];before=[p.read_bytes() for p in paths]
    assert run()['status']=='preview'
    assert [p.read_bytes() for p in paths]==before
    assert not Path(mgr._exits_log_path()).exists()

@pytest.mark.parametrize('latch',[True,False])
def test_exact_historical_commit_and_idempotent_replay(case,latch):
    mgr,r,kw,run=case(latch)
    out=run(apply=True);assert out['status']=='committed',out
    path=Path(mgr._exits_log_path());row=json.loads(path.read_text().splitlines()[0])
    assert row['close_ts']==row['ts']=='2026-08-25T13:30:00+00:00'
    assert row['realized_pnl']==60.0 and row['realized_pnl_net']==56.0
    assert row['entry_commission']==row['exit_commission']==2.0
    assert row['fill_evidence_source']=='ibkr_flex'
    assert not {'order_id','perm_id','client_id'}.intersection(row)
    assert ('native_submission_context' in row)==latch
    marker=json.loads(Path(mgr.config.journal.path).read_text().splitlines()[-1])
    assert _terminal_flex_marker(marker) and not _terminal_manager_marker(marker)
    assert not {'order_id','perm_id','client_id','submitted_close'}.intersection(marker)
    assert mgr.state_manager.state.get_in_flight(1001) is None
    assert '1001' not in mgr.state_manager.state.peak_prices
    out=run(apply=True);assert out['status'] in ('committed','already_committed'),out
    assert len(path.read_text().splitlines())==1
    assert len(Path(mgr.config.journal.path).read_text().splitlines())==2
    mgr.ib_conn.place_order.assert_not_called();mgr._post_exit_alert.assert_not_called()
    mgr._maybe_write_reload_ticket.assert_not_called()

@pytest.mark.parametrize('changes',[dict(positions=None),dict(order_leg_con_ids=None),dict(positions={1001:1}),
    dict(positions={1002:-1}),dict(order_leg_con_ids=[1002]),dict(current_account_id=None),
    dict(current_account_id='wrong'),dict(observed_at=datetime(2000,1,1,tzinfo=timezone.utc)),
    dict(observed_at=datetime(2099,1,1,tzinfo=timezone.utc))])
def test_no_commit_on_unknown_live_wrong_account_or_stale_observation(case,changes):
    mgr,r,kw,run=case();paths=[Path(mgr.config.state.path),Path(mgr.config.journal.path)]
    before=[p.read_bytes() for p in paths];out=run(apply=True,**changes)
    assert out['status']=='refused',out
    assert [p.read_bytes() for p in paths]==before
    assert not Path(mgr._exits_log_path()).exists()

@pytest.mark.parametrize('change',['journal','latch','campaign','receipt'])
def test_changed_authority_never_clears_old_or_new_campaign(case,change):
    mgr,r,kw,run=case()
    if change=='journal':
        with Path(mgr.config.journal.path).open('a') as f:
            f.write(json.dumps(dict(kw['entry_snapshot'],expiry='20310117',ts='2026-09-08T13:00:00+00:00'))+'\n')
    elif change=='latch':
        mgr.state_manager.state.get_in_flight(1001).order_ref='other';mgr.state_manager.save()
    elif change=='campaign':
        mgr.state_manager.state.campaign_bindings['1001']['campaign_seq']=2;mgr.state_manager.save()
    else:r['net_fill_price']='999'
    assert run(apply=True)['status']=='refused'
    assert mgr.state_manager.state.get_in_flight(1001) is not None
    assert mgr.state_manager.state.peak_prices['1001']==3.0


def test_ledger_failure_does_not_append_boundary_or_release_latch(case,monkeypatch):
    mgr,r,kw,run=case();monkeypatch.setattr(mgr,'_log_exit',lambda *a,**k:False)
    assert run(apply=True)['status']=='refused'
    assert mgr.state_manager.state.get_in_flight(1001) is not None
    assert len(Path(mgr.config.journal.path).read_text().splitlines())==1


def test_missing_order_id_legacy_row_cannot_swallow_archive_fill(case):
    mgr,r,kw,run=case(False)
    Path(mgr._exits_log_path()).write_text(json.dumps(dict(contract_id=9999,fill_status='Filled'))+'\n')
    out=run(apply=True);assert out['status']=='committed',out
    assert len(Path(mgr._exits_log_path()).read_text().splitlines())==2


@pytest.mark.parametrize('con_id',[1001,'1001'])
def test_ambiguous_filled_same_contract_never_double_books(case,con_id):
    mgr,r,kw,run=case()
    path=Path(mgr._exits_log_path())
    path.write_text(json.dumps(dict(contract_id=con_id,fill_status='Filled',realized_pnl=60))+'\n')
    before=path.read_bytes()
    out=run(apply=True);assert out['status']=='refused',out
    assert 'unbound filled ledger' in out['reason']
    assert path.read_bytes()==before
    assert mgr.state_manager.state.get_in_flight(1001) is not None
    assert len(Path(mgr.config.journal.path).read_text().splitlines())==1


@pytest.mark.parametrize('con_id',[1001,'1001'])
def test_same_order_reference_other_identity_never_double_books(case,con_id):
    mgr,r,kw,run=case()
    path=Path(mgr._exits_log_path())
    path.write_text(json.dumps(dict(contract_id=con_id,fill_status='Filled',
        order_ref=r['close_order_ref'],close_identity='native:existing'))+'\n')
    before=path.read_bytes()
    out=run(apply=True);assert out['status']=='refused',out
    assert 'different ledger identity' in out['reason']
    assert path.read_bytes()==before
    assert mgr.state_manager.state.get_in_flight(1001) is not None
    assert len(Path(mgr.config.journal.path).read_text().splitlines())==1


def test_apply_requires_observation_inside_real_host_and_journal_locks(case,monkeypatch,tmp_path):
    import fcntl,os
    mgr,r,kw,run=case();host=tmp_path/'order.lock'
    monkeypatch.setenv('EXITMGR_ORDER_LOCK',str(host))
    assert run(apply=True,observation_provider=None)['status']=='refused'
    calls=[]
    def observe():
        for path in (host,Path(str(mgr.config.journal.path)+'.lock')):
            fd=os.open(path,os.O_RDWR)
            try:
                with pytest.raises(BlockingIOError):fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            finally:os.close(fd)
        calls.append(True)
        return dict(current_account_id='fixture-account',positions={},order_leg_con_ids=[],observed_at=datetime.now(timezone.utc))
    out=run(apply=True,positions={1001:99},observation_provider=observe)
    assert out['status']=='committed',out
    assert calls==[True]


def test_lying_exit_writer_cannot_release_latch(case,monkeypatch):
    mgr,r,kw,run=case();monkeypatch.setattr(mgr,'_log_exit',lambda *a,**k:True)
    assert run(apply=True)['status']=='refused'
    assert mgr.state_manager.state.get_in_flight(1001) is not None
    assert len(Path(mgr.config.journal.path).read_text().splitlines())==1


def test_state_write_crash_replays_exact_durable_boundary_once(case,monkeypatch):
    mgr,r,kw,run=case();save=mgr.state_manager.save
    monkeypatch.setattr(mgr.state_manager,'save',Mock(side_effect=OSError('synthetic crash')))
    assert run(apply=True)['status']=='refused'
    assert mgr.state_manager.state.get_in_flight(1001) is not None
    assert len(Path(mgr._exits_log_path()).read_text().splitlines())==1
    assert len(Path(mgr.config.journal.path).read_text().splitlines())==2
    monkeypatch.setattr(mgr.state_manager,'save',save)
    out=run(apply=True);assert out['status']=='committed',out
    assert mgr.state_manager.state.get_in_flight(1001) is None
    assert len(Path(mgr._exits_log_path()).read_text().splitlines())==1
    assert len(Path(mgr.config.journal.path).read_text().splitlines())==2


@pytest.mark.parametrize('field,value',[('realized_pnl_net',999),('entry_commission',-2),('fill_evidence_source','native')])
def test_corrupt_existing_ledger_refuses_replay(case,field,value):
    mgr,r,kw,run=case();assert run(apply=True)['status']=='committed'
    path=Path(mgr._exits_log_path());row=json.loads(path.read_text());row[field]=value
    path.write_text(json.dumps(row)+'\n')
    assert run(apply=True)['status']=='refused'


@pytest.mark.parametrize('delta',[-10,10])
def test_rehashed_marker_with_invalid_observation_time_is_not_terminal(case,delta):
    from datetime import timedelta
    from exitmgr.flex_close_recovery import digest
    mgr,r,kw,run=case();assert run(apply=True)['status']=='committed'
    marker=json.loads(Path(mgr.config.journal.path).read_text().splitlines()[-1])
    marker['broker_flat_evidence']['observed_at']=(datetime.fromisoformat(marker['recorded_at'])+timedelta(seconds=delta)).isoformat()
    marker.pop('binding_sha');marker['binding_sha']=digest(marker)
    assert not _terminal_flex_marker(marker)
