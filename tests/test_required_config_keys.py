"""Public API contract; production-derived narrative omitted."""
import copy
import os

import pytest
import yaml

from exitmgr import entry_safety
from exitmgr.config import (REQUIRED_KEYS, TRADING_DEFAULTS, Config, ConfigError,
                            require_present)

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(APP, "config.yaml")

_DEL = object()


def _cfg_from_live(tmp_path, **overrides):
    """Public API contract; production-derived narrative omitted."""
    d = copy.deepcopy(yaml.safe_load(open(CONFIG_PATH)))
    for dotted, value in overrides.items():
        head, _, leaf = dotted.replace("__", ".").rpartition(".")
        node = d
        for part in head.split(".") if head else []:
            node = node[part]
        if value is _DEL:
            node.pop(leaf, None)
        else:
            node[leaf] = value
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(d))
    return str(p)



def test_the_live_config_states_every_required_key():
    """Public API contract; production-derived narrative omitted."""


    assert set(REQUIRED_KEYS) >= {"caps.max_orders_per_day", "caps.max_notional_per_day"}
    raw = yaml.safe_load(open(CONFIG_PATH)) or {}
    for path in REQUIRED_KEYS:
        section, _, leaf = path.rpartition(".")
        assert leaf in (raw.get(section) or {}), path
    Config.from_yaml(CONFIG_PATH)


@pytest.mark.parametrize("path", REQUIRED_KEYS)
def test_deleting_a_required_key_is_a_startup_error(tmp_path, path):
    """Public API contract; production-derived narrative omitted."""
    with pytest.raises(ConfigError) as exc:
        Config.from_yaml(_cfg_from_live(tmp_path, **{path.replace(".", "__"): _DEL}))
    assert exc.value.key_path == path
    assert "required" in str(exc.value)


def test_deleting_the_whole_caps_section_is_also_an_error(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    d = copy.deepcopy(yaml.safe_load(open(CONFIG_PATH)))
    d.pop("caps")
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(d))
    with pytest.raises(ConfigError):
        Config.from_yaml(str(p))


def test_require_present_binds_the_RAW_dict_the_slate_and_manual_lane_read():
    """Public API contract; production-derived narrative omitted."""
    with pytest.raises(ConfigError):
        require_present({}, "caps")
    with pytest.raises(ConfigError):
        require_present({"max_orders_per_day": 20}, "caps")
    with pytest.raises(ConfigError):
        require_present(None, "caps")
    require_present({"max_orders_per_day": 20, "max_notional_per_day": 50000.0}, "caps")
    require_present({}, "rules")


def test_an_absent_OPTIONAL_key_still_keeps_its_default(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    cfg = Config.from_yaml(_cfg_from_live(tmp_path, caps__max_orders_per_cycle=_DEL))
    assert cfg.caps.max_orders_per_cycle == 5


def test_a_present_required_key_is_still_VALUE_checked(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    with pytest.raises(ConfigError) as exc:
        Config.from_yaml(_cfg_from_live(tmp_path, caps__max_orders_per_day=0))
    assert exc.value.key_path == "caps.max_orders_per_day"



@pytest.mark.parametrize("key,expected", [("max_single_name_agg_pct", 0.36),
                                          ("max_trade_pct_hard", 0.25)])
def test_the_hardest_caps_are_registered_at_the_value_that_was_already_binding(key, expected):
    """Public API contract; production-derived narrative omitted."""
    assert dict(TRADING_DEFAULTS)[key] == expected


@pytest.mark.parametrize("key", ["max_single_name_agg_pct", "max_trade_pct_hard"])
def test_the_hardest_caps_can_now_be_SET_from_config(tmp_path, key):
    """Public API contract; production-derived narrative omitted."""
    cfg = Config.from_yaml(_cfg_from_live(tmp_path, **{"trading__" + key: 0.19}))
    assert getattr(cfg, key) == 0.19
    assert getattr(entry_safety.risk_limits_from_config(cfg), key) == 0.19


def test_the_live_config_states_both_caps_at_their_binding_values():
    """Public API contract; production-derived narrative omitted."""
    tr = (yaml.safe_load(open(CONFIG_PATH)) or {})["trading"]
    assert tr["max_single_name_agg_pct"] == 0.36
    assert tr["max_trade_pct_hard"] == 0.25
    limits = entry_safety.risk_limits_from_config(tr)
    assert limits.max_single_name_agg_pct == 0.36
    assert limits.max_trade_pct_hard == 0.25



def test_a_blank_sector_value_is_refused_at_load(tmp_path):
    with pytest.raises(ConfigError) as exc:
        Config.from_yaml(_cfg_from_live(tmp_path, trading__sector_map={"SYMF": "  "}))
    assert exc.value.key_path == "trading.sector_map.SYMF"
    assert "blank" in str(exc.value)


def test_a_sector_named_like_the_sentinel_is_refused_at_load(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    from exitmgr.risk import UNCLASSIFIED_SECTOR
    with pytest.raises(ConfigError) as exc:
        Config.from_yaml(
            _cfg_from_live(tmp_path, trading__sector_map={"SYMF": UNCLASSIFIED_SECTOR}))
    assert exc.value.key_path == "trading.sector_map.SYMF"
    assert "reserved" in str(exc.value)


def test_a_blank_sector_KEY_is_refused_at_load(tmp_path):
    with pytest.raises(ConfigError):
        Config.from_yaml(_cfg_from_live(tmp_path, trading__sector_map={"": "semis"}))
