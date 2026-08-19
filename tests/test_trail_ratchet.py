"""The trail floor may only ever ratchet UP.

Trevor's ask (2026-08-19): "the trailing stop should be adjusted up in the event that
the position is gaining more".  With an ATR-sized trail the floor is `peak - k*ATR*delta`,
so it follows the peak automatically AND the giveback fraction shrinks as the gain grows.
These tests pin the monotonicity: tightening is allowed, loosening never is.
"""
import pytest

from exitmgr.state import State

CID = 714652653
EPS = 0.49          # PFE entry per share
PEAK = 0.6083


def _armed_state():
    st = State()
    st.arm_on_peak_gain(CID, PEAK, 392.0, 8, 20.0)
    assert st.is_trail_armed(CID)
    return st


def floor_of(gb, eps=EPS, peak=PEAK):
    return eps + (1 - gb) * (peak - eps)


def test_first_ratchet_acts_as_the_initial_pin():
    st = _armed_state()
    assert st.pinned_trail_params(CID) is None
    assert st.ratchet_trail_params(CID, 20.0, 0.56) is True
    assert st.pinned_trail_params(CID)["giveback_fraction"] == pytest.approx(0.56)


def test_a_smaller_giveback_tightens_and_raises_the_floor():
    st = _armed_state()
    st.ratchet_trail_params(CID, 20.0, 0.56)
    before = floor_of(st.pinned_trail_params(CID)["giveback_fraction"])
    assert st.ratchet_trail_params(CID, 20.0, 0.32) is True
    after = floor_of(st.pinned_trail_params(CID)["giveback_fraction"])
    assert after > before, "a tighter giveback must raise the protected floor"


def test_a_larger_giveback_is_refused_so_the_floor_never_drops():
    """A volatility expansion would widen the ATR distance and loosen the floor. That is
    exactly the move this guard exists to refuse."""
    st = _armed_state()
    st.ratchet_trail_params(CID, 20.0, 0.40)
    assert st.ratchet_trail_params(CID, 20.0, 0.75) is False
    assert st.pinned_trail_params(CID)["giveback_fraction"] == pytest.approx(0.40)


def test_an_unchanged_giveback_is_not_a_ratchet():
    st = _armed_state()
    st.ratchet_trail_params(CID, 20.0, 0.40)
    assert st.ratchet_trail_params(CID, 20.0, 0.40) is False


def test_ratchet_is_monotonic_across_a_whole_run_up():
    """Simulate a winner grinding higher: the ATR distance is constant, so the giveback
    fraction falls every time the peak rises. The floor must never once step down."""
    st = _armed_state()
    atr_prem = 0.0665                      # 1 ATR of PFE in spread-premium terms
    floors, peak = [], PEAK
    for _ in range(12):
        peak *= 1.03
        gb = atr_prem / (peak - EPS)
        st.ratchet_trail_params(CID, 20.0, gb)
        pinned = st.pinned_trail_params(CID)["giveback_fraction"]
        floors.append(floor_of(pinned, peak=peak))
    assert floors == sorted(floors), "protected floor stepped DOWN: %s" % floors
    assert floors[-1] > floors[0]


def test_an_unarmed_position_is_never_pinned_by_the_ratchet():
    """Arming owns the first pin. A ratchet must not arm anything by side effect."""
    st = State()
    assert st.is_trail_armed(CID) is False
    assert st.ratchet_trail_params(CID, 20.0, 0.4) is False
    assert st.pinned_trail_params(CID) is None


@pytest.mark.parametrize("bad", [None, "x", float("nan")])
def test_garbage_giveback_is_refused(bad):
    st = _armed_state()
    st.ratchet_trail_params(CID, 20.0, 0.5)
    assert st.ratchet_trail_params(CID, 20.0, bad) is False
    assert st.pinned_trail_params(CID)["giveback_fraction"] == pytest.approx(0.5)


def test_ratchet_respects_the_hard_clamp():
    st = _armed_state()
    st.ratchet_trail_params(CID, 20.0, 0.5)
    st.ratchet_trail_params(CID, 20.0, 0.0001)
    assert st.pinned_trail_params(CID)["giveback_fraction"] == pytest.approx(0.1)


def test_clearing_trail_state_drops_the_ratcheted_pin():
    """A re-entry into the same conId must never inherit an old floor."""
    st = _armed_state()
    st.ratchet_trail_params(CID, 20.0, 0.3)
    st.clear_trail_state(CID)
    assert st.pinned_trail_params(CID) is None
    assert st.is_trail_armed(CID) is False
