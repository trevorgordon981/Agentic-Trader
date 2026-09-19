"""Public API contract; production-derived narrative omitted."""

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from exitmgr import connection as conn_mod
from exitmgr import trader as trader_mod
from exitmgr.account import PotSnapshot
from exitmgr.config import Config, RulesConfig, ScaleOutConfig, TrailingConfig
from exitmgr.connection import OrderData, PositionData
from exitmgr.manager import ExitManager
from exitmgr.order import OrderManager, OrderResult
from exitmgr.risk import RiskLimits
from exitmgr.state import StateManager
from exitmgr.strategist import TradeIdea
from tests._stage_stub import stub_stage_a


CON = 884401
JOURNAL = {
    "ts": "2026-08-01T14:00:00+00:00",
    "contract_id": CON,
    "symbol": "AAPL",
    "right": "C",
    "strike": 200.0,
    "expiry": "20261231",
    "quantity": 4,
    "debit": 2000.0,
    "conviction": 7,
}


def _manager(tmp_path, *, stop_pct=30.0):
    cfg = Config()
    cfg.dry_run = False
    cfg.loop_mode = False
    cfg.journal.path = str(tmp_path / "trades.log")
    cfg.state.path = str(tmp_path / "state.json")
    cfg.kill_switch.path = str(tmp_path / "KILL")
    cfg.audit_path = str(tmp_path / "audit.jsonl")
    cfg.manage_positions = False
    cfg.alerts_channel = ""
    cfg.error_channel = ""
    cfg.rules = RulesConfig(
        profit_target_pct=300.0,
        stop_pct=stop_pct,
        time_stop_days=0,
        trailing=TrailingConfig(enabled=False),
        scale_out=ScaleOutConfig(enabled=False),
    )
    (tmp_path / "trades.log").write_text(json.dumps(JOURNAL) + "\n")
    return ExitManager(cfg)


def _wire(mgr, *, quotes=None, positions=None):
    """Public API contract; production-derived narrative omitted."""
    pos = positions if positions is not None else {
        CON: PositionData(con_id=CON, symbol="AAPL", right="C", quantity=4,
                          avg_cost=5.0, expiry="20261231")}
    mgr.ib_conn.get_positions = AsyncMock(return_value=pos)
    mgr.ib_conn.fetch_quotes = AsyncMock(return_value=quotes or {})
    mgr.ib_conn.ib = MagicMock()
    mgr.ib_conn.ib.trades.return_value = []
    mgr.ib_conn.ib.reqCompletedOrdersAsync = AsyncMock(return_value=[])
    mgr.ib_conn.ib.reqExecutionsAsync = AsyncMock(return_value=[])
    return mgr


def _reconcile_clean_then(exc_or_value):
    """Public API contract; production-derived narrative omitted."""
    calls = {"n": 0}

    async def _go(short_leg_con_ids=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return {}
        if isinstance(exc_or_value, BaseException):
            raise exc_or_value
        return dict(exc_or_value)

    return _go, calls






@pytest.mark.asyncio
async def test_an_unreadable_open_order_read_with_an_existing_close_places_ZERO_orders(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr = _wire(_manager(tmp_path),
                quotes={CON: {"price": 3.0, "bid": 2.9, "ask": 3.1, "mark": 3.0}})
    (tmp_path / "manual_exits.json").write_text(json.dumps([CON]))
    mgr.config.manual_exits_path = str(tmp_path / "manual_exits.json")
    go, calls = _reconcile_clean_then(asyncio.TimeoutError())
    mgr.ib_conn.get_open_orders = go
    place = AsyncMock(return_value=OrderResult(success=True, message="", con_id=CON, order_id=99))
    mgr.order_manager.place_close_order = place

    await mgr.run_cycle(dry_run=False)

    assert calls["n"] >= 2, "the cycle never got as far as the read under test"
    place.assert_not_awaited()


@pytest.mark.asyncio
async def test_NEGATIVE_CONTROL_a_clean_empty_read_still_places_the_manual_close(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr = _wire(_manager(tmp_path),
                quotes={CON: {"price": 3.0, "bid": 2.9, "ask": 3.1, "mark": 3.0}})
    (tmp_path / "manual_exits.json").write_text(json.dumps([CON]))
    mgr.config.manual_exits_path = str(tmp_path / "manual_exits.json")
    go, _calls = _reconcile_clean_then({})
    mgr.ib_conn.get_open_orders = go
    place = AsyncMock(return_value=OrderResult(success=True, message="", con_id=CON, order_id=99))
    mgr.order_manager.place_close_order = place

    await mgr.run_cycle(dry_run=False)

    place.assert_awaited()
    assert place.await_args.kwargs["con_id"] == CON






def test_known_empty_is_distinguishable_from_unknown():
    """Public API contract; production-derived narrative omitted."""
    empty = conn_mod.OrderView.known({})
    unknown = conn_mod.OrderView.unknown("timed out (TimeoutError)")
    assert empty.readable is True and empty.orders == {}
    assert unknown.readable is False
    assert empty.orders == unknown.orders
    assert empty.readable != unknown.readable


def test_an_OrderView_has_NO_truth_value():
    """Public API contract; production-derived narrative omitted."""
    with pytest.raises(TypeError, match="no truth value"):
        bool(conn_mod.OrderView.unknown("broker down"))
    with pytest.raises(TypeError, match="no truth value"):
        if conn_mod.OrderView.known({}):
            pass


def test_an_unknown_view_CANNOT_be_flattened_into_an_empty_map():
    """Public API contract; production-derived narrative omitted."""
    assert conn_mod.OrderView.known({}).as_state_dicts() == {}
    with pytest.raises(conn_mod.OrderBookUnreadable):
        conn_mod.OrderView.unknown("timed out (TimeoutError)").as_state_dicts()






@pytest.mark.asyncio
async def test_a_None_open_order_response_is_UNKNOWN_not_empty():
    """Public API contract; production-derived narrative omitted."""
    c = conn_mod.IBConnection.__new__(conn_mod.IBConnection)
    c._require_link = lambda: None
    c.ib = MagicMock()
    c.ib.reqAllOpenOrdersAsync = AsyncMock(return_value=None)
    with pytest.raises(conn_mod.OrderBookUnreadable):
        await c.get_open_orders()
    view = await c.get_open_orders_view()
    assert view.readable is False


@pytest.mark.asyncio
async def test_NEGATIVE_CONTROL_a_real_empty_list_is_a_readable_empty_book():
    c = conn_mod.IBConnection.__new__(conn_mod.IBConnection)
    c._require_link = lambda: None
    c.ib = MagicMock()
    c.ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[])
    view = await c.get_open_orders_view()
    assert view.readable is True and view.orders == {}


@pytest.mark.asyncio
async def test_a_timeout_is_reported_with_its_TYPE_never_a_bare_colon():
    """Public API contract; production-derived narrative omitted."""
    c = conn_mod.IBConnection.__new__(conn_mod.IBConnection)
    c._require_link = lambda: None
    c.ib = MagicMock()
    c.ib.reqAllOpenOrdersAsync = AsyncMock(side_effect=asyncio.TimeoutError())
    view = await c.get_open_orders_view()
    assert view.readable is False
    assert view.error and "TimeoutError" in view.error






def _order_manager(tmp_path):
    return OrderManager(MagicMock(), StateManager(str(tmp_path / "state.json")))


@pytest.mark.asyncio
async def test_can_place_close_REFUSES_an_unreadable_order_view(tmp_path):
    om = _order_manager(tmp_path)
    ok, why = await om.can_place_close(
        CON, 4, conn_mod.OrderView.unknown("timed out (TimeoutError)"))
    assert ok is False
    assert "cannot verify" in why and "TimeoutError" in why


@pytest.mark.asyncio
async def test_NEGATIVE_CONTROL_can_place_close_PERMITS_a_readable_empty_view(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    om = _order_manager(tmp_path)
    ok, why = await om.can_place_close(CON, 4, conn_mod.OrderView.known({}))
    assert ok is True and why == ""


@pytest.mark.asyncio
async def test_a_readable_view_that_SEES_a_resting_close_still_refuses(tmp_path):
    om = _order_manager(tmp_path)
    view = conn_mod.OrderView.known(
        {CON: OrderData(con_id=CON, order_id=4242, remaining=4)})
    ok, why = await om.can_place_close(CON, 4, view)
    assert ok is False and "4242" in why


@pytest.mark.asyncio
async def test_the_legacy_dict_call_is_unchanged(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    om = _order_manager(tmp_path)
    assert (await om.can_place_close(CON, 4, {})) == (True, "")
    ok, why = await om.can_place_close(CON, 4, {CON: {"order_id": 77, "remaining": 4}})
    assert ok is False and "77" in why






@pytest.mark.asyncio
async def test_a_mechanical_stop_is_WITHHELD_when_the_order_book_is_unreadable(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr = _wire(_manager(tmp_path),
                quotes={CON: {"price": 3.0, "bid": 2.9, "ask": 3.1, "mark": 3.0}})
    go, calls = _reconcile_clean_then(asyncio.TimeoutError())
    mgr.ib_conn.get_open_orders = go
    place = AsyncMock(return_value=OrderResult(success=True, message="", con_id=CON, order_id=1))
    mgr.order_manager.place_close_order = place
    alerts = []
    mgr._post_stops_withheld_alert = lambda items, reason=None: alerts.append((items, reason))

    await mgr.run_cycle(dry_run=False)

    place.assert_not_awaited()
    assert alerts, "a withheld protective stop must never be silent"
    _items, _reason = alerts[0]
    assert [cid for _s, cid, _w in _items] == [CON]
    assert _reason and "UNREADABLE" in _reason, (
        "an operator must be told the broker could not be read, NOT sent to fix a position "
        "that is not at fault")


@pytest.mark.asyncio
async def test_NEGATIVE_CONTROL_the_same_stop_fires_when_the_book_reads_clean(tmp_path):
    mgr = _wire(_manager(tmp_path),
                quotes={CON: {"price": 3.0, "bid": 2.9, "ask": 3.1, "mark": 3.0}})
    go, _calls = _reconcile_clean_then({})
    mgr.ib_conn.get_open_orders = go
    place = AsyncMock(return_value=OrderResult(success=True, message="", con_id=CON, order_id=1))
    mgr.order_manager.place_close_order = place
    alerts = []
    mgr._post_stops_withheld_alert = lambda items, reason=None: alerts.append((items, reason))

    await mgr.run_cycle(dry_run=False)

    place.assert_awaited()
    assert not alerts


@pytest.mark.asyncio
async def test_reconnect_restores_readability_and_the_refusal_lifts(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr = _wire(_manager(tmp_path),
                quotes={CON: {"price": 3.0, "bid": 2.9, "ask": 3.1, "mark": 3.0}})
    state = {"down": True}

    async def _go(short_leg_con_ids=None):
        if state["down"]:
            raise asyncio.TimeoutError()
        return {}


    first = {"n": 0}

    async def _wrapped(short_leg_con_ids=None):
        first["n"] += 1
        if first["n"] == 1:
            return {}
        return await _go(short_leg_con_ids)

    mgr.ib_conn.get_open_orders = _wrapped
    place = AsyncMock(return_value=OrderResult(success=True, message="", con_id=CON, order_id=1))
    mgr.order_manager.place_close_order = place
    mgr._post_stops_withheld_alert = lambda items, reason=None: None

    await mgr.run_cycle(dry_run=False)
    place.assert_not_awaited()

    state["down"] = False
    first["n"] = 99
    await mgr.run_cycle(dry_run=False)
    place.assert_awaited()


@pytest.mark.asyncio
async def test_an_unreadable_book_never_advances_the_in_flight_intent_release(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr = _wire(_manager(tmp_path))
    go, _calls = _reconcile_clean_then(asyncio.TimeoutError())
    mgr.ib_conn.get_open_orders = go
    mgr._poll_in_flight_fills = AsyncMock(return_value=set())

    await mgr.run_cycle(dry_run=False)

    mgr._poll_in_flight_fills.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_unreadable_order_book_also_suppresses_ENTRIES(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr = _wire(_manager(tmp_path))
    go, _calls = _reconcile_clean_then(asyncio.TimeoutError())
    mgr.ib_conn.get_open_orders = go
    mgr.order_manager.place_close_order = AsyncMock()
    mgr._post_stops_withheld_alert = lambda items, reason=None: None

    await mgr.run_cycle(dry_run=False)
    assert mgr._reconcile_ok is False


@pytest.mark.asyncio
async def test_NEGATIVE_CONTROL_a_readable_cycle_leaves_entries_armed(tmp_path):
    mgr = _wire(_manager(tmp_path))
    mgr.ib_conn.get_open_orders = AsyncMock(return_value={})
    mgr.order_manager.place_close_order = AsyncMock()
    await mgr.run_cycle(dry_run=False)
    assert mgr._reconcile_ok is True






@pytest.mark.asyncio
async def test_an_unreadable_reconcile_read_leaves_no_con_id_provably_clean(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr = _wire(_manager(tmp_path))
    mgr._reconcile_bad_con_ids = set()
    mgr.ib_conn.get_open_orders = AsyncMock(side_effect=asyncio.TimeoutError())

    assert await mgr._reconcile_on_startup() is False
    assert mgr._reconcile_bad_con_ids is None, (
        "no per-con_id detail exists after a failed read; None is the only honest answer")


@pytest.mark.asyncio
async def test_NEGATIVE_CONTROL_a_readable_reconcile_still_yields_a_per_con_id_verdict(tmp_path):
    mgr = _wire(_manager(tmp_path))
    mgr.ib_conn.get_open_orders = AsyncMock(return_value={})
    await mgr._reconcile_on_startup()
    assert mgr._reconcile_bad_con_ids is not None






LIM = RiskLimits(max_concurrent=8)
IDEA = TradeIdea("SPY", True, "bullish", "long call", 7, 0.35, 90.0, 4, "trend")


def _trader(tmp_path):
    ibc = MagicMock(); ibc.ib = MagicMock()
    ibc.ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[])
    ibc.get_positions = AsyncMock(return_value={})
    em = MagicMock(); em.run_cycle = AsyncMock()
    t = trader_mod.Trader(
        ib_conn=ibc, exit_manager=em, limits=LIM, approved_names=set(),
        endpoint="http://x", model="m", slack_token="tok", slack_channel="C1",
        approver_ids={"OWNER"}, baseline_path=str(tmp_path / "b.json"),
        audit_path=str(tmp_path / "a.jsonl"), approve_timeout_s=60,
        trading_down_path=str(tmp_path / "TRADING_DOWN"))
    resolved = trader_mod.ResolvedOrder(
        "SPY", "C", "20260620", 50.0, 1, 1.20, MagicMock(conId=123),
        entry_bid=1.15, entry_ask=1.25, quote_observed_at=time.monotonic(),
        decision_id="decision-" + "a" * 32)
    t._resolve_order = AsyncMock(return_value=resolved)
    t._refresh_approved_entry = AsyncMock(
        side_effect=lambda idea, original, baseline: (
            original, PotSnapshot(1010.0, 9000.0, 1010.0), ()))
    t._submit_order = AsyncMock(return_value=("Filled", []))
    return t, ibc


@pytest.fixture
def _hermetic_entry(monkeypatch):
    monkeypatch.setattr(trader_mod.research, "gather", AsyncMock(return_value={}))
    monkeypatch.setattr(trader_mod.research, "days_to_earnings", lambda *a, **k: None)
    monkeypatch.setattr(trader_mod.research, "days_to_ex_dividend", lambda *a, **k: None)
    monkeypatch.setattr(trader_mod, "_market_open", lambda: True)
    monkeypatch.setattr(trader_mod, "get_pot_snapshot",
                        AsyncMock(return_value=PotSnapshot(1010.0, 9000.0, 1010.0)))
    monkeypatch.setattr(trader_mod.approval, "post_proposal", lambda *a, **k: "ts1")
    monkeypatch.setattr(trader_mod.approval, "await_approval", lambda *a, **k: "approve")


def _audit_events(tmp_path):
    p = tmp_path / "a.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


@pytest.mark.asyncio
async def test_an_unreadable_active_buy_read_blocks_a_new_entry(
        tmp_path, monkeypatch, _hermetic_entry):
    """Public API contract; production-derived narrative omitted."""
    stub_stage_a(monkeypatch, lambda *a, **k: [IDEA])
    t, ibc = _trader(tmp_path)
    ibc.ib.reqAllOpenOrdersAsync = AsyncMock(side_effect=asyncio.TimeoutError())

    await t.run_once(dry_run=False)

    t._submit_order.assert_not_awaited()
    blocks = [e for e in _audit_events(tmp_path)
              if e["event"] == "entry_blocked_order_book_unreadable"]
    assert blocks, "the refusal must be AUDITED, not merely logged"
    assert blocks[0]["stage"] == "pre_exit_book"
    assert "TimeoutError" in str(blocks[0]["error"]), (
        "an error that stringifies to '' must still name its TYPE")


@pytest.mark.asyncio
async def test_NEGATIVE_CONTROL_the_same_idea_is_submitted_when_the_book_reads_clean(
        tmp_path, monkeypatch, _hermetic_entry):
    stub_stage_a(monkeypatch, lambda *a, **k: [IDEA])
    t, ibc = _trader(tmp_path)
    ibc.ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[])

    await t.run_once(dry_run=False)

    t._submit_order.assert_awaited()


@pytest.mark.asyncio
async def test_an_unreadable_close_in_flight_scan_blocks_a_new_entry(
        tmp_path, monkeypatch, _hermetic_entry):
    """Public API contract; production-derived narrative omitted."""
    stub_stage_a(monkeypatch, lambda *a, **k: [IDEA])
    t, ibc = _trader(tmp_path)
    calls = {"n": 0}

    async def _orders():
        calls["n"] += 1

        if calls["n"] >= 3:
            raise asyncio.TimeoutError()
        return []

    ibc.ib.reqAllOpenOrdersAsync = AsyncMock(side_effect=_orders)

    await t.run_once(dry_run=False)

    t._submit_order.assert_not_awaited()
    assert any(e["event"] == "close_inflight_orders_error" for e in _audit_events(tmp_path))


@pytest.mark.asyncio
async def test_an_unreadable_POST_EXIT_refetch_alone_blocks_a_new_entry(
        tmp_path, monkeypatch, _hermetic_entry):
    """Public API contract; production-derived narrative omitted."""
    stub_stage_a(monkeypatch, lambda *a, **k: [IDEA])
    t, ibc = _trader(tmp_path)
    calls = {"n": 0}

    async def _orders():
        calls["n"] += 1
        if calls["n"] == 3:
            raise asyncio.TimeoutError()
        return []

    ibc.ib.reqAllOpenOrdersAsync = AsyncMock(side_effect=_orders)

    await t.run_once(dry_run=False)

    assert calls["n"] >= 4, "the cycle never reached the reads after the refetch"
    t._submit_order.assert_not_awaited()
    blocks = [e for e in _audit_events(tmp_path)
              if e["event"] == "entry_blocked_order_book_unreadable"]
    assert [b["stage"] for b in blocks] == ["post_exit_refetch"], (
        "exactly one limb must have refused, and it must be the refetch")


@pytest.mark.asyncio
async def test_the_close_in_flight_scan_reports_readability(tmp_path, _hermetic_entry):
    """Public API contract; production-derived narrative omitted."""
    t, ibc = _trader(tmp_path)
    ibc.ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[])
    names, readable = await t._underlyings_with_close_in_flight()
    assert names == set() and readable is True

    ibc.ib.reqAllOpenOrdersAsync = AsyncMock(side_effect=asyncio.TimeoutError())
    names, readable = await t._underlyings_with_close_in_flight()
    assert names == set() and readable is False

    ibc.ib.reqAllOpenOrdersAsync = AsyncMock(return_value=None)
    names, readable = await t._underlyings_with_close_in_flight()
    assert readable is False, "a None response is UNKNOWN, not 'no names closing'"
