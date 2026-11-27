"""Sanitized 2x Leveraged-ETF Swing Execution Infrastructure.

This package provides deterministic execution, protective-stop management,
multi-slot cash/exposure reservation, and paper-to-live graduation rails.

Boundary:
    - Included: broker-agnostic execution safety, fail-closed stop cancel/replace,
      T+1/cash reserve accounting, cross-client order-ID isolation, and
      neutral-before-fees alert formatting.
    - Excluded: proprietary signal generation, alpha formulas, ticker rules,
      regime filters, and tuned entry/exit thresholds. Supply your own
      ``SwingSignalProvider`` implementation or use the synthetic demo-only
      ``ToyCrossoverSignal`` for offline testing.
"""

from swing_executor.coordinator import (
    CoordinatorConfig,
    CoordinatorConfigError,
    CycleReport,
    ReconciliationReport,
    SwingExecutionCoordinator,
    validate_coordinator_config,
)
from swing_executor.graduation import (
    ClosedPaperTrade,
    GraduationContract,
    GraduationReport,
    ProbationDecision,
    evaluate_live_probation,
    evaluate_paper_graduation,
    format_entry_alert,
    format_exit_alert,
    format_stop_sync_alert,
)
from swing_executor.ledger import (
    AccountCashState,
    AccountMismatchError,
    LedgerCorruptionError,
    ReservationDecision,
    ReservationError,
    SlotLedger,
    SlotReservation,
    compute_monotonic_stop,
    evaluate_trailing_stop,
    round_tick,
)
from swing_executor.signals import (
    PriceBar,
    SignalProposal,
    SwingSignalProvider,
    ToyCrossoverSignal,
)
from swing_executor.stops import (
    BrokerOrder,
    BrokerPosition,
    BrokerStopClient,
    ProtectiveStopEngine,
    SimulatedBroker,
    StopActionResult,
    count_other_working_close_qty,
    same_broker_order,
)

__all__ = [
    "AccountCashState",
    "AccountMismatchError",
    "BrokerOrder",
    "BrokerPosition",
    "BrokerStopClient",
    "ClosedPaperTrade",
    "CoordinatorConfig",
    "CoordinatorConfigError",
    "CycleReport",
    "GraduationContract",
    "GraduationReport",
    "LedgerCorruptionError",
    "PriceBar",
    "ProbationDecision",
    "ProtectiveStopEngine",
    "ReconciliationReport",
    "ReservationDecision",
    "ReservationError",
    "SignalProposal",
    "SimulatedBroker",
    "SlotLedger",
    "SlotReservation",
    "StopActionResult",
    "SwingExecutionCoordinator",
    "SwingSignalProvider",
    "ToyCrossoverSignal",
    "compute_monotonic_stop",
    "count_other_working_close_qty",
    "evaluate_live_probation",
    "evaluate_paper_graduation",
    "evaluate_trailing_stop",
    "format_entry_alert",
    "format_exit_alert",
    "format_stop_sync_alert",
    "round_tick",
    "same_broker_order",
    "validate_coordinator_config",
]
