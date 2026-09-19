"""Public API contract; production-derived narrative omitted."""
import asyncio
import pytest

from tests.test_quote_snapshot_completion_budget import connection, Contract, assert_clean


@pytest.mark.asyncio
async def test_completed_snapshot_survives_other_contract_timeout(monkeypatch):
    conn, wrapper, client = connection(monkeypatch, finish_after=None)
    original = client.reqMktData

    def request(req_id, contract, *args):
        original(req_id, contract, *args)
        if contract.conId == 1:
            asyncio.get_running_loop().call_soon(wrapper._endReq, req_id)
    client.reqMktData = request

    rows = await conn._snapshot_tickers([Contract(1), Contract(2)], 0.02)
    assert [ticker.contract.conId for ticker in rows] == [1]
    assert client.cancelled == [812]
    assert_clean(wrapper)


@pytest.mark.asyncio
async def test_broker_request_error_does_not_abort_sibling_snapshot(monkeypatch):
    conn, wrapper, client = connection(monkeypatch, finish_after=None)
    original = client.reqMktData

    def finish(req_id, failed):
        if failed:
            wrapper._results.pop(req_id, None)
            wrapper._reqId2Contract.pop(req_id, None)
            wrapper._futures.pop(req_id).set_exception(RuntimeError('contract rejected'))
        else:
            wrapper._endReq(req_id)

    def request(req_id, contract, *args):
        original(req_id, contract, *args)
        asyncio.get_running_loop().call_later(
            0.001 if contract.conId == 1 else 0.02, finish, req_id, contract.conId == 1)
    client.reqMktData = request
    rows = await conn.fetch_quotes([1, 2])
    assert set(rows) == {2}
    assert rows[2]['price'] == pytest.approx(2.1)
    assert client.cancelled == []
    assert_clean(wrapper)


@pytest.mark.asyncio
async def test_all_broker_errors_remain_unpriceable(monkeypatch):
    conn, wrapper, client = connection(monkeypatch, finish_after=None)
    original = client.reqMktData

    def request(req_id, contract, *args):
        original(req_id, contract, *args)
        wrapper._results.pop(req_id, None)
        wrapper._reqId2Contract.pop(req_id, None)
        wrapper._futures.pop(req_id).set_exception(RuntimeError('contract rejected'))
    client.reqMktData = request
    assert await conn.fetch_quotes([1, 2]) == {}
    assert_clean(wrapper)
