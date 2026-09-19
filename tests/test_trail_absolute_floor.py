import math
import pytest
from exitmgr.state import State, StateManager
from exitmgr.rules import evaluate_trailing_stop, evaluate_position
from exitmgr.config import RulesConfig, TrailingConfig


def armed():
    st=State()
    st.arm_on_peak_gain(1,6.,500.,1,10.)
    st.pin_trail_params(1,10.,.5)
    return st


def test_scale_in_cannot_erase_or_move_earned_floor():
    st=armed()
    assert st.ratchet_trail_floor(1,500.,1,.5)==5.5
    assert st.ratchet_trail_floor(1,1300.,2,.5)==5.5
    trigger=evaluate_trailing_stop(5.2,1300.,2,6.,10.,.5,armed=True,
                                  protected_floor_price=st.trail_protected_floor(1))
    assert trigger.trigger_type=='trailing_stop'
    assert trigger.pnl_pct==pytest.approx(-20.)
    assert evaluate_trailing_stop(5.6,1300.,2,6.,10.,.5,armed=True,
                                 protected_floor_price=5.5) is None


def test_restart_preserves_anchor_and_floor_after_high_cost_scale_in(tmp_path):
    sm=StateManager(str(tmp_path/'state.json'))
    sm.state.arm_on_peak_gain(1,6.,500.,1,10.)
    sm.state.pin_trail_params(1,10.,.5)
    sm.state.ratchet_trail_floor(1,500.,1,.5)
    sm.save()
    restored=StateManager(str(tmp_path/'state.json'))
    assert restored.state.ratchet_trail_floor(1,1300.,2,.5)==5.5
    assert restored.state.trail_confirmation_for(1)['protected_entry_per_share']==5.
    assert evaluate_trailing_stop(5.2,1300,2,6,10,.5,armed=True,
        protected_floor_price=restored.state.trail_protected_floor(1)) is not None


def test_only_market_peak_or_tighter_giveback_can_raise_floor():
    st=armed()
    st.ratchet_trail_floor(1,500,1,.5)
    assert st.ratchet_trail_floor(1,200,2,.9)==5.5
    st.record_trail_peak(1,7.)
    assert st.ratchet_trail_floor(1,1300,2,.5)==6.
    assert st.ratchet_trail_floor(1,1300,2,.25)==6.5
    assert st.ratchet_trail_floor(1,1300,2,.9)==6.5


def test_legacy_state_seeds_without_disabling_armed_low_gain():
    st=armed()
    assert st.trail_protected_floor(1) is None
    st.trail_confirmation['1']['pinned_activation_gain_pct']=30.
    assert st.ratchet_trail_floor(1,500,1,.5)==5.5
    assert evaluate_trailing_stop(5.2,500,1,6,30,.5,armed=True,
        protected_floor_price=st.trail_protected_floor(1)) is not None


@pytest.mark.parametrize('bad',[float('nan'),float('inf'),-1,'bad',None])
def test_corrupt_optional_floor_never_disables_hard_loss(bad):
    st=armed()
    st.trail_confirmation['1']['protected_floor_price']=bad
    assert st.trail_protected_floor(1) is None
    rules=RulesConfig(stop_pct=30,profit_target_pct=None,
                      trailing=TrailingConfig(enabled=True,activation_gain_pct=10,giveback_fraction=.5))
    out=evaluate_position(1,'X',1,500,3.5,100,6,rules,trail_armed=True,
                         peak_since_arm=6,protected_floor_price=bad)
    assert out.trigger_type=='stop'


def test_unarmed_record_never_gains_authority_from_a_floor():
    st=State()
    assert st.ratchet_trail_floor(1,500,1,.5) is None
    assert evaluate_trailing_stop(3,500,1,6,10,.5,armed=False,protected_floor_price=5.5) is None
