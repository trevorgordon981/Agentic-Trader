"""Public API contract; production-derived narrative omitted."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from exitmgr.config import Config, JournalConfig, StateConfig
from exitmgr.connection import PositionData
from exitmgr.manager import ExitManager
from exitmgr.state import InFlightClose, StateManager

CID = 9002001
SHORT = 9002002


def _row(cid=CID):
    return dict(contract_id=cid, symbol="SYNTH", right="C", expiry="20261218",
                strike=5.0, quantity=1, debit=100.0, ts="2026-09-15T18:00:00+00:00",
                order_ref=f"synthetic-{cid}", spread={"short_con_id": SHORT,
                                                      "short_strike": 6.0, "width": 1.0})


def _setup(tmp_path, rows=()):
    journal = tmp_path / "trades.log"
    journal.write_text("".join(json.dumps(x)+"\n" for x in rows))
    state_path = tmp_path / "state.json"
    owner = StateManager(str(state_path))
    owner.save()
    cfg = Config()
    cfg.journal = JournalConfig(path=str(journal))
    cfg.state = StateConfig(path=str(state_path))
    mgr = ExitManager(cfg, state_persist=False, journal_side_effects=False)
    pos = PositionData(con_id=CID, symbol="SYNTH", right="C", quantity=1,
                       avg_cost=100.0, expiry="20261218", strike=5.0)
    mgr._fetch_position_book = AsyncMock(return_value=({CID:pos},{},{}))
    mgr.ib_conn = SimpleNamespace(get_open_orders=AsyncMock(return_value={}))
    mgr._post_reconcile_block_alert = Mock(side_effect=AssertionError("no Slack"))
    mgr._emit_tool_close = Mock(side_effect=AssertionError("no capture/finalization"))
    mgr.order_manager.place_close_order = AsyncMock(side_effect=AssertionError("no orders"))
    return mgr, owner, journal, state_path


def _refresh(mgr):
    return asyncio.run(mgr.refresh_entry_reconciliation())


def test_newly_journaled_position_recognized_without_restart_and_no_writes(tmp_path):
    mgr, owner, journal, state = _setup(tmp_path)
    assert CID not in mgr._journal_entries
    before = state.read_bytes()
    journal.write_text(json.dumps(_row())+"\n")
    assert _refresh(mgr) is True
    assert mgr._reconcile_ok is True
    assert CID in mgr._journal_entries
    assert not mgr._unjournaled_con_ids
    assert state.read_bytes() == before
    mgr._post_reconcile_block_alert.assert_not_called()
    mgr._emit_tool_close.assert_not_called()
    mgr.order_manager.place_close_order.assert_not_called()


def test_genuinely_unknown_position_blocks_entries_not_unrelated_exit_scope(tmp_path):
    mgr, _, _, state = _setup(tmp_path)
    before = state.read_bytes()
    assert _refresh(mgr) is False
    assert mgr._entry_reconcile_error == "unjournaled_positions"
    assert mgr._unjournaled_con_ids == {CID}
    assert mgr._reconcile_bad_con_ids == set()
    assert state.read_bytes() == before
    mgr._post_reconcile_block_alert.assert_not_called()


@pytest.mark.parametrize("failure", ["missing", "broken_json", "non_object", "unreadable"])
def test_unreadable_journal_cannot_reuse_previously_good_gate(tmp_path, monkeypatch, failure):
    mgr, _, journal, state = _setup(tmp_path, [_row()])
    assert _refresh(mgr)
    before = state.read_bytes()
    if failure == "missing": journal.unlink()
    elif failure == "broken_json": journal.write_text(json.dumps(_row())+'\n{"truncated":')
    elif failure == "non_object": journal.write_text('[]\n')
    else:
        original = Path.exists
        def broken(path):
            if path == journal: raise PermissionError("synthetic")
            return original(path)
        monkeypatch.setattr(Path, "exists", broken)
    mgr._fetch_position_book.reset_mock()
    assert _refresh(mgr) is False
    assert mgr._reconcile_ok is False
    assert mgr._entry_reconcile_error.startswith("unreadable:")
    mgr._fetch_position_book.assert_not_called()
    assert state.read_bytes() == before


@pytest.mark.parametrize("which", ["positions", "orders", "state"])
def test_unreadable_canonical_evidence_halts_without_writes(tmp_path, which):
    mgr, _, _, state = _setup(tmp_path, [_row()])
    assert _refresh(mgr)
    if which == "positions": mgr._fetch_position_book.side_effect = TimeoutError("synthetic")
    elif which == "orders": mgr.ib_conn.get_open_orders.side_effect = TimeoutError("synthetic")
    else: state.write_text('{"corrupt":')
    before = state.read_bytes()
    assert _refresh(mgr) is False
    assert not mgr._reconcile_ok
    assert state.read_bytes() == before
    mgr._post_reconcile_block_alert.assert_not_called()
    mgr.order_manager.place_close_order.assert_not_called()


def test_canonical_state_reload_preserves_new_SYMD_style_ambiguous_latch(tmp_path):
    mgr, owner, _, state = _setup(tmp_path, [_row()])
    owner.state.add_in_flight(InFlightClose(con_id=CID,order_id=23255,remaining_qty=1,
        entry_debit=100, client_id=5892, order_ref="synthetic-ambiguous",identity_version=1,
        placement_state="transmission_ambiguous",exit_context={"close_qty":1}))
    owner.save()
    before=state.read_bytes()
    assert _refresh(mgr) is True
    assert mgr.state_manager.state.get_in_flight(CID).placement_state == "transmission_ambiguous"
    assert state.read_bytes() == before


def test_journal_appended_during_broker_await_is_seen(tmp_path):
    mgr, _, journal, _ = _setup(tmp_path)
    original = mgr._fetch_position_book.return_value
    async def book():
        journal.write_text(json.dumps(_row())+"\n")
        return original
    mgr._fetch_position_book.side_effect = book
    assert _refresh(mgr) is True
    assert CID in mgr._journal_entries


def test_entry_refresh_cannot_borrow_protective_writer(tmp_path):
    mgr, _, _, state = _setup(tmp_path, [_row()])
    mgr.state_manager.persist=True
    before=state.read_bytes()
    assert _refresh(mgr) is False
    mgr._fetch_position_book.assert_not_called()
    assert state.read_bytes() == before
