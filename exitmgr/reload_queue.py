"""Take-profit-and-reload queue + friction gate (2026-07-03).

Encodes Trevor's serial-reload exit style WITHOUT any autonomous action. The flow is a strict
hand-off between two already-existing, already-gated stages:

  1. EXIT side (manager.py): when the MODEL banks a winner (`take_profit`) AND still sees room
     (`reload=true`), the manager forces the normal close. ONLY AFTER that close CONFIRMS `Filled`
     does it write a persisted reload TICKET here (keyed by symbol). It NEVER writes on a resting /
     rejected close -- that is the double-exposure guard.

  2. ENTRY side (trader.py): each cycle, AFTER the existing kill-switch / reconcile-halt /
     exit-fail-streak entry gates, the trader DRAINS ready tickets into synthetic high-priority
     ideas that flow through the SAME gate -> construct -> Slack-approve -> submit path as strategist
     ideas. Trevor approves EVERY trade, so a reload is just another suggested entry -- not an
     auto-fire. Each fresh entry journals its OWN debit + 30% stop, so the basis/stop re-anchor is
     automatic and banked gain can't be given back.

Anti-churn is built in: ONE reload per close (a ticket is consumed exactly once on drain), a TTL so
a stale continuation signal is dropped rather than fired, and a per-name-per-day depth cap. The whole
feature is OFF BY DEFAULT (config `reload_enabled=False`); when off, nothing here is ever called.

Persistence is a small JSON file next to the trades journal -- deliberately SEPARATE from the
critical exit-state (state.json) serialization so a queue bug can never corrupt exit state.
"""
import json
import os
import time
from pathlib import Path
from typing import List, Optional, Tuple


def queue_path(journal_path: str) -> str:
    """Reload-queue JSON path, next to the trades journal. An explicit env override
    ($EXITMGR_RELOAD_QUEUE) lets a test point it at a tmp file; unset => next-to-journal."""
    env = os.environ.get("EXITMGR_RELOAD_QUEUE")
    if env:
        return env
    base = os.path.dirname(journal_path) or "."
    return os.path.join(base, "reload_queue.json")


def make_ticket(*, symbol: str, thesis: str, right: str, width: Optional[float],
                dte_target: Optional[int], structure: str, is_index: bool,
                reload_conviction: Optional[float], realized_pnl: Optional[float],
                original_debit: Optional[float], now_ts: Optional[float] = None,
                ttl_cycles: int = 3, interval_seconds: int = 60) -> dict:
    """Build a reload ticket dict. expires_after_ts = now + ttl_cycles * loop-interval, so a ticket
    that is never drained (market closed / entries halted for a while) is dropped rather than fired
    days later. All fields are plain JSON so the queue file stays trivially serializable."""
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
        "created_ts": now,
        "expires_after_ts": now + ttl,
    }


class ReloadQueue:
    """Persisted, fill-gated reload queue. Load-modify-save on a plain JSON file:
        {"tickets": [ <ticket>, ... ], "depth": {"<trading-day>": {"<SYM>": <count>}}}
    Isolated from state.json on purpose (a queue bug can never touch exit state)."""

    def __init__(self, path: str):
        self.path = path
        self.tickets: List[dict] = []
        self.depth: dict = {}
        self._load()

    def _load(self) -> None:
        try:
            if Path(self.path).exists():
                with open(self.path, "r") as f:
                    data = json.load(f) or {}
                self.tickets = list(data.get("tickets", []) or [])
                self.depth = dict(data.get("depth", {}) or {})
        except Exception as e:  # noqa: BLE001 - a corrupt queue must never break trading; start empty
            print(f"[RELOAD] could not load reload queue {self.path}: {e} (starting empty)")
            self.tickets, self.depth = [], {}

    def save(self) -> None:
        tmp = f"{self.path}.tmp"
        with open(tmp, "w") as f:
            json.dump({"tickets": self.tickets, "depth": self.depth}, f, indent=2)
        os.replace(tmp, self.path)

    def add(self, ticket: dict) -> None:
        """Append a ticket and persist. Called by the manager ONLY after a Filled take-profit close."""
        self.tickets.append(dict(ticket))
        self.save()

    def drain(self, *, today: str, max_per_name: int, now_ts: Optional[float] = None
              ) -> Tuple[List[dict], dict]:
        """Consume the queue for this cycle. Returns (ready_tickets, summary).

        Every current ticket is removed (CONSUME-ONCE): a ticket is either EXPIRED (past its
        expires_after_ts -> dropped), DEPTH-CAPPED (this name already hit max_per_name reloads today
        -> dropped, not retried), or READY (returned for suggestion + counted against the daily
        depth). Depth is tracked per trading-day; older days are pruned. Never raises."""
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
        # Consume-once: after a drain pass no ticket survives (ready were suggested, the rest dropped).
        self.tickets = []
        self.depth = {today: day_depth}  # prune every other trading-day's depth counters
        self.save()
        return ready, {"expired": expired, "capped": capped, "ready": len(ready)}


def reload_friction_ok(*, reload_conviction, conviction_min, tp_pct, new_debit, qty, is_spread,
                       theta_per_share, entry_spread_pct, k,
                       commission_per_contract: float = 0.65,
                       min_slippage_frac: float = 0.005) -> Tuple[bool, str, dict]:
    """Anti-churn friction gate for a reload. A reload clears ONLY if BOTH hold:
      (1) the model's reload_conviction >= conviction_min, AND
      (2) expected continuation (clamped tp% x new debit) exceeds k x total friction, where friction
          = fresh-entry commission + one-cycle theta + entry slippage.

    Pure function (no I/O) so it is trivially unit-testable. tp_pct is in PERCENT units (30.0 == 30%),
    matching construction.clamp_tp_sl output. theta_per_share is the long-leg per-share/day theta
    (usually negative); entry_spread_pct is the bid/ask spread as a percent of mid. Returns
    (ok, reason, detail) -- detail carries the component numbers for the audit trail.
    """
    detail = {}
    try:
        conv = None if reload_conviction is None else float(reload_conviction)
    except (TypeError, ValueError):
        conv = None
    cmin = float(conviction_min)
    if conv is None or conv < cmin:
        return False, f"reload_conviction {conv} < min {cmin:g}", {"reload_conviction": conv,
                                                                    "conviction_min": cmin}
    debit = max(0.0, float(new_debit or 0.0))
    q = max(1, int(qty or 1))
    legs = 2 if is_spread else 1
    commission = float(commission_per_contract) * q * legs
    theta_cost = abs(float(theta_per_share or 0.0)) * 100.0 * q  # ~one cycle (~one day) of theta
    slip_frac = max(float(entry_spread_pct or 0.0) / 100.0, float(min_slippage_frac))
    slippage = slip_frac * debit
    friction = commission + theta_cost + slippage
    expected = (float(tp_pct or 0.0) / 100.0) * debit
    detail = {"expected_continuation": round(expected, 2), "commission": round(commission, 2),
              "theta_cost": round(theta_cost, 2), "slippage": round(slippage, 2),
              "friction": round(friction, 2), "k": float(k), "reload_conviction": conv}
    if expected > float(k) * friction:
        return True, "", detail
    return False, (f"continuation ${expected:,.2f} <= {float(k):g}x friction ${friction:,.2f} "
                   f"(commission ${commission:,.2f} + theta ${theta_cost:,.2f} + slip ${slippage:,.2f})"), detail
