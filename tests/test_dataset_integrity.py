import json
import hashlib

import pytest

from exitmgr import dataset_integrity as di


def test_unmarked_and_unknown_status_fail_closed_for_every_purpose():
    for row in ({"kind": "trade"},
                {"kind": "trade", "record_status": "MAYBE", "canonical": True,
                 "usable_for_training": True, "usable_for_pnl": True},
                {"kind": "trade", "record_status": "CANONICAL", "canonical": True}):
        assert di.allowed(row, "training")[0] is False
        assert di.allowed(row, "pnl")[0] is False


def test_migrate_ledger_quarantines_unmarked_and_keeps_explicit_canonical(tmp_path):
    path = tmp_path / "trade_dataset.jsonl"
    canonical = {"kind": "no_trade", "record_status": "CANONICAL", "canonical": True,
                 "usable_for_training": True, "usable_for_pnl": False}
    unmarked = {"kind": "trade", "source": "app", "symbol": "OLD"}
    unknown = {"kind": "trade", "record_status": "MAYBE", "symbol": "ODD"}
    path.write_text("\n".join(json.dumps(row) for row in (canonical, unmarked, unknown)) + "\n")
    result = di.migrate_ledger(str(path))
    assert result["before"] == 3 and result["canonical"] == 1 and result["migrated"] == 2
    kept = [json.loads(line) for line in path.read_text().splitlines()]
    assert kept == [canonical]
    quarantined = [json.loads(line) for line in
                   (tmp_path / "trade_dataset.quarantine.jsonl").read_text().splitlines()]
    assert len(quarantined) == 2
    assert all(row["record_status"] == "LEGACY" and row["canonical"] is False
               and row["usable_for_training"] is False and row["usable_for_pnl"] is False
               for row in quarantined)
    assert (tmp_path / "trade_dataset.jsonl.pre-canonical.bak").exists()


def _eligible_no_trade():
    return {
        "schema": "trade_dataset.v2", "kind": "no_trade", "source": "trader",
        "ts": "2026-08-24T14:37:55+00:00", "_dedup_key": "no_trade:exact",
        "record_status": "CANONICAL", "canonical": True,
        "usable_for_training": True, "usable_for_pnl": False,
    }


def _append_exact(path, row, **overrides):
    raw = path.read_bytes().splitlines(keepends=True)[0]
    args = {
        "target_line_sha256": di.raw_jsonl_line_sha256(raw),
        "target_dedup_key": row["_dedup_key"],
        "target_kind": row["kind"], "target_source": row["source"],
        "target_ts": row["ts"], "reason_code": "MISLABELED_EMPTY_SLATE",
        "evidence_line_sha256": ["a" * 64],
        "created_at": "2026-08-25T16:00:00-07:00",
    }
    args.update(overrides)
    return di.append_training_exclusion(str(path), **args)


def test_training_exclusion_is_append_only_exact_and_idempotent(tmp_path):
    path = tmp_path / "trade_dataset.jsonl"
    row = _eligible_no_trade()
    path.write_text(json.dumps(row, sort_keys=True) + "\n")
    source_before = path.read_bytes()
    source_sha = hashlib.sha256(source_before).hexdigest()

    first = _append_exact(path, row)
    second = _append_exact(path, row, created_at="2026-08-25T17:00:00-07:00")

    assert first["appended"] is True and second["appended"] is False
    assert path.read_bytes() == source_before
    assert hashlib.sha256(path.read_bytes()).hexdigest() == source_sha
    sidecar = tmp_path / "trade_dataset.training_exclusions.jsonl"
    assert len(sidecar.read_text().splitlines()) == 1
    loaded = di.load_training_exclusions(str(path), di.read_source_rows(str(path)))
    assert set(loaded["records_by_target_sha256"]) == {
        di.raw_jsonl_line_sha256(source_before)
    }


@pytest.mark.parametrize("override", [
    {"target_dedup_key": "no_trade:wrong"},
    {"target_kind": "trade"},
    {"target_source": "daily_slate"},
    {"target_ts": "2026-08-25T00:00:00Z"},
])
def test_training_exclusion_selector_mismatch_refuses_without_append(tmp_path, override):
    path = tmp_path / "trade_dataset.jsonl"
    row = _eligible_no_trade()
    path.write_text(json.dumps(row, sort_keys=True) + "\n")
    with pytest.raises(di.DatasetIntegrityError):
        _append_exact(path, row, **override)
    sidecar = tmp_path / "trade_dataset.training_exclusions.jsonl"
    assert not sidecar.read_text() if sidecar.exists() else True


def test_training_exclusion_source_drift_fails_closed(tmp_path):
    path = tmp_path / "trade_dataset.jsonl"
    row = _eligible_no_trade()
    path.write_text(json.dumps(row, sort_keys=True) + "\n")
    _append_exact(path, row)
    row["reason"] = "mutated"
    path.write_text(json.dumps(row, sort_keys=True) + "\n")
    with pytest.raises(di.DatasetIntegrityError, match="does not match exactly one source row"):
        di.load_training_exclusions(str(path), di.read_source_rows(str(path)))


@pytest.mark.parametrize("bad_line", [
    b"not-json\n",
    (json.dumps({"schema": di.TRAINING_EXCLUSION_SCHEMA, "action": "INCLUDE"}) + "\n").encode(),
])
def test_malformed_or_unknown_training_exclusion_ledger_fails_closed(tmp_path, bad_line):
    path = tmp_path / "trade_dataset.jsonl"
    row = _eligible_no_trade()
    path.write_text(json.dumps(row, sort_keys=True) + "\n")
    (tmp_path / "trade_dataset.training_exclusions.jsonl").write_bytes(bad_line)
    with pytest.raises(di.DatasetIntegrityError):
        di.load_training_exclusions(str(path), di.read_source_rows(str(path)))
