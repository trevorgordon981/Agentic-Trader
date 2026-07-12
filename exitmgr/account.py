"""Live account/pot valuation from IBKR (DYNAMIC pot sizing).

The pot is never hardcoded: we read NetLiquidation live each cycle and the risk gate
computes every cap as a % of it, so sizing scales automatically as the account moves.
"""
from dataclasses import dataclass


@dataclass
class PotSnapshot:
    net_liq: float          # NetLiquidation -- total account value; the "pot" for % caps
    available_funds: float  # buying power available for new entries
    cash: float             # TotalCashValue


async def get_pot_snapshot(ib) -> PotSnapshot:
    """Read live account values via ib_async.accountSummaryAsync()."""
    rows = await ib.accountSummaryAsync()

    def val(tag: str, default: float = 0.0) -> float:
        for r in rows:
            if getattr(r, "tag", None) == tag:
                try:
                    return float(r.value)
                except (TypeError, ValueError):
                    return default
        return default

    snapshot = PotSnapshot(
        net_liq=val("NetLiquidation"),
        available_funds=val("AvailableFunds"),
        cash=val("TotalCashValue"),
    )
    try:
        from exitmgr.byron_evidence import record_account_snapshot
        record_account_snapshot({
            "net_liq": snapshot.net_liq,
            "available_funds": snapshot.available_funds,
            "cash": snapshot.cash,
            "source": "IBKR.accountSummaryAsync",
        })
    except Exception:
        pass
    return snapshot
