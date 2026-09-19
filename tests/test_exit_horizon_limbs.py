"""Public API contract; production-derived narrative omitted."""
import asyncio
import json
import math
import re
import types
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from exitmgr import atr_cache
from exitmgr.atr_levels import horizon_k, net_structure_delta
from exitmgr.config import Config
from exitmgr.connection import IBConnection
from exitmgr.manager import ExitManager
from exitmgr.order import OrderResult
from exitmgr.rules import (evaluate_position, evaluate_short_stop,
                           evaluate_short_trailing_stop, evaluate_stop,
                           evaluate_trailing_stop)
from exitmgr.state import ATR_STOP_FIELDS, State, atr_stop_basis







SPOT = STRIKE = 100.0
IV = 0.30
ATR = 0.52
ED, QTY = 500.0, 1
TIME_STOP_DAYS = 15


_FIXTURE_NOW = datetime.now(timezone.utc)


def _days_ago(n):

    return (_FIXTURE_NOW - timedelta(days=float(n))).isoformat()


def _levels_mgr(tmp_path, monkeypatch, name="lv", *, atr=ATR, spot=SPOT):
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    cfg = Config()
    cfg.dry_run = True
    cfg.loop_mode = False
    cfg.manage_positions = False
    cfg.journal.path = str(d / "trades.log")
    cfg.state.path = str(d / "state.json")
    cfg.kill_switch.path = str(d / "KILL")



    cfg.rules.stop_pct = 30.0
    cfg.rules.time_stop_days = TIME_STOP_DAYS
    mgr = ExitManager(cfg)
    monkeypatch.setattr(atr_cache, "read",
                        lambda symbol, *a, **k: {"atr": float(atr), "spot": float(spot),
                                                 "asof": "2026-08-21"})
    return mgr


def _levels(mgr, *, dte, hold=None, ts=None, con_id=1, symbol="SYMK",
            iv=IV, entry_iv=None, strike=STRIKE, require=True):
    """Public API contract; production-derived narrative omitted."""
    je = {"symbol": symbol, "strike": strike, "right": "C", "debit": ED, "quantity": QTY}
    if hold is not None:
        je["intended_hold_days"] = hold
    if ts is not None:
        je["ts"] = ts
    if entry_iv is not None:
        je["entry_iv"] = entry_iv
    mgr._journal_entries[con_id] = je
    lv = mgr._atr_levels_for(con_id, symbol, ED, QTY, iv, dte)
    if not require:
        return lv


    assert lv is not None, f"fixture produced no ATR levels (dte={dte} hold={hold} ts={ts})"
    return lv




def test_horizon_k_is_k_times_the_square_root_of_the_hold():
    """Public API contract; production-derived narrative omitted."""
    assert horizon_k(0.5, 100.0) == pytest.approx(5.0)
    assert horizon_k(2.0, 9.0) == pytest.approx(6.0)
    assert horizon_k(0.5, 38.0) == pytest.approx(0.5 * math.sqrt(38.0))


def test_horizon_k_is_monotonic_in_hold_days():
    """Public API contract; production-derived narrative omitted."""
    vals = [horizon_k(0.5, d) for d in (1, 2, 5, 12, 38, 91, 300, 800)]
    assert all(v is not None for v in vals)
    assert all(b > a for a, b in zip(vals, vals[1:])), vals


def test_horizon_k_reads_differently_from_the_legacy_per_day_constant_in_both_directions():
    """Public API contract; production-derived narrative omitted."""
    assert horizon_k(0.5, 38.0) > 2.0
    assert horizon_k(0.5, 5.0) < 2.0


def test_horizon_k_floors_at_min_days():
    """Public API contract; production-derived narrative omitted."""
    assert horizon_k(0.5, 1.0) == pytest.approx(0.5)
    assert horizon_k(0.5, 0.25) == pytest.approx(0.5)
    assert horizon_k(0.5, 0.0) == pytest.approx(0.5)
    assert horizon_k(1.0, 2.0, min_days=9.0) == pytest.approx(3.0)


def test_horizon_k_declines_a_negative_hold_rather_than_flooring_it():
    """Public API contract; production-derived narrative omitted."""
    assert horizon_k(0.5, -50.0) is None
    assert horizon_k(0.5, -0.001) is None
    assert horizon_k(0.5, 0.0) == pytest.approx(0.5)


@pytest.mark.parametrize("bad_k", [None, 0, 0.0, -1.0, float("nan"), "x", object()])
def test_horizon_k_declines_an_unusable_base_instead_of_inventing_one(bad_k):
    assert horizon_k(bad_k, 30.0) is None


@pytest.mark.parametrize("bad_d", [None, float("nan"), "x", object()])
def test_horizon_k_declines_an_unusable_hold_instead_of_inventing_one(bad_d):
    assert horizon_k(0.5, bad_d) is None




def test_a_leap_gets_a_materially_wider_stop_than_a_swing(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    m = _levels_mgr(tmp_path, monkeypatch)
    leap = _levels(m, dte=300)
    swing = _levels(m, dte=91)


    assert leap["hold_days"] == pytest.approx(38.0)
    assert swing["hold_days"] == pytest.approx(12.0)
    assert leap["k_eff"] > swing["k_eff"]
    assert leap["stop_pct"] > swing["stop_pct"] * 1.5, (leap["stop_pct"], swing["stop_pct"])


def test_the_horizon_stop_stays_strictly_inside_the_cap_and_the_floor(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    m = _levels_mgr(tmp_path, monkeypatch)
    cap = float(m.config.rules.stop_pct)
    floor = float(m.config.rules.atr_levels.min_stop_pct)
    for dte in (60, 91, 150, 300, 500):
        pct = _levels(m, dte=dte)["stop_pct"]
        assert floor < pct < cap, (dte, pct)


def test_the_cap_binds_so_a_long_horizon_can_never_widen_a_stop(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    m = _levels_mgr(tmp_path, monkeypatch, "wide", atr=50.0)
    assert _levels(m, dte=300)["stop_pct"] == pytest.approx(float(m.config.rules.stop_pct))


def test_the_floor_binds_so_a_low_delta_cannot_produce_a_hair_trigger(tmp_path, monkeypatch):
    m = _levels_mgr(tmp_path, monkeypatch, "tight", atr=0.001)
    lv = _levels(m, dte=300)
    assert lv["stop_pct"] == pytest.approx(
        float(m.config.rules.atr_levels.min_stop_pct))


def test_horizon_scaling_off_falls_back_to_the_legacy_per_day_multiple(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    m = _levels_mgr(tmp_path, monkeypatch)
    cfg = m.config.rules.atr_levels
    on = _levels(m, dte=300)
    cfg.horizon_scaling = False
    off = _levels(m, dte=300)
    assert off["k_eff"] == pytest.approx(float(cfg.k_stop))
    assert on["k_eff"] != pytest.approx(off["k_eff"])
    assert on["hold_days"] == off["hold_days"], "the flag must move k, not the horizon"




def test_lapse_leaves_the_horizon_untouched_inside_the_intended_window(tmp_path, monkeypatch):
    m = _levels_mgr(tmp_path, monkeypatch)
    lv = _levels(m, dte=300, hold=20.0, ts=_days_ago(5))
    assert lv["hold_days"] == pytest.approx(20.0)
    assert lv["held_days"] == pytest.approx(5.0, abs=0.01)
    assert lv["lapse_over_days"] is None, "nothing has lapsed yet"


def test_lapse_halves_the_horizon_every_half_life_past_the_window(tmp_path, monkeypatch):
    m = _levels_mgr(tmp_path, monkeypatch)
    SYMO = float(m.config.rules.atr_levels.lapse_halflife_days)
    assert SYMO == 10.0, "fixture assumes the shipped 10-day half-life"
    at_window = _levels(m, dte=300, hold=20.0, ts=_days_ago(20))
    one_SYMO = _levels(m, dte=300, hold=20.0, ts=_days_ago(20 + SYMO))
    two_SYMO = _levels(m, dte=300, hold=20.0, ts=_days_ago(20 + 2 * SYMO))
    assert at_window["hold_days"] == pytest.approx(20.0, rel=1e-3)
    assert one_SYMO["hold_days"] == pytest.approx(10.0, rel=1e-3)
    assert two_SYMO["hold_days"] == pytest.approx(5.0, rel=1e-3)
    assert one_SYMO["lapse_over_days"] == pytest.approx(SYMO, abs=0.01)


def test_lapse_only_ever_tightens_never_loosens(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    m = _levels_mgr(tmp_path, monkeypatch)
    holds, stops = [], []
    for over in (0, 3, 7, 12, 25, 44):
        lv = _levels(m, dte=300, hold=20.0, ts=_days_ago(20 + over))
        holds.append(lv["hold_days"])
        stops.append(lv["stop_pct"])
    assert all(b <= a + 1e-9 for a, b in zip(holds, holds[1:])), holds
    assert all(b <= a + 1e-9 for a, b in zip(stops, stops[1:])), stops
    assert max(holds) <= 20.0 + 1e-9


def test_lapse_is_floored_at_lapse_floor_days(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    m = _levels_mgr(tmp_path, monkeypatch)
    cfg = m.config.rules.atr_levels
    lv = _levels(m, dte=300, hold=20.0, ts=_days_ago(20 + 10 * cfg.lapse_halflife_days))
    assert lv["hold_days"] == pytest.approx(float(cfg.lapse_floor_days))
    assert lv["stop_pct"] == pytest.approx(float(cfg.min_stop_pct))


def test_lapse_tighten_false_is_a_genuine_no_op(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    m = _levels_mgr(tmp_path, monkeypatch)
    ts = _days_ago(60)
    on = _levels(m, dte=300, hold=20.0, ts=ts)
    m.config.rules.atr_levels.lapse_tighten = False
    off = _levels(m, dte=300, hold=20.0, ts=ts)



    assert off["hold_days"] == pytest.approx(20.0)
    assert off["lapse_over_days"] is None

    assert on["hold_days"] == pytest.approx(1.25, rel=1e-3)
    assert on["lapse_over_days"] == pytest.approx(40.0, abs=0.01)
    assert on["hold_days"] < off["hold_days"]
    assert on["k_eff"] < off["k_eff"]
    assert on["stop_pct"] < off["stop_pct"], "lapse_tighten=True did not move the stop"

    assert off["held_days"] == pytest.approx(60.0, abs=0.01)


def test_lapse_needs_a_journal_timestamp_and_degrades_silently_without_one(tmp_path,
                                                                          monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    m = _levels_mgr(tmp_path, monkeypatch)
    lv = _levels(m, dte=300, hold=20.0)
    assert lv["held_days"] is None
    assert lv["lapse_over_days"] is None
    assert lv["hold_days"] == pytest.approx(20.0)




def test_the_leash_shortens_before_the_wind_down_tag_ever_appears(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    m = _levels_mgr(tmp_path, monkeypatch)
    far = _levels(m, dte=100, hold=38.0)
    biting = _levels(m, dte=48, hold=38.0)
    assert far["hold_days"] == pytest.approx(38.0)
    assert far["wind_down_room"] is None
    assert biting["hold_days"] == pytest.approx(48 - TIME_STOP_DAYS)
    assert biting["wind_down_room"] is None, "the TAG threshold is not crossed yet"
    assert biting["stop_pct"] < far["stop_pct"], "the leash did not actually shorten"


def test_wind_down_start_dte_only_tags_the_log_it_does_not_gate_the_arithmetic(tmp_path,
                                                                              monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    m = _levels_mgr(tmp_path, monkeypatch)
    untagged = _levels(m, dte=40, hold=38.0)


    m.config.rules.atr_levels.wind_down_start_dte = 1000
    tagged = _levels(m, dte=40, hold=38.0)
    assert untagged["wind_down_room"] is None
    assert tagged["wind_down_room"] == pytest.approx(40 - TIME_STOP_DAYS)
    assert tagged["hold_days"] == pytest.approx(untagged["hold_days"])
    assert tagged["k_eff"] == pytest.approx(untagged["k_eff"])
    assert tagged["stop_pct"] == pytest.approx(untagged["stop_pct"])


def test_the_horizon_never_goes_below_half_a_day_at_the_hard_close(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    m = _levels_mgr(tmp_path, monkeypatch)
    for dte in (TIME_STOP_DAYS, TIME_STOP_DAYS - 5, 1):
        lv = _levels(m, dte=dte, hold=38.0)
        assert lv["hold_days"] == pytest.approx(0.5)

        assert lv["k_eff"] == pytest.approx(
            float(m.config.rules.atr_levels.k_stop_horizon))


def test_the_stop_never_cliffs_between_adjacent_dte(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    m = _levels_mgr(tmp_path, monkeypatch)
    series = {dte: _levels(m, dte=dte, hold=38.0)["stop_pct"] for dte in range(16, 141)}
    for dte in range(17, 141):
        lo, hi = sorted((series[dte - 1], series[dte]))
        assert hi / lo <= 1.12, (
            f"stop discontinuity at DTE {dte - 1}->{dte}: "
            f"{series[dte - 1]:.2f}% -> {series[dte]:.2f}%")


    assert series[140] > series[60] > series[30]


def _hold_after_wind_down(dte, intended, *, time_stop_days=TIME_STOP_DAYS,
                          start_dte=20.0, gated):
    """Public API contract; production-derived narrative omitted."""
    room = max(0.5, float(dte) - float(time_stop_days))
    if gated and float(dte) > float(start_dte):
        return float(intended)
    return min(float(intended), room)


def test_the_continuity_bound_has_teeth_the_gated_variant_still_cliffs():
    """Public API contract; production-derived narrative omitted."""
    net = net_structure_delta(SPOT, STRIKE, None, 20, IV)

    def stop_for(dte, gated):
        hold = _hold_after_wind_down(dte, 38.0, gated=gated)
        k = horizon_k(0.5, hold)
        n = net_structure_delta(SPOT, STRIKE, None, dte, IV)
        return max(8.0, min(k * ATR * n / (ED / (100.0 * QTY)) * 100.0, 30.0))

    assert net is not None
    shipped = stop_for(21, False), stop_for(20, False)
    gated = stop_for(21, True), stop_for(20, True)
    assert max(shipped) / min(shipped) <= 1.12, shipped
    assert max(gated) / min(gated) > 2.0, gated





def _expiry(days):
    return (datetime.now(timezone.utc).date() + timedelta(days=days)).strftime("%Y%m%d")


FAR = _expiry(120)


def _raw(con_id, right, qty, *, symbol="RKLB", avg_cost=4.10, expiry=FAR, strike=20.0):
    """Public API contract; production-derived narrative omitted."""
    p = types.SimpleNamespace()
    p.contract = types.SimpleNamespace(
        conId=con_id, symbol=symbol, right=right, secType="OPT", strike=strike,
        lastTradeDateOrContractMonth=expiry, multiplier="100")
    p.position = qty
    p.avgCost = avg_cost * 100
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




def _long_journal(**over):
    je = {"ts": _days_ago(60), "contract_id": 101, "symbol": "RKLB", "right": "C",
          "strike": 20.0, "expiry": FAR, "quantity": 2, "debit": 820.0, "conviction": 6,
          "decision_id": "dec-backstop-1", "intended_hold_days": 5}
    je.update(over)
    return je



def _credit_journal(**over):
    je = {"ts": _days_ago(60), "contract_id": 105, "symbol": "SPY", "right": "P",
          "strike": 500.0, "expiry": FAR, "side": "credit", "structure": "cash secured put",
          "action": "SELL", "quantity": -1, "contracts": 1, "collateral_usd": 50000.0,
          "net_credit_usd": 175.0, "max_loss_usd": 49825.0, "debit": 49825.0,
          "conviction": 7, "stop_pct": 30.0, "decision_id": "dec-backstop-csp",
          "intended_hold_days": 5}
    je.update(over)
    return je


def _cycle_mgr(tmp_path, monkeypatch, name, journal_lines, *, rows, marks, quotes,
               atr=None, spot=20.0):
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    cfg = Config()
    cfg.dry_run = False
    cfg.loop_mode = False
    cfg.manage_positions = False
    cfg.journal.path = str(d / "trades.log")
    cfg.state.path = str(d / "state.json")
    cfg.kill_switch.path = str(d / "KILL")
    (d / "trades.log").write_text("".join(json.dumps(x) + "\n" for x in journal_lines))
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


    mgr._maybe_record_session_close = lambda *a, **k: None






    monkeypatch.setattr(
        atr_cache, "read",
        (lambda symbol, *a, **k: None) if atr is None else
        (lambda symbol, *a, **k: {"atr": float(atr), "spot": float(spot),
                                  "asof": "2026-08-21"}))
    return mgr


def _capture_closes(mgr):
    calls = []

    async def _stub(**kw):
        calls.append(kw)
        return OrderResult(success=True, message="", con_id=kw["con_id"], order_id=4242,
                           trade=None)

    mgr.order_manager.place_close_order = _stub
    return calls


def _run(mgr):
    asyncio.run(mgr.run_cycle(dry_run=False))


def _assert_evaluated(mgr, con_id):
    """Public API contract; production-derived narrative omitted."""
    assert str(con_id) in mgr.state_manager.state.peak_prices, (
        f"con_id={con_id} was never evaluated -- this assertion would pass vacuously")




def test_the_backstop_reclaims_dead_money_past_the_multiple(tmp_path, monkeypatch, capsys):
    """Public API contract; production-derived narrative omitted."""
    mgr = _cycle_mgr(tmp_path, monkeypatch, "bs_fire", [_long_journal()],
                     rows=[_raw(101, "C", 2)], marks=[_mark(101, 3.60)],
                     quotes={101: {"price": 3.60, "bid": 3.50, "ask": 3.70}})
    calls = _capture_closes(mgr)
    _run(mgr)
    assert len(calls) == 1, "dead money past 4x its window was never reclaimed"
    assert calls[0]["con_id"] == 101
    assert calls[0]["trigger_type"] == "time_stop"
    out = capsys.readouterr().out
    assert "[HOLD-BACKSTOP]" in out
    assert "no other rule fired" in out, "the backstop must announce it is the last resort"


def test_the_backstop_does_not_fire_inside_the_multiple(tmp_path, monkeypatch, capsys):
    """Public API contract; production-derived narrative omitted."""
    mgr = _cycle_mgr(tmp_path, monkeypatch, "bs_early",
                     [_long_journal(ts=_days_ago(10))],
                     rows=[_raw(101, "C", 2)], marks=[_mark(101, 3.60)],
                     quotes={101: {"price": 3.60, "bid": 3.50, "ask": 3.70}})
    calls = _capture_closes(mgr)
    _run(mgr)
    _assert_evaluated(mgr, 101)
    assert calls == []
    assert "[HOLD-BACKSTOP]" not in capsys.readouterr().out


def test_the_backstop_never_force_realises_a_gain(tmp_path, monkeypatch, capsys):
    """Public API contract; production-derived narrative omitted."""
    mgr = _cycle_mgr(tmp_path, monkeypatch, "bs_profit", [_long_journal()],
                     rows=[_raw(101, "C", 2)], marks=[_mark(101, 4.30)],
                     quotes={101: {"price": 4.30, "bid": 4.20, "ask": 4.40}})
    calls = _capture_closes(mgr)
    _run(mgr)
    _assert_evaluated(mgr, 101)
    assert calls == [], "the backstop banked a winner"
    out = capsys.readouterr().out
    assert "IN PROFIT" in out and "not reclaiming" in out
    assert mgr.state_manager.state.is_trail_armed(101) is False, (
        "fixture invalid: the profit exemption must be tested on an UNARMED position, "
        "or the armed exemption is what did the work")


def test_the_backstop_exempts_an_armed_trail(tmp_path, monkeypatch, capsys):
    """Public API contract; production-derived narrative omitted."""
    from exitmgr.state import State
    monkeypatch.setattr(State, "is_trail_armed", lambda self, con_id: True)
    mgr = _cycle_mgr(tmp_path, monkeypatch, "bs_armed", [_long_journal()],
                     rows=[_raw(101, "C", 2)], marks=[_mark(101, 3.60)],
                     quotes={101: {"price": 3.60, "bid": 3.50, "ask": 3.70}})
    calls = _capture_closes(mgr)
    _run(mgr)
    _assert_evaluated(mgr, 101)
    out = capsys.readouterr().out
    assert "[HOLD-BACKSTOP]" not in out, "the backstop evaluated an armed position at all"
    assert all("Hold backstop" not in str(c.get("exit_context", {}).get("reason", ""))
               for c in calls)
    assert calls == []


def test_a_lapsed_short_is_governed_by_the_short_family_not_the_backstop(tmp_path,
                                                                         monkeypatch, capsys):
    """Public API contract; production-derived narrative omitted."""
    mgr = _cycle_mgr(tmp_path, monkeypatch, "bs_credit", [_credit_journal()],
                     rows=[_raw(105, "P", -1, symbol="SPY", strike=500.0, avg_cost=1.75)],
                     marks=[_mark(105, 1.80)],
                     quotes={105: {"price": 1.80, "bid": 1.70, "ask": 1.90}})
    calls = _capture_closes(mgr)
    _run(mgr)
    out = capsys.readouterr().out

    assert "[EVAL-SHORT] con_id=105" in out
    assert calls == [], "a lapsed CSP was force-closed by a debit-space rule"
    assert "[HOLD-BACKSTOP]" not in out


def test_the_backstop_declines_a_credit_row_that_does_reach_the_long_loop(tmp_path,
                                                                         monkeypatch, capsys):
    """Public API contract; production-derived narrative omitted."""
    mgr = _cycle_mgr(tmp_path, monkeypatch, "bs_credit_long",
                     [_long_journal(side="credit")],
                     rows=[_raw(101, "C", 2)], marks=[_mark(101, 3.60)],
                     quotes={101: {"price": 3.60, "bid": 3.50, "ask": 3.70}})
    calls = _capture_closes(mgr)
    _run(mgr)
    _assert_evaluated(mgr, 101)
    assert calls == [], "the backstop reclaimed a row journalled as credit"
    assert "[HOLD-BACKSTOP]" not in capsys.readouterr().out


def test_the_backstop_never_preempts_a_rule_that_would_have_fired(tmp_path, monkeypatch,
                                                                  capsys):
    """Public API contract; production-derived narrative omitted."""
    mgr = _cycle_mgr(tmp_path, monkeypatch, "bs_preempt", [_long_journal()],
                     rows=[_raw(101, "C", 2)], marks=[_mark(101, 2.50)],
                     quotes={101: {"price": 2.50, "bid": 2.40, "ask": 2.60}})
    calls = _capture_closes(mgr)
    _run(mgr)
    assert len(calls) == 1
    assert calls[0]["trigger_type"] == "stop", (
        "the backstop preempted the protective stop again")
    out = capsys.readouterr().out


    assert "no other rule fired" not in out


def test_hold_backstop_enabled_false_is_a_genuine_no_op(tmp_path, monkeypatch, capsys):
    """Public API contract; production-derived narrative omitted."""
    mgr = _cycle_mgr(tmp_path, monkeypatch, "bs_off", [_long_journal()],
                     rows=[_raw(101, "C", 2)], marks=[_mark(101, 3.60)],
                     quotes={101: {"price": 3.60, "bid": 3.50, "ask": 3.70}})
    mgr.config.rules.atr_levels.hold_backstop_enabled = False
    calls = _capture_closes(mgr)
    _run(mgr)
    _assert_evaluated(mgr, 101)
    assert calls == []
    assert "[HOLD-BACKSTOP]" not in capsys.readouterr().out


def test_the_backstop_multiple_is_the_threshold_that_decides(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    short_thesis = _cycle_mgr(tmp_path, monkeypatch, "bs_mult_a",
                              [_long_journal(intended_hold_days=5)],
                              rows=[_raw(101, "C", 2)], marks=[_mark(101, 3.60)],
                              quotes={101: {"price": 3.60, "bid": 3.50, "ask": 3.70}})
    a = _capture_closes(short_thesis)
    _run(short_thesis)

    long_thesis = _cycle_mgr(tmp_path, monkeypatch, "bs_mult_b",
                             [_long_journal(intended_hold_days=30)],
                             rows=[_raw(101, "C", 2)], marks=[_mark(101, 3.60)],
                             quotes={101: {"price": 3.60, "bid": 3.50, "ask": 3.70}})
    b = _capture_closes(long_thesis)
    _run(long_thesis)

    assert len(a) == 1 and a[0]["trigger_type"] == "time_stop"
    _assert_evaluated(long_thesis, 101)
    assert b == []









IV_STRIKE = 110.0
IV_ATR = 1.00
LIVE_IV, ENTRY_IV = 0.30, 0.45
IV_HOLD, IV_DTE = 38.0, 120


def _iv_levels(tmp_path, monkeypatch, name, *, iv, entry_iv=None, require=True):
    m = _levels_mgr(tmp_path, monkeypatch, name, atr=IV_ATR, spot=SPOT)
    return _levels(m, dte=IV_DTE, hold=IV_HOLD, iv=iv, entry_iv=entry_iv,
                   strike=IV_STRIKE, require=require)


def test_a_quote_with_no_iv_falls_back_to_the_journalled_entry_iv(tmp_path, monkeypatch,
                                                                  capsys):
    """Public API contract; production-derived narrative omitted."""
    blind = _iv_levels(tmp_path, monkeypatch, "iv_blind", iv=None, require=False)
    assert blind is None, "a vol-less quote with no entry IV must NOT invent an ATR stop"
    assert "net delta unpriceable" in capsys.readouterr().out

    saved = _iv_levels(tmp_path, monkeypatch, "iv_saved", iv=None, entry_iv=ENTRY_IV)
    out = capsys.readouterr().out
    assert f"live IV unavailable, using entry IV {ENTRY_IV:.4f}" in out, (
        "the fallback must SAY it used a stale vol -- a silently degraded stop is the bug")


    assert 8.0 < saved["stop_pct"] < 30.0
    assert saved["net_delta"] > 0


def test_the_fallback_consumes_the_entry_iv_and_nothing_else(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    live_at_entry_iv = _iv_levels(tmp_path, monkeypatch, "iv_a", iv=ENTRY_IV)
    live_at_other_iv = _iv_levels(tmp_path, monkeypatch, "iv_b", iv=LIVE_IV)
    fell_back = _iv_levels(tmp_path, monkeypatch, "iv_c", iv=None, entry_iv=ENTRY_IV)

    assert fell_back["net_delta"] == pytest.approx(live_at_entry_iv["net_delta"])
    assert fell_back["stop_pct"] == pytest.approx(live_at_entry_iv["stop_pct"])


    assert live_at_other_iv["stop_pct"] != pytest.approx(live_at_entry_iv["stop_pct"])
    assert abs(fell_back["stop_pct"] - live_at_other_iv["stop_pct"]) > 3.0


@pytest.mark.parametrize("useless", [None, 0.0, 0, "", "not-a-number", -0.42])
def test_every_shape_of_unusable_live_iv_reaches_the_fallback(tmp_path, monkeypatch,
                                                              useless):
    """Public API contract; production-derived narrative omitted."""
    lv = _iv_levels(tmp_path, monkeypatch, f"iv_bad_{abs(hash(str(useless))) % 9999}",
                    iv=useless, entry_iv=ENTRY_IV)
    ref = _iv_levels(tmp_path, monkeypatch, "iv_ref", iv=ENTRY_IV)
    assert lv["stop_pct"] == pytest.approx(ref["stop_pct"])


def test_a_nan_live_iv_should_reach_the_fallback_too(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    lv = _iv_levels(tmp_path, monkeypatch, "iv_nan", iv=float("nan"), entry_iv=ENTRY_IV,
                    require=False)
    ref = _iv_levels(tmp_path, monkeypatch, "iv_nan_ref", iv=ENTRY_IV)
    assert lv is not None, "a NaN live IV silently discarded the journalled entry IV"
    assert lv["stop_pct"] == pytest.approx(ref["stop_pct"])


@pytest.mark.parametrize("bad_entry", [0.0, -1.0, "junk"])
def test_the_fallback_never_invents_a_vol_the_journal_does_not_have(tmp_path, monkeypatch,
                                                                    bad_entry):
    """Public API contract; production-derived narrative omitted."""
    lv = _iv_levels(tmp_path, monkeypatch, "iv_noentry", iv=None, entry_iv=bad_entry,
                    require=False)
    assert lv is None


def _iv_effective(live, entry, *, fallback):
    """Public API contract; production-derived narrative omitted."""
    try:
        v = float(live or 0.0)
    except (TypeError, ValueError):
        v = 0.0
    if v <= 0 and fallback:
        try:
            v = float(entry or 0.0)
        except (TypeError, ValueError):
            v = 0.0
    return v


def test_the_iv_fallback_assertion_has_teeth_without_it_the_structure_is_unpriceable():
    """Public API contract; production-derived narrative omitted."""
    assert _iv_effective(None, ENTRY_IV, fallback=True) == pytest.approx(ENTRY_IV)
    assert _iv_effective(None, ENTRY_IV, fallback=False) == 0.0
    with_fb = net_structure_delta(SPOT, IV_STRIKE, None, IV_DTE,
                                  _iv_effective(None, ENTRY_IV, fallback=True))
    without_fb = net_structure_delta(SPOT, IV_STRIKE, None, IV_DTE,
                                     _iv_effective(None, ENTRY_IV, fallback=False))
    assert with_fb is not None and with_fb > 0
    assert without_fb is None












ATR_WIDE, ATR_TIGHT = 0.67, 0.40
EPS_LONG = 4.10
MARK_PAST_THE_FLOOR = 3.28
MARK_WITH_ROOM = 3.90


def _atr_cycle(tmp_path, monkeypatch, name, *, price, atr, stop_pct=None,
               decision_id=None):
    """Public API contract; production-derived narrative omitted."""
    je = _long_journal(ts=_days_ago(3), intended_hold_days=30)
    if stop_pct is not None:
        je["stop_pct"] = stop_pct
    if decision_id is not None:
        je["decision_id"] = decision_id
    mgr = _cycle_mgr(tmp_path, monkeypatch, name, [je],
                     rows=[_raw(101, "C", 2)], marks=[_mark(101, price)],
                     quotes={101: {"price": price, "bid": round(price - 0.05, 2),
                                   "ask": round(price + 0.05, 2), "iv": LIVE_IV}},
                     atr=atr, spot=20.0)


    mgr.config.rules.stop_pct = 30.0
    mgr.config.rules.time_stop_days = TIME_STOP_DAYS
    return mgr


def _tightened_to(out):
    """Public API contract; production-derived narrative omitted."""
    m = re.search(r"stop (\d+\.\d)% -> (\d+\.\d)%", out)
    return (float(m.group(1)), float(m.group(2))) if m else None


def test_a_wider_atr_answer_never_loosens_the_stop_a_position_already_carries(
        tmp_path, monkeypatch, capsys):
    """Public API contract; production-derived narrative omitted."""
    mgr = _atr_cycle(tmp_path, monkeypatch, "atr_wider",
                     price=MARK_PAST_THE_FLOOR, atr=ATR_WIDE, stop_pct=12.0)
    calls = _capture_closes(mgr)
    _run(mgr)
    out = capsys.readouterr().out

    assert "is not tighter than 12.0% -- keeping it" in out, (
        f"the widen guard did not run at all:\n{out}")
    assert _tightened_to(out) is None, "a WIDER ATR answer was adopted -- stops must only tighten"
    assert len(calls) == 1, "the 12% stop this position carries did not fire"
    assert calls[0]["con_id"] == 101
    assert calls[0]["trigger_type"] == "stop"


    assert MARK_PAST_THE_FLOOR <= EPS_LONG * (1 - 12.0 / 100.0)
    assert MARK_PAST_THE_FLOOR > EPS_LONG * (1 - 25.0 / 100.0)


def test_a_genuinely_tighter_atr_answer_is_adopted_while_the_position_still_has_room(
        tmp_path, monkeypatch, capsys):
    """Public API contract; production-derived narrative omitted."""
    mgr = _atr_cycle(tmp_path, monkeypatch, "atr_tighten",
                     price=MARK_WITH_ROOM, atr=ATR_TIGHT)
    calls = _capture_closes(mgr)
    _run(mgr)
    out = capsys.readouterr().out

    moved = _tightened_to(out)
    assert moved is not None, f"the ATR stop was never installed:\n{out}"
    was, now = moved
    assert was == pytest.approx(30.0), "the position should have started on the airtight 30%"
    assert 12.0 < now < 20.0, f"the installed stop is not the ATR answer: {now}"
    assert "GRANDFATHERED" not in out, "a position with room must not be grandfathered"


    _assert_evaluated(mgr, 101)
    assert calls == []


def test_a_position_already_past_the_new_stop_keeps_the_one_it_was_opened_under(
        tmp_path, monkeypatch, capsys):
    """Public API contract; production-derived narrative omitted."""
    mgr = _atr_cycle(tmp_path, monkeypatch, "atr_grandfather",
                     price=MARK_PAST_THE_FLOOR, atr=ATR_TIGHT)
    calls = _capture_closes(mgr)
    _run(mgr)
    out = capsys.readouterr().out

    assert "GRANDFATHERED" in out, f"the grandfather clause never ran:\n{out}"
    assert "PERMANENTLY" in out, "the exemption must be stated as the one-shot it now is"
    assert _tightened_to(out) is None, "the tighter stop was installed over a position past it"
    _assert_evaluated(mgr, 101)
    assert calls == [], "a policy change retroactively force-closed a live position"


    assert MARK_PAST_THE_FLOOR > EPS_LONG * (1 - 30.0 / 100.0)
    assert MARK_PAST_THE_FLOOR <= EPS_LONG * (1 - 15.0 / 100.0)


def test_only_the_mark_separates_the_grandfathered_position_from_the_tightened_one(
        tmp_path, monkeypatch, capsys):
    """Public API contract; production-derived narrative omitted."""
    with_room = _atr_cycle(tmp_path, monkeypatch, "gf_room",
                           price=MARK_WITH_ROOM, atr=ATR_TIGHT)
    _run(with_room)
    room_out = capsys.readouterr().out

    past_it = _atr_cycle(tmp_path, monkeypatch, "gf_past",
                         price=MARK_PAST_THE_FLOOR, atr=ATR_TIGHT)
    _run(past_it)
    past_out = capsys.readouterr().out

    assert _tightened_to(room_out) is not None and "GRANDFATHERED" not in room_out
    assert _tightened_to(past_out) is None and "GRANDFATHERED" in past_out


def _atr_stop_in_force(cur, atr_stop, mark, eps, *, tighten_only=True, grandfather=True):
    """Public API contract; production-derived narrative omitted."""
    if tighten_only and atr_stop >= cur:
        return cur
    if not tighten_only and atr_stop < cur:
        return cur
    if grandfather and mark <= eps * (1.0 - atr_stop / 100.0):
        return cur
    return round(atr_stop, 1)


def test_the_tighten_only_and_grandfather_assertions_have_teeth():
    """Public API contract; production-derived narrative omitted."""

    assert _atr_stop_in_force(12.0, 25.3, MARK_PAST_THE_FLOOR, EPS_LONG) == 12.0
    assert _atr_stop_in_force(12.0, 25.3, MARK_PAST_THE_FLOOR, EPS_LONG,
                              tighten_only=False) == 25.3
    assert MARK_PAST_THE_FLOOR <= EPS_LONG * (1 - 12.0 / 100.0)
    assert MARK_PAST_THE_FLOOR > EPS_LONG * (1 - 25.3 / 100.0)



    assert _atr_stop_in_force(30.0, 15.1, MARK_PAST_THE_FLOOR, EPS_LONG) == 30.0
    assert _atr_stop_in_force(30.0, 15.1, MARK_PAST_THE_FLOOR, EPS_LONG,
                              grandfather=False) == 15.1
    assert MARK_PAST_THE_FLOOR > EPS_LONG * (1 - 30.0 / 100.0)
    assert MARK_PAST_THE_FLOOR <= EPS_LONG * (1 - 15.1 / 100.0)

    assert _atr_stop_in_force(30.0, 15.1, MARK_WITH_ROOM, EPS_LONG) == 15.1
    assert _atr_stop_in_force(30.0, 15.1, MARK_WITH_ROOM, EPS_LONG, grandfather=False) == 15.1




















MARK_THROUGH_THE_ADOPTED_STOP = 3.40


def test_a_tightened_atr_stop_survives_the_cycle_and_actually_closes_the_trade(
        tmp_path, monkeypatch, capsys):
    """Public API contract; production-derived narrative omitted."""
    first = _atr_cycle(tmp_path, monkeypatch, "atr_latch",
                       price=MARK_WITH_ROOM, atr=ATR_TIGHT)
    _capture_closes(first)
    _run(first)
    adopted = _tightened_to(capsys.readouterr().out)
    assert adopted is not None, "cycle 1 never adopted the tighter stop -- nothing to inherit"
    assert 12.0 < adopted[1] < 20.0

    second = _atr_cycle(tmp_path, monkeypatch, "atr_latch",
                        price=MARK_THROUGH_THE_ADOPTED_STOP, atr=ATR_TIGHT)
    calls = _capture_closes(second)
    _run(second)
    out = capsys.readouterr().out

    _assert_evaluated(second, 101)
    assert len(calls) == 1, f"the adopted ATR stop never fired -- it did not survive:\n{out}"
    assert calls[0]["con_id"] == 101
    assert calls[0]["trigger_type"] == "stop", (
        f"closed, but not as a stop: {calls[0]['trigger_type']}")


    assert MARK_THROUGH_THE_ADOPTED_STOP > EPS_LONG * (1 - 30.0 / 100.0)
    assert MARK_THROUGH_THE_ADOPTED_STOP <= EPS_LONG * (1 - 15.1 / 100.0)




    rec = (json.loads((tmp_path / "atr_latch" / "state.json").read_text())
           .get("atr_stop_introduction") or {}).get("101")
    assert rec and rec["grandfathered"] is False, (
        f"the introduction decision was re-taken on a later cycle: {rec}")
    assert float(rec["stop_pct"]) == pytest.approx(adopted[1])


def test_a_later_wider_atr_answer_never_widens_the_stop_already_adopted(
        tmp_path, monkeypatch, capsys):
    """Public API contract; production-derived narrative omitted."""
    first = _atr_cycle(tmp_path, monkeypatch, "atr_widen", price=MARK_WITH_ROOM, atr=ATR_TIGHT)
    _capture_closes(first)
    _run(first)
    was, adopted = _tightened_to(capsys.readouterr().out)
    assert 12.0 < adopted < 20.0

    second = _atr_cycle(tmp_path, monkeypatch, "atr_widen",
                        price=MARK_THROUGH_THE_ADOPTED_STOP, atr=ATR_WIDE)
    calls = _capture_closes(second)
    _run(second)
    out = capsys.readouterr().out

    _assert_evaluated(second, 101)
    assert "INHERITED" in out, f"the adopted stop was never read back:\n{out}"
    assert f"not tighter than {adopted:.1f}%" in out, (
        f"the wider ATR answer was measured against the wrong stop:\n{out}")
    assert len(calls) == 1, f"a vol expansion widened a stop that was already adopted:\n{out}"
    assert calls[0]["trigger_type"] == "stop"


    assert MARK_THROUGH_THE_ADOPTED_STOP <= EPS_LONG * (1 - adopted / 100.0)
    assert MARK_THROUGH_THE_ADOPTED_STOP > EPS_LONG * (1 - 25.3 / 100.0)
    assert MARK_THROUGH_THE_ADOPTED_STOP > EPS_LONG * (1 - 30.0 / 100.0)


def test_the_adopted_stop_is_durable_state_not_a_log_line(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    mgr = _atr_cycle(tmp_path, monkeypatch, "atr_latch_state",
                     price=MARK_WITH_ROOM, atr=ATR_TIGHT)
    _capture_closes(mgr)
    _run(mgr)
    on_disk = json.loads((tmp_path / "atr_latch_state" / "state.json").read_text())
    rec = (on_disk.get("atr_stop_introduction") or {}).get("101")
    assert rec, "the adopted stop was never persisted -- there is no later cycle to inherit it"
    assert rec["grandfathered"] is False
    assert 12.0 < float(rec["stop_pct"]) < 20.0
    assert rec["basis"], "an introduction with no entry basis can outlive its position"

    assert tuple(rec) == ATR_STOP_FIELDS, f"on-disk shape drifted: {tuple(rec)}"





ATR_TIGHTER = 0.30
MARK_INSIDE_THE_RETIGHTENED_STOP = 3.55


def test_an_adopted_stop_goes_on_tightening_and_the_tighter_one_can_fire(
        tmp_path, monkeypatch, capsys):
    """Public API contract; production-derived narrative omitted."""
    first = _atr_cycle(tmp_path, monkeypatch, "atr_ratchet",
                       price=MARK_WITH_ROOM, atr=ATR_TIGHT)
    _capture_closes(first)
    _run(first)
    _, adopted = _tightened_to(capsys.readouterr().out)

    second = _atr_cycle(tmp_path, monkeypatch, "atr_ratchet",
                        price=MARK_INSIDE_THE_RETIGHTENED_STOP, atr=ATR_TIGHTER)
    calls = _capture_closes(second)
    _run(second)
    out = capsys.readouterr().out

    _assert_evaluated(second, 101)
    moved = _tightened_to(out)
    assert moved is not None, f"the adopted stop never re-sized:\n{out}"
    was, now = moved
    assert was == pytest.approx(adopted), "it re-sized against the wrong starting stop"
    assert now < adopted, f"the stop did not tighten further: {now} vs {adopted}"
    assert "GRANDFATHERED" not in out, (
        "a live adoption was converted into a permanent exemption by a re-run price test")
    assert len(calls) == 1, f"the re-tightened stop never fired:\n{out}"
    assert calls[0]["trigger_type"] == "stop"

    rec = (json.loads((tmp_path / "atr_ratchet" / "state.json").read_text())
           .get("atr_stop_introduction") or {}).get("101")
    assert rec and rec["grandfathered"] is False
    assert float(rec["stop_pct"]) == pytest.approx(now)

    assert MARK_INSIDE_THE_RETIGHTENED_STOP > EPS_LONG * (1 - adopted / 100.0)
    assert MARK_INSIDE_THE_RETIGHTENED_STOP <= EPS_LONG * (1 - now / 100.0)
    assert MARK_INSIDE_THE_RETIGHTENED_STOP > EPS_LONG * (1 - 30.0 / 100.0)


def test_a_grandfathered_position_is_decided_once_and_never_re_tested(
        tmp_path, monkeypatch, capsys):
    """Public API contract; production-derived narrative omitted."""
    first = _atr_cycle(tmp_path, monkeypatch, "atr_gf_latch",
                       price=MARK_PAST_THE_FLOOR, atr=ATR_TIGHT)
    calls_1 = _capture_closes(first)
    _run(first)
    assert "GRANDFATHERED" in capsys.readouterr().out
    assert calls_1 == [], "the introducing cycle force-closed the position"

    second = _atr_cycle(tmp_path, monkeypatch, "atr_gf_latch",
                        price=MARK_WITH_ROOM, atr=ATR_TIGHT)
    calls_2 = _capture_closes(second)
    _run(second)
    out = capsys.readouterr().out

    _assert_evaluated(second, 101)
    assert _tightened_to(out) is None, (
        f"a permanently grandfathered position adopted the tighter stop anyway:\n{out}")
    assert "GRANDFATHERED" in out, f"the persisted decision was not read back:\n{out}"
    assert calls_2 == []


def test_a_new_campaign_on_the_same_con_id_does_not_inherit_the_exemption(
        tmp_path, monkeypatch, capsys):
    """Public API contract; production-derived narrative omitted."""
    first = _atr_cycle(tmp_path, monkeypatch, "atr_gf_reuse",
                       price=MARK_PAST_THE_FLOOR, atr=ATR_TIGHT)
    _capture_closes(first)
    _run(first)
    assert "GRANDFATHERED" in capsys.readouterr().out

    second = _atr_cycle(tmp_path, monkeypatch, "atr_gf_reuse",
                        price=MARK_WITH_ROOM, atr=ATR_TIGHT,
                        decision_id="dec-a-completely-new-campaign")
    _capture_closes(second)
    _run(second)
    out = capsys.readouterr().out

    assert _tightened_to(out) is not None, (
        f"a NEW campaign inherited the old one's permanent exemption:\n{out}")
    assert "GRANDFATHERED" not in out


def test_the_entry_basis_invalidates_the_decision_when_a_scale_in_moves_it(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    st = State()
    basis = atr_stop_basis("dec-1", 4.10)
    st.introduce_atr_stop(101, basis, grandfathered=False, stop_pct=15.1)
    assert st.atr_stop_decision(101, basis)["stop_pct"] == pytest.approx(15.1)

    assert st.atr_stop_decision(101, atr_stop_basis("dec-1", 3.55)) is None
    assert st.atr_stop_decision(101, atr_stop_basis("dec-2", 4.10)) is None


def test_an_absent_or_corrupt_record_means_not_yet_introduced_never_grandfathered():
    """Public API contract; production-derived narrative omitted."""
    st = State()
    basis = atr_stop_basis("dec-1", 4.10)
    assert st.atr_stop_decision(101, basis) is None
    for junk in (None, {}, [], "grandfathered", {"basis": basis}, 7,
                 {"basis": basis, "grandfathered": False, "stop_pct": None},
                 {"basis": basis, "grandfathered": False, "stop_pct": float("nan")},
                 {"basis": basis, "grandfathered": False, "stop_pct": 0.0}):
        st.atr_stop_introduction["101"] = junk
        assert st.atr_stop_decision(101, basis) is None, junk
    assert st.atr_stop_decision(101, None) is None


def test_a_persisted_adopted_stop_only_ever_tightens(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    st = State()
    basis = atr_stop_basis("dec-1", 4.10)
    st.introduce_atr_stop(101, basis, grandfathered=False, stop_pct=15.1)
    assert st.tighten_atr_stop(101, basis, 20.0) is False
    assert st.atr_stop_decision(101, basis)["stop_pct"] == pytest.approx(15.1)
    assert st.tighten_atr_stop(101, basis, 15.1) is False
    assert st.tighten_atr_stop(101, basis, 11.2) is True
    assert st.atr_stop_decision(101, basis)["stop_pct"] == pytest.approx(11.2)
    assert st.tighten_atr_stop(101, atr_stop_basis("dec-1", 3.55), 9.0) is False
    st.introduce_atr_stop(102, basis, grandfathered=True)
    assert st.tighten_atr_stop(102, basis, 9.0) is False
    assert st.atr_stop_decision(102, basis)["grandfathered"] is True


def test_the_decision_is_dropped_when_the_position_is_no_longer_active():
    """Public API contract; production-derived narrative omitted."""
    st = State()
    basis = atr_stop_basis("dec-1", 4.10)
    st.introduce_atr_stop(101, basis, grandfathered=True)
    st.introduce_atr_stop(202, basis, grandfathered=False, stop_pct=15.1)
    st.prune_tracking({101})
    assert st.atr_stop_decision(101, basis) is not None
    assert st.atr_stop_decision(202, basis) is None


def test_an_atr_stop_with_no_durable_key_is_declined_rather_than_adopted_unlatched(
        tmp_path, monkeypatch, capsys):
    """Public API contract; production-derived narrative omitted."""
    monkeypatch.setattr("exitmgr.manager.atr_stop_basis", lambda *a, **k: None)
    mgr = _atr_cycle(tmp_path, monkeypatch, "atr_nobasis", price=MARK_WITH_ROOM, atr=ATR_TIGHT)
    calls = _capture_closes(mgr)
    _run(mgr)
    out = capsys.readouterr().out

    _assert_evaluated(mgr, 101)
    assert "no usable entry basis" in out, f"the guard never ran:\n{out}"
    assert "stop sizing skipped" not in out, (
        "a deliberate, logged decline was reported as an unexpected error")
    assert _tightened_to(out) is None, (
        "a stop was adopted that nothing could persist -- it is gone next cycle")
    assert calls == []
    assert not (tmp_path / "atr_nobasis" / "state.json").read_text().count('"basis"'), (
        "an unkeyed decision was written and can attach itself to the next position here")


def _atr_stop_in_force_latched(cur, atr_stop, mark, eps, record, *, latched=True):
    """Public API contract; production-derived narrative omitted."""
    if record is not None and record.get("grandfathered"):
        return cur, record
    if record is not None and record.get("stop_pct"):
        cur = min(cur, float(record["stop_pct"]))
    if atr_stop >= cur:
        return cur, record
    if (record is None or not latched) and mark <= eps * (1.0 - atr_stop / 100.0):
        return cur, {"grandfathered": True}
    return round(atr_stop, 1), {"grandfathered": False, "stop_pct": round(atr_stop, 1)}


def test_the_latch_assertions_have_teeth_the_unlatched_variant_can_never_fire():
    """Public API contract; production-derived narrative omitted."""
    fired = lambda stop, mark: mark <= EPS_LONG * (1.0 - stop / 100.0)


    s1, rec = _atr_stop_in_force_latched(30.0, 15.1, MARK_WITH_ROOM, EPS_LONG, None)
    assert (s1, rec["stop_pct"]) == (15.1, 15.1)
    s2, _ = _atr_stop_in_force_latched(30.0, 15.1, MARK_THROUGH_THE_ADOPTED_STOP,
                                       EPS_LONG, rec)
    assert s2 == 15.1 and fired(s2, MARK_THROUGH_THE_ADOPTED_STOP)




    u1, _ = _atr_stop_in_force_latched(30.0, 15.1, MARK_WITH_ROOM, EPS_LONG, None,
                                       latched=False)
    u2, _ = _atr_stop_in_force_latched(30.0, 15.1, MARK_THROUGH_THE_ADOPTED_STOP,
                                       EPS_LONG, None, latched=False)
    assert (u1, u2) == (15.1, 30.0)
    assert not fired(u2, MARK_THROUGH_THE_ADOPTED_STOP)
    assert s2 != u2, "the two shapes agree -- this proof would be vacuous"


    g1, grec = _atr_stop_in_force_latched(30.0, 15.1, MARK_PAST_THE_FLOOR, EPS_LONG, None)
    assert g1 == 30.0 and grec["grandfathered"] is True
    g2, _ = _atr_stop_in_force_latched(30.0, 15.1, MARK_WITH_ROOM, EPS_LONG, grec)
    assert g2 == 30.0
    u_g2, _ = _atr_stop_in_force_latched(30.0, 15.1, MARK_WITH_ROOM, EPS_LONG, None,
                                         latched=False)
    assert u_g2 == 15.1 and g2 != u_g2








TRAIL_ENTRY_DEBIT, TRAIL_QTY = 820.0, 2
TRAIL_PEAK = 5.30
GAP_BREACHING_BOTH = 2.50
GAP_BREACHING_ONLY_THE_TRAIL = 4.50


def _trail_rules():
    base = Config().rules
    return replace(base, stop_pct=30.0, profit_target_pct=None, time_stop_days=None,
                   trailing=replace(base.trailing, enabled=True,
                                    activation_gain_pct=25.0, giveback_fraction=0.5))


def test_the_stop_and_the_trail_really_do_breach_together_on_this_gap():
    """Public API contract; production-derived narrative omitted."""
    assert evaluate_stop(GAP_BREACHING_BOTH, TRAIL_ENTRY_DEBIT, TRAIL_QTY, 30.0) is not None
    assert evaluate_trailing_stop(GAP_BREACHING_BOTH, TRAIL_ENTRY_DEBIT, TRAIL_QTY,
                                  TRAIL_PEAK, 25.0, 0.5, armed=True) is not None


def test_a_simultaneous_breach_is_reported_as_a_stop_not_as_gain_protection():
    """Public API contract; production-derived narrative omitted."""
    trigger = evaluate_position(
        con_id=101, symbol="RKLB", quantity=TRAIL_QTY, entry_debit=TRAIL_ENTRY_DEBIT,
        current_price=GAP_BREACHING_BOTH, days_to_expiry=120, peak_price=TRAIL_PEAK,
        rules=_trail_rules(), trail_armed=True, peak_since_arm=TRAIL_PEAK)
    assert trigger is not None
    assert trigger.trigger_type == "stop", "a realized loss was reported as gain protection"
    assert trigger.pnl_pct < -30.0
    assert "Stop hit" in trigger.message


def test_the_trail_still_wins_when_the_trail_alone_is_breached():
    """Public API contract; production-derived narrative omitted."""
    trigger = evaluate_position(
        con_id=101, symbol="RKLB", quantity=TRAIL_QTY, entry_debit=TRAIL_ENTRY_DEBIT,
        current_price=GAP_BREACHING_ONLY_THE_TRAIL, days_to_expiry=120,
        peak_price=TRAIL_PEAK, rules=_trail_rules(), trail_armed=True,
        peak_since_arm=TRAIL_PEAK)
    assert trigger is not None
    assert trigger.trigger_type == "trailing_stop"
    assert trigger.pnl_pct > 0
    assert evaluate_stop(GAP_BREACHING_ONLY_THE_TRAIL, TRAIL_ENTRY_DEBIT,
                         TRAIL_QTY, 30.0) is None


def test_the_transmitted_exit_carries_stop_all_the_way_to_the_order(tmp_path, monkeypatch,
                                                                    capsys):
    """Public API contract; production-derived narrative omitted."""
    mgr = _cycle_mgr(tmp_path, monkeypatch, "trail_vs_stop",
                     [_long_journal(ts=_days_ago(3), intended_hold_days=30)],
                     rows=[_raw(101, "C", 2)], marks=[_mark(101, GAP_BREACHING_BOTH)],
                     quotes={101: {"price": GAP_BREACHING_BOTH, "bid": 2.45, "ask": 2.55}})
    mgr.config.rules.stop_pct = 30.0
    mgr.config.rules.trailing.enabled = True
    mgr.config.rules.trailing.activation_gain_pct = 25.0
    mgr.config.rules.trailing.giveback_fraction = 0.5

    assert mgr.state_manager.state.arm_on_peak_gain(
        101, TRAIL_PEAK, TRAIL_ENTRY_DEBIT, TRAIL_QTY, 25.0) is True
    calls = _capture_closes(mgr)
    _run(mgr)

    assert mgr.state_manager.state.is_trail_armed(101) is True, "fixture never armed the trail"
    assert mgr.state_manager.state.trail_peak_since_arm(101) == pytest.approx(TRAIL_PEAK)
    assert len(calls) == 1
    assert calls[0]["trigger_type"] == "stop"
    ctx = calls[0]["exit_context"] or {}
    assert ctx.get("trigger_type") == "stop"
    assert "Stop hit" in (ctx.get("trigger_message") or "")
    assert (ctx.get("extra") or {}).get("rule_fired") == "stop"



    assert ctx.get("reason") == "stop"
    assert ctx.get("trigger_pnl_pct") < -30.0
    capsys.readouterr()







SHORT_CREDIT, SHORT_QTY = 400.0, -1
SHORT_TROUGH = 1.00
SHORT_STOP_PCT = 100.0
SHORT_SPIKE_BREACHING_BOTH = 9.00
SHORT_SPIKE_BREACHING_ONLY_THE_TRAIL = 3.00


def _short_rules():
    base = Config().rules
    return replace(base, stop_pct=SHORT_STOP_PCT, profit_target_pct=None, time_stop_days=None,
                   trailing=replace(base.trailing, enabled=True,
                                    activation_gain_pct=25.0, giveback_fraction=0.5))


def _short_verdict(price):
    return evaluate_position(
        con_id=105, symbol="SPY", quantity=SHORT_QTY, entry_debit=0.0, current_price=price,
        days_to_expiry=120, peak_price=None, rules=_short_rules(), trail_armed=True,
        peak_since_arm=SHORT_TROUGH, entry_credit=SHORT_CREDIT)


def test_the_short_stop_and_the_short_trail_really_do_breach_together_on_this_spike():
    """Public API contract; production-derived narrative omitted."""
    assert evaluate_short_stop(SHORT_SPIKE_BREACHING_BOTH, SHORT_CREDIT, SHORT_QTY,
                               SHORT_STOP_PCT) is not None
    assert evaluate_short_trailing_stop(SHORT_SPIKE_BREACHING_BOTH, SHORT_CREDIT, SHORT_QTY,
                                        SHORT_TROUGH, 25.0, 0.5, armed=True) is not None


def test_a_simultaneous_short_breach_is_reported_as_a_stop_not_as_banked_decay():
    """Public API contract; production-derived narrative omitted."""
    trigger = _short_verdict(SHORT_SPIKE_BREACHING_BOTH)
    assert trigger is not None
    assert trigger.trigger_type == "stop"
    assert trigger.pnl_pct < -100.0
    assert "Short stop hit" in trigger.message


def test_the_short_trail_still_wins_when_the_short_trail_alone_is_breached():
    """Public API contract; production-derived narrative omitted."""
    trigger = _short_verdict(SHORT_SPIKE_BREACHING_ONLY_THE_TRAIL)
    assert trigger is not None
    assert trigger.trigger_type == "trailing_stop"
    assert trigger.pnl_pct > 0
    assert evaluate_short_stop(SHORT_SPIKE_BREACHING_ONLY_THE_TRAIL, SHORT_CREDIT, SHORT_QTY,
                               SHORT_STOP_PCT) is None


def _priority_pick(fired, *, trail_first):
    """Public API contract; production-derived narrative omitted."""
    priority = {"profit_target": 1,
                "stop": 3 if trail_first else 2,
                "trailing_stop": 2 if trail_first else 3,
                "scale_out": 4, "time_stop": 5}
    return sorted(fired, key=lambda t: priority.get(t, 99))[0]


def test_the_priority_assertion_has_teeth_the_old_order_still_reports_the_trail():
    """Public API contract; production-derived narrative omitted."""
    fired = ["stop", "trailing_stop"]
    assert _priority_pick(fired, trail_first=False) == "stop"
    assert _priority_pick(fired, trail_first=True) == "trailing_stop"

    assert _priority_pick(["time_stop", "trailing_stop", "stop"], trail_first=False) == "stop"
    assert _priority_pick(["scale_out", "stop"], trail_first=False) == "stop"
