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
  * Wall Street Horizon (IBKR reqWshMetaData/reqWshEventData) -> next earnings + ex-div (PRIMARY)
  * yfinance (if installed)     -> next earnings / ex-dividend date per single name (FALLBACK)
  * Yahoo Finance RSS           -> recent headlines per symbol
"""
import asyncio
import json
import math
import os
import urllib.request
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from typing import Dict, List, Optional

from exitmgr.risk import INDEX_UNDERLYINGS
from exitmgr import enrichment as _enrich

SECTION_TIMEOUT_S = 20
HEADLINES_PER_SYMBOL = 3
MAX_HEADLINES = 12

# --- Optional RAG enrichment (OFF by default) -------------------------------------------
# When STRATEGIST_RAG_ENABLED=1, the brief gains a small "Prior context from Trevor's corpus"
# block pulled from the rag-host RAG server. This is purely informational text the strategist
# reads; it NEVER changes which trades are proposed, sized, or executed. Fully fail-soft:
# any timeout/error/empty result yields no block and the cycle proceeds exactly as before.
RAG_ENABLED = os.environ.get("STRATEGIST_RAG_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")
RAG_ENDPOINT = os.environ.get("ALFRED_RAG_ENDPOINT", "http://localhost:9000/search")
RAG_TIMEOUT_S = float(os.environ.get("STRATEGIST_RAG_TIMEOUT_S", "8"))
RAG_TOP_K = int(os.environ.get("STRATEGIST_RAG_TOP_K", "4"))
RAG_SNIPPET_CHARS = 320


def _rag_query_sync(query: str, top_k: int = RAG_TOP_K) -> List[str]:
    """POST one query to the RAG server; return up to top_k short result snippets.
    Best-effort and self-contained: returns [] on any error so callers never handle
    exceptions. Uses stdlib urllib only (no new deps)."""
    try:
        body = json.dumps({"query": query, "top_k": top_k}).encode("utf-8")
        req = urllib.request.Request(
            RAG_ENDPOINT, data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=RAG_TIMEOUT_S) as r:
            payload = json.loads(r.read().decode("utf-8", "replace"))
        out = []
        for item in (payload.get("results") or [])[:top_k]:
            text = (item.get("text") or "").strip().replace(chr(10), " ")
            if not text:
                continue
            if len(text) > RAG_SNIPPET_CHARS:
                text = text[:RAG_SNIPPET_CHARS].rstrip() + "..."
            dom = item.get("domain")
            out.append(("[%s] %s" % (dom, text)) if dom else text)
        return out
    except Exception:
        return []


def rag_context_sync(symbols: List[str]) -> List[str]:
    """Gather prior corpus context for the names in play. Returns a deduped list of snippet
    lines (possibly empty). Disabled unless STRATEGIST_RAG_ENABLED is set. Never raises."""
    if not RAG_ENABLED:
        return []
    try:
        tickers = sorted({s.upper() for s in symbols if s})[:8]
        if not tickers:
            return []
        query = " ".join(tickers) + " thesis history trade journal conviction"
        seen, out = set(), []
        for line in _rag_query_sync(query):
            if line not in seen:
                seen.add(line)
                out.append(line)
        return out
    except Exception:
        return []

# Published FOMC decision days (second day of each 2026 meeting). Static by design:
# the schedule is fixed a year ahead and a wrong scrape is worse than a short list.
FOMC_DECISIONS_2026 = [
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
]


# ---------------------------------------------------------------- pure helpers (unit-tested)

def _ann_vol_20(closes: List[float], end: int) -> Optional[float]:
    """Annualized 20-day realized vol of the window ENDING at index `end` (inclusive),
    from a clean oldest-first closes list. None if the window is too short."""
    window = closes[max(0, end - 20):end + 1]
    if len(window) < 6:
        return None
    rets = [(window[i] - window[i - 1]) / window[i - 1] for i in range(1, len(window))]
    if len(rets) < 5:
        return None
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(252) * 100.0


def realized_vol_rank(closes: List[float], lookback: int = 252) -> Optional[int]:
    """IVR proxy: rank today's annualized 20d realized vol as a 0-100 percentile over the
    trailing `lookback` days. Historical option IV isn't available, so the fine-tune is
    trained on realized-vol-rank; live MUST use the same so the model sees one signal.
    None if there isn't enough history to compute a meaningful rank."""
    closes = [c for c in closes if c is not None and c == c and c > 0]
    if len(closes) < 26:  # need a 20d vol window + a few prior points to rank against
        return None
    series = []
    start = max(20, len(closes) - lookback)
    for end in range(start, len(closes)):
        v = _ann_vol_20(closes, end)
        if v is not None:
            series.append(v)
    if len(series) < 6:
        return None
    cur = series[-1]
    below = sum(1 for v in series if v < cur)
    return int(round(below / (len(series) - 1) * 100.0)) if len(series) > 1 else None


def vix_regime(level: Optional[float]) -> Optional[str]:
    """Map a VIX level to a coarse regime label. None if level is unusable."""
    if level is None or level != level:
        return None
    if level < 14:
        return "calm"
    if level < 19:
        return "normal"
    if level < 26:
        return "elevated"
    if level < 36:
        return "high"
    return "extreme"


def momentum_stats(closes: List[float]) -> Optional[dict]:
    """5d/20d return, distance from the 20d high/low, annualized 20d realized vol, and the
    realized-vol rank (IVR proxy, 0-100), from daily closes oldest-first. None if there
    isn't enough usable history."""
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
        "ivr": realized_vol_rank(closes),
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
                day_pnl_pct: Optional[float] = None,
                movers: Optional[dict] = None, options_flow: Optional[List[str]] = None,
                web_news: Optional[List[str]] = None,
                rag_snippets: Optional[List[str]] = None,
                opt_iv: Optional[Dict[str, float]] = None) -> str:
    """The full strategist brief. Sections with no data degrade to explicit fallback lines so
    the model knows the data is missing rather than implicitly flat.

    `opt_iv` (optional) maps symbol -> live OPRA option implied vol; when present it's appended
    to that name's IVR field as context. It's normally absent at brief-build time (the chosen
    contract isn't priced until after the model proposes), so each name emits a plain IVR."""
    # Parse the brief date once so the per-name days-to-earnings is computed against it.
    try:
        _today_date = datetime.strptime(today, "%Y-%m-%d").date()
    except Exception:
        _today_date = datetime.now(timezone.utc).date()
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

    vix_reg = vix_regime(vix)
    lines.append("Price structure (daily closes):")
    if price_stats:
        for sym, st in price_stats.items():
            if not st:
                lines.append(f"  {sym}: history unavailable")
                continue
            # IVR (realized-vol-rank proxy; matches the fine-tune's training signal). Append the
            # live OPRA option IV as context when it's available at build time, e.g. "IVR 72 (optIV 45%)".
            ivr = st.get("ivr")
            ivr_s = f"IVR {ivr}" if ivr is not None else "IVR n/a"
            oiv = (opt_iv or {}).get(sym)
            if ivr is not None and isinstance(oiv, (int, float)) and oiv == oiv:
                ivr_s += f" (optIV {oiv * 100:.0f}%)" if oiv < 5 else f" (optIV {oiv:.0f}%)"
            earn_s = _earnings_field(sym, today=_today_date)
            vix_s = f"VIX {vix:.0f} {vix_reg}" if vix_reg else "VIX n/a"
            lines.append(f"  {sym}: {st['last']:.2f} | 5d {_pct(st['ret_5d'])} | 20d {_pct(st['ret_20d'])}"
                         f" | {_pct(st['from_high_pct'])} from 20d high | 20d vol {_pct(st['vol_20d_ann'], signed=False)}"
                         f" | {ivr_s}. {earn_s}. {vix_s}.")
    else:
        lines.append("  (unavailable this cycle)")

    lines.append(f"VIX: {vix:.1f}" if isinstance(vix, (int, float)) and vix == vix else "VIX: unavailable")

    if movers:
        lines.append("Market movers today (whole US market, liquid names):")
        lines.extend("  " + r for r in _enrich.format_movers(movers))

    lines.append("Upcoming events:")
    lines.extend(f"  - {e}" for e in events or []) if events else lines.append("  (none within 45 days / unavailable)")

    lines.append("Recent headlines:")
    lines.extend(f"  - {h}" for h in headlines or []) if headlines else lines.append("  (unavailable this cycle)")

    if options_flow:
        lines.append("Options flow / positioning (near-term — what the makers are pricing):")
        lines.extend(f"  - {o}" for o in options_flow)
    if web_news:
        lines.append("Web research (sourced excerpts):")
        lines.extend(f"  - {w}" for w in web_news)

    lines.append("Current book:")
    if book:
        for p in book:
            kind = "index" if getattr(p, "is_index", False) else "single name"
            lines.append(f"  {getattr(p, 'underlying', '?')}: ~${getattr(p, 'notional', 0):,.0f} at risk ({kind})")
    else:
        lines.append("  no open positions")
    if isinstance(day_pnl_pct, (int, float)) and day_pnl_pct == day_pnl_pct:
        lines.append(f"Day P&L: {day_pnl_pct * 100:+.2f}%")

    if rag_snippets:
        lines.append("Prior context from Trevor's corpus (background only; do NOT treat as a"
                     " price/news feed or trade instruction):")
        lines.extend("  - " + s for s in rag_snippets)

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


async def _boxed_long(coro):
    try:
        return await asyncio.wait_for(coro, 75)   # IBKR options-flow per-contract tickers are slow
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
                c, endDateTime="", durationStr="1 Y", barSizeSetting="1 day",
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


# Per-run cache of days-to-next-earnings so the per-name brief line doesn't refetch yfinance
# for the same ticker. Reset at the start of each `gather` so a long-lived process stays fresh.
_EARNINGS_DAYS_CACHE: Dict[str, Optional[int]] = {}

# Per-run cache of days-to-next-ex-dividend (assignment-risk gate A6 input). Same lifecycle as
# the earnings cache -- cleared at the start of each `gather`.
_EX_DIV_DAYS_CACHE: Dict[str, Optional[int]] = {}


# --- Earnings + ex-dividend feed: Wall Street Horizon (PRIMARY) -> yfinance (FALLBACK) -------
# days_to_earnings() / days_to_ex_dividend() firm up the earnings-blackout + assignment gates.
# The PRIMARY source is Wall Street Horizon (WSH) corporate-event data pulled via the IBKR API and
# prefetched ASYNC in gather() (prefetch_wsh_events) into the per-run caches below, so the in-loop
# SYNC readers just read cache. yfinance is the FALLBACK when WSH has no cached hit. The public
# signatures + int|None return contract are UNCHANGED so daily_recommend.py / trader.py callers are
# unaffected. NEVER raises. (_extract_date + the ISO/US date regexes below are reused by both the
# WSH parser and the ex-dividend parser.)
_ISO_DATE_RE = re.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})")
_US_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")


def _extract_date(text: Optional[str]) -> Optional[date]:
    """First calendar date found in `text` as a date, or None. Accepts YYYY-MM-DD / YYYY/MM/DD
    and US MM/DD/YYYY. Pure; never raises."""
    if not text:
        return None
    m = _ISO_DATE_RE.search(text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = _US_DATE_RE.search(text)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    return None


def days_to_earnings(ticker: str, today: Optional[date] = None,
                     horizon_days: int = 90) -> Optional[int]:
    """Integer days to the next earnings date; None if no upcoming date within `horizon_days`
    (or lookup fails). LAYERED source, same signature + int|None return contract as before:
      1. PRIMARY  -- Wall Street Horizon (WSH) via IBKR, prefetched async into the per-run cache
                     by gather()'s prefetch_wsh_events(); the sync read here just hits that cache.
      2. FALLBACK -- yfinance get_earnings_dates() (the original path), used whenever WSH had no
                     cached hit for this name (gateway down / not prefetched => the common case).
    Index/ETF underlyings have no earnings and are filtered by the caller. Cached per run.
    NEVER raises (callers rely on None-on-failure)."""
    sym = (ticker or "").upper()
    if not sym or sym in INDEX_UNDERLYINGS:
        return None
    if sym in _EARNINGS_DAYS_CACHE:
        return _EARNINGS_DAYS_CACHE[sym]
    ref = today or datetime.now(timezone.utc).date()
    result: Optional[int] = None

    # PRIMARY is the per-run WSH cache checked above (populated async by prefetch_wsh_events in
    # gather()); reaching here means WSH had no cached hit for this name.
    # FALLBACK: the original yfinance path, unchanged.
    if result is None:
        try:
            import yfinance as yf
            df = yf.Ticker(sym).get_earnings_dates(limit=12)
            future = []
            for idx in (df.index if df is not None else []):
                try:
                    d = idx.date()
                except Exception:
                    continue
                n = (d - ref).days
                if 0 <= n <= horizon_days:
                    future.append(n)
            if future:
                result = min(future)
        except Exception:
            result = None

    _EARNINGS_DAYS_CACHE[sym] = result
    return result


def _ex_div_days(raw, ref: date, horizon_days: int = 90) -> Optional[int]:
    """PURE helper (unit-tested; no I/O): integer days from `ref` to the next ex-dividend date
    parsed out of `raw`, or None. `raw` is whatever yfinance's calendar exposes for the
    "Ex-Dividend Date" field -- a date/datetime, a pandas Timestamp, a string, or a list of
    those. Only a future date within `horizon_days` (0 <= n <= horizon_days) is returned;
    past/unparseable/missing -> None. Never raises (the assignment gate must never blow up the
    trade path, and a fabricated ex-div date is worse than 'unknown')."""
    if raw is None:
        return None
    vals = raw if isinstance(raw, (list, tuple)) else [raw]
    best: Optional[int] = None
    for v in vals:
        d: Optional[date] = None
        try:
            if isinstance(v, datetime):
                d = v.date()
            elif isinstance(v, date):
                d = v
            elif hasattr(v, "date") and callable(getattr(v, "date")):
                d = v.date()          # pandas Timestamp / similar
            else:
                d = _extract_date(str(v))
        except Exception:
            d = None
        if d is None:
            continue
        n = (d - ref).days
        if 0 <= n <= horizon_days:
            best = n if best is None else min(best, n)
    return best


def days_to_ex_dividend(ticker: str, today: Optional[date] = None,
                        horizon_days: int = 90) -> Optional[int]:
    """Integer days to the next ex-dividend date; None if unknown / no upcoming date within
    `horizon_days` (or lookup fails). Mirrors days_to_earnings()'s int|None contract and is the
    input to construction.assignment_risk_ok (early-assignment / ex-div gate A6).

    Source: yfinance Ticker.calendar "Ex-Dividend Date" (free, keyless). Thin wrapper over the
    pure, unit-tested `_ex_div_days` core. Index/ETF underlyings (INDEX_UNDERLYINGS) have no
    single-name dividend event here and return None. Cached per run. NEVER raises (callers rely
    on None-on-failure). Never fabricates a date -- an unknown ex-div stays None so the caller
    fails open + flags 'unchecked'."""
    sym = (ticker or "").upper()
    if not sym or sym in INDEX_UNDERLYINGS:
        return None
    if sym in _EX_DIV_DAYS_CACHE:
        return _EX_DIV_DAYS_CACHE[sym]
    ref = today or datetime.now(timezone.utc).date()
    result: Optional[int] = None
    try:
        import yfinance as yf
        cal = yf.Ticker(sym).calendar
        raw = None
        if isinstance(cal, dict):
            raw = cal.get("Ex-Dividend Date")
        elif cal is not None:
            # older yfinance returns a DataFrame indexed by field name
            try:
                raw = cal.loc["Ex-Dividend Date"]
                raw = list(raw) if hasattr(raw, "__iter__") and not isinstance(raw, str) else raw
            except Exception:
                raw = None
        result = _ex_div_days(raw, ref, horizon_days)
    except Exception:
        result = None
    _EX_DIV_DAYS_CACHE[sym] = result
    return result


# ---------------------------------------------------------------- Wall Street Horizon (PRIMARY)
# WSH corporate-event data (earnings + ex-dividend dates) via the IBKR API. reqWshMetaData must be
# issued once before reqWshEventData; ib_async 2.x exposes the awaitable accessors as
# getWshMetaDataAsync() / getWshEventDataAsync(WshEventData), and reqWshEventData returns a JSON
# string shaped like a nested {"data": [ {event...}, ... ]} of typed corporate events. The parser
# below is deliberately TOLERANT -- it scans recursively for earnings-like / dividend-like event
# objects and ISO (or yyyymmdd) dates, mirroring the namespace/casing tolerance the prior
# XML-fundamentals parser used -- so minor WSH field/casing drift still resolves. Pure + unit-tested;
# the network wrapper (prefetch_wsh_events) is isolated so tests need no live connection.

# event-type / category keyword hints (case-insensitive substring match over an object's keys+values)
_WSH_EARN_HINTS = ("earning", "eps")                    # "Earnings", "Earnings Per Share (EPS)"
_WSH_EXDIV_HINTS = ("ex-div", "exdiv", "ex_div", "ex div", "ex-dividend", "exdividend", "ex dividend")
_WSH_DIV_HINTS = ("dividend", "distribution")           # a dividend event -> the ex-date is the input we want
# date-carrying field-name hints: prefer real event-date fields, skip fiscal/metadata date fields.
_WSH_DATE_KEY_GOOD = ("date", "announce", "report", "event", "earn", "exdiv", "ex_date", "ex-date")
_WSH_DATE_KEY_BAD = ("fiscal", "period", "fyend", "fy_end", "modified", "updated", "created", "lastmod")
_WSH_EX_DATE_KEY = ("exdate", "ex_date", "ex-date", "exdividend", "exdiv")


def _wsh_yyyymmdd(v) -> Optional[date]:
    """A bare 20260731-style int/float (or its string form) -> date, else None. Never raises."""
    try:
        n = int(v)
    except Exception:
        return None
    if 19000101 <= n <= 99991231:
        try:
            return date(n // 10000, (n // 100) % 100, n % 100)
        except ValueError:
            return None
    return None


def _iter_wsh_objs(obj):
    """Yield every dict found anywhere in a parsed-JSON structure (recursive). Never raises."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _iter_wsh_objs(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _iter_wsh_objs(v)


def _wsh_obj_dates(obj: dict):
    """[(key_lower, date), ...] extractable from an object's scalar fields, preferring real
    event-date keys over neutral ones and skipping fiscal/metadata date keys. Never raises."""
    good, other = [], []
    for k, v in obj.items():
        kl = str(k).lower()
        if any(b in kl for b in _WSH_DATE_KEY_BAD):
            continue
        d = None
        if isinstance(v, str):
            d = _extract_date(v)
        if d is None and isinstance(v, (int, float)) and not isinstance(v, bool):
            d = _wsh_yyyymmdd(v)
        if d is None:
            continue
        (good if any(g in kl for g in _WSH_DATE_KEY_GOOD) else other).append((kl, d))
    return good or other


def _wsh_classify(obj: dict) -> Optional[str]:
    """'earnings' | 'exdiv' | None for a WSH event object, from its keys+scalar values. Ex-div /
    dividend wins over earnings when both hint-sets match (a dividend event is not an earnings)."""
    parts = [str(k).lower() for k in obj.keys()]
    parts += [str(v).lower() for v in obj.values()
              if isinstance(v, (str, int, float)) and not isinstance(v, bool)]
    hay = " ".join(parts)
    if any(h in hay for h in _WSH_EXDIV_HINTS) or any(h in hay for h in _WSH_DIV_HINTS):
        return "exdiv"
    if any(h in hay for h in _WSH_EARN_HINTS):
        return "earnings"
    return None


def _soonest_future(dates, ref: date, horizon_days: int) -> Optional[int]:
    """Fewest days from `ref` to a date in `dates` with 0 <= n <= horizon_days, else None."""
    best: Optional[int] = None
    for d in dates:
        n = (d - ref).days
        if 0 <= n <= horizon_days:
            best = n if best is None else min(best, n)
    return best


def _parse_wsh_events(payload_json: Optional[str], today: Optional[date] = None,
                      horizon_days: int = 90) -> Dict[str, Optional[int]]:
    """PURE parser (unit-tested; no I/O): from a raw WSH reqWshEventData JSON string, extract the
    integer days from `today` to the NEXT future earnings event and the NEXT future ex-dividend
    event, each within `horizon_days` (0 <= n <= horizon_days). Returns
    {"earnings_days": int|None, "ex_div_days": int|None}; past / malformed / empty / missing -> None
    for that field. Tolerant of WSH field/casing drift. Never raises (never fabricates a date)."""
    out: Dict[str, Optional[int]] = {"earnings_days": None, "ex_div_days": None}
    if not payload_json or not isinstance(payload_json, str):
        return out
    ref = today or datetime.now(timezone.utc).date()
    try:
        data = json.loads(payload_json)
    except Exception:
        return out
    earn_dates, exdiv_dates = [], []
    try:
        for obj in _iter_wsh_objs(data):
            if not isinstance(obj, dict):
                continue
            kind = _wsh_classify(obj)
            if kind is None:
                continue
            dated = _wsh_obj_dates(obj)
            if not dated:
                continue
            if kind == "exdiv":
                ex_specific = [d for (k, d) in dated if any(h in k for h in _WSH_EX_DATE_KEY)]
                exdiv_dates.extend(ex_specific or [d for (_k, d) in dated])
            else:
                earn_dates.extend(d for (_k, d) in dated)
    except Exception:
        return {"earnings_days": None, "ex_div_days": None}
    out["earnings_days"] = _soonest_future(earn_dates, ref, horizon_days)
    out["ex_div_days"] = _soonest_future(exdiv_dates, ref, horizon_days)
    return out


def _wsh_event_data_cls():
    """The backend's WshEventData request class (ib_async or ib_insync), or None if unavailable.
    Read off the ibkr shim's backend module so we don't add an import the shim doesn't re-export."""
    try:
        from exitmgr import ibkr as _ibkr
        return getattr(getattr(_ibkr, "_ib", None), "WshEventData", None)
    except Exception:
        return None


async def _wsh_meta(ib):
    """Issue reqWshMetaData once (required before event data). Adapts to either the ib_async
    getWshMetaDataAsync() or a reqWshMetaDataAsync() spelling. None if the handle exposes neither."""
    fn = getattr(ib, "getWshMetaDataAsync", None) or getattr(ib, "reqWshMetaDataAsync", None)
    if fn is None:
        return None
    return await fn()


async def _wsh_event(ib, data):
    """Fetch one contract's WSH event-data JSON. Adapts to getWshEventDataAsync / reqWshEventDataAsync."""
    fn = getattr(ib, "getWshEventDataAsync", None) or getattr(ib, "reqWshEventDataAsync", None)
    if fn is None:
        return None
    return await fn(data)


async def prefetch_wsh_events(ib, tickers, today: Optional[date] = None,
                              horizon_days: int = 90) -> None:
    """ASYNC prefetch of Wall Street Horizon earnings + ex-dividend events for the candidate single
    names, run from gather() (which already holds the live IB handle) BEFORE the sync days_to_*
    readers fire. This is the whole fix for the in-loop 'sync can't await' problem: the async WSH
    work happens HERE, and the sync readers just hit the caches. Issues reqWshMetaData once, then per
    name resolves the stock conId and requests + parses that name's WSH event JSON, populating both
    _EARNINGS_DAYS_CACHE and _EX_DIV_DAYS_CACHE. Only POSITIVE hits are cached; a miss leaves the name
    UNCACHED so the sync reader cleanly falls back to yfinance. NEVER raises; any per-name failure is
    swallowed and simply yields no cache entry for that name."""
    if ib is None:
        return
    names = [t.upper() for t in (tickers or []) if t and t.upper() not in INDEX_UNDERLYINGS]
    if not names:
        return
    ref = today or datetime.now(timezone.utc).date()
    try:
        await _wsh_meta(ib)   # required once before any event request; proceed regardless of content
    except Exception:
        return                 # WSH unusable this run -> every name falls back to yfinance
    WED = _wsh_event_data_cls()
    if WED is None:
        return
    try:
        from exitmgr.ibkr import Stock
    except Exception:
        return
    for sym in names:
        try:
            con_id = None
            try:
                q = await ib.qualifyContractsAsync(Stock(sym, "SMART", "USD"))
                con_id = next((getattr(c, "conId", None) for c in (q or [])
                               if getattr(c, "conId", None)), None)
            except Exception:
                con_id = None
            if not con_id:
                continue
            data = WED(conId=int(con_id), fillWatchlist=False,
                       fillPortfolio=False, fillCompetitors=False)
            payload = await _wsh_event(ib, data)
            parsed = _parse_wsh_events(payload, today=ref, horizon_days=horizon_days)
            if parsed.get("earnings_days") is not None:
                _EARNINGS_DAYS_CACHE[sym] = parsed["earnings_days"]
            if parsed.get("ex_div_days") is not None:
                _EX_DIV_DAYS_CACHE[sym] = parsed["ex_div_days"]
        except Exception:
            continue


def _earnings_field(ticker: str, today: Optional[date] = None) -> str:
    """The training-format earnings token: '{n}d to earnings' within 90 days else 'earn n/a'."""
    n = days_to_earnings(ticker, today=today)
    return f"{n}d to earnings" if n is not None else "earn n/a"


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
    today = datetime.now(timezone.utc).date()
    _EARNINGS_DAYS_CACHE.clear()  # fresh per-run days-to-earnings cache for the per-name brief line
    _EX_DIV_DAYS_CACHE.clear()    # fresh per-run days-to-ex-dividend cache (assignment-risk gate A6)
    # PRIMARY earnings + ex-div source: Wall Street Horizon via IBKR. Prefetched HERE (async, holding
    # the live IB handle, BEFORE the sync days_to_* readers run) so those in-loop sync readers just
    # read cache; a WSH miss per name leaves it uncached -> yfinance fallback. Fully fail-soft.
    price_stats, vix, earnings, fh_news, yf_news, web_news, mv, opt_flow, rag_snippets, _wsh = await asyncio.gather(
        _boxed(_price_structure(ib, symbols)),
        _boxed(_vix(ib)),
        _boxed(asyncio.to_thread(_earnings_sync, names)) if names else _none(),
        _boxed(asyncio.to_thread(_enrich.news_finnhub, symbols)),
        _boxed(asyncio.to_thread(_headlines_sync, symbols)),
        _boxed(asyncio.to_thread(_enrich.news_parallel, symbols)),
        _boxed(_enrich.movers(ib)),
        _boxed_long(_enrich.options_flow_ib(ib, names or symbols)),
        _boxed(asyncio.to_thread(rag_context_sync, symbols)) if RAG_ENABLED else _none(),
        _boxed(prefetch_wsh_events(ib, names, today)) if names else _none(),
    )
    return {
        "price_stats": price_stats,
        "vix": vix,
        "events": next_events(today, earnings or []),
        "headlines": (fh_news or []) or (yf_news or []),
        "web_news": web_news, "movers": mv, "options_flow": opt_flow,
        "rag_snippets": rag_snippets,
    }


async def _none():
    return None
