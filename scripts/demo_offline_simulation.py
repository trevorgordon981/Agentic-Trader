#!/usr/bin/env python3
"""Broker-free, offline proposal-to-close simulation for Agentic Trader.

Runs completely locally with zero network calls and zero broker credentials.
Exercises:
  1. Exit-manager fail-closed startup & throttle counter gates (AT01-AT04).
  2. End-to-end 2x leveraged-ETF swing execution lifecycle via ``swing_executor``:
       - Synthetic demo-only ``ToyCrossoverSignal`` proposal (NOT a live strategy)
       - Multi-slot ledger reservation with T+1 cash reserve & exposure caps
       - Entry fill and GTC protective stop arming
       - Concurrent external partial-fill stop resize without overselling
       - Broker timeout fail-closed (``uncertain_holdings=True`` / ``rearm_deferred_uncertain_qty=True``)
       - Recovery, trailing-stop tighten, clean close, and neutral-before-fees alert formatting
       - Paper-to-live graduation evaluation (always requires human approval)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from exitmgr.config import ConfigError, load_config
from exitmgr.entry_throttle import EntryThrottleStore, EntryThrottleUnreadable
from exitmgr.manager import BrokerStartupError, ExitManager
from swing_executor import (
    AccountCashState,
    BrokerOrder,
    ClosedPaperTrade,
    CoordinatorConfig,
    GraduationContract,
    PriceBar,
    SimulatedBroker,
    SwingExecutionCoordinator,
    ToyCrossoverSignal,
    evaluate_paper_graduation,
    evaluate_trailing_stop,
    format_stop_sync_alert,
)


def _synthetic_bars() -> dict[str, list[PriceBar]]:
    """Deterministic synthetic OHLCV series that triggers ToyCrossoverSignal."""
    closes_a = [48.0, 47.5, 47.0, 46.8, 47.2, 50.0]
    closes_b = [30.0, 29.8, 29.6, 29.5, 29.7, 31.5]
    return {
        "SYN2X_A": [
            PriceBar(
                timestamp=f"2026-06-0{idx + 1}T15:55:00-04:00",
                open=round(px - 0.3, 2),
                high=round(px + 0.4, 2),
                low=round(px - 0.5, 2),
                close=px,
                volume=100_000.0,
            )
            for idx, px in enumerate(closes_a)
        ],
        "SYN2X_B": [
            PriceBar(
                timestamp=f"2026-06-0{idx + 1}T15:55:00-04:00",
                open=round(px - 0.2, 2),
                high=round(px + 0.3, 2),
                low=round(px - 0.4, 2),
                close=px,
                volume=85_000.0,
            )
            for idx, px in enumerate(closes_b)
        ],
    }


async def _verify_exitmgr_fail_closed(tmp_dir: Path) -> dict[str, object]:
    results: dict[str, object] = {}

    # AT01: Armed startup refuses a missing config file.
    missing_cfg = tmp_dir / "missing.yaml"
    try:
        load_config(str(missing_cfg), arm=True)
        raise AssertionError("expected ConfigError for missing armed config")
    except ConfigError as exc:
        results["at01_missing_armed_config_refused"] = str(exc)

    # AT02: Malformed CLI cap overrides fail the same domain checks as YAML.
    paper_cfg = REPO_ROOT / "config.paper.yaml"
    try:
        load_config(str(paper_cfg), max_orders_day=-1)
        raise AssertionError("expected ConfigError for negative max_orders_day")
    except ConfigError as exc:
        results["at02_negative_cli_cap_refused"] = str(exc)

    # AT03: Persisted partial/NaN/negative throttle counters fail closed & preserve evidence.
    throttle_path = tmp_dir / "entry_throttle.json"
    throttle_path.write_text('{"2026-06-06": {"orders_opened": 3}}', encoding="utf-8")
    store = EntryThrottleStore(throttle_path)
    try:
        store.read_day("2026-06-06")
        raise AssertionError("expected EntryThrottleUnreadable for partial counter record")
    except EntryThrottleUnreadable as exc:
        results["at03_partial_counter_refused"] = str(exc)

    # AT04: Broker connection failure raises typed BrokerStartupError (nonzero CLI exit).
    cfg = load_config(str(paper_cfg), arm=False)
    cfg.state.path = str(tmp_dir / "state.json")
    journal_file = tmp_dir / "trades.log"
    journal_file.write_text("", encoding="utf-8")
    cfg.journal.path = str(journal_file)
    cfg.kill_switch.path = str(tmp_dir / "KILL_SWITCH")

    class _FailingConn:
        async def connect(self) -> bool:
            return False

    import contextlib
    import io

    with contextlib.redirect_stdout(io.StringIO()):
        mgr = ExitManager(cfg)
        mgr.ib_conn = _FailingConn()
        try:
            await mgr.run()
            raise AssertionError("expected BrokerStartupError when connect() returns False")
        except BrokerStartupError as exc:
            results["at04_broker_startup_error"] = str(exc)

    return results


async def run_offline_simulation() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="agentic_trader_demo_") as tmp:
        tmp_dir = Path(tmp)
        fail_closed_checks = await _verify_exitmgr_fail_closed(tmp_dir)

        cfg_file = tmp_dir / "swing_paper.yaml"
        cfg_file.write_text("profile: offline_demo\n", encoding="utf-8")

        broker = SimulatedBroker(account="SIM_CASH_01")
        signal_provider = ToyCrossoverSignal(fast_window=3, slow_window=5, stop_pct=0.05)
        coordinator = SwingExecutionCoordinator(
            config=CoordinatorConfig(
                account="SIM_CASH_01",
                client_id=77,
                db_path=tmp_dir / "swing_ledger.sqlite",
                kill_switch_path=tmp_dir / "KILL_SWITCH",
                config_path=cfg_file,
                arm=True,
                max_concurrent_slots=2,
                max_slot_exposure_fraction=0.20,
                max_deployed_fraction=0.60,
                max_sector_fraction=0.30,
                cash_buffer_fraction=0.02,
                max_orders_per_day=5,
                max_notional_per_day=20_000.0,
                sector_groups={"synthetic_tech_2x": ("SYN2X_A", "SYN2X_B")},
            ),
            signal_provider=signal_provider,
            broker=broker,
            broker_timeout_seconds=1.0,
        )

        # Cash account with $10,000 NetLiq, $2,000 ring-fenced reserve, and $1,500 unsettled T+1 proceeds.
        # Spendable cash is capped at min(9000-2000, 7500-2000-1500, 9000-2000) = $4,000.
        cash_state = AccountCashState(
            account="SIM_CASH_01",
            net_liq_usd=10_000.0,
            total_cash_usd=9_000.0,
            settled_cash_usd=7_500.0,
            available_funds_usd=9_000.0,
            ring_fenced_reserve_usd=2_000.0,
            unsettled_t1_proceeds_usd=1_500.0,
        )

        # 1. Propose -> Reserve -> Fill -> Arm GTC Stop (SYN2X_A fills; SYN2X_B blocked by 30% sector cap).
        cycle = await coordinator.run_entry_cycle(
            bars_by_symbol=_synthetic_bars(),
            cash_state=cash_state,
            session_day="2026-06-06",
            as_of="2026-06-06T15:55:00-04:00",
            auto_fill_in_sim=True,
        )
        assert len(cycle.stops_armed) == 1 and cycle.stops_armed[0].order is not None
        armed_stop = cycle.stops_armed[0].order
        entry_qty = armed_stop.total_quantity

        # 2. Trailing stop tighten + concurrent external partial-fill race (different client_id, same order_id!).
        _, _, tightened_stop = evaluate_trailing_stop(
            direction="LONG",
            entry_price=50.0,
            prev_peak=50.0,
            mark=53.0,
            current_stop=armed_stop.aux_price,
            activate_pct=4.0,
            distance_pct=2.0,
        )
        assert tightened_stop is not None

        external_trim = broker.add_external_order(
            BrokerOrder(
                account="SIM_CASH_01",
                symbol="SYN2X_A",
                action="SELL",
                order_type="LMT",
                total_quantity=10,
                client_id=999,  # external client
                order_id=armed_stop.order_id,  # deliberate cross-client order_id collision
                order_ref="MANUAL_TRIM",
                limit_price=53.0,
                status="Submitted",
            )
        )
        # External order partially fills 4 shares (6 still working), leaving held_qty = entry_qty - 4.
        broker.simulate_partial_fill(external_trim, 4)

        sync_res = await coordinator.stop_engine.sync_protective_stop(
            symbol="SYN2X_A",
            direction="LONG",
            target_stop_price=tightened_stop,
            managed_qty=entry_qty,
            cached_stop=armed_stop,
            mark_price=53.0,
        )
        assert sync_res.action == "stop_synced_gtc" and sync_res.order is not None
        # Aggregate working close qty (resized stop + remaining 6 external) == held_qty (entry_qty - 4).
        expected_stop_remaining = (entry_qty - 4) - 6
        assert sync_res.order.remaining == expected_stop_remaining
        stop_sync_alert = format_stop_sync_alert(
            symbol="SYN2X_A",
            old_stop=armed_stop.aux_price,
            new_stop=sync_res.order.aux_price,
            remaining_qty=sync_res.order.remaining,
        )

        # 3. Broker timeout during stop re-arm fails closed into uncertain_holdings=True.
        broker.fail_positions_exc = asyncio.TimeoutError("simulated positions timeout")
        timeout_res = await coordinator.stop_engine.rearm_after_exit_attempt(
            symbol="SYN2X_A",
            direction="LONG",
            stop_price=sync_res.order.aux_price,
            managed_remaining_qty=expected_stop_remaining,
            trade_key=cycle.reserved[0],
        )
        assert timeout_res.uncertain_holdings is True
        assert timeout_res.rearm_deferred_uncertain_qty is True
        assert timeout_res.stop_rearmed is False
        broker.fail_positions_exc = None

        # 4. Close position and emit neutral-before-fees alert first, then verified-fees net alert.
        exit_pending_fees = coordinator.build_exit_alert(
            symbol="SYN2X_A",
            qty=expected_stop_remaining,
            entry_price=50.0,
            exit_price=52.10,
            strategy_id=signal_provider.name,
            reason="trailing_stop_filled",
            fees_usd=None,
        )
        exit_verified_fees = coordinator.build_exit_alert(
            symbol="SYN2X_A",
            qty=expected_stop_remaining,
            entry_price=50.0,
            exit_price=52.10,
            strategy_id=signal_provider.name,
            reason="trailing_stop_filled",
            fees_usd=0.70,
        )
        assert exit_pending_fees["tone"] == "NEUTRAL"
        assert exit_verified_fees["tone"] == "WIN"

        # 5. Paper-to-live graduation check (toy sample fails sample-size gate and never auto-promotes).
        grad = evaluate_paper_graduation(
            signal_provider.name,
            [
                ClosedPaperTrade(
                    trade_key="demo_1",
                    symbol="SYN2X_A",
                    strategy_id=signal_provider.name,
                    direction="LONG",
                    session_day="2026-06-06",
                    entry_price=50.0,
                    exit_price=52.10,
                    signal_price=49.98,
                    entry_spread_bps=4.0,
                )
            ],
            live_net_liq_usd=10_000.0,
            contract=GraduationContract(),
        )
        assert grad.eligible_for_human_review is False
        assert grad.requires_human_approval is True
        assert grad.auto_promoted is False

        return {
            "mode": "offline_broker_free_simulation",
            "signal_provider": {
                "name": signal_provider.name,
                "is_production_strategy": signal_provider.IS_PRODUCTION_STRATEGY,
            },
            "exitmgr_fail_closed_checks": fail_closed_checks,
            "swing_lifecycle": {
                "spendable_cash_usd": cash_state.spendable_cash_usd,
                "proposals_seen": cycle.proposals_seen,
                "reserved": cycle.reserved,
                "rejected": cycle.rejected,
                "entry_alert": cycle.alerts[0],
                "initial_stop": {
                    "order_id": armed_stop.order_id,
                    "qty": armed_stop.total_quantity,
                    "aux_price": armed_stop.aux_price,
                },
                "concurrent_partial_fill_sync": {
                    "action": sync_res.action,
                    "fresh_held_qty": sync_res.fresh_held_qty,
                    "resized_stop_remaining": sync_res.order.remaining,
                    "external_remaining": external_trim.remaining,
                    "new_stop_price": sync_res.order.aux_price,
                    "alert": stop_sync_alert,
                },
                "timeout_fail_closed": {
                    "action": timeout_res.action,
                    "uncertain_holdings": timeout_res.uncertain_holdings,
                    "rearm_deferred_uncertain_qty": timeout_res.rearm_deferred_uncertain_qty,
                    "stop_rearmed": timeout_res.stop_rearmed,
                },
                "exit_alert_before_fees": exit_pending_fees,
                "exit_alert_after_fees": exit_verified_fees,
                "graduation_gate": {
                    "verified_trades": grad.verified_trades,
                    "eligible_for_human_review": grad.eligible_for_human_review,
                    "requires_human_approval": grad.requires_human_approval,
                    "auto_promoted": grad.auto_promoted,
                    "passed_gates": grad.passed_gates,
                },
            },
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Agentic Trader offline proposal-to-close simulation.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON trace.")
    args = parser.parse_args()

    trace = asyncio.run(run_offline_simulation())
    if args.json:
        print(json.dumps(trace, indent=2, sort_keys=True))
        return 0

    swing = trace["swing_lifecycle"]
    print("=== Agentic Trader: Offline Proposal-to-Close Simulation (No Broker / No Network) ===")
    print(f"Signal provider : {trace['signal_provider']['name']} (production_strategy={trace['signal_provider']['is_production_strategy']})")
    print("Fail-closed gates:")
    for key, msg in trace["exitmgr_fail_closed_checks"].items():
        print(f"  [PASS] {key}: {msg}")
    print("\n2x Swing Execution Trace:")
    print(f"  Spendable cash (after reserve & T+1 hold): ${swing['spendable_cash_usd']:,.2f}")
    print(f"  Proposals seen: {swing['proposals_seen']} | Reserved: {swing['reserved']} | Rejected: {swing['rejected']}")
    print(f"  {swing['entry_alert']}")
    print(f"  Initial GTC stop: order_id={swing['initial_stop']['order_id']} qty={swing['initial_stop']['qty']} @ ${swing['initial_stop']['aux_price']:.2f}")
    sync = swing["concurrent_partial_fill_sync"]
    print(f"  {sync['alert']} (held={sync['fresh_held_qty']}, external_working={sync['external_remaining']})")
    to = swing["timeout_fail_closed"]
    print(
        f"  Broker timeout fail-closed: action={to['action']} "
        f"uncertain_holdings={to['uncertain_holdings']} "
        f"rearm_deferred_uncertain_qty={to['rearm_deferred_uncertain_qty']}"
    )
    print(f"  Before fees: {swing['exit_alert_before_fees']['text']} [tone={swing['exit_alert_before_fees']['tone']}]")
    print(f"  After fees : {swing['exit_alert_after_fees']['text']} [tone={swing['exit_alert_after_fees']['tone']}]")
    grad = swing["graduation_gate"]
    print(
        f"  Graduation : verified_trades={grad['verified_trades']} "
        f"eligible_for_human_review={grad['eligible_for_human_review']} "
        f"requires_human_approval={grad['requires_human_approval']}"
    )
    print("\n[OK] Offline simulation completed cleanly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
