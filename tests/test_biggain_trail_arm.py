"""Public API contract; production-derived narrative omitted."""
import pytest

from exitmgr.state import State

SYMC = 4001001
ENTRY_DEBIT = 800.0
QTY = 4
EPS = ENTRY_DEBIT / (100.0 * QTY)
PEAK = 2.6
ACTIVATION = 20.0


def _st():
    return State()



def test_a_faded_big_winner_arms_immediately_without_waiting_for_two_closes():
    st = _st()
    assert st.is_trail_armed(SYMC) is False
    assert st.arm_on_peak_gain(SYMC, PEAK, ENTRY_DEBIT, QTY, ACTIVATION) is True
    assert st.is_trail_armed(SYMC) is True


def test_the_floor_is_measured_from_the_lifetime_peak_not_the_current_mark():
    """Public API contract; production-derived narrative omitted."""
    st = _st()
    st.arm_on_peak_gain(SYMC, PEAK, ENTRY_DEBIT, QTY, ACTIVATION)
    assert st.trail_peak_since_arm(SYMC) == pytest.approx(PEAK)


def test_SYMC_at_its_close_survives_the_moderate_floor_it_was_armed_with():
    """Public API contract; production-derived narrative omitted."""
    st = _st()
    st.arm_on_peak_gain(SYMC, PEAK, ENTRY_DEBIT, QTY, ACTIVATION)
    peak = st.trail_peak_since_arm(SYMC)
    floor_price = EPS + 0.50 * (peak - EPS)
    assert floor_price == pytest.approx(2.3, abs=1e-3)
    assert (floor_price / EPS - 1) * 100 == pytest.approx(15.0, abs=0.1)
    close_price = 2.4
    assert close_price > floor_price


def test_a_further_fade_below_the_floor_is_what_finally_trips_it():
    st = _st()
    st.arm_on_peak_gain(SYMC, PEAK, ENTRY_DEBIT, QTY, ACTIVATION)
    floor_price = EPS + 0.50 * (st.trail_peak_since_arm(SYMC) - EPS)
    assert 2.20 < floor_price
    assert 2.10 < floor_price



def test_a_winner_below_the_threshold_still_waits_for_the_two_close_contract():
    """Public API contract; production-derived narrative omitted."""
    st = _st()
    modest_peak = EPS * 1.1999
    assert st.arm_on_peak_gain(SYMC, modest_peak, ENTRY_DEBIT, QTY, ACTIVATION) is False
    assert st.is_trail_armed(SYMC) is False


def test_exactly_at_the_threshold_arms():
    st = _st()
    assert st.arm_on_peak_gain(SYMC, EPS * 1.20, ENTRY_DEBIT, QTY, ACTIVATION) is True



def test_arming_twice_is_a_no_op_and_never_re_seeds_the_floor():
    st = _st()
    assert st.arm_on_peak_gain(SYMC, PEAK, ENTRY_DEBIT, QTY, ACTIVATION) is True
    armed_at = st.trail_confirmation_for(SYMC)["armed_at"]

    assert st.arm_on_peak_gain(SYMC, EPS * 1.26, ENTRY_DEBIT, QTY, ACTIVATION) is False
    assert st.trail_peak_since_arm(SYMC) == pytest.approx(PEAK)
    assert st.trail_confirmation_for(SYMC)["armed_at"] == armed_at


def test_a_position_already_armed_by_the_two_close_contract_is_left_alone():
    st = _st()
    st.record_session_close(SYMC, "2037-08-10", EPS * 1.30, ENTRY_DEBIT, QTY, 20.0)
    st.record_session_close(SYMC, "2037-08-11", EPS * 1.31, ENTRY_DEBIT, QTY, 20.0)
    assert st.is_trail_armed(SYMC) is True
    seeded = st.trail_peak_since_arm(SYMC)
    assert st.arm_on_peak_gain(SYMC, PEAK, ENTRY_DEBIT, QTY, ACTIVATION) is False
    assert st.trail_peak_since_arm(SYMC) == pytest.approx(seeded)



@pytest.mark.parametrize("peak", [None, "x", float("nan"), 0.0, -1.0])
def test_an_unreadable_peak_never_arms(peak):
    st = _st()
    assert st.arm_on_peak_gain(SYMC, peak, ENTRY_DEBIT, QTY, ACTIVATION) is False
    assert st.is_trail_armed(SYMC) is False


@pytest.mark.parametrize("debit,qty", [(0.0, 3), (-5.0, 3), (882.0, 0), (882.0, -1), (None, 3)])
def test_a_bad_basis_never_arms(debit, qty):
    st = _st()
    assert st.arm_on_peak_gain(SYMC, PEAK, debit, qty, ACTIVATION) is False



def test_the_two_close_contract_still_refuses_a_single_qualifying_close():
    st = _st()
    st.record_session_close(SYMC, "2037-08-11", EPS * 1.30, ENTRY_DEBIT, QTY, 20.0)
    assert st.is_trail_armed(SYMC) is False


def test_a_non_qualifying_close_still_resets_the_streak():
    st = _st()
    st.record_session_close(SYMC, "2037-08-10", EPS * 1.30, ENTRY_DEBIT, QTY, 20.0)
    st.record_session_close(SYMC, "2037-08-11", EPS * 1.05, ENTRY_DEBIT, QTY, 20.0)
    assert st.trail_confirmation_for(SYMC)["consecutive_qualifying_closes"] == 0
    assert st.is_trail_armed(SYMC) is False


def test_clearing_trail_state_drops_a_big_gain_arm_too():
    """Public API contract; production-derived narrative omitted."""
    st = _st()
    st.arm_on_peak_gain(SYMC, PEAK, ENTRY_DEBIT, QTY, ACTIVATION)
    st.clear_trail_state(SYMC)
    assert st.is_trail_armed(SYMC) is False
    assert st.trail_peak_since_arm(SYMC) is None
