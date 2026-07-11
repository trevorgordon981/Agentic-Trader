import json

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
