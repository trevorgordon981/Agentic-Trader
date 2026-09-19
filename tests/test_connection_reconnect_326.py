"""Public API contract; production-derived narrative omitted."""
import asyncio
import os
import sys

import pytest

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP not in sys.path:
    sys.path.insert(0, APP)

import exitmgr.connection as connection
from exitmgr.connection import IBConnection

ERR_326 = "Unable to connect as the client id is already in use. Retry with a unique client id."


class _Event(list):
    """Public API contract; production-derived narrative omitted."""

    def __iadd__(self, fn):
        self.append(fn)
        return self

    def fire(self, *a):
        for fn in list(self):
            fn(*a)


class FakeIB:
    """Public API contract; production-derived narrative omitted."""

    occupied = frozenset()
    attempts = []
    subscribed_before_connect = []
    sync_error = None

    def __init__(self):
        self.errorEvent = _Event()
        self.disconnectedEvent = _Event()
        self.updatePortfolioEvent = _Event()
        self._up = False

    async def connectAsync(self, host=None, port=None, clientId=None, timeout=None,
                           *, raiseSyncErrors=False):
        assert raiseSyncErrors is True, "production must request strict account/position sync"
        FakeIB.attempts.append(clientId)
        FakeIB.subscribed_before_connect.append(
            all((self.errorEvent, self.disconnectedEvent, self.updatePortfolioEvent)))
        if clientId in FakeIB.occupied:
            self.errorEvent.fire(-1, 326, ERR_326, None)
            raise TimeoutError()
        self._up = True
        if FakeIB.sync_error is not None:
            raise FakeIB.sync_error

    def disconnect(self):
        was_up = self._up
        self._up = False
        if was_up:
            self.disconnectedEvent.fire()

    def isConnected(self):
        return self._up

    def reqMarketDataType(self, _t):
        pass


@pytest.fixture
def gateway(monkeypatch, tmp_path):
    """Public API contract; production-derived narrative omitted."""
    FakeIB.occupied = frozenset()
    FakeIB.attempts = []
    FakeIB.subscribed_before_connect = []
    FakeIB.sync_error = None
    monkeypatch.setattr(connection, "IB", FakeIB)
    monkeypatch.setenv("EXITMGR_ROTATION_CURSOR_PATH", str(tmp_path / "cursor.json"))
    slept = []

    async def _sleep(sec, *a, **k):
        slept.append(sec)

    monkeypatch.setattr(asyncio, "sleep", _sleep)
    return slept


def test_a_reconnect_rotates_off_an_in_use_id_instead_of_retrying_it(gateway):
    """Public API contract; production-derived narrative omitted."""
    FakeIB.occupied = frozenset({4010, 4011, 4012})
    c = IBConnection(host="h", port=4001, client_id=1)
    c._rotation_idx = 0
    assert asyncio.run(c.reconnect(retries=3, retry_delay=10)) is True
    assert FakeIB.attempts == [4010, 4011, 4012, 4013], FakeIB.attempts
    assert c.client_id == 4013


def test_the_old_behaviour_would_have_burned_every_attempt_on_one_id(gateway):
    """Public API contract; production-derived narrative omitted."""
    FakeIB.occupied = frozenset({4010, 4011, 4012})
    c = IBConnection(host="h", port=4001, client_id=1)
    c._rotation_idx = 0
    asyncio.run(c.reconnect(retries=3, retry_delay=10))
    assert FakeIB.attempts != [4010, 4010, 4010, 4010]


def test_no_retry_delay_is_paid_after_a_rotation(gateway):
    """Public API contract; production-derived narrative omitted."""
    FakeIB.occupied = frozenset({4010, 4011})
    c = IBConnection(host="h", port=4001, client_id=1)
    c._rotation_idx = 0
    assert asyncio.run(c.reconnect(retries=3, retry_delay=10)) is True
    assert gateway == [], "a rotated retry must not sleep out the old id's reservation window"


def test_a_gateway_that_is_simply_down_still_backs_off(gateway, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    async def refused(self, host=None, port=None, clientId=None, timeout=None,
                      *, raiseSyncErrors=False):
        assert raiseSyncErrors is True, "production must request strict account/position sync"
        FakeIB.attempts.append(clientId)
        raise ConnectionRefusedError(61, "Connect call failed")

    monkeypatch.setattr(FakeIB, "connectAsync", refused)
    c = IBConnection(host="h", port=4001, client_id=1)
    c._rotation_idx = 0
    assert asyncio.run(c.reconnect(retries=2, retry_delay=10)) is False
    assert FakeIB.attempts == [4010, 4010, 4010]
    assert gateway == [10, 10]


def test_a_plain_connect_is_not_silently_moved_off_its_allocated_id(gateway):
    """Public API contract; production-derived narrative omitted."""
    FakeIB.occupied = frozenset({93})
    c = IBConnection(host="h", port=4001, client_id=93)
    assert asyncio.run(c.connect(retries=2, retry_delay=5)) is False
    assert FakeIB.attempts == [93, 93, 93]
    assert c._client_id_in_use is True


def test_the_error_handler_is_attached_before_the_connect_not_after(gateway):
    """Public API contract; production-derived narrative omitted."""
    FakeIB.occupied = frozenset({4010})
    c = IBConnection(host="h", port=4001, client_id=1)
    c._rotation_idx = 0
    asyncio.run(c.reconnect(retries=1, retry_delay=1))
    assert FakeIB.subscribed_before_connect and all(FakeIB.subscribed_before_connect)


def test_the_handler_is_attached_exactly_once_per_ib_instance(gateway):
    """Public API contract; production-derived narrative omitted."""
    c = IBConnection(host="h", port=4001, client_id=1)
    assert asyncio.run(c.connect()) is True
    assert len(c.ib.errorEvent) == 1
    assert len(c.ib.disconnectedEvent) == 1
    assert len(c.ib.updatePortfolioEvent) == 1


def test_326_does_not_mark_the_uplink_down(gateway):
    """Public API contract; production-derived narrative omitted."""
    c = IBConnection(host="h", port=4001, client_id=1)
    c._uplink_ok = True
    c._on_error(-1, 326, ERR_326, None)
    assert c._client_id_in_use is True
    assert c._uplink_ok is True


def test_a_successful_connect_clears_the_in_use_flag(gateway):
    """Public API contract; production-derived narrative omitted."""
    FakeIB.occupied = frozenset({4010})
    c = IBConnection(host="h", port=4001, client_id=1)
    c._rotation_idx = 0
    assert asyncio.run(c.reconnect(retries=2, retry_delay=1)) is True
    assert c._client_id_in_use is False


def test_a_restarted_process_does_not_re_offer_its_predecessors_id(gateway):
    """Public API contract; production-derived narrative omitted."""
    dead = IBConnection(host="h", port=4001, client_id=1)
    dead._rotation_idx = 0
    assert asyncio.run(dead.reconnect(retries=0)) is True
    held = dead.client_id
    assert held == 4010

    FakeIB.occupied = frozenset({held})
    FakeIB.attempts = []
    replacement = IBConnection(host="h", port=4001, client_id=1)
    assert asyncio.run(replacement.reconnect(retries=0)) is True
    assert FakeIB.attempts == [4011], "the replacement led with the id its predecessor held"


def test_sync_failure_after_socket_connect_is_not_healthy_or_an_id_collision(gateway):
    """Public API contract; production-derived narrative omitted."""
    FakeIB.sync_error = ConnectionError('positions synchronization timed out')
    c = IBConnection(host='h', port=4001, client_id=1)
    c._rotation_idx = 0
    assert asyncio.run(c.reconnect(retries=1, retry_delay=3)) is False
    assert FakeIB.attempts == [4010, 4010]
    assert gateway == [3]
    assert not c.is_healthy()
    assert not c._client_id_in_use
    assert c.ib is None
