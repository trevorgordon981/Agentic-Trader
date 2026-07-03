#!/usr/bin/env python3
"""Byte-exact verification of exitmgr.technical_card (v4) against the SFT generator
gen_train_huge4.py.

For a hand-picked set of (ticker, historical-date) points -- chosen to exercise EVERY v4 code
path -- we:
  (a) run the EXACT inline logic copied VERBATIM from gen_train_huge4.py on the FULL history
      (the truth: v4 feats()/rsi()/vregime() + true_ivr()/earn_field() + the v4 `base` f-string), and
  (b) run exitmgr.technical_card.technical_card() on the SAME bars truncated to that date,
and assert (a) == (b) character-for-character.

v4 PATHS COVERED (asserted present at the end):
  - TRUE-IV + real earnings countdown  : AAPL near an upcoming earnings date (in BOTH dbs).
  - TRUE-IV + `earn n/a`               : AAPL at a date with no earnings inside 90d.
  - PROXY-IV + real earnings countdown : a name in earnings_deep but NOT ibkr_ivr (e.g. NVDA).
  - PROXY-IV + `earn n/a` (ETF/absent) : SPY / IWM (in neither db) -> proxy IVR + `earn n/a`.

WHY truncation is sound: every indicator in feats()/rsi() and the proxy IVR vol-rank uses only
BACKWARD-looking rolling/ewm/rank windows; true_ivr/earn_field look up dates <= / > bar-date.
So the value AT a bar is identical whether or not future bars exist. technical_card scores the
LATEST bar, so we hand it df[:date].

Run:  ~/ib-grader-venv/bin/python ~/exitmgr-app/test_card_match.py
"""
import os, sys, json, bisect
import numpy as np, pandas as pd, yfinance as yf

sys.path.insert(0, os.path.expanduser("~/exitmgr-app"))
from exitmgr.technical_card import technical_card

# ---------- v4 DATA SOURCES -- loaded the SAME WAY gen_train_huge4.py loads them ----------
D = os.path.expanduser("~/gordon-gauntlet/trading")
IVR_DB = json.load(open(f"{D}/ibkr_ivr.json"))
IVR_SORTED = {t: (sorted(d.keys()), d) for t, d in IVR_DB.items()}
EARN_DEEP = json.load(open(f"{D}/earnings_deep.json"))
EARN_SORTED = {t: sorted(v) for t, v in EARN_DEEP.items()}

# ---------- (a) INLINE LOGIC -- copied VERBATIM from gen_train_huge4.py ----------
def true_ivr(t, bar_date):
    s = IVR_SORTED.get(t)
    if not s: return None
    keys, dmap = s; ds = bar_date.strftime("%Y-%m-%d")
    j = bisect.bisect_right(keys, ds) - 1
    if j < 0: return None
    return dmap[keys[j]]
def earn_field(t, bar_date):
    ev = EARN_SORTED.get(t)
    if not ev: return "earn n/a"
    ds = bar_date.strftime("%Y-%m-%d")
    j = bisect.bisect_right(ev, ds)
    if j >= len(ev): return "earn n/a"
    nxt = pd.Timestamp(ev[j]); nd = (nxt - bar_date).days
    return f"{nd}d to earnings" if 0 < nd <= 90 else "earn n/a"
def vregime(v):
    return "calm" if v<14 else "normal" if v<19 else "elevated" if v<26 else "high" if v<36 else "extreme"
def rsi(s,n=14):
    d=s.diff(); up=d.clip(lower=0).rolling(n).mean(); dn=(-d.clip(upper=0)).rolling(n).mean()
    return 100-100/(1+up/dn.replace(0,np.nan))
def feats(df):
    c=df["Close"]; hi=df["High"]; lo=df["Low"]
    df["sma20"]=c.rolling(20).mean(); df["sma50"]=c.rolling(50).mean(); df["sma200"]=c.rolling(200).mean()
    df["rsi"]=rsi(c); e12=c.ewm(span=12).mean(); e26=c.ewm(span=26).mean(); m=e12-e26
    df["macd_h"]=m-m.ewm(span=9).mean(); sd=c.rolling(20).std()
    df["bb"]=(c-(df["sma20"]-2*sd))/(4*sd); tr=pd.concat([hi-lo,(hi-c.shift()).abs(),(lo-c.shift()).abs()],axis=1).max(axis=1)
    df["atr"]=tr.rolling(14).mean()/c*100; df["volr"]=df["Volume"]/df["Volume"].rolling(20).mean()
    df["hi52"]=c/c.rolling(252).max()*100-100; df["lo52"]=c/c.rolling(252).min()*100-100
    rv=c.pct_change().rolling(20).std()*np.sqrt(252)*100
    df["ivr_proxy"]=rv.rolling(252).rank(pct=True)*100
    return df

def inline_prompt(t, full_df, VIX, i, label):
    """Reproduce gen_train_huge4's exact base+stance string for bar i of full_df (returns also the
    classification of which v4 path it took, for coverage assertions)."""
    h=feats(full_df.copy()); c=h["Close"].values; n=len(c)
    h["vix"]=VIX.reindex(h.index,method="ffill")
    def mom(c,i,k): return (c[i]/c[i-k]-1)*100
    row=h.iloc[i]; bar_date=h.index[i]
    if pd.isna(row["sma200"]) or pd.isna(row["rsi"]) or pd.isna(row["ivr_proxy"]) or pd.isna(row["vix"]):
        return None, None
    tiv=true_ivr(t, bar_date)
    if tiv is not None: ivr_val=float(tiv); src="true"
    else: ivr_val=float(row["ivr_proxy"]); src="proxy"
    ef=earn_field(t, bar_date)
    base=(f"{t} ${c[i]:.2f}. Mom 5d {mom(c,i,5):+.1f}% 20d {mom(c,i,20):+.1f}% 60d {mom(c,i,60):+.1f}% 120d {mom(c,i,120):+.1f}%. "
          f"RSI14 {row['rsi']:.0f}. vs SMA20 {(c[i]/row['sma20']-1)*100:+.1f}% SMA50 {(c[i]/row['sma50']-1)*100:+.1f}% SMA200 {(c[i]/row['sma200']-1)*100:+.1f}%. "
          f"SMA20{'>' if row['sma20']>row['sma50'] else '<'}SMA50. MACD-h {row['macd_h']:+.2f}. BB%B {row['bb']:.2f}. ATR {row['atr']:.1f}%. "
          f"Vol {row['volr']:.1f}x. {row['hi52']:+.0f}% from 52wk high, {row['lo52']:+.0f}% above 52wk low. "
          f"IVR {ivr_val:.0f}. {ef}. VIX {row['vix']:.0f} {vregime(row['vix'])}.")
    path = (src, "earn" if ef != "earn n/a" else "noearn")
    return base+f" Stance next {label}?", path
# ---------------------------------------------------------------------------------

# (ticker, target-date, expected-path) chosen to hit each v4 path. The actual bar used is the LAST
# trading bar on-or-before target-date. AAPL is in BOTH dbs; NVDA earnings-only; SPY/IWM neither.
TARGETS = [
    ("AAPL", "2023-04-10", ("true",  "earn")),    # true-IV + real countdown
    ("AAPL", "2022-10-29", ("true",  "noearn")),  # true-IV + earn n/a (just after 10-27 print, next 02-02 is 96d out)
    ("NVDA", "2023-08-10", ("proxy", "earn")),    # proxy-IV (not in ibkr_ivr) + countdown
    ("NVDA", "2023-03-10", ("proxy", "earn")),    # proxy-IV + countdown (second)
    ("SPY",  "2023-06-15", ("proxy", "noearn")),  # ETF: neither db -> proxy + n/a
    ("IWM",  "2024-09-20", ("proxy", "noearn")),  # ETF: neither db -> proxy + n/a
    ("AAPL", "2023-07-20", ("true",  "earn")),    # true-IV + countdown
    ("SPY",  "2024-11-01", ("proxy", "noearn")),  # ETF again, different regime
]
ALL_TK = sorted({t for t, _, _ in TARGETS})
LABELS = ["~1 week", "~2 weeks", "~1 month", "~2 months", "~3 months"]
START = "2010-01-01"; END = "2025-12-15"

print("Downloading ^VIX ...", flush=True)
vr_ = yf.download("^VIX", start="2000-01-01", end=END, auto_adjust=True, progress=False)
VIX = vr_["Close"].squeeze()

print(f"Downloading underlyings {ALL_TK} ...", flush=True)
raw = yf.download(ALL_TK, start=START, end=END, auto_adjust=True, group_by="ticker", threads=True, progress=False)

npass = nfail = 0
paths_seen = set()
for k, (t, dstr, want_path) in enumerate(TARGETS, 1):
    h = raw[t].dropna()
    tgt = pd.Timestamp(dstr)
    idxpos = h.index.searchsorted(tgt, side="right") - 1  # last bar on-or-before target
    if idxpos < 420:
        print(f"[{k:2}] {t} {dstr}: SKIP (insufficient history, idxpos={idxpos})"); continue
    i = idxpos
    label = LABELS[k % len(LABELS)]
    a, path = inline_prompt(t, h.copy(), VIX, i, label)
    sub = h.iloc[:i + 1].copy()
    try:
        b = technical_card(t, sub, VIX, horizon_label=label)
    except Exception as e:
        b = f"<EXC {type(e).__name__}: {e}>"
    if a is None:
        print(f"[{k:2}] {t} {dstr}: SKIP (inline NaN indicators)"); continue
    bar = h.index[i].strftime("%Y-%m-%d")
    if path != want_path:
        print(f"[{k:2}] NOTE  {t} bar={bar}: path {path} != expected {want_path} (data shifted)")
    paths_seen.add(path)
    if a == b:
        npass += 1
        seg = a[a.index("IVR "):a.index(" VIX ")]
        print(f"[{k:2}] PASS  {t} bar={bar} path={path} {label!r}")
        print(f"      ...{seg}")
    else:
        nfail += 1
        print(f"[{k:2}] FAIL  {t} bar={bar} path={path} {label!r}")
        print(f"      A: {a!r}")
        print(f"      B: {b!r}")
        for j, (ca, cb) in enumerate(zip(a, b)):
            if ca != cb:
                print(f"      first diff at char {j}: A={ca!r} B={cb!r} ...{a[max(0,j-15):j+15]!r} vs {b[max(0,j-15):j+15]!r}")
                break
        else:
            print(f"      length diff: len(A)={len(a)} len(B)={len(b)}")

# ----- coverage: every v4 path must have been exercised AND matched -----
REQUIRED = {("true", "earn"), ("true", "noearn"), ("proxy", "earn"), ("proxy", "noearn")}
missing = REQUIRED - paths_seen
print("\n==================================================")
print(f"RESULT: {npass} PASS / {nfail} FAIL  (of {npass+nfail} compared)")
print(f"v4 paths exercised: {sorted(paths_seen)}")
if missing:
    print(f"MISSING v4 PATHS (coverage gap): {sorted(missing)}")
print("==================================================")
sys.exit(0 if (nfail == 0 and npass > 0 and not missing) else 1)
