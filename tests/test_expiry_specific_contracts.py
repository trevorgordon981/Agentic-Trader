from types import SimpleNamespace

import pytest

from exitmgr import ibkr


def _contract(con_id, strike, expiry="20270115", right="P"):
    return SimpleNamespace(
        conId=con_id, strike=strike, lastTradeDateOrContractMonth=expiry,
        right=right, secType="OPT", exchange="SMART", symbol="SYMQ",
        tradingClass="SYMQ", multiplier="100", currency="USD")


@pytest.mark.asyncio
async def test_expiry_contract_query_filters_exact_series_before_near_spot_window(monkeypatch):
    queries = []

    class FakeIB:
        async def reqContractDetailsAsync(self, query):
            queries.append(query)
            rows = [
                _contract(1, 65), _contract(2, 100), _contract(3, 120),
                _contract(4, 125), _contract(5, 200),
                _contract(6, 120, expiry="20261218"),
                _contract(7, 120, right="C"),
                _contract(0, 115),
                SimpleNamespace(**{**vars(_contract(8, 120)), "multiplier": "10"}),
                SimpleNamespace(**{**vars(_contract(9, 120)), "tradingClass": "SYMQ1"}),
                SimpleNamespace(**{**vars(_contract(10, 120)), "secType": "FOP"}),
                SimpleNamespace(**{**vars(_contract(11, 120)), "currency": "CAD"}),
            ]
            return [SimpleNamespace(contract=row) for row in rows]

    monkeypatch.setattr(
        ibkr, "Option",
        lambda symbol, expiry, strike, right, exchange, **kwargs: SimpleNamespace(
            symbol=symbol, expiry=expiry, strike=strike, right=right, exchange=exchange,
            **kwargs))

    result = await ibkr.option_contracts_for_expiry(
        FakeIB(), "SYMQ", "20270115", "P", ref_price=120, timeout_s=1)

    assert len(queries) == 1
    assert queries[0].strike == 0.0
    assert queries[0].tradingClass == "SYMQ"
    assert queries[0].multiplier == "100"
    assert [(row.conId, row.strike) for row in result] == [(2, 100), (3, 120), (4, 125)]


@pytest.mark.asyncio
async def test_nonstandard_chain_multiplier_fails_closed_before_broker_query(monkeypatch):
    class FakeIB:
        def reqContractDetailsAsync(self, _query):
            raise AssertionError("nonstandard multiplier must not reach the broker")

    monkeypatch.setattr(ibkr, "Option", lambda *args, **kwargs: SimpleNamespace())
    assert await ibkr.option_contracts_for_expiry(
        FakeIB(), "SYMQ", "20270115", "P", multiplier="10") == []


@pytest.mark.asyncio
async def test_real_contract_details_failure_fails_closed_without_union_fallback(monkeypatch):
    class FakeIB:
        async def reqContractDetailsAsync(self, _query):
            raise RuntimeError("broker contract-details read failed")

    monkeypatch.setattr(ibkr, "Option", lambda *args, **kwargs: SimpleNamespace())
    assert await ibkr.option_contracts_for_expiry(
        FakeIB(), "SYMQ", "20270115", "P", ref_price=120, timeout_s=1) == []


@pytest.mark.asyncio
async def test_nonawaitable_legacy_adapter_is_explicit_compatibility_signal(monkeypatch):
    class LegacyIB:
        def reqContractDetailsAsync(self, _query):
            return []

    monkeypatch.setattr(ibkr, "Option", lambda *args, **kwargs: SimpleNamespace())
    assert await ibkr.option_contracts_for_expiry(
        LegacyIB(), "SYMQ", "20270115", "P", ref_price=120, timeout_s=1) is None


def test_all_three_entry_builders_use_expiry_specific_contract_discovery():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for relative in ("exitmgr/entry_builder.py", "exitmgr/trader.py", "daily_recommend.py",
                     "place_trade.py"):
        source = (root / relative).read_text()
        assert "option_contracts_for_expiry(" in source


@pytest.mark.asyncio
async def test_manual_route_picks_standard_chain_and_exact_expiry_contract(monkeypatch):
    import place_trade

    bad = SimpleNamespace(exchange="MIAX", tradingClass="SYMQ", multiplier="100",
                          expirations={"20270115"}, strikes={120.0})
    good = SimpleNamespace(exchange="SMART", tradingClass="SYMQ", multiplier="100",
                           expirations={"20270115", "20270219"}, strikes={120.0, 125.0})
    exact = _contract(77, 125)
    observed = {}

    async def exact_series(ib, symbol, expiry, right, **kwargs):
        observed.update(symbol=symbol, expiry=expiry, right=right, **kwargs)
        return [_contract(76, 120), exact]

    class FakeIB:
        async def qualifyContractsAsync(self, _stock):
            return [SimpleNamespace(conId=9001)]

        async def reqSecDefOptParamsAsync(self, symbol, blank, sec_type, con_id):
            assert (symbol, blank, sec_type, con_id) == ("SYMQ", "", "STK", 9001)
            return [bad, good]

    monkeypatch.setattr(place_trade, "option_contracts_for_expiry", exact_series)
    chain, expiry, strike, result = await place_trade._resolve_exact_option(
        FakeIB(), "SYMQ", "P", "20270115", 124.0)

    assert chain is good
    assert (expiry, strike, result.conId) == ("20270115", 125.0, 77)
    assert observed == {
        "symbol": "SYMQ", "expiry": "20270115", "right": "P",
        "exchange": "SMART", "ref_price": 124.0, "trading_class": "SYMQ",
        "multiplier": "100", "currency": "USD"}


@pytest.mark.asyncio
async def test_manual_route_refuses_union_strike_fallback(monkeypatch):
    import place_trade

    chain = SimpleNamespace(exchange="SMART", tradingClass="SYMQ", multiplier="100",
                            expirations={"20270115"}, strikes={999.0})

    class FakeIB:
        async def qualifyContractsAsync(self, _stock):
            return [SimpleNamespace(conId=9001)]

        async def reqSecDefOptParamsAsync(self, *args):
            return [chain]

    async def no_exact(*args, **kwargs):
        return []

    monkeypatch.setattr(place_trade, "option_contracts_for_expiry", no_exact)
    with pytest.raises(ValueError, match="expiry-specific"):
        await place_trade._resolve_exact_option(
            FakeIB(), "SYMQ", "P", "20270115", 999.0)
