"""Public API contract; production-derived narrative omitted."""
import pytest

from exitmgr.state import State

SYMC = 4001001
ENTRY_DEBIT = 800.0
QTY = 4
EPS = ENTRY_DEBIT / (100.0 * QTY)
PEAK = 2.6


def _armed():
    """Public API contract; production-derived narrative omitted."""
    st = State()
    st.arm_on_peak_gain(SYMC, PEAK, ENTRY_DEBIT, QTY, 25.0)
    return st



def test_nothing_is_pinned_before_the_first_arming():
    assert State().pinned_trail_params(SYMC) is None


def test_the_first_pin_takes_and_reports_the_transition():
    st = _armed()
    assert st.pin_trail_params(SYMC, 20.0, 0.50) is True
    assert st.pinned_trail_params(SYMC) == {"activation_gain_pct": 20.0,
                                            "giveback_fraction": 0.50}


def test_a_later_pin_is_refused_so_the_floor_cannot_drift():
    """Public API contract; production-derived narrative omitted."""
    st = _armed()
    st.pin_trail_params(SYMC, 20.0, 0.50)
    assert st.pin_trail_params(SYMC, 25.0, 0.40) is False
    assert st.pinned_trail_params(SYMC)["giveback_fraction"] == 0.50
    assert st.pinned_trail_params(SYMC)["activation_gain_pct"] == 20.0


def test_the_model_omitting_params_later_also_changes_nothing():
    """Public API contract; production-derived narrative omitted."""
    st = _armed()
    st.pin_trail_params(SYMC, 20.0, 0.50)
    assert st.pin_trail_params(SYMC, 20.0, 0.40) is False
    assert st.pinned_trail_params(SYMC)["giveback_fraction"] == 0.50



@pytest.mark.parametrize("act,gb", [(None, 0.5), (20.0, None), ("x", 0.5), (20.0, "y"),
                                    (float("nan"), 0.5), (20.0, float("nan"))])
def test_an_unreadable_parameter_never_pins(act, gb):
    st = _armed()
    assert st.pin_trail_params(SYMC, act, gb) is False
    assert st.pinned_trail_params(SYMC) is None


@pytest.mark.parametrize("raw,clamped", [(0.99, 0.9), (0.01, 0.1), (0.5, 0.5)])
def test_the_pinned_giveback_is_clamped_to_the_sane_band(raw, clamped):
    st = _armed()
    st.pin_trail_params(SYMC, 20.0, raw)
    assert st.pinned_trail_params(SYMC)["giveback_fraction"] == pytest.approx(clamped)



def test_closing_the_position_drops_the_pin_so_a_re_entry_starts_clean():
    st = _armed()
    st.pin_trail_params(SYMC, 20.0, 0.50)
    st.clear_trail_state(SYMC)
    assert st.pinned_trail_params(SYMC) is None
    assert st.is_trail_armed(SYMC) is False


def test_the_pin_survives_a_state_round_trip():
    """Public API contract; production-derived narrative omitted."""
    st = _armed()
    st.pin_trail_params(SYMC, 20.0, 0.50)
    blob = st.to_dict() if hasattr(st, "to_dict") else None
    if blob is None:
        pytest.skip("State has no to_dict/from_dict round trip")
    st2 = State.from_dict(blob) if hasattr(State, "from_dict") else None
    if st2 is None:
        pytest.skip("State has no from_dict")
    assert st2.pinned_trail_params(SYMC)["giveback_fraction"] == 0.50



def test_SYMC_keeps_the_moderate_floor_it_was_armed_with():
    """Public API contract; production-derived narrative omitted."""
    st = _armed()
    st.pin_trail_params(SYMC, 20.0, 0.50)
    st.pin_trail_params(SYMC, 25.0, 0.40)
    gb = st.pinned_trail_params(SYMC)["giveback_fraction"]
    floor = EPS + (1 - gb) * (PEAK - EPS)
    assert (floor / EPS - 1) * 100 == pytest.approx(15.0, abs=0.1)
    assert 2.4 > floor
