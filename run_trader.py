"""CLI entry for the LLM trading orchestrator.

DEFAULTS ARE SAFE: dry-run ON, paper port. Live trading requires BOTH --arm AND a Slack
approval per entry. Read the README before using --arm.
"""
import asyncio
import os
import sys
import typer

from exitmgr.config import load_config
from exitmgr.connection import IBConnection
from exitmgr.manager import ExitManager
from exitmgr.risk import RiskLimits
from exitmgr.trader import Trader

app = typer.Typer(help="LLM trading orchestrator (propose -> gate -> approve -> execute -> manage)")

# TRADING-DOWN MARKER (2026-07-03 gap-fix). The wrapper run_trader_service.sh refuses to --arm while
# this marker exists, but a bare `python run_trader.py --arm` bypassed the wrapper entirely. Enforce
# the SAME guard here so a manual arm can't skip it. Located next to this file (the repo root), so it
# is found regardless of the caller's cwd. Only the ARMING/LIVE path is blocked -- a dry-run/read-only
# invocation (no --arm) is never blocked.
TRADING_DOWN_MARKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "TRADING_DOWN")


def _refuse_if_trading_down(arm: bool) -> None:
    """Refuse to ARM live trading while the TRADING_DOWN marker exists (gap-fix 2026-07-03),
    mirroring run_trader_service.sh so a manual `python run_trader.py --arm` can't bypass it.
    Dry-run / read-only invocations (arm=False) are never blocked. Raises typer.Exit(1) to refuse."""
    if arm and os.path.exists(TRADING_DOWN_MARKER):
        print("[run_trader] TRADING_DOWN marker present -- refusing to arm live trading.", file=sys.stderr)
        print(f"[run_trader] rm {TRADING_DOWN_MARKER} to re-enable armed trading.", file=sys.stderr)
        raise typer.Exit(code=1)


@app.command()
def main(
    config: str = typer.Option("config.yaml", "--config", "-c"),
    arm: bool = typer.Option(False, "--arm", help="LIVE: place real orders (still needs Slack approval per entry)"),
    loop: bool = typer.Option(False, "--loop"),
    interval: int = typer.Option(900, "--interval", help="seconds between cycles in --loop"),
):
    # TRADING-DOWN GUARD (2026-07-03 gap-fix): refuse to ARM live trading while the marker exists,
    # mirroring run_trader_service.sh so a manual `python run_trader.py --arm` cannot bypass it.
    # Dry-run / read-only invocations (no --arm) are intentionally NOT blocked.
    _refuse_if_trading_down(arm)

    cfg = load_config(config_path=config, arm=arm, loop=loop, interval=interval)
    dry_run = not arm

    # conviction->size curve from config (YAML keys may be str or int); None -> dataclass default.
    # PENDING CONVICTION CALIBRATION -- ships flat at the base cap.
    _curve_raw = getattr(cfg, "conviction_size_curve", None)
    conviction_curve = None
    if _curve_raw:
        try:
            conviction_curve = {int(k): float(v) for k, v in dict(_curve_raw).items()}
        except (TypeError, ValueError):
            conviction_curve = None
    # back-compat: an old config that only set confident_conviction lowers the bypass threshold.
    _bypass = int(getattr(cfg, "cap_bypass_min_conviction",
                          getattr(cfg, "confident_conviction", 6)))
    # conviction->size MULTIPLIERS from config (YAML keys may be str or int) -- normalize to int
    # keys exactly like conviction_size_curve above, else risk.conviction_multiplier's exact-match
    # lookup misses every str key ("6" != 6) and silently returns the flat 1.0 (dead config).
    _mult_raw = getattr(cfg, "conviction_size_multipliers", None)
    conviction_multipliers = None
    if _mult_raw:
        try:
            conviction_multipliers = {int(k): float(v) for k, v in dict(_mult_raw).items()}
        except (TypeError, ValueError):
            conviction_multipliers = None

    ib_conn = IBConnection(host=cfg.ib.host, port=cfg.ib.port, client_id=cfg.ib.client_id,
                           market_data_type=getattr(cfg.ib, "market_data_type", 3))
    exit_mgr = ExitManager(cfg)
    exit_mgr.ib_conn = ib_conn  # share the one connection

    trader = Trader(
        ib_conn=ib_conn, exit_manager=exit_mgr,
        limits=RiskLimits(
            max_trade_pct=getattr(cfg, "max_trade_pct", 0.12),
            max_concurrent=getattr(cfg, "max_concurrent", 4),
            daily_halt_pct=getattr(cfg, "daily_halt_pct", 0.08),
            pot_cap_usd=getattr(cfg, "pot_cap_usd", None),
            allow_any_name=bool(getattr(cfg, "allow_model_names", False)),
            confident_full_size=bool(getattr(cfg, "confident_full_size", False)),
            cap_bypass_min_conviction=_bypass,
            cash_buffer_pct=float(getattr(cfg, "cash_buffer_pct", 0.05)),
            **({"conviction_size_curve": conviction_curve} if conviction_curve else {}),
            blocked_names={n.upper() for n in getattr(cfg, "blocked_names", [])},
            max_sector_agg_pct=float(getattr(cfg, "max_sector_agg_pct", 0.25)),
            # static symbol->sector/cluster map; keys uppercased (like blocked_names). Empty => no-op.
            sector_map={str(k).upper(): str(v) for k, v in (getattr(cfg, "sector_map", {}) or {}).items()},
            # WIRED 2026-07-03: config-driven per-conviction size multipliers, keys normalized to int
            # (str-key config was dead: exact-match lookup missed every key). None => flat 1.0.
            conviction_size_multipliers=conviction_multipliers,
        ),
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
    )

    async def run():
        if not await ib_conn.connect(retries=3, retry_delay=10):
            print("[ERROR] could not connect to IBKR"); return
        if not await exit_mgr._reconcile_on_startup():
            print("[ERROR] reconciliation failed - aborting"); await ib_conn.disconnect(); return
        print(f"[INFO] {'LIVE (--arm)' if arm else 'DRY RUN'} | port {cfg.ib.port}")
        if loop:
            while True:
                try:
                    # SELF-HEAL: a dropped IBKR link must not leave exits blind. On Error 1100
                    # the local socket stays open (isConnected() lies True) while the uplink is
                    # dead -- so we use an ACTIVE liveness probe (ensure_connected) instead of
                    # isConnected(), and force a real reconnect when it's unhealthy.
                    if not await ib_conn.ensure_connected():
                        print("[WARN] IBKR link unhealthy -- forcing reconnect")
                        if await ib_conn.reconnect(retries=3, retry_delay=10):
                            # HONOR THE RECONCILE RESULT (2026-07-03): a post-reconnect reconcile
                            # that comes back UNSAFE means broker/journal state disagrees -- do NOT
                            # trade this cycle (the per-cycle reconcile inside run_cycle re-checks).
                            if not await exit_mgr._reconcile_on_startup():
                                print("[WARN] post-reconnect reconcile UNSAFE -- skipping this cycle (no trading)")
                                await asyncio.sleep(interval); continue
                            print("[INFO] reconnected to IBKR")
                        else:
                            print("[ERROR] reconnect failed; retrying next cycle")
                            await asyncio.sleep(interval); continue
                    await trader.run_once(dry_run)
                except Exception as e:
                    print(f"[ERROR] cycle error (loop continues): {e}")
                await asyncio.sleep(interval)
        else:
            await trader.run_once(dry_run)
        await ib_conn.disconnect()

    asyncio.run(run())


if __name__ == "__main__":
    app()
