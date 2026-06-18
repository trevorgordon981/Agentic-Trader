"""CLI entry for the LLM trading orchestrator.

DEFAULTS ARE SAFE: dry-run ON, paper port. Live trading requires BOTH --arm AND a Slack
approval per entry. Read the README before using --arm.
"""
import asyncio
import os
import typer

from exitmgr.config import load_config
from exitmgr.connection import IBConnection
from exitmgr.manager import ExitManager
from exitmgr.risk import RiskLimits
from exitmgr.trader import Trader

app = typer.Typer(help="LLM trading orchestrator (propose -> gate -> approve -> execute -> manage)")


@app.command()
def main(
    config: str = typer.Option("config.yaml", "--config", "-c"),
    arm: bool = typer.Option(False, "--arm", help="LIVE: place real orders (still needs Slack approval per entry)"),
    loop: bool = typer.Option(False, "--loop"),
    interval: int = typer.Option(900, "--interval", help="seconds between cycles in --loop"),
):
    cfg = load_config(config_path=config, arm=arm, loop=loop, interval=interval)
    dry_run = not arm

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
            confident_conviction=int(getattr(cfg, "confident_conviction", 4)),
            blocked_names={n.upper() for n in getattr(cfg, "blocked_names", [])},
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
                    # SELF-HEAL: a dropped IBKR link must not leave exits blind. Reconnect each cycle.
                    if not (ib_conn.ib and ib_conn.ib.isConnected()):
                        print("[WARN] IBKR connection lost -- reconnecting")
                        ib_conn._connected = False
                        try:
                            if ib_conn.ib: ib_conn.ib.disconnect()
                        except Exception:
                            pass
                        if await ib_conn.connect(retries=3, retry_delay=10):
                            await exit_mgr._reconcile_on_startup()
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
