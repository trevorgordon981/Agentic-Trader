"""Public API contract; production-derived narrative omitted."""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple


def _num(v) -> Optional[float]:
    """Public API contract; production-derived narrative omitted."""
    try:
        if v is None:
            return None
        f = float(v)
        return f if f == f and f not in (float("inf"), float("-inf")) else None
    except (TypeError, ValueError):
        return None


def net_realized(row: Dict[str, Any]) -> Tuple[Optional[float], str]:
    """Public API contract; production-derived narrative omitted."""



    close = row.get("close") if isinstance(row.get("close"), dict) else {}
    if (row.get("usable_for_pnl") is False
            or row.get("pnl_valid") is False
            or row.get("pnl_quarantined") is True
            or row.get("commission_unknown") is True
            or close.get("pnl_valid") is False
            or close.get("pnl_quarantined") is True
            or close.get("commission_unknown") is True):
        return None, "invalid"

    stored = _num(row.get("realized_pnl_net"))
    if stored is not None:
        return stored, "stored_net"

    gross = _num(row.get("realized_pnl"))
    if gross is None:
        return None, "unknown"

    ec, xc = _num(row.get("entry_commission")), _num(row.get("exit_commission"))
    if ec is not None and xc is not None:
        return round(gross - ec - xc, 2), "gross_minus_fees"

















    return None, "unknown"


def ledger_net(rows) -> Dict[str, Any]:
    """Public API contract; production-derived narrative omitted."""
    total = 0.0
    known = unknown = 0
    by_basis: Dict[str, int] = {}
    for r in rows:


        if r.get("realized_pnl") is None and r.get("realized_pnl_net") is None:
            continue
        net, basis = net_realized(r)
        by_basis[basis] = by_basis.get(basis, 0) + 1
        if net is None:
            unknown += 1
        else:
            total += net
            known += 1
    known_subtotal = round(total, 2)
    return {
        "total": known_subtotal,
        "known_subtotal": known_subtotal,
        "canonical_total": known_subtotal if unknown == 0 else None,
        "complete": unknown == 0,
        "rows_known": known,
        "rows_unknown": unknown,
        "rows_realized": known + unknown,
        "by_basis": by_basis,
    }
