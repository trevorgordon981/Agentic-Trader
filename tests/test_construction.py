"""Public API contract; production-derived narrative omitted."""
from exitmgr import construction
from exitmgr.config import ConstructionConfig


def cons():
    return ConstructionConfig()




def test_long_strike_ok_missing_spot_holds():
    c = cons()
    for bad_spot in (None, 0, 0.0, -1):
        ok, why = construction.long_strike_ok(105.0, bad_spot, "C", 30, 0.30, c)
        assert ok is False, f"missing spot {bad_spot!r} must NOT pass the gate"
        assert why.startswith("INSUFFICIENT_DATA"), why


def test_long_strike_ok_unparseable_holds():
    ok, why = construction.long_strike_ok("N/A", "N/A", "C", 30, 0.30, cons())
    assert ok is False
    assert why.startswith("INSUFFICIENT_DATA")


def test_long_strike_ok_missing_strike_holds():
    ok, why = construction.long_strike_ok(0, 100.0, "C", 30, 0.30, cons())
    assert ok is False
    assert why.startswith("INSUFFICIENT_DATA")


def test_spread_structure_ok_missing_spot_holds():
    c = cons()
    for bad_spot in (None, 0, 0.0, -5):
        ok, why = construction.spread_structure_ok(101.0, 104.0, bad_spot, "C", 30, 0.30, c)
        assert ok is False, f"missing spot {bad_spot!r} must NOT pass the gate"
        assert why.startswith("INSUFFICIENT_DATA"), why


def test_spread_structure_ok_missing_strikes_holds():
    ok, why = construction.spread_structure_ok(0, 104.0, 100.0, "C", 30, 0.30, cons())
    assert ok is False and why.startswith("INSUFFICIENT_DATA")
    ok, why = construction.spread_structure_ok(101.0, 0, 100.0, "C", 30, 0.30, cons())
    assert ok is False and why.startswith("INSUFFICIENT_DATA")


def test_missing_spot_reason_is_not_a_lottery_reason():
    """Public API contract; production-derived narrative omitted."""
    _, why_nodata = construction.long_strike_ok(105.0, None, "C", 30, 0.30, cons())
    _, why_lottery = construction.long_strike_ok(115.0, 100.0, "C", 30, 0.30, cons())

    assert why_nodata.startswith("INSUFFICIENT_DATA")
    assert "lottery-ticket structure" not in why_nodata
    assert not why_lottery.startswith("INSUFFICIENT_DATA")
    assert "lottery-ticket structure" in why_lottery




def test_long_strike_ok_itm_leg_passes():

    ok, why = construction.long_strike_ok(95.0, 100.0, "C", 30, 0.30, cons())
    assert ok is True and why == ""


def test_long_strike_ok_within_expected_move_passes():

    ok, why = construction.long_strike_ok(105.0, 100.0, "C", 30, 0.30, cons())
    assert ok is True and why == ""


def test_long_strike_ok_lottery_leg_rejected_with_iv():

    ok, why = construction.long_strike_ok(115.0, 100.0, "C", 30, 0.30, cons())
    assert ok is False
    assert "lottery" in why and "INSUFFICIENT_DATA" not in why


def test_spread_structure_ok_short_within_move_passes():
    ok, why = construction.spread_structure_ok(101.0, 105.0, 100.0, "C", 30, 0.30, cons())
    assert ok is True and why == ""


def test_spread_structure_ok_far_short_rejected_with_iv():
    ok, why = construction.spread_structure_ok(101.0, 115.0, 100.0, "C", 30, 0.30, cons())
    assert ok is False
    assert "expected move" in why and "INSUFFICIENT_DATA" not in why




def test_long_strike_ok_no_iv_near_spot_passes():

    ok, why = construction.long_strike_ok(102.0, 100.0, "C", 30, None, cons())
    assert ok is True and why == ""


def test_long_strike_ok_no_iv_far_rejected_conservative():

    ok, why = construction.long_strike_ok(105.0, 100.0, "C", 30, None, cons())
    assert ok is False
    assert "conservative" in why and "INSUFFICIENT_DATA" not in why


def test_spread_structure_ok_no_iv_tight_passes():

    ok, why = construction.spread_structure_ok(101.0, 104.0, 100.0, "C", 30, None, cons())
    assert ok is True and why == ""


def test_spread_structure_ok_no_iv_wide_rejected():

    ok, why = construction.spread_structure_ok(101.0, 115.0, 100.0, "C", 30, None, cons())
    assert ok is False
    assert "conservative" in why and "INSUFFICIENT_DATA" not in why


def test_spread_structure_ok_no_iv_long_leg_too_far_rejected():

    ok, why = construction.spread_structure_ok(110.0, 112.0, 100.0, "C", 30, None, cons())
    assert ok is False
    assert "conservative" in why and "INSUFFICIENT_DATA" not in why











import datetime as _dt

import pytest


TODAY = _dt.date(2026, 7, 26)


def _exp(dte):
    """Public API contract; production-derived narrative omitted."""
    return (TODAY + _dt.timedelta(days=dte)).strftime("%Y%m%d")


def _chain(*dtes):
    return [_exp(d) for d in dtes]



FULL_CHAIN = _chain(1, 3, 7, 14, 21, 25, 30, 45, 46, 60, 90, 170, 365, 497, 638, 795, 800)


class _Cons:
    """Public API contract; production-derived narrative omitted."""

    def __init__(self, **kw):
        self.min_dte = 25
        self.prefer_dte_max = 800
        self.credit_min_dte = 3
        self.credit_max_dte = 45
        self.__dict__.update(kw)




def test_normalize_side_only_exact_credit_is_credit():
    assert construction.normalize_side("credit") == "credit"
    assert construction.normalize_side("CREDIT") == "credit"
    assert construction.normalize_side("  Credit  ") == "credit"


@pytest.mark.parametrize("side", [
    None, "", "debit", "DEBIT", "Debit", "csp", "cash secured put", "credits", "creditt",
    "cred", "short put", 0, 1, [], {}, object(),
])
def test_normalize_side_everything_else_is_debit(side):
    """Public API contract; production-derived narrative omitted."""
    assert construction.normalize_side(side) == "debit"




def test_debit_bounds_are_the_long_premium_doctrine():
    b = construction.dte_bounds_for_side("debit", _Cons())
    assert b.min_dte == 25
    assert b.prefer_dte_max == 800
    assert b.max_dte is None


def test_credit_bounds_are_the_write_window():
    b = construction.dte_bounds_for_side("credit", _Cons())
    assert (b.min_dte, b.prefer_dte_max, b.max_dte) == (3, 45, 45)


def test_debit_bounds_ignore_the_credit_keys_entirely():
    """Public API contract; production-derived narrative omitted."""
    for credit_floor in (1, 3, 5, 7):
        b = construction.dte_bounds_for_side("debit", _Cons(credit_min_dte=credit_floor))
        assert b.min_dte == 25, f"credit_min_dte={credit_floor} leaked into the debit floor"


def test_credit_bounds_fall_back_to_spec_defaults_without_config_keys():
    """Public API contract; production-derived narrative omitted."""
    from exitmgr.config import ConstructionConfig
    b = construction.dte_bounds_for_side("credit", ConstructionConfig())
    assert (b.min_dte, b.prefer_dte_max, b.max_dte) == (3, 45, 45)
    b_none = construction.dte_bounds_for_side("credit", None)
    assert (b_none.min_dte, b_none.max_dte) == (3, 45)


def test_credit_bounds_honour_config_when_present():
    """Public API contract; production-derived narrative omitted."""
    b = construction.dte_bounds_for_side("credit", _Cons(credit_min_dte=7, credit_max_dte=30))
    assert (b.min_dte, b.prefer_dte_max, b.max_dte) == (7, 30, 30)


def test_bounds_fail_safe_on_garbage_config():

    for bad in (0, -5, None, "abc"):
        assert construction.dte_bounds_for_side("debit", _Cons(min_dte=bad)).min_dte == 25

    b = construction.dte_bounds_for_side("credit", _Cons(credit_min_dte=45, credit_max_dte=3))
    assert b.max_dte is not None and b.max_dte == b.min_dte == 45

    assert construction.dte_bounds_for_side("credit", _Cons(credit_min_dte=0)).min_dte == 1




@pytest.mark.parametrize("target,expected", [(3, 3), (4, 3), (6, 7), (7, 7)])
def test_credit_idea_reaches_3_to_7_dte(target, expected):
    exp, dte, adjusted = construction.pick_expiry_for_side(
        FULL_CHAIN, target, "credit", _Cons(), today=TODAY)
    assert dte == expected, f"credit target {target} -> {dte} DTE, expected {expected}"
    assert exp == _exp(expected)


def test_credit_idea_at_exactly_the_floor_is_not_adjusted():
    exp, dte, adjusted = construction.pick_expiry_for_side(
        FULL_CHAIN, 3, "credit", _Cons(), today=TODAY)
    assert (dte, adjusted) == (3, False)


def test_credit_idea_below_the_floor_is_lifted_to_3():
    exp, dte, adjusted = construction.pick_expiry_for_side(
        FULL_CHAIN, 1, "credit", _Cons(), today=TODAY)
    assert dte == 3 and adjusted is True


def test_credit_idea_capped_at_45():
    """Public API contract; production-derived narrative omitted."""
    for target in (46, 60, 90, 365, 800):
        exp, dte, adjusted = construction.pick_expiry_for_side(
            FULL_CHAIN, target, "credit", _Cons(), today=TODAY)
        assert dte == 45, f"credit target {target} -> {dte} DTE (ceiling breached)"
        assert adjusted is True


def test_credit_idea_at_exactly_45_is_allowed_and_unadjusted():
    exp, dte, adjusted = construction.pick_expiry_for_side(
        FULL_CHAIN, 45, "credit", _Cons(), today=TODAY)
    assert (dte, adjusted) == (45, False)


def test_credit_ceiling_is_hard_not_nearest():
    """Public API contract; production-derived narrative omitted."""
    exp, dte, adjusted = construction.pick_expiry_for_side(
        _chain(60, 90, 365), 30, "credit", _Cons(), today=TODAY)
    assert (exp, dte) == (None, None), "a 60-DTE cash-secured put is out of doctrine"


def test_credit_refused_when_only_sub_floor_expiries_exist():
    exp, dte, _ = construction.pick_expiry_for_side(
        _chain(0, 1, 2), 3, "credit", _Cons(), today=TODAY)
    assert (exp, dte) == (None, None)






@pytest.mark.parametrize("target", [0, 1, 2, 3, 5, 7, 10, 14, 21, 24, -1, None, "3", "junk"])
def test_debit_idea_asking_short_dated_is_floored_to_min_dte(target):
    """Public API contract; production-derived narrative omitted."""
    exp, dte, adjusted = construction.pick_expiry_for_side(
        FULL_CHAIN, target, "debit", _Cons(), today=TODAY)
    assert dte is not None
    assert dte >= 25, f"debit target {target!r} selected a {dte}-DTE expiry -- theta-bleed hole"
    assert exp not in (_exp(1), _exp(3), _exp(7), _exp(14), _exp(21))


def test_debit_idea_asking_3_dte_gets_25():
    """Public API contract; production-derived narrative omitted."""
    exp, dte, adjusted = construction.pick_expiry_for_side(
        FULL_CHAIN, 3, "debit", _Cons(), today=TODAY)
    assert (dte, adjusted) == (25, True)
    assert exp == _exp(25)


@pytest.mark.parametrize("side", [None, "", "debit", "DEBIT", "csp", "cash secured put",
                                  "creditt", "short put", 0, object()])
def test_no_side_but_credit_can_reach_a_sub_25_expiry(side):
    """Public API contract; production-derived narrative omitted."""
    _, dte, _ = construction.pick_expiry_for_side(FULL_CHAIN, 3, side, _Cons(), today=TODAY)
    assert dte >= 25, f"side={side!r} reached {dte} DTE"


def test_debit_floor_holds_even_when_credit_floor_is_1():
    _, dte, _ = construction.pick_expiry_for_side(
        FULL_CHAIN, 1, "debit", _Cons(credit_min_dte=1, credit_max_dte=800), today=TODAY)
    assert dte >= 25


def test_pick_expiry_for_side_has_no_floor_argument_to_abuse():
    """Public API contract; production-derived narrative omitted."""
    import inspect
    params = list(inspect.signature(construction.pick_expiry_for_side).parameters)
    assert params == ["expirations", "target_dte", "side", "cons", "today"]
    for forbidden in ("min_dte", "max_dte", "prefer_dte_max", "floor", "ceiling"):
        assert forbidden not in params


def test_debit_never_selects_sub_floor_across_the_whole_argument_space():
    """Public API contract; production-derived narrative omitted."""
    sides = [None, "", "debit", "Debit", "DEBIT", "csp", "credits", "creditt", 0, object()]
    targets = [None, -10, 0, 1, 2, 3, 5, 7, 10, 20, 24, 25, 45, 365, 800, 5000, "3", "junk"]
    chains = [FULL_CHAIN, _chain(1, 2, 3, 25, 30), _chain(3, 7, 30, 60, 365)]
    configs = [_Cons(), _Cons(credit_min_dte=1), _Cons(credit_max_dte=800),
               _Cons(credit_min_dte=1, credit_max_dte=900)]
    for side in sides:
        for target in targets:
            for chain in chains:
                for cons_obj in configs:
                    _, dte, _ = construction.pick_expiry_for_side(
                        chain, target, side, cons_obj, today=TODAY)
                    if dte is not None:
                        assert dte >= cons_obj.min_dte, (
                            f"side={side!r} target={target!r} chain={chain} -> {dte} DTE")


def test_negative_control_credit_side_does_reach_short_dte():
    """Public API contract; production-derived narrative omitted."""
    for chain in (FULL_CHAIN, _chain(1, 2, 3, 25, 30), _chain(3, 7, 30, 60, 365)):
        _, dte, _ = construction.pick_expiry_for_side(chain, 3, "credit", _Cons(), today=TODAY)
        assert dte is not None and dte < 25, (
            f"chain {chain} has no sub-25 expiry -- the debit-floor assertions would be vacuous")


def test_negative_control_primitive_would_permit_the_hole():
    """Public API contract; production-derived narrative omitted."""
    _, dte, _ = construction.pick_expiry(FULL_CHAIN, 3, min_dte=3, prefer_dte_max=45, today=TODAY)
    assert dte == 3




@pytest.mark.parametrize("target", [365, 497, 638, 795])
def test_long_dated_debit_is_not_collapsed_to_170(target):
    """Public API contract; production-derived narrative omitted."""
    _, dte, adjusted = construction.pick_expiry_for_side(
        FULL_CHAIN, target, "debit", _Cons(), today=TODAY)
    assert dte == target, f"{target}-DTE request landed at {dte} DTE"
    assert adjusted is False


@pytest.mark.parametrize("target", [365, 497, 638, 795])
def test_negative_control_old_170_ceiling_did_collapse(target):
    """Public API contract; production-derived narrative omitted."""
    _, dte, adjusted = construction.pick_expiry_for_side(
        FULL_CHAIN, target, "debit", _Cons(prefer_dte_max=170), today=TODAY)
    assert dte == 170 and adjusted is True


def test_boundary_debit_at_exactly_25_and_800():
    _, dte, adjusted = construction.pick_expiry_for_side(
        FULL_CHAIN, 25, "debit", _Cons(), today=TODAY)
    assert (dte, adjusted) == (25, False)
    _, dte, adjusted = construction.pick_expiry_for_side(
        FULL_CHAIN, 800, "debit", _Cons(), today=TODAY)
    assert (dte, adjusted) == (800, False)


def test_debit_has_no_hard_ceiling_beyond_prefer_dte_max():
    """Public API contract; production-derived narrative omitted."""
    _, dte, _ = construction.pick_expiry_for_side(_chain(900, 1000), 365, "debit", _Cons(),
                                                  today=TODAY)
    assert dte == 900




def test_pick_expiry_legacy_positional_signature_unchanged():
    """Public API contract; production-derived narrative omitted."""
    exp, dte, adjusted = construction.pick_expiry(FULL_CHAIN, 10, 25, 45, TODAY)
    assert (dte, adjusted) == (25, True)
    exp, dte, adjusted = construction.pick_expiry(FULL_CHAIN, 30, 25, 45, TODAY)
    assert (dte, adjusted) == (30, False)


def test_pick_expiry_defaults_and_reject_unchanged():
    assert construction.pick_expiry([], 30, today=TODAY) == (None, None, False)
    assert construction.pick_expiry(_chain(1, 5, 10), 30, today=TODAY) == (None, None, False)
    assert construction.pick_expiry(["garbage", None, _exp(30)], 30, today=TODAY)[1] == 30


def test_pick_expiry_max_dte_defaults_to_no_ceiling():
    """Public API contract; production-derived narrative omitted."""
    assert construction.pick_expiry(FULL_CHAIN, 365, 25, 800, TODAY)[1] == 365
    assert construction.pick_expiry(FULL_CHAIN, 365, 25, 800, TODAY, None)[1] == 365


def test_pick_expiry_garbage_max_dte_refuses_rather_than_dropping_the_ceiling():
    exp, dte, _ = construction.pick_expiry(FULL_CHAIN, 30, 3, 45, TODAY, max_dte="junk")
    assert (exp, dte) == (None, None)
