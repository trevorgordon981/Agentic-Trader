"""ApeWisdom name discovery for the morning trader pipeline.

Only the company identity, current rank, and ApeWisdom's normalized 30-day
mention curve leave this module.  They are attention context, never direction,
conviction, sizing, or order authority in the main trade-proposal request.
"""

from __future__ import annotations

import html
import json
import math
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable


API_TEMPLATE = "https://apewisdom.io/api/v1.0/filter/{filter_name}/page/1"
TABLE_URLS = {
    "all-stocks": "https://apewisdom.io/",
    "stocks": "https://apewisdom.io/stocks/",
}
SUPPORTED_FILTERS = ("all-stocks", "stocks")
DEFAULT_FILTER = "all-stocks"
DEFAULT_SOURCE_LIMIT = 25
MAX_SOURCE_LIMIT = 100
TIMEOUT_SECONDS = 10
MAX_RESPONSE_BYTES = 2_000_000
CACHE_TTL_SECONDS = 30 * 60
MAX_STALE_SECONDS = 90 * 60
# Source-eligibility floors. 2026-07-26: the previous hardcoded $20 / 3M values
# were written for a much larger account and excluded essentially every name this
# book can actually buy. At ~$1,893 with a 25% per-trade cap and a 365-DTE floor,
# affordability is governed by spot x vol (a 1-year 0.60-delta call costs roughly
# 0.40 * spot * vol * 100), so the tradeable universe is almost entirely sub-$20.
#
# Lowered, NOT removed: sub-$1.50 and thin-tape names still fail, because option
# spreads there make the fill cost swamp any edge the trade might have.
# Env-overridable so the floors can be tightened without another code change.
MIN_PRICE = float(os.environ.get("ALFRED_MIN_PRICE", "1.50"))
MIN_AVG_VOLUME = int(os.environ.get("ALFRED_MIN_AVG_VOLUME", "1000000"))
USER_AGENT = "Alfred-Trader-ApeWisdom/1.0 (read-only discovery client)"
TICKER_RE = re.compile(r"^[A-Z]{1,5}$")
ALLOWED_QUOTE_TYPES = {"EQUITY", "ETF"}


class ApeWisdomError(RuntimeError):
    pass


def _utc_iso(epoch: float | None = None) -> str:
    dt = datetime.fromtimestamp(epoch if epoch is not None else time.time(), timezone.utc)
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def _strict_int(value: Any, field: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool):
        raise ApeWisdomError(f"{field} is boolean, not integer")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        result = int(value.strip())
    else:
        raise ApeWisdomError(f"{field} is not an integer")
    if minimum is not None and result < minimum:
        raise ApeWisdomError(f"{field} is below {minimum}")
    return result


def _nullable_nonnegative_int(value: Any, field: str) -> int | None:
    if value is None or value == "":
        return None
    return _strict_int(value, field, minimum=0)


def _clean_name(value: Any) -> str:
    if not isinstance(value, str):
        raise ApeWisdomError("name is not a string")
    name = " ".join(html.unescape(value).split())[:160]
    if not name:
        raise ApeWisdomError("name is empty")
    return name


def normalize_row(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ApeWisdomError("result row is not an object")
    ticker = str(raw.get("ticker") or "").strip().upper()
    if not TICKER_RE.fullmatch(ticker):
        raise ApeWisdomError("ticker is not a supported US stock symbol")
    rank = _strict_int(raw.get("rank"), "rank", minimum=1)
    return {
        "rank": rank,
        "ticker": ticker,
        "name": _clean_name(raw.get("name")),
    }


def _trend_from_path(path: str) -> list[int]:
    """Convert ApeWisdom's 31 SVG y-coordinates to a 0..100 activity shape.

    The table does not publish historical counts.  It publishes a normalized
    sparkline, so this deliberately preserves only that shape and never labels
    the values as mention counts.
    """
    pairs = re.findall(r"(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)", path or "")
    by_x: dict[int, float] = {}
    for x_raw, y_raw in pairs:
        x, y = float(x_raw), float(y_raw)
        xi = int(round(x))
        if abs(x - xi) < 1e-6 and 0 <= xi <= 150 and xi % 5 == 0:
            if not math.isfinite(y) or not 0 <= y <= 60:
                raise ApeWisdomError("30-day trend contains an invalid coordinate")
            by_x[xi] = y
    expected = list(range(0, 151, 5))
    if sorted(by_x) != expected:
        raise ApeWisdomError("30-day trend is not the expected 31-point curve")
    # ApeWisdom's chart uses y=3 for the high and y=41 for the low.  Clamp to
    # that published plotting range, invert, and retain a comparable shape.
    return [round((41 - min(41.0, max(3.0, by_x[x]))) / 38 * 100) for x in expected]


class _TrendTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_table = False
        self.row: dict[str, Any] | None = None
        self.td_index = -1
        self.rows: dict[str, dict[str, Any]] = {}

    def handle_starttag(self, tag: str, attrs) -> None:
        attr = dict(attrs)
        if tag == "table" and attr.get("id") == "default-table":
            self.in_table = True
        elif self.in_table and tag == "tr":
            self.row, self.td_index = {}, -1
        elif self.row is not None and tag == "td":
            self.td_index += 1
            if self.td_index == 1 and attr.get("data-sort"):
                self.row["rank"] = attr["data-sort"]
        elif self.row is not None and tag == "a" and self.td_index == 2:
            match = re.fullmatch(r"/stocks/([A-Za-z]{1,5})/", attr.get("href", ""))
            if match:
                self.row["ticker"] = match.group(1).upper()
        elif self.row is not None and tag == "path" and self.td_index == 6:
            self.row["path"] = attr.get("d", "")

    def handle_endtag(self, tag: str) -> None:
        if self.in_table and tag == "tr" and self.row is not None:
            try:
                ticker = self.row["ticker"]
                parsed = {
                    "table_rank": _strict_int(self.row.get("rank"), "table_rank", minimum=1),
                    "mention_trend_30d": _trend_from_path(self.row.get("path", "")),
                }
                self.rows[ticker] = parsed
            except (KeyError, ApeWisdomError):
                pass
            self.row = None
        elif tag == "table" and self.in_table:
            self.in_table = False


def parse_trend_table(document: str) -> dict[str, dict[str, Any]]:
    if not isinstance(document, str) or "Trend (30 days)" not in document:
        raise ApeWisdomError("stock table lacks the 30-day trend contract")
    parser = _TrendTableParser()
    parser.feed(document)
    parser.close()
    if not parser.rows:
        raise ApeWisdomError("stock table contains no valid 30-day trends")
    return parser.rows


def trend_sparkline(values: Any) -> str:
    if not isinstance(values, list) or len(values) != 31:
        return "n/a"
    if any(isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 100 for v in values):
        return "n/a"
    bars = "▁▂▃▄▅▆▇█"
    return "".join(bars[min(7, max(0, round(v * 7 / 100)))] for v in values)


def normalize_payload(
    payload: Any, filter_name: str, limit: int, trend_rows: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ApeWisdomError("API payload is not an object")
    _strict_int(payload.get("count"), "count", minimum=0)
    _strict_int(payload.get("pages"), "pages", minimum=0)
    if _strict_int(payload.get("current_page"), "current_page", minimum=1) != 1:
        raise ApeWisdomError("unexpected API page")
    raw_results = payload.get("results")
    if not isinstance(raw_results, list) or len(raw_results) > 100:
        raise ApeWisdomError("results is not a bounded list")

    by_ticker: dict[str, dict[str, Any]] = {}
    rejected = 0
    for raw in raw_results:
        try:
            row = normalize_row(raw)
            trend = trend_rows.get(row["ticker"])
            if not isinstance(trend, dict):
                raise ApeWisdomError("30-day trend missing for ticker")
            values = trend.get("mention_trend_30d")
            if trend_sparkline(values) == "n/a":
                raise ApeWisdomError("30-day trend malformed for ticker")
            row["mention_trend_30d"] = values
            row["mention_trend_30d_sparkline"] = trend_sparkline(values)
        except ApeWisdomError:
            rejected += 1
            continue
        existing = by_ticker.get(row["ticker"])
        if existing is None or row["rank"] < existing["rank"]:
            by_ticker[row["ticker"]] = row
    rows = sorted(by_ticker.values(), key=lambda row: (row["rank"], row["ticker"]))
    if raw_results and not rows:
        raise ApeWisdomError("no valid result rows")
    if rejected > max(3, len(raw_results) // 4):
        raise ApeWisdomError("too many malformed rows; possible API schema drift")
    return {
        "schema": "apewisdom-name-discovery.v2",
        "source": "apewisdom",
        "source_url": API_TEMPLATE.format(filter_name=filter_name),
        "trend_source_url": TABLE_URLS[filter_name],
        "filter": filter_name,
        "observed_window_days": 30,
        "signal_type": "attention_only",
        "sentiment_direction": None,
        "trade_authority": False,
        "training_eligible": False,
        "results": rows[:limit],
        "rejected_rows": rejected,
    }


def _default_cache_dir() -> Path:
    return Path.home() / "Library" / "Caches" / "alfred-trader" / "apewisdom"


def _cache_path(cache_dir: Path, filter_name: str) -> Path:
    return cache_dir / f"{filter_name}.json"


def _read_cache(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("fetched_at_epoch"), (int, float)):
            return None
        if not isinstance(data.get("data"), dict):
            return None
        return data
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp_name = tempfile.mkstemp(prefix=".apewisdom-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _fetch_json(url: str) -> Any:
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": USER_AGENT}, method="GET"
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        if "json" not in (response.headers.get("Content-Type") or "").lower():
            raise ApeWisdomError("API returned non-JSON content")
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ApeWisdomError("API response exceeds size limit")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApeWisdomError("API returned invalid JSON") from exc


def _fetch_text(url: str) -> str:
    request = urllib.request.Request(
        url, headers={"Accept": "text/html", "User-Agent": USER_AGENT}, method="GET"
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        if "html" not in (response.headers.get("Content-Type") or "").lower():
            raise ApeWisdomError("stock table returned non-HTML content")
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ApeWisdomError("stock table response exceeds size limit")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ApeWisdomError("stock table returned invalid UTF-8") from exc


def load_trends(
    filter_name: str = DEFAULT_FILTER,
    limit: int = DEFAULT_SOURCE_LIMIT,
    *,
    cache_dir: Path | None = None,
    now: float | None = None,
    fetch_json: Callable[[str], Any] = _fetch_json,
    fetch_text: Callable[[str], str] = _fetch_text,
) -> dict[str, Any]:
    if filter_name not in SUPPORTED_FILTERS:
        raise ApeWisdomError(f"unsupported filter: {filter_name}")
    if not 1 <= limit <= MAX_SOURCE_LIMIT:
        raise ApeWisdomError(f"limit must be 1..{MAX_SOURCE_LIMIT}")
    current_time = time.time() if now is None else float(now)
    path = _cache_path(cache_dir or _default_cache_dir(), filter_name)
    cached = _read_cache(path)
    if cached and 0 <= current_time - cached["fetched_at_epoch"] <= CACHE_TTL_SECONDS:
        result = dict(cached["data"])
        result.update({
            "fetched_at_utc": _utc_iso(cached["fetched_at_epoch"]),
            "age_seconds": int(current_time - cached["fetched_at_epoch"]),
            "stale": False,
            "cache_hit": True,
        })
        result["results"] = result.get("results", [])[:limit]
        return result
    url = API_TEMPLATE.format(filter_name=filter_name)
    try:
        trends = parse_trend_table(fetch_text(TABLE_URLS[filter_name]))
        normalized = normalize_payload(
            fetch_json(url), filter_name, MAX_SOURCE_LIMIT, trends
        )
        _write_cache(path, {"fetched_at_epoch": current_time, "data": normalized})
        result = dict(normalized)
        result.update({"fetched_at_utc": _utc_iso(current_time), "age_seconds": 0,
                       "stale": False, "cache_hit": False})
        result["results"] = result["results"][:limit]
        return result
    except Exception as exc:
        if cached:
            age = current_time - cached["fetched_at_epoch"]
            if 0 <= age <= MAX_STALE_SECONDS:
                result = dict(cached["data"])
                result.update({"fetched_at_utc": _utc_iso(cached["fetched_at_epoch"]),
                               "age_seconds": int(age), "stale": True, "cache_hit": True,
                               "fetch_error": f"{type(exc).__name__}: {exc}"})
                result["results"] = result.get("results", [])[:limit]
                return result
        if isinstance(exc, ApeWisdomError):
            raise
        if isinstance(exc, urllib.error.HTTPError):
            raise ApeWisdomError(f"API HTTP {exc.code}") from exc
        raise ApeWisdomError(f"API unavailable: {type(exc).__name__}: {exc}") from exc


def initial_rows(feed: dict[str, Any], blocked: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Return rank-ordered, exact-ticker-filtered rows before live eligibility probes."""
    blocked_up = {str(t).upper() for t in blocked}
    # The locked trader doctrine explicitly bans TSLA while allowing SPCX as the one exception.
    blocked_up.add("TSLA")
    out = []
    for row in feed.get("results", []) if isinstance(feed, dict) else []:
        ticker = str(row.get("ticker") or "").upper()
        if TICKER_RE.fullmatch(ticker) and ticker not in blocked_up:
            out.append(dict(row, ticker=ticker))
    return out


def security_profile(ticker: str) -> dict[str, Any]:
    """Fetch the source-eligibility profile. Missing fields remain missing and fail closed."""
    import yfinance as yf

    info = yf.Ticker(ticker).info or {}
    price = info.get("regularMarketPrice") or info.get("currentPrice")
    volume = info.get("averageVolume") or info.get("averageVolume10days")
    return {
        "quote_type": str(info.get("quoteType") or "").upper(),
        "currency": str(info.get("currency") or "").upper(),
        "exchange": str(info.get("exchange") or ""),
        "price": float(price) if price is not None else None,
        "average_volume": int(volume) if volume is not None else None,
        "industry": str(info.get("industry") or ""),
        "sector": str(info.get("sector") or ""),
    }


def profile_eligible(profile: dict[str, Any], blocked_sector_keywords: Iterable[str]) -> tuple[bool, str]:
    """Fail-closed external-source screen. Floors are MIN_PRICE / MIN_AVG_VOLUME."""
    if profile.get("quote_type") not in ALLOWED_QUOTE_TYPES:
        return False, "not_equity_or_etf"
    if profile.get("currency") != "USD":
        return False, "not_usd"
    price, volume = profile.get("price"), profile.get("average_volume")
    if (not isinstance(price, (int, float)) or isinstance(price, bool)
            or not math.isfinite(price) or price < MIN_PRICE):
        return False, f"price_below_{MIN_PRICE:g}"
    if not isinstance(volume, int) or isinstance(volume, bool) or volume < MIN_AVG_VOLUME:
        return False, f"average_volume_below_{MIN_AVG_VOLUME}"
    hay = f"{profile.get('industry', '')} {profile.get('sector', '')}".lower().strip()
    if not hay:
        return False, "sector_unknown"
    if any(str(k).strip().lower() in hay for k in blocked_sector_keywords if str(k).strip()):
        return False, "blocked_sector"
    return True, "eligible"


def merge_research_universe(base_names: Iterable[str], eligible_rows: Iterable[dict[str, Any]]) -> list[str]:
    """Preserve the existing universe and append Ape symbols alphabetically, never by rank."""
    base, seen = [], set()
    for name in base_names:
        ticker = str(name).upper()
        if TICKER_RE.fullmatch(ticker) and ticker not in seen:
            seen.add(ticker)
            base.append(ticker)
    additions = sorted({str(r.get("ticker") or "").upper() for r in eligible_rows
                        if TICKER_RE.fullmatch(str(r.get("ticker") or "").upper())} - seen)
    return base + additions


def _price_trend(ticker: str, price_stats: Any) -> str:
    stats = price_stats.get(ticker) if isinstance(price_stats, dict) else None
    value = stats.get("ret_20d") if isinstance(stats, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "n/a"
    return f"{value:+.1f}%"


def discovery_context(
    brief: str,
    eligible_rows: Iterable[dict[str, Any]],
    watched: Iterable[str],
    price_stats: Any = None,
) -> str:
    """Add the requested rank/trends only to the separate name-discovery call."""
    watched_up = {str(t).upper() for t in watched}
    rows = [r for r in eligible_rows if r.get("ticker") not in watched_up]
    lines = [brief, "", "ApeWisdom candidate pool (rank and normalized 30-day mention shape only — NOT sentiment,",
             "price direction, conviction, or a buy signal). Select ONLY from this pool in the",
             "ApeWisdom pass; every name still requires independent research and all normal gates:"]
    for row in rows:
        lines.append(
            f"  {row['ticker']} ({row['name']}): ApeWisdom rank #{row['rank']}; "
            f"30d mention shape {row['mention_trend_30d_sparkline']}; "
            f"~30d price trend (20 sessions) {_price_trend(row['ticker'], price_stats)}"
        )
    return "\n".join(lines)


def bind_reviewed_candidates(
    reviewed, eligible_rows: Iterable[dict[str, Any]], watched=(), price_stats: Any = None
):
    """Code-side allowlist and deterministic source label for model-reviewed Ape names."""
    watched_up = {str(t).upper() for t in watched}
    by_ticker = {str(r.get("ticker") or "").upper(): r for r in eligible_rows}
    out, seen = [], set()
    for ticker, _model_reason in reviewed or []:
        ticker = str(ticker).upper()
        row = by_ticker.get(ticker)
        if row is None or ticker in watched_up or ticker in seen:
            continue
        seen.add(ticker)
        out.append((
            ticker,
            f"ApeWisdom rank #{row['rank']} | 30d mentions "
            f"{row['mention_trend_30d_sparkline']} | ~30d price "
            f"(20 sessions) {_price_trend(ticker, price_stats)}; explore only, not a buy signal",
        ))
    return out
