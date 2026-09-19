"""Public API contract; production-derived narrative omitted."""

import asyncio
import inspect
import json
import math

import pytest

from exitmgr import rules as R
from exitmgr.order import OrderManager, DEFAULT_EXIT_SLIPPAGE_FLOOR
from exitmgr.state import StateManager
from exitmgr.config import RulesConfig, TrailingConfig









class _Order:
    """Public API contract; production-derived narrative omitted."""

    def __init__(self, action=None, orderType=None, totalQuantity=None, lmtPrice=None):
        self.action = action
        self.orderType = orderType
        self.totalQuantity = totalQuantity
        self.lmtPrice = lmtPrice
        self.tif = "DAY"
        self.orderId = 0
        self.orderRef = None
        self.permId = 0
        self.clientId = 7

    def key(self):
        """Public API contract; production-derived narrative omitted."""
        return (self.action, self.orderType, self.totalQuantity, self.lmtPrice, self.tif)

    def __repr__(self):
        return f"_Order{self.key()}"


class _Contract:
    def __init__(self, conId=0, symbol="", right="", secType="OPT", strike=0.0):
        self.conId = conId
        self.symbol = symbol
        self.right = right
        self.secType = secType
        self.strike = strike


class _PortfolioItem:
    def __init__(self, con_id, right="P", qty=-1, sec_type="OPT", symbol="SPY"):
        self.contract = _Contract(conId=con_id, symbol=symbol, right=right, secType=sec_type)
        self.position = qty
        self.avgCost = 400.0


class _OrderData:
    """Public API contract; production-derived narrative omitted."""

    def __init__(self, con_id, order_id=901, remaining=1):
        self.con_id = con_id
        self.order_id = order_id
        self.remaining = remaining
        self.perm_id = 0
        self.client_id = 7
        self.order_ref = "exitmgr-x"
        self.status = "Submitted"


class _OrderStatus:
    def __init__(self, status="Filled", filled=1, avgFillPrice=0.40):
        self.status = status
        self.filled = filled
        self.avgFillPrice = avgFillPrice


class _Trade:
    def __init__(self, order, status="Filled", filled=1):
        self.order = order
        self.orderStatus = _OrderStatus(status=status, filled=filled)
        self.log = []
        self.fills = []


class _IB:
    def __init__(self, portfolio_items=None, portfolio_raises=False):
        self._portfolio_items = portfolio_items if portfolio_items is not None else []
        self._portfolio_raises = portfolio_raises

    def portfolio(self):
        if self._portfolio_raises:
            raise RuntimeError("portfolio read failed")
        return list(self._portfolio_items)


class _Conn:
    """Public API contract; production-derived narrative omitted."""

    def __init__(self, portfolio_items=None, open_orders=None,
                 open_orders_raises=None, portfolio_raises=False,
                 supports_short_leg_kwarg=True):
        self.client_id = 7
        self.ib = _IB(portfolio_items, portfolio_raises=portfolio_raises)
        self.calls = []
        self.placed = []
        self._open_orders = open_orders if open_orders is not None else {}
        self._open_orders_raises = open_orders_raises
        self._supports_short_leg_kwarg = supports_short_leg_kwarg
        self.open_order_queries = []


    def create_limit_order(self, action, total_quantity, limit_price):
        self.calls.append(("limit", action, total_quantity, limit_price))
        return _Order(action, "LMT", total_quantity, limit_price)

    def create_market_order(self, action, total_quantity):
        self.calls.append(("market", action, total_quantity))
        return _Order(action, "MKT", total_quantity, None)

    def create_contract(self, con_id, symbol="", right="C"):
        self.calls.append(("contract", con_id, symbol, right))
        return _Contract(conId=con_id, symbol=symbol, right=right)

    def create_combo_contract(self, symbol, legs):
        self.calls.append(("combo", symbol, tuple(legs)))
        return _Contract(symbol=symbol, secType="BAG")


    def reserve_order_id(self):
        return 555

    async def place_order(self, contract, order):
        self.placed.append((contract, order))
        return _Trade(order)

    async def get_open_orders(self, short_leg_con_ids=None):
        if not self._supports_short_leg_kwarg:
            raise TypeError("get_open_orders() got an unexpected keyword argument")
        self.open_order_queries.append(set(short_leg_con_ids or []))
        if self._open_orders_raises:
            raise RuntimeError(self._open_orders_raises)
        return dict(self._open_orders)


def _om(tmp_path, conn=None, **kw):
    conn = conn if conn is not None else _Conn(**kw)
    sm = StateManager(str(tmp_path / "state.json"))
    return OrderManager(conn, sm), conn, sm


def _rules(profit_target_pct=80.0, stop_pct=100.0, time_stop_days=0,
           trailing_enabled=False, activation=50.0, giveback=0.4):
    r = RulesConfig()
    r.profit_target_pct = profit_target_pct
    r.stop_pct = stop_pct
    r.time_stop_days = time_stop_days
    r.trailing = TrailingConfig(enabled=trailing_enabled,
                                activation_gain_pct=activation,
                                giveback_fraction=giveback)
    return r



CREDIT = 400.0
QTY = -1
CPS = 4.00






def test_profitable_csp_is_a_GAIN_not_a_loss():
    """Public API contract; production-derived narrative omitted."""
    assert R.short_pnl_pct(0.40, CREDIT, QTY) == pytest.approx(90.0)


def test_long_and_short_pnl_are_exact_mirrors():
    """Public API contract; production-derived narrative omitted."""
    for px in (0.40, 1.00, 2.00, 4.00, 6.50):
        long_pct = R.calculate_pnl_pct(px, CREDIT, 1)
        short_pct = R.short_pnl_pct(px, CREDIT, QTY)
        assert short_pct == pytest.approx(-long_pct), f"sign inversion lost at price {px}"


def test_profitable_csp_does_NOT_trip_the_stop():
    """Public API contract; production-derived narrative omitted."""
    assert R.evaluate_short_stop(0.40, CREDIT, QTY, stop_pct=100.0) is None


def test_the_long_stop_WOULD_have_fired_on_that_same_winner():
    """Public API contract; production-derived narrative omitted."""
    wrong = R.evaluate_stop(0.40, CREDIT, 1, stop_pct=50.0)
    assert wrong is not None and wrong.trigger_type == "stop"

    assert wrong.pnl_pct == pytest.approx(-90.0)


def test_losing_csp_breaches_the_stop_correctly():
    """Public API contract; production-derived narrative omitted."""
    t = R.evaluate_short_stop(8.40, CREDIT, QTY, stop_pct=100.0)
    assert t is not None
    assert t.trigger_type == "stop"
    assert t.is_short is True
    assert t.entry_credit == CREDIT
    assert t.pnl_pct == pytest.approx(-110.0)
    assert t.current_value == pytest.approx(840.0)


@pytest.mark.parametrize("px,fires", [
    (7.99, False),
    (8.00, True),
    (8.01, True),
])
def test_short_stop_boundary_is_inclusive_and_upward(px, fires):
    assert (R.evaluate_short_stop(px, CREDIT, QTY, stop_pct=100.0) is not None) is fires


@pytest.mark.parametrize("px,fires", [
    (2.01, False),
    (2.00, True),
    (0.40, True),
])
def test_short_profit_target_boundary_is_inclusive_and_downward(px, fires):
    """Public API contract; production-derived narrative omitted."""
    assert (R.evaluate_short_profit_target(px, CREDIT, QTY, 50.0) is not None) is fires


def test_short_profit_target_reports_a_positive_pnl():
    t = R.evaluate_short_profit_target(0.40, CREDIT, QTY, 80.0)
    assert t is not None and t.pnl_pct == pytest.approx(90.0) and t.is_short


def test_evaluate_position_routes_a_short_to_the_profit_target_not_the_stop():
    """Public API contract; production-derived narrative omitted."""
    t = R.evaluate_position(
        con_id=42, symbol="SPY", quantity=QTY, entry_debit=0.0, current_price=0.40,
        days_to_expiry=30, peak_price=None, rules=_rules(), entry_credit=CREDIT)
    assert t is not None
    assert t.trigger_type == "profit_target"
    assert t.is_short is True
    assert t.pnl_pct == pytest.approx(90.0)
    assert t.con_id == 42


def test_evaluate_position_on_a_short_used_to_silently_return_None():
    """Public API contract; production-derived narrative omitted."""
    t = R.evaluate_position(
        con_id=42, symbol="SPY", quantity=QTY, entry_debit=0.0, current_price=8.40,
        days_to_expiry=30, peak_price=None, rules=_rules(), entry_credit=CREDIT)
    assert t is not None and t.trigger_type == "stop"


def test_short_without_a_credit_basis_refuses_loudly_and_returns_None(capsys):
    """Public API contract; production-derived narrative omitted."""
    t = R.evaluate_position(
        con_id=42, symbol="SPY", quantity=QTY, entry_debit=500.0, current_price=8.40,
        days_to_expiry=30, peak_price=None, rules=_rules(), entry_credit=None)
    assert t is None
    out = capsys.readouterr().out
    assert "UNMANAGED" in out and "NOT protected" in out


@pytest.mark.parametrize("bad", [None, 0.0, -400.0, float("nan"), "x"])
def test_unusable_credit_never_fabricates_a_basis(bad):
    assert R._short_credit_per_share(bad, QTY) is None
    assert R.evaluate_short_stop(8.40, bad, QTY, 100.0) is None
    assert R.evaluate_short_profit_target(0.10, bad, QTY, 80.0) is None


def test_quantity_sign_does_not_change_the_answer():
    """Public API contract; production-derived narrative omitted."""
    for qty_pair in [(-1, 1), (-2, 2), (-5, 5)]:
        neg, pos = qty_pair
        credit = CREDIT * abs(neg)
        a = R.evaluate_short_stop(8.40, credit, neg, 100.0)
        b = R.evaluate_short_stop(8.40, credit, pos, 100.0)
        assert (a is None) == (b is None)
        assert a.pnl_pct == pytest.approx(b.pnl_pct)
        assert a.current_value == pytest.approx(b.current_value)


def test_multi_contract_short_scales_basis_correctly():
    """Public API contract; production-derived narrative omitted."""
    assert R.short_pnl_pct(0.40, 1200.0, -3) == pytest.approx(90.0)




def test_short_trailing_stop_is_a_ceiling_the_price_rises_into():
    """Public API contract; production-derived narrative omitted."""
    assert R.evaluate_short_trailing_stop(2.19, CREDIT, QTY, trough_since_arm=1.00,
                                          activation_gain_pct=50.0, giveback_fraction=0.4,
                                          armed=True) is None
    t = R.evaluate_short_trailing_stop(2.20, CREDIT, QTY, trough_since_arm=1.00,
                                       activation_gain_pct=50.0, giveback_fraction=0.4,
                                       armed=True)
    assert t is not None and t.trigger_type == "trailing_stop"

    assert t.pnl_pct == pytest.approx(45.0)


def test_short_trail_falling_further_does_not_fire():
    """Public API contract; production-derived narrative omitted."""
    assert R.evaluate_short_trailing_stop(0.80, CREDIT, QTY, trough_since_arm=1.00,
                                          activation_gain_pct=50.0, giveback_fraction=0.4,
                                          armed=True) is None


def test_short_trail_unarmed_never_fires():
    """Public API contract; production-derived narrative omitted."""
    assert R.evaluate_short_trailing_stop(3.90, CREDIT, QTY, trough_since_arm=1.00,
                                          activation_gain_pct=50.0, giveback_fraction=0.4,
                                          armed=False) is None
    assert R.evaluate_short_trailing_stop(3.90, CREDIT, QTY, trough_since_arm=None,
                                          activation_gain_pct=50.0, giveback_fraction=0.4,
                                          armed=True) is None


def test_short_trail_requires_the_trough_to_have_cleared_activation():
    """Public API contract; production-derived narrative omitted."""
    assert R.evaluate_short_trailing_stop(3.00, CREDIT, QTY, trough_since_arm=2.50,
                                          activation_gain_pct=50.0, giveback_fraction=0.4,
                                          armed=True) is None


def test_short_trail_direction_disagrees_with_the_long_trail():
    """Public API contract; production-derived narrative omitted."""
    px, ratchet = 2.20, 1.00
    short_fires = R.evaluate_short_trailing_stop(px, CREDIT, QTY, trough_since_arm=ratchet,
                                                 activation_gain_pct=50.0, giveback_fraction=0.4,
                                                 armed=True) is not None
    long_fires = R.evaluate_trailing_stop(px, CREDIT, 1, ratchet, 50.0, 0.4, armed=True) is not None
    assert short_fires and not long_fires


def test_evaluate_position_reinterprets_peak_since_arm_as_a_trough_for_a_short():
    t = R.evaluate_position(
        con_id=9, symbol="SPY", quantity=QTY, entry_debit=0.0, current_price=2.20,
        days_to_expiry=30, peak_price=None,
        rules=_rules(profit_target_pct=95.0, stop_pct=200.0, trailing_enabled=True),
        trail_armed=True, peak_since_arm=1.00, entry_credit=CREDIT)
    assert t is not None and t.trigger_type == "trailing_stop"


def test_short_time_stop_reports_short_signed_pnl():
    t = R.evaluate_short_time_stop(0.40, CREDIT, QTY, days_to_expiry=2, time_stop_days=3)
    assert t is not None and t.trigger_type == "time_stop"
    assert t.pnl_pct == pytest.approx(90.0)


def test_short_priority_full_exit_ordering_matches_the_long_side():
    """Public API contract; production-derived narrative omitted."""
    t = R.evaluate_short_position(
        con_id=1, symbol="SPY", quantity=QTY, entry_credit=CREDIT, current_price=0.10,
        days_to_expiry=1, rules=_rules(profit_target_pct=80.0, stop_pct=100.0, time_stop_days=3))
    assert t.trigger_type == "profit_target"


def test_short_scale_out_is_deliberately_absent():
    """Public API contract; production-derived narrative omitted."""
    r = _rules()
    r.scale_out.enabled = True
    r.scale_out.first_target_pct = 10.0
    t = R.evaluate_short_position(
        con_id=1, symbol="SPY", quantity=-4, entry_credit=1600.0, current_price=3.40,
        days_to_expiry=30, rules=r)
    assert t is None or t.trigger_type != "scale_out"


def test_is_short_quantity_predicate():
    assert R.is_short_quantity(-1) and R.is_short_quantity(-99)
    assert not R.is_short_quantity(0)
    assert not R.is_short_quantity(1)
    assert not R.is_short_quantity(None) and not R.is_short_quantity("x")






def test_short_close_is_BUY_positive_quantity_and_ask_anchored(tmp_path):
    om, conn, _ = _om(tmp_path)
    o = om._build_short_close_order(quantity=-1, limit_price=2.00, market=True, ask=2.10,
                                    trigger_type="stop")
    assert o.action == "BUY"
    assert o.totalQuantity == 1 and o.totalQuantity > 0
    assert o.lmtPrice == pytest.approx(2.10)
    assert conn.calls == [("limit", "BUY", 1, 2.10)]


def test_short_close_never_anchors_to_the_bid(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    om, conn, _ = _om(tmp_path)
    o = om._build_close_order(quantity=1, limit_price=2.00, market=True,
                              bid=0.90, trigger_type="stop", short_close=True, ask=2.10)
    assert o.action == "BUY" and o.lmtPrice == pytest.approx(2.10)


def test_short_close_signed_quantity_never_reaches_the_broker(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    om, conn, _ = _om(tmp_path)
    for q in (-1, -3, -10):
        o = om._build_short_close_order(quantity=q, limit_price=2.00, market=True, ask=2.10)
        assert o.totalQuantity == abs(q) > 0


def test_short_close_slippage_CEILING_refuses_a_junk_ask(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    om, conn, _ = _om(tmp_path)
    o = om._build_short_close_order(quantity=-1, limit_price=2.00, market=True, ask=99.00,
                                    trigger_type="stop")
    assert o.lmtPrice == pytest.approx(3.00)
    assert o.lmtPrice < 99.00


def test_short_ceiling_tracks_the_configured_slippage_knob(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    conn = _Conn()
    sm = StateManager(str(tmp_path / "s.json"))
    om = OrderManager(conn, sm, exit_slippage_floor=0.10)
    o = om._build_short_close_order(quantity=-1, limit_price=2.00, market=True, ask=99.0)
    assert o.lmtPrice == pytest.approx(2.20)
    assert DEFAULT_EXIT_SLIPPAGE_FLOOR == 0.50


def test_short_close_no_ask_hard_stop_is_a_MARKET_buy(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    om, conn, _ = _om(tmp_path)
    o = om._build_short_close_order(quantity=-1, limit_price=2.00, market=True, ask=None,
                                    trigger_type="stop")
    assert o.action == "BUY" and o.orderType == "MKT" and o.totalQuantity == 1


def test_short_close_no_ask_profit_target_rests_passively(tmp_path):
    om, conn, _ = _om(tmp_path)
    o = om._build_short_close_order(quantity=-1, limit_price=2.00, market=True, ask=None,
                                    trigger_type="profit_target")
    assert o.action == "BUY" and o.orderType == "LMT" and o.lmtPrice == pytest.approx(2.00)


def test_short_close_nan_ask_is_ignored(tmp_path):
    om, conn, _ = _om(tmp_path)
    o = om._build_short_close_order(quantity=-1, limit_price=2.00, market=True,
                                    ask=float("nan"), trigger_type="stop")
    assert o.orderType == "MKT"


def test_short_close_untriggered_rests_a_passive_BUY_limit(tmp_path):
    om, conn, _ = _om(tmp_path)
    o = om._build_short_close_order(quantity=-1, limit_price=2.00, market=False, ask=2.10)
    assert o.action == "BUY" and o.orderType == "LMT" and o.lmtPrice == pytest.approx(2.00)




@pytest.mark.parametrize("order,expected,ok", [
    (_Order("BUY", "LMT", 1, 2.10), "BUY", True),
    (_Order("SELL", "LMT", 1, 2.10), "BUY", False),
    (_Order("BUY", "LMT", -1, 2.10), "BUY", False),
    (_Order("BUY", "LMT", 0, 2.10), "BUY", False),
    (_Order("BUY", "LMT", 1, 0.0), "BUY", False),
    (_Order("BUY", "MKT", 1, None), "BUY", True),
    (_Order(None, "LMT", 1, 2.10), "BUY", False),
])
def test_validate_close_order_strict_rejects_every_unsafe_shape(order, expected, ok):
    got, why = OrderManager._validate_close_order(order, expected, strict=True)
    assert got is ok, why


def test_validate_close_order_lenient_mode_tolerates_test_doubles():
    """Public API contract; production-derived narrative omitted."""
    from unittest.mock import MagicMock
    stub = MagicMock()
    ok, why = OrderManager._validate_close_order(stub, "SELL", strict=False)
    assert ok, why

    bad, why2 = OrderManager._validate_close_order(_Order("BUY", "LMT", 1, 2.0), "SELL",
                                                  strict=False)
    assert not bad and "wrong-side" in why2

    neg, _ = OrderManager._validate_close_order(_Order("SELL", "LMT", -1, 2.0), "SELL",
                                                strict=False)
    assert not neg


def test_short_close_with_no_mark_is_refused_not_left_resting_at_zero(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    om, conn, _ = _csp_om(tmp_path)
    res = asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=-1, limit_price=0.0, entry_debit=CREDIT,
        live_open_orders={}, short_close=True, market=False))
    assert not res.success
    assert "limit price must be positive" in res.message
    assert conn.placed == []


def test_short_close_stamps_an_unreadable_builder_side_before_transmit(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    om, conn, _ = _csp_om(tmp_path)
    monkeypatch.setattr(om, "_build_close_order",
                        lambda *a, **k: _Order(None, "LMT", 1, 2.10))
    res = asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=-1, limit_price=2.00, entry_debit=CREDIT,
        live_open_orders={}, short_close=True, ask=2.10, market=True))
    assert res.success
    assert conn.placed[0][1].action == "BUY"


def test_place_close_order_corrects_a_wrong_side_built_order(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    om, conn, _ = _om(tmp_path, portfolio_items=[_PortfolioItem(111, "P", -1)])
    monkeypatch.setattr(om, "_build_close_order",
                        lambda *a, **k: _Order("SELL", "LMT", 1, 2.10))
    res = asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=-1, limit_price=2.00, entry_debit=CREDIT,
        live_open_orders={}, short_close=True, ask=2.10, market=True))
    assert res.success
    assert conn.placed[0][1].action == "BUY"




def _csp_om(tmp_path, qty=-1, right="P", sec_type="OPT", open_orders=None, **kw):
    return _om(tmp_path,
               portfolio_items=[_PortfolioItem(111, right, qty, sec_type)],
               open_orders=open_orders or {}, **kw)


def test_place_short_close_end_to_end(tmp_path):
    om, conn, sm = _csp_om(tmp_path)
    res = asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=-1, limit_price=2.00, entry_debit=CREDIT,
        live_open_orders={}, short_close=True, ask=2.10, market=True,
        trigger_type="stop", exit_context={"symbol": "SPY", "reason": "stop"}))
    assert res.success
    contract, order = conn.placed[0]
    assert order.action == "BUY" and order.totalQuantity == 1
    assert contract.conId == 111
    assert contract.right == "P"
    inf = sm.state.get_in_flight(111)
    assert inf is not None and inf.remaining_qty == 1 > 0
    assert inf.exit_context["close_qty"] == 1
    assert inf.exit_context["is_short"] is True
    assert inf.exit_context["close_action"] == "BUY"


def test_short_close_clamps_to_the_contracts_actually_held(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    om, conn, sm = _csp_om(tmp_path, qty=-2)
    res = asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=-5, limit_price=2.00, entry_debit=CREDIT,
        live_open_orders={}, short_close=True, ask=2.10, market=True))
    assert res.success
    assert conn.placed[0][1].totalQuantity == 2
    assert sm.state.get_in_flight(111).remaining_qty == 2


def test_short_close_refuses_a_spread_combo(tmp_path):
    om, conn, _ = _csp_om(tmp_path)
    res = asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=-1, limit_price=2.00, entry_debit=CREDIT,
        live_open_orders={}, spread={"short_con_id": 222}, short_close=True, ask=2.10))
    assert not res.success and "spread" in res.message
    assert conn.placed == []


def test_short_close_refuses_when_the_right_is_unresolvable(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    om, conn, _ = _csp_om(tmp_path, right="")
    res = asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=-1, limit_price=2.00, entry_debit=CREDIT,
        live_open_orders={}, short_close=True, ask=2.10))
    assert not res.success and "right unresolved" in res.message
    assert conn.placed == []


@pytest.mark.parametrize("qty,frag", [(0, "not short"), (1, "not short")])
def test_short_close_refuses_when_the_position_is_not_short(tmp_path, qty, frag):
    """Public API contract; production-derived narrative omitted."""
    om, conn, _ = _csp_om(tmp_path, qty=qty)
    res = asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=-1, limit_price=2.00, entry_debit=CREDIT,
        live_open_orders={}, short_close=True, ask=2.10))
    assert not res.success and frag in res.message
    assert conn.placed == []


def test_short_close_fails_closed_on_an_unreadable_portfolio(tmp_path):
    om, conn, _ = _om(tmp_path, portfolio_raises=True)
    res = asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=-1, limit_price=2.00, entry_debit=CREDIT,
        live_open_orders={}, short_close=True, ask=2.10))
    assert not res.success and "cannot prove we are short" in res.message
    assert conn.placed == []


def test_short_close_fails_closed_on_an_empty_portfolio(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    om, conn, _ = _om(tmp_path, portfolio_items=[])
    res = asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=-1, limit_price=2.00, entry_debit=CREDIT,
        live_open_orders={}, short_close=True, ask=2.10))
    assert not res.success
    assert "returned no rows" in res.message
    assert "no longer in the portfolio" not in res.message
    assert conn.placed == []






def _reference_long_close_order(om, conn, quantity, limit_price, market, bid, trigger_type):
    """Public API contract; production-derived narrative omitted."""
    mark = limit_price if (limit_price and limit_price > 0) else None
    if not market:
        return conn.create_limit_order("SELL", quantity, limit_price)
    if bid is not None and bid == bid and bid > 0:
        floor = round(mark * (1 - om.EXIT_SLIPPAGE_FLOOR), 2) if mark else 0.01
        floor = max(floor, 0.01)
        px = max(round(bid, 2), floor)
        return conn.create_limit_order("SELL", quantity, px)
    if (trigger_type or "").lower() in om.TARGET_TRIGGERS and mark:
        return conn.create_limit_order("SELL", quantity, round(mark, 2))
    return conn.create_market_order("SELL", quantity)


_LONG_MATRIX = [
    (q, lp, mk, bid, tt)
    for q in (1, 3)
    for lp, mk in ((2.00, True), (2.00, False), (0.0, True), (0.0, False))
    for bid in (None, 1.90, 0.02, float("nan"), 0.0)
    for tt in (None, "stop", "profit_target", "take_profit", "trailing_stop", "manual")
]


@pytest.mark.parametrize("quantity,limit_price,market,bid,trigger_type", _LONG_MATRIX)
def test_long_close_path_is_byte_identical(tmp_path, quantity, limit_price, market, bid,
                                           trigger_type):
    """Public API contract; production-derived narrative omitted."""
    om_a, conn_a, _ = _om(tmp_path / "a")
    om_b, conn_b, _ = _om(tmp_path / "b")
    (tmp_path / "a").mkdir(exist_ok=True)
    (tmp_path / "b").mkdir(exist_ok=True)

    produced = om_a._build_close_order(quantity, limit_price, market,
                                       bid=bid, trigger_type=trigger_type)
    expected = _reference_long_close_order(om_b, conn_b, quantity, limit_price, market,
                                           bid, trigger_type)
    assert produced.key() == expected.key()
    assert conn_a.calls == conn_b.calls


@pytest.mark.parametrize("quantity,limit_price,market,bid,trigger_type", _LONG_MATRIX[::17])
def test_ask_argument_is_inert_on_the_long_path(tmp_path, quantity, limit_price, market, bid,
                                                trigger_type):
    """Public API contract; production-derived narrative omitted."""
    om, conn, _ = _om(tmp_path)
    without = om._build_close_order(quantity, limit_price, market, bid=bid,
                                    trigger_type=trigger_type)
    with_ask = om._build_close_order(quantity, limit_price, market, bid=bid,
                                     trigger_type=trigger_type, ask=7.77, short_close=False)
    assert without.key() == with_ask.key()


def test_long_close_still_places_a_SELL_end_to_end(tmp_path):
    om, conn, sm = _om(tmp_path, portfolio_items=[_PortfolioItem(111, "C", 1)])
    res = asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=1, limit_price=2.50, entry_debit=250.0,
        live_open_orders={}))
    assert res.success
    assert conn.placed[0][1].action == "SELL"
    assert sm.state.get_in_flight(111).remaining_qty == 1


def test_long_path_never_consults_the_short_open_order_read(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    om, conn, _ = _om(tmp_path, portfolio_items=[_PortfolioItem(111, "C", 1)])
    asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=1, limit_price=2.50, entry_debit=250.0,
        live_open_orders={}))
    assert conn.open_order_queries == []


def test_long_evaluate_position_is_unchanged_by_the_short_routing():
    """Public API contract; production-derived narrative omitted."""
    r = _rules(profit_target_pct=100.0, stop_pct=50.0)
    a = R.evaluate_position(con_id=1, symbol="SPY", quantity=1, entry_debit=200.0,
                            current_price=4.00, days_to_expiry=30, peak_price=None, rules=r)
    b = R.evaluate_position(con_id=1, symbol="SPY", quantity=1, entry_debit=200.0,
                            current_price=4.00, days_to_expiry=30, peak_price=None, rules=r,
                            entry_credit=999.0)
    assert a is not None and b is not None
    assert (a.trigger_type, a.pnl_pct, a.is_short) == (b.trigger_type, b.pnl_pct, b.is_short)
    assert a.is_short is False and a.entry_credit is None


def test_zero_quantity_still_returns_None_on_both_families():
    """Public API contract; production-derived narrative omitted."""
    r = _rules()
    assert R.evaluate_position(con_id=1, symbol="SPY", quantity=0, entry_debit=200.0,
                               current_price=0.1, days_to_expiry=30, peak_price=None,
                               rules=r, entry_credit=CREDIT) is None






def test_resting_buy_to_close_is_visible_even_though_the_managers_dict_is_blind(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    om, conn, _ = _csp_om(tmp_path, open_orders={111: _OrderData(111, order_id=901, remaining=1)})
    ok, why = asyncio.run(om.can_place_close(111, 1, live_open_orders={}, short_close=True))
    assert not ok
    assert "resting buy-to-close" in why and "901" in why


def test_the_short_read_admits_exactly_this_con_id(tmp_path):
    om, conn, _ = _csp_om(tmp_path, open_orders={})
    asyncio.run(om.can_place_close(111, 1, live_open_orders={}, short_close=True))
    assert conn.open_order_queries == [{111}]


def test_a_second_close_is_refused_so_the_cover_is_never_doubled(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    om, conn, sm = _csp_om(tmp_path, open_orders={})
    r1 = asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=-1, limit_price=2.00, entry_debit=CREDIT,
        live_open_orders={}, short_close=True, ask=2.10, market=True))
    assert r1.success

    r2 = asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=-1, limit_price=2.00, entry_debit=CREDIT,
        live_open_orders={}, short_close=True, ask=2.10, market=True))
    assert not r2.success and "in-flight" in r2.message
    assert len(conn.placed) == 1


def test_unverifiable_open_orders_fail_CLOSED(tmp_path):
    om, conn, _ = _csp_om(tmp_path, open_orders_raises="gateway down")
    ok, why = asyncio.run(om.can_place_close(111, 1, live_open_orders={}, short_close=True))
    assert not ok and "cannot verify" in why


def test_connection_without_short_leg_support_fails_CLOSED(tmp_path):
    om, conn, _ = _csp_om(tmp_path, supports_short_leg_kwarg=False)
    ok, why = asyncio.run(om.can_place_close(111, 1, live_open_orders={}, short_close=True))
    assert not ok and "cannot verify" in why


def test_no_resting_cover_allows_the_close(tmp_path):
    om, conn, _ = _csp_om(tmp_path, open_orders={999: _OrderData(999)})
    ok, why = asyncio.run(om.can_place_close(111, 1, live_open_orders={}, short_close=True))
    assert ok and why == ""


def test_short_read_does_not_mutate_the_dict_reconcile_also_uses(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    loo = {}
    before = dict(loo)
    om, conn, _ = _csp_om(tmp_path, open_orders={111: _OrderData(111)})
    asyncio.run(om.can_place_close(111, 1, live_open_orders=loo, short_close=True))
    assert loo == before == {}


def test_short_read_is_never_issued_when_short_close_is_false(tmp_path):
    om, conn, _ = _csp_om(tmp_path, open_orders={111: _OrderData(111)})
    ok, _ = asyncio.run(om.can_place_close(111, 1, live_open_orders={}, short_close=False))
    assert ok
    assert conn.open_order_queries == []


def test_manager_supplied_live_order_still_blocks(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    om, conn, _ = _csp_om(tmp_path)
    ok, why = asyncio.run(om.can_place_close(
        111, 1, live_open_orders={111: {"order_id": 42}}, short_close=True))
    assert not ok and "live open order" in why






def test_normalize_short_context_forces_positive_quantities():
    ctx = OrderManager._normalize_short_context(
        {"close_qty": -1, "position_qty": -1, "symbol": "SPY"}, close_qty=-1)
    assert ctx["close_qty"] == 1
    assert ctx["position_qty"] == 1
    assert ctx["is_short"] is True and ctx["close_action"] == "BUY"
    assert ctx["symbol"] == "SPY"


def test_finalize_guard_would_have_returned_False_forever_on_a_signed_context():
    """Public API contract; production-derived narrative omitted."""
    raw = {"close_qty": -1}
    assert int(raw.get("close_qty") or 0) <= 0
    fixed = OrderManager._normalize_short_context(raw, close_qty=-1)
    assert int(fixed.get("close_qty") or 0) > 0


def test_unfilled_alarm_guard_sees_a_short_in_flight(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    om, conn, sm = _csp_om(tmp_path)
    asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=-1, limit_price=2.00, entry_debit=CREDIT,
        live_open_orders={}, short_close=True, ask=2.10, market=True))
    inf = sm.state.get_in_flight(111)
    assert inf.order_id and inf.placed_at and inf.remaining_qty > 0


def test_manager_guard_expressions_are_still_what_this_suite_assumes():
    """Public API contract; production-derived narrative omitted."""
    from exitmgr.manager import ExitManager
    alert_src = inspect.getsource(ExitManager._alert_unfilled_orders)
    final_src = inspect.getsource(ExitManager._finalize_in_flight_exit)
    assert "inf.remaining_qty <= 0" in alert_src






    assert '_durable_qty(ctx, "close_qty"' in final_src and "planned_qty <= 0" in final_src


def test_a_rejected_short_close_releases_the_in_flight_so_it_can_retry(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    om, conn, sm = _csp_om(tmp_path)

    async def _reject(contract, order):
        conn.placed.append((contract, order))
        return _Trade(order, status="Cancelled", filled=0)

    conn.place_order = _reject
    res = asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=-1, limit_price=2.00, entry_debit=CREDIT,
        live_open_orders={}, short_close=True, ask=2.10, market=True))
    assert not res.success
    assert sm.state.get_in_flight(111) is None


def test_an_ambiguous_transmission_retains_the_intent(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    om, conn, sm = _csp_om(tmp_path)

    async def _boom(contract, order):
        raise RuntimeError("socket died")

    conn.place_order = _boom
    res = asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=-1, limit_price=2.00, entry_debit=CREDIT,
        live_open_orders={}, short_close=True, ask=2.10, market=True))
    assert not res.success
    inf = sm.state.get_in_flight(111)
    assert inf is not None and inf.remaining_qty == 1


def test_a_refused_short_close_is_reported_not_swallowed(tmp_path, capsys):
    om, conn, _ = _csp_om(tmp_path, qty=1)
    res = asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=-1, limit_price=2.00, entry_debit=CREDIT,
        live_open_orders={}, short_close=True, ask=2.10))
    assert not res.success
    assert "Refusing buy-to-close" in capsys.readouterr().out
    assert res.message


def test_short_exit_context_survives_json_round_trip(tmp_path):
    om, conn, sm = _csp_om(tmp_path)
    asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=-1, limit_price=2.00, entry_debit=CREDIT,
        live_open_orders={}, short_close=True, ask=2.10, market=True,
        exit_context={"symbol": "SPY", "trigger_pnl_pct": 90.0}))
    ctx = sm.state.get_in_flight(111).exit_context
    json.dumps(ctx, allow_nan=False)
    assert ctx["trigger_pnl_pct"] == 90.0






def test_assigned_csp_option_row_is_gone_so_no_option_order_is_sent(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    om, conn, _ = _om(tmp_path, portfolio_items=[
        _PortfolioItem(222, right="", qty=100, sec_type="STK", symbol="SPY")])
    res = asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=-1, limit_price=2.00, entry_debit=CREDIT,
        live_open_orders={}, short_close=True, ask=2.10))
    assert not res.success and "no longer in the portfolio" in res.message
    assert conn.placed == []


def test_a_stock_row_under_the_same_con_id_is_refused_as_wrong_instrument(tmp_path):
    om, conn, _ = _csp_om(tmp_path, right="P", sec_type="STK")
    res = asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=-100, limit_price=2.00, entry_debit=CREDIT,
        live_open_orders={}, short_close=True, ask=2.10))
    assert not res.success and "not an option" in res.message
    assert conn.placed == []


def test_assignment_refusal_leaves_no_durable_state_behind(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    om, conn, sm = _om(tmp_path, portfolio_items=[
        _PortfolioItem(222, right="", qty=100, sec_type="STK")])
    asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=-1, limit_price=2.00, entry_debit=CREDIT,
        live_open_orders={}, short_close=True, ask=2.10))
    assert sm.state.get_in_flight(111) is None


def test_futures_option_short_is_still_closeable(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    om, conn, _ = _csp_om(tmp_path, sec_type="FOP")
    res = asyncio.run(om.place_close_order(
        con_id=111, symbol="SPY", quantity=-1, limit_price=2.00, entry_debit=CREDIT,
        live_open_orders={}, short_close=True, ask=2.10, market=True))
    assert res.success and conn.placed[0][1].action == "BUY"
