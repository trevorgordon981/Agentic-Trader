"""Tests for exitmgr.construction structure-sanity gates.

Focus of this file: the P2 audit fix -- long_strike_ok / spread_structure_ok must NOT
silent-pass ((True, "")) when spot is unavailable. Missing spot now fails SAFE:
(False, "INSUFFICIENT_DATA: ...") so a data gap HOLDS the trade (callers treat `not ok`
as skip/hold). Also pins the unchanged data-present behavior and the missing-IV/present-spot
conservative fallback so the fix can't silently regress them.
"""
from exitmgr import construction
from exitmgr.config import ConstructionConfig


def cons():
    return ConstructionConfig()  # defaults: strike_near_spot_pct=0.03, spread_width_max_pct=0.08


# ------------------------------------------------ missing spot -> fail SAFE (not silent pass)

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
    """The missing-data reason must be DISTINGUISHABLE from a real structural rejection,
    so the caller (and audit log) can tell 'no data' apart from 'lottery ticket'."""
    _, why_nodata = construction.long_strike_ok(105.0, None, "C", 30, 0.30, cons())
    _, why_lottery = construction.long_strike_ok(115.0, 100.0, "C", 30, 0.30, cons())
    # no-data reason carries the INSUFFICIENT_DATA prefix; the real structural reject does not
    assert why_nodata.startswith("INSUFFICIENT_DATA")
    assert "lottery-ticket structure" not in why_nodata
    assert not why_lottery.startswith("INSUFFICIENT_DATA")
    assert "lottery-ticket structure" in why_lottery


# ------------------------------------------------ data present -> behavior UNCHANGED

def test_long_strike_ok_itm_leg_passes():
    # long call strike below spot = ITM/ATM, always fine
    ok, why = construction.long_strike_ok(95.0, 100.0, "C", 30, 0.30, cons())
    assert ok is True and why == ""


def test_long_strike_ok_within_expected_move_passes():
    # spot 100, iv 0.30, 30dte -> EM ~= $8.6; a 5-OTM long leg is inside it
    ok, why = construction.long_strike_ok(105.0, 100.0, "C", 30, 0.30, cons())
    assert ok is True and why == ""


def test_long_strike_ok_lottery_leg_rejected_with_iv():
    # 15-OTM long leg > ~1 expected move -> lottery ticket, rejected (present-data reject)
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


# ------------------------------ missing IV but present spot -> conservative fallback intact

def test_long_strike_ok_no_iv_near_spot_passes():
    # spot present, IV None -> fallback = within 3% of spot; 2-OTM passes
    ok, why = construction.long_strike_ok(102.0, 100.0, "C", 30, None, cons())
    assert ok is True and why == ""


def test_long_strike_ok_no_iv_far_rejected_conservative():
    # 5-OTM > 3% of spot with no IV -> conservative reject (fallback still binds)
    ok, why = construction.long_strike_ok(105.0, 100.0, "C", 30, None, cons())
    assert ok is False
    assert "conservative" in why and "INSUFFICIENT_DATA" not in why


def test_spread_structure_ok_no_iv_tight_passes():
    # width 3 (<=8% of spot) AND long strike within 3% -> conservative fallback passes
    ok, why = construction.spread_structure_ok(101.0, 104.0, 100.0, "C", 30, None, cons())
    assert ok is True and why == ""


def test_spread_structure_ok_no_iv_wide_rejected():
    # width 14 > 8% of spot with no IV -> conservative reject
    ok, why = construction.spread_structure_ok(101.0, 115.0, 100.0, "C", 30, None, cons())
    assert ok is False
    assert "conservative" in why and "INSUFFICIENT_DATA" not in why


def test_spread_structure_ok_no_iv_long_leg_too_far_rejected():
    # width small but long strike 10 away (>3% of spot) with no IV -> conservative reject
    ok, why = construction.spread_structure_ok(110.0, 112.0, 100.0, "C", 30, None, cons())
    assert ok is False
    assert "conservative" in why and "INSUFFICIENT_DATA" not in why
