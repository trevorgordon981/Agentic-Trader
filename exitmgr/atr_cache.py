"""Public API contract; production-derived narrative omitted."""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
from datetime import date, datetime, timedelta
from typing import Iterable, Optional

CACHE_PATH = os.environ.get("EXITMGR_ATR_CACHE") or os.path.expanduser(
    "~/.local/var/exitmgr/atr-cache.json")




MAX_AGE_DAYS = 6

ATR_PERIOD = 14


def _load() -> dict:
    try:
        with open(CACHE_PATH) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def read(symbol: str, max_age_days: int = MAX_AGE_DAYS,
         today: Optional[date] = None) -> Optional[dict]:
    """Public API contract; production-derived narrative omitted."""
    if not symbol:
        return None
    rec = _load().get(str(symbol).upper())
    if not isinstance(rec, dict):
        return None
    try:
        atr, spot = float(rec["atr"]), float(rec["spot"])
        asof = datetime.strptime(str(rec["asof"])[:10], "%Y-%m-%d").date()
    except Exception:
        return None
    if atr != atr or spot != spot or atr <= 0 or spot <= 0:
        return None
    ref = today or date.today()
    if (ref - asof).days > int(max_age_days):
        return None
    return {"atr": atr, "spot": spot, "asof": asof.isoformat()}


def refresh(symbols: Iterable[str], period: str = "1y") -> dict:
    """Public API contract; production-derived narrative omitted."""
    import pandas as pd
    import yfinance as yf




    updates = {}
    ok, failed = [], []
    for sym in sorted({str(s).upper() for s in symbols if s}):
        try:
            df = yf.download(sym, period=period, interval="1d", progress=False,
                             auto_adjust=False)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            c, hi, lo = df["Close"], df["High"], df["Low"]
            tr = pd.concat([hi - lo, (hi - c.shift()).abs(), (lo - c.shift()).abs()],
                           axis=1).max(axis=1)
            atr = float(tr.rolling(ATR_PERIOD).mean().iloc[-1])
            spot = float(c.iloc[-1])
            asof = df.index[-1].date().isoformat()
            if atr != atr or atr <= 0 or spot <= 0:
                raise ValueError("non-positive atr/spot")
            updates[sym] = {"atr": atr, "spot": spot, "asof": asof,
                            "period": ATR_PERIOD}
            ok.append(sym)
        except Exception as e:
            failed.append("%s (%s)" % (sym, e))
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    lock_path = CACHE_PATH + ".lock"
    with open(lock_path, "a+") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        out = _load()
        out.update(updates)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(CACHE_PATH), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(out, fh, indent=1, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, CACHE_PATH)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    return {"ok": ok, "failed": failed, "path": CACHE_PATH}


def symbols_from_journal(journal_path: str = "trades.log") -> list:
    """Public API contract; production-derived narrative omitted."""
    syms = set()
    try:
        with open(journal_path) as fh:
            for line in fh:
                try:
                    j = json.loads(line)
                except Exception:
                    continue
                if j.get("symbol"):
                    syms.add(str(j["symbol"]).upper())
    except Exception:
        pass
    return sorted(syms)


if __name__ == "__main__":
    import sys

    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    syms = args or symbols_from_journal()
    res = refresh(syms)
    print("[atr-cache] wrote %s" % res["path"])
    print("[atr-cache] ok(%d): %s" % (len(res["ok"]), " ".join(res["ok"])))
    if res["failed"]:
        print("[atr-cache] FAILED(%d): %s" % (len(res["failed"]), "; ".join(res["failed"])))
