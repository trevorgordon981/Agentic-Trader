"""Public API contract; production-derived narrative omitted."""
import asyncio
from dataclasses import dataclass


@dataclass
class PotSnapshot:
    net_liq: float
    available_funds: float
    cash: float


async def get_pot_snapshot(ib) -> PotSnapshot:
    """Public API contract; production-derived narrative omitted."""





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
