"""Tests for the options-flow brief line (2026-07-26 long-dated chain block).

Written because `_opt_one_ib` had NO test coverage before this change, and the change
touches the only options data the strategist model ever sees.
"""
from __future__ import annotations

import datetime as dt
import types

import pytest

from exitmgr import enrichment as E


class _Greeks:
    def __init__(self, iv, delta):
        self.impliedVol, self.delta = iv, delta


class _Contract:
    def __init__(self, strike, right, conId=1):
        self.strike, self.right, self.conId = strike, right, conId


class _Ticker:
    def __init__(self, strike, right, volume=100, iv=0.45, delta=0.5, bid=1.0, ask=1.2):
        self.contract = _Contract(strike, right)
        self.volume, self.bid, self.ask = volume, bid, ask
        self.modelGreeks = _Greeks(iv, delta) if iv is not None else None
        self.lastGreeks = None


def _chain(expirations, strikes):
    return types.SimpleNamespace(expirations=set(expirations), strikes=sorted(strikes))


def _fake_ib(expirations, strikes, ticker_factory, spot=100.0):
    """Minimal ib stub. ticker_factory(expiry_used, contracts) -> list[_Ticker]."""
    calls = {"qualify": 0, "tickers": 0, "expiries": []}

    class IB:
        async def qualifyContractsAsync(self, *cs):
            calls["qualify"] += 1
            out = []
            for c in cs:
                if isinstance(c, _Contract) or hasattr(c, "strike"):
                    out.append(c)
                else:                       # the Stock() qualify
                    c.conId = 42
                    out.append(c)
            return out

        async def reqSecDefOptParamsAsync(self, *a, **k):
            return [_chain(expirations, strikes)]

        async def reqTickersAsync(self, *cs):
            calls["tickers"] += 1
            exp = getattr(cs[0], "lastTradeDateOrContractMonth", None)
            calls["expiries"].append(exp)
            return ticker_factory(exp, cs)

    return IB(), calls


@pytest.fixture(autouse=True)
def _patch_ibkr(monkeypatch):
    """Patch the exitmgr.ibkr helpers _opt_one_ib imports at call time."""
    import exitmgr.ibkr as ibkr

    def _Option(sym, expiry, strike, right, exch):
        c = _Contract(strike, right)
        c.lastTradeDateOrContractMonth = expiry
        return c

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
    out = await E._opt_one_ib(ib, "MU")
    assert out.startswith("MU: ")
    # both tenors present, and tagged with their DTE
    assert "ATM IV 60% (" in out and "ATM IV 38% (" in out
    # real mid quotes, total dollars per contract: (2.0+2.4)/2 * 100 = 220
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
            # bid missing, zero ask, and a crossed book -- none may become a quote
            return [_Ticker(cs[0].strike, cs[0].right, bid=None, ask=3.0),
                    _Ticker(cs[1].strike, cs[1].right, bid=1.0, ask=0.0),
                    _Ticker(cs[2].strike, cs[2].right, bid=5.0, ask=4.0)]
        return [_Ticker(c.strike, c.right) for c in cs]

    ib, _ = _fake_ib([near, far], [95.0, 100.0, 105.0], tf)
    out = await E._opt_one_ib(ib, "XYZ")
    assert "mid/contract" not in out, "no two-sided book -> no quotes emitted"


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
