"""Tests for the earnings-blackout construction gate (construction.earnings_ok).

A DEBIT held THROUGH an earnings print is an IV-crush loser by construction: IV collapses
post-print and the long premium bleeds even when direction is right. This gate blocks entering
a debit whose holding horizon straddles a KNOWN next-earnings date. Pinned behavior:
  * earnings on/before expiry  -> BLOCK (False, reason)
  * earnings after expiry      -> PASS  (True, "")
  * unknown earnings (None)    -> PASS-but-FAIL-OPEN (True, "") [caller flags 'unchecked']
  * gate disabled              -> no-op PASS (True, "")
  * earnings_blackout_days     -> cushion beyond expiry; boundary is inclusive
  * earnings already past entry-> PASS (can't be held through)
"""
from datetime import date

from exitmgr import construction
from exitmgr.config import ConstructionConfig


def cons(enabled=True, days=0):
    return ConstructionConfig(earnings_blackout_enabled=enabled, earnings_blackout_days=days)


ENTRY = date(2026, 7, 3)
EXPIRY = "20260821"          # 2026-08-21, IBKR YYYYMMDD form
EXPIRY_D = date(2026, 8, 21)


# ------------------------------------------------ earnings BEFORE expiry -> block

def test_earnings_before_expiry_blocks():
    ok, why = construction.earnings_ok(ENTRY, EXPIRY, date(2026, 7, 25), cons())
    assert ok is False
    assert "earnings" in why.lower() and "iv-crush" in why.lower()


def test_earnings_on_expiry_blocks():
    # boundary: earnings ON expiry day is still held-through -> block
    ok, why = construction.earnings_ok(ENTRY, EXPIRY, EXPIRY_D, cons())
    assert ok is False


def test_earnings_string_and_date_expiry_equivalent():
    a = construction.earnings_ok(ENTRY, EXPIRY, date(2026, 7, 25), cons())
    b = construction.earnings_ok(ENTRY, EXPIRY_D, "2026-07-25", cons())
    assert a[0] is False and b[0] is False


# ------------------------------------------------ earnings AFTER expiry -> pass

def test_earnings_after_expiry_passes():
    ok, why = construction.earnings_ok(ENTRY, EXPIRY, date(2026, 9, 1), cons())
    assert ok is True
    assert why == ""


# ------------------------------------------------ unknown earnings -> fail-open pass

def test_unknown_earnings_passes_fail_open():
    ok, why = construction.earnings_ok(ENTRY, EXPIRY, None, cons())
    assert ok is True
    assert why == ""  # caller is responsible for surfacing the 'unchecked' flag


def test_unparseable_earnings_passes_fail_open():
    ok, why = construction.earnings_ok(ENTRY, EXPIRY, "not-a-date", cons())
    assert ok is True
    assert why == ""


# ------------------------------------------------ disabled -> no-op

def test_disabled_is_noop():
    # earnings squarely inside the horizon, but the gate is OFF -> must pass
    ok, why = construction.earnings_ok(ENTRY, EXPIRY, date(2026, 7, 25), cons(enabled=False))
    assert ok is True
    assert why == ""


# ------------------------------------------------ buffer-days boundary

def test_buffer_extends_block_past_expiry():
    # earnings 3 days AFTER expiry; with a 5-day cushion it is still blocked...
    ok, why = construction.earnings_ok(ENTRY, EXPIRY, date(2026, 8, 24), cons(days=5))
    assert ok is False
    # ...but with no cushion (default 0) the same earnings passes (position already expired)
    ok0, _ = construction.earnings_ok(ENTRY, EXPIRY, date(2026, 8, 24), cons(days=0))
    assert ok0 is True


def test_buffer_boundary_inclusive():
    # exactly expiry + buffer days -> inclusive block; one day beyond -> pass
    ok_in, _ = construction.earnings_ok(ENTRY, EXPIRY, date(2026, 8, 26), cons(days=5))   # 21+5
    ok_out, _ = construction.earnings_ok(ENTRY, EXPIRY, date(2026, 8, 27), cons(days=5))  # 21+6
    assert ok_in is False
    assert ok_out is True


# ------------------------------------------------ earnings already past at entry -> pass

def test_earnings_before_entry_passes():
    ok, why = construction.earnings_ok(ENTRY, EXPIRY, date(2026, 7, 1), cons())
    assert ok is True
    assert why == ""
