"""Market context for the strategist: live quotes for the universe, formatted into a brief.

`format_context` is pure (testable). `fetch_universe_quotes` does the IB I/O and is called by
the orchestrator; failures there fall back to a no-quotes brief rather than crashing the cycle.
"""
from typing import Dict, List


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
                     " (every entry still needs human approval).")
    else:
        lines.append("Universe: " + ", ".join(universe))
    lines.append("Propose only high-conviction, defined-risk ideas, or none.")
    return "\n".join(lines)


def usable_price(px) -> bool:
    """IB reports 'no data' as None, NaN, or a -1.0 sentinel (common after hours on
    delayed feeds) -- only a finite positive number is a real price."""
    return px is not None and px == px and px > 0


async def fetch_universe_quotes(ib, symbols: List[str]) -> Dict[str, dict]:
    """Qualify each underlying as a US stock, fetch tickers, compute % change vs prior close."""
    from exitmgr.ibkr import Stock
    qc = await ib.qualifyContractsAsync(*[Stock(s, "SMART", "USD") for s in symbols])
    tickers = await ib.reqTickersAsync(*[c for c in qc if getattr(c, "conId", None)])
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
