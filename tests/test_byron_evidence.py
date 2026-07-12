from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from exitmgr import byron_evidence


def _ticker(*, source_time=True):
    return SimpleNamespace(
        contract=SimpleNamespace(
            conId=123, symbol="SPY", right="C",
            lastTradeDateOrContractMonth="20260821", strike=700.0,
        ),
        bid=1.9,
        ask=2.0,
        bidSize=10,
        askSize=12,
        time=(datetime(2026, 7, 11, 18, 0, tzinfo=timezone.utc)
              if source_time else None),
    )


def test_capture_is_off_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv(byron_evidence.PATH_ENV, raising=False)
    before = set(tmp_path.iterdir())
    byron_evidence.record_audit({"ts": "2026-07-11T18:00:00Z", "event": "noop"})
    assert set(tmp_path.iterdir()) == before
    assert byron_evidence.capture_status()["reason"] == "capture_disabled"


def test_complete_quote_keeps_native_time_sizes_and_hash_chain(monkeypatch, tmp_path):
    path = tmp_path / "source.jsonl"
    monkeypatch.setenv(byron_evidence.PATH_ENV, str(path))
    byron_evidence.record_ibkr_quotes(
        [_ticker()], context="entry_option_chain", metadata={"underlying": "SPY"}
    )
    byron_evidence.record_audit({"ts": "2026-07-11T18:00:10Z", "event": "gated"})
    events = byron_evidence.verify_source_capture(path)
    assert len(events) == 2
    quote = events[0]["payload"]["quotes"][0]
    assert quote["source"] == "IBKR"
    assert quote["source_timestamp"] == "2026-07-11T18:00:00Z"
    assert quote["bid_size"] == 10.0
    assert quote["ask_size"] == 12.0
    assert len(quote["source_artifact_sha256"]) == 64
    assert byron_evidence.capture_status()["valid"] is True


def test_missing_native_quote_time_marks_whole_run_invalid(monkeypatch, tmp_path):
    path = tmp_path / "source.jsonl"
    monkeypatch.setenv(byron_evidence.PATH_ENV, str(path))
    byron_evidence.record_ibkr_quotes([_ticker(source_time=False)], context="exit_manager_mark")
    marker = path.with_name(path.name + ".INVALID.json")
    assert marker.exists()
    payload = json.loads(marker.read_text())
    assert payload["reason"] == "incomplete_quote_provenance"
    assert byron_evidence.capture_status()["valid"] is False


def test_broker_execution_id_is_idempotent(monkeypatch, tmp_path):
    path = tmp_path / "source.jsonl"
    monkeypatch.setenv(byron_evidence.PATH_ENV, str(path))
    fill = {
        "exec_id": "abc.1", "time": "2026-07-11T18:00:00Z",
        "con_id": 123, "shares": 1, "price": 2.0,
    }
    byron_evidence.record_fill(fill)
    byron_evidence.record_fill(fill)
    events = byron_evidence.verify_source_capture(path)
    assert len(events) == 1
    assert events[0]["event_id"] == "fill-abc.1"
