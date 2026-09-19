"""Public API contract; production-derived narrative omitted."""
import asyncio
import json
import os
import types
from datetime import date, timedelta

import pytest
from unittest.mock import AsyncMock, MagicMock

from exitmgr.config import Config
from exitmgr.connection import IBConnection, PositionData
from exitmgr.manager import ExitManager


_FUTURE_EXPIRY = (date.today() + timedelta(days=180)).strftime("%Y%m%d")



def _raw(con_id, right, qty, *, symbol="SPY", avg_cost=1.75, expiry=_FUTURE_EXPIRY,
         sec_type="OPT", strike=50.0):
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
    c.ib.reqPositionsAsync = AsyncMock(return_value=rows)
    return c




def _realistic_book():
    return [
        _raw(101, "C", 2, symbol="RKLB", avg_cost=4.10, expiry=_FUTURE_EXPIRY, strike=20.0),
        _raw(102, "C", 1, symbol="NVDA", avg_cost=12.30, expiry="20261218", strike=180.0),
        _raw(103, "P", 3, symbol="QQQ", avg_cost=2.05, expiry="20260821", strike=560.0),
        _raw(104, "C", -2, symbol="RKLB", avg_cost=1.05, expiry=_FUTURE_EXPIRY, strike=25.0),
        _raw(105, "P", -1, symbol="SPY", avg_cost=1.75, expiry=_FUTURE_EXPIRY, strike=500.0),
        _raw(106, "", 100, symbol="SPY", avg_cost=498.25, expiry="", sec_type="STK", strike=0.0),
    ]




_LEGACY_FIELDS = ("con_id", "symbol", "right", "quantity", "avg_cost", "expiry")


def _legacy_view(book):
    return {cid: tuple(getattr(pd, f) for f in _LEGACY_FIELDS) for cid, pd in book.items()}



def test_short_put_appears_with_negative_quantity():
    """Public API contract; production-derived narrative omitted."""
    out = asyncio.run(_conn(_realistic_book()).get_positions(include_short=True))
    assert 105 in out, "the filled cash-secured put is still invisible"
    csp = out[105]
    assert csp.quantity == -1, "the sign was stripped -- a short must never look like a long"
    assert csp.is_short is True
    assert csp.right == "P" and csp.symbol == "SPY" and csp.strike == 500.0


def test_short_quantity_is_never_abs_ed():
    """Public API contract; production-derived narrative omitted."""
    out = asyncio.run(_conn(_realistic_book()).get_positions(include_short=True))
    assert all(out[c].quantity < 0 for c in (104, 105))
    assert all(out[c].quantity > 0 for c in (101, 102, 103))


def test_default_call_still_excludes_every_short():
    """Public API contract; production-derived narrative omitted."""
    out = asyncio.run(_conn(_realistic_book()).get_positions())
    assert set(out) == {101, 102, 103}
    assert all(pd.quantity > 0 for pd in out.values())



def test_long_positions_are_byte_identical_differential():
    """Public API contract; production-derived narrative omitted."""
    conn = _conn(_realistic_book())
    default = asyncio.run(conn.get_positions())
    with_short = asyncio.run(conn.get_positions(include_short=True))
    with_both = asyncio.run(conn.get_positions(include_short=True, include_stock=True))

    frozen = {
        101: (101, "RKLB", "C", 2, 4.10, _FUTURE_EXPIRY),
        102: (102, "NVDA", "C", 1, 12.30, "20261218"),
        103: (103, "QQQ", "P", 3, 2.05, "20260821"),
    }
    assert _legacy_view(default) == frozen

    longs_with_short = {k: v for k, v in with_short.items() if v.quantity > 0}
    longs_with_both = {k: v for k, v in with_both.items()
                       if v.quantity > 0 and v.sec_type != "STK"}
    assert _legacy_view(longs_with_short) == frozen
    assert _legacy_view(longs_with_both) == frozen

    assert {k: with_short[k] for k in frozen} == {k: default[k] for k in frozen}


def test_long_only_book_is_completely_unaffected_by_the_new_flags():
    """Public API contract; production-derived narrative omitted."""
    rows = [_raw(201, "C", 1, symbol="AAPL"), _raw(202, "P", 5, symbol="TSLA")]
    conn = _conn(rows)
    a = asyncio.run(conn.get_positions())
    b = asyncio.run(conn.get_positions(include_short=True))
    c = asyncio.run(conn.get_positions(include_short=True, include_stock=True))
    assert a == b == c and set(a) == {201, 202}


def test_zero_quantity_row_is_excluded_everywhere():
    """Public API contract; production-derived narrative omitted."""
    rows = [_raw(301, "P", 0, symbol="SPY")]
    conn = _conn(rows)
    assert asyncio.run(conn.get_positions()) == {}
    assert asyncio.run(conn.get_positions(include_short=True, include_stock=True)) == {}



def test_assigned_stock_with_residual_option_fields_is_not_treated_as_a_short():
    """Public API contract; production-derived narrative omitted."""
    rows = [_raw(401, "P", -100, symbol="SPY", sec_type="STK", strike=500.0)]
    out = asyncio.run(_conn(rows).get_positions(include_short=True))
    assert out == {}, "a STOCK row was mistaken for a short option"


def test_stock_is_excluded_unless_explicitly_requested():
    conn = _conn(_realistic_book())
    assert 106 not in asyncio.run(conn.get_positions())
    assert 106 not in asyncio.run(conn.get_positions(include_short=True))
    both = asyncio.run(conn.get_positions(include_short=True, include_stock=True))
    assert both[106].sec_type == "STK" and both[106].right == "" and both[106].quantity == 100


def test_short_with_unreadable_sectype_is_still_reported():
    """Public API contract; production-derived narrative omitted."""
    p = types.SimpleNamespace()
    p.contract = types.SimpleNamespace(conId=501, symbol="SPY", right="P", strike=500.0,
                                       lastTradeDateOrContractMonth=_FUTURE_EXPIRY)
    p.position = -1
    p.avgCost = 1.75
    out = asyncio.run(_conn([p]).get_positions(include_short=True))
    assert 501 in out and out[501].quantity == -1



def _mgr(tmp_path, journal_lines=(), *, rows=()):
    cfg = Config()
    cfg.dry_run = True
    cfg.loop_mode = False
    cfg.journal.path = str(tmp_path / "trades.log")
    cfg.state.path = str(tmp_path / "state.json")
    cfg.kill_switch.path = str(tmp_path / "KILL")
    cfg.audit_path = str(tmp_path / "audit.jsonl")
    (tmp_path / "trades.log").write_text(
        "".join(json.dumps(x) + "\n" for x in journal_lines))
    mgr = ExitManager(cfg)
    mgr.ib_conn = _conn(list(rows))
    mgr.ib_conn.ib.portfolio = MagicMock(return_value=[])
    return mgr, cfg



CSP_JOURNAL = {
    "ts": "2026-07-20T14:00:00+00:00", "contract_id": 105, "symbol": "SPY", "right": "P",
    "strike": 500.0, "expiry": _FUTURE_EXPIRY, "side": "credit", "structure": "cash secured put",
    "action": "SELL", "quantity": -1, "contracts": 1, "collateral_usd": 50000.0,
    "net_credit_usd": 175.0, "max_loss_usd": 49825.0, "debit": 49825.0,
    "assignment_possible": True, "conviction": 7, "profit_target_pct": 50.0, "stop_pct": 100.0,
}
LONG_JOURNAL = {
    "ts": "2026-07-20T14:00:00+00:00", "contract_id": 101, "symbol": "RKLB", "right": "C",
    "strike": 20.0, "expiry": _FUTURE_EXPIRY, "quantity": 2, "debit": 820.0, "conviction": 6,
}


def _exits(cfg):
    p = os.path.join(os.path.dirname(cfg.journal.path) or ".", "exits.log")
    if not os.path.exists(p):
        return []
    with open(p) as f:
        return [json.loads(l) for l in f if l.strip()]



def test_fetch_position_book_splits_and_the_long_book_is_unchanged(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr, _ = _mgr(tmp_path, [CSP_JOURNAL], rows=_realistic_book())
    longs, shorts, stocks = asyncio.run(mgr._fetch_position_book())
    assert set(longs) == {101, 102, 103}
    assert set(shorts) == {104, 105}
    assert set(stocks) == {106}
    assert _legacy_view(longs) == _legacy_view(
        asyncio.run(_conn(_realistic_book()).get_positions()))
    assert mgr._short_positions is not None and set(mgr._short_positions) == {104, 105}


def test_fetch_position_book_falls_back_when_the_connection_rejects_the_keywords(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr, _ = _mgr(tmp_path)
    calls = []

    async def _old_signature(*args, **kwargs):

        if args or kwargs:
            raise TypeError("get_positions() takes 1 positional argument")
        calls.append("plain")
        return {101: PositionData(101, "RKLB", "C", 2, 4.10, _FUTURE_EXPIRY)}

    mgr.ib_conn.get_positions = _old_signature
    longs, shorts, stocks = asyncio.run(mgr._fetch_position_book())
    assert calls == ["plain"]
    assert set(longs) == {101} and shorts == {} and stocks == {}


def test_unparseable_quantity_defaults_to_the_long_book(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr, _ = _mgr(tmp_path)
    weird = MagicMock()
    weird.quantity = object()
    weird.sec_type = "OPT"
    mgr.ib_conn.get_positions = AsyncMock(return_value={999: weird})
    longs, shorts, stocks = asyncio.run(mgr._fetch_position_book())
    assert set(longs) == {999} and shorts == {} and stocks == {}


def test_reconcile_is_handed_only_longs_and_stays_safe_with_a_csp_open(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr, _ = _mgr(tmp_path, [CSP_JOURNAL], rows=_realistic_book())
    mgr.ib_conn.get_open_orders = AsyncMock(return_value={})
    assert asyncio.run(mgr._reconcile_on_startup()) is True
    assert mgr._reconcile_bad_con_ids == set()


def test_a_short_never_reaches_the_long_only_management_path(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    rows = [_raw(105, "P", -1, symbol="SPY", strike=500.0, avg_cost=1.75)]
    mgr, _ = _mgr(tmp_path, [CSP_JOURNAL], rows=rows)
    mgr.ib_conn.get_open_orders = AsyncMock(return_value={})
    mgr.ib_conn.fetch_quotes = AsyncMock(return_value={})
    mgr.order_manager.place_close_order = AsyncMock(
        side_effect=AssertionError("an exit order was built for a SHORT position"))
    asyncio.run(mgr.run_cycle(dry_run=True))
    assert mgr.order_manager.place_close_order.await_count == 0


def test_scope_selection_never_includes_a_short(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr, _ = _mgr(tmp_path, [CSP_JOURNAL, LONG_JOURNAL], rows=_realistic_book())
    longs, _s, _k = asyncio.run(mgr._fetch_position_book())
    assert 105 not in mgr._get_scope_con_ids(longs)
    assert 101 in mgr._get_scope_con_ids(longs)


def test_backstop_refuses_a_short_that_reaches_managed_positions(tmp_path, capsys):
    """Public API contract; production-derived narrative omitted."""
    poisoned = [_raw(105, "P", -1, symbol="SPY", strike=500.0)]
    mgr, _ = _mgr(tmp_path, [CSP_JOURNAL], rows=poisoned)

    mgr._fetch_position_book = AsyncMock(return_value=(
        {105: PositionData(105, "SPY", "P", -1, 1.75, _FUTURE_EXPIRY)}, {}, {}))
    mgr.ib_conn.get_open_orders = AsyncMock(return_value={})
    mgr.order_manager.place_close_order = AsyncMock(
        side_effect=AssertionError("a SHORT was routed into the long-only close path"))
    asyncio.run(mgr.run_cycle(dry_run=True))
    assert "is SHORT" in capsys.readouterr().out
    assert mgr.order_manager.place_close_order.await_count == 0


def test_trader_open_positions_counts_a_csp_at_collateral(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    from exitmgr.trader import Trader
    (tmp_path / "trades.log").write_text(json.dumps(CSP_JOURNAL) + "\n")
    t = Trader.__new__(Trader)
    t.ib_conn = _conn(_realistic_book())
    t.ib_conn.ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[])
    t.journal_path = str(tmp_path / "trades.log")
    t.audit_path = str(tmp_path / "audit.jsonl")
    out = asyncio.run(Trader._open_positions(t))
    spy = [p for p in out if p.underlying == "SPY"]
    assert len(spy) == 1, "the CSP must occupy exactly one slot in the concurrent book"
    assert spy[0].is_credit is True
    assert spy[0].notional == 50_000.0, "a CSP is valued at collateral, not premium/max-loss"


    assert all(not (p.underlying == "RKLB" and p.notional == 2_500.0) for p in out)


def test_construction_open_book_would_be_poisoned_by_a_short(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    from exitmgr import construction
    (tmp_path / "trades.log").write_text(json.dumps(CSP_JOURNAL) + "\n")
    short = {105: PositionData(105, "SPY", "P", -1, 1.75, _FUTURE_EXPIRY)}
    poisoned = construction.open_book_items(short, str(tmp_path / "trades.log"), [])
    assert poisoned, "expected the short to enter the deployment book"
    assert max(float(b) for b, _dte in poisoned.values()) > 40_000

    assert construction.open_book_items({}, str(tmp_path / "trades.log"), []) == {}



def test_short_pnl_a_csp_whose_price_fell_is_a_GAIN():
    """Public API contract; production-derived narrative omitted."""
    r = ExitManager.short_pnl(CSP_JOURNAL, 0.50, 1)
    assert r["credit_usd"] == 175.0
    assert r["cost_to_close_usd"] == 50.0
    assert r["pnl_usd"] == 125.0 and r["pnl_usd"] > 0
    assert r["pct_of_credit"] == pytest.approx(71.43, abs=0.01)


def test_short_pnl_a_csp_whose_price_rose_is_a_LOSS():
    r = ExitManager.short_pnl(CSP_JOURNAL, 3.00, 1)
    assert r["pnl_usd"] == -125.0 and r["pnl_usd"] < 0
    assert r["pct_of_credit"] < 0


def test_short_pnl_is_monotonically_decreasing_in_price():
    """Public API contract; production-derived narrative omitted."""
    pnls = [ExitManager.short_pnl(CSP_JOURNAL, px, 1)["pnl_usd"]
            for px in (0.0, 0.5, 1.75, 3.0, 10.0)]
    assert pnls == sorted(pnls, reverse=True)
    assert pnls[0] == 175.0
    assert pnls[2] == 0.0


def test_short_pnl_scales_with_contracts():
    r = ExitManager.short_pnl(dict(CSP_JOURNAL, quantity=-3, net_credit_usd=525.0), 0.50, 3)
    assert r["cost_to_close_usd"] == 150.0 and r["pnl_usd"] == 375.0


def test_short_pnl_refuses_to_guess():
    """Public API contract; production-derived narrative omitted."""
    assert ExitManager.short_pnl(CSP_JOURNAL, None, 1)["pnl_usd"] is None
    assert ExitManager.short_pnl(CSP_JOURNAL, float("nan"), 1)["pnl_usd"] is None
    assert ExitManager.short_pnl({"quantity": -1}, 0.50, 1)["pnl_usd"] is None


def test_is_credit_row_detects_a_short_by_either_signal():
    assert ExitManager._is_credit_row(CSP_JOURNAL) is True
    assert ExitManager._is_credit_row({"quantity": -2}) is True
    assert ExitManager._is_credit_row({"side": "credit"}) is True
    assert ExitManager._is_credit_row(LONG_JOURNAL) is False
    assert ExitManager._is_credit_row(None) is False
    assert ExitManager._is_credit_row({"quantity": "junk"}) is False


def test_log_exit_books_a_cheap_buyback_as_a_profit(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr, cfg = _mgr(tmp_path, [CSP_JOURNAL])
    trig = types.SimpleNamespace(trigger_type="profit_target", pnl_pct=0.0, message="")
    mgr._log_exit(105, "SPY", trig, exit_price_per_share=0.10, quantity=-1,
                  reason="profit_target", extra={"exit_event": "manual"})
    rows = _exits(cfg)
    assert len(rows) == 1
    r = rows[0]
    assert r["realized_pnl"] == 165.0 and r["realized_pnl"] > 0
    assert r["side"] == "credit" and r["is_short"] is True
    assert r["quantity"] == -1 and r["contracts"] == 1
    assert r["entry_credit_usd"] == 175.0 and r["close_cost_usd"] == 10.0
    assert r["realized_pnl_pct"] > 0


def test_log_exit_books_an_expensive_buyback_as_a_loss(tmp_path):
    mgr, cfg = _mgr(tmp_path, [CSP_JOURNAL])
    trig = types.SimpleNamespace(trigger_type="stop", pnl_pct=0.0, message="")
    mgr._log_exit(105, "SPY", trig, exit_price_per_share=5.00, quantity=-1, reason="stop")
    r = _exits(cfg)[0]
    assert r["realized_pnl"] == -325.0 and r["realized_pnl_pct"] < 0


def test_log_exit_long_path_is_byte_identical(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr, cfg = _mgr(tmp_path, [LONG_JOURNAL])
    trig = types.SimpleNamespace(trigger_type="profit_target", pnl_pct=0.0, message="")
    mgr._log_exit(101, "RKLB", trig, exit_price_per_share=6.15, quantity=2,
                  reason="profit_target")
    r = _exits(cfg)[0]
    assert r["proceeds"] == 1230.0
    assert r["entry_debit"] == 820.0
    assert r["realized_pnl"] == 410.0
    assert r["realized_pnl_pct"] == 50.0
    assert r["structure"] == "single" and r["quantity"] == 2
    for k in ("side", "is_short", "entry_credit_usd", "close_cost_usd", "pnl_basis"):
        assert k not in r, f"a credit-only field ({k}) leaked onto a long exit record"


def test_expired_worthless_short_keeps_the_whole_credit(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr, cfg = _mgr(tmp_path, [CSP_JOURNAL])
    mgr._emit_expiry_close(105, CSP_JOURNAL, spot=520.0)
    r = _exits(cfg)[0]
    assert r["realized_pnl"] == 175.0
    assert r["realized_pnl_pct"] == 100.0
    assert "full credit kept" in r["exit_reasoning"]
    assert r["assigned"] is False


def test_expired_itm_short_is_flagged_as_an_assignment(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr, cfg = _mgr(tmp_path, [CSP_JOURNAL])
    mgr._emit_expiry_close(105, CSP_JOURNAL, spot=495.0)
    r = _exits(cfg)[0]
    assert r["realized_pnl"] == -325.0
    assert r["assigned"] is True and r["assigned_shares"] == 100
    assert r["side"] == "credit"


def test_expired_long_is_unchanged(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr, cfg = _mgr(tmp_path, [LONG_JOURNAL])
    mgr._emit_expiry_close(101, LONG_JOURNAL, spot=15.0)
    r = _exits(cfg)[0]
    assert r["realized_pnl"] == -820.0 and r["realized_pnl_pct"] == -100.0
    assert "assigned" not in r



def test_csp_is_reported_on_every_cycle_not_only_the_fill_cycle(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    rows = [_raw(105, "P", -1, symbol="SPY", strike=500.0, avg_cost=1.75)]
    mgr, _ = _mgr(tmp_path, [CSP_JOURNAL], rows=rows)
    mgr.ib_conn.get_open_orders = AsyncMock(return_value={})
    mgr.ib_conn.fetch_quotes = AsyncMock(return_value={})
    mark = types.SimpleNamespace(contract=types.SimpleNamespace(conId=105), marketPrice=0.50)
    mgr.ib_conn.ib.portfolio = MagicMock(return_value=[mark])

    for cycle in range(3):
        asyncio.run(mgr.run_cycle(dry_run=True))
        assert len(mgr._short_report) == 1, f"the CSP went invisible on cycle {cycle}"
        row = mgr._short_report[0]
        assert row["con_id"] == 105
        assert row["quantity"] == -1
        assert row["contracts"] == 1
        assert row["collateral_usd"] == 50000.0
        assert row["unrealized_pnl_usd"] == 125.0
        assert row["managed"] is True and row["unmanaged_reason"] is None


def test_short_report_counts_a_csp_toward_the_concurrent_book(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    rows = [_raw(105, "P", -1, symbol="SPY", strike=500.0),
            _raw(107, "P", -2, symbol="QQQ", strike=540.0)]
    j2 = dict(CSP_JOURNAL, contract_id=107, symbol="QQQ", strike=540.0, quantity=-2,
              net_credit_usd=400.0, collateral_usd=108000.0, max_loss_usd=107600.0,
              debit=107600.0)
    mgr, _ = _mgr(tmp_path, [CSP_JOURNAL, j2], rows=rows)
    mgr.ib_conn.get_open_orders = AsyncMock(return_value={})
    mgr.ib_conn.fetch_quotes = AsyncMock(return_value={})
    asyncio.run(mgr.run_cycle(dry_run=True))
    asyncio.run(mgr.run_cycle(dry_run=True))
    assert len(mgr._short_report) == 2
    assert {r["con_id"] for r in mgr._short_report} == {105, 107}
    assert sum(r["contracts"] for r in mgr._short_report) == 3


def test_an_unjournaled_short_is_still_reported(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    rows = [_raw(900, "P", -1, symbol="AMD", strike=140.0)]
    mgr, _ = _mgr(tmp_path, [], rows=rows)
    mgr.ib_conn.get_open_orders = AsyncMock(return_value={})
    mgr.ib_conn.fetch_quotes = AsyncMock(return_value={})
    asyncio.run(mgr.run_cycle(dry_run=True))
    assert len(mgr._short_report) == 1
    row = mgr._short_report[0]
    assert row["journaled"] is False
    assert row["unrealized_pnl_usd"] is None
    assert row["collateral_usd"] == 14000.0



def test_assignment_emits_a_terminal_row_and_does_not_orphan_the_journal(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr, cfg = _mgr(tmp_path, [CSP_JOURNAL])
    shorts = {}
    stocks = {106: PositionData(106, "SPY", "", 100, 498.25, "", sec_type="STK")}
    mgr._spot_price = AsyncMock(return_value=495.0)
    asyncio.run(mgr._process_short_assignments(shorts, stocks))

    rows = _exits(cfg)
    assert len(rows) == 1
    r = rows[0]
    assert r["reason"] == "assigned" and r["assigned"] is True
    assert r["assigned_shares"] == 100 and r["side"] == "credit"
    assert r["realized_pnl"] == -325.0
    assert r["contract_id"] == 105

    mgr._load_journal()
    assert mgr._journal_entries[105]["side"] == "credit"

    assert "105" not in mgr.state_manager.state.peak_prices


def test_assignment_is_recorded_exactly_once(tmp_path):
    mgr, cfg = _mgr(tmp_path, [CSP_JOURNAL])
    stocks = {106: PositionData(106, "SPY", "", 100, 498.25, "", sec_type="STK")}
    mgr._spot_price = AsyncMock(return_value=495.0)
    for _ in range(3):
        asyncio.run(mgr._process_short_assignments({}, stocks))
    assert len(_exits(cfg)) == 1


def test_disappearance_without_stock_is_not_declared_an_assignment(tmp_path, capsys):
    """Public API contract; production-derived narrative omitted."""
    mgr, cfg = _mgr(tmp_path, [CSP_JOURNAL])
    mgr._spot_price = AsyncMock(return_value=495.0)
    asyncio.run(mgr._process_short_assignments({}, {}))
    assert _exits(cfg) == []
    assert "NOT assignment" in capsys.readouterr().out


def test_a_live_short_is_never_mistaken_for_assigned(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr, cfg = _mgr(tmp_path, [CSP_JOURNAL])
    live = {105: PositionData(105, "SPY", "P", -1, 1.75, _FUTURE_EXPIRY, strike=500.0)}
    stocks = {106: PositionData(106, "SPY", "", 100, 498.25, "", sec_type="STK")}
    asyncio.run(mgr._process_short_assignments(live, stocks))
    assert _exits(cfg) == []


def test_a_live_short_is_never_treated_as_expired(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr, cfg = _mgr(tmp_path, [dict(CSP_JOURNAL, expiry="20200101")])
    live_short = {105: PositionData(105, "SPY", "P", -1, 1.75, "20200101", strike=500.0)}
    mgr._spot_price = AsyncMock(return_value=495.0)
    asyncio.run(mgr._process_expiries({}, live_shorts=live_short))
    assert _exits(cfg) == []

    asyncio.run(mgr._process_expiries({}, live_shorts={}))
    assert len(_exits(cfg)) == 1


def test_assignment_transition_runs_clean_through_a_whole_cycle(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    rows_before = [_raw(105, "P", -1, symbol="SPY", strike=500.0)]
    mgr, cfg = _mgr(tmp_path, [CSP_JOURNAL], rows=rows_before)
    mgr.ib_conn.get_open_orders = AsyncMock(return_value={})
    mgr.ib_conn.fetch_quotes = AsyncMock(return_value={})
    mgr._spot_price = AsyncMock(return_value=495.0)
    asyncio.run(mgr.run_cycle(dry_run=True))
    assert len(mgr._short_report) == 1 and _exits(cfg) == []


    rows_after = [_raw(106, "", 100, symbol="SPY", sec_type="STK", strike=0.0, expiry="")]
    mgr.ib_conn.ib.reqPositionsAsync = AsyncMock(return_value=rows_after)
    asyncio.run(mgr.run_cycle(dry_run=True))

    assert mgr._short_report == []
    assert set(mgr._stock_positions) == {106}
    r = _exits(cfg)[0]
    assert r["reason"] == "assigned" and r["assigned_shares"] == 100


def test_assigned_stock_is_never_managed_as_an_option(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    rows = [_raw(106, "", 100, symbol="SPY", sec_type="STK", strike=0.0, expiry="")]
    mgr, _ = _mgr(tmp_path, [CSP_JOURNAL], rows=rows)
    mgr.ib_conn.get_open_orders = AsyncMock(return_value={})
    mgr.ib_conn.fetch_quotes = AsyncMock(return_value={})
    mgr._spot_price = AsyncMock(return_value=495.0)
    mgr.order_manager.place_close_order = AsyncMock(
        side_effect=AssertionError("an option close was built for a STOCK position"))
    asyncio.run(mgr.run_cycle(dry_run=True))
    longs, _s, stocks = asyncio.run(mgr._fetch_position_book())
    assert longs == {} and set(stocks) == {106}
    assert mgr.order_manager.place_close_order.await_count == 0


def test_credit_con_ids_tracks_the_journal(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr, cfg = _mgr(tmp_path, [CSP_JOURNAL, LONG_JOURNAL])
    assert mgr._credit_con_ids == {105}
    with open(cfg.journal.path, "a") as f:
        f.write(json.dumps({"contract_id": 105, "symbol": "SPY", "event": "closed_by_tool",
                            "status": "Filled", "avg_fill_price": 0.10,
                            "tool": "close_symbol", "client_id": 91,
                            "broker_flat_confirmed": True}) + "\n")
    mgr._load_journal()
    assert mgr._credit_con_ids == set()
