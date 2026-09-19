"""Public API contract; production-derived narrative omitted."""
import os

import pytest

from exitmgr import exec_capture, flex_ingest
from exitmgr.config import ConstructionConfig

REAL_CONFIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "config.yaml")
SHIPPED = ConstructionConfig.__dataclass_fields__["max_combo_spread_pct"].default


def _write(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(text)
    return p


def _real_config_text():
    with open(REAL_CONFIG) as fh:
        return fh.read()


def _typo_config_text():
    """Public API contract; production-derived narrative omitted."""
    txt = _real_config_text()
    old = "max_combo_spread_pct: %s" % SHIPPED
    assert txt.count(old) == 1
    return txt.replace(old, "max_combo_spread_pcx: %s" % SHIPPED)


@pytest.fixture
def calls(monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    seen = {}

    def fake_flex(**kw):
        seen["flex"] = kw.get("config")
        return {"ok": True}

    def fake_capture(**kw):
        seen["capture"] = kw.get("config")
        return {"ok": True}

    monkeypatch.setattr(flex_ingest, "ingest_flex", fake_flex)
    monkeypatch.setattr(exec_capture, "capture_external_fills_blocking", fake_capture)
    return seen



@pytest.mark.parametrize("key", ["flex", "capture"])
def test_a_misspelt_key_stops_the_run_and_is_named(tmp_path, monkeypatch, capsys, calls, key):
    _write(tmp_path, _typo_config_text())
    monkeypatch.chdir(tmp_path)
    rc = flex_ingest._main(["--dry-run"]) if key == "flex" else exec_capture._main([])
    err = capsys.readouterr().err
    assert rc != 0, "a config the loader REFUSES must not exit 0"
    assert key not in calls, "the ingest ran anyway, against defaults -- the exact silent failure"
    assert "max_combo_spread_pcx" in err, "the refusal must name the offending key"


@pytest.mark.parametrize("key", ["flex", "capture"])
def test_unparseable_yaml_stops_the_run(tmp_path, monkeypatch, capsys, calls, key):
    _write(tmp_path, "rules: [1, 2\n  bad: :\n")
    monkeypatch.chdir(tmp_path)
    rc = flex_ingest._main(["--dry-run"]) if key == "flex" else exec_capture._main([])
    capsys.readouterr()
    assert rc != 0
    assert key not in calls


@pytest.mark.parametrize("key", ["flex", "capture"])
def test_an_invalid_value_stops_the_run(tmp_path, monkeypatch, capsys, calls, key):
    """Public API contract; production-derived narrative omitted."""
    txt = _real_config_text()
    assert txt.count("max_entry_spread_pct:") == 1
    _write(tmp_path, txt.replace("max_entry_spread_pct:", "max_entry_spread_pct: nope #"))
    monkeypatch.chdir(tmp_path)
    rc = flex_ingest._main(["--dry-run"]) if key == "flex" else exec_capture._main([])
    err = capsys.readouterr().err
    assert rc != 0
    assert key not in calls
    assert "max_entry_spread_pct" in err



@pytest.mark.parametrize("key", ["flex", "capture"])
def test_a_good_config_still_reaches_the_ingest(tmp_path, monkeypatch, capsys, calls, key):
    _write(tmp_path, _real_config_text())
    monkeypatch.chdir(tmp_path)
    rc = flex_ingest._main(["--dry-run"]) if key == "flex" else exec_capture._main([])
    capsys.readouterr()
    assert rc == 0
    assert calls[key] is not None, "a VALID config must reach the ingest, not be dropped"
    assert calls[key].construction.max_combo_spread_pct == SHIPPED



@pytest.mark.parametrize("key", ["flex", "capture"])
def test_no_config_on_disk_is_still_tolerated(tmp_path, monkeypatch, capsys, calls, key):
    """Public API contract; production-derived narrative omitted."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(os.path, "exists", lambda _p: False)
    rc = flex_ingest._main(["--dry-run"]) if key == "flex" else exec_capture._main([])
    capsys.readouterr()
    assert rc == 0
    assert key in calls and calls[key] is None
