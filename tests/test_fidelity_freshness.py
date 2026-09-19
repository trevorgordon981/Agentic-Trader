"""Public API contract; production-derived narrative omitted."""
import datetime as dt
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.expanduser("~/exitmgr-app"))

import fidelity_curate as FC
import fidelity_freshness as FF
try:
    from tests._fidelity_fixtures import (BUY_OPEN, SELL_CLOSE, export, row)
except ImportError:
    from _fidelity_fixtures import (BUY_OPEN, SELL_CLOSE, export, row)



@pytest.fixture
def posted(monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    sent = []
    monkeypatch.setattr(FF.alerting, "post",
                        lambda text, ch, **kw: sent.append(text) or True)
    return sent


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(FF, "STATE", str(tmp_path / "state.json"))


def run(tmp_path, *extra):
    out = tmp_path / "gold.jsonl"
    return FF.main(["--exports", str(tmp_path / "Accounts_History*.csv"),
                    "--out", str(out)] + list(extra)), out


def days_ago(n):
    """Public API contract; production-derived narrative omitted."""
    return (dt.date.today() - dt.timedelta(days=n)).strftime("%m/%d/%Y")


def iso_days_ago(n):
    """Public API contract; production-derived narrative omitted."""
    return (dt.date.today() - dt.timedelta(days=n)).isoformat()


def test_a_current_book_is_silent(tmp_path, posted):
    export(tmp_path, row(days_ago(3), BUY_OPEN, 1, 5.0, -500.67))
    rc, out = run(tmp_path)
    assert rc == 0
    assert posted == [], "a healthy run must not post"
    assert len(out.read_text().splitlines()) == 1


def test_a_curated_file_behind_the_exports_alarms(tmp_path, posted):
    """Public API contract; production-derived narrative omitted."""
    export(tmp_path, row(days_ago(3), BUY_OPEN, 1, 5.0, -500.67))
    out = tmp_path / "gold.jsonl"
    rc = FF.main(["--exports", str(tmp_path / "Accounts_History*.csv"),
                  "--out", str(out), "--no-write"])

    assert rc == 1
    assert posted, "a stale book must reach Slack"


def test_stale_curation_is_detected_from_the_file_on_disk_not_from_the_run(tmp_path, posted):
    """Public API contract; production-derived narrative omitted."""
    export(tmp_path, row(days_ago(1), BUY_OPEN, 1, 5.0, -500.67))
    out = tmp_path / "gold.jsonl"
    stale = dict.fromkeys(FC.SCHEMA)
    stale.update({"entry_date": "2026-07-01", "exit_date": "2026-07-01", "acct_no": "100000001"})
    out.write_text(json.dumps(stale) + "\n")
    rc = FF.main(["--exports", str(tmp_path / "Accounts_History*.csv"),
                  "--out", str(out), "--no-write"])
    assert rc == 1
    assert any("STALE CURATION" in m for m in posted)


def test_exports_that_stop_arriving_alarm_even_though_curation_is_perfect(tmp_path, posted):
    """Public API contract; production-derived narrative omitted."""



    export(tmp_path, row(days_ago(30), BUY_OPEN, 1, 5.0, -500.67))
    rc, out = run(tmp_path)
    assert rc == 1
    assert any("NO FRESH EXPORT" in m for m in posted)

    assert len(out.read_text().splitlines()) == 1
    assert not any("STALE CURATION" in m for m in posted)


def test_quiet_weeks_inside_the_threshold_do_not_page(tmp_path, posted):
    """Public API contract; production-derived narrative omitted."""
    export(tmp_path, row(days_ago(10), BUY_OPEN, 1, 5.0, -500.67))
    rc, _ = run(tmp_path)
    assert rc == 0 and posted == []


def test_a_collapsed_rebuild_is_refused_rather_than_written(tmp_path, posted):
    """Public API contract; production-derived narrative omitted."""
    out = tmp_path / "gold.jsonl"
    good = [dict.fromkeys(FC.SCHEMA, None) for _ in range(100)]
    for i, g in enumerate(good):
        g.update({"entry_date": iso_days_ago(3), "exit_date": iso_days_ago(3)})
    out.write_text("".join(json.dumps(g) + "\n" for g in good))
    export(tmp_path, row(days_ago(3), BUY_OPEN, 1, 5.0, -500.67))
    rc = FF.main(["--exports", str(tmp_path / "Accounts_History*.csv"), "--out", str(out)])
    assert rc == 1
    assert any("REFUSED TO WRITE" in m for m in posted)
    assert len(out.read_text().splitlines()) == 100, "the previous file must survive intact"


def test_the_alarm_is_edge_triggered(tmp_path, posted):
    """Public API contract; production-derived narrative omitted."""
    export(tmp_path, row(days_ago(30), BUY_OPEN, 1, 5.0, -500.67))
    run(tmp_path)
    run(tmp_path)
    assert len(posted) == 1, "a persistent problem must not repeat every run"


    other = BUY_OPEN.replace("$320", "$400")
    export(tmp_path, row(days_ago(1), other, 1, 5.0, -500.67),
           name="Accounts_History (1).csv")
    rc, _ = run(tmp_path)
    assert rc == 0
    assert len(posted) == 2 and "current again" in posted[1]


def test_a_curation_crash_still_reaches_slack(tmp_path, posted, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    export(tmp_path, row(days_ago(1), BUY_OPEN, 1, 5.0, -500.67))
    monkeypatch.setattr(FC, "curate",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("parser exploded")))
    rc, out = run(tmp_path)
    assert rc == 1
    assert any("curation FAILED" in m and "parser exploded" in m for m in posted)


def test_blocking_anomalies_page_and_benign_notes_do_not(tmp_path, posted):
    """Public API contract; production-derived narrative omitted."""
    export(tmp_path, row(days_ago(3), BUY_OPEN, 1, 5.0, -500.67),
           row(days_ago(2), SELL_CLOSE, -1, 6.0, 599.33),
           row(days_ago(1), "EXPIRED CALL (AAPL) APPLE INC AUG 28 26 $320 as of 2026-08-28 "
                            "(100 SHS)", -5, "", 0.00))
    rc, _ = run(tmp_path)
    assert rc == 1
    assert any("ANOMALIES" in m and "unmatched_close" in m for m in posted)


def test_the_thresholds_sit_where_the_measured_data_says_they_should():
    """Public API contract; production-derived narrative omitted."""
    assert 10 <= FF.EXPORT_STALE_DAYS <= 21, "must clear the observed 9-day max, but still bite"
    assert 1 <= FF.CURATION_LAG_DAYS <= 3, "a healthy run leaves zero lag"
    assert 0.5 < FF.MIN_ROW_RATIO < 1.0
