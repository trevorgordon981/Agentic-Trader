"""Public API contract; production-derived narrative omitted."""
import re

import pytest

from exitmgr import strategist
from exitmgr.config import load_config


def _band_from_prompt():
    """Public API contract; production-derived narrative omitted."""
    m = re.search(r"land between (\d+\.\d+) and (\d+\.\d+)", strategist._DOCTRINE)
    assert m, "the doctrine no longer states a numeric delta band"
    return float(m.group(1)), float(m.group(2))


def _band_from_config():
    cons = load_config("config.yaml").construction
    return float(cons.delta_min), float(cons.delta_max)


def test_the_prompt_states_the_same_band_construction_enforces():
    assert _band_from_prompt() == _band_from_config()


def test_the_doctrine_actually_mentions_the_delta_rule():
    assert "LONG-LEG DELTA IS A BAND" in strategist._DOCTRINE


def test_the_model_is_told_the_clamp_is_silent_and_automatic():
    """Public API contract; production-derived narrative omitted."""
    d = strategist._DOCTRINE.lower()
    assert "clamped" in d


def test_the_model_is_told_to_SIZE_for_the_in_band_leg():
    """Public API contract; production-derived narrative omitted."""
    d = strategist._DOCTRINE
    assert "est_debit_usd" in d
    assert "allocation_pct_net_liq" in d


def test_credit_structures_are_explicitly_exempt():
    """Public API contract; production-derived narrative omitted."""
    assert "credit" in strategist._DOCTRINE.lower()


def test_the_doctrine_is_actually_shipped_in_the_system_prompt():
    """Public API contract; production-derived narrative omitted."""
    assert "LONG-LEG DELTA IS A BAND" in strategist.SYSTEM_PROMPT



@pytest.mark.parametrize("asked,expected", [(0.30, 0.55), (0.40, 0.55), (0.55, 0.55),
                                            (0.60, 0.60), (0.65, 0.65), (0.90, 0.65)])
def test_the_clamp_still_binds_regardless_of_what_the_model_asks(asked, expected):
    """Public API contract; production-derived narrative omitted."""
    from exitmgr import construction
    cons = load_config("config.yaml").construction
    assert construction.effective_delta(asked, cons) == pytest.approx(expected)
