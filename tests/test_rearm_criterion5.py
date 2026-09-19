"""Public API contract; production-derived narrative omitted."""
import importlib
import json
import os

import pytest

import rearm_check


COHORT = rearm_check.COHORT_START


def _jsonl(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _passing_world(tmp_path, entries):
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


def _entry(symbol="SYMY", dte=240, hold=30, day="18"):
    return {"ts": "2037-08-%sT14:28:00+00:00" % day, "symbol": symbol,
            "dte_at_entry": dte, "intended_hold_days": hold}


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
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
    """Public API contract; production-derived narrative omitted."""
    line = [x for x in out.splitlines() if "VERDICT:" in x]
    assert len(line) == 1, out
    return line[0].split("VERDICT:", 1)[1].strip()


def test_eligible_is_reachable_when_all_six_criteria_are_met(world, capsys):
    """Public API contract; production-derived narrative omitted."""
    _passing_world(world, [_entry(dte=240, hold=30)])
    code, out = _run(capsys)
    assert _verdict(out) == "RE-ARM ELIGIBLE"
    assert code == 0
    assert "criteria evaluated: 6 of 6" in out


def test_mixed_or_unknown_pnl_can_never_rearm_capital(world, capsys):
    """Public API contract; production-derived narrative omitted."""
    _passing_world(world, [_entry(dte=240, hold=30)])
    _jsonl(world / "exits.log", [
        {"realized_pnl": 10.0, "entry_ts": COHORT + "T14:00:00+00:00",
         "close_ts": "2037-08-2%d" % (i % 10), "reason": "target"}
        for i in range(30)
    ])
    code, out = _run(capsys)
    assert _verdict(out) == "NOT YET" and code == 1
    assert "[UNEV] 2 expectancy > $0" in out
    assert "[UNEV] 3 cohort net realized > $0" in out
    assert "known subtotal $+0.00 is NOT a total" in out


@pytest.mark.parametrize("entries, why", [
    ([], "no entry rows at all"),
    ([{"ts": "2037-08-18T14:00:00+00:00", "symbol": "SYMY"}], "rows carry no dte_at_entry"),
    ([_entry(day="17")], "the only entry predates the cohort"),
])
def test_eligible_is_unreachable_while_criterion_5_is_unevaluated(world, capsys, entries, why):
    """Public API contract; production-derived narrative omitted."""
    _passing_world(world, entries)
    code, out = _run(capsys)
    assert _verdict(out) == "NOT YET", why
    assert code == 1
    assert "[UNEV] 5 doctrine compliance" in out
    assert "UNEVALUATED" in out
    assert "criteria evaluated: 5 of 6" in out


def test_unevaluated_warning_prints_on_every_path_not_only_below_n30(world, capsys):
    """Public API contract; production-derived narrative omitted."""
    _passing_world(world, [])
    code, out = _run(capsys)
    assert "  [PASS] 1 closed trades >= 30" in out
    assert "!! UNEVALUATED: 5 doctrine compliance >= 95%" in out
    assert "cannot return RE-ARM ELIGIBLE" in out


def test_criterion_5_is_counted_in_the_verdict_not_merely_displayed(world, capsys):
    """Public API contract; production-derived narrative omitted."""
    entries = [_entry(symbol="OK%d" % i, dte=240, hold=30) for i in range(19)]
    entries.append(_entry(symbol="SHORT", dte=100, hold=30))
    _passing_world(world, entries)
    code, out = _run(capsys)
    assert _verdict(out) == "RE-ARM ELIGIBLE" and code == 0
    entries.append(_entry(symbol="SHORT2", dte=100, hold=30))
    _passing_world(world, entries)
    code, out = _run(capsys)
    assert _verdict(out) == "NOT YET" and code == 1
    assert "under floor: SHORT" in out


def test_entry_without_intended_hold_days_counts_against_not_excluded(world, capsys):
    """Public API contract; production-derived narrative omitted."""
    _passing_world(world, [
        _entry(symbol="GOOD", dte=240, hold=30),
        {"ts": "2037-08-21T14:28:00+00:00", "symbol": "SYMG",
         "dte_at_entry": 300, "intended_hold_days": None},
    ])
    code, out = _run(capsys)
    assert "50% (1/2)" in out
    assert "no intended_hold_days (counted as NON-compliant): SYMG" in out
    assert _verdict(out) == "NOT YET" and code == 1


def test_a_doctrine_change_does_not_retroactively_fail_a_historical_entry(world, capsys,
                                                                          monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    entry_builder = importlib.import_module("exitmgr.entry_builder")


    _passing_world(world, [dict(_entry(dte=213, hold=30), conviction=7)])
    assert _verdict(_run(capsys)[1]) == "RE-ARM ELIGIBLE"
    monkeypatch.setattr(entry_builder, "DEBIT_HOLD_FLOOR_MULTIPLE", 40)
    code, out = _run(capsys)
    assert _verdict(out) == "RE-ARM ELIGIBLE" and code == 0
    assert "cleared the DTE floor IN FORCE AT THEIR ENTRY" in out


def test_unreadable_doctrine_multiple_fails_closed(world, capsys, monkeypatch):









    entry_builder = importlib.import_module("exitmgr.entry_builder")
    _passing_world(world, [_entry(dte=240, hold=30)])
    monkeypatch.setattr(entry_builder, "DEBIT_HOLD_FLOOR_MULTIPLE", 0)
    code, out = _run(capsys)
    assert "[UNEV] 5 doctrine compliance" in out
    assert _verdict(out) == "NOT YET" and code == 1


def test_halt_still_outranks_everything(world, capsys, monkeypatch):
    _passing_world(world, [_entry(dte=240, hold=30)])
    monkeypatch.setitem(
        __import__("sys").modules, "book_return",
        type("_M", (), {"book_return": staticmethod(lambda net_liq: {"pnl_pct": -40.0})}))
    code, out = _run(capsys)
    assert _verdict(out) == "HALT" and code == 2


def test_missing_or_malformed_exit_evidence_cannot_rearm(world, capsys):
    _passing_world(world, [_entry(dte=240, hold=30)])
    (world / "exits.log").write_text('{"realized_pnl": 10}\n{"partial":')
    code, out = _run(capsys)
    assert _verdict(out) == "NOT YET" and code == 1
    assert "[UNEV] 1 closed trades >= 30" in out
    assert "not complete JSON" in out


@pytest.mark.parametrize("fault", ["missing_net_liq", "book_nan"])
def test_unevaluated_book_halt_input_blocks_rearm(world, capsys, monkeypatch, fault):
    _passing_world(world, [_entry(dte=240, hold=30)])
    if fault == "missing_net_liq":
        _jsonl(world / "audit.jsonl", [{"event": "cycle_start"}])
    else:
        monkeypatch.setitem(
            __import__("sys").modules, "book_return",
            type("_M", (), {"book_return": staticmethod(
                lambda net_liq: {"pnl_pct": float("nan")})}))
    code, out = _run(capsys)
    assert _verdict(out) == "NOT YET" and code == 1
    assert "[UNEV] book P&L <= -35%" in out
    assert "UNEVALUATED HALT INPUTS" in out


def test_missing_net_liq_cannot_turn_single_loss_halt_into_zero_percent(world, capsys):
    _passing_world(world, [_entry(dte=240, hold=30)])
    _jsonl(world / "audit.jsonl", [])
    code, out = _run(capsys)
    assert _verdict(out) == "NOT YET" and code == 1
    assert "[UNEV] single loss > 15% of net liq" in out
