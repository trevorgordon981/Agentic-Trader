"""Guard test (2026-07-03): prove the EXITMGR_DATASET_DIR override isolates test writes from
the production data/*.jsonl training corpus, and that WITHOUT the override production behavior
(data/ next to the journal) is unchanged. Regression guard for the pytest-pollution incident
where 141 synthetic rows leaked into data/trade_dataset.jsonl + data/decision_context.jsonl."""
import os
import json

from exitmgr import trade_capture as tc


def test_env_override_redirects_all_capture_writes(tmp_path, monkeypatch):
    prod = tmp_path / "prod_data"          # stand-in for the real ./data
    prod.mkdir()
    override = tmp_path / "override_dir"
    override.mkdir()
    journal = str(tmp_path / "trades.log")

    monkeypatch.setenv("EXITMGR_DATASET_DIR", str(override))
    # even with an explicit journal AND cfg path, the env override wins
    d = tc.dataset_dir(journal, str(prod / "trade_dataset.jsonl"))
    assert d == str(override)

    tc.capture_no_trade(d, source="trader", reason="market_closed")
    tc.capture_decision(d, source="trader", symbol="SPY", strike=50.0, right="C")

    # writes landed in the override dir...
    assert os.path.exists(os.path.join(str(override), "trade_dataset.jsonl"))
    assert os.path.exists(os.path.join(str(override), "decision_context.jsonl"))
    # ...and NOT in the prod dir
    assert not os.path.exists(os.path.join(str(prod), "trade_dataset.jsonl"))
    assert not os.path.exists(os.path.join(str(prod), "decision_context.jsonl"))


def test_without_override_prod_behavior_unchanged(tmp_path, monkeypatch):
    # remove the autouse-fixture override so we exercise real resolution
    monkeypatch.delenv("EXITMGR_DATASET_DIR", raising=False)
    journal = str(tmp_path / "trades.log")
    (tmp_path / "trades.log").write_text("")

    d = tc.dataset_dir(journal)
    assert d == str(tmp_path / "data")      # data/ next to the journal, as production expects

    # explicit cfg dataset path -> its parent, unchanged
    cfg_path = str(tmp_path / "custom" / "trade_dataset.jsonl")
    assert tc.dataset_dir(journal, cfg_path) == str(tmp_path / "custom")


def test_empty_override_is_ignored(tmp_path, monkeypatch):
    # an empty env var must NOT hijack resolution (falsy -> fall through to journal)
    monkeypatch.setenv("EXITMGR_DATASET_DIR", "")
    journal = str(tmp_path / "trades.log")
    assert tc.dataset_dir(journal) == str(tmp_path / "data")
