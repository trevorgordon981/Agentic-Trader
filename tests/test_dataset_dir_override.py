"""Public API contract; production-derived narrative omitted."""
import os
import json

from exitmgr import trade_capture as tc


def test_env_override_redirects_all_capture_writes(tmp_path, monkeypatch):
    prod = tmp_path / "prod_data"
    prod.mkdir()
    override = tmp_path / "override_dir"
    override.mkdir()
    journal = str(tmp_path / "trades.log")

    monkeypatch.setenv("EXITMGR_DATASET_DIR", str(override))

    d = tc.dataset_dir(journal, str(prod / "trade_dataset.jsonl"))
    assert d == str(override)

    tc.capture_no_trade(d, source="trader", reason="market_closed")
    tc.capture_decision(d, source="trader", symbol="SPY", strike=50.0, right="C")


    assert os.path.exists(os.path.join(str(override), "trade_dataset.jsonl"))
    assert os.path.exists(os.path.join(str(override), "decision_context.jsonl"))

    assert not os.path.exists(os.path.join(str(prod), "trade_dataset.jsonl"))
    assert not os.path.exists(os.path.join(str(prod), "decision_context.jsonl"))


def test_without_override_prod_behavior_unchanged(tmp_path, monkeypatch):

    monkeypatch.delenv("EXITMGR_DATASET_DIR", raising=False)
    journal = str(tmp_path / "trades.log")
    (tmp_path / "trades.log").write_text("")

    d = tc.dataset_dir(journal)
    assert d == str(tmp_path / "data")


    cfg_path = str(tmp_path / "custom" / "trade_dataset.jsonl")
    assert tc.dataset_dir(journal, cfg_path) == str(tmp_path / "custom")


def test_empty_override_is_ignored(tmp_path, monkeypatch):

    monkeypatch.setenv("EXITMGR_DATASET_DIR", "")
    journal = str(tmp_path / "trades.log")
    assert tc.dataset_dir(journal) == str(tmp_path / "data")
