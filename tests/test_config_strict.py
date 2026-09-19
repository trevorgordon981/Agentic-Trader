"""Public API contract; production-derived narrative omitted."""
import copy
import os

import pytest
import yaml

from exitmgr.config import (Config, ConfigError, TRADING_DEFAULTS, load_config,
                            AutoTrailConfig, ConstructionConfig, IBConfig, RulesConfig)

LIVE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml")


def _tmp_cfg(tmp_path, mutate=None):
    d = yaml.safe_load(open(LIVE))
    d = copy.deepcopy(d)
    if mutate:
        mutate(d)
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(d))
    return str(p)



def test_the_live_config_has_zero_unknown_keys(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    Config.from_yaml(LIVE)


def test_every_trading_key_in_the_live_config_is_registered():
    declared = {k for k, _ in TRADING_DEFAULTS}
    present = set((yaml.safe_load(open(LIVE)) or {}).get("trading") or {})
    assert present - declared == set()



@pytest.mark.parametrize("mutate,path", [
    (lambda d: d.update({"trding": {}}), "trding"),
    (lambda d: d["ib"].update({"prot_client_id": 1}), "ib.prot_client_id"),
    (lambda d: d["caps"].update({"max_orders_per_dayy": 1}), "caps.max_orders_per_dayy"),
    (lambda d: d["rules"].update({"stop_pcnt": 30}), "rules.stop_pcnt"),
    (lambda d: d["rules"]["auto_trail"].update({"giveback_fracton": 0.5}),
     "rules.auto_trail.giveback_fracton"),
    (lambda d: d["construction"].update({"min_dtee": 1}), "construction.min_dtee"),
    (lambda d: d["trading"].update({"manage_positions_shadow_onlyy": False}),
     "trading.manage_positions_shadow_onlyy"),
])
def test_an_unknown_key_is_refused_by_its_full_path(tmp_path, mutate, path):
    with pytest.raises(ConfigError) as e:
        Config.from_yaml(_tmp_cfg(tmp_path, mutate))
    assert e.value.key_path == path
    assert path in str(e.value), "the error must NAME the offending key, not just fail"


def test_the_error_suggests_the_key_that_was_meant(tmp_path):
    with pytest.raises(ConfigError) as e:
        Config.from_yaml(_tmp_cfg(tmp_path,
                                  lambda d: d["trading"].update({"max_concurent": 4})))
    assert "max_concurrent" in str(e.value)


def test_rejection_reaches_the_bottom_of_the_tree_not_just_the_top(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    with pytest.raises(ConfigError) as e:
        Config.from_yaml(_tmp_cfg(
            tmp_path, lambda d: d["rules"].__setitem__("atr_levels", {"k_stopp": 2.0})))
    assert e.value.key_path == "rules.atr_levels.k_stopp"


def test_an_omitted_section_is_still_legal(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    cfg = Config.from_yaml(_tmp_cfg(tmp_path, lambda d: d.pop("scope")))
    assert cfg.scope.mode == "journal"



def test_from_yaml_and_load_config_agree_field_for_field():
    a, b = Config.from_yaml(LIVE), load_config(LIVE)
    for k, _ in TRADING_DEFAULTS:
        assert getattr(a, k) == getattr(b, k), k
    assert a.rules == b.rules and a.construction == b.construction and a.ib == b.ib


def test_from_yaml_reads_the_live_safety_flag():
    """Public API contract; production-derived narrative omitted."""
    live = (yaml.safe_load(open(LIVE)) or {}).get("trading") or {}
    assert Config.from_yaml(LIVE).manage_positions_shadow_only is live["manage_positions_shadow_only"]


def test_from_yaml_exposes_every_trading_field():
    cfg = Config.from_yaml(LIVE)
    missing = [k for k, _ in TRADING_DEFAULTS if not hasattr(cfg, k)]
    assert missing == []


def test_a_missing_file_still_lands_the_registry_defaults(tmp_path):
    cfg = load_config(str(tmp_path / "nope.yaml"))
    for k, dflt in TRADING_DEFAULTS:
        assert getattr(cfg, k) == dflt, k


def test_cli_overrides_still_win(tmp_path):
    cfg = load_config(_tmp_cfg(tmp_path), arm=True, loop=True, interval=7,
                      max_orders_cycle=3, max_orders_day=4, max_notional_day=5.0)
    assert cfg.loop.interval_seconds == 7 and cfg.caps.max_orders_per_cycle == 3
    assert cfg.caps.max_orders_per_day == 4 and cfg.caps.max_notional_per_day == 5.0
    assert cfg.arm is True and cfg.dry_run is False and cfg.loop_mode is True


def test_a_non_mapping_document_is_refused(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("- just\n- a list\n")
    with pytest.raises(ConfigError):
        Config.from_yaml(str(p))
