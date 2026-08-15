"""IB library shim.

Prefer the maintained `ib_async` (py3.10+); fall back to the older `ib_insync` (works on
Hermes's py3.9.6). The public API is identical across both, so the rest of the app imports
from here and runs unchanged in either environment.
"""

import asyncio   # used by the bounded-read guards below (wait_for)
try:
    import ib_async as _ib            # maintained fork, py3.10+
    BACKEND = "ib_async"
except ImportError:                    # pragma: no cover
    import ib_insync as _ib            # legacy, py3.9-compatible (Hermes)
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

# --- option-chain selection helpers (added 2026-06-18) ---------------------------------
# reqSecDefOptParams returns one entry per (exchange, tradingClass) and the order is
# arbitrary: params[0] was MIAX for RKLB -> Option(exchange="MIAX") qualifies as an "Unknown
# contract" (a specific non-SMART exchange does not resolve generically). Whole chains also
# carry stale adjusted strikes (e.g. QQQ 174.78) far from spot that 404 on qualify. These
# helpers pick the canonical SMART chain and window candidates to near-the-money strikes.
import bisect as _bisect

# BOUNDED BROKER READS (2026-08-13). An unbounded `await ib.*Async()` wedged the exit
# loop for 6+ minutes tonight with every position unevaluated. Same guard applied here.
# A timeout raises into the caller's existing error handling; a hang has no handler.
_IB_CALL_TIMEOUT_S = 30



def pick_chain(params, underlying):
    """Canonical chain from reqSecDefOptParams: SMART exchange + trading class matching the
    underlying symbol. Falls back to any-SMART, then params[0]. None if params is empty."""
    if not params:
        return None
    pool = [p for p in params if getattr(p, "exchange", "") == "SMART"] or list(params)
    std = [p for p in pool if getattr(p, "tradingClass", "") == underlying]
    return (std or pool)[0]


def strikes_near(strikes, ref_price, per_side=20):
    """Up to `per_side` strikes each side of `ref_price` (spot). Avoids qualifying the whole
    chain (hundreds of strikes incl. stale adjusted ones far from spot). No usable ref ->
    full sorted chain (prior behavior)."""
    ks = sorted({float(k) for k in strikes})
    if not ks:
        return []
    if not ref_price or ref_price != ref_price or ref_price <= 0:
        return ks
    i = _bisect.bisect_left(ks, ref_price)
    return ks[max(0, i - per_side): i + per_side]


async def underlying_price(ib, stk):
    """Best-effort spot for a qualified Stock: marketPrice, then last, then close. None if
    unavailable (caller falls back to the full chain)."""
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

