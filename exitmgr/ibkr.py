"""Public API contract; production-derived narrative omitted."""

import asyncio
import inspect
try:
    import ib_async as _ib
    BACKEND = "ib_async"
except ImportError:
    import ib_insync as _ib
    BACKEND = "ib_insync"

IB = _ib.IB
ComboLeg = _ib.ComboLeg
Contract = _ib.Contract
Index = getattr(_ib, "Index", None)
Option = _ib.Option
Order = _ib.Order
Position = _ib.Position
Stock = _ib.Stock
Ticker = getattr(_ib, "Ticker", None)







import bisect as _bisect




_IB_CALL_TIMEOUT_S = 30



def pick_chain(params, underlying):
    """Public API contract; production-derived narrative omitted."""
    if not params:
        return None
    pool = [p for p in params if getattr(p, "exchange", "") == "SMART"] or list(params)
    std = [p for p in pool if getattr(p, "tradingClass", "") == underlying]
    return (std or pool)[0]


def strikes_near(strikes, ref_price, per_side=20, band_pct=0.35, max_candidates=140):
    """Public API contract; production-derived narrative omitted."""
    ks = sorted({float(k) for k in strikes})
    if not ks:
        return []
    if not ref_price or ref_price != ref_price or ref_price <= 0:
        return ks
    lo, hi = ref_price * (1.0 - band_pct), ref_price * (1.0 + band_pct)
    band = [k for k in ks if lo <= k <= hi]
    if not band:
        i = _bisect.bisect_left(ks, ref_price)
        return ks[max(0, i - per_side): i + per_side]
    if len(band) > max_candidates:
        band.sort(key=lambda k: abs(k - ref_price))
        band = sorted(band[:max_candidates])
    return band


async def option_contracts_for_expiry(ib, symbol, expiry, right, exchange="SMART",
                                      ref_price=None, timeout_s=20, *,
                                      trading_class=None, multiplier="100",
                                      currency="USD"):
    """Public API contract; production-derived narrative omitted."""
    method = getattr(ib, "reqContractDetailsAsync", None)
    if not callable(method):
        return None
    wanted_symbol = str(symbol).upper()
    wanted_class = str(trading_class or symbol)
    wanted_multiplier = str(multiplier or "")
    wanted_currency = str(currency or "").upper()



    if wanted_multiplier != "100" or not wanted_class or not wanted_currency:
        return []
    query = Option(
        str(symbol), str(expiry), 0.0, str(right), str(exchange),
        multiplier=wanted_multiplier, currency=wanted_currency,
        tradingClass=wanted_class)
    try:
        pending = method(query)
        if not inspect.isawaitable(pending):
            return None
        details = await asyncio.wait_for(pending, float(timeout_s))
    except (asyncio.TimeoutError, TimeoutError):
        return []
    except Exception:
        return []

    exact = []
    wanted_expiry = str(expiry)
    wanted_right = str(right).upper()
    seen = set()
    for row in details or ():
        contract = getattr(row, "contract", None)
        try:
            con_id = int(getattr(contract, "conId", 0) or 0)
            strike = float(getattr(contract, "strike", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        observed_expiry = str(
            getattr(contract, "lastTradeDateOrContractMonth", "") or "")[:8]
        observed_right = str(getattr(contract, "right", "") or "").upper()
        observed_type = str(getattr(contract, "secType", "") or "").upper()
        observed_symbol = str(getattr(contract, "symbol", "") or "").upper()
        observed_class = str(getattr(contract, "tradingClass", "") or "")
        observed_multiplier = str(getattr(contract, "multiplier", "") or "")
        observed_currency = str(getattr(contract, "currency", "") or "").upper()
        observed_exchange = str(getattr(contract, "exchange", "") or "").upper()
        if (con_id <= 0 or strike <= 0 or observed_expiry != wanted_expiry
                or observed_right != wanted_right or observed_type != "OPT"
                or observed_symbol != wanted_symbol or observed_class != wanted_class
                or observed_multiplier != wanted_multiplier
                or observed_currency != wanted_currency
                or observed_exchange != str(exchange).upper() or con_id in seen):
            continue
        seen.add(con_id)
        exact.append(contract)

    allowed = set(strikes_near(
        [float(getattr(contract, "strike", 0) or 0) for contract in exact], ref_price))
    return sorted(
        [contract for contract in exact
         if float(getattr(contract, "strike", 0) or 0) in allowed],
        key=lambda contract: (float(getattr(contract, "strike", 0) or 0),
                              int(getattr(contract, "conId", 0) or 0)))


async def underlying_price(ib, stk):
    """Public API contract; production-derived narrative omitted."""
    try:
        tickers = await asyncio.wait_for(ib.reqTickersAsync(stk), _IB_CALL_TIMEOUT_S)
    except Exception:
        return None
    if not tickers:
        return None
    tk = tickers[0]
    for v in (tk.marketPrice(), getattr(tk, "last", None), getattr(tk, "close", None)):
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if v == v and v > 0:
            return v
    return None
