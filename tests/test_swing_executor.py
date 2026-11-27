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


# ---------------------------------------------------------------------------
# AT08: Durable deferred/async fill reconciliation & idempotent stop arming
# ---------------------------------------------------------------------------


class _ProtocolDeferredBroker:
    """Strict 4-method BrokerStopClient (no req_completed_orders / simulate_partial_fill exposed)."""

    def __init__(self, *, account: str) -> None:
        self._inner = SimulatedBroker(account=account)

    async def req_open_orders(self) -> list[BrokerOrder]:
        return await self._inner.req_open_orders()

    async def req_positions(self) -> list[BrokerPosition]:
        return await self._inner.req_positions()

    async def place_order(self, order: BrokerOrder) -> BrokerOrder:
        return await self._inner.place_order(order)

    async def cancel_order(self, order: BrokerOrder) -> None:
        await self._inner.cancel_order(order)


@pytest.mark.asyncio
async def test_at08_deferred_full_fill_reconciles_arms_gtc_stop_and_stays_idempotent(tmp_path: Path) -> None:
    """Reproduces exact AT08 counterexample: WORKING (0 filled) -> broker fills 40 -> next cycle arms stop."""
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text("profile: sim\n", encoding="utf-8")
    db_path = tmp_path / "ledger.sqlite"
    broker = SimulatedBroker(account="SIM_AT08")

    coord = SwingExecutionCoordinator(
        config=CoordinatorConfig(
            account="SIM_AT08",
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
    cash = AccountCashState("SIM_AT08", 10_000.0, 10_000.0, 10_000.0, 10_000.0)

    # Cycle 1: working limit submitted with 0 filled -> WORKING, 0 stops.
    c1 = await coord.run_entry_cycle(
        bars_by_symbol=_crossover_bars("SYN2X_A"),
        cash_state=cash,
        session_day="2026-06-06",
        as_of="2026-06-06T15:55:00Z",
    )
    assert len(c1.reserved) == 1
    assert c1.filled == []
    assert c1.stops_armed == []
    assert broker.place_order_calls == 1
    entry_order = broker.orders[0]
    assert entry_order.status == "Submitted" and entry_order.filled == 0 and entry_order.total_quantity == 40

    rows1, _, _ = coord.ledger.validate_integrity("SIM_AT08", "2026-06-06")
    assert rows1[0].status == "WORKING"
    assert rows1[0].filled_qty == 0
    assert rows1[0].stop_price == 47.5

    # Async/deferred full fill arrives on broker between cycles.
    broker.simulate_partial_fill(entry_order, entry_order.remaining)
    assert broker.positions["SYN2X_A"].quantity == 40
    assert entry_order.status == "Filled"

    # Cycle 2: reconciles WORKING -> FILLED (40) and immediately arms GTC protective stop for 40 shares.
    c2 = await coord.run_entry_cycle(
        bars_by_symbol={},
        cash_state=cash,
        session_day="2026-06-06",
        as_of="2026-06-06T16:00:00Z",
    )
    assert c2.uncertain_holdings is False
    assert len(c2.reconciled_fills) == 1
    assert len(c2.filled) == 1
    assert c2.reconciled_fills[0]["symbol"] == "SYN2X_A"
    assert c2.reconciled_fills[0]["filled_qty"] == 40
    assert c2.reconciled_fills[0]["ledger_status"] == "FILLED"
    assert len(c2.alerts) == 1 and "SYN2X_A BUY 40" in c2.alerts[0]

    assert len(c2.stops_armed) == 1
    stop_res = c2.stops_armed[0]
    assert stop_res.action == "stop_armed_gtc"
    assert stop_res.stop_rearmed is True
    assert stop_res.fresh_held_qty == 40
    assert stop_res.order is not None
    assert stop_res.order.order_type == "STP"
    assert stop_res.order.tif == "GTC"
    assert stop_res.order.total_quantity == 40
    assert stop_res.order.aux_price == 47.5
    assert broker.place_order_calls == 2

    rows2, _, _ = coord.ledger.validate_integrity("SIM_AT08", "2026-06-06")
    assert rows2[0].status == "FILLED"
    assert rows2[0].filled_qty == 40
    assert rows2[0].stop_armed is True

    # Cycle 3: strictly idempotent — does NOT double-arm stops or re-emit fills.
    c3 = await coord.run_entry_cycle(
        bars_by_symbol={},
        cash_state=cash,
        session_day="2026-06-06",
        as_of="2026-06-06T16:05:00Z",
    )
    assert c3.reconciled_fills == []
    assert c3.filled == []
    assert c3.stops_armed == []
    assert broker.place_order_calls == 2
    working_stops = [o for o in broker.orders if o.order_type == "STP" and o.status == "Submitted"]
    assert len(working_stops) == 1


@pytest.mark.asyncio
async def test_at08_strict_protocol_broker_and_restart_durable_reconcile_working_entries(tmp_path: Path) -> None:
    """Verifies strict 4-method BrokerStopClient reconciliation survives coordinator restart."""
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text("profile: sim\n", encoding="utf-8")
    db_path = tmp_path / "ledger.sqlite"
    broker = _ProtocolDeferredBroker(account="SIM_PROTO_DEF")
    assert not hasattr(broker, "req_completed_orders")

    cfg = CoordinatorConfig(
        account="SIM_PROTO_DEF",
        client_id=77,
        db_path=db_path,
        kill_switch_path=tmp_path / "KILL_SWITCH",
        config_path=cfg_file,
        arm=True,
    )
    coord1 = SwingExecutionCoordinator(
        config=cfg,
        signal_provider=ToyCrossoverSignal(fast_window=3, slow_window=5, stop_pct=0.05),
        broker=broker,
    )
    cash = AccountCashState("SIM_PROTO_DEF", 10_000.0, 10_000.0, 10_000.0, 10_000.0)
    c1 = await coord1.run_entry_cycle(
        bars_by_symbol=_crossover_bars("SYN2X_A"),
        cash_state=cash,
        session_day="2026-06-06",
        as_of="2026-06-06T15:55:00Z",
    )
    assert len(c1.reserved) == 1 and c1.stops_armed == []

    # Broker fills all 40 shares asynchronously; entry order leaves req_open_orders(), position=40.
    broker._inner.simulate_partial_fill(broker._inner.orders[0], 40)

    # Fresh coordinator instance (simulated process restart) calls reconcile_working_entries() directly.
    coord2 = SwingExecutionCoordinator(
        config=cfg,
        signal_provider=ToyCrossoverSignal(fast_window=3, slow_window=5, stop_pct=0.05),
        broker=broker,
    )
    rec = await coord2.reconcile_working_entries(session_day="2026-06-06", as_of="2026-06-06T16:00:00Z")
    assert rec.uncertain_holdings is False
    assert len(rec.reconciled_fills) == 1
    assert rec.reconciled_fills[0]["filled_qty"] == 40
    assert rec.reconciled_fills[0]["ledger_status"] == "FILLED"
    assert len(rec.stops_armed) == 1
    assert rec.stops_armed[0].action == "stop_armed_gtc"
    assert rec.stops_armed[0].order is not None
    assert rec.stops_armed[0].order.total_quantity == 40
    assert rec.stops_armed[0].order.aux_price == 47.5

    # Repeating reconcile_working_entries() is completely idempotent.
    calls_before = broker._inner.place_order_calls
    rec2 = await coord2.reconcile_working_entries(session_day="2026-06-06", as_of="2026-06-06T16:01:00Z")
    assert rec2.reconciled_fills == []
    assert rec2.stops_armed == []
    assert broker._inner.place_order_calls == calls_before


@pytest.mark.asyncio
async def test_at08_partial_fill_growth_resizes_existing_stop_without_duplicating(tmp_path: Path) -> None:
    """Partial fill (15/40) arms 15-share stop; subsequent remaining fill (40/40) resizes stop to 40."""
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text("profile: sim\n", encoding="utf-8")
    broker = SimulatedBroker(account="SIM_PARTIAL")

    coord = SwingExecutionCoordinator(
        config=CoordinatorConfig(
            account="SIM_PARTIAL",
            client_id=77,
            db_path=tmp_path / "ledger.sqlite",
            kill_switch_path=tmp_path / "KILL_SWITCH",
            config_path=cfg_file,
            arm=True,
        ),
        signal_provider=ToyCrossoverSignal(fast_window=3, slow_window=5, stop_pct=0.05),
        broker=broker,
    )
    cash = AccountCashState("SIM_PARTIAL", 10_000.0, 10_000.0, 10_000.0, 10_000.0)
    await coord.run_entry_cycle(
        bars_by_symbol=_crossover_bars("SYN2X_A"),
        cash_state=cash,
        session_day="2026-06-06",
        as_of="2026-06-06T15:55:00Z",
    )
    entry_order = broker.orders[0]

    # First deferred partial fill: 15 shares (25 still working).
    broker.simulate_partial_fill(entry_order, 15)
    c2 = await coord.run_entry_cycle(
        bars_by_symbol={}, cash_state=cash, session_day="2026-06-06", as_of="2026-06-06T15:56:00Z"
    )
    assert len(c2.reconciled_fills) == 1
    assert c2.reconciled_fills[0]["filled_qty"] == 15
    assert c2.reconciled_fills[0]["ledger_status"] == "WORKING"
    assert len(c2.stops_armed) == 1
    assert c2.stops_armed[0].action == "stop_armed_gtc"
    assert c2.stops_armed[0].order is not None and c2.stops_armed[0].order.total_quantity == 15

    # Remaining 25 shares fill (total 40).
    broker.simulate_partial_fill(entry_order, 25)
    c3 = await coord.run_entry_cycle(
        bars_by_symbol={}, cash_state=cash, session_day="2026-06-06", as_of="2026-06-06T15:57:00Z"
    )
    assert len(c3.reconciled_fills) == 1
    assert c3.reconciled_fills[0]["filled_qty"] == 40
    assert c3.reconciled_fills[0]["newly_filled_qty"] == 25
    assert c3.reconciled_fills[0]["ledger_status"] == "FILLED"
    assert len(c3.stops_armed) == 1
    assert c3.stops_armed[0].action == "stop_synced_gtc"
    assert c3.stops_armed[0].order is not None and c3.stops_armed[0].order.total_quantity == 40
    working_stops = [o for o in broker.orders if o.order_type == "STP" and o.status == "Submitted"]
    assert len(working_stops) == 1 and working_stops[0].total_quantity == 40


@pytest.mark.asyncio
@pytest.mark.parametrize("strict_protocol", [False, True])
async def test_at08_cancelled_without_fill_transitions_to_cancelled_and_frees_slot(
    tmp_path: Path, strict_protocol: bool
) -> None:
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text("profile: sim\n", encoding="utf-8")
    broker = _ProtocolDeferredBroker(account="SIM_CANCEL") if strict_protocol else SimulatedBroker(account="SIM_CANCEL")
    inner = broker._inner if strict_protocol else broker

    coord = SwingExecutionCoordinator(
        config=CoordinatorConfig(
            account="SIM_CANCEL",
            client_id=77,
            db_path=tmp_path / "ledger.sqlite",
            kill_switch_path=tmp_path / "KILL_SWITCH",
            config_path=cfg_file,
            arm=True,
            max_concurrent_slots=1,
            max_orders_per_day=2,
        ),
        signal_provider=ToyCrossoverSignal(fast_window=3, slow_window=5, stop_pct=0.05),
        broker=broker,
    )
    cash = AccountCashState("SIM_CANCEL", 10_000.0, 10_000.0, 10_000.0, 10_000.0)
    c1 = await coord.run_entry_cycle(
        bars_by_symbol=_crossover_bars("SYN2X_A"),
        cash_state=cash,
        session_day="2026-06-06",
        as_of="2026-06-06T15:55:00Z",
    )
    assert len(c1.reserved) == 1

    # Entry order expires/cancels with 0 fill and 0 shares held.
    await inner.cancel_order(inner.orders[0])

    # Cycle 2 reconciles SYN2X_A -> CANCELLED, freeing the single concurrent slot for SYN2X_B!
    c2 = await coord.run_entry_cycle(
        bars_by_symbol=_crossover_bars("SYN2X_B"),
        cash_state=cash,
        session_day="2026-06-06",
        as_of="2026-06-06T15:56:00Z",
    )
    assert c2.uncertain_holdings is False
    assert len(c2.reconciled_cancellations) == 1
    assert c2.reconciled_cancellations[0]["symbol"] == "SYN2X_A"
    assert c2.reconciled_cancellations[0]["ledger_status"] == "CANCELLED"
    assert c2.stops_armed == []
    assert len(c2.reserved) == 1

    rows, _, _ = coord.ledger.validate_integrity("SIM_CANCEL", "2026-06-06")
    by_sym = {r.symbol: r for r in rows}
    assert by_sym["SYN2X_A"].status == "CANCELLED"
    assert by_sym["SYN2X_A"].filled_qty == 0
    assert by_sym["SYN2X_B"].status == "WORKING"


@pytest.mark.asyncio
async def test_at08_broker_timeout_and_ambiguous_position_fail_closed(tmp_path: Path) -> None:
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text("profile: sim\n", encoding="utf-8")
    broker = SimulatedBroker(account="SIM_TO")

    coord = SwingExecutionCoordinator(
        config=CoordinatorConfig(
            account="SIM_TO",
            client_id=77,
            db_path=tmp_path / "ledger.sqlite",
            kill_switch_path=tmp_path / "KILL_SWITCH",
            config_path=cfg_file,
            arm=True,
            max_concurrent_slots=2,
            max_orders_per_day=5,
        ),
        signal_provider=ToyCrossoverSignal(fast_window=3, slow_window=5, stop_pct=0.05),
        broker=broker,
        broker_timeout_seconds=0.5,
    )
    cash = AccountCashState("SIM_TO", 10_000.0, 10_000.0, 10_000.0, 10_000.0)
    await coord.run_entry_cycle(
        bars_by_symbol=_crossover_bars("SYN2X_A"),
        cash_state=cash,
        session_day="2026-06-06",
        as_of="2026-06-06T15:55:00Z",
    )
    assert broker.place_order_calls == 1

    # Broker positions read times out -> fail closed with uncertain_holdings=True and zero new orders.
    broker.fail_positions_exc = asyncio.TimeoutError("positions read timed out")
    c_timeout = await coord.run_entry_cycle(
        bars_by_symbol=_crossover_bars("SYN2X_A"),
        cash_state=cash,
        session_day="2026-06-06",
        as_of="2026-06-06T15:56:00Z",
    )
    assert c_timeout.uncertain_holdings is True
    assert "SYN2X_A" in c_timeout.uncertain_symbols
    assert c_timeout.blocked_reason == "reconciliation_uncertain_holdings"
    assert c_timeout.reserved == []
    assert c_timeout.stops_armed == []
    assert c_timeout.rejected == [{"symbol": "SYN2X_A", "reason": "reconciliation_uncertain_holdings"}]
    assert broker.place_order_calls == 1
    rows, _, _ = coord.ledger.validate_integrity("SIM_TO", "2026-06-06")
    assert rows[0].status == "WORKING"

    # Ambiguous excess position on strict 4-method broker -> fail closed for that symbol only.
    proto_broker = _ProtocolDeferredBroker(account="SIM_AMB")
    coord_amb = SwingExecutionCoordinator(
        config=CoordinatorConfig(
            account="SIM_AMB",
            client_id=77,
            db_path=tmp_path / "amb.sqlite",
            kill_switch_path=tmp_path / "KILL_SWITCH",
            config_path=cfg_file,
            arm=True,
            max_concurrent_slots=2,
            max_orders_per_day=5,
        ),
        signal_provider=ToyCrossoverSignal(fast_window=3, slow_window=5, stop_pct=0.05),
        broker=proto_broker,
    )
    cash_amb = AccountCashState("SIM_AMB", 10_000.0, 10_000.0, 10_000.0, 10_000.0)
    await coord_amb.run_entry_cycle(
        bars_by_symbol=_crossover_bars("SYN2X_A"),
        cash_state=cash_amb,
        session_day="2026-06-06",
        as_of="2026-06-06T15:55:00Z",
    )
    # Entry order disappears from open orders, but broker holds 100 shares (> 40 ordered) with no order-status endpoint.
    proto_broker._inner.orders[0].status = "Cancelled"
    proto_broker._inner.set_position("SYN2X_A", 100, avg_cost=50.0)

    bars_both = {**_crossover_bars("SYN2X_A"), **_crossover_bars("SYN2X_B")}
    c_amb = await coord_amb.run_entry_cycle(
        bars_by_symbol=bars_both,
        cash_state=cash_amb,
        session_day="2026-06-06",
        as_of="2026-06-06T15:56:00Z",
    )
    assert c_amb.uncertain_holdings is True
    assert c_amb.uncertain_symbols == ["SYN2X_A"]
    assert {"symbol": "SYN2X_A", "reason": "reconciliation_uncertain_holdings"} in c_amb.rejected
    assert c_amb.stops_armed == []
    # Unrelated symbol SYN2X_B is still admitted cleanly while SYN2X_A remains fail-closed WORKING.
    amb_rows, _, _ = coord_amb.ledger.validate_integrity("SIM_AMB", "2026-06-06")
    amb_by_sym = {r.symbol: r for r in amb_rows}
    assert amb_by_sym["SYN2X_A"].status == "WORKING"
    assert amb_by_sym["SYN2X_B"].status == "WORKING"


@pytest.mark.asyncio
async def test_at08_retries_stop_protection_after_transient_stop_rejection(tmp_path: Path) -> None:
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text("profile: sim\n", encoding="utf-8")
    broker = SimulatedBroker(account="SIM_RETRY")

    coord = SwingExecutionCoordinator(
        config=CoordinatorConfig(
            account="SIM_RETRY",
            client_id=77,
            db_path=tmp_path / "ledger.sqlite",
            kill_switch_path=tmp_path / "KILL_SWITCH",
            config_path=cfg_file,
            arm=True,
        ),
        signal_provider=ToyCrossoverSignal(fast_window=3, slow_window=5, stop_pct=0.05),
        broker=broker,
    )
    cash = AccountCashState("SIM_RETRY", 10_000.0, 10_000.0, 10_000.0, 10_000.0)
    await coord.run_entry_cycle(
        bars_by_symbol=_crossover_bars("SYN2X_A"),
        cash_state=cash,
        session_day="2026-06-06",
        as_of="2026-06-06T15:55:00Z",
    )
    broker.simulate_partial_fill(broker.orders[0], 40)

    # First reconciliation cycle: broker transiently rejects the stop order -> stop_armed remains False.
    broker.reject_next_place_status = "Inactive"
    c2 = await coord.run_entry_cycle(
        bars_by_symbol={}, cash_state=cash, session_day="2026-06-06", as_of="2026-06-06T15:56:00Z"
    )
    assert len(c2.reconciled_fills) == 1
    assert c2.stops_armed[0].action == "stop_arm_rejected"
    rows2, _, _ = coord.ledger.validate_integrity("SIM_RETRY", "2026-06-06")
    assert rows2[0].status == "FILLED"
    assert rows2[0].stop_armed is False

    # Next cycle automatically retries arming protection without double-reporting the fill.
    c3 = await coord.run_entry_cycle(
        bars_by_symbol={}, cash_state=cash, session_day="2026-06-06", as_of="2026-06-06T15:57:00Z"
    )
    assert c3.reconciled_fills == []
    assert len(c3.stops_armed) == 1
    assert c3.stops_armed[0].action == "stop_armed_gtc"
    assert c3.stops_armed[0].order is not None and c3.stops_armed[0].order.total_quantity == 40
    rows3, _, _ = coord.ledger.validate_integrity("SIM_RETRY", "2026-06-06")
    assert rows3[0].stop_armed is True

    # Cycle 4 is a complete idempotent no-op.
    calls_before = broker.place_order_calls
    c4 = await coord.run_entry_cycle(
        bars_by_symbol={}, cash_state=cash, session_day="2026-06-06", as_of="2026-06-06T15:58:00Z"
    )
    assert c4.stops_armed == []
    assert broker.place_order_calls == calls_before


# ---------------------------------------------------------------------------
# AT09: Rejected stop resize must remain retryable across fresh restart
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("strict_protocol", [False, True])
async def test_at09_rejected_stop_resize_retries_after_fresh_restart_without_duplicates(
    tmp_path: Path, strict_protocol: bool
) -> None:
    """Reproduces AT09: 40-share reserve -> 15 fill + 15 stop -> +25 fill + Inactive resize -> restart retries to 40."""
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text("profile: sim\n", encoding="utf-8")
    db_path = tmp_path / "ledger.sqlite"
    account = "SIM_AT09_PROTO" if strict_protocol else "SIM_AT09"
    broker = _ProtocolDeferredBroker(account=account) if strict_protocol else SimulatedBroker(account=account)
    inner = broker._inner if strict_protocol else broker

    cfg = CoordinatorConfig(
        account=account,
        client_id=77,
        db_path=db_path,
        kill_switch_path=tmp_path / "KILL_SWITCH",
        config_path=cfg_file,
        arm=True,
    )
    coord1 = SwingExecutionCoordinator(
        config=cfg,
        signal_provider=ToyCrossoverSignal(fast_window=3, slow_window=5, stop_pct=0.05),
        broker=broker,
    )
    cash = AccountCashState(account, 10_000.0, 10_000.0, 10_000.0, 10_000.0)

    # Cycle 1: Reserve 40 synthetic shares; entry order stays working with 0 filled.
    c1 = await coord1.run_entry_cycle(
        bars_by_symbol=_crossover_bars("SYN2X_A"),
        cash_state=cash,
        session_day="2026-06-06",
        as_of="2026-06-06T15:55:00Z",
    )
    assert len(c1.reserved) == 1
    assert c1.stops_armed == []
    entry_order = inner.orders[0]
    assert entry_order.total_quantity == 40 and entry_order.filled == 0

    # Partial fill of 15 shares -> Cycle 2 arms a confirmed 15-share GTC protective stop.
    inner.simulate_partial_fill(entry_order, 15)
    c2 = await coord1.run_entry_cycle(
        bars_by_symbol={},
        cash_state=cash,
        session_day="2026-06-06",
        as_of="2026-06-06T15:56:00Z",
    )
    assert len(c2.reconciled_fills) == 1
    assert c2.reconciled_fills[0]["filled_qty"] == 15
    assert len(c2.stops_armed) == 1
    assert c2.stops_armed[0].action == "stop_armed_gtc"
    assert c2.stops_armed[0].order is not None and c2.stops_armed[0].order.total_quantity == 15
    stop_order_id = c2.stops_armed[0].order.order_id
    stop_perm_id = c2.stops_armed[0].order.perm_id

    rows2, _, _ = coord1.ledger.validate_integrity(account, "2026-06-06")
    assert rows2[0].status == "WORKING"
    assert rows2[0].filled_qty == 15
    assert rows2[0].stop_armed is True
    assert rows2[0].stop_covered_qty == 15

    # Remaining 25 shares fill (total 40), and broker rejects the stop resize as Inactive.
    inner.simulate_partial_fill(entry_order, 25)
    assert inner.positions["SYN2X_A"].quantity == 40
    inner.reject_next_place_status = "Inactive"

    c3 = await coord1.run_entry_cycle(
        bars_by_symbol={},
        cash_state=cash,
        session_day="2026-06-06",
        as_of="2026-06-06T15:57:00Z",
    )
    assert len(c3.reconciled_fills) == 1
    assert c3.reconciled_fills[0]["filled_qty"] == 40
    assert c3.reconciled_fills[0]["newly_filled_qty"] == 25
    assert c3.reconciled_fills[0]["ledger_status"] == "FILLED"
    assert len(c3.stops_armed) == 1
    assert c3.stops_armed[0].action == "stop_sync_rejected"
    assert c3.stops_armed[0].stop_rearmed is False

    # Ledger MUST NOT mark full protection complete: stop_armed=False, stop_covered_qty=15 (< 40).
    rows3, _, _ = coord1.ledger.validate_integrity(account, "2026-06-06")
    assert rows3[0].status == "FILLED"
    assert rows3[0].filled_qty == 40
    assert rows3[0].stop_armed is False
    assert rows3[0].stop_covered_qty == 15
    reconcilable_after_reject = coord1.ledger.reconcilable_reservations(account, "2026-06-06")
    assert len(reconcilable_after_reject) == 1
    assert reconcilable_after_reject[0].reservation_id == rows3[0].reservation_id

    working_stops_before_restart = [o for o in inner.orders if o.order_type == "STP" and o.status == "Submitted"]
    assert len(working_stops_before_restart) == 1
    assert working_stops_before_restart[0].total_quantity == 15

    # Fresh coordinator restart against the same SQLite database + broker.
    coord2 = SwingExecutionCoordinator(
        config=cfg,
        signal_provider=ToyCrossoverSignal(fast_window=3, slow_window=5, stop_pct=0.05),
        broker=broker,
    )
    c4 = await coord2.run_entry_cycle(
        bars_by_symbol={},
        cash_state=cash,
        session_day="2026-06-06",
        as_of="2026-06-06T15:58:00Z",
    )
    assert c4.uncertain_holdings is False
    assert c4.reconciled_fills == []
    assert len(c4.stops_armed) == 1
    assert c4.stops_armed[0].action == "stop_synced_gtc"
    assert c4.stops_armed[0].stop_rearmed is True
    assert c4.stops_armed[0].order is not None
    assert c4.stops_armed[0].order.total_quantity == 40
    assert c4.stops_armed[0].order.tif == "GTC"
    assert c4.stops_armed[0].order.order_id == stop_order_id
    assert c4.stops_armed[0].order.perm_id == stop_perm_id

    # Exactly one 40-share protective stop is working (resized in place, zero duplicates).
    working_stops_after_retry = [o for o in inner.orders if o.order_type == "STP" and o.status == "Submitted"]
    assert len(working_stops_after_retry) == 1
    assert working_stops_after_retry[0].total_quantity == 40
    assert working_stops_after_retry[0].remaining == 40

    rows4, _, _ = coord2.ledger.validate_integrity(account, "2026-06-06")
    assert rows4[0].status == "FILLED"
    assert rows4[0].filled_qty == 40
    assert rows4[0].stop_armed is True
    assert rows4[0].stop_covered_qty == 40
    assert coord2.ledger.reconcilable_reservations(account, "2026-06-06") == []

    # Next cycle after another fresh restart is a strict idempotent no-op.
    calls_before = inner.place_order_calls
    coord3 = SwingExecutionCoordinator(
        config=cfg,
        signal_provider=ToyCrossoverSignal(fast_window=3, slow_window=5, stop_pct=0.05),
        broker=broker,
    )
    c5 = await coord3.run_entry_cycle(
        bars_by_symbol={},
        cash_state=cash,
        session_day="2026-06-06",
        as_of="2026-06-06T15:59:00Z",
    )
    assert c5.reconciled_fills == []
    assert c5.stops_armed == []
    assert inner.place_order_calls == calls_before
    working_stops_final = [o for o in inner.orders if o.order_type == "STP" and o.status == "Submitted"]
    assert len(working_stops_final) == 1 and working_stops_final[0].total_quantity == 40


def test_at09_ledger_stop_covered_qty_invariants_and_corruption_guards(tmp_path: Path) -> None:
    """Ledger enforces stop_covered_qty <= filled_qty, clears stale stop_armed, and stays retryable."""
    db_path = tmp_path / "at09_ledger.sqlite"
    ledger = SlotLedger(db_path, account="SIM_INV")
    cash = AccountCashState("SIM_INV", 10_000.0, 10_000.0, 10_000.0, 10_000.0)
    dec = ledger.reserve_entry_slot(
        cash_state=cash,
        client_id=77,
        symbol="SYN2X_A",
        qty=40,
        limit_price=50.0,
        session_day="2026-06-06",
        stop_price=47.5,
        now_iso="2026-06-06T15:55:00Z",
    )
    assert dec.allowed and dec.reservation is not None
    rid = dec.reservation.reservation_id

    # Armed for initial 15-share partial fill.
    ledger.update_reservation_status(
        rid,
        status="WORKING",
        filled_qty=15,
        stop_armed=True,
        stop_covered_qty=15,
        now_iso="2026-06-06T15:56:00Z",
    )
    rows, _, _ = ledger.validate_integrity("SIM_INV", "2026-06-06")
    assert rows[0].stop_armed is True and rows[0].stop_covered_qty == 15

    # Refuses claiming stop_armed=True while stop_covered_qty < filled_qty.
    with pytest.raises(ReservationError, match="stop_covered_qty"):
        ledger.update_reservation_status(
            rid,
            status="FILLED",
            filled_qty=40,
            stop_armed=True,
            stop_covered_qty=15,
            now_iso="2026-06-06T15:57:00Z",
        )

    # Refuses stop_covered_qty > filled_qty.
    with pytest.raises(ReservationError, match="exceeds filled_qty"):
        ledger.update_reservation_status(
            rid,
            status="WORKING",
            filled_qty=15,
            stop_covered_qty=20,
            now_iso="2026-06-06T15:57:00Z",
        )

    # Growing filled_qty past stop_covered_qty automatically clears stop_armed even if omitted.
    ledger.update_reservation_status(
        rid,
        status="FILLED",
        filled_qty=40,
        now_iso="2026-06-06T15:57:00Z",
    )
    rows_after, _, _ = ledger.validate_integrity("SIM_INV", "2026-06-06")
    assert rows_after[0].status == "FILLED"
    assert rows_after[0].filled_qty == 40
    assert rows_after[0].stop_armed is False
    assert rows_after[0].stop_covered_qty == 15
    assert [r.reservation_id for r in ledger.reconcilable_reservations("SIM_INV", "2026-06-06")] == [rid]

    # Even if an external/stale DB write set stop_armed=1 with stop_covered_qty=15 < 40, still reconcilable.
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "UPDATE slot_reservations SET stop_armed = 1, stop_covered_qty = 15 WHERE reservation_id = ?",
            (rid,),
        )
        conn.commit()
    assert [r.reservation_id for r in ledger.reconcilable_reservations("SIM_INV", "2026-06-06")] == [rid]

    # Corrupted stop_covered_qty > filled_qty fails closed on validate_integrity.
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "UPDATE slot_reservations SET stop_covered_qty = 45 WHERE reservation_id = ?",
            (rid,),
        )
        conn.commit()
    with pytest.raises(LedgerCorruptionError, match="stop_covered_qty"):
        ledger.validate_integrity("SIM_INV", "2026-06-06")


# ---------------------------------------------------------------------------
# AT10: Legacy ledger upgrade must never infer stop_covered_qty from stop_armed
# ---------------------------------------------------------------------------


def _create_legacy_pre_at09_db(
    db_path: Path,
    *,
    account: str,
    client_id: int = 77,
    symbol: str = "SYN2X_A",
    qty: int = 40,
    filled_qty: int = 40,
    status: str = "FILLED",
    stop_armed: int = 1,
    stop_price: float = 47.5,
    order_id: int = 5001,
    perm_id: int = 9001,
    reservation_id: str = "res_legacy_at10",
    session_day: str = "2026-06-06",
) -> str:
    """Create a genuine pre-AT09 SQLite ledger (without stop_covered_qty column)."""
    order_ref = f"SWING_ENTRY:{symbol}:{reservation_id}"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE slot_reservations (
                reservation_id TEXT PRIMARY KEY,
                account TEXT NOT NULL,
                client_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                qty INTEGER NOT NULL,
                limit_price REAL NOT NULL,
                notional_usd REAL NOT NULL,
                order_ref TEXT NOT NULL,
                status TEXT NOT NULL,
                session_day TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                order_id INTEGER,
                perm_id INTEGER,
                filled_qty INTEGER NOT NULL DEFAULT 0,
                stop_price REAL,
                strategy_id TEXT NOT NULL DEFAULT '',
                stop_armed INTEGER NOT NULL DEFAULT 0,
                UNIQUE(account, order_ref)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE daily_counters (
                account TEXT NOT NULL,
                session_day TEXT NOT NULL,
                orders_opened INTEGER NOT NULL,
                notional_opened REAL NOT NULL,
                PRIMARY KEY(account, session_day)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO slot_reservations (
                reservation_id, account, client_id, symbol, side, qty, limit_price,
                notional_usd, order_ref, status, session_day, created_at, updated_at,
                order_id, perm_id, filled_qty, stop_price, strategy_id, stop_armed
            ) VALUES (?, ?, ?, ?, 'BUY', ?, 50.0, ?, ?, ?, ?, '2026-06-06T15:55:00Z',
                      '2026-06-06T15:57:00Z', ?, ?, ?, ?, 'toy_sma_crossover', ?)
            """,
            (
                reservation_id,
                account,
                client_id,
                symbol,
                qty,
                round(qty * 50.0, 2),
                order_ref,
                status,
                session_day,
                order_id,
                perm_id,
                filled_qty,
                stop_price,
                stop_armed,
            ),
        )
        conn.execute(
            """
            INSERT INTO daily_counters (account, session_day, orders_opened, notional_opened)
            VALUES (?, ?, 1, ?)
            """,
            (account, session_day, round(qty * 50.0, 2)),
        )
        conn.commit()
    return order_ref


@pytest.mark.asyncio
@pytest.mark.parametrize("strict_protocol", [False, True])
async def test_at10_legacy_db_upgrade_resizes_underprotected_stop_on_first_reconciliation(
    tmp_path: Path, strict_protocol: bool
) -> None:
    """Reproduces AT10: legacy DB (FILLED/40/stop_armed=1, no stop_covered_qty) + 15-share stop -> resizes to 40."""
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text("profile: sim\n", encoding="utf-8")
    db_path = tmp_path / "legacy_at10.sqlite"
    account = "SIM_AT10_PROTO" if strict_protocol else "SIM_AT10"

    entry_ref = _create_legacy_pre_at09_db(
        db_path,
        account=account,
        client_id=77,
        symbol="SYN2X_A",
        qty=40,
        filled_qty=40,
        status="FILLED",
        stop_armed=1,
        stop_price=47.5,
        order_id=5001,
        perm_id=9001,
        reservation_id="res_legacy_at10",
    )

    # Confirm the fixture is genuinely pre-AT09 before current code opens it.
    with sqlite3.connect(str(db_path)) as conn:
        legacy_cols = {row[1] for row in conn.execute("PRAGMA table_info(slot_reservations)").fetchall()}
        legacy_row = conn.execute(
            "SELECT status, qty, filled_qty, stop_armed FROM slot_reservations WHERE reservation_id = ?",
            ("res_legacy_at10",),
        ).fetchone()
    assert "stop_covered_qty" not in legacy_cols
    assert legacy_row == ("FILLED", 40, 40, 1)

    broker = _ProtocolDeferredBroker(account=account) if strict_protocol else SimulatedBroker(account=account)
    inner = broker._inner if strict_protocol else broker
    inner.set_position("SYN2X_A", 40, avg_cost=50.0)
    inner.orders.append(
        BrokerOrder(
            account=account,
            symbol="SYN2X_A",
            action="BUY",
            order_type="LMT",
            total_quantity=40,
            client_id=77,
            order_id=5001,
            perm_id=9001,
            order_ref=entry_ref,
            status="Filled",
            limit_price=50.0,
            filled=40,
            tif="DAY",
        )
    )
    inner.orders.append(
        BrokerOrder(
            account=account,
            symbol="SYN2X_A",
            action="SELL",
            order_type="STP",
            total_quantity=15,
            client_id=77,
            order_id=1001,
            perm_id=9002,
            order_ref="SWING_STOP:res_legacy_at10",
            status="Submitted",
            aux_price=47.5,
            filled=0,
            tif="GTC",
        )
    )

    cfg = CoordinatorConfig(
        account=account,
        client_id=77,
        db_path=db_path,
        kill_switch_path=tmp_path / "KILL_SWITCH",
        config_path=cfg_file,
        arm=True,
    )
    coord = SwingExecutionCoordinator(
        config=cfg,
        signal_provider=ToyCrossoverSignal(fast_window=3, slow_window=5, stop_pct=0.05),
        broker=broker,
    )

    # Migration must NOT infer stop_covered_qty=40 from legacy stop_armed=1.
    migrated_rows, _, _ = coord.ledger.validate_integrity(account, "2026-06-06")
    assert len(migrated_rows) == 1
    assert migrated_rows[0].status == "FILLED"
    assert migrated_rows[0].filled_qty == 40
    assert migrated_rows[0].stop_armed is False
    assert migrated_rows[0].stop_covered_qty == 0
    assert [r.reservation_id for r in coord.ledger.reconcilable_reservations(account, "2026-06-06")] == [
        "res_legacy_at10"
    ]

    # One normal reconciliation cycle resizes the 15-share stop in place to 40 shares.
    cash = AccountCashState(account, 10_000.0, 8_000.0, 8_000.0, 8_000.0, open_symbol_notional={"SYN2X_A": 2_000.0})
    calls_before = inner.place_order_calls
    c1 = await coord.run_entry_cycle(
        bars_by_symbol={},
        cash_state=cash,
        session_day="2026-06-06",
        as_of="2026-06-06T15:58:00Z",
    )
    assert c1.uncertain_holdings is False
    assert c1.reconciled_fills == []
    assert len(c1.stops_armed) == 1
    assert c1.stops_armed[0].action == "stop_synced_gtc"
    assert c1.stops_armed[0].stop_rearmed is True
    assert c1.stops_armed[0].order is not None
    assert c1.stops_armed[0].order.total_quantity == 40
    assert c1.stops_armed[0].order.remaining == 40
    assert c1.stops_armed[0].order.tif == "GTC"
    assert c1.stops_armed[0].order.order_id == 1001
    assert c1.stops_armed[0].order.perm_id == 9002
    assert inner.place_order_calls == calls_before + 1

    working_stops = [o for o in inner.orders if o.order_type == "STP" and o.status == "Submitted"]
    assert len(working_stops) == 1
    assert working_stops[0].total_quantity == 40
    assert working_stops[0].remaining == 40

    rows_after, _, _ = coord.ledger.validate_integrity(account, "2026-06-06")
    assert rows_after[0].stop_armed is True
    assert rows_after[0].stop_covered_qty == 40
    assert coord.ledger.reconcilable_reservations(account, "2026-06-06") == []

    # Fresh restart on the now-migrated DB preserves confirmed coverage and performs zero new orders.
    coord2 = SwingExecutionCoordinator(
        config=cfg,
        signal_provider=ToyCrossoverSignal(fast_window=3, slow_window=5, stop_pct=0.05),
        broker=broker,
    )
    calls_before_restart = inner.place_order_calls
    c2 = await coord2.run_entry_cycle(
        bars_by_symbol={},
        cash_state=cash,
        session_day="2026-06-06",
        as_of="2026-06-06T15:59:00Z",
    )
    assert c2.reconciled_fills == []
    assert c2.stops_armed == []
    assert inner.place_order_calls == calls_before_restart
    rows_restart, _, _ = coord2.ledger.validate_integrity(account, "2026-06-06")
    assert rows_restart[0].stop_armed is True
    assert rows_restart[0].stop_covered_qty == 40


@pytest.mark.asyncio
@pytest.mark.parametrize("strict_protocol", [False, True])
async def test_at10_legacy_db_control_confirms_existing_full_stop_without_duplicates(
    tmp_path: Path, strict_protocol: bool
) -> None:
    """AT10 control: legacy DB (FILLED/40/stop_armed=1) + existing 40-share GTC stop -> confirms with zero orders."""
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text("profile: sim\n", encoding="utf-8")
    db_path = tmp_path / "legacy_at10_control.sqlite"
    account = "SIM_AT10_CTRL_PROTO" if strict_protocol else "SIM_AT10_CTRL"

    entry_ref = _create_legacy_pre_at09_db(
        db_path,
        account=account,
        client_id=77,
        symbol="SYN2X_A",
        qty=40,
        filled_qty=40,
        status="FILLED",
        stop_armed=1,
        stop_price=47.5,
        order_id=5001,
        perm_id=9001,
        reservation_id="res_legacy_at10_ctrl",
    )

    broker = _ProtocolDeferredBroker(account=account) if strict_protocol else SimulatedBroker(account=account)
    inner = broker._inner if strict_protocol else broker
    inner.set_position("SYN2X_A", 40, avg_cost=50.0)
    inner.orders.append(
        BrokerOrder(
            account=account,
            symbol="SYN2X_A",
            action="BUY",
            order_type="LMT",
            total_quantity=40,
            client_id=77,
            order_id=5001,
            perm_id=9001,
            order_ref=entry_ref,
            status="Filled",
            limit_price=50.0,
            filled=40,
            tif="DAY",
        )
    )
    inner.orders.append(
        BrokerOrder(
            account=account,
            symbol="SYN2X_A",
            action="SELL",
            order_type="STP",
            total_quantity=40,
            client_id=77,
            order_id=1001,
            perm_id=9002,
            order_ref="SWING_STOP:res_legacy_at10_ctrl",
            status="Submitted",
            aux_price=47.5,
            filled=0,
            tif="GTC",
        )
    )

    cfg = CoordinatorConfig(
        account=account,
        client_id=77,
        db_path=db_path,
        kill_switch_path=tmp_path / "KILL_SWITCH",
        config_path=cfg_file,
        arm=True,
    )
    coord = SwingExecutionCoordinator(
        config=cfg,
        signal_provider=ToyCrossoverSignal(fast_window=3, slow_window=5, stop_pct=0.05),
        broker=broker,
    )

    # Migration leaves coverage unverified until broker confirms it.
    migrated_rows, _, _ = coord.ledger.validate_integrity(account, "2026-06-06")
    assert migrated_rows[0].stop_armed is False
    assert migrated_rows[0].stop_covered_qty == 0

    cash = AccountCashState(account, 10_000.0, 8_000.0, 8_000.0, 8_000.0, open_symbol_notional={"SYN2X_A": 2_000.0})
    calls_before = inner.place_order_calls
    c1 = await coord.run_entry_cycle(
        bars_by_symbol={},
        cash_state=cash,
        session_day="2026-06-06",
        as_of="2026-06-06T15:58:00Z",
    )
    assert c1.uncertain_holdings is False
    assert c1.reconciled_fills == []
    assert c1.stops_armed == []
    assert inner.place_order_calls == calls_before

    working_stops = [o for o in inner.orders if o.order_type == "STP" and o.status == "Submitted"]
    assert len(working_stops) == 1
    assert working_stops[0].total_quantity == 40
    assert working_stops[0].order_id == 1001
    assert working_stops[0].perm_id == 9002

    rows_after, _, _ = coord.ledger.validate_integrity(account, "2026-06-06")
    assert rows_after[0].stop_armed is True
    assert rows_after[0].stop_covered_qty == 40
    assert coord.ledger.reconcilable_reservations(account, "2026-06-06") == []

    # Subsequent restart is a strict no-op.
    coord2 = SwingExecutionCoordinator(
        config=cfg,
        signal_provider=ToyCrossoverSignal(fast_window=3, slow_window=5, stop_pct=0.05),
        broker=broker,
    )
    c2 = await coord2.run_entry_cycle(
        bars_by_symbol={},
        cash_state=cash,
        session_day="2026-06-06",
        as_of="2026-06-06T15:59:00Z",
    )
    assert c2.stops_armed == []
    assert inner.place_order_calls == calls_before


# ---------------------------------------------------------------------------
# AT10-staged-upgrade: Broker-truth reconciliation for staged upgrades & stale DB claims
# ---------------------------------------------------------------------------


def _apply_prior_1a45609_migration(db_path: Path) -> tuple[str, int, int, int]:
    """Apply the exact 1a45609 legacy migration that backfilled stop_covered_qty from stop_armed."""
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("ALTER TABLE slot_reservations ADD COLUMN stop_covered_qty INTEGER NOT NULL DEFAULT 0")
        conn.execute(
            "UPDATE slot_reservations SET stop_covered_qty = filled_qty "
            "WHERE stop_armed = 1 AND filled_qty > 0 AND stop_covered_qty = 0"
        )
        conn.commit()
        row = conn.execute(
            "SELECT status, filled_qty, stop_armed, stop_covered_qty FROM slot_reservations"
        ).fetchone()
    assert row is not None
    return (str(row[0]), int(row[1]), int(row[2]), int(row[3]))


@pytest.mark.asyncio
@pytest.mark.parametrize("strict_protocol", [False, True])
async def test_at10_staged_upgrade_from_1a45609_resizes_underprotected_stop_and_stays_idempotent(
    tmp_path: Path, strict_protocol: bool
) -> None:
    """Reproduces AT10-staged-upgrade: legacy DB -> 1a45609 (FILLED/40/armed=1/covered=40) + 15-share stop -> resizes to 40 once."""
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text("profile: sim\n", encoding="utf-8")
    db_path = tmp_path / "staged_at10.sqlite"
    account = "SIM_AT10_STAGED_PROTO" if strict_protocol else "SIM_AT10_STAGED"

    entry_ref = _create_legacy_pre_at09_db(
        db_path,
        account=account,
        client_id=77,
        symbol="SYN2X_A",
        qty=40,
        filled_qty=40,
        status="FILLED",
        stop_armed=1,
        stop_price=47.5,
        order_id=5001,
        perm_id=9001,
        reservation_id="res_staged_at10",
    )
    prior_state = _apply_prior_1a45609_migration(db_path)
    assert prior_state == ("FILLED", 40, 1, 40)

    broker = _ProtocolDeferredBroker(account=account) if strict_protocol else SimulatedBroker(account=account)
    inner = broker._inner if strict_protocol else broker
    inner.set_position("SYN2X_A", 40, avg_cost=50.0)
    inner.orders.append(
        BrokerOrder(
            account=account,
            symbol="SYN2X_A",
            action="BUY",
            order_type="LMT",
            total_quantity=40,
            client_id=77,
            order_id=5001,
            perm_id=9001,
            order_ref=entry_ref,
            status="Filled",
            limit_price=50.0,
            filled=40,
            tif="DAY",
        )
    )
    inner.orders.append(
        BrokerOrder(
            account=account,
            symbol="SYN2X_A",
            action="SELL",
            order_type="STP",
            total_quantity=15,
            client_id=77,
            order_id=1001,
            perm_id=9002,
            order_ref="SWING_STOP:res_staged_at10",
            status="Submitted",
            aux_price=47.5,
            filled=0,
            tif="GTC",
        )
    )

    cfg = CoordinatorConfig(
        account=account,
        client_id=77,
        db_path=db_path,
        kill_switch_path=tmp_path / "KILL_SWITCH",
        config_path=cfg_file,
        arm=True,
    )
    coord = SwingExecutionCoordinator(
        config=cfg,
        signal_provider=ToyCrossoverSignal(fast_window=3, slow_window=5, stop_pct=0.05),
        broker=broker,
    )

    cash = AccountCashState(account, 10_000.0, 8_000.0, 8_000.0, 8_000.0, open_symbol_notional={"SYN2X_A": 2_000.0})
    calls_before = inner.place_order_calls
    c1 = await coord.run_entry_cycle(
        bars_by_symbol={},
        cash_state=cash,
        session_day="2026-06-06",
        as_of="2026-06-06T15:58:00Z",
    )
    assert c1.uncertain_holdings is False
    assert c1.reconciled_fills == []
    assert len(c1.stops_armed) == 1
    assert c1.stops_armed[0].action == "stop_synced_gtc"
    assert c1.stops_armed[0].stop_rearmed is True
    assert c1.stops_armed[0].order is not None
    assert c1.stops_armed[0].order.total_quantity == 40
    assert c1.stops_armed[0].order.remaining == 40
    assert c1.stops_armed[0].order.order_id == 1001
    assert c1.stops_armed[0].order.perm_id == 9002
    assert inner.place_order_calls == calls_before + 1

    working_stops = [o for o in inner.orders if o.order_type == "STP" and o.status == "Submitted"]
    assert len(working_stops) == 1
    assert working_stops[0].total_quantity == 40
    assert working_stops[0].remaining == 40

    rows_after, _, _ = coord.ledger.validate_integrity(account, "2026-06-06")
    assert rows_after[0].status == "FILLED"
    assert rows_after[0].filled_qty == 40
    assert rows_after[0].stop_armed is True
    assert rows_after[0].stop_covered_qty == 40

    # Second cycle after a fresh restart is strictly idempotent (zero broker orders).
    coord2 = SwingExecutionCoordinator(
        config=cfg,
        signal_provider=ToyCrossoverSignal(fast_window=3, slow_window=5, stop_pct=0.05),
        broker=broker,
    )
    calls_before_restart = inner.place_order_calls
    c2 = await coord2.run_entry_cycle(
        bars_by_symbol={},
        cash_state=cash,
        session_day="2026-06-06",
        as_of="2026-06-06T15:59:00Z",
    )
    assert c2.uncertain_holdings is False
    assert c2.reconciled_fills == []
    assert c2.stops_armed == []
    assert inner.place_order_calls == calls_before_restart
    working_stops_restart = [o for o in inner.orders if o.order_type == "STP" and o.status == "Submitted"]
    assert len(working_stops_restart) == 1 and working_stops_restart[0].remaining == 40


@pytest.mark.asyncio
@pytest.mark.parametrize("strict_protocol", [False, True])
async def test_at10_staged_upgrade_rejected_resize_persists_broker_truth_and_retries(
    tmp_path: Path, strict_protocol: bool
) -> None:
    """When staged-upgrade resize is rejected, ledger downgrades to broker truth (15) and retries cleanly."""
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text("profile: sim\n", encoding="utf-8")
    db_path = tmp_path / "staged_at10_reject.sqlite"
    account = "SIM_AT10_REJ_PROTO" if strict_protocol else "SIM_AT10_REJ"

    entry_ref = _create_legacy_pre_at09_db(
        db_path,
        account=account,
        client_id=77,
        symbol="SYN2X_A",
        qty=40,
        filled_qty=40,
        status="FILLED",
        stop_armed=1,
        stop_price=47.5,
        order_id=5001,
        perm_id=9001,
        reservation_id="res_staged_rej",
    )
    assert _apply_prior_1a45609_migration(db_path) == ("FILLED", 40, 1, 40)

    broker = _ProtocolDeferredBroker(account=account) if strict_protocol else SimulatedBroker(account=account)
    inner = broker._inner if strict_protocol else broker
    inner.set_position("SYN2X_A", 40, avg_cost=50.0)
    inner.orders.append(
        BrokerOrder(
            account=account,
            symbol="SYN2X_A",
            action="BUY",
            order_type="LMT",
            total_quantity=40,
            client_id=77,
            order_id=5001,
            perm_id=9001,
            order_ref=entry_ref,
            status="Filled",
            limit_price=50.0,
            filled=40,
            tif="DAY",
        )
    )
    inner.orders.append(
        BrokerOrder(
            account=account,
            symbol="SYN2X_A",
            action="SELL",
            order_type="STP",
            total_quantity=15,
            client_id=77,
            order_id=1001,
            perm_id=9002,
            order_ref="SWING_STOP:res_staged_rej",
            status="Submitted",
            aux_price=47.5,
            filled=0,
            tif="GTC",
        )
    )

    cfg = CoordinatorConfig(
        account=account,
        client_id=77,
        db_path=db_path,
        kill_switch_path=tmp_path / "KILL_SWITCH",
        config_path=cfg_file,
        arm=True,
    )
    cash = AccountCashState(account, 10_000.0, 8_000.0, 8_000.0, 8_000.0, open_symbol_notional={"SYN2X_A": 2_000.0})

    # First startup: resize rejected as Inactive -> ledger must record actual broker truth (armed=False, covered=15).
    inner.reject_next_place_status = "Inactive"
    coord1 = SwingExecutionCoordinator(
        config=cfg,
        signal_provider=ToyCrossoverSignal(fast_window=3, slow_window=5, stop_pct=0.05),
        broker=broker,
    )
    c1 = await coord1.run_entry_cycle(
        bars_by_symbol={},
        cash_state=cash,
        session_day="2026-06-06",
        as_of="2026-06-06T15:58:00Z",
    )
    assert len(c1.stops_armed) == 1
    assert c1.stops_armed[0].action == "stop_sync_rejected"
    rows1, _, _ = coord1.ledger.validate_integrity(account, "2026-06-06")
    assert rows1[0].stop_armed is False
    assert rows1[0].stop_covered_qty == 15
    assert [r.reservation_id for r in coord1.ledger.reconcilable_reservations(account, "2026-06-06")] == [
        "res_staged_rej"
    ]

    # Second restart: resize succeeds once to 40, updating ledger to armed=True, covered=40.
    coord2 = SwingExecutionCoordinator(
        config=cfg,
        signal_provider=ToyCrossoverSignal(fast_window=3, slow_window=5, stop_pct=0.05),
        broker=broker,
    )
    c2 = await coord2.run_entry_cycle(
        bars_by_symbol={},
        cash_state=cash,
        session_day="2026-06-06",
        as_of="2026-06-06T15:59:00Z",
    )
    assert len(c2.stops_armed) == 1
    assert c2.stops_armed[0].action == "stop_synced_gtc"
    assert c2.stops_armed[0].order is not None and c2.stops_armed[0].order.remaining == 40
    rows2, _, _ = coord2.ledger.validate_integrity(account, "2026-06-06")
    assert rows2[0].stop_armed is True
    assert rows2[0].stop_covered_qty == 40

    # Third restart: strict zero-order no-op.
    calls_before = inner.place_order_calls
    coord3 = SwingExecutionCoordinator(
        config=cfg,
        signal_provider=ToyCrossoverSignal(fast_window=3, slow_window=5, stop_pct=0.05),
        broker=broker,
    )
    c3 = await coord3.run_entry_cycle(
        bars_by_symbol={},
        cash_state=cash,
        session_day="2026-06-06",
        as_of="2026-06-06T16:00:00Z",
    )
    assert c3.stops_armed == []
    assert inner.place_order_calls == calls_before


@pytest.mark.asyncio
@pytest.mark.parametrize("strict_protocol", [False, True])
async def test_at10_staged_upgrade_already_protected_position_submits_zero_orders(
    tmp_path: Path, strict_protocol: bool
) -> None:
    """Staged-upgrade DB with an already-full 40-share broker stop submits zero orders on startup."""
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text("profile: sim\n", encoding="utf-8")
    db_path = tmp_path / "staged_at10_full.sqlite"
    account = "SIM_AT10_FULL_PROTO" if strict_protocol else "SIM_AT10_FULL"

    entry_ref = _create_legacy_pre_at09_db(
        db_path,
        account=account,
        client_id=77,
        symbol="SYN2X_A",
        qty=40,
        filled_qty=40,
        status="FILLED",
        stop_armed=1,
        stop_price=47.5,
        order_id=5001,
        perm_id=9001,
        reservation_id="res_staged_full",
    )
    assert _apply_prior_1a45609_migration(db_path) == ("FILLED", 40, 1, 40)

    broker = _ProtocolDeferredBroker(account=account) if strict_protocol else SimulatedBroker(account=account)
    inner = broker._inner if strict_protocol else broker
    inner.set_position("SYN2X_A", 40, avg_cost=50.0)
    inner.orders.append(
        BrokerOrder(
            account=account,
            symbol="SYN2X_A",
            action="BUY",
            order_type="LMT",
            total_quantity=40,
            client_id=77,
            order_id=5001,
            perm_id=9001,
            order_ref=entry_ref,
            status="Filled",
            limit_price=50.0,
            filled=40,
            tif="DAY",
        )
    )
    inner.orders.append(
        BrokerOrder(
            account=account,
            symbol="SYN2X_A",
            action="SELL",
            order_type="STP",
            total_quantity=40,
            client_id=77,
            order_id=1001,
            perm_id=9002,
            order_ref="SWING_STOP:res_staged_full",
            status="Submitted",
            aux_price=47.5,
            filled=0,
            tif="GTC",
        )
    )

    coord = SwingExecutionCoordinator(
        config=CoordinatorConfig(
            account=account,
            client_id=77,
            db_path=db_path,
            kill_switch_path=tmp_path / "KILL_SWITCH",
            config_path=cfg_file,
            arm=True,
        ),
        signal_provider=ToyCrossoverSignal(fast_window=3, slow_window=5, stop_pct=0.05),
        broker=broker,
    )
    cash = AccountCashState(account, 10_000.0, 8_000.0, 8_000.0, 8_000.0, open_symbol_notional={"SYN2X_A": 2_000.0})
    calls_before = inner.place_order_calls
    c1 = await coord.run_entry_cycle(
        bars_by_symbol={},
        cash_state=cash,
        session_day="2026-06-06",
        as_of="2026-06-06T15:58:00Z",
    )
    assert c1.uncertain_holdings is False
    assert c1.reconciled_fills == []
    assert c1.stops_armed == []
    assert inner.place_order_calls == calls_before


@pytest.mark.asyncio
async def test_at10_staged_upgrade_open_orders_read_failure_fails_closed(tmp_path: Path) -> None:
    """If broker open-orders query fails during FILLED stop audit, fail closed with zero blind orders."""
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text("profile: sim\n", encoding="utf-8")
    db_path = tmp_path / "staged_at10_fail_closed.sqlite"
    account = "SIM_AT10_FAIL"

    entry_ref = _create_legacy_pre_at09_db(
        db_path,
        account=account,
        client_id=77,
        symbol="SYN2X_A",
        qty=40,
        filled_qty=40,
        status="FILLED",
        stop_armed=1,
        stop_price=47.5,
        order_id=5001,
        perm_id=9001,
        reservation_id="res_staged_fail",
    )
    assert _apply_prior_1a45609_migration(db_path) == ("FILLED", 40, 1, 40)

    broker = SimulatedBroker(account=account)
    broker.set_position("SYN2X_A", 40, avg_cost=50.0)
    broker.orders.append(
        BrokerOrder(
            account=account,
            symbol="SYN2X_A",
            action="BUY",
            order_type="LMT",
            total_quantity=40,
            client_id=77,
            order_id=5001,
            perm_id=9001,
            order_ref=entry_ref,
            status="Filled",
            limit_price=50.0,
            filled=40,
            tif="DAY",
        )
    )
    broker.orders.append(
        BrokerOrder(
            account=account,
            symbol="SYN2X_A",
            action="SELL",
            order_type="STP",
            total_quantity=15,
            client_id=77,
            order_id=1001,
            perm_id=9002,
            order_ref="SWING_STOP:res_staged_fail",
            status="Submitted",
            aux_price=47.5,
            filled=0,
            tif="GTC",
        )
    )

    coord = SwingExecutionCoordinator(
        config=CoordinatorConfig(
            account=account,
            client_id=77,
            db_path=db_path,
            kill_switch_path=tmp_path / "KILL_SWITCH",
            config_path=cfg_file,
            arm=True,
        ),
        signal_provider=ToyCrossoverSignal(fast_window=3, slow_window=5, stop_pct=0.05),
        broker=broker,
    )
    cash = AccountCashState(account, 10_000.0, 8_000.0, 8_000.0, 8_000.0, open_symbol_notional={"SYN2X_A": 2_000.0})

    broker.fail_open_orders_exc = asyncio.TimeoutError("open orders unavailable")
    c_fail = await coord.run_entry_cycle(
        bars_by_symbol=_crossover_bars("SYN2X_B"),
        cash_state=cash,
        session_day="2026-06-06",
        as_of="2026-06-06T15:58:00Z",
    )
    assert c_fail.uncertain_holdings is True
    assert c_fail.blocked_reason == "reconciliation_uncertain_holdings"
    assert "SYN2X_A" in c_fail.uncertain_symbols
    assert c_fail.stops_armed == []
    assert c_fail.reserved == []
    assert broker.place_order_calls == 0

    # Once broker read recovers, reconciliation repairs the 15 -> 40 stop in one call.
    broker.fail_open_orders_exc = None
    c_ok = await coord.run_entry_cycle(
        bars_by_symbol={},
        cash_state=cash,
        session_day="2026-06-06",
        as_of="2026-06-06T15:59:00Z",
    )
    assert c_ok.uncertain_holdings is False
    assert len(c_ok.stops_armed) == 1
    assert c_ok.stops_armed[0].action == "stop_synced_gtc"
    assert c_ok.stops_armed[0].order is not None and c_ok.stops_armed[0].order.remaining == 40
    assert broker.place_order_calls == 1


@pytest.mark.asyncio
async def test_at10_closed_or_superseded_filled_reservation_submits_zero_orders(tmp_path: Path) -> None:
    """Flat closed trades and older superseded FILLED reservations never trigger spurious stops."""
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text("profile: sim\n", encoding="utf-8")
    db_path = tmp_path / "historical_filled.sqlite"
    account = "SIM_HIST"

    ledger = SlotLedger(db_path, account=account)
    cash = AccountCashState(account, 10_000.0, 10_000.0, 10_000.0, 10_000.0)
    d1 = ledger.reserve_entry_slot(
        cash_state=cash,
        client_id=77,
        symbol="SYN2X_A",
        qty=40,
        limit_price=50.0,
        session_day="2026-06-05",
        stop_price=47.5,
        now_iso="2026-06-05T15:55:00Z",
    )
    assert d1.allowed and d1.reservation is not None
    ledger.update_reservation_status(
        d1.reservation.reservation_id,
        status="FILLED",
        filled_qty=40,
        stop_armed=True,
        stop_covered_qty=40,
        now_iso="2026-06-05T15:56:00Z",
    )

    # Case 1: Broker is flat -> zero orders submitted.
    broker = SimulatedBroker(account=account)
    coord = SwingExecutionCoordinator(
        config=CoordinatorConfig(
            account=account,
            client_id=77,
            db_path=db_path,
            kill_switch_path=tmp_path / "KILL_SWITCH",
            config_path=cfg_file,
            arm=True,
        ),
        signal_provider=ToyCrossoverSignal(fast_window=3, slow_window=5, stop_pct=0.05),
        broker=broker,
    )
    c_flat = await coord.run_entry_cycle(
        bars_by_symbol={},
        cash_state=cash,
        session_day="2026-06-06",
        as_of="2026-06-06T15:55:00Z",
    )
    assert c_flat.uncertain_holdings is False
    assert c_flat.stops_armed == []
    assert broker.place_order_calls == 0

    # Case 2: Newer 20-share trade on Day 2 is fully covered by a 20-share stop (older Day 1 was 40 shares).
    d2 = ledger.reserve_entry_slot(
        cash_state=cash,
        client_id=77,
        symbol="SYN2X_A",
        qty=20,
        limit_price=55.0,
        session_day="2026-06-06",
        stop_price=52.25,
        now_iso="2026-06-06T15:55:00Z",
    )
    assert d2.allowed and d2.reservation is not None
    ledger.update_reservation_status(
        d2.reservation.reservation_id,
        status="FILLED",
        filled_qty=20,
        stop_armed=True,
        stop_covered_qty=20,
        now_iso="2026-06-06T15:56:00Z",
    )
    broker.set_position("SYN2X_A", 20, avg_cost=55.0)
    broker.orders.append(
        BrokerOrder(
            account=account,
            symbol="SYN2X_A",
            action="SELL",
            order_type="STP",
            total_quantity=20,
            client_id=77,
            order_id=1002,
            perm_id=9010,
            order_ref=f"SWING_STOP:{d2.reservation.reservation_id}",
            status="Submitted",
            aux_price=52.25,
            filled=0,
            tif="GTC",
        )
    )
    c_superseded = await coord.run_entry_cycle(
        bars_by_symbol={},
        cash_state=cash,
        session_day="2026-06-06",
        as_of="2026-06-06T15:57:00Z",
    )
    assert c_superseded.uncertain_holdings is False
    assert c_superseded.stops_armed == []
    assert broker.place_order_calls == 0
