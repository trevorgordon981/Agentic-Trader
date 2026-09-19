"""Public API contract; production-derived narrative omitted."""
import importlib
import json
import os

import pytest

import rearm_check


COHORT = rearm_check.COHORT_START
SLIDE_ERA = "2037-08-18T14:28:00+00:00"
FLAT_ERA = "2037-08-25T14:28:00+00:00"


def _jsonl(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _world(tmp_path, entries):
    """Public API contract; production-derived narrative omitted."""
    _jsonl(tmp_path / "exits.log", [
        {"realized_pnl": 10.0, "realized_pnl_net": 10.0,
         "entry_ts": COHORT + "T14:00:00+00:00",
         "close_ts": "2037-08-2%d" % (i % 10), "reason": "target"}
        for i in range(30)
    ])
    _jsonl(tmp_path / "audit.jsonl", [{"event": "cycle_start", "net_liq": 100000.0}])
    _jsonl(tmp_path / "trades.log", entries)
    (tmp_path / "contributions.jsonl").write_text("")
    return tmp_path


@pytest.fixture
def world(tmp_path, monkeypatch):
    real_expanduser = os.path.expanduser

    def fake_expanduser(path):
        if path == "~/contributions.jsonl":
            return str(tmp_path / "contributions.jsonl")
        return real_expanduser(path)

    monkeypatch.setattr(rearm_check, "HERE", str(tmp_path))
    monkeypatch.setattr(os.path, "expanduser", fake_expanduser)
    monkeypatch.setitem(
        __import__("sys").modules, "book_return",
        type("_M", (), {"book_return": staticmethod(lambda net_liq: {"pnl_pct": -1.0})}))
    return tmp_path


def _run(capsys):
    code = rearm_check.main()
    return code, capsys.readouterr().out


def _verdict(out):
    line = [x for x in out.splitlines() if "VERDICT:" in x]
    assert len(line) == 1, out
    return line[0].split("VERDICT:", 1)[1].strip()


def _row(symbol, dte, hold, conviction, ts=SLIDE_ERA, **extra):
    row = {"ts": ts, "symbol": symbol, "dte_at_entry": dte,
           "intended_hold_days": hold, "conviction": conviction}
    row.update(extra)
    return row



LIVE_THREE = [
    _row("SYMY", 240, 30, 7, ts="2037-08-18T14:28:55+00:00"),
    _row("SYMZ", 160, 20, 7, ts="2037-08-20T14:24:34+00:00"),
    _row("SYMJ", 160, 20, 7, ts="2037-08-20T15:42:37+00:00"),
]


def test_three_synthetic_conviction_7_entries_are_compliant_under_the_rule_at_entry():
    """Public API contract; production-derived narrative omitted."""
    for row, required_then in zip(LIVE_THREE, (180, 120, 120)):
        floor, basis = rearm_check.required_floor_dte(row, 8)
        assert (floor, basis) == (required_then, "reconstructed"), row["symbol"]
        assert row["dte_at_entry"] >= floor, row["symbol"]


def test_criterion_5_does_not_report_60_percent_because_a_rule_changed(world, capsys):
    """Public API contract; production-derived narrative omitted."""
    others = [_row("OK%d" % i, 240, 30, 6) for i in range(7)]
    _world(world, LIVE_THREE + others)
    _code, out = _run(capsys)
    assert "5 doctrine compliance >= 95%   100% (10/10)" in out
    assert "cleared the DTE floor IN FORCE AT THEIR ENTRY" in out
    assert "under floor:" not in out


def test_a_later_tightening_cannot_fail_an_earlier_entry(world, capsys, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    entry_builder = importlib.import_module("exitmgr.entry_builder")
    _world(world, LIVE_THREE)
    assert _verdict(_run(capsys)[1]) == "RE-ARM ELIGIBLE"
    for tightened in (8, 12, 40):
        monkeypatch.setattr(entry_builder, "DEBIT_HOLD_FLOOR_MULTIPLE", tightened)
        code, out = _run(capsys)
        assert _verdict(out) == "RE-ARM ELIGIBLE", tightened
        assert code == 0


def test_the_current_era_is_graded_at_the_flat_multiple(world, capsys):
    """Public API contract; production-derived narrative omitted."""

    row = _row("LATE", 100, 21, 10, ts=FLAT_ERA)
    assert rearm_check.required_floor_dte(row, 8) == (168, "reconstructed")
    _world(world, [row])
    code, out = _run(capsys)
    assert "0% (0/1)" in out
    assert "under floor: LATE" in out
    assert _verdict(out) == "NOT YET" and code == 1



def test_a_stamped_floor_beats_every_reconstruction():
    """Public API contract; production-derived narrative omitted."""


    row = _row("STAMPED", 160, 30, 7, doctrine_dte_floor=150, doctrine_hold_multiple=4)
    assert rearm_check.required_floor_dte(row, 8) == (150, "stamp")


def test_a_stamped_multiple_is_used_when_no_floor_is_stamped():
    row = _row("STAMPED", 160, 30, 7, doctrine_hold_multiple=5)
    assert rearm_check.required_floor_dte(row, 8) == (150, "stamp")


def test_a_stamped_row_is_immune_to_a_future_doctrine_change(world, capsys, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    entry_builder = importlib.import_module("exitmgr.entry_builder")
    row = _row("STAMPED", 213, 30, 7, ts=FLAT_ERA, doctrine_dte_floor=180,
               doctrine_hold_multiple=6, doctrine_side="debit")
    _world(world, [row])
    assert _verdict(_run(capsys)[1]) == "RE-ARM ELIGIBLE"
    monkeypatch.setattr(entry_builder, "DEBIT_HOLD_FLOOR_MULTIPLE", 40)
    code, out = _run(capsys)
    assert _verdict(out) == "RE-ARM ELIGIBLE" and code == 0
    assert "1 stamped at entry, 0 reconstructed" in out


def test_a_credit_stamp_closes_the_known_limb():
    """Public API contract; production-derived narrative omitted."""
    stamped = _row("CSP", 30, 20, 7, ts=FLAT_ERA, doctrine_dte_floor=20,
                   doctrine_hold_multiple=1, doctrine_side="credit")
    floor, basis = rearm_check.required_floor_dte(stamped, 8)
    assert (floor, basis) == (20, "stamp") and stamped["dte_at_entry"] >= floor
    unstamped = _row("CSP", 30, 20, 7, ts=FLAT_ERA)
    floor, _basis = rearm_check.required_floor_dte(unstamped, 8)
    assert floor == 160 and unstamped["dte_at_entry"] < floor



def test_a_missing_hold_still_counts_against_and_names_the_row(world, capsys):
    """Public API contract; production-derived narrative omitted."""
    _world(world, [
        _row("GOOD", 240, 30, 6),
        {"ts": "2037-08-21T14:28:33+00:00", "symbol": "SYMG", "conviction": 7,
         "dte_at_entry": 300, "intended_hold_days": None},
    ])
    code, out = _run(capsys)
    assert "50% (1/2)" in out
    assert "no intended_hold_days (counted as NON-compliant): SYMG" in out
    assert _verdict(out) == "NOT YET" and code == 1


def test_an_unnameable_policy_makes_the_criterion_unevaluated_not_wrong(world, capsys,
                                                                       monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    entry_builder = importlib.import_module("exitmgr.entry_builder")
    _world(world, [_row("LATE", 400, 30, 7, ts=FLAT_ERA)])
    assert _verdict(_run(capsys)[1]) == "RE-ARM ELIGIBLE"
    monkeypatch.setattr(entry_builder, "DEBIT_HOLD_FLOOR_MULTIPLE", 40)
    code, out = _run(capsys)
    assert "[UNEV] 5 doctrine compliance" in out
    assert "under a doctrine this file cannot name" in out
    assert "criteria evaluated: 5 of 6" in out
    assert _verdict(out) == "NOT YET" and code == 1


def test_a_stamped_row_survives_the_drift_that_unevaluates_an_unstamped_one(world, capsys,
                                                                            monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    entry_builder = importlib.import_module("exitmgr.entry_builder")
    monkeypatch.setattr(entry_builder, "DEBIT_HOLD_FLOOR_MULTIPLE", 40)
    _world(world, [_row("STAMPED", 213, 30, 7, ts=FLAT_ERA, doctrine_dte_floor=180)])
    code, out = _run(capsys)
    assert _verdict(out) == "RE-ARM ELIGIBLE" and code == 0


def test_a_row_with_no_conviction_is_graded_at_the_strictest_multiple_of_its_era():
    """Public API contract; production-derived narrative omitted."""
    row = {"ts": SLIDE_ERA, "symbol": "NOCONV", "dte_at_entry": 213, "intended_hold_days": 30}
    assert rearm_check.required_floor_dte(row, 8) == (240, "reconstructed_strict")
    row_ok = dict(row, dte_at_entry=240)
    floor, basis = rearm_check.required_floor_dte(row_ok, 8)
    assert row_ok["dte_at_entry"] >= floor and basis == "reconstructed_strict"


def test_the_strict_fallback_is_visible_in_the_evidence_line(world, capsys):
    _world(world, [{"ts": SLIDE_ERA, "symbol": "NOCONV", "dte_at_entry": 240,
                    "intended_hold_days": 30}])
    _code, out = _run(capsys)
    assert "recorded no conviction and were graded at the STRICTEST multiple" in out


def test_an_unreadable_live_multiple_still_fails_closed(world, capsys, monkeypatch):
    entry_builder = importlib.import_module("exitmgr.entry_builder")
    monkeypatch.setattr(entry_builder, "DEBIT_HOLD_FLOOR_MULTIPLE", 0)
    _world(world, LIVE_THREE)
    code, out = _run(capsys)
    assert "[UNEV] 5 doctrine compliance" in out
    assert _verdict(out) == "NOT YET" and code == 1



def test_the_policy_history_matches_the_recovered_source():
    """Public API contract; production-derived narrative omitted."""
    slide = rearm_check._POLICY_HISTORY[0][2]
    assert [slide(c) for c in range(1, 11)] == [8, 8, 8, 8, 8, 8, 6, 5, 4, 4]
    flat = rearm_check._POLICY_HISTORY[-1][2]
    assert [flat(c) for c in range(1, 11)] == [8] * 10


def test_the_archive_agrees_with_the_live_entry_path_today():
    """Public API contract; production-derived narrative omitted."""
    entry_builder = importlib.import_module("exitmgr.entry_builder")
    assert (rearm_check._HISTORY_CURRENT_MULTIPLE
            == entry_builder.DEBIT_HOLD_FLOOR_MULTIPLE)


def test_the_era_boundary_is_the_flattening_commit():
    """Public API contract; production-derived narrative omitted."""
    assert rearm_check._POLICY_HISTORY[-1][0] == "2037-08-22T03:54:05+00:00"
    before = {"ts": "2037-08-22T03:54:04+00:00", "conviction": 9,
              "dte_at_entry": 100, "intended_hold_days": 20}
    after = dict(before, ts="2037-08-22T03:54:06+00:00")
    assert rearm_check.required_floor_dte(before, 8)[0] == 80
    assert rearm_check.required_floor_dte(after, 8)[0] == 160
