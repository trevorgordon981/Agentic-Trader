"""Public API contract; production-derived narrative omitted."""
import asyncio
from typing import Dict, List




_IB_CALL_TIMEOUT_S = 30



def format_context(quotes: Dict[str, dict], universe: List[str], today: str,
                   allow_any_name: bool = False) -> str:
    lines = [f"Date (UTC): {today}", "Market snapshot (symbol: last, day change):"]
    shown = 0
    for sym in universe:
        q = quotes.get(sym)
        if not q:
            continue
        last = q.get("last")
        chg = q.get("change_pct")
        last_s = f"{last:.2f}" if isinstance(last, (int, float)) and last == last else "n/a"
        chg_s = f"{chg:+.2f}%" if isinstance(chg, (int, float)) and chg == chg else "n/a"
        lines.append(f"  {sym}: {last_s} ({chg_s})")
        shown += 1
    if shown == 0:
        lines.append("  (live quotes unavailable this cycle)")
    if allow_any_name:
        lines.append("Universe: " + ", ".join(universe)
                     + " — plus any LIQUID large-cap US single name you have real conviction on"
                     " (gate-clean entries that also clear autonomous-entry checks may execute "
                     "automatically without a human tap; exceptions may require human approval).")
    else:
        lines.append("Universe: " + ", ".join(universe))
    lines.append("Propose only high-conviction, defined-risk ideas, or none.")
    return "\n".join(lines)


def usable_price(px) -> bool:
    """Public API contract; production-derived narrative omitted."""
    return px is not None and px == px and px > 0


async def fetch_universe_quotes(ib, symbols: List[str]) -> Dict[str, dict]:
    """Public API contract; production-derived narrative omitted."""
    from exitmgr.ibkr import Stock
    qc = await asyncio.wait_for(ib.qualifyContractsAsync(*[Stock(s, "SMART", "USD") for s in symbols]), _IB_CALL_TIMEOUT_S)
    tickers = await asyncio.wait_for(ib.reqTickersAsync(*[c for c in qc if getattr(c, "conId", None)]), _IB_CALL_TIMEOUT_S)
    out: Dict[str, dict] = {}
    for tk in tickers:
        sym = getattr(tk.contract, "symbol", None)
        if not sym:
            continue
        close = tk.close if usable_price(tk.close) else None
        last = tk.last if usable_price(tk.last) else close
        chg = None
        if last is not None and close:
            chg = (last - close) / close * 100.0
        out[sym] = {"last": last, "close": close, "change_pct": chg}
    return out
