"""Layer 1b: close-tool 'closed_by_tool' journal markers must drop the position (and any
spread it anchored) so the trader's exit manager never re-closes an already-closed position
(the 2026-06-29 double-close that left a +2 long residual)."""
import json
from exitmgr.manager import ExitManager


def _mgr(tmp_path, sample_config, lines):
    jpath = tmp_path / "trades.log"
    jpath.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
    sample_config.journal.path = str(jpath)
    return ExitManager(sample_config)


def test_closed_by_tool_on_short_leg_drops_whole_spread(tmp_path, sample_config):
    mgr = _mgr(tmp_path, sample_config, [
        {"contract_id": 111, "symbol": "NOK", "right": "C", "strike": 14.0, "quantity": 2,
         "debit": 150.0, "spread": {"short_con_id": 222, "short_strike": 25.0}},
        {"contract_id": 222, "symbol": "NOK", "event": "closed_by_tool", "status": "Filled"},
    ])
    assert 111 not in mgr._journal_entries          # parent spread dropped
    assert 222 not in mgr._journal_entries
    assert 222 not in mgr._spread_short_legs


def test_closed_by_tool_on_long_leg_drops_entry_and_short_tracking(tmp_path, sample_config):
    mgr = _mgr(tmp_path, sample_config, [
        {"contract_id": 111, "symbol": "NOK", "right": "C", "strike": 14.0, "quantity": 2,
         "debit": 150.0, "spread": {"short_con_id": 222, "short_strike": 25.0}},
        {"contract_id": 111, "symbol": "NOK", "event": "closed_by_tool", "status": "Filled"},
    ])
    assert 111 not in mgr._journal_entries
    assert 222 not in mgr._spread_short_legs        # short-leg tracking for the parent cleared


def test_open_entry_without_marker_is_kept(tmp_path, sample_config):
    mgr = _mgr(tmp_path, sample_config, [
        {"contract_id": 333, "symbol": "IWM", "right": "C", "strike": 290.0, "quantity": 1,
         "debit": 400.0},
    ])
    assert 333 in mgr._journal_entries              # normal open entry unaffected
