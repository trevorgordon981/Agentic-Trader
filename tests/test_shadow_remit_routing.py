"""Public API contract; production-derived narrative omitted."""
import asyncio
import json
import os
import time
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from exitmgr import atr_cache
from exitmgr.config import Config, TrailingConfig
from exitmgr.connection import IBConnection
from exitmgr.construction import TP_POLICY_CURRENT
from exitmgr.manager import ExitManager



CID = 901
QTY = 2
ENTRY_DEBIT = 820.0
RED_MARK = 3.60
GREEN_MARK = 4.50
FAR_DTE = 120
NEAR_DTE = 8
TIME_STOP_DAYS = 15


def _expiry(days):
    return (datetime.now(timezone.utc).date() + timedelta(days=days)).strftime("%Y%m%d")


def _days_ago(n):
    return (datetime.now(timezone.utc) - timedelta(days=float(n))).isoformat()


def _journal(**over):
    je = {"ts": _days_ago(3), "contract_id": CID, "symbol": "RKLB", "right": "C",
          "strike": 20.0, "expiry": _expiry(FAR_DTE), "quantity": QTY, "debit": ENTRY_DEBIT,
          "conviction": 6, "decision_id": "dec-t5-1", "intended_hold_days": 30}
    je.update(over)
    return je


def _raw_position(expiry):
    p = types.SimpleNamespace()
    p.contract = types.SimpleNamespace(conId=CID, symbol="RKLB", right="C", secType="OPT",
                                       strike=20.0, lastTradeDateOrContractMonth=expiry, multiplier="100")
    p.position = QTY
    p.avgCost = ENTRY_DEBIT / QTY
    return p


def _portfolio_mark(price):
    return types.SimpleNamespace(contract=types.SimpleNamespace(conId=CID),
                                 marketPrice=price, position=QTY)


def _decision(action, **over):
    d = {"action": action, "reason": f"unit-test {action}"}
    if action == "arm_trail":
        d["trail_activation_gain_pct"] = 40.0
        d["trail_giveback_fraction"] = 0.9
    d.update(over)
    return d


def _mgr(tmp_path, monkeypatch, name, *, shadow, dte, mark, decision=None, bid=None,
         stale=False, je_over=None, trail_configured=False, state_dir=None,
         trailing=None, manage=True):
    """Public API contract; production-derived narrative omitted."""
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    sd = state_dir or d
    cfg = Config()
    cfg.dry_run = False
    cfg.loop_mode = False
    cfg.manage_positions = manage
    cfg.manage_positions_shadow_only = shadow
    cfg.journal.path = str(d / "trades.log")
    cfg.state.path = str(sd / "state.json")
    cfg.kill_switch.path = str(d / "KILL")
    cfg.rules.stop_pct = 30.0
    cfg.rules.time_stop_days = TIME_STOP_DAYS




    cfg.rules.exit_market_orders = True
    cfg.rules.profit_target_pct = None
    cfg.rules.trailing = trailing or TrailingConfig(enabled=False, activation_gain_pct=30.0,
                                                    giveback_fraction=0.3)
    expiry = _expiry(dte)
    je = _journal(expiry=expiry, **(je_over or {}))
    (d / "trades.log").write_text(json.dumps(je) + "\n")

    mgr = ExitManager(cfg)
    conn = IBConnection(host="h", port=1, client_id=2)
    conn._connected = True
    conn.ib = MagicMock()
    conn.ib.reqPositionsAsync = AsyncMock(return_value=[_raw_position(expiry)])
    conn.ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[])
    conn.ib.reqOpenOrdersAsync = AsyncMock(return_value=[])
    conn.ib.portfolio = MagicMock(return_value=[_portfolio_mark(mark)])
    conn.get_open_orders = AsyncMock(return_value={})
    quote = {"price": mark, "bid": bid, "ask": (mark + 0.10)}
    conn.fetch_quotes = AsyncMock(return_value={CID: quote})
    conn.create_contract = MagicMock(
        side_effect=lambda cid, symbol=None, right=None: types.SimpleNamespace(
            conId=cid, symbol=symbol, right=right, secType="OPT"))
    conn.create_limit_order = MagicMock(
        side_effect=lambda action, qty, px: types.SimpleNamespace(
            action=action, totalQuantity=qty, lmtPrice=px, orderType="LMT", orderId=0))
    conn.create_market_order = MagicMock(
        side_effect=lambda action, qty: types.SimpleNamespace(
            action=action, totalQuantity=qty, lmtPrice=0.0, orderType="MKT", orderId=0))
    conn.reserve_order_id = MagicMock(return_value=77)
    mgr.ib_conn = conn
    mgr.order_manager.ib_conn = conn
    mgr._reconcile_on_startup = AsyncMock(return_value=True)
    mgr._capture_external_fills_safe = AsyncMock(return_value=None)
    mgr._alert_unfilled_orders = AsyncMock(return_value=None)
    mgr._maybe_record_session_close = lambda *a, **k: None
    monkeypatch.setattr(atr_cache, "read", lambda symbol, *a, **k: None)

    if trail_configured:
        mgr.state_manager.state.trail_configured[str(CID)] = True
        mgr.state_manager.save()
    if decision is not None:
        mgr._mgmt_cache = {
            "decisions": {CID: dict(decision)},
            "raw": '{"decisions": {}}',
            "model_identity": "unit-test-model",
            "model_identity_source": "config",
            "ts": (time.time() - 100000.0) if stale else time.time(),
            "assessed_cids": {CID},
            "qty_by_cid": {CID: QTY},
        }
    return mgr


def _spy(mgr, mark=0.0):
    """Public API contract; production-derived narrative omitted."""
    sent, intents = [], []
    real = mgr.order_manager.place_close_order

    async def _place_order(contract, order):
        sent.append({"orderType": getattr(order, "orderType", None),
                     "totalQuantity": getattr(order, "totalQuantity", None),
                     "lmtPrice": getattr(order, "lmtPrice", None),
                     "orderRef": getattr(order, "orderRef", None),
                     "action": getattr(order, "action", None),
                     "conId": getattr(contract, "conId", None)})



        return types.SimpleNamespace(
            order=order, log=[],
            orderStatus=types.SimpleNamespace(status="Filled", filled=float(QTY),
                                              remaining=0.0, avgFillPrice=float(mark)))

    async def _pco(**kw):
        intents.append(kw)
        return await real(**kw)

    mgr.ib_conn.place_order = _place_order
    mgr.order_manager.place_close_order = _pco
    return sent, intents


def _run(mgr):
    asyncio.run(mgr.run_cycle(dry_run=False, defer_model=True))


def _assert_evaluated(mgr):
    assert str(CID) in mgr.state_manager.state.peak_prices, (
        "the position was never evaluated -- any 'nothing happened' assertion is vacuous")




def test_the_remit_is_hold_or_cut_and_nothing_else():
    assert ExitManager.MGMT_REMIT_ACTIONS == ("hold", "cut")
    for gone in ("arm_trail", "take_profit", "tighten_stop"):
        assert gone not in ExitManager.MGMT_REMIT_ACTIONS


@pytest.mark.parametrize("action", ["arm_trail", "take_profit", "tighten_stop", "reload"])
def test_an_out_of_remit_proposal_is_neutered_not_dropped(action):
    """Public API contract; production-derived narrative omitted."""
    out, refused = ExitManager._narrow_to_remit({CID: _decision(action)})
    assert refused == [CID]
    assert CID in out, "dropping it would read downstream as an implicit hold -- invented evidence"
    assert out[CID]["action"] == ExitManager.MGMT_OUT_OF_REMIT
    assert out[CID]["proposed_action"] == action
    assert out[CID]["reason"] == f"unit-test {action}"


@pytest.mark.parametrize("action", ["hold", "cut"])
def test_an_in_remit_proposal_passes_through_untouched(action):
    out, refused = ExitManager._narrow_to_remit({CID: _decision(action)})
    assert refused == []
    assert out[CID]["action"] == action
    assert "proposed_action" not in out[CID]


def test_narrowing_is_idempotent_and_never_overwrites_the_original_proposal():
    """Public API contract; production-derived narrative omitted."""
    once, _ = ExitManager._narrow_to_remit({CID: _decision("arm_trail")})
    twice, refused = ExitManager._narrow_to_remit(once)
    assert twice[CID]["proposed_action"] == "arm_trail"
    assert twice[CID]["action"] == ExitManager.MGMT_OUT_OF_REMIT
    assert refused == []


def test_a_refused_action_is_inert_in_every_authority_branch():
    """Public API contract; production-derived narrative omitted."""
    marker = ExitManager.MGMT_OUT_OF_REMIT
    assert marker not in ExitManager.MGMT_ALERT_ACTIONS
    rules = Config().rules
    out_rules, forced = ExitManager._apply_decision(
        MagicMock(), rules, {"action": marker, "proposed_action": "take_profit"},
        4.0, ENTRY_DEBIT, QTY, CID, "RKLB")
    assert forced is None, "a refused take_profit must not force a close"
    assert out_rules is rules
    kept = ExitManager._reconcile_ceiling_backstop(
        Config().rules.__class__(profit_target_pct=150.0),
        {"action": marker, "proposed_action": "arm_trail"}, configured=False)
    assert kept.profit_target_pct == 150.0, "a refused arm_trail must not suppress the ceiling"






MATRIX = [

    ("on-red-empty",        True,  NEAR_DTE, RED_MARK,  None,          False, ("MKT", None,  "time_stop")),
    ("on-red-hold",         True,  NEAR_DTE, RED_MARK,  "hold",        False, ("MKT", None,  "time_stop")),
    ("on-red-arm",          True,  NEAR_DTE, RED_MARK,  "arm_trail",   False, ("MKT", None,  "time_stop")),
    ("on-red-cut",          True,  NEAR_DTE, RED_MARK,  "cut",         False, ("MKT", None,  "time_stop")),
    ("on-red-tp",           True,  NEAR_DTE, RED_MARK,  "take_profit", False, ("MKT", None,  "time_stop")),
    ("on-green-hold",       True,  NEAR_DTE, GREEN_MARK,"hold",        False, ("LMT", GREEN_MARK, "time_stop")),
    ("on-green-arm",        True,  NEAR_DTE, GREEN_MARK,"arm_trail",   False, ("LMT", GREEN_MARK, "time_stop")),
    ("on-green-cut",        True,  NEAR_DTE, GREEN_MARK,"cut",         False, ("LMT", GREEN_MARK, "time_stop")),
    ("on-far-cut",          True,  FAR_DTE,  RED_MARK,  "cut",         False, None),
    ("on-far-hold",         True,  FAR_DTE,  RED_MARK,  "hold",        False, None),
    ("on-red-cut-stale",    True,  NEAR_DTE, RED_MARK,  "cut",         True,  ("MKT", None,  "time_stop")),

    ("off-red-empty",       False, NEAR_DTE, RED_MARK,  None,          False, ("LMT", RED_MARK, "time_stop")),
    ("off-red-hold",        False, NEAR_DTE, RED_MARK,  "hold",        False, ("LMT", RED_MARK, "time_stop")),
    ("off-red-arm",         False, NEAR_DTE, RED_MARK,  "arm_trail",   False, ("MKT", None,  "time_stop")),
    ("off-red-tp",          False, NEAR_DTE, RED_MARK,  "take_profit", False, ("MKT", None,  "time_stop")),
    ("off-red-cut",         False, NEAR_DTE, RED_MARK,  "cut",         False, ("MKT", None,  "model_cut")),
    ("off-red-cut-stale",   False, NEAR_DTE, RED_MARK,  "cut",         True,  ("LMT", RED_MARK, "time_stop")),
    ("off-far-cut",         False, FAR_DTE,  RED_MARK,  "cut",         False, ("MKT", None,  "model_cut")),
    ("off-far-arm",         False, FAR_DTE,  RED_MARK,  "arm_trail",   False, None),
    ("off-far-hold",        False, FAR_DTE,  RED_MARK,  "hold",        False, None),
]


@pytest.mark.parametrize("name,shadow,dte,mark,action,stale,expect",
                         MATRIX, ids=[r[0] for r in MATRIX])
def test_matrix(tmp_path, monkeypatch, name, shadow, dte, mark, action, stale, expect):
    mgr = _mgr(tmp_path, monkeypatch, name.replace("-", "_"), shadow=shadow, dte=dte, mark=mark,
               decision=(None if action is None else _decision(action)), stale=stale)
    sent, intents = _spy(mgr, mark)
    _run(mgr)
    if expect is None:


        _assert_evaluated(mgr)
        assert sent == [], f"{name}: nothing should have been transmitted, got {sent}"
        return
    otype, price, trigger_type = expect
    assert len(sent) == 1, f"{name}: expected exactly one transmitted order, got {sent}"
    o = sent[0]
    assert o["orderType"] == otype, f"{name}: transmitted {o['orderType']}, expected {otype}"
    assert o["action"] == "SELL"
    assert o["totalQuantity"] == QTY, f"{name}: closed {o['totalQuantity']} of {QTY}"
    assert o["conId"] == CID
    assert isinstance(o["orderRef"], str) and o["orderRef"].startswith(f"exitmgr-{CID}-"), (
        f"{name}: durable order identity missing ({o['orderRef']!r})")
    if price is None:
        assert o["lmtPrice"] in (0.0, None), f"{name}: a MKT order must carry no limit"
    else:
        assert o["lmtPrice"] == pytest.approx(price), f"{name}: limit {o['lmtPrice']} != {price}"
    assert intents[0]["trigger_type"] == trigger_type, (
        f"{name}: fired as {intents[0]['trigger_type']}, expected {trigger_type}")


def test_shadow_takes_the_guaranteed_fill_on_a_losing_time_stop_and_off_does_not(
        tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    on = _mgr(tmp_path, monkeypatch, "gf_on", shadow=True, dte=NEAR_DTE, mark=RED_MARK)
    on_sent, _ = _spy(on)
    _run(on)
    off = _mgr(tmp_path, monkeypatch, "gf_off", shadow=False, dte=NEAR_DTE, mark=RED_MARK)
    off_sent, _ = _spy(off)
    _run(off)
    assert on_sent[0]["orderType"] == "MKT"
    assert off_sent[0]["orderType"] == "LMT" and off_sent[0]["lmtPrice"] == pytest.approx(RED_MARK)


def test_a_live_bid_prices_the_guaranteed_fill_at_the_bid(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    mgr = _mgr(tmp_path, monkeypatch, "bid_on", shadow=True, dte=NEAR_DTE, mark=RED_MARK,
               bid=3.50)
    sent, _ = _spy(mgr)
    _run(mgr)
    assert sent[0]["orderType"] == "LMT"
    assert sent[0]["lmtPrice"] == pytest.approx(3.50)




def test_persisted_trail_configured_no_longer_suppresses_the_ceiling_after_a_restart(
        tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    shared = tmp_path / "shared_state"
    shared.mkdir()
    je = {"tp_policy": TP_POLICY_CURRENT, "profit_target_pct": 100.0,


          "ts": "2026-08-23T00:00:00+00:00"}

    first = _mgr(tmp_path, monkeypatch, "restart_1", shadow=True, dte=FAR_DTE, mark=9.00,
                 je_over=je, trail_configured=True, state_dir=shared)
    assert first.state_manager.state.trail_configured[str(CID)] is True

    second = _mgr(tmp_path, monkeypatch, "restart_2", shadow=True, dte=FAR_DTE, mark=9.00,
                  je_over=je, state_dir=shared)
    assert second.state_manager.state.trail_configured.get(str(CID)) is True, (
        "fixture is wrong: the flag did not survive the restart, so this proves nothing")
    sent, intents = _spy(second)
    _run(second)
    assert len(sent) == 1, "the fixed take-profit ceiling was still suppressed by stale state"
    assert intents[0]["trigger_type"] == "profit_target"
    assert sent[0]["orderType"] == "LMT" and sent[0]["lmtPrice"] == pytest.approx(9.00)
    assert sent[0]["totalQuantity"] == QTY

    assert second.state_manager.state.trail_configured.get(str(CID)) is True, (
        "the resolution is to ignore the flag, not to erase the experiment's evidence")


def test_the_ignore_is_logged_once_per_position_not_once_per_cycle(tmp_path, monkeypatch, capsys):
    mgr = _mgr(tmp_path, monkeypatch, "ignore_log", shadow=True, dte=FAR_DTE, mark=RED_MARK,
               trail_configured=True)
    _spy(mgr)
    _run(mgr)
    _run(mgr)
    out = capsys.readouterr().out
    assert out.count("IGNORING persisted") == 1, "a 30s loop must not turn a notice into a firehose"


def test_the_flag_still_governs_when_the_model_legitimately_has_authority(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    rules = Config().rules.__class__(profit_target_pct=150.0)
    assert ExitManager._reconcile_ceiling_backstop(rules, None, configured=True) \
        .profit_target_pct is None
    assert ExitManager._reconcile_ceiling_backstop(rules, None, configured=False) \
        .profit_target_pct == 150.0


def test_model_trail_state_has_no_authority_under_shadow_or_the_narrowed_remit(tmp_path,
                                                                               monkeypatch):
    on = _mgr(tmp_path, monkeypatch, "auth_on", shadow=True, dte=FAR_DTE, mark=RED_MARK)
    off = _mgr(tmp_path, monkeypatch, "auth_off", shadow=False, dte=FAR_DTE, mark=RED_MARK)
    assert on._model_trail_state_has_authority() is False

    assert off._model_trail_state_has_authority() is False




class _T:
    def __init__(self, act, gb):
        self.activation_gain_pct = act
        self.giveback_fraction = gb


@pytest.mark.parametrize("pin,rules,expect", [

    ({"activation_gain_pct": 40.0, "giveback_fraction": 0.9}, (30.0, 0.3), (30.0, 0.3)),

    ({"activation_gain_pct": 20.0, "giveback_fraction": 0.15}, (30.0, 0.3), (20.0, 0.15)),

    ({"activation_gain_pct": 30.0, "giveback_fraction": 0.3}, (30.0, 0.3), (30.0, 0.3)),

    ({"activation_gain_pct": None, "giveback_fraction": 0.2}, (30.0, 0.3), (30.0, 0.2)),
    ({}, (30.0, 0.3), (30.0, 0.3)),
])
def test_a_persisted_pin_may_only_protect_more_than_the_rules(pin, rules, expect):
    assert ExitManager._bound_pinned_trail(pin, _T(*rules)) == expect


def test_the_pin_bound_survives_garbage(tmp_path):
    assert ExitManager._bound_pinned_trail(
        {"activation_gain_pct": "nope", "giveback_fraction": float("nan")},
        _T(30.0, 0.3)) == (30.0, 0.3)




def _ledger(mgr):
    p = mgr._shadow_experiment_file
    return json.load(open(p)) if os.path.exists(p) else {}


def test_repeated_thirty_second_marks_are_one_episode_not_many(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    mgr = _mgr(tmp_path, monkeypatch, "episodes", shadow=True, dte=FAR_DTE, mark=RED_MARK,
               decision=_decision("cut"))
    sent, _ = _spy(mgr)
    for _ in range(5):
        _run(mgr)
    assert sent == [], "a shadow cut must never transmit"
    eps = _ledger(mgr).get("episodes") or {}
    assert list(eps) == ["dec-t5-1"], eps
    assert eps["dec-t5-1"]["symbol"] == "RKLB"
    assert eps["dec-t5-1"]["entry_week"], "the predeclared entry-week cluster must be recorded"


def test_an_episode_needs_a_real_disagreement(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    mgr = _mgr(tmp_path, monkeypatch, "agreed", shadow=True, dte=NEAR_DTE, mark=RED_MARK,
               decision=_decision("cut"))
    sent, _ = _spy(mgr)
    _run(mgr)
    assert len(sent) == 1 and sent[0]["orderType"] == "MKT"
    assert (_ledger(mgr).get("episodes") or {}) == {}


def test_a_refused_proposal_is_never_counted_as_a_divergence(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, monkeypatch, "arm_noep", shadow=True, dte=FAR_DTE, mark=RED_MARK,
               decision=_decision("arm_trail"))
    _spy(mgr)
    _run(mgr)
    assert (_ledger(mgr).get("episodes") or {}) == {}


def test_divergences_are_only_counted_in_shadow(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    mgr = _mgr(tmp_path, monkeypatch, "live_noep", shadow=False, dte=FAR_DTE, mark=RED_MARK,
               decision=_decision("cut"))
    sent, _ = _spy(mgr)
    _run(mgr)
    assert len(sent) == 1
    assert (_ledger(mgr).get("episodes") or {}) == {}


def _closes(mgr, n, *, day="2026-08-21", start=5000):
    """Public API contract; production-derived narrative omitted."""
    with open(mgr._exits_log_path(), "a") as fh:
        for i in range(n):
            fh.write(json.dumps({"ts": f"{day}T12:00:0{i % 10}-07:00", "contract_id": start + i,
                                 "symbol": "X", "realized_pnl": -10.0,
                                 "realized_pnl_pct": -5.0, "exit_price_per_share": 1.0,
                                 "reason": "stop"}) + "\n")


def test_the_cap_retires_the_experiment_at_sixty_completed_closes(tmp_path, monkeypatch, capsys):
    mgr = _mgr(tmp_path, monkeypatch, "cap_closes", shadow=True, dte=FAR_DTE, mark=RED_MARK)
    assert mgr._shadow_experiment_status()["retire"] is False
    _closes(mgr, ExitManager.SHADOW_MAX_CLOSES - 1, day="2027-01-01")
    st = mgr._shadow_experiment_status()
    assert st["closes"] == 59 and st["retire"] is False, st
    _closes(mgr, 1, day="2027-01-02", start=6000)
    st = mgr._shadow_experiment_status()
    assert st["closes"] == 60 and st["expired"] is True and st["retire"] is True
    assert "operationally redundant" in st["reason"]

    called = []
    monkeypatch.setattr("exitmgr.manager.assess_positions",
                        lambda *a, **k: called.append(1) or ({}, {}))
    mgr.publish_mgmt_views([{"con_id": CID, "symbol": "RKLB"}], regime={"regime": "bull"})
    assert asyncio.run(mgr.assess_positions_offcycle()) is False
    assert called == [], "a retired experiment must not keep calling the model"
    assert "RETIRED" in capsys.readouterr().out
    assert _ledger(mgr).get("retired_at"), "retirement must be durable across a restart"


def test_a_scale_out_trim_and_its_runner_close_count_as_one(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    mgr = _mgr(tmp_path, monkeypatch, "cap_dedupe", shadow=True, dte=FAR_DTE, mark=RED_MARK)
    with open(mgr._exits_log_path(), "a") as fh:
        for _ in range(3):
            fh.write(json.dumps({"ts": "2027-01-01T12:00:00-07:00", "contract_id": 7001,
                                 "realized_pnl": -1.0, "reason": "scale_out",
                                 "exit_price_per_share": 1.0}) + "\n")
    assert mgr._shadow_experiment_status()["closes"] == 1


def test_a_phantom_close_is_not_a_sample(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    mgr = _mgr(tmp_path, monkeypatch, "cap_phantom", shadow=True, dte=FAR_DTE, mark=RED_MARK)
    with open(mgr._exits_log_path(), "a") as fh:
        fh.write(json.dumps({"ts": "2027-01-01T12:00:00-07:00", "contract_id": 7100,
                             "realized_pnl": -100.0, "realized_pnl_pct": -100.0,
                             "exit_price_per_share": 0.0, "reason": "stop"}) + "\n")
        fh.write(json.dumps({"ts": "2027-01-01T12:00:00-07:00", "contract_id": 7101,
                             "realized_pnl": -100.0, "realized_pnl_pct": -100.0,
                             "exit_price_per_share": 0.0, "reason": "expired"}) + "\n")
    assert mgr._shadow_experiment_status()["closes"] == 1


def test_the_cap_retires_the_experiment_at_ninety_days(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, monkeypatch, "cap_days", shadow=True, dte=FAR_DTE, mark=RED_MARK)
    led = mgr._load_shadow_experiment()
    led["started_at"] = (datetime.now().astimezone() - timedelta(days=91)).isoformat()
    led.setdefault("episodes", {})
    mgr._save_shadow_experiment(led)
    st = mgr._shadow_experiment_status()
    assert st["days"] >= 90 and st["retire"] is True


def test_twelve_episodes_at_the_cap_reads_as_gate_review_not_redundancy(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, monkeypatch, "cap_gate", shadow=True, dte=FAR_DTE, mark=RED_MARK)
    led = mgr._load_shadow_experiment()
    led["started_at"] = (datetime.now().astimezone() - timedelta(days=91)).isoformat()
    led["episodes"] = {f"c{i}": {"symbol": f"S{i}", "entry_week": f"2026-W{10 + i}"}
                       for i in range(12)}
    mgr._save_shadow_experiment(led)
    st = mgr._shadow_experiment_status()
    assert st["episodes"] == 12 and st["symbols"] == 12 and st["entry_weeks"] == 12
    assert st["retire"] is True and "gate review required" in st["reason"]


def test_a_recorded_gate_pass_keeps_the_experiment_alive(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    mgr = _mgr(tmp_path, monkeypatch, "cap_pass", shadow=True, dte=FAR_DTE, mark=RED_MARK)
    led = mgr._load_shadow_experiment()
    led["started_at"] = (datetime.now().astimezone() - timedelta(days=200)).isoformat()
    led["gate_passed"] = True
    mgr._save_shadow_experiment(led)
    st = mgr._shadow_experiment_status()
    assert st["expired"] is True and st["retire"] is False


def test_the_cap_never_touches_a_non_shadow_deployment(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, monkeypatch, "cap_live", shadow=False, dte=FAR_DTE, mark=RED_MARK)
    _closes(mgr, 80, day="2027-01-01")
    st = mgr._shadow_experiment_status()
    assert st["retire"] is False and st["active"] is False


def test_experiment_bookkeeping_failure_never_retires_or_raises(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    mgr = _mgr(tmp_path, monkeypatch, "cap_soft", shadow=True, dte=FAR_DTE, mark=RED_MARK)
    monkeypatch.setattr(ExitManager, "_completed_closes_since",
                        lambda self, started: (_ for _ in ()).throw(RuntimeError("boom")))
    st = mgr._shadow_experiment_status()
    assert st["retire"] is False




def test_the_refused_proposal_is_preserved_on_the_captured_mark(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    mgr = _mgr(tmp_path, monkeypatch, "capture", shadow=False, dte=FAR_DTE, mark=RED_MARK,
               decision=_decision("arm_trail"))
    _spy(mgr)
    _run(mgr)
    marks = mgr.state_manager.state.mark_path[str(CID)]
    assert marks[-1]["mgmt_action"] == ExitManager.MGMT_OUT_OF_REMIT
    assert marks[-1]["mgmt_proposed_action"] == "arm_trail"
    assert marks[-1]["mgmt_reason"] == "unit-test arm_trail"


def test_an_in_remit_cut_is_captured_as_a_cut(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, monkeypatch, "capture_cut", shadow=True, dte=FAR_DTE, mark=RED_MARK,
               decision=_decision("cut"))
    _spy(mgr)
    _run(mgr)
    marks = mgr.state_manager.state.mark_path[str(CID)]
    assert marks[-1]["mgmt_action"] == "cut"
    assert "mgmt_proposed_action" not in marks[-1]


def test_a_refused_proposal_is_never_paged(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    mgr = _mgr(tmp_path, monkeypatch, "no_page", shadow=True, dte=FAR_DTE, mark=RED_MARK)
    posted = []
    monkeypatch.setattr("exitmgr.alerting.post",
                        lambda text, ch, label=None: posted.append(label) or True)
    narrowed, _ = ExitManager._narrow_to_remit({CID: _decision("arm_trail")})
    mgr._alert_exit_decisions(narrowed, {CID: {"con_id": CID, "symbol": "RKLB"}})
    assert posted == []
    mgr._alert_exit_decisions({CID: _decision("cut")}, {CID: {"con_id": CID, "symbol": "RKLB"}})
    assert posted == ["exit-decision"], "an in-remit cut IS news and must still page"
