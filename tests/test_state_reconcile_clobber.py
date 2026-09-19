"""Public API contract; production-derived narrative omitted."""

import asyncio
import io
import json
import os
import tokenize
from pathlib import Path

import pytest

from exitmgr.config import Config, JournalConfig, StateConfig
from exitmgr.connection import OrderData, PositionData
from exitmgr.manager import ExitManager
from exitmgr.state import InFlightClose, StateManager

CON_ID = 4001001
SHORT_CON_ID = 4001002
ENTRY_DEBIT = 900.0
QTY = 3




def _entry_manager(tmp_path, journal_qty=QTY):
    """Public API contract; production-derived narrative omitted."""
    cfg = Config()
    cfg.state = StateConfig(path=os.path.join(str(tmp_path), "exitmgr_state.json"))
    cfg.journal = JournalConfig(path=os.path.join(str(tmp_path), "trades.log"))
    mgr = ExitManager(cfg)
    mgr._journal_entries = {CON_ID: {"debit": ENTRY_DEBIT, "quantity": journal_qty}}
    mgr._spread_short_legs = {SHORT_CON_ID: CON_ID}

    mgr._post_reconcile_block_alert = lambda alerts: None
    return mgr


def _wire_broker(mgr, open_orders=None):
    """Public API contract; production-derived narrative omitted."""
    position = PositionData(con_id=CON_ID, symbol="SYMC", right="C", quantity=QTY,
                            avg_cost=300.0, expiry="20300117", strike=40.0)

    async def _book():
        return {CON_ID: position}, {}, {}

    async def _orders(short_leg_con_ids=None):
        return dict(open_orders or {})

    mgr._fetch_position_book = _book

    class _Conn:
        get_open_orders = staticmethod(_orders)

    mgr.ib_conn = _Conn()
    return mgr


def _protective_writer(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    return StateManager(os.path.join(str(tmp_path), "exitmgr_state.json"))


def _on_disk(tmp_path):
    return json.loads(Path(str(tmp_path), "exitmgr_state.json").read_text())


def _configure_post_fix(mgr):
    """Public API contract; production-derived narrative omitted."""
    mgr.state_manager.persist = False


def _configure_pre_fix(mgr):
    """Public API contract; production-derived narrative omitted."""
    return None


PRE_FIX = pytest.param(
    _configure_pre_fix, id="pre_fix_entry_writes_shared_state",
    marks=pytest.mark.xfail(
        strict=True,
        reason="PRE-FIX: the entry process reconciled from -- and saved -- a snapshot as old as "
               "its first state access, so every reconnect reverted the protective loop's work."))
POST_FIX = pytest.param(_configure_post_fix, id="post_fix_entry_state_read_only")
BOTH = [PRE_FIX, POST_FIX]




@pytest.mark.parametrize("configure", BOTH)
def test_entry_reconnect_reconcile_does_not_revert_protective_state(tmp_path, configure):
    """Public API contract; production-derived narrative omitted."""
    protective = _protective_writer(tmp_path)
    protective.state.peak_prices[str(CON_ID)] = 3.00
    protective.save()

    mgr = _wire_broker(_entry_manager(tmp_path))
    configure(mgr)
    mgr.state_manager.state


    protective.state.peak_prices[str(CON_ID)] = 9.00
    protective.state.add_in_flight(InFlightClose(
        con_id=CON_ID, order_id=7, remaining_qty=QTY, entry_debit=ENTRY_DEBIT))
    protective.save()


    assert asyncio.run(mgr._reconcile_on_startup()) is True

    disk = _on_disk(tmp_path)
    assert disk["peak_prices"].get(str(CON_ID)) == 9.00, "trailing-stop peak was reverted"
    assert str(CON_ID) in disk["in_flight"], "double-close guard (in_flight) was reverted"





    assert mgr._reconcile_bad_con_ids == set()
    can_close, reason = asyncio.run(
        mgr.order_manager.can_place_close(CON_ID, QTY, live_open_orders={}))
    assert can_close is False
    assert "durable in-flight close" in reason


@pytest.mark.parametrize("configure", BOTH)
def test_repeated_reconnects_never_accumulate_a_write(tmp_path, configure):
    """Public API contract; production-derived narrative omitted."""
    protective = _protective_writer(tmp_path)
    protective.state.peak_prices[str(CON_ID)] = 4.25
    protective.save()

    mgr = _wire_broker(_entry_manager(tmp_path))
    configure(mgr)
    mgr.state_manager.state

    before = Path(str(tmp_path), "exitmgr_state.json").read_bytes()
    protective.state.peak_prices[str(CON_ID)] = 7.75
    protective.save()
    after_protective = Path(str(tmp_path), "exitmgr_state.json").read_bytes()

    for _ in range(25):
        assert asyncio.run(mgr._reconcile_on_startup()) is True

    assert Path(str(tmp_path), "exitmgr_state.json").read_bytes() == after_protective
    assert before != after_protective




@pytest.mark.parametrize("configure", BOTH)
def test_stale_snapshot_makes_reconcile_falsely_declare_the_account_unsafe(tmp_path, configure):
    """Public API contract; production-derived narrative omitted."""
    protective = _protective_writer(tmp_path)
    protective.save()

    mgr = _entry_manager(tmp_path)
    configure(mgr)
    mgr.state_manager.state


    protective.state.add_in_flight(InFlightClose(
        con_id=CON_ID, order_id=188, remaining_qty=QTY, entry_debit=ENTRY_DEBIT,
        perm_id=99001, client_id=189, order_ref="alfred-exit:x", identity_version=1))
    protective.save()

    _wire_broker(mgr, open_orders={CON_ID: OrderData(
        con_id=CON_ID, order_id=188, remaining=QTY, perm_id=99001, client_id=189,
        order_ref="alfred-exit:x", status="Submitted")})

    assert asyncio.run(mgr._reconcile_on_startup()) is True, (
        "the entry gate declared the account unsafe over a close the protective loop placed")




def test_reload_picks_up_another_processes_write(tmp_path):
    path = os.path.join(str(tmp_path), "s.json")
    owner, reader = StateManager(path), StateManager(path)
    owner.state.peak_prices["1"] = 1.0
    owner.save()
    assert reader.state.peak_prices == {"1": 1.0}

    owner.state.peak_prices["2"] = 2.0
    owner.save()
    assert reader.state.peak_prices == {"1": 1.0}, "cached snapshot must stay cached until reload"

    reload_returned = reader.reload()
    assert reader.state.peak_prices == {"1": 1.0, "2": 2.0}
    assert reload_returned is reader.state


def test_persist_false_swallows_every_save_and_counts_it(tmp_path):
    path = os.path.join(str(tmp_path), "s.json")
    ro = StateManager(path, persist=False)
    ro.state.peak_prices["1"] = 1.0
    ro.save()
    ro.update_last_cycle()
    assert not os.path.exists(path), "a read-only manager must never create the state file"
    assert ro.suppressed_saves == 2

    assert ro.state.peak_prices == {"1": 1.0}


def test_persist_true_is_the_default_so_the_owner_is_unchanged(tmp_path):
    path = os.path.join(str(tmp_path), "s.json")
    sm = StateManager(path)
    assert sm.persist is True
    sm.state.peak_prices["1"] = 1.0
    sm.save()
    assert json.loads(Path(path).read_text())["peak_prices"] == {"1": 1.0}


def test_the_owner_does_not_reload_during_reconcile(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr = _wire_broker(_entry_manager(tmp_path))
    assert mgr.state_manager.persist is True
    mgr.state_manager.state.peak_prices[str(CON_ID)] = 12.5

    assert asyncio.run(mgr._reconcile_on_startup()) is True

    assert mgr.state_manager.state.peak_prices[str(CON_ID)] == 12.5, (
        "the owning process's unflushed in-memory mutation was dropped by a reload")




def _code_only(path):
    """Public API contract; production-derived narrative omitted."""
    src = Path(path).read_text()
    code = "".join(
        tok.string for tok in tokenize.generate_tokens(io.StringIO(src).readline)
        if tok.type != tokenize.COMMENT)
    return "".join(code.split())


def _repo_file(name):
    return Path(__file__).resolve().parent.parent / name


def test_run_trader_constructs_entry_processes_with_read_only_state():
    code = _code_only(_repo_file("run_trader.py"))
    assert 'state_persist=(mode!="entry")' in code, (
        "run_trader.py no longer constructs the entry manager read-only -- the "
        "reconnect-reconcile clobber is back")


def test_reconcile_reloads_only_for_a_non_persisting_manager():
    code = _code_only(_repo_file("exitmgr/manager.py"))
    assert 'ifnotgetattr(self.state_manager,"persist",True):self.state_manager.reload()' in code, (
        "the reconcile path must refresh a non-owner snapshot, and must NOT refresh the owner's")


def test_owner_writes_are_serialized_but_entry_process_never_takes_the_lock():
    """Public API contract; production-derived narrative omitted."""
    code = _code_only(_repo_file("exitmgr/state.py"))
    assert "fcntl.flock(lock_fd,fcntl.LOCK_EX)" in code
    assert "ifnotself.persist:" in code and "self.suppressed_saves+=1" in code
