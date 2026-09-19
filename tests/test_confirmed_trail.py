"""Public API contract; production-derived narrative omitted."""
import os
from dataclasses import replace

import pytest

from exitmgr.config import (Config, RulesConfig, TrailingConfig, AutoTrailConfig,
                            StateConfig, JournalConfig)
from exitmgr.connection import PositionData
from exitmgr.manager import ExitManager
from exitmgr import rules as rules_mod
from exitmgr.rules import evaluate_position, evaluate_trailing_stop
from exitmgr.state import StateCorruptionError, StateManager




ED, QTY = 500.0, 1
ACT, GIVEBACK = 20.0, 0.4
QUALIFYING, SUB_THRESHOLD = 6.10, 5.80



FRI = "2026-07-24"
MON = "2026-07-27"
TUE = "2026-07-28"
THU_PRE_HOLIDAY = "2026-07-02"
MON_POST_HOLIDAY = "2026-07-06"


def _rules(profit_target_pct=None, trailing_enabled=True):
    return RulesConfig(profit_target_pct=profit_target_pct, stop_pct=30.0,
                       trailing=TrailingConfig(enabled=trailing_enabled,
                                               activation_gain_pct=ACT,
                                               giveback_fraction=GIVEBACK))


def _mgr(tmp_path):
    cfg = Config()
    cfg.state = StateConfig(path=os.path.join(str(tmp_path), "state.json"))
    cfg.journal = JournalConfig(path=os.path.join(str(tmp_path), "trades.log"))
    cfg.rules = _rules(profit_target_pct=30.0)
    return ExitManager(cfg)


def _sm(tmp_path):
    return StateManager(os.path.join(str(tmp_path), "state.json"))


def _close(state, con_id, session, price):
    """Public API contract; production-derived narrative omitted."""
    return state.record_session_close(con_id, session, price, ED, QTY, ACT)


def _eval(price, rules, state, con_id, peak_price=None):
    """Public API contract; production-derived narrative omitted."""
    return evaluate_position(
        con_id=con_id, symbol="X", quantity=QTY, entry_debit=ED,
        current_price=price, days_to_expiry=200,
        peak_price=peak_price,
        rules=rules,
        trail_armed=state.is_trail_armed(con_id),
        peak_since_arm=state.trail_peak_since_arm(con_id),
    )






def test_0a_lifetime_peak_alone_never_fires_a_trail():
    """Public API contract; production-derived narrative omitted."""
    trig = evaluate_position(con_id=1, symbol="X", quantity=QTY, entry_debit=ED,
                             current_price=5.60, days_to_expiry=200, peak_price=6.10,
                             rules=_rules())
    assert trig is None or trig.trigger_type != "trailing_stop"


def test_0b_view_armed_bit_is_not_the_feature_toggle(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    m = _mgr(tmp_path)
    assert m.config.rules.trailing.enabled is True
    pos = PositionData(con_id=1, symbol="X", right="C", quantity=QTY, avg_cost=5.0,
                       expiry="20270115")
    m._journal_entries[1] = {"symbol": "X", "debit": ED, "quantity": QTY}
    views = m._build_position_views([pos], {1: {"price": 5.05}})
    assert views[0]["trail_armed"] is False



def test_1_intraday_spike_does_not_arm(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    st = _sm(tmp_path).state

    for px in (5.20, 6.10, 5.90):
        st.record_mark(101, px, ED, QTY)
    assert st.peak_prices["101"] == 6.10
    assert st.is_trail_armed(101) is False
    assert st.trail_peak_since_arm(101) is None
    assert st.trail_confirmation_for(101)["consecutive_qualifying_closes"] == 0




    trig = _eval(5.60, _rules(), st, 101, peak_price=6.10)
    assert trig is None or trig.trigger_type != "trailing_stop"


    trig2 = evaluate_position(con_id=101, symbol="X", quantity=QTY, entry_debit=ED,
                              current_price=5.60, days_to_expiry=200, peak_price=6.10,
                              rules=_rules())
    assert trig2 is None or trig2.trigger_type != "trailing_stop"



def test_2_qualifying_then_sub_threshold_close_resets(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    st = _sm(tmp_path).state
    rec = _close(st, 102, FRI, QUALIFYING)
    assert rec["consecutive_qualifying_closes"] == 1
    assert st.is_trail_armed(102) is False

    rec = _close(st, 102, MON, SUB_THRESHOLD)
    assert rec["consecutive_qualifying_closes"] == 0
    assert st.is_trail_armed(102) is False
    assert st.trail_peak_since_arm(102) is None


    rec = _close(st, 102, TUE, QUALIFYING)
    assert rec["consecutive_qualifying_closes"] == 1
    assert st.is_trail_armed(102) is False



    st2 = _sm(tmp_path).state
    assert _close(st2, 122, FRI, 6.00)["consecutive_qualifying_closes"] == 1
    assert _close(st2, 122, MON, 6.00)["consecutive_qualifying_closes"] == 2
    assert st2.is_trail_armed(122) is True
    st3 = _sm(tmp_path).state
    assert _close(st3, 132, FRI, 5.99)["consecutive_qualifying_closes"] == 0
    assert st3.is_trail_armed(132) is False



def test_3_two_consecutive_qualifying_closes_arm_exactly_once(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    st = _sm(tmp_path).state
    _close(st, 103, FRI, QUALIFYING)
    assert st.is_trail_armed(103) is False

    rec = _close(st, 103, MON, 6.20)
    assert st.is_trail_armed(103) is True
    assert rec["consecutive_qualifying_closes"] == 2
    armed_at = rec["armed_at"]
    assert armed_at is not None
    assert st.trail_peak_since_arm(103) == 6.20


    rec = _close(st, 103, TUE, 6.50)
    assert rec["armed_at"] == armed_at
    assert st.trail_peak_since_arm(103) == 6.20


    st2 = _sm(tmp_path).state
    _close(st2, 113, FRI, QUALIFYING)
    _close(st2, 113, FRI, QUALIFYING)
    assert st2.is_trail_armed(113) is False



    rec = _close(st, 103, FRI, QUALIFYING)
    assert rec["last_session"] == TUE
    assert rec["consecutive_qualifying_closes"] == 2
    assert rec["armed_at"] == armed_at



def test_4_calendar_adjacency_and_missing_close(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    assert rules_mod.is_next_trading_session(FRI, MON) is True
    assert rules_mod.is_next_trading_session(FRI, TUE) is False

    assert rules_mod.is_next_trading_session(THU_PRE_HOLIDAY, MON_POST_HOLIDAY) is True

    st = _sm(tmp_path).state
    _close(st, 104, FRI, QUALIFYING)
    _close(st, 104, MON, QUALIFYING)
    assert st.is_trail_armed(104) is True


    st2 = _sm(tmp_path).state
    _close(st2, 204, FRI, QUALIFYING)
    rec = _close(st2, 204, TUE, QUALIFYING)
    assert rec["consecutive_qualifying_closes"] == 1
    assert st2.is_trail_armed(204) is False


    st3 = _sm(tmp_path).state
    _close(st3, 304, FRI, QUALIFYING)
    st3.record_session_close(304, MON, None, ED, QTY, ACT)
    st3.record_session_close(304, MON, float("nan"), ED, QTY, ACT)
    assert st3.is_trail_armed(304) is False
    assert st3.trail_confirmation_for(304)["last_session"] == FRI



def test_5_restart_between_the_two_closes_preserves_one_confirmation(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    sm = _sm(tmp_path)
    _close(sm.state, 105, FRI, QUALIFYING)
    sm.save()

    reloaded = StateManager(sm.state_path)
    assert reloaded.state.trail_confirmation_for(105)["consecutive_qualifying_closes"] == 1
    assert reloaded.state.trail_confirmation_for(105)["last_session"] == FRI
    assert reloaded.state.is_trail_armed(105) is False

    _close(reloaded.state, 105, MON, 6.20)
    assert reloaded.state.is_trail_armed(105) is True
    assert reloaded.state.trail_peak_since_arm(105) == 6.20



def test_6_enabled_and_configured_but_unconfirmed_reports_unarmed(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    m = _mgr(tmp_path)
    assert m.config.rules.trailing.enabled is True
    m._apply_decision(m.config.rules, {"action": "arm_trail"}, 7.0, ED, QTY, 106, "X")
    assert m.state_manager.state.trail_configured.get("106") is True
    assert m.state_manager.state.is_trail_armed(106) is False

    pos = PositionData(con_id=106, symbol="X", right="C", quantity=QTY, avg_cost=5.0,
                       expiry="20270115")
    m._journal_entries[106] = {"symbol": "X", "debit": ED, "quantity": QTY}
    views = m._build_position_views([pos], {106: {"price": 6.30}})
    assert len(views) == 1
    v = views[0]
    assert v["trail_enabled"] is True
    assert v["trail_configured"] is True
    assert v["trail_armed"] is False


    trig = _eval(5.00, _rules(), m.state_manager.state, 106, peak_price=7.00)
    assert trig is None or trig.trigger_type != "trailing_stop"



def test_7_pre_arm_spike_cannot_set_the_post_arm_floor(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    st = _sm(tmp_path).state
    st.record_mark(107, 8.00, ED, QTY)
    assert st.peak_prices["107"] == 8.00


    assert st.record_trail_peak(107, 8.00) is None
    assert st.trail_peak_since_arm(107) is None
    assert "107" not in st.trail_confirmation

    _close(st, 107, FRI, QUALIFYING)
    _close(st, 107, MON, 6.10)
    assert st.is_trail_armed(107) is True
    assert st.trail_peak_since_arm(107) == 6.10



    trig = _eval(6.00, _rules(), st, 107, peak_price=8.00)
    assert trig is None
    trig = _eval(5.65, _rules(), st, 107, peak_price=8.00)
    assert trig is not None and trig.trigger_type == "trailing_stop"



def test_8_floor_ratchets_from_peak_since_arm_and_survives_restart(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    sm = _sm(tmp_path)
    st = sm.state
    _close(st, 108, FRI, QUALIFYING)
    _close(st, 108, MON, 6.10)
    assert st.trail_peak_since_arm(108) == 6.10

    st.record_trail_peak(108, 7.00)
    assert st.trail_peak_since_arm(108) == 7.00
    st.record_trail_peak(108, 6.50)
    assert st.trail_peak_since_arm(108) == 7.00

    sm.save()
    reloaded = StateManager(sm.state_path)
    assert reloaded.state.is_trail_armed(108) is True
    assert reloaded.state.trail_peak_since_arm(108) == 7.00


    r = _rules()
    assert _eval(6.25, r, reloaded.state, 108) is None
    trig = _eval(6.19, r, reloaded.state, 108)
    assert trig is not None and trig.trigger_type == "trailing_stop"
    assert trig.pnl_pct > 0



def test_9_auto_trail_cannot_bypass_the_confirmation_contract(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    m = _mgr(tmp_path)
    auto = AutoTrailConfig(enabled=True, activation_gain_pct=25.0, giveback_fraction=0.5)
    st = m.state_manager.state
    base = replace(m.config.rules, trailing=TrailingConfig(enabled=False))


    st.record_mark(109, 8.00, ED, QTY)
    out, widened = m._apply_auto_trail(base, auto, 8.00, ED, QTY,
                                       armed=st.is_trail_armed(109),
                                       peak_since_arm=st.trail_peak_since_arm(109))
    assert widened is False
    assert out is base
    trig = _eval(5.50, out, st, 109, peak_price=8.00)
    assert trig is None or trig.trigger_type != "trailing_stop"



    out, widened = m._apply_auto_trail(base, auto, 8.00, ED, QTY,
                                       armed=False, peak_since_arm=8.00)
    assert widened is False and out is base



    out, widened = m._apply_auto_trail(base, auto, 8.00, ED, QTY,
                                       armed=True, peak_since_arm=6.10)
    assert widened is False and out is base


    _close(st, 109, FRI, QUALIFYING)
    _close(st, 109, MON, 6.10)
    st.record_trail_peak(109, 8.00)
    out2, widened2 = m._apply_auto_trail(base, auto, 8.00, ED, QTY,
                                         armed=st.is_trail_armed(109),
                                         peak_since_arm=st.trail_peak_since_arm(109))
    assert widened2 is True
    assert out2.trailing.enabled is True
    assert out2.trailing.giveback_fraction == 0.5
    assert out2.profit_target_pct == base.profit_target_pct



def test_10_close_and_prune_clear_all_trail_state(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    m = _mgr(tmp_path)
    st = m.state_manager.state
    m._apply_decision(m.config.rules, {"action": "arm_trail"}, 7.0, ED, QTY, 110, "X")
    _close(st, 110, FRI, QUALIFYING)
    _close(st, 110, MON, 6.10)
    st.record_trail_peak(110, 7.00)
    assert st.is_trail_armed(110) is True

    m._clear_closed_position(110)
    assert st.is_trail_armed(110) is False
    assert st.trail_peak_since_arm(110) is None
    assert "110" not in st.trail_confirmation
    assert "110" not in st.trail_configured
    assert st.trail_confirmation_for(110)["last_session"] is None
    assert st.trail_confirmation_for(110)["consecutive_qualifying_closes"] == 0


    _close(st, 310, FRI, QUALIFYING)
    _close(st, 310, MON, 6.10)
    st.trail_configured["310"] = True
    assert st.is_trail_armed(310) is True
    st.clear_trail_state(310)
    assert "310" not in st.trail_confirmation
    assert "310" not in st.trail_configured
    assert st.is_trail_armed(310) is False


    _close(st, 210, FRI, QUALIFYING)
    _close(st, 210, MON, 6.10)
    st.trail_configured["210"] = True
    assert st.is_trail_armed(210) is True
    st.prune_tracking(active_con_ids=[999])
    assert st.is_trail_armed(210) is False
    assert "210" not in st.trail_confirmation
    assert "210" not in st.trail_configured



def test_migration_old_state_loads_unarmed_with_no_mfe_backfill(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    import json
    p = os.path.join(str(tmp_path), "state.json")
    with open(p, "w") as f:
        json.dump({"in_flight": {}, "daily_stats": {}, "last_cycle": None,
                   "peak_prices": {"900": 9.99}, "mfe_pct": {"900": 99.0},
                   "mae_pct": {}, "mfe_ts": {}, "mae_ts": {}, "mark_path": {},
                   "scaled_out": {}, "trail_armed": {"900": True}}, f)
    st = StateManager(p).state
    assert st.trail_configured.get("900") is True
    assert st.is_trail_armed(900) is False
    assert st.trail_peak_since_arm(900) is None
    assert st.peak_prices["900"] == 9.99
    assert st.trail_confirmation == {}


def test_migration_round_trips_and_mirrors_the_legacy_key(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    import json
    sm = _sm(tmp_path)
    sm.state.trail_configured["901"] = True
    _close(sm.state, 902, FRI, QUALIFYING)
    _close(sm.state, 902, MON, 6.10)
    sm.save()
    with open(sm.state_path) as f:
        raw = json.load(f)
    assert raw["trail_configured"] == {"901": True}
    assert raw["trail_armed"] == {"901": True}
    assert raw["trail_confirmation"]["902"]["peak_since_arm"] == 6.10


    raw["trail_confirmation"]["903"] = {"last_session": FRI, "consecutive_qualifying_closes": 2,
                                        "armed_at": "2026-07-24T16:05:00-04:00",
                                        "peak_since_arm": None}
    raw["trail_confirmation"]["904"] = "garbage"
    with open(sm.state_path, "w") as f:
        json.dump(raw, f)
    with pytest.raises(StateCorruptionError):
        _ = StateManager(sm.state_path).state



def test_session_close_recorded_only_from_an_official_post_close_mark(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    m = _mgr(tmp_path)

    assert ExitManager._completed_session_date(datetime(2026, 7, 24, 16, 5, tzinfo=et)) == FRI
    assert ExitManager._completed_session_date(datetime(2026, 7, 24, 12, 0, tzinfo=et)) is None
    assert ExitManager._completed_session_date(datetime(2026, 7, 25, 17, 0, tzinfo=et)) is None
    assert ExitManager._completed_session_date(datetime(2026, 7, 3, 17, 0, tzinfo=et)) is None

    post_close = datetime(2026, 7, 24, 16, 5, tzinfo=et)

    m._maybe_record_session_close(120, QUALIFYING, ED, QTY, is_official_mark=False,
                                  now=post_close)
    assert m.state_manager.state.trail_confirmation_for(120)["last_session"] is None

    m._maybe_record_session_close(120, QUALIFYING, ED, QTY, is_official_mark=True, now=post_close)
    assert m.state_manager.state.trail_confirmation_for(120)["last_session"] == FRI

    for _ in range(5):
        m._maybe_record_session_close(120, QUALIFYING, ED, QTY, is_official_mark=True,
                                      now=post_close)
    assert m.state_manager.state.is_trail_armed(120) is False


def test_armed_bit_gates_independently_of_the_ratchet_price(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    trig = evaluate_position(con_id=1, symbol="X", quantity=QTY, entry_debit=ED,
                             current_price=5.60, days_to_expiry=200, peak_price=7.00,
                             rules=_rules(), trail_armed=False, peak_since_arm=7.00)
    assert trig is None or trig.trigger_type != "trailing_stop"
    trig = evaluate_position(con_id=1, symbol="X", quantity=QTY, entry_debit=ED,
                             current_price=5.60, days_to_expiry=200, peak_price=7.00,
                             rules=_rules(), trail_armed=True, peak_since_arm=7.00)
    assert trig is not None and trig.trigger_type == "trailing_stop"


def test_evaluate_trailing_stop_requires_explicit_armed_state():
    """Public API contract; production-derived narrative omitted."""
    assert evaluate_trailing_stop(5.60, ED, QTY, 6.10, ACT, GIVEBACK, armed=False) is None
    assert evaluate_trailing_stop(5.60, ED, QTY, None, ACT, GIVEBACK, armed=True) is None
    assert evaluate_trailing_stop(5.60, ED, QTY, 6.10, ACT, GIVEBACK, armed=True) is not None







import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from exitmgr.config import ScaleOutConfig
from exitmgr.order import OrderResult

E2E_CON = 4242
E2E_JOURNAL = {"ts": "2026-07-01T14:00:00+00:00", "contract_id": E2E_CON, "symbol": "AAPL",
               "right": "C", "strike": 200.0, "expiry": "20261231", "quantity": 4,
               "debit": 2000.0, "conviction": 6}


def _e2e_mgr(tmp_path):
    """Public API contract; production-derived narrative omitted."""
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
        profit_target_pct=None, stop_pct=30.0, time_stop_days=10,
        trailing=TrailingConfig(enabled=True, activation_gain_pct=ACT,
                                giveback_fraction=GIVEBACK),
        scale_out=ScaleOutConfig(enabled=False),
        auto_trail=AutoTrailConfig(enabled=False),
    )
    (tmp_path / "trades.log").write_text(json.dumps(E2E_JOURNAL) + "\n")
    return ExitManager(cfg)


def _e2e_wire(mgr, price, qty=4):
    pos = {E2E_CON: PositionData(con_id=E2E_CON, symbol="AAPL", right="C",
                                 quantity=qty, avg_cost=5.00, expiry="20261231")}
    mgr.ib_conn.get_positions = AsyncMock(return_value=pos)
    mgr.ib_conn.get_open_orders = AsyncMock(return_value={})
    mgr.ib_conn.fetch_quotes = AsyncMock(return_value={E2E_CON: {"price": price}})
    mgr.ib_conn.ib = MagicMock()
    mgr.ib_conn.ib.portfolio = lambda: []
    mgr._spot_price = AsyncMock(return_value=None)
    trade = SimpleNamespace(order=SimpleNamespace(orderId=555),
                            orderStatus=SimpleNamespace(status="Filled", avgFillPrice=price,
                                                        filled=qty, remaining=0),
                            fills=[])
    place = AsyncMock(return_value=OrderResult(success=True, order_id=555, con_id=E2E_CON,
                                               trade=trade))
    mgr.order_manager.place_close_order = place
    return place


@pytest.mark.asyncio
async def test_e2e_unconfirmed_winner_is_not_trailed_out_by_the_real_cycle(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr = _e2e_mgr(tmp_path)
    _e2e_wire(mgr, 6.10)
    await mgr.run_cycle(dry_run=False)
    assert mgr.state_manager.state.peak_prices[str(E2E_CON)] == 6.10
    assert mgr.state_manager.state.is_trail_armed(E2E_CON) is False

    place = _e2e_wire(mgr, 5.60)
    await mgr.run_cycle(dry_run=False)
    place.assert_not_called()


@pytest.mark.asyncio
async def test_e2e_confirmed_winner_is_trailed_out_by_the_real_cycle(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr = _e2e_mgr(tmp_path)
    st = mgr.state_manager.state
    _close(st, E2E_CON, FRI, QUALIFYING)
    _close(st, E2E_CON, MON, 6.10)
    assert st.is_trail_armed(E2E_CON) is True

    place = _e2e_wire(mgr, 5.60)
    await mgr.run_cycle(dry_run=False)
    place.assert_called_once()
    assert place.call_args.kwargs.get("quantity", 4) == 4
