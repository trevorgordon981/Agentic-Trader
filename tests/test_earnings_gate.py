"""Public API contract; production-derived narrative omitted."""
from datetime import date

from exitmgr import construction
from exitmgr import strategist
from exitmgr.config import ConstructionConfig


def cons(enabled=True, days=0, hard=False):
    return ConstructionConfig(earnings_blackout_enabled=enabled,
                              earnings_block_hard=hard,
                              earnings_blackout_days=days)


ENTRY = date(2026, 7, 3)
EXPIRY = "20260821"
EXPIRY_D = date(2026, 8, 21)




def test_earnings_before_expiry_warns_and_allows():
    ok, why = construction.earnings_ok(ENTRY, EXPIRY, date(2026, 7, 25), cons())
    assert ok is True
    assert "earnings" in why.lower() and "iv crush" in why.lower()
    assert "holding through earnings is allowed" in why.lower()


def test_earnings_on_expiry_warns_and_allows():

    ok, why = construction.earnings_ok(ENTRY, EXPIRY, EXPIRY_D, cons())
    assert ok is True
    assert why


def test_same_day_earnings_is_always_allowed_with_unresolved_event_warning():
    for cfg in (cons(), cons(hard=True), cons(enabled=False, hard=True)):
        ok, why = construction.earnings_ok(ENTRY, EXPIRY, ENTRY, cfg)
        assert ok is True
        assert "EARNINGS TODAY" in why
        assert ENTRY.isoformat() in why
        assert "timing/status is unverified" in why
        assert "treat the event as UPCOMING" in why
        assert "Entry remains allowed" in why
        assert "overnight gap beyond ordinary risk controls" in why
        assert "fresh option IV/NBBO" in why


def test_every_live_strategist_prompt_forbids_inferring_same_day_release_status():
    for prompt in (strategist.SYSTEM_PROMPT, strategist.RECOMMEND_PROMPT,
                   strategist.STAGE_A_SYSTEM_PROMPT,
                   strategist.STAGE_A_RECOMMEND_PROMPT, strategist.SINGLE_PROMPT):
        assert "EARNINGS TODAY" in prompt
        assert "does NOT mean the release has already happened" in prompt
        assert "reported timestamp earlier than this decision" in prompt
        assert "do not by themselves bar entry" in prompt
        assert "BEFORE a release" in prompt and "AFTER a verified release" in prompt
        assert "IV crush can hurt long premium" in prompt
        assert "short put may benefit from crush" in prompt
        assert "never merely assume the option is cheaper" in prompt


def test_earnings_string_and_date_expiry_equivalent():
    a = construction.earnings_ok(ENTRY, EXPIRY, date(2026, 7, 25), cons())
    b = construction.earnings_ok(ENTRY, EXPIRY_D, "2026-07-25", cons())
    assert a == b




def test_earnings_after_expiry_passes():
    ok, why = construction.earnings_ok(ENTRY, EXPIRY, date(2026, 9, 1), cons())
    assert ok is True
    assert why == ""




def test_unknown_earnings_passes_fail_open():
    ok, why = construction.earnings_ok(ENTRY, EXPIRY, None, cons())
    assert ok is True
    assert why == ""


def test_unparseable_earnings_passes_fail_open():
    ok, why = construction.earnings_ok(ENTRY, EXPIRY, "not-a-date", cons())
    assert ok is True
    assert why == ""




def test_disabled_is_noop():

    ok, why = construction.earnings_ok(ENTRY, EXPIRY, date(2026, 7, 25), cons(enabled=False))
    assert ok is True
    assert why == ""


def test_legacy_hard_disposition_is_explicit_opt_in():
    ok, why = construction.earnings_ok(
        ENTRY, EXPIRY, date(2026, 7, 25), cons(hard=True))
    assert ok is False
    assert "hard earnings gate enabled" in why




def test_buffer_extends_block_past_expiry():

    ok, why = construction.earnings_ok(ENTRY, EXPIRY, date(2026, 8, 24), cons(days=5))
    assert ok is True and why

    ok0, _ = construction.earnings_ok(ENTRY, EXPIRY, date(2026, 8, 24), cons(days=0))
    assert ok0 is True


def test_buffer_boundary_inclusive():

    ok_in, why_in = construction.earnings_ok(
        ENTRY, EXPIRY, date(2026, 8, 26), cons(days=5))
    ok_out, why_out = construction.earnings_ok(
        ENTRY, EXPIRY, date(2026, 8, 27), cons(days=5))
    assert ok_in is True and why_in
    assert ok_out is True and why_out == ""




def test_earnings_before_entry_passes():
    ok, why = construction.earnings_ok(ENTRY, EXPIRY, date(2026, 7, 1), cons())
    assert ok is True
    assert why == ""
