"""CLI entry for the LLM trading orchestrator.

DEFAULTS ARE SAFE: dry-run ON, paper port. Live trading requires BOTH --arm AND a Slack
approval per entry. Read the README before using --arm.
"""
import asyncio
import os
import sys
from typing import Optional
import typer

from exitmgr.config import load_config
from exitmgr.connection import IBConnection
from exitmgr.manager import ExitManager
from exitmgr import entry_safety
from exitmgr.trader import Trader
from exitmgr.trader import entry_window_wait_seconds as _entry_window_wait

app = typer.Typer(help="LLM trading orchestrator (propose -> gate -> approve -> execute -> manage)")

# TRADING-DOWN MARKER (2026-07-03 gap-fix). The wrapper run_trader_service.sh refuses to --arm while
# this marker exists, but a bare `python run_trader.py --arm` bypassed the wrapper entirely. Enforce
# the SAME guard here so a manual arm can't skip it. Located next to this file (the repo root), so it
# is found regardless of the caller's cwd. Only the ARMING/LIVE path is blocked -- a dry-run/read-only
# invocation (no --arm) is never blocked.
TRADING_DOWN_MARKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "TRADING_DOWN")


def _refuse_if_trading_down(arm: bool, mode: str = "combined") -> None:
    """Compatibility hook: warn, but do not disarm risk-reducing exits.

    Entry placement is independently blocked before every proposal/submit.  Refusing to start the
    process here also refused the protective SELL loop, which made a stand-down increase risk.
    """
    if arm and os.path.exists(TRADING_DOWN_MARKER) and mode in ("combined", "entry"):
        print("[run_trader] TRADING_DOWN active: BUY entries blocked; protective exits remain armed.",
              file=sys.stderr)
    elif arm and os.path.exists(TRADING_DOWN_MARKER) and mode == "protective":
        print("[run_trader] TRADING_DOWN active: protective-only mode remains armed (no BUY path).",
              file=sys.stderr)


def _selected_client_id(cfg, mode: str, override: Optional[int]) -> int:
    entry_client_id = int(cfg.ib.client_id)
    selected = int(override) if override is not None else (
        int(getattr(cfg.ib, "protective_client_id", 189)) if mode == "protective"
        else entry_client_id)
    if selected <= 0:
        raise typer.BadParameter("IBKR client id must be positive")
    if mode == "protective" and selected == entry_client_id:
        raise typer.BadParameter("protective client id must differ from the entry client id")
    return selected


@app.command()
def main(
    config: str = typer.Option("config.yaml", "--config", "-c"),
    arm: bool = typer.Option(False, "--arm", help="LIVE: place real orders (still needs Slack approval per entry)"),
    loop: bool = typer.Option(False, "--loop"),
    interval: int = typer.Option(900, "--interval", help="seconds between cycles in --loop"),
    protective_interval: int = typer.Option(
        30, "--protective-interval", min=15, max=60,
        help="seconds between independent static protective-exit cycles"),
    mode: str = typer.Option(
        "combined", "--mode",
        help="combined, entry, or protective (protective never calls the model)"),
    client_id: Optional[int] = typer.Option(
        None, "--client-id", min=1, max=2_147_483_647,
        help="IBKR client id override; protective mode must use a distinct id"),
):
    mode = str(mode).strip().lower()
    if mode not in {"combined", "entry", "protective"}:
        raise typer.BadParameter("--mode must be combined, entry, or protective")
    # TRADING-DOWN GUARD (2026-07-03 gap-fix): refuse to ARM live trading while the marker exists,
    # mirroring run_trader_service.sh so a manual `python run_trader.py --arm` cannot bypass it.
    # Dry-run / read-only invocations (no --arm) are intentionally NOT blocked.
    _refuse_if_trading_down(arm, mode)

    cfg = load_config(config_path=config, arm=arm, loop=loop, interval=interval)

    # CREDIT MASTER SWITCH -> env, because plan_idea (trader.py) reads it from the environment so
    # that a caller constructing RiskLimits by hand cannot route around it. Explicit "1"/"0" only.
    os.environ["EXITMGR_CREDIT_ENTRIES"] = (
        "1" if bool(getattr(cfg, "credit_entries_enabled", False)) else "0")
    dry_run = not arm

    selected_client_id = _selected_client_id(cfg, mode, client_id)
    ib_conn = IBConnection(host=cfg.ib.host, port=cfg.ib.port, client_id=selected_client_id,
                           market_data_type=getattr(cfg.ib, "market_data_type", 3))
    exit_mgr = ExitManager(cfg)
    exit_mgr.ib_conn = ib_conn  # share the one connection

    # STATE OWNERSHIP (2026-08-12, stale-write clobber fix).
    # -----------------------------------------------------
    # In the split deployment the `--mode protective` process is the SOLE OWNER of
    # exitmgr_state.json: it writes peak_prices (the trailing stop), in_flight (the double-close
    # guard), mfe/mae and the trail confirmation every ~30s from live values.  The `--mode entry`
    # process only READS that data, for its reconcile safety gate -- but it also called save() on
    # the reconcile path, and that path fires on EVERY IBKR reconnect, not just at startup
    # (trader.log shows ~390 reconciliations).  StateManager had no reload, so each of those saves
    # flushed a snapshot as old as this process's FIRST state access back over the file, reverting
    # trailing-stop peaks and deleting in-flight close records.
    #
    # Make the entry process's manager READ-ONLY.  Every mutation it makes during reconcile is
    # already made independently by the protective loop's own per-cycle reconcile, from a fresh
    # snapshot, so nothing is lost by not writing here -- the write was redundant, not
    # load-bearing.  Pairs with the reload() in ExitManager._reconcile_on_startup, which keeps the
    # entry gate's READS fresh.  Deliberately NOT applied to `combined`, where one process runs
    # both loops and therefore legitimately owns the file.
    if mode == "entry":
        exit_mgr.state_manager.persist = False
        print("[INFO] entry mode: shared exit state is READ-ONLY here "
              "(the protective process owns exitmgr_state.json)")

    broker_order_lock = asyncio.Lock()
    trader = Trader(
        ib_conn=ib_conn, exit_manager=exit_mgr,
        limits=entry_safety.risk_limits_from_config(cfg),
        approved_names=set(getattr(cfg, "approved_names", [])),
        endpoint=getattr(cfg, "llm_endpoint", "http://127.0.0.1:8082/v1/chat/completions"),
        model=getattr(cfg, "llm_model", ""),
        slack_token=os.environ.get("SLACK_BOT_TOKEN", ""),
        slack_channel=getattr(cfg, "slack_channel", ""),
        approver_ids=set(getattr(cfg, "approver_ids", [])),
        baseline_path=getattr(cfg, "baseline_path", "./day_baseline.json"),
        audit_path=getattr(cfg, "audit_path", "./audit.jsonl"),
        journal_path=cfg.journal.path,
        entry_limit_buffer_pct=getattr(cfg, "entry_limit_buffer_pct", 0.05),
        blocked_sector_keywords=list(getattr(cfg, "blocked_sector_keywords", [])),
        construction_cfg=getattr(cfg, "construction", None),  # 2026-07-01 constructor-rework gates
        caps_tp_tiers=list(getattr(cfg.caps, "tp_tiers", []) or []),  # 2026-07-03 pot-tiered TP ceiling
        kill_switch_path=cfg.kill_switch.path,  # 2026-07-03: KILL_SWITCH halts ENTRIES too
        config_path=config,
        trading_down_path=TRADING_DOWN_MARKER,
        broker_order_lock=broker_order_lock,
        # ENTRY THROTTLE CEILINGS (2026-07-03 gap-fix): caps.* were loaded but only enforced on the
        # EXIT path; wire them so NEW entries also respect per-cycle / per-day order + notional caps.
        max_orders_per_cycle=int(getattr(cfg.caps, "max_orders_per_cycle", 5)),
        max_orders_per_day=int(getattr(cfg.caps, "max_orders_per_day", 20)),
        max_notional_per_day=float(getattr(cfg.caps, "max_notional_per_day", 50000.0)),
        # TAKE-PROFIT-AND-RELOAD (2026-07-03). OFF BY DEFAULT (reload_enabled=False => no-op);
        # Trevor flips it on in config.yaml `trading:` after re-arm + validation. Knobs gate churn.
        auto_approve_within_gates=bool(getattr(cfg, "auto_approve_within_gates", False)),
        reload_enabled=bool(getattr(cfg, "reload_enabled", False)),
        reload_conviction_min=float(getattr(cfg, "reload_conviction_min", 6)),
        reload_friction_k=float(getattr(cfg, "reload_friction_k", 1.5)),
        reload_expected_continuation_pct=float(
            getattr(cfg, "reload_expected_continuation_pct", 3.0)),
        reload_max_per_name_per_day=int(getattr(cfg, "reload_max_per_name_per_day", 2)),
        reload_ttl_cycles=int(getattr(cfg, "reload_ttl_cycles", 3)),
    )

    async def run():
        if not await ib_conn.connect(retries=3, retry_delay=10):
            print("[ERROR] could not connect to IBKR"); return
        if not await exit_mgr._reconcile_on_startup():
            print("[ERROR] reconciliation failed - aborting"); await ib_conn.disconnect(); return
        print(f"[INFO] {'LIVE (--arm)' if arm else 'DRY RUN'} | port {cfg.ib.port}")
        if loop:
            connection_lock = asyncio.Lock()

            async def _ensure_live_connection():
                async with connection_lock:
                    if await ib_conn.ensure_connected():
                        return True
                    print("[WARN] IBKR link unhealthy -- forcing reconnect")
                    if not await ib_conn.reconnect(retries=3, retry_delay=10):
                        print("[ERROR] reconnect failed")
                        return False
                    if not await exit_mgr._reconcile_on_startup():
                        print("[WARN] post-reconnect reconcile UNSAFE")
                        return False
                    return True

            async def _protective_loop():
                # Static rules never call the model.  The order lock serializes their SELL mutations
                # with BUY submission while leaving slow model/Slack waits free to run concurrently.
                cadence = min(60, max(15, int(protective_interval)))
                while True:
                    started = asyncio.get_running_loop().time()
                    try:
                        if await _ensure_live_connection():
                            async with broker_order_lock:
                                await exit_mgr.run_cycle(
                                    dry_run, regime=trader._regime,
                                    price_stats=trader._price_stats, defer_model=True)
                            trader._exit_fail_streak = 0
                        else:
                            trader._exit_fail_streak += 1
                    except Exception as e:
                        trader._exit_fail_streak += 1
                        print(f"[ERROR] protective cycle error: {e}")
                    elapsed = asyncio.get_running_loop().time() - started
                    await asyncio.sleep(max(0.0, cadence - elapsed))

            async def _model_assessment_loop():
                """MODEL-DRIVEN EXIT MANAGEMENT (2026-08-12), deliberately OFF the stop path.

                `assess_positions` is a blocking HTTP call with a 75s timeout, and run_cycle
                awaits it BEFORE evaluating stops -- so calling the model from `_protective_loop`
                would delay a protective stop by up to a full model timeout on every cycle.  A
                delayed stop is worse than the bug we are fixing.

                Instead: the protective cycle publishes the position views it already built, this
                task turns them into model decisions beside the loop, and the next 30s cycle picks
                them up with a dict lookup (age-bounded -- see _consume_model_decisions).  This
                task takes NO lock, holds NO broker connection and places NO order, so it cannot
                delay, block or deadlock exit management.  If it dies, stalls or the model is
                down, the cache simply ages out and every cycle runs pure static rules.
                """
                # A bad config value must not take this task down: `asyncio.gather` below would
                # propagate the exception and cancel the PROTECTIVE loop with it.  Fall back to the
                # default cadence instead.
                try:
                    cadence = max(60.0, float(getattr(cfg, "manage_positions_interval_s",
                                                      ExitManager.MGMT_DEFAULT_INTERVAL_S)))
                except (TypeError, ValueError):
                    cadence = float(ExitManager.MGMT_DEFAULT_INTERVAL_S)
                    print("[WARN] manage_positions_interval_s is not a number; using "
                          f"{cadence:.0f}s")
                # Let at least one protective cycle publish its views before the first assessment.
                await asyncio.sleep(min(45.0, cadence))
                while True:
                    started = asyncio.get_running_loop().time()
                    try:
                        await exit_mgr.assess_positions_offcycle()
                    except asyncio.CancelledError:
                        raise
                    except BaseException as e:   # noqa: BLE001 - see below
                        # DELIBERATELY BROAD.  This task is gathered with `_protective_loop`, so
                        # ANY escaping exception here cancels the loop that places stops.  Nothing
                        # the model does is worth that: log it and take the next tick.
                        print(f"[ERROR] off-cycle model assessment error: {e!r}")
                    elapsed = asyncio.get_running_loop().time() - started
                    await asyncio.sleep(max(0.0, cadence - elapsed))

            async def _entry_loop():
                entry_cadence = max(60, int(interval))
                while True:
                    # Sleep to the session boundary rather than free-running on a fixed cadence:
                    # a fixed interval has arbitrary phase and can leave an interval-sized hole
                    # across the open (2026-08-18: last cycle 13:25:57Z, bell 13:30Z).
                    _wait = _entry_window_wait()
                    if _wait > 0:
                        # Capped so a long overnight wait stays interruptible and re-reads config.
                        await asyncio.sleep(min(_wait, 900.0))
                        continue
                    started = asyncio.get_running_loop().time()
                    try:
                        if await _ensure_live_connection():
                            await trader.run_once(dry_run, skip_exit_cycle=True)
                    except Exception as e:
                        print(f"[ERROR] entry/model cycle error: {e}")
                    elapsed = asyncio.get_running_loop().time() - started
                    await asyncio.sleep(max(0.0, entry_cadence - elapsed))

            loops = []
            if mode in ("combined", "protective"):
                loops.append(_protective_loop())
                # The assessor lives in the SAME process as the protective loop -- the one process
                # that owns the exit state and places closing orders -- so model-driven exits can
                # never be issued by two processes for one position.
                if getattr(cfg, "manage_positions", False):
                    loops.append(_model_assessment_loop())
                    print("[INFO] model-driven exit management ON (off-cycle assessor; "
                          "static stops keep running every "
                          f"{min(60, max(15, int(protective_interval)))}s regardless)")
            if mode in ("combined", "entry"):
                loops.append(_entry_loop())
            await asyncio.gather(*loops)
        else:
            if mode == "protective":
                await exit_mgr.run_cycle(dry_run, regime=None, price_stats={}, defer_model=True)
            elif mode == "entry":
                await trader.run_once(dry_run, skip_exit_cycle=True)
            else:
                await trader.run_once(dry_run)
        await ib_conn.disconnect()

    asyncio.run(run())


if __name__ == "__main__":
    app()
