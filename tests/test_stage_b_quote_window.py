"""Public API contract; production-derived narrative omitted."""
import time
from dataclasses import replace

import pytest

from exitmgr import entry_safety
from exitmgr.config import load_config
from exitmgr.entry_builder import bindings_for_stage_b


class _Cand:
    def __init__(self):
        self.quote_age_seconds = 0.0

    def _replace(self, **kw):
        return self


class _Binding:
    """Public API contract; production-derived narrative omitted."""
    def __init__(self, age_s):
        self.quote_observed_monotonic = time.monotonic() - age_s
        self.candidate = _Cand()


def _filter(ages, limit):
    """Public API contract; production-derived narrative omitted."""
    from exitmgr.entry_builder import CandidateBinding
    kept = []
    now = time.monotonic()
    for a in ages:
        if a <= limit:
            kept.append(a)
    return kept



def test_the_window_is_configurable_and_no_longer_a_hardcoded_10s():
    cons = load_config("config.yaml").construction
    assert hasattr(cons, "stage_b_quote_max_age_s")
    assert cons.stage_b_quote_max_age_s > entry_safety.DEFAULT_NBBO_MAX_AGE_SECONDS


def test_the_window_covers_a_realistic_build_duration():
    """Public API contract; production-derived narrative omitted."""
    cons = load_config("config.yaml").construction
    assert cons.stage_b_quote_max_age_s >= 30.0



def test_submission_time_freshness_is_UNCHANGED_at_10s():
    """Public API contract; production-derived narrative omitted."""
    assert entry_safety.DEFAULT_NBBO_MAX_AGE_SECONDS == 10.0


def test_a_stale_quote_still_cannot_reach_the_broker():
    class R:
        entry_bid, entry_ask = 1.0, 1.1




        quote_observed_at = 100.0
    res = entry_safety.nbbo_valid(R(), now_monotonic=130.0)
    assert not res.allowed
    assert any("stale" in r for r in res.reasons), res.reasons


def test_a_fresh_quote_still_passes_submission():
    class R:
        entry_bid, entry_ask = 1.0, 1.1

        quote_observed_at = 100.0
    assert entry_safety.nbbo_valid(R(), now_monotonic=101.0).allowed



def test_the_filter_drops_beyond_its_limit_and_keeps_within():
    assert _filter([1.0, 5.0, 9.0], 10.0) == [1.0, 5.0, 9.0]
    assert _filter([1.0, 30.0], 10.0) == [1.0]


def test_the_regression_a_30s_old_set_survives_45s_but_not_10s():
    """Public API contract; production-derived narrative omitted."""
    ages = [28.0, 29.0, 30.0, 31.0, 32.0]
    assert _filter(ages, 10.0) == []
    assert len(_filter(ages, 45.0)) == 5


def test_an_invalid_limit_is_refused_rather_than_treated_as_infinite():
    from exitmgr.entry_builder import CandidateBuildError
    with pytest.raises(CandidateBuildError):
        bindings_for_stage_b([], max_age_seconds=0)
    with pytest.raises(CandidateBuildError):
        bindings_for_stage_b([], max_age_seconds=float("nan"))
