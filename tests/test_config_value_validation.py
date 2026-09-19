"""Public API contract; production-derived narrative omitted."""

import os

import pytest
import yaml

from exitmgr.config import (Config, ConfigError, TRADING_DEFAULTS, _SPECS, _TRADING_KINDS,
                            AtrLevelsConfig, AutoTrailConfig, ScaleOutConfig, TrailingConfig)

REAL_CONFIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "config.yaml")

_DEL = object()


def _cfg(tmp_path, overrides=None):
    """Public API contract; production-derived narrative omitted."""
    with open(REAL_CONFIG) as fh:
        data = yaml.safe_load(fh)
    for dotted, value in (overrides or {}).items():
        parts = dotted.split(".")
        node = data
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        if value is _DEL:
            node.pop(parts[-1], None)
        else:
            node[parts[-1]] = value
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(data, sort_keys=False))
    return str(p)



def test_the_live_config_loads_unchanged():
    """Public API contract; production-derived narrative omitted."""
    cfg = Config.from_yaml(REAL_CONFIG)
    assert cfg.construction.max_combo_spread_pct == 45.0
    assert cfg.construction.max_entry_spread_pct == 25.0
    assert cfg.construction.sl_pct == -0.30
    assert cfg.cap_bypass_min_conviction == 11
    assert cfg.rules.profit_target_pct is None
    assert cfg.manage_positions_shadow_only is True


def test_the_harness_round_trip_is_itself_clean(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    assert Config.from_yaml(_cfg(tmp_path)).construction.max_combo_spread_pct == 45.0



def test_a_non_number_is_refused_by_full_key_path(tmp_path):
    with pytest.raises(ConfigError) as e:
        Config.from_yaml(_cfg(tmp_path, {"construction.max_combo_spread_pct": "not-a-number"}))
    assert e.value.key_path == "construction.max_combo_spread_pct"
    assert "not-a-number" in str(e.value), "the refusal must quote the offending VALUE"


@pytest.mark.parametrize("path", [
    "trading.auto_approve_within_gates",
    "trading.manage_positions_shadow_only",
    "construction.deterministic_construction",
    "rules.auto_trail.enabled",
])
def test_the_string_false_is_not_a_boolean(tmp_path, path):
    """Public API contract; production-derived narrative omitted."""
    with pytest.raises(ConfigError) as e:
        Config.from_yaml(_cfg(tmp_path, {path: "false"}))
    assert e.value.key_path == path
    assert "boolean" in str(e.value)


def test_an_integer_flag_is_not_a_boolean_either(tmp_path):
    with pytest.raises(ConfigError):
        Config.from_yaml(_cfg(tmp_path, {"trading.auto_approve_within_gates": 1}))


def test_a_string_number_is_refused_where_a_number_belongs(tmp_path):
    with pytest.raises(ConfigError) as e:
        Config.from_yaml(_cfg(tmp_path, {"rules.stop_pct": "30"}))
    assert e.value.key_path == "rules.stop_pct"


def test_a_boolean_is_refused_where_an_integer_belongs(tmp_path):
    with pytest.raises(ConfigError):
        Config.from_yaml(_cfg(tmp_path, {"construction.min_dte": True}))


def test_a_whole_float_is_accepted_and_coerced(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    cfg = Config.from_yaml(_cfg(tmp_path, {"construction.min_dte": 25.0}))
    assert cfg.construction.min_dte == 25 and isinstance(cfg.construction.min_dte, int)


def test_a_fractional_value_is_refused_where_a_whole_number_belongs(tmp_path):
    with pytest.raises(ConfigError) as e:
        Config.from_yaml(_cfg(tmp_path, {"construction.min_dte": 25.5}))
    assert e.value.key_path == "construction.min_dte"


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_a_non_finite_number_is_refused(tmp_path, bad):
    with pytest.raises(ConfigError) as e:
        Config.from_yaml(_cfg(tmp_path, {"construction.max_combo_spread_pct": bad}))
    assert e.value.key_path == "construction.max_combo_spread_pct"


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 0.0, "nope"])
def test_the_older_entry_spread_validator_still_fires_first(tmp_path, bad):
    """Public API contract; production-derived narrative omitted."""
    with pytest.raises(ValueError) as e:
        Config.from_yaml(_cfg(tmp_path, {"construction.max_entry_spread_pct": bad}))
    assert "max_entry_spread_pct" in str(e.value)


def test_a_list_of_non_strings_is_refused_by_index(tmp_path):
    with pytest.raises(ConfigError) as e:
        Config.from_yaml(_cfg(tmp_path, {"trading.approved_names": ["SPY", 7]}))
    assert e.value.key_path == "trading.approved_names[1]"


def test_a_mapping_where_a_list_belongs_is_refused(tmp_path):
    with pytest.raises(ConfigError):
        Config.from_yaml(_cfg(tmp_path, {"trading.blocked_names": {"TSLA": True}}))



def test_a_zero_combo_ceiling_is_refused(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    with pytest.raises(ConfigError) as e:
        Config.from_yaml(_cfg(tmp_path, {"construction.max_combo_spread_pct": 0.0}))
    assert e.value.key_path == "construction.max_combo_spread_pct"
    assert "> 0" in str(e.value)


def test_a_percent_written_as_a_fraction_is_refused(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    with pytest.raises(ConfigError) as e:
        Config.from_yaml(_cfg(tmp_path, {"trading.max_trade_pct": 25}))
    assert e.value.key_path == "trading.max_trade_pct"


def test_a_sign_flipped_stop_is_refused(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    with pytest.raises(ConfigError) as e:
        Config.from_yaml(_cfg(tmp_path, {"construction.sl_pct": 0.30}))
    assert e.value.key_path == "construction.sl_pct"


def test_a_conviction_threshold_of_zero_is_refused(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    with pytest.raises(ConfigError) as e:
        Config.from_yaml(_cfg(tmp_path, {"trading.cap_bypass_min_conviction": 0}))
    assert e.value.key_path == "trading.cap_bypass_min_conviction"


def test_eleven_stays_legal_because_it_is_the_live_never_idiom(tmp_path):
    assert Config.from_yaml(
        _cfg(tmp_path, {"trading.cap_bypass_min_conviction": 11})).cap_bypass_min_conviction == 11


def test_a_zero_hold_multiple_is_refused(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    with pytest.raises(ConfigError) as e:
        Config.from_yaml(_cfg(tmp_path, {"rules.atr_levels.hold_backstop_multiple": 0}))
    assert e.value.key_path == "rules.atr_levels.hold_backstop_multiple"


def test_a_nested_section_is_named_to_the_bottom(tmp_path):
    with pytest.raises(ConfigError) as e:
        Config.from_yaml(_cfg(tmp_path, {"rules.auto_trail.giveback_fraction": 1.5}))
    assert e.value.key_path == "rules.auto_trail.giveback_fraction"


def test_a_conviction_curve_value_outside_the_pot_is_refused(tmp_path):
    with pytest.raises(ConfigError) as e:
        Config.from_yaml(_cfg(tmp_path, {"trading.conviction_size_curve": {7: 1.5}}))
    assert e.value.key_path == "trading.conviction_size_curve.7"


def test_a_conviction_curve_key_off_the_scale_is_refused(tmp_path):
    with pytest.raises(ConfigError):
        Config.from_yaml(_cfg(tmp_path, {"trading.conviction_size_curve": {0: 0.25}}))



@pytest.mark.parametrize("path,attr", [
    ("construction.max_combo_spread_pct", None),
    ("trading.max_trade_pct", None),
    ("rules.stop_pct", None),
])
def test_an_absent_key_keeps_its_default(tmp_path, path, attr):
    """Public API contract; production-derived narrative omitted."""
    Config.from_yaml(_cfg(tmp_path, {path: _DEL}))


def test_null_is_accepted_where_the_field_is_optional(tmp_path):
    cfg = Config.from_yaml(_cfg(tmp_path, {"rules.profit_target_pct": None,
                                           "trading.intended_hold_days_fallback": None,
                                           "trading.pot_cap_usd": None}))
    assert cfg.rules.profit_target_pct is None
    assert cfg.intended_hold_days_fallback is None
    assert cfg.pot_cap_usd is None


def test_null_is_refused_where_the_field_is_not_optional(tmp_path):
    with pytest.raises(ConfigError) as e:
        Config.from_yaml(_cfg(tmp_path, {"construction.min_dte": None}))
    assert e.value.key_path == "construction.min_dte"
    assert "omit the key" in str(e.value), "the message must point at the legal alternative"


def test_trim_fraction_is_left_to_its_own_enabled_only_validator(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    assert "rules.scale_out.trim_fraction" not in _SPECS
    Config.from_yaml(_cfg(tmp_path, {"rules.scale_out.enabled": False,
                                     "rules.scale_out.trim_fraction": 1.5}))
    with pytest.raises(ValueError):
        Config.from_yaml(_cfg(tmp_path, {"rules.scale_out.enabled": True,
                                         "rules.scale_out.trim_fraction": 1.5}))



def test_every_spec_path_points_at_a_real_field():
    """Public API contract; production-derived narrative omitted."""
    sections = dict(Config._SECTIONS)
    sections["rules"] = type(Config().rules)
    sections["construction"] = type(Config().construction)
    for name, dc in Config._RULES_NESTED.items():
        sections["rules.%s" % name] = dc
    registry = {k for k, _ in TRADING_DEFAULTS}

    unresolved = []
    for path in _SPECS:
        head, _, leaf = path.rpartition(".")
        if head == "trading":
            ok = leaf in registry
        else:
            dc = sections.get(head)
            ok = dc is not None and leaf in dc.__dataclass_fields__
        if not ok:
            unresolved.append(path)
    assert unresolved == [], "_SPECS entries that match nothing: %s" % unresolved


def test_every_untyped_registry_key_states_its_type_explicitly():
    """Public API contract; production-derived narrative omitted."""
    untyped = [k for k, v in TRADING_DEFAULTS if _TRADING_KINDS.get(k) is None]
    missing = [k for k in untyped
               if not (_SPECS.get("trading.%s" % k) and _SPECS["trading.%s" % k].kind)]
    assert missing == [], "None-default trading keys with no declared kind: %s" % missing


def test_the_registry_types_are_read_off_the_defaults_not_a_second_table():
    """Public API contract; production-derived narrative omitted."""
    assert _TRADING_KINDS["manage_positions"] == "bool"
    assert _TRADING_KINDS["max_trade_pct"] == "number"
    assert _TRADING_KINDS["max_concurrent"] == "int"
    assert _TRADING_KINDS["approved_names"] == "list"
    assert _TRADING_KINDS["sector_map"] == "dict"
    assert set(_TRADING_KINDS) == {k for k, _ in TRADING_DEFAULTS}
