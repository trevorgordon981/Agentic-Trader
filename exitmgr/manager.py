"""Main manager orchestrating the exit management loop."""

import asyncio
import os
import json
import signal
import sys
from datetime import datetime, date
from pathlib import Path
from typing import Optional, Dict, List, Set

from exitmgr.config import Config
from exitmgr.connection import IBConnection, PositionData
from exitmgr.order import OrderManager
from exitmgr.rules import evaluate_position, ExitTrigger, days_to_expiry
from exitmgr.position_manager import assess_positions
from exitmgr import regime as regime_mod
from exitmgr.state import StateManager


class ExitManager:
    """Main orchestrator for exit management."""

    def __init__(self, config: Config):
        self.config = config
        self.ib_conn = IBConnection(
            host=config.ib.host,
            port=config.ib.port,
            client_id=config.ib.client_id,
        )
        self.state_manager = StateManager(config.state.path)
        self.order_manager = OrderManager(self.ib_conn, self.state_manager)

        self._running = False
        self._shutdown_requested = False

        # Track peak prices for trailing stop (in-memory cache)
        self._peak_prices: Dict[int, float] = {}

        # Load journal entries
        self._journal_entries: Dict[int, dict] = {}
        self._load_journal()

    def _load_journal(self) -> None:
        """Load trade journal from trades.log."""
        journal_path = Path(self.config.journal.path)
        if not journal_path.exists():
            print(f"[WARN] Journal file not found: {self.config.journal.path}")
            return

        self._journal_entries = {}
        try:
            with open(journal_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        con_id = entry.get("contract_id")
                        if con_id is not None:
                            self._journal_entries[int(con_id)] = entry
                    except json.JSONDecodeError as e:
                        print(f"[WARN] Could not parse journal line: {e}")
        except Exception as e:
            print(f"[ERROR] Could not load journal: {e}")

        print(f"[INFO] Loaded {len(self._journal_entries)} journal entries")

    def _post_exit_alert(self, symbol: str, trigger) -> None:
        """Real-time ping to #trading-alerts the moment a position is sold by the exit manager."""
        import os
        channel = getattr(self.config, "alerts_channel", "") or ""
        token = os.environ.get("SLACK_BOT_TOKEN", "")
        if not (channel and token):
            return
        names = {"profit_target": "take-profit :dart:", "stop": "STOP :octagonal_sign:",
                 "time_stop": "time stop :hourglass:", "trailing_stop": "trailing stop"}
        why = names.get(getattr(trigger, "trigger_type", ""), getattr(trigger, "trigger_type", "exit"))
        pnl = getattr(trigger, "pnl_pct", 0.0)
        emoji = ":green_circle:" if pnl >= 0 else ":red_circle:"
        try:
            from exitmgr import approval
            approval.post_proposal(token, channel,
                f":rotating_light: *EXIT — {symbol}* sold ({why}) {emoji} *P&L {pnl:+.1f}%*\n"
                f"_{getattr(trigger, 'message', '')}_")
        except Exception as e:
            print(f"[WARN] exit alert post failed: {e}")

    def _exits_log_path(self) -> str:
        """Durable, append-only exits log (JSONL) next to the journal. Realized P&L was
        previously NOWHERE on disk (only inside IBKR) -- this is the on-disk record that
        conviction_calibration.py reads via --fills (keyed contract_id -> realized_pnl)."""
        cfg_path = getattr(getattr(self.config, "exits", None), "path", None)
        if cfg_path:
            return cfg_path
        return os.path.join(os.path.dirname(self.config.journal.path) or ".", "exits.log")

    def _recover_conviction(self, je: dict, symbol: str) -> Optional[float]:
        """Conviction carried from the original entry. Prefer the value the trader now
        persists ON the journal entry; fall back to the audit.jsonl daily_rec_posted events
        (matched by symbol + long strike), mirroring how conviction_calibration.py recovers it.
        Returns None if not recoverable (so calibration can bucket it 'unknown')."""
        c = je.get("conviction")
        try:
            if c is not None and float(c) >= 0:
                return float(c)
        except (TypeError, ValueError):
            pass
        # fall back to audit.jsonl
        audit_path = getattr(self.config, "audit_path", "") or os.path.join(
            os.path.dirname(self.config.journal.path) or ".", "audit.jsonl")
        if not (symbol and os.path.exists(audit_path)):
            return None
        long_strike = je.get("strike")
        best = None  # (conviction, had_strike_match)
        try:
            with open(audit_path) as f:
                for line in f:
                    line = line.strip()
                    if not line or '"daily_rec_posted"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if d.get("event") != "daily_rec_posted" or d.get("underlying") != symbol:
                        continue
                    conv = d.get("conviction")
                    if conv is None:
                        continue
                    order = d.get("order") or ""
                    toks = order.replace("/", " ").replace("C", " ").replace("P", " ")
                    strike_hit = False
                    if long_strike is not None:
                        for tok in toks.split():
                            t = tok.strip("$x@()~,")
                            try:
                                v = float(t)
                            except ValueError:
                                continue
                            if 1e7 <= v <= 9.9e7:   # skip yyyymmdd expiries
                                continue
                            if abs(v - float(long_strike)) < 1e-6:
                                strike_hit = True
                                break
                    if strike_hit:
                        return float(conv)
                    if best is None:
                        best = float(conv)  # symbol-only fallback (closest available)
        except Exception:
            return best
        return best

    def _log_exit(self, con_id: int, symbol: str, trigger, exit_price_per_share: Optional[float],
                  quantity: int, reason: str) -> None:
        """Append-only record of a realized exit to exits.log (JSONL). ADDITIVE -- never changes
        exit behavior, only records the outcome. Schema is consumable by conviction_calibration.py
        --fills (contract_id -> realized_pnl + conviction). Must NOT raise into the exit path."""
        try:
            je = self._journal_entries.get(con_id) or {}
            entry_debit = je.get("debit")
            try:
                entry_debit = float(entry_debit) if entry_debit is not None else None
            except (TypeError, ValueError):
                entry_debit = None
            qty = int(quantity) if quantity else int(je.get("quantity", 1) or 1)
            sp = je.get("spread")
            structure = "spread" if sp else "single"
            proceeds = None
            realized_pnl = None
            realized_pct = None
            if exit_price_per_share is not None:
                try:
                    proceeds = round(float(exit_price_per_share) * 100 * qty, 2)
                except (TypeError, ValueError):
                    proceeds = None
            if proceeds is not None and entry_debit is not None:
                realized_pnl = round(proceeds - entry_debit, 2)
                realized_pct = round(realized_pnl / entry_debit * 100, 2) if entry_debit else None
            entry_ts = je.get("ts")
            holding_days = None
            now = datetime.now().astimezone()
            if entry_ts:
                try:
                    et = datetime.fromisoformat(str(entry_ts).replace("Z", "+00:00"))
                    if et.tzinfo is None:
                        from datetime import timezone as _tz
                        et = et.replace(tzinfo=_tz.utc)
                    holding_days = round((now - et).total_seconds() / 86400.0, 3)
                except Exception:
                    holding_days = None
            rec = {
                "ts": now.isoformat(),                 # close timestamp (close_ts for calibration)
                "close_ts": now.isoformat(),
                "contract_id": con_id,                 # matches journal contract_id / --fills key
                "conId": con_id,
                "symbol": symbol,
                "right": je.get("right"),
                "strike": je.get("strike"),
                "structure": structure,
                "quantity": qty,
                "entry_debit": entry_debit,            # cost basis ($)
                "exit_price_per_share": (round(float(exit_price_per_share), 4)
                                         if exit_price_per_share is not None else None),
                "proceeds": proceeds,                  # exit proceeds ($)
                "realized_pnl": realized_pnl,          # realized P&L ($) -- calibration --fills key
                "realizedPNL": realized_pnl,
                "realized_pnl_pct": realized_pct,      # realized P&L (%)
                "reason": reason,                      # profit_target / stop / time_stop / manual
                "entry_ts": entry_ts,
                "holding_days": holding_days,
                "conviction": self._recover_conviction(je, symbol),
            }
            if sp:
                rec["spread"] = {
                    "short_con_id": sp.get("short_con_id"),
                    "short_strike": sp.get("short_strike"),
                    "width": sp.get("width"),
                }
            with open(self._exits_log_path(), "a") as f:
                f.write(json.dumps(rec, default=str) + "\n")
            print(f"[EXIT-LOG] {symbol} con_id={con_id} reason={reason} "
                  f"pnl=${realized_pnl if realized_pnl is not None else 'n/a'}")
        except Exception as e:
            print(f"[WARN] exits.log write failed for con_id={con_id}: {e}")

    @staticmethod
    def _exit_reason(trigger) -> str:
        """Map an ExitTrigger.trigger_type to the calibration reason vocabulary."""
        tt = (getattr(trigger, "trigger_type", "") or "").lower()
        if "profit" in tt or "take_profit" in tt:
            return "profit_target"
        if "time" in tt:
            return "time_stop"
        if "trail" in tt:
            return "stop"
        if "stop" in tt or "cut" in tt:
            return "stop"
        if "manual" in tt:
            return "manual"
        return tt or "exit"

    def _check_kill_switch(self) -> bool:
        """Check if kill switch file exists. Returns True if kill switch is ACTIVE (stop)."""
        kill_path = Path(self.config.kill_switch.path)
        if kill_path.exists():
            print(f"[KILL SWITCH] Kill switch file detected at {self.config.kill_switch.path} - halting order placement")
            return True
        return False

    def _manual_exit_path(self):
        return os.path.join(os.path.dirname(self.config.journal.path) or ".", "manual_exits.json")

    def _read_manual_exits(self) -> Set[int]:
        try:
            with open(self._manual_exit_path()) as f:
                return {int(x) for x in json.load(f)}
        except Exception:
            return set()

    def _manual_exit_fail(self, sym, cid, reason) -> None:
        # NO silent failure: record the error + ping Slack, then drop the request (avoid retry-spam).
        try:
            with open(os.path.join(os.path.dirname(self.config.journal.path) or ".", "manual_exit_errors.log"), "a") as f:
                f.write(f"{datetime.utcnow().isoformat()} {sym} con_id={cid} FAILED: {reason}\n")
        except Exception:
            pass
        try:
            ch = getattr(self.config, "alerts_channel", "") or ""; tok = os.environ.get("SLACK_BOT_TOKEN", "")
            if ch and tok:
                from exitmgr import approval
                approval.post_proposal(tok, ch, f":warning: *Could not auto-sell {sym}* (one-tap early-exit): {reason}. Sell it manually if you want out.")
        except Exception:
            pass
        print(f"[MANUAL-EXIT] {sym} con_id={cid} FAILED: {reason}")
        self._clear_manual_exit(cid)

    def _clear_manual_exit(self, con_id) -> None:
        try:
            cur = self._read_manual_exits(); cur.discard(int(con_id))
            with open(self._manual_exit_path(), "w") as f:
                json.dump(sorted(cur), f)
        except Exception:
            pass

    def _get_scope_con_ids(self, live_positions: Dict[int, PositionData]) -> Set[int]:
        """Get set of contract IDs to manage based on scope setting."""
        if self.config.scope.mode == "journal":
            # Only manage positions that are in the journal
            return set(self._journal_entries.keys()) & set(live_positions.keys())
        else:
            # Manage all long call positions
            return set(live_positions.keys())

    def _check_caps(self, dry_run: bool) -> tuple[bool, str]:
        """
        Check if we've exceeded any hard caps.
        Returns (can_proceed, reason).
        """
        today = datetime.utcnow().strftime("%Y-%m-%d")
        daily_stats = self.state_manager.state.daily_stats.get(today)

        if daily_stats is None:
            daily_stats = {"orders_placed": 0, "notional_closed": 0.0}
            orders_today = 0
            notional_today = 0.0
        else:
            orders_today = daily_stats.orders_placed
            notional_today = daily_stats.notional_closed

        # Check orders per day cap
        if orders_today >= self.config.caps.max_orders_per_day:
            return False, f"Daily order cap reached: {orders_today} >= {self.config.caps.max_orders_per_day}"

        # Check notional per day cap
        if notional_today >= self.config.caps.max_notional_per_day:
            return False, f"Daily notional cap reached: ${notional_today:.2f} >= ${self.config.caps.max_notional_per_day:.2f}"

        return True, ""

    async def _reconcile_on_startup(self) -> bool:
        """
        Reconcile state on startup.
        Returns True if safe to proceed, False if must abort.
        """
        print("[INFO] Starting reconciliation...")

        # Get live positions and open orders
        try:
            live_positions_raw = await self.ib_conn.get_positions()
            live_open_orders_raw = await self.ib_conn.get_open_orders()
        except Exception as e:
            print(f"[ERROR] Could not fetch live data for reconciliation: {e}")
            return False

        # Convert to dicts expected by reconcile_state
        live_positions = {
            pd.con_id: {"qty": pd.quantity, "avg_cost": pd.avg_cost}
            for pd in live_positions_raw.values()
        }
        live_open_orders = {
            od.con_id: {"order_id": od.order_id, "remaining": od.remaining}
            for od in live_open_orders_raw.values()
        }

        # Build journal entries dict (con_id -> debit)
        journal_debits = {
            con_id: entry.get("debit", 0.0)
            for con_id, entry in self._journal_entries.items()
        }

        # Reconcile
        from exitmgr.state import reconcile_state
        safe, alerts = reconcile_state(
            self.state_manager.state,
            live_positions,
            live_open_orders,
            journal_debits,
        )

        # Log all alerts
        for alert in alerts:
            print(f"[RECONCILE] {alert}")

        if safe:
            print("[RECONCILE] Reconciliation complete - safe to proceed")
            # Persist any changes made during reconciliation
            self.state_manager.save()
            return True
        else:
            print("[RECONCILE] Reconciliation found inconsistencies - ABORTING for safety")
            return False

    def _build_position_views(self, managed_positions, quotes, price_stats=None):
        """Compact, read-only per-position state for the model to assess.
        Mirrors the eval loop's net-price logic (spreads valued long-minus-short).
        price_stats: optional {symbol: momentum_stats} so each view carries the underlying's
        trend strength (lets the model let strong winners run in a bull)."""
        views = []
        for p in managed_positions:
            cid = p.con_id
            q = quotes.get(cid)
            if q is None:
                continue
            cur = q["price"]
            je = self._journal_entries.get(cid) or {}
            sp = je.get("spread")
            if sp and sp.get("short_con_id"):
                sq = quotes.get(int(sp["short_con_id"]))
                if sq is None:
                    continue
                cur = cur - sq["price"]
            qty = min(p.quantity, je.get("quantity", p.quantity))
            entry_debit = je.get("debit") or (p.avg_cost * 100 * p.quantity)
            current_value = cur * 100 * qty
            pnl_pct = round((current_value - entry_debit) / entry_debit * 100, 1) if entry_debit else 0.0
            peak = self.state_manager.state.peak_prices.get(str(cid), cur)
            from_peak = round((cur / peak - 1) * 100, 1) if peak else 0.0
            tc = self.config.rules.trailing
            sym = je.get("symbol", p.symbol)
            tstats = (price_stats or {}).get(sym)
            trend = regime_mod.trend_strength(tstats) if tstats else None
            views.append({
                "con_id": cid,
                "symbol": sym,
                "structure": "spread" if sp else "single",
                "pnl_pct": pnl_pct,
                "pct_from_peak": from_peak,
                "trend": trend,  # {"score":-100..100, "label":...} per-underlying, or None
                "dte": days_to_expiry(getattr(p, "expiry", "")),
                "profit_target_pct": je.get("profit_target_pct") or self.config.rules.profit_target_pct,
                "stop_pct": je.get("stop_pct") or self.config.rules.stop_pct,
                "trail_armed": bool(tc.enabled),
                "trail_activation_gain_pct": tc.activation_gain_pct,
                "trail_giveback_fraction": tc.giveback_fraction,
            })
        return views

    def _apply_decision(self, rules, decision, current_price, entry_debit, quantity, con_id, symbol, regime=None):
        """Apply a model decision to a RulesConfig with regime-aware guardrails.
        Stops are ALWAYS monotonic (only tighten). Trails are monotonic-tighten in neutral/risk_off,
        but in a CONFIRMED BULL the model may WIDEN/arm-later a trail so a strong winner can run.
        Returns (rules, forced_trigger). forced_trigger != None => exit this cycle."""
        from dataclasses import replace
        action = (decision or {}).get("action", "hold")
        if action in ("take_profit", "cut"):
            current_value = current_price * 100 * quantity
            pnl = (current_value - entry_debit) / entry_debit * 100 if entry_debit > 0 else 0.0
            reason = (decision.get("reason") or "").strip()
            return rules, ExitTrigger(
                con_id=con_id, trigger_type=("take_profit" if action == "take_profit" else "model_cut"),
                current_price=current_price, entry_debit=entry_debit, current_value=current_value,
                pnl_pct=pnl, message=(f"model {action}: {reason}" if reason else f"model {action}"))
        if action == "arm_trail":
            tc = rules.trailing
            bull = regime_mod.is_bull(regime)
            act, gb = decision.get("trail_activation_gain_pct"), decision.get("trail_giveback_fraction")
            new_act, new_gb = tc.activation_gain_pct, tc.giveback_fraction
            # bull (or not-yet-armed): accept the model's params so a winner can RUN (arm later / wider
            # giveback allowed). neutral/risk_off with an existing trail: monotonic-tighten only.
            free = bull or not tc.enabled
            if act is not None:
                new_act = float(act) if free else min(float(act), tc.activation_gain_pct)
            if gb is not None:
                gb = max(0.1, min(0.9, float(gb)))
                new_gb = gb if free else min(gb, tc.giveback_fraction)
            return replace(rules, trailing=replace(tc, enabled=True, activation_gain_pct=new_act, giveback_fraction=new_gb)), None
        if action == "tighten_stop":
            ns = decision.get("stop_pct")
            if ns is not None and float(ns) > 0:
                cur = rules.stop_pct  # tighter = smaller pct (smaller max loss); only reduce
                new_stop = min(float(ns), cur) if cur is not None else float(ns)
                return replace(rules, stop_pct=new_stop), None
        return rules, None

    async def run_cycle(self, dry_run: bool, regime=None, price_stats=None) -> None:
        """Run one evaluation cycle. regime/price_stats (from the Trader's market context) let the
        model manage by regime + per-underlying trend; both default None -> regime-neutral behavior."""
        self._regime = regime
        self._price_stats = price_stats
        cycle_start = datetime.utcnow()
        print(f"\n{'='*60}")
        print(f"[CYCLE] Starting evaluation cycle at {cycle_start.isoformat()}")
        print(f"[CYCLE] Dry run: {dry_run}")

        # Reload the journal: the trader appends new entries (incl. spreads) at runtime,
        # and scope=journal must see them without a restart
        self._load_journal()

        # Manual early-exits (2026-06-18): con_ids the user one-tapped "sell" on in the book review.
        # Force-closed below (before any mechanical stop) via the same close path -> single executor.
        manual_exit_ids = self._read_manual_exits()
        if manual_exit_ids:
            print(f"[CYCLE] manual early-exit requests: {sorted(manual_exit_ids)}")

        # Check kill switch first
        if self._check_kill_switch():
            print("[CYCLE] Kill switch active - skipping order placement this cycle")
            # Still do evaluation and logging but don't place orders

        # Check caps
        caps_ok, cap_reason = self._check_caps(dry_run)
        if not caps_ok:
            print(f"[CYCLE] Caps exceeded - {cap_reason}. Skipping order placement.")

        # Get live positions
        try:
            live_positions = await self.ib_conn.get_positions()
        except Exception as e:
            print(f"[ERROR] Could not fetch positions: {e}")
            return

        # Determine scope
        scope_con_ids = self._get_scope_con_ids(live_positions)
        print(f"[CYCLE] Managing {len(scope_con_ids)} positions (scope={self.config.scope.mode})")

        # Filter to only positions we manage and that aren't already being closed
        managed_positions = []
        for con_id in scope_con_ids:
            pos_data = live_positions.get(con_id)
            if pos_data is None:
                continue

            # Check if already in-flight (being closed)
            in_flight = self.state_manager.state.get_in_flight(con_id)
            if in_flight is not None and in_flight.order_id != 0:
                print(f"[CYCLE] con_id={con_id} already in-flight (order_id={in_flight.order_id}, remaining={in_flight.remaining_qty}), skipping")
                continue

            managed_positions.append(pos_data)

        if not managed_positions:
            print("[CYCLE] No positions to evaluate")
            self.state_manager.update_last_cycle()
            return
        # MANUAL EARLY-EXITS (force MARKET close, no quote needed): process here, BEFORE the
        # quote-gated eval loop, so an unpriceable option leg can still be sold on a one-tap request.
        if manual_exit_ids and not dry_run and not self._check_kill_switch():
            try:
                _loo_raw = await self.ib_conn.get_open_orders()
                _loo = {od.con_id: {"order_id": od.order_id, "remaining": od.remaining} for od in _loo_raw.values()}
            except Exception:
                _loo = {}
            import types as _t
            for _p in managed_positions:
                cid = _p.con_id
                if cid not in manual_exit_ids:
                    continue
                je = self._journal_entries.get(cid) or {}
                sym = je.get("symbol", _p.symbol)
                qty = min(_p.quantity, je.get("quantity", _p.quantity))
                edebit = je.get("debit", _p.avg_cost * 100 * _p.quantity)
                try:
                    res = await self.order_manager.place_close_order(
                        con_id=cid, symbol=sym, quantity=qty, limit_price=0.0, entry_debit=edebit,
                        live_open_orders=_loo, spread=je.get("spread"), market=True)
                    if res.success:
                        _loo[cid] = {"order_id": res.order_id, "remaining": qty}
                        self._post_exit_alert(sym, _t.SimpleNamespace(trigger_type="manual early-exit",
                            pnl_pct=0.0, message="early exit via book-review one-tap (market, ahead of stop)"))
                        # durable record (FIX #2): MARKET manual close -> fill price not known
                        # synchronously, so proceeds/realized P&L are null here (reason=manual).
                        self._log_exit(cid, sym, _t.SimpleNamespace(trigger_type="manual"),
                                       exit_price_per_share=None, quantity=qty, reason="manual")
                        print(f"[MANUAL-EXIT] {sym} con_id={cid} MARKET close placed")
                        self._clear_manual_exit(cid)   # clear ONLY on success
                    else:
                        self._manual_exit_fail(sym, cid, res.message)
                except Exception as _e:
                    self._manual_exit_fail(sym, cid, repr(_e))

        # Get quotes for managed positions (+ short legs of journaled spreads, for net pricing)
        con_ids_to_fetch = [p.con_id for p in managed_positions]
        for p in managed_positions:
            sp = (self._journal_entries.get(p.con_id) or {}).get("spread")
            if sp and sp.get("short_con_id"):
                con_ids_to_fetch.append(int(sp["short_con_id"]))
        try:
            quotes = await asyncio.wait_for(self.ib_conn.fetch_quotes(con_ids_to_fetch), timeout=40)
        except asyncio.TimeoutError:
            print("[ERROR] fetch_quotes timed out (40s) -- option quotes not streaming; skipping eval this cycle (manual one-tap exits still work)")
            self.state_manager.update_last_cycle(); return
        except Exception as e:
            print(f"[ERROR] Could not fetch quotes: {e}")
            return

        # Evaluate each position
        triggers: List[ExitTrigger] = []
        orders_placed_this_cycle = 0
        orders_notional_this_cycle = 0.0

        # Get live open orders for idempotency check
        try:
            live_open_orders_raw = await self.ib_conn.get_open_orders()
            live_open_orders = {
                od.con_id: {"order_id": od.order_id, "remaining": od.remaining}
                for od in live_open_orders_raw.values()
            }
        except Exception as e:
            print(f"[ERROR] Could not fetch open orders: {e}")
            live_open_orders = {}

        # Model-driven position management: one LLM call per cycle assesses every open
        # position and returns per-con_id decisions (arm/tighten trail, tighten stop,
        # take-profit, cut), applied below with monotonic guardrails. {} -> static rules.
        model_decisions = {}
        if getattr(self.config, "manage_positions", False):
            try:
                views = self._build_position_views(managed_positions, quotes, self._price_stats)
                model_decisions = assess_positions(self.config.llm_endpoint, self.config.llm_model,
                                                   views, market_regime=self._regime)
                if model_decisions:
                    rg = (self._regime or {}).get("regime", "n/a")
                    print(f"[POSMGMT] regime={rg} model decisions for con_ids {sorted(model_decisions)}")
            except Exception as e:
                print(f"[POSMGMT] assessment skipped ({e}); using static rules")
                model_decisions = {}

        for pos_data in managed_positions:
            con_id = pos_data.con_id
            quote = quotes.get(con_id)

            if quote is None:
                print(f"[WARN] No valid quote for con_id={con_id}, skipping")
                continue

            current_price = quote["price"]

            # Journaled spread: value the position as the NET (long - short) so the same
            # profit/stop/time rules apply to the spread as a unit. Never evaluate on the
            # long leg alone -- and never close it alone (see place_close_order spread path).
            spread = (self._journal_entries.get(con_id) or {}).get("spread")
            if spread and spread.get("short_con_id"):
                short_quote = quotes.get(int(spread["short_con_id"]))
                if short_quote is None:
                    print(f"[WARN] No valid quote for spread short leg of con_id={con_id}, skipping")
                    continue
                current_price = current_price - short_quote["price"]

            # Get entry debit from journal (or estimate from avg_cost if not in journal)
            if con_id in self._journal_entries:
                entry_debit = self._journal_entries[con_id].get("debit", 0.0)
                symbol = self._journal_entries[con_id].get("symbol", pos_data.symbol)
                quantity_in_journal = self._journal_entries[con_id].get("quantity", pos_data.quantity)
                # Use minimum of position qty and journal qty (in case of partial close)
                quantity = min(pos_data.quantity, quantity_in_journal)
            else:
                # Estimate from avg_cost (avg_cost is per share)
                entry_debit = pos_data.avg_cost * 100 * pos_data.quantity
                symbol = pos_data.symbol
                quantity = pos_data.quantity

            # Update peak price tracking (PERSISTED in state -> survives restarts)
            peaks = self.state_manager.state.peak_prices
            k = str(con_id)
            if k not in peaks or current_price > peaks[k]:
                peaks[k] = current_price

            # Real days-to-expiry from the position's option contract
            dte = days_to_expiry(getattr(pos_data, "expiry", ""))
            # Per-position sell levels the model recommended (journaled); fall back to the global
            # config rule when a level wasn't specified for this trade.
            rules = self.config.rules
            je = self._journal_entries.get(con_id) or {}
            if je.get("profit_target_pct") or je.get("stop_pct"):
                from dataclasses import replace
                rules = replace(self.config.rules,
                                profit_target_pct=je.get("profit_target_pct") or self.config.rules.profit_target_pct,
                                stop_pct=je.get("stop_pct") or self.config.rules.stop_pct)
            # Apply the model's per-position decision (monotonic guardrails). A take_profit/cut
            # forces an immediate exit this cycle; otherwise the (possibly tuned) rules drive eval.
            forced = None
            decision = model_decisions.get(con_id)
            if decision and decision.get("action", "hold") != "hold":
                rules, forced = self._apply_decision(
                    rules, decision, current_price, entry_debit, quantity, con_id, symbol,
                    regime=self._regime)
            if forced is not None:
                trigger = forced
            else:
                trigger = evaluate_position(
                    con_id=con_id,
                    symbol=symbol,
                    quantity=quantity,
                    entry_debit=entry_debit,
                    current_price=current_price,
                    days_to_expiry=dte,
                    peak_price=peaks.get(k),
                    rules=rules,
                )

            if trigger:
                trigger.con_id = con_id
                triggers.append(trigger)
                print(f"[EVAL] con_id={con_id} ({symbol}): {trigger.message} (pnl={trigger.pnl_pct:.2f}%)")

                # Place order if armed and within caps and kill switch not active
                if not dry_run and not self._check_kill_switch() and caps_ok:
                    # Check per-cycle order cap
                    if orders_placed_this_cycle >= self.config.caps.max_orders_per_cycle:
                        print(f"[CYCLE] Per-cycle order cap reached: {orders_placed_this_cycle} >= {self.config.caps.max_orders_per_cycle}")
                        break

                    # Check per-cycle notional cap (approximate)
                    order_notional = current_price * 100 * quantity
                    if orders_notional_this_cycle + order_notional > self.config.caps.max_notional_per_day:
                        print(f"[CYCLE] Would exceed daily notional cap, skipping order for con_id={con_id}")
                        continue

                    # Place order
                    result = await self.order_manager.place_close_order(
                        con_id=con_id,
                        symbol=symbol,
                        quantity=quantity,
                        limit_price=current_price,  # Limit at mid (net mid for spreads)
                        entry_debit=entry_debit,
                        live_open_orders=live_open_orders,
                        spread=spread,
                        market=getattr(self.config.rules, "exit_market_orders", False),
                    )

                    if result.success:
                        orders_placed_this_cycle += 1
                        orders_notional_this_cycle += order_notional
                        # Update live_open_orders for next iteration
                        live_open_orders[con_id] = {"order_id": result.order_id, "remaining": quantity}
                        self._post_exit_alert(symbol, trigger)
                        # durable realized-exit record (FIX #2). current_price is the per-share
                        # NET (long-minus-short for spreads) used to value the exit, so proceeds =
                        # current_price*100*qty and realized P&L = proceeds - entry_debit.
                        self._log_exit(con_id, symbol, trigger,
                                       exit_price_per_share=current_price, quantity=quantity,
                                       reason=self._exit_reason(trigger))
            else:
                # Log evaluation (no trigger)
                pnl_pct = (current_price * 100 * quantity - entry_debit) / entry_debit * 100 if entry_debit > 0 else 0
                print(f"[EVAL] con_id={con_id} ({symbol}): no trigger (price={current_price:.4f}, pnl={pnl_pct:.2f}%)")

        print(f"[CYCLE] Evaluation complete. Triggers: {len(triggers)}, Orders placed: {orders_placed_this_cycle}")
        print(f"[CYCLE] Cycle finished at {datetime.utcnow().isoformat()}")
        print(f"{'='*60}\n")

        # Update last cycle timestamp
        self.state_manager.update_last_cycle()

    async def run(self) -> None:
        """Run the exit manager (one-shot or loop)."""
        # Connect to IB
        connected = await self.ib_conn.connect()
        if not connected:
            print("[ERROR] Could not connect to IB - exiting")
            return

        # Reconcile on startup
        if not await self._reconcile_on_startup():
            print("[ERROR] Reconciliation failed - exiting for safety")
            await self.ib_conn.disconnect()
            sys.exit(1)

        # Handle shutdown signals
        def request_shutdown(signum, frame):
            print(f"\n[SHUTDOWN] Received signal {signum}, initiating graceful shutdown...")
            self._shutdown_requested = True
            self._running = False

        signal.signal(signal.SIGINT, request_shutdown)
        signal.signal(signal.SIGTERM, request_shutdown)

        dry_run = self.config.dry_run
        if dry_run:
            print("[INFO] Running in DRY RUN mode - no orders will be placed")
        else:
            print("[WARN] Running in LIVE mode - orders WILL be placed!")

        if self.config.loop_mode:
            print(f"[INFO] Running in LOOP mode with interval={self.config.loop.interval_seconds}s")
            self._running = True
            while self._running and not self._shutdown_requested:
                await self.run_cycle(dry_run)

                if not self._shutdown_requested:
                    await asyncio.sleep(self.config.loop.interval_seconds)
        else:
            await self.run_cycle(dry_run)

        # Cleanup
        await self.ib_conn.disconnect()
        print("[INFO] Exit manager stopped")
