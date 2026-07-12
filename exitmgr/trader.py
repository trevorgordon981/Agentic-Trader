"""Orchestrator for the LLM trading system.

Flow each cycle:  live pot -> day-start baseline (circuit breaker) -> market context ->
strategist proposes -> risk gate filters -> RESOLVE the concrete option order -> Slack
approve-each (showing the EXACT order) -> submit exactly what was approved -> exit manager.

Hard invariants:
  * Nothing is submitted unless dry_run is OFF (--arm) AND an explicit approval came back.
  * The approval message shows the resolved order (strike/expiry/qty/limit) -- you approve the
    real order, not just the idea.
  * Every proposal, gate decision, resolution, approval, and fill is appended to the audit log.
"""
import asyncio
import json
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set

from exitmgr.account import get_pot_snapshot
from exitmgr.risk import (
    RiskLimits, OpenPosition, ProposedTrade, GateDecision, evaluate_trade, day_pnl_pct,
)
from exitmgr.strategist import propose, TradeIdea
from exitmgr import (approval, construction, research, regime, slate_lock, trade_capture,
                     reload_queue, model_release_gate)
from exitmgr import entry_safety
from dataclasses import replace as _replace_dc
from exitmgr.config import ConstructionConfig


# ---------------------------------------------------------------- pure helpers (unit-tested)

def audit(path: str, event: str, **fields) -> dict:
    rec = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(rec, default=str) + "\n")
    return rec


def _trading_day(now=None) -> str:
    """US/Eastern calendar date -- the trading day the circuit-breaker baseline is keyed to.
    Using the exchange timezone (not UTC) means the day-start baseline rolls over exactly once
    per session, in the gap between the prior close and the next open, and NEVER mid-RTH. The old
    UTC-date key mislabeled the 20:00-00:00 UTC window (8pm-ET -> midnight-ET) as the NEXT day,
    so the breaker's reference pot could be captured from a stale prior-evening read."""
    try:
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        n = now or datetime.now(et)
        if getattr(n, "tzinfo", None) is None:
            n = n.replace(tzinfo=timezone.utc)
        return str(n.astimezone(et).date())
    except Exception:
        return str((now or datetime.now(timezone.utc)).date())


def day_start_pot(baselines: Dict[str, float], today: str, current_net_liq: float):
    """Return (day_start_baseline, updated_baselines). The baseline is the pot value at the START
    of the trading day `today`; it gates the -8% daily circuit breaker (the solvency backstop), so
    it must roll over exactly once per day and can NEVER go stale or be poisoned by a garbage read.

    HARDENING (P3.9 audit, 2026-07-02):
      * A bad net-liq read (None / NaN / <=0) never SETS or overwrites a baseline -- a transient
        $0 from IBKR would otherwise make day_pnl_pct explode or silently disable the breaker.
      * The baseline is STICKY within the day (only set when `today` is absent), so the pot at the
        first GOOD read of the session stays the reference all day -- it can't drift.
      * A fresh day drops stale prior days, so the store can't grow unbounded.
    Failure is SAFE: with no valid baseline yet, day_pnl_pct sees pot_day_start<=0 and returns 0%,
    so the breaker neither fabricates a halt nor suppresses one on bogus data."""
    b = dict(baselines or {})
    valid = isinstance(current_net_liq, (int, float)) and current_net_liq == current_net_liq \
        and current_net_liq > 0
    if today not in b:
        if not valid:
            return (b.get(today, 0.0) or 0.0), b   # don't invent a baseline from a bad read
        b = {today: float(current_net_liq)}        # fresh day -> drop stale prior days
    return b[today], b


def _market_open() -> bool:
    """US regular session ~13:30-20:00 UTC, Mon-Fri (ignores holidays; gateway maint handled
    elsewhere). When closed, the trader skips the strategist/model call — no trade can be
    entered, so there's nothing to propose."""
    t = datetime.now(timezone.utc)
    if t.weekday() >= 5:  # Sat/Sun
        return False
    mins = t.hour * 60 + t.minute
    return 13 * 60 + 30 <= mins <= 20 * 60


@dataclass
class Plan:
    idea: TradeIdea
    trade: ProposedTrade
    gate: GateDecision
    action: str


def plan_idea(idea: TradeIdea, *, net_liq: float, available_funds: float,
              positions: List[OpenPosition], baseline: float,
              approved_names: Set[str], limits: RiskLimits, regime=None) -> Plan:
    trade = ProposedTrade(idea.underlying, idea.est_debit_usd, idea.is_index, idea.conviction,
                          is_long=(getattr(idea, "direction", "bullish") == "bullish"),
                          profit_target_pct=getattr(idea, "profit_target_pct", 0.0) or 0.0,
                          stop_pct=getattr(idea, "stop_pct", 0.0) or 0.0)
    gate = evaluate_trade(
        trade, net_liq=net_liq, available_funds=available_funds,
        open_positions=positions, pot_day_start=baseline,
        approved_names=approved_names, limits=limits, regime_info=regime,
    )
    return Plan(idea, trade, gate, "needs_approval" if gate.approved else "gate_rejected")


@dataclass
class ResolvedOrder:
    """The concrete order, resolved from an idea BEFORE approval, so the human approves it."""
    underlying: str
    right: str          # "C" | "P"
    expiry: str         # YYYYMMDD
    strike: float       # long leg
    qty: int
    limit: float        # per-share debit: the premium, or the NET debit for spreads
    contract: object = None         # qualified IB contract (long leg), for submission
    short_strike: float = 0.0       # debit spread sold leg; 0 = single-leg order
    short_contract: object = None
    conviction: float = -1.0        # entry conviction (1-10) carried through for the journal; -1 = unknown
    thesis: str = ""                # strategist entry thesis, carried through for the durable journal
    # 2026-07-01 constructor rework: construction/enrichment facts carried into the journal.
    tp_pct: float = 0.0             # clamped take-profit % (25-35 band; 0 = global default rule)
    sl_pct: float = 0.0             # clamped stop % (default 30; 0 = global default rule)
    spot: float = 0.0               # underlying price at construction (0 = unknown)
    entry_delta: float = 0.0        # long-leg delta at construction (0 = unknown)
    entry_iv: float = 0.0           # long-leg implied vol at construction (0 = unknown)
    dte: int = 0                    # DTE of the chosen expiry at construction
    dte_adjusted: bool = False      # True when the expiry was ADJUSTED to satisfy the min-DTE floor
    # FULL GREEKS + LIQUIDITY at construction (v2 dataset, record-only; 0/None = unavailable from
    # the feed). Net greeks == long-leg greeks for a single; spread net greeks need the short leg's
    # greeks (not retained) so they stay None for spreads.
    entry_gamma: float = 0.0
    entry_theta: float = 0.0
    entry_vega: float = 0.0
    entry_ivr: float = 0.0          # IV rank/percentile if available (0 = unknown)
    entry_bid: float = 0.0
    entry_ask: float = 0.0
    entry_spread_pct: float = 0.0   # (ask-bid)/mid -- a bid/ask liquidity measure
    net_delta: float = 0.0
    net_theta: float = 0.0
    net_gamma: float = 0.0
    net_vega: float = 0.0
    quote_observed_at: float = 0.0  # monotonic timestamp of the reqTickersAsync result
    decision_id: str = ""          # immutable proposal -> order -> fill -> close lineage
    decision_revision: int = 0
    model_identity: Optional[dict] = None


def order_summary(r: ResolvedOrder) -> str:
    if r.short_contract is not None:
        width = abs(r.short_strike - r.strike)
        return (f"BUY {r.qty}x {r.underlying} {r.expiry} {r.strike:g}/{r.short_strike:g}{r.right} "
                f"debit spread @ ~${r.limit:.2f} (marketable limit after fresh NBBO)  "
                f"(max loss ~${r.limit * 100 * r.qty:,.0f}, max value ${width * 100 * r.qty:,.0f})")
    return (f"BUY {r.qty}x {r.underlying} {r.expiry} {r.strike:g}{r.right} "
            f"@ ~${r.limit:.2f} (marketable limit after fresh NBBO)  (~${r.limit * 100 * r.qty:,.0f})")


def contract_snapshot(r: ResolvedOrder) -> dict:
    """JSON-safe exact terms approved/submitted for one decision revision."""
    return {
        "underlying": r.underlying, "right": r.right, "expiry": r.expiry,
        "long_con_id": getattr(r.contract, "conId", None), "long_strike": r.strike,
        "short_con_id": getattr(r.short_contract, "conId", None),
        "short_strike": (r.short_strike or None), "quantity": r.qty,
        "limit": r.limit, "max_loss_usd": round(r.limit * 100 * r.qty, 2),
        "quote_observed_at": r.quote_observed_at,
    }


def pick_spread_short(candidates, long_strike: float, long_mid: float, right: str,
                      per_trade_cap: float, *, spot=None, dte=None, atm_iv=None, cons=None):
    """Choose the sold leg of a debit vertical: the WIDEST further-OTM strike whose net debit
    (long_mid - short_mid, for one spread) still fits the per-trade cap. candidates =
    [(strike, mid), ...]. Returns (short_strike, net_debit_per_share) or None.
    STRUCTURE SANITY (2026-07-01, keyword-only so old callers/tests are unchanged): when
    `cons` (ConstructionConfig) is given, strikes failing spread_structure_ok -- short leg
    beyond ~1 expected move of spot (or the conservative width fallback) -- are SKIPPED, so
    a lottery vertical (the NOK 14/25 on a $13.62 stock) can never be constructed."""
    otm = [(k, m) for k, m in candidates
           if m is not None and m == m and m > 0
           and ((right == "C" and k > long_strike) or (right == "P" and k < long_strike))]
    otm.sort(key=lambda km: abs(km[0] - long_strike), reverse=True)  # widest first
    for k, m in otm:
        if cons is not None:
            ok, _why = construction.spread_structure_ok(long_strike, k, spot, right, dte, atm_iv, cons)
            if not ok:
                continue
        net = long_mid - m
        if net <= 0.01:
            continue
        if net * 100 <= per_trade_cap + 1e-6:
            return k, round(net, 2)
    return None


def size_within_cap(unit_cost: float, budget: float, per_trade_cap: float) -> Optional[int]:
    """Contracts to buy so the order fits BOTH the idea budget and the per-trade $ cap.

    HARD-REJECT (2026-07-03): returns None -- reject the trade -- when even ONE contract exceeds the
    per-trade cap, instead of the old `max(1, ...)` that force-clamped qty to 1 and shipped an order
    OVER the risk cap. `unit_cost` is the per-contract cost (mid*100 for a single, net*100 for a
    spread). Pure + unit-tested."""
    if unit_cost <= 0:
        return None
    qty = int(min(budget, per_trade_cap) // unit_cost)
    if qty < 1:
        return None   # a single contract already exceeds the cap -> reject, never clamp to 1
    if unit_cost * qty > per_trade_cap + 1e-6:
        qty -= 1
    return qty if qty >= 1 else None


# ---------------------------------------------------------------- orchestrator (I/O)

class Trader:
    def __init__(self, *, ib_conn, exit_manager, limits: RiskLimits, approved_names: Set[str],
                 endpoint: str, model: str, slack_token: str, slack_channel: str,
                 approver_ids: Set[str], baseline_path: str, audit_path: str,
                 approve_timeout_s: int = 900, journal_path: str = "./trades.log",
                 blocked_sector_keywords: Optional[List[str]] = None,
                 entry_limit_buffer_pct: float = 0.05,
                 construction_cfg: Optional[ConstructionConfig] = None,
                 caps_tp_tiers: Optional[List[dict]] = None,
                 kill_switch_path: Optional[str] = None,
                 config_path: str = "config.yaml",
                 trading_down_path: Optional[str] = None,
                 broker_order_lock=None,
                 max_orders_per_cycle: Optional[int] = None,
                 max_orders_per_day: Optional[int] = None,
                 max_notional_per_day: Optional[float] = None,
                 reload_enabled: bool = False,
                 reload_conviction_min: float = 6,
                 reload_friction_k: float = 1.5,
                 reload_max_per_name_per_day: int = 2,
                 reload_ttl_cycles: int = 3,
                 model_release_gate_settings: Optional[
                     model_release_gate.ModelReleaseGateSettings] = None):
        self.ib_conn = ib_conn
        # KILL SWITCH (2026-07-03): the manager's kill switch only gated EXITS; the trader never
        # checked it, so entries kept flowing under a kill switch. When configured (run_trader.py
        # passes cfg.kill_switch.path) an existing file HALTS new entries. None (bare Trader/tests)
        # -> never active.
        self.kill_switch_path = kill_switch_path or "./KILL_SWITCH"
        self.config_path = config_path
        self.trading_down_path = trading_down_path
        self.broker_order_lock = broker_order_lock
        # EXIT-CYCLE FAILURE STREAK (2026-07-03): consecutive run_cycle failures. After
        # _EXIT_FAIL_SUPPRESS_ENTRIES in a row, new entries are suppressed (a broken exit path must
        # not be compounded by opening MORE positions we then can't manage).
        self._exit_fail_streak = 0
        self.exit_manager = exit_manager
        self.limits = limits
        self.approved_names = {n.upper() for n in approved_names}
        self.endpoint, self.model = endpoint, model
        self.slack_token, self.slack_channel = slack_token, slack_channel
        self.approver_ids = approver_ids
        self.baseline_path, self.audit_path = baseline_path, audit_path
        self.approve_timeout_s = approve_timeout_s
        self.journal_path = journal_path
        self.blocked_sector_keywords = [k for k in (blocked_sector_keywords or []) if k.strip()]
        self.entry_limit_buffer_pct = float(entry_limit_buffer_pct)
        # 2026-07-01 constructor-rework thresholds (min DTE / TP-SL clamp / structure sanity /
        # budget gates); defaults apply when the config has no `construction:` section.
        self.construction = construction_cfg or ConstructionConfig()
        # POT-TIERED TP RUNNER CEILING rows (caps.tp_tiers). None/empty => flat no-op (today's
        # behavior). Scales ONLY the take-profit ceiling at ENTRY; never the protective stop.
        self.caps_tp_tiers = list(caps_tp_tiers or [])
        # ENTRY THROTTLE CEILINGS (2026-07-03 gap-fix). caps.max_orders_per_cycle / _per_day /
        # notional_per_day were loaded but only enforced on the EXIT path; enforce them on NEW
        # entries too. None => that ceiling is disabled (bare Trader / older tests keep prior
        # behavior). These are CEILINGS that only ADD safety -- they never up-size or force a trade.
        self.max_orders_per_cycle = max_orders_per_cycle
        self.max_orders_per_day = max_orders_per_day
        self.max_notional_per_day = max_notional_per_day
        # TAKE-PROFIT-AND-RELOAD (2026-07-03). OFF BY DEFAULT: reload_enabled=False => the drain is a
        # pure no-op and behavior is byte-identical to today. When enabled, ready reload tickets
        # (written by the exit manager ONLY after a Filled take-profit close) are drained each cycle
        # into synthetic same-name suggestions that flow through the SAME gate/construct/approve/
        # submit path as strategist ideas. reload_conviction_min + reload_friction_k gate churn;
        # reload_max_per_name_per_day + reload_ttl_cycles bound anti-churn. NEVER auto-fires -- every
        # reload is a normal human-approved suggestion.
        self.reload_enabled = bool(reload_enabled)
        self.reload_conviction_min = float(reload_conviction_min)
        self.reload_friction_k = float(reload_friction_k)
        self.reload_max_per_name_per_day = int(reload_max_per_name_per_day)
        self.reload_ttl_cycles = int(reload_ttl_cycles)
        # OFF unless an explicit, strictly parsed config block is supplied.  When
        # enabled, the final BUY seam re-proves the signed v3 promotion and exact
        # active custom-Python runtime immediately before placeOrder.
        self.model_release_gate_settings = (
            model_release_gate_settings or model_release_gate.ModelReleaseGateSettings())
        # market regime + per-underlying momentum, refreshed each cycle in _market_context;
        # feeds both regime-aware sizing (entries) and the position manager (exits)
        self._regime = None
        self._price_stats = {}

    # After this many consecutive exit-cycle failures, stop opening NEW entries.
    _EXIT_FAIL_SUPPRESS_ENTRIES = 3

    def _kill_switch_active(self) -> bool:
        """True if the KILL_SWITCH file exists (halts ENTRIES; exits are gated by the manager).
        No path configured (bare Trader / tests) -> never active. Never raises."""
        try:
            return bool(self.kill_switch_path) and Path(self.kill_switch_path).exists()
        except Exception:
            return False

    def _entry_markers_clear(self) -> entry_safety.SafetyResult:
        return entry_safety.entry_markers_clear(
            config_path=self.config_path,
            kill_switch_path=self.kill_switch_path,
            trading_down_path=self.trading_down_path)

    def _load_baselines(self) -> Dict[str, float]:
        p = Path(self.baseline_path)
        if p.exists():
            try:
                return json.loads(p.read_text())
            except json.JSONDecodeError:
                return {}
        return {}

    def _save_baselines(self, b: Dict[str, float]) -> None:
        # Atomic replacement: a crash during a direct write must not erase the daily-loss baseline
        # and silently disarm the circuit breaker.
        path = Path(self.baseline_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(b))
        tmp.replace(path)

    def _load_journal_debits(self) -> Dict[int, float]:
        """Map long-leg contract_id -> journaled NET debit (max loss) for every entry in
        trades.log. The journal records `debit` = limit*100*qty, i.e. the net debit paid for a
        spread (or the premium for a single leg) -- the TRUE capital at risk for these
        defined-risk structures. The newest entry per contract_id wins (re-entries)."""
        debits: Dict[int, float] = {}
        p = Path(self.journal_path)
        if not p.exists():
            return debits
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            cid = rec.get("contract_id")
            d = rec.get("debit")
            if cid is None or d is None:
                continue
            try:
                debits[int(cid)] = float(d)
            except (TypeError, ValueError):
                continue
        return debits

    async def _open_positions(self) -> List[OpenPosition]:
        raw = await self.ib_conn.get_positions()
        journal_debits = self._load_journal_debits()
        out = []
        for pd in raw.values():
            is_index = pd.symbol.upper() in {"SPY", "QQQ", "IWM"}
            # EXPOSURE = MAX LOSS for these defined-risk structures (long calls/puts + debit
            # spreads): the NET debit paid, not the long-leg notional. get_positions() returns
            # ONLY the long leg of a spread (short leg filtered), so avg_cost*100*qty is the GROSS
            # long-leg value -- e.g. a NOK 14C/25C spread (qty2, net debit ~$152) reads as ~$16,140
            # off the deep-ITM long 14C alone. That bogus ~100x number then blows the single-name
            # aggregate cap and gates every idea. Use the journaled net debit keyed by the long
            # leg's contract_id; fall back to the gross formula ONLY when there's no journal entry
            # (conservative -- never silently 0), and log it so the blow-up can't hide.
            con_id = getattr(pd, "con_id", None)
            gross = abs(pd.avg_cost) * 100 * abs(pd.quantity)
            net_debit = journal_debits.get(int(con_id)) if con_id is not None else None
            if net_debit is not None:
                notional = float(net_debit)
            else:
                notional = gross
                audit(self.audit_path, "exposure_no_journal_debit",
                      symbol=pd.symbol.upper(), con_id=con_id, fallback_gross=round(gross, 2),
                      note="no journaled net debit; using gross long-leg value (conservative)")
            out.append(OpenPosition(pd.symbol.upper(), notional, is_index))

        # FOLD RESTING ENTRY BUYS (2026-07-03): get_positions() returns FILLED positions only, so a
        # BUY entry we placed that is still resting/unfilled is INVISIBLE to the concurrent-count and
        # aggregate-exposure caps -> the gate could wave through an additional over-concentrating or
        # over-limit entry while the first is still working. Count live open BUY orders (all
        # clientIds) as pseudo-positions in the exposure book. Best-effort: a read failure just
        # leaves the filled-only book (never blocks the cycle).
        try:
            existing_cids = set(raw.keys())
            open_trades = await self.ib_conn.ib.reqAllOpenOrdersAsync()
            for t in open_trades or []:
                o = getattr(t, "order", None)
                c = getattr(t, "contract", None)
                st = getattr(getattr(t, "orderStatus", None), "status", None)
                if o is None or c is None or getattr(o, "action", "") != "BUY":
                    continue
                if st in {"Cancelled", "ApiCancelled", "Inactive", "Filled"}:
                    continue
                sym = (getattr(c, "symbol", "") or "").upper()
                if not sym:
                    continue
                cid = getattr(c, "conId", None)
                if cid is not None and int(cid) in existing_cids:
                    continue  # already counted as a filled position
                is_index = sym in {"SPY", "QQQ", "IWM"}
                nd = journal_debits.get(int(cid)) if cid is not None else None
                if nd is None:
                    lp = getattr(o, "lmtPrice", None)
                    q = getattr(o, "totalQuantity", 0) or 0
                    nd = (float(lp) * 100 * q) if (isinstance(lp, (int, float)) and lp == lp and lp > 0) else 0.0
                out.append(OpenPosition(sym, float(nd), is_index))
                audit(self.audit_path, "resting_entry_folded", symbol=sym, con_id=cid,
                      notional=round(float(nd), 2))
        except Exception as _oe:
            audit(self.audit_path, "open_buy_fold_error", error=str(_oe))
        return out

    async def _underlyings_with_close_in_flight(self) -> Set[str]:
        """Uppercased underlyings that currently have an IN-FLIGHT or RESTING close (SELL-to-close)
        working. Used by GUARDRAIL 2 to DEFER a NEW entry into a name whose exit is still settling,
        so we never stack a second spread on a slot that is mid-close (transient double exposure).
        Two independent sources, unioned:
          (1) RESTING SELL/close orders on the book across ALL clientIds -- via the same
              reqAllOpenOrdersAsync() machinery _open_positions folds resting BUYs with; the
              underlying comes straight off the order's contract.symbol (a spread BAG carries the
              underlying symbol too).
          (2) StateManager in-flight closes (keyed by con_id) -- mapped to an underlying via the
              live position book (con_id -> symbol). A close still in-flight means the position is
              still (partly) open, so it is present in that book.
        Best-effort: any read failure just yields the names we DID find (never raises into the entry
        path). A miss can only FAIL TO DEFER an entry -- it can never loosen a real risk gate."""
        names: Set[str] = set()
        # (1) resting SELL/close orders -> underlying via contract.symbol
        try:
            trades = await self.ib_conn.ib.reqAllOpenOrdersAsync()
            for t in trades or []:
                o = getattr(t, "order", None)
                c = getattr(t, "contract", None)
                st = getattr(getattr(t, "orderStatus", None), "status", None)
                if o is None or c is None or getattr(o, "action", "") != "SELL":
                    continue
                if st in {"Cancelled", "ApiCancelled", "Inactive", "Filled"}:
                    continue
                sym = (getattr(c, "symbol", "") or "").upper()
                if sym:
                    names.add(sym)
        except Exception as _re:
            audit(self.audit_path, "close_inflight_orders_error", error=str(_re))
        # (2) StateManager in-flight closes -> underlying via the live position book (con_id->symbol)
        try:
            sm = getattr(self.exit_manager, "state_manager", None)
            inflight_cids = {int(k) for k in sm.state.in_flight.keys()} if sm is not None else set()
            if inflight_cids:
                raw = await self.ib_conn.get_positions()
                cid_to_sym: Dict[int, str] = {}
                for pd in raw.values():
                    cid = getattr(pd, "con_id", None)
                    if cid is not None:
                        cid_to_sym[int(cid)] = (getattr(pd, "symbol", "") or "").upper()
                for cid in inflight_cids:
                    sym = cid_to_sym.get(cid)
                    if sym:
                        names.add(sym)
                    # a con_id with no live position: the close likely already filled (reconcile
                    # pending) -> can't map a name; the resting-order scan above covers a still-working
                    # close, so skipping here is safe (conservative).
        except Exception as _se:
            audit(self.audit_path, "close_inflight_state_error", error=str(_se))
        return names

    async def _market_context(self, positions: Optional[List[OpenPosition]] = None,
                              day_pnl: Optional[float] = None) -> str:
        from exitmgr.market import fetch_universe_quotes, format_context
        names = sorted({"SPY", "QQQ", "IWM"} | self.approved_names)
        today = str(datetime.now(timezone.utc).date())
        try:
            quotes = await fetch_universe_quotes(self.ib_conn.ib, names)
        except Exception as e:
            audit(self.audit_path, "context_quote_error", error=str(e))
            quotes = {}
        # research brief (price structure / events / headlines); falls back to the bare
        # quote context if the research layer itself blows up
        try:
            single_names = sorted(self.approved_names
                                  | {p.underlying for p in positions or [] if not p.is_index})
            data = await research.gather(self.ib_conn.ib, names, single_names=single_names)
            # Classify the market regime from the index momentum + VIX gather() already fetched,
            # and keep the per-underlying momentum -- feeds regime-aware sizing + the position manager.
            ps = data.get("price_stats") or {}
            self._price_stats = ps
            self._regime = regime.classify_regime([ps.get("SPY"), ps.get("QQQ"), ps.get("IWM")], data.get("vix"))
            audit(self.audit_path, "regime", **(self._regime or {}))
            brief = research.build_brief(today=today, quotes=quotes, universe=names,
                                         allow_any_name=self.limits.allow_any_name,
                                         book=positions, day_pnl_pct=day_pnl, **data)
            audit(self.audit_path, "strategist_brief", brief=brief)
            return brief
        except Exception as e:
            audit(self.audit_path, "research_error", error=str(e))
            return format_context(quotes, names, today,
                                  allow_any_name=self.limits.allow_any_name)

    async def _drop_blocked_sectors(self, ideas):
        """Drop single-name ideas whose sector/industry matches a blocked keyword (e.g. biotech).
        Best-effort: a lookup failure lets the name through to human approval. Index ETFs and
        explicit-ticker blocks aren't handled here -- the risk gate's blocked_names covers those."""
        if not self.blocked_sector_keywords or not ideas:
            return ideas
        kept = []
        for idea in ideas:
            if idea.is_index:
                kept.append(idea)
                continue
            industry, sector = await asyncio.to_thread(research.sector_of, idea.underlying)
            if research.matches_blocked_sector(industry, sector, self.blocked_sector_keywords):
                audit(self.audit_path, "blocked_sector", underlying=idea.underlying,
                      industry=industry, sector=sector)
            else:
                kept.append(idea)
        return kept

    async def run_once(self, dry_run: bool, *, skip_exit_cycle: bool = False) -> None:
        # SOFT-MUTEX (2026-06-23): if the daily slate is mid-generation on the single-threaded
        # model server, DEFER this tick's exit-management model call so we don't collide/queue
        # behind it. Static stops/targets still run; the model tuning is picked up next tick.
        defer_model = slate_lock.slate_active()
        pot = await get_pot_snapshot(self.ib_conn.ib)
        today = _trading_day()   # US/Eastern trading day (P3.9): rolls between sessions, not mid-RTH
        baselines = self._load_baselines()
        baseline, baselines = day_start_pot(baselines, today, pot.net_liq)
        self._save_baselines(baselines)

        dp = day_pnl_pct(pot.net_liq, baseline)
        audit(self.audit_path, "cycle_start", net_liq=pot.net_liq, available=pot.available_funds,
              day_start=baseline, day_pnl_pct=round(dp, 4), dry_run=dry_run)
        if defer_model:
            audit(self.audit_path, "model_deferred", reason="slate_active")

        positions = await self._open_positions()
        context = await self._market_context(positions, dp)

        # EXITS-FIRST (2026-06-26 reliability fix): manage stops/targets on open positions
        # BEFORE the slow/failure-prone strategist+approval path, so a hung model call (30-min
        # propose timeout) or a pending Slack approval can never delay or skip a stop. The exit
        # cycle only needs positions + regime/price_stats (set by _market_context above) and
        # defer_model (set at top of run_once).
        if not skip_exit_cycle:
            try:
                await self.exit_manager.run_cycle(
                    dry_run, regime=self._regime, price_stats=self._price_stats,
                    defer_model=defer_model)
                self._exit_fail_streak = 0
            except Exception as e:
                self._exit_fail_streak += 1
                audit(self.audit_path, "exit_cycle_error", error=str(e), streak=self._exit_fail_streak)
                # NO SILENT FAILURE (2026-07-03): a failing exit cycle means positions may be going
                # unmanaged -- Slack it, and after N in a row suppress new entries (below) so we don't
                # pile on more positions we can't exit.
                try:
                    approval.post_proposal(self.slack_token, self.slack_channel,
                        f":warning: *Exit cycle FAILED* ({self._exit_fail_streak}x consecutive): {e}"
                        + (f"\n_New entries SUPPRESSED until exits recover._"
                           if self._exit_fail_streak >= self._EXIT_FAIL_SUPPRESS_ENTRIES else ""))
                except Exception as _se:
                    print(f"[WARN] exit-cycle-failure Slack alert failed: {_se}")

        # Market-closed guard (2026-06-28): skip the strategist/model call when the market is
        # closed — nothing can be entered, so there's nothing to propose; spares the single-
        # threaded model server a ~20k-token brief every 15 min on weekends/overnight. Exits-
        # first (above) still runs every tick; resumes automatically at the next open.
        # ENTRY HALT GATES (2026-07-03): halt NEW entries (exits already ran above) when
        #   * the KILL_SWITCH file is present (previously only gated exits);
        #   * the exit manager's most recent per-cycle reconcile was UNSAFE (don't open into an
        #     inconsistent book);
        #   * the exit cycle has failed N times in a row (don't pile on unmanageable positions).
        _entries_halted, _halt_reason = False, None
        _marker_gate = self._entry_markers_clear()
        if not _marker_gate.allowed:
            _entries_halted, _halt_reason = True, "; ".join(_marker_gate.reasons)
        elif getattr(self.exit_manager, "_reconcile_ok", True) is False:
            _entries_halted, _halt_reason = True, "reconcile_unsafe"
        elif self._exit_fail_streak >= self._EXIT_FAIL_SUPPRESS_ENTRIES:
            _entries_halted, _halt_reason = True, f"exit_cycle_failing_{self._exit_fail_streak}x"

        _raw_strategist = None      # verbatim model output, for decision/no-trade capture (v2)
        _cot = None                 # [m3cot] chain-of-thought (reasoning_content), separate from the answer
        _model_identity = None      # immutable artifact/runtime/prompt/request/context hashes
        if not _market_open():
            audit(self.audit_path, "strategist_skipped", reason="market_closed")
            ideas = []
        elif _entries_halted:
            audit(self.audit_path, "strategist_skipped", reason=_halt_reason)
            ideas = []
        else:
            try:
                # OFF-LOOP (2026-07-03): propose() is a BLOCKING HTTP call to the model server;
                # run it in a worker thread so the IBKR event loop stays responsive.
                _res = await asyncio.to_thread(
                    propose, self.endpoint, self.model, context,
                    return_cot=True, return_identity=True)
                # robust to all shapes: (ideas, raw, cot) 3-tuple, (ideas, raw) 2-tuple, or a
                # bare list (older/mocked propose). cot is optional/None-safe.
                if isinstance(_res, tuple) and len(_res) == 4:
                    ideas, _raw_strategist, _cot, _model_identity = _res
                elif isinstance(_res, tuple) and len(_res) == 3:
                    ideas, _raw_strategist, _cot = _res
                elif isinstance(_res, tuple) and len(_res) == 2:
                    ideas, _raw_strategist = _res
                else:
                    ideas = _res
            except Exception as e:
                audit(self.audit_path, "strategist_error", error=str(e))
                ideas = []
        audit(self.audit_path, "proposals", count=len(ideas))

        # TAKE-PROFIT-AND-RELOAD (2026-07-03): drain ready reload tickets into synthetic high-priority
        # same-name suggestions. OFF BY DEFAULT (reload_enabled=False => no-op). Sits AFTER the
        # kill-switch / reconcile-halt / exit-fail-streak entry gates (skipped when entries are halted)
        # and only when the market is open. The reload ideas are PREPENDED so they run first, and then
        # flow through EXACTLY the same _drop_blocked_sectors -> risk gate -> construct -> throttle ->
        # G1 fresh-book / G2 in-flight-defer -> Slack-approve -> submit path as strategist ideas (so
        # they can never bypass a cap or the human approval). Never raises into the trading path.
        if self.reload_enabled and not _entries_halted and _market_open():
            try:
                _reload_ideas = self._drain_reload_ideas(today)
            except Exception as _rde:
                audit(self.audit_path, "reload_drain_error", error=str(_rde))
                _reload_ideas = []
            if _reload_ideas:
                ideas = list(_reload_ideas) + list(ideas)
                audit(self.audit_path, "reload_ideas_drained", count=len(_reload_ideas),
                      symbols=[i.underlying for i in _reload_ideas])

        # RECORD-ONLY (v2): learn from PASSES too -- when the model proposes nothing (market
        # closed or an empty/silent slate), capture a light NO_TRADE row with the raw output +
        # context + regime. Never raises into the trading path.
        try:
            if not ideas:
                trade_capture.capture_no_trade(
                    trade_capture.dataset_dir(self.journal_path), source="trader",
                    reason=("market_closed" if not _market_open() else "empty_slate"),
                    raw_strategist=_raw_strategist, cot=_cot, candidates=None,
                    regime=self._regime, market_context=context)
        except Exception as _ce:
            print(f"[WARN] no_trade capture failed (continuing): {_ce}")

        ideas = await self._drop_blocked_sectors(ideas)

        # 2026-07-01 budget gates value the open book as (net_debit, dte) pairs -- the
        # deployed-premium (<=40% net-liq) and portfolio-decay (<=4%/day) caps need DTE,
        # which OpenPosition doesn't carry. One extra positions fetch, only when there are ideas.
        _open_book = []
        if ideas:
            try:
                _open_book = construction.open_book(await self.ib_conn.get_positions(), self.journal_path)
            except Exception as e:
                audit(self.audit_path, "open_book_error", error=str(e))

        _ddir = trade_capture.dataset_dir(self.journal_path)

        def _cap_rej(stage, reason, *, resolved=None, gate=None, construction=None):
            """Record-only: append a REJECTED row for a killed idea. Never raises."""
            try:
                trade_capture.capture_rejected(
                    _ddir, source="trader", symbol=idea.underlying, reason=reason, stage=stage,
                    idea=idea, gate=gate, construction=construction,
                    structure=getattr(idea, "structure", None),
                    right=(getattr(resolved, "right", None)),
                    strike=(getattr(resolved, "strike", None)),
                    expiry=(getattr(resolved, "expiry", None)),
                    order=(order_summary(resolved) if resolved is not None else None),
                    regime=self._regime)
            except Exception as _re:
                print(f"[WARN] capture_rejected failed (continuing): {_re}")

        # ENTRY THROTTLE CEILINGS (2026-07-03 gap-fix): enforce caps.max_orders_per_cycle /
        # max_orders_per_day / max_notional_per_day on NEW entries (previously only max_concurrent
        # bound). Per-cycle count is this run_once's submitted entries; per-day figures come from the
        # persisted DailyStats (orders_opened / notional_opened), keyed by the SAME US/Eastern trading
        # day used elsewhere. Ceilings only ADD safety -- they skip an idea, never up-size one.
        _orders_this_cycle = 0
        _sm = getattr(self.exit_manager, "state_manager", None)

        def _day_open_counts():
            """(orders_opened_today, notional_opened_today) from persisted state; (0, 0.0) if absent."""
            try:
                ds = _sm.state.daily_stats.get(today) if _sm is not None else None
                if ds is None:
                    return 0, 0.0
                return int(getattr(ds, "orders_opened", 0)), float(getattr(ds, "notional_opened", 0.0))
            except Exception:
                return 0, 0.0

        # GUARDRAIL 1 (2026-07-03 order-state fix): the entry RISK gate must evaluate exposure off
        # the FRESH POST-EXIT book, NOT the stale `positions` fetched at the top of run_once BEFORE
        # exit_manager.run_cycle ran. A name CLOSED during THIS cycle's exit run is otherwise still
        # counted against max_concurrent / single-name-agg / sector caps -- wrongly blocking a legit
        # new entry, and (critically) blocking a same-name re-entry on its own just-vacated slot.
        # Re-fetch the book (same _open_positions() used pre-exit, so fresh vs stale can't diverge in
        # shape; it also folds resting BUYs). Fall back to the stale `positions` on any error -- the
        # stale book OVER-counts, so a fallback can only BLOCK, never loosen a gate. Intra-cycle
        # sequential gating is PRESERVED: accepted fills are appended to THIS `entry_positions` list
        # below, so later ideas in the same cycle still see earlier fills.
        entry_positions = positions
        if ideas:
            try:
                entry_positions = await self._open_positions()
            except Exception as _pfe:
                audit(self.audit_path, "entry_book_refetch_error", error=str(_pfe))
                entry_positions = positions

        # GUARDRAIL 2 (2026-07-03 order-state fix): compute, ONCE per cycle, the set of underlyings
        # that currently have an IN-FLIGHT or RESTING close (SELL-to-close) working. A new entry into
        # such a name would stack a second spread on a slot that is still mid-exit -> transient double
        # exposure in that name. Any idea in one of these names is DEFERRED below (fires a later cycle
        # once the underlying is confirmed flat/settled). Best-effort read; a miss can only fail to
        # defer, never loosen a gate.
        _closing_names = await self._underlyings_with_close_in_flight() if ideas else set()

        for idea in ideas:
            # GUARDRAIL 2: DEFER an entry whose underlying has an in-flight/resting close in progress.
            if idea.underlying.upper() in _closing_names:
                audit(self.audit_path, "entry_deferred_close_in_flight", underlying=idea.underlying)
                _cap_rej("close_in_flight",
                         "entry_deferred_close_in_flight: underlying has an in-flight/resting close")
                continue
            plan = plan_idea(idea, net_liq=pot.net_liq, available_funds=pot.available_funds,
                             positions=entry_positions, baseline=baseline,
                             approved_names=self.approved_names, limits=self.limits,
                             regime=self._regime)
            audit(self.audit_path, "gated", idea=asdict(idea),
                  approved=plan.gate.approved, reasons=plan.gate.reasons,
                  per_trade_cap=plan.gate.per_trade_cap)
            if not plan.gate.approved:
                _cap_rej("risk_gate", plan.gate.reasons, gate=plan.gate)
                continue

            # Resolve the CONCRETE order BEFORE asking -- so the human approves the real order.
            try:
                resolved = await self._resolve_order(idea, plan.gate.per_trade_cap)
            except Exception as e:
                audit(self.audit_path, "resolve_error", underlying=idea.underlying, error=str(e))
                _cap_rej("resolve_error", str(e), gate=plan.gate)
                continue
            if resolved is None:
                audit(self.audit_path, "resolve_failed", underlying=idea.underlying)
                _cap_rej("resolve_failed", "no usable contract (construction/chain)", gate=plan.gate)
                continue
            resolved.conviction = getattr(idea, "conviction", -1.0)  # carry conviction into the journal
            try:  # carry the entry thesis into the journal (non-blocking)
                resolved.thesis = str(getattr(idea, "thesis", "") or "")
            except Exception as _te:
                print(f"[WARN] thesis carry failed (continuing): {_te}")
            try:  # carry the entry-time technical_card into the journal so a later reload/continuation
                  # call is NOT price-only (fed back to position_manager via _build_position_views).
                resolved.technical_card = (self._price_stats or {}).get(idea.underlying)
            except Exception as _tce:
                print(f"[WARN] technical_card carry failed (continuing): {_tce}")

            # CONSTRUCTION GATES (2026-07-01). TP/SL clamp: +75-100% targets were touched
            # 0/9 times -- clamp any model target into the 25-35% band (default +30%); stop
            # defaults -30% and may only be tighter. Then the budget gates: premium <=15% of
            # net-liq (downsize qty first), deployed premium <=40%, theta decay <=1%/day per
            # trade (<=4%/day portfolio).
            # POT-TIERED TP CEILING (2026-07-03): scale ONLY the runner ceiling + default target
            # with the LIVE pot; per-call cons copy so the shared self.construction is untouched.
            # Stop (sl_pct) + tp_min are unchanged; empty tiers => flat no-op.
            _tp_max, _tp_def = construction.tp_tier_for_pot(
                pot.net_liq, self.caps_tp_tiers, self.construction.tp_max_pct, self.construction.tp_pct)
            _cons_tp = _replace_dc(self.construction, tp_max_pct=_tp_max, tp_pct=_tp_def)
            resolved.tp_pct, resolved.sl_pct = construction.clamp_tp_sl(
                getattr(idea, "profit_target_pct", 0.0), getattr(idea, "stop_pct", 0.0),
                _cons_tp)
            # RELOAD FRICTION GATE (2026-07-03, anti-churn): a reload suggestion clears ONLY if the
            # model's reload_conviction >= reload_conviction_min AND the expected continuation
            # (clamped tp% x new debit) exceeds reload_friction_k x (fresh-entry commission + one-cycle
            # theta + entry slippage). Rejects a churn that just feeds the broker. Applies ONLY to
            # reload ideas; strategist ideas are untouched. Uses the RESOLVED order (final qty/limit/
            # tp% + captured greeks/liquidity), so it runs here, right after the tp/sl clamp.
            if getattr(idea, "is_reload", False):
                _rf_ok, _rf_reason, _rf_detail = reload_queue.reload_friction_ok(
                    reload_conviction=getattr(idea, "reload_conviction", None),
                    conviction_min=self.reload_conviction_min,
                    tp_pct=resolved.tp_pct,
                    new_debit=resolved.limit * 100 * resolved.qty,
                    qty=resolved.qty,
                    is_spread=(resolved.short_contract is not None),
                    theta_per_share=getattr(resolved, "entry_theta", 0.0),
                    entry_spread_pct=getattr(resolved, "entry_spread_pct", 0.0),
                    k=self.reload_friction_k)
                if not _rf_ok:
                    audit(self.audit_path, "reload_friction_rejected", underlying=idea.underlying,
                          order=order_summary(resolved), reason=_rf_reason, detail=_rf_detail)
                    _cap_rej("reload_friction", _rf_reason, resolved=resolved, gate=plan.gate)
                    approval.post_proposal(self.slack_token, self.slack_channel,
                        f":no_entry: Skipped RELOAD *{idea.underlying}* {order_summary(resolved)} — "
                        f"anti-churn: {_rf_reason}")
                    continue
            _max_prem = construction.max_premium_budget(pot.net_liq, self.construction)
            if _max_prem > 0 and resolved.limit * 100 * resolved.qty > _max_prem + 1e-6:
                _newq = int(_max_prem // (resolved.limit * 100))
                if _newq >= 1:
                    audit(self.audit_path, "premium_downsized", underlying=idea.underlying,
                          from_qty=resolved.qty, to_qty=_newq, premium_cap=round(_max_prem))
                    resolved.qty = _newq
            ok_budget, budget_reasons = construction.check_budget(
                resolved.limit * 100 * resolved.qty, resolved.dte, pot.net_liq,
                _open_book, self.construction)
            if not ok_budget:
                audit(self.audit_path, "budget_rejected", underlying=idea.underlying,
                      order=order_summary(resolved), reasons=budget_reasons)
                _cap_rej("budget", budget_reasons, resolved=resolved, gate=plan.gate,
                         construction={"tp_pct": resolved.tp_pct, "sl_pct": resolved.sl_pct,
                                       "dte": resolved.dte, "dte_adjusted": resolved.dte_adjusted,
                                       "qty": resolved.qty, "budget_reasons": budget_reasons})
                approval.post_proposal(self.slack_token, self.slack_channel,
                    f":no_entry: Skipped *{idea.underlying}* {order_summary(resolved)} — budget gate: "
                    + "; ".join(budget_reasons))
                continue

            # EARNINGS BLACKOUT (2026-07-03 gate A5): a DEBIT held THROUGH an earnings print is an
            # IV-crush loser by construction (IV collapses post-print; the long premium bleeds even
            # when direction is right). Block if a KNOWN next-earnings date (research.days_to_earnings
            # via yfinance) lands within the holding horizon (on/before expiry + cushion). FAIL-OPEN
            # on unknown earnings: never hard-block, but flag it 'unchecked' (surfaced below +
            # journaled) so an unchecked trade is never treated as verified-clear of earnings.
            resolved.earnings_unchecked = False
            _entry = datetime.now(timezone.utc).date()
            try:
                # OFF-LOOP: blocking yfinance lookup -> worker thread (don't stall the IBKR loop).
                _edays = await asyncio.to_thread(research.days_to_earnings, idea.underlying)
            except Exception as _ee:
                print(f"[WARN] earnings lookup failed for {idea.underlying} (fail-open, unchecked): {_ee}")
                _edays = None
            _earn_date = (_entry + timedelta(days=_edays)) if _edays is not None else None
            resolved.earnings_unchecked = _earn_date is None
            ok_earn, why_earn = construction.earnings_ok(_entry, resolved.expiry, _earn_date,
                                                         self.construction)
            if not ok_earn:
                audit(self.audit_path, "earnings_blackout_rejected", underlying=idea.underlying,
                      order=order_summary(resolved), reason=why_earn)
                _cap_rej("earnings_blackout", why_earn, resolved=resolved, gate=plan.gate,
                         construction={"tp_pct": resolved.tp_pct, "sl_pct": resolved.sl_pct,
                                       "dte": resolved.dte, "dte_adjusted": resolved.dte_adjusted,
                                       "qty": resolved.qty, "earnings_date": str(_earn_date),
                                       "earnings_reason": why_earn})
                approval.post_proposal(self.slack_token, self.slack_channel,
                    f":no_entry: Skipped *{idea.underlying}* {order_summary(resolved)} — {why_earn}")
                continue
            if resolved.earnings_unchecked:
                # A5: no earnings date available -- the IV-crush blackout could NOT be checked.
                # Surfaced (never silent-clear) via audit + Slack note + journal field so an
                # unchecked entry is never presented as verified-clear of earnings.
                audit(self.audit_path, "earnings_unchecked", underlying=idea.underlying,
                      order=order_summary(resolved))

            # EARLY-ASSIGNMENT / EX-DIV RISK (2026-07-03 gate A6): a DEBIT SPREAD whose ITM short
            # leg heads into an ex-dividend date can be assigned EARLY (a counterparty exercises
            # the ITM short to grab the dividend, converting the spread). Applies ONLY to spreads
            # -- a single long leg has no short to be assigned. Default disposition is WARN (surface
            # the risk, still allow); hard-block only when construction.assignment_block_hard is set.
            # FAIL-OPEN on an unknown ex-div date, flagged 'unchecked' (never silent-clear).
            resolved.assignment_warn = ""
            resolved.assignment_unchecked = False
            if resolved.short_contract is not None:
                _entry_a = datetime.now(timezone.utc).date()
                try:
                    # OFF-LOOP: blocking yfinance lookup -> worker thread.
                    _xdays = await asyncio.to_thread(research.days_to_ex_dividend, idea.underlying)
                except Exception as _xe:
                    print(f"[WARN] ex-div lookup failed for {idea.underlying} (fail-open, unchecked): {_xe}")
                    _xdays = None
                _exdiv_date = (_entry_a + timedelta(days=_xdays)) if _xdays is not None else None
                resolved.assignment_unchecked = _exdiv_date is None
                ok_assign, why_assign = construction.assignment_risk_ok(
                    resolved.short_strike, resolved.spot, resolved.right, resolved.expiry,
                    _exdiv_date, resolved.dte, self.construction)
                if not ok_assign:
                    audit(self.audit_path, "assignment_risk_rejected", underlying=idea.underlying,
                          order=order_summary(resolved), reason=why_assign)
                    _cap_rej("assignment_risk", why_assign, resolved=resolved, gate=plan.gate,
                             construction={"tp_pct": resolved.tp_pct, "sl_pct": resolved.sl_pct,
                                           "dte": resolved.dte, "dte_adjusted": resolved.dte_adjusted,
                                           "qty": resolved.qty, "short_strike": resolved.short_strike,
                                           "ex_div_date": str(_exdiv_date),
                                           "assignment_reason": why_assign})
                    approval.post_proposal(self.slack_token, self.slack_channel,
                        f":no_entry: Skipped *{idea.underlying}* {order_summary(resolved)} — {why_assign}")
                    continue
                if why_assign:
                    resolved.assignment_warn = why_assign
                    audit(self.audit_path, "assignment_risk_warn", underlying=idea.underlying,
                          order=order_summary(resolved), reason=why_assign)
                elif resolved.assignment_unchecked:
                    audit(self.audit_path, "assignment_unchecked", underlying=idea.underlying,
                          order=order_summary(resolved))

            # ENTRY THROTTLE (2026-07-03 gap-fix): refuse a NEW entry that would breach a per-cycle
            # or per-day ceiling BEFORE asking the human, so an over-cap idea is simply not offered.
            # resolved.qty/limit are final here (premium downsizing already applied). None => disabled.
            _throttle = None
            _cost_throttle = resolved.limit * 100 * resolved.qty
            _od_today, _nd_today = _day_open_counts()
            if self.max_orders_per_cycle is not None and _orders_this_cycle >= self.max_orders_per_cycle:
                _throttle = (f"per-cycle order cap reached "
                             f"({_orders_this_cycle} >= {self.max_orders_per_cycle})")
            elif self.max_orders_per_day is not None and _od_today + 1 > self.max_orders_per_day:
                _throttle = (f"daily order cap reached "
                             f"({_od_today} >= {self.max_orders_per_day})")
            elif (self.max_notional_per_day is not None
                  and _nd_today + _cost_throttle > self.max_notional_per_day + 1e-6):
                _throttle = (f"daily notional cap: ${_nd_today:,.0f} + ${_cost_throttle:,.0f} "
                             f"> ${self.max_notional_per_day:,.0f}")
            if _throttle:
                audit(self.audit_path, "entry_cap_skipped", underlying=idea.underlying,
                      reason=_throttle, order=order_summary(resolved))
                _cap_rej("entry_cap", _throttle, resolved=resolved, gate=plan.gate)
                continue

            resolved.decision_id = entry_safety.new_decision_id()
            resolved.model_identity = _model_identity
            resolved.decision_revision = 0
            try:
                trade_capture.capture_decision(
                    _ddir, source="trader", symbol=idea.underlying,
                    right=resolved.right, strike=resolved.strike, expiry=resolved.expiry,
                    structure=("spread" if resolved.short_contract is not None else "single"),
                    con_id=getattr(resolved.contract, "conId", None), chosen_idea=idea,
                    candidates=ideas, raw_strategist=_raw_strategist, cot=_cot,
                    gate=plan.gate, regime=self._regime, market_context=context,
                    technical_card=self._price_stats,
                    decision_id=resolved.decision_id, revision=0, event="proposal",
                    model_identity=_model_identity, final_contract=contract_snapshot(resolved))
            except Exception as _capture_error:
                print(f"[WARN] proposal capture failed (continuing): {_capture_error}")
            _approval_ttl = min(entry_safety.DEFAULT_APPROVAL_TTL_SECONDS,
                                max(1, int(self.approve_timeout_s)))
            msg = approval.format_proposal(idea, pot.net_liq, plan.gate.per_trade_cap,
                                           max(1, _approval_ttl // 60), order_summary(resolved))
            if getattr(idea, "is_reload", False):
                # RELOAD: make it unmistakable in Slack that this is a same-name RE-ENTRY on
                # continued conviction (banked the prior winner; fresh basis + fresh 30% stop).
                msg = (f":arrows_counterclockwise: *RELOAD / continuation* — re-entering "
                       f"*{idea.underlying}* after banking a take-profit "
                       f"(model reload_conviction {getattr(idea, 'reload_conviction', '?')}).\n") + msg
            if getattr(resolved, "earnings_unchecked", False):
                # A5: never present an unchecked trade as verified-clear of earnings.
                msg += "\n:grey_question: earnings date unknown — IV-crush blackout UNCHECKED"
            if getattr(resolved, "assignment_warn", ""):
                # A6 (warn disposition): surface the ITM-short ex-div early-assignment risk.
                msg += f"\n:warning: {resolved.assignment_warn}"
            elif getattr(resolved, "assignment_unchecked", False):
                # A6: never present a spread as verified-clear of assignment risk when ex-div is unknown.
                msg += "\n:grey_question: ex-dividend date unknown — early-assignment risk UNCHECKED"
            msg += (f"\n_Decision ID: `{resolved.decision_id}` — approval expires in "
                    f"{_approval_ttl // 60 or 1} minutes._")
            if dry_run:
                approval.post_proposal(self.slack_token, self.slack_channel,
                                       "[DRY RUN — nothing will be submitted]\n" + msg)
                audit(self.audit_path, "dry_run_proposal", underlying=idea.underlying,
                      order=order_summary(resolved))
                continue

            # RISK SCREEN (live only): block genuinely oversized orders. This account is
            # long-debit-only (long calls/puts + debit spreads), so the capital actually at risk
            # is the NET DEBIT (max loss = limit*100*qty), NOT the strike notional. The old screen
            # measured strike notional (~1.1x single / ~2.4x sum-of-strikes spread) vs 30x NetLiq,
            # which structurally rejected every cheap defined-risk debit spread (e.g. ORCL
            # 162.5/160P @ $1.25 = $125 risk read as ~$32k). Measure the real max loss instead;
            # available_funds already bounds it to the pot. Skip BEFORE asking the human.
            _est_gross = resolved.limit * 100 * resolved.qty  # max loss / net debit = capital at risk
            if pot.net_liq > 0 and _est_gross > 0.95 * 30 * pot.net_liq:
                audit(self.audit_path, "gross_rejected", underlying=idea.underlying,
                      order=order_summary(resolved), est_gross=round(_est_gross), cap=round(30 * pot.net_liq))
                _cap_rej("gross", f"capital at risk ${_est_gross:,.0f} exceeds 30x-NetLiq cap",
                         resolved=resolved, gate=plan.gate)
                approval.post_proposal(self.slack_token, self.slack_channel,
                    f":no_entry: Skipped *{idea.underlying}* {order_summary(resolved)} — capital at risk "
                    f"${_est_gross:,.0f} exceeds the 30x-NetLiq cap (${30*pot.net_liq:,.0f}). "
                    f"Too large for this ${pot.net_liq:,.0f} pot.")
                continue

            ts = approval.post_proposal(self.slack_token, self.slack_channel, msg)
            if not ts:
                audit(self.audit_path, "slack_post_failed", underlying=idea.underlying)
                continue
            # OFF-LOOP (2026-07-03): await_approval BLOCKS (polls Slack + sleeps) for up to the
            # approve timeout; run it in a worker thread so the IBKR event loop / exit I/O isn't
            # starved for minutes while a human decides.
            _posted_at = time.monotonic()
            decision = await asyncio.to_thread(
                approval.await_approval, self.slack_token, self.slack_channel, ts,
                self.approver_ids, _approval_ttl)
            audit(self.audit_path, "approval", underlying=idea.underlying,
                  order=order_summary(resolved), decision=decision,
                  decision_id=resolved.decision_id)
            if decision != "approve":
                _cap_rej("approval", f"human decision: {decision}", resolved=resolved, gate=plan.gate)
                continue

            _age = entry_safety.approval_expired(_posted_at, ttl_seconds=_approval_ttl)
            if not _age.allowed:
                audit(self.audit_path, "approval_expired", underlying=idea.underlying,
                      decision_id=resolved.decision_id, reasons=_age.reasons)
                continue

            # The approval binds to the displayed terms, not merely the ticker. Rebuild from a new
            # account/chain/NBBO and rerun every hard gate. A material change gets one new proposal
            # and a second explicit tap; continued movement aborts rather than looping forever.
            fresh, _pot2, _final_reasons = await self._refresh_approved_entry(
                idea, resolved, baseline)
            if _final_reasons or fresh is None:
                audit(self.audit_path, "final_entry_gate_blocked", underlying=idea.underlying,
                      decision_id=resolved.decision_id, reasons=_final_reasons)
                approval.post_proposal(self.slack_token, self.slack_channel,
                    f":no_entry: Approved *{idea.underlying}* was NOT submitted — final hard gate: "
                    + "; ".join(_final_reasons or ("fresh order unavailable",)))
                continue
            _changes = entry_safety.material_changes(resolved, fresh)
            if _changes:
                _remsg = (f":repeat: *Reapproval required — {idea.underlying}*\n"
                           f"Refreshed order: `{order_summary(fresh)}`\n"
                           f"Executable BUY limit: *${entry_safety.executable_price(fresh):.2f}*\n"
                           f"Changed: {'; '.join(_changes)}\n"
                           f":point_down: Approve again within {_approval_ttl // 60 or 1} minutes.\n"
                           f"_Decision ID: `{resolved.decision_id}`, revision 1_")
                _rts = approval.post_proposal(self.slack_token, self.slack_channel, _remsg)
                if not _rts:
                    audit(self.audit_path, "reapproval_post_failed", decision_id=resolved.decision_id)
                    continue
                _reposted_at = time.monotonic()
                _rdecision = await asyncio.to_thread(
                    approval.await_approval, self.slack_token, self.slack_channel, _rts,
                    self.approver_ids, _approval_ttl)
                audit(self.audit_path, "reapproval", underlying=idea.underlying,
                      decision_id=resolved.decision_id, decision=_rdecision, changes=_changes)
                if _rdecision != "approve" or not entry_safety.approval_expired(
                        _reposted_at, ttl_seconds=_approval_ttl).allowed:
                    continue
                fresh2, _pot3, _recheck_reasons = await self._refresh_approved_entry(
                    idea, fresh, baseline)
                if _recheck_reasons or fresh2 is None:
                    audit(self.audit_path, "reapproval_gate_blocked", underlying=idea.underlying,
                          decision_id=resolved.decision_id, reasons=_recheck_reasons)
                    continue
                _changes2 = entry_safety.material_changes(fresh, fresh2)
                if _changes2:
                    audit(self.audit_path, "reapproval_churn_blocked", underlying=idea.underlying,
                          decision_id=resolved.decision_id, changes=_changes2)
                    approval.post_proposal(self.slack_token, self.slack_channel,
                        f":no_entry: *{idea.underlying}* changed again after reapproval — nothing submitted.")
                    continue
                fresh = fresh2
                fresh.decision_revision = 1
            resolved = fresh

            # Adjacent marker stat: if a halt flips after refresh, _submit_order also refuses.
            _marker_now = self._entry_markers_clear()
            if not _marker_now.allowed:
                audit(self.audit_path, "marker_blocked_submit", underlying=idea.underlying,
                      decision_id=resolved.decision_id, reasons=_marker_now.reasons)
                continue
            try:
                status, reasons = await self._submit_order(resolved)
                if status in ("Cancelled", "ApiCancelled", "Inactive"):
                    reason = reasons[-1] if reasons else f"order {status}"
                    audit(self.audit_path, "rejected", underlying=idea.underlying,
                          order=order_summary(resolved), status=status, reason=reason)
                    _cap_rej("ibkr_rejected", reason, resolved=resolved, gate=plan.gate)
                    approval.post_proposal(self.slack_token, self.slack_channel,
                        f":x: *Order REJECTED by IBKR* — {idea.underlying} {order_summary(resolved)} "
                        f"was NOT placed.\n{reason}")
                else:
                    audit(self.audit_path, "executed", underlying=idea.underlying,
                          order=order_summary(resolved), status=status,
                          decision_id=resolved.decision_id)
                    # DECISION CAPTURE (v2, record-only): the full DECISION -> ENTRY context for an
                    # ENTERED trade -- raw strategist reasoning, EVERY candidate + conviction, the
                    # chosen idea, the risk GateDecision + bound caps, construction adjustments,
                    # regime, and the RAG/news/journal brief. con_id is known here (resolved.contract);
                    # joined into the closed-trade v2 record at close. Never raises into the loop.
                    try:
                        trade_capture.capture_decision(
                            _ddir, source="trader", symbol=idea.underlying,
                            right=resolved.right, strike=resolved.strike, expiry=resolved.expiry,
                            structure=("spread" if resolved.short_contract is not None else "single"),
                            con_id=getattr(resolved.contract, "conId", None),
                            chosen_idea=idea, candidates=ideas, raw_strategist=_raw_strategist, cot=_cot,
                            gate=plan.gate, regime=self._regime, market_context=context,
                            technical_card=self._price_stats,  # per-name technical indicators fed to the model
                            construction={"tp_pct": resolved.tp_pct, "sl_pct": resolved.sl_pct,
                                          "dte": resolved.dte, "dte_adjusted": resolved.dte_adjusted,
                                          "qty": resolved.qty, "limit": resolved.limit,
                                          "short_strike": resolved.short_strike},
                            sizing={"per_trade_cap": plan.gate.per_trade_cap,
                                    "net_liq": pot.net_liq, "available_funds": pot.available_funds,
                                    "qty": resolved.qty, "limit": resolved.limit},
                            extra={"order": order_summary(resolved), "status": status,
                                   "decision_id": resolved.decision_id},
                            decision_id=resolved.decision_id,
                            revision=resolved.decision_revision, event="submitted",
                            model_identity=resolved.model_identity,
                            final_contract=contract_snapshot(resolved),
                            order_ref=entry_safety.decision_order_ref(resolved.decision_id),
                            human_action={"action": "approve"})
                    except Exception as _dce:
                        print(f"[WARN] capture_decision failed (continuing): {_dce}")
                    # GUARDRAIL 1: append to the FRESH entry book so later ideas THIS cycle see this
                    # fill (intra-cycle sequential gating preserved on the post-exit book).
                    _resolved_cost = round(resolved.limit * 100 * resolved.qty, 2)
                    entry_positions.append(OpenPosition(idea.underlying, _resolved_cost, idea.is_index))
                    # ENTRY THROTTLE accrual (2026-07-03 gap-fix): count this submitted entry against
                    # the per-cycle counter and the persisted per-day opened order/notional aggregates
                    # so subsequent ideas (this cycle and later cycles today) see the updated ceiling.
                    _orders_this_cycle += 1
                    try:
                        if _sm is not None:
                            _sm.state.update_daily_open_stats(
                                today, 1, round(resolved.limit * 100 * resolved.qty, 2))
                            _sm.save()
                    except Exception as _ue:
                        print(f"[WARN] entry daily-stats update failed (continuing): {_ue}")
            except Exception as e:
                audit(self.audit_path, "submit_error", underlying=idea.underlying, error=str(e))


    async def _refresh_approved_entry(self, idea, original: ResolvedOrder, baseline: float):
        """Rebuild and hard-gate an approved BUY from current broker state.

        Returns ``(fresh_order, fresh_pot, reasons)``. Any exception becomes a blocking reason;
        this method never converts unavailable risk data into approval.
        """
        reasons = list(self._entry_markers_clear().reasons)
        fresh = None
        pot = None
        try:
            pot = await get_pot_snapshot(self.ib_conn.ib)
            reasons.extend(entry_safety.account_snapshot_valid(pot).reasons)
            open_positions = await self._open_positions()
            gate = plan_idea(
                idea, net_liq=pot.net_liq, available_funds=pot.available_funds,
                positions=open_positions, baseline=baseline,
                approved_names=self.approved_names, limits=self.limits,
                regime=self._regime).gate
            if not gate.approved:
                reasons.extend(gate.reasons)
            raw_positions = await self.ib_conn.get_positions()
            # Slow/error-prone earnings lookup happens before the final contract request so the
            # NBBO timestamp remains tight at the money boundary.
            days = await asyncio.to_thread(research.days_to_earnings, idea.underlying)
            if days is None:
                reasons.append("earnings date unavailable at approval time")
            fresh = await self._resolve_order(idea, gate.per_trade_cap)
            if fresh is None:
                reasons.append("fresh contract/NBBO resolution returned no order")
            else:
                fresh.decision_id = original.decision_id
                fresh.decision_revision = original.decision_revision
                fresh.model_identity = original.model_identity
                fresh.conviction = original.conviction
                fresh.thesis = original.thesis
                fresh.tp_pct = original.tp_pct
                fresh.sl_pct = original.sl_pct
                fresh.technical_card = getattr(original, "technical_card", None)
                reasons.extend(entry_safety.nbbo_valid(fresh).reasons)
                ok_budget, budget_reasons = construction.check_budget(
                    entry_safety.executable_price(fresh) * 100 * fresh.qty,
                    fresh.dte, pot.net_liq,
                    construction.open_book(raw_positions, self.journal_path), self.construction)
                if not ok_budget:
                    reasons.extend(budget_reasons)
                if days is not None:
                    entry_date = datetime.now(timezone.utc).date()
                    earnings_date = entry_date + timedelta(days=days)
                    earn_ok, earn_reason = construction.earnings_ok(
                        entry_date, fresh.expiry, earnings_date, self.construction)
                    if not earn_ok:
                        reasons.append(earn_reason)
        except Exception as exc:
            reasons.append(f"fresh account/contract/NBBO/risk/earnings gate failed: {exc}")
        return fresh, pot, tuple(dict.fromkeys(str(r) for r in reasons if r))

    def _drain_reload_ideas(self, today: str) -> List[TradeIdea]:
        """Drain ready reload tickets into synthetic TradeIdea suggestions (2026-07-03).
        Consume-once + TTL + per-name-per-day depth cap are enforced by ReloadQueue.drain(). Each
        idea is tagged is_reload so the friction gate + Slack banner fire, then it flows through the
        SAME entry path as a strategist idea. A fresh entry journals its OWN debit + 30% stop, so the
        basis/stop re-anchor to the new position automatically (banked gain can't be given back)."""
        q = reload_queue.ReloadQueue(reload_queue.queue_path(self.journal_path))
        ready, summary = q.drain(today=today, max_per_name=self.reload_max_per_name_per_day)
        if summary.get("expired") or summary.get("capped"):
            audit(self.audit_path, "reload_tickets_dropped",
                  expired=summary.get("expired", 0), capped=summary.get("capped", 0))
        ideas: List[TradeIdea] = []
        for t in ready:
            right = (t.get("right") or "C").upper()
            direction = "bullish" if right == "C" else "bearish"
            # "spread"/"single" in the ticket -> the structure string _resolve_order keys on.
            structure = "debit spread" if t.get("structure") == "spread" else "long option"
            dte = int(t.get("dte_target") or getattr(self.construction, "min_dte", 30) or 30)
            # budget the reload to the ORIGINAL spend when known (bounded further by the per-trade
            # cap in the gate); unknown -> a large sentinel so ONLY the per-trade cap binds.
            _od = t.get("original_debit")
            budget = float(_od) if (_od and float(_od) > 0) else 1e12
            conv = t.get("reload_conviction")
            try:
                conv_int = int(round(float(conv))) if conv is not None else 0
            except (TypeError, ValueError):
                conv_int = 0
            base_thesis = (t.get("thesis") or "").strip()
            thesis = (f"[RELOAD/continuation] {base_thesis}" if base_thesis else "[RELOAD/continuation]")
            idea = TradeIdea(underlying=str(t["symbol"]), is_index=bool(t.get("is_index")),
                             direction=direction, structure=structure, target_dte=dte,
                             target_delta=0.0, est_debit_usd=budget, conviction=conv_int,
                             thesis=thesis)
            # dynamic tags read by the friction gate + Slack banner (dataclass has no slots).
            idea.is_reload = True
            idea.reload_conviction = conv
            ideas.append(idea)
        return ideas

    async def _resolve_order(self, idea: TradeIdea, per_trade_cap: float) -> Optional[ResolvedOrder]:
        """Select the concrete option contract from target DTE/delta and size it. Returns a
        ResolvedOrder (no order placed yet) or None if nothing usable. Validate on first live run."""
        from exitmgr.ibkr import Option, Stock, pick_chain, strikes_near, underlying_price
        from exitmgr.market import usable_price
        ib = self.ib_conn.ib
        right = "C" if idea.direction == "bullish" else "P"
        stk = (await ib.qualifyContractsAsync(Stock(idea.underlying, "SMART", "USD")))[0]
        params = await ib.reqSecDefOptParamsAsync(idea.underlying, "", "STK", stk.conId)
        if not params:
            return None
        p = pick_chain(params, idea.underlying)
        if p is None:
            return None
        cons = self.construction
        # MIN-DTE FLOOR (2026-07-01 gate A1): median 17.5 DTE at entry bled 5.9-12.5%/day
        # theta. Nearest expiry to target among those >= min_dte; a too-short model target is
        # ADJUSTED into the 25-45 band (annotated on the journal), rejected only if no valid
        # expiry exists at all.
        expiry, chosen_dte, dte_adjusted = construction.pick_expiry(
            p.expirations, idea.target_dte, cons.min_dte, cons.prefer_dte_max)
        if expiry is None:
            audit(self.audit_path, "construction_rejected", underlying=idea.underlying,
                  reason=f"no expiry >= {cons.min_dte} DTE available")
            return None
        if dte_adjusted:
            audit(self.audit_path, "dte_adjusted", underlying=idea.underlying,
                  requested_dte=idea.target_dte, adjusted_dte=chosen_dte, min_dte=cons.min_dte)
        spot = await underlying_price(ib, stk)
        cands = [Option(idea.underlying, expiry, k, right, "SMART") for k in strikes_near(p.strikes, spot)]
        qualified = await ib.qualifyContractsAsync(*cands)
        tickers = await ib.reqTickersAsync(*[c for c in qualified if getattr(c, "conId", None)])
        # LONG-LEG DELTA BAND (gate A3): target ~0.55-0.65 delta -- a leg that is already
        # working, not a lottery ticket. The model's target_delta is clamped into the band.
        tgt_delta = construction.effective_delta(idea.target_delta, cons)
        best, best_err, best_greeks = None, 1e9, None
        best_bidask = (None, None)   # bid/ask of the winning long leg (record-only liquidity capture)
        by_strike = {}   # strike -> (mid, contract), for spread short-leg selection
        quote_by_strike = {}  # strike -> (bid, ask), needed for executable combo NBBO
        for tk in tickers:
            # -1 SENTINEL GUARD (2026-07-03): IB reports "no quote" as None/NaN/-1.0. The old
            # `tk.bid and tk.ask` let a -1 bid/ask through (truthy) and averaged it into a bogus mid;
            # `tk.last or 0` let a -1 last through too. usable_price() accepts only a finite positive
            # price, so a one-sided -1 falls back to last, and a -1 last falls back to 0 (skipped).
            if usable_price(tk.bid) and usable_price(tk.ask):
                mid = (tk.bid + tk.ask) / 2
            else:
                mid = tk.last if usable_price(tk.last) else 0
            if not (mid == mid and mid > 0):
                continue
            k = float(getattr(tk.contract, "strike", 0) or 0)
            if k:
                by_strike[k] = (mid, tk.contract)
                quote_by_strike[k] = (getattr(tk, "bid", None), getattr(tk, "ask", None))
            g = getattr(tk, "modelGreeks", None) or getattr(tk, "lastGreeks", None)
            if g and g.delta is not None:
                err = abs(abs(g.delta) - tgt_delta)
                if err < best_err:
                    best, best_err, best_greeks = (tk.contract, mid), err, g
                    best_bidask = (getattr(tk, "bid", None), getattr(tk, "ask", None))
        if not best and by_strike and spot:
            # No greeks at all (delayed feed etc.) -> conservative fallback: nearest-to-spot
            # priced strike, only if within the near-spot band (same fallback logic as A3).
            k_near = min(by_strike, key=lambda k: abs(k - spot))
            if abs(k_near - spot) <= cons.strike_near_spot_pct * spot:
                best = (by_strike[k_near][1], by_strike[k_near][0])
                best_bidask = quote_by_strike.get(k_near, (None, None))
        if not best:
            return None
        contract, mid = best
        atm_iv = getattr(best_greeks, "impliedVol", None) if best_greeks else None
        # LOTTERY-LONG check (gate A3): the long strike may not sit further OTM than ~1
        # expected move for the horizon (fallback: 3% of spot).
        ok, why = construction.long_strike_ok(float(contract.strike), spot, right,
                                              chosen_dte, atm_iv, cons)
        if not ok:
            audit(self.audit_path, "construction_rejected", underlying=idea.underlying, reason=why)
            return None
        enrich = dict(spot=float(spot or 0.0),
                      entry_delta=float(abs(best_greeks.delta)) if (best_greeks and best_greeks.delta is not None) else 0.0,
                      entry_iv=float(atm_iv) if (atm_iv and atm_iv == atm_iv) else 0.0,
                      dte=int(chosen_dte), dte_adjusted=bool(dte_adjusted),
                      quote_observed_at=time.monotonic())
        # FULL GREEKS + LIQUIDITY capture (v2, record-only). Best-effort off the already-fetched
        # ticker/greeks -- no extra IBKR round-trip. Never raises into the resolve path.
        try:
            def _f(x):
                try:
                    return float(x) if (x is not None and x == x) else 0.0
                except (TypeError, ValueError):
                    return 0.0
            g = best_greeks
            _d = _f(getattr(g, "delta", None)) if g else 0.0
            _th = _f(getattr(g, "theta", None)) if g else 0.0
            _ga = _f(getattr(g, "gamma", None)) if g else 0.0
            _ve = _f(getattr(g, "vega", None)) if g else 0.0
            _bid, _ask = best_bidask
            _bid, _ask = _f(_bid), _f(_ask)
            _spr = round((_ask - _bid) / mid * 100, 2) if (mid and _ask >= _bid > 0) else 0.0
            _single = "spread" not in (idea.structure or "").lower()
            enrich.update(
                entry_gamma=_ga, entry_theta=_th, entry_vega=_ve,
                entry_bid=_bid, entry_ask=_ask, entry_spread_pct=_spr,
                # single-leg net greeks == long-leg greeks; spread net greeks need the short leg's
                # greeks (not retained) -> left 0/unknown for spreads.
                net_delta=(abs(_d) if _single else 0.0),
                net_theta=(_th if _single else 0.0),
                net_gamma=(_ga if _single else 0.0),
                net_vega=(_ve if _single else 0.0),
            )
        except Exception as _ge:
            print(f"[WARN] greeks/liquidity capture failed for {idea.underlying} (continuing): {_ge}")

        if "spread" in (idea.structure or "").lower():
            # STRUCTURE SANITY (gate A3): short leg constrained to ~1 expected move of spot
            # (conservative width fallback when IV is unavailable) inside pick_spread_short.
            pick = pick_spread_short([(k, m) for k, (m, _) in by_strike.items()],
                                     float(contract.strike), mid, right, per_trade_cap,
                                     spot=spot, dte=chosen_dte, atm_iv=atm_iv, cons=cons)
            if not pick:
                audit(self.audit_path, "construction_rejected", underlying=idea.underlying,
                      reason="no sane affordable short leg (structure-sanity + cap)")
                return None
            short_strike, net = pick
            short_contract = by_strike[short_strike][1]
            # Synthetic executable combo NBBO for a BUY debit: sell the short at its bid and buy
            # the long at its ask.  The reverse legs form the combo bid.  Missing/one-sided quotes
            # remain zero and the final fail-closed NBBO gate refuses submission.
            try:
                _lb, _la = (float(x) for x in best_bidask)
                _sb, _sa = (float(x) for x in quote_by_strike[short_strike])
                enrich["entry_bid"] = round(_lb - _sa, 4)
                enrich["entry_ask"] = round(_la - _sb, 4)
                _net_mid = (enrich["entry_bid"] + enrich["entry_ask"]) / 2
                enrich["entry_spread_pct"] = (
                    round((enrich["entry_ask"] - enrich["entry_bid"]) / _net_mid * 100, 2)
                    if _net_mid > 0 and enrich["entry_ask"] >= enrich["entry_bid"] else 0.0)
            except (TypeError, ValueError, KeyError):
                enrich["entry_bid"] = enrich["entry_ask"] = 0.0
                enrich["entry_spread_pct"] = 0.0
            # HARD-REJECT (2026-07-03): if a single spread already exceeds the per-trade cap, reject
            # instead of clamping qty to 1 and shipping an order over the risk cap.
            qty = size_within_cap(net * 100, idea.est_debit_usd, per_trade_cap)
            if qty is None:
                audit(self.audit_path, "construction_rejected", underlying=idea.underlying,
                      reason=f"one spread (${net*100:,.0f}) exceeds per-trade cap ${per_trade_cap:,.0f}")
                return None
            return ResolvedOrder(idea.underlying, right, expiry, float(contract.strike),
                                 qty, net, contract,
                                 short_strike=short_strike, short_contract=short_contract,
                                 **enrich)

        # size to the per-trade cap (never exceed the gate's $ cap); HARD-REJECT if even one
        # contract exceeds it (no clamp-to-1 past the cap).
        qty = size_within_cap(mid * 100, idea.est_debit_usd, per_trade_cap)
        if qty is None:
            audit(self.audit_path, "construction_rejected", underlying=idea.underlying,
                  reason=f"one contract (${mid*100:,.0f}) exceeds per-trade cap ${per_trade_cap:,.0f}")
            return None
        return ResolvedOrder(idea.underlying, right, expiry, float(contract.strike),
                             qty, round(mid, 2), contract, **enrich)

    async def _submit_order(self, r: ResolvedOrder):
        """Serialize BUY placement against the fast protective-exit mutation loop."""
        lock = self.broker_order_lock
        if lock is None:
            return await self._submit_order_unlocked(r)
        async with lock:
            return await self._submit_order_unlocked(r)

    async def _submit_order_unlocked(self, r: ResolvedOrder):
        """Place the entry as a MARKETABLE LIMIT at the freshly observed executable ask, never raw
        MKT or a stale mid-plus-buffer. Wait for a decisive IBKR status and return
        (status, [reason msgs]); journal only if it was NOT rejected, so a bounced order never
        pollutes the exit-managed book."""
        from exitmgr.ibkr import Order
        marker_gate = self._entry_markers_clear()
        if not marker_gate.allowed:
            raise RuntimeError("entry markers block submit: " + "; ".join(marker_gate.reasons))
        quote_gate = entry_safety.nbbo_valid(r)
        if not quote_gate.allowed:
            raise RuntimeError("fresh NBBO blocks submit: " + "; ".join(quote_gate.reasons))
        try:
            release_evidence = model_release_gate.require_v3_release(
                self.model_release_gate_settings, endpoint=self.endpoint,
                decision_identity=r.model_identity, decision_origin="model")
        except model_release_gate.ModelReleaseGateError as exc:
            audit(self.audit_path, "model_release_gate_blocked",
                  decision_id=r.decision_id, underlying=r.underlying, reason=str(exc))
            raise RuntimeError("v3 model release gate blocks entry: " + str(exc)) from exc
        if release_evidence.get("enabled"):
            audit(self.audit_path, "model_release_gate_passed",
                  decision_id=r.decision_id, underlying=r.underlying,
                  promotion=release_evidence)
        # Submission is bound to the final, two-sided reqTickersAsync observation.  Using a stale
        # mid + percentage buffer can still rest below a wide ask; crossing at the observed ask is
        # both executable and capped.  Callers must run nbbo_valid immediately beforehand.
        _lmt = entry_safety.executable_price(r)
        order = Order(action="BUY", orderType="LMT", lmtPrice=_lmt, totalQuantity=r.qty, tif="DAY")
        order.orderRef = entry_safety.decision_order_ref(r.decision_id)
        if r.short_contract is not None:
            # spreads trade as ONE combo order -- legs can never fill/close independently
            combo = self.ib_conn.create_combo_contract(
                r.underlying,
                [(r.contract.conId, "BUY"), (r.short_contract.conId, "SELL")])
            from exitmgr.order_lock import order_mutation_lock
            with order_mutation_lock():
                model_release_gate.revalidate_v3_release(
                    release_evidence, self.model_release_gate_settings, endpoint=self.endpoint,
                    decision_identity=r.model_identity, decision_origin="model")
                trade = self.ib_conn.ib.placeOrder(combo, order)
        else:
            from exitmgr.order_lock import order_mutation_lock
            with order_mutation_lock():
                model_release_gate.revalidate_v3_release(
                    release_evidence, self.model_release_gate_settings, endpoint=self.endpoint,
                    decision_identity=r.model_identity, decision_origin="model")
                trade = self.ib_conn.ib.placeOrder(r.contract, order)
        live = {"Filled", "Submitted", "PreSubmitted"}
        dead = {"Cancelled", "ApiCancelled", "Inactive"}
        for _ in range(24):  # up to ~12s for IBKR to ACK or REJECT
            await asyncio.sleep(0.5)
            if trade.orderStatus.status in live or trade.orderStatus.status in dead:
                break
        # FILL VERIFICATION (2026-07-01 audit: 5/15 "executed" entries never filled and the
        # journal recorded intent, not fills). After the ACK, wait a bit longer for the actual
        # fill so the journal entry carries fill status + price + timestamp. A marketable-LIMIT
        # DAY order (2026-07-03: entries are LMT, not MKT) normally fills in seconds; one that
        # doesn't is caught by the exit-manager's unfilled-order alarm on the next cycle.
        if trade.orderStatus.status not in dead:
            for _ in range(20):  # up to ~10 more seconds for the fill itself
                if trade.orderStatus.status == "Filled":
                    break
                await asyncio.sleep(0.5)
        status = trade.orderStatus.status
        reasons = [le.message for le in trade.log if getattr(le, "errorCode", 0)]
        if status not in dead:
            _afp = getattr(trade.orderStatus, "avgFillPrice", None)
            _afp_val = (float(_afp) if (status == "Filled" and _afp and _afp == _afp) else None)
            # COMMISSIONS + REAL BASIS (2026-07-03): capture the actual entry fee + the fill-based
            # cost basis so realized P&L can be reported NET of fees and entry slippage is recorded.
            # All ADDITIVE; never raises into the order path; never fabricates a fee/price.
            from exitmgr.order import commission_from_trade, compute_entry_basis
            _est_debit = round(r.limit * 100 * r.qty, 2)
            _entry_comm = commission_from_trade(trade) if status == "Filled" else None
            _efd, _eslip, _eslip_pct = compute_entry_basis(_est_debit, _afp_val, r.qty)
            fill = {
                "decision_id": r.decision_id,
                "decision_revision": r.decision_revision,
                "model_identity": r.model_identity,
                "order_ref": getattr(trade.order, "orderRef", None),
                "order_id": getattr(trade.order, "orderId", None),
                "order_status": status,
                "avg_fill_price": _afp_val,
                "fill_ts": (datetime.now(timezone.utc).isoformat() if status == "Filled" else None),
                "entry_commission": _entry_comm,          # actual IBKR entry fee (both legs), $ or None
                "entry_fill_debit": _efd,                 # real cost basis from avg_fill_price ($)
                "entry_slippage": _eslip,                 # actual - estimated ($)
                "entry_slippage_pct": _eslip_pct,
                "basis_source": ("fill" if _efd is not None else "estimate"),
            }
            self._journal_entry(r, fill=fill)
        return status, reasons

    def _journal_entry(self, r: ResolvedOrder, fill: Optional[dict] = None) -> None:
        """Append the entry to trades.log so the exit manager picks it up. Journal-at-submit is
        safe: with scope=journal the manager only acts on journal ∩ live positions, so an
        unfilled order is simply never matched. All 2026-07-01 fields are ADDITIVE -- every
        existing consumer keys on contract_id/debit and ignores unknown fields."""
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "contract_id": getattr(r.contract, "conId", None),
            "symbol": r.underlying,
            "right": r.right,
            "expiry": r.expiry,
            "strike": r.strike,
            "quantity": r.qty,
            "debit": round(r.limit * 100 * r.qty, 2),
            "profit_target_pct": (getattr(r, "tp_pct", 0.0) or None),  # clamped 25-35 band
            "stop_pct": (getattr(r, "sl_pct", 0.0) or None),           # clamped, default 30
            "conviction": getattr(r, "conviction", -1.0),
            "thesis": getattr(r, "thesis", ""),
            "decision_id": getattr(r, "decision_id", None),
            "decision_revision": getattr(r, "decision_revision", 0),
            "model_identity": getattr(r, "model_identity", None),
            "order_ref": entry_safety.decision_order_ref(r.decision_id),
            # entry-time technical_card (per-name indicators fed to the model at entry). ADDITIVE;
            # consumed by the exit manager's position view so a reload/continuation call sees the
            # ORIGINAL setup, not just live price. None when unavailable (older/mocked entries).
            "technical_card": getattr(r, "technical_card", None),
            # construction/enrichment annotations (2026-07-01)
            "underlying_price_at_entry": (getattr(r, "spot", 0.0) or None),
            "entry_delta": (getattr(r, "entry_delta", 0.0) or None),
            "entry_iv": (getattr(r, "entry_iv", 0.0) or None),
            "dte_at_entry": (getattr(r, "dte", 0) or None),
            "dte_adjusted": bool(getattr(r, "dte_adjusted", False)),
            "earnings_unchecked": bool(getattr(r, "earnings_unchecked", False)),  # A5: IV-crush blackout could not be verified
            # v2 full greeks + liquidity (ADDITIVE; every existing consumer ignores unknown keys)
            "entry_gamma": (getattr(r, "entry_gamma", 0.0) or None),
            "entry_theta": (getattr(r, "entry_theta", 0.0) or None),
            "entry_vega": (getattr(r, "entry_vega", 0.0) or None),
            "entry_ivr": (getattr(r, "entry_ivr", 0.0) or None),
            "entry_bid": (getattr(r, "entry_bid", 0.0) or None),
            "entry_ask": (getattr(r, "entry_ask", 0.0) or None),
            "entry_spread_pct": (getattr(r, "entry_spread_pct", 0.0) or None),
            "net_delta": (getattr(r, "net_delta", 0.0) or None),
            "net_theta": (getattr(r, "net_theta", 0.0) or None),
            "net_gamma": (getattr(r, "net_gamma", 0.0) or None),
            "net_vega": (getattr(r, "net_vega", 0.0) or None),
        }
        if fill:
            rec.update(fill)
        if r.short_contract is not None:
            rec["spread"] = {
                "short_con_id": getattr(r.short_contract, "conId", None),
                "short_strike": r.short_strike,
                "width": abs(r.short_strike - r.strike),
            }
        with open(self.journal_path, "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
