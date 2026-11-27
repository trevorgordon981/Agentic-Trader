"""Fail-closed swing execution coordinator wiring signals, ledger, and protective stops.

Validates explicit armed config files, domain caps, kill-switch status, and
persisted ledger integrity BEFORE calling any broker method, and reconciles
previously submitted working/partially-filled entries before admitting new
proposals.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional, Sequence

from swing_executor.graduation import format_entry_alert, format_exit_alert
from swing_executor.ledger import (
    AccountCashState,
    AccountMismatchError,
    LedgerCorruptionError,
    ReservationError,
    SlotLedger,
    SlotReservation,
    round_tick,
)
from swing_executor.signals import PriceBar, SignalProposal, SwingSignalProvider
from swing_executor.stops import (
    BrokerOrder,
    BrokerStopClient,
    ProtectiveStopEngine,
    SimulatedBroker,
    StopActionResult,
    TERMINAL_STATUSES,
    WORKING_STATUSES,
    _held_qty_for_symbol,
    count_other_working_close_qty,
    is_owned_stop_order,
)


class CoordinatorConfigError(ValueError):
    """Raised when coordinator configuration violates domain or armed-startup safety rules."""


@dataclass(frozen=True)
class CoordinatorConfig:
    """Validated runtime configuration for ``SwingExecutionCoordinator``."""

    account: str
    client_id: int
    db_path: Path
    kill_switch_path: Path
    config_path: Optional[Path] = None
    arm: bool = False
    max_concurrent_slots: int = 4
    max_slot_exposure_fraction: float = 0.20
    max_deployed_fraction: float = 0.80
    max_sector_fraction: float = 0.40
    cash_buffer_fraction: float = 0.02
    max_orders_per_day: int = 10
    max_notional_per_day: float = 50000.0
    sector_groups: Mapping[str, Sequence[str]] = field(default_factory=dict)


def validate_coordinator_config(cfg: CoordinatorConfig) -> CoordinatorConfig:
    """Validate config with fail-closed armed-startup and domain checks."""
    if not isinstance(cfg.account, str) or not cfg.account.strip() or cfg.account != cfg.account.strip():
        raise CoordinatorConfigError("account must be a non-empty trimmed string")
    if isinstance(cfg.client_id, bool) or not isinstance(cfg.client_id, int) or cfg.client_id < 0:
        raise CoordinatorConfigError(f"client_id must be a non-negative integer, got {cfg.client_id!r}")

    if cfg.arm:
        if cfg.config_path is None:
            raise CoordinatorConfigError("armed startup requires an explicit config_path")
        cfg_p = Path(cfg.config_path)
        if not cfg_p.is_file():
            raise CoordinatorConfigError(f"armed startup config_path does not exist: {cfg_p}")

    for name, val in (
        ("max_concurrent_slots", cfg.max_concurrent_slots),
        ("max_orders_per_day", cfg.max_orders_per_day),
    ):
        if isinstance(val, bool) or not isinstance(val, int) or val <= 0:
            raise CoordinatorConfigError(f"{name} must be a positive integer, got {val!r}")

    if (
        isinstance(cfg.max_notional_per_day, bool)
        or not isinstance(cfg.max_notional_per_day, (int, float))
        or not math.isfinite(cfg.max_notional_per_day)
        or cfg.max_notional_per_day <= 0.0
    ):
        raise CoordinatorConfigError(f"max_notional_per_day must be finite and > 0, got {cfg.max_notional_per_day!r}")

    for name, val in (
        ("max_slot_exposure_fraction", cfg.max_slot_exposure_fraction),
        ("max_deployed_fraction", cfg.max_deployed_fraction),
        ("max_sector_fraction", cfg.max_sector_fraction),
    ):
        if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val) or not (0.0 < val <= 1.0):
            raise CoordinatorConfigError(f"{name} must be finite in (0, 1], got {val!r}")

    if (
        isinstance(cfg.cash_buffer_fraction, bool)
        or not isinstance(cfg.cash_buffer_fraction, (int, float))
        or not math.isfinite(cfg.cash_buffer_fraction)
        or not (0.0 <= cfg.cash_buffer_fraction < 1.0)
    ):
        raise CoordinatorConfigError(f"cash_buffer_fraction must be finite in [0, 1), got {cfg.cash_buffer_fraction!r}")

    return cfg


@dataclass
class ReconciliationReport:
    """Structured summary of deferred/async working-entry reconciliation."""

    session_day: str
    checked_reservations: int = 0
    reconciled_fills: list[dict[str, object]] = field(default_factory=list)
    reconciled_cancellations: list[dict[str, object]] = field(default_factory=list)
    stops_armed: list[StopActionResult] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    uncertain_holdings: bool = False
    uncertain_symbols: list[str] = field(default_factory=list)
    broker_read_failed: bool = False
    errors: list[dict[str, object]] = field(default_factory=list)
    blocked_reason: Optional[str] = None


@dataclass
class CycleReport:
    """Structured summary of one coordinator evaluation cycle."""

    session_day: str
    proposals_seen: int = 0
    reserved: list[str] = field(default_factory=list)
    filled: list[dict[str, object]] = field(default_factory=list)
    reconciled_fills: list[dict[str, object]] = field(default_factory=list)
    reconciled_cancellations: list[dict[str, object]] = field(default_factory=list)
    rejected: list[dict[str, str]] = field(default_factory=list)
    stops_armed: list[StopActionResult] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    uncertain_holdings: bool = False
    uncertain_symbols: list[str] = field(default_factory=list)
    blocked_reason: Optional[str] = None


def _is_valid_positive_price(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and float(value) > 0.0
    )


def _match_order_to_reservation(
    orders: Sequence[BrokerOrder],
    res: SlotReservation,
) -> tuple[Optional[BrokerOrder], Optional[str]]:
    """Find a unique broker order matching ``res``; fail closed on identity conflict."""
    sym_u = res.symbol.upper()
    side_u = res.side.upper()
    ref_s = str(res.order_ref or "").strip()

    # Check for cross-account or cross-symbol identity conflicts first.
    for order in orders:
        o_ref = str(order.order_ref or "").strip()
        same_ref = bool(ref_s and o_ref and o_ref == ref_s)
        same_perm = bool(res.perm_id is not None and res.perm_id > 0 and order.perm_id > 0 and order.perm_id == res.perm_id)
        if same_ref or same_perm:
            if (
                order.account != res.account
                or order.symbol.upper() != sym_u
                or order.action.upper() != side_u
            ):
                return (
                    None,
                    f"order_identity_mismatch(account={order.account!r},symbol={order.symbol!r},action={order.action!r})",
                )

    matches: list[BrokerOrder] = []
    seen_keys: set[tuple] = set()
    for order in orders:
        if order.account != res.account or order.symbol.upper() != sym_u or order.action.upper() != side_u:
            continue
        o_ref = str(order.order_ref or "").strip()
        matched = False
        if res.perm_id is not None and res.perm_id > 0 and order.perm_id > 0:
            matched = order.perm_id == res.perm_id
        elif ref_s and o_ref:
            matched = o_ref == ref_s
        elif res.order_id is not None and res.order_id > 0 and order.order_id > 0:
            matched = order.order_id == res.order_id and order.client_id == res.client_id
        if not matched:
            continue
        key: tuple = (
            ("perm", order.perm_id)
            if order.perm_id > 0
            else (("acct_cid_oid", order.account, order.client_id, order.order_id) if order.order_id > 0 else ("obj", id(order)))
        )
        if key not in seen_keys:
            seen_keys.add(key)
            matches.append(order)

    if len(matches) > 1:
        return None, f"multiple_matching_broker_orders({len(matches)})"
    return (matches[0] if matches else None), None


class SwingExecutionCoordinator:
    """Coordinates proposal admission, deferred-fill reconciliation, slot reservation, and stop arming."""

    def __init__(
        self,
        *,
        config: CoordinatorConfig,
        signal_provider: SwingSignalProvider,
        broker: BrokerStopClient,
        broker_timeout_seconds: float = 6.0,
    ) -> None:
        self.config = validate_coordinator_config(config)
        self.signal_provider = signal_provider
        self.broker = broker
        self._broker_timeout_seconds = float(broker_timeout_seconds)
        self.ledger = SlotLedger(
            self.config.db_path,
            account=self.config.account,
            max_concurrent_slots=self.config.max_concurrent_slots,
            max_slot_exposure_fraction=self.config.max_slot_exposure_fraction,
            max_deployed_fraction=self.config.max_deployed_fraction,
            max_sector_fraction=self.config.max_sector_fraction,
            cash_buffer_fraction=self.config.cash_buffer_fraction,
            max_orders_per_day=self.config.max_orders_per_day,
            max_notional_per_day=self.config.max_notional_per_day,
            sector_groups=self.config.sector_groups,
        )
        self.stop_engine = ProtectiveStopEngine(
            broker=broker,
            account=self.config.account,
            client_id=self.config.client_id,
            broker_timeout_seconds=broker_timeout_seconds,
            lock_factory=self.ledger.mutation_lock,
        )
        self._next_entry_order_id = 5000

    def _assert_account_bindings(self) -> None:
        if self.ledger.account is not None and self.ledger.account != self.config.account:
            raise AccountMismatchError(
                f"ledger.account {self.ledger.account!r} does not match coordinator account {self.config.account!r}"
            )
        broker_account = getattr(self.broker, "account", None)
        if isinstance(broker_account, str) and broker_account.strip() and broker_account != self.config.account:
            raise AccountMismatchError(
                f"broker.account {broker_account!r} does not match coordinator account {self.config.account!r}"
            )

    def _size_proposal(self, proposal: SignalProposal, cash_state: AccountCashState) -> int:
        slot_budget = min(
            cash_state.net_liq_usd * self.config.max_slot_exposure_fraction,
            cash_state.spendable_cash_usd * (1.0 - self.config.cash_buffer_fraction),
        )
        if slot_budget < proposal.signal_price:
            return 0
        return int(slot_budget // proposal.signal_price)

    def _has_adequate_owned_stop(
        self,
        fresh_orders: list[BrokerOrder],
        *,
        symbol: str,
        expected_action: str,
        desired_qty: int,
        held_abs: int,
    ) -> bool:
        sym = symbol.upper()
        owned_stops = [
            o
            for o in fresh_orders
            if o.symbol.upper() == sym
            and o.status in WORKING_STATUSES
            and is_owned_stop_order(
                o,
                account=self.config.account,
                client_id=self.config.client_id,
                expected_action=expected_action,
            )
        ]
        if not owned_stops:
            return False
        owned_stop_ids = {id(o) for o in owned_stops}
        non_owned_orders = [o for o in fresh_orders if id(o) not in owned_stop_ids]
        other_close_qty = count_other_working_close_qty(
            non_owned_orders,
            symbol=sym,
            account=self.config.account,
            close_side=expected_action,
        )
        uncovered_target = max(0, min(desired_qty, held_abs - other_close_qty))
        covered_qty = sum(max(0, o.remaining) for o in owned_stops)
        return covered_qty >= uncovered_target

    async def _ensure_protective_stop(
        self,
        *,
        symbol: str,
        direction: str,
        stop_price: float,
        managed_qty: int,
        trade_key: str,
        dry_run: bool = False,
    ) -> StopActionResult:
        """Arm GTC protection idempotently, resizing in-place if a partial fill grew."""
        stop_res = await self.stop_engine.arm_protective_stop(
            symbol=symbol,
            direction=direction,
            stop_price=stop_price,
            managed_qty=managed_qty,
            trade_key=trade_key,
            dry_run=dry_run,
        )
        if (
            not dry_run
            and stop_res.action == "stop_already_armed"
            and stop_res.order is not None
            and stop_res.fresh_held_qty is not None
        ):
            target_cap = min(managed_qty, abs(stop_res.fresh_held_qty))
            if stop_res.order.remaining < target_cap:
                return await self.stop_engine.sync_protective_stop(
                    symbol=symbol,
                    direction=direction,
                    target_stop_price=stop_price,
                    managed_qty=managed_qty,
                    cached_stop=stop_res.order,
                    dry_run=False,
                )
        return stop_res

    async def reconcile_working_entries(
        self,
        *,
        session_day: Optional[str] = None,
        as_of: Optional[str] = None,
        dry_run: Optional[bool] = None,
    ) -> ReconciliationReport:
        """Reconcile persisted WORKING/PARTIALLY_FILLED entries against live broker state.

        Queries broker open orders, positions, and optional terminal order status;
        transitions newly filled entries to ``FILLED``/``PARTIALLY_FILLED`` and
        idempotently arms GTC protective stops via ``ProtectiveStopEngine``.
        Transitions zero-fill non-open entries to ``CANCELLED`` to free the slot.
        Fails closed (``uncertain_holdings=True``) on broker timeout or ambiguity.
        """
        eff_session_day = session_day or dt.datetime.now(dt.timezone.utc).date().isoformat()
        eff_as_of = as_of or dt.datetime.now(dt.timezone.utc).isoformat()
        eff_dry_run = (not self.config.arm) if dry_run is None else bool(dry_run)
        report = ReconciliationReport(session_day=eff_session_day)

        self._assert_account_bindings()

        if Path(self.config.kill_switch_path).exists():
            report.blocked_reason = "kill_switch_active"
            return report

        candidates = self.ledger.reconcilable_reservations(self.config.account, eff_session_day)
        report.checked_reservations = len(candidates)
        if not candidates:
            return report

        try:
            fresh_orders = list(
                await asyncio.wait_for(self.broker.req_open_orders(), timeout=self._broker_timeout_seconds)
            )
        except Exception as exc:
            report.uncertain_holdings = True
            report.broker_read_failed = True
            report.uncertain_symbols = sorted({r.symbol for r in candidates})
            report.errors.append({"stage": "open_orders_read", "error": f"{type(exc).__name__}: {exc}"})
            return report

        try:
            fresh_positions = list(
                await asyncio.wait_for(self.broker.req_positions(), timeout=self._broker_timeout_seconds)
            )
        except Exception as exc:
            report.uncertain_holdings = True
            report.broker_read_failed = True
            report.uncertain_symbols = sorted({r.symbol for r in candidates})
            report.errors.append({"stage": "positions_read", "error": f"{type(exc).__name__}: {exc}"})
            return report

        completed_orders: list[BrokerOrder] = []
        req_completed = getattr(self.broker, "req_completed_orders", None)
        if callable(req_completed):
            try:
                completed_orders = list(
                    await asyncio.wait_for(req_completed(), timeout=self._broker_timeout_seconds)
                )
            except Exception as exc:
                report.uncertain_holdings = True
                report.broker_read_failed = True
                report.uncertain_symbols = sorted({r.symbol for r in candidates})
                report.errors.append({"stage": "completed_orders_read", "error": f"{type(exc).__name__}: {exc}"})
                return report

        req_order_status = getattr(self.broker, "req_order_status", None)

        unresolved_per_symbol: dict[str, int] = {}
        for r in candidates:
            if r.status in ("SUBMITTED", "WORKING"):
                unresolved_per_symbol[r.symbol] = unresolved_per_symbol.get(r.symbol, 0) + 1

        uncertain_symbols_set: set[str] = set()

        for res in candidates:
            direction = "LONG" if res.side == "BUY" else "SHORT"
            expected_stop_action = "SELL" if direction == "LONG" else "BUY"
            held_qty = _held_qty_for_symbol(fresh_positions, account=self.config.account, symbol=res.symbol)
            directional_held = held_qty if direction == "LONG" else -held_qty

            open_match, open_err = _match_order_to_reservation(fresh_orders, res)
            if open_err is not None:
                report.uncertain_holdings = True
                uncertain_symbols_set.add(res.symbol)
                report.errors.append({"reservation_id": res.reservation_id, "symbol": res.symbol, "reason": open_err})
                continue

            status_match: Optional[BrokerOrder] = None
            status_err: Optional[str] = None
            if open_match is None:
                if completed_orders:
                    status_match, status_err = _match_order_to_reservation(completed_orders, res)
                if status_match is None and status_err is None and callable(req_order_status):
                    try:
                        looked_up = await asyncio.wait_for(
                            req_order_status(order_id=res.order_id, perm_id=res.perm_id, order_ref=res.order_ref),
                            timeout=self._broker_timeout_seconds,
                        )
                    except Exception as exc:
                        report.uncertain_holdings = True
                        uncertain_symbols_set.add(res.symbol)
                        report.errors.append(
                            {
                                "reservation_id": res.reservation_id,
                                "symbol": res.symbol,
                                "reason": f"order_status_read_error({type(exc).__name__}: {exc})",
                            }
                        )
                        continue
                    if looked_up is not None:
                        status_match, status_err = _match_order_to_reservation([looked_up], res)
            if status_err is not None:
                report.uncertain_holdings = True
                uncertain_symbols_set.add(res.symbol)
                report.errors.append({"reservation_id": res.reservation_id, "symbol": res.symbol, "reason": status_err})
                continue

            new_status = res.status
            new_filled_qty = res.filled_qty
            broker_status = "Reconciled"
            eff_order_id = res.order_id
            eff_perm_id = res.perm_id
            fill_px = res.limit_price
            is_cancelled = False
            ambiguity_reason: Optional[str] = None

            if open_match is not None:
                raw_filled = open_match.filled
                if (
                    isinstance(raw_filled, bool)
                    or not isinstance(raw_filled, int)
                    or raw_filled < 0
                    or raw_filled > res.qty
                    or raw_filled < res.filled_qty
                ):
                    ambiguity_reason = f"invalid_open_order_fill_qty({raw_filled!r},prev={res.filled_qty},ordered={res.qty})"
                elif open_match.status == "Filled":
                    if raw_filled <= 0:
                        ambiguity_reason = "broker_filled_without_fill_qty"
                    else:
                        new_filled_qty = int(raw_filled)
                        new_status = "FILLED" if new_filled_qty == res.qty else "PARTIALLY_FILLED"
                        broker_status = open_match.status
                elif open_match.status in WORKING_STATUSES:
                    new_filled_qty = int(raw_filled)
                    new_status = "FILLED" if new_filled_qty == res.qty else "WORKING"
                    broker_status = open_match.status
                else:
                    ambiguity_reason = f"unexpected_open_order_status({open_match.status!r})"
                eff_order_id = open_match.order_id if open_match.order_id > 0 else res.order_id
                eff_perm_id = open_match.perm_id if open_match.perm_id > 0 else res.perm_id
                if _is_valid_positive_price(open_match.limit_price):
                    fill_px = float(open_match.limit_price)
            elif status_match is not None:
                raw_filled = status_match.filled
                if (
                    isinstance(raw_filled, bool)
                    or not isinstance(raw_filled, int)
                    or raw_filled < 0
                    or raw_filled > res.qty
                    or raw_filled < res.filled_qty
                ):
                    ambiguity_reason = f"invalid_terminal_fill_qty({raw_filled!r},prev={res.filled_qty},ordered={res.qty})"
                elif status_match.status == "Filled":
                    if raw_filled <= 0:
                        ambiguity_reason = "broker_filled_without_fill_qty"
                    else:
                        new_filled_qty = int(raw_filled)
                        new_status = "FILLED" if new_filled_qty == res.qty else "PARTIALLY_FILLED"
                        broker_status = status_match.status
                elif status_match.status in TERMINAL_STATUSES:
                    new_filled_qty = int(raw_filled)
                    broker_status = status_match.status
                    if new_filled_qty > 0:
                        new_status = "FILLED" if new_filled_qty == res.qty else "PARTIALLY_FILLED"
                    else:
                        is_cancelled = True
                        new_status = "CANCELLED"
                else:
                    ambiguity_reason = f"unexpected_terminal_status({status_match.status!r})"
                eff_order_id = status_match.order_id if status_match.order_id > 0 else res.order_id
                eff_perm_id = status_match.perm_id if status_match.perm_id > 0 else res.perm_id
                if _is_valid_positive_price(status_match.limit_price):
                    fill_px = float(status_match.limit_price)
            else:
                # Strict 4-method BrokerStopClient path: infer from absence in open orders + live positions.
                if res.status in ("SUBMITTED", "WORKING") and unresolved_per_symbol.get(res.symbol, 0) > 1 and directional_held > 0:
                    ambiguity_reason = (
                        f"multiple_working_entries_for_symbol({res.symbol},count={unresolved_per_symbol[res.symbol]})"
                    )
                elif directional_held < 0:
                    ambiguity_reason = f"opposite_direction_position({res.symbol},held={held_qty},side={res.side})"
                elif directional_held == 0:
                    if res.filled_qty == 0:
                        is_cancelled = True
                        new_status = "CANCELLED"
                        broker_status = "NotOpenZeroPosition"
                    else:
                        new_filled_qty = res.filled_qty
                        new_status = "FILLED" if res.filled_qty == res.qty else "PARTIALLY_FILLED"
                        broker_status = "FlatAfterPriorFill"
                elif directional_held > res.qty:
                    ambiguity_reason = f"ambiguous_excess_position({res.symbol},held={directional_held},ordered={res.qty})"
                else:
                    # 0 < directional_held <= res.qty
                    new_filled_qty = max(res.filled_qty, directional_held)
                    new_status = "FILLED" if new_filled_qty == res.qty else "PARTIALLY_FILLED"
                    broker_status = "FilledFromPosition" if new_filled_qty == res.qty else "PartialFillFromPosition"

            if ambiguity_reason is not None:
                report.uncertain_holdings = True
                uncertain_symbols_set.add(res.symbol)
                report.errors.append(
                    {
                        "reservation_id": res.reservation_id,
                        "symbol": res.symbol,
                        "reason": ambiguity_reason,
                    }
                )
                continue

            if is_cancelled:
                self.ledger.update_reservation_status(
                    res.reservation_id,
                    status="CANCELLED",
                    order_id=eff_order_id,
                    perm_id=eff_perm_id,
                    filled_qty=0,
                    now_iso=eff_as_of,
                    expected_account=self.config.account,
                )
                report.reconciled_cancellations.append(
                    {
                        "reservation_id": res.reservation_id,
                        "symbol": res.symbol,
                        "order_id": eff_order_id,
                        "perm_id": eff_perm_id,
                        "ordered_qty": res.qty,
                        "filled_qty": 0,
                        "ledger_status": "CANCELLED",
                        "broker_status": broker_status,
                    }
                )
                continue

            newly_filled_qty = new_filled_qty - res.filled_qty
            if (
                new_status != res.status
                or new_filled_qty != res.filled_qty
                or eff_order_id != res.order_id
                or eff_perm_id != res.perm_id
            ):
                self.ledger.update_reservation_status(
                    res.reservation_id,
                    status=new_status,
                    order_id=eff_order_id,
                    perm_id=eff_perm_id,
                    filled_qty=new_filled_qty,
                    now_iso=eff_as_of,
                    expected_account=self.config.account,
                )

            if newly_filled_qty > 0:
                report.reconciled_fills.append(
                    {
                        "reservation_id": res.reservation_id,
                        "symbol": res.symbol,
                        "order_id": eff_order_id,
                        "perm_id": eff_perm_id,
                        "ordered_qty": res.qty,
                        "filled_qty": new_filled_qty,
                        "previously_filled_qty": res.filled_qty,
                        "newly_filled_qty": newly_filled_qty,
                        "fill_price": fill_px,
                        "ledger_status": new_status,
                        "broker_status": broker_status,
                        "reconciled": True,
                    }
                )
                if res.stop_price is not None:
                    report.alerts.append(
                        format_entry_alert(
                            symbol=res.symbol,
                            qty=new_filled_qty,
                            fill_price=fill_px,
                            stop_price=res.stop_price,
                            strategy_id=(
                                res.strategy_id if res.strategy_id != "" else self.signal_provider.name
                            ),
                        )
                    )

            if new_filled_qty <= 0:
                continue

            already_covered = self._has_adequate_owned_stop(
                fresh_orders,
                symbol=res.symbol,
                expected_action=expected_stop_action,
                desired_qty=new_filled_qty,
                held_abs=max(0, directional_held),
            )
            if already_covered and newly_filled_qty == 0:
                if not res.stop_armed and not eff_dry_run:
                    self.ledger.update_reservation_status(
                        res.reservation_id,
                        status=new_status,
                        filled_qty=new_filled_qty,
                        stop_armed=True,
                        now_iso=eff_as_of,
                        expected_account=self.config.account,
                    )
                continue

            if directional_held <= 0 and newly_filled_qty == 0:
                if not res.stop_armed and not eff_dry_run:
                    self.ledger.update_reservation_status(
                        res.reservation_id,
                        status=new_status,
                        filled_qty=new_filled_qty,
                        stop_armed=True,
                        now_iso=eff_as_of,
                        expected_account=self.config.account,
                    )
                continue

            if res.stop_price is None:
                report.uncertain_holdings = True
                uncertain_symbols_set.add(res.symbol)
                report.errors.append(
                    {
                        "reservation_id": res.reservation_id,
                        "symbol": res.symbol,
                        "reason": "missing_persisted_stop_price",
                    }
                )
                continue

            stop_res = await self._ensure_protective_stop(
                symbol=res.symbol,
                direction=direction,
                stop_price=res.stop_price,
                managed_qty=new_filled_qty,
                trade_key=res.reservation_id,
                dry_run=eff_dry_run,
            )
            report.stops_armed.append(stop_res)
            if stop_res.uncertain_holdings:
                report.uncertain_holdings = True
                uncertain_symbols_set.add(res.symbol)
                report.errors.append(
                    {
                        "reservation_id": res.reservation_id,
                        "symbol": res.symbol,
                        "reason": stop_res.action,
                        "detail": dict(stop_res.detail),
                    }
                )
            elif (
                not eff_dry_run
                and (
                    stop_res.stop_rearmed
                    or stop_res.action
                    in ("stop_arm_skipped_flat_or_reversed", "stop_arm_skipped_covered_by_other_closes")
                )
            ):
                self.ledger.update_reservation_status(
                    res.reservation_id,
                    status=new_status,
                    filled_qty=new_filled_qty,
                    stop_armed=True,
                    now_iso=eff_as_of,
                    expected_account=self.config.account,
                )

        report.uncertain_symbols = sorted(uncertain_symbols_set)
        return report

    async def run_entry_cycle(
        self,
        *,
        bars_by_symbol: Mapping[str, Sequence[PriceBar]],
        cash_state: AccountCashState,
        session_day: str,
        as_of: str,
        external_open_symbols: Sequence[str] = (),
        auto_fill_in_sim: bool = False,
    ) -> CycleReport:
        """Execute deferred-fill reconciliation followed by one deterministic entry + stop cycle."""
        report = CycleReport(session_day=session_day)

        # Preflight 0: demo-only simulation switch constraint & strict account binding
        # BEFORE touching ledger or broker.
        if auto_fill_in_sim and not isinstance(self.broker, SimulatedBroker):
            raise CoordinatorConfigError(
                "auto_fill_in_sim=True is restricted to SimulatedBroker demo instances"
            )
        if cash_state.account != self.config.account:
            raise AccountMismatchError(
                f"cash_state.account {cash_state.account!r} does not match coordinator account {self.config.account!r}"
            )
        self._assert_account_bindings()

        # Preflight 1: kill switch check BEFORE touching broker
        if Path(self.config.kill_switch_path).exists():
            report.blocked_reason = "kill_switch_active"
            return report

        # Preflight 2: persisted ledger & counter integrity BEFORE touching broker
        try:
            self.ledger.validate_integrity(self.config.account, session_day)
        except LedgerCorruptionError:
            raise

        # Preflight 3: durable deferred/async fill reconciliation BEFORE admitting new proposals
        reconcile_res = await self.reconcile_working_entries(
            session_day=session_day,
            as_of=as_of,
            dry_run=not self.config.arm,
        )
        report.reconciled_fills.extend(reconcile_res.reconciled_fills)
        report.filled.extend(reconcile_res.reconciled_fills)
        report.reconciled_cancellations.extend(reconcile_res.reconciled_cancellations)
        report.stops_armed.extend(reconcile_res.stops_armed)
        report.alerts.extend(reconcile_res.alerts)
        report.uncertain_holdings = reconcile_res.uncertain_holdings
        report.uncertain_symbols = list(reconcile_res.uncertain_symbols)

        effective_cash = cash_state
        for rfill in reconcile_res.reconciled_fills:
            sym = str(rfill["symbol"]).upper()
            filled_qty_r = int(rfill["filled_qty"])
            fill_px_r = float(rfill["fill_price"])
            if effective_cash.open_symbol_notional.get(sym, 0.0) <= 0.0 and filled_qty_r > 0:
                filled_notional = round(filled_qty_r * fill_px_r, 2)
                updated_open = dict(effective_cash.open_symbol_notional)
                updated_open[sym] = round(updated_open.get(sym, 0.0) + filled_notional, 2)
                effective_cash = AccountCashState(
                    account=effective_cash.account,
                    net_liq_usd=effective_cash.net_liq_usd,
                    total_cash_usd=max(0.0, round(effective_cash.total_cash_usd - filled_notional, 2)),
                    settled_cash_usd=max(0.0, round(effective_cash.settled_cash_usd - filled_notional, 2)),
                    available_funds_usd=max(0.0, round(effective_cash.available_funds_usd - filled_notional, 2)),
                    ring_fenced_reserve_usd=effective_cash.ring_fenced_reserve_usd,
                    unsettled_t1_proceeds_usd=effective_cash.unsettled_t1_proceeds_usd,
                    open_symbol_notional=updated_open,
                )

        proposals = self.signal_provider.generate_proposals(bars_by_symbol, as_of=as_of)
        report.proposals_seen = len(proposals)
        if reconcile_res.broker_read_failed:
            report.blocked_reason = "reconciliation_uncertain_holdings"
            for proposal in proposals:
                report.rejected.append({"symbol": proposal.symbol, "reason": "reconciliation_uncertain_holdings"})
            return report
        if not proposals:
            return report

        uncertain_symbol_set = {s.upper() for s in report.uncertain_symbols}

        for proposal in proposals:
            if proposal.symbol.upper() in uncertain_symbol_set:
                report.rejected.append({"symbol": proposal.symbol, "reason": "reconciliation_uncertain_holdings"})
                continue

            qty = self._size_proposal(proposal, effective_cash)
            if qty <= 0:
                report.rejected.append({"symbol": proposal.symbol, "reason": "insufficient_spendable_cash_for_1_share"})
                continue

            limit_px = round_tick(proposal.signal_price)
            entry_side = "BUY" if proposal.direction == "LONG" else "SELL"
            decision = self.ledger.reserve_entry_slot(
                cash_state=effective_cash,
                client_id=self.config.client_id,
                symbol=proposal.symbol,
                qty=qty,
                limit_price=limit_px,
                session_day=session_day,
                side=entry_side,
                stop_price=proposal.stop_price,
                strategy_id=proposal.strategy_id,
                external_open_symbols=external_open_symbols,
                now_iso=as_of,
                expected_account=self.config.account,
            )
            if not decision.allowed or decision.reservation is None:
                report.rejected.append({"symbol": proposal.symbol, "reason": decision.reason})
                continue

            res = decision.reservation
            report.reserved.append(res.reservation_id)

            if not self.config.arm:
                self.ledger.update_reservation_status(
                    res.reservation_id,
                    status="CANCELLED",
                    now_iso=as_of,
                    expected_account=self.config.account,
                )
                report.rejected.append({"symbol": proposal.symbol, "reason": "dry_run_not_armed"})
                continue

            self._next_entry_order_id += 1
            entry_order = BrokerOrder(
                account=self.config.account,
                symbol=proposal.symbol,
                action=entry_side,
                order_type="LMT",
                total_quantity=qty,
                client_id=self.config.client_id,
                order_id=self._next_entry_order_id,
                order_ref=res.order_ref,
                limit_price=limit_px,
                tif="DAY",
            )
            placed = await self.broker.place_order(entry_order)

            # Reconcile returned broker order identity before trusting status or arming protection.
            if placed.account != self.config.account:
                self.ledger.update_reservation_status(
                    res.reservation_id,
                    status="UNKNOWN",
                    order_id=placed.order_id,
                    perm_id=placed.perm_id,
                    now_iso=as_of,
                    expected_account=self.config.account,
                )
                raise AccountMismatchError(
                    f"broker returned order for account {placed.account!r}, expected {self.config.account!r}"
                )
            if placed.symbol.upper() != proposal.symbol.upper():
                self.ledger.update_reservation_status(
                    res.reservation_id,
                    status="UNKNOWN",
                    order_id=placed.order_id,
                    perm_id=placed.perm_id,
                    now_iso=as_of,
                    expected_account=self.config.account,
                )
                raise ReservationError(
                    f"broker returned order for symbol {placed.symbol!r}, expected {proposal.symbol!r}"
                )

            # Demo-only fill driver for SimulatedBroker: transitions a working order to Filled
            # so it flows through the exact same broker-response reconciliation path below.
            if (
                auto_fill_in_sim
                and isinstance(self.broker, SimulatedBroker)
                and placed.status in WORKING_STATUSES
                and placed.remaining > 0
            ):
                self.broker.simulate_partial_fill(placed, placed.remaining)

            raw_filled = placed.filled
            if isinstance(raw_filled, bool) or not isinstance(raw_filled, int) or raw_filled < 0 or raw_filled > qty:
                self.ledger.update_reservation_status(
                    res.reservation_id,
                    status="UNKNOWN",
                    order_id=placed.order_id,
                    perm_id=placed.perm_id,
                    now_iso=as_of,
                    expected_account=self.config.account,
                )
                report.rejected.append(
                    {
                        "symbol": proposal.symbol,
                        "reason": f"invalid_broker_fill_qty({raw_filled!r},ordered={qty})",
                    }
                )
                continue
            filled_qty = int(raw_filled)

            if placed.status == "Filled":
                if filled_qty <= 0:
                    self.ledger.update_reservation_status(
                        res.reservation_id,
                        status="UNKNOWN",
                        order_id=placed.order_id,
                        perm_id=placed.perm_id,
                        filled_qty=0,
                        now_iso=as_of,
                        expected_account=self.config.account,
                    )
                    report.rejected.append({"symbol": proposal.symbol, "reason": "broker_filled_without_fill_qty"})
                    continue
                ledger_status = "FILLED" if filled_qty == qty else "PARTIALLY_FILLED"
            elif placed.status in WORKING_STATUSES:
                ledger_status = "FILLED" if filled_qty == qty else "WORKING"
            elif placed.status in TERMINAL_STATUSES:
                if filled_qty > 0:
                    ledger_status = "FILLED" if filled_qty == qty else "PARTIALLY_FILLED"
                else:
                    ledger_status = "CANCELLED" if placed.status in ("Cancelled", "ApiCancelled") else "REJECTED"
                    self.ledger.update_reservation_status(
                        res.reservation_id,
                        status=ledger_status,
                        order_id=placed.order_id,
                        perm_id=placed.perm_id,
                        filled_qty=0,
                        now_iso=as_of,
                        expected_account=self.config.account,
                    )
                    report.rejected.append({"symbol": proposal.symbol, "reason": f"broker_rejected({placed.status})"})
                    continue
            else:
                ledger_status = "UNKNOWN"
                self.ledger.update_reservation_status(
                    res.reservation_id,
                    status=ledger_status,
                    order_id=placed.order_id,
                    perm_id=placed.perm_id,
                    filled_qty=filled_qty,
                    now_iso=as_of,
                    expected_account=self.config.account,
                )
                report.rejected.append({"symbol": proposal.symbol, "reason": f"broker_unknown_status({placed.status})"})
                if filled_qty <= 0:
                    continue

            if ledger_status != "UNKNOWN":
                self.ledger.update_reservation_status(
                    res.reservation_id,
                    status=ledger_status,
                    order_id=placed.order_id,
                    perm_id=placed.perm_id,
                    filled_qty=filled_qty,
                    now_iso=as_of,
                    expected_account=self.config.account,
                )

            if filled_qty > 0:
                fill_px = float(placed.limit_price) if _is_valid_positive_price(placed.limit_price) else limit_px
                filled_notional = round(filled_qty * fill_px, 2)
                updated_open = dict(effective_cash.open_symbol_notional)
                updated_open[proposal.symbol] = round(updated_open.get(proposal.symbol, 0.0) + filled_notional, 2)
                effective_cash = AccountCashState(
                    account=effective_cash.account,
                    net_liq_usd=effective_cash.net_liq_usd,
                    total_cash_usd=max(0.0, round(effective_cash.total_cash_usd - filled_notional, 2)),
                    settled_cash_usd=max(0.0, round(effective_cash.settled_cash_usd - filled_notional, 2)),
                    available_funds_usd=max(0.0, round(effective_cash.available_funds_usd - filled_notional, 2)),
                    ring_fenced_reserve_usd=effective_cash.ring_fenced_reserve_usd,
                    unsettled_t1_proceeds_usd=effective_cash.unsettled_t1_proceeds_usd,
                    open_symbol_notional=updated_open,
                )
                report.filled.append(
                    {
                        "reservation_id": res.reservation_id,
                        "symbol": proposal.symbol,
                        "order_id": placed.order_id,
                        "perm_id": placed.perm_id,
                        "ordered_qty": qty,
                        "filled_qty": filled_qty,
                        "fill_price": fill_px,
                        "ledger_status": ledger_status,
                        "broker_status": placed.status,
                    }
                )
                report.alerts.append(
                    format_entry_alert(
                        symbol=proposal.symbol,
                        qty=filled_qty,
                        fill_price=fill_px,
                        stop_price=proposal.stop_price,
                        strategy_id=proposal.strategy_id,
                    )
                )
                stop_res = await self.stop_engine.arm_protective_stop(
                    symbol=proposal.symbol,
                    direction=proposal.direction,
                    stop_price=proposal.stop_price,
                    managed_qty=filled_qty,
                    trade_key=res.reservation_id,
                    dry_run=False,
                )
                report.stops_armed.append(stop_res)
                if stop_res.uncertain_holdings:
                    report.uncertain_holdings = True
                    if proposal.symbol not in report.uncertain_symbols:
                        report.uncertain_symbols.append(proposal.symbol)
                elif (
                    ledger_status != "UNKNOWN"
                    and (
                        stop_res.stop_rearmed
                        or stop_res.action
                        in ("stop_arm_skipped_flat_or_reversed", "stop_arm_skipped_covered_by_other_closes")
                    )
                ):
                    self.ledger.update_reservation_status(
                        res.reservation_id,
                        status=ledger_status,
                        filled_qty=filled_qty,
                        stop_armed=True,
                        now_iso=as_of,
                        expected_account=self.config.account,
                    )

        return report

    def build_exit_alert(
        self,
        *,
        symbol: str,
        qty: int,
        entry_price: float,
        exit_price: float,
        strategy_id: str,
        reason: str,
        fees_usd: Optional[float] = None,
    ) -> dict[str, object]:
        """Emit an exit alert following the neutral-before-fees contract."""
        return format_exit_alert(
            symbol=symbol,
            qty=qty,
            entry_price=entry_price,
            exit_price=exit_price,
            strategy_id=strategy_id,
            reason=reason,
            fees_usd=fees_usd,
        )
