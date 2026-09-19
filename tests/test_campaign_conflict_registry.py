import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from exitmgr.campaign_conflicts import (
    CampaignConflictRegistryError, active_campaign_conflict_symbols, journal_sha256,
)
from exitmgr import entry_safety, trader as trader_mod
from exitmgr.trader import Trader


def test_registry_returns_only_active_bound_symbols(tmp_path, monkeypatch):
    path = tmp_path / "conflicts.json"
    journal = tmp_path / "trades.log"
    journal.write_text('{"event":"entry"}\n')
    path.write_text(json.dumps({
        "schema": "campaign_conflicts.v2",
        "journal_sha256": journal_sha256(journal),
        "conditions": {
            "active": {"active": True, "evidence": {"symbol": "SYMQ"}},
            "resolved": {"active": False, "evidence": {"symbol": "SYMS"}},
        },
    }))
    monkeypatch.setenv("EXITMGR_CAMPAIGN_CONFLICT_PATH", str(path))
    assert active_campaign_conflict_symbols(journal) == frozenset({"SYMQ"})


def test_registry_bound_to_exact_current_journal(tmp_path, monkeypatch):
    journal = tmp_path / "trades.log"
    journal.write_text('{"event":"entry"}\n')
    path = tmp_path / "conflicts.json"
    path.write_text(json.dumps({
        "schema": "campaign_conflicts.v2",
        "journal_sha256": journal_sha256(journal),
        "conditions": {},
    }))
    monkeypatch.setenv("EXITMGR_CAMPAIGN_CONFLICT_PATH", str(path))
    journal.write_text('{"event":"entry"}\n{"event":"close"}\n')
    with pytest.raises(CampaignConflictRegistryError, match="does not match"):
        active_campaign_conflict_symbols(journal)


def test_missing_or_malformed_registry_fails_closed(tmp_path, monkeypatch):
    path = tmp_path / "missing.json"
    (tmp_path / "trades.log").write_text("")
    monkeypatch.setenv("EXITMGR_CAMPAIGN_CONFLICT_PATH", str(path))
    with pytest.raises(CampaignConflictRegistryError):
        active_campaign_conflict_symbols(tmp_path / "trades.log")
    path.write_text('{"schema":"wrong","conditions":{}}')
    with pytest.raises(CampaignConflictRegistryError):
        active_campaign_conflict_symbols(tmp_path / "trades.log")


@pytest.mark.parametrize(
    "resident,durable,symbol,allowed",
    [((), (), "SPY", True), (("SYMQ",), (), "SPY", False),
     (("SYMQ",), ("SYMQ",), "SYMQ", False)],
)
def test_continuous_money_boundary_requires_matching_durable_quarantine(
        monkeypatch, resident, durable, symbol, allowed):
    instance = object.__new__(Trader)
    instance.exit_manager = MagicMock()
    instance.exit_manager.campaign_conflict_symbols.return_value = set(resident)
    instance.journal_path = "/unused/unit-test-journal"
    instance._short_option_entry_authority = lambda _r: entry_safety.SafetyResult(True)
    instance._entry_markers_clear = lambda: entry_safety.SafetyResult(True)
    monkeypatch.setattr(entry_safety, "nbbo_valid", lambda _r: entry_safety.SafetyResult(True))
    monkeypatch.setattr(
        trader_mod, "active_campaign_conflict_symbols", lambda _path: frozenset(durable))
    result, reasons = instance._admission_recheck(
        SimpleNamespace(underlying=symbol),
        entry_safety.EntrySubmissionAuthority.autonomous())()
    assert result is allowed
    assert bool(reasons) is (not allowed)
