from datetime import date, datetime, timedelta, timezone
import time
from unittest.mock import MagicMock

import pytest
from exitmgr.trail_qualification import qualify
from exitmgr.config import TrailingConfig
from tests.test_broker_mark_when_quotes_fail import _mgr, _wire, _PortRow, CON, SCID, JOURNAL

NOW=datetime(2026,9,18,19,0,tzinfo=timezone.utc)


def quote(bid=5.9,ask=6.,delta=.5,**kw):
    return dict(bid=bid,ask=ask,delta=delta,min_tick=.01,market_data_type=1,
                observed_monotonic=99.,bid_observed_utc=(NOW-timedelta(seconds=1)).isoformat(),
                ask_observed_utc=(NOW-timedelta(seconds=1)).isoformat(),**kw)


def runq(quotes=None,**kw):
    args=dict(con_id=CON,quotes=quotes or {CON:quote()},entry_debit=2000,quantity=4,
              journal=JOURNAL,atr_record=dict(atr=1.,asof='2026-09-18',spot=200),
              now_monotonic=100.,now_utc=NOW,today=NOW.date())
    args.update(kw)
    return qualify(**args)


def test_qualifies_current_executable_gain_with_actual_delta_and_tick():
    q=runq()
    assert q['qualified']
    assert q['activation_gain_pct']==10.
    assert q['executable_price']==5.9
    assert not q['entry_commission_known'] and not q['exit_commission_known']


@pytest.mark.parametrize('change,reason',[
    ({'market_data_type':3},'nonlive_or_unknown_market_data'),
    ({'market_data_type':4},'nonlive_or_unknown_market_data'),
    ({'market_data_type':2},'nonlive_or_unknown_market_data'),
    ({'market_data_type':None},'nonlive_or_unknown_market_data'),
    ({'bid':float('nan')},'missing_or_crossed_bid_ask'),
    ({'ask':5.8},'missing_or_crossed_bid_ask'),
    ({'min_tick':None},'unknown_tick_size'),
    ({'observed_monotonic':30.},'stale_quote'),
    ({'observed_monotonic':101.},'stale_quote'),
    ({'bid_observed_utc':None},'missing_bid_receipt'),
    ({'ask_observed_utc':'2026-09-18T19:00:00'},'unzoned_ask_receipt'),
    ({'delta':float('nan')},'invalid_quote_delta'),
    ({'delta':0.},'invalid_net_delta'),
])
def test_missing_invalid_inputs_do_not_arm(change,reason):
    q=quote();q.update(change)
    out=runq({CON:q})
    assert not out['qualified'] and out['reason'].startswith(reason)


def test_leg_receipts_must_align_even_when_batch_timestamp_matches():
    journal=dict(JOURNAL,strike=200,spread=dict(short_con_id=SCID,short_strike=210))
    long=quote(7.,7.1,.7);short=quote(1.,1.1,.2)
    short['ask_observed_utc']=(NOW-timedelta(seconds=5)).isoformat()
    out=runq({CON:long,SCID:short},journal=journal)
    assert out['reason']=='asynchronous_spread_quotes'


@pytest.mark.parametrize('atr',[None,dict(atr=1,asof='2026-09-10'),dict(atr=1,asof='2026-09-19')])
def test_no_atr_fallback_for_new_arm(atr):
    out=runq(atr_record=atr)
    assert out['quote_qualified'] and not out['qualified']
    assert out['activation_gain_pct'] is None


def test_spread_uses_executable_net_not_long_mid():
    journal=dict(JOURNAL,strike=200,spread=dict(short_con_id=SCID,short_strike=210))
    out=runq({CON:quote(7.,7.2,.7),SCID:quote(1.7,1.9,.2)},journal=journal)
    assert out['executable_price']==pytest.approx(5.1)
    assert not out['qualified']
    assert out['reason']=='current_executable_gain_below_activation'


def test_real_cost_floor_prevents_ultralow_noise_arm():
    out=runq({CON:quote(5.15,5.6,.01)},journal=dict(JOURNAL,entry_commission=4.))
    assert out['entry_commission_known']
    assert out['noise_and_known_cost_floor_pct']==pytest.approx(4.5)
    assert not out['qualified']
    assert out['gain_after_known_entry_fees_pct']==pytest.approx(2.8)


def live_quote(price):
    now=datetime.now(timezone.utc).isoformat()
    return dict(price=price,bid=price,ask=price+.05,delta=.5,min_tick=.01,market_data_type=1,
                observed_monotonic=time.monotonic(),bid_observed_utc=now,ask_observed_utc=now)


def manager(tmp_path,monkeypatch):
    mgr,cfg=_mgr(tmp_path)
    cfg.rules.trailing=TrailingConfig(enabled=True,activation_gain_pct=20,giveback_fraction=.4)
    cfg.rules.auto_trail.activation_gain_pct=20
    cfg.rules.atr_levels.dynamic_activation_enabled=True
    cfg.rules.atr_levels.qualification_enabled=True
    monkeypatch.setattr('exitmgr.atr_cache.read',lambda *a,**kw:
                        dict(atr=1.,spot=200.,asof=date.today().isoformat()))
    mgr._atr_levels_for=MagicMock(return_value=None)
    return mgr,cfg


@pytest.mark.asyncio
async def test_historical_peak_cannot_arm_new_trail_and_fresh_arm_seeds_current(tmp_path,monkeypatch):
    mgr,cfg=manager(tmp_path,monkeypatch)
    mgr.state_manager.state.peak_prices[str(CON)]=15.
    portfolio=[_PortRow(CON,5.3)]
    quotes={CON:live_quote(5.3)}
    place,_=_wire(mgr,quotes=quotes,portfolio=portfolio)
    await mgr.run_cycle(dry_run=False)
    assert not mgr.state_manager.state.is_trail_armed(CON)
    portfolio[0].marketPrice=5.9;quotes[CON]=live_quote(5.9)
    await mgr.run_cycle(dry_run=False)
    assert mgr.state_manager.state.is_trail_armed(CON)
    assert mgr.state_manager.state.trail_peak_since_arm(CON)==5.9
    place.assert_not_called()


@pytest.mark.asyncio
async def test_missing_quote_blocks_new_arm_but_never_hard_loss(tmp_path,monkeypatch):
    mgr,cfg=manager(tmp_path,monkeypatch)
    mgr._trail_qualification_alert_not_before = 0
    monkeypatch.setattr(mgr, '_option_rth_now', lambda now=None: True)
    qualification_alert = MagicMock(return_value=True)
    mgr._post_new_trail_qualification_alert = qualification_alert
    portfolio=[_PortRow(CON,7.)]
    place,alert=_wire(mgr,quotes={},portfolio=portfolio)
    await mgr.run_cycle(dry_run=False)
    assert not mgr.state_manager.state.is_trail_armed(CON)
    assert qualification_alert.called
    assert not alert.called
    portfolio[0].marketPrice=3.5
    await mgr.run_cycle(dry_run=False)
    assert place.call_args.kwargs['trigger_type']=='stop'


def test_new_trail_alert_is_rth_warmup_gated_reason_stable_and_clears(tmp_path,monkeypatch):
    mgr,_=manager(tmp_path,monkeypatch)
    def delivered(*args, **kwargs):
        kwargs['on_delivery'](True)
        return True
    posted=MagicMock(side_effect=delivered)
    mgr._post_unthrottled_alert=posted
    mgr._trail_qualification_alert_not_before=time.monotonic()+60
    monkeypatch.setattr(mgr,'_option_rth_now',lambda now=None:True)
    assert not mgr._post_new_trail_qualification_alert('AAPL',CON,'stale_quote')
    mgr._trail_qualification_alert_not_before=0
    assert mgr._post_new_trail_qualification_alert('AAPL',CON,'stale_quote')
    assert not mgr._post_new_trail_qualification_alert(
        'AAPL',CON,'nonlive_or_unknown_market_data')
    body=posted.call_args.args[0]
    assert 'New trailing-stop activation deferred' in body
    assert 'Protective exits WITHHELD' not in body
    mgr._clear_trail_qualification_alert(CON)
    assert mgr._post_new_trail_qualification_alert('AAPL',CON,'stale_quote')
    monkeypatch.setattr(mgr,'_option_rth_now',lambda now=None:False)
    assert not mgr._post_new_trail_qualification_alert('AAPL',CON,'stale_quote')


def test_failed_new_trail_alert_delivery_does_not_consume_backoff(tmp_path, monkeypatch):
    mgr, _ = manager(tmp_path, monkeypatch)
    mgr._trail_qualification_alert_not_before = 0
    monkeypatch.setattr(mgr, '_option_rth_now', lambda now=None: True)
    delivered = iter((False, True))
    posts = []

    def post(*args, **kwargs):
        posts.append((args, kwargs))
        return next(delivered)

    monkeypatch.setattr('exitmgr.alerting.post', post)
    assert not mgr._post_new_trail_qualification_alert('AAPL', CON, 'stale_quote')
    assert mgr._post_new_trail_qualification_alert('AAPL', CON, 'stale_quote')
    assert len(posts) == 2
    assert not mgr._post_new_trail_qualification_alert(
        'AAPL', CON, 'nonlive_or_unknown_market_data')


def test_cancelled_new_trail_delivery_leaves_no_restart_backoff(tmp_path, monkeypatch):
    mgr, _ = manager(tmp_path, monkeypatch)
    mgr._trail_qualification_alert_not_before = 0
    monkeypatch.setattr(mgr, '_option_rth_now', lambda now=None: True)
    pending = []
    mgr._post_unthrottled_alert = lambda *a, **kw: (
        pending.append(kw['on_delivery']) or True)
    assert mgr._post_new_trail_qualification_alert('AAPL', CON, 'stale_quote')
    assert pending, "test did not leave a delivery pending"



    restarted, _ = manager(tmp_path, monkeypatch)
    restarted._trail_qualification_alert_not_before = 0
    monkeypatch.setattr(restarted, '_option_rth_now', lambda now=None: True)
    posts = []
    monkeypatch.setattr('exitmgr.alerting.post', lambda *a, **kw: posts.append(1) or True)
    assert restarted._post_new_trail_qualification_alert('AAPL', CON, 'stale_quote')
    assert posts == [1]


def test_new_campaign_on_same_conid_does_not_inherit_trail_alert_backoff(
        tmp_path, monkeypatch):
    mgr, _ = manager(tmp_path, monkeypatch)
    mgr._trail_qualification_alert_not_before = 0
    monkeypatch.setattr(mgr, '_option_rth_now', lambda now=None: True)
    posts = []
    monkeypatch.setattr('exitmgr.alerting.post', lambda *a, **kw: posts.append(1) or True)
    first_key = mgr._trail_qualification_key(CON)
    assert mgr._post_new_trail_qualification_alert('AAPL', CON, 'stale_quote')
    prior = dict(mgr.state_manager.state.campaign_bindings[str(CON)])
    prior['first_lot_ts'] = '2026-09-19T14:00:00+00:00'
    mgr.state_manager.state.campaign_bindings[str(CON)] = prior
    assert mgr._trail_qualification_key(CON) != first_key
    assert mgr._post_new_trail_qualification_alert('AAPL', CON, 'stale_quote')
    assert posts == [1, 1]


@pytest.mark.asyncio
async def test_armed_trail_survives_missing_atr_and_prices_do_not_raise_peak(tmp_path,monkeypatch):
    mgr,cfg=manager(tmp_path,monkeypatch)
    st=mgr.state_manager.state
    st.arm_on_peak_gain(CON,5.9,2000,4,10)
    st.pin_trail_params(CON,10,.4)
    monkeypatch.setattr('exitmgr.atr_cache.read',lambda *a,**kw:None)
    portfolio=[_PortRow(CON,8.)]
    place,_=_wire(mgr,quotes={},portfolio=portfolio)
    await mgr.run_cycle(dry_run=False)
    assert st.trail_peak_since_arm(CON)==5.9
    assert st.is_trail_armed(CON)
    portfolio[0].marketPrice=5.2
    await mgr.run_cycle(dry_run=False)
    assert place.call_args.kwargs['trigger_type']=='trailing_stop'


@pytest.mark.asyncio
async def test_executable_breach_fires_even_if_broker_mid_above_floor(tmp_path,monkeypatch):
    mgr,cfg=manager(tmp_path,monkeypatch)
    st=mgr.state_manager.state
    st.arm_on_peak_gain(CON,5.9,2000,4,10)
    st.pin_trail_params(CON,10,.4)
    portfolio=[_PortRow(CON,5.8)]
    place,_=_wire(mgr,quotes={CON:live_quote(5.2)},portfolio=portfolio)
    await mgr.run_cycle(dry_run=False)
    assert place.call_args.kwargs['trigger_type']=='trailing_stop'


@pytest.mark.parametrize('giveback',[.1,.5,.8,.9])
def test_initial_protected_gain_covers_known_fee_and_tick_after_giveback(giveback):

    out=runq({CON:quote(5.7,5.71,.001)},journal=dict(JOURNAL,entry_commission=20.),
             giveback_fraction=giveback)
    assert out['qualified']
    retained=out['retained_gain_at_activation_per_share']
    assert retained+1e-12 >= .01 + 20/(100*4)
    assert out['activation_gain_pct']==pytest.approx((.01+.05)/(1-giveback)/5*100)
    assert out['exit_commission_known'] is False
    assert 'not a net-profit guarantee' in out['fees_note']


def test_partial_quantity_prorates_only_observed_commission():
    out=runq(journal=dict(JOURNAL,entry_commission=8.),quantity=2,entry_debit=1000.)
    assert out['known_entry_commission']==4.
    assert out['entry_commission_allocation']=='proportional_to_remaining_quantity'


def test_missing_new_lot_fee_is_not_invented_by_scaling_old_fee_up():
    out=runq(journal=dict(JOURNAL,quantity=1,entry_commission=2.),quantity=4)
    assert not out['entry_commission_known']
    assert out['known_entry_commission'] is None
    assert out['entry_commission_allocation']=='unknown_additional_lot_fees'


@pytest.mark.parametrize('bad',[1.,float('nan'),-.1,None])
def test_unusable_giveback_cannot_qualify_new_arm(bad):
    out=runq(giveback_fraction=bad)
    assert not out['qualified']
    assert out['reason']=='invalid_giveback_fraction'
