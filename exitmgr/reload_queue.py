"""Public API contract; production-derived narrative omitted."""
import json
import os
import time
from pathlib import Path
from typing import List, Optional, Tuple


def queue_path(journal_path: str) -> str:
    """Public API contract; production-derived narrative omitted."""
    env = os.environ.get("EXITMGR_RELOAD_QUEUE")
    if env:
        return env
    base = os.path.dirname(journal_path) or "."
    return os.path.join(base, "reload_queue.json")


def make_ticket(*, symbol: str, thesis: str, right: str, width: Optional[float],
                dte_target: Optional[int], structure: str, is_index: bool,
                reload_conviction: Optional[float], realized_pnl: Optional[float],
                original_debit: Optional[float], now_ts: Optional[float] = None,
                ttl_cycles: int = 3, interval_seconds: int = 60,
                source_fill_key: Optional[str] = None) -> dict:
    """Public API contract; production-derived narrative omitted."""
    now = time.time() if now_ts is None else now_ts
    ttl = max(1, int(ttl_cycles or 1)) * max(1, int(interval_seconds or 1))
    return {
        "symbol": str(symbol).upper(),
        "thesis": str(thesis or ""),
        "right": (str(right).upper() if right else None),
        "width": (float(width) if width not in (None, "") else None),
        "dte_target": (int(dte_target) if dte_target else None),
        "structure": ("spread" if str(structure).lower() == "spread" or width else "single"),
        "is_index": bool(is_index),
        "reload_conviction": (float(reload_conviction) if reload_conviction is not None else None),
        "realized_pnl": (float(realized_pnl) if realized_pnl is not None else None),
        "original_debit": (float(original_debit) if original_debit is not None else None),
        "source_fill_key": (str(source_fill_key) if source_fill_key else None),
        "created_ts": now,
        "expires_after_ts": now + ttl,
    }


class ReloadQueue:
    """Public API contract; production-derived narrative omitted."""

    def __init__(self, path: str):
        self.path = path
        self.tickets: List[dict] = []
        self.depth: dict = {}
        self.seen_fill_keys: set = set()
        self._load()

    def _load(self) -> None:
        try:
            if Path(self.path).exists():
                with open(self.path, "r") as f:
                    data = json.load(f) or {}
                self.tickets = list(data.get("tickets", []) or [])
                self.depth = dict(data.get("depth", {}) or {})
                self.seen_fill_keys = set(data.get("seen_fill_keys", []) or [])
        except Exception as e:
            print(f"[RELOAD] could not load reload queue {self.path}: {e} (starting empty)")
            self.tickets, self.depth, self.seen_fill_keys = [], {}, set()

    def save(self) -> None:
        tmp = f"{self.path}.tmp"
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"tickets": self.tickets, "depth": self.depth,
                       "seen_fill_keys": sorted(self.seen_fill_keys)}, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
        try:
            dfd = os.open(str(Path(self.path).parent),
                          os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass

    def add_once(self, ticket: dict) -> bool:
        """Public API contract; production-derived narrative omitted."""
        fill_key = ticket.get("source_fill_key")
        if not fill_key:
            print("[RELOAD] ticket refused: source_fill_key missing")
            return False
        fill_key = str(fill_key)
        if fill_key in self.seen_fill_keys:
            return False
        self.tickets.append(dict(ticket))
        self.seen_fill_keys.add(fill_key)
        self.save()
        return True

    def add(self, ticket: dict) -> None:
        """Public API contract; production-derived narrative omitted."""
        if ticket.get("source_fill_key"):
            self.add_once(ticket)
            return
        self.tickets.append(dict(ticket))
        self.save()

    def drain(self, *, today: str, max_per_name: int, now_ts: Optional[float] = None
              ) -> Tuple[List[dict], dict]:
        """Public API contract; production-derived narrative omitted."""
        now = time.time() if now_ts is None else now_ts
        ready: List[dict] = []
        expired = capped = 0
        day_depth = dict(self.depth.get(today, {}))
        try:
            cap = int(max_per_name)
        except (TypeError, ValueError):
            cap = 2
        for t in self.tickets:
            exp = t.get("expires_after_ts")
            if exp is not None and now > float(exp):
                expired += 1
                continue
            sym = str(t.get("symbol", "")).upper()
            if day_depth.get(sym, 0) >= cap:
                capped += 1
                continue
            day_depth[sym] = day_depth.get(sym, 0) + 1
            ready.append(t)

        self.tickets = []
        self.depth = {today: day_depth}
        self.save()
        return ready, {"expired": expired, "capped": capped, "ready": len(ready)}


def reload_friction_ok(*, reload_conviction, conviction_min, expected_continuation_pct,
                       new_debit, qty, is_spread,
                       theta_per_share, entry_spread_pct, k,
                       commission_per_contract: float = 0.65,
                       min_slippage_frac: float = 0.005) -> Tuple[bool, str, dict]:
    """Public API contract; production-derived narrative omitted."""
    detail = {}
    try:
        conv = None if reload_conviction is None else float(reload_conviction)
    except (TypeError, ValueError):
        conv = None
    cmin = float(conviction_min)
    if conv is None or conv < cmin:
        return False, f"reload_conviction {conv} < min {cmin:g}", {"reload_conviction": conv,
                                                                    "conviction_min": cmin}


    try:
        _ecp = None if expected_continuation_pct is None else float(expected_continuation_pct)
    except (TypeError, ValueError):
        _ecp = None
    if _ecp is None or _ecp <= 0.0:
        return False, ("no continuation estimate on ticket (realized_pnl / original_debit missing "
                       "or non-positive) -- cannot judge friction"), {
            "expected_continuation_pct": _ecp, "reload_conviction": conv}

    debit = max(0.0, float(new_debit or 0.0))
    q = max(1, int(qty or 1))
    legs = 2 if is_spread else 1
    commission = float(commission_per_contract) * q * legs
    theta_cost = abs(float(theta_per_share or 0.0)) * 100.0 * q
    slip_frac = max(float(entry_spread_pct or 0.0) / 100.0, float(min_slippage_frac))
    slippage = slip_frac * debit
    friction = commission + theta_cost + slippage
    expected = (_ecp / 100.0) * debit
    detail = {"expected_continuation": round(expected, 2),
              "expected_continuation_pct": round(_ecp, 2),
              "commission": round(commission, 2),
              "theta_cost": round(theta_cost, 2), "slippage": round(slippage, 2),
              "friction": round(friction, 2), "k": float(k), "reload_conviction": conv}
    if expected > float(k) * friction:
        return True, "", detail
    return False, (f"continuation ${expected:,.2f} <= {float(k):g}x friction ${friction:,.2f} "
                   f"(commission ${commission:,.2f} + theta ${theta_cost:,.2f} + slip ${slippage:,.2f})"), detail
