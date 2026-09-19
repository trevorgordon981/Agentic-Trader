"""Public API contract; production-derived narrative omitted."""
import asyncio
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock, MagicMock

from exitmgr.config import Config, RulesConfig, TrailingConfig, ScaleOutConfig
from exitmgr.connection import IBConnection, PositionData
from exitmgr.order import OrderResult
from exitmgr.manager import ExitManager
from exitmgr.trader import _market_open_at


@pytest.fixture(autouse=True)
def regular_session_clock(monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    instant = datetime(2026, 9, 8, 17, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("exitmgr.trader._market_open", lambda: _market_open_at(instant))


CON = 4100
SCID = 4200

JOURNAL = {"ts": "2026-07-01T14:00:00+00:00", "contract_id": CON, "symbol": "AAPL",
           "right": "C", "strike": 200.0, "expiry": "20261231", "quantity": 4, "debit": 2000.0,
           "conviction": 6}

SPREAD_JOURNAL = dict(JOURNAL, debit=800.0,
                      spread={"short_con_id": SCID, "short_strike": 210.0, "width": 10.0})


class _PortRow:
    """Public API contract; production-derived narrative omitted."""

    def __init__(self, con_id, market_price, position=4):
        self.contract = MagicMock()
        self.contract.conId = con_id
        self.position = position
        self.marketPrice = market_price


def _mgr(tmp_path, journal=JOURNAL):
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
    cfg.rules = RulesConfig(profit_target_pct=100.0, stop_pct=30.0, time_stop_days=10,
                            trailing=TrailingConfig(enabled=False),
                            scale_out=ScaleOutConfig(enabled=False))
    (tmp_path / "trades.log").write_text(json.dumps(journal) + "\n")
    return ExitManager(cfg), cfg


def _wire(mgr, *, quotes, portfolio=(), positions=None):
    if positions is None:
        positions = {CON: PositionData(con_id=CON, symbol="AAPL", right="C",
                                       quantity=4, avg_cost=5.00, expiry="20261231")}
    mgr.ib_conn.get_positions = AsyncMock(return_value=positions)
    mgr.ib_conn.get_open_orders = AsyncMock(return_value={})
    mgr.ib_conn.fetch_quotes = AsyncMock(return_value=quotes)
    mgr.ib_conn.ib = MagicMock()
    mgr.ib_conn.ib.portfolio = lambda: list(portfolio)
    mgr._spot_price = AsyncMock(return_value=None)
    place = AsyncMock(return_value=OrderResult(success=True, order_id=555, con_id=CON, trade=None))
    mgr.order_manager.place_close_order = place
    alert = MagicMock(return_value=True)
    mgr._post_stops_withheld_alert = alert
    return place, alert


def _blind_alert_rows(alert):
    """Public API contract; production-derived narrative omitted."""
    rows = []
    for call in alert.call_args_list:
        reason = call.kwargs.get("reason") or ""
        if "usable price" in reason:
            rows.extend(call.args[0])
    return rows


def _healthy(conn):
    conn._connected = True
    conn._uplink_ok = True
    conn._link_fault = False
    conn._price_events_accepted = True
    conn.ib.isConnected.return_value = True



@pytest.mark.asyncio
async def test_missing_quote_still_stops_on_the_broker_mark(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr, _ = _mgr(tmp_path)
    place, alert = _wire(mgr, quotes={}, portfolio=[_PortRow(CON, 3.50)])
    await mgr.run_cycle(dry_run=False)

    place.assert_called_once()
    kw = place.call_args.kwargs
    assert kw["con_id"] == CON
    assert kw["quantity"] == 4
    assert kw["trigger_type"] == "stop"

    assert _blind_alert_rows(alert) == []


@pytest.mark.asyncio
async def test_quote_batch_timeout_still_stops_on_the_broker_mark(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr, _ = _mgr(tmp_path)
    place, alert = _wire(mgr, quotes={}, portfolio=[_PortRow(CON, 3.50)])
    mgr.ib_conn.fetch_quotes = AsyncMock(side_effect=asyncio.TimeoutError())

    await mgr.run_cycle(dry_run=False)

    place.assert_called_once()
    assert place.call_args.kwargs["trigger_type"] == "stop"
    assert _blind_alert_rows(alert) == []


@pytest.mark.asyncio
async def test_quote_batch_exception_still_stops_on_the_broker_mark(tmp_path):
    mgr, _ = _mgr(tmp_path)
    place, alert = _wire(mgr, quotes={}, portfolio=[_PortRow(CON, 3.50)])
    mgr.ib_conn.fetch_quotes = AsyncMock(side_effect=RuntimeError("quote feed broke"))

    await mgr.run_cycle(dry_run=False)

    place.assert_called_once()
    assert place.call_args.kwargs["trigger_type"] == "stop"
    assert _blind_alert_rows(alert) == []


@pytest.mark.asyncio
async def test_real_quote_hang_is_bounded_and_falls_through_to_broker_mark(tmp_path):
    mgr, _ = _mgr(tmp_path)
    place, _alert = _wire(mgr, quotes={}, portfolio=[_PortRow(CON, 3.50)])
    mgr.PROTECTIVE_QUOTE_TIMEOUT_S = 0.01

    async def _never(_con_ids):
        await asyncio.Event().wait()

    mgr.ib_conn.fetch_quotes = AsyncMock(side_effect=_never)

    await asyncio.wait_for(mgr.run_cycle(dry_run=False), timeout=0.5)

    place.assert_called_once()
    assert place.call_args.kwargs["trigger_type"] == "stop"


@pytest.mark.asyncio
async def test_quote_timeout_without_any_broker_mark_refuses_and_alerts(tmp_path):
    mgr, _ = _mgr(tmp_path)
    place, alert = _wire(mgr, quotes={}, portfolio=[])
    mgr.ib_conn.fetch_quotes = AsyncMock(side_effect=asyncio.TimeoutError())

    await mgr.run_cycle(dry_run=False)

    place.assert_not_called()
    rows = _blind_alert_rows(alert)
    assert [(symbol, con_id) for symbol, con_id, _why in rows] == [("AAPL", CON)]


@pytest.mark.asyncio
async def test_armed_cycle_uses_fresh_event_mark_without_inline_quote_io(tmp_path):
    mgr, cfg = _mgr(tmp_path)
    cfg.arm = True
    place, _alert = _wire(mgr, quotes={}, portfolio=[_PortRow(CON, 99.0)])
    _healthy(mgr.ib_conn)
    mgr.ib_conn._connection_generation = 7
    mgr.ib_conn._portfolio_observations[CON] = {
        "price": 3.50, "observed_monotonic": time.monotonic(),
        "observed_utc": "2026-09-02T17:00:00+00:00", "generation": 7}

    await mgr.run_cycle(dry_run=False)

    mgr.ib_conn.fetch_quotes.assert_not_awaited()
    place.assert_called_once()
    assert place.call_args.kwargs["trigger_type"] == "stop"


@pytest.mark.asyncio
async def test_armed_cycle_rejects_repeated_but_unobserved_portfolio_cache(tmp_path):
    mgr, cfg = _mgr(tmp_path)
    cfg.arm = True
    place, alert = _wire(mgr, quotes={}, portfolio=[_PortRow(CON, 3.50)])

    await mgr.run_cycle(dry_run=False)
    await mgr.run_cycle(dry_run=False)

    place.assert_not_called()
    assert _blind_alert_rows(alert), "second consecutive stale cycle must page"


@pytest.mark.asyncio
async def test_armed_cycle_uses_only_fresh_generation_bound_quote_cache(tmp_path):
    mgr, cfg = _mgr(tmp_path)
    cfg.arm = True
    place, _alert = _wire(mgr, quotes={}, portfolio=[])
    _healthy(mgr.ib_conn)
    mgr.ib_conn._connection_generation = 4
    mgr._protective_quote_snapshot[CON] = {
        "price": 3.50, "bid": 3.40, "observed_monotonic": time.monotonic(),
        "observed_utc": "2026-09-02T17:00:00+00:00", "generation": 4}

    await mgr.run_cycle(dry_run=False)

    place.assert_called_once()

    mgr.ib_conn._connection_generation = 5
    assert mgr._fresh_protective_quotes() == {}


def test_uplink_loss_immediately_invalidates_young_price_rows_and_contract_cache():
    conn = IBConnection(host="127.0.0.1", port=4002, client_id=118)
    conn._connection_generation = 7
    conn._portfolio_observations[CON] = {
        "price": 3.5, "observed_monotonic": time.monotonic(), "generation": 7}
    conn._qualified_quote_contracts[CON] = MagicMock()

    conn._on_error(0, 1100, "connectivity lost")

    assert conn._connection_generation == 8
    assert conn.portfolio_mark_observations() == {}
    assert conn._qualified_quote_contracts == {}


def test_zero_position_event_revokes_the_previous_portfolio_mark():
    conn = IBConnection(host="127.0.0.1", port=4002, client_id=118)
    conn._price_events_accepted = True
    conn._on_update_portfolio(_PortRow(CON, 3.5, position=4))
    assert CON in conn.portfolio_mark_observations()

    conn._on_update_portfolio(_PortRow(CON, 3.5, position=0))

    assert CON not in conn.portfolio_mark_observations()


def test_late_portfolio_event_after_uplink_loss_cannot_repopulate_new_generation():
    conn = IBConnection(host="127.0.0.1", port=4002, client_id=118)
    conn._price_events_accepted = True
    conn._on_update_portfolio(_PortRow(CON, 3.5, position=4))
    conn._on_error(0, 1100, "connectivity lost")

    conn._on_update_portfolio(_PortRow(CON, 3.6, position=4))

    assert conn.portfolio_mark_observations() == {}


@pytest.mark.asyncio
async def test_quote_batch_crossing_connection_generation_is_discarded():
    conn = IBConnection(host="127.0.0.1", port=4002, client_id=118)
    conn.ib = MagicMock()
    _healthy(conn)
    contract = SimpleNamespace(conId=CON)
    conn._qualified_quote_contracts[CON] = contract
    ticker = SimpleNamespace(
        contract=contract, bid=3.4, ask=3.6, last=3.5, mark=3.5,
        modelGreeks=None, lastGreeks=None)

    async def _disconnect_mid_snapshot(*_contracts):
        conn._on_error(0, 1100, "connectivity lost")
        return [ticker]

    conn.ib.reqTickersAsync = AsyncMock(side_effect=_disconnect_mid_snapshot)

    assert await conn.fetch_quotes([CON]) == {}


@pytest.mark.asyncio
async def test_young_cache_on_dead_socket_is_never_used_by_armed_cycle(tmp_path):
    mgr, cfg = _mgr(tmp_path)
    cfg.arm = True
    place, alert = _wire(mgr, quotes={}, portfolio=[])
    mgr.ib_conn._connection_generation = 9
    mgr._protective_quote_snapshot[CON] = {
        "price": 3.50, "observed_monotonic": time.monotonic(), "generation": 9}

    await mgr.run_cycle(dry_run=False)
    await mgr.run_cycle(dry_run=False)

    place.assert_not_called()
    assert _blind_alert_rows(alert)


@pytest.mark.asyncio
async def test_atr_peak_arms_and_persists_in_the_same_manager_cycle(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr, _ = _mgr(tmp_path)
    peak = 5.65
    place, _alert = _wire(
        mgr, quotes={CON: {"price": peak, "iv": 0.30, "delta": 0.60}},
        portfolio=[_PortRow(CON, peak)])
    mgr._atr_levels_for = MagicMock(return_value={
        "activation_gain_pct": 12.9,
        "entry_per_share": 5.0,
        "atr": 1.0,
        "net_delta": 0.5,
        "stop_pct": 30.0,
    })

    await mgr.run_cycle(dry_run=False)

    assert mgr.state_manager.state.is_trail_armed(CON)
    assert mgr.state_manager.state.trail_peak_since_arm(CON) == pytest.approx(peak)
    assert mgr.state_manager.state.trail_confirmation_for(CON)["armed_at"] is not None
    place.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(("gain_pct", "should_arm"), [(19.99, False), (22.31, True)])
async def test_immediate_arm_uses_the_earliest_enabled_trail_boundary(
        tmp_path, gain_pct, should_arm):
    """Public API contract; production-derived narrative omitted."""
    mgr, cfg = _mgr(tmp_path)
    cfg.rules.trailing = TrailingConfig(
        enabled=True, activation_gain_pct=20.0, giveback_fraction=0.4)
    cfg.rules.auto_trail.activation_gain_pct = 25.0
    peak = 5.0 * (1.0 + gain_pct / 100.0)
    place, _alert = _wire(
        mgr, quotes={CON: {"price": peak, "iv": 0.30, "delta": 0.60}},
        portfolio=[_PortRow(CON, peak)])
    mgr._atr_levels_for = MagicMock(return_value={
        "activation_gain_pct": 22.5987,
        "entry_per_share": 5.0,
        "atr": 1.0,
        "net_delta": 0.5,
        "stop_pct": 30.0,
    })

    await mgr.run_cycle(dry_run=False)

    assert mgr.state_manager.state.is_trail_armed(CON) is should_arm
    if should_arm:
        assert mgr.state_manager.state.trail_peak_since_arm(CON) == pytest.approx(peak)
    place.assert_not_called()


@pytest.mark.asyncio
async def test_unusable_quote_price_still_stops_on_the_broker_mark(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr, _ = _mgr(tmp_path)
    place, alert = _wire(mgr, quotes={CON: {"price": float("nan"), "bid": float("nan")}},
                         portfolio=[_PortRow(CON, 3.50)])
    await mgr.run_cycle(dry_run=False)

    place.assert_called_once()
    assert place.call_args.kwargs["trigger_type"] == "stop"
    assert _blind_alert_rows(alert) == []


@pytest.mark.asyncio
async def test_the_broker_mark_still_wins_when_both_exist(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr, _ = _mgr(tmp_path)
    place, _alert = _wire(mgr, quotes={CON: {"price": 6.50, "bid": 6.40}},
                          portfolio=[_PortRow(CON, 3.50)])
    await mgr.run_cycle(dry_run=False)

    place.assert_called_once()
    assert place.call_args.kwargs["trigger_type"] == "stop"



@pytest.mark.asyncio
async def test_no_quote_and_no_mark_places_nothing_and_alerts(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr, _ = _mgr(tmp_path)
    place, alert = _wire(mgr, quotes={}, portfolio=[])
    await mgr.run_cycle(dry_run=False)

    place.assert_not_called()
    rows = _blind_alert_rows(alert)
    assert [(s, c) for s, c, _ in rows] == [("AAPL", CON)]
    assert "no streaming quote and no broker mark" in rows[0][2]


@pytest.mark.asyncio
async def test_nan_quote_and_no_mark_is_blind_not_a_silent_pass(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr, _ = _mgr(tmp_path)
    place, alert = _wire(mgr, quotes={CON: {"price": float("nan")}}, portfolio=[])
    await mgr.run_cycle(dry_run=False)

    place.assert_not_called()
    rows = _blind_alert_rows(alert)
    assert [(s, c) for s, c, _ in rows] == [("AAPL", CON)]
    assert "no usable price" in rows[0][2]



@pytest.mark.asyncio
async def test_a_zero_quote_with_no_mark_still_closes_the_position(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr, _ = _mgr(tmp_path)
    place, alert = _wire(mgr, quotes={CON: {"price": 0.0}}, portfolio=[_PortRow(CON, 0.0)])
    await mgr.run_cycle(dry_run=False)

    place.assert_called_once()
    assert place.call_args.kwargs["trigger_type"] == "stop"
    assert _blind_alert_rows(alert) == []



@pytest.mark.asyncio
async def test_spread_short_leg_priced_by_quote_when_the_long_leg_has_a_mark(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr, _ = _mgr(tmp_path, journal=SPREAD_JOURNAL)
    place, alert = _wire(mgr, quotes={SCID: {"price": 3.80, "bid": 3.70}},
                         portfolio=[_PortRow(CON, 5.00)])
    await mgr.run_cycle(dry_run=False)

    place.assert_called_once()
    kw = place.call_args.kwargs
    assert kw["spread"]["short_con_id"] == SCID
    assert kw["trigger_type"] == "stop"
    assert _blind_alert_rows(alert) == []


@pytest.mark.asyncio
async def test_unpriceable_short_leg_makes_the_spread_blind_not_leg_priced(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr, _ = _mgr(tmp_path, journal=SPREAD_JOURNAL)
    place, alert = _wire(mgr, quotes={SCID: {"price": float("nan")}},
                         portfolio=[_PortRow(CON, 5.00)])
    await mgr.run_cycle(dry_run=False)

    place.assert_not_called()
    rows = _blind_alert_rows(alert)
    assert [(s, c) for s, c, _ in rows] == [("AAPL", CON)]
    assert "short leg" in rows[0][2]


@pytest.mark.asyncio
async def test_armed_spread_short_leg_miss_is_consecutive_and_pages_on_second_cycle(tmp_path):
    mgr, cfg = _mgr(tmp_path, journal=SPREAD_JOURNAL)
    cfg.arm = True
    place, alert = _wire(mgr, quotes={}, portfolio=[])
    _healthy(mgr.ib_conn)
    mgr.ib_conn._connection_generation = 12
    mgr.ib_conn._portfolio_observations[CON] = {
        "price": 5.0, "observed_monotonic": time.monotonic(),
        "observed_utc": "2026-09-02T17:00:00+00:00", "generation": 12}

    await mgr.run_cycle(dry_run=False)
    assert _blind_alert_rows(alert) == []
    await mgr.run_cycle(dry_run=False)

    place.assert_not_called()
    rows = _blind_alert_rows(alert)
    assert len(rows) == 1 and "short leg" in rows[0][2]
    assert "2 consecutive stale cycles" in rows[0][2]


@pytest.mark.asyncio
async def test_after_hours_blind_cycles_log_without_order_or_page_then_rth_pages(
        tmp_path, monkeypatch, capsys):
    """Public API contract; production-derived narrative omitted."""
    closed = datetime(2026, 9, 9, 4, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("exitmgr.trader._market_open", lambda: _market_open_at(closed))
    mgr, cfg = _mgr(tmp_path)
    cfg.arm = True
    place, alert = _wire(mgr, quotes={}, portfolio=[])
    _healthy(mgr.ib_conn)



    await mgr.run_cycle(dry_run=False)
    await mgr.run_cycle(dry_run=False)

    place.assert_not_called()
    alert.assert_not_called()
    mgr.ib_conn.fetch_quotes.assert_not_awaited()
    output = capsys.readouterr().out
    assert "UNPRICEABLE this cycle" in output
    assert "[CYCLE] POSITIONS UNPRICEABLE" in output
    assert "('AAPL', 4100)" in output

    opened = datetime(2026, 9, 9, 17, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("exitmgr.trader._market_open", lambda: _market_open_at(opened))
    await mgr.run_cycle(dry_run=False)

    place.assert_not_called()
    rows = _blind_alert_rows(alert)
    assert [(symbol, con_id) for symbol, con_id, _why in rows] == [("AAPL", CON)]
    assert "3 consecutive stale cycles" in rows[0][2]
