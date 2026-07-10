"""Crash-safe state persistence and reconciliation logic."""

import json
import os
import shutil
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from typing import Optional, Dict, List
from pathlib import Path


@dataclass
class InFlightClose:
    """Durable record of a pending close order for a contract.

    ``exit_context`` is deliberately persisted with the order.  A close can fill after the
    placement cycle (or after this process restarts); without the entry snapshot and trigger
    metadata the later fill cannot be turned into an honest realized-P&L record.  The default
    keeps old state files and tests backward compatible.
    """
    con_id: int
    order_id: int  # IB order id (0 if not yet placed)
    remaining_qty: int  # contracts still to be closed
    entry_debit: float  # total dollars paid at entry (for P&L calc)
    order_price: Optional[float] = None  # limit price if order placed
    placed_at: Optional[str] = None  # ISO timestamp
    exit_context: Dict[str, object] = field(default_factory=dict)
    # IB order ids are scoped to a client session and can be reused.  Persist the rest of the
    # broker identity so restart reconciliation and realized-P&L dedupe never join an unrelated
    # order that happens to carry the same numeric orderId.
    perm_id: int = 0
    # ``None`` means a pre-identity legacy record.  IB clientId=0 is legitimate (TWS/manual) and
    # must remain distinguishable from unknown, otherwise orderId-only fallback can cross sessions.
    client_id: Optional[int] = None
    order_ref: Optional[str] = None
    identity_version: int = 0
    # ``intent`` is fsynced before transmission; ``submitted`` is bound to the Trade returned by
    # IB.  This closes the crash window where a live order previously existed with no durable
    # finalization context at all.
    placement_state: str = "submitted"
    fill_key: Optional[str] = None
    # Durable checkpoints for non-ledger side effects.  Reload tickets have their own fill-key
    # dedupe; alerts use a deterministic Slack client_msg_id.  These checkpoints keep a restart
    # replay from repeating either action after the ledger commit.
    side_effects: Dict[str, bool] = field(default_factory=dict)


@dataclass
class DailyStats:
    """Daily aggregate statistics."""
    date: str  # YYYY-MM-DD
    orders_placed: int = 0        # CLOSING orders placed (exit path); feeds the exit-side day cap
    notional_closed: float = 0.0  # notional closed (exit path)
    # ENTRY-side day aggregates (2026-07-03 gap-fix). Tracked SEPARATELY from the exit fields so the
    # entry throttle (caps.max_orders_per_day / max_notional_per_day on NEW entries) is independent
    # of the exit-side cap -- an exit is a protective action and must never be blocked by entry
    # activity, and vice-versa. Default 0.0/0 so pre-existing state files load unchanged.
    orders_opened: int = 0        # NEW entry orders submitted today
    notional_opened: float = 0.0  # entry notional (limit*100*qty) opened today


@dataclass
class State:
    """Persisted state for crash-safe operation."""
    in_flight: Dict[str, InFlightClose] = field(default_factory=dict)  # key: con_id as str
    daily_stats: Dict[str, DailyStats] = field(default_factory=dict)  # key: date str
    last_cycle: Optional[str] = None  # ISO timestamp of last evaluation cycle
    peak_prices: Dict[str, float] = field(default_factory=dict)  # con_id(str)->peak mark
    mfe_pct: Dict[str, float] = field(default_factory=dict)  # con_id(str)->max favorable excursion %
                                                             # (persisted so audits never reconstruct
                                                             # MFE from netliq again; 2026-07-01)
    # Per-trade excursion capture (2026-07-02) -- all persisted with the state so a process
    # bounce never loses the running excursions/path. RECORD-ONLY; nothing here feeds a trading
    # decision. MAE is the field the exits corpus never had; the mark_path is a per-cycle
    # mark-to-market time series purpose-built for fine-tuning/retro-ing exits.
    mae_pct: Dict[str, float] = field(default_factory=dict)   # con_id(str)->max ADVERSE excursion %
    mfe_ts: Dict[str, str] = field(default_factory=dict)      # con_id(str)->ISO ts the MFE peak was set
    mae_ts: Dict[str, str] = field(default_factory=dict)      # con_id(str)->ISO ts the MAE trough was set
    mark_path: Dict[str, List[dict]] = field(default_factory=dict)  # con_id(str)->[{ts,price,value,pnl_pct,underlying}]
    # SCALE-OUT (2026-07-02): con_id(str)->True once a position has had its partial trim, so the
    # scale_out rule never re-fires on the runner (rules.evaluate_position(already_trimmed=...)).
    # Persisted so a process bounce doesn't forget a trim and re-trim the runner. Backward-compatible:
    # an old state file with no `scaled_out` key loads as empty (no position considered trimmed).
    scaled_out: Dict[str, bool] = field(default_factory=dict)
    # TRAIL-ARMED (2026-07-03 Part 1): con_id(str)->True once the MODEL has armed a trailing stop
    # for this position (a decision arm_trail). Persisted so the take-profit CEILING stays
    # SUPPRESSED across SUBSEQUENT cycles (incl. plain 'hold') for the life of the position --
    # otherwise the fixed pot-tier profit_target would snap back and force-close (clip) a runner the
    # model chose to let RUN. Cleared on close via prune_tracking. Backward-compatible: an old state
    # file with no `trail_armed` key loads as {} (nothing armed). Gates ONLY the take-profit side;
    # never the protective stop.
    trail_armed: Dict[str, bool] = field(default_factory=dict)

    def record_mark(self, con_id: int, current_price: float, entry_debit: Optional[float],
                    quantity: int, ts: Optional[str] = None, path_cap: int = 5000,
                    enrich: Optional[dict] = None) -> Optional[float]:
        """Record ONE mark-to-market observation for an OPEN position each exit cycle:
        update the peak price, the MFE (max favorable %) and MAE (max adverse %) excursions
        with their timestamps, and append to the bounded mark path. RECORD-ONLY -- pure
        bookkeeping on the persisted state, never touches IBKR or any trading decision.
        Returns the current excursion % (or None if entry_debit is unusable).

        `enrich` (2026-07-02 v2 path enrichment): an OPTIONAL dict of extra per-mark context
        merged onto the appended mark entry (record-only) -- e.g. underlying, iv, delta,
        gamma, theta, vega, dte, days_held, dist_to_tp_pct, dist_to_sl_pct, and the
        position-manager LLM assessment (mgmt_action/mgmt_reason) produced that cycle. Only
        NON-None keys are merged, so a missing greek/feed never clobbers with null; unknown
        keys are accepted verbatim. Fully backward-compatible: callers that omit `enrich`
        get exactly the v1 mark shape."""
        k = str(con_id)
        if ts is None:
            ts = datetime.now().astimezone().isoformat()
        # peak price feeds the trailing-stop rule; keep it monotonic-up as before
        if k not in self.peak_prices or current_price > self.peak_prices[k]:
            self.peak_prices[k] = current_price
        exc = None
        try:
            ed = float(entry_debit) if entry_debit is not None else None
        except (TypeError, ValueError):
            ed = None
        if ed and ed > 0:
            exc = round((current_price * 100 * quantity - ed) / ed * 100, 2)
            if k not in self.mfe_pct or exc > self.mfe_pct[k]:
                self.mfe_pct[k] = exc
                self.mfe_ts[k] = ts
            if k not in self.mae_pct or exc < self.mae_pct[k]:
                self.mae_pct[k] = exc
                self.mae_ts[k] = ts
            mp = self.mark_path.setdefault(k, [])
            if len(mp) < path_cap:
                mark = {"ts": ts, "price": round(current_price, 4),
                        "value": round(current_price * 100 * quantity, 2),
                        "pnl_pct": exc, "underlying": None}
                if enrich:
                    try:
                        for kk, vv in enrich.items():
                            if vv is not None:
                                mark[kk] = vv
                    except Exception:
                        pass   # record-only: enrichment can never break the mark append
                mp.append(mark)
        return exc

    def prune_tracking(self, active_con_ids) -> None:
        """Drop per-position excursion/mark tracking for contracts that are no longer active
        (not live at the broker AND not in the journal). RECORD-ONLY housekeeping so the
        persisted state / mark paths stay bounded across closed trades. A closed trade's
        dataset record is written at close (while it is still live), so this never drops path
        data still needed."""
        active = {str(c) for c in (active_con_ids or [])}
        for d in (self.peak_prices, self.mfe_pct, self.mae_pct,
                  self.mfe_ts, self.mae_ts, self.mark_path, self.scaled_out,
                  self.trail_armed):
            for kk in [k for k in d if k not in active]:
                del d[kk]

    def get_in_flight(self, con_id: int) -> Optional[InFlightClose]:
        return self.in_flight.get(str(con_id))

    def add_in_flight(self, close: InFlightClose) -> None:
        self.in_flight[str(close.con_id)] = close

    def remove_in_flight(self, con_id: int) -> None:
        if str(con_id) in self.in_flight:
            del self.in_flight[str(con_id)]

    def update_daily_stats(self, date_str: str, order_count: int, notional: float) -> None:
        if date_str not in self.daily_stats:
            self.daily_stats[date_str] = DailyStats(date=date_str)
        stats = self.daily_stats[date_str]
        stats.orders_placed += order_count
        stats.notional_closed += notional

    def update_daily_open_stats(self, date_str: str, order_count: int, notional: float) -> None:
        """Accrue ENTRY-side day aggregates (2026-07-03 gap-fix). Mirror of update_daily_stats but
        for NEW entries, kept separate so the entry throttle never blocks exits and vice-versa."""
        if date_str not in self.daily_stats:
            self.daily_stats[date_str] = DailyStats(date=date_str)
        stats = self.daily_stats[date_str]
        stats.orders_opened += order_count
        stats.notional_opened += notional


class StateManager:
    """Manages crash-safe state persistence using atomic writes."""

    def __init__(self, state_path: str):
        self.state_path = Path(state_path)
        self._state: Optional[State] = None

    @property
    def state(self) -> State:
        if self._state is None:
            self._state = self._load()
        return self._state

    def _load(self) -> State:
        """Load state from disk, or return empty state if file doesn't exist."""
        if not self.state_path.exists():
            return State()
        try:
            with open(self.state_path, "r") as f:
                data = json.load(f)

            # Reconstruct in_flight dict with InFlightClose objects
            in_flight = {}
            for con_id_str, close_data in data.get("in_flight", {}).items():
                in_flight[con_id_str] = InFlightClose(**close_data)

            # Reconstruct daily_stats dict
            daily_stats = {}
            for date_str, stats_data in data.get("daily_stats", {}).items():
                daily_stats[date_str] = DailyStats(**stats_data)

            return State(
                in_flight=in_flight,
                daily_stats=daily_stats,
                last_cycle=data.get("last_cycle"),
                peak_prices=data.get("peak_prices", {}),
                mfe_pct=data.get("mfe_pct", {}),
                mae_pct=data.get("mae_pct", {}),
                mfe_ts=data.get("mfe_ts", {}),
                mae_ts=data.get("mae_ts", {}),
                mark_path=data.get("mark_path", {}),
                scaled_out=data.get("scaled_out", {}),  # backward-compatible: absent -> {}
                trail_armed=data.get("trail_armed", {}),  # backward-compatible: absent -> {}
            )
        except (json.JSONDecodeError, TypeError, KeyError) as e:
            # Corrupted state file - treat as empty but log
            print(f"[WARN] Could not parse state file {self.state_path}: {e}. Starting fresh.")
            return State()

    def save(self) -> None:
        """Atomically write state to disk using temp file + rename."""
        # Ensure parent directory exists
        self.state_path.parent.mkdir(parents=True, exist_ok=True)

        # Serialize to JSON
        data = {
            "in_flight": {k: asdict(v) for k, v in self.state.in_flight.items()},
            "daily_stats": {k: asdict(v) for k, v in self.state.daily_stats.items()},
            "last_cycle": self.state.last_cycle,
            "peak_prices": self.state.peak_prices,
            "mfe_pct": self.state.mfe_pct,
            "mae_pct": self.state.mae_pct,
            "mfe_ts": self.state.mfe_ts,
            "mae_ts": self.state.mae_ts,
            "mark_path": self.state.mark_path,
            "scaled_out": self.state.scaled_out,
            "trail_armed": self.state.trail_armed,
        }

        # Write + fsync the temp file, atomically replace, then fsync the directory.  A rename
        # without those durability barriers can acknowledge exit_context and still lose it on a
        # power loss, defeating restart-time fill finalization. State contains account/trade data,
        # so keep both the temp and final file owner-only.
        temp_path = self.state_path.with_suffix(".tmp")
        fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, self.state_path)
        try:
            dfd = os.open(self.state_path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            # Directory fsync is unavailable on some non-POSIX test filesystems; the atomic file
            # replacement still holds there.
            pass

    def update_last_cycle(self) -> None:
        """Update last cycle timestamp and persist."""
        self.state.last_cycle = datetime.utcnow().isoformat()
        self.save()


def reconcile_state(
    state: State,
    live_positions: Dict[int, dict],  # con_id -> position data (qty, avg_cost)
    live_open_orders: Dict[int, dict],  # con_id -> order data (order_id, remaining_qty)
    journal_entries: Dict[int, dict],  # con_id -> journal entry (debit)
    journal_qtys: Optional[Dict[int, int]] = None,  # con_id -> entered quantity (2026-07-03)
    detail: Optional[dict] = None,  # OUT (2026-07-09): filled with the SPECIFIC con_ids that are
                                    # `inconsistent` (block ONLY these exits) and `closed` (fully
                                    # closed this pass, caller purges tracking). Ignored if None, so
                                    # every existing `safe, alerts = reconcile_state(...)` caller is
                                    # byte-for-byte unaffected.
) -> tuple[bool, List[str]]:
    """
    Reconcile persisted in-flight state against live broker positions and open orders.

    Returns:
        (safe_to_proceed, alerts). If safe_to_proceed is False, caller must NOT take any action.

    Reconciliation rules:
    1. If in_flight record exists but no live position and no live order -> position was closed (fill event),
       remove in_flight and continue (OK).
    2. If in_flight record exists and live order exists -> reconcile quantities (OK if consistent).
    3. If in_flight record exists and NO live order but position still exists -> order was cancelled/failed,
       keep in_flight but alert (need manual intervention?).
    4. If in_flight record exists and NO live order and NO position -> fully filled (OK).
    5. If in_flight record exists and live position qty < in_flight remaining_qty -> partial fill (OK).
    6. If in_flight record exists and live position qty > in_flight remaining_qty -> inconsistency (ABORT).
    7. If live position exists but NOT in journal (under journal scope) and NOT in in_flight -> ABORT (unexpected).
    8. If live order exists for contract NOT in in_flight -> ABORT (unexpected order).
    """
    alerts: List[str] = []
    safe = True
    journal_qtys = journal_qtys or {}
    # C1a/C3 (2026-07-09): the SPECIFIC con_ids that are inconsistent (caller blocks ONLY these,
    # so one manual TWS position no longer halts every automated stop) and the ones fully closed
    # this pass (caller purges their per-contract tracking + journal entry).
    inconsistent_con_ids: set = set()
    closed_con_ids: set = set()

    def _pos_consistent_with_order(con_id: int, order_remaining) -> bool:
        """A live position qty that equals (journaled entry qty - order remaining) is the EXPECTED
        state of a close still working -- e.g. entered 5, a close for 5 has filled 2 so 3 remain on
        the order and 2 contracts are still held... no: while 3 remain to close, 3 are still held.
        Concretely: contracts still HELD == contracts the order still has to close == remaining, and
        contracts already closed == journal_qty - remaining. So position qty == remaining, which
        also equals journal_qty - (journal_qty - remaining). We accept EITHER equality as consistent
        so a partial-close-in-progress is never mislabeled an inconsistency (2026-07-03)."""
        jq = journal_qtys.get(con_id)
        pq = (live_positions.get(con_id) or {}).get("qty")
        if jq is None or pq is None or order_remaining is None:
            return False
        try:
            jq = int(jq); pq = int(pq); r = int(order_remaining)
        except (TypeError, ValueError):
            return False
        return pq == r or pq == jq - r

    def _client_id(value):
        if isinstance(value, bool):
            return None
        if isinstance(value, int) and value >= 0:
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
        return None

    def _same_order_identity(in_flight: InFlightClose, live_order: dict) -> bool:
        """Match the strongest broker identity available, never orderId across clients blindly."""
        iperm = int(getattr(in_flight, "perm_id", 0) or 0)
        lperm = int(live_order.get("perm_id", 0) or 0)
        if iperm and lperm:
            return iperm == lperm
        iref = getattr(in_flight, "order_ref", None)
        lref = live_order.get("order_ref")
        if iref and lref:
            return str(iref) == str(lref)
        ioid = int(getattr(in_flight, "order_id", 0) or 0)
        loid = int(live_order.get("order_id", 0) or 0)
        iclient = _client_id(getattr(in_flight, "client_id", None))
        lclient = _client_id(live_order.get("client_id"))
        strong_client = bool(getattr(in_flight, "identity_version", 0) >= 1
                             or iclient not in (None, 0))
        if strong_client:
            return bool(ioid and loid and ioid == loid
                        and lclient is not None and iclient == lclient)
        # Backward compatibility for pre-identity state files.  New placements always have an
        # orderRef/client id and therefore never take this weaker branch.
        return bool(ioid and loid and ioid == loid)

    # Build sets
    in_flight_con_ids = set(int(k) for k in state.in_flight.keys())
    live_position_con_ids = set(live_positions.keys())
    live_order_con_ids = set(live_open_orders.keys())
    journal_con_ids = set(journal_entries.keys())

    # Check 1: in_flight but no position and no order -> a terminal close CANDIDATE.  Context-rich
    # records must remain durable until ExitManager confirms ``Filled`` and writes the realized
    # record.  Removing them here used to discard the only order/entry/trigger linkage before the
    # asynchronous fill poll ran (and made restart-time fills impossible to finalize).  Legacy
    # context-free records retain the historical cleanup behavior because there is nothing the
    # finalizer could reconstruct from them.
    for con_id in in_flight_con_ids:
        if con_id not in live_position_con_ids and con_id not in live_order_con_ids:
            in_flight = state.get_in_flight(con_id)
            if in_flight is not None and in_flight.exit_context:
                alerts.append(
                    f"[INFO] Position con_id={con_id} and close order are no longer live; "
                    f"retaining in_flight until the terminal fill is confirmed and finalized."
                )
            else:
                alerts.append(
                    f"[INFO] Position con_id={con_id} fully closed (legacy fill event), "
                    f"removing context-free in_flight."
                )
                state.remove_in_flight(con_id)
                closed_con_ids.add(con_id)

    # Check 2 & 3: in_flight with position but no order
    for con_id in in_flight_con_ids:
        if con_id in live_position_con_ids and con_id not in live_order_con_ids:
            # Order missing but position still there - could be cancelled/failed
            in_flight = state.get_in_flight(con_id)
            live_qty = live_positions[con_id].get("qty", 0)
            if in_flight and in_flight.remaining_qty != live_qty:
                # Quantities don't match - inconsistency
                alerts.append(
                    f"[ERROR] con_id={con_id}: in_flight remaining_qty={in_flight.remaining_qty}, "
                    f"live position qty={live_qty}. Cannot reconcile safely."
                )
                safe = False
                inconsistent_con_ids.add(con_id)
            elif in_flight and in_flight.exit_context:
                # Do not discard the only basis/trigger/order snapshot before ExitManager asks
                # IBKR's completed-order/execution stores. The order may have filled between the
                # positions and open-orders snapshots. A confirmed terminal cancellation is
                # released by the fill poll so the close can retry.
                alerts.append(
                    f"[INFO] con_id={con_id}: context-rich close order no longer live while "
                    f"position remains; retaining in_flight pending terminal broker status."
                )
            else:
                # Order vanished (cancelled/expired) but position intact and quantities agree:
                # clear the stale in_flight so exits are re-evaluated/re-protected next cycle
                # (idempotency vs live open orders still prevents a double-close).
                alerts.append(
                    f"[INFO] con_id={con_id}: close order no longer live but position open; "
                    f"clearing stale in_flight so exits are re-evaluated."
                )
                state.remove_in_flight(con_id)

    # Check 4: in_flight with order - reconcile quantities
    for con_id in in_flight_con_ids:
        if con_id in live_order_con_ids:
            in_flight = state.get_in_flight(con_id)
            live_order = live_open_orders[con_id]
            live_order_id = live_order.get("order_id", 0)
            live_remaining = live_order.get("remaining", 0)

            if in_flight and not _same_order_identity(in_flight, live_order):
                alerts.append(
                    f"[ERROR] con_id={con_id}: durable close identity mismatch with the live "
                    f"order (stored order_id={in_flight.order_id}, live order_id={live_order_id}). "
                    f"Cannot reconcile safely."
                )
                safe = False
                inconsistent_con_ids.add(con_id)
            elif in_flight:
                # Bind a prepared intent (or enrich an older submitted record) to the live Trade's
                # full identity.  This update is persisted by the caller after reconciliation.
                in_flight.order_id = int(live_order_id or in_flight.order_id or 0)
                in_flight.perm_id = int(live_order.get("perm_id", 0)
                                        or getattr(in_flight, "perm_id", 0) or 0)
                live_client = _client_id(live_order.get("client_id"))
                if live_client is not None:
                    in_flight.client_id = live_client
                in_flight.order_ref = (live_order.get("order_ref")
                                       or getattr(in_flight, "order_ref", None))
                in_flight.placement_state = "submitted"
                if (in_flight.perm_id or in_flight.order_ref
                        or in_flight.client_id is not None):
                    in_flight.identity_version = 1

            if in_flight and in_flight.remaining_qty != live_remaining:
                # Partial fill or quantity mismatch
                if in_flight.remaining_qty > live_remaining:
                    # Some contracts closed - update in_flight
                    alerts.append(
                        f"[INFO] con_id={con_id}: partial fill detected, "
                        f"remaining_qty updated from {in_flight.remaining_qty} to {live_remaining}."
                    )
                    in_flight.remaining_qty = live_remaining
                elif _pos_consistent_with_order(con_id, live_remaining):
                    # CONSISTENT (2026-07-03): the live position matches (journal qty - order
                    # remaining) -- a legitimate close-in-progress, not an inconsistency. Sync the
                    # in_flight to the live order and continue instead of aborting.
                    alerts.append(
                        f"[INFO] con_id={con_id}: in_flight remaining_qty={in_flight.remaining_qty} "
                        f"< live order remaining={live_remaining}, but the live position is consistent "
                        f"with (journal qty - remaining); syncing in_flight and continuing."
                    )
                    in_flight.remaining_qty = live_remaining
                else:
                    alerts.append(
                        f"[ERROR] con_id={con_id}: in_flight remaining_qty={in_flight.remaining_qty}, "
                        f"live order remaining={live_remaining}. Cannot reconcile safely."
                    )
                    safe = False
                    inconsistent_con_ids.add(con_id)

    # Check 5: live position NOT in journal and NOT in in_flight -- an unexpected position (e.g. a
    # manual TWS buy). C1b (2026-07-09): this used to ABORT the WHOLE reconcile (safe=False), which
    # blocked EVERY automated stop account-wide over one untracked position. A position we have never
    # touched carries NO double-order risk UNLESS a live order is already resting on it. So: if there
    # is a live order on this con_id (potential double-action), keep it FATAL + inconsistent; if there
    # is NONE, downgrade to a WARN (do not set safe=False) so clean positions still get their stops.
    for con_id in live_position_con_ids:
        if con_id not in in_flight_con_ids and con_id not in journal_con_ids:
            if con_id in live_order_con_ids:
                alerts.append(
                    f"[ERROR] con_id={con_id}: live position exists but NOT in journal and NOT in "
                    f"in_flight, AND a live order rests on it. Double-order risk -- aborting for safety."
                )
                safe = False
                inconsistent_con_ids.add(con_id)
            else:
                alerts.append(
                    f"[WARN] con_id={con_id}: unexpected live position (not in journal / in_flight), "
                    f"but no in-flight or live order on it -- no double-order risk. Not treating as fatal; "
                    f"clean positions are still protected."
                )

    # Check 6: live order NOT in in_flight
    for con_id in live_order_con_ids:
        if con_id in in_flight_con_ids:
            continue
        # ORDER-WITHOUT-POSITION (2026-07-03): a resting order for a con_id we hold NO live
        # position in is NOT a reconcile-fatal inconsistency -- there is no position at risk of a
        # double action. It is the tail of a close that already flattened the position (the close/
        # liquidate tool on another clientId, or our own close mid-settle), or an entry BUY not yet
        # filled. WARN, don't abort. A truly unknown order that DOES sit on a live position we hold
        # still aborts below (that is the dangerous case).
        if con_id not in live_position_con_ids:
            alerts.append(
                f"[WARN] con_id={con_id}: live order exists with NO live position "
                f"(order-without-position: a close finishing/cancelling or an unfilled entry). "
                f"Not treating as fatal."
            )
            continue
        alerts.append(
            f"[ERROR] con_id={con_id}: live order exists but NOT in in_flight. "
            f"Cannot reconcile safely. Aborting for safety."
        )
        safe = False
        inconsistent_con_ids.add(con_id)

    if detail is not None:
        detail["inconsistent"] = inconsistent_con_ids
        detail["closed"] = closed_con_ids
    return safe, alerts
