"""Public API contract; production-derived narrative omitted."""
import datetime as dt
import json

import pytest

import experiment_readout as R
from exitmgr.manager import ExitManager


def _write(path, rows):
    with open(path, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "APP", str(tmp_path))
    monkeypatch.setattr(R, "MARKS", str(tmp_path / "events.jsonl"))
    monkeypatch.setenv(R.SHADOW_LEDGER_ENV, str(tmp_path / "shadow.json"))
    _write(tmp_path / "events.jsonl", [])
    _write(tmp_path / "exits.log", [])
    return tmp_path


def _ledger(env, **kw):
    led = {"schema": "shadow_exit_experiment.v1",
           "started_at": dt.datetime.now().astimezone().isoformat(),
           "episodes": {}, "gate_passed": False}
    led.update(kw)
    (env / "shadow.json").write_text(json.dumps(led))
    return led



def test_the_constants_mirror_the_live_loop():
    """Public API contract; production-derived narrative omitted."""
    assert R.MAX_CLOSES == ExitManager.SHADOW_MAX_CLOSES
    assert R.MAX_DAYS == ExitManager.SHADOW_MAX_DAYS
    assert R.GATE_MIN_EPISODES == ExitManager.SHADOW_GATE_MIN_EPISODES
    assert R.GATE_MIN_SYMBOLS == ExitManager.SHADOW_GATE_MIN_SYMBOLS
    assert R.GATE_MIN_ENTRY_WEEKS == ExitManager.SHADOW_GATE_MIN_ENTRY_WEEKS
    assert R.REMIT_ACTIONS == ExitManager.MGMT_REMIT_ACTIONS


def test_it_reads_the_same_ledger_the_loop_writes(env, monkeypatch):
    assert R.SHADOW_LEDGER_ENV == ExitManager._SHADOW_EXPERIMENT_ENV
    assert R.SHADOW_LEDGER_DEFAULT == ExitManager._SHADOW_EXPERIMENT_DEFAULT
    assert R._ledger_path() == str(env / "shadow.json")
    monkeypatch.delenv(R.SHADOW_LEDGER_ENV)
    assert R._ledger_path() == R.SHADOW_LEDGER_DEFAULT


def test_no_ledger_is_a_state_not_a_crash(env):
    txt = "\n".join(R.shadow_experiment())
    assert "clock not started" in txt



def test_episodes_come_from_the_ledger_not_from_the_marks(env):
    """Public API contract; production-derived narrative omitted."""
    _ledger(env, episodes={
        "camp-a": {"con_id": 1, "symbol": "AAA", "entry_week": "2026-W34"},
        "camp-b": {"con_id": 2, "symbol": "BBB", "entry_week": "2026-W35"}})
    _write(env / "events.jsonl", [
        {"event_type": "position_path", "con_id": 1, "ts": "2026-08-21T0%d:00:00" % i,
         "mgmt_action": "cut", "pnl_pct": -12.0} for i in range(7)])
    txt = "\n".join(R.shadow_experiment())
    assert "binding divergence episodes 2" in txt
    assert "across 2 symbols / 2 entry weeks" in txt


def test_take_profit_is_no_longer_a_divergence_signal(env):
    """Public API contract; production-derived narrative omitted."""
    _write(env / "exits.log", [
        {"con_id": c, "realized_pnl": 100.0, "realized_pnl_pct": 20.0, "ts": "2026-08-21T12:00:00"}
        for c in (7, 8, 9)])

    def _marks(extra):
        rows = []
        for c in (7, 8, 9):
            r = {"event_type": "position_path", "con_id": c, "ts": "2026-08-21T09:00:00",
                 "pnl_pct": 80.0}
            r.update(extra)
            rows.append(r)
        _write(env / "events.jsonl", rows)

    for shape in ({"mgmt_action": "out_of_remit", "mgmt_proposed_action": "take_profit"},
                  {"mgmt_action": "take_profit"}):
        _marks(shape)
        n, MUTX, _, _ = R.paired_delta()
        assert n == 3
        assert MUTX == pytest.approx(0.0), \
            "take_profit counted as a divergence (%+.1fpp) for mark shape %r" % (MUTX, shape)

    _marks({"mgmt_action": "cut"})
    n, MUTX, _, _ = R.paired_delta()
    assert n == 3 and MUTX == pytest.approx(60.0)


def test_closes_are_campaigns_and_phantoms_are_excluded(env):
    _write(env / "exits.log", [
        {"con_id": 1, "realized_pnl": 10.0, "realized_pnl_pct": 5.0, "ts": "2026-08-21T10:00:00"},
        {"con_id": 1, "realized_pnl": 20.0, "realized_pnl_pct": 9.0, "ts": "2026-08-21T11:00:00"},
        {"con_id": 2, "realized_pnl": -800.0, "realized_pnl_pct": -100.0,
         "exit_price_per_share": 0.0, "reason": "stop", "ts": "2026-08-21T10:00:00"},
        {"con_id": 3, "realized_pnl": -800.0, "realized_pnl_pct": -100.0,
         "exit_price_per_share": 0.0, "reason": "expired", "ts": "2026-08-21T10:00:00"},
    ])


    assert R.completed_closes() == 2


def test_closes_before_the_experiment_started_do_not_count(env):
    _write(env / "exits.log", [
        {"con_id": 1, "realized_pnl": 10.0, "realized_pnl_pct": 5.0, "ts": "2026-01-01T10:00:00"},
        {"con_id": 2, "realized_pnl": 10.0, "realized_pnl_pct": 5.0, "ts": "2026-08-21T10:00:00"}])
    assert R.completed_closes("2026-08-01T00:00:00") == 1



def test_it_reports_progress_against_the_cap(env):
    _ledger(env)
    txt = "\n".join(R.shadow_experiment())
    assert "completed closes 0 / %d" % R.MAX_CLOSES in txt
    assert "/ %d days" % R.MAX_DAYS in txt
    assert "running. Retires at the EARLIER of" in txt


def test_the_close_cap_is_reported_as_reached(env):
    _ledger(env, started_at="2026-08-20T00:00:00+00:00")
    _write(env / "exits.log", [
        {"con_id": i, "realized_pnl": 1.0, "realized_pnl_pct": 1.0, "ts": "2026-08-21T10:00:00"}
        for i in range(1, R.MAX_CLOSES + 1)])
    txt = "\n".join(R.shadow_experiment())
    assert "CAP REACHED" in txt and "operationally redundant" in txt


def test_a_zero_contract_id_is_still_a_close(env):
    """Public API contract; production-derived narrative omitted."""
    _write(env / "exits.log", [
        {"con_id": 0, "realized_pnl": 1.0, "realized_pnl_pct": 1.0, "ts": "2026-08-21T10:00:00"}])
    assert R.completed_closes() == 1


def test_the_day_cap_is_reported_as_reached(env):
    old = (dt.datetime.now().astimezone() - dt.timedelta(days=R.MAX_DAYS + 1)).isoformat()
    _ledger(env, started_at=old)
    txt = "\n".join(R.shadow_experiment())
    assert "CAP REACHED" in txt


def test_a_recorded_gate_pass_stops_the_cap_retiring_it(env):
    old = (dt.datetime.now().astimezone() - dt.timedelta(days=R.MAX_DAYS + 1)).isoformat()
    _ledger(env, started_at=old, gate_passed=True)
    txt = "\n".join(R.shadow_experiment())
    assert "gate recorded as PASSED" in txt and "CAP REACHED" not in txt


def test_a_retired_experiment_reports_as_retired(env):
    _ledger(env, retired_at="2026-09-01T00:00:00", retired_reason="operationally redundant")
    txt = "\n".join(R.shadow_experiment())
    assert "*RETIRED* 2026-09-01T00:00:00" in txt and "operationally redundant" in txt


def test_out_of_remit_traffic_is_surfaced_as_marks_not_episodes(env):
    _ledger(env)
    _write(env / "events.jsonl", [
        {"event_type": "position_path", "con_id": 4, "ts": "2026-08-21T09:00:00",
         "mgmt_action": "out_of_remit", "mgmt_proposed_action": "arm_trail"} for _ in range(3)])
    txt = "\n".join(R.shadow_experiment())
    assert "out-of-remit proposals still arriving (marks, not episodes): arm_trail 3" in txt
