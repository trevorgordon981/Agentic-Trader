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
from exitmgr import entry_safety, model_release_gate
from exitmgr.trader import Trader

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
    dry_run = not arm

    try:
        _release_gate = model_release_gate.settings_from_mapping(
            {"model_release_gate": cfg.model_release_gate}
            if getattr(cfg, "model_release_gate", None) is not None else {})
    except model_release_gate.ModelReleaseGateError as exc:
        # Do not abort the process: combined/protective mode must retain its
        # risk-reducing SELL loop.  Carry a poisoned enabled setting so every
        # future BUY fails closed at the final seam while exits continue.
        print(f"[BLOCKED] new entries: invalid model release gate configuration: {exc}. "
              "Protective exits remain armed.")
        _release_gate = model_release_gate.ModelReleaseGateSettings(
            enabled=True, configuration_error=str(exc))

    selected_client_id = _selected_client_id(cfg, mode, client_id)
    ib_conn = IBConnection(host=cfg.ib.host, port=cfg.ib.port, client_id=selected_client_id,
                           market_data_type=getattr(cfg.ib, "market_data_type", 3))
    exit_mgr = ExitManager(cfg)
    exit_mgr.ib_conn = ib_conn  # share the one connection

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
        reload_enabled=bool(getattr(cfg, "reload_enabled", False)),
        reload_conviction_min=float(getattr(cfg, "reload_conviction_min", 6)),
        reload_friction_k=float(getattr(cfg, "reload_friction_k", 1.5)),
        reload_max_per_name_per_day=int(getattr(cfg, "reload_max_per_name_per_day", 2)),
        reload_ttl_cycles=int(getattr(cfg, "reload_ttl_cycles", 3)),
        model_release_gate_settings=_release_gate,
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

            async def _entry_loop():
                entry_cadence = max(60, int(interval))
                while True:
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
