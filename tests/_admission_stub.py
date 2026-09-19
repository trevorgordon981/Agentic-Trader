"""Public API contract; production-derived narrative omitted."""
from unittest.mock import AsyncMock, MagicMock


def _account_rows(net_liq, available):
    rows = []
    for tag, value in (("NetLiquidation", net_liq), ("AvailableFunds", available),
                       ("TotalCashValue", available)):
        row = MagicMock()
        row.tag = tag
        row.value = str(value)
        rows.append(row)
    return rows


def stub_admission_reads(ib_conn, *, net_liq=100_000.0, available_funds=60_000.0,
                         positions=None, open_orders=()):
    """Public API contract; production-derived narrative omitted."""
    ib = ib_conn.ib
    ib.accountSummaryAsync = AsyncMock(return_value=_account_rows(net_liq, available_funds))
    ib.reqAllOpenOrdersAsync = AsyncMock(return_value=list(open_orders))
    ib.reqPositionsAsync = AsyncMock(return_value=[])
    ib_conn.get_positions = AsyncMock(return_value=dict(positions or {}))
    return ib_conn
