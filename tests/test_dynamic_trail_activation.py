"""Public API contract; production-derived narrative omitted."""
from unittest.mock import MagicMock

import pytest
from exitmgr.config import TrailingConfig
from exitmgr.connection import PositionData
from exitmgr.state import StateManager
from tests.test_broker_mark_when_quotes_fail import _mgr, _wire, _PortRow, CON, SCID, JOURNAL


def setup_dynamic(tmp_path):
    mgr, cfg = _mgr(tmp_path)
    cfg.rules.trailing = TrailingConfig(enabled=True, activation_gain_pct=20, giveback_fraction=.4)
    cfg.rules.auto_trail.activation_gain_pct = 20
    cfg.rules.atr_levels.dynamic_activation_enabled = True
    cfg.rules.atr_levels.dynamic_k_arm = 1
    return mgr, cfg


def levels(per_atr=15.58):
    return dict(per_atr_pct=per_atr, activation_gain_pct=per_atr * 1.5,
                entry_per_share=5., atr=1., net_delta=.779, stop_pct=30.)


@pytest.mark.parametrize('value,expected', [(5.,5.), (15.58,15.58), (35.,20.)])
def test_per_position_conversion_and_configured_safety_ceiling(tmp_path,value,expected):
    mgr,_=setup_dynamic(tmp_path)
    assert mgr._trail_activation_for(CON,levels(value),2000,4)['activation_gain_pct']==expected


@pytest.mark.parametrize('lv', [None, {}, levels(float('nan')), levels(float('inf')), levels(0)])
def test_invalid_or_missing_atr_uses_explicit_configured_fallback(tmp_path,lv):
    mgr,_=setup_dynamic(tmp_path)
    info=mgr._trail_activation_for(CON,lv,2000,4)
    assert info['activation_gain_pct']==20
    assert info['source']=='configured_fallback:atr_unavailable'


def test_friction_uses_both_legs_but_never_raises_existing_safety_ceiling(tmp_path):
    mgr,_=setup_dynamic(tmp_path)
    quotes={CON:dict(bid=4,ask=5),SCID:dict(bid=1,ask=1.5)}
    info=mgr._trail_activation_for(CON,levels(5),2000,4,quotes,dict(short_con_id=SCID))
    assert info['friction_floor_pct']==pytest.approx(15)
    assert info['activation_gain_pct']==pytest.approx(15)
    quotes[CON]['ask']=10
    assert mgr._trail_activation_for(CON,levels(5),2000,4,quotes,dict(short_con_id=SCID))['activation_gain_pct']==20


@pytest.mark.parametrize('bad', [dict(bid=6,ask=5),dict(bid=float('nan'),ask=5),{},dict(bid=0,ask=5)])
def test_bad_friction_quotes_are_not_invented(tmp_path,bad):
    mgr,_=setup_dynamic(tmp_path)
    info=mgr._trail_activation_for(CON,levels(),2000,4,{CON:bad})
    assert info['activation_gain_pct']==15.58
    assert info['friction_floor_pct'] is None
    assert 'friction_unavailable' in info['source']


@pytest.mark.asyncio
async def test_real_cycles_arm_below_twenty_and_exit_on_giveback_after_restart(tmp_path):
    mgr,cfg=setup_dynamic(tmp_path)
    peak=5*(1+.1825)
    portfolio=[_PortRow(CON,peak)]
    place,_=_wire(mgr,quotes={CON:dict(price=peak,iv=.3)},portfolio=portfolio)
    mgr._atr_levels_for=MagicMock(return_value=levels())
    await mgr.run_cycle(dry_run=False)
    state=mgr.state_manager.state
    assert state.is_trail_armed(CON)
    assert state.pinned_trail_params(CON)['activation_gain_pct']==15.58
    place.assert_not_called()

    mgr.state_manager=StateManager(cfg.state.path)
    mgr._atr_levels_for.return_value=levels(60)
    portfolio[0].marketPrice=5.2
    await mgr.run_cycle(dry_run=False)
    place.assert_called_once()
    assert place.call_args.kwargs['trigger_type']=='trailing_stop'


@pytest.mark.asyncio
async def test_price_above_protected_floor_holds_and_model_matches_pin(tmp_path):
    mgr,cfg=setup_dynamic(tmp_path)
    peak=5.9125
    portfolio=[_PortRow(CON,peak)]
    quotes={CON:dict(price=peak,iv=.3)}
    place,_=_wire(mgr,quotes=quotes,portfolio=portfolio)
    mgr._atr_levels_for=MagicMock(return_value=levels())
    await mgr.run_cycle(dry_run=False)
    portfolio[0].marketPrice=5.8
    await mgr.run_cycle(dry_run=False)
    place.assert_not_called()
    pos=PositionData(con_id=CON,symbol='AAPL',right='C',quantity=4,avg_cost=5,expiry='20371231')
    view=mgr._build_position_views([pos],quotes,portfolio_marks={CON:5.8})[0]
    assert view['trail_activation_gain_pct']==15.58
    assert view['trail_armed'] is True
    assert view['trail_giveback_fraction']<=.5


@pytest.mark.asyncio
async def test_thirty_percent_loss_still_fires_stop_with_dynamic_trail(tmp_path):
    mgr,_=setup_dynamic(tmp_path)
    portfolio=[_PortRow(CON,5.9125)]
    place,_=_wire(mgr,quotes={},portfolio=portfolio)
    mgr._atr_levels_for=MagicMock(return_value=levels())
    await mgr.run_cycle(dry_run=False)
    portfolio[0].marketPrice=3.5
    await mgr.run_cycle(dry_run=False)
    assert place.call_args.kwargs['trigger_type']=='stop'


def test_old_pinned_activation_and_giveback_cannot_be_loosened(tmp_path):
    mgr,_=setup_dynamic(tmp_path)
    st=mgr.state_manager.state
    st.arm_on_peak_gain(CON,5.6,2000,4,10)
    st.pin_trail_params(CON,10,.25)
    info=mgr._trail_activation_for(CON,levels(40),2000,4)
    assert info['activation_gain_pct']==10
    rules=mgr._with_trail_activation(mgr.config.rules,info['activation_gain_pct'])
    assert mgr._bound_pinned_trail(st.pinned_trail_params(CON),rules.trailing)==(10,.25)


def test_disabled_dynamic_retains_legacy_multiple(tmp_path):
    mgr,cfg=setup_dynamic(tmp_path)
    cfg.rules.atr_levels.dynamic_activation_enabled=False
    assert mgr._trail_activation_for(CON,levels(8),2000,4)['activation_gain_pct']==12


@pytest.mark.asyncio
async def test_recorded_SYMS_peak_would_arm_and_giveback_reaches_exit(tmp_path):


    journal=dict(JOURNAL,symbol="SYMS",quantity=1,debit=400.)
    mgr,cfg=_mgr(tmp_path,journal=journal)
    cfg.rules.trailing=TrailingConfig(enabled=True,activation_gain_pct=20,giveback_fraction=.4)
    cfg.rules.auto_trail.activation_gain_pct=20
    cfg.rules.atr_levels.dynamic_activation_enabled=True
    portfolio=[_PortRow(CON,5.0,position=1)]
    pos=PositionData(con_id=CON,symbol="SYMS",right="C",quantity=1,avg_cost=4.0,expiry="20371231")
    place,_=_wire(mgr,quotes={},portfolio=portfolio,positions={CON:pos})
    lv=levels(); lv.update(entry_per_share=4.0,net_delta=.5)
    mgr._atr_levels_for=MagicMock(return_value=lv)
    await mgr.run_cycle(dry_run=False)
    assert mgr.state_manager.state.is_trail_armed(CON)
    assert mgr.state_manager.state.pinned_trail_params(CON)["activation_gain_pct"]==15.58
    place.assert_not_called()
    portfolio[0].marketPrice=3.7
    await mgr.run_cycle(dry_run=False)
    assert place.call_args.kwargs["trigger_type"]=="trailing_stop"


def test_dynamic_config_roundtrips_with_strict_loader(tmp_path):
    import yaml
    from exitmgr.config import Config, ConfigError
    from tests.test_config_value_validation import REAL_CONFIG
    data=yaml.safe_load(open(REAL_CONFIG))
    data['rules'].setdefault('atr_levels',{}).update(
        dynamic_activation_enabled=True,dynamic_k_arm=1.)
    path=tmp_path/'dynamic.yaml';path.write_text(yaml.safe_dump(data))
    cfg=Config.from_yaml(str(path))
    assert cfg.rules.atr_levels.dynamic_activation_enabled is True
    assert cfg.rules.atr_levels.dynamic_k_arm==1.
    for invalid in (0,-1,float('nan'),float('inf')):
        data['rules']['atr_levels']['dynamic_k_arm']=invalid
        path.write_text(yaml.safe_dump(data))
        with pytest.raises(ConfigError):
            Config.from_yaml(str(path))


@pytest.mark.asyncio
async def test_dynamic_auto_safety_alone_enables_and_fires_below_twenty(tmp_path):
    mgr,cfg=setup_dynamic(tmp_path)
    cfg.rules.trailing.enabled=False
    peak=5.9125
    portfolio=[_PortRow(CON,peak)]
    place,_=_wire(mgr,quotes={},portfolio=portfolio)
    mgr._atr_levels_for=MagicMock(return_value=levels())
    await mgr.run_cycle(dry_run=False)
    assert mgr.state_manager.state.is_trail_armed(CON)
    place.assert_not_called()
    portfolio[0].marketPrice=5.2
    await mgr.run_cycle(dry_run=False)
    place.assert_called_once()
    assert place.call_args.kwargs['trigger_type']=='trailing_stop'
