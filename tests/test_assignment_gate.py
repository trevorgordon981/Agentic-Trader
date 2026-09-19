"""Public API contract; production-derived narrative omitted."""
from datetime import date

from exitmgr import construction, research
from exitmgr.config import ConstructionConfig


def cons(enabled=True, hard=False, cushion=0):
    return ConstructionConfig(assignment_check_enabled=enabled,
                              assignment_block_hard=hard,
                              assignment_cushion_days=cushion)


EXPIRY = "20260821"
EXPIRY_D = date(2026, 8, 21)
DTE = 30
K = 100.0




def test_itm_short_call_exdiv_before_expiry_warns_by_default():

    ok, why = construction.assignment_risk_ok(K, 105.0, "C", EXPIRY, date(2026, 8, 1), DTE, cons())
    assert ok is True
    assert why != "" and "assignment" in why.lower()


def test_itm_short_call_hard_block():
    ok, why = construction.assignment_risk_ok(
        K, 105.0, "C", EXPIRY, date(2026, 8, 1), DTE, cons(hard=True))
    assert ok is False
    assert "assignment" in why.lower()




def test_itm_short_put_exdiv_before_expiry_warns():

    ok, why = construction.assignment_risk_ok(K, 95.0, "P", EXPIRY, date(2026, 8, 1), DTE, cons())
    assert ok is True
    assert why != ""




def test_otm_short_call_clean_pass():

    ok, why = construction.assignment_risk_ok(K, 95.0, "C", EXPIRY, date(2026, 8, 1), DTE, cons())
    assert ok is True
    assert why == ""


def test_otm_short_put_clean_pass():
    ok, why = construction.assignment_risk_ok(K, 105.0, "P", EXPIRY, date(2026, 8, 1), DTE, cons())
    assert ok is True
    assert why == ""




def test_unknown_exdiv_fail_open():
    ok, why = construction.assignment_risk_ok(K, 105.0, "C", EXPIRY, None, DTE, cons())
    assert ok is True
    assert why == ""


def test_unparseable_exdiv_fail_open():
    ok, why = construction.assignment_risk_ok(K, 105.0, "C", EXPIRY, "not-a-date", DTE, cons())
    assert ok is True
    assert why == ""


def test_missing_spot_fail_open():
    ok, why = construction.assignment_risk_ok(K, 0.0, "C", EXPIRY, date(2026, 8, 1), DTE, cons())
    assert ok is True
    assert why == ""




def test_single_leg_no_short_passes():

    ok, why = construction.assignment_risk_ok(0.0, 105.0, "C", EXPIRY, date(2026, 8, 1), DTE, cons())
    assert ok is True
    assert why == ""




def test_disabled_is_noop():

    ok, why = construction.assignment_risk_ok(
        K, 105.0, "C", EXPIRY, date(2026, 8, 1), DTE, cons(enabled=False, hard=True))
    assert ok is True
    assert why == ""




def test_exdiv_after_expiry_clean_pass():
    ok, why = construction.assignment_risk_ok(K, 105.0, "C", EXPIRY, date(2026, 9, 1), DTE, cons())
    assert ok is True
    assert why == ""




def test_cushion_extends_window_past_expiry():

    ok, why = construction.assignment_risk_ok(K, 105.0, "C", EXPIRY, date(2026, 8, 24), DTE, cons(cushion=5))
    assert ok is True and why != ""

    ok0, why0 = construction.assignment_risk_ok(K, 105.0, "C", EXPIRY, date(2026, 8, 24), DTE, cons(cushion=0))
    assert ok0 is True and why0 == ""


def test_cushion_boundary_inclusive():

    ok_in, why_in = construction.assignment_risk_ok(
        K, 105.0, "C", EXPIRY, date(2026, 8, 26), DTE, cons(cushion=5))
    ok_out, why_out = construction.assignment_risk_ok(
        K, 105.0, "C", EXPIRY, date(2026, 8, 27), DTE, cons(cushion=5))
    assert ok_in is True and why_in != ""
    assert ok_out is True and why_out == ""


def test_cushion_boundary_hard_block():
    ok_in, _ = construction.assignment_risk_ok(
        K, 105.0, "C", EXPIRY, date(2026, 8, 26), DTE, cons(hard=True, cushion=5))
    assert ok_in is False




REF = date(2026, 7, 3)


def test_ex_div_days_future_within_horizon():
    assert research._ex_div_days(date(2026, 7, 20), REF) == 17


def test_ex_div_days_string_and_list():
    assert research._ex_div_days("2026-07-20", REF) == 17

    assert research._ex_div_days([date(2026, 8, 1), date(2026, 7, 20)], REF) == 17


def test_ex_div_days_past_is_none():
    assert research._ex_div_days(date(2026, 6, 1), REF) is None


def test_ex_div_days_none_and_beyond_horizon():
    assert research._ex_div_days(None, REF) is None
    assert research._ex_div_days(date(2027, 1, 1), REF, horizon_days=90) is None


def test_ex_div_days_unparseable_is_none():
    assert research._ex_div_days("garbage", REF) is None
