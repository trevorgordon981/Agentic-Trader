"""Public API contract; production-derived narrative omitted."""
import copy
import inspect
import json
from datetime import datetime, timezone

from exitmgr.config import Config
from exitmgr.manager import ExitManager
from exitmgr.state import InFlightClose


def _manager(tmp_path):
    cfg = Config()
    cfg.dry_run = True
    cfg.loop_mode = False
    cfg.journal.path = str(tmp_path / "trades.log")
    cfg.state.path = str(tmp_path / "state.json")
    cfg.kill_switch.path = str(tmp_path / "KILL")
    cfg.audit_path = str(tmp_path / "audit.jsonl")
    (tmp_path / "trades.log").write_text("")
    return ExitManager(cfg), cfg


def test_stop_receipt_measures_threshold_to_fill_not_just_mark_to_fill():
    ctx = {
        "triggered_at": "2026-09-18T14:30:00+00:00",
        "trigger_threshold_pct": -30.0,
        "trigger_threshold_price": 3.50,
        "trigger_threshold_basis": "long_debit_return_pct",
        "extra": {"trigger_mark": 3.40},
    }
    row = ExitManager._protective_fill_telemetry(
        ctx, fill_price=3.20, quantity=1, basis=500.0, short_close=False,
        fill_ts="2026-09-18T14:30:05+00:00")
    assert row["triggered_at"] == ctx["triggered_at"]
    assert row["trigger_mark"] == 3.40
    assert row["trigger_threshold_pct"] == -30.0
    assert row["trigger_threshold_basis"] == "long_debit_return_pct"
    assert row["fill_price"] == 3.20
    assert row["trigger_to_fill_seconds"] == 5.0
    assert row["realized_loss_usd"] == 180.0
    assert row["realized_loss_pct"] == 36.0
    assert row["threshold_to_fill_slippage_per_share"] == -0.30
    assert row["adverse_slippage_beyond_threshold_per_share"] == 0.30
    assert row["threshold_to_fill_slippage_pct_points"] == -6.0
    assert row["adverse_slippage_beyond_threshold_pct_points"] == 6.0


def test_hard_stop_breach_uses_distinct_unthrottled_incident(tmp_path):
    mgr, _ = _manager(tmp_path)
    calls = []
    mgr._post_unthrottled_alert = lambda body, what, **kw: calls.append(
        (body, what, kw)) or True
    telemetry = {
        "trigger_threshold_pct": -30.0, "realized_loss_pct": 36.0,
        "adverse_slippage_beyond_threshold_pct_points": 6.0,
        "trigger_mark": 3.40, "fill_price": 3.20, "trigger_to_fill_seconds": 5.0,
    }
    assert mgr._post_hard_stop_breach_incident(
        symbol="SYMS", con_id=101, fill_key="fill-101", telemetry=telemetry)
    body, what, kw = calls[0]
    assert what == "hard-stop-execution-breach"
    assert kw["repeat_seconds"] == 0 and kw["condition_key"] == "fill-101"
    assert "does not claim a maximum realized loss" in body
    assert "6.00 percentage points beyond threshold" in body


def test_failed_hard_stop_breach_delivery_does_not_checkpoint_and_retries(tmp_path):
    mgr, _ = _manager(tmp_path)
    inf = InFlightClose(con_id=101, order_id=9, remaining_qty=1, entry_debit=500,
                        client_id=8, perm_id=99, order_ref="exitmgr-101",
                        fill_key="fill-101")
    mgr.state_manager.state.add_in_flight(inf)
    telemetry = {
        "trigger_threshold_pct": -30.0, "realized_loss_pct": 36.0,
        "adverse_slippage_beyond_threshold_pct_points": 6.0,
        "trigger_mark": 3.4, "fill_price": 3.2, "trigger_to_fill_seconds": 5.0,
    }
    deliveries = iter((False, True))
    calls = []

    def transport(_body, _what, **kwargs):
        result = next(deliveries)
        calls.append(result)
        kwargs["on_delivery"](result)
        return True

    mgr._post_unthrottled_alert = transport
    args = dict(symbol="SYMS", con_id=101, fill_key="fill-101",
                telemetry=telemetry, inf=inf)
    assert mgr._ensure_hard_stop_breach_alert(**args) is False
    assert "hard_stop_breach_alert" not in inf.side_effects
    assert mgr._ensure_hard_stop_breach_alert(**args) is True
    assert inf.side_effects["hard_stop_breach_alert"] is True
    assert calls == [False, True]


def test_hard_stop_delivery_callback_revalidates_exact_order_identity(tmp_path):
    mgr, _ = _manager(tmp_path)
    inf = InFlightClose(con_id=101, order_id=9, remaining_qty=1, entry_debit=500,
                        client_id=8, perm_id=99, order_ref="exitmgr-101",
                        fill_key="fill-101")
    mgr.state_manager.state.add_in_flight(inf)
    callbacks = []
    mgr._post_unthrottled_alert = lambda _body, _what, **kw: (
        callbacks.append(kw["on_delivery"]) or True)
    telemetry = {
        "trigger_threshold_pct": -30.0, "realized_loss_pct": 36.0,
        "adverse_slippage_beyond_threshold_pct_points": 6.0,
    }
    assert not mgr._ensure_hard_stop_breach_alert(
        symbol="SYMS", con_id=101, fill_key="fill-101", telemetry=telemetry, inf=inf)
    inf.order_id = 10
    callbacks[0](True)
    assert "hard_stop_breach_alert" not in inf.side_effects


def test_hard_stop_delivery_callback_revalidates_fill_key_with_same_order_tuple(tmp_path):
    mgr, _ = _manager(tmp_path)
    inf = InFlightClose(con_id=101, order_id=9, remaining_qty=1, entry_debit=500,
                        client_id=8, perm_id=99, order_ref="exitmgr-101",
                        fill_key="fill-101")
    mgr.state_manager.state.add_in_flight(inf)
    callbacks = []
    mgr._post_unthrottled_alert = lambda _body, _what, **kw: (
        callbacks.append(kw["on_delivery"]) or True)
    telemetry = {
        "trigger_threshold_pct": -30.0, "realized_loss_pct": 36.0,
        "adverse_slippage_beyond_threshold_pct_points": 6.0,
    }
    assert not mgr._ensure_hard_stop_breach_alert(
        symbol="SYMS", con_id=101, fill_key="fill-101", telemetry=telemetry, inf=inf)
    inf.fill_key = "replacement-fill"
    callbacks[0](True)
    assert "hard_stop_breach_alert" not in inf.side_effects


def test_finalizer_persists_telemetry_and_checkpoints_breach_alert():
    src = inspect.getsource(ExitManager._finalize_in_flight_exit)
    assert "self._protective_fill_telemetry(" in src
    assert "extra.update(telemetry)" in src
    assert "self._ensure_hard_stop_breach_alert(" in src
    assert "return False" in src
    ensure = inspect.getsource(ExitManager._ensure_hard_stop_breach_alert)
    assert 'current.side_effects["hard_stop_breach_alert"] = True' in ensure
    assert "if not delivered:" in ensure and "current_identity != identity" in ensure


def test_prior_session_day_page_is_persistently_deduped_without_order_mutation(tmp_path):
    mgr, cfg = _manager(tmp_path)
    inf = InFlightClose(
        con_id=202, order_id=77, remaining_qty=1, entry_debit=500.0,
        placed_at="2026-09-17T15:00:00+00:00",
        exit_context={"symbol": "SYMS", "trigger_type": "stop"},
        client_id=8, perm_id=7077, order_ref="exitmgr-202", identity_version=1,
        submitted_close={
            "schema": "submitted_close.v1", "sec_type": "OPT", "action": "SELL",
            "combo_qty": 1,
            "legs": [{"con_id": 202, "ratio": 1, "expected_side": "SLD",
                      "multiplier": 100}],
            "order_type": "LMT", "tif": "DAY", "limit_price": 3.5,
            "stop_price": None,
        })
    mgr.state_manager.state.add_in_flight(inf)
    mgr.state_manager.save()
    frozen_order = copy.deepcopy(inf.submitted_close)
    posts = []
    def delivered(*args, **kwargs):
        posts.append((args, kwargs))
        callback = kwargs.get("on_delivery")
        if callback:
            callback(True)
        return True
    mgr._post_unthrottled_alert = delivered
    now = datetime(2026, 9, 18, 15, 0, tzinfo=timezone.utc)
    assert mgr._page_prior_session_day_closes(now) == 1
    assert inf.submitted_close == frozen_order
    assert inf.side_effects["next_rth_day_close_page"] is True
    assert len(posts) == 1


    mgr2 = ExitManager(cfg)
    mgr2._post_unthrottled_alert = lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("durably deduped page repeated after restart"))
    assert mgr2._page_prior_session_day_closes(now) == 0
    reloaded = mgr2.state_manager.state.get_in_flight(202)
    assert reloaded.submitted_close == frozen_order
    assert reloaded.side_effects["next_rth_day_close_page"] is True


def test_failed_prior_session_page_delivery_does_not_checkpoint_and_retries(tmp_path):
    mgr, _ = _manager(tmp_path)
    inf = InFlightClose(
        con_id=303, order_id=88, remaining_qty=1, entry_debit=500.0,
        placed_at="2026-09-17T15:00:00+00:00", client_id=8, perm_id=8088,
        order_ref="exitmgr-303", identity_version=1,
        exit_context={"symbol": "SYMR", "trigger_type": "stop"},
        submitted_close={"schema": "submitted_close.v1", "sec_type": "OPT",
                         "action": "SELL", "combo_qty": 1,
                         "legs": [{"con_id": 303, "ratio": 1,
                                   "expected_side": "SLD", "multiplier": 100}],
                         "order_type": "LMT", "tif": "DAY", "limit_price": 1.0,
                         "stop_price": None})
    mgr.state_manager.state.add_in_flight(inf)
    deliveries = iter((False, True))
    calls = []

    def transport(*_args, **kwargs):
        result = next(deliveries)
        calls.append(result)
        kwargs["on_delivery"](result)
        return True

    mgr._post_unthrottled_alert = transport
    now = datetime(2026, 9, 18, 15, 0, tzinfo=timezone.utc)
    assert mgr._page_prior_session_day_closes(now) == 1
    assert "next_rth_day_close_page" not in inf.side_effects
    assert mgr._page_prior_session_day_closes(now) == 1
    assert inf.side_effects["next_rth_day_close_page"] is True
    assert calls == [False, True]


def test_telemetry_fields_are_exported_in_the_closed_trade_dataset():
    src = inspect.getsource(ExitManager._log_trade_dataset)
    for field in (
        "triggered_at", "trigger_threshold_pct", "trigger_threshold_price",
        "trigger_threshold_basis", "trigger_to_fill_seconds", "realized_loss_usd",
        "realized_loss_pct", "threshold_to_fill_slippage_pct_points",
        "adverse_slippage_beyond_threshold_pct_points",
    ):
        assert json.dumps(field)[1:-1] in src
