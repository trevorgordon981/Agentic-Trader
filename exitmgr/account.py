"""Live account/pot valuation from IBKR (DYNAMIC pot sizing).

The pot is never hardcoded: we read NetLiquidation live each cycle and the risk gate
computes every cap as a % of it, so sizing scales automatically as the account moves.
"""
import asyncio
from dataclasses import dataclass


@dataclass
class PotSnapshot:
    net_liq: float          # NetLiquidation -- total account value; the "pot" for % caps
    available_funds: float  # buying power available for new entries
    cash: float             # TotalCashValue


async def get_pot_snapshot(ib) -> PotSnapshot:
    """Read live account values via ib_async.accountSummaryAsync()."""
    # BOUNDED (2026-08-13). This had no timeout and no guard: a stalled account read hung the
    # caller indefinitely. Callers are ENTRY-path only, and entry_safety.account_snapshot_valid()
    # already rejects unavailable values, so letting the TimeoutError propagate blocks NEW trades
    # (fail-safe) and can never unprotect an open position. Fabricating a zero snapshot instead
    # would silently drive every %-of-pot cap off 0.
    _ACCT_TIMEOUT_S = 30
    rows = await asyncio.wait_for(ib.accountSummaryAsync(), _ACCT_TIMEOUT_S)

    def val(tag: str, default: float = 0.0) -> float:
        for r in rows:
            if getattr(r, "tag", None) == tag:
                try:
                    return float(r.value)
                except (TypeError, ValueError):
                    return default
        return default

    return PotSnapshot(
        net_liq=val("NetLiquidation"),
        available_funds=val("AvailableFunds"),
        cash=val("TotalCashValue"),
    )
