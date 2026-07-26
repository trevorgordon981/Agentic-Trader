"""CONFIRMED TRAILING STOP -- Sol audit R5 section R1.

A trailing stop may arm ONLY after TWO CONSECUTIVE COMPLETED regular trading sessions whose
OFFICIAL CLOSING option mark was at or above the activation gain versus entry.

  * intraday peaks never count;
  * a session with no closing mark does not count and is NEVER inferred from lifetime MFE;
  * a close below the threshold resets the consecutive count;
  * weekend/holiday adjacency follows the exchange calendar;
  * on the SECOND qualifying close the trail arms ONCE and `peak_since_arm` is seeded FROM THAT
    CLOSE, ratcheting only thereafter -- so a one-day pre-confirmation spike can never poison the
    floor and cause an immediate noise exit;
  * the monotonic lifetime `peak_prices` stays audit/MFE evidence and may neither arm the trail nor
    set its floor.

Three concepts that used to be one boolean are kept apart throughout:
  trail_enabled    -- the FEATURE may be used (rules.trailing.enabled);
  trail_configured -- the MODEL asked for a trail (arm_trail): relaxes the take-profit ceiling only;
  trail_armed      -- PERSISTED: price AND session confirmation actually passed.

The ten tests below are R1's required fail-then-pass list, in order.
"""
import os
from dataclasses import replace

import pytest

from exitmgr.config import (Config, RulesConfig, TrailingConfig, AutoTrailConfig,
                            StateConfig, JournalConfig)
from exitmgr.connection import PositionData
from exitmgr.manager import ExitManager
from exitmgr import rules as rules_mod
from exitmgr.rules import evaluate_position, evaluate_trailing_stop
from exitmgr.state import StateManager


# entry_debit=500, qty=1 -> entry per share = 5.00
# activation +20% -> a qualifying CLOSE is >= 6.00; giveback 0.4; stop 30% -> 3.50
ED, QTY = 500.0, 1
ACT, GIVEBACK = 20.0, 0.4
QUALIFYING, SUB_THRESHOLD = 6.10, 5.80

# Real 2026 exchange sessions.  Jul 4 2026 falls on a SATURDAY, so the NYSE observes Independence
# Day on FRIDAY 2026-07-03 -- which makes Thu 07-02 -> Mon 07-06 a consecutive pair.
FRI = "2026-07-24"
MON = "2026-07-27"      # the very next session after FRI
TUE = "2026-07-28"      # the session after MON
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
    """Record one official closing mark against the standard basis."""
    return state.record_session_close(con_id, session, price, ED, QTY, ACT)


def _eval(price, rules, state, con_id, peak_price=None):
    """Evaluate exactly the way the manager does: lifetime peak for audit, persisted confirmation
    for the trail."""
    return evaluate_position(
        con_id=con_id, symbol="X", quantity=QTY, entry_debit=ED,
        current_price=price, days_to_expiry=200,
        peak_price=peak_price,
        rules=rules,
        trail_armed=state.is_trail_armed(con_id),
        peak_since_arm=state.trail_peak_since_arm(con_id),
    )


# ------------------------------------------------------- 0: pure-semantic sentinels
# These two call ONLY the pre-change public surface, so against the old code they fail on an
# ASSERTION (wrong behavior) rather than on a missing attribute. They are the honest behavioral
# proof of the fail-then-pass: everything below additionally needs new API to exist.
def test_0a_lifetime_peak_alone_never_fires_a_trail():
    """A caller that supplies only the monotonic LIFETIME peak and no confirmation state gets NO
    trailing exit.  Before R1 this exact call fired a trailing_stop armed by a single intraday
    tick: peak 6.10 (+22%) past the +20% activation, floor 6.10-0.4*1.10 = 5.66, price 5.60."""
    trig = evaluate_position(con_id=1, symbol="X", quantity=QTY, entry_debit=ED,
                             current_price=5.60, days_to_expiry=200, peak_price=6.10,
                             rules=_rules())
    assert trig is None or trig.trigger_type != "trailing_stop"


def test_0b_view_armed_bit_is_not_the_feature_toggle(tmp_path):
    """The always-armed defect, stated on its own: with the FEATURE enabled and nothing else done,
    the position-manager view reported trail_armed=True (it was literally bool(trailing.enabled))
    for a position whose price had never approached activation."""
    m = _mgr(tmp_path)
    assert m.config.rules.trailing.enabled is True
    pos = PositionData(con_id=1, symbol="X", right="C", quantity=QTY, avg_cost=5.0,
                       expiry="20270115")
    m._journal_entries[1] = {"symbol": "X", "debit": ED, "quantity": QTY}
    views = m._build_position_views([pos], {1: {"price": 5.05}})   # +1%, nowhere near +20%
    assert views[0]["trail_armed"] is False


# --------------------------------------------------------------------------------- 1
def test_1_intraday_spike_does_not_arm(tmp_path):
    """One INTRADAY +22% spike, no qualifying close: the trail must stay unarmed and must not
    fire, however far the lifetime peak ran."""
    st = _sm(tmp_path).state
    # the spike only ever touches the lifetime/MFE books
    for px in (5.20, 6.10, 5.90):
        st.record_mark(101, px, ED, QTY)
    assert st.peak_prices["101"] == 6.10          # lifetime peak recorded (audit evidence)
    assert st.is_trail_armed(101) is False        # ...and it armed exactly nothing
    assert st.trail_peak_since_arm(101) is None
    assert st.trail_confirmation_for(101)["consecutive_qualifying_closes"] == 0

    # 5.75 is BELOW the floor a lifetime-peak trail would have set (6.10 - 0.4*1.10 = 5.66 is the
    # confirmed floor; the OLD tick-armed trail floor off peak 6.10 was the same number, and a
    # deeper give-back proves the point) -- so drop to 5.60, under any such floor:
    trig = _eval(5.60, _rules(), st, 101, peak_price=6.10)
    assert trig is None or trig.trigger_type != "trailing_stop"

    # ...and the same via the unwired/default caller: no confirmation state supplied at all.
    trig2 = evaluate_position(con_id=101, symbol="X", quantity=QTY, entry_debit=ED,
                              current_price=5.60, days_to_expiry=200, peak_price=6.10,
                              rules=_rules())
    assert trig2 is None or trig2.trigger_type != "trailing_stop"


# --------------------------------------------------------------------------------- 2
def test_2_qualifying_then_sub_threshold_close_resets(tmp_path):
    """A qualifying close followed by a sub-threshold close resets the streak: still unarmed."""
    st = _sm(tmp_path).state
    rec = _close(st, 102, FRI, QUALIFYING)
    assert rec["consecutive_qualifying_closes"] == 1
    assert st.is_trail_armed(102) is False

    rec = _close(st, 102, MON, SUB_THRESHOLD)     # closed back under +20%
    assert rec["consecutive_qualifying_closes"] == 0
    assert st.is_trail_armed(102) is False
    assert st.trail_peak_since_arm(102) is None

    # and a single qualifying close AFTER the reset is only #1 again, not #2
    rec = _close(st, 102, TUE, QUALIFYING)
    assert rec["consecutive_qualifying_closes"] == 1
    assert st.is_trail_armed(102) is False

    # BOUNDARY: the contract is "AT LEAST the activation threshold". A close exactly ON the
    # threshold (5.00 * 1.20 = 6.00) qualifies; one cent under does not.
    st2 = _sm(tmp_path).state
    assert _close(st2, 122, FRI, 6.00)["consecutive_qualifying_closes"] == 1
    assert _close(st2, 122, MON, 6.00)["consecutive_qualifying_closes"] == 2
    assert st2.is_trail_armed(122) is True
    st3 = _sm(tmp_path).state
    assert _close(st3, 132, FRI, 5.99)["consecutive_qualifying_closes"] == 0
    assert st3.is_trail_armed(132) is False


# --------------------------------------------------------------------------------- 3
def test_3_two_consecutive_qualifying_closes_arm_exactly_once(tmp_path):
    """Two consecutive qualifying closes arm the trail -- once, and only once."""
    st = _sm(tmp_path).state
    _close(st, 103, FRI, QUALIFYING)
    assert st.is_trail_armed(103) is False        # one close is not enough

    rec = _close(st, 103, MON, 6.20)
    assert st.is_trail_armed(103) is True
    assert rec["consecutive_qualifying_closes"] == 2
    armed_at = rec["armed_at"]
    assert armed_at is not None
    assert st.trail_peak_since_arm(103) == 6.20   # seeded from THAT close

    # a third qualifying close must not re-arm or re-seed the floor
    rec = _close(st, 103, TUE, 6.50)
    assert rec["armed_at"] == armed_at            # armed exactly once
    assert st.trail_peak_since_arm(103) == 6.20   # ratcheting is record_trail_peak's job

    # re-recording an already-recorded session cannot double-count into an arm either
    st2 = _sm(tmp_path).state
    _close(st2, 113, FRI, QUALIFYING)
    _close(st2, 113, FRI, QUALIFYING)             # same session, replayed cycle
    assert st2.is_trail_armed(113) is False

    # ...and a STALE close arriving out of order (a late/replayed earlier session) may not rewind
    # the record either -- last_session must never move backwards.
    rec = _close(st, 103, FRI, QUALIFYING)        # FRI is older than the recorded TUE
    assert rec["last_session"] == TUE
    assert rec["consecutive_qualifying_closes"] == 2
    assert rec["armed_at"] == armed_at


# --------------------------------------------------------------------------------- 4
def test_4_calendar_adjacency_and_missing_close(tmp_path):
    """Friday/Monday are consecutive sessions; a session whose close was never recorded is a GAP
    that breaks the streak and is never inferred."""
    assert rules_mod.is_next_trading_session(FRI, MON) is True        # over a weekend
    assert rules_mod.is_next_trading_session(FRI, TUE) is False       # Monday skipped
    # Jul 4 2026 is a Saturday -> observed Friday 07-03 -> Thu 07-02 and Mon 07-06 are adjacent
    assert rules_mod.is_next_trading_session(THU_PRE_HOLIDAY, MON_POST_HOLIDAY) is True

    st = _sm(tmp_path).state
    _close(st, 104, FRI, QUALIFYING)
    _close(st, 104, MON, QUALIFYING)
    assert st.is_trail_armed(104) is True                   # Fri + Mon == consecutive

    # missing close: Friday qualified, MONDAY produced no mark at all, Tuesday qualified
    st2 = _sm(tmp_path).state
    _close(st2, 204, FRI, QUALIFYING)
    rec = _close(st2, 204, TUE, QUALIFYING)                 # Monday never recorded
    assert rec["consecutive_qualifying_closes"] == 1        # streak restarted, not continued
    assert st2.is_trail_armed(204) is False

    # a MISSING close is also never manufactured from an unusable price
    st3 = _sm(tmp_path).state
    _close(st3, 304, FRI, QUALIFYING)
    st3.record_session_close(304, MON, None, ED, QTY, ACT)          # no mark
    st3.record_session_close(304, MON, float("nan"), ED, QTY, ACT)  # unusable mark
    assert st3.is_trail_armed(304) is False
    assert st3.trail_confirmation_for(304)["last_session"] == FRI


# --------------------------------------------------------------------------------- 5
def test_5_restart_between_the_two_closes_preserves_one_confirmation(tmp_path):
    """A process bounce between the first and second qualifying close keeps the first one."""
    sm = _sm(tmp_path)
    _close(sm.state, 105, FRI, QUALIFYING)
    sm.save()

    reloaded = StateManager(sm.state_path)                  # fresh process
    assert reloaded.state.trail_confirmation_for(105)["consecutive_qualifying_closes"] == 1
    assert reloaded.state.trail_confirmation_for(105)["last_session"] == FRI
    assert reloaded.state.is_trail_armed(105) is False

    _close(reloaded.state, 105, MON, 6.20)
    assert reloaded.state.is_trail_armed(105) is True
    assert reloaded.state.trail_peak_since_arm(105) == 6.20


# --------------------------------------------------------------------------------- 6
def test_6_enabled_and_configured_but_unconfirmed_reports_unarmed(tmp_path):
    """The feature being ON and the model having asked for a trail is NOT armed.  The
    position-manager view must report the PERSISTED armed bit, never bool(trailing.enabled)."""
    m = _mgr(tmp_path)
    assert m.config.rules.trailing.enabled is True          # feature enabled
    m._apply_decision(m.config.rules, {"action": "arm_trail"}, 7.0, ED, QTY, 106, "X")
    assert m.state_manager.state.trail_configured.get("106") is True   # model configured it
    assert m.state_manager.state.is_trail_armed(106) is False          # ...but nothing armed

    pos = PositionData(con_id=106, symbol="X", right="C", quantity=QTY, avg_cost=5.0,
                       expiry="20270115")
    m._journal_entries[106] = {"symbol": "X", "debit": ED, "quantity": QTY}
    views = m._build_position_views([pos], {106: {"price": 6.30}})
    assert len(views) == 1
    v = views[0]
    assert v["trail_enabled"] is True
    assert v["trail_configured"] is True
    assert v["trail_armed"] is False        # the always-armed defect: was bool(trailing.enabled)

    # and no trailing exit exists for it, at any price above the protective stop
    trig = _eval(5.00, _rules(), m.state_manager.state, 106, peak_price=7.00)
    assert trig is None or trig.trigger_type != "trailing_stop"


# --------------------------------------------------------------------------------- 7
def test_7_pre_arm_spike_cannot_set_the_post_arm_floor(tmp_path):
    """A pre-confirmation spike to +60% must not become the floor: the floor comes from the
    SECOND qualifying close, or the position exits on noise the moment it arms."""
    st = _sm(tmp_path).state
    st.record_mark(107, 8.00, ED, QTY)                      # lifetime peak +60%, intraday
    assert st.peak_prices["107"] == 8.00

    # an explicit ratchet attempt while UNARMED is inert -- it must not create a floor
    assert st.record_trail_peak(107, 8.00) is None
    assert st.trail_peak_since_arm(107) is None
    assert "107" not in st.trail_confirmation

    _close(st, 107, FRI, QUALIFYING)
    _close(st, 107, MON, 6.10)
    assert st.is_trail_armed(107) is True
    assert st.trail_peak_since_arm(107) == 6.10             # NOT 8.00

    # floor from the arming close: 6.10 - 0.4*(6.10-5.00) = 5.66.
    # floor from the poisoned lifetime peak would be 8.00 - 0.4*3.00 = 6.80 -> an instant exit.
    trig = _eval(6.00, _rules(), st, 107, peak_price=8.00)
    assert trig is None                                     # 6.00 > 5.66: the runner lives
    trig = _eval(5.65, _rules(), st, 107, peak_price=8.00)
    assert trig is not None and trig.trigger_type == "trailing_stop"


# --------------------------------------------------------------------------------- 8
def test_8_floor_ratchets_from_peak_since_arm_and_survives_restart(tmp_path):
    """After arming the floor ratchets off peak_since_arm -- upward only -- and the ratchet is
    persisted."""
    sm = _sm(tmp_path)
    st = sm.state
    _close(st, 108, FRI, QUALIFYING)
    _close(st, 108, MON, 6.10)
    assert st.trail_peak_since_arm(108) == 6.10

    st.record_trail_peak(108, 7.00)
    assert st.trail_peak_since_arm(108) == 7.00
    st.record_trail_peak(108, 6.50)                         # ratchet never reverses
    assert st.trail_peak_since_arm(108) == 7.00

    sm.save()
    reloaded = StateManager(sm.state_path)
    assert reloaded.state.is_trail_armed(108) is True
    assert reloaded.state.trail_peak_since_arm(108) == 7.00

    # floor = 7.00 - 0.4*(7.00-5.00) = 6.20, strictly above the 5.66 floor of the arming close
    r = _rules()
    assert _eval(6.25, r, reloaded.state, 108) is None
    trig = _eval(6.19, r, reloaded.state, 108)
    assert trig is not None and trig.trigger_type == "trailing_stop"
    assert trig.pnl_pct > 0                                 # protects a gain, not a round-trip


# --------------------------------------------------------------------------------- 9
def test_9_auto_trail_cannot_bypass_the_confirmation_contract(tmp_path):
    """The auto-trail safety floor is a second arming mechanism if left ungated -- it must obey
    exactly the same two-close contract."""
    m = _mgr(tmp_path)
    auto = AutoTrailConfig(enabled=True, activation_gain_pct=25.0, giveback_fraction=0.5)
    st = m.state_manager.state
    base = replace(m.config.rules, trailing=TrailingConfig(enabled=False))

    # lifetime peak 8.00 (+60%, well past the auto activation) but NO confirmed close
    st.record_mark(109, 8.00, ED, QTY)
    out, widened = m._apply_auto_trail(base, auto, 8.00, ED, QTY,
                                       armed=st.is_trail_armed(109),
                                       peak_since_arm=st.trail_peak_since_arm(109))
    assert widened is False
    assert out is base                                      # exact no-op
    trig = _eval(5.50, out, st, 109, peak_price=8.00)
    assert trig is None or trig.trigger_type != "trailing_stop"

    # ...and it stays a no-op even when handed a ratchet price, if the arm is absent: the armed
    # bit is its own gate, not a proxy for "peak_since_arm exists".
    out, widened = m._apply_auto_trail(base, auto, 8.00, ED, QTY,
                                       armed=False, peak_since_arm=8.00)
    assert widened is False and out is base

    # ...and once armed it measures its activation from peak_since_arm, NOT the lifetime peak:
    # lifetime 8.00 is +60% (past the +25% auto activation) but the post-arm ratchet is only +22%.
    out, widened = m._apply_auto_trail(base, auto, 8.00, ED, QTY,
                                       armed=True, peak_since_arm=6.10)
    assert widened is False and out is base

    # once CONFIRMED, auto-trail may widen -- and only then
    _close(st, 109, FRI, QUALIFYING)
    _close(st, 109, MON, 6.10)
    st.record_trail_peak(109, 8.00)
    out2, widened2 = m._apply_auto_trail(base, auto, 8.00, ED, QTY,
                                         armed=st.is_trail_armed(109),
                                         peak_since_arm=st.trail_peak_since_arm(109))
    assert widened2 is True
    assert out2.trailing.enabled is True
    assert out2.trailing.giveback_fraction == 0.5
    assert out2.profit_target_pct == base.profit_target_pct  # never suppresses the ceiling


# --------------------------------------------------------------------------------- 10
def test_10_close_and_prune_clear_all_trail_state(tmp_path):
    """Closing or pruning a position removes the confirmation, the armed state and
    peak_since_arm, so a re-entry of the same conId can never inherit an arm."""
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

    # clear_trail_state on its own clears both books
    _close(st, 310, FRI, QUALIFYING)
    _close(st, 310, MON, 6.10)
    st.trail_configured["310"] = True
    assert st.is_trail_armed(310) is True
    st.clear_trail_state(310)
    assert "310" not in st.trail_confirmation
    assert "310" not in st.trail_configured
    assert st.is_trail_armed(310) is False

    # prune_tracking (the housekeeping path) clears the same four fields
    _close(st, 210, FRI, QUALIFYING)
    _close(st, 210, MON, 6.10)
    st.trail_configured["210"] = True
    assert st.is_trail_armed(210) is True
    st.prune_tracking(active_con_ids=[999])
    assert st.is_trail_armed(210) is False
    assert "210" not in st.trail_confirmation
    assert "210" not in st.trail_configured


# ------------------------------------------------------------------ MIGRATION (live state file)
def test_migration_old_state_loads_unarmed_with_no_mfe_backfill(tmp_path):
    """A state file written before this change -- lifetime peaks, a fat MFE, the legacy
    `trail_armed` flag -- must load UNARMED, with the model-configured flag preserved."""
    import json
    p = os.path.join(str(tmp_path), "state.json")
    with open(p, "w") as f:
        json.dump({"in_flight": {}, "daily_stats": {}, "last_cycle": None,
                   "peak_prices": {"900": 9.99}, "mfe_pct": {"900": 99.0},
                   "mae_pct": {}, "mfe_ts": {}, "mae_ts": {}, "mark_path": {},
                   "scaled_out": {}, "trail_armed": {"900": True}}, f)
    st = StateManager(p).state
    assert st.trail_configured.get("900") is True   # legacy flag survives (ceiling stays relaxed)
    assert st.is_trail_armed(900) is False          # ...but nothing is armed
    assert st.trail_peak_since_arm(900) is None
    assert st.peak_prices["900"] == 9.99            # audit evidence untouched
    assert st.trail_confirmation == {}              # no backfill from peak/MFE


def test_migration_round_trips_and_mirrors_the_legacy_key(tmp_path):
    """The saved file keeps the legacy key mirrored so a rollback still finds the configured
    flag, and a corrupt/partial confirmation degrades to UNARMED rather than armed."""
    import json
    sm = _sm(tmp_path)
    sm.state.trail_configured["901"] = True
    _close(sm.state, 902, FRI, QUALIFYING)
    _close(sm.state, 902, MON, 6.10)
    sm.save()
    with open(sm.state_path) as f:
        raw = json.load(f)
    assert raw["trail_configured"] == {"901": True}
    assert raw["trail_armed"] == {"901": True}      # rollback mirror
    assert raw["trail_confirmation"]["902"]["peak_since_arm"] == 6.10

    # armed_at with no ratchet price is not a usable arm
    raw["trail_confirmation"]["903"] = {"last_session": FRI, "consecutive_qualifying_closes": 2,
                                        "armed_at": "2026-07-24T16:05:00-04:00",
                                        "peak_since_arm": None}
    raw["trail_confirmation"]["904"] = "garbage"
    with open(sm.state_path, "w") as f:
        json.dump(raw, f)
    st = StateManager(sm.state_path).state
    assert st.is_trail_armed(903) is False
    assert st.is_trail_armed(904) is False
    assert st.is_trail_armed(902) is True


# ------------------------------------------------------------- post-close recording window
def test_session_close_recorded_only_from_an_official_post_close_mark(tmp_path):
    """Only a broker mark read in the post-close window of a real trading day may advance the
    confirmation; a streaming-quote fallback, mid-session, a weekend or a holiday may not."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    m = _mgr(tmp_path)

    assert ExitManager._completed_session_date(datetime(2026, 7, 24, 16, 5, tzinfo=et)) == FRI
    assert ExitManager._completed_session_date(datetime(2026, 7, 24, 12, 0, tzinfo=et)) is None
    assert ExitManager._completed_session_date(datetime(2026, 7, 25, 17, 0, tzinfo=et)) is None
    assert ExitManager._completed_session_date(datetime(2026, 7, 3, 17, 0, tzinfo=et)) is None

    post_close = datetime(2026, 7, 24, 16, 5, tzinfo=et)
    # streaming-quote fallback -> a MISSING close, never recorded
    m._maybe_record_session_close(120, QUALIFYING, ED, QTY, is_official_mark=False,
                                  now=post_close)
    assert m.state_manager.state.trail_confirmation_for(120)["last_session"] is None
    # broker mark -> recorded
    m._maybe_record_session_close(120, QUALIFYING, ED, QTY, is_official_mark=True, now=post_close)
    assert m.state_manager.state.trail_confirmation_for(120)["last_session"] == FRI
    # replayed cycles inside the same window never double-count into an arm
    for _ in range(5):
        m._maybe_record_session_close(120, QUALIFYING, ED, QTY, is_official_mark=True,
                                      now=post_close)
    assert m.state_manager.state.is_trail_armed(120) is False


def test_armed_bit_gates_independently_of_the_ratchet_price(tmp_path):
    """Defense in depth: even handed a perfectly good post-arm ratchet price, evaluate_position
    must refuse to fire while trail_armed is False.  The two conditions are independent gates, not
    one condition expressed twice."""
    trig = evaluate_position(con_id=1, symbol="X", quantity=QTY, entry_debit=ED,
                             current_price=5.60, days_to_expiry=200, peak_price=7.00,
                             rules=_rules(), trail_armed=False, peak_since_arm=7.00)
    assert trig is None or trig.trigger_type != "trailing_stop"
    trig = evaluate_position(con_id=1, symbol="X", quantity=QTY, entry_debit=ED,
                             current_price=5.60, days_to_expiry=200, peak_price=7.00,
                             rules=_rules(), trail_armed=True, peak_since_arm=7.00)
    assert trig is not None and trig.trigger_type == "trailing_stop"


def test_evaluate_trailing_stop_requires_explicit_armed_state():
    """The pure rule refuses to fire without the explicit armed flag, whatever the peak says."""
    assert evaluate_trailing_stop(5.60, ED, QTY, 6.10, ACT, GIVEBACK, armed=False) is None
    assert evaluate_trailing_stop(5.60, ED, QTY, None, ACT, GIVEBACK, armed=True) is None
    assert evaluate_trailing_stop(5.60, ED, QTY, 6.10, ACT, GIVEBACK, armed=True) is not None


# ------------------------------------------------------- END TO END through the real exit cycle
# Drives manager.run_cycle with the broker and order layer mocked (the harness pattern from
# tests/test_scale_out_hook.py): NO broker is touched and NO order is placed -- we capture what
# place_close_order was handed. This is the only test that exercises the eval loop's own wiring of
# the confirmation state, so it is what stops the loop from quietly passing armed=True.
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
    """Manager with ONLY the trail live: no take-profit ceiling, no scale-out, no auto-trail --
    so anything that closes the position can only be the trailing stop (or the -30% stop)."""
    cfg = Config()
    cfg.dry_run = False
    cfg.loop_mode = False
    cfg.journal.path = str(tmp_path / "trades.log")
    cfg.state.path = str(tmp_path / "state.json")
    cfg.kill_switch.path = str(tmp_path / "KILL")
    cfg.audit_path = str(tmp_path / "audit.jsonl")
    cfg.manage_positions = False            # skip the LLM assessment call
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
    mgr.ib_conn.ib.portfolio = lambda: []       # no server marking -> the quote is used
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
    """A winner that spiked to +22% INTRADAY and faded gets no trailing exit from the live loop.
    Cycle 1 sets the lifetime peak to 6.10; cycle 2 fades to 5.60, which is below the floor a
    lifetime-peak trail would have set (6.10 - 0.4*1.10 = 5.66).  Nothing may be closed."""
    mgr = _e2e_mgr(tmp_path)
    _e2e_wire(mgr, 6.10)
    await mgr.run_cycle(dry_run=False)
    assert mgr.state_manager.state.peak_prices[str(E2E_CON)] == 6.10   # peak recorded
    assert mgr.state_manager.state.is_trail_armed(E2E_CON) is False    # nothing armed

    place = _e2e_wire(mgr, 5.60)
    await mgr.run_cycle(dry_run=False)
    place.assert_not_called()                                          # the runner lives


@pytest.mark.asyncio
async def test_e2e_confirmed_winner_is_trailed_out_by_the_real_cycle(tmp_path):
    """The same fade, once the two-close confirmation exists, DOES exit -- the mechanism works, it
    is merely gated.  peak_since_arm 6.10 -> floor 6.10 - 0.4*1.10 = 5.66; 5.60 is below it."""
    mgr = _e2e_mgr(tmp_path)
    st = mgr.state_manager.state
    _close(st, E2E_CON, FRI, QUALIFYING)
    _close(st, E2E_CON, MON, 6.10)
    assert st.is_trail_armed(E2E_CON) is True

    place = _e2e_wire(mgr, 5.60)
    await mgr.run_cycle(dry_run=False)
    place.assert_called_once()
    assert place.call_args.kwargs.get("quantity", 4) == 4               # full exit, not a trim
