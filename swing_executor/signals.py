"""Pluggable signal-provider protocol and synthetic demo-only signal fixture.

Proprietary alpha logic is intentionally omitted from this public repository.
Any strategy plugged into ``SwingExecutionCoordinator`` only needs to satisfy
the ``SwingSignalProvider`` protocol.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping, Protocol, Sequence, runtime_checkable


@dataclass(frozen=True)
class PriceBar:
    """Single OHLCV price bar for offline simulation or signal evaluation."""

    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def __post_init__(self) -> None:
        for attr in ("open", "high", "low", "close"):
            val = getattr(self, attr)
            if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val) or val <= 0:
                raise ValueError(f"PriceBar.{attr} must be a finite positive number, got {val!r}")
        if isinstance(self.volume, bool) or not isinstance(self.volume, (int, float)) or not math.isfinite(self.volume) or self.volume < 0:
            raise ValueError(f"PriceBar.volume must be finite and non-negative, got {self.volume!r}")
        if self.low > self.high:
            raise ValueError(f"PriceBar.low ({self.low}) cannot exceed PriceBar.high ({self.high})")


@dataclass(frozen=True)
class SignalProposal:
    """Typed trade proposal emitted by a ``SwingSignalProvider``."""

    symbol: str
    direction: str
    signal_price: float
    stop_price: float
    strategy_id: str
    bar_timestamp: str
    reason: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        sym = str(self.symbol or "").strip().upper()
        if not sym:
            raise ValueError("SignalProposal.symbol must be a non-empty string")
        object.__setattr__(self, "symbol", sym)

        direction = str(self.direction or "").strip().upper()
        if direction not in ("LONG", "SHORT"):
            raise ValueError(f"SignalProposal.direction must be 'LONG' or 'SHORT', got {self.direction!r}")
        object.__setattr__(self, "direction", direction)

        strat = str(self.strategy_id or "").strip()
        if not strat:
            raise ValueError("SignalProposal.strategy_id must be a non-empty string")
        object.__setattr__(self, "strategy_id", strat)

        for attr in ("signal_price", "stop_price"):
            val = getattr(self, attr)
            if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val) or val <= 0:
                raise ValueError(f"SignalProposal.{attr} must be a finite positive number, got {val!r}")

        if direction == "LONG" and self.stop_price >= self.signal_price:
            raise ValueError(
                f"LONG SignalProposal.stop_price ({self.stop_price}) must be below signal_price ({self.signal_price})"
            )
        if direction == "SHORT" and self.stop_price <= self.signal_price:
            raise ValueError(
                f"SHORT SignalProposal.stop_price ({self.stop_price}) must be above signal_price ({self.signal_price})"
            )


@runtime_checkable
class SwingSignalProvider(Protocol):
    """Protocol implemented by any pluggable swing signal generator."""

    name: str

    def generate_proposals(
        self,
        bars_by_symbol: Mapping[str, Sequence[PriceBar]],
        *,
        as_of: str,
    ) -> list[SignalProposal]:
        """Inspect historical bars and return zero or more validated proposals."""
        ...


class ToyCrossoverSignal:
    """SYNTHETIC DEMO-ONLY moving-average crossover signal provider.

    WARNING:
        This class is NOT a production trading strategy and has no claimed
        trading edge. It exists solely to exercise the execution, reservation,
        stop-management, and graduation rails offline without proprietary alpha.
    """

    IS_PRODUCTION_STRATEGY: bool = False

    def __init__(
        self,
        *,
        fast_window: int = 3,
        slow_window: int = 5,
        stop_pct: float = 0.05,
        name: str = "toy_sma_crossover_demo_only",
    ) -> None:
        if isinstance(fast_window, bool) or not isinstance(fast_window, int) or fast_window < 1:
            raise ValueError(f"fast_window must be a positive integer, got {fast_window!r}")
        if isinstance(slow_window, bool) or not isinstance(slow_window, int) or slow_window <= fast_window:
            raise ValueError(f"slow_window ({slow_window!r}) must be an integer > fast_window ({fast_window!r})")
        if isinstance(stop_pct, bool) or not isinstance(stop_pct, (int, float)) or not math.isfinite(stop_pct) or not (0.0 < stop_pct < 0.5):
            raise ValueError(f"stop_pct must be finite in (0, 0.5), got {stop_pct!r}")
        self.fast_window = fast_window
        self.slow_window = slow_window
        self.stop_pct = float(stop_pct)
        self.name = str(name)

    def generate_proposals(
        self,
        bars_by_symbol: Mapping[str, Sequence[PriceBar]],
        *,
        as_of: str,
    ) -> list[SignalProposal]:
        proposals: list[SignalProposal] = []
        min_bars = self.slow_window + 1
        for raw_symbol in sorted(bars_by_symbol):
            bars = list(bars_by_symbol[raw_symbol])
            if len(bars) < min_bars:
                continue
            closes = [b.close for b in bars]
            prev_fast = sum(closes[-1 - self.fast_window : -1]) / self.fast_window
            prev_slow = sum(closes[-1 - self.slow_window : -1]) / self.slow_window
            curr_fast = sum(closes[-self.fast_window :]) / self.fast_window
            curr_slow = sum(closes[-self.slow_window :]) / self.slow_window

            if prev_fast <= prev_slow and curr_fast > curr_slow:
                last_close = closes[-1]
                stop_px = round(last_close * (1.0 - self.stop_pct), 2)
                if stop_px <= 0 or stop_px >= last_close:
                    continue
                proposals.append(
                    SignalProposal(
                        symbol=raw_symbol,
                        direction="LONG",
                        signal_price=round(last_close, 2),
                        stop_price=stop_px,
                        strategy_id=self.name,
                        bar_timestamp=as_of,
                        reason=(
                            f"synthetic_demo_crossover(fast{self.fast_window}={curr_fast:.2f}>"
                            f"slow{self.slow_window}={curr_slow:.2f})"
                        ),
                        metadata={
                            "demo_only": True,
                            "fast_sma": round(curr_fast, 4),
                            "slow_sma": round(curr_slow, 4),
                        },
                    )
                )
        return proposals
