"""Public API contract; production-derived narrative omitted."""
import math

import pytest

from exitmgr.atr_levels import (atr_giveback, atr_stop_pct, bs_call_delta, describe,
                                net_structure_delta, premium_move_for_atrs)


SYMN = dict(spot=56.0, long_strike=54.0, short_strike=56.0, dte=130, iv=0.25,
           eps=1.0, peak=1.25, atr=1.2)

SYMO = dict(spot=30.0, long_strike=25.0, short_strike=28.0, dte=45, iv=0.55,
          eps=1.5, peak=2.1, atr=1.6)




def test_vertical_delta_is_the_difference_of_the_legs_not_the_long_leg():
    """Public API contract; production-derived narrative omitted."""
    net = net_structure_delta(SYMN["spot"], SYMN["long_strike"], SYMN["short_strike"],
                              SYMN["dte"], SYMN["iv"])
    long_only = net_structure_delta(SYMN["spot"], SYMN["long_strike"], None,
                                    SYMN["dte"], SYMN["iv"])
    assert 0.05 < net < 0.20, net
    assert long_only > 0.6
    assert net < long_only / 3, "a $1-wide vertical must be far less levered than its long leg"


def test_naked_long_has_no_short_leg_to_net():
    d = net_structure_delta(100.0, 95.0, None, 90, 0.30)
    assert 0.5 < d < 1.0


def test_wider_spread_has_more_delta_than_narrower_one():
    narrow = net_structure_delta(56.0, 54.0, 56.0, 130, 0.25)
    wide = net_structure_delta(56.0, 54.0, 66.0, 130, 0.25)
    assert wide > narrow


def test_inverted_or_degenerate_structures_return_none():
    assert net_structure_delta(28.0, 28.0, 28.0, 121, 0.22) is None
    assert net_structure_delta(28.0, 30.0, 27.0, 121, 0.22) is None
    assert net_structure_delta(28.0, 27.0, 28.0, None, 0.22) is None


def test_credit_orientation_is_refused_for_both_rights():
    """Public API contract; production-derived narrative omitted."""

    assert net_structure_delta(28.0, 27.0, 28.0, 121, 0.22, right="C") is not None

    assert net_structure_delta(28.0, 28.0, 27.0, 121, 0.22, right="C") is None

    assert net_structure_delta(100.0, 105.0, 95.0, 90, 0.35, right="P") is not None

    assert net_structure_delta(100.0, 95.0, 105.0, 90, 0.35, right="P") is None


def test_delta_rejects_garbage_rather_than_guessing():
    assert bs_call_delta(-1, 27, 0.3, 0.2) is None
    assert bs_call_delta(28, 0, 0.3, 0.2) is None
    assert bs_call_delta(float("nan"), 27, 0.3, 0.2) is None




def test_stop_never_exceeds_the_configured_cap():
    """Public API contract; production-derived narrative omitted."""
    pct = atr_stop_pct(1.06, atr_abs=99.0, net_delta=0.5, k_atr=3.0, max_stop_pct=30.0)
    assert pct == 30.0


def test_stop_is_floored_so_a_tiny_delta_cannot_produce_a_hair_trigger():
    pct = atr_stop_pct(5.0, atr_abs=0.01, net_delta=0.01, k_atr=1.0,
                       max_stop_pct=30.0, min_stop_pct=8.0)
    assert pct == 8.0


def test_stop_scales_with_k_between_the_bounds():
    kw = dict(entry_per_share=1.0, atr_abs=1.0, net_delta=0.1, max_stop_pct=30.0,
              min_stop_pct=1.0)
    assert atr_stop_pct(k_atr=1.0, **kw) == pytest.approx(10.0)
    assert atr_stop_pct(k_atr=2.0, **kw) == pytest.approx(20.0)
    assert atr_stop_pct(k_atr=4.0, **kw) == 30.0


def test_synthetic_verticals_land_at_or_under_doctrine():
    for p in (SYMN, SYMO):
        net = net_structure_delta(p["spot"], p["long_strike"], p["short_strike"],
                                  p["dte"], p["iv"])
        pct = atr_stop_pct(p["eps"], p["atr"], net, k_atr=2.0, max_stop_pct=30.0)
        assert 0 < pct <= 30.0




def test_giveback_puts_the_floor_k_atrs_below_peak():
    """Public API contract; production-derived narrative omitted."""
    eps, peak, atr, net, k = 0.49, 0.61, 0.62, 0.106, 1.0
    gb = atr_giveback(eps, peak, atr, net, k, gb_min=0.0, gb_max=1.0)
    floor = peak - gb * (peak - eps)
    assert (peak - floor) / (atr * net) == pytest.approx(k, rel=1e-9)


def test_a_big_gain_does_not_hand_most_of_it_back():
    """Public API contract; production-derived narrative omitted."""
    gb = atr_giveback(1.0, 4.0, atr_abs=3.0, net_delta=0.9, k_atr=1.0, gb_max=0.75)
    assert gb == 0.75


def test_a_thin_gain_does_not_produce_a_noise_tight_trail():
    gb = atr_giveback(1.0, 1.02, atr_abs=0.01, net_delta=0.01, k_atr=1.0, gb_min=0.25)
    assert gb == 0.25


def test_no_gain_means_no_trail():
    assert atr_giveback(1.0, 1.0, 0.5, 0.3, 1.0) is None
    assert atr_giveback(1.0, 0.8, 0.5, 0.3, 1.0) is None


def test_fixed_giveback_and_atr_giveback_agree_on_synthetic_case():
    """Public API contract; production-derived narrative omitted."""
    net = net_structure_delta(SYMN["spot"], SYMN["long_strike"], SYMN["short_strike"],
                              SYMN["dte"], SYMN["iv"])
    gb = atr_giveback(SYMN["eps"], SYMN["peak"], SYMN["atr"], net, k_atr=1.0)
    assert 0.4 < gb < 0.7, gb




@pytest.mark.parametrize("bad", [None, 0, -1, float("nan")])
def test_every_entry_point_declines_bad_input_instead_of_inventing(bad):
    assert premium_move_for_atrs(1.0, bad, 0.1) is None
    assert atr_stop_pct(bad, 0.6, 0.1, 1.0, 30.0) is None
    assert atr_giveback(bad, 2.0, 0.6, 0.1, 1.0) is None


def test_describe_reports_levels_in_atrs():
    d = describe(0.49, 0.6083, atr_abs=0.62, net_delta=0.106,
                 stop_pct=30.0, giveback=0.5)
    assert d["stop_atr"] == pytest.approx(2.238, rel=1e-2)
    assert d["trail_atr"] == pytest.approx(0.900, rel=1e-2)


def test_describe_is_silent_when_it_cannot_measure():
    assert describe(0.49, 0.61, atr_abs=0.0, net_delta=0.1,
                    stop_pct=30.0, giveback=0.5) == {}




def test_put_vertical_delta_is_positive_magnitude():
    """Public API contract; production-derived narrative omitted."""
    d = net_structure_delta(100.0, 105.0, 95.0, 90, 0.35, right="P")
    assert d is not None and d > 0


def test_naked_long_put_has_delta_magnitude_under_one():
    d = net_structure_delta(100.0, 105.0, None, 90, 0.35, right="P")
    assert 0.0 < d < 1.0


def test_call_and_put_verticals_of_the_same_strikes_have_equal_magnitude():
    c = net_structure_delta(100.0, 95.0, 105.0, 90, 0.35, right="C")
    p = net_structure_delta(100.0, 105.0, 95.0, 90, 0.35, right="P")
    assert c == pytest.approx(p)


def test_right_defaults_to_call_and_is_case_insensitive():
    a = net_structure_delta(100.0, 95.0, 105.0, 90, 0.35)
    b = net_structure_delta(100.0, 95.0, 105.0, 90, 0.35, right="c")
    assert a == pytest.approx(b)
