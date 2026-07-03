"""Live-market enrichment for the strategist brief — added 2026-06-18.

Four grounded, fail-soft sources on top of the base brief:
  * market movers          -> IBKR scanner (free): filtered top gainers/losers/most-active
  * options flow / makers  -> marketdata.app: P/C ratio, unusual volume vs OI, ATM IV per name
  * clean news headlines   -> Finnhub company-news (real sources)
  * web research           -> Parallel.ai search (citation-aware excerpts)

PAID sources (marketdata.app, Parallel.ai) are TTL-cached to a file so the daily slate and the
15-min trader loop share one fetch and we don't re-pay every cycle (ENRICH_CACHE_TTL_S, default
30 min). Everything degrades to an empty section on any error — never blocks a trading cycle.
"""
import hashlib, json, os, time, urllib.request
from datetime import date, timedelta
from typing import List, Optional

CACHE_DIR = os.path.expanduser("~/.cache/exitmgr-research")
TTL = int(os.environ.get("ENRICH_CACHE_TTL_S", "1800"))
MARKETDATA_TOKEN = os.environ.get("MARKETDATA_TOKEN", "")
FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY", "")
PARALLEL_API_KEY = os.environ.get("PARALLEL_API_KEY", "")
PARALLEL_MODE = os.environ.get("ENRICH_PARALLEL_MODE", "basic")  # turbo=cheapest, basic=balanced, advanced=best   # turbo<base<advanced in cost/quality

def _cached(key, ttl, producer):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        p = os.path.join(CACHE_DIR, hashlib.md5(key.encode()).hexdigest() + ".json")
        if os.path.exists(p) and time.time() - os.path.getmtime(p) < ttl:
            with open(p) as f:
                return json.load(f)
        val = producer()
        with open(p, "w") as f:
            json.dump(val, f)
        return val
    except Exception:
        try:
            return producer()
        except Exception:
            return []

# ---------- market movers (IBKR scanner, free) ----------
async def movers(ib, want=6) -> Optional[dict]:
    from ib_async import ScannerSubscription, TagValue
    filt = [TagValue("priceAbove", "20"), TagValue("volumeAbove", "3000000")]
    out = {}
    for label, code in (("gainers", "TOP_PERC_GAIN"), ("losers", "TOP_PERC_LOSE"), ("active", "MOST_ACTIVE")):
        try:
            sub = ScannerSubscription(instrument="STK", locationCode="STK.US.MAJOR", scanCode=code)
            data = await ib.reqScannerDataAsync(sub, [], filt)
            out[label] = [d.contractDetails.contract.symbol for d in data[:want]]
        except Exception:
            out[label] = []
    return out if any(out.values()) else None

def format_movers(m: Optional[dict]) -> List[str]:
    if not m:
        return []
    rows = []
    if m.get("gainers"): rows.append("top % gainers: " + ", ".join(m["gainers"]))
    if m.get("losers"):  rows.append("top % losers: " + ", ".join(m["losers"]))
    if m.get("active"):  rows.append("most active: " + ", ".join(m["active"]))
    return rows

# ---------- options flow / "what the makers are doing" (marketdata.app) ----------
def _opt_one(sym):
    if not MARKETDATA_TOKEN:
        return None
    to = (date.today() + timedelta(days=21)).isoformat()   # near-term only + strikeLimit -> bounds credit spend
    url = (f"https://api.marketdata.app/v1/options/chain/{sym}/?token={MARKETDATA_TOKEN}"
           f"&to={to}&strikeLimit=10&columns=side,volume,openInterest,iv,delta")
    try:
        with urllib.request.urlopen(url, timeout=12) as r:
            d = json.load(r)
    except Exception:
        return None
    if "side" not in d:   # columns= responses omit the top-level "s" status field
        return None
    sides, vols, ois = d.get("side", []), d.get("volume", []), d.get("openInterest", [])
    ivs, deltas = d.get("iv", []), d.get("delta", [])
    cv = sum((vols[i] or 0) for i, s in enumerate(sides) if s == "call")
    pv = sum((vols[i] or 0) for i, s in enumerate(sides) if s == "put")
    toi = sum((o or 0) for o in ois)
    tv = cv + pv
    atm_iv, best = None, 9.0
    for i in range(min(len(ivs), len(deltas))):
        if ivs[i] is None or deltas[i] is None:
            continue
        if abs(abs(deltas[i]) - 0.5) < best:
            best, atm_iv = abs(abs(deltas[i]) - 0.5), ivs[i]
    parts = []
    if cv:
        pcr = pv / cv
        tag = " call-heavy/bullish" if pcr < 0.7 else " put-heavy/bearish" if pcr > 1.3 else ""
        parts.append(f"P/C {pcr:.2f}{tag}")
    if tv and toi and tv > 0.8 * toi:
        parts.append(f"unusual vol {tv:,}/{toi:,} OI")
    if atm_iv:
        parts.append(f"ATM IV {atm_iv*100:.0f}%")
    return f"{sym}: " + "; ".join(parts) if parts else None

def options_flow(names, limit=5) -> List[str]:
    names = [n for n in names][:limit]
    if not names:
        return []
    return _cached("optflow:" + ",".join(sorted(names)), TTL,
                   lambda: [ln for ln in (_opt_one(s) for s in names) if ln])

# ---------- clean headlines (Finnhub company-news) ----------
def _fh_one(sym):
    if not FINNHUB_KEY:
        return []
    frm = (date.today() - timedelta(days=4)).isoformat()
    url = f"https://finnhub.io/api/v1/company-news?symbol={sym}&from={frm}&to={date.today().isoformat()}&token={FINNHUB_KEY}"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            arr = json.load(r)
        return [f"[{sym}] {a['headline']} — {a.get('source','')}" for a in arr[:3] if a.get("headline")]
    except Exception:
        return []

def news_finnhub(symbols, limit=6, cap=12) -> List[str]:
    syms = symbols[:limit]
    def produce():
        seen, out = set(), []
        for s in syms:
            for h in _fh_one(s):
                if h not in seen:
                    seen.add(h); out.append(h)
        return out[:cap]
    return _cached("fhnews:" + ",".join(syms), min(TTL, 900), produce)

# ---------- web research (Parallel.ai) ----------
def news_parallel(symbols, limit_syms=6, max_items=8) -> List[str]:
    if not PARALLEL_API_KEY:
        return []
    syms = symbols[:limit_syms]
    def produce():
        try:
            from parallel import Parallel
            c = Parallel(api_key=PARALLEL_API_KEY)
            obj = ("Latest market-moving news, catalysts, analyst rating changes, and notable price "
                   "moves today for: " + ", ".join(syms) + ", and the broad US stock market.")
            qs = [f"{s} stock news today" for s in syms[:3]] + ["US stock market movers today",
                  "semiconductor and AI stocks news today"]
            r = c.search(objective=obj, search_queries=qs[:5], mode=PARALLEL_MODE, max_chars_total=6000)
            out = []
            for res in (getattr(r, "results", None) or []):
                title = (getattr(res, "title", "") or "").strip()
                dt = getattr(res, "publish_date", "") or ""
                ex = getattr(res, "excerpts", None) or []
                snippet = ""
                if ex:
                    snippet = " ".join(ex[0].split())[:240]
                if title:
                    out.append(f"{title}" + (f" ({dt})" if dt else "") + (f" — {snippet}" if snippet else ""))
            return out[:max_items]
        except Exception:
            return []
    return _cached(f"pnews:{PARALLEL_MODE}:" + ",".join(syms), TTL, produce)


# ---------- options flow via IBKR OPRA (free, no daily cap) — preferred over marketdata.app ----------
def _cache_read(key, ttl):
    try:
        p = os.path.join(CACHE_DIR, hashlib.md5(key.encode()).hexdigest() + ".json")
        if os.path.exists(p) and time.time() - os.path.getmtime(p) < ttl:
            with open(p) as f:
                return json.load(f)
    except Exception:
        pass
    return None

def _cache_write(key, val):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(os.path.join(CACHE_DIR, hashlib.md5(key.encode()).hexdigest() + ".json"), "w") as f:
            json.dump(val, f)
    except Exception:
        pass

async def _opt_one_ib(ib, sym):
    import datetime as _dt, statistics as _st
    from exitmgr.ibkr import Option, Stock, pick_chain, strikes_near, underlying_price
    stk = (await ib.qualifyContractsAsync(Stock(sym, "SMART", "USD")))[0]
    params = await ib.reqSecDefOptParamsAsync(sym, "", "STK", stk.conId)
    p = pick_chain(params, sym)
    if not p or not p.expirations or not p.strikes:
        return None
    spot = await underlying_price(ib, stk)
    today = _dt.date.today()
    exps = sorted(p.expirations)
    exp = next((e for e in exps if (_dt.datetime.strptime(e, "%Y%m%d").date() - today).days >= 7), exps[0])
    ks = strikes_near(p.strikes, spot, per_side=5)
    conts = [Option(sym, exp, k, r, "SMART") for k in ks for r in ("C", "P")]
    q = [c for c in await ib.qualifyContractsAsync(*conts) if getattr(c, "conId", None)]
    if not q:
        return None
    tks = await ib.reqTickersAsync(*q)
    cv = pv = 0.0; ivs = []
    for t in tks:
        v = t.volume if (t.volume and t.volume == t.volume and t.volume > 0) else 0
        if t.contract.right == "C":
            cv += v
        else:
            pv += v
        g = t.modelGreeks or t.lastGreeks
        if g and g.impliedVol and g.delta is not None and 0.3 < abs(g.delta) < 0.7:
            ivs.append(g.impliedVol)
    parts = []
    if cv:
        pcr = pv / cv
        tag = " call-heavy/bullish" if pcr < 0.7 else " put-heavy/bearish" if pcr > 1.3 else ""
        parts.append(f"P/C {pcr:.2f}{tag}")
    if cv + pv > 0:
        parts.append(f"{exp[4:6]}/{exp[6:]} opt vol {int(cv+pv):,}")
    if ivs:
        parts.append(f"ATM IV {_st.median(ivs)*100:.0f}%")
    return f"{sym}: " + "; ".join(parts) if parts else None

async def options_flow_ib(ib, names, limit=3):
    names = [n for n in (names or [])][:limit]
    if not names:
        return []
    key = "optflow_ib:" + ",".join(sorted(names))
    c = _cache_read(key, TTL)
    if c is not None:
        return c
    out = []
    for s in names:
        try:
            ln = await _opt_one_ib(ib, s)
            if ln:
                out.append(ln)
        except Exception:
            continue
    _cache_write(key, out)
    return out
