"""Tests for the early-assignment / ex-dividend risk gate (construction.assignment_risk_ok)
plus the pure ex-div lookup core (research._ex_div_days).

A DEBIT SPREAD whose ITM short leg heads into an ex-dividend date carries early-assignment
risk: a counterparty exercises the ITM short (classically a short CALL) to capture the
dividend, so you get assigned, forfeit the dividend, and the spread converts early. This is
MANAGEABLE (not a guaranteed loss), so the DEFAULT disposition is WARN -- (True, reason) to
surface the risk -- and only a hard block (False, reason) when assignment_block_hard is set.
Pinned behavior:
  * ITM short leg + ex-div on/before expiry, default cfg -> WARN  (True, non-empty reason)
  * ...same with assignment_block_hard=True              -> BLOCK (False, reason)
  * OTM short leg                                        -> clean PASS (True, "")
  * unknown ex-div (None) / missing spot                 -> FAIL-OPEN PASS (True, "")
  * single long leg (short_strike<=0)                    -> PASS (True, "")  [nothing to assign]
  * gate disabled                                        -> no-op PASS (True, "")
  * ex-div after expiry (+cushion)                       -> clean PASS (True, "")
  * assignment_cushion_days                              -> cushion beyond expiry; inclusive
No IBKR / no yfinance: the gate is pure, and the research core is fed values directly.
"""
from datetime import date

from exitmgr import construction, research
from exitmgr.config import ConstructionConfig


def cons(enabled=True, hard=False, cushion=0):
    return ConstructionConfig(assignment_check_enabled=enabled,
                              assignment_block_hard=hard,
                              assignment_cushion_days=cushion)


EXPIRY = "20260821"          # 2026-08-21, IBKR YYYYMMDD form
EXPIRY_D = date(2026, 8, 21)
DTE = 30
K = 100.0                    # short-leg strike


# ------------------------------------------------ ITM short CALL + ex-div before expiry -> WARN

def test_itm_short_call_exdiv_before_expiry_warns_by_default():
    # spot >= short_strike => ITM short call; ex-div 2026-08-01 is on/before expiry.
    ok, why = construction.assignment_risk_ok(K, 105.0, "C", EXPIRY, date(2026, 8, 1), DTE, cons())
    assert ok is True                     # WARN disposition: PASSES...
    assert why != "" and "assignment" in why.lower()   # ...but SURFACES the risk


def test_itm_short_call_hard_block():
    ok, why = construction.assignment_risk_ok(
        K, 105.0, "C", EXPIRY, date(2026, 8, 1), DTE, cons(hard=True))
    assert ok is False
    assert "assignment" in why.lower()


# ------------------------------------------------ ITM short PUT -> WARN

def test_itm_short_put_exdiv_before_expiry_warns():
    # short put ITM when spot <= short_strike
    ok, why = construction.assignment_risk_ok(K, 95.0, "P", EXPIRY, date(2026, 8, 1), DTE, cons())
    assert ok is True
    assert why != ""


# ------------------------------------------------ OTM short leg -> clean pass

def test_otm_short_call_clean_pass():
    # spot < short_strike => OTM short call: not worth exercising for the dividend
    ok, why = construction.assignment_risk_ok(K, 95.0, "C", EXPIRY, date(2026, 8, 1), DTE, cons())
    assert ok is True
    assert why == ""


def test_otm_short_put_clean_pass():
    ok, why = construction.assignment_risk_ok(K, 105.0, "P", EXPIRY, date(2026, 8, 1), DTE, cons())
    assert ok is True
    assert why == ""


# ------------------------------------------------ unknown ex-div / missing spot -> fail-open

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


# ------------------------------------------------ single long leg (no short) -> pass

def test_single_leg_no_short_passes():
    # short_strike <= 0 means a single long option: nothing can be assigned
    ok, why = construction.assignment_risk_ok(0.0, 105.0, "C", EXPIRY, date(2026, 8, 1), DTE, cons())
    assert ok is True
    assert why == ""


# ------------------------------------------------ disabled -> no-op

def test_disabled_is_noop():
    # ITM short call into ex-div, but the gate is OFF -> must pass clean even in hard mode
    ok, why = construction.assignment_risk_ok(
        K, 105.0, "C", EXPIRY, date(2026, 8, 1), DTE, cons(enabled=False, hard=True))
    assert ok is True
    assert why == ""


# ------------------------------------------------ ex-div after expiry -> clean pass

def test_exdiv_after_expiry_clean_pass():
    ok, why = construction.assignment_risk_ok(K, 105.0, "C", EXPIRY, date(2026, 9, 1), DTE, cons())
    assert ok is True
    assert why == ""


# ------------------------------------------------ cushion boundary

def test_cushion_extends_window_past_expiry():
    # ex-div 3 days AFTER expiry: with a 5-day cushion it is still at-risk (warn)...
    ok, why = construction.assignment_risk_ok(K, 105.0, "C", EXPIRY, date(2026, 8, 24), DTE, cons(cushion=5))
    assert ok is True and why != ""
    # ...but with no cushion (default 0) the same ex-div passes clean (we are already out)
    ok0, why0 = construction.assignment_risk_ok(K, 105.0, "C", EXPIRY, date(2026, 8, 24), DTE, cons(cushion=0))
    assert ok0 is True and why0 == ""


def test_cushion_boundary_inclusive():
    # exactly expiry + cushion -> inclusive at-risk (warn); one day beyond -> clean pass
    ok_in, why_in = construction.assignment_risk_ok(
        K, 105.0, "C", EXPIRY, date(2026, 8, 26), DTE, cons(cushion=5))   # 21+5
    ok_out, why_out = construction.assignment_risk_ok(
        K, 105.0, "C", EXPIRY, date(2026, 8, 27), DTE, cons(cushion=5))   # 21+6
    assert ok_in is True and why_in != ""
    assert ok_out is True and why_out == ""


def test_cushion_boundary_hard_block():
    ok_in, _ = construction.assignment_risk_ok(
        K, 105.0, "C", EXPIRY, date(2026, 8, 26), DTE, cons(hard=True, cushion=5))
    assert ok_in is False


# ------------------------------------------------ pure ex-div lookup core (research._ex_div_days)

REF = date(2026, 7, 3)


def test_ex_div_days_future_within_horizon():
    assert research._ex_div_days(date(2026, 7, 20), REF) == 17


def test_ex_div_days_string_and_list():
    assert research._ex_div_days("2026-07-20", REF) == 17
    # a list picks the nearest FUTURE date
    assert research._ex_div_days([date(2026, 8, 1), date(2026, 7, 20)], REF) == 17


def test_ex_div_days_past_is_none():
    assert research._ex_div_days(date(2026, 6, 1), REF) is None


def test_ex_div_days_none_and_beyond_horizon():
    assert research._ex_div_days(None, REF) is None
    assert research._ex_div_days(date(2027, 1, 1), REF, horizon_days=90) is None


def test_ex_div_days_unparseable_is_none():
    assert research._ex_div_days("garbage", REF) is None
