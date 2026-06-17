"""Tests for debit-spread support: short-leg selection, combo orders, journal, atomic close."""
import asyncio
import json

import pytest
from unittest.mock import AsyncMock, MagicMock

from exitmgr.trader import ResolvedOrder, Trader, order_summary, pick_spread_short
from exitmgr.order import OrderManager
from exitmgr.risk import RiskLimits
from exitmgr.state import StateManager


# ---------------- pick_spread_short (pure)

def test_picks_widest_affordable_short_call():
    # long 300C mid 2.40, cap $121: 310 short -> net 1.60 ($160, too big); 305 -> net 1.10 ($110, fits)
    cands = [(295.0, 4.10), (300.0, 2.40), (305.0, 1.30), (310.0, 0.80)]
    assert pick_spread_short(cands, 300.0, 2.40, "C", 121.0) == (305.0, 1.10)


def test_picks_widest_when_it_fits():
    cands = [(300.0, 1.20), (305.0, 0.70), (310.0, 0.45)]
    assert pick_spread_short(cands, 300.0, 1.20, "C", 121.0) == (310.0, 0.75)


def test_put_spread_shorts_lower_strike():
    # bearish: long 290P, short must be BELOW
    cands = [(295.0, 3.0), (290.0, 2.0), (285.0, 1.2), (280.0, 0.7)]
    assert pick_spread_short(cands, 290.0, 2.0, "P", 121.0) == (285.0, 0.80)  # 280 -> $130 > cap


def test_none_when_no_usable_short():
    assert pick_spread_short([(305.0, 2.39)], 300.0, 2.40, "C", 121.0) is None   # net <= 0.01
    assert pick_spread_short([(295.0, 3.0)], 300.0, 2.40, "C", 121.0) is None    # not OTM
    assert pick_spread_short([(305.0, float("nan"))], 300.0, 2.40, "C", 121.0) is None
    assert pick_spread_short([], 300.0, 2.40, "C", 121.0) is None


# ---------------- order_summary

def test_spread_summary_shows_both_strikes_and_risk():
    r = ResolvedOrder("IWM", "C", "20260626", 300.0, 1, 1.10, object(),
                      short_strike=305.0, short_contract=object())
    s = order_summary(r)
    assert "300/305C debit spread" in s
    assert "$1.10 LMT" in s
    assert "max loss ~$110" in s and "max value $500" in s


def test_single_leg_summary_unchanged():
    r = ResolvedOrder("SPY", "C", "20260620", 610.0, 1, 1.20, object())
    assert "610C @ $1.20 LMT" in order_summary(r)


# ---------------- submit + journal

def _trader(tmp_path):
    ibc = MagicMock()
    ibc.ib = MagicMock()
    return Trader(ib_conn=ibc, exit_manager=MagicMock(), limits=RiskLimits(),
                  approved_names=set(), endpoint="http://x", model="m", slack_token="t",
                  slack_channel="C", approver_ids=set(), baseline_path=str(tmp_path / "b.json"),
                  audit_path=str(tmp_path / "a.jsonl"), journal_path=str(tmp_path / "trades.log"))


def _contract(con_id):
    c = MagicMock()
    c.conId = con_id
    return c


@pytest.mark.asyncio
async def test_single_leg_submit_journals_entry(tmp_path):
    t = _trader(tmp_path)
    r = ResolvedOrder("SPY", "C", "20260620", 610.0, 1, 1.20, _contract(111))
    await t._submit_order(r)
    t.ib_conn.ib.placeOrder.assert_called_once()
    rec = json.loads(open(tmp_path / "trades.log").read().splitlines()[-1])
    assert rec["contract_id"] == 111 and rec["debit"] == 120.0 and "spread" not in rec


@pytest.mark.asyncio
async def test_spread_submit_uses_combo_and_journals_legs(tmp_path):
    t = _trader(tmp_path)
    r = ResolvedOrder("IWM", "C", "20260626", 300.0, 1, 1.10, _contract(111),
                      short_strike=305.0, short_contract=_contract(222))
    await t._submit_order(r)
    t.ib_conn.create_combo_contract.assert_called_once_with("IWM", [(111, "BUY"), (222, "SELL")])
    placed_contract = t.ib_conn.ib.placeOrder.call_args[0][0]
    assert placed_contract is t.ib_conn.create_combo_contract.return_value
    rec = json.loads(open(tmp_path / "trades.log").read().splitlines()[-1])
    assert rec["contract_id"] == 111
    assert rec["spread"]["short_con_id"] == 222 and rec["spread"]["width"] == 5.0


# ---------------- exit side: atomic combo close

def _order_manager(tmp_path):
    ib_conn = MagicMock()
    trade = MagicMock()
    trade.order.orderId = 77
    ib_conn.place_order = AsyncMock(return_value=trade)
    sm = StateManager(str(tmp_path / "state.json"))
    return OrderManager(ib_conn, sm), ib_conn, sm


def test_spread_close_places_atomic_bag(tmp_path):
    om, ib_conn, sm = _order_manager(tmp_path)
    res = asyncio.run(om.place_close_order(
        con_id=111, symbol="IWM", quantity=1, limit_price=2.50, entry_debit=110.0,
        live_open_orders={}, spread={"short_con_id": 222, "short_strike": 305.0}))
    assert res.success
    ib_conn.create_combo_contract.assert_called_once_with("IWM", [(111, "BUY"), (222, "SELL")])
    ib_conn.create_contract.assert_not_called()          # never closes the long leg alone
    ib_conn.create_limit_order.assert_called_once_with("SELL", 1, 2.50)
    assert sm.state.get_in_flight(111) is not None       # idempotency keyed on the long leg


def test_single_leg_close_unchanged(tmp_path):
    om, ib_conn, _ = _order_manager(tmp_path)
    res = asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=1, limit_price=2.50, entry_debit=120.0,
        live_open_orders={}))
    assert res.success
    ib_conn.create_contract.assert_called_once()
    ib_conn.create_combo_contract.assert_not_called()


# ---------------- positions now include puts

def _pos(con_id, right, qty):
    p = MagicMock()
    p.contract.conId = con_id
    p.contract.right = right
    p.contract.symbol = "X"
    p.contract.lastTradeDateOrContractMonth = "20260626"
    p.position = qty
    p.avgCost = 1.0
    return p


def test_get_positions_includes_long_puts_excludes_shorts():
    from exitmgr.connection import IBConnection
    conn = IBConnection(host="h", port=1, client_id=2)
    conn._connected = True
    conn.ib = MagicMock()
    conn.ib.reqPositionsAsync = AsyncMock(return_value=[
        _pos(1, "C", 1), _pos(2, "P", 2), _pos(3, "C", -1)])
    out = asyncio.run(conn.get_positions())
    assert set(out) == {1, 2}          # long call + long put managed; short leg excluded
    assert out[2].right == "P"
