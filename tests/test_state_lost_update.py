"""Public API contract; production-derived narrative omitted."""

import json
import os
import subprocess
import sys
import textwrap
import time

import pytest

from exitmgr.entry_throttle import (
    EntryThrottleStore, EntryThrottleUnreadable, entry_day_open_counts, record_entry_open,
)
from exitmgr.state import InFlightClose, StateCorruptionError, StateManager

DAY = "2026-08-12"




def _record_entry_open_PRE_FIX(sm, _store_ignored, date_str, order_count, notional):
    """Public API contract; production-derived narrative omitted."""
    sm.state.update_daily_open_stats(date_str, order_count, notional)
    sm.save()
    return True


def _day_open_counts_PRE_FIX(sm, _store, date_str):
    """Public API contract; production-derived narrative omitted."""
    ds = sm.state.daily_stats.get(date_str)
    if ds is None:
        return 0, 0.0
    return int(ds.orders_opened), float(ds.notional_opened)


PRE_FIX = pytest.param(
    _record_entry_open_PRE_FIX, _day_open_counts_PRE_FIX,
    id="pre_fix_shared_state_save",
    marks=pytest.mark.xfail(
        strict=True,
        reason="PRE-FIX: the entry process persisted counters into the shared state file, so its "
               "save clobbered the protective loop and the protective loop's 30s save clobbered "
               "it right back."),
)
POST_FIX = pytest.param(
    record_entry_open, entry_day_open_counts, id="post_fix_entry_throttle_file")

BOTH = [PRE_FIX, POST_FIX]


@pytest.fixture()
def state_path(tmp_path):
    return tmp_path / "exitmgr_state.json"


def _store(state_path):
    return EntryThrottleStore.for_state_path(state_path)




def test_a_caching_writer_cannot_revert_a_newer_generation(state_path):
    """Public API contract; production-derived narrative omitted."""
    protective = StateManager(state_path)
    protective.state.peak_prices["111"] = 1.00
    protective.save()

    entry = StateManager(state_path)
    assert entry.state.peak_prices == {"111": 1.00}

    protective.state.peak_prices["222"] = 2.00
    protective.save()

    with pytest.raises(StateCorruptionError, match="stale state write"):
        entry.save()

    on_disk = json.loads(state_path.read_text())
    assert "222" in on_disk["peak_prices"]


    protective.state.peak_prices["333"] = 3.00
    protective.save()
    assert "333" not in entry.state.peak_prices
    entry.reload()
    assert "333" in entry.state.peak_prices


    read_only = StateManager(state_path, persist=False)
    read_only.state.peak_prices.clear()
    read_only.save()
    assert "333" in json.loads(state_path.read_text())["peak_prices"]




@pytest.mark.parametrize("record,read", BOTH)
def test_entry_accrual_does_not_revert_protective_state(state_path, record, read):
    """Public API contract; production-derived narrative omitted."""
    protective = StateManager(state_path)
    protective.state.peak_prices["111"] = 1.00
    protective.save()

    entry = StateManager(state_path)
    entry.state


    protective.state.peak_prices["222"] = 2.00
    protective.state.add_in_flight(
        InFlightClose(con_id=222, order_id=7, remaining_qty=1, entry_debit=250.0))
    protective.save()


    record(entry, _store(state_path), DAY, 1, 1234.0)

    on_disk = json.loads(state_path.read_text())
    assert on_disk["peak_prices"].get("222") == 2.00, "trailing-stop peak was reverted"
    assert "222" in on_disk["in_flight"], "double-close guard was reverted"




@pytest.mark.parametrize("record,read", BOTH)
def test_day_open_counters_survive_the_protective_loop_and_a_restart(state_path, record, read):
    """Public API contract; production-derived narrative omitted."""
    protective = StateManager(state_path)
    protective.state.peak_prices["111"] = 1.00
    protective.save()

    entry = StateManager(state_path)
    record(entry, _store(state_path), DAY, 1, 1234.0)
    record(entry, _store(state_path), DAY, 1, 1000.0)



    protective.state.peak_prices["111"] = 1.50
    protective.save()


    restarted = StateManager(state_path)
    orders, notional = read(restarted, _store(state_path), DAY)
    assert (orders, notional) == (2, 2234.0), "day-open throttle counters did not survive"


def test_counts_are_visible_within_the_running_cycle(state_path):
    """Public API contract; production-derived narrative omitted."""
    entry = StateManager(state_path)
    store = _store(state_path)
    assert entry_day_open_counts(entry, store, DAY) == (0, 0.0)
    assert record_entry_open(entry, store, DAY, 1, 500.0)
    assert entry_day_open_counts(entry, store, DAY) == (1, 500.0)


def test_counts_take_the_max_of_file_and_memory(state_path):
    """Public API contract; production-derived narrative omitted."""
    entry = StateManager(state_path)
    store = _store(state_path)
    entry.state.update_daily_open_stats(DAY, 3, 9000.0)
    assert entry_day_open_counts(entry, store, DAY) == (3, 9000.0)
    assert store.read_day(DAY) == (0, 0.0)


def test_the_entry_path_never_saves_the_shared_state_file():
    """Public API contract; production-derived narrative omitted."""
    import io
    import tokenize
    from pathlib import Path
    from exitmgr import trader as trader_mod

    src = Path(trader_mod.__file__).read_text()

    code = "".join(
        tok.string for tok in tokenize.generate_tokens(io.StringIO(src).readline)
        if tok.type not in (tokenize.COMMENT, tokenize.STRING))
    assert "_sm.save()" not in code, (
        "exitmgr/trader.py saves the SHARED state from the entry process again -- that reverts "
        "the protective loop's peak_prices / in_flight. Use exitmgr.entry_throttle instead.")
    assert "record_entry_open" in code




def test_store_is_isolated_from_the_shared_state_file(state_path):
    store = _store(state_path)
    assert store.add(DAY, 1, 100.0)
    assert store.path != state_path
    assert store.path.parent == state_path.parent
    assert not state_path.exists(), "the throttle store must never touch the shared state file"
    assert oct(os.stat(store.path).st_mode)[-3:] == "600"


def test_a_corrupt_file_refuses_rather_than_reading_as_an_unused_day(state_path):
    """Public API contract; production-derived narrative omitted."""
    store = _store(state_path)
    store.path.write_text("{not json")
    with pytest.raises(EntryThrottleUnreadable):
        store.read_day(DAY)
    assert store.add(DAY, 1, 10.0) is False, "must not overwrite a file it cannot read"
    assert store.path.read_text() == "{not json", "the unreadable file is left for an operator"
    with pytest.raises(EntryThrottleUnreadable):
        entry_day_open_counts(None, store, DAY)


def test_an_absent_file_is_still_a_legitimate_zero(state_path):
    """Public API contract; production-derived narrative omitted."""
    store = _store(state_path)
    assert not store.path.exists()
    assert store.read_day(DAY) == (0, 0.0)
    assert entry_day_open_counts(None, store, DAY) == (0, 0.0)


def test_a_present_but_unusable_day_record_also_refuses(state_path):
    """Public API contract; production-derived narrative omitted."""
    store = _store(state_path)
    store.path.write_text(json.dumps({DAY: {"orders_opened": "lots", "notional_opened": 1.0}}))
    with pytest.raises(EntryThrottleUnreadable):
        store.read_day(DAY)
    store.path.write_text(json.dumps({DAY: ["not", "an", "object"]}))
    with pytest.raises(EntryThrottleUnreadable):
        store.read_day(DAY)


def test_no_source_at_all_refuses_instead_of_answering_zero():
    """Public API contract; production-derived narrative omitted."""
    with pytest.raises(EntryThrottleUnreadable):
        entry_day_open_counts(None, None, DAY)


def test_store_prunes_old_days(state_path):
    from exitmgr import entry_throttle
    store = _store(state_path)
    for i in range(entry_throttle.RETENTION_DAYS + 5):
        assert store.add(f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}", 1, 1.0)
    assert len(json.loads(store.path.read_text())) == entry_throttle.RETENTION_DAYS


def test_a_busy_lock_fails_fast_and_safe_instead_of_hanging(state_path):
    """Public API contract; production-derived narrative omitted."""
    store = EntryThrottleStore.for_state_path(state_path, lock_timeout_seconds=0.3)
    holder_src = textwrap.dedent(f"""
        import fcntl, os, sys, time
        fd = os.open({str(store.lock_path)!r}, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        sys.stdout.write("locked\\n"); sys.stdout.flush()
        time.sleep(30)
    """)
    holder = subprocess.Popen([sys.executable, "-c", holder_src],
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        started = time.monotonic()
        assert store.add(DAY, 1, 100.0) is False
        assert time.monotonic() - started < 5.0
    finally:
        holder.kill()
        holder.wait(timeout=10)


def test_separate_store_instances_accumulate_exactly(state_path):
    """Public API contract; production-derived narrative omitted."""
    store = _store(state_path)
    a, b = _store(state_path), _store(state_path)
    for _ in range(25):
        assert a.add(DAY, 1, 2.0)
        assert b.add(DAY, 1, 3.0)
    assert store.read_day(DAY) == (50, 125.0)
