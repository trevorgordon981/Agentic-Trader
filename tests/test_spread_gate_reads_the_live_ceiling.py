"""Public API contract; production-derived narrative omitted."""
import io
import json
import os

import pytest
import yaml

import experiment_readout as R
from exitmgr.config import Config, ConstructionConfig


REAL_CONFIG = R.CONFIG_PATH


FIXTURE_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "fixtures", "config_live_snapshot.yaml")
SHIPPED = ConstructionConfig.__dataclass_fields__["max_combo_spread_pct"].default


def _config_with(tmp_path, value):
    """Public API contract; production-derived narrative omitted."""
    with io.open(FIXTURE_CONFIG, encoding="utf-8") as fh:
        txt = fh.read()
    old = "max_combo_spread_pct: %s" % SHIPPED
    assert txt.count(old) == 1, (
        "the pinned fixture no longer states the shipped ceiling verbatim -- re-pin it from "
        "config.yaml (see FIXTURE_CONFIG above)")
    p = tmp_path / "config.yaml"
    p.write_text(txt.replace(old, "max_combo_spread_pct: %s" % value))
    return str(p)


def _key_paths(node, prefix=()):
    """Public API contract; production-derived narrative omitted."""


    if prefix == ("trading", "sector_map"):
        return set()
    out = set()
    if isinstance(node, dict):
        for k, v in node.items():
            out.add(prefix + (str(k),))
            out |= _key_paths(v, prefix + (str(k),))
    return out


def _entries(tmp_path, spreads, day="2026-08-22"):
    """Public API contract; production-derived narrative omitted."""
    rows = [{"ts": "%sT10:%02d:00-07:00" % (day, i), "debit": 300.0, "entry_spread_pct": s}
            for i, s in enumerate(spreads)]
    (tmp_path / "trades.log").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return rows


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "APP", str(tmp_path))
    (tmp_path / "trades.log").write_text("")
    return tmp_path



def test_the_ceiling_is_never_a_constant_in_this_module():
    """Public API contract; production-derived narrative omitted."""
    assert not hasattr(R, "NEW_LIMIT")


def test_it_reads_the_same_ceiling_the_trader_enforces():
    """Public API contract; production-derived narrative omitted."""
    limit, baseline, err = R.live_combo_limit(FIXTURE_CONFIG)
    assert err is None
    assert limit == Config.from_yaml(FIXTURE_CONFIG).construction.max_combo_spread_pct
    assert baseline == SHIPPED


def test_the_pinned_fixture_is_still_a_faithful_stand_in():
    """Public API contract; production-derived narrative omitted."""
    if not os.path.exists(REAL_CONFIG):
        pytest.skip("live exitmgr config is not present on this host")
    with io.open(REAL_CONFIG, encoding="utf-8") as fh:
        live = yaml.safe_load(fh)
    with io.open(FIXTURE_CONFIG, encoding="utf-8") as fh:
        pinned = yaml.safe_load(fh)
    assert isinstance(live, dict), "the live config.yaml did not load as a mapping"

    live_keys, pinned_keys = _key_paths(live), _key_paths(pinned)
    added = sorted(".".join(k) for k in live_keys - pinned_keys)
    gone = sorted(".".join(k) for k in pinned_keys - live_keys)
    assert not (added or gone), (
        "\n\nTHE PINNED CONFIG FIXTURE HAS DRIFTED FROM THE LIVE config.yaml.\n\n"
        + ("  keys the LIVE file has and the fixture does not:\n"
           + "\n".join("    " + k for k in added) + "\n" if added else "")
        + ("  keys the FIXTURE has and the live file does not:\n"
           + "\n".join("    " + k for k in gone) + "\n" if gone else "")
        + "\nRe-pin it:  cp ~/exitmgr-app/config.yaml "
          "tests/fixtures/config_live_snapshot.yaml\n"
          "then re-read this file's tests -- a NEW required key can change what the loader does\n"
          "with the config every test above builds.\n")




    assert R.live_combo_limit(REAL_CONFIG)[2] is None


@pytest.mark.parametrize("value", [33.0, 35.0, 45.0, 50.0])
def test_the_limit_tracks_whatever_the_config_says(tmp_path, value):
    assert R.live_combo_limit(_config_with(tmp_path, value))[0] == value


def test_the_headline_quotes_the_live_ceiling(app, tmp_path, monkeypatch):
    monkeypatch.setattr(R, "CONFIG_PATH", _config_with(tmp_path, 33.0))
    assert "33.0%" in R.spread_experiment()[0]



def test_at_the_shipped_ceiling_it_reports_no_experiment_rather_than_a_comparison(
        app, tmp_path, monkeypatch):
    monkeypatch.setattr(R, "CONFIG_PATH", _config_with(tmp_path, SHIPPED))
    _entries(app, [38.0, 39.0, 40.0, 41.0, 42.0, 43.0])
    lines = R.spread_experiment()
    text = "\n".join(lines)



    assert lines[0] == ("*Spread gate* — `max_combo_spread_pct` = 45.0% (live config; "
                        "the shipped default)")
    assert "no experiment running" in text
    assert "reverted" in text
    assert "a probe IS in force" not in text
    assert "too early" not in text


def test_legal_entries_are_not_reported_as_leaks(app, tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    monkeypatch.setattr(R, "CONFIG_PATH", _config_with(tmp_path, 45.0))
    _entries(app, [38.0, 39.0, 40.0, 41.0, 42.0, 43.0])
    text = "\n".join(R.spread_experiment())
    assert "leak" not in text
    assert "above the 45.0% ceiling in force: 0" in text


def test_a_real_leak_is_still_named_at_the_shipped_ceiling(app, tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    monkeypatch.setattr(R, "CONFIG_PATH", _config_with(tmp_path, 45.0))
    _entries(app, [40.0, 46.5])
    text = "\n".join(R.spread_experiment())
    assert "the gate leaks" in text and "46.5%" in text



def test_a_ceiling_below_the_shipped_one_is_reported_as_a_probe(app, tmp_path, monkeypatch):
    monkeypatch.setattr(R, "CONFIG_PATH", _config_with(tmp_path, 35.0))
    _entries(app, [38.0, 39.0, 40.0, 41.0, 42.0, 43.0])
    text = "\n".join(R.spread_experiment())
    assert "a probe IS in force" in text
    assert "no experiment running" not in text
    assert "above the 35.0% ceiling in force: 6" in text
    assert "the gate leaks" in text and "43.0%" in text


def test_a_probe_with_too_few_entries_says_so(app, tmp_path, monkeypatch):
    monkeypatch.setattr(R, "CONFIG_PATH", _config_with(tmp_path, 35.0))
    _entries(app, [30.0, 31.0])
    text = "\n".join(R.spread_experiment())
    assert "too early" in text



def test_a_broken_config_reports_blindness_not_a_stale_number(app, tmp_path, monkeypatch):
    bad = tmp_path / "bad.yaml"
    bad.write_text("construction:\n  max_combo_spread_pcx: 35.0\n")
    monkeypatch.setattr(R, "CONFIG_PATH", str(bad))
    _entries(app, [38.0, 39.0, 40.0, 41.0, 42.0, 43.0])
    text = "\n".join(R.spread_experiment())
    assert "cannot read the live ceiling" in text
    assert "leak" not in text
    assert "ceiling in force" not in text


def test_a_missing_config_reports_blindness_too(app, tmp_path, monkeypatch):
    monkeypatch.setattr(R, "CONFIG_PATH", str(tmp_path / "nope.yaml"))
    text = "\n".join(R.spread_experiment())
    assert "cannot read the live ceiling" in text
