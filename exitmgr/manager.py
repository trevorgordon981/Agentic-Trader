"""Main manager orchestrating the exit management loop."""

import asyncio
import os
import json
import signal
import sys
import uuid
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Optional, Dict, List, Set

from exitmgr.config import Config
from exitmgr.connection import IBConnection, PositionData
# H1 (2026-07-03): import the canonical ET trading-day key so _check_caps READS daily_stats
# under the SAME America/New_York key that order.place_close_order WRITES it (order._trading_day).
# Safe against import cycles: order.py imports only ibkr/connection/state -- never manager -- so
# this backward edge cannot close a loop. Replicated in order.py/trader.py; do NOT add a 3rd copy.
from exitmgr.order import OrderManager, _trading_day, commission_from_trade, compute_entry_basis
from exitmgr.rules import evaluate_position, ExitTrigger, days_to_expiry
from exitmgr.position_manager import assess_positions
from exitmgr import regime as regime_mod
from exitmgr.state import StateManager
from exitmgr import trade_capture, dataset_integrity


# AIRTIGHT-STOP BACKSTOP magnitude (percent, positive). Last-resort protective stop applied
# to an open position ONLY when neither the journal nor the config supplies one, so no trade
# ever runs unstopped. Matches the constructor-default stop (|ConstructionConfig.sl_pct|*100 =
# 30%). Never loosens an existing stop; the pot-tiered TP ceiling never touches it.
_STOP_BACKSTOP_PCT = 30.0


class ExitManager:
    """Main orchestrator for exit management."""

    # Cap on the per-position mark-to-market path length persisted in state (record-only).
    # ~1 RTH day of 60s marks is ~390 points; this bounds a multi-day hold so state.json
    # can't grow unbounded on an illiquid position that never fills its exit.
    MARK_PATH_CAP = 5000

    def __init__(self, config: Config):
        self.config = config
        self.ib_conn = IBConnection(
            host=config.ib.host,
            port=config.ib.port,
            client_id=config.ib.client_id,
        )
        self.state_manager = StateManager(config.state.path)
        self.order_manager = OrderManager(
            self.ib_conn, self.state_manager,
            # WIRED 2026-07-03: pass the config-driven exit slippage floor through so a
            # config.yaml value flows to production. Defaults to 0.50 when unset (byte-identical).
            exit_slippage_floor=getattr(self.config.rules, "exit_slippage_floor", 0.50),
        )

        self._running = False
        self._shutdown_requested = False
        # PER-CYCLE RECONCILE GATE (2026-07-03): result of the most recent reconcile. Order
        # placement (exits) is gated on it each cycle, and the Trader reads it to suppress ENTRIES
        # when broker/journal state is inconsistent. Defaults True (open) until the first reconcile.
        self._reconcile_ok = True
        # C1a (2026-07-09): the SPECIFIC con_ids the last reconcile found inconsistent. Exits are
        # blocked ONLY for these (per-con_id), so one manual TWS position no longer withholds every
        # automated stop. An empty set = all clean; None = reconcile could not run at all (fetch
        # failure) -> block ALL exits that cycle (fail-safe).
        self._reconcile_bad_con_ids: Optional[Set[int]] = set()

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
        self._spread_short_legs = {}   # short_con_id -> parent long con_id (known spread legs)
        # TERMINAL-STATE (2026-07-03): collect close-tool markers + the ENTRY arc they drop, so a
        # tool-closed trade emits ONE full closed-trade dataset row instead of vanishing (the
        # journal-drop below used to orphan the whole entry+mark-path arc). markers keyed by BOTH
        # legs (net-fill for spreads); arcs keyed by the parent LONG con_id (dedup across legs).
        _tool_markers: Dict[int, dict] = {}
        _tool_close_arcs: Dict[int, dict] = {}
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
                            if entry.get("event") == "closed_by_tool":
                                # LAYER 1b: a close path (close_symbol/liquidate) closed this
                                # contract -> drop it (and any spread it anchored) so the trader
                                # does not re-close an already-closed position.
                                _cid = int(con_id)
                                _tool_markers[_cid] = entry
                                # Capture the ENTRY arc BEFORE it is dropped: the marker may land
                                # on the LONG leg (a journal key) OR the SHORT leg (resolve to its
                                # parent long). Dedup by parent con_id -> at most one arc/spread.
                                _arc_je = self._journal_entries.get(_cid)
                                _arc_cid = _cid
                                if _arc_je is None:
                                    _par = self._spread_short_legs.get(_cid)
                                    if _par is not None:
                                        _arc_cid = int(_par)
                                        _arc_je = self._journal_entries.get(_arc_cid)
                                if _arc_je is not None:
                                    _tool_close_arcs.setdefault(_arc_cid, _arc_je)
                                self._journal_entries.pop(_cid, None)
                                _parent = self._spread_short_legs.pop(_cid, None)
                                if _parent is not None:
                                    self._journal_entries.pop(int(_parent), None)
                                for _scid, _p in list(self._spread_short_legs.items()):
                                    if _p == _cid:
                                        self._spread_short_legs.pop(_scid, None)
                                continue
                            self._journal_entries[int(con_id)] = entry
                            # A spread's short leg is a KNOWN position too. Track it so an
                            # over-covered short residual (6/29 double-close) is not flagged
                            # "unexpected" by reconciliation and does not blind the manager.
                            _sp = entry.get("spread") or {}
                            if _sp.get("short_con_id") is not None:
                                self._spread_short_legs[int(_sp["short_con_id"])] = int(con_id)
                    except json.JSONDecodeError as e:
                        print(f"[WARN] Could not parse journal line: {e}")
        except Exception as e:
            print(f"[ERROR] Could not load journal: {e}")

        print(f"[INFO] Loaded {len(self._journal_entries)} journal entries")

        # TERMINAL-STATE (2026-07-03): emit ONE full closed-trade row per tool-closed arc (the
        # journal-drop above no longer orphans it). Deduped by con_id across cycles + a restart
        # (via the on-disk dataset). Fully wrapped -- can never raise into load/reconcile.
        if _tool_close_arcs:
            try:
                for _acid, _aje in _tool_close_arcs.items():
                    self._emit_tool_close(_acid, _aje, _tool_markers)
            except Exception as e:
                print(f"[WARN] tool-close terminal logging failed: {e} (continuing)")

    def _post_exit_alert(self, symbol: str, trigger, client_msg_id: Optional[str] = None) -> bool:
        """Real-time ping to #trading-alerts the moment a position is sold by the exit manager."""
        import os
        channel = getattr(self.config, "alerts_channel", "") or ""
        token = os.environ.get("SLACK_BOT_TOKEN", "")
        if not (channel and token):
            return True
        names = {"profit_target": "take-profit :dart:", "stop": "STOP :octagonal_sign:",
                 "time_stop": "time stop :hourglass:", "trailing_stop": "trailing stop"}
        why = names.get(getattr(trigger, "trigger_type", ""), getattr(trigger, "trigger_type", "exit"))
        pnl = getattr(trigger, "pnl_pct", 0.0)
        emoji = ":green_circle:" if pnl >= 0 else ":red_circle:"
        try:
            from exitmgr import approval
            ts = approval.post_proposal(token, channel,
                f":rotating_light: *EXIT — {symbol}* sold ({why}) {emoji} *P&L {pnl:+.1f}%*\n"
                f"_{getattr(trigger, 'message', '')}_", client_msg_id=client_msg_id)
            return bool(ts)
        except Exception as e:
            print(f"[WARN] exit alert post failed: {e}")
            return False

    def _exits_log_path(self) -> str:
        """Durable, append-only exits log (JSONL) next to the journal. Realized P&L was
        previously NOWHERE on disk (only inside IBKR) -- this is the on-disk record that
        conviction_calibration.py reads via --fills (keyed contract_id -> realized_pnl)."""
        cfg_path = getattr(getattr(self.config, "exits", None), "path", None)
        if cfg_path:
            return cfg_path
        return os.path.join(os.path.dirname(self.config.journal.path) or ".", "exits.log")

    def _dataset_path(self) -> str:
        """Durable, append-only, fine-tuning-ready per-trade dataset (JSONL). ONE object per
        CLOSED trade carrying the full entry snapshot + mark path + MFE/MAE + close + labels.
        Purpose-built for training/retro-ing exits; SEPARATE from the human-readable
        trades.log/exits.log. Lives in a `data/` dir next to the journal by default (override
        via config.dataset.path)."""
        # TEST/OVERRIDE ISOLATION (2026-07-03 fold-in): honor EXITMGR_DATASET_DIR so the
        # EXIT-MANAGER's OWN dataset writes (_log_trade_dataset, the terminal-close rows below,
        # and the ddir passed to trade_capture in _log_unfilled_exit) land in the SAME tmp dir the
        # autouse conftest fixture points every capture at -- previously this path resolved data/
        # independently of trade_capture.dataset_dir(), so the override did NOT reach manager
        # writes and a test using the real config journal could still touch production data/.
        # PRODUCTION UNCHANGED: the env var is set only by the test suite; unset -> byte-identical
        # data/-next-to-journal resolution below.
        env_dir = os.environ.get("EXITMGR_DATASET_DIR")
        if env_dir:
            try:
                os.makedirs(env_dir, exist_ok=True)
            except Exception:
                pass
            return os.path.join(env_dir, "trade_dataset.jsonl")
        cfg_path = getattr(getattr(self.config, "dataset", None), "path", None)
        if cfg_path:
            return cfg_path
        base = os.path.dirname(self.config.journal.path) or "."
        d = os.path.join(base, "data")
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            return os.path.join(base, "trade_dataset.jsonl")
        return os.path.join(d, "trade_dataset.jsonl")

    def _dataset_dir(self) -> str:
        """Directory that holds trade_dataset.jsonl + the decision_context/reviews sidecars.
        Honors EXITMGR_DATASET_DIR (via _dataset_path) so exit-path writes are test-isolatable,
        matching trade_capture.dataset_dir()."""
        try:
            return os.path.dirname(self._dataset_path()) or "."
        except Exception:
            return "."

    def _log_trade_dataset(self, exit_rec: dict, je: dict, con_id: int) -> bool:
        """Append ONE complete, fine-tuning-ready record for a closed trade to trade_dataset.jsonl.
        ADDITIVE + RECORD-ONLY: assembled from the already-computed exit_rec, the entry journal
        entry (je), and the persisted mark path / MFE / MAE. Double-wrapped so it can NEVER raise
        into the exit path. Must be called AFTER the exits.log write so a dataset issue can never
        affect the calibration-critical exits.log."""
        try:
            st = self.state_manager.state
            k = str(con_id)
            mark_path = list(st.mark_path.get(k, []))
            mfe = st.mfe_pct.get(k)
            mae = st.mae_pct.get(k)
            realized_pct = exit_rec.get("realized_pnl_pct")
            # drawdown-from-peak at close = how much of the peak favorable excursion was given back
            dd_from_peak = round(mfe - realized_pct, 2) if (mfe is not None and realized_pct is not None) else None
            # outcome labels (scratch band = +/-1% of debit)
            outcome = None
            if realized_pct is not None:
                outcome = "win" if realized_pct > 1.0 else ("loss" if realized_pct < -1.0 else "scratch")
            # round-trip: ran to a real gain (MFE>=15%) but closed flat/negative
            round_trip = bool(mfe is not None and mfe >= 15.0
                              and realized_pct is not None and realized_pct <= 0)
            # TP / SL level-hit booleans (journal stores stop_pct as a positive magnitude)
            tp_pct = je.get("profit_target_pct")
            sl_pct = je.get("stop_pct")
            tp_hit = None
            sl_hit = None
            if realized_pct is not None:
                try:
                    tp_hit = (tp_pct is not None and realized_pct >= float(tp_pct))
                except (TypeError, ValueError):
                    tp_hit = None
                try:
                    sl_hit = (sl_pct is not None and realized_pct <= -abs(float(sl_pct)))
                except (TypeError, ValueError):
                    sl_hit = None
            sp = je.get("spread") or {}
            # ---------- FILL QUALITY / SLIPPAGE (v2) ----------
            # slippage = realized fill vs the NET mark that TRIGGERED the exit (favorable when the
            # fill beat the mark). Only when we saw a real fill AND the trigger mark was recorded.
            trigger_mark = exit_rec.get("trigger_mark")
            fill_px = exit_rec.get("avg_fill_price")
            slippage = None
            slippage_pct = None
            try:
                if fill_px is not None and trigger_mark is not None:
                    slippage = round(float(fill_px) - float(trigger_mark), 4)
                    if float(trigger_mark) != 0:
                        slippage_pct = round(slippage / abs(float(trigger_mark)) * 100, 2)
            except (TypeError, ValueError):
                slippage = slippage_pct = None
            # ---------- DECISION CONTEXT (v2, joined from the decision sidecar) ----------
            # The full DECISION -> ENTRY arc: raw strategist reasoning, all candidates + convictions,
            # the chosen idea, the risk GateDecision + bound caps, the construction adjustments,
            # regime + the RAG/news/journal brief that fed the idea. The immutable decision_id is the
            # ONLY causal join; legacy rows without it remain honestly unjoined.
            decision = None
            try:
                decision = trade_capture.load_decision_context(
                    self._dataset_dir(), decision_id=je.get("decision_id"),
                    con_id=con_id, symbol=je.get("symbol"),
                    strike=je.get("strike"), expiry=je.get("expiry"), right=je.get("right"))
            except Exception as _de:
                print(f"[WARN] decision-context join failed for con_id={con_id}: {_de}")
            # ---------- REVIEW (v2, best-effort) ----------
            # A post-trade review/coach verdict IF one was persisted to reviews.jsonl (keyed by
            # symbol/con_id/date). morning_review currently posts to Slack only; when a sidecar
            # producer is wired this attaches automatically. None otherwise.
            review = None
            try:
                review = trade_capture.load_review(
                    self._dataset_dir(), decision_id=je.get("decision_id"),
                    con_id=con_id, symbol=je.get("symbol"),
                    date=(str(je.get("ts") or "")[:10] or None))
            except Exception as _re:
                print(f"[WARN] review join failed for con_id={con_id}: {_re}")
            rec = {
                "schema": "trade_dataset.v2",
                "kind": "trade",                     # trade | no_trade | rejected (v2 row taxonomy)
                "decision_id": je.get("decision_id"),
                "model_identity": je.get("model_identity"),
                "con_id": con_id,
                "symbol": exit_rec.get("symbol"),
                # ---------- DECISION -> the reasoning that produced the trade ----------
                "decision": decision,
                # ---------- ENTRY SNAPSHOT (+ full greeks / IV / liquidity, v2) ----------
                "entry": {
                    "ts": je.get("ts"),
                    "decision_id": je.get("decision_id"),
                    "model_identity": je.get("model_identity"),
                    "symbol": je.get("symbol"),
                    "right": je.get("right"),
                    "strike": je.get("strike"),
                    "expiry": je.get("expiry"),
                    "structure": exit_rec.get("structure"),
                    "spread": ({"short_con_id": sp.get("short_con_id"),
                                "short_strike": sp.get("short_strike"),
                                "width": sp.get("width")} if sp else None),
                    "quantity": exit_rec.get("quantity"),
                    "debit": exit_rec.get("entry_debit"),
                    "dte_at_entry": je.get("dte_at_entry"),
                    "dte_adjusted": je.get("dte_adjusted"),
                    "profit_target_pct": tp_pct,
                    "stop_pct": sl_pct,
                    "conviction": exit_rec.get("conviction"),
                    "thesis": je.get("thesis"),
                    # greeks per long leg + IV/IVR (v2). net greeks captured when journaled.
                    "entry_delta": je.get("entry_delta"),
                    "entry_gamma": je.get("entry_gamma"),
                    "entry_theta": je.get("entry_theta"),
                    "entry_vega": je.get("entry_vega"),
                    "entry_iv": je.get("entry_iv"),
                    "entry_ivr": je.get("entry_ivr"),
                    "net_delta": je.get("net_delta"),
                    "net_theta": je.get("net_theta"),
                    "net_gamma": je.get("net_gamma"),
                    "net_vega": je.get("net_vega"),
                    # bid/ask + liquidity measure at entry (v2)
                    "entry_bid": je.get("entry_bid"),
                    "entry_ask": je.get("entry_ask"),
                    "entry_spread_pct": je.get("entry_spread_pct"),
                    "entry_liquidity": je.get("entry_liquidity"),
                    "underlying_price_at_entry": je.get("underlying_price_at_entry"),
                    "entry_order_status": je.get("order_status"),
                    "entry_avg_fill_price": je.get("avg_fill_price"),
                    "entry_fill_ts": je.get("fill_ts"),
                    # COMMISSIONS + REAL BASIS (2026-07-03): actual entry fee + the fill-based cost
                    # basis and entry slippage vs the mid estimate (all None on pre-2026-07-03 rows).
                    "entry_commission": je.get("entry_commission"),
                    "entry_fill_debit": je.get("entry_fill_debit"),
                    "entry_slippage": je.get("entry_slippage"),
                    "entry_slippage_pct": je.get("entry_slippage_pct"),
                    "basis_source": je.get("basis_source"),
                },
                # ---------- LIFECYCLE (mark-to-market path + excursions) ----------
                # mark_path entries now carry v2 per-cycle enrichment: iv/greeks, dte, days_held,
                # dist_to_tp_pct, dist_to_sl_pct, and the position-manager assessment that cycle
                # (mgmt_action + mgmt_reason). Older marks may lack these (backward compatible).
                "lifecycle": {
                    "mark_path": mark_path,
                    "marks": len(mark_path),
                    "mfe_pct": mfe,                  # peak favorable excursion %
                    "mfe_ts": st.mfe_ts.get(k),      # when the peak occurred
                    "mae_pct": mae,                  # max ADVERSE excursion % (never captured before)
                    "mae_ts": st.mae_ts.get(k),      # when the trough occurred
                    "peak_price": st.peak_prices.get(k),
                    "drawdown_from_peak_pct": dd_from_peak,
                },
                # ---------- CLOSE (+ fill quality / slippage / exit greeks / rule, v2) ----------
                "close": {
                    "ts": exit_rec.get("close_ts"),
                    # Durable idempotency key for asynchronous/restart-time finalization.  The
                    # exits.log row is canonical; this lets a restart repair a missing dataset
                    # mirror without appending a second closed-trade row.
                    "order_id": exit_rec.get("order_id"),
                    "perm_id": exit_rec.get("perm_id"),
                    "client_id": exit_rec.get("client_id"),
                    "order_ref": exit_rec.get("order_ref"),
                    "close_identity": exit_rec.get("close_identity"),
                    "reason": exit_rec.get("reason"),
                    "rule_fired": exit_rec.get("rule_fired"),          # the raw trigger_type that fired
                    "exit_reasoning": exit_rec.get("exit_reasoning"),  # trigger message / model-cut text
                    "exit_model_identity": exit_rec.get("exit_model_identity"),
                    "exit_price_per_share": exit_rec.get("exit_price_per_share"),
                    "proceeds": exit_rec.get("proceeds"),
                    "realized_pnl": exit_rec.get("realized_pnl"),
                    # NET of round-trip IBKR fees (2026-07-03): gross realized - entry_commission
                    # - exit_commission; None (with commission_unknown set) if either fee unknown.
                    "realized_pnl_net": exit_rec.get("realized_pnl_net"),
                    "entry_commission": exit_rec.get("entry_commission"),   # allocated to closed qty
                    "exit_commission": exit_rec.get("exit_commission"),
                    "commission_unknown": exit_rec.get("commission_unknown"),
                    "realized_pnl_pct": realized_pct,
                    # H3 (2026-07-03): mark valuation for a NON-filled exit, kept distinct from
                    # realized_pnl_pct (which is None on a non-fill) so it is never read as a
                    # realized outcome. None on a genuine fill (nothing was nulled).
                    "mark_estimate_pnl_pct": exit_rec.get("mark_estimate_pnl_pct"),
                    "holding_days": exit_rec.get("holding_days"),
                    "fill_status": exit_rec.get("fill_status"),
                    "avg_fill_price": exit_rec.get("avg_fill_price"),
                    "trigger_mark": trigger_mark,        # NET mark that fired the exit
                    "slippage_per_share": slippage,      # fill - trigger mark (fill quality)
                    "slippage_pct": slippage_pct,
                    "underlying_price_at_exit": exit_rec.get("underlying_price"),
                    "exit_iv": exit_rec.get("iv"),
                    "exit_delta": exit_rec.get("delta"),
                    "exit_gamma": exit_rec.get("gamma"),
                    "exit_theta": exit_rec.get("theta"),
                    "exit_vega": exit_rec.get("vega"),
                    "tp_hit": tp_hit,
                    "sl_hit": sl_hit,
                    # SCALE-OUT (2026-07-02): a partial trim is NOT a terminal close -- a runner
                    # remains open. `partial` True + `close_qty`/`remaining_qty` let a dataset
                    # consumer treat this row as a partial realization, not a full round-trip.
                    # Full closes emit partial=False (default) -> unchanged v1 shape/meaning.
                    "partial": bool(exit_rec.get("partial", False)),
                    "close_qty": exit_rec.get("close_qty"),
                    "remaining_qty": exit_rec.get("remaining_qty"),
                    # TERMINAL-STATE (2026-07-03): additive fields for tool-close / expiry endings.
                    # None on a normal placed exit (backward compatible). `exit_event` tags the
                    # terminal kind (closed_by_tool | liquidated | expired); `expiry_value_unknown`
                    # + `realized_unknown_reason` explain a null realized (no fabricated P&L);
                    # `close_client_id` is the clientId of the close tool.
                    "exit_event": exit_rec.get("exit_event"),
                    "expiry_value_unknown": exit_rec.get("expiry_value_unknown"),
                    "realized_unknown_reason": exit_rec.get("realized_unknown_reason"),
                    "close_client_id": exit_rec.get("close_client_id"),
                    "dte_at_close": exit_rec.get("dte_at_close"),   # 0 on an expiry row
                },
                # ---------- OUTCOME LABELS ----------
                "labels": {
                    "outcome": outcome,              # win / loss / scratch
                    "win": (outcome == "win") if outcome is not None else None,
                    "round_trip": round_trip,        # ran to a gain then closed flat/negative
                },
                # ---------- REVIEW (post-trade coach verdict, if persisted) ----------
                "review": review,
            }
            _pnl_canonical = (rec["close"].get("realized_pnl_net") is not None
                              and not bool(rec["close"].get("commission_unknown")))
            _training_canonical = _pnl_canonical and decision is not None
            _reason = ("missing immutable decision join" if decision is None else
                       "net realized P&L unavailable")
            dataset_integrity.mark(
                rec, status=dataset_integrity.CANONICAL,
                training=_training_canonical, pnl=_pnl_canonical, reason=_reason)
            with open(self._dataset_path(), "a") as f:
                f.write(json.dumps(rec, default=str) + "\n")
                f.flush()
                os.fsync(f.fileno())
            print(f"[TRADE-DS] {exit_rec.get('symbol')} con_id={con_id} "
                  f"mfe={mfe} mae={mae} outcome={outcome} round_trip={round_trip} marks={len(mark_path)}")
            return True
        except Exception as e:
            print(f"[WARN] trade_dataset write failed for con_id={con_id}: {e}")
            return False

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

    async def _spot_price(self, symbol: str) -> Optional[float]:
        """Best-effort underlying spot for exit-record enrichment (2026-07-01). Only called
        on an actual exit (rare) so the extra qualify+ticker round-trip is cheap. Never raises."""
        try:
            from exitmgr.ibkr import Stock, underlying_price
            q = await self.ib_conn.ib.qualifyContractsAsync(Stock(symbol, "SMART", "USD"))
            if not q:
                return None
            return await underlying_price(self.ib_conn.ib, q[0])
        except Exception as e:
            print(f"[WARN] spot fetch for {symbol} failed (exit record will omit it): {e}")
            return None

    def _fills_log_path(self) -> str:
        """Location of daily_recommend's entry-fill sidecar (fills.log), resolved next to the
        journal. LATE entry fills (an order journaled BEFORE it filled) land here and were joined
        by NOTHING until 2026-07-03; _join_entry_fill reads it to backfill avg_fill_price +
        entry_commission at close time."""
        return os.path.join(os.path.dirname(self.config.journal.path) or ".", "fills.log")

    def _join_entry_fill(self, con_id, je) -> dict:
        """Backfill a LATE entry fill from fills.log. An entry that journaled before its fill has
        avg_fill_price=None (and no entry_commission) in trades.log; the real fill later landed in
        fills.log (event=entry_fill). Return ONLY the fields the journal is missing
        (avg_fill_price / entry_commission and, when the basis was never computed, a recomputed
        entry_fill_debit / entry_slippage / entry_slippage_pct / basis_source). Prefers the
        journal value when present; never overwrites a known value; never fabricates. NEVER raises."""
        try:
            afp = je.get("avg_fill_price")
            comm = je.get("entry_commission")
            if afp is not None and comm is not None:
                return {}
            path = self._fills_log_path()
            if not os.path.exists(path):
                return {}
            latest = None
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if d.get("event") != "entry_fill" or d.get("contract_id") != con_id:
                        continue
                    latest = d  # last matching fill wins
            if not latest:
                return {}
            out = {}
            j_afp = afp if afp is not None else latest.get("avg_fill_price")
            j_comm = comm if comm is not None else latest.get("entry_commission")
            if afp is None and j_afp is not None:
                out["avg_fill_price"] = j_afp
            if comm is None and j_comm is not None:
                out["entry_commission"] = j_comm
            if je.get("entry_fill_debit") is None and j_afp is not None:
                efd, eslip, eslip_pct = compute_entry_basis(je.get("debit"), j_afp, je.get("quantity"))
                out["entry_fill_debit"] = efd
                out["entry_slippage"] = eslip
                out["entry_slippage_pct"] = eslip_pct
                out["basis_source"] = ("fill" if efd is not None else je.get("basis_source"))
            return out
        except Exception as e:
            print(f"[WARN] fills.log entry-join failed for con_id={con_id}: {e}")
            return {}

    def _existing_exit_for_order(self, order_id, close_identity=None, con_id=None) -> Optional[dict]:
        """Return the canonical realized exit for the composite broker identity, if appended.

        A fill finalizer can be replayed after any crash boundary.  Checking the durable JSONL
        before appending makes that replay idempotent; malformed/truncated lines are ignored.
        ``order_id`` alone is accepted only for legacy records that predate client/perm identity.
        """
        if not close_identity and order_id in (None, 0, "0"):
            return None
        try:
            with open(self._exits_log_path()) as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    identity_match = (bool(close_identity)
                                      and rec.get("close_identity") == close_identity)
                    legacy_match = (not rec.get("close_identity")
                                    and str(rec.get("order_id")) == str(order_id)
                                    and (con_id is None
                                         or str(rec.get("contract_id")) == str(con_id)))
                    if (identity_match or legacy_match) and rec.get("fill_status") == "Filled":
                        return rec
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[WARN] exit dedupe scan failed for order_id={order_id}: {e}")
        return None

    def _dataset_has_exit_order(self, order_id, close_identity=None, con_id=None) -> bool:
        if not close_identity and order_id in (None, 0, "0"):
            return False
        try:
            with open(self._dataset_path()) as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    close = rec.get("close") or {}
                    identity_match = (bool(close_identity)
                                      and close.get("close_identity") == close_identity)
                    legacy_match = (not close.get("close_identity")
                                    and str(close.get("order_id")) == str(order_id)
                                    and (con_id is None
                                         or str(rec.get("con_id", rec.get("contract_id")))
                                         == str(con_id)))
                    if (rec.get("kind") == "trade" and (identity_match or legacy_match)
                            and close.get("fill_status") == "Filled"):
                        return True
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[WARN] dataset dedupe scan failed for order_id={order_id}: {e}")
        return False

    def _log_exit(self, con_id: int, symbol: str, trigger, exit_price_per_share: Optional[float],
                  quantity: int, reason: str, extra: Optional[dict] = None,
                  entry_debit: Optional[float] = None, je: Optional[dict] = None) -> bool:
        """Append-only record of a realized exit to exits.log (JSONL). ADDITIVE -- never changes
        exit behavior, only records the outcome. Schema is consumable by conviction_calibration.py
        --fills (contract_id -> realized_pnl + conviction). Must NOT raise into the exit path.

        `entry_debit` (2026-07-02, scale-out): the cost basis for the contracts CLOSED by this
        record. When None (all pre-scale-out callers) it falls back to the FULL journaled debit --
        exactly the v1 behavior for a full close of a full position. A scale-out / runner close
        passes the PRO-RATED basis for `quantity` closed so realized P&L is correct on a partial."""
        try:
            # TERMINAL-STATE (2026-07-03): callers may pass the entry `je` EXPLICITLY. A tool-close
            # emit runs AFTER the journal-drop has already popped the entry from
            # self._journal_entries, so re-fetching here would yield {} and blank the entry
            # snapshot. Prefer the passed je; fall back to the live lookup for all normal callers.
            if je is None:
                je = self._journal_entries.get(con_id) or {}
            # LATE-FILL JOIN + isolation: work on a COPY so the backfill never mutates the
            # in-memory journal entry, and merge any fills.log entry-fill (avg_fill_price +
            # entry_commission) that landed AFTER this entry journaled (nothing joined it before).
            je = dict(je or {})
            je.update(self._join_entry_fill(con_id, je))
            # The append + state cleanup is replayable across process death.  If exits.log already
            # has this Filled order, do not append it again; repair the dataset mirror if a crash
            # happened between the two durable appends.
            _order_id = (extra or {}).get("order_id")
            _close_identity = (extra or {}).get("close_identity")
            _existing = self._existing_exit_for_order(
                _order_id, close_identity=_close_identity, con_id=con_id)
            if _existing is not None:
                if self._dataset_has_exit_order(
                        _order_id, close_identity=_close_identity, con_id=con_id):
                    return True
                return self._log_trade_dataset(_existing, je, con_id)
            if entry_debit is None:
                # C2b (2026-07-09): when the caller passes no explicit (scale-out pro-rated) basis,
                # prefer the REAL entry fill debit (basis_source=="fill" or entry_fill_debit present)
                # over the estimated debit so realized P&L anchors to what was actually paid. The
                # managed-exit path already passes a fill-based pro-rated basis, so this branch only
                # covers manual/expiry/tool full-close callers.
                _efd = je.get("entry_fill_debit")
                _bs = je.get("basis_source")
                entry_debit = _efd if (_efd is not None and (_bs == "fill" or _efd is not None)) else je.get("debit")
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
                "decision_id": je.get("decision_id"),
                "model_identity": je.get("model_identity"),
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
            if extra:  # ADDITIVE enrichment (2026-07-01): fill status/price, spot, IV/delta, MFE
                rec.update({k: v for k, v in extra.items() if k not in rec or rec[k] is None})
            # H3 FIX (2026-07-03): a close row may carry REALIZED P&L only if the exit actually
            # FILLED. The fill-verification path calls _log_exit for a still-resting exit with
            # exit_price_per_share=current_price (the MARK) and fill_status in extra -- so the
            # realized_pnl / realized_pnl_pct computed above are MARK-derived, not realized. When
            # fill_status is present and != "Filled" (Submitted / PreSubmitted / Cancelled / etc.),
            # null the realized fields; _log_trade_dataset derives outcome/win/tp_hit/sl_hit from
            # realized_pnl_pct, so nulling it here nulls those labels too. The mark valuation is
            # preserved under DISTINCT mark_estimate_* keys so no information is lost but it can
            # never be mistaken for a realized outcome by calibrate_conviction_sizing.py (qualifies
            # rows only when realized_pnl_pct is present) or export_training_data.py. A genuinely
            # Filled exit (fill_status == "Filled", real fill price) is UNTOUCHED -- byte-identical
            # to the prior path -- as is any caller that passes no fill_status (manual MKT close,
            # direct _log_exit test calls).
            _fs = rec.get("fill_status")
            if _fs is not None and _fs != "Filled":
                rec["mark_estimate_pnl"] = realized_pnl        # mark-derived $ P&L (NOT realized)
                rec["mark_estimate_pnl_pct"] = realized_pct    # mark-derived % P&L (NOT realized)
                rec["realized_pnl"] = None
                rec["realizedPNL"] = None
                rec["realized_pnl_pct"] = None
                realized_pnl = None  # keep the [EXIT-LOG] line honest (pnl=n/a for a non-fill)
            # COMMISSIONS + NET REALIZED P&L (2026-07-03). Persist round-trip IBKR fees and a
            # realized_pnl_net = gross realized - entry_commission - exit_commission ALONGSIDE the
            # (unchanged) gross realized_pnl. entry_commission is joined from journal/fills.log and
            # PRO-RATED to the contracts closed by THIS row (mirrors closed_basis on a scale-out);
            # exit_commission arrives via `extra` from the exit path. Null-safe: if either fee is
            # unknown, flag commission_unknown and leave net NULL (never fabricated).
            def _numok(v):
                try:
                    return v is not None and float(v) == float(v)
                except (TypeError, ValueError):
                    return False
            try:
                _full_q = int(je.get("quantity", qty) or qty)
            except (TypeError, ValueError):
                _full_q = qty
            _entry_comm_raw = je.get("entry_commission")
            _entry_comm = None
            if _numok(_entry_comm_raw):
                _entry_comm = (round(float(_entry_comm_raw) * qty / _full_q, 4)
                               if _full_q else float(_entry_comm_raw))
            _exit_comm_raw = rec.get("exit_commission")   # set via `extra` by the exit path
            _exit_comm = float(_exit_comm_raw) if _numok(_exit_comm_raw) else None
            _commission_unknown = (_entry_comm is None) or (_exit_comm is None)
            _final_gross = rec.get("realized_pnl")        # post-H3 (None on a non-fill)
            _realized_net = None
            if _final_gross is not None and not _commission_unknown:
                _realized_net = round(float(_final_gross) - _entry_comm - _exit_comm, 2)
            rec["entry_commission"] = _entry_comm
            rec["exit_commission"] = _exit_comm
            rec["commission_unknown"] = _commission_unknown
            rec["realized_pnl_net"] = _realized_net
            # real entry basis + slippage carried onto the exit row too (additive, from journal/join)
            rec["entry_fill_debit"] = je.get("entry_fill_debit")
            rec["entry_slippage"] = je.get("entry_slippage")
            rec["entry_slippage_pct"] = je.get("entry_slippage_pct")
            rec["basis_source"] = je.get("basis_source")
            with open(self._exits_log_path(), "a") as f:
                f.write(json.dumps(rec, default=str) + "\n")
                f.flush()
                os.fsync(f.fileno())
            print(f"[EXIT-LOG] {symbol} con_id={con_id} reason={reason} "
                  f"pnl=${realized_pnl if realized_pnl is not None else 'n/a'}")
            # ADDITIVE (2026-07-02): also emit the rich, fine-tuning-ready closed-trade record
            # (entry snapshot + mark path + MFE/MAE + close + labels) to the separate dataset.
            # AFTER the exits.log write + its own try/except so it can never affect exits.log.
            _dataset_ok = self._log_trade_dataset(rec, je, con_id)
            # C3 (2026-07-09): on a CONFIRMED FULL close, purge per-contract tracking + drop the
            # journal entry (AFTER the dataset row is written, so nothing needed for the record is
            # lost). Guarded so a still-RESTING exit (fill_status present and != "Filled") is NEVER
            # cleared -- that position is still live and must keep its peak/journal. A partial trim
            # (extra["partial"]) keeps the runner and is never cleared. Full closes qualify when the
            # fill CONFIRMED Filled, or on an independently confirmed tool/expiry terminal close.
            # ``manual`` is intentionally NOT terminal by reason alone: a submitted manual market
            # order remains pending until IBKR confirms Filled.
            _partial = bool((extra or {}).get("partial"))
            _fs_final = rec.get("fill_status")
            _terminal_reason = str(reason) in ("expired", "closed_by_tool", "liquidated")
            if _dataset_ok and (not _partial) and (_fs_final == "Filled" or _terminal_reason):
                self._clear_closed_position(con_id)
            return _dataset_ok
        except Exception as e:
            print(f"[WARN] exits.log write failed for con_id={con_id}: {e}")
            return False

    def _log_unfilled_exit(self, con_id: int, symbol: str, trigger, *, fill_status,
                           close_qty, trigger_mark, bid, limit_price, order_id,
                           reason=None, placed_at=None) -> None:
        """Record a TRIGGERED exit that did NOT fill (a terminal reject/cancel at placement, or a
        resting order abandoned unfilled) so fill_quality_report.py's fill-rate denominator isn't
        blind to a too-tight exit floor. ADDITIVE + RECORD-ONLY: writes ONLY the lightweight
        `kind:"trade"` non-fill row (no exits.log entry, no realized P&L) via trade_capture.

        NEVER raises into the exit path (matches trade_capture's contract). DEDUPED by
        (con_id, order_id): order.py's reject path deliberately RETRIES the exit next cycle, and a
        resting order can be re-observed across cycles -- so re-observing the SAME order can never
        emit a second unfilled row and inflate the denominator. A genuinely NEW placement (new
        order_id) logs its own row, as intended."""
        try:
            if not hasattr(self, "_unfilled_logged"):
                self._unfilled_logged = set()
            key = (con_id, order_id)
            if key in self._unfilled_logged:
                return
            self._unfilled_logged.add(key)
            je = self._journal_entries.get(con_id) or {}
            sp = je.get("spread")
            trade_capture.capture_unfilled(
                self._dataset_dir(), source="exit_manager", symbol=symbol, con_id=con_id,
                fill_status=fill_status, close_qty=close_qty, trigger_mark=trigger_mark,
                bid=bid, limit_price=limit_price, order_id=order_id, placed_at=placed_at,
                reason=reason, rule_fired=getattr(trigger, "trigger_type", None),
                spread=({"short_con_id": sp.get("short_con_id"),
                         "short_strike": sp.get("short_strike"),
                         "width": sp.get("width")} if sp else None))
            print(f"[UNFILLED-LOG] {symbol} con_id={con_id} order_id={order_id} "
                  f"status={fill_status} qty={close_qty} (non-fill recorded; continuing)")
        except Exception as e:
            try:
                print(f"[WARN] unfilled-exit log failed for con_id={con_id}: {e} (continuing)")
            except Exception:
                pass

    # ============================================================ TERMINAL-STATE CLOSES
    # (2026-07-03) Two whole classes of trade ENDING were invisible to the dataset:
    #   1. TOOL-CLOSES  -- close_symbol.py / liquidate.py (clientId 91) flatten a position; the
    #      journal-drop path dropped the ENTIRE entry+mark-path arc with no exits.log / dataset row.
    #   2. EXPIRIES     -- a position that expires worthless / auto-exercises just VANISHED from
    #      live positions with no close record at all.
    # Both now emit ONE complete closed-trade row via the SAME _log_exit machinery a placed exit
    # uses (so the row carries the full entry snapshot + accumulated mark path/MFE/MAE + a close
    # block + labels, and honors the H3 realized-vs-mark convention). RECORD-ONLY + fully wrapped:
    # a logging bug can NEVER raise into the load/reconcile/close path, and a price/P&L is never
    # fabricated (null with a reason when unknown). Deduped so each terminal close emits EXACTLY
    # once across cycles AND across a process restart (via the on-disk dataset).

    def _terminal_dedupe_set(self) -> Set[int]:
        """In-memory set of con_ids already given a terminal (tool-close/expiry) row THIS process.
        Survives the per-cycle _load_journal reset (created lazily, never cleared) like
        _unfilled_logged, so a re-observed terminal state can't double-log within the process."""
        if not hasattr(self, "_terminal_logged"):
            self._terminal_logged: Set[int] = set()
        return self._terminal_logged

    def _full_close_on_disk(self) -> Set[int]:
        """con_ids that ALREADY have a FULL (non-partial) closed-trade row in trade_dataset.jsonl.
        A trade's journal ENTRY line persists in trades.log forever, so after a process restart a
        past-expiry entry (or a re-seen tool marker) would otherwise re-emit; this cross-restart
        guard suppresses that. Uses the dataset's `partial` flag (exits.log has none) so a
        scaled-out RUNNER that later truly expires is NOT suppressed. Best-effort; never raises."""
        ids: Set[int] = set()
        try:
            dp = self._dataset_path()
            if not os.path.exists(dp):
                return ids
            with open(dp) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if r.get("kind") != "trade":
                        continue
                    close = r.get("close") or {}
                    if close.get("partial"):
                        continue  # a scale-out trim is NOT a full/terminal close
                    if r.get("unfilled"):
                        continue  # a resting/rejected attempt is explicitly NOT a close
                    fill_status = close.get("fill_status")
                    terminal_event = close.get("exit_event") or close.get("reason")
                    confirmed = (
                        fill_status == "Filled"
                        or terminal_event in {"expired", "closed_by_tool", "liquidated"}
                        # Backward compatibility for older realized rows that predate fill_status.
                        or (fill_status is None and close.get("realized_pnl") is not None)
                    )
                    if not confirmed:
                        continue
                    cid = r.get("con_id")
                    if cid is not None:
                        try:
                            ids.add(int(cid))
                        except (TypeError, ValueError):
                            pass
        except Exception as e:
            print(f"[WARN] full-close on-disk scan failed (continuing): {e}")
        return ids

    def _emit_tool_close(self, con_id: int, je: dict, markers: dict) -> None:
        """Emit ONE full closed-trade row for a position a close TOOL flattened (the journal-drop
        arc). exit_reason = 'liquidated' (liquidate.py) or 'closed_by_tool' (close_symbol.py).
        Realized P&L ONLY when a real close fill price is known (single: the long-leg fill; spread:
        long fill - short-leg buy-to-close fill), else null with a reason (H3 convention -- never
        fabricated). Deduped by con_id (in-process + on-disk). Never raises into load/reconcile."""
        try:
            con_id = int(con_id)
            dedupe = self._terminal_dedupe_set()
            if con_id in dedupe:
                return
            if con_id in self._full_close_on_disk():
                dedupe.add(con_id)
                return
            sp = je.get("spread") or {}
            is_spread = bool(sp.get("short_con_id"))
            long_marker = markers.get(con_id) or {}
            short_marker = {}
            if sp.get("short_con_id") is not None:
                try:
                    short_marker = markers.get(int(sp["short_con_id"])) or {}
                except (TypeError, ValueError):
                    short_marker = {}
            primary = long_marker or short_marker or {}
            tool = primary.get("tool")
            reason = "liquidated" if tool == "liquidate" else "closed_by_tool"
            client_id = primary.get("client_id")

            def _num_ok(v) -> bool:
                try:
                    return v is not None and float(v) == float(v)
                except (TypeError, ValueError):
                    return False

            qty = int(je.get("quantity", 1) or 1)
            entry_debit = je.get("debit")
            long_fill = long_marker.get("avg_fill_price")
            exit_px = None
            fill_known = False
            if long_marker.get("status") == "Filled" and _num_ok(long_fill):
                if is_spread:
                    short_fill = short_marker.get("avg_fill_price")
                    if short_marker.get("status") == "Filled" and _num_ok(short_fill):
                        exit_px = float(long_fill) - float(short_fill)  # NET combo proceeds/share
                        fill_known = True
                    # else: cannot value the net without the short-leg fill -> realized stays null
                else:
                    exit_px = float(long_fill)
                    fill_known = True
            import types as _t
            trig = _t.SimpleNamespace(trigger_type=reason, pnl_pct=0.0,
                                      message=f"position closed by {tool or 'close tool'}")
            extra = {
                "exit_event": reason,
                "rule_fired": reason,
                "exit_reasoning": f"closed by {tool or 'close tool'} (clientId={client_id})",
                # H3: fill_status Filled + a real price => realized preserved; otherwise a
                # non-Filled status nulls realized (proceeds are None here anyway).
                "fill_status": ("Filled" if fill_known
                                else (long_marker.get("status") or "closed_by_tool")),
                "avg_fill_price": (exit_px if fill_known else None),
                "close_client_id": client_id,
                "mfe_pct": self.state_manager.state.mfe_pct.get(str(con_id)),
            }
            if not fill_known:
                extra["realized_unknown_reason"] = (
                    "spread_net_fill_unknown" if is_spread else "tool_close_fill_unknown")
            self._log_exit(con_id, je.get("symbol"), trig,
                           exit_price_per_share=(exit_px if fill_known else None),
                           quantity=qty, reason=reason, extra=extra, entry_debit=entry_debit,
                           je=je)
            dedupe.add(con_id)
            print(f"[TERMINAL-CLOSE] {je.get('symbol')} con_id={con_id} reason={reason} "
                  f"fill_known={fill_known} (tool-close recorded; continuing)")
        except Exception as e:
            try:
                print(f"[WARN] tool-close logging failed for con_id={con_id}: {e} (continuing)")
            except Exception:
                pass

    def _emit_expiry_close(self, con_id: int, je: dict, spot: Optional[float] = None) -> None:
        """Emit ONE 'expired' closed-trade row for a journaled position past its expiry and gone
        from live positions. Realized outcome from the option's INTRINSIC value at `spot`:
        OTM -> intrinsic 0 -> worthless -> realized = -100% of debit; ITM -> intrinsic value.
        Spreads value the NET intrinsic (long - short). If `spot` (or a needed strike) is unknown
        the value is NOT assumed -- realized is null and `expiry_value_unknown` is flagged. DTE=0.
        Deduped by con_id (in-process + on-disk). Never raises into the cycle."""
        try:
            con_id = int(con_id)
            dedupe = self._terminal_dedupe_set()
            if con_id in dedupe:
                return
            if con_id in self._full_close_on_disk():
                dedupe.add(con_id)
                return
            sp = je.get("spread") or {}
            is_spread = bool(sp.get("short_con_id"))
            right = (je.get("right") or "C").upper()
            strike = je.get("strike")
            qty = int(je.get("quantity", 1) or 1)
            entry_debit = je.get("debit")

            def _intrinsic(k):
                try:
                    if k is None or spot is None:
                        return None
                    if right.startswith("C"):
                        return max(0.0, float(spot) - float(k))
                    return max(0.0, float(k) - float(spot))
                except (TypeError, ValueError):
                    return None

            exit_px = None
            value_known = False
            if spot is not None:
                li = _intrinsic(strike)
                if is_spread:
                    si = _intrinsic(sp.get("short_strike"))
                    if li is not None and si is not None:
                        exit_px = li - si          # net combo intrinsic at expiry
                        value_known = True
                elif li is not None:
                    exit_px = li
                    value_known = True
            import types as _t
            trig = _t.SimpleNamespace(trigger_type="expired", pnl_pct=0.0,
                                      message="option expired")
            extra = {
                "exit_event": "expired",
                "rule_fired": "expired",
                "exit_reasoning": ("expired worthless (OTM)"
                                   if (value_known and not exit_px)
                                   else "expired"),
                "dte_at_close": 0,
                "underlying_price": spot,
                "mfe_pct": self.state_manager.state.mfe_pct.get(str(con_id)),
            }
            if not value_known:
                # Do NOT assume worthless without the settlement spot: flag it, realized null.
                extra["expiry_value_unknown"] = True
                extra["realized_unknown_reason"] = "expiry_value_unknown"
            # value_known -> pass the intrinsic (0.0 for OTM => proceeds 0 => realized -100%).
            # No fill_status here: an expiry realized value is definitional, not a resting mark,
            # so H3's non-fill nulling must NOT strip the -100%/intrinsic outcome.
            self._log_exit(con_id, je.get("symbol"), trig,
                           exit_price_per_share=(exit_px if value_known else None),
                           quantity=qty, reason="expired", extra=extra, entry_debit=entry_debit,
                           je=je)
            dedupe.add(con_id)
            print(f"[TERMINAL-CLOSE] {je.get('symbol')} con_id={con_id} reason=expired "
                  f"value_known={value_known} spot={spot} (expiry recorded; continuing)")
        except Exception as e:
            try:
                print(f"[WARN] expiry logging failed for con_id={con_id}: {e} (continuing)")
            except Exception:
                pass

    async def _process_expiries(self, live_positions) -> None:
        """Detect journaled positions PAST expiry AND gone from live positions and emit an
        'expired' close row for each (best-effort intrinsic value from a spot fetch). Tool-closed
        arcs are already dropped from _journal_entries by _load_journal, so they are never seen
        here (no double-log). Deduped; never raises into the cycle."""
        try:
            _on_disk = None
            for con_id, je in list(self._journal_entries.items()):
                try:
                    if con_id in live_positions:
                        continue  # still open at the broker
                    dte = days_to_expiry(je.get("expiry"))
                    if dte is None or dte >= 0:
                        continue  # unknown or not-yet-expired -> not an expiry ending
                    if con_id in self._terminal_dedupe_set():
                        continue
                    if _on_disk is None:
                        _on_disk = self._full_close_on_disk()
                    if con_id in _on_disk:
                        # closed earlier (its full row is on disk) -> mark seen, don't re-emit
                        self._terminal_dedupe_set().add(con_id)
                        continue
                    spot = None
                    try:
                        spot = await self._spot_price(je.get("symbol"))
                    except Exception:
                        spot = None
                    self._emit_expiry_close(con_id, je, spot=spot)
                except Exception as _ie:
                    print(f"[WARN] expiry check failed for con_id={con_id}: {_ie} (continuing)")
        except Exception as e:
            print(f"[WARN] expiry processing errored (continuing): {e}")

    @staticmethod
    def _exit_reason(trigger) -> str:
        """Map an ExitTrigger.trigger_type to the calibration reason vocabulary."""
        tt = (getattr(trigger, "trigger_type", "") or "").lower()
        # scale_out (partial trim) is its own calibration reason -- MUST be checked before the
        # profit/stop matchers so a partial is never mislabeled as a full profit_target exit.
        if "scale" in tt:
            return "scale_out"
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
        """Report the ENTRY kill switch state.

        The switch is intentionally informational in ExitManager: it blocks new risk in Trader,
        never risk-reducing closes here.
        """
        kill_path = Path(self.config.kill_switch.path)
        if kill_path.exists():
            print(f"[KILL SWITCH] {self.config.kill_switch.path} active: halting entries only; "
                  "protective/manual exits remain armed")
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
        # NO silent failure: record + alert, but KEEP the request durable.  A manual exit is an
        # outcome request (be flat), not a one-shot submission request; only a confirmed Filled
        # terminal state may remove it from manual_exits.json.
        try:
            with open(os.path.join(os.path.dirname(self.config.journal.path) or ".", "manual_exit_errors.log"), "a") as f:
                f.write(f"{datetime.utcnow().isoformat()} {sym} con_id={cid} FAILED: {reason}\n")
        except Exception:
            pass
        try:
            ch = getattr(self.config, "alerts_channel", "") or ""; tok = os.environ.get("SLACK_BOT_TOKEN", "")
            if ch and tok:
                from exitmgr import approval
                approval.post_proposal(tok, ch, f":warning: *Could not auto-sell {sym}* (one-tap early-exit): {reason}. The request remains pending and will retry until Filled.")
        except Exception:
            pass
        print(f"[MANUAL-EXIT] {sym} con_id={cid} FAILED/PENDING: {reason}")

    def _clear_manual_exit(self, con_id) -> None:
        try:
            cur = self._read_manual_exits(); cur.discard(int(con_id))
            path = Path(self._manual_exit_path())
            tmp = path.with_suffix(path.suffix + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(sorted(cur), f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            try:
                dfd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            except OSError:
                pass
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
        # H1 FIX (2026-07-03): key by the US/Eastern trading day, matching the WRITE side
        # (order.place_close_order -> state.update_daily_stats(_trading_day(), ...)). The old
        # datetime.utcnow() key disagreed from ~20:00 ET to midnight ET (UTC already "tomorrow"),
        # so max_orders_per_day / max_notional_per_day read an EMPTY bucket and could not bind,
        # and evening activity was split across two date keys. Same convention both sides now.
        today = _trading_day()
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

    def _post_reconcile_block_alert(self, alerts) -> None:
        """Slack #trading-alerts when reconciliation ABORTS, so a blocked exit manager is never a
        silent multi-hour outage (6/29: an over-covered spread leg crash-looped the trader ~3h with
        no notification). Throttled to once / 30 min, persisted across the loop's respawns."""
        import os, json as _json, urllib.request, time
        channel = getattr(self.config, "alerts_channel", "") or ""
        token = os.environ.get("SLACK_BOT_TOKEN", "")
        if not (channel and token):
            return
        tsf = os.path.expanduser("~/exitmgr-app/.reconcile_alert_ts")
        try:
            if os.path.exists(tsf) and (time.time() - os.path.getmtime(tsf)) < 1800:
                return
        except Exception:
            pass
        errs = [a for a in alerts if "[ERROR]" in a] or alerts
        body = (":octagonal_sign: *Exit-manager reconciliation BLOCKED -- trader cannot manage exits.*\n"
                + "\n".join(errs[:6]) + "\n_Resolve the position(s) above (journal or close) to unblock._")
        try:
            req = urllib.request.Request("https://slack.com/api/chat.postMessage",
                data=_json.dumps({"channel": channel, "text": body}).encode(),
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=8)
            open(tsf, "w").write(str(time.time()))
        except Exception as _e:
            print(f"[WARN] reconcile-block Slack alert failed: {_e}")

    # Protective (risk-REDUCING) exit trigger types. C1c (2026-07-09): the daily order/notional caps
    # bound risk-ADDING activity; a protective close must NEVER be withheld because the day cap is
    # hit. A scale_out is an optional profit-trim (not protective), so it stays subject to the caps.
    _PROTECTIVE_TRIGGERS = frozenset({
        "stop", "trailing_stop", "time_stop", "profit_target", "take_profit", "model_cut"})

    def _is_protective_exit(self, trigger) -> bool:
        return getattr(trigger, "trigger_type", None) in self._PROTECTIVE_TRIGGERS

    def _clear_closed_position(self, con_id) -> None:
        """C3 (2026-07-09): on a CONFIRMED full close, purge ALL per-contract tracking
        (peak/mfe/mae + their timestamps/mark path, scaled_out, trail_armed) AND drop the journal
        entry, so a later RE-ENTRY of the SAME conId starts clean. Without this, a stale peak
        survived (prune keeps live UNION journal) and the fresh position inherited it -> auto-trail
        armed and fired immediately. Idempotent; persists the state; never raises into the caller."""
        try:
            k = str(con_id)
            st = self.state_manager.state
            for d in (st.peak_prices, st.mfe_pct, st.mae_pct, st.mfe_ts, st.mae_ts,
                      st.mark_path, st.scaled_out, st.trail_armed):
                d.pop(k, None)
            try:
                self._journal_entries.pop(int(con_id), None)
            except (TypeError, ValueError):
                pass
            self._journal_entries.pop(con_id, None)
            self.state_manager.save()
        except Exception as e:
            print(f"[WARN] clear-closed-position failed for con_id={con_id}: {e}")

    def _post_stops_withheld_alert(self, items) -> None:
        """C1d (2026-07-09): UNTHROTTLED Slack alert whenever a PROTECTIVE exit (stop/TP/time-stop)
        was actually withheld this cycle because that con_id is reconcile-inconsistent. Unlike the
        30-min-throttled reconcile-BLOCK alert, this fires every cycle a stop is truly being held
        back -- a withheld protective close is an active-risk event, not a quiet status. Slack-only;
        never raises into the cycle."""
        if not items:
            return
        import urllib.request, json as _json
        channel = getattr(self.config, "alerts_channel", "") or ""
        token = os.environ.get("SLACK_BOT_TOKEN", "")
        if not (channel and token):
            return
        lines = "\n".join(f"• *{sym}* con_id={cid} — {why} WITHHELD (con_id reconcile-inconsistent)"
                          for sym, cid, why in items[:10])
        body = (":octagonal_sign: *Protective exits WITHHELD this cycle* (position(s) reconcile-inconsistent):\n"
                + lines + "\n_Resolve the flagged position(s) (journal or close) to release their stops._")
        try:
            req = urllib.request.Request("https://slack.com/api/chat.postMessage",
                data=_json.dumps({"channel": channel, "text": body}).encode(),
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=8)
        except Exception as _e:
            print(f"[WARN] stops-withheld Slack alert failed: {_e}")

    async def _alert_unfilled_orders(self) -> None:
        """FILL-INTEGRITY alarm (2026-07-01 audit: EXECUTION was the biggest loss pool --
        5/15 'executed' entries never filled, 3 exits filled 2-3 days late, a fired
        trailing-stop win never filled and became a loss). During RTH:
          * any in-flight EXIT older than construction.fill_alarm_minutes with remaining
            qty -> Slack #error-logs EVERY cycle until it fills or is cancelled;
          * any resting BUY (entry) order -> alarm too (entries are DAY MKT orders; one
            resting at all means it is not filling).
        Read-only + Slack only; never raises into the exit path."""
        cons = getattr(self.config, "construction", None)
        mins = float(getattr(cons, "fill_alarm_minutes", 15) or 15)
        token = os.environ.get("SLACK_BOT_TOKEN", "")
        channel = (getattr(self.config, "error_channel", "") or ""
                   ) or (getattr(self.config, "alerts_channel", "") or "")
        if not (token and channel):
            return
        try:
            from exitmgr.trader import _market_open
            if not _market_open():
                return  # RTH-only alarm; an overnight non-fill alarms at the next open
        except Exception:
            pass
        stale: List[str] = []
        # AWARE-DATETIME FIX (2026-07-03): placed_at is written as an AWARE UTC isoformat
        # (datetime.now(timezone.utc).isoformat() -> "...+00:00") in order.place_close_order. The
        # old code parsed it into an aware datetime, then subtracted it from a NAIVE datetime.utcnow()
        # -> "can't subtract offset-naive and offset-aware datetimes" TypeError on EVERY in-flight,
        # so this whole exit-side alarm was dead. Use an aware `now` and parse robustly (assume UTC
        # for a naive/legacy timestamp), and keep the subtraction inside the try.
        now = datetime.now(timezone.utc)
        # Exits: in-flight closes we placed, still unfilled past the threshold.
        for cid_str, inf in dict(self.state_manager.state.in_flight).items():
            if not inf.order_id or not inf.placed_at or inf.remaining_qty <= 0:
                continue
            try:
                placed = datetime.fromisoformat(str(inf.placed_at).replace("Z", "+00:00"))
                if placed.tzinfo is None:
                    placed = placed.replace(tzinfo=timezone.utc)
                age_min = (now - placed).total_seconds() / 60.0
            except (ValueError, TypeError):
                continue
            if age_min >= mins:
                je = self._journal_entries.get(int(cid_str)) or {}
                stale.append(f"• EXIT *{je.get('symbol', '?')}* con_id={cid_str} order_id={inf.order_id} "
                             f"— unfilled for {age_min:.0f} min (qty {inf.remaining_qty})")
        # Entries: any resting BUY order on the account (a DAY MKT entry should fill in seconds).
        try:
            open_trades = await self.ib_conn.ib.reqOpenOrdersAsync()
            for t in open_trades or []:
                o = getattr(t, "order", None)
                c = getattr(t, "contract", None)
                if not o or getattr(o, "action", "") != "BUY":
                    continue
                age_min = None
                try:
                    logs = getattr(t, "log", None) or []
                    if logs:
                        t0 = logs[0].time
                        from datetime import timezone as _tz
                        age_min = (datetime.now(_tz.utc) - t0).total_seconds() / 60.0
                except Exception:
                    age_min = None
                if age_min is not None and age_min < mins:
                    continue  # young order -- give it time
                stale.append(f"• ENTRY *{getattr(c, 'symbol', '?')}* order_id={getattr(o, 'orderId', '?')} "
                             f"— BUY resting unfilled"
                             + (f" for {age_min:.0f} min" if age_min is not None else " (age unknown)"))
        except Exception as e:
            print(f"[WARN] open-order scan for fill alarm failed: {e}")
        if not stale:
            return
        try:
            from exitmgr import approval
            approval.post_proposal(token, channel,
                ":hourglass_flowing_sand: *UNFILLED ORDER ALARM* — placed but not filled:\n"
                + "\n".join(stale)
                + "\n_Escalates every cycle until filled/cancelled. Check the gateway; consider a manual close/cancel._")
            print(f"[FILL-ALARM] {len(stale)} unfilled order(s) alerted to Slack")
        except Exception as e:
            print(f"[WARN] unfilled-order Slack alarm failed: {e}")

    @staticmethod
    def _trade_order_id(trade):
        try:
            return int(getattr(getattr(trade, "order", None), "orderId", 0) or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _positive_int(value) -> int:
        if isinstance(value, bool):
            return 0
        if isinstance(value, int):
            return value if value > 0 else 0
        if isinstance(value, str) and value.isdigit():
            parsed = int(value)
            return parsed if parsed > 0 else 0
        return 0

    @staticmethod
    def _ib_client_id(value):
        """Return an IB client id including legitimate zero; ``None`` means unknown/legacy."""
        if isinstance(value, bool):
            return None
        if isinstance(value, int) and value >= 0:
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
        return None

    def _trade_key_con_id(self, trade) -> int:
        contract = getattr(trade, "contract", None)
        if contract is None:
            return 0
        try:
            return int(self.ib_conn._order_key_con_id(contract) or 0)
        except Exception:
            return self._positive_int(getattr(contract, "conId", 0))

    def _trade_matches_in_flight(self, trade, inf) -> bool:
        """Match a Trade to one durable close using the strongest available IB identity."""
        tcid = self._trade_key_con_id(trade)
        if tcid and tcid != int(inf.con_id):
            return False
        order = getattr(trade, "order", None)
        tperm = self._positive_int(getattr(order, "permId", 0))
        iperm = self._positive_int(getattr(inf, "perm_id", 0))
        if tperm and iperm:
            return tperm == iperm
        tref = getattr(order, "orderRef", None)
        iref = getattr(inf, "order_ref", None)
        if tref and iref:
            return str(tref) == str(iref)
        toid = self._positive_int(getattr(order, "orderId", 0))
        ioid = self._positive_int(getattr(inf, "order_id", 0))
        tclient = self._ib_client_id(getattr(order, "clientId", None))
        iclient = self._ib_client_id(getattr(inf, "client_id", None))
        strong_client = bool(getattr(inf, "identity_version", 0) >= 1
                             or iclient is not None)
        if strong_client:
            return bool(toid and ioid and toid == ioid
                        and tclient is not None and tclient == iclient)
        # Legacy records did not persist clientId/permId/orderRef.  Preserve their only possible
        # join while ensuring all new records take a stronger branch above.
        return bool(toid and ioid and toid == ioid and iclient is None)

    def _execution_matches_in_flight(self, fill, inf) -> bool:
        ex = getattr(fill, "execution", None)
        contract = getattr(fill, "contract", None)
        ecid = self._positive_int(getattr(contract, "conId", 0))
        if ecid and ecid != int(inf.con_id):
            return False
        eperm = self._positive_int(getattr(ex, "permId", 0))
        iperm = self._positive_int(getattr(inf, "perm_id", 0))
        if eperm and iperm:
            return eperm == iperm
        eoid = self._positive_int(getattr(ex, "orderId", 0))
        ioid = self._positive_int(getattr(inf, "order_id", 0))
        eclient = self._ib_client_id(getattr(ex, "clientId", None))
        iclient = self._ib_client_id(getattr(inf, "client_id", None))
        strong_client = bool(getattr(inf, "identity_version", 0) >= 1
                             or iclient is not None)
        if strong_client:
            return bool(eoid and ioid and eoid == ioid
                        and eclient is not None and eclient == iclient)
        return bool(eoid and ioid and eoid == ioid and iclient is None)

    async def _terminal_trades_for_in_flight(self, infs: dict) -> Dict[int, object]:
        """Find terminal trades in both the current-session and restart-safe broker stores.

        ``ib.trades()`` loses history when the process reconnects.  Completed orders are therefore
        queried as the second source; for a single-leg close, executions are a final fallback.  No
        source is allowed to manufacture a fill: only an explicit ``Filled`` status, or executions
        whose aggregate quantity covers the requested close, is returned.
        """
        import types as _types

        targets = []
        for key, inf in infs.items():
            try:
                targets.append((int(key), inf))
            except (TypeError, ValueError):
                continue
        found: Dict[int, object] = {}
        self._terminal_lookup_complete = False

        def _status(tr):
            return getattr(getattr(tr, "orderStatus", None), "status", None)

        def _rank(tr) -> int:
            status = _status(tr)
            if status == "Filled":
                return 3
            if status in {"Cancelled", "ApiCancelled", "Inactive"}:
                return 2
            return 1

        def _is_terminal(tr) -> bool:
            return _status(tr) == "Filled" or _status(tr) in {
                "Cancelled", "ApiCancelled", "Inactive"}

        def _collect(items) -> None:
            try:
                seq = list(items or [])
            except Exception:
                return
            for tr in seq:
                for cid, inf in targets:
                    if self._trade_matches_in_flight(tr, inf):
                        # A stale current-session Submitted trade must never mask the broker's
                        # completed Filled record after a restart. Prefer terminal/fill evidence.
                        if cid not in found or _rank(tr) >= _rank(found[cid]):
                            found[cid] = tr

        ib = getattr(self.ib_conn, "ib", None)
        if ib is None or not targets:
            return found
        try:
            _collect(ib.trades())
        except Exception as e:
            print(f"[WARN] in-flight fill poll: current trade blotter unavailable ({e})")

        missing = {cid for cid, _ in targets if cid not in found or not _is_terminal(found[cid])}
        completed_ok = False
        if missing and hasattr(ib, "reqCompletedOrdersAsync"):
            try:
                _collect(await ib.reqCompletedOrdersAsync(apiOnly=False))
                completed_ok = True
            except TypeError:
                try:
                    _collect(await ib.reqCompletedOrdersAsync(False))
                    completed_ok = True
                except Exception as e:
                    print(f"[WARN] completed-order lookup failed ({e})")
            except Exception as e:
                print(f"[WARN] completed-order lookup failed ({e})")

        # Last-resort reconstruction for SINGLE-leg closes.  Combo executions are per-leg and
        # cannot be safely collapsed into a net fill price here, so those wait for a completed Trade.
        # Executions alone prove a terminal fill only when their aggregate covers the FULL planned
        # close. A partial execution may still belong to a working order and is never fabricated as
        # a cancellation.
        missing = {cid for cid, _ in targets if cid not in found or not _is_terminal(found[cid])}
        executions_ok = False
        if missing and hasattr(ib, "reqExecutionsAsync"):
            try:
                fills = list(await ib.reqExecutionsAsync() or [])
                executions_ok = True
                for cid, inf in targets:
                    if cid not in missing:
                        continue
                    ctx = dict(getattr(inf, "exit_context", {}) or {})
                    if (ctx.get("journal_entry") or {}).get("spread"):
                        continue
                    rows = [fl for fl in fills if self._execution_matches_in_flight(fl, inf)]
                    # reqExecutions can repeat rows; execId is the broker's unique execution key.
                    unique = []
                    seen_exec_ids = set()
                    for fl in rows:
                        exec_id = getattr(getattr(fl, "execution", None), "execId", None)
                        key = str(exec_id) if exec_id else id(fl)
                        if key in seen_exec_ids:
                            continue
                        seen_exec_ids.add(key)
                        unique.append(fl)
                    rows = unique
                    qty = 0.0
                    value = 0.0
                    for fl in rows:
                        ex = getattr(fl, "execution", None)
                        try:
                            q = float(getattr(ex, "shares", 0) or 0)
                            p = float(getattr(ex, "price", 0))
                        except (TypeError, ValueError):
                            continue
                        qty += q
                        value += q * p
                    needed = int(ctx.get("close_qty") or getattr(inf, "remaining_qty", 0) or 0)
                    if qty >= needed > 0:
                        found[cid] = _types.SimpleNamespace(
                            contract=_types.SimpleNamespace(conId=cid, secType="OPT"),
                            order=_types.SimpleNamespace(
                                orderId=getattr(inf, "order_id", 0),
                                permId=getattr(inf, "perm_id", 0),
                                clientId=getattr(inf, "client_id", 0),
                                orderRef=getattr(inf, "order_ref", None)),
                            orderStatus=_types.SimpleNamespace(
                                status="Filled", avgFillPrice=value / qty,
                                filled=qty, remaining=max(0.0, needed - qty)),
                            fills=rows,
                        )
            except Exception as e:
                print(f"[WARN] execution fill lookup failed ({e})")
        # For an orphaned pre-transmission intent, absence is actionable only when BOTH broker
        # history reads succeeded.  The poller also requires that no live order matched.
        self._terminal_lookup_complete = bool(completed_ok and executions_ok)
        return found

    @staticmethod
    def _trade_fill_timestamp(trade) -> str:
        try:
            times = [getattr(getattr(fl, "execution", None), "time", None)
                     for fl in (getattr(trade, "fills", None) or [])]
            times = [t for t in times if t is not None]
            if times:
                return max(times).isoformat()
        except Exception:
            pass
        return datetime.now().astimezone().isoformat()

    @staticmethod
    def _trade_reported_filled(trade) -> float:
        try:
            value = float(getattr(getattr(trade, "orderStatus", None), "filled", 0) or 0)
            return value if value == value and value > 0 else 0.0
        except (TypeError, ValueError):
            return 0.0

    def _fill_identity(self, con_id: int, inf, trade) -> str:
        """Freeze one durable, collision-safe key for this close/fill.

        Once chosen it never upgrades (for example orderRef -> later permId), because changing the
        key after the exits-log commit would defeat replay dedupe.
        """
        existing = getattr(inf, "fill_key", None)
        if existing:
            return str(existing)
        order = getattr(trade, "order", None)
        perm_id = (self._positive_int(getattr(order, "permId", 0))
                   or self._positive_int(getattr(inf, "perm_id", 0)))
        order_id = (self._positive_int(getattr(order, "orderId", 0))
                    or self._positive_int(getattr(inf, "order_id", 0)))
        trade_client_id = self._ib_client_id(getattr(order, "clientId", None))
        stored_client_id = self._ib_client_id(getattr(inf, "client_id", None))
        client_id = (trade_client_id if trade_client_id is not None else stored_client_id)
        trade_ref = getattr(order, "orderRef", None)
        stored_ref = getattr(inf, "order_ref", None)
        order_ref = (trade_ref if isinstance(trade_ref, str) and trade_ref
                     else stored_ref if isinstance(stored_ref, str) and stored_ref else None)
        inf.perm_id = perm_id
        inf.order_id = order_id
        inf.client_id = client_id
        inf.order_ref = order_ref
        if perm_id:
            key = f"perm:{perm_id}:con:{int(con_id)}"
        elif order_ref:
            key = f"ref:{order_ref}:con:{int(con_id)}"
        elif client_id is not None and order_id:
            key = f"api:{client_id}:order:{order_id}:con:{int(con_id)}"
        else:
            key = f"legacy:order:{order_id}:con:{int(con_id)}"
        inf.fill_key = key
        if perm_id or order_ref or client_id is not None:
            inf.identity_version = 1
        return key

    def _finalize_in_flight_exit(self, con_id: int, inf, trade) -> bool:
        """Finalize one confirmed fill exactly once, then release its durable state.

        A frozen composite fill key is the commit key.  The ledger, reload handoff, manual cleanup,
        and alert each have a durable replay checkpoint; the in-flight record is removed last.
        """
        try:
            order_status = getattr(trade, "orderStatus", None)
            status = getattr(order_status, "status", None)
            terminal_cancel = status in {"Cancelled", "ApiCancelled", "Inactive"}
            if status != "Filled" and not terminal_cancel:
                return False

            ctx = dict(getattr(inf, "exit_context", {}) or {})
            if not ctx:
                # A legacy record has no basis/trigger snapshot to book honestly. Preserve the
                # historical full-fill cleanup, but never treat a cancellation as a fill.
                if status == "Filled":
                    self.state_manager.state.remove_in_flight(con_id)
                    self.state_manager.save()
                    return True
                return False

            planned_qty = int(ctx.get("close_qty") or inf.remaining_qty or 0)
            if planned_qty <= 0:
                print(f"[WARN] Terminal order_id={inf.order_id} has no durable close quantity; retaining")
                return False
            if status == "Filled":
                qty = planned_qty
            else:
                qty = min(planned_qty, int(self._trade_reported_filled(trade)))
                if qty <= 0:
                    return False

            fill_px = getattr(order_status, "avgFillPrice", None)
            try:
                fill_px = float(fill_px)
                if fill_px != fill_px or fill_px < 0:
                    raise ValueError("invalid fill price")
            except (TypeError, ValueError):
                print(f"[WARN] Filled order_id={inf.order_id} has no valid avgFillPrice; "
                      "retaining in-flight for a later broker read")
                return False

            import types as _types
            trig = _types.SimpleNamespace(
                trigger_type=ctx.get("trigger_type") or ctx.get("reason") or "exit",
                pnl_pct=float(ctx.get("trigger_pnl_pct") or 0.0),
                message=ctx.get("trigger_message") or "asynchronous exit fill",
                reload=bool(ctx.get("reload", False)),
                reload_conviction=ctx.get("reload_conviction"),
            )
            try:
                planned_basis = float(ctx.get("entry_debit", inf.entry_debit))
            except (TypeError, ValueError):
                planned_basis = float(inf.entry_debit)
            basis = planned_basis * qty / planned_qty
            try:
                if basis > 0:
                    trig.pnl_pct = (fill_px * 100 * qty - basis) / basis * 100
            except (TypeError, ValueError):
                pass
            je = dict(ctx.get("journal_entry") or self._journal_entries.get(con_id) or {})
            extra = dict(ctx.get("extra") or {})
            position_qty = int(ctx.get("position_qty") or planned_qty)
            full_position_closed = qty >= position_qty
            partial = bool(extra.get("partial")) or not full_position_closed
            fill_key = self._fill_identity(con_id, inf, trade)
            # Freeze the key before the first append.  A later broker read may expose permId, but
            # replay must keep using the identity under which this fill was initially committed.
            self.state_manager.save()
            extra.update({
                "order_id": inf.order_id,
                "perm_id": getattr(inf, "perm_id", 0),
                "client_id": getattr(inf, "client_id", 0),
                "order_ref": getattr(inf, "order_ref", None),
                "close_identity": fill_key,
                "fill_status": "Filled",
                "terminal_order_status": status,
                "avg_fill_price": fill_px,
                "fill_ts": self._trade_fill_timestamp(trade),
                "exit_commission": commission_from_trade(trade),
                "exit_model_identity": ctx.get("exit_model_identity"),
                "partial": partial,
                "close_qty": qty,
                "remaining_qty": max(0, position_qty - qty),
            })
            ok = self._log_exit(
                con_id, ctx.get("symbol") or je.get("symbol") or "",
                trig, exit_price_per_share=fill_px, quantity=qty,
                reason=ctx.get("reason") or self._exit_reason(trig),
                extra=extra, entry_debit=basis, je=je,
            )
            if not ok:
                return False

            effects = inf.side_effects
            if not effects.get("ledger"):
                effects["ledger"] = True
                self.state_manager.save()

            if ctx.get("trigger_type") == "scale_out" and not effects.get("scale_out"):
                self.state_manager.state.scaled_out[str(con_id)] = True
                effects["scale_out"] = True
                self.state_manager.save()
            if not effects.get("reload"):
                reload_ok = self._maybe_write_reload_ticket(
                    con_id, ctx.get("symbol") or je.get("symbol") or "", trig,
                    position_qty, planned_basis, fill_px,
                    ("Filled" if full_position_closed and status == "Filled" else None),
                    fill_key=fill_key, je=je)
                if not reload_ok:
                    return False
                effects["reload"] = True
                self.state_manager.save()
            if (ctx.get("manual_request") and full_position_closed
                    and not effects.get("manual_clear")):
                self._clear_manual_exit(con_id)
                effects["manual_clear"] = True
                self.state_manager.save()
            if full_position_closed and not effects.get("position_clear"):
                self._clear_closed_position(con_id)
                effects["position_clear"] = True
                self.state_manager.save()
            if not effects.get("alert"):
                slack_key = str(uuid.uuid5(uuid.NAMESPACE_URL, fill_key))
                if not self._post_exit_alert(
                        ctx.get("symbol") or je.get("symbol") or "", trig,
                        client_msg_id=slack_key):
                    return False
                effects["alert"] = True
                self.state_manager.save()
            self.state_manager.state.remove_in_flight(con_id)
            self.state_manager.save()
            print(f"[EXIT-FINALIZED] con_id={con_id} order_id={inf.order_id} "
                  f"{status}, filled_qty={qty} @ {fill_px:.4f} (durable, deduped)")
            return True
        except Exception as e:
            print(f"[WARN] in-flight finalization failed for con_id={con_id}: {e}; retaining state")
            return False

    async def _poll_in_flight_fills(self, live_open_orders: Dict[int, dict],
                                    live_positions: Optional[Dict[int, object]] = None) -> Set[int]:
        """Persist partial progress and finalize confirmed async/restart fills.

        Resting/cancelled orders are never mistaken for fills.  A full fill is removed only after
        its actual average price and P&L are durably committed exactly once.
        """
        changed: Set[int] = set()
        try:
            infs = dict(self.state_manager.state.in_flight)
            if not infs:
                return changed
            terminal = await self._terminal_trades_for_in_flight(infs)
            dirty = False
            for cid_str, inf in infs.items():
                try:
                    cid = int(cid_str)
                except (TypeError, ValueError):
                    continue
                live = live_open_orders.get(cid)
                if live is not None:
                    rem = live.get("remaining")
                    if isinstance(rem, (int, float)) and rem == rem and int(rem) >= 0:
                        rem = int(rem)
                        if rem != inf.remaining_qty:
                            inf.remaining_qty = rem
                            dirty = True
                trade = terminal.get(cid)
                if trade is None:
                    # A crash/exception can leave the fsynced pre-transmission intent without any
                    # broker-side order. Release it only after completed-order AND execution reads
                    # both succeeded, no live close exists, the original position quantity is
                    # unchanged, and a short grace period has elapsed. Any uncertainty retains the
                    # block (fail closed against a double-close).
                    if (getattr(inf, "placement_state", "submitted") == "intent"
                            and live is None and getattr(self, "_terminal_lookup_complete", False)
                            and live_positions is not None and cid in live_positions):
                        pos = live_positions[cid]
                        live_qty = (getattr(pos, "quantity", None)
                                    if not isinstance(pos, dict) else pos.get("qty"))
                        try:
                            unchanged = int(live_qty) == int(
                                (inf.exit_context or {}).get("position_qty")
                                or inf.remaining_qty)
                            placed = datetime.fromisoformat(
                                str(inf.placed_at).replace("Z", "+00:00"))
                            if placed.tzinfo is None:
                                placed = placed.replace(tzinfo=timezone.utc)
                            old_enough = ((datetime.now(timezone.utc) - placed.astimezone(timezone.utc))
                                          .total_seconds() >= 30)
                        except Exception:
                            unchanged = old_enough = False
                        if unchanged and old_enough:
                            print(f"[RECONCILE] Releasing untransmitted close intent for con_id={cid}; "
                                  "all broker history reads are clean and position is unchanged")
                            self.state_manager.state.remove_in_flight(cid)
                            dirty = True
                            changed.add(cid)
                    continue
                status = getattr(getattr(trade, "orderStatus", None), "status", None)
                if status == "Filled" or status in {"Cancelled", "ApiCancelled", "Inactive"}:
                    if self._finalize_in_flight_exit(cid, inf, trade):
                        changed.add(cid)
                        continue
                    # A context-rich terminal cancellation with ZERO fills is safe to release:
                    # the broker says this exact order is dead, so retaining it would suppress the
                    # next protective/manual retry forever. Partial terminal fills are finalized
                    # above; invalid/missing fill prices remain retained for a later broker read.
                    if (status in {"Cancelled", "ApiCancelled", "Inactive"}
                            and self._trade_reported_filled(trade) <= 0
                            and getattr(inf, "exit_context", None)):
                        ctx = dict(inf.exit_context or {})
                        import types as _types
                        trig = _types.SimpleNamespace(
                            trigger_type=ctx.get("trigger_type") or ctx.get("reason") or "exit")
                        extra = dict(ctx.get("extra") or {})
                        self._log_unfilled_exit(
                            cid, ctx.get("symbol") or "", trig, fill_status=status,
                            close_qty=int(ctx.get("close_qty") or inf.remaining_qty or 0),
                            trigger_mark=extra.get("trigger_mark"), bid=extra.get("bid"),
                            limit_price=extra.get("limit_price", inf.order_price),
                            order_id=inf.order_id, reason=ctx.get("reason"),
                            placed_at=inf.placed_at)
                        self.state_manager.state.remove_in_flight(cid)
                        dirty = True
                        changed.add(cid)
            if dirty:
                self.state_manager.save()
        except Exception as e:
            print(f"[WARN] in-flight fill poll errored (continuing): {e}")
        return changed

    async def _reconcile_on_startup(self) -> bool:
        """
        Reconcile state on startup.
        Returns True if safe to proceed, False if must abort.
        """
        print("[INFO] Starting reconciliation...")

        # Get live positions and open orders
        try:
            live_positions_raw = await self.ib_conn.get_positions()
            live_open_orders_raw = await self.ib_conn.get_open_orders(
                short_leg_con_ids=set(getattr(self, "_spread_short_legs", {}).keys()))
        except Exception as e:
            print(f"[ERROR] Could not fetch live data for reconciliation: {e}")
            return False

        # Convert to dicts expected by reconcile_state
        live_positions = {
            pd.con_id: {"qty": pd.quantity, "avg_cost": pd.avg_cost}
            for pd in live_positions_raw.values()
        }
        live_open_orders = {
            od.con_id: {"order_id": od.order_id, "remaining": od.remaining,
                        "perm_id": getattr(od, "perm_id", 0),
                        "client_id": getattr(od, "client_id", None),
                        "order_ref": getattr(od, "order_ref", None),
                        "status": getattr(od, "status", None)}
            for od in live_open_orders_raw.values()
        }

        # Build journal entries dict (con_id -> debit)
        journal_debits = {
            con_id: entry.get("debit", 0.0)
            for con_id, entry in self._journal_entries.items()
        }
        # Spread short legs are KNOWN positions -- include them so an over-covered short
        # residual is not treated as an "unexpected" position (the 6/29 double-close that
        # crash-looped the trader for ~3h).
        for _scid in getattr(self, "_spread_short_legs", {}):
            journal_debits.setdefault(int(_scid), 0.0)

        # journal_qtys (2026-07-03): con_id -> entered quantity, so reconcile can recognize the
        # CONSISTENT in-flight-close state (live position qty == journal qty - order remaining)
        # instead of aborting on it.
        journal_qtys = {}
        for con_id, entry in self._journal_entries.items():
            try:
                journal_qtys[int(con_id)] = int(entry.get("quantity"))
            except (TypeError, ValueError):
                pass

        # Reconcile
        from exitmgr.state import reconcile_state
        _detail: dict = {}
        safe, alerts = reconcile_state(
            self.state_manager.state,
            live_positions,
            live_open_orders,
            journal_debits,
            journal_qtys=journal_qtys,
            detail=_detail,
        )
        # C1a: record the SPECIFIC inconsistent con_ids so the cycle blocks ONLY those exits (a
        # single manual TWS position no longer withholds every automated stop). Empty set on a
        # clean reconcile -> nothing blocked.
        self._reconcile_bad_con_ids = set(_detail.get("inconsistent") or set())
        # C3: purge per-contract tracking + journal for anything fully closed this pass (reconcile
        # check-1). Keeps a re-entered conId from inheriting a stale peak.
        for _cc in (_detail.get("closed") or set()):
            self._clear_closed_position(_cc)

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
            self._post_reconcile_block_alert(alerts)
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
                # TAKE-PROFIT-AND-RELOAD (2026-07-03): let the CONTINUATION judgment be more than
                # price-only. Feed the ORIGINAL entry thesis + the entry-time technical_card
                # (captured at entry in the journal) + the running MFE so the model can weigh
                # whether to reload on continued strength vs. bank-and-stop into exhaustion.
                "thesis": je.get("thesis"),
                "entry_technical_card": je.get("technical_card"),
                "mfe_pct": self.state_manager.state.mfe_pct.get(str(cid)),
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
            # TAKE-PROFIT-AND-RELOAD (2026-07-03): carry a model reload signal onto the take_profit
            # trigger. It does NOT change the exit at all -- the close fires exactly as before; the
            # flag is only read AFTER the close CONFIRMS Filled to write a fill-gated reload ticket.
            # reload applies to take_profit ONLY (a model_cut never reloads a broken thesis).
            _reload = bool(decision.get("reload")) if action == "take_profit" else False
            _reload_conv = decision.get("reload_conviction") if _reload else None
            return rules, ExitTrigger(
                con_id=con_id, trigger_type=("take_profit" if action == "take_profit" else "model_cut"),
                current_price=current_price, entry_debit=entry_debit, current_value=current_value,
                pnl_pct=pnl, message=(f"model {action}: {reason}" if reason else f"model {action}"),
                reload=_reload, reload_conviction=_reload_conv)
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
            # PART 1 (2026-07-03): DURABLY mark this position trail-armed so the take-profit CEILING
            # stays suppressed on SUBSEQUENT cycles (incl. plain 'hold'). Persisted with state (saved
            # at cycle end), so it survives a process bounce. Best-effort: a state hiccup here must
            # never break the decision path. Gates ONLY the take-profit side; never the stop.
            try:
                self.state_manager.state.trail_armed[str(con_id)] = True
            except Exception:
                pass
            return replace(rules, trailing=replace(tc, enabled=True, activation_gain_pct=new_act, giveback_fraction=new_gb)), None
        if action == "tighten_stop":
            ns = decision.get("stop_pct")
            if ns is not None and float(ns) > 0:
                cur = rules.stop_pct  # tighter = smaller pct (smaller max loss); only reduce
                new_stop = min(float(ns), cur) if cur is not None else float(ns)
                return replace(rules, stop_pct=new_stop), None
        return rules, None

    @staticmethod
    def _reconcile_ceiling_backstop(rules, decision, armed=False):
        """MODEL JUDGMENT = PRIMARY, TIER CEILING = BACKSTOP (2026-07-03; PERSISTED 2026-07-03 P1).
        Suppress the fixed pot-tier profit_target whenever a trailing stop is ARMED for this
        position -- either the model says arm_trail THIS cycle OR it armed one on a PRIOR cycle
        (`armed`, read from persisted state.trail_armed). Without the persistent check a model that
        armed a trail and then returned 'hold' would let the ceiling snap back and force-close (clip)
        exactly the runner the trail was meant to let RUN. While armed, the armed TRAILING STOP (+
        the untouched airtight 30% stop) govern the exit. When NO trail is armed (hold / take_profit
        / cut / never-armed / no model response) the ceiling is UNCHANGED and still fires as the
        entry-default backstop. Only relaxes take-profit -- NEVER the stop."""
        _arm = bool(armed) or bool(decision and decision.get("action") == "arm_trail")
        if _arm and rules.profit_target_pct is not None:
            from dataclasses import replace as _rp_pt
            return _rp_pt(rules, profit_target_pct=None)
        return rules

    @staticmethod
    def _apply_auto_trail(rules, auto_cfg, peak_price, entry_debit, quantity):
        """PART 2 (2026-07-03) GAIN-PROTECTING AUTO-TRAIL SAFETY FLOOR. Independently of the model
        AND of the global rules.trailing toggle, once a winner's PEAK gain clears
        auto_trail.activation_gain_pct ensure a WIDE protective trailing stop is active for this
        eval so the gain can't round-trip to breakeven when the model returns 'hold'. WIDEN-ONLY: it
        enables the trail and takes the ROOMIER (larger) giveback of {config, auto} so it never
        chokes a runner the config would have let run, and lowers the activation only enough to keep
        it armed -- it never RAISES an existing activation or tightens below what config/model set.
        It rides the monotonic peak UP (peak_price is monotonic, so the protected floor only
        ratchets up). It NEVER suppresses the take-profit ceiling (that is arm_trail's job) and NEVER
        touches the protective stop. No-op when disabled, no peak, or below activation. Returns
        (rules, armed_now: bool)."""
        from dataclasses import replace as _rp_at
        if auto_cfg is None or not getattr(auto_cfg, "enabled", False):
            return rules, False
        try:
            q = int(quantity)
            ed = float(entry_debit)
            pk = float(peak_price)
        except (TypeError, ValueError):
            return rules, False
        if peak_price is None or q <= 0 or ed <= 0:
            return rules, False
        entry_per_share = ed / (100.0 * q)
        if entry_per_share <= 0:
            return rules, False
        peak_gain_pct = (pk / entry_per_share - 1.0) * 100.0
        if peak_gain_pct < float(auto_cfg.activation_gain_pct):
            return rules, False
        tc = rules.trailing
        auto_gb = float(auto_cfg.giveback_fraction)
        if tc.enabled:
            # widen-only: keep the ROOMIER giveback; keep it armed without raising the activation
            new_gb = max(float(tc.giveback_fraction), auto_gb)
            new_act = min(float(tc.activation_gain_pct), float(auto_cfg.activation_gain_pct))
        else:
            new_gb = auto_gb
            new_act = float(auto_cfg.activation_gain_pct)
        new_gb = max(0.1, min(0.9, new_gb))
        return _rp_at(rules, trailing=_rp_at(tc, enabled=True,
                                             activation_gain_pct=new_act,
                                             giveback_fraction=new_gb)), True

    def _maybe_write_reload_ticket(self, con_id, symbol, trigger, quantity, entry_debit,
                                   fill_px, fill_status, *, fill_key=None, je=None) -> bool:
        """FILL-GATED reload hand-off (2026-07-03). Write a persisted reload ticket IFF the feature
        is enabled, this is a model take_profit that signalled reload=true, and the close CONFIRMED
        Filled. NEVER writes on a resting/rejected close -- that is the double-exposure guard: a
        second same-name spread must not stack on a slot still mid-exit. Best-effort + fully wrapped;
        a bug here can never raise into or alter the exit path."""
        try:
            if not bool(getattr(self.config, "reload_enabled", False)):
                return True
            if getattr(trigger, "trigger_type", None) != "take_profit":
                return True
            if not bool(getattr(trigger, "reload", False)):
                return True
            # DOUBLE-EXPOSURE GUARD: only a CONFIRMED Filled close is a real vacated slot. A None
            # fill_status (unobservable / mocked) or any non-Filled status writes NOTHING.
            if fill_status != "Filled":
                return True
            from exitmgr import reload_queue as _rq
            # The realized-exit commit can clear the live journal before this handoff.  Prefer the
            # frozen entry snapshot from exit_context so reload construction never loses thesis,
            # right, width, or DTE on a restart replay.
            je = dict(je or self._journal_entries.get(con_id) or {})
            sp = je.get("spread") or {}
            _px = fill_px if fill_px is not None else getattr(trigger, "current_price", None)
            realized = None
            try:
                if _px is not None and entry_debit is not None:
                    realized = float(_px) * 100 * int(quantity) - float(entry_debit)
            except (TypeError, ValueError):
                realized = None
            _INDEX = {"SPY", "QQQ", "IWM", "DIA"}
            ticket = _rq.make_ticket(
                symbol=symbol,
                thesis=je.get("thesis") or "",
                right=je.get("right"),
                width=sp.get("width"),
                dte_target=je.get("dte_at_entry"),
                structure=("spread" if sp else "single"),
                is_index=(str(symbol).upper() in _INDEX),
                reload_conviction=getattr(trigger, "reload_conviction", None),
                realized_pnl=realized,
                original_debit=je.get("debit") or entry_debit,
                ttl_cycles=int(getattr(self.config, "reload_ttl_cycles", 3) or 3),
                interval_seconds=int(getattr(self.config.loop, "interval_seconds", 60) or 60),
                source_fill_key=fill_key,
            )
            created = _rq.ReloadQueue(_rq.queue_path(self.config.journal.path)).add_once(ticket)
            print(f"[RELOAD] {'wrote' if created else 'deduped'} fill-gated reload ticket for "
                  f"{symbol} (reload_conviction={ticket.get('reload_conviction')}, "
                  f"realized={realized})")
            return True
        except Exception as e:
            print(f"[WARN] reload-ticket write skipped for con_id={con_id} ({symbol}): {e} (continuing)")
            return False

    async def _capture_external_fills_safe(self) -> None:
        """Best-effort per-cycle capture of MANUAL/external IBKR fills into trade_dataset.jsonl
        via exec_capture.capture_external_fills (READ-ONLY reqExecutions). Reuses this cycle's
        live IB handle -- no extra socket/clientId. RECORD-ONLY + DEFENSIVE: swallows every
        exception so a capture bug can NEVER raise into, block, or alter the exit path.
        Throttled to once per ~15 min so it does not re-pull executions every fast loop."""
        try:
            import time as _t
            now = _t.time()
            last = getattr(self, "_last_extfill_capture", 0.0)
            if now - last < 900:  # 15 min throttle
                return
            self._last_extfill_capture = now
            ib = getattr(self.ib_conn, "ib", None)
            if ib is None or not self.ib_conn.is_healthy():
                return
            from exitmgr import exec_capture
            summary = await exec_capture.capture_external_fills(
                ib=ib, config=self.config, lookback_days=7)
            if summary.get("appended") or summary.get("positions_appended"):
                print(f"[EXT-FILL] captured manual fills: trades+={summary.get('appended')} "
                      f"positions+={summary.get('positions_appended')} "
                      f"manual_fills={summary.get('manual_fills')}")
        except Exception as e:
            try:
                print(f"[WARN] external-fill capture skipped: {e}")
            except Exception:
                pass

    async def run_cycle(self, dry_run: bool, regime=None, price_stats=None, defer_model: bool = False) -> None:
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

        # EXTERNAL-FILL CAPTURE (2026-07-03): fold EVERY IBKR account execution -- including
        # Trevor's MANUAL / direct-in-TWS trades -- into the training dataset. READ-ONLY
        # (reqExecutions + commissionReport); reuses this cycle's live IB handle; NEVER places
        # an order. Best-effort + never raises into the exit path.
        await self._capture_external_fills_safe()

        # Manual early-exits (2026-06-18): con_ids the user one-tapped "sell" on in the book review.
        # Force-closed below (before any mechanical stop) via the same close path -> single executor.
        manual_exit_ids = self._read_manual_exits()
        if manual_exit_ids:
            print(f"[CYCLE] manual early-exit requests: {sorted(manual_exit_ids)}")

        # ENTRY kill switch status is informational here.  Exits are risk-reducing and must remain
        # live while new entries are halted.
        if self._check_kill_switch():
            print("[CYCLE] Entry kill switch active - continuing exit management")

        # Check caps
        caps_ok, cap_reason = self._check_caps(dry_run)
        if not caps_ok:
            print(f"[CYCLE] Caps exceeded - {cap_reason}. Skipping order placement.")

        # PER-CYCLE RECONCILE (2026-07-03): re-run the startup safety reconcile EVERY cycle, not
        # just at process start. A mid-session order-state divergence (the 6/29 double-close /
        # naked-short residual that crash-looped the trader ~3h) otherwise went unseen until a
        # restart. Gates ORDER PLACEMENT only (like caps / kill switch): evaluation + mark logging
        # still run, and the Trader reads self._reconcile_ok to also suppress ENTRIES. A reconcile
        # that can't fetch or finds an inconsistency is treated as UNSAFE -> no new orders.
        try:
            reconcile_ok = await self._reconcile_on_startup()
        except Exception as _re:
            print(f"[CYCLE] per-cycle reconcile errored ({_re}); treating as UNSAFE (no new orders)")
            reconcile_ok = False
            # C1a: a reconcile that could not run at all yields NO per-con_id detail -> block ALL
            # exits this cycle (fail-safe), not just a specific set.
            self._reconcile_bad_con_ids = None
        self._reconcile_ok = reconcile_ok
        if not reconcile_ok:
            print("[CYCLE] reconciliation UNSAFE - clean positions still get stops; only "
                  "reconcile-inconsistent con_ids are withheld (entries suppressed globally)")

        # Get live positions
        try:
            live_positions = await self.ib_conn.get_positions()
        except Exception as e:
            print(f"[ERROR] Could not fetch positions: {e}")
            return

        # Fill-integrity alarm (2026-07-01): runs EVERY cycle, before the managed-position
        # early-return -- an unfilled ENTRY means there is no position to manage yet, and an
        # unfilled EXIT must escalate each cycle, not go quiet.
        try:
            await self._alert_unfilled_orders()
        except Exception as e:
            print(f"[WARN] unfilled-order alarm errored (continuing): {e}")

        # TERMINAL-STATE (2026-07-03): a journaled position PAST expiry AND gone from live
        # positions expired (worthless / auto-exercised) -- emit its 'expired' close row so the
        # arc is never invisible to the dataset. Runs before prune (mark path still intact) and
        # before the managed-position early-return (an expired position is not "managed").
        try:
            await self._process_expiries(live_positions)
        except Exception as e:
            print(f"[WARN] expiry terminal-logging errored (continuing): {e}")

        # RECORD-ONLY housekeeping: drop excursion/mark tracking for contracts no longer active
        # (not live at the broker AND not journaled) so state.json / mark paths stay bounded
        # across closed trades. A closed trade's dataset record is written at close while the
        # position is still live, so this never drops path data we still need. Conservative
        # (keeps journaled con_ids) so a transient empty positions read can't wipe live tracking.
        try:
            _active = set(live_positions.keys()) | set(self._journal_entries.keys())
            self.state_manager.state.prune_tracking(_active)
        except Exception as e:
            print(f"[WARN] tracking prune errored (continuing): {e}")

        # FINALIZE BEFORE FILTER/EARLY-RETURN.  In-flight positions are intentionally excluded from
        # ``managed_positions``; polling later meant a book containing only a filled/pending close
        # returned "No positions to evaluate" forever and never finalized it.  Read all-client
        # orders here, finalize confirmed fills (including completed orders after restart), then
        # build the management set from the updated durable state.
        try:
            live_open_orders_raw = await self.ib_conn.get_open_orders(
                short_leg_con_ids=set(getattr(self, "_spread_short_legs", {}).keys()))
            live_open_orders = {
                od.con_id: {"order_id": od.order_id, "remaining": od.remaining,
                            "perm_id": getattr(od, "perm_id", 0),
                            "client_id": getattr(od, "client_id", None),
                            "order_ref": getattr(od, "order_ref", None),
                            "status": getattr(od, "status", None)}
                for od in live_open_orders_raw.values()
            }
        except Exception as e:
            print(f"[ERROR] Could not fetch open orders: {e}")
            live_open_orders = {}
        terminal_changes = await self._poll_in_flight_fills(
            live_open_orders, live_positions=live_positions)
        if terminal_changes:
            # The snapshots above predate the fills we just finalized.  Continuing with them can
            # size a second SELL from the old quantity (or sell a now-flat contract).  Refresh both
            # sides of the broker book and fail closed if either read is unavailable.
            try:
                live_positions = await self.ib_conn.get_positions()
                live_open_orders_raw = await self.ib_conn.get_open_orders(
                    short_leg_con_ids=set(getattr(self, "_spread_short_legs", {}).keys()))
                live_open_orders = {
                    od.con_id: {"order_id": od.order_id, "remaining": od.remaining,
                                "perm_id": getattr(od, "perm_id", 0),
                                "client_id": getattr(od, "client_id", None),
                                "order_ref": getattr(od, "order_ref", None),
                                "status": getattr(od, "status", None)}
                    for od in live_open_orders_raw.values()
                }
                print(f"[CYCLE] Refreshed broker snapshots after terminal changes for "
                      f"con_ids={sorted(terminal_changes)}")
            except Exception as e:
                print(f"[ERROR] Could not refresh positions/orders after terminal fill: {e}; "
                      "skipping further close placement this cycle")
                return
        # The poll may have completed a queued one-tap exit and atomically removed its request.
        manual_exit_ids = self._read_manual_exits()

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
            if in_flight is not None:
                print(f"[CYCLE] con_id={con_id} already has durable close state "
                      f"(order_id={in_flight.order_id}, remaining={in_flight.remaining_qty}), skipping")
                continue

            managed_positions.append(pos_data)

        if not managed_positions:
            print("[CYCLE] No positions to evaluate")
            self.state_manager.update_last_cycle()
            return
        # MANUAL EARLY-EXITS (force MARKET close, no quote needed): process here, BEFORE the
        # quote-gated eval loop, so an unpriceable option leg can still be sold on a one-tap request.
        manual_processed_ids: Set[int] = set()
        if manual_exit_ids and not dry_run:
            try:
                _loo_raw = await self.ib_conn.get_open_orders(
                short_leg_con_ids=set(getattr(self, "_spread_short_legs", {}).keys()))
                _loo = {
                    od.con_id: {"order_id": od.order_id, "remaining": od.remaining,
                                "perm_id": getattr(od, "perm_id", 0),
                                "client_id": getattr(od, "client_id", None),
                                "order_ref": getattr(od, "order_ref", None),
                                "status": getattr(od, "status", None)}
                    for od in _loo_raw.values()
                }
            except Exception:
                _loo = {}
            for _p in managed_positions:
                cid = _p.con_id
                if cid not in manual_exit_ids:
                    continue
                je = self._journal_entries.get(cid) or {}
                sym = je.get("symbol", _p.symbol)
                qty = min(_p.quantity, je.get("quantity", _p.quantity))
                full_basis = je.get("entry_fill_debit") or je.get(
                    "debit", _p.avg_cost * 100 * _p.quantity)
                # A prior terminal partial fill leaves the manual request pending.  On retry the
                # live quantity is only the remainder, so allocate only that fraction of the
                # original journal basis; reusing the full debit double-counted cost and corrupted
                # realized P&L across the two fills.
                try:
                    journal_qty = int(je.get("quantity") or _p.quantity)
                    edebit = float(full_basis) * int(qty) / journal_qty if journal_qty else float(full_basis)
                except (TypeError, ValueError, ZeroDivisionError):
                    edebit = full_basis
                _manual_ctx = {
                    "symbol": sym,
                    "reason": "manual",
                    "trigger_type": "manual",
                    "trigger_message": "early exit via book-review one-tap (market)",
                    "trigger_pnl_pct": 0.0,
                    "close_qty": qty,
                    "position_qty": qty,
                    "entry_debit": edebit,
                    "journal_entry": dict(je),
                    "manual_request": True,
                    "extra": {
                        "partial": False,
                        "close_qty": qty,
                        "remaining_qty": 0,
                        "rule_fired": "manual",
                        "exit_reasoning": "early exit via book-review one-tap (market)",
                        "mfe_pct": self.state_manager.state.mfe_pct.get(str(cid)),
                    },
                }
                try:
                    res = await self.order_manager.place_close_order(
                        con_id=cid, symbol=sym, quantity=qty, limit_price=0.0, entry_debit=edebit,
                        live_open_orders=_loo, spread=je.get("spread"), market=True,
                        right=je.get("right"), trigger_type="manual",
                        exit_context=_manual_ctx)
                    if res.success:
                        # Whether it filled immediately or is still working, this contract has
                        # already consumed its one close action for the cycle.  Exclude it from the
                        # later static/model evaluation even if finalization removes in_flight.
                        manual_processed_ids.add(cid)
                        _loo[cid] = {"order_id": res.order_id, "remaining": qty}
                        # Placement is not the outcome.  Keep manual_exits.json pending and let the
                        # durable in-flight finalizer clear it only after IBKR says Filled.
                        _inf = self.state_manager.state.get_in_flight(cid)
                        _done = bool(_inf is not None and res.trade is not None
                                     and self._finalize_in_flight_exit(cid, _inf, res.trade))
                        print(f"[MANUAL-EXIT] {sym} con_id={cid} MARKET close "
                              f"{'Filled/finalized' if _done else 'placed; request remains pending'}")
                    else:
                        self._manual_exit_fail(sym, cid, res.message)
                except Exception as _e:
                    self._manual_exit_fail(sym, cid, repr(_e))

        if manual_processed_ids:
            managed_positions = [p for p in managed_positions
                                 if p.con_id not in manual_processed_ids]
            if not managed_positions:
                print("[CYCLE] All managed positions were handled by manual exits this cycle")
                self.state_manager.update_last_cycle()
                return

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

        # Model-driven position management: one LLM call per cycle assesses every open
        # position and returns per-con_id decisions (arm/tighten trail, tighten stop,
        # take-profit, cut), applied below with monotonic guardrails. {} -> static rules.
        model_decisions = {}
        # DATASET CAPTURE (2026-07-03, ADDITIVE): per-cycle model-reasoning artifacts recorded onto
        # each mark for fine-tuning. mgmt_raw = the full raw model response this cycle; views_by_cid
        # = the exact per-position view fed to the model (its input). Both default empty so the
        # enrich block below is safe when management is off / deferred / errored.
        mgmt_raw = None
        mgmt_identity = None
        views_by_cid = {}
        # FLAT-SKIP (2026-06-23): only spend a model call when there is actually something to manage.
        # managed_positions is already non-empty here (we early-returned above on a flat book), but
        # guard explicitly so a future refactor cannot reintroduce a flat-account model call -- that
        # was the common case that collided with the daily slate on the single-threaded M3 server.
        # SOFT-MUTEX (2026-06-23): when the daily slate is mid-generation the trader passes
        # defer_model=True; we SKIP the (non-urgent) exit-management model call this tick and
        # fall through to static rules, picking it up next tick. Static stops/targets/trails
        # still run -- only the model TUNING is deferred -- so exits are never left blind.
        if defer_model and getattr(self.config, "manage_positions", False) and managed_positions:
            print("[POSMGMT] slate active -- deferring model assessment this cycle (static rules apply)")
        if getattr(self.config, "manage_positions", False) and managed_positions and not defer_model:
            try:
                views = self._build_position_views(managed_positions, quotes, self._price_stats)
                views_by_cid = {v.get("con_id"): v for v in views}
                # OFF-LOOP (2026-07-03): assess_positions makes a BLOCKING HTTP call to the model
                # server; running it inline stalls the asyncio event loop (and thus the IBKR
                # heartbeat / any concurrent exit I/O) for the whole model latency. Run it in a
                # worker thread so the loop stays responsive.
                model_decisions, _mgmt_meta = await asyncio.to_thread(
                    assess_positions,
                    self.config.llm_endpoint, self.config.llm_model,
                    views, market_regime=self._regime, return_meta=True)
                mgmt_raw = (_mgmt_meta or {}).get("raw")
                mgmt_identity = (_mgmt_meta or {}).get("model_identity")
                if model_decisions:
                    rg = (self._regime or {}).get("regime", "n/a")
                    print(f"[POSMGMT] regime={rg} model decisions for con_ids {sorted(model_decisions)}")
            except Exception as e:
                print(f"[POSMGMT] assessment skipped ({e}); using static rules")
                model_decisions = {}

        # IBKR server-side portfolio marking (authoritative; same source position_monitor uses).
        # Streaming option quotes go stale/crossed for illiquid spreads (esp. near close) and have
        # hidden real losses (a -47% spread read +25% -> stop never fired, 2026-06-26).
        try:
            # NaN GUARD (2026-07-03): a NaN marketPrice passes the `is not None` filter, then
            # current_price = _mark.get(con_id) becomes NaN and EVERY comparison (>= profit target,
            # <= stop) is False -> the stop silently never fires (a -47% spread that read +25% never
            # stopped, 2026-06-26). Require a real, positive mark (px == px rejects NaN); a bad mark
            # is simply omitted so the code falls back to the streaming quote for that contract.
            _mark = {p.contract.conId: p.marketPrice for p in self.ib_conn.ib.portfolio()
                     if p.position != 0 and p.marketPrice is not None
                     and p.marketPrice == p.marketPrice and p.marketPrice > 0}
        except Exception as _e:
            print(f"[WARN] portfolio marking unavailable ({_e}); using streaming quotes")
            _mark = {}

        # C1d (2026-07-09): protective exits withheld THIS cycle because their con_id is
        # reconcile-inconsistent -> one UNTHROTTLED Slack alert after the loop.
        withheld_stops: List[tuple] = []
        for pos_data in managed_positions:
            con_id = pos_data.con_id
            quote = quotes.get(con_id)

            if quote is None:
                print(f"[WARN] No valid quote for con_id={con_id}, skipping")
                continue

            # PREFER IBKR marking over the (possibly stale) streaming quote.
            current_price = _mark.get(con_id, quote["price"])

            # Journaled spread: value the position as the NET (long - short) so the same
            # profit/stop/time rules apply to the spread as a unit. Never evaluate on the
            # long leg alone -- and never close it alone (see place_close_order spread path).
            spread = (self._journal_entries.get(con_id) or {}).get("spread")
            if spread and spread.get("short_con_id"):
                scid = int(spread["short_con_id"])
                short_px = _mark.get(scid)
                if short_px is None:
                    short_quote = quotes.get(scid)
                    if short_quote is None:
                        print(f"[WARN] No price (mark or quote) for spread short leg of con_id={con_id}, skipping")
                        continue
                    short_px = short_quote["price"]
                current_price = current_price - short_px

            # Get entry debit from journal (or estimate from avg_cost if not in journal)
            if con_id in self._journal_entries:
                journal_debit = self._journal_entries[con_id].get("debit", 0.0)
                # C2b (2026-07-09): anchor the rules engine (and, via the pro-rated basis passed to
                # _log_exit below, realized P&L) to the REAL entry fill debit when we have it -- the
                # journal already records entry_fill_debit / basis_source but nothing consumed it, so
                # every % threshold (profit target / stop / trail) ran off the ESTIMATED debit. Prefer
                # the fill basis when basis_source=="fill" or an entry_fill_debit is present; fall back
                # to the estimate otherwise. Numeric-guarded so a bad value never displaces the estimate.
                _jefd = self._journal_entries[con_id].get("entry_fill_debit")
                _jbs = self._journal_entries[con_id].get("basis_source")
                if _jefd is not None and (_jbs == "fill" or _jefd is not None):
                    try:
                        _jefd_f = float(_jefd)
                        if _jefd_f == _jefd_f and _jefd_f > 0:
                            journal_debit = _jefd_f
                    except (TypeError, ValueError):
                        pass
                symbol = self._journal_entries[con_id].get("symbol", pos_data.symbol)
                quantity_in_journal = self._journal_entries[con_id].get("quantity", pos_data.quantity)
                # Use minimum of position qty and journal qty (in case of partial close)
                quantity = min(pos_data.quantity, quantity_in_journal)
                # SCALE-OUT PRO-RATING (2026-07-02): the journaled `debit` is the FULL entry cost for
                # `quantity_in_journal` contracts. After a partial scale-out trim, the runner we still
                # manage has fewer contracts (`quantity`) but the SAME journaled debit -- so the rule
                # engine (profit target / stop / trail) must value it against a basis PRO-RATED to the
                # contracts actually held, or its per-share entry (and thus every % threshold) is off
                # by the trim ratio and it exits on the wrong basis. No-op for an untrimmed full
                # position (quantity == quantity_in_journal); also self-corrects any external partial.
                if quantity_in_journal and quantity_in_journal > 0 and quantity != quantity_in_journal:
                    entry_debit = journal_debit * quantity / quantity_in_journal
                else:
                    entry_debit = journal_debit
            else:
                # Estimate from avg_cost (avg_cost is per share -> already scales with held qty)
                entry_debit = pos_data.avg_cost * 100 * pos_data.quantity
                symbol = pos_data.symbol
                quantity = pos_data.quantity
                quantity_in_journal = pos_data.quantity

            # Record this mark-to-market: peak price + MFE (max favorable %) AND MAE (max
            # ADVERSE %) excursions + the mark-path time series, all PERSISTED with the state
            # so a process bounce never loses the running excursions/path. RECORD-ONLY
            # (2026-07-02) -- MAE is the field the exits corpus never had, MFE was previously
            # reconstructed synthetically. `peaks`/`k` stay defined for downstream eval.
            peaks = self.state_manager.state.peak_prices
            k = str(con_id)
            # Real days-to-expiry from the position's option contract
            dte = days_to_expiry(getattr(pos_data, "expiry", ""))
            # PATH ENRICHMENT (v2, 2026-07-02) -- record-only per-cycle context on the mark:
            # greeks/IV from the streamed quote, DTE + days-held, distance-to-TP / distance-to-SL,
            # and ANY position-management LLM assessment produced this cycle (action + reasoning).
            # Wrapped so a capture bug can never affect the mark or the exit decision.
            _enrich = None
            try:
                _je_e = self._journal_entries.get(con_id) or {}
                _q_e = quotes.get(con_id) or {}
                _dec_e = model_decisions.get(con_id) or {}
                # HOLD-BY-OMISSION (2026-07-03): the model's schema treats any OPEN position it
                # OMITS from "decisions" as an implicit hold (rules unchanged) -- those silent holds
                # previously left NO assessment record, so they were invisible to the dataset. When
                # the model actually ran this cycle (mgmt_raw present) and this position was in the
                # view set but got no explicit decision, record a lightweight implicit-hold so every
                # managed position has an assessment row. NOT fabricated: if the model was down /
                # deferred (no raw), we record nothing. No effect on the exit decision (hold==no-op).
                _mgmt_action = _dec_e.get("action")
                _mgmt_reason = _dec_e.get("reason")
                if _mgmt_action is None and mgmt_raw and (con_id in views_by_cid):
                    _mgmt_action = "hold"
                    _mgmt_reason = "implicit hold (omitted from model decisions this cycle)"
                _pnl_e = ((current_price * 100 * quantity - entry_debit) / entry_debit * 100
                          if entry_debit and entry_debit > 0 else None)
                _tp_e = _je_e.get("profit_target_pct") or getattr(self.config.rules, "profit_target_pct", None)
                _sl_e = _je_e.get("stop_pct") or getattr(self.config.rules, "stop_pct", None)
                _days_held = None
                _ets = _je_e.get("ts")
                if _ets:
                    try:
                        _et = datetime.fromisoformat(str(_ets).replace("Z", "+00:00"))
                        if _et.tzinfo is None:
                            from datetime import timezone as _tz
                            _et = _et.replace(tzinfo=_tz.utc)
                        _days_held = round((datetime.now().astimezone() - _et).total_seconds() / 86400.0, 3)
                    except Exception:
                        _days_held = None
                _und = None
                try:
                    _und = ((self._price_stats or {}).get(symbol) or {}).get("last")
                except Exception:
                    _und = None
                _enrich = {
                    "underlying": _und,
                    "iv": _q_e.get("iv"), "delta": _q_e.get("delta"),
                    "gamma": _q_e.get("gamma"), "theta": _q_e.get("theta"), "vega": _q_e.get("vega"),
                    "dte": dte, "days_held": _days_held,
                    "dist_to_tp_pct": (round(float(_tp_e) - _pnl_e, 2)
                                       if (_tp_e is not None and _pnl_e is not None) else None),
                    "dist_to_sl_pct": (round(_pnl_e + abs(float(_sl_e)), 2)
                                       if (_sl_e is not None and _pnl_e is not None) else None),
                    "mgmt_action": _mgmt_action,
                    "mgmt_reason": _mgmt_reason,
                    # FULL per-cycle model reasoning for the fine-tuning corpus (2026-07-03,
                    # ADDITIVE, record-only). mgmt_raw = the complete raw model response this cycle;
                    # mgmt_input = the exact per-position view fed to the model. record_mark merges
                    # only non-None keys, so both are simply absent on cycles with no assessment.
                    "mgmt_raw": mgmt_raw,
                    "mgmt_model_identity": mgmt_identity,
                    "mgmt_input": views_by_cid.get(con_id),
                    # P1.2 audit: for a journaled debit spread, current_price above is the NET combo
                    # mark (long - short, computed at the top of this loop), NOT a single leg -- flag
                    # it so the recorded mark path is unambiguously the net value the rules saw.
                    "is_net_spread": bool(spread and spread.get("short_con_id")),
                }
            except Exception as _ee:
                print(f"[WARN] mark enrichment build failed for con_id={con_id} (recording plain mark): {_ee}")
                _enrich = None
            self.state_manager.state.record_mark(
                con_id, current_price, entry_debit, quantity, path_cap=self.MARK_PATH_CAP,
                enrich=_enrich)
            # Per-position sell levels the model recommended (journaled); fall back to the global
            # config rule when a level wasn't specified for this trade.
            rules = self.config.rules
            je = self._journal_entries.get(con_id) or {}
            if je.get("profit_target_pct") or je.get("stop_pct"):
                from dataclasses import replace
                rules = replace(self.config.rules,
                                profit_target_pct=je.get("profit_target_pct") or self.config.rules.profit_target_pct,
                                stop_pct=je.get("stop_pct") or self.config.rules.stop_pct)
            # AIRTIGHT STOP BACKSTOP (2026-07-03): a protective stop must NEVER lapse for an open
            # position. The stop is stamped at entry (journal stop_pct) and falls back to the config
            # rule above; this backstop closes the last gap -- if BOTH are missing/<=0 (bare config,
            # a pre-stop legacy fill, a corrupt journal line) the position would otherwise run with
            # NO stop. Force the constructor-default magnitude so every open trade is always stopped.
            # This only raises a stop into existence; it never loosens an existing one, and the TP
            # tiering never touches it.
            if rules.stop_pct is None or float(rules.stop_pct) <= 0:
                from dataclasses import replace as _rp_stop
                rules = _rp_stop(rules, stop_pct=_STOP_BACKSTOP_PCT)
            # Apply the model's per-position decision (monotonic guardrails). A take_profit/cut
            # forces an immediate exit this cycle; otherwise the (possibly tuned) rules drive eval.
            forced = None
            decision = model_decisions.get(con_id)
            if decision and decision.get("action", "hold") != "hold":
                rules, forced = self._apply_decision(
                    rules, decision, current_price, entry_debit, quantity, con_id, symbol,
                    regime=self._regime)
            # MODEL JUDGMENT = PRIMARY, TIER CEILING = BACKSTOP (2026-07-03, per Trevor: "adopt my
            # STRATEGIES not my JUDGMENTS -- let winners run to a level IT sees worthy of selling").
            # The pot-tiered tp_max_pct ceiling is stamped onto the journal profit_target_pct at
            # ENTRY; without this, evaluate_position's fixed profit_target (priority 1) would
            # FORCE-CLOSE a winner the model just chose to LET RUN (arm_trail), overriding its
            # judgment. When the model actively says arm_trail THIS cycle, suppress the fixed target
            # for this eval so the armed TRAILING STOP (+ the untouched airtight 30% stop) govern the
            # exit instead -- the winner runs until IT decides to bank it. The tier ceiling remains
            # the ENTRY default and still fires as a BACKSTOP on any hold / no-model-response cycle.
            # NOTE: this ONLY relaxes the take-profit ceiling; the protective stop is never loosened.
            rules = self._reconcile_ceiling_backstop(
                rules, decision,
                armed=(str(con_id) in self.state_manager.state.trail_armed))
            # PART 2 (2026-07-03) GAIN-PROTECTING AUTO-TRAIL SAFETY FLOOR. Independently of the model
            # and of the global rules.trailing toggle, guarantee a WIDE protective trailing stop for
            # any winner whose PEAK gain has cleared auto_trail.activation_gain_pct, so an up-big
            # winner can't round-trip to breakeven when the model returns 'hold'. Widen-only; NEVER
            # suppresses the ceiling and NEVER touches the stop; rides the monotonic peak UP.
            rules, _auto_armed = self._apply_auto_trail(
                rules, getattr(self.config.rules, "auto_trail", None),
                peaks.get(k), entry_debit, quantity)
            if _auto_armed:
                print(f"[EVAL] con_id={con_id} ({symbol}): auto-trail safety floor active "
                      f"(keep {(1 - rules.trailing.giveback_fraction):.0%} of peak gain)")
            # SCALE-OUT: has this position already had its partial trim? Persisted in state so the
            # scale_out rule fires at most once and never re-trims the runner.
            already_trimmed = str(con_id) in self.state_manager.state.scaled_out
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
                    already_trimmed=already_trimmed,
                )

            if trigger:
                trigger.con_id = con_id
                triggers.append(trigger)
                # SCALE-OUT: how many contracts this trigger closes. A full/risk exit carries
                # quantity_fraction>=1.0 -> close the WHOLE position (unchanged historical behavior).
                # A partial scale_out closes round(quantity*fraction), clamped to leave at least one
                # runner AND close at least one contract. is_partial <=> a scale_out trim here.
                _qf = getattr(trigger, "quantity_fraction", 1.0) or 1.0
                if _qf >= 1.0:
                    close_qty = quantity
                else:
                    close_qty = max(1, min(quantity - 1, round(quantity * _qf)))
                is_partial = close_qty < quantity
                print(f"[EVAL] con_id={con_id} ({symbol}): {trigger.message} (pnl={trigger.pnl_pct:.2f}%)"
                      + (f" [SCALE-OUT trim {close_qty}/{quantity}, keep {quantity - close_qty} runner]"
                         if is_partial else ""))

                # Place order if armed and this con_id is not reconcile-inconsistent.
                # C1a (2026-07-09): gate PER-CON_ID, not globally -- a clean position still gets its
                #   stop even when ANOTHER position is inconsistent. _bad is None => reconcile could
                #   not run at all => block everything (fail-safe).
                # C1c (2026-07-09): a PROTECTIVE exit (stop/TP/time-stop) is exempt from the daily
                #   order/notional caps -- caps bound risk-ADDING activity, never a protective close.
                _bad = self._reconcile_bad_con_ids
                _reconcile_blocked = (_bad is None) or (con_id in _bad)
                _protective = self._is_protective_exit(trigger)
                # C1d: a PROTECTIVE close held back by reconcile-inconsistency is an active-risk
                # event -> collect for an UNTHROTTLED post-cycle alert.  The entry kill switch is
                # deliberately irrelevant to exits.
                if _protective and _reconcile_blocked and not dry_run:
                    withheld_stops.append((symbol, con_id, trigger.trigger_type))
                if (not dry_run and (caps_ok or _protective) and not _reconcile_blocked):
                    # ALL order/notional caps apply only to optional exits (currently scale_out).
                    # A stop, target, trailing stop, model cut, or time exit is risk-reducing and
                    # bypasses daily AND per-cycle ceilings.  Do not ``break`` on an optional trim's
                    # cap: a later position in the loop may need a protective exit.
                    if (not _protective
                            and orders_placed_this_cycle >= self.config.caps.max_orders_per_cycle):
                        print(f"[CYCLE] Per-cycle order cap reached: {orders_placed_this_cycle} >= {self.config.caps.max_orders_per_cycle}")
                        continue

                    # Check per-cycle notional cap (approximate) -- on the CLOSED qty, not full
                    order_notional = current_price * 100 * close_qty
                    if (not _protective and orders_notional_this_cycle + order_notional
                            > self.config.caps.max_notional_per_day):
                        print(f"[CYCLE] Would exceed daily notional cap, skipping order for con_id={con_id}")
                        continue

                    # TIME-STOP REWORK (2026-07-01 audit): the old DTE<=3 MKT dump force-sold a
                    # direction-RIGHT winner that expired at full value 3 days later. At the new
                    # DTE<=10 evaluation, a GREEN position -- or one the model still holds the
                    # thesis on -- gets a MANAGED exit (LIMIT at the current mark) instead of a
                    # dump-at-any-price MKT order. Red + thesis-broken still goes MKT so it always
                    # fills; the unfilled-order alarm escalates any resting managed exit.
                    market_flag = getattr(self.config.rules, "exit_market_orders", False)
                    if trigger.trigger_type == "time_stop":
                        _dec = model_decisions.get(con_id) or {}
                        _thesis_intact = _dec.get("action", "hold") in ("hold", "arm_trail")
                        if trigger.pnl_pct >= 0 or _thesis_intact:
                            market_flag = False
                            print(f"[CYCLE] time-stop MANAGED exit for {symbol} "
                                  f"(green={trigger.pnl_pct >= 0}, thesis_intact={_thesis_intact}) -> LIMIT at mark")

                    # Place order (close_qty: full for a risk/target exit, the trim for a scale_out)
                    # BID WIRING (2026-07-03): thread the single-leg bid + trigger_type so a
                    # TRIGGERED exit rests a MARKETABLE LIMIT at the bid (fills on wide books)
                    # instead of falling back to MARKET. NEVER bid-anchor a SPREAD -- a per-leg
                    # bid != the net combo price -- so pass bid=None for combos (order.py's
                    # _eff_bid guards this too). A missing/NaN/<=0 bid -> None so order.py keeps
                    # its guaranteed-fill fallback.
                    _is_spread = bool(spread and spread.get("short_con_id"))
                    _raw_bid = None if _is_spread else (quote or {}).get("bid")
                    _close_bid = (_raw_bid
                                  if (_raw_bid is not None and _raw_bid == _raw_bid and _raw_bid > 0)
                                  else None)
                    # Persist everything required to turn a LATER fill into a complete realized
                    # record.  This snapshot is committed with InFlightClose by OrderManager.
                    # The basis is pro-rated to the contracts this order closes.
                    closed_basis = (entry_debit if close_qty == quantity
                                    else (entry_debit * close_qty / quantity
                                          if quantity else entry_debit))
                    _q = quotes.get(con_id) or {}
                    _exit_ctx = {
                        "symbol": symbol,
                        "reason": self._exit_reason(trigger),
                        "trigger_type": getattr(trigger, "trigger_type", None),
                        "trigger_message": getattr(trigger, "message", None),
                        "trigger_pnl_pct": getattr(trigger, "pnl_pct", None),
                        "reload": bool(getattr(trigger, "reload", False)),
                        "reload_conviction": getattr(trigger, "reload_conviction", None),
                        "close_qty": close_qty,
                        "position_qty": quantity,
                        "entry_debit": closed_basis,
                        "journal_entry": dict(je),
                        "decision_id": je.get("decision_id"),
                        "entry_model_identity": je.get("model_identity"),
                        "exit_model_identity": mgmt_identity,
                        "manual_request": False,
                        "extra": {
                            "partial": is_partial,
                            "close_qty": close_qty,
                            "remaining_qty": quantity - close_qty,
                            "underlying_price": ((_enrich or {}).get("underlying")
                                                 if isinstance(_enrich, dict) else None),
                            "iv": _q.get("iv"), "delta": _q.get("delta"),
                            "gamma": _q.get("gamma"), "theta": _q.get("theta"),
                            "vega": _q.get("vega"),
                            "trigger_mark": current_price,
                            "bid": _close_bid,
                            "limit_price": current_price,
                            "rule_fired": getattr(trigger, "trigger_type", None),
                            "exit_reasoning": getattr(trigger, "message", None),
                            "mfe_pct": self.state_manager.state.mfe_pct.get(str(con_id)),
                        },
                    }
                    result = await self.order_manager.place_close_order(
                        con_id=con_id,
                        symbol=symbol,
                        quantity=close_qty,
                        limit_price=current_price,  # Limit at mid (net mid for spreads)
                        entry_debit=closed_basis,
                        live_open_orders=live_open_orders,
                        spread=spread,
                        market=market_flag,
                        right=je.get("right"),
                        bid=_close_bid,
                        trigger_type=trigger.trigger_type,
                        exit_context=_exit_ctx,
                    )

                    if result.success:
                        orders_placed_this_cycle += 1
                        orders_notional_this_cycle += order_notional
                        # Update live_open_orders for next iteration
                        live_open_orders[con_id] = {"order_id": result.order_id, "remaining": close_qty}
                        # OrderManager normally persisted this atomically before returning.  Keep a
                        # defensive backstop for mocked/legacy implementations: no successful
                        # placement may exist without durable finalization context.
                        _inf = self.state_manager.state.get_in_flight(con_id)
                        if _inf is None:
                            from exitmgr.state import InFlightClose
                            _result_client = self._ib_client_id(
                                getattr(result, "client_id", None))
                            _result_perm = int(getattr(result, "perm_id", 0) or 0)
                            _result_ref = getattr(result, "order_ref", None)
                            _inf = InFlightClose(
                                con_id=con_id, order_id=int(result.order_id or 0),
                                remaining_qty=close_qty, entry_debit=closed_basis,
                                order_price=current_price,
                                placed_at=datetime.now(timezone.utc).isoformat(),
                                exit_context=_exit_ctx,
                                perm_id=_result_perm,
                                client_id=_result_client,
                                order_ref=_result_ref,
                                identity_version=(1 if (_result_perm or _result_ref
                                                        or _result_client is not None) else 0),
                                placement_state="submitted")
                            self.state_manager.state.add_in_flight(_inf)
                            self.state_manager.save()

                        # Give an immediate fill a short chance to resolve, but placement is never
                        # logged as a realized exit.  A resting order remains in-flight and the same
                        # finalizer runs on a later cycle/restart with the broker's actual fill.
                        _tr = getattr(result, "trade", None)
                        if _tr is not None:
                            try:
                                for _ in range(12):  # up to ~6s
                                    _status_now = getattr(getattr(_tr, "orderStatus", None),
                                                          "status", None)
                                    if _status_now in ("Filled", "Cancelled", "ApiCancelled", "Inactive"):
                                        break
                                    await asyncio.sleep(0.5)
                            except Exception as _fe:
                                print(f"[WARN] fill capture failed for con_id={con_id}: {_fe}")
                        _filled_now = bool(_tr is not None
                                           and self._finalize_in_flight_exit(con_id, _inf, _tr))
                        if not _filled_now:
                            _st = getattr(getattr(_tr, "orderStatus", None), "status", None)
                            if isinstance(_st, str):
                                self._log_unfilled_exit(
                                    con_id, symbol, trigger, fill_status=_st,
                                    close_qty=close_qty, trigger_mark=current_price,
                                    bid=_close_bid, limit_price=current_price,
                                    order_id=result.order_id,
                                    reason=self._exit_reason(trigger),
                                    placed_at=getattr(_inf, "placed_at", None))
                            print(f"[EXIT-PENDING] {symbol} con_id={con_id} order_id={result.order_id} "
                                  f"status={_st or 'unknown'}; durable context retained")
                    elif result.trade is not None:
                        # NON-FILL LOGGING (2026-07-03): a TERMINAL reject/cancel at placement.
                        # order.py returns success=False WITH the trade object ONLY on a real
                        # reject (Cancelled/ApiCancelled/Inactive); an idempotency SKIP (a prior
                        # close still working) returns trade=None and is excluded here -- that
                        # prior order logs its own row, so this branch can never double-count.
                        # A resting (Submitted/PreSubmitted, success=True) exit is already logged
                        # as unfilled by the fill-verification _log_exit path above; this ADDS the
                        # only remaining blind spot -- the rejected/cancelled exit that otherwise
                        # produced NO row -- so the fill-rate denominator sees the too-tight exit.
                        try:
                            _nfs = None
                            _ost = getattr(result.trade, "orderStatus", None)
                            if _ost is not None:
                                _nfs = getattr(_ost, "status", None)
                            _nfs = _nfs if isinstance(_nfs, str) else "Cancelled"
                            self._log_unfilled_exit(
                                con_id, symbol, trigger, fill_status=_nfs,
                                close_qty=close_qty, trigger_mark=current_price,
                                bid=_close_bid, limit_price=current_price,
                                order_id=result.order_id, reason=self._exit_reason(trigger),
                                placed_at=datetime.now().astimezone().isoformat())
                        except Exception as _ue:
                            print(f"[WARN] non-fill logging failed for con_id={con_id}: "
                                  f"{_ue} (continuing)")
            else:
                # Log evaluation (no trigger)
                pnl_pct = (current_price * 100 * quantity - entry_debit) / entry_debit * 100 if entry_debit > 0 else 0
                print(f"[EVAL] con_id={con_id} ({symbol}): no trigger (price={current_price:.4f}, pnl={pnl_pct:.2f}%)")

        # C1d: fire the UNTHROTTLED stops-withheld alert if any protective exit was held back this
        # cycle by reconcile-inconsistency (a truly active-risk condition, unlike the throttled block).
        if withheld_stops:
            print(f"[CYCLE] PROTECTIVE EXITS WITHHELD (reconcile-inconsistent con_ids): "
                  f"{[(s, c) for s, c, _ in withheld_stops]}")
            try:
                self._post_stops_withheld_alert(withheld_stops)
            except Exception as _wa:
                print(f"[WARN] stops-withheld alert errored (continuing): {_wa}")
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
