"""Public API contract; production-derived narrative omitted."""
import json
import pytest
from exitmgr.manager import ExitManager


def _mgr(tmp_path, sample_config, lines):
    jpath = tmp_path / "trades.log"
    jpath.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
    sample_config.journal.path = str(jpath)
    return ExitManager(sample_config)


def test_closed_by_tool_on_short_leg_drops_whole_spread(tmp_path, sample_config):
    mgr = _mgr(tmp_path, sample_config, [
        {"contract_id": 111, "symbol": "SYMF", "right": "C", "strike": 14.0, "quantity": 2,
         "debit": 150.0, "spread": {"short_con_id": 222, "short_strike": 25.0}},
        {"contract_id": 222, "symbol": "SYMF", "event": "closed_by_tool", "status": "Filled",
         "broker_flat_confirmed": True, "topology_complete": True},
    ])
    assert 111 not in mgr._journal_entries
    assert 222 not in mgr._journal_entries
    assert 222 not in mgr._spread_short_legs


def test_closed_by_tool_on_long_leg_drops_entry_and_short_tracking(tmp_path, sample_config):
    mgr = _mgr(tmp_path, sample_config, [
        {"contract_id": 111, "symbol": "SYMF", "right": "C", "strike": 14.0, "quantity": 2,
         "debit": 150.0, "spread": {"short_con_id": 222, "short_strike": 25.0}},
        {"contract_id": 111, "symbol": "SYMF", "event": "closed_by_tool", "status": "Filled",
         "broker_flat_confirmed": True, "topology_complete": True},
    ])
    assert 111 not in mgr._journal_entries
    assert 222 not in mgr._spread_short_legs


def test_open_entry_without_marker_is_kept(tmp_path, sample_config):
    mgr = _mgr(tmp_path, sample_config, [
        {"contract_id": 333, "symbol": "IWM", "right": "C", "strike": 290.0, "quantity": 1,
         "debit": 400.0},
    ])
    assert 333 in mgr._journal_entries


@pytest.mark.parametrize("status", ["Submitted", "PreSubmitted", "Inactive", "Cancelled", None])
def test_nonfilled_tool_marker_never_drops_live_spread(tmp_path, sample_config, status):
    mgr = _mgr(tmp_path, sample_config, [
        {"contract_id": 111, "symbol": "SYMF", "right": "C", "strike": 14.0, "quantity": 2,
         "debit": 150.0, "spread": {"short_con_id": 222, "short_strike": 25.0}},
        {"contract_id": 111, "symbol": "SYMF", "event": "closed_by_tool", "status": status},
    ])
    assert 111 in mgr._journal_entries
    assert mgr._spread_short_legs[222] == 111


def test_one_filled_leg_without_complete_topology_never_drops_spread(tmp_path, sample_config):
    mgr = _mgr(tmp_path, sample_config, [
        {"contract_id": 111, "symbol": "SYMF", "right": "C", "strike": 14.0, "quantity": 2,
         "debit": 150.0, "spread": {"short_con_id": 222, "short_strike": 25.0}},
        {"contract_id": 111, "symbol": "SYMF", "event": "closed_by_tool", "status": "Filled"},
    ])
    assert 111 in mgr._journal_entries
    assert mgr._spread_short_legs[222] == 111
