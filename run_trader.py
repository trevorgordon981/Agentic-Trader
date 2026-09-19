"""Run the entry and protective services. The continuous entry loop and daily slate can
submit a model-authored candidate automatically when deterministic risk gates grant
autonomous authority through auto_approve_within_gates; this is the no-tap path and no approval is awaited.
A refreshed exact order clears every hard gate before submission. Other approval cards
wait for the configured human reaction.
"""
import asyncio
import os
from pathlib import Path
import sys
from typing import Optional
import typer

from exitmgr.config import load_config
from exitmgr.connection import IBConnection
from exitmgr.manager import ExitManager
from exitmgr.state import StateCorruptionError
from exitmgr import entry_safety
from exitmgr.trader import Trader, audit
from exitmgr.trader import (entry_window_wait_seconds as _entry_window_wait,
                            _market_open_at)
from exitmgr.runtime_identity import freeze_runtime_identity
from exitmgr.protective_clock import ProtectiveClock, alert_text as protective_clock_alert
from exitmgr.log_streams import install_standard_stream_rotation

app = typer.Typer(help="LLM trading orchestrator (propose -> gate -> authorize -> execute -> manage)")






TRADING_DOWN_MARKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "TRADING_DOWN")

_QUOTE_RTH_CADENCE_S = 10.0
_QUOTE_CLOSED_CADENCE_S = 300.0


def _protective_quote_lane_plan(now=None):
    """Public API contract; production-derived narrative omitted."""
    opened = _market_open_at(now)
    if opened:
        return True, _QUOTE_RTH_CADENCE_S



    return False, min(_QUOTE_CLOSED_CADENCE_S,
                      _entry_window_wait(now, delay_min=0))


def _refuse_if_trading_down(arm: bool, mode: str = "combined") -> None:
    """Public API contract; production-derived narrative omitted."""
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


def _prepare_entry_fence(arm: bool, mode: str) -> bool:
    if arm and mode == "entry":
        from exitmgr.entry_startup import ensure_entry_startup_fence
        return ensure_entry_startup_fence()
    return False


def _config_interval_for_mode(mode: str, interval: int) -> Optional[int]:
    """Public API contract; production-derived narrative omitted."""
    return None if mode == "protective" else interval


def _bind_exit_connection(exit_mgr: ExitManager, ib_conn: IBConnection) -> None:
    """Public API contract; production-derived narrative omitted."""
    exit_mgr.ib_conn = ib_conn
    exit_mgr.order_manager.ib_conn = ib_conn


def _invalidate_protective_deadline(conn: IBConnection) -> None:
    """Public API contract; production-derived narrative omitted."""
    conn._link_fault = True
    conn._invalidate_price_observations()


def _bind_quote_connection(exit_mgr: ExitManager, quote_ib_conn: IBConnection) -> None:
    """Public API contract; production-derived narrative omitted."""
    exit_mgr.quote_ib_conn = quote_ib_conn


def _install_service_log_rotation(mode: str):
    """Public API contract; production-derived narrative omitted."""
    if os.environ.get("EXITMGR_ROTATE_SERVICE_LOGS") != "1":
        return None
    root = Path(os.environ.get("EXITMGR_APP_LOG_DIR", "~/logs")).expanduser().resolve()
    stem = "exitmgr-%s" % str(mode).strip().lower()
    installed = install_standard_stream_rotation(
        root / (stem + ".log"), root / (stem + ".error.log"))
    if installed is not None:
        print("[log-rotation] app-owned streams active: %s / %s" %
              (installed.stdout.path, installed.stderr.path))
    return installed


async def _ensure_quote_connection(conn: IBConnection) -> bool:
    """Public API contract; production-derived narrative omitted."""
    if conn.ib is None:
        return await conn.connect(retries=1, retry_delay=1)
    if await conn.ensure_connected():
        return True
    print("[WARN] protective quote lane unhealthy -- reconnecting "
          f"clientId={conn.client_id}")
    return await conn.reconnect(retries=1, retry_delay=1)


@app.command()
def main(
    config: str = typer.Option("config.yaml", "--config", "-c"),
    arm: bool = typer.Option(
        False, "--arm",
        help=("LIVE: place real orders. NOT a per-entry human tap: with "
              "trading.auto_approve_within_gates on (it is), --mode entry submits gate-clean "
              "proposals itself and only Slack-posts a receipt. See the module docstring.")),
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
    quote_client_id: int = typer.Option(
        118, "--quote-client-id", min=1, max=2_147_483_647,
        help="read-only protective quote lane; must differ from every order lane"),
):
    mode = str(mode).strip().lower()
    if mode not in {"combined", "entry", "protective"}:
        raise typer.BadParameter("--mode must be combined, entry, or protective")



    _refuse_if_trading_down(arm, mode)

    cfg = load_config(
        config_path=config,
        arm=arm,
        loop=loop,
        interval=_config_interval_for_mode(mode, interval),
    )
    runtime_identity = freeze_runtime_identity(cfg, require_clean_code=True)
    print("[IDENTITY] code_version=%s policy_version=%s" %
          (runtime_identity.code_version, runtime_identity.policy_version))
    audit(getattr(cfg, "audit_path", "./audit.jsonl"), "process_runtime_identity",
          mode=mode, code_version=runtime_identity.code_version,
          policy_version=runtime_identity.policy_version)




    os.environ["EXITMGR_CREDIT_ENTRIES"] = (
        "1" if bool(getattr(cfg, "credit_entries_enabled", False)) else "0")
    os.environ["EXITMGR_ASSIGNED_STOCK_AUTHORITY"] = (
        "1" if bool(getattr(cfg, "assigned_stock_authority_enabled", False)) else "0")
    dry_run = not arm

    if _prepare_entry_fence(arm, mode):
        print("[INFO] restored reboot-cleared legacy fence from validated durable evidence")

    selected_client_id = _selected_client_id(cfg, mode, client_id)
    _order_ids = {selected_client_id, int(cfg.ib.client_id),
                  int(getattr(cfg.ib, "protective_client_id", 189))}
    if mode in {"combined", "protective"} and int(quote_client_id) in _order_ids:
        raise typer.BadParameter("quote client id must differ from every order client id")
    _entry_startup = {"startup_completed_orders": False} if mode == "entry" else {}
    ib_conn = IBConnection(host=cfg.ib.host, port=cfg.ib.port, client_id=selected_client_id,
                           market_data_type=getattr(cfg.ib, "market_data_type", 3),
                           **_entry_startup)
    quote_ib_conn = (IBConnection(
        host=cfg.ib.host, port=cfg.ib.port, client_id=int(quote_client_id),
        market_data_type=getattr(cfg.ib, "market_data_type", 3))
        if mode in {"combined", "protective"} else None)



    try:
        exit_mgr = ExitManager(
            cfg, runtime_identity=runtime_identity, state_persist=(mode != "entry"),
            journal_side_effects=(mode != "entry"))
        _ = exit_mgr.state_manager.state
    except StateCorruptionError as exc:
        print(f"[FATAL] {exc}", file=sys.stderr)
        raise typer.Exit(code=78)
    _bind_exit_connection(exit_mgr, ib_conn)
    if quote_ib_conn is not None:
        _bind_quote_connection(exit_mgr, quote_ib_conn)


















    if mode == "entry":
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
        construction_cfg=getattr(cfg, "construction", None),
        caps_tp_tiers=list(getattr(cfg.caps, "tp_tiers", []) or []),
        kill_switch_path=cfg.kill_switch.path,
        config_path=config,
        resolved_config=cfg,
        runtime_identity=runtime_identity,
        trading_down_path=TRADING_DOWN_MARKER,
        broker_order_lock=broker_order_lock,


        entry_ledger_required=(mode != "protective"),


        max_orders_per_cycle=int(getattr(cfg.caps, "max_orders_per_cycle", 5)),
        max_orders_per_day=int(getattr(cfg.caps, "max_orders_per_day", 20)),
        max_notional_per_day=float(getattr(cfg.caps, "max_notional_per_day", 50000.0)),


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

            protective_clock = ProtectiveClock.for_state_path(cfg.state.path)

            async def _quote_connection_loop():
                """Public API contract; production-derived narrative omitted."""
                assert quote_ib_conn is not None
                while True:
                    refresh, cadence = _protective_quote_lane_plan()
                    if not refresh:
                        await asyncio.sleep(cadence)
                        continue
                    started = asyncio.get_running_loop().time()
                    try:
                        await _ensure_quote_connection(quote_ib_conn)
                        if quote_ib_conn.is_healthy():
                            await exit_mgr.refresh_protective_quotes_offcycle()
                    except asyncio.CancelledError:
                        raise
                    except BaseException as exc:
                        print(f"[WARN] protective quote-lane reconnect failed ({exc!r}); "
                              "broker marks remain available")



                    elapsed = asyncio.get_running_loop().time() - started
                    await asyncio.sleep(max(0.1, cadence - elapsed))

            async def _protective_loop():


                cadence = min(60, max(15, int(protective_interval)),
                              max(5.0, float(cfg.loop.protective_poll_seconds)))
                cycle_timeout = float(cfg.loop.protective_cycle_timeout_seconds)
                while True:
                    started = asyncio.get_running_loop().time()
                    heartbeat_sequence = None
                    try:



                        try:
                            _status = exit_mgr.protective_clock_status()
                            heartbeat_sequence = await asyncio.to_thread(
                                protective_clock.start_cycle,
                                unresolved_close_count=_status["unresolved_close_count"],
                                last_good_broker_view_at=_status["last_good_broker_view_at"])
                        except Exception as _he:
                            print(f"[ERROR] protective heartbeat START write failed: {_he}")



                        async with asyncio.timeout(cycle_timeout):
                            if await _ensure_live_connection():
                                async with broker_order_lock:
                                    await exit_mgr.run_cycle(
                                        dry_run, regime=trader._regime,
                                        price_stats=trader._price_stats, defer_model=True,
                                        research_context=getattr(trader, "_research_context", None))
                                trader._exit_fail_streak = 0
                            else:
                                trader._exit_fail_streak += 1
                    except asyncio.TimeoutError:
                        _invalidate_protective_deadline(ib_conn)
                        trader._exit_fail_streak += 1
                        print(f"[ERROR] protective cycle exceeded {cycle_timeout:.0f}s; "
                              "pending order state retained for reconciliation")
                        exit_mgr._post_unthrottled_alert(
                            f"Protective cycle exceeded {cycle_timeout:.0f}s. "
                            "Pending orders remain latched; next cycle will reconcile before retry.",
                            "protective-cycle-timeout")
                    except Exception as e:
                        trader._exit_fail_streak += 1
                        print(f"[ERROR] protective cycle error: {e}")
                    finally:
                        duration = asyncio.get_running_loop().time() - started
                        if heartbeat_sequence is not None:
                            try:
                                _status = exit_mgr.protective_clock_status()
                                await asyncio.to_thread(
                                    protective_clock.complete_cycle, heartbeat_sequence,
                                    duration_seconds=duration,
                                    unresolved_close_count=_status["unresolved_close_count"],
                                    last_good_broker_view_at=_status["last_good_broker_view_at"])
                            except Exception as _he:
                                print(f"[ERROR] protective heartbeat END write failed: {_he}")
                    elapsed = asyncio.get_running_loop().time() - started
                    await asyncio.sleep(max(0.0, cadence - elapsed))

            async def _research_refresh_loop():
                """Public API contract; production-derived narrative omitted."""
                from exitmgr import research, regime as _rg
                while True:
                    started = asyncio.get_running_loop().time()
                    try:
                        positions = await trader._open_positions()
                        names = sorted({"SPY", "QQQ", "IWM"}
                                       | {p.underlying for p in positions or [] if not p.is_index})
                        if names:
                            data = await research.gather(
                                trader.ib_conn.ib, names, single_names=names)
                            ps = data.get("price_stats") or {}
                            new_regime = trader._regime
                            if ps:
                                new_regime = _rg.classify_regime(
                                    [ps.get("SPY"), ps.get("QQQ"), ps.get("IWM")],
                                    data.get("vix"))
                            new_research = {
                                k: data.get(k) for k in
                                ("events", "headlines", "web_news", "movers", "options_flow",
                                 "rag_snippets", "vix")}


                            if ps:
                                trader._price_stats = ps
                                trader._regime = new_regime
                            trader._research_context = new_research
                    except asyncio.CancelledError:
                        raise
                    except BaseException as _ce:
                        print(f"[WARN] off-cycle protective context refresh failed "
                              f"(exits unaffected, previous snapshot retained): {_ce!r}")
                    elapsed = asyncio.get_running_loop().time() - started
                    await asyncio.sleep(max(0.0, 600.0 - elapsed))

            async def _terminal_history_loop():
                """Public API contract; production-derived narrative omitted."""
                while True:
                    started = asyncio.get_running_loop().time()
                    try:
                        if ib_conn.is_healthy():
                            await exit_mgr.refresh_terminal_history_offcycle()
                    except asyncio.CancelledError:
                        raise
                    except BaseException as _te:
                        print(f"[WARN] off-cycle terminal-history worker failed "
                              f"(latches retained): {_te!r}")
                    elapsed = asyncio.get_running_loop().time() - started

                    await asyncio.sleep(max(0.0, 60.0 - elapsed))

            async def _atr_cache_refresh_loop():
                """Public API contract; production-derived narrative omitted."""
                while True:
                    started = asyncio.get_running_loop().time()
                    try:
                        await exit_mgr.refresh_requested_atr_offcycle()
                    except asyncio.CancelledError:
                        raise
                    except BaseException as exc:
                        print(f"[WARN] off-cycle ATR worker failed (static exits unaffected; "
                              f"will retry): {exc!r}")
                    elapsed = asyncio.get_running_loop().time() - started
                    await asyncio.sleep(max(0.0, 15.0 - elapsed))

            async def _protective_clock_watchdog():
                """Public API contract; production-derived narrative omitted."""
                from exitmgr import alerting
                while True:
                    try:
                        claimed = await asyncio.to_thread(protective_clock.claim_alert)
                        if claimed is not None:
                            kind, snapshot = claimed
                            channel = ((getattr(cfg, "error_channel", "") or "")
                                       or (getattr(cfg, "alerts_channel", "") or "")
                                       or alerting.error_channel())
                            if channel:
                                await asyncio.to_thread(
                                    alerting.post, protective_clock_alert(kind, snapshot), channel,
                                    label="protective-clock-slo")
                    except asyncio.CancelledError:
                        raise
                    except BaseException as _we:
                        print(f"[ERROR] protective-clock watchdog failed: {_we!r}")
                    await asyncio.sleep(5.0)

            async def _model_assessment_loop():
                """Public API contract; production-derived narrative omitted."""



                try:
                    cadence = max(60.0, float(getattr(cfg, "manage_positions_interval_s",
                                                      ExitManager.MGMT_DEFAULT_INTERVAL_S)))
                except (TypeError, ValueError):
                    cadence = float(ExitManager.MGMT_DEFAULT_INTERVAL_S)
                    print("[WARN] manage_positions_interval_s is not a number; using "
                          f"{cadence:.0f}s")

                await asyncio.sleep(min(45.0, cadence))
                while True:
                    started = asyncio.get_running_loop().time()
                    try:
                        await exit_mgr.assess_positions_offcycle()
                    except asyncio.CancelledError:
                        raise
                    except BaseException as e:



                        print(f"[ERROR] off-cycle model assessment error: {e!r}")
                    elapsed = asyncio.get_running_loop().time() - started
                    await asyncio.sleep(max(0.0, cadence - elapsed))

            async def _entry_loop():
                entry_cadence = max(60, int(interval))
                while True:



                    _wait = _entry_window_wait()
                    if _wait > 0:

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
                loops.append(_quote_connection_loop())
                loops.append(_research_refresh_loop())
                loops.append(_terminal_history_loop())
                loops.append(_atr_cache_refresh_loop())
                loops.append(_protective_clock_watchdog())



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
        if quote_ib_conn is not None:
            await quote_ib_conn.disconnect()
        await ib_conn.disconnect()





    _service_logs = _install_service_log_rotation(mode)
    try:
        asyncio.run(run())
    finally:
        if _service_logs is not None:
            _service_logs.restore()


if __name__ == "__main__":
    app()
