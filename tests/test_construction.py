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


# =====================================================================================
# STRUCTURE-AWARE DTE FLOOR (2026-07-26; CREDIT_PATH_SPEC.md S3, SOL_AUDIT_R3.md S4:117-125)
#
# THE POINT OF THIS BLOCK: `min_dte: 3` applied globally would enable weekly cash-secured puts
# AND license a 3-DTE LONG CALL -- maximum theta bleed, the -$1,521 failure mode the 365-DTE
# doctrine exists to abolish. The floor is therefore SPLIT by side, and these tests exist to
# prove the credit floor is UNREACHABLE from the debit path by any argument combination.
# =====================================================================================

import datetime as _dt

import pytest


TODAY = _dt.date(2026, 7, 26)


def _exp(dte):
    """IBKR-style expiry string `dte` days after the pinned TODAY."""
    return (TODAY + _dt.timedelta(days=dte)).strftime("%Y%m%d")


def _chain(*dtes):
    return [_exp(d) for d in dtes]


# A realistic chain: weeklies, monthlies, quarterlies and LEAPs.
FULL_CHAIN = _chain(1, 3, 7, 14, 21, 25, 30, 45, 46, 60, 90, 170, 365, 497, 638, 795, 800)


class _Cons:
    """Minimal stand-in for ConstructionConfig (only the DTE keys matter here).

    Deliberately NOT ConstructionConfig: as of 2026-07-26 that dataclass does not yet carry
    credit_min_dte/credit_max_dte (exitmgr/config.py is owned elsewhere), and construction.py
    reads them defensively via getattr. These tests must pin the behaviour BOTH ways -- see
    test_credit_bounds_fall_back_to_spec_defaults_without_config_keys.
    """

    def __init__(self, **kw):
        self.min_dte = 25
        self.prefer_dte_max = 800
        self.credit_min_dte = 3
        self.credit_max_dte = 45
        self.__dict__.update(kw)


# --------------------------------------------------- side normalization fails SAFE to debit

def test_normalize_side_only_exact_credit_is_credit():
    assert construction.normalize_side("credit") == "credit"
    assert construction.normalize_side("CREDIT") == "credit"
    assert construction.normalize_side("  Credit  ") == "credit"


@pytest.mark.parametrize("side", [
    None, "", "debit", "DEBIT", "Debit", "csp", "cash secured put", "credits", "creditt",
    "cred", "short put", 0, 1, [], {}, object(),
])
def test_normalize_side_everything_else_is_debit(side):
    """A mangled/absent side must land in the STRICT long-premium doctrine, never the 3-DTE
    credit window. This asymmetry is the whole safety property."""
    assert construction.normalize_side(side) == "debit"


# --------------------------------------------------------------- bounds resolution by side

def test_debit_bounds_are_the_long_premium_doctrine():
    b = construction.dte_bounds_for_side("debit", _Cons())
    assert b.min_dte == 25
    assert b.prefer_dte_max == 800
    assert b.max_dte is None  # soft ceiling only -- the long-dated clamp fix must not become hard


def test_credit_bounds_are_the_write_window():
    b = construction.dte_bounds_for_side("credit", _Cons())
    assert (b.min_dte, b.prefer_dte_max, b.max_dte) == (3, 45, 45)  # ceiling is HARD for credit


def test_debit_bounds_ignore_the_credit_keys_entirely():
    """THE STRUCTURAL GUARANTEE: credit_min_dte is not an input to the debit doctrine.
    Even set to 1, the debit floor stays at min_dte."""
    for credit_floor in (1, 3, 5, 7):
        b = construction.dte_bounds_for_side("debit", _Cons(credit_min_dte=credit_floor))
        assert b.min_dte == 25, f"credit_min_dte={credit_floor} leaked into the debit floor"


def test_credit_bounds_fall_back_to_spec_defaults_without_config_keys():
    """ConstructionConfig does not yet carry the credit keys; construction.py must still
    produce the spec window (3-45) rather than crash or inherit the debit floor."""
    from exitmgr.config import ConstructionConfig
    b = construction.dte_bounds_for_side("credit", ConstructionConfig())
    assert (b.min_dte, b.prefer_dte_max, b.max_dte) == (3, 45, 45)
    b_none = construction.dte_bounds_for_side("credit", None)
    assert (b_none.min_dte, b_none.max_dte) == (3, 45)


def test_credit_bounds_honour_config_when_present():
    """Negative control for the test above: the numbers are READ, not hardcoded."""
    b = construction.dte_bounds_for_side("credit", _Cons(credit_min_dte=7, credit_max_dte=30))
    assert (b.min_dte, b.prefer_dte_max, b.max_dte) == (7, 30, 30)


def test_bounds_fail_safe_on_garbage_config():
    # a 0/negative/unparseable debit floor is a typo, never a licence for 3-DTE longs
    for bad in (0, -5, None, "abc"):
        assert construction.dte_bounds_for_side("debit", _Cons(min_dte=bad)).min_dte == 25
    # inverted credit window degenerates to a single-day window, never an OPEN ceiling
    b = construction.dte_bounds_for_side("credit", _Cons(credit_min_dte=45, credit_max_dte=3))
    assert b.max_dte is not None and b.max_dte == b.min_dte == 45
    # a 0/negative credit floor still cannot select a same-day expiry
    assert construction.dte_bounds_for_side("credit", _Cons(credit_min_dte=0)).min_dte == 1


# ------------------------------------------------------- credit ideas reach the weekly window

@pytest.mark.parametrize("target,expected", [(3, 3), (4, 3), (6, 7), (7, 7)])
def test_credit_idea_reaches_3_to_7_dte(target, expected):
    exp, dte, adjusted = construction.pick_expiry_for_side(
        FULL_CHAIN, target, "credit", _Cons(), today=TODAY)
    assert dte == expected, f"credit target {target} -> {dte} DTE, expected {expected}"
    assert exp == _exp(expected)


def test_credit_idea_at_exactly_the_floor_is_not_adjusted():
    exp, dte, adjusted = construction.pick_expiry_for_side(
        FULL_CHAIN, 3, "credit", _Cons(), today=TODAY)
    assert (dte, adjusted) == (3, False)  # boundary: 3 is inside the window, not clamped


def test_credit_idea_below_the_floor_is_lifted_to_3():
    exp, dte, adjusted = construction.pick_expiry_for_side(
        FULL_CHAIN, 1, "credit", _Cons(), today=TODAY)
    assert dte == 3 and adjusted is True  # the 1-DTE expiry exists in the chain and is refused


def test_credit_idea_capped_at_45():
    """A model asking for a 90/365-DTE write is pulled back to the 45-DTE ceiling."""
    for target in (46, 60, 90, 365, 800):
        exp, dte, adjusted = construction.pick_expiry_for_side(
            FULL_CHAIN, target, "credit", _Cons(), today=TODAY)
        assert dte == 45, f"credit target {target} -> {dte} DTE (ceiling breached)"
        assert adjusted is True


def test_credit_idea_at_exactly_45_is_allowed_and_unadjusted():
    exp, dte, adjusted = construction.pick_expiry_for_side(
        FULL_CHAIN, 45, "credit", _Cons(), today=TODAY)
    assert (dte, adjusted) == (45, False)  # boundary: 45 is INSIDE the window


def test_credit_ceiling_is_hard_not_nearest():
    """The 45 ceiling is a candidate FILTER, not a target clamp: with only out-of-window
    expiries available the write is REFUSED, never rounded up to 60 DTE."""
    exp, dte, adjusted = construction.pick_expiry_for_side(
        _chain(60, 90, 365), 30, "credit", _Cons(), today=TODAY)
    assert (exp, dte) == (None, None), "a 60-DTE cash-secured put is out of doctrine"


def test_credit_refused_when_only_sub_floor_expiries_exist():
    exp, dte, _ = construction.pick_expiry_for_side(
        _chain(0, 1, 2), 3, "credit", _Cons(), today=TODAY)
    assert (exp, dte) == (None, None)


# ============================================================================================
# THE CRITICAL TEST: a debit idea can NEVER reach the credit floor.
# ============================================================================================

@pytest.mark.parametrize("target", [0, 1, 2, 3, 5, 7, 10, 14, 21, 24, -1, None, "3", "junk"])
def test_debit_idea_asking_short_dated_is_floored_to_min_dte(target):
    """A 3-DTE LONG CALL IS THE BUG THIS ENTIRE CHANGE EXISTS TO PREVENT. Whatever a debit
    idea asks for, the selected expiry is >= 25 DTE."""
    exp, dte, adjusted = construction.pick_expiry_for_side(
        FULL_CHAIN, target, "debit", _Cons(), today=TODAY)
    assert dte is not None
    assert dte >= 25, f"debit target {target!r} selected a {dte}-DTE expiry -- theta-bleed hole"
    assert exp not in (_exp(1), _exp(3), _exp(7), _exp(14), _exp(21))


def test_debit_idea_asking_3_dte_gets_25():
    """The named case from the spec, pinned exactly."""
    exp, dte, adjusted = construction.pick_expiry_for_side(
        FULL_CHAIN, 3, "debit", _Cons(), today=TODAY)
    assert (dte, adjusted) == (25, True)
    assert exp == _exp(25)


@pytest.mark.parametrize("side", [None, "", "debit", "DEBIT", "csp", "cash secured put",
                                  "creditt", "short put", 0, object()])
def test_no_side_but_credit_can_reach_a_sub_25_expiry(side):
    """Every side token EXCEPT exactly "credit" gets the strict floor -- a typo in the
    strategist's `side` field must not open the 3-DTE window."""
    _, dte, _ = construction.pick_expiry_for_side(FULL_CHAIN, 3, side, _Cons(), today=TODAY)
    assert dte >= 25, f"side={side!r} reached {dte} DTE"


def test_debit_floor_holds_even_when_credit_floor_is_1():
    _, dte, _ = construction.pick_expiry_for_side(
        FULL_CHAIN, 1, "debit", _Cons(credit_min_dte=1, credit_max_dte=800), today=TODAY)
    assert dte >= 25


def test_pick_expiry_for_side_has_no_floor_argument_to_abuse():
    """STRUCTURAL PROOF, not a behavioural one: the sanctioned entry point exposes no
    floor/ceiling parameter, so no caller can pass one for a debit idea. If someone adds one,
    this test fails and the reviewer has to justify reopening the hole."""
    import inspect
    params = list(inspect.signature(construction.pick_expiry_for_side).parameters)
    assert params == ["expirations", "target_dte", "side", "cons", "today"]
    for forbidden in ("min_dte", "max_dte", "prefer_dte_max", "floor", "ceiling"):
        assert forbidden not in params


def test_debit_never_selects_sub_floor_across_the_whole_argument_space():
    """Brute force: every side token x every target x several chains x several configs.
    Not one combination may select an expiry below the debit floor."""
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
    """NEGATIVE CONTROL for the block above -- proves those assertions are not vacuous (i.e.
    the chain really does contain sub-25 expiries that COULD have been selected)."""
    for chain in (FULL_CHAIN, _chain(1, 2, 3, 25, 30), _chain(3, 7, 30, 60, 365)):
        _, dte, _ = construction.pick_expiry_for_side(chain, 3, "credit", _Cons(), today=TODAY)
        assert dte is not None and dte < 25, (
            f"chain {chain} has no sub-25 expiry -- the debit-floor assertions would be vacuous")


def test_negative_control_primitive_would_permit_the_hole():
    """NEGATIVE CONTROL: the raw primitive obeys whatever bounds it is handed -- passing
    min_dte=3 DOES select a 3-DTE expiry. That is exactly why side dispatch, not a global
    `min_dte: 3`, is the fix. If this test ever passes trivially, pick_expiry stopped honouring
    its arguments and the credit path is silently broken."""
    _, dte, _ = construction.pick_expiry(FULL_CHAIN, 3, min_dte=3, prefer_dte_max=45, today=TODAY)
    assert dte == 3


# --------------------------------------------------- long-dated clamp (2026-07-26 fix) holds

@pytest.mark.parametrize("target", [365, 497, 638, 795])
def test_long_dated_debit_is_not_collapsed_to_170(target):
    """prefer_dte_max 170 -> 800: a LEAP request must reach its expiry. At 170 EVERY one of
    these collapsed to ~170 DTE -- the ~168-DTE theta bleed the v8 corpus abolishes."""
    _, dte, adjusted = construction.pick_expiry_for_side(
        FULL_CHAIN, target, "debit", _Cons(), today=TODAY)
    assert dte == target, f"{target}-DTE request landed at {dte} DTE"
    assert adjusted is False


@pytest.mark.parametrize("target", [365, 497, 638, 795])
def test_negative_control_old_170_ceiling_did_collapse(target):
    """NEGATIVE CONTROL for the clamp test: with the OLD ceiling the same request collapses to
    170. Proves the test above measures the ceiling and not merely the chain contents."""
    _, dte, adjusted = construction.pick_expiry_for_side(
        FULL_CHAIN, target, "debit", _Cons(prefer_dte_max=170), today=TODAY)
    assert dte == 170 and adjusted is True


def test_boundary_debit_at_exactly_25_and_800():
    _, dte, adjusted = construction.pick_expiry_for_side(
        FULL_CHAIN, 25, "debit", _Cons(), today=TODAY)
    assert (dte, adjusted) == (25, False)  # exactly the floor -> untouched
    _, dte, adjusted = construction.pick_expiry_for_side(
        FULL_CHAIN, 800, "debit", _Cons(), today=TODAY)
    assert (dte, adjusted) == (800, False)  # exactly the ceiling -> untouched


def test_debit_has_no_hard_ceiling_beyond_prefer_dte_max():
    """A 900-DTE LEAP is still selectable when it is the only thing there: prefer_dte_max is a
    SOFT target clamp for debit (unchanged since 2026-07-01), unlike the HARD credit ceiling."""
    _, dte, _ = construction.pick_expiry_for_side(_chain(900, 1000), 365, "debit", _Cons(),
                                                  today=TODAY)
    assert dte == 900


# --------------------------------------------- back-compat: existing callers must not break

def test_pick_expiry_legacy_positional_signature_unchanged():
    """trader.py:1271 and daily_recommend.py:330 call
    pick_expiry(expirations, target_dte, cons.min_dte, cons.prefer_dte_max) positionally.
    Those two call sites are owned by other agents right now -- this must keep working."""
    exp, dte, adjusted = construction.pick_expiry(FULL_CHAIN, 10, 25, 45, TODAY)
    assert (dte, adjusted) == (25, True)
    exp, dte, adjusted = construction.pick_expiry(FULL_CHAIN, 30, 25, 45, TODAY)
    assert (dte, adjusted) == (30, False)


def test_pick_expiry_defaults_and_reject_unchanged():
    assert construction.pick_expiry([], 30, today=TODAY) == (None, None, False)
    assert construction.pick_expiry(_chain(1, 5, 10), 30, today=TODAY) == (None, None, False)
    assert construction.pick_expiry(["garbage", None, _exp(30)], 30, today=TODAY)[1] == 30


def test_pick_expiry_max_dte_defaults_to_no_ceiling():
    """The new parameter is opt-in: omitted => byte-identical behaviour to the old function."""
    assert construction.pick_expiry(FULL_CHAIN, 365, 25, 800, TODAY)[1] == 365
    assert construction.pick_expiry(FULL_CHAIN, 365, 25, 800, TODAY, None)[1] == 365


def test_pick_expiry_garbage_max_dte_refuses_rather_than_dropping_the_ceiling():
    exp, dte, _ = construction.pick_expiry(FULL_CHAIN, 30, 3, 45, TODAY, max_dte="junk")
    assert (exp, dte) == (None, None)
