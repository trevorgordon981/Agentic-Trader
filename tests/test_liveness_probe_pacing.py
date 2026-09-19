"""Public API contract; production-derived narrative omitted."""
import ast
import asyncio
from pathlib import Path
import socket
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "connection.py"
if not SOURCE.exists():
    SOURCE = ROOT / "exitmgr" / "connection.py"
TREE = ast.parse(SOURCE.read_text())
METHOD = next(n for n in ast.walk(TREE) if isinstance(n, ast.AsyncFunctionDef) and n.name == "ensure_connected")
SCOPE = {"asyncio": asyncio}
exec(compile(ast.fix_missing_locations(ast.Module(body=[METHOD], type_ignores=[])), str(SOURCE), "exec"), SCOPE)
PROBE = SCOPE["ensure_connected"]


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", Mock(side_effect=AssertionError("network forbidden")))


class DuplicateSuppressingBroker:
    """Public API contract; production-derived narrative omitted."""
    def __init__(self):
        self.calls = []
        self.responses = 0
        self.active = 0
        self.max_active = 0

    async def reqCurrentTimeAsync(self):
        now = asyncio.get_running_loop().time()
        duplicate = bool(self.calls and now - self.calls[-1] < 1.0)
        self.calls.append(now)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if duplicate:
                await asyncio.Event().wait()
            self.responses += 1
            return self.responses
        finally:
            self.active -= 1


def connection(ib=None):
    c = SimpleNamespace(ib=ib or DuplicateSuppressingBroker(), _connection_generation=1,
                        _link_fault=False, _uplink_ok=True, invalidations=0)
    c.is_healthy = lambda: c._uplink_ok and not c._link_fault
    def invalidate():
        c.invalidations += 1
        c._connection_generation += 1
    c._invalidate_price_observations = invalidate
    return c


def seed_recent(c):
    c._last_health_probe_reply = (c.ib, c._connection_generation, asyncio.get_running_loop().time())


@pytest.mark.asyncio
async def test_duplicate_suppression_fixture_reproduces_unpaced_loss():
    ib = DuplicateSuppressingBroker()
    assert await ib.reqCurrentTimeAsync() == 1
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(ib.reqCurrentTimeAsync(), 0.02)
    assert len(ib.calls) == 2 and ib.responses == 1


@pytest.mark.asyncio
async def test_second_probe_makes_fresh_roundtrip_instead_of_accepting_cached_success():
    c = connection()
    assert await PROBE(c, timeout=2)
    started = time.monotonic()
    assert await PROBE(c, timeout=2)
    assert 1.19 <= time.monotonic() - started < 2
    assert c.ib.responses == 2 and len(c.ib.calls) == 2
    assert c.ib.calls[1] - c.ib.calls[0] >= 1.19


@pytest.mark.asyncio
async def test_concurrent_probes_serialize_without_future_overwrite():
    c = connection()
    assert await asyncio.gather(PROBE(c, timeout=2), PROBE(c, timeout=2)) == [True, True]
    assert c.ib.responses == 2
    assert c.ib.max_active == 1
    assert c.ib.calls[1] - c.ib.calls[0] >= 1.19


@pytest.mark.asyncio
async def test_two_second_budget_includes_lock_spacing_and_response():
    c = connection()
    c._health_probe_lock = asyncio.Lock()
    await c._health_probe_lock.acquire()
    async def release_lock():
        await asyncio.sleep(0.2)
        seed_recent(c)
        c._health_probe_lock.release()
    async def slow_response():
        await asyncio.sleep(0.8)
        return 123
    c.ib.reqCurrentTimeAsync = AsyncMock(side_effect=slow_response)
    releaser = asyncio.create_task(release_lock())
    started = time.monotonic()
    assert await PROBE(c, timeout=2) is False
    elapsed = time.monotonic() - started
    await releaser
    assert 1.9 <= elapsed < 2.3, "must not reset deadline after lock or spacing"
    c.ib.reqCurrentTimeAsync.assert_awaited_once()
    assert c.invalidations == 1


@pytest.mark.asyncio
async def test_lock_deadline_sends_nothing():
    c = connection()
    c._health_probe_lock = asyncio.Lock()
    await c._health_probe_lock.acquire()
    assert await PROBE(c, timeout=0.02) is False
    assert c.ib.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("waiting", ["lock", "spacing"])
async def test_cancellation_before_send_does_not_fault_connection(waiting):
    c = connection()
    if waiting == "lock":
        c._health_probe_lock = asyncio.Lock()
        await c._health_probe_lock.acquire()
    else:
        seed_recent(c)
    task = asyncio.create_task(PROBE(c, timeout=2))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert c.ib.calls == [] and c.invalidations == 0
    assert c.is_healthy()


@pytest.mark.asyncio
async def test_cancellation_after_send_invalidates_uncorrelated_late_reply_lane():
    c = connection()
    entered = asyncio.Event()
    async def pending():
        entered.set()
        await asyncio.Event().wait()
    c.ib.reqCurrentTimeAsync = AsyncMock(side_effect=pending)
    task = asyncio.create_task(PROBE(c, timeout=2))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert c.invalidations == 1 and not c.is_healthy()


@pytest.mark.asyncio
@pytest.mark.parametrize("swap", ["generation", "ib"])
@pytest.mark.parametrize("outcome", ["reply", "timeout", "cancel"])
async def test_old_probe_cannot_validate_or_invalidate_replacement(swap, outcome):
    c = connection()
    entered, release = asyncio.Event(), asyncio.Event()
    async def pending():
        entered.set()
        await release.wait()
        return 123
    c.ib.reqCurrentTimeAsync = AsyncMock(side_effect=pending)
    task = asyncio.create_task(PROBE(c, timeout=0.05))
    await entered.wait()
    if swap == "generation":
        c._connection_generation += 1
    else:
        c.ib = DuplicateSuppressingBroker()
    if outcome == "reply":
        release.set()
        assert await task is False
    elif outcome == "timeout":
        assert await task is False
    else:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert c.invalidations == 0 and c.is_healthy()
    assert not hasattr(c, "_last_health_probe_reply")


@pytest.mark.asyncio
async def test_swap_while_waiting_never_sends_on_old_or_new_ib():
    c = connection()
    old = c.ib
    c._health_probe_lock = asyncio.Lock()
    await c._health_probe_lock.acquire()
    task = asyncio.create_task(PROBE(c, timeout=2))
    await asyncio.sleep(0)
    c.ib = DuplicateSuppressingBroker()
    c._health_probe_lock.release()
    assert await task is False
    assert old.calls == c.ib.calls == []
    assert c.invalidations == 0


@pytest.mark.asyncio
async def test_new_generation_does_not_reuse_old_pacing_or_reply():
    c = connection()
    seed_recent(c)
    c._connection_generation += 1
    started = time.monotonic()
    assert await PROBE(c, timeout=0.1)
    assert time.monotonic() - started < 0.1
    assert c.ib.responses == 1


@pytest.mark.asyncio
async def test_probe_false_preserves_cheap_health_check():
    c = connection()
    assert await PROBE(c, probe=False)
    assert c.ib.calls == []
