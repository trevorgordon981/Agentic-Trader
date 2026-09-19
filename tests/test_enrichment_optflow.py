"""Public API contract; production-derived narrative omitted."""
from __future__ import annotations

import datetime as dt
import types

import pytest

from exitmgr import enrichment as E


class _Greeks:
    def __init__(self, iv, delta):
        self.impliedVol, self.delta = iv, delta


class _Contract:
    def __init__(self, strike, right, conId=1, *, symbol="MUTX", expiry="",
                 trading_class=None, multiplier="100"):
        self.strike, self.right, self.conId = strike, right, conId
        self.symbol = symbol
        self.lastTradeDateOrContractMonth = expiry
        self.secType = "OPT"
        self.currency = "USD"
        self.tradingClass = trading_class or symbol
        self.multiplier = multiplier


class _Ticker:
    def __init__(self, strike, right, volume=100, iv=0.45, delta=0.5, bid=1.0, ask=1.2):
        self.contract = _Contract(strike, right)
        self.volume, self.bid, self.ask = volume, bid, ask
        self.modelGreeks = _Greeks(iv, delta) if iv is not None else None
        self.lastGreeks = None


def _chain(expirations, strikes, symbol="MUTX"):
    return types.SimpleNamespace(
        expirations=set(expirations), strikes=sorted(strikes),
        tradingClass=symbol, multiplier="100")


def _fake_ib(expirations, strikes, ticker_factory, spot=100.0):
    """Public API contract; production-derived narrative omitted."""
    by_expiry = strikes if isinstance(strikes, dict) else {
        expiry: list(strikes) for expiry in expirations
    }
    chain_strikes = sorted({strike for values in by_expiry.values() for strike in values})
    calls = {
        "qualify": 0, "tickers": 0, "expiries": [], "details": [],
        "requested": [], "batch_sizes": [],
    }

    class IB:
        async def qualifyContractsAsync(self, *cs):
            calls["qualify"] += 1
            out = []
            for c in cs:
                if isinstance(c, _Contract) or hasattr(c, "strike"):
                    out.append(c)
                else:
                    c.conId = 42
                    out.append(c)
            return out

        async def reqSecDefOptParamsAsync(self, *a, **k):
            return [_chain(expirations, chain_strikes)]

        async def reqContractDetailsAsync(self, query):
            expiry = query.lastTradeDateOrContractMonth
            right = query.right
            calls["details"].append((expiry, right, query.strike))
            out = []
            for index, strike in enumerate(by_expiry.get(expiry, []), start=1):
                con_id = int(expiry[-4:]) * 100_000 + index * 10 + (1 if right == "C" else 2)
                contract = _Contract(
                    strike, right, con_id, symbol=query.symbol, expiry=expiry,
                    trading_class=query.tradingClass, multiplier=query.multiplier)
                out.append(types.SimpleNamespace(contract=contract))
            return out

        async def reqTickersAsync(self, *cs):
            calls["tickers"] += 1
            exp = getattr(cs[0], "lastTradeDateOrContractMonth", None)
            calls["expiries"].append(exp)
            calls["batch_sizes"].append(len(cs))
            calls["requested"].extend(
                (c.lastTradeDateOrContractMonth, c.strike, c.right, c.conId) for c in cs)
            return ticker_factory(exp, cs)

    return IB(), calls


@pytest.fixture(autouse=True)
def _patch_ibkr(monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    import exitmgr.ibkr as ibkr

    def _Option(sym, expiry, strike, right, exch, multiplier="", currency="USD", **kwargs):
        return _Contract(
            strike, right, 0, symbol=sym, expiry=expiry,
            trading_class=kwargs.get("tradingClass") or sym, multiplier=multiplier or "100")

    def _Stock(sym, exch, cur):
        return types.SimpleNamespace(symbol=sym, conId=None)

    async def _underlying_price(ib, stk):
        return 100.0

    monkeypatch.setattr(ibkr, "Option", _Option, raising=False)
    monkeypatch.setattr(ibkr, "Stock", _Stock, raising=False)
    monkeypatch.setattr(ibkr, "underlying_price", _underlying_price, raising=False)
    monkeypatch.setattr(ibkr, "pick_chain", lambda params, sym: params[0], raising=False)
    monkeypatch.setattr(ibkr, "strikes_near",
                        lambda strikes, ref, per_side=5:
                        sorted(strikes, key=lambda k: abs(k - ref))[:per_side * 2],
                        raising=False)


def _exps(*day_offsets):
    today = dt.date.today()
    return [(today + dt.timedelta(days=d)).strftime("%Y%m%d") for d in day_offsets]


@pytest.mark.asyncio
async def test_emits_both_tenors_and_real_mid_quotes():
    near, far = _exps(30, 400)
    ib, calls = _fake_ib([near, far], [90.0, 95.0, 100.0, 105.0, 110.0],
                         lambda exp, cs: [_Ticker(c.strike, c.right,
                                                  iv=0.60 if exp == near else 0.38,
                                                  bid=2.0, ask=2.4) for c in cs])
    out = await E._opt_one_ib(ib, "MUTX")
    assert out.startswith("MUTX: ")

    assert "ATM IV 60% (" in out and "ATM IV 38% (" in out

    assert "mid/contract" in out and "$220" in out
    assert calls["tickers"] == 2, "should sample the near AND the long expiry"


@pytest.mark.asyncio
async def test_degrades_to_prior_behaviour_when_no_leap_listed():
    (near,) = _exps(30)
    ib, calls = _fake_ib([near], [95.0, 100.0, 105.0],
                         lambda exp, cs: [_Ticker(c.strike, c.right) for c in cs])
    out = await E._opt_one_ib(ib, "XYZ")
    assert "mid/contract" not in out
    assert calls["tickers"] == 1, "no long expiry -> no extra IBKR round trip"
    assert "ATM IV" in out and "P/C" in out


@pytest.mark.asyncio
async def test_one_sided_and_crossed_books_are_not_prices():
    near, far = _exps(30, 400)

    def tf(exp, cs):
        if exp == far:

            return [_Ticker(cs[0].strike, cs[0].right, bid=None, ask=3.0),
                    _Ticker(cs[1].strike, cs[1].right, bid=1.0, ask=0.0),
                    _Ticker(cs[2].strike, cs[2].right, bid=5.0, ask=4.0)]
        return [_Ticker(c.strike, c.right) for c in cs]

    ib, _ = _fake_ib([near, far], [95.0, 100.0, 105.0], tf)
    out = await E._opt_one_ib(ib, "XYZ")
    assert "mid/contract" not in out, "no two-sided book -> no quotes emitted"


@pytest.mark.asyncio
async def test_expiry_specific_contract_details_prevent_union_strike_cartesian_requests():
    near, far = _exps(30, 400)


    valid = {
        near: [97.5, 100.0, 102.5],
        far: [90.0, 100.0, 110.0],
    }
    ib, calls = _fake_ib(
        [near, far], valid,
        lambda exp, cs: [_Ticker(c.strike, c.right) for c in cs],
    )

    await E._opt_one_ib(ib, "MUTX")

    assert calls["qualify"] == 1, "only the stock is qualified; option guesses are forbidden"
    assert calls["details"] == [
        (near, "C", 0.0), (near, "P", 0.0),
        (far, "C", 0.0), (far, "P", 0.0),
    ]
    for expiry, strike, _right, _con_id in calls["requested"]:
        assert strike in valid[expiry]
    assert max(calls["batch_sizes"]) <= E._OPTION_SNAPSHOT_BATCH_SIZE
    requested_ids = [row[3] for row in calls["requested"]]
    assert len(requested_ids) == len(set(requested_ids))


@pytest.mark.asyncio
async def test_exact_expiry_options_deduplicates_and_rejects_identity_drift():
    (expiry,) = _exps(30)
    chain = _chain([expiry], [95.0, 100.0, 105.0], symbol="MUTX")
    good = _Contract(100.0, "C", 101, symbol="MUTX", expiry=expiry)
    duplicate = _Contract(100.0, "C", 101, symbol="MUTX", expiry=expiry)
    wrong_expiry = _Contract(100.0, "C", 102, symbol="MUTX", expiry=_exps(31)[0])
    wrong_class = _Contract(
        100.0, "P", 103, symbol="MUTX", expiry=expiry, trading_class="MUTX1")
    put = _Contract(95.0, "P", 104, symbol="MUTX", expiry=expiry)

    class IB:
        async def reqContractDetailsAsync(self, query):
            rows = [good, duplicate, wrong_expiry] if query.right == "C" else [wrong_class, put]
            return [types.SimpleNamespace(contract=contract) for contract in rows]

    contracts = await E._exact_expiry_options(
        IB(), "MUTX", expiry, chain, 100.0, max_per_right=50)

    assert [contract.conId for contract in contracts] == [101, 104]
    assert len(contracts) <= E._OPTION_MAX_PER_RIGHT * 2


@pytest.mark.asyncio
async def test_exact_expiry_options_hard_caps_each_right_nearest_spot_first():
    (expiry,) = _exps(30)
    chain = _chain([expiry], range(50, 151), symbol="MUTX")

    class IB:
        async def reqContractDetailsAsync(self, query):
            offset = 1_000 if query.right == "C" else 2_000
            return [types.SimpleNamespace(contract=_Contract(
                float(strike), query.right, offset + strike, symbol="MUTX", expiry=expiry))
                    for strike in range(50, 151)]

    contracts = await E._exact_expiry_options(
        IB(), "MUTX", expiry, chain, 100.0, max_per_right=10_000)

    calls = [contract for contract in contracts if contract.right == "C"]
    puts = [contract for contract in contracts if contract.right == "P"]
    assert len(calls) == len(puts) == E._OPTION_MAX_PER_RIGHT
    assert max(abs(contract.strike - 100.0) for contract in contracts) <= 8.0


@pytest.mark.asyncio
async def test_option_ticker_requests_are_deduplicated_batched_and_partial_on_timeout():
    contracts = [types.SimpleNamespace(conId=index) for index in range(1, 36)]
    contracts.extend([types.SimpleNamespace(conId=1), types.SimpleNamespace(conId=2)])

    class IB:
        def __init__(self):
            self.batch_sizes = []

        async def reqTickersAsync(self, *batch):
            self.batch_sizes.append(len(batch))
            if len(self.batch_sizes) == 2:
                raise TimeoutError("one broker snapshot batch timed out")
            return list(batch)

    ib = IB()
    tickers = await E._bounded_option_tickers(ib, contracts)

    assert ib.batch_sizes == [16, 16, 3]
    assert [ticker.conId for ticker in tickers] == list(range(1, 17)) + [33, 34, 35]


@pytest.mark.asyncio
async def test_long_block_failure_cannot_break_the_brief():
    near, far = _exps(30, 400)

    def tf(exp, cs):
        if exp == far:
            raise RuntimeError("IBKR pacing violation")
        return [_Ticker(c.strike, c.right) for c in cs]

    ib, _ = _fake_ib([near, far], [95.0, 100.0, 105.0], tf)
    out = await E._opt_one_ib(ib, "XYZ")
    assert out is not None and "ATM IV" in out, "a long-dated failure must not lose the near data"
    assert "mid/contract" not in out
