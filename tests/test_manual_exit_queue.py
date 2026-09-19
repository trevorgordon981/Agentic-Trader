import json
import multiprocessing
from types import SimpleNamespace

import pytest

from exitmgr.manual_exit_frontend import ManualExitPreflightError, parent_targets
from exitmgr.manual_exit_queue import ManualExitQueue, ManualExitQueueError, binding_sha
from exitmgr.runtime_identity import RuntimeIdentity


IDENTITY = RuntimeIdentity("1" * 40, "2" * 64)


def _add(path, value):
    ManualExitQueue(path).add([_request(value)])


def _request(value):
    rec = {"request_id": f"request-{value}", "created_at": "2026-08-25T00:00:00+00:00",
           "parent_con_id": value, "symbol": "TST", "quantity": 1,
           "campaign": {"campaign_seq": 1, "identity": ["TST", "C", "20270101", 10.0],
                        "lot_lines": [1], "first_lot_ts": "2026-08-25T00:00:00+00:00"},
           "journal_identity": {"symbol": "TST", "right": "C", "expiry": "20270101",
                                "strike": 10.0, "decision_id": "d", "ts": "2026-08-25"},
           "topology": [{"con_id": value, "position": 1, "role": "long"}],
           "code_version": IDENTITY.code_version, "policy_version": IDENTITY.policy_version,
           "action": "SELL", "order_type": "MARKET", "sec_type": "OPT"}
    rec["binding_sha"] = binding_sha(rec)
    return rec


def _position(con_id, symbol, quantity, sec_type="OPT"):
    return SimpleNamespace(
        contract=SimpleNamespace(conId=con_id, symbol=symbol, secType=sec_type),
        position=quantity,
    )


def test_queue_is_idempotent_atomic_and_owner_only(tmp_path):
    path = tmp_path / "manual_exits.json"
    q = ManualExitQueue(path)
    assert set(q.add([_request(11), _request(11), _request(12)])) == {11, 12}
    assert set(q.read()) == {11, 12}
    assert set(q.discard(11, "request-11")) == {12}
    assert path.stat().st_mode & 0o777 == 0o600


def test_corrupt_present_queue_refuses_instead_of_becoming_empty(tmp_path):
    path = tmp_path / "manual_exits.json"
    path.write_text("{bad")
    q = ManualExitQueue(path)
    with pytest.raises(ManualExitQueueError):
        q.read()
    with pytest.raises(ManualExitQueueError):
        q.add([_request(22)])
    assert path.read_text() == "{bad"


def test_concurrent_adds_do_not_lose_requests(tmp_path):
    path = str(tmp_path / "manual_exits.json")
    procs = [multiprocessing.Process(target=_add, args=(path, value))
             for value in range(100, 108)]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(5)
        assert proc.exitcode == 0
    assert set(ManualExitQueue(path).read()) == set(range(100, 108))


def test_nonempty_legacy_conid_queue_refuses(tmp_path):
    path = tmp_path / "manual_exits.json"
    path.write_text("[11]\n")
    with pytest.raises(ManualExitQueueError):
        ManualExitQueue(path).read()


def test_stale_clear_cannot_remove_a_different_request(tmp_path):
    path = tmp_path / "manual_exits.json"
    q = ManualExitQueue(path)
    q.add([_request(11)])
    with pytest.raises(ManualExitQueueError):
        q.discard(11, "different-request")
    assert q.read()[11]["request_id"] == "request-11"


def test_terminal_archive_is_exact_durable_and_idempotent(tmp_path):
    path = tmp_path / "manual_exits.json"
    q = ManualExitQueue(path)
    rec = _request(11)
    q.add([rec])
    evidence = {"broker_flat": True, "observed_at": "2026-09-18T20:00:00+00:00",
                "topology_con_ids": [11], "queued_campaign": rec["campaign"]}
    assert q.archive_terminal(11, rec["request_id"], rec["binding_sha"], evidence) == {}
    rows = [json.loads(line) for line in q.archive_path.read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["request"] == rec
    assert q.archive_path.stat().st_mode & 0o777 == 0o600

    q.add([rec])
    assert q.archive_terminal(11, rec["request_id"], rec["binding_sha"], evidence) == {}
    assert len(q.archive_path.read_text().splitlines()) == 1


def test_terminal_archive_retains_request_on_ambiguity_or_binding_drift(tmp_path):
    q = ManualExitQueue(tmp_path / "manual_exits.json")
    rec = _request(11)
    q.add([rec])
    with pytest.raises(ManualExitQueueError):
        q.archive_terminal(11, rec["request_id"], "0" * 64,
                           {"broker_flat": True, "observed_at": "now"})
    with pytest.raises(ManualExitQueueError):
        q.archive_terminal(11, rec["request_id"], rec["binding_sha"],
                           {"broker_flat": False})
    assert 11 in q.read()


def test_vertical_maps_to_one_parent_bag_request(tmp_path, sample_config):
    journal = tmp_path / "trades.log"
    journal.write_text(json.dumps({
        "contract_id": 111, "symbol": "SYMY", "right": "C", "quantity": 2,
        "debit": 600.0, "spread": {"short_con_id": 222, "short_strike": 85.0},
    }) + "\n")
    sample_config.journal.path = str(journal)
    assert parent_targets(sample_config, [
        _position(111, "SYMY", 2), _position(222, "SYMY", -2),
    ], symbol="SYMY", runtime_identity=IDENTITY) == {111}


@pytest.mark.parametrize("positions", [
    [_position(111, "SYMY", 2)],
    [_position(111, "SYMY", 2), _position(222, "SYMY", -1)],
    [_position(111, "SYMY", 2), _position(222, "SYMY", -2),
     _position(333, "SYMY", 1)],
])
def test_ambiguous_or_incomplete_vertical_refuses_all(tmp_path, sample_config, positions):
    journal = tmp_path / "trades.log"
    journal.write_text(json.dumps({
        "contract_id": 111, "symbol": "SYMY", "right": "C", "quantity": 2,
        "debit": 600.0, "spread": {"short_con_id": 222, "short_strike": 85.0},
    }) + "\n")
    sample_config.journal.path = str(journal)
    with pytest.raises(ManualExitPreflightError):
        parent_targets(sample_config, positions, symbol="SYMY", runtime_identity=IDENTITY)
