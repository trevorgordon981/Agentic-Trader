"""Public API contract; production-derived narrative omitted."""

from types import SimpleNamespace

import pytest

import close_symbol
import liquidate


class _ReadOnlyIB:
    def __init__(self):
        self.positions_reads = 0

    async def reqPositionsAsync(self):
        self.positions_reads += 1
        return [SimpleNamespace(
            contract=SimpleNamespace(conId=101, symbol="XYZ", secType="OPT"),
            position=1,
        )]

    def cancelOrder(self, *_args, **_kwargs):
        raise AssertionError("manual frontend must not cancel broker orders")

    def placeOrder(self, *_args, **_kwargs):
        raise AssertionError("manual frontend must not place broker orders")


class _ReadOnlyConnection:
    instances = []

    def __init__(self, *_args, **_kwargs):
        self.ib = _ReadOnlyIB()
        self.disconnected = False
        self.__class__.instances.append(self)

    async def connect(self):
        return True

    async def disconnect(self):
        self.disconnected = True

    async def place_order(self, *_args, **_kwargs):
        raise AssertionError("manual frontend must not place broker orders")


class _Queue:
    added = []

    def __init__(self, path):
        self.path = path

    def add(self, requests):
        assert _ReadOnlyConnection.instances[-1].ib.positions_reads == 1
        self.__class__.added.extend(requests)


def _install(monkeypatch, module, tmp_path):
    _ReadOnlyConnection.instances.clear()
    _Queue.added.clear()
    cfg = SimpleNamespace(
        ib=SimpleNamespace(host="127.0.0.1", port=4001, market_data_type=1),
        journal=SimpleNamespace(path=str(tmp_path / "trades.log")),
    )
    request = {"parent_con_id": 101, "quantity": 1}
    monkeypatch.setattr(module, "load_config", lambda _path: cfg)
    monkeypatch.setattr(module, "freeze_runtime_identity", lambda *a, **k: object())
    monkeypatch.setattr(module, "IBConnection", _ReadOnlyConnection)
    monkeypatch.setattr(module, "bound_requests", lambda *a, **k: [request])
    monkeypatch.setattr(module, "ManualExitQueue", _Queue)
    monkeypatch.setattr(module, "_protective_running", lambda: True)
    monkeypatch.setattr(module, "slack", lambda *_args, **_kwargs: True)
    return request


@pytest.mark.asyncio
async def test_close_symbol_refreshes_then_queues_without_broker_mutation(monkeypatch, tmp_path):
    request = _install(monkeypatch, close_symbol, tmp_path)

    result = await close_symbol.run(
        True, 91, symbol="XYZ", config_path=str(tmp_path / "config.yaml"))

    assert result == 0
    assert _Queue.added == [request]
    assert _ReadOnlyConnection.instances[-1].disconnected


@pytest.mark.asyncio
async def test_liquidate_refreshes_then_queues_without_broker_mutation(monkeypatch, tmp_path):
    request = _install(monkeypatch, liquidate, tmp_path)

    result = await liquidate.run(True, 91, config_path=str(tmp_path / "config.yaml"))

    assert result == 0
    assert _Queue.added == [request]
    assert _ReadOnlyConnection.instances[-1].disconnected
