"""CLI entry point for Exit Manager."""

import asyncio
import typer

from exitmgr.config import load_config
from exitmgr.manager import ExitManager


app = typer.Typer(
    name="exitmgr",
    help="Automated exit management for IBKR long call positions",
)


@app.command()
def main(
    config: str = typer.Option("config.yaml", "--config", "-c", help="Path to config YAML"),
    dry_run: bool = typer.Option(True, "--dry-run", help="Dry run mode (default true)"),
    arm: bool = typer.Option(False, "--arm", help="Arm for live trading (overrides dry-run)"),
    loop: bool = typer.Option(False, "--loop", help="Run in loop mode"),
    interval: int = typer.Option(60, "--interval", help="Loop interval in seconds"),
    max_orders_cycle: int = typer.Option(None, "--max-orders-cycle", help="Max orders per cycle"),
    max_orders_day: int = typer.Option(None, "--max-orders-day", help="Max orders per day"),
    max_notional_day: float = typer.Option(None, "--max-notional-day", help="Max notional per day"),
):
    """Run the exit manager."""
    # Load configuration
    cfg = load_config(
        config_path=config,
        dry_run=dry_run,
        arm=arm,
        loop=loop,
        interval=interval,
        max_orders_cycle=max_orders_cycle,
        max_orders_day=max_orders_day,
        max_notional_day=max_notional_day,
    )

    # Create and run manager
    manager = ExitManager(cfg)

    # Run async event loop
    try:
        asyncio.run(manager.run())
    except KeyboardInterrupt:
        print("\n[INFO] Interrupted by user")
    except Exception as e:
        print(f"[ERROR] Unexpected error: {e}")
        raise


if __name__ == "__main__":
    app()
