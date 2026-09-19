"""Public API contract; production-derived narrative omitted."""
import asyncio
import inspect
import json
import os
import types
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from exitmgr import rules as R
from exitmgr.config import Config
from exitmgr.connection import IBConnection, PositionData
from exitmgr.manager import ExitManager
from exitmgr.order import OrderManager, OrderResult
from exitmgr.risk import OpenPosition, ProposedTrade, RiskLimits, evaluate_trade
from exitmgr.state import InFlightClose
from exitmgr.trader import Trader




def _expiry(days: int) -> str:
    return (datetime.now(timezone.utc).date() + timedelta(days=days)).strftime("%Y%m%d")


FAR = _expiry(45)
NEAR = _expiry(4)


def _raw(con_id, right, qty, *, symbol="SPY", avg_cost=1.75, expiry=FAR,
         sec_type="OPT", strike=500.0):
    """Public API contract; production-derived narrative omitted."""
    p = types.SimpleNamespace()
    p.contract = types.SimpleNamespace(
        conId=con_id, symbol=symbol, right=right, secType=sec_type, strike=strike,
        lastTradeDateOrContractMonth=expiry, multiplier="100" if sec_type == "OPT" else "")
    p.position = qty
    p.avgCost = avg_cost * 100 if sec_type == "OPT" else avg_cost
    return p


def _conn(rows):
    c = IBConnection(host="h", port=1, client_id=2)
    c._connected = True
    c.ib = MagicMock()
    c.ib.reqPositionsAsync = AsyncMock(return_value=list(rows))
    c.ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[])
    c.ib.reqOpenOrdersAsync = AsyncMock(return_value=[])
    c.ib.portfolio = MagicMock(return_value=[])
    return c


def _mark(con_id, price):
    return types.SimpleNamespace(contract=types.SimpleNamespace(conId=con_id),
                                 marketPrice=price, position=-1)


class _Status:
    def __init__(self, status="Filled", avg=0.0, filled=1):
        self.status = status
        self.avgFillPrice = avg
        self.filled = filled
        self.remaining = 0


class _Trade:
    """Public API contract; production-derived narrative omitted."""
    def __init__(self, order=None, status="Filled", avg=0.0, filled=1):
        self.order = order or types.SimpleNamespace(
            orderId=7, permId=0, clientId=0, orderRef=None)
        self.contract = types.SimpleNamespace(conId=0)
        self.orderStatus = _Status(status, avg, filled)
        self.fills = []
        self.log = []




CSP_JOURNAL = {
    "ts": "2026-07-20T14:00:00+00:00", "contract_id": 105, "symbol": "SPY", "right": "P",
    "strike": 500.0, "expiry": FAR, "side": "credit", "structure": "cash secured put",
    "action": "SELL", "quantity": -1, "contracts": 1, "collateral_usd": 50000.0,
    "net_credit_usd": 175.0, "max_loss_usd": 49825.0, "debit": 49825.0,
    "assignment_possible": True, "conviction": 7, "profit_target_pct": None, "stop_pct": 30.0,
    "decision_id": "dec-csp-1", "model_identity": "m3",
}
LONG_JOURNAL = {
    "ts": "2026-07-20T14:00:00+00:00", "contract_id": 101, "symbol": "RKLB", "right": "C",
    "strike": 20.0, "expiry": FAR, "quantity": 2, "debit": 820.0, "conviction": 6,
    "decision_id": "dec-long-1",
}


def _mgr(tmp_path, journal_lines=(), *, rows=(), marks=(), quotes=None):
    cfg = Config()
    cfg.dry_run = False
    cfg.loop_mode = False
    cfg.manage_positions = False
    cfg.journal.path = str(tmp_path / "trades.log")
    cfg.state.path = str(tmp_path / "state.json")
    cfg.kill_switch.path = str(tmp_path / "KILL")
    (tmp_path / "trades.log").write_text(
        "".join(json.dumps(x) + "\n" for x in journal_lines))
    mgr = ExitManager(cfg)
    mgr.ib_conn = _conn(rows)
    mgr.ib_conn.ib.portfolio = MagicMock(return_value=list(marks))
    mgr.ib_conn.get_open_orders = AsyncMock(return_value={})
    mgr.ib_conn.fetch_quotes = AsyncMock(return_value=dict(quotes or {}))
    mgr.ib_conn.create_contract = MagicMock(
        side_effect=lambda cid, symbol=None, right=None: types.SimpleNamespace(
            conId=cid, symbol=symbol, right=right, secType="OPT"))
    mgr.ib_conn.create_limit_order = MagicMock(
        side_effect=lambda action, qty, px: types.SimpleNamespace(
            action=action, totalQuantity=qty, lmtPrice=px, orderType="LMT", orderId=0))
    mgr.ib_conn.create_market_order = MagicMock(
        side_effect=lambda action, qty: types.SimpleNamespace(
            action=action, totalQuantity=qty, lmtPrice=0.0, orderType="MKT", orderId=0))
    mgr.ib_conn.reserve_order_id = MagicMock(return_value=77)


    mgr.order_manager.ib_conn = mgr.ib_conn
    mgr._reconcile_on_startup = AsyncMock(return_value=True)
    mgr._capture_external_fills_safe = AsyncMock(return_value=None)
    mgr._alert_unfilled_orders = AsyncMock(return_value=None)
    return mgr, cfg


def _exits(cfg):
    p = os.path.join(os.path.dirname(cfg.journal.path) or ".", "exits.log")
    if not os.path.exists(p):
        return []
    with open(p) as f:
        return [json.loads(l) for l in f if l.strip()]


def _capture_closes(mgr):
    """Public API contract; production-derived narrative omitted."""
    calls = []

    async def _stub(**kw):
        calls.append(kw)
        return OrderResult(success=True, message="", con_id=kw["con_id"], order_id=4242,
                           trade=None)

    mgr.order_manager.place_close_order = _stub
    return calls






def test_a_csp_at_its_stop_gets_a_buy_to_close_placed(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    rows = [_raw(105, "P", -1, symbol="SPY", strike=500.0)]
    mgr, _ = _mgr(tmp_path, [CSP_JOURNAL], rows=rows, marks=[_mark(105, 4.00)],
                  quotes={105: {"price": 4.00, "bid": 3.90, "ask": 4.10}})
    calls = _capture_closes(mgr)
    asyncio.run(mgr.run_cycle(dry_run=False))
    assert len(calls) == 1, "a triggered short close was never placed"
    kw = calls[0]
    assert kw["con_id"] == 105
    assert kw["short_close"] is True, "the close did not go through order.py's short path"
    assert kw["ask"] == 4.10, "a buy-to-close must be anchored to the ASK, never the bid"
    assert kw["entry_debit"] == 175.0, "the basis must be the CREDIT, not the journal debit"
    assert kw["exit_context"]["close_qty"] == 1 > 0
    assert kw["exit_context"]["position_qty"] == 1 > 0
    assert kw["spread"] is None


def test_the_placed_close_is_evaluated_by_the_short_rule_family(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    rows = [_raw(105, "P", -1, symbol="SPY", strike=500.0)]
    mgr, _ = _mgr(tmp_path, [CSP_JOURNAL], rows=rows, marks=[_mark(105, 4.00)],
                  quotes={105: {"price": 4.00, "bid": 3.90, "ask": 4.10}})
    calls = _capture_closes(mgr)
    asyncio.run(mgr.run_cycle(dry_run=False))
    ctx = calls[0]["exit_context"]
    assert ctx["trigger_type"] == "stop"
    assert ctx["is_short"] is True
    assert ctx["trigger_pnl_pct"] < 0, "a $4.00 buyback against a $1.75 credit is a LOSS"
    assert abs(ctx["trigger_pnl_pct"] - (-128.57)) < 0.05
    assert mgr._short_managed[105]["trigger"] == "stop"
    assert mgr._short_managed[105]["placed"] is True


def test_the_close_reaches_the_broker_as_a_BUY_with_a_positive_quantity(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    rows = [_raw(105, "P", -1, symbol="SPY", strike=500.0)]
    mgr, _ = _mgr(tmp_path, [CSP_JOURNAL], rows=rows, marks=[_mark(105, 4.00)],
                  quotes={105: {"price": 4.00, "bid": 3.90, "ask": 4.10}})
    live = types.SimpleNamespace(contract=types.SimpleNamespace(conId=105, secType="OPT",
                                                               right="P", symbol="SPY"),
                                 position=-1, marketPrice=4.00)
    mgr.ib_conn.ib.portfolio = MagicMock(return_value=[live])
    placed = []

    async def _place(contract, order):
        placed.append((contract, order))
        return _Trade(order, status="Submitted")

    mgr.ib_conn.place_order = _place
    asyncio.run(mgr.run_cycle(dry_run=False))
    assert len(placed) == 1
    _c, order = placed[0]
    assert order.action == "BUY"
    assert order.totalQuantity == 1 and order.totalQuantity > 0
    assert not isinstance(order.totalQuantity, bool)
    assert getattr(order, "lmtPrice", 1) >= 0


def test_dry_run_evaluates_the_short_but_places_nothing(tmp_path):
    rows = [_raw(105, "P", -1, symbol="SPY", strike=500.0)]
    mgr, _ = _mgr(tmp_path, [CSP_JOURNAL], rows=rows, marks=[_mark(105, 4.00)],
                  quotes={105: {"price": 4.00, "ask": 4.10}})
    calls = _capture_closes(mgr)
    asyncio.run(mgr.run_cycle(dry_run=True))
    assert calls == []
    assert mgr._short_managed[105]["trigger"] == "stop"


def test_a_short_with_a_close_already_in_flight_is_not_closed_twice(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    rows = [_raw(105, "P", -1, symbol="SPY", strike=500.0)]
    mgr, _ = _mgr(tmp_path, [CSP_JOURNAL], rows=rows, marks=[_mark(105, 4.00)],
                  quotes={105: {"price": 4.00, "ask": 4.10}})
    mgr.state_manager.state.add_in_flight(InFlightClose(
        con_id=105, order_id=99, remaining_qty=1, entry_debit=175.0,
        placed_at=datetime.now(timezone.utc).isoformat(), exit_context={"close_qty": 1}))
    calls = _capture_closes(mgr)
    asyncio.run(mgr.run_cycle(dry_run=False))
    assert calls == []
    assert mgr._short_managed[105]["reason"] == "close_already_in_flight"


def test_a_spread_short_leg_is_never_covered_alone(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    spread_long = dict(LONG_JOURNAL, contract_id=101,
                       spread={"short_con_id": 104, "short_strike": 25.0, "width": 5.0})
    rows = [_raw(104, "C", -2, symbol="RKLB", strike=25.0)]
    mgr, _ = _mgr(tmp_path, [spread_long], rows=rows, marks=[_mark(104, 9.99)],
                  quotes={104: {"price": 9.99, "ask": 10.10}})
    calls = _capture_closes(mgr)
    asyncio.run(mgr.run_cycle(dry_run=False))
    assert calls == []
    assert mgr._short_managed[104]["reason"] == "spread_short_leg_closes_with_its_long"


def test_a_reconcile_inconsistent_short_is_withheld(tmp_path):
    rows = [_raw(105, "P", -1, symbol="SPY", strike=500.0)]
    mgr, _ = _mgr(tmp_path, [CSP_JOURNAL], rows=rows, marks=[_mark(105, 4.00)],
                  quotes={105: {"price": 4.00, "ask": 4.10}})
    calls = _capture_closes(mgr)

    async def _bad_reconcile():
        mgr._reconcile_bad_con_ids = {105}
        return False

    mgr._reconcile_on_startup = _bad_reconcile
    asyncio.run(mgr.run_cycle(dry_run=False))
    assert calls == []


def test_the_short_path_runs_on_an_account_holding_nothing_else(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    rows = [_raw(105, "P", -1, symbol="SPY", strike=500.0)]
    mgr, _ = _mgr(tmp_path, [CSP_JOURNAL], rows=rows, marks=[_mark(105, 4.00)],
                  quotes={105: {"price": 4.00, "ask": 4.10}})
    calls = _capture_closes(mgr)
    asyncio.run(mgr.run_cycle(dry_run=False))
    longs, shorts, _ = asyncio.run(mgr._fetch_position_book())
    assert longs == {} and set(shorts) == {105}
    assert len(calls) == 1


def test_a_short_time_stop_fires_on_dte(tmp_path):
    je = dict(CSP_JOURNAL, expiry=NEAR)
    rows = [_raw(105, "P", -1, symbol="SPY", strike=500.0, expiry=NEAR)]
    mgr, _ = _mgr(tmp_path, [je], rows=rows, marks=[_mark(105, 0.90)],
                  quotes={105: {"price": 0.90, "ask": 1.00}})

    mgr.config.rules = replace(mgr.config.rules, time_stop_days=10)
    calls = _capture_closes(mgr)
    asyncio.run(mgr.run_cycle(dry_run=False))
    assert len(calls) == 1
    assert calls[0]["exit_context"]["trigger_type"] == "time_stop"

    assert calls[0]["market"] is False






@pytest.mark.parametrize("mark", [1.70, 1.00, 0.40, 0.10, 0.01])
def test_a_profitable_csp_never_gets_a_stop(tmp_path, mark):
    """Public API contract; production-derived narrative omitted."""
    rows = [_raw(105, "P", -1, symbol="SPY", strike=500.0)]
    mgr, _ = _mgr(tmp_path, [CSP_JOURNAL], rows=rows, marks=[_mark(105, mark)],
                  quotes={105: {"price": mark, "ask": mark + 0.10}})
    calls = _capture_closes(mgr)
    asyncio.run(mgr.run_cycle(dry_run=False))
    assert calls == [], f"a WINNING CSP (mark {mark} vs 1.75 credit) was closed"
    assert mgr._short_managed[105]["trigger"] is None
    assert mgr._short_managed[105]["managed"] is True


def test_the_long_stop_WOULD_have_fired_on_that_same_winner(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    long_style = R.evaluate_stop(0.10, 175.0, 1, stop_pct=30.0)
    assert long_style is not None, "sanity: the long rule does fire on this tape"
    short_style = R.evaluate_short_stop(0.10, 175.0, 1, stop_pct=100.0)
    assert short_style is None, "the short rule must not fire on a winner"
    assert R.short_pnl_pct(0.10, 175.0, 1) > 90


def test_no_mechanical_profit_target_is_introduced_on_the_credit_side(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr, _ = _mgr(tmp_path, [CSP_JOURNAL])


    mgr.config.rules = replace(mgr.config.rules,
                               trailing=replace(mgr.config.rules.trailing, enabled=True,
                                                activation_gain_pct=20.0,
                                                giveback_fraction=0.4),
                               profit_target_pct=30.0)
    r = mgr._short_exit_rules(CSP_JOURNAL)
    assert r.profit_target_pct is None
    assert r.trailing.enabled is False, "no persisted TROUGH exists; a trail must not be armed"

    assert R.evaluate_short_position(105, "SPY", -1, 175.0, 0.01, 45, r) is None


def test_the_short_stop_is_a_percent_of_credit_not_the_debit_shaped_journal_value(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr, _ = _mgr(tmp_path, [CSP_JOURNAL])
    assert mgr._short_exit_rules(CSP_JOURNAL).stop_pct == 100.0
    assert R.evaluate_short_stop(2.28, 175.0, 1, stop_pct=100.0) is None
    assert R.evaluate_short_stop(2.28, 175.0, 1, stop_pct=30.0) is not None


def test_an_explicit_short_stop_override_is_honoured(tmp_path):
    mgr, _ = _mgr(tmp_path, [CSP_JOURNAL])
    assert mgr._short_exit_rules(dict(CSP_JOURNAL, short_stop_pct=200.0)).stop_pct == 200.0
    mgr.config.rules = replace(mgr.config.rules)
    setattr(mgr.config.rules, "short_stop_pct", 150.0)
    assert mgr._short_exit_rules(CSP_JOURNAL).stop_pct == 150.0
    assert mgr._short_exit_rules(dict(CSP_JOURNAL, short_stop_pct=0)).stop_pct == 150.0


def test_a_trigger_that_is_not_short_signed_is_refused(tmp_path, monkeypatch, capsys):
    """Public API contract; production-derived narrative omitted."""
    import exitmgr.manager as M
    rows = [_raw(105, "P", -1, symbol="SPY", strike=500.0)]
    mgr, _ = _mgr(tmp_path, [CSP_JOURNAL], rows=rows, marks=[_mark(105, 0.40)],
                  quotes={105: {"price": 0.40, "ask": 0.50}})
    long_trigger = R.ExitTrigger(con_id=105, trigger_type="stop", current_price=0.40,
                                 entry_debit=175.0, current_value=40.0, pnl_pct=-77.0,
                                 message="LONG-family stop on a short")
    assert long_trigger.is_short is False
    monkeypatch.setattr(M, "evaluate_short_position", lambda **kw: long_trigger)
    calls = _capture_closes(mgr)
    asyncio.run(mgr.run_cycle(dry_run=False))
    assert calls == [], "a long-family trigger was allowed to place a buy-to-close"
    assert "not short-signed" in capsys.readouterr().out






def _trader(tmp_path, rows, journal_lines=()):
    t = Trader.__new__(Trader)
    t.ib_conn = _conn(rows)
    t.journal_path = str(tmp_path / "trades.log")
    t.audit_path = str(tmp_path / "audit.jsonl")
    (tmp_path / "trades.log").write_text(
        "".join(json.dumps(x) + "\n" for x in journal_lines))
    return t


def test_a_csp_counts_toward_the_concurrent_book_on_every_cycle(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    t = _trader(tmp_path, [_raw(105, "P", -1, symbol="SPY", strike=500.0)], [CSP_JOURNAL])
    for _cycle in range(3):
        book = asyncio.run(t._open_positions())
        assert len(book) == 1, f"the CSP went uncounted on cycle {_cycle}"
        assert book[0].underlying == "SPY"
        assert book[0].is_credit is True


def test_the_csp_is_valued_at_collateral_not_premium_and_not_max_loss(tmp_path):
    t = _trader(tmp_path, [_raw(105, "P", -1, symbol="SPY", strike=500.0, avg_cost=1.75)],
                [CSP_JOURNAL])
    book = asyncio.run(t._open_positions())
    assert book[0].notional == 50_000.0
    assert book[0].notional != 175.0
    assert book[0].notional != 49_825.0


def test_collateral_falls_back_to_the_broker_strike_when_unjournaled(tmp_path):
    t = _trader(tmp_path, [_raw(900, "P", -2, symbol="AMD", strike=140.0)], [])
    book = asyncio.run(t._open_positions())
    assert len(book) == 1
    assert book[0].notional == 28_000.0
    assert book[0].is_index is False


def test_an_unreadable_collateral_is_still_counted_but_never_guessed(tmp_path):
    t = _trader(tmp_path, [_raw(900, "P", -1, symbol="AMD", strike=0.0)], [])
    book = asyncio.run(t._open_positions())
    assert len(book) == 1 and book[0].notional == 0.0
    events = [json.loads(l)["event"]
              for l in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert "short_collateral_unreadable" in events


def test_the_concurrent_cap_actually_binds_with_csps_open(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    rows = [_raw(900 + i, "P", -1, symbol=s, strike=100.0)
            for i, s in enumerate(("AMD", "MUTX", "SYML", "F"))]
    t = _trader(tmp_path, rows, [])
    book = asyncio.run(t._open_positions())
    assert len(book) == 4
    d = evaluate_trade(ProposedTrade("SPY", 300.0, True, conviction=7),
                       net_liq=100_000.0, available_funds=90_000.0, open_positions=book,
                       pot_day_start=100_000.0, approved_names=set(), limits=RiskLimits())
    assert any("max concurrent" in r for r in d.reasons)


def test_the_csp_never_reaches_the_long_premium_deployment_book(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    from exitmgr import construction
    (tmp_path / "trades.log").write_text(json.dumps(CSP_JOURNAL) + "\n")
    t = _trader(tmp_path, [_raw(105, "P", -1, symbol="SPY", strike=500.0)], [CSP_JOURNAL])
    asyncio.run(t._open_positions())
    entry_book_positions = asyncio.run(t.ib_conn.get_positions())
    assert 105 not in entry_book_positions
    items = construction.open_book_items(entry_book_positions, t.journal_path, [])
    assert items == {}, "a credit row leaked into the long-premium deployment book"


def test_a_short_call_is_not_folded_into_the_concurrent_book(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    t = _trader(tmp_path, [_raw(104, "C", -2, symbol="RKLB", strike=25.0)], [])
    assert asyncio.run(t._open_positions()) == []


def test_a_journaled_spread_short_leg_is_not_double_counted(tmp_path):


    spread_long = dict(LONG_JOURNAL, right="P", strike=25.0,
                       spread={"short_con_id": 555, "short_strike": 20.0, "width": 5.0})
    rows = [_raw(101, "P", 2, symbol="RKLB", strike=25.0, avg_cost=6.0,
                 expiry=spread_long["expiry"]),
            _raw(555, "P", -2, symbol="RKLB", strike=20.0, avg_cost=1.90,
                 expiry=spread_long["expiry"])]
    t = _trader(tmp_path, rows, [spread_long])
    book = asyncio.run(t._open_positions())
    assert len(book) == 1 and book[0].notional == 820.0


def test_assigned_stock_is_not_a_short_position(tmp_path):
    stk = _raw(106, "", 100, symbol="SPY", sec_type="STK", strike=0.0, avg_cost=498.25)
    t = _trader(tmp_path, [stk], [CSP_JOURNAL])
    assert asyncio.run(t._open_positions()) == []


def test_a_broker_read_failure_refuses_the_book_LOUDLY(tmp_path):
    t = _trader(tmp_path, [], [])
    _real = t.ib_conn.get_positions

    calls = []
    async def _long_ok_short_broken(*a, **kw):
        calls.append(kw)
        if kw.get("include_short"):
            raise RuntimeError("socket died")
        return await _real(*a, **kw)

    t.ib_conn.get_positions = _long_ok_short_broken
    with pytest.raises(RuntimeError, match="socket died"):
        asyncio.run(t._open_positions())
    assert calls == [{"include_short": True}]
    events = [json.loads(l) for l in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert any(event["event"] == "risk_position_read_error" and event.get("error") == "socket died"
               for event in events)






def test_the_basis_joins_from_a_real_journal_row(tmp_path):
    mgr, _ = _mgr(tmp_path, [CSP_JOURNAL])
    assert mgr._short_entry_credit(105, CSP_JOURNAL, 1) == 175.0


def test_the_basis_is_never_the_debit_field_of_a_credit_row(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr, _ = _mgr(tmp_path, [CSP_JOURNAL])
    credit = mgr._short_entry_credit(105, CSP_JOURNAL, 1)
    assert credit != CSP_JOURNAL["debit"] and credit != CSP_JOURNAL["max_loss_usd"]
    fired_on_credit = R.evaluate_short_stop(4.00, credit, 1, 100.0)
    fired_on_debit = R.evaluate_short_stop(4.00, CSP_JOURNAL["debit"], 1, 100.0)
    assert fired_on_credit is not None and fired_on_debit is None


def test_a_missing_basis_refuses_loudly_and_leaves_the_short_unmanaged(tmp_path, capsys):
    """Public API contract; production-derived narrative omitted."""
    thin = {k: v for k, v in CSP_JOURNAL.items()
            if k not in ("net_credit_usd", "collateral_usd", "max_loss_usd",
                         "avg_fill_price", "limit", "entry_price")}
    rows = [_raw(105, "P", -1, symbol="SPY", strike=500.0)]
    mgr, _ = _mgr(tmp_path, [thin], rows=rows, marks=[_mark(105, 4.00)],
                  quotes={105: {"price": 4.00, "ask": 4.10}})
    calls = _capture_closes(mgr)
    asyncio.run(mgr.run_cycle(dry_run=False))
    out = capsys.readouterr().out
    assert "NO usable entry-credit basis" in out
    assert "UNMANAGED" in out
    assert calls == [], "a close was placed against a basis we do not have"
    assert mgr._short_managed[105]["managed"] is False
    assert mgr._short_managed[105]["reason"] == "no_entry_credit_basis"


def test_an_unjournaled_short_is_reported_unmanaged_not_closed(tmp_path):
    rows = [_raw(900, "P", -1, symbol="AMD", strike=140.0)]
    mgr, _ = _mgr(tmp_path, [], rows=rows, marks=[_mark(900, 9.00)],
                  quotes={900: {"price": 9.00, "ask": 9.10}})
    calls = _capture_closes(mgr)
    asyncio.run(mgr.run_cycle(dry_run=False))
    assert calls == []
    row = [r for r in mgr._short_report if r["con_id"] == 900][0]
    assert row["managed"] is False and row["unmanaged_reason"] == "no_entry_credit_basis"


def test_no_mark_this_cycle_means_unmanaged_not_a_guessed_price(tmp_path, capsys):
    rows = [_raw(105, "P", -1, symbol="SPY", strike=500.0)]
    mgr, _ = _mgr(tmp_path, [CSP_JOURNAL], rows=rows, marks=[], quotes={})
    calls = _capture_closes(mgr)
    asyncio.run(mgr.run_cycle(dry_run=False))
    assert calls == []
    assert mgr._short_managed[105]["reason"] == "no_mark_this_cycle"


    out = capsys.readouterr().out
    assert "NOT EVALUATED (no_mark_this_cycle)" in out
    assert "short exit evaluation failed" not in out


def test_the_report_flag_and_the_exit_path_can_never_disagree(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    rows = [_raw(105, "P", -1, symbol="SPY", strike=500.0)]
    mgr, _ = _mgr(tmp_path, [CSP_JOURNAL], rows=rows, marks=[_mark(105, 1.00)],
                  quotes={105: {"price": 1.00, "ask": 1.10}})
    _capture_closes(mgr)
    asyncio.run(mgr.run_cycle(dry_run=False))
    reported = [r for r in mgr._short_report if r["con_id"] == 105][0]
    assert reported["managed"] is mgr._short_managed[105]["managed"] is True
    assert reported["unmanaged_reason"] is None






def _finalize(tmp_path, journal, *, fill_px, credit, qty=1, is_short=True, alerts=None):
    mgr, cfg = _mgr(tmp_path, [journal])
    if alerts is not None:
        def _spy(symbol, trigger, client_msg_id=None):
            alerts.append({"symbol": symbol, "trigger_type": trigger.trigger_type,
                           "pnl_pct": trigger.pnl_pct})
            return True
        mgr._post_exit_alert = _spy
    ctx = {
        "symbol": journal["symbol"], "reason": "stop", "trigger_type": "stop",
        "trigger_message": "m", "trigger_pnl_pct": 0.0, "close_qty": qty, "position_qty": qty,
        "entry_debit": credit, "journal_entry": dict(journal),
        "extra": {"partial": False, "close_qty": qty, "remaining_qty": 0},
    }
    if is_short:
        ctx.update({"is_short": True, "close_action": "BUY"})
        ctx["extra"]["is_short"] = True
        ctx["extra"]["entry_credit_usd"] = credit
    inf = InFlightClose(con_id=journal["contract_id"], order_id=7, remaining_qty=qty,
                        entry_debit=credit,
                        placed_at=datetime.now(timezone.utc).isoformat(), exit_context=ctx,
                        client_id=0, identity_version=1)
    mgr.state_manager.state.add_in_flight(inf)
    ok = mgr._finalize_in_flight_exit(
        journal["contract_id"], inf,
        _Trade(status="Filled", avg=fill_px, filled=qty),
        broker_flat_evidence={
            "schema": "position_flat_evidence.v1",
            "source": "ibkr_reqPositions",
            "observed_at": "2026-08-25T17:00:00+00:00",
            "absent_con_ids": [journal["contract_id"]],
        })
    return ok, _exits(cfg)[-1] if _exits(cfg) else None


def test_a_cheap_buyback_is_booked_as_a_GAIN(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    ok, row = _finalize(tmp_path, CSP_JOURNAL, fill_px=0.10, credit=175.0)
    assert ok and row["realized_pnl"] == 165.0 > 0
    assert row["realized_pnl_pct"] > 0
    assert row["side"] == "credit" and row["is_short"] is True
    assert row["pnl_basis"] == "credit_received_minus_close_cost"


def test_an_expensive_buyback_is_booked_as_a_LOSS(tmp_path):
    ok, row = _finalize(tmp_path, CSP_JOURNAL, fill_px=4.00, credit=175.0)
    assert ok and row["realized_pnl"] == -225.0 < 0
    assert row["realized_pnl_pct"] < 0


def test_the_trigger_pnl_carried_into_the_record_is_short_signed(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    _ok, row = _finalize(tmp_path, CSP_JOURNAL, fill_px=0.10, credit=175.0)
    assert row["realized_pnl_pct"] == pytest.approx(94.29, abs=0.05)


def test_pct_of_max_loss_is_not_silently_the_pct_of_credit(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    _ok, row = _finalize(tmp_path, CSP_JOURNAL, fill_px=0.10, credit=175.0)
    assert row["max_loss_usd"] == 49_825.0
    assert row["realized_pct_of_max_loss"] == pytest.approx(0.33, abs=0.01)
    assert row["realized_pct_of_credit"] == pytest.approx(94.29, abs=0.05)


def test_the_credit_branch_survives_a_journal_row_that_thinned_out(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    thin = {k: v for k, v in CSP_JOURNAL.items()
            if k not in ("net_credit_usd", "collateral_usd", "max_loss_usd")}
    ok, row = _finalize(tmp_path, thin, fill_px=0.10, credit=175.0)
    assert ok and row["is_short"] is True and row["realized_pnl"] == 165.0


def test_a_long_finalization_is_still_long_signed(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    ok, row = _finalize(tmp_path, LONG_JOURNAL, fill_px=6.15, credit=820.0, qty=2,
                        is_short=False)
    assert ok
    assert row["proceeds"] == 1230.0
    assert row["realized_pnl"] == 410.0
    assert row["realized_pnl_pct"] == 50.0
    for k in ("side", "is_short", "entry_credit_usd", "close_cost_usd", "pnl_basis"):
        assert k not in row, f"a credit-only field ({k}) leaked onto a long exit record"


def test_the_pnl_that_reaches_the_alert_and_the_reload_ticket_is_short_signed(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    win, loss, longs = [], [], []
    (tmp_path / "w").mkdir(); (tmp_path / "l").mkdir(); (tmp_path / "d").mkdir()
    _finalize(tmp_path / "w", CSP_JOURNAL, fill_px=0.10, credit=175.0, alerts=win)
    _finalize(tmp_path / "l", CSP_JOURNAL, fill_px=4.00, credit=175.0, alerts=loss)
    _finalize(tmp_path / "d", LONG_JOURNAL, fill_px=6.15, credit=820.0, qty=2,
              is_short=False, alerts=longs)
    assert win and win[0]["pnl_pct"] == pytest.approx(94.29, abs=0.05), \
        "a $0.10 buyback of a $1.75 credit is a WIN and must be alerted as one"
    assert loss and loss[0]["pnl_pct"] == pytest.approx(-128.57, abs=0.05)
    assert longs and longs[0]["pnl_pct"] == pytest.approx(50.0, abs=0.01)


def test_a_credit_fill_with_no_journal_row_is_still_booked_credit_signed(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    orphan = {"contract_id": 777, "symbol": "AMD"}
    ok, row = _finalize(tmp_path, orphan, fill_px=0.10, credit=175.0)
    assert ok
    assert row["is_short"] is True and row["side"] == "credit"
    assert row["realized_pnl"] == 165.0 > 0
    assert row["entry_credit_usd"] == 175.0






def test_the_two_manager_guard_expressions_are_still_intact():
    """Public API contract; production-derived narrative omitted."""
    alert_src = inspect.getsource(ExitManager._alert_unfilled_orders)
    final_src = inspect.getsource(ExitManager._finalize_in_flight_exit)
    assert "inf.remaining_qty <= 0" in alert_src






    assert '_durable_qty(ctx, "close_qty"' in final_src and "planned_qty <= 0" in final_src


def test_the_short_close_context_is_normalized_positive_end_to_end(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    rows = [_raw(105, "P", -1, symbol="SPY", strike=500.0)]
    mgr, _ = _mgr(tmp_path, [CSP_JOURNAL], rows=rows, marks=[_mark(105, 4.00)],
                  quotes={105: {"price": 4.00, "ask": 4.10}})
    live = types.SimpleNamespace(contract=types.SimpleNamespace(conId=105, secType="OPT",
                                                               right="P", symbol="SPY"),
                                 position=-1, marketPrice=4.00)
    mgr.ib_conn.ib.portfolio = MagicMock(return_value=[live])
    mgr.ib_conn.place_order = AsyncMock(side_effect=lambda c, o: _Trade(o, status="Submitted"))
    asyncio.run(mgr.run_cycle(dry_run=False))
    inf = mgr.state_manager.state.get_in_flight(105)
    assert inf is not None
    assert inf.remaining_qty > 0
    assert int(inf.exit_context["close_qty"]) > 0
    assert inf.exit_context["is_short"] is True












def _diff_risk_gate():
    lim = RiskLimits()
    books = [
        [],
        [OpenPosition("RKLB", 820.0, False)],
        [OpenPosition("SPY", 300.0, True), OpenPosition("NVDA", 1500.0, False)],
        [OpenPosition("RKLB", 500.0, False), OpenPosition("NVDA", 500.0, False),
         OpenPosition("QQQ", 400.0, True), OpenPosition("AMD", 600.0, False)],
    ]
    trades = [
        ProposedTrade("SPY", 300.0, True, conviction=6),
        ProposedTrade("RKLB", 800.0, False, conviction=8),
        ProposedTrade("NVDA", 5000.0, False, conviction=4),
        ProposedTrade("AMD", 120.0, False, conviction=9, stop_pct=30.0),
    ]
    out = []
    for bi, book in enumerate(books):
        for ti, tr in enumerate(trades):
            d = evaluate_trade(tr, net_liq=20_000.0, available_funds=15_000.0,
                               open_positions=book, pot_day_start=20_000.0,
                               approved_names={"RKLB", "NVDA", "AMD"}, limits=lim)
            out.append([bi, ti, bool(d.approved), sorted(d.reasons),
                        round(d.pot_value, 4), round(d.per_trade_cap, 4)])
    return out


def _diff_open_positions(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    books = [
        ([], []),
        ([_raw(101, "C", 2, symbol="RKLB", strike=20.0, avg_cost=4.10)], [LONG_JOURNAL]),
        ([_raw(101, "C", 2, symbol="RKLB", strike=20.0, avg_cost=4.10),
          _raw(102, "C", 1, symbol="NVDA", strike=180.0, avg_cost=12.30),
          _raw(103, "P", 3, symbol="QQQ", strike=560.0, avg_cost=2.05)], [LONG_JOURNAL]),
    ]
    out = []
    for i, (rows, journal) in enumerate(books):
        d = tmp_path / f"op{i}"
        d.mkdir()
        t = _trader(d, rows, journal)
        book = asyncio.run(t._open_positions())
        out.append(sorted([[p.underlying, round(p.notional, 4), bool(p.is_index)]
                           for p in book]))
    return out


_DIFF_STABLE_EXIT_KEYS = (
    "contract_id", "symbol", "structure", "quantity", "entry_debit", "exit_price_per_share",
    "proceeds", "realized_pnl", "realized_pnl_pct", "reason", "conviction", "fill_status",
    "commission_unknown", "realized_pnl_net", "basis_source", "right", "strike",
)


def _stable_exit(row):
    return {k: row.get(k) for k in _DIFF_STABLE_EXIT_KEYS}


def _diff_log_exit(tmp_path):
    out = []
    for i, (journal, cid, sym, px, qty, reason) in enumerate((
            (LONG_JOURNAL, 101, "RKLB", 6.15, 2, "profit_target"),
            (LONG_JOURNAL, 101, "RKLB", 2.05, 2, "stop"),
            (LONG_JOURNAL, 101, "RKLB", 0.0, 2, "expired"),
    )):
        d = tmp_path / f"le{i}"
        d.mkdir()
        mgr, cfg = _mgr(d, [journal])
        trig = types.SimpleNamespace(trigger_type=reason, pnl_pct=0.0, message="")
        mgr._log_exit(cid, sym, trig, exit_price_per_share=px, quantity=qty, reason=reason)
        rows = _exits(cfg)
        out.append([_stable_exit(r) for r in rows])
    return out


def _diff_close_order(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    om = OrderManager(MagicMock(), MagicMock())
    om.ib_conn.create_limit_order = MagicMock(
        side_effect=lambda a, q, p: {"kind": "LMT", "action": a, "qty": q, "px": round(p, 4)})
    om.ib_conn.create_market_order = MagicMock(
        side_effect=lambda a, q: {"kind": "MKT", "action": a, "qty": q})
    out = []
    for market in (False, True):
        for bid in (None, 0.0, 1.90, float("nan")):
            for tt in (None, "stop", "profit_target", "time_stop"):
                out.append([market, (None if bid is None or bid != bid else bid), tt,
                            om._build_close_order(2, 2.00, market, bid=bid, trigger_type=tt)])
    return out


def _diff_run_cycle(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    scenarios = [

        ("stop_fires", [_raw(101, "C", 2, symbol="RKLB", strike=20.0, avg_cost=4.10)],
         [LONG_JOURNAL], {101: 2.50}, {101: {"price": 2.50, "bid": 2.40, "ask": 2.60}}),
        ("no_trigger", [_raw(101, "C", 2, symbol="RKLB", strike=20.0, avg_cost=4.10)],
         [LONG_JOURNAL], {101: 4.60}, {101: {"price": 4.60, "bid": 4.50, "ask": 4.70}}),
        ("time_stop", [_raw(101, "C", 2, symbol="RKLB", strike=20.0, avg_cost=4.10,
                            expiry=NEAR)],
         [dict(LONG_JOURNAL, expiry=NEAR)], {101: 4.60},
         {101: {"price": 4.60, "bid": 4.50, "ask": 4.70}}),
        ("two_positions",
         [_raw(101, "C", 2, symbol="RKLB", strike=20.0, avg_cost=4.10),
          _raw(102, "C", 1, symbol="NVDA", strike=180.0, avg_cost=12.30)],
         [LONG_JOURNAL, dict(LONG_JOURNAL, contract_id=102, symbol="NVDA", quantity=1,
                             debit=1230.0, decision_id="dec-long-2")],
         {101: 2.50, 102: 15.00},
         {101: {"price": 2.50, "bid": 2.40, "ask": 2.60},
          102: {"price": 15.0, "bid": 14.9, "ask": 15.1}}),
    ]
    out = []
    for name, rows, journal, marks, quotes in scenarios:
        d = tmp_path / f"rc_{name}"
        d.mkdir()
        mgr, cfg = _mgr(d, journal, rows=rows,
                        marks=[_mark(c, p) for c, p in marks.items()], quotes=quotes)





        mgr._maybe_record_session_close = lambda *a, **k: None
        calls = _capture_closes(mgr)
        asyncio.run(mgr.run_cycle(dry_run=False))
        captured = []
        for kw in calls:
            ctx = kw.get("exit_context") or {}
            captured.append({
                "con_id": kw.get("con_id"), "symbol": kw.get("symbol"),
                "quantity": kw.get("quantity"), "limit_price": round(kw.get("limit_price"), 4),
                "entry_debit": round(kw.get("entry_debit"), 4), "market": kw.get("market"),
                "right": kw.get("right"), "bid": kw.get("bid"),
                "trigger_type": kw.get("trigger_type"), "spread": kw.get("spread"),
                "short_close": kw.get("short_close", False), "ask": kw.get("ask"),
                "ctx_reason": ctx.get("reason"), "ctx_close_qty": ctx.get("close_qty"),
                "ctx_position_qty": ctx.get("position_qty"),
                "ctx_entry_debit": round(float(ctx.get("entry_debit") or 0.0), 4),
                "ctx_is_short": ctx.get("is_short"),
            })
        infs = sorted([[int(c), i.remaining_qty, round(float(i.entry_debit), 4)]
                       for c, i in mgr.state_manager.state.in_flight.items()])
        out.append([name, captured, infs, [_stable_exit(r) for r in _exits(cfg)]])
    return out


def _debit_differential(tmp_path):
    return {
        "risk_gate": _diff_risk_gate(),
        "open_positions": _diff_open_positions(tmp_path),
        "log_exit": _diff_log_exit(tmp_path),
        "close_order": _diff_close_order(tmp_path),
        "run_cycle": _diff_run_cycle(tmp_path),
    }


GOLDEN = json.loads("""
{
 "close_order": [
  [
   false,
   null,
   null,
   {
    "action": "SELL",
    "kind": "LMT",
    "px": 2.0,
    "qty": 2
   }
  ],
  [
   false,
   null,
   "stop",
   {
    "action": "SELL",
    "kind": "LMT",
    "px": 2.0,
    "qty": 2
   }
  ],
  [
   false,
   null,
   "profit_target",
   {
    "action": "SELL",
    "kind": "LMT",
    "px": 2.0,
    "qty": 2
   }
  ],
  [
   false,
   null,
   "time_stop",
   {
    "action": "SELL",
    "kind": "LMT",
    "px": 2.0,
    "qty": 2
   }
  ],
  [
   false,
   0.0,
   null,
   {
    "action": "SELL",
    "kind": "LMT",
    "px": 2.0,
    "qty": 2
   }
  ],
  [
   false,
   0.0,
   "stop",
   {
    "action": "SELL",
    "kind": "LMT",
    "px": 2.0,
    "qty": 2
   }
  ],
  [
   false,
   0.0,
   "profit_target",
   {
    "action": "SELL",
    "kind": "LMT",
    "px": 2.0,
    "qty": 2
   }
  ],
  [
   false,
   0.0,
   "time_stop",
   {
    "action": "SELL",
    "kind": "LMT",
    "px": 2.0,
    "qty": 2
   }
  ],
  [
   false,
   1.9,
   null,
   {
    "action": "SELL",
    "kind": "LMT",
    "px": 2.0,
    "qty": 2
   }
  ],
  [
   false,
   1.9,
   "stop",
   {
    "action": "SELL",
    "kind": "LMT",
    "px": 2.0,
    "qty": 2
   }
  ],
  [
   false,
   1.9,
   "profit_target",
   {
    "action": "SELL",
    "kind": "LMT",
    "px": 2.0,
    "qty": 2
   }
  ],
  [
   false,
   1.9,
   "time_stop",
   {
    "action": "SELL",
    "kind": "LMT",
    "px": 2.0,
    "qty": 2
   }
  ],
  [
   false,
   null,
   null,
   {
    "action": "SELL",
    "kind": "LMT",
    "px": 2.0,
    "qty": 2
   }
  ],
  [
   false,
   null,
   "stop",
   {
    "action": "SELL",
    "kind": "LMT",
    "px": 2.0,
    "qty": 2
   }
  ],
  [
   false,
   null,
   "profit_target",
   {
    "action": "SELL",
    "kind": "LMT",
    "px": 2.0,
    "qty": 2
   }
  ],
  [
   false,
   null,
   "time_stop",
   {
    "action": "SELL",
    "kind": "LMT",
    "px": 2.0,
    "qty": 2
   }
  ],
  [
   true,
   null,
   null,
   {
    "action": "SELL",
    "kind": "MKT",
    "qty": 2
   }
  ],
  [
   true,
   null,
   "stop",
   {
    "action": "SELL",
    "kind": "MKT",
    "qty": 2
   }
  ],
  [
   true,
   null,
   "profit_target",
   {
    "action": "SELL",
    "kind": "LMT",
    "px": 2.0,
    "qty": 2
   }
  ],
  [
   true,
   null,
   "time_stop",
   {
    "action": "SELL",
    "kind": "MKT",
    "qty": 2
   }
  ],
  [
   true,
   0.0,
   null,
   {
    "action": "SELL",
    "kind": "MKT",
    "qty": 2
   }
  ],
  [
   true,
   0.0,
   "stop",
   {
    "action": "SELL",
    "kind": "MKT",
    "qty": 2
   }
  ],
  [
   true,
   0.0,
   "profit_target",
   {
    "action": "SELL",
    "kind": "LMT",
    "px": 2.0,
    "qty": 2
   }
  ],
  [
   true,
   0.0,
   "time_stop",
   {
    "action": "SELL",
    "kind": "MKT",
    "qty": 2
   }
  ],
  [
   true,
   1.9,
   null,
   {
    "action": "SELL",
    "kind": "LMT",
    "px": 1.9,
    "qty": 2
   }
  ],
  [
   true,
   1.9,
   "stop",
   {
    "action": "SELL",
    "kind": "LMT",
    "px": 1.9,
    "qty": 2
   }
  ],
  [
   true,
   1.9,
   "profit_target",
   {
    "action": "SELL",
    "kind": "LMT",
    "px": 1.9,
    "qty": 2
   }
  ],
  [
   true,
   1.9,
   "time_stop",
   {
    "action": "SELL",
    "kind": "LMT",
    "px": 1.9,
    "qty": 2
   }
  ],
  [
   true,
   null,
   null,
   {
    "action": "SELL",
    "kind": "MKT",
    "qty": 2
   }
  ],
  [
   true,
   null,
   "stop",
   {
    "action": "SELL",
    "kind": "MKT",
    "qty": 2
   }
  ],
  [
   true,
   null,
   "profit_target",
   {
    "action": "SELL",
    "kind": "LMT",
    "px": 2.0,
    "qty": 2
   }
  ],
  [
   true,
   null,
   "time_stop",
   {
    "action": "SELL",
    "kind": "MKT",
    "qty": 2
   }
  ]
 ],
 "log_exit": [
  [
   {
    "basis_source": null,
    "commission_unknown": true,
    "contract_id": 101,
    "conviction": 6.0,
    "entry_debit": 820.0,
    "exit_price_per_share": 6.15,
    "fill_status": null,
    "proceeds": 1230.0,
    "quantity": 2,
    "realized_pnl": 410.0,
    "realized_pnl_net": null,
    "realized_pnl_pct": 50.0,
    "reason": "profit_target",
    "right": "C",
    "strike": 20.0,
    "structure": "single",
    "symbol": "RKLB"
   }
  ],
  [
   {
    "basis_source": null,
    "commission_unknown": true,
    "contract_id": 101,
    "conviction": 6.0,
    "entry_debit": 820.0,
    "exit_price_per_share": 2.05,
    "fill_status": null,
    "proceeds": 410.0,
    "quantity": 2,
    "realized_pnl": -410.0,
    "realized_pnl_net": null,
    "realized_pnl_pct": -50.0,
    "reason": "stop",
    "right": "C",
    "strike": 20.0,
    "structure": "single",
    "symbol": "RKLB"
   }
  ],
  [
   {
    "basis_source": null,
    "commission_unknown": true,
    "contract_id": 101,
    "conviction": 6.0,
    "entry_debit": 820.0,
    "exit_price_per_share": 0.0,
    "fill_status": null,
    "proceeds": 0.0,
    "quantity": 2,
    "realized_pnl": -820.0,
    "realized_pnl_net": null,
    "realized_pnl_pct": -100.0,
    "reason": "expired",
    "right": "C",
    "strike": 20.0,
    "structure": "single",
    "symbol": "RKLB"
   }
  ]
 ],
 "open_positions": [
  [],
  [
   [
    "RKLB",
    820.0,
    false
   ]
  ],
  [
   [
    "NVDA",
    1230.0,
    false
   ],
   [
    "QQQ",
    615.0,
    true
   ],
   [
    "RKLB",
    820.0,
    false
   ]
  ]
 ],
 "risk_gate": [
  [
   0,
   0,
   true,
   [],
   20000.0,
   2400.0
  ],
  [
   0,
   1,
   true,
   [],
   20000.0,
   2400.0
  ],
  [
   0,
   2,
   false,
   [
    "notional $5,000 exceeds 12%-of-pot cap $2,400"
   ],
   20000.0,
   2400.0
  ],
  [
   0,
   3,
   true,
   [],
   20000.0,
   2400.0
  ],
  [
   1,
   0,
   true,
   [],
   20000.0,
   2400.0
  ],
  [
   1,
   1,
   true,
   [],
   20000.0,
   2400.0
  ],
  [
   1,
   2,
   false,
   [
    "notional $5,000 exceeds 12%-of-pot cap $2,400"
   ],
   20000.0,
   2400.0
  ],
  [
   1,
   3,
   true,
   [],
   20000.0,
   2400.0
  ],
  [
   2,
   0,
   true,
   [],
   20000.0,
   2400.0
  ],
  [
   2,
   1,
   true,
   [],
   20000.0,
   2400.0
  ],
  [
   2,
   2,
   false,
   [
    "notional $5,000 exceeds 12%-of-pot cap $2,400"
   ],
   20000.0,
   2400.0
  ],
  [
   2,
   3,
   true,
   [],
   20000.0,
   2400.0
  ],
  [
   3,
   0,
   false,
   [
    "at max concurrent positions (4/4)"
   ],
   20000.0,
   2400.0
  ],
  [
   3,
   1,
   false,
   [
    "at max concurrent positions (4/4)"
   ],
   20000.0,
   2400.0
  ],
  [
   3,
   2,
   false,
   [
    "at max concurrent positions (4/4)",
    "notional $5,000 exceeds 12%-of-pot cap $2,400"
   ],
   20000.0,
   2400.0
  ],
  [
   3,
   3,
   false,
   [
    "at max concurrent positions (4/4)"
   ],
   20000.0,
   2400.0
  ]
 ],
 "run_cycle": [
  [
   "stop_fires",
   [
    {
     "ask": null,
     "bid": 2.4,
     "con_id": 101,
     "ctx_close_qty": 2,
     "ctx_entry_debit": 820.0,
     "ctx_is_short": null,
     "ctx_position_qty": 2,
     "ctx_reason": "stop",
     "entry_debit": 820.0,
     "limit_price": 2.5,
     "market": false,
     "quantity": 2,
     "right": "C",
     "short_close": false,
     "spread": null,
     "symbol": "RKLB",
     "trigger_type": "stop"
    }
   ],
   [
    [
     101,
     2,
     820.0
    ]
   ],
   []
  ],
  [
   "no_trigger",
   [],
   [],
   []
  ],
  [
   "time_stop",
   [],
   [],
   []
  ],
  [
   "two_positions",
   [
    {
     "ask": null,
     "bid": 2.4,
     "con_id": 101,
     "ctx_close_qty": 2,
     "ctx_entry_debit": 820.0,
     "ctx_is_short": null,
     "ctx_position_qty": 2,
     "ctx_reason": "stop",
     "entry_debit": 820.0,
     "limit_price": 2.5,
     "market": false,
     "quantity": 2,
     "right": "C",
     "short_close": false,
     "spread": null,
     "symbol": "RKLB",
     "trigger_type": "stop"
    }
   ],
   [
    [
     101,
     2,
     820.0
    ]
   ],
   []
  ]
 ]
}
""")


def test_the_debit_path_is_behaviourally_unchanged(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    assert GOLDEN is not None, "the golden differential was never captured"
    now = _debit_differential(tmp_path)
    for section in sorted(GOLDEN):
        assert json.loads(json.dumps(now[section], default=str)) == GOLDEN[section], (
            f"the long/debit path changed behaviour in: {section}")
