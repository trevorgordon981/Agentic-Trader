#!/usr/bin/env python3
"""Fetch fresh, Trevor-relevant news into the Alfred RAG news domain."""
from __future__ import annotations
import html, os, re, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

OUT_DIR = os.path.expanduser("~/rag-news-stage")
CONFIG = os.path.expanduser("~/exitmgr-app/config.yaml")
RETENTION_DAYS = 14
RAW_PER_QUERY = 25
KEEP_PER_QUERY = 8
MAX_AGE_DAYS = 21
UA = "Mozilla/5.0 (rag-news-fetch)"

TOPICS = [
    ("AI & LLMs", "artificial intelligence OR large language model"),
    ("Semiconductors", "semiconductor OR Nvidia OR Micron chips"),
    ("Space", "ASTS SpaceMobile OR Rocket Lab OR space launch"),
    ("Porsche", "Porsche Panamera"),
    ("Self-hosting & homelab", "self-hosting OR homelab OR Kubernetes"),
]

TICKER_NAMES = {
    "SPY": ["s&p 500", "s&p500", "spdr", "sp500"],
    "MU": ["micron"],
    "NBIS": ["nebius"],
    "ASTS": ["ast spacemobile", "spacemobile", "ast space"],
    "RKLB": ["rocket lab", "rocketlab"],
    "CRWV": ["coreweave"],
    "LITE": ["lumentum"],
    "LUMN": ["lumen"],
    "NOK": ["nokia"],
    "DRAM": ["roundhill memory", "memory etf", "dram"],
    "SOXL": ["direxion", "semiconductor bull", "semis 3x", "semiconductor 3x"],
    "TQQQ": ["proshares ultrapro qqq", "ultrapro qqq", "nasdaq-100", "nasdaq 100"],
    "UPRO": ["proshares ultrapro s&p", "ultrapro s&p", "s&p 500 bull", "s&p500 3x"],
    "URTY": ["proshares ultrapro russell", "russell 2000", "ultrapro russell"],
}

SOURCE_DENYLIST = [
    "moomoo", "fathom journal", "chartmill", "stock traders daily",
    "etf database", "etf trends", "tikr.com", "tikr", "quiver quantitative",
    "techstock", "mshale", "simplywall.st", "simply wall st", "stockstory",
    "eciks.org", "barchart", "trefis", "247 wall st", "24/7 wall st", "aol.com",
]

TITLE_DENY_PATTERNS = [
    r"\bstocks? to buy\b",
    r"\bbest\b.*\bstocks?\b.*\b(to )?buy\b",
    r"bet (his|her|their) house",
    r"\bmark your calendars?\b",
    r"horoscope|zodiac|astrolog",
    r"see u guys|happy weekend|red red red|next week",
    r"stock options chain|options chain \|",
    r"cert deposito|repr 0\.0",
    r"top performing .*etfs?:",
    r"\bstock chart\b|price and chart",
    r"\d+\s+leveraged.*etf plays",
    r"could investing.*make you (a )?millionaire",
    r"make you (a )?millionaire",
]
TITLE_DENY_PATTERNS.append(chr(92)+chr(40)+chr(92)+chr(119)+"{6,}"+chr(92)+chr(41)+chr(92)+"s*$")
_TITLE_DENY = [re.compile(p, re.I) for p in TITLE_DENY_PATTERNS]
_WORD_RE_CACHE = {}


def watchlist_tickers():
    try:
        text = open(CONFIG).read()
    except OSError:
        return []
    m = re.search(r"approved_names\s*:\s*\[([^\]]*)\]", text)
    if m:
        return [t.strip().strip("'\"") for t in m.group(1).split(",") if t.strip()]
    lines = text.splitlines()
    out = []
    for i, line in enumerate(lines):
        if re.match(r"\s*approved_names\s*:\s*$", line):
            base = len(line) - len(line.lstrip())
            for nxt in lines[i + 1:]:
                if not nxt.strip():
                    continue
                indent = len(nxt) - len(nxt.lstrip())
                stripped = nxt.strip()
                if not stripped.startswith("-") or indent < base:
                    break
                tok = stripped[1:].strip().strip("'\"")
                tok = tok.split("#", 1)[0].strip()
                if tok:
                    out.append(tok)
            break
    return out


def _word_re(token):
    pat = _WORD_RE_CACHE.get(token)
    if pat is None:
        pat = re.compile(r"(?<![A-Za-z0-9])" + re.escape(token) + r"(?![A-Za-z0-9])", re.I)
        _WORD_RE_CACHE[token] = pat
    return pat


def norm_title(title):
    # Google News titles end with " - Outlet"; strip it so cross-outlet
    # reprints of the same story dedupe together.
    base = re.sub(r"\s+-\s+[^-]+$", "", title)
    return re.sub(r"[^a-z0-9]+", " ", base.lower()).strip()


def parse_pub(pub):
    if not pub:
        return None
    try:
        dt = parsedate_to_datetime(pub)
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def source_denied(source):
    s = source.lower()
    return any(bad in s for bad in SOURCE_DENYLIST)


def title_denied(title):
    return any(p.search(title) for p in _TITLE_DENY)


def ticker_relevant(ticker, title):
    if _word_re(ticker).search(title):
        return True
    for kw in TICKER_NAMES.get(ticker, []):
        if kw in title.lower():
            return True
    return False


def fetch_rss(query):
    url = ("https://news.google.com/rss/search?q="
           + urllib.parse.quote(query)
           + "&hl=en-US&gl=US&ceid=US:en")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            xml = r.read().decode("utf-8", "replace")
    except Exception:
        return []
    items = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = (it.findtext("pubDate") or "").strip()
        src_el = it.find("source")
        source = (src_el.text or "").strip() if src_el is not None else ""
        if not title:
            continue
        items.append({"title": html.unescape(title), "link": link,
                      "pub": pub, "source": source})
        if len(items) >= RAW_PER_QUERY:
            break
    return items


def filtered_items(items, ticker, now, seen):
    cutoff = now - timedelta(days=MAX_AGE_DAYS)
    kept = []
    for it in items:
        title = it["title"]
        if source_denied(it["source"]):
            continue
        if title_denied(title):
            continue
        dt = parse_pub(it["pub"])
        if dt is None or dt < cutoff:
            continue
        if ticker is not None and not ticker_relevant(ticker, title):
            continue
        key = norm_title(title)
        if not key or key in seen:
            continue
        seen.add(key)
        kept.append(it)
        if len(kept) >= KEEP_PER_QUERY:
            break
    return kept


def section(heading, items):
    if not items:
        return []
    out = ["## " + heading, ""]
    for it in items:
        meta = " · ".join(x for x in (it["source"], it["pub"][:16]) if x)
        out.append("- " + it["title"] + ("  \n  _" + meta + "_" if meta else ""))
        if it["link"]:
            out.append("  " + it["link"])
    out.append("")
    return out


def prune_old(today):
    cutoff = today - timedelta(days=RETENTION_DAYS)
    for fn in os.listdir(OUT_DIR):
        m = re.match(r"news-(\d{4}-\d{2}-\d{2})\.md$", fn)
        if not m:
            continue
        try:
            d = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue
        if d < cutoff:
            os.remove(os.path.join(OUT_DIR, fn))


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    today = date.today()
    now = datetime.now(timezone.utc)
    seen = set()
    lines = [
        "---", "date: " + today.isoformat(), "type: news-digest",
        "source: google-news-rss", "---", "",
        "# News digest " + today.isoformat(), "",
        "Fresh headlines for Trevor watchlist tickers and the topics he follows.",
        "",
    ]
    tickers = watchlist_tickers()
    if tickers:
        watch_lines = []
        for t in tickers:
            time.sleep(0.5)
            raw = fetch_rss(t + " stock")
            kept = filtered_items(raw, t, now, seen)
            watch_lines += section(t, kept)
        if watch_lines:
            lines += ["# Watchlist", ""] + watch_lines
    topic_lines = []
    for label, query in TOPICS:
        time.sleep(0.5)
        raw = fetch_rss(query)
        kept = filtered_items(raw, None, now, seen)
        topic_lines += section(label, kept)
    if topic_lines:
        lines += ["# Topics", ""] + topic_lines

    path = os.path.join(OUT_DIR, "news-" + today.isoformat() + ".md")
    with open(path, "w") as fh:
        fh.write("\n".join(lines))
    prune_old(today)
    remaining = sorted(fn for fn in os.listdir(OUT_DIR) if fn.startswith("news-"))
    print("Wrote " + path + "; rolling window now " + str(len(remaining))
          + " day(s): " + remaining[0] + ".." + remaining[-1])


if __name__ == "__main__":
    main()
