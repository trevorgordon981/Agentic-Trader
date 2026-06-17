"""IB library shim.

Prefer the maintained `ib_async` (py3.10+); fall back to the older `ib_insync` (works on
Hermes's py3.9.6). The public API is identical across both, so the rest of the app imports
from here and runs unchanged in either environment.
"""
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
