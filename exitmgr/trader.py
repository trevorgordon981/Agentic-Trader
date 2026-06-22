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
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set

from exitmgr.account import get_pot_snapshot
from exitmgr.risk import (
    RiskLimits, OpenPosition, ProposedTrade, GateDecision, evaluate_trade, day_pnl_pct,
)
from exitmgr.strategist import propose, TradeIdea
from exitmgr import approval, research, regime


# ---------------------------------------------------------------- pure helpers (unit-tested)

def audit(path: str, event: str, **fields) -> dict:
    rec = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(rec, default=str) + "\n")
    return rec


def day_start_pot(baselines: Dict[str, float], today: str, current_net_liq: float):
    b = dict(baselines)
    if today not in b:
        b = {today: current_net_liq}
    return b[today], b


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
                          is_long=(getattr(idea, "direction", "bullish") == "bullish"))
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


def order_summary(r: ResolvedOrder) -> str:
    if r.short_contract is not None:
        width = abs(r.short_strike - r.strike)
        return (f"BUY {r.qty}x {r.underlying} {r.expiry} {r.strike:g}/{r.short_strike:g}{r.right} "
                f"debit spread @ ${r.limit:.2f} LMT  "
                f"(max loss ~${r.limit * 100 * r.qty:,.0f}, max value ${width * 100 * r.qty:,.0f})")
    return (f"BUY {r.qty}x {r.underlying} {r.expiry} {r.strike:g}{r.right} "
            f"@ ${r.limit:.2f} LMT  (~${r.limit * 100 * r.qty:,.0f})")


def pick_spread_short(candidates, long_strike: float, long_mid: float, right: str,
                      per_trade_cap: float):
    """Choose the sold leg of a debit vertical: the WIDEST further-OTM strike whose net debit
    (long_mid - short_mid, for one spread) still fits the per-trade cap. candidates =
    [(strike, mid), ...]. Returns (short_strike, net_debit_per_share) or None."""
    otm = [(k, m) for k, m in candidates
           if m is not None and m == m and m > 0
           and ((right == "C" and k > long_strike) or (right == "P" and k < long_strike))]
    otm.sort(key=lambda km: abs(km[0] - long_strike), reverse=True)  # widest first
    for k, m in otm:
        net = long_mid - m
        if net <= 0.01:
            continue
        if net * 100 <= per_trade_cap + 1e-6:
            return k, round(net, 2)
    return None


# ---------------------------------------------------------------- orchestrator (I/O)

class Trader:
    def __init__(self, *, ib_conn, exit_manager, limits: RiskLimits, approved_names: Set[str],
                 endpoint: str, model: str, slack_token: str, slack_channel: str,
                 approver_ids: Set[str], baseline_path: str, audit_path: str,
                 approve_timeout_s: int = 900, journal_path: str = "./trades.log",
                 blocked_sector_keywords: Optional[List[str]] = None,
                 entry_limit_buffer_pct: float = 0.05):
        self.ib_conn = ib_conn
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
        # market regime + per-underlying momentum, refreshed each cycle in _market_context;
        # feeds both regime-aware sizing (entries) and the position manager (exits)
        self._regime = None
        self._price_stats = {}

    def _load_baselines(self) -> Dict[str, float]:
        p = Path(self.baseline_path)
        if p.exists():
            try:
                return json.loads(p.read_text())
            except json.JSONDecodeError:
                return {}
        return {}

    def _save_baselines(self, b: Dict[str, float]) -> None:
        Path(self.baseline_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.baseline_path).write_text(json.dumps(b))

    async def _open_positions(self) -> List[OpenPosition]:
        raw = await self.ib_conn.get_positions()
        out = []
        for pd in raw.values():
            is_index = pd.symbol.upper() in {"SPY", "QQQ", "IWM"}
            notional = abs(pd.avg_cost) * 100 * abs(pd.quantity)
            out.append(OpenPosition(pd.symbol.upper(), notional, is_index))
        return out

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

    async def run_once(self, dry_run: bool) -> None:
        pot = await get_pot_snapshot(self.ib_conn.ib)
        today = str(datetime.now(timezone.utc).date())
        baselines = self._load_baselines()
        baseline, baselines = day_start_pot(baselines, today, pot.net_liq)
        self._save_baselines(baselines)

        dp = day_pnl_pct(pot.net_liq, baseline)
        audit(self.audit_path, "cycle_start", net_liq=pot.net_liq, available=pot.available_funds,
              day_start=baseline, day_pnl_pct=round(dp, 4), dry_run=dry_run)

        positions = await self._open_positions()
        context = await self._market_context(positions, dp)

        try:
            ideas = propose(self.endpoint, self.model, context)
        except Exception as e:
            audit(self.audit_path, "strategist_error", error=str(e))
            ideas = []
        audit(self.audit_path, "proposals", count=len(ideas))

        ideas = await self._drop_blocked_sectors(ideas)

        for idea in ideas:
            plan = plan_idea(idea, net_liq=pot.net_liq, available_funds=pot.available_funds,
                             positions=positions, baseline=baseline,
                             approved_names=self.approved_names, limits=self.limits,
                             regime=self._regime)
            audit(self.audit_path, "gated", idea=asdict(idea),
                  approved=plan.gate.approved, reasons=plan.gate.reasons,
                  per_trade_cap=plan.gate.per_trade_cap)
            if not plan.gate.approved:
                continue

            # Resolve the CONCRETE order BEFORE asking -- so the human approves the real order.
            try:
                resolved = await self._resolve_order(idea, plan.gate.per_trade_cap)
            except Exception as e:
                audit(self.audit_path, "resolve_error", underlying=idea.underlying, error=str(e))
                continue
            if resolved is None:
                audit(self.audit_path, "resolve_failed", underlying=idea.underlying)
                continue

            msg = approval.format_proposal(idea, pot.net_liq, plan.gate.per_trade_cap,
                                           self.approve_timeout_s // 60, order_summary(resolved))
            if dry_run:
                approval.post_proposal(self.slack_token, self.slack_channel,
                                       "[DRY RUN — nothing will be submitted]\n" + msg)
                audit(self.audit_path, "dry_run_proposal", underlying=idea.underlying,
                      order=order_summary(resolved))
                continue

            ts = approval.post_proposal(self.slack_token, self.slack_channel, msg)
            if not ts:
                audit(self.audit_path, "slack_post_failed", underlying=idea.underlying)
                continue
            decision = approval.await_approval(self.slack_token, self.slack_channel, ts,
                                               self.approver_ids, self.approve_timeout_s)
            audit(self.audit_path, "approval", underlying=idea.underlying,
                  order=order_summary(resolved), decision=decision)
            if decision != "approve":
                continue
            try:
                await self._submit_order(resolved)
                audit(self.audit_path, "executed", underlying=idea.underlying, order=order_summary(resolved))
                positions.append(OpenPosition(idea.underlying, idea.est_debit_usd, idea.is_index))
            except Exception as e:
                audit(self.audit_path, "submit_error", underlying=idea.underlying, error=str(e))

        try:
            await self.exit_manager.run_cycle(dry_run, regime=self._regime, price_stats=self._price_stats)
        except Exception as e:
            audit(self.audit_path, "exit_cycle_error", error=str(e))

    async def _resolve_order(self, idea: TradeIdea, per_trade_cap: float) -> Optional[ResolvedOrder]:
        """Select the concrete option contract from target DTE/delta and size it. Returns a
        ResolvedOrder (no order placed yet) or None if nothing usable. Validate on first live run."""
        from exitmgr.ibkr import Option, Stock, pick_chain, strikes_near, underlying_price
        ib = self.ib_conn.ib
        right = "C" if idea.direction == "bullish" else "P"
        stk = (await ib.qualifyContractsAsync(Stock(idea.underlying, "SMART", "USD")))[0]
        params = await ib.reqSecDefOptParamsAsync(idea.underlying, "", "STK", stk.conId)
        if not params:
            return None
        p = pick_chain(params, idea.underlying)
        if p is None:
            return None
        def dte(exp):
            return abs((datetime.strptime(exp, "%Y%m%d").date() - datetime.now(timezone.utc).date()).days - idea.target_dte)
        expiry = min(sorted(p.expirations), key=dte)
        spot = await underlying_price(ib, stk)
        cands = [Option(idea.underlying, expiry, k, right, "SMART") for k in strikes_near(p.strikes, spot)]
        qualified = await ib.qualifyContractsAsync(*cands)
        tickers = await ib.reqTickersAsync(*[c for c in qualified if getattr(c, "conId", None)])
        best, best_err = None, 1e9
        by_strike = {}   # strike -> (mid, contract), for spread short-leg selection
        for tk in tickers:
            mid = (tk.bid + tk.ask) / 2 if (tk.bid and tk.ask) else (tk.last or 0)
            if not (mid == mid and mid > 0):
                continue
            k = float(getattr(tk.contract, "strike", 0) or 0)
            if k:
                by_strike[k] = (mid, tk.contract)
            g = getattr(tk, "modelGreeks", None) or getattr(tk, "lastGreeks", None)
            if g and g.delta is not None:
                err = abs(abs(g.delta) - idea.target_delta)
                if err < best_err:
                    best, best_err = (tk.contract, mid), err
        if not best:
            return None
        contract, mid = best

        if "spread" in (idea.structure or "").lower():
            pick = pick_spread_short([(k, m) for k, (m, _) in by_strike.items()],
                                     float(contract.strike), mid, right, per_trade_cap)
            if not pick:
                return None
            short_strike, net = pick
            short_contract = by_strike[short_strike][1]
            qty = max(1, int(min(idea.est_debit_usd, per_trade_cap) // (net * 100)))
            if net * 100 * qty > per_trade_cap + 1e-6:
                qty = max(1, qty - 1)
            return ResolvedOrder(idea.underlying, right, expiry, float(contract.strike),
                                 qty, net, contract,
                                 short_strike=short_strike, short_contract=short_contract)

        # size to the per-trade cap (never exceed the gate's $ cap), >=1 contract
        qty = max(1, int(min(idea.est_debit_usd, per_trade_cap) // (mid * 100)))
        if mid * 100 * qty > per_trade_cap + 1e-6:
            qty = max(1, qty - 1)
        return ResolvedOrder(idea.underlying, right, expiry, float(contract.strike),
                             qty, round(mid, 2), contract)

    async def _submit_order(self, r: ResolvedOrder) -> None:
        from exitmgr.ibkr import Order
        lmt = round(r.limit * (1 + self.entry_limit_buffer_pct), 2)   # marketable -> entry fills, not rests
        order = Order(action="BUY", orderType="LMT", totalQuantity=r.qty, lmtPrice=lmt, tif="DAY")
        if r.short_contract is not None:
            # spreads trade as ONE combo order -- legs can never fill/close independently
            combo = self.ib_conn.create_combo_contract(
                r.underlying,
                [(r.contract.conId, "BUY"), (r.short_contract.conId, "SELL")])
            self.ib_conn.ib.placeOrder(combo, order)
        else:
            self.ib_conn.ib.placeOrder(r.contract, order)
        self._journal_entry(r)

    def _journal_entry(self, r: ResolvedOrder) -> None:
        """Append the entry to trades.log so the exit manager picks it up. Journal-at-submit is
        safe: with scope=journal the manager only acts on journal ∩ live positions, so an
        unfilled order is simply never matched."""
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "contract_id": getattr(r.contract, "conId", None),
            "symbol": r.underlying,
            "right": r.right,
            "expiry": r.expiry,
            "strike": r.strike,
            "quantity": r.qty,
            "debit": round(r.limit * 100 * r.qty, 2),
        }
        if r.short_contract is not None:
            rec["spread"] = {
                "short_con_id": getattr(r.short_contract, "conId", None),
                "short_strike": r.short_strike,
                "width": abs(r.short_strike - r.strike),
            }
        with open(self.journal_path, "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
