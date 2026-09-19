#!/usr/bin/env python3
"""Public API contract; production-derived narrative omitted."""
import os
import json
import bisect
import numpy as np
import pandas as pd



_HORIZON_LABELS = {"~1 week", "~2 weeks", "~1 month", "~2 months", "~3 months"}








_TRADING_DIR = os.environ.get("EXITMGR_TRADING_DIR",
                              os.path.expanduser("~/evaluation-harness/trading"))


def _load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


_IVR_DB = _load_json(os.path.join(_TRADING_DIR, "ibkr_ivr.json"))

_IVR_SORTED = {t: (sorted(d.keys()), d) for t, d in _IVR_DB.items()}
_EARN_DEEP = _load_json(os.path.join(_TRADING_DIR, "earnings_deep.json"))

_EARN_SORTED = {t: sorted(v) for t, v in _EARN_DEEP.items()}


def true_ivr(t, bar_date):
    """Public API contract; production-derived narrative omitted."""
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
    """Public API contract; production-derived narrative omitted."""
    ev = _EARN_SORTED.get(t)
    if not ev:
        return "earn n/a"
    ds = bar_date.strftime("%Y-%m-%d")
    j = bisect.bisect_right(ev, ds)
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
    """Public API contract; production-derived narrative omitted."""
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
    """Public API contract; production-derived narrative omitted."""


def technical_card(ticker, df, vix_series, horizon_label="~2 weeks"):
    """Public API contract; production-derived narrative omitted."""
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
    i = n - 1
    if i < 120:
        raise InsufficientHistory(f"{ticker}: need >=121 bars for 120d momentum, have {n}")
    row = h.iloc[i]
    if pd.isna(row["sma200"]) or pd.isna(row["rsi"]) or pd.isna(row["ivr"]) or pd.isna(row["vix"]):
        raise InsufficientHistory(f"{ticker}: indicators not yet defined on latest bar (NaN sma200/rsi/ivr/vix)")
    base = base_string(ticker, c, i, row, row.name)
    return base + f" Stance next {horizon_label}?"






SYS_T = ("You are a disciplined market technician. From the technical indicators, judge the next {label} "
         "and answer ONLY as JSON: {{\"call\":\"BULLISH|BEARISH|NEUTRAL\",\"conviction\":1-10}}.")


def fetch_card(ticker, vix_series=None, horizon_label="~2 weeks", period_days=520, end=None):
    """Public API contract; production-derived narrative omitted."""
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
    """Public API contract; production-derived narrative omitted."""
    return [
        {"role": "system", "content": SYS_T.format(label=horizon_label)},
        {"role": "user", "content": card_text},
    ]
