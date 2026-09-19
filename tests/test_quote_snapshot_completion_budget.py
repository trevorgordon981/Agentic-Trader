"""Public API contract; production-derived narrative omitted."""

import asyncio
import time
from datetime import datetime, timezone
from collections import defaultdict
from types import SimpleNamespace

import pytest

import exitmgr.connection as connection_mod
from exitmgr.connection import IBConnection


class Contract:
    def __init__(self, con_id):
        self.conId = con_id

    def __hash__(self):
        return self.conId


class Wrapper:
    def __init__(self):
        self._futures = {}
        self._results = {}
        self._reqId2Contract = {}
        self.reqId2Ticker = {}
        self.ticker2ReqId = defaultdict(dict)
        self.tickers = {}

    def startReq(self, key, contract=None):
        future = asyncio.get_running_loop().create_future()
        self._futures[key] = future
        self._results[key] = []
        self._reqId2Contract[key] = contract
        return future

    def _endReq(self, key):
        future = self._futures.pop(key, None)
        result = self._results.pop(key, [])
        self._reqId2Contract.pop(key, None)
        if future is not None and not future.done():
            future.set_result(result)

    def startTicker(self, req_id, contract, tick_type):

        ticker = type("Ticker", (), {"contract": contract})()
        self.reqId2Ticker[req_id] = ticker
        self.ticker2ReqId[tick_type][ticker] = req_id
        self.tickers[hash(contract)] = ticker
        return ticker

    def endTicker(self, ticker, tick_type):
        self.ticker2ReqId[tick_type].pop(ticker, None)


class Client:
    def __init__(self, wrapper, finish_after):
        self.wrapper = wrapper
        self.finish_after = finish_after
        self.next_id = 810
        self.requested = []
        self.cancelled = []
        self.handles = []

    def getReqId(self):
        self.next_id += 1
        return self.next_id

    def reqMktData(self, req_id, contract, generic, snapshot, regulatory, options):
        assert snapshot is True and regulatory is False
        self.requested.append(req_id)
        ticker = self.wrapper.reqId2Ticker[req_id]
        ticker.bid, ticker.ask = 2.0, 2.2
        if self.finish_after is not None:
            self.handles.append(asyncio.get_running_loop().call_later(
                self.finish_after, self.wrapper._endReq, req_id))

    def cancelMktData(self, req_id):
        self.cancelled.append(req_id)


def connection(monkeypatch, finish_after):
    monkeypatch.setattr(connection_mod.importlib_metadata, "version", lambda _name: "2.1.0")
    conn = IBConnection("127.0.0.1", 4002, 118)
    wrapper = Wrapper()
    client = Client(wrapper, finish_after)
    conn.ib = SimpleNamespace(wrapper=wrapper, client=client)
    conn._qualified_quote_contracts = {1: Contract(1), 2: Contract(2)}
    monkeypatch.setattr(conn, "_require_link", lambda: None)
    monkeypatch.setattr(conn, "_link_usable", lambda: True)
    return conn, wrapper, client


def assert_clean(wrapper):
    assert wrapper._futures == {}
    assert wrapper._results == {}
    assert wrapper._reqId2Contract == {}
    assert wrapper.reqId2Ticker == {}
    assert dict(wrapper.ticker2ReqId["snapshot"]) == {}
    assert wrapper.tickers == {}


@pytest.mark.asyncio
async def test_broker_normal_eleven_second_end_returns_fresh_quotes(monkeypatch):
    conn, wrapper, client = connection(monkeypatch, finish_after=11.0)
    try:
        rows = await conn.fetch_quotes([1, 2])
        assert set(rows) == {1, 2}
        assert rows[1]["price"] == pytest.approx(2.1)
        assert rows[1]["generation"] == conn._connection_generation

        assert time.monotonic() - rows[1]["observed_monotonic"] >= 10.5
        assert (datetime.now(timezone.utc) - datetime.fromisoformat(
            rows[1]["observed_utc"])).total_seconds() >= 10.5
        assert client.cancelled == []
        assert_clean(wrapper)
    finally:
        for handle in client.handles:
            handle.cancel()


@pytest.mark.asyncio
async def test_snapshot_wait_does_not_occupy_other_async_work(monkeypatch):
    conn, wrapper, client = connection(monkeypatch, finish_after=0.15)
    turns = 0

    async def protective_clock():
        nonlocal turns
        for _ in range(5):
            await asyncio.sleep(0.01)
            turns += 1

    rows, _ = await asyncio.gather(conn.fetch_quotes([1]), protective_clock())
    assert set(rows) == {1}
    assert turns == 5
    assert_clean(wrapper)


@pytest.mark.asyncio
async def test_stalled_snapshot_still_cancels_exact_requests(monkeypatch):
    conn, wrapper, client = connection(monkeypatch, finish_after=None)
    with pytest.raises(asyncio.TimeoutError):
        await conn._snapshot_tickers([Contract(1), Contract(2)], 0.03)
    assert client.cancelled == client.requested == [811, 812]
    assert_clean(wrapper)


@pytest.mark.asyncio
async def test_parent_cancel_still_cancels_exact_snapshot_requests(monkeypatch):
    conn, wrapper, client = connection(monkeypatch, finish_after=None)
    task = asyncio.create_task(conn.fetch_quotes([1, 2]))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.cancelled == client.requested == [811, 812]
    assert_clean(wrapper)


@pytest.mark.asyncio
async def test_qualification_keeps_separate_five_second_budget(monkeypatch):
    conn, wrapper, client = connection(monkeypatch, finish_after=0.001)
    conn._qualified_quote_contracts = {}
    seen = []
    actual_wait_for = asyncio.wait_for

    async def qualify(*contracts):
        return contracts

    async def observe_wait(awaitable, timeout):
        seen.append(timeout)
        return await actual_wait_for(awaitable, timeout)

    conn.ib.qualifyContractsAsync = qualify
    monkeypatch.setattr(connection_mod, "Contract", lambda: Contract(0))
    monkeypatch.setattr(connection_mod.asyncio, "wait_for", observe_wait)
    assert set(await conn.fetch_quotes([1])) == {1}
    assert seen[0] == 5
    assert seen[1] > 11
    assert seen[1] <= 20
    assert_clean(wrapper)


@pytest.mark.asyncio
async def test_connection_generation_change_drops_completed_quote(monkeypatch):
    conn, wrapper, client = connection(monkeypatch, finish_after=0.03)
    asyncio.get_running_loop().call_later(
        0.01, setattr, conn, "_connection_generation", conn._connection_generation + 1)
    assert await conn.fetch_quotes([1]) == {}
    assert_clean(wrapper)
