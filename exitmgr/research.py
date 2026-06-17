"""Research brief for the strategist: price structure, events, headlines, and the current book.

Upgrades the strategist's input from a bare quote snapshot to grounded market data, while the
strategist itself stays a single locked-prompt completion (prompt-independent, testable).

Every fetcher is fail-soft and time-boxed: a dead feed degrades its section to a fallback line
instead of killing the trading cycle. Pure formatters (`momentum_stats`, `parse_rss_titles`,
`build_brief`, `next_events`) are unit-tested; `gather` does the I/O.

Data sources (all free, keyless):
  * IB delayed historical bars  -> 5d/20d momentum, distance from range, realized vol
  * IB delayed index quote      -> VIX level
  * Static published Fed schedule -> next FOMC decision date
  * yfinance (if installed)     -> next earnings date per single name
  * Yahoo Finance RSS           -> recent headlines per symbol
"""
import asyncio
import math
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from typing import Dict, List, Optional

from exitmgr.risk import INDEX_UNDERLYINGS

SECTION_TIMEOUT_S = 20
HEADLINES_PER_SYMBOL = 3
MAX_HEADLINES = 12

# Published FOMC decision days (second day of each 2026 meeting). Static by design:
# the schedule is fixed a year ahead and a wrong scrape is worse than a short list.
FOMC_DECISIONS_2026 = [
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
]


# ---------------------------------------------------------------- pure helpers (unit-tested)

def momentum_stats(closes: List[float]) -> Optional[dict]:
    """5d/20d return, distance from the 20d high/low, and annualized 20d realized vol,
    from daily closes oldest-first. None if there isn't enough usable history."""
    closes = [c for c in closes if c is not None and c == c and c > 0]
    if len(closes) < 6:
        return None
    last = closes[-1]

    def ret(n: int) -> Optional[float]:
        return (last - closes[-(n + 1)]) / closes[-(n + 1)] * 100.0 if len(closes) > n else None

    window = closes[-21:]
    hi, lo = max(window), min(window)
    rets = [(window[i] - window[i - 1]) / window[i - 1] for i in range(1, len(window))]
    vol = None
    if len(rets) >= 5:
        m = sum(rets) / len(rets)
        var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
        vol = math.sqrt(var) * math.sqrt(252) * 100.0
    return {
        "last": last, "ret_5d": ret(5), "ret_20d": ret(20),
        "from_high_pct": (last - hi) / hi * 100.0,
        "from_low_pct": (last - lo) / lo * 100.0,
        "vol_20d_ann": vol,
    }


def next_events(today: date, earnings: Optional[List[tuple]] = None,
                fomc_dates: Optional[List[str]] = None, horizon_days: int = 45) -> List[str]:
    """Upcoming-event lines: next FOMC decision + per-name earnings within the horizon."""
    out = []
    for ds in (fomc_dates if fomc_dates is not None else FOMC_DECISIONS_2026):
        try:
            d = date.fromisoformat(ds)
        except ValueError:
            continue
        if 0 <= (d - today).days <= horizon_days:
            out.append(f"FOMC rate decision {d.isoformat()} (in {(d - today).days}d)")
            break
    for sym, ds in earnings or []:
        try:
            d = date.fromisoformat(str(ds)[:10])
        except ValueError:
            continue
        if 0 <= (d - today).days <= horizon_days:
            out.append(f"{sym} earnings {d.isoformat()} (in {(d - today).days}d)")
    return out


def matches_blocked_sector(industry: Optional[str], sector: Optional[str],
                           keywords: List[str]) -> bool:
    """True if a name's yfinance industry/sector matches any blocked keyword (case-insensitive).
    Pure + tested; the I/O (looking up the industry) is in `sector_of`."""
    hay = " ".join(x for x in (industry, sector) if x).lower()
    return any(k.strip().lower() in hay for k in keywords if k.strip())


def sector_of(ticker: str):
    """(industry, sector) for a ticker via yfinance, or (None, None) on any failure. Best-effort:
    a blocked-sector filter that fails to look up a name lets it through to human approval."""
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info or {}
        return info.get("industry"), info.get("sector")
    except Exception:
        return None, None


def parse_rss_titles(xml_text: str, limit: int = HEADLINES_PER_SYMBOL) -> List[str]:
    """Item titles from an RSS feed (the first <title> is the channel name, skip it)."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    titles = [el.text.strip() for el in root.iter("title") if el.text and el.text.strip()]
    return titles[1:limit + 1]


def _pct(v: Optional[float], signed: bool = True) -> str:
    if v is None or v != v:
        return "n/a"
    return f"{v:+.1f}%" if signed else f"{v:.1f}%"


def build_brief(*, today: str, quotes: Dict[str, dict], universe: List[str],
                allow_any_name: bool, price_stats: Optional[Dict[str, Optional[dict]]] = None,
                vix: Optional[float] = None, events: Optional[List[str]] = None,
                headlines: Optional[List[str]] = None, book: Optional[list] = None,
                day_pnl_pct: Optional[float] = None) -> str:
    """The full strategist brief. Sections with no data degrade to explicit fallback lines so
    the model knows the data is missing rather than implicitly flat."""
    lines = [f"Date (UTC): {today}", "Delayed quote snapshot (~15min lag; symbol: last, day change):"]
    shown = 0
    for sym in universe:
        q = quotes.get(sym) or {}
        last, chg = q.get("last"), q.get("change_pct")
        if last is None:
            continue
        chg_s = f"{chg:+.2f}%" if isinstance(chg, (int, float)) and chg == chg else "n/a"
        lines.append(f"  {sym}: {last:.2f} ({chg_s})")
        shown += 1
    if shown == 0:
        lines.append("  (quotes unavailable this cycle)")

    lines.append("Price structure (daily closes):")
    if price_stats:
        for sym, st in price_stats.items():
            if not st:
                lines.append(f"  {sym}: history unavailable")
                continue
            lines.append(f"  {sym}: {st['last']:.2f} | 5d {_pct(st['ret_5d'])} | 20d {_pct(st['ret_20d'])}"
                         f" | {_pct(st['from_high_pct'])} from 20d high | 20d vol {_pct(st['vol_20d_ann'], signed=False)}")
    else:
        lines.append("  (unavailable this cycle)")

    lines.append(f"VIX: {vix:.1f}" if isinstance(vix, (int, float)) and vix == vix else "VIX: unavailable")

    lines.append("Upcoming events:")
    lines.extend(f"  - {e}" for e in events or []) if events else lines.append("  (none within 45 days / unavailable)")

    lines.append("Recent headlines:")
    lines.extend(f"  - {h}" for h in headlines or []) if headlines else lines.append("  (unavailable this cycle)")

    lines.append("Current book:")
    if book:
        for p in book:
            kind = "index" if getattr(p, "is_index", False) else "single name"
            lines.append(f"  {getattr(p, 'underlying', '?')}: ~${getattr(p, 'notional', 0):,.0f} at risk ({kind})")
    else:
        lines.append("  no open positions")
    if isinstance(day_pnl_pct, (int, float)) and day_pnl_pct == day_pnl_pct:
        lines.append(f"Day P&L: {day_pnl_pct * 100:+.2f}%")

    if allow_any_name:
        lines.append("Universe: " + ", ".join(universe)
                     + " — plus any LIQUID large-cap US single name you have real conviction on"
                     " (every entry still needs human approval).")
    else:
        lines.append("Universe: " + ", ".join(universe))
    lines.append("Propose only high-conviction, defined-risk ideas, or none. Ground every thesis in the"
                 " data above — do NOT assume prices, events, or news that are not shown here.")
    return "\n".join(lines)


# ---------------------------------------------------------------- fetchers (I/O, fail-soft)

async def _boxed(coro):
    try:
        return await asyncio.wait_for(coro, SECTION_TIMEOUT_S)
    except Exception:
        return None


async def _price_structure(ib, symbols: List[str]) -> Dict[str, Optional[dict]]:
    from exitmgr.ibkr import Stock
    qc = await ib.qualifyContractsAsync(*[Stock(s, "SMART", "USD") for s in symbols])
    out: Dict[str, Optional[dict]] = {}
    for c in qc:
        if not getattr(c, "conId", None):
            continue
        try:
            bars = await ib.reqHistoricalDataAsync(
                c, endDateTime="", durationStr="30 D", barSizeSetting="1 day",
                whatToShow="TRADES", useRTH=True, formatDate=1)
            out[c.symbol] = momentum_stats([b.close for b in bars])
        except Exception:
            out[c.symbol] = None
    return out


async def _vix(ib) -> Optional[float]:
    from exitmgr.ibkr import Index
    from exitmgr.market import usable_price
    if Index is None:
        return None
    qc = await ib.qualifyContractsAsync(Index("VIX", "CBOE"))
    if not qc or not getattr(qc[0], "conId", None):
        return None
    tickers = await ib.reqTickersAsync(qc[0])
    for tk in tickers:
        for px in (tk.last, tk.close):
            if usable_price(px):
                return float(px)
    return None


def _earnings_sync(symbols: List[str]) -> List[tuple]:
    import yfinance as yf  # optional dep; ImportError is caught by the _boxed wrapper
    out = []
    for s in symbols:
        try:
            cal = yf.Ticker(s).calendar
            dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
            if dates:
                out.append((s, str(dates[0])))
        except Exception:
            continue
    return out


def _symbol_headlines_sync(sym: str) -> List[str]:
    # primary: yfinance news (Yahoo's JSON API; the old per-ticker RSS endpoint is dead/404)
    titles: List[str] = []
    try:
        import yfinance as yf
        for item in (yf.Ticker(sym).news or [])[:HEADLINES_PER_SYMBOL]:
            t = (item.get("content") or {}).get("title") or item.get("title")
            if t:
                titles.append(str(t).strip())
    except Exception:
        pass
    if titles:
        return titles
    # fallback: Google News RSS (keyless)
    try:
        url = f"https://news.google.com/rss/search?q={sym}+stock&hl=en-US&gl=US&ceid=US:en"
        with urllib.request.urlopen(url, timeout=10) as r:
            return parse_rss_titles(r.read().decode("utf-8", "replace"))
    except Exception:
        return []


def _headlines_sync(symbols: List[str]) -> List[str]:
    seen, out = set(), []
    for sym in symbols[:5]:
        for t in _symbol_headlines_sync(sym):
            if t not in seen:
                seen.add(t)
                out.append(t)
    return out[:MAX_HEADLINES]


async def gather(ib, symbols: List[str], single_names: Optional[List[str]] = None) -> dict:
    """Fetch all research sections concurrently. Returns {price_stats, vix, events, headlines};
    any failed section is None. `single_names` (held + approved non-index names) drive the
    earnings lookup -- index ETFs have no earnings."""
    names = sorted({s.upper() for s in (single_names or []) if s.upper() not in INDEX_UNDERLYINGS})
    price_stats, vix, earnings, headlines = await asyncio.gather(
        _boxed(_price_structure(ib, symbols)),
        _boxed(_vix(ib)),
        _boxed(asyncio.to_thread(_earnings_sync, names)) if names else _none(),
        _boxed(asyncio.to_thread(_headlines_sync, symbols)),
    )
    today = datetime.now(timezone.utc).date()
    return {
        "price_stats": price_stats,
        "vix": vix,
        "events": next_events(today, earnings or []),
        "headlines": headlines,
    }


async def _none():
    return None
