"""Public API contract; production-derived narrative omitted."""
import pytest

from exitmgr.state import State

CID = 8001002
EPS = 0.49
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
    """Public API contract; production-derived narrative omitted."""
    st = _armed_state()
    st.ratchet_trail_params(CID, 20.0, 0.40)
    assert st.ratchet_trail_params(CID, 20.0, 0.75) is False
    assert st.pinned_trail_params(CID)["giveback_fraction"] == pytest.approx(0.40)


def test_an_unchanged_giveback_is_not_a_ratchet():
    st = _armed_state()
    st.ratchet_trail_params(CID, 20.0, 0.40)
    assert st.ratchet_trail_params(CID, 20.0, 0.40) is False


def test_ratchet_is_monotonic_across_a_whole_run_up():
    """Public API contract; production-derived narrative omitted."""
    st = _armed_state()
    atr_prem = 0.0665
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
    """Public API contract; production-derived narrative omitted."""
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
    """Public API contract; production-derived narrative omitted."""
    st = _armed_state()
    st.ratchet_trail_params(CID, 20.0, 0.3)
    st.clear_trail_state(CID)
    assert st.pinned_trail_params(CID) is None
    assert st.is_trail_armed(CID) is False
