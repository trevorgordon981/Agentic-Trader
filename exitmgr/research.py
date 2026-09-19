"""Public API contract; production-derived narrative omitted."""
import asyncio
import json
import math
import os
import urllib.request
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from exitmgr.risk import INDEX_UNDERLYINGS
from exitmgr import enrichment as _enrich




_IB_CALL_TIMEOUT_S = 30


SECTION_TIMEOUT_S = 20



PRICE_STRUCTURE_BUDGET_S = max(1.0, SECTION_TIMEOUT_S - 3.0)
HEADLINES_PER_SYMBOL = 3
MAX_HEADLINES = 12




_PRICE_STRUCTURE_CACHE_DAY: Optional[date] = None
_PRICE_STRUCTURE_CACHE: Dict[str, dict] = {}


def _price_structure_trading_day() -> date:
    """Public API contract; production-derived narrative omitted."""
    return datetime.now(ZoneInfo("America/New_York")).date()






RAG_ENABLED = os.environ.get("STRATEGIST_RAG_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")
RAG_ENDPOINT = os.environ.get("ALFRED_RAG_ENDPOINT", "http://localhost:9000/search")
RAG_TIMEOUT_S = float(os.environ.get("STRATEGIST_RAG_TIMEOUT_S", "8"))
RAG_TOP_K = int(os.environ.get("STRATEGIST_RAG_TOP_K", "4"))
RAG_SNIPPET_CHARS = 320


def _rag_query_sync(query: str, top_k: int = RAG_TOP_K) -> List[str]:
    """Public API contract; production-derived narrative omitted."""
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
    """Public API contract; production-derived narrative omitted."""
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



FOMC_DECISIONS_2026 = [
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
]




def _ann_vol_20(closes: List[float], end: int) -> Optional[float]:
    """Public API contract; production-derived narrative omitted."""
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
    """Public API contract; production-derived narrative omitted."""
    closes = [c for c in closes if c is not None and c == c and c > 0]
    if len(closes) < 26:
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
    """Public API contract; production-derived narrative omitted."""
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
    """Public API contract; production-derived narrative omitted."""
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


def _quote_snapshot_label(market_data_type=3):
    """Public API contract; production-derived narrative omitted."""
    try:
        t = int(market_data_type)
    except Exception:
        t = 3
    if t == 1:
        return "REAL-TIME quote snapshot (live subscription; symbol: last, day change):"
    if t == 2:
        return "Frozen quote snapshot (last close of the prior session; symbol: last, day change):"
    if t == 4:
        return "Delayed-frozen quote snapshot (~15min lag, frozen; symbol: last, day change):"
    return "Delayed quote snapshot (~15min lag; symbol: last, day change):"


def next_events(today: date, earnings: Optional[List[tuple]] = None,
                fomc_dates: Optional[List[str]] = None, horizon_days: int = 45) -> List[str]:
    """Public API contract; production-derived narrative omitted."""
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
    """Public API contract; production-derived narrative omitted."""
    hay = " ".join(x for x in (industry, sector) if x).lower()
    return any(k.strip().lower() in hay for k in keywords if k.strip())


def sector_of(ticker: str):
    """Public API contract; production-derived narrative omitted."""
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info or {}
        return info.get("industry"), info.get("sector")
    except Exception:
        return None, None


def parse_rss_titles(xml_text: str, limit: int = HEADLINES_PER_SYMBOL) -> List[str]:
    """Public API contract; production-derived narrative omitted."""
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


_ACCOUNT_SIZING_HEADER = "Account sizing snapshot (live; execution revalidates before any order):"
_ACCOUNT_SIZING_FOOTER = "End account sizing snapshot."


def account_sizing_snapshot(*, net_liq: Optional[float],
                            available_funds: Optional[float],
                            max_premium_pct: float = 0.25) -> str:
    """Public API contract; production-derived narrative omitted."""
    try:
        nl = float(net_liq)
        af = float(available_funds)
    except (TypeError, ValueError):
        nl = af = float("nan")
    if not (math.isfinite(nl) and nl > 0 and math.isfinite(af) and af >= 0):
        body = "  UNAVAILABLE OR INVALID — do not propose a new trade."
    else:




        try:
            cap = float(max_premium_pct) * nl
        except (TypeError, ValueError):
            cap = 0.25 * nl
        body = (f"  Net liquidation value: ${nl:,.2f}\n"
                f"  Available funds: ${af:,.2f}\n"
                f"  Max debit per trade: ${cap:,.0f}  <-- a proposal whose spread cannot be "
                f"built for less than this is unusable; screen the name on this BEFORE proposing")
    return f"{_ACCOUNT_SIZING_HEADER}\n{body}\n{_ACCOUNT_SIZING_FOOTER}"


def with_account_sizing_snapshot(brief: str, *, net_liq: Optional[float],
                                 available_funds: Optional[float],
                                 max_premium_pct: float = 0.10) -> str:
    """Public API contract; production-derived narrative omitted."""





    fresh = account_sizing_snapshot(net_liq=net_liq, available_funds=available_funds,
                                    max_premium_pct=max_premium_pct)
    text = str(brief or "")
    start = text.find(_ACCOUNT_SIZING_HEADER)
    if start >= 0:
        end = text.find(_ACCOUNT_SIZING_FOOTER, start)
        if end >= 0:
            end += len(_ACCOUNT_SIZING_FOOTER)
            return text[:start] + fresh + text[end:]
    first, sep, rest = text.partition("\n")
    return f"{first}\n{fresh}{sep}{rest}" if first else fresh


def build_brief(*, today: str, quotes: Dict[str, dict], universe: List[str],
                allow_any_name: bool, price_stats: Optional[Dict[str, Optional[dict]]] = None,
                vix: Optional[float] = None, events: Optional[List[str]] = None,
                headlines: Optional[List[str]] = None, book: Optional[list] = None,
                day_pnl_pct: Optional[float] = None,
                movers: Optional[dict] = None, options_flow: Optional[List[str]] = None,
                web_news: Optional[List[str]] = None,
                rag_snippets: Optional[List[str]] = None,
                opt_iv: Optional[Dict[str, float]] = None,
                net_liq: float,
                available_funds: float, book_detail: Optional[dict] = None,
                market_data_type: int = 3,
                max_premium_pct: float = 0.10,
                setup_watchlist_context: Optional[str] = None) -> str:
    """Public API contract; production-derived narrative omitted."""

    try:
        _today_date = datetime.strptime(today, "%Y-%m-%d").date()
    except Exception:
        _today_date = datetime.now(timezone.utc).date()
    lines = [f"Date (UTC): {today}",
             account_sizing_snapshot(net_liq=net_liq, available_funds=available_funds,
                                     max_premium_pct=max_premium_pct),
             _quote_snapshot_label(market_data_type)]
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





    if setup_watchlist_context:
        lines.extend(str(setup_watchlist_context).splitlines())

    lines.append("Current book:")
    if book:
        for p in book:
            kind = "index" if getattr(p, "is_index", False) else "single name"
            sym = str(getattr(p, "underlying", "?"))
            base = f"  {sym}: ~${getattr(p, 'notional', 0):,.0f} at risk ({kind})"






            detail_book = book_detail or {}
            contract_id = getattr(p, "primary_con_id", None)
            if isinstance(detail_book.get("contracts"), dict):
                d = detail_book["contracts"].get(str(contract_id)) or {}
            else:

                d = detail_book.get(sym) or {}
            bits = []
            if contract_id:
                bits.append(f"contract {int(contract_id)}")
            campaign_seq = d.get("campaign_seq")
            if isinstance(campaign_seq, int) and campaign_seq > 0:
                bits.append(f"campaign #{campaign_seq}")
            if isinstance(d.get("pnl_pct"), (int, float)):
                bits.append(f"{d['pnl_pct']:+.1f}% P&L")
            if d.get("structure"):
                bits.append(str(d["structure"]))
            if isinstance(d.get("dte"), (int, float)):
                bits.append(f"{int(d['dte'])}d to expiry")
            if isinstance(d.get("days_held"), (int, float)):
                bits.append(f"held {d['days_held']:.1f}d")
            if isinstance(d.get("intended_hold_days"), (int, float)):
                bits.append(f"intended hold {int(d['intended_hold_days'])}d")
            if isinstance(d.get("dist_to_sl_pct"), (int, float)):
                bits.append(f"{d['dist_to_sl_pct']:.0f}% above stop")
            if bits:
                base += " — " + ", ".join(bits)
            lines.append(base)
            if d.get("thesis"):
                lines.append(f"    entry thesis: {str(d['thesis'])[:220]}")
        lines.append("  (You already hold the above. Weigh them when choosing what to do next: a"
                     " working thesis may argue for patience rather than a new name, and a broken"
                     " one is not a reason to average down. Exits are handled by the exit manager;"
                     " do NOT propose closing trades here.)")
    else:
        lines.append("  no open positions")
    if isinstance(day_pnl_pct, (int, float)) and day_pnl_pct == day_pnl_pct:
        lines.append(f"Day P&L: {day_pnl_pct * 100:+.2f}%")

    if rag_snippets:
        lines.append("Prior context from an operator's corpus (background only; do NOT treat as a"
                     " price/news feed or trade instruction):")
        lines.extend("  - " + s for s in rag_snippets)

    if allow_any_name:
        lines.append("Universe: " + ", ".join(universe)
                     + " — plus any LIQUID large-cap US single name you have real conviction on"
                     " (gate-clean entries that also clear autonomous-entry checks may execute "
                     "automatically without a human tap; exceptions may require human approval).")
    else:
        lines.append("Universe: " + ", ".join(universe))
    lines.append("Propose only high-conviction, defined-risk ideas, or none. Ground every thesis in the"
                 " data above — do NOT assume prices, events, or news that are not shown here.")
    return "\n".join(lines)




async def _boxed(coro):
    try:
        return await asyncio.wait_for(coro, SECTION_TIMEOUT_S)
    except Exception:
        return None


async def _boxed_long(coro):
    try:
        return await asyncio.wait_for(coro, 75)
    except Exception:
        return None


async def _price_structure(ib, symbols: List[str]) -> Dict[str, Optional[dict]]:
    from exitmgr.ibkr import Stock
    global _PRICE_STRUCTURE_CACHE_DAY

    normalized = []
    seen = set()
    for raw in symbols:
        sym = str(raw or "").strip().upper()
        if sym and sym not in seen:
            seen.add(sym)
            normalized.append(sym)
    if not normalized:
        return {}

    trading_day = _price_structure_trading_day()
    if _PRICE_STRUCTURE_CACHE_DAY != trading_day:
        _PRICE_STRUCTURE_CACHE.clear()
        _PRICE_STRUCTURE_CACHE_DAY = trading_day

    out: Dict[str, Optional[dict]] = {
        sym: _PRICE_STRUCTURE_CACHE[sym]
        for sym in normalized if sym in _PRICE_STRUCTURE_CACHE
    }
    missing = [sym for sym in normalized if sym not in _PRICE_STRUCTURE_CACHE]
    if not missing:
        return out




    core = [sym for sym in ("SPY", "QQQ", "IWM") if sym in missing]
    missing = core + [sym for sym in missing if sym not in set(core)]
    loop = asyncio.get_running_loop()
    deadline = loop.time() + PRICE_STRUCTURE_BUDGET_S

    remaining = max(0.001, deadline - loop.time())
    qc = await asyncio.wait_for(
        ib.qualifyContractsAsync(*[Stock(s, "SMART", "USD") for s in missing]),
        min(_IB_CALL_TIMEOUT_S, remaining),
    )
    contracts = [c for c in qc if getattr(c, "conId", None)]
    if not contracts:
        return out
    sem = asyncio.Semaphore(8)

    async def _one(c):
        async with sem:
            bars = await asyncio.wait_for(ib.reqHistoricalDataAsync(
                c, endDateTime="", durationStr="1 Y", barSizeSetting="1 day",
                whatToShow="TRADES", useRTH=True, formatDate=1), _IB_CALL_TIMEOUT_S)
            return momentum_stats([b.close for b in bars])

    tasks = {asyncio.create_task(_one(c)): c for c in contracts}
    remaining = max(0.0, deadline - loop.time())
    done, pending = await asyncio.wait(tasks, timeout=remaining)
    for task in done:
        contract = tasks[task]
        try:
            stats = task.result()
        except Exception:
            continue
        if stats:
            sym = str(contract.symbol).upper()
            _PRICE_STRUCTURE_CACHE[sym] = stats
            out[sym] = stats
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    return out


async def _vix(ib) -> Optional[float]:
    """Public API contract; production-derived narrative omitted."""
    from exitmgr.ibkr import Index
    from exitmgr.market import usable_price
    if Index is None:
        return None
    qc = await asyncio.wait_for(ib.qualifyContractsAsync(Index("VIX", "CBOE")), _IB_CALL_TIMEOUT_S)
    if not qc or not getattr(qc[0], "conId", None):
        return None
    contract = qc[0]
    ticker = ib.reqMktData(contract, "", False, False)
    try:
        for _ in range(30):
            await asyncio.sleep(0.1)
            for px in (ticker.last, ticker.close):
                if usable_price(px):
                    return float(px)
    finally:
        try:
            ib.cancelMktData(contract)
        except Exception:
            pass
    try:
        bars = await asyncio.wait_for(ib.reqHistoricalDataAsync(
            contract, endDateTime="", durationStr="2 D", barSizeSetting="1 day",
            whatToShow="TRADES", useRTH=True, formatDate=1), _IB_CALL_TIMEOUT_S)
        if bars and usable_price(bars[-1].close):
            return float(bars[-1].close)
    except Exception:
        pass
    return None


def _earnings_sync(symbols: List[str]) -> List[tuple]:
    import yfinance as yf
    out = []
    for s in symbols:
        try:
            cal = yf.Ticker(earnings_reference_symbol(s)).calendar
            dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
            if dates:
                out.append((s, str(dates[0])))
        except Exception:
            continue
    return out






_EARNINGS_DAYS_CACHE: Dict[str, Optional[int]] = {}




EARNINGS_PROXY_SYMBOLS = {
    "NVDX": "NVDA",
}


def earnings_reference_symbol(ticker: str) -> str:
    """Public API contract; production-derived narrative omitted."""
    sym = (ticker or "").strip().upper()
    return EARNINGS_PROXY_SYMBOLS.get(sym, sym)



_EX_DIV_DAYS_CACHE: Dict[str, Optional[int]] = {}










_ISO_DATE_RE = re.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})")
_US_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")


def _extract_date(text: Optional[str]) -> Optional[date]:
    """Public API contract; production-derived narrative omitted."""
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


def next_earnings_days(ticker: str, today: Optional[date] = None,
                       force_refresh: bool = False) -> Optional[int]:
    """Public API contract; production-derived narrative omitted."""
    requested_sym = (ticker or "").strip().upper()
    sym = earnings_reference_symbol(requested_sym)
    if not sym or requested_sym in INDEX_UNDERLYINGS:
        return None
    cached_result = _EARNINGS_DAYS_CACHE.get(sym)
    if not force_refresh and sym in _EARNINGS_DAYS_CACHE:
        return _EARNINGS_DAYS_CACHE[sym]
    ref = today or datetime.now(timezone.utc).date()
    result: Optional[int] = None
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
            if n >= 0:
                future.append(n)
        if future:
            result = min(future)
    except Exception:
        result = None

    if result is not None:
        _EARNINGS_DAYS_CACHE[sym] = result
        return result



    return cached_result if force_refresh else None


def days_to_earnings(ticker: str, today: Optional[date] = None,
                     horizon_days: Optional[int] = None,
                     force_refresh: bool = False) -> Optional[int]:
    """Public API contract; production-derived narrative omitted."""
    result = next_earnings_days(ticker, today=today, force_refresh=force_refresh)
    if result is None:
        return None
    if horizon_days is not None and result > int(horizon_days):
        return None
    return result


def _ex_div_days(raw, ref: date, horizon_days: int = 90) -> Optional[int]:
    """Public API contract; production-derived narrative omitted."""
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
                d = v.date()
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
    """Public API contract; production-derived narrative omitted."""
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













_WSH_EARN_HINTS = ("earning", "eps")
_WSH_EXDIV_HINTS = ("ex-div", "exdiv", "ex_div", "ex div", "ex-dividend", "exdividend", "ex dividend")
_WSH_DIV_HINTS = ("dividend", "distribution")

_WSH_DATE_KEY_GOOD = ("date", "announce", "report", "event", "earn", "exdiv", "ex_date", "ex-date")
_WSH_DATE_KEY_BAD = ("fiscal", "period", "fyend", "fy_end", "modified", "updated", "created", "lastmod")
_WSH_EX_DATE_KEY = ("exdate", "ex_date", "ex-date", "exdividend", "exdiv")


def _wsh_yyyymmdd(v) -> Optional[date]:
    """Public API contract; production-derived narrative omitted."""
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
    """Public API contract; production-derived narrative omitted."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _iter_wsh_objs(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _iter_wsh_objs(v)


def _wsh_obj_dates(obj: dict):
    """Public API contract; production-derived narrative omitted."""
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
    """Public API contract; production-derived narrative omitted."""
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
    """Public API contract; production-derived narrative omitted."""
    best: Optional[int] = None
    for d in dates:
        n = (d - ref).days
        if 0 <= n <= horizon_days:
            best = n if best is None else min(best, n)
    return best


def _soonest_nonnegative(dates, ref: date) -> Optional[int]:
    """Public API contract; production-derived narrative omitted."""
    future = [(d - ref).days for d in dates if (d - ref).days >= 0]
    return min(future) if future else None


def _parse_wsh_events(payload_json: Optional[str], today: Optional[date] = None,
                      horizon_days: int = 90) -> Dict[str, Optional[int]]:
    """Public API contract; production-derived narrative omitted."""
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
    out["earnings_days"] = _soonest_nonnegative(earn_dates, ref)
    out["ex_div_days"] = _soonest_future(exdiv_dates, ref, horizon_days)
    return out


def _wsh_event_data_cls():
    """Public API contract; production-derived narrative omitted."""
    try:
        from exitmgr import ibkr as _ibkr
        return getattr(getattr(_ibkr, "_ib", None), "WshEventData", None)
    except Exception:
        return None


async def _wsh_meta(ib):
    """Public API contract; production-derived narrative omitted."""
    fn = getattr(ib, "getWshMetaDataAsync", None) or getattr(ib, "reqWshMetaDataAsync", None)
    if fn is None:
        return None
    return await fn()


async def _wsh_event(ib, data):
    """Public API contract; production-derived narrative omitted."""
    fn = getattr(ib, "getWshEventDataAsync", None) or getattr(ib, "reqWshEventDataAsync", None)
    if fn is None:
        return None
    return await fn(data)


async def prefetch_wsh_events(ib, tickers, today: Optional[date] = None,
                              horizon_days: int = 90) -> None:
    """Public API contract; production-derived narrative omitted."""
    if ib is None:
        return
    requested_names = {
        t.upper() for t in (tickers or []) if t and t.upper() not in INDEX_UNDERLYINGS
    }

    names = sorted(requested_names | {earnings_reference_symbol(t) for t in requested_names})
    if not names:
        return
    ref = today or datetime.now(timezone.utc).date()
    try:
        await _wsh_meta(ib)
    except Exception:
        return
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
                q = await asyncio.wait_for(ib.qualifyContractsAsync(Stock(sym, "SMART", "USD")), _IB_CALL_TIMEOUT_S)
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
            if (parsed.get("earnings_days") is not None
                    and sym not in EARNINGS_PROXY_SYMBOLS):
                _EARNINGS_DAYS_CACHE[sym] = parsed["earnings_days"]
            if parsed.get("ex_div_days") is not None:
                _EX_DIV_DAYS_CACHE[sym] = parsed["ex_div_days"]
        except Exception:
            continue


def _earnings_field(ticker: str, today: Optional[date] = None) -> str:
    """Public API contract; production-derived narrative omitted."""
    n = days_to_earnings(ticker, today=today, horizon_days=90)
    if n == 0:
        return ("EARNINGS TODAY — SAME-DAY BINARY EVENT WARNING; release timing/status is "
                "unverified from the date alone, so treat it as UPCOMING unless this brief "
                "contains authoritative evidence of a reported timestamp before this decision. "
                "Pre-release: underwrite an overnight gap beyond ordinary risk controls, the "
                "structure-specific downside, and post-print IV repricing. "
                "Post-release: reassess the actual reaction, fresh option IV/NBBO, and stabilization")
    return f"{n}d to earnings" if n is not None else "earn n/a"


def _symbol_headlines_sync(sym: str) -> List[str]:

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

    try:
        url = f"https://news.google.com/rss/search?q={sym}+stock&SYMO=en-US&gl=US&ceid=US:en"
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
    """Public API contract; production-derived narrative omitted."""
    names = sorted({s.upper() for s in (single_names or []) if s.upper() not in INDEX_UNDERLYINGS})
    today = datetime.now(timezone.utc).date()
    _EARNINGS_DAYS_CACHE.clear()
    _EX_DIV_DAYS_CACHE.clear()



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
