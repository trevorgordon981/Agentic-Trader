"""Public API contract; production-derived narrative omitted."""
import io
import os
import re
import stat
import sys
import tempfile

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cross_book
from exitmgr import entry_safety, risk
from exitmgr.config import ConfigError, TRADING_DEFAULTS, load_config
from exitmgr.risk import RiskLimits

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(APP, "config.yaml")

_PATH_LINE = re.compile(r"^  external_book_path: .*$", re.M)
_AGE_LINE = re.compile(r"^  external_book_max_age_s: .*$", re.M)


def _variant(tmp_path, *, path_val=None, age_val=None, drop_path=False):
    """Public API contract; production-derived narrative omitted."""
    s = io.open(CONFIG_PATH, encoding="utf-8").read()
    if drop_path:
        s = _PATH_LINE.sub("", s)
    elif path_val is not None:
        s = _PATH_LINE.sub("  external_book_path: %s" % path_val, s)
    if age_val is not None:
        s = _AGE_LINE.sub("  external_book_max_age_s: %s" % age_val, s)
    p = tmp_path / "config_variant.yaml"
    io.open(str(p), "w", encoding="utf-8").write(s)
    return str(p)


def _limits(cfg_path):
    return entry_safety.risk_limits_from_config(
        load_config(config_path=cfg_path, arm=False, loop=False, interval=900))



def test_both_keys_are_REGISTERED_or_the_live_config_will_not_load():
    """Public API contract; production-derived narrative omitted."""
    registered = dict(TRADING_DEFAULTS)
    assert "external_book_path" in registered, (
        "unregistered: _checked() would reject trading.external_book_path in config.yaml as an "
        "unknown key, and the whole app would fail to load its config")
    assert "external_book_max_age_s" in registered

    load_config(config_path=CONFIG_PATH, arm=False, loop=False, interval=900)


def test_the_DEFAULT_is_a_string_because_the_default_declares_the_TYPE():
    """Public API contract; production-derived narrative omitted."""
    assert dict(TRADING_DEFAULTS)["external_book_path"] == ""
    assert isinstance(dict(TRADING_DEFAULTS)["external_book_path"], str)
    assert isinstance(dict(TRADING_DEFAULTS)["external_book_max_age_s"], float)


@pytest.mark.parametrize("bad,kind", [("12345", "int"), ("true", "bool"), ("[a, b]", "list")])
def test_a_NON_STRING_snapshot_path_is_REFUSED_at_load(tmp_path, bad, kind):
    """Public API contract; production-derived narrative omitted."""
    with pytest.raises(ConfigError) as exc:
        _limits(_variant(tmp_path, path_val=bad))
    assert "external_book_path" in str(exc.value)
    assert kind in str(exc.value)



@pytest.mark.parametrize("blank", ['""', '"   "'])
def test_a_BLANK_path_is_UNARMED_and_normalises_to_None(tmp_path, blank):
    """Public API contract; production-derived narrative omitted."""
    limits = _limits(_variant(tmp_path, path_val=blank))
    assert limits.external_book_path is None
    assert risk.cross_book_authority(limits)["armed"] is False


def test_an_ABSENT_key_is_UNARMED_and_that_is_the_dormant_state(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    limits = _limits(_variant(tmp_path, drop_path=True))
    assert limits.external_book_path is None
    assert risk.cross_book_authority(limits)["armed"] is False



def test_the_LIVE_config_reaches_RiskLimits_and_ARMS_the_rail():
    """Public API contract; production-derived narrative omitted."""
    limits = _limits(CONFIG_PATH)
    assert limits.external_book_path, "the live config no longer arms the cross-book rail"
    assert risk.cross_book_authority(limits)["armed"] is True


def test_the_configured_path_is_CWD_INDEPENDENT():
    """Public API contract; production-derived narrative omitted."""
    limits = _limits(CONFIG_PATH)
    assert os.path.isabs(os.path.expanduser(limits.external_book_path)), (
        "trading.external_book_path is relative (%r): it resolves differently per caller's CWD"
        % limits.external_book_path)


def test_the_GATE_reads_the_file_the_SNAPSHOT_JOB_writes():
    """Public API contract; production-derived narrative omitted."""
    limits = _limits(CONFIG_PATH)
    configured = os.path.realpath(os.path.expanduser(limits.external_book_path))
    written = os.path.realpath(os.path.expanduser(cross_book.DEFAULT_SNAPSHOT))
    assert configured == written, (
        "the gate reads %s but `cross_book.py --write-snapshot` defaults to %s"
        % (configured, written))



def test_the_LIVE_bound_is_the_measured_72h():
    """Public API contract; production-derived narrative omitted."""
    limits = _limits(CONFIG_PATH)
    assert limits.external_book_max_age_s == 259200.0
    assert limits.external_book_max_age_s == risk.DEFAULT_EXTERNAL_BOOK_MAX_AGE_S


def test_a_ZERO_bound_is_PASSED_THROUGH_not_eaten_by_a_falsy_default(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    limits = _limits(_variant(tmp_path, age_val="0"))
    assert limits.external_book_max_age_s == 0.0, "a zero was eaten on the way through"

    assert risk.bounded_max_age_s(0.0) == risk.DEFAULT_EXTERNAL_BOOK_MAX_AGE_S
    assert risk.cross_book_authority(limits)["max_age_s"] == risk.DEFAULT_EXTERNAL_BOOK_MAX_AGE_S


@pytest.mark.parametrize("bad", ["null", "notanumber"])
def test_a_MALFORMED_bound_is_LOUD_on_the_validated_path(tmp_path, bad):
    """Public API contract; production-derived narrative omitted."""
    with pytest.raises(ConfigError) as exc:
        _limits(_variant(tmp_path, age_val=bad))
    assert "external_book_max_age_s" in str(exc.value)


def test_a_MALFORMED_bound_on_the_RAW_DICT_path_falls_back_rather_than_crashing_an_entry():
    """Public API contract; production-derived narrative omitted."""
    for junk in (None, "notanumber", object()):
        limits = entry_safety.risk_limits_from_config(
            {"external_book_path": "/nope/x.json", "external_book_max_age_s": junk})
        assert limits.external_book_max_age_s == risk.DEFAULT_EXTERNAL_BOOK_MAX_AGE_S



def test_BOTH_caller_shapes_arm_identically():
    """Public API contract; production-derived narrative omitted."""
    from_object = _limits(CONFIG_PATH)
    raw = yaml.safe_load(io.open(CONFIG_PATH, encoding="utf-8"))["trading"]
    from_mapping = entry_safety.risk_limits_from_config(raw)
    assert from_object.external_book_path == from_mapping.external_book_path
    assert from_object.external_book_max_age_s == from_mapping.external_book_max_age_s
    assert risk.cross_book_authority(from_mapping)["armed"] is True



def test_a_BARE_RiskLimits_is_still_UNARMED():
    """Public API contract; production-derived narrative omitted."""
    assert RiskLimits().external_book_path is None
    assert RiskLimits().external_book_max_age_s == risk.DEFAULT_EXTERNAL_BOOK_MAX_AGE_S
    assert risk.cross_book_authority(RiskLimits())["armed"] is False



def test_the_SNAPSHOT_JOB_does_not_glob_the_TCC_PROTECTED_Downloads():
    """Public API contract; production-derived narrative omitted."""
    script = os.path.join(APP, "run_external_book_snapshot.sh")
    assert os.path.exists(script), "the scheduled snapshot job is missing"
    assert os.stat(script).st_mode & stat.S_IXUSR, "snapshot job is not executable"
    body = io.open(script, encoding="utf-8").read()
    assert "fidelity-exports" in body
    assert "ib-grader-venv" in body, (
        "the job must pin the production interpreter; `python3` here is 3.9.6")
    code = [ln for ln in body.splitlines() if not ln.lstrip().startswith("#")]
    assert not [ln for ln in code if "Downloads" in ln], (
        "the snapshot job reads ~/Downloads, which can wedge it under launchd")
