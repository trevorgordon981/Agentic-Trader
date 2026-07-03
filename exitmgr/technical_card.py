#!/usr/bin/env python3
"""technical_card.py -- byte-exact port of the SFT user-prompt produced by
~/gordon-gauntlet/trading/gen_train_huge4.py.

The fine-tuned Gemma was trained on a VERY specific prompt format. For the fine-tune to be
worth anything in production, the live trading system must serialise incoming price data into
the EXACT string the model trained on -- byte-for-byte (rounding, sign, spacing, < vs >, wording).

`technical_card(ticker, df, vix_series, horizon_label="~2 weeks")` returns the full
user-prompt for the LATEST bar in `df`, i.e. the source generator's

    base + f" Stance next {label}?"

with the indicator columns computed identically to gen_train_huge4.feats()/rsi()/mom()/vregime().

PORTED VERBATIM (do not "improve" the math/format -- byte-exactness is the contract):
  - rsi(): Wilder-less simple-mean RSI exactly as the source.
  - feats(): every rolling indicator, including the realized-vol-rank "ivr" (annualized 20-day
    realized vol ranked as a 0-100 percentile over a trailing 252-day window) -- now used ONLY as
    the proxy FALLBACK (see below).
  - vregime(): VIX regime buckets.
  - mom(): array-index momentum (c[i]/c[i-k]-1)*100 on the Close ndarray.
  - the `base` f-string, character for character (v4 layout: ... IVR. {earn_field}. VIX ...).

v4 DELTAS vs the old v3 port (gen_train_huge3) -- ALL ELSE BYTE-IDENTICAL:
  1. IVR VALUE source: the TRUE IBKR IV-rank from ibkr_ivr.json (nearest date <= bar-date) when
     present, else the realized-vol proxy from feats()['ivr']. Format `IVR {ivr:.0f}` unchanged.
  2. A new `{earn_field}` is inserted RIGHT AFTER the IVR field and BEFORE VIX:
       `{N}d to earnings`  -- next earnings_deep[ticker] strictly after bar-date, within 90 days
       `earn n/a`          -- otherwise (ETFs / ticker absent / next date >90d away)

NEW DATA DEPENDENCIES (loaded once at import from ~/gordon-gauntlet/trading):
  - earnings_deep.json : {ticker: [YYYY-MM-DD, ...]}  -> the days-to-earnings field.
  - ibkr_ivr.json      : {ticker: {YYYY-MM-DD: rank0-100}}  -> the true IV-rank value.
  A live ticker absent from these (e.g. an ETF like SPY/IWM) emits `earn n/a` and the proxy IVR,
  which is EXACTLY what gen_train_huge4 emits for the same absent ticker -> no train/serve skew.

`vix_series` is a pandas Series of ^VIX closes (DatetimeIndex). It is reindexed onto df's index
with method="ffill" exactly like the source (`VIX.reindex(h.index, method="ffill")`).
"""
import os
import json
import bisect
import numpy as np
import pandas as pd

# Horizon label -> exactly the (H, label) pairs the generator used; only the LABEL string is
# part of the prompt ("Stance next {label}?"). Default "~2 weeks" matches HS=(10,"~2 weeks").
_HORIZON_LABELS = {"~1 week", "~2 weeks", "~1 month", "~2 months", "~3 months"}

# --------------------------------------------------------------------------------------------
# v4 data sources -- loaded ONCE at import, mirroring gen_train_huge4.py's module-level load.
#   ibkr_ivr.json    : {ticker: {YYYY-MM-DD: rank0-100}}  -> true IBKR IV-rank
#   earnings_deep.json : {ticker: [YYYY-MM-DD, ...]}       -> days-to-earnings field
# Path resolution mirrors the generator's D = ~/gordon-gauntlet/trading, overridable via
# EXITMGR_TRADING_DIR for tests / relocated installs. Missing files degrade gracefully to the
# v3 behaviour (proxy IVR + `earn n/a`), but in production both files are expected to exist.
_TRADING_DIR = os.environ.get("EXITMGR_TRADING_DIR",
                              os.path.expanduser("~/gordon-gauntlet/trading"))


def _load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


_IVR_DB = _load_json(os.path.join(_TRADING_DIR, "ibkr_ivr.json"))
# {ticker: (sorted_date_keys, {date: rank})} -- same shape as gen_train_huge4.IVR_SORTED
_IVR_SORTED = {t: (sorted(d.keys()), d) for t, d in _IVR_DB.items()}
_EARN_DEEP = _load_json(os.path.join(_TRADING_DIR, "earnings_deep.json"))
# {ticker: sorted([YYYY-MM-DD, ...])} -- same shape as gen_train_huge4.EARN_SORTED
_EARN_SORTED = {t: sorted(v) for t, v in _EARN_DEEP.items()}


def true_ivr(t, bar_date):
    """Nearest IBKR IV-rank with date <= bar_date; None if no such ticker/date.
    VERBATIM port of gen_train_huge4.true_ivr()."""
    s = _IVR_SORTED.get(t)
    if not s:
        return None
    keys, dmap = s
    ds = bar_date.strftime("%Y-%m-%d")
    j = bisect.bisect_right(keys, ds) - 1
    if j < 0:
        return None
    return dmap[keys[j]]


def earn_field(t, bar_date):
    """`<N>d to earnings` if next earnings strictly after bar_date within 90d, else `earn n/a`.
    VERBATIM port of gen_train_huge4.earn_field()."""
    ev = _EARN_SORTED.get(t)
    if not ev:
        return "earn n/a"
    ds = bar_date.strftime("%Y-%m-%d")
    j = bisect.bisect_right(ev, ds)   # first earnings date strictly > bar_date
    if j >= len(ev):
        return "earn n/a"
    nxt = pd.Timestamp(ev[j]); nd = (nxt - bar_date).days
    return f"{nd}d to earnings" if 0 < nd <= 90 else "earn n/a"


def vregime(v):
    return "calm" if v < 14 else "normal" if v < 19 else "elevated" if v < 26 else "high" if v < 36 else "extreme"


def rsi(s, n=14):
    d = s.diff(); up = d.clip(lower=0).rolling(n).mean(); dn = (-d.clip(upper=0)).rolling(n).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def feats(df):
    c = df["Close"]; hi = df["High"]; lo = df["Low"]
    df["sma20"] = c.rolling(20).mean(); df["sma50"] = c.rolling(50).mean(); df["sma200"] = c.rolling(200).mean()
    df["rsi"] = rsi(c); e12 = c.ewm(span=12).mean(); e26 = c.ewm(span=26).mean(); m = e12 - e26
    df["macd_h"] = m - m.ewm(span=9).mean(); sd = c.rolling(20).std()
    df["bb"] = (c - (df["sma20"] - 2 * sd)) / (4 * sd); tr = pd.concat([hi - lo, (hi - c.shift()).abs(), (lo - c.shift()).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean() / c * 100; df["volr"] = df["Volume"] / df["Volume"].rolling(20).mean()
    df["hi52"] = c / c.rolling(252).max() * 100 - 100; df["lo52"] = c / c.rolling(252).min() * 100 - 100
    rv = c.pct_change().rolling(20).std() * np.sqrt(252) * 100
    df["ivr"] = rv.rolling(252).rank(pct=True) * 100
    return df


def base_string(ticker, c, i, row, bar_date):
    """The generator's `base` f-string, VERBATIM (v4 layout). `c` is the Close ndarray, `i` the bar
    index, `row` the (feats-enriched) row including a 'vix' field, `bar_date` the bar's Timestamp
    (used for the true-IV lookup and the days-to-earnings field).

    IVR VALUE: true IBKR rank (ibkr_ivr.json, nearest date <= bar_date) when present, else the
    realized-vol proxy in row['ivr'] -- IDENTICAL to gen_train_huge4 (which falls back to
    row['ivr_proxy']; this port stores that same proxy under the column name 'ivr')."""
    def mom(c, i, k):
        return (c[i] / c[i - k] - 1) * 100
    t = ticker
    tiv = true_ivr(t, bar_date)
    ivr_val = float(tiv) if tiv is not None else float(row["ivr"])
    ef = earn_field(t, bar_date)
    return (f"{t} ${c[i]:.2f}. Mom 5d {mom(c,i,5):+.1f}% 20d {mom(c,i,20):+.1f}% 60d {mom(c,i,60):+.1f}% 120d {mom(c,i,120):+.1f}%. "
            f"RSI14 {row['rsi']:.0f}. vs SMA20 {(c[i]/row['sma20']-1)*100:+.1f}% SMA50 {(c[i]/row['sma50']-1)*100:+.1f}% SMA200 {(c[i]/row['sma200']-1)*100:+.1f}%. "
            f"SMA20{'>' if row['sma20']>row['sma50'] else '<'}SMA50. MACD-h {row['macd_h']:+.2f}. BB%B {row['bb']:.2f}. ATR {row['atr']:.1f}%. "
            f"Vol {row['volr']:.1f}x. {row['hi52']:+.0f}% from 52wk high, {row['lo52']:+.0f}% above 52wk low. "
            f"IVR {ivr_val:.0f}. {ef}. VIX {row['vix']:.0f} {vregime(row['vix'])}.")


class InsufficientHistory(ValueError):
    """Raised when df doesn't have enough clean bars to compute the indicators the prompt needs."""


def technical_card(ticker, df, vix_series, horizon_label="~2 weeks"):
    """Produce the byte-exact SFT user-prompt for the LATEST bar of `df`.

    ticker         : symbol string (used verbatim, e.g. "AAPL").
    df             : daily OHLCV DataFrame, most-recent bar LAST, columns Open/High/Low/Close/Volume.
    vix_series     : pandas Series of ^VIX closes (DatetimeIndex), reindexed+ffilled onto df.
    horizon_label  : the "{label}" in "Stance next {label}?" (default "~2 weeks").

    Returns: base + f" Stance next {horizon_label}?"  -- identical to what gen_train_huge4 wrote.

    Edge cases (mirror the generator, which SKIPS such bars rather than emitting them):
      - needs >=420 bars and a non-NaN sma200/rsi/ivr/vix on the latest bar, else InsufficientHistory.
      - the generator only ever scores bars i in [210, n-MAXH-1); for the latest bar we don't need
        the forward window, only that the indicators are defined (mom needs >=121 bars of history).
    """
    if horizon_label not in _HORIZON_LABELS:
        raise ValueError(f"horizon_label {horizon_label!r} not one of {sorted(_HORIZON_LABELS)}")
    h = df.dropna()
    if len(h) < 420:
        raise InsufficientHistory(f"{ticker}: need >=420 clean bars, have {len(h)}")
    h = feats(h.copy())
    c = h["Close"].values
    n = len(c)
    h = h.copy()
    h["vix"] = vix_series.reindex(h.index, method="ffill")
    i = n - 1  # latest bar
    if i < 120:
        raise InsufficientHistory(f"{ticker}: need >=121 bars for 120d momentum, have {n}")
    row = h.iloc[i]
    if pd.isna(row["sma200"]) or pd.isna(row["rsi"]) or pd.isna(row["ivr"]) or pd.isna(row["vix"]):
        raise InsufficientHistory(f"{ticker}: indicators not yet defined on latest bar (NaN sma200/rsi/ivr/vix)")
    base = base_string(ticker, c, i, row, row.name)
    return base + f" Stance next {horizon_label}?"


# --------------------------------------------------------------------------------------------
# Integration helpers for the live system (used ONLY when the fine-tuned Gemma is the model).
# These do not touch IBKR, place trades, or alter the MiniMax brief path.
# --------------------------------------------------------------------------------------------
SYS_T = ("You are a disciplined market technician. From the technical indicators, judge the next {label} "
         "and answer ONLY as JSON: {{\"call\":\"BULLISH|BEARISH|NEUTRAL\",\"conviction\":1-10}}.")


def fetch_card(ticker, vix_series=None, horizon_label="~2 weeks", period_days=520, end=None):
    """Self-contained: download daily bars (and ^VIX if not supplied) via yfinance and build the
    byte-exact card for `ticker`'s latest bar. Mirrors how the SFT data was generated (yfinance,
    auto_adjust=True). Returns the user-prompt string, or raises InsufficientHistory.

    Kept separate from the live IB brief on purpose: the SFT format needs >=420 daily bars and the
    252-day vol-rank, which the IB delayed-bar window in research.gather does not provide.
    """
    import yfinance as yf
    start = (pd.Timestamp.today().normalize() - pd.Timedelta(days=max(period_days, 520) * 2))
    df = yf.download(ticker, start=start.strftime("%Y-%m-%d"),
                     end=(end or None), auto_adjust=True, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df = df.xs(ticker, axis=1, level=-1, drop_level=True) if ticker in df.columns.get_level_values(-1) \
            else df.droplevel(0, axis=1)
    if vix_series is None:
        vr = yf.download("^VIX", start=start.strftime("%Y-%m-%d"),
                         end=(end or None), auto_adjust=True, progress=False)
        vix_series = vr["Close"].squeeze()
    return technical_card(ticker, df, vix_series, horizon_label=horizon_label)


def card_messages(ticker, card_text, horizon_label="~2 weeks"):
    """The exact chat-message shape the Gemma fine-tune trained on: SYS_T (with the matching label)
    as system, the technical card as the user turn. (Assistant turn is the model's job to produce.)"""
    return [
        {"role": "system", "content": SYS_T.format(label=horizon_label)},
        {"role": "user", "content": card_text},
    ]
