"""Fail-closed swing execution coordinator wiring signals, ledger, and protective stops.

Validates explicit armed config files, domain caps, kill-switch status, and
persisted ledger integrity BEFORE calling any broker method.
"""

from __future__ import annotations

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
class CycleReport:
    """Structured summary of one coordinator evaluation cycle."""

    session_day: str
    proposals_seen: int = 0
    reserved: list[str] = field(default_factory=list)
    filled: list[dict[str, object]] = field(default_factory=list)
    rejected: list[dict[str, str]] = field(default_factory=list)
    stops_armed: list[StopActionResult] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    blocked_reason: Optional[str] = None


class SwingExecutionCoordinator:
    """Coordinates proposal admission, slot reservation, entry fill, and stop arming."""

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

    def _size_proposal(self, proposal: SignalProposal, cash_state: AccountCashState) -> int:
        slot_budget = min(
            cash_state.net_liq_usd * self.config.max_slot_exposure_fraction,
            cash_state.spendable_cash_usd * (1.0 - self.config.cash_buffer_fraction),
        )
        if slot_budget < proposal.signal_price:
            return 0
        return int(slot_budget // proposal.signal_price)

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
        """Execute one deterministic entry + protective-stop arming cycle."""
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
        if self.ledger.account is not None and self.ledger.account != self.config.account:
            raise AccountMismatchError(
                f"ledger.account {self.ledger.account!r} does not match coordinator account {self.config.account!r}"
            )
        broker_account = getattr(self.broker, "account", None)
        if isinstance(broker_account, str) and broker_account.strip() and broker_account != self.config.account:
            raise AccountMismatchError(
                f"broker.account {broker_account!r} does not match coordinator account {self.config.account!r}"
            )

        # Preflight 1: kill switch check BEFORE touching broker
        if Path(self.config.kill_switch_path).exists():
            report.blocked_reason = "kill_switch_active"
            return report

        # Preflight 2: persisted ledger & counter integrity BEFORE touching broker
        try:
            self.ledger.validate_integrity(self.config.account, session_day)
        except LedgerCorruptionError:
            raise

        proposals = self.signal_provider.generate_proposals(bars_by_symbol, as_of=as_of)
        report.proposals_seen = len(proposals)
        if not proposals:
            return report

        effective_cash = cash_state
        for proposal in proposals:
            qty = self._size_proposal(proposal, effective_cash)
            if qty <= 0:
                report.rejected.append({"symbol": proposal.symbol, "reason": "insufficient_spendable_cash_for_1_share"})
                continue

            limit_px = round_tick(proposal.signal_price)
            decision = self.ledger.reserve_entry_slot(
                cash_state=effective_cash,
                client_id=self.config.client_id,
                symbol=proposal.symbol,
                qty=qty,
                limit_price=limit_px,
                session_day=session_day,
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
                action="BUY" if proposal.direction == "LONG" else "SELL",
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
                fill_px = (
                    float(placed.limit_price)
                    if (
                        not isinstance(placed.limit_price, bool)
                        and isinstance(placed.limit_price, (int, float))
                        and math.isfinite(placed.limit_price)
                        and placed.limit_price > 0.0
                    )
                    else limit_px
                )
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
