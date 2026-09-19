"""Public API contract; production-derived narrative omitted."""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from exitmgr import entry_safety, regime, risk
from exitmgr.config import load_config


_HISTORY_MARKER = "KEEP THIS BLOCK HONEST"


@pytest.fixture(scope="module")
def doc():
    text = risk.__doc__
    assert text, "exitmgr.risk lost its module docstring"
    return text


@pytest.fixture(scope="module")
def current(doc):
    """Public API contract; production-derived narrative omitted."""
    assert _HISTORY_MARKER in doc, (
        "the paragraph explaining why these numbers are pinned was removed; without it the next "
        "editor has no reason not to let them drift again")
    return doc.split(_HISTORY_MARKER, 1)[0]


@pytest.fixture(scope="module")
def limits():
    """Public API contract; production-derived narrative omitted."""
    return entry_safety.risk_limits_from_config(load_config())


def _states(doc, pattern, why):
    assert re.search(pattern, doc), (
        "exitmgr/risk.py's module docstring no longer states %s.\n"
        "Expected to match: %s\nUpdate the docstring in the same commit as the config." % (why, pattern))


def test_the_concurrent_cap_in_the_docstring_is_the_live_one(doc, current, limits):
    """Public API contract; production-derived narrative omitted."""
    assert limits.max_concurrent == 10, (
        "trading.max_concurrent is now %d -- update risk.py's docstring and this test together"
        % limits.max_concurrent)
    _states(doc, r"concurrent positions\s+%d\b" % limits.max_concurrent, "the concurrent cap")
    assert not re.search(r"concurrent positions\s+(?!%d\b)\d" % limits.max_concurrent, current), (
        "the limits table states a concurrent cap that is not the live one")


def test_the_docstring_does_not_claim_single_names_need_approval(doc, current, limits):
    """Public API contract; production-derived narrative omitted."""
    assert limits.allow_any_name is True, (
        "trading.allow_model_names is now False -- the universe gate is back ON and risk.py's "
        "docstring must say so instead of 'OPEN'")
    assert "single names must be user-approved" not in current
    _states(doc, r"universe\s+OPEN", "that the universe gate is open")
    _states(doc, r"allow_model_names is TRUE", "which config key opens it")


def test_the_hard_and_soft_per_trade_caps_are_the_live_ones(doc, current, limits):
    hard = limits.max_trade_pct_hard
    assert hard == 0.25
    _states(doc, r"HARD ceiling\s+%g%% of pot" % (hard * 100), "the hard per-trade ceiling")

    mult = set(limits.conviction_size_multipliers.values())
    assert mult == {0.75}, "conviction_size_multipliers is no longer flat at 0.75: %s" % sorted(mult)
    soft = limits.max_trade_pct * 0.75
    assert soft == pytest.approx(0.1875)
    _states(doc, r"soft cap\s+%g%% of pot" % (soft * 100), "the soft per-trade cap")
    assert "soft cap == the 25% hard ceiling" not in current, (
        "the 0.75 multipliers broke that equality on 2026-08-21")


def test_the_bull_regime_lift_is_stated_because_it_changes_the_binding_number(doc):
    """Public API contract; production-derived narrative omitted."""
    assert regime.BULL_LONG_SIZE_MULT == 1.5
    _states(doc, r"BULL_LONG_SIZE_MULT", "the regime multiplier that lifts the soft cap")
    _states(doc, r"28\.125%", "where the lifted cap lands before the hard ceiling clamps it")


def test_the_daily_breaker_and_buffers_are_the_live_ones(doc, limits):
    assert limits.daily_halt_pct == 0.2
    _states(doc, r"daily breaker\s+-%g%% on the day" % (limits.daily_halt_pct * 100),
            "the daily breaker")
    assert limits.cash_buffer_pct == 0.05
    _states(doc, r"cash buffer\s+%g%% of pot" % (limits.cash_buffer_pct * 100), "the cash buffer")
    assert limits.max_single_name_agg_pct == 0.36
    _states(doc, r"single-name aggregate\s+%g%% of pot" % (limits.max_single_name_agg_pct * 100),
            "the single-name aggregate cap")
    assert limits.max_sector_agg_pct == 0.40
    _states(doc, r"sector aggregate\s+%g%% of pot" % (limits.max_sector_agg_pct * 100),
            "the sector aggregate cap")


def test_the_index_set_is_the_one_in_the_code(doc):
    assert risk.INDEX_UNDERLYINGS == {"SPY", "QQQ", "IWM"}
    for sym in sorted(risk.INDEX_UNDERLYINGS):
        assert sym in doc
    _states(doc, r"NO_EARNINGS_ETFS", "that the earnings-exempt ETF set is a different, wider one")
    assert risk.INDEX_UNDERLYINGS < set(entry_safety.NO_EARNINGS_ETFS), (
        "NO_EARNINGS_ETFS is no longer a strict superset; the docstring's distinction has changed")


def test_the_dataclass_defaults_are_still_the_documented_fallback(doc):
    """Public API contract; production-derived narrative omitted."""
    d = risk.RiskLimits()
    assert (d.max_trade_pct, d.max_concurrent, d.daily_halt_pct) == (0.12, 4, 0.08)
    _states(doc, r"12% / 4 / -8%", "the dataclass fallback values")


def test_every_documented_limit_is_actually_overridden_from_config(limits):
    """Public API contract; production-derived narrative omitted."""
    bare = risk.RiskLimits()
    for field in ("max_trade_pct", "max_trade_pct_hard", "max_concurrent", "daily_halt_pct",
                  "cash_buffer_pct", "max_single_name_agg_pct", "max_sector_agg_pct",
                  "allow_any_name", "conviction_size_multipliers"):
        assert hasattr(limits, field), field
    for field, default in (("max_concurrent", bare.max_concurrent),
                           ("daily_halt_pct", bare.daily_halt_pct),
                           ("max_trade_pct", bare.max_trade_pct)):
        assert getattr(limits, field) != default, (
            "%s equals the dataclass default (%r) -- either config stopped overriding it, or the "
            "config value drifted onto the fallback and this test can no longer tell the "
            "difference" % (field, default))


def test_the_one_field_config_cannot_set_is_documented(doc, limits):
    """Public API contract; production-derived narrative omitted."""
    assert limits.max_cross_book_name_agg_pct is None
    _states(doc, r"max_cross_book_name_agg_pct", "the one field config cannot set")
