"""Unit and integration tests for sanitized 2x leveraged-ETF swing_executor package."""

from __future__ import annotations

import asyncio
import math
import sqlite3
from pathlib import Path

import pytest

from swing_executor import (
    AccountCashState,
    AccountMismatchError,
    BrokerOrder,
    BrokerPosition,
    ClosedPaperTrade,
    CoordinatorConfig,
    CoordinatorConfigError,
    GraduationContract,
    LedgerCorruptionError,
    PriceBar,
    ProtectiveStopEngine,
    ReservationError,
    SignalProposal,
    SimulatedBroker,
    SlotLedger,
    SwingExecutionCoordinator,
    SwingSignalProvider,
    ToyCrossoverSignal,
    compute_monotonic_stop,
    count_other_working_close_qty,
    evaluate_live_probation,
    evaluate_paper_graduation,
    evaluate_trailing_stop,
    format_entry_alert,
    format_exit_alert,
    format_stop_sync_alert,
    same_broker_order,
    validate_coordinator_config,
)
from swing_executor.ledger import validate_persisted_counter_json


# ---------------------------------------------------------------------------
# Signals & ToyCrossoverSignal
# ---------------------------------------------------------------------------


def test_toy_crossover_signal_implements_protocol_and_flags_non_production() -> None:
    sig = ToyCrossoverSignal(fast_window=3, slow_window=5, stop_pct=0.05)
    assert isinstance(sig, SwingSignalProvider)
    assert sig.IS_PRODUCTION_STRATEGY is False

    closes = [48.0, 47.5, 47.0, 46.8, 47.2, 50.0]
    bars = {
        "SYN2X_A": [
            PriceBar(f"2026-06-0{i+1}", px - 0.2, px + 0.3, px - 0.4, px, 1000.0)
            for i, px in enumerate(closes)
        ]
    }
    proposals = sig.generate_proposals(bars, as_of="2026-06-06T15:55:00-04:00")
    assert len(proposals) == 1
    p = proposals[0]
    assert p.symbol == "SYN2X_A"
    assert p.direction == "LONG"
    assert p.signal_price == 50.0
    assert p.stop_price == 47.5
    assert p.metadata["demo_only"] is True


def test_signal_proposal_rejects_invalid_prices_and_wrong_side_stop() -> None:
    with pytest.raises(ValueError):
        SignalProposal("SYN2X_A", "LONG", 50.0, 50.5, "demo", "2026-06-06")
    with pytest.raises(ValueError):
        SignalProposal("SYN2X_A", "SHORT", 50.0, 49.5, "demo", "2026-06-06")
    with pytest.raises(ValueError):
        SignalProposal("SYN2X_A", "LONG", float("nan"), 48.0, "demo", "2026-06-06")


# ---------------------------------------------------------------------------
# Protective Stop Engine: fresh reads, timeout fail-closed, partial-fill races
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_arm_and_rearm_fail_closed_on_broker_timeout() -> None:
    broker = SimulatedBroker(account="SIM1")
    broker.set_position("SYN2X_A", 100, avg_cost=50.0)
    engine = ProtectiveStopEngine(broker=broker, account="SIM1", client_id=77, broker_timeout_seconds=0.5)

    # Timeout during open-orders read -> fail closed, zero orders placed.
    broker.fail_open_orders_exc = asyncio.TimeoutError("open orders timed out")
    res_orders = await engine.arm_protective_stop(
        symbol="SYN2X_A", direction="LONG", stop_price=47.5, managed_qty=100, trade_key="tk1"
    )
    assert res_orders.uncertain_holdings is True
    assert res_orders.rearm_deferred_uncertain_qty is True
    assert res_orders.stop_rearmed is False
    assert broker.place_order_calls == 0

    # Timeout during positions read -> fail closed, never size from stale pre-cancel qty!
    broker.fail_open_orders_exc = None
    broker.fail_positions_exc = asyncio.TimeoutError("positions timed out")
    res_pos = await engine.rearm_after_exit_attempt(
        symbol="SYN2X_A", direction="LONG", stop_price=47.5, managed_remaining_qty=100, trade_key="tk1"
    )
    assert res_pos.uncertain_holdings is True
    assert res_pos.rearm_deferred_uncertain_qty is True
    assert res_pos.stop_rearmed is False
    assert res_pos.detail["stage"] == "positions_read"
    assert broker.place_order_calls == 0


@pytest.mark.asyncio
async def test_stop_sync_handles_concurrent_external_partial_fill_and_cross_client_oid_collision() -> None:
    broker = SimulatedBroker(account="SIM1")
    broker.set_position("SYN2X_A", 100, avg_cost=50.0)
    engine = ProtectiveStopEngine(broker=broker, account="SIM1", client_id=77)

    arm_res = await engine.arm_protective_stop(
        symbol="SYN2X_A", direction="LONG", stop_price=47.5, managed_qty=100, trade_key="tk1"
    )
    assert arm_res.action == "stop_armed_gtc" and arm_res.order is not None
    owned_stop = arm_res.order
    assert owned_stop.total_quantity == 100

    # Another client places a manual SELL limit with the EXACT SAME order_id as our stop.
    external = broker.add_external_order(
        BrokerOrder(
            account="SIM1",
            symbol="SYN2X_A",
            action="SELL",
            order_type="LMT",
            total_quantity=40,
            client_id=999,
            order_id=owned_stop.order_id,  # deliberate cross-client collision
            order_ref="MANUAL_EXIT",
            limit_price=52.0,
            status="Submitted",
        )
    )
    assert same_broker_order(owned_stop, external) is False

    # Concurrent partial fill: external order fills 15 shares (25 still working); held_qty drops to 85.
    broker.simulate_partial_fill(external, 15)
    assert broker.positions["SYN2X_A"].quantity == 85
    assert external.remaining == 25

    sync_res = await engine.sync_protective_stop(
        symbol="SYN2X_A",
        direction="LONG",
        target_stop_price=49.0,
        managed_qty=100,
        cached_stop=owned_stop,
        mark_price=52.5,
    )
    assert sync_res.action == "stop_synced_gtc" and sync_res.order is not None
    # Uncovered held shares = 85 - 25 = 60. Total working sells = 60 + 25 = 85 (no oversell!).
    assert sync_res.order.remaining == 60
    assert sync_res.order.aux_price == 49.0
    assert sync_res.order.remaining + external.remaining == broker.positions["SYN2X_A"].quantity


@pytest.mark.asyncio
async def test_stop_sync_preserves_cumulative_total_qty_when_stop_partially_filled() -> None:
    broker = SimulatedBroker(account="SIM1")
    broker.set_position("SYN2X_A", 100, avg_cost=50.0)
    engine = ProtectiveStopEngine(broker=broker, account="SIM1", client_id=77)

    arm_res = await engine.arm_protective_stop(
        symbol="SYN2X_A", direction="LONG", stop_price=47.5, managed_qty=100, trade_key="tk1"
    )
    stop_order = arm_res.order
    assert stop_order is not None

    # Stop itself partially fills 30 shares (held drops to 70, stop.filled=30, stop.remaining=70).
    broker.simulate_partial_fill(stop_order, 30)
    # External close order enters for 20 shares -> uncovered remaining = 70 - 20 = 50.
    broker.add_external_order(
        BrokerOrder(
            account="SIM1",
            symbol="SYN2X_A",
            action="SELL",
            order_type="LMT",
            total_quantity=20,
            client_id=999,
            order_id=7777,
            order_ref="MANUAL",
            limit_price=51.0,
        )
    )

    sync_res = await engine.sync_protective_stop(
        symbol="SYN2X_A",
        direction="LONG",
        target_stop_price=48.0,
        managed_qty=70,
        cached_stop=stop_order,
        mark_price=51.0,
    )
    assert sync_res.action == "stop_synced_gtc" and sync_res.order is not None
    # IBKR cumulative totalQuantity = filled (30) + target_remaining (50) = 80.
    assert sync_res.order.total_quantity == 80
    assert sync_res.order.filled == 30
    assert sync_res.order.remaining == 50


@pytest.mark.asyncio
async def test_stop_cancelled_when_external_orders_cover_full_position() -> None:
    broker = SimulatedBroker(account="SIM1")
    broker.set_position("SYN2X_A", 50, avg_cost=50.0)
    engine = ProtectiveStopEngine(broker=broker, account="SIM1", client_id=77)

    arm_res = await engine.arm_protective_stop(
        symbol="SYN2X_A", direction="LONG", stop_price=47.5, managed_qty=50, trade_key="tk1"
    )
    assert arm_res.order is not None
    broker.add_external_order(
        BrokerOrder(
            account="SIM1",
            symbol="SYN2X_A",
            action="SELL",
            order_type="LMT",
            total_quantity=50,
            client_id=999,
            order_id=8888,
            order_ref="MANUAL_FULL",
            limit_price=52.0,
        )
    )
    sync_res = await engine.sync_protective_stop(
        symbol="SYN2X_A",
        direction="LONG",
        target_stop_price=48.0,
        managed_qty=50,
        cached_stop=arm_res.order,
        mark_price=52.0,
    )
    assert sync_res.action == "stop_cancelled_covered_or_flat"
    assert arm_res.order.order_id in sync_res.cancelled_order_ids


@pytest.mark.asyncio
async def test_rearm_skips_when_working_owned_exit_exists_and_dedupe_ignores_external_stops() -> None:
    broker = SimulatedBroker(account="SIM1")
    broker.set_position("SYN2X_A", 50, avg_cost=50.0)
    engine = ProtectiveStopEngine(broker=broker, account="SIM1", client_id=77)

    # Owned working exit blocks stop re-arm.
    broker.add_external_order(
        BrokerOrder(
            account="SIM1",
            symbol="SYN2X_A",
            action="SELL",
            order_type="LMT",
            total_quantity=50,
            client_id=77,
            order_id=3001,
            order_ref="SWING_EXIT:tk1",
            limit_price=51.0,
        )
    )
    rearm_res = await engine.rearm_after_exit_attempt(
        symbol="SYN2X_A", direction="LONG", stop_price=47.5, managed_remaining_qty=50, trade_key="tk1"
    )
    assert rearm_res.action == "rearm_skipped_working_close"
    assert rearm_res.working_close_present is True

    # Dedupe cancels older owned stop but leaves external client's stop untouched.
    broker.orders.clear()
    s1 = broker.add_external_order(
        BrokerOrder("SIM1", "SYN2X_A", "SELL", "STP", 50, client_id=77, order_id=10, order_ref="SWING_STOP:a", aux_price=47.0)
    )
    s2 = broker.add_external_order(
        BrokerOrder("SIM1", "SYN2X_A", "SELL", "STP", 50, client_id=77, order_id=11, order_ref="SWING_STOP:b", aux_price=47.5)
    )
    ext = broker.add_external_order(
        BrokerOrder("SIM1", "SYN2X_A", "SELL", "STP", 25, client_id=999, order_id=10, order_ref="MANUAL_STP", aux_price=46.0)
    )
    dedupe_res = await engine.dedupe_resting_stops(symbol="SYN2X_A", direction="LONG")
    assert dedupe_res.action == "cancelled_duplicate_stops"
    assert dedupe_res.cancelled_order_ids == (s1.order_id,)
    assert s2.status == "Submitted"
    assert ext.status == "Submitted"


# ---------------------------------------------------------------------------
# SlotLedger: T+1 cash reserve guard, exposure caps, fail-closed corruption
# ---------------------------------------------------------------------------


def test_ledger_t1_cash_reserve_and_slot_sector_caps(tmp_path: Path) -> None:
    ledger = SlotLedger(
        tmp_path / "ledger.sqlite",
        max_concurrent_slots=2,
        max_slot_exposure_fraction=0.25,
        max_deployed_fraction=0.50,
        max_sector_fraction=0.30,
        cash_buffer_fraction=0.02,
        max_orders_per_day=5,
        max_notional_per_day=20_000.0,
        sector_groups={"semis_2x": ("SYN2X_A", "SYN2X_B")},
    )

    # Total cash is $10,000, but $6,000 is ring-fenced and $2,500 is unsettled T+1 proceeds.
    # Spendable cash = min(10000-6000, 9000-6000-2500, 10000-6000) = $500!
    tight_cash = AccountCashState(
        account="SIM1",
        net_liq_usd=10_000.0,
        total_cash_usd=10_000.0,
        settled_cash_usd=9_000.0,
        available_funds_usd=10_000.0,
        ring_fenced_reserve_usd=6_000.0,
        unsettled_t1_proceeds_usd=2_500.0,
    )
    assert tight_cash.spendable_cash_usd == 500.0

    denied_cash = ledger.reserve_entry_slot(
        cash_state=tight_cash, client_id=77, symbol="SYN2X_A", qty=20, limit_price=50.0, session_day="2026-06-06"
    )
    assert denied_cash.allowed is False
    assert denied_cash.reason.startswith("t1_cash_reserve_guard")

    # Sufficient settled cash: slot exposure cap (25% of $10,000 = $2,500) enforced.
    ample_cash = AccountCashState(
        account="SIM1",
        net_liq_usd=10_000.0,
        total_cash_usd=10_000.0,
        settled_cash_usd=10_000.0,
        available_funds_usd=10_000.0,
    )
    denied_slot = ledger.reserve_entry_slot(
        cash_state=ample_cash, client_id=77, symbol="SYN2X_A", qty=60, limit_price=50.0, session_day="2026-06-06"
    )
    assert denied_slot.allowed is False
    assert denied_slot.reason.startswith("per_slot_exposure_cap")

    # First slot ($2,000) succeeds; correlated second slot ($2,000) hits 30% sector cap ($4,000 > $3,000).
    ok1 = ledger.reserve_entry_slot(
        cash_state=ample_cash, client_id=77, symbol="SYN2X_A", qty=40, limit_price=50.0, session_day="2026-06-06"
    )
    assert ok1.allowed is True
    denied_sector = ledger.reserve_entry_slot(
        cash_state=ample_cash, client_id=77, symbol="SYN2X_B", qty=40, limit_price=50.0, session_day="2026-06-06"
    )
    assert denied_sector.allowed is False
    assert denied_sector.reason.startswith("sector_cap_semis_2x")

    # Uncorrelated symbol ($2,000) succeeds; 3rd symbol hits max_concurrent_slots=2.
    ok2 = ledger.reserve_entry_slot(
        cash_state=ample_cash, client_id=77, symbol="SYN2X_C", qty=40, limit_price=50.0, session_day="2026-06-06"
    )
    assert ok2.allowed is True
    denied_slots = ledger.reserve_entry_slot(
        cash_state=ample_cash, client_id=77, symbol="SYN2X_D", qty=10, limit_price=50.0, session_day="2026-06-06"
    )
    assert denied_slots.allowed is False
    assert denied_slots.reason.startswith("max_concurrent_slots")


def test_ledger_fails_closed_on_corrupted_counters_and_json(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.sqlite"
    ledger = SlotLedger(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO daily_counters (account, session_day, orders_opened, notional_opened) VALUES ('SIM1', '2026-06-06', -5, 100.0)"
    )
    conn.commit()
    conn.close()

    with pytest.raises(LedgerCorruptionError):
        ledger.validate_integrity("SIM1", "2026-06-06")

    bad_json = tmp_path / "counters.json"
    bad_json.write_text('{"2026-06-06": {"orders_opened": 2, "notional_opened": NaN}}', encoding="utf-8")
    with pytest.raises(LedgerCorruptionError):
        validate_persisted_counter_json(bad_json)

    null_json = tmp_path / "null_counters.json"
    null_json.write_text('{"2026-06-06": null}', encoding="utf-8")
    with pytest.raises(LedgerCorruptionError):
        validate_persisted_counter_json(null_json)


def test_monotonic_and_trailing_stop_math() -> None:
    assert compute_monotonic_stop("LONG", 48.0, 47.0) == 48.0
    assert compute_monotonic_stop("LONG", 48.0, 49.5) == 49.5
    assert compute_monotonic_stop("SHORT", 52.0, 53.0) == 52.0
    assert compute_monotonic_stop("SHORT", 52.0, 50.5) == 50.5

    peak, gain, stop = evaluate_trailing_stop(
        direction="LONG", entry_price=50.0, prev_peak=50.0, mark=53.0, current_stop=47.5, activate_pct=4.0, distance_pct=2.0
    )
    assert peak == 53.0
    assert gain == 6.0
    assert stop == 51.94


# ---------------------------------------------------------------------------
# Coordinator fail-closed config & kill switch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coordinator_refuses_missing_armed_config_and_bad_caps_and_kill_switch(tmp_path: Path) -> None:
    # Armed without existing config file fails closed.
    with pytest.raises(CoordinatorConfigError):
        validate_coordinator_config(
            CoordinatorConfig(
                account="SIM1",
                client_id=77,
                db_path=tmp_path / "l.sqlite",
                kill_switch_path=tmp_path / "KILL",
                config_path=tmp_path / "missing.yaml",
                arm=True,
            )
        )

    # Negative / NaN caps fail closed.
    with pytest.raises(CoordinatorConfigError):
        validate_coordinator_config(
            CoordinatorConfig(
                account="SIM1",
                client_id=77,
                db_path=tmp_path / "l.sqlite",
                kill_switch_path=tmp_path / "KILL",
                max_notional_per_day=float("nan"),
            )
        )

    # Kill switch stops cycle BEFORE broker is called.
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text("ok: true\n", encoding="utf-8")
    kill_path = tmp_path / "KILL_SWITCH"
    kill_path.write_text("HALT\n", encoding="utf-8")
    broker = SimulatedBroker(account="SIM1")
    coord = SwingExecutionCoordinator(
        config=CoordinatorConfig(
            account="SIM1",
            client_id=77,
            db_path=tmp_path / "l.sqlite",
            kill_switch_path=kill_path,
            config_path=cfg_file,
            arm=True,
        ),
        signal_provider=ToyCrossoverSignal(),
        broker=broker,
    )
    cash = AccountCashState("SIM1", 10_000.0, 10_000.0, 10_000.0, 10_000.0)
    report = await coord.run_entry_cycle(bars_by_symbol={}, cash_state=cash, session_day="2026-06-06", as_of="2026-06-06T15:55:00Z")
    assert report.blocked_reason == "kill_switch_active"
    assert broker.place_order_calls == 0


# ---------------------------------------------------------------------------
# Paper-to-live graduation & neutral-before-fees alert contract
# ---------------------------------------------------------------------------


def test_neutral_before_fees_alert_contract() -> None:
    # Even on a big gross gain OR big gross loss, pending fees MUST stay NEUTRAL.
    pending_win = format_exit_alert(
        symbol="SYN2X_A",
        qty=40,
        entry_price=50.0,
        exit_price=55.0,
        strategy_id="demo",
        reason="trail_stop",
        fees_usd=None,
    )
    assert pending_win["tone"] == "NEUTRAL"
    assert pending_win["fees_verified"] is False
    assert "fees pending" in str(pending_win["text"])
    assert "EXIT_WIN" not in str(pending_win["text"])

    pending_loss = format_exit_alert(
        symbol="SYN2X_A",
        qty=40,
        entry_price=50.0,
        exit_price=45.0,
        strategy_id="demo",
        reason="hard_stop",
        fees_usd=None,
    )
    assert pending_loss["tone"] == "NEUTRAL"
    assert "EXIT_LOSS" not in str(pending_loss["text"])

    # Small gross gain ($0.40) turned into net loss after $0.70 verified fees!
    flipped = format_exit_alert(
        symbol="SYN2X_A",
        qty=40,
        entry_price=50.0,
        exit_price=50.01,
        strategy_id="demo",
        reason="flat_exit",
        fees_usd=0.70,
    )
    assert flipped["tone"] == "LOSS"
    assert flipped["fees_verified"] is True
    assert flipped["net_pnl_usd"] == pytest.approx(-0.30)

    assert "[ENTRY]" in format_entry_alert(symbol="SYN2X_A", qty=40, fill_price=50.0, stop_price=47.5, strategy_id="demo")
    assert "[STOP_SYNC]" in format_stop_sync_alert(symbol="SYN2X_A", old_stop=47.5, new_stop=49.0, remaining_qty=30)


def test_paper_graduation_excludes_proxy_exits_and_never_auto_promotes() -> None:
    trades: list[ClosedPaperTrade] = []
    # 44 verified broker fills across 44 trading days (30 wins, 14 small losses) + 3 proxy exits
    for i in range(44):
        day = f"2026-{(i // 20) + 1:02d}-{(i % 20) + 1:02d}"
        is_win = (i % 3) != 0
        exit_px = 52.0 if is_win else 49.0
        trades.append(
            ClosedPaperTrade(
                trade_key=f"t_{i:03d}",
                symbol="SYN2X_A",
                strategy_id="candidate_demo",
                direction="LONG",
                session_day=day,
                entry_price=50.0,
                exit_price=exit_px,
                signal_price=49.98,
                entry_spread_bps=5.0,
                is_proxy_exit=False,
            )
        )
    for j in range(3):
        trades.append(
            ClosedPaperTrade(
                trade_key=f"proxy_{j}",
                symbol="SYN2X_A",
                strategy_id="candidate_demo",
                direction="LONG",
                session_day="2026-04-01",
                entry_price=50.0,
                exit_price=70.0,
                is_proxy_exit=True,
            )
        )

    rep = evaluate_paper_graduation("candidate_demo", trades, live_net_liq_usd=10_000.0, contract=GraduationContract())
    assert rep.verified_trades == 44
    assert rep.proxy_exits_excluded == 3
    assert rep.eligible_for_human_review is True
    assert rep.requires_human_approval is True
    assert rep.auto_promoted is False

    # Probation sizing transitions: half -> unlocked -> demoted on drawdown.
    p_half = evaluate_live_probation([50.0, 40.0, -20.0], live_net_liq_usd=10_000.0)
    assert p_half.status == "PROBATION_HALF" and p_half.size_multiplier == 0.50

    p_unlock = evaluate_live_probation([60.0] * 7 + [-25.0] * 3, live_net_liq_usd=10_000.0)
    assert p_unlock.status == "FULL_UNLOCKED" and p_unlock.size_multiplier == 1.0

    p_demote = evaluate_live_probation([100.0, -650.0], live_net_liq_usd=10_000.0)
    assert p_demote.status == "PROBATION_DEMOTED" and p_demote.allow_new_entries is False


# ---------------------------------------------------------------------------
# AT06: Strict account binding across coordinator, ledger, snapshot, and broker
# ---------------------------------------------------------------------------


def _crossover_bars(symbol: str = "SYN2X_A") -> dict[str, list[PriceBar]]:
    closes = [48.0, 47.5, 47.0, 46.8, 47.2, 50.0]
    return {
        symbol: [
            PriceBar(f"2026-06-0{i + 1}T15:55:00-04:00", px - 0.2, px + 0.3, px - 0.4, px, 1000.0)
            for i, px in enumerate(closes)
        ]
    }


@pytest.mark.asyncio
async def test_at06_cross_account_cash_snapshot_rejected_before_ledger_or_broker(tmp_path: Path) -> None:
    """Reproduces exact AT06 counterexample: cap=1 must NOT be bypassed by a second account snapshot."""
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text("profile: sim\n", encoding="utf-8")
    db_path = tmp_path / "ledger.sqlite"
    broker = SimulatedBroker(account="SIM_ACCT_A")

    coord = SwingExecutionCoordinator(
        config=CoordinatorConfig(
            account="SIM_ACCT_A",
            client_id=77,
            db_path=db_path,
            kill_switch_path=tmp_path / "KILL_SWITCH",
            config_path=cfg_file,
            arm=True,
            max_orders_per_day=1,
            max_notional_per_day=20_000.0,
        ),
        signal_provider=ToyCrossoverSignal(fast_window=3, slow_window=5, stop_pct=0.05),
        broker=broker,
    )

    cash_a = AccountCashState("SIM_ACCT_A", 10_000.0, 10_000.0, 10_000.0, 10_000.0)
    first = await coord.run_entry_cycle(
        bars_by_symbol=_crossover_bars("SYN2X_A"),
        cash_state=cash_a,
        session_day="2026-06-06",
        as_of="2026-06-06T15:55:00Z",
    )
    assert len(first.reserved) == 1
    assert broker.place_order_calls == 1

    # Control: same account hits daily_order_cap=1.
    second_same = await coord.run_entry_cycle(
        bars_by_symbol=_crossover_bars("SYN2X_A"),
        cash_state=cash_a,
        session_day="2026-06-06",
        as_of="2026-06-06T15:56:00Z",
    )
    assert second_same.reserved == []
    assert second_same.rejected[0]["reason"].startswith("daily_order_cap")
    assert broker.place_order_calls == 1

    # Attack / counterexample: supplying a DIFFERENT account's cash snapshot MUST fail closed
    # BEFORE touching the ledger or broker.
    cash_b = AccountCashState("SIM_ACCT_B", 10_000.0, 10_000.0, 10_000.0, 10_000.0)
    with pytest.raises(AccountMismatchError, match="SIM_ACCT_B"):
        await coord.run_entry_cycle(
            bars_by_symbol=_crossover_bars("SYN2X_A"),
            cash_state=cash_b,
            session_day="2026-06-06",
            as_of="2026-06-06T15:57:00Z",
        )
    assert broker.place_order_calls == 1

    conn = sqlite3.connect(str(db_path))
    try:
        b_res = conn.execute("SELECT COUNT(*) FROM slot_reservations WHERE account = 'SIM_ACCT_B'").fetchone()[0]
        b_cnt = conn.execute("SELECT COUNT(*) FROM daily_counters WHERE account = 'SIM_ACCT_B'").fetchone()[0]
        a_cnt = conn.execute("SELECT orders_opened FROM daily_counters WHERE account = 'SIM_ACCT_A'").fetchone()[0]
    finally:
        conn.close()
    assert b_res == 0
    assert b_cnt == 0
    assert a_cnt == 1


def test_at06_slot_ledger_enforces_account_equality_before_mutation(tmp_path: Path) -> None:
    ledger = SlotLedger(tmp_path / "bound.sqlite", account="SIM_ACCT_A", max_orders_per_day=1)
    cash_b = AccountCashState("SIM_ACCT_B", 10_000.0, 10_000.0, 10_000.0, 10_000.0)

    with pytest.raises(AccountMismatchError):
        ledger.validate_integrity("SIM_ACCT_B", "2026-06-06")

    with pytest.raises(AccountMismatchError):
        ledger.reserve_entry_slot(
            cash_state=cash_b,
            client_id=77,
            symbol="SYN2X_A",
            qty=10,
            limit_price=50.0,
            session_day="2026-06-06",
        )

    # Even an unbound ledger enforces explicit expected_account.
    unbound = SlotLedger(tmp_path / "unbound.sqlite", max_orders_per_day=1)
    with pytest.raises(AccountMismatchError):
        unbound.reserve_entry_slot(
            cash_state=cash_b,
            client_id=77,
            symbol="SYN2X_A",
            qty=10,
            limit_price=50.0,
            session_day="2026-06-06",
            expected_account="SIM_ACCT_A",
        )


@pytest.mark.asyncio
async def test_at06_broker_returned_mismatched_order_account_fails_closed_to_unknown(tmp_path: Path) -> None:
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text("profile: sim\n", encoding="utf-8")
    db_path = tmp_path / "ledger.sqlite"

    class _WrongAccountBroker:
        async def req_open_orders(self) -> list[BrokerOrder]:
            return []

        async def req_positions(self) -> list[BrokerPosition]:
            return []

        async def place_order(self, order: BrokerOrder) -> BrokerOrder:
            return BrokerOrder(
                account="WRONG_ACCT",
                symbol=order.symbol,
                action=order.action,
                order_type=order.order_type,
                total_quantity=order.total_quantity,
                client_id=order.client_id,
                order_id=order.order_id,
                perm_id=99901,
                order_ref=order.order_ref,
                limit_price=order.limit_price,
                status="Filled",
                filled=order.total_quantity,
            )

        async def cancel_order(self, order: BrokerOrder) -> None:
            return None

    coord = SwingExecutionCoordinator(
        config=CoordinatorConfig(
            account="SIM_ACCT_A",
            client_id=77,
            db_path=db_path,
            kill_switch_path=tmp_path / "KILL_SWITCH",
            config_path=cfg_file,
            arm=True,
        ),
        signal_provider=ToyCrossoverSignal(fast_window=3, slow_window=5, stop_pct=0.05),
        broker=_WrongAccountBroker(),
    )
    cash_a = AccountCashState("SIM_ACCT_A", 10_000.0, 10_000.0, 10_000.0, 10_000.0)
    with pytest.raises(AccountMismatchError, match="WRONG_ACCT"):
        await coord.run_entry_cycle(
            bars_by_symbol=_crossover_bars("SYN2X_A"),
            cash_state=cash_a,
            session_day="2026-06-06",
            as_of="2026-06-06T15:55:00Z",
        )

    rows, _, _ = coord.ledger.validate_integrity("SIM_ACCT_A", "2026-06-06")
    assert len(rows) == 1
    assert rows[0].status == "UNKNOWN"


# ---------------------------------------------------------------------------
# AT07: Immediate Filled & partial-fill reconciliation on generic BrokerStopClient
# ---------------------------------------------------------------------------


class _ProtocolImmediateFillBroker:
    """Strict 4-method BrokerStopClient wrapper with NO simulate_partial_fill attribute."""

    def __init__(self, *, account: str, fill_qty_override: int | None = None, post_status: str | None = None) -> None:
        self._inner = SimulatedBroker(account=account)
        self._fill_qty_override = fill_qty_override
        self._post_status = post_status

    async def req_open_orders(self) -> list[BrokerOrder]:
        return await self._inner.req_open_orders()

    async def req_positions(self) -> list[BrokerPosition]:
        return await self._inner.req_positions()

    async def place_order(self, order: BrokerOrder) -> BrokerOrder:
        placed = await self._inner.place_order(order)
        if placed.action.upper() == "BUY":
            fill_n = placed.total_quantity if self._fill_qty_override is None else self._fill_qty_override
            if fill_n > 0:
                self._inner.simulate_partial_fill(placed, fill_n)
            if self._post_status is not None:
                placed.status = self._post_status
        return placed

    async def cancel_order(self, order: BrokerOrder) -> None:
        await self._inner.cancel_order(order)


@pytest.mark.asyncio
async def test_at07_generic_protocol_broker_immediate_filled_records_fill_and_arms_stop(tmp_path: Path) -> None:
    """Reproduces exact AT07 counterexample: 4-method BrokerStopClient returning Filled 40/40."""
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text("profile: sim\n", encoding="utf-8")
    db_path = tmp_path / "ledger.sqlite"

    broker = _ProtocolImmediateFillBroker(account="SIM_PROTO")
    assert not hasattr(broker, "simulate_partial_fill")

    coord = SwingExecutionCoordinator(
        config=CoordinatorConfig(
            account="SIM_PROTO",
            client_id=77,
            db_path=db_path,
            kill_switch_path=tmp_path / "KILL_SWITCH",
            config_path=cfg_file,
            arm=True,
        ),
        signal_provider=ToyCrossoverSignal(fast_window=3, slow_window=5, stop_pct=0.05),
        broker=broker,
    )
    cash = AccountCashState("SIM_PROTO", 10_000.0, 10_000.0, 10_000.0, 10_000.0)

    # Default auto_fill_in_sim=False!
    report = await coord.run_entry_cycle(
        bars_by_symbol=_crossover_bars("SYN2X_A"),
        cash_state=cash,
        session_day="2026-06-06",
        as_of="2026-06-06T15:55:00Z",
    )

    assert len(report.reserved) == 1
    assert report.rejected == []
    assert len(report.filled) == 1
    assert report.filled[0]["symbol"] == "SYN2X_A"
    assert report.filled[0]["ordered_qty"] == 40
    assert report.filled[0]["filled_qty"] == 40
    assert report.filled[0]["ledger_status"] == "FILLED"
    assert report.filled[0]["broker_status"] == "Filled"
    assert len(report.alerts) == 1 and "SYN2X_A BUY 40" in report.alerts[0]

    # Ledger must record FILLED with filled_qty=40 (NEVER REJECTED).
    rows, _, _ = coord.ledger.validate_integrity("SIM_PROTO", "2026-06-06")
    assert len(rows) == 1
    assert rows[0].status == "FILLED"
    assert rows[0].filled_qty == 40

    # Protective stop must be armed immediately for all 40 held shares.
    assert len(report.stops_armed) == 1
    stop_res = report.stops_armed[0]
    assert stop_res.action == "stop_armed_gtc"
    assert stop_res.stop_rearmed is True
    assert stop_res.fresh_held_qty == 40
    assert stop_res.order is not None
    assert stop_res.order.order_type == "STP"
    assert stop_res.order.total_quantity == 40
    assert stop_res.order.aux_price == 47.5


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "post_status,expected_ledger_status",
    [
        ("Submitted", "WORKING"),
        ("Cancelled", "PARTIALLY_FILLED"),
    ],
)
async def test_at07_partial_fill_binds_stop_quantity_to_executed_shares(
    tmp_path: Path, post_status: str, expected_ledger_status: str
) -> None:
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text("profile: sim\n", encoding="utf-8")
    broker = _ProtocolImmediateFillBroker(account="SIM_PROTO", fill_qty_override=15, post_status=post_status)

    coord = SwingExecutionCoordinator(
        config=CoordinatorConfig(
            account="SIM_PROTO",
            client_id=77,
            db_path=tmp_path / "ledger.sqlite",
            kill_switch_path=tmp_path / "KILL_SWITCH",
            config_path=cfg_file,
            arm=True,
        ),
        signal_provider=ToyCrossoverSignal(fast_window=3, slow_window=5, stop_pct=0.05),
        broker=broker,
    )
    cash = AccountCashState("SIM_PROTO", 10_000.0, 10_000.0, 10_000.0, 10_000.0)
    report = await coord.run_entry_cycle(
        bars_by_symbol=_crossover_bars("SYN2X_A"),
        cash_state=cash,
        session_day="2026-06-06",
        as_of="2026-06-06T15:55:00Z",
    )

    assert report.rejected == []
    assert len(report.filled) == 1
    assert report.filled[0]["filled_qty"] == 15
    assert report.filled[0]["ordered_qty"] == 40
    assert report.filled[0]["ledger_status"] == expected_ledger_status

    rows, _, _ = coord.ledger.validate_integrity("SIM_PROTO", "2026-06-06")
    assert rows[0].status == expected_ledger_status
    assert rows[0].filled_qty == 15

    # Stop MUST be sized to 15 executed/held shares, NOT 40 ordered shares.
    assert len(report.stops_armed) == 1
    assert report.stops_armed[0].action == "stop_armed_gtc"
    assert report.stops_armed[0].order is not None
    assert report.stops_armed[0].order.total_quantity == 15


@pytest.mark.asyncio
async def test_at07_inconsistent_filled_zero_qty_and_demo_switch_constraint(tmp_path: Path) -> None:
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text("profile: sim\n", encoding="utf-8")
    broker = _ProtocolImmediateFillBroker(account="SIM_PROTO", fill_qty_override=0, post_status="Filled")

    coord = SwingExecutionCoordinator(
        config=CoordinatorConfig(
            account="SIM_PROTO",
            client_id=77,
            db_path=tmp_path / "ledger.sqlite",
            kill_switch_path=tmp_path / "KILL_SWITCH",
            config_path=cfg_file,
            arm=True,
        ),
        signal_provider=ToyCrossoverSignal(fast_window=3, slow_window=5, stop_pct=0.05),
        broker=broker,
    )
    cash = AccountCashState("SIM_PROTO", 10_000.0, 10_000.0, 10_000.0, 10_000.0)

    # Demo-only switch MUST be rejected for non-SimulatedBroker adapters before touching broker.
    with pytest.raises(CoordinatorConfigError, match="SimulatedBroker"):
        await coord.run_entry_cycle(
            bars_by_symbol=_crossover_bars("SYN2X_A"),
            cash_state=cash,
            session_day="2026-06-06",
            as_of="2026-06-06T15:55:00Z",
            auto_fill_in_sim=True,
        )

    # Filled status with filled=0 has no execution evidence -> fail closed to UNKNOWN with rejection reason.
    report = await coord.run_entry_cycle(
        bars_by_symbol=_crossover_bars("SYN2X_A"),
        cash_state=cash,
        session_day="2026-06-06",
        as_of="2026-06-06T15:55:00Z",
    )
    assert report.filled == []
    assert report.stops_armed == []
    assert report.rejected == [{"symbol": "SYN2X_A", "reason": "broker_filled_without_fill_qty"}]
    rows, _, _ = coord.ledger.validate_integrity("SIM_PROTO", "2026-06-06")
    assert rows[0].status == "UNKNOWN"
    assert rows[0].filled_qty == 0
