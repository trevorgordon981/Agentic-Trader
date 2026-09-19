"""Public API contract; production-derived narrative omitted."""

import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from exitmgr import manager as manager_mod
from exitmgr import trader as trader_mod
from exitmgr.apewisdom import profile_eligible, security_profile
from exitmgr.config import Config, RulesConfig, ScaleOutConfig, TrailingConfig
from exitmgr.manager import ExitManager, _durable_qty, _first_present, _MalformedCloseQty
from exitmgr.risk import RiskLimits
from exitmgr.state import InFlightClose
from exitmgr.trader import Trader

from tests._admission_stub import stub_admission_reads


REPO = Path(__file__).resolve().parents[1]


def _module_under_test(name):
    """Public API contract; production-derived narrative omitted."""
    import importlib.util
    import sys as _sys
    spec = importlib.util.spec_from_file_location("uut_" + name, REPO / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    _sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    assert mod.__file__.startswith(str(REPO)), mod.__file__
    return mod


CON = 883301
JOURNAL = {
    "ts": "2026-08-01T14:00:00+00:00",
    "contract_id": CON,
    "symbol": "AAPL",
    "right": "C",
    "strike": 200.0,
    "expiry": "20261231",
    "quantity": 4,
    "debit": 2000.0,
    "conviction": 6,
}


def _manager(tmp_path):
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
        profit_target_pct=30.0, stop_pct=30.0, time_stop_days=10,
        trailing=TrailingConfig(enabled=False), scale_out=ScaleOutConfig(enabled=False))
    (tmp_path / "trades.log").write_text(json.dumps(JOURNAL) + "\n")
    return ExitManager(cfg)


def _context(close_qty=4, position_qty=4, *, drop_close_qty=False):
    ctx = {
        "symbol": "AAPL",
        "reason": "stop",
        "trigger_type": "stop",
        "trigger_message": "zero-vs-absent regression",
        "trigger_pnl_pct": -30.0,
        "close_qty": close_qty,
        "position_qty": position_qty,
        "entry_debit": 2000.0,
        "journal_entry": dict(JOURNAL),
        "manual_request": False,
        "extra": {"partial": False, "trigger_mark": 3.5, "bid": 3.4, "limit_price": 3.5},
    }
    if drop_close_qty:
        ctx.pop("close_qty")
    return ctx


def _trade(order_id, status, *, avg_fill=0.0, filled=0, remaining=0, con_id=CON):
    return SimpleNamespace(
        order=SimpleNamespace(orderId=order_id, clientId=0, permId=0, orderRef=None),
        orderStatus=SimpleNamespace(status=status, avgFillPrice=avg_fill,
                                    filled=filled, remaining=remaining),
        fills=[],
        contract=SimpleNamespace(conId=con_id, secType="OPT"),
    )


def _exec_fill(order_id, shares, price, *, exec_id="E1"):
    return SimpleNamespace(
        contract=SimpleNamespace(conId=CON, secType="OPT"),
        execution=SimpleNamespace(orderId=order_id, clientId=0, permId=0,
                                  side="SLD", shares=shares, price=price, execId=exec_id),
        commissionReport=SimpleNamespace(commission=1.0),
    )


def _rows(path):
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def _flat_evidence():
    return {
        "schema": "position_flat_evidence.v1",
        "source": "ibkr_reqPositions",
        "observed_at": "2026-08-25T17:00:00+00:00",
        "absent_con_ids": [CON],
    }






def test_the_old_expression_really_did_promote_zero_to_the_whole_position():
    """Public API contract; production-derived narrative omitted."""
    ctx, remaining_qty = {"close_qty": 0}, 4
    assert int(ctx.get("close_qty") or remaining_qty or 0) == 4
    with pytest.raises(_MalformedCloseQty):
        _durable_qty(ctx, "close_qty", remaining_qty)


def test_absent_key_still_falls_back_so_legacy_state_files_replay():
    assert _durable_qty({}, "close_qty", 4) == 4
    assert _durable_qty({"close_qty": None}, "close_qty", 4) == 4

    assert _durable_qty({}, "close_qty", None) == 0
    assert _durable_qty({}, "close_qty", "junk") == 0


def test_a_present_but_unusable_quantity_is_refused_loudly():
    for bad in (0, 0.0, -1, -3.5, 2.5, "", "x", float("nan"), float("inf"), True, [], {}):
        with pytest.raises(_MalformedCloseQty):
            _durable_qty({"close_qty": bad}, "close_qty", 4)


def test_a_valid_quantity_is_returned_as_an_int():
    assert _durable_qty({"close_qty": 3}, "close_qty", 4) == 3
    assert _durable_qty({"close_qty": 3.0}, "close_qty", 4) == 3
    assert isinstance(_durable_qty({"close_qty": 3.0}, "close_qty", 4), int)


    assert _durable_qty({"close_qty": "3"}, "close_qty", 4) == 3


def test_the_refusal_message_names_the_field_and_the_value():
    """Public API contract; production-derived narrative omitted."""
    with pytest.raises(_MalformedCloseQty) as e:
        _durable_qty({"close_qty": 0}, "close_qty", 4, where="con_id=1 order_id=2")
    msg = str(e.value)
    assert "close_qty" in msg and "con_id=1 order_id=2" in msg
    assert "close everything" in msg






def test_zero_close_qty_is_not_booked_as_a_full_position_exit(tmp_path):
    mgr = _manager(tmp_path)
    inf = InFlightClose(CON, 77, 4, 2000.0, exit_context=_context(close_qty=0, position_qty=4))
    mgr.state_manager.state.add_in_flight(inf)

    assert mgr._finalize_in_flight_exit(CON, inf, _trade(77, "Filled", avg_fill=6.0, filled=4)) is False
    assert _rows(tmp_path / "exits.log") == []
    assert mgr.state_manager.state.get_in_flight(CON) is not None


def test_zero_close_qty_alarms_with_the_contract_and_order_id(tmp_path, capsys):
    mgr = _manager(tmp_path)
    inf = InFlightClose(CON, 77, 4, 2000.0, exit_context=_context(close_qty=0))
    mgr.state_manager.state.add_in_flight(inf)
    mgr._finalize_in_flight_exit(CON, inf, _trade(77, "Filled", avg_fill=6.0, filled=4))
    out = capsys.readouterr().out
    assert "[ALERT]" in out and f"con_id={CON}" in out and "order_id=77" in out


def test_a_legacy_context_without_close_qty_still_books_the_remaining_quantity(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr = _manager(tmp_path)
    inf = InFlightClose(
        CON, 77, 4, 2000.0, exit_context=_context(drop_close_qty=True),
        client_id=0, identity_version=1)
    mgr.state_manager.state.add_in_flight(inf)

    assert mgr._finalize_in_flight_exit(
        CON, inf, _trade(77, "Filled", avg_fill=6.0, filled=4),
        broker_flat_evidence=_flat_evidence())
    row = _rows(tmp_path / "exits.log")[0]
    assert row["close_qty"] == 4
    assert row["realized_pnl"] == pytest.approx(400.0)


def test_a_real_partial_close_is_unaffected(tmp_path):
    mgr = _manager(tmp_path)
    inf = InFlightClose(
        CON, 77, 2, 1000.0,
        exit_context=_context(close_qty=2, position_qty=4),
        client_id=0, identity_version=1)
    mgr.state_manager.state.add_in_flight(inf)

    assert mgr._finalize_in_flight_exit(CON, inf, _trade(77, "Filled", avg_fill=6.0, filled=2))
    row = _rows(tmp_path / "exits.log")[0]
    assert row["close_qty"] == 2 and row["remaining_qty"] == 2 and row["partial"] is True


def test_zero_position_qty_is_not_promoted_to_the_close_size(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    assert int(0 or 2) == 2
    mgr = _manager(tmp_path)
    inf = InFlightClose(CON, 77, 2, 1000.0, exit_context=_context(close_qty=2, position_qty=0))
    mgr.state_manager.state.add_in_flight(inf)

    assert mgr._finalize_in_flight_exit(CON, inf, _trade(77, "Filled", avg_fill=6.0, filled=2)) is False
    assert _rows(tmp_path / "exits.log") == []
    assert mgr.state_manager.state.get_in_flight(CON) is not None


def test_absent_position_qty_still_falls_back(tmp_path):
    mgr = _manager(tmp_path)
    ctx = _context(close_qty=4, position_qty=4)
    ctx.pop("position_qty")
    inf = InFlightClose(
        CON, 77, 4, 2000.0, exit_context=ctx,
        client_id=0, identity_version=1)
    mgr.state_manager.state.add_in_flight(inf)

    assert mgr._finalize_in_flight_exit(
        CON, inf, _trade(77, "Filled", avg_fill=6.0, filled=4),
        broker_flat_evidence=_flat_evidence())
    assert _rows(tmp_path / "exits.log")[0]["partial"] is False


@pytest.mark.asyncio
async def test_executions_never_synthesise_a_filled_trade_for_a_zero_close_qty(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr = _manager(tmp_path)
    mgr.ib_conn.ib = MagicMock()
    mgr.ib_conn.ib.trades.return_value = []
    mgr.ib_conn.ib.reqCompletedOrdersAsync = AsyncMock(return_value=[])
    mgr.ib_conn.ib.reqExecutionsAsync = AsyncMock(return_value=[_exec_fill(77, 4, 6.0)])

    good = InFlightClose(CON, 77, 4, 2000.0, exit_context=_context(close_qty=4),
                         client_id=0, identity_version=1)
    assert (await mgr._terminal_trades_for_in_flight({str(CON): good}))[CON].orderStatus.status == "Filled"

    zero = InFlightClose(CON, 77, 4, 2000.0, exit_context=_context(close_qty=0),
                         client_id=0, identity_version=1)
    assert CON not in await mgr._terminal_trades_for_in_flight({str(CON): zero})


def test_every_durable_quantity_read_goes_through_the_refusing_reader():
    """Public API contract; production-derived narrative omitted."""
    for fn in (ExitManager._finalize_in_flight_exit,
               ExitManager._terminal_trades_for_in_flight,
               ExitManager._poll_in_flight_fills):
        src = inspect.getsource(fn)
        assert '_durable_qty(' in src, fn.__name__
        assert 'ctx.get("close_qty") or' not in src, fn.__name__
        assert 'get("position_qty")\n' not in src, fn.__name__
    poll = inspect.getsource(ExitManager._poll_in_flight_fills)


    assert "close_qty=_unfilled_qty" in poll
    assert "_unfilled_qty = None" in poll






def test_a_zero_contract_one_tap_close_is_refused_at_the_source():
    """Public API contract; production-derived narrative omitted."""
    src = inspect.getsource(ExitManager.run_cycle)
    assert "ZERO-contract close" in src
    assert "_qty_ok" in src





    assert "journal_qty = int(_p.quantity if _jq is None else _jq)" in src
    assert 'int(je.get("quantity") or _p.quantity)' not in src






def test_first_present_keeps_a_zero_id():
    assert _first_present({"con_id": 0, "contract_id": 12345}, "con_id", "contract_id") == 0
    assert ({"con_id": 0, "contract_id": 12345}.get("con_id")
            or {"con_id": 0, "contract_id": 12345}.get("contract_id")) == 12345
    assert _first_present({"contract_id": 7}, "con_id", "contract_id") == 7
    assert _first_present({}, "con_id", "contract_id") is None


def test_completed_close_count_attributes_a_zero_con_id_to_contract_zero(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr = _manager(tmp_path)
    log = tmp_path / "exits.log"
    log.write_text("\n".join(json.dumps(r) for r in [
        {"con_id": 0, "contract_id": 111, "conId": 111, "ts": "2026-08-01T00:00:00+00:00",
         "realized_pnl": 10.0, "exit_price_per_share": 1.0},
        {"con_id": 111, "ts": "2026-08-01T00:00:01+00:00",
         "realized_pnl": 10.0, "exit_price_per_share": 1.0},
    ]) + "\n")
    mgr._exits_log_path = lambda: str(log)

    assert mgr._completed_closes_since("2026-07-01T00:00:00+00:00") == 2


def test_closed_con_ids_from_exits_log_keeps_contract_zero(tmp_path):
    mgr = _manager(tmp_path)
    log = tmp_path / "exits.log"
    log.write_text(json.dumps(
        {"con_id": 0, "contract_id": 222, "realized_pnl": 1.0}) + "\n")
    mgr._exits_log_path = lambda: str(log)
    assert mgr._closed_con_ids_from_exits_log() == {0}






def test_fill_alarm_minutes_zero_means_alarm_immediately():
    src = inspect.getsource(ExitManager._alert_unfilled_orders)
    assert '_cfg_num(cons, "fill_alarm_minutes", 15)' in src
    assert manager_mod._cfg_num(SimpleNamespace(fill_alarm_minutes=0), "fill_alarm_minutes", 15) == 0.0
    assert manager_mod._cfg_num(SimpleNamespace(), "fill_alarm_minutes", 15) == 15.0
    assert (float(getattr(SimpleNamespace(fill_alarm_minutes=0), "fill_alarm_minutes", 15) or 15)
            == 15.0)


def test_reload_ticket_cadence_reads_a_configured_zero():
    src = inspect.getsource(ExitManager._maybe_write_reload_ticket)
    assert '_cfg_num(self.config, "reload_ttl_cycles", 3)' in src
    assert '_cfg_num(self.config.loop, "interval_seconds", 60)' in src

    assert 'je.get("debit") is None' in src


def test_reload_ticket_dte_target_zero_is_not_silently_thirty(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    class _Q:
        def __init__(self, path):
            pass

        def drain(self, *, today, max_per_name):
            return [{"symbol": "SPY", "right": "C", "structure": "spread", "dte_target": 0,
                     "original_debit": 400.0, "reload_conviction": 7, "thesis": "runner"}], {}

    monkeypatch.setattr(trader_mod.reload_queue, "ReloadQueue", _Q)
    ibc = MagicMock()
    ibc.ib = MagicMock()
    stub_admission_reads(ibc)
    t = Trader(ib_conn=ibc, exit_manager=MagicMock(), limits=RiskLimits(), approved_names=set(),
               endpoint="http://x", model="m", slack_token="t", slack_channel="C",
               approver_ids=set(), baseline_path=str(tmp_path / "b.json"),
               audit_path=str(tmp_path / "a.jsonl"), journal_path=str(tmp_path / "trades.log"))
    t.construction = SimpleNamespace(min_dte=30)

    ideas = t._drain_reload_ideas("2026-08-21")
    assert [i.target_dte for i in ideas] == [0]


def test_deterministic_construction_reads_a_configured_min_dte_of_zero(monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    from exitmgr import construction as _c

    seen = []
    _real = _c.deterministic_expiry

    def _spy(hold, floor, *a, **k):
        seen.append(floor)
        return _real(hold, floor, *a, **k)

    monkeypatch.setattr(_c, "deterministic_expiry", _spy)
    idea = SimpleNamespace(underlying="SPY", side="debit", intended_hold_days=10,
                           target_dte=17, target_delta=0.35)
    out = _c.apply_construction_policy([idea], min_dte=0, enabled=True)
    assert len(out.ideas) == 1 and not out.dropped
    assert seen and set(seen) == {0}, (
        "a configured min_dte of 0 must reach the rule as 0, not as the 25-day default: %r"
        % (seen,))

    seen.clear()
    _c.apply_construction_policy(
        [SimpleNamespace(underlying="SPY", side="debit", intended_hold_days=10,
                         target_dte=17, target_delta=0.35)],
        min_dte=None, enabled=True)
    assert set(seen) == {_c.DEBIT_MIN_DTE_DEFAULT}, (
        "only an ABSENT floor may become the default: %r" % (seen,))
    assert "or 25" not in inspect.getsource(_c.apply_construction_policy)






def test_zero_average_volume_reaches_the_fail_closed_floor(monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    info = {"regularMarketPrice": 12.0, "currentPrice": 12.0,
            "averageVolume": 0, "averageVolume10days": 5_000_000,
            "quoteType": "EQUITY", "currency": "USD", "exchange": "NMS",
            "industry": "Software", "sector": "Technology"}
    assert (info.get("averageVolume") or info.get("averageVolume10days")) == 5_000_000

    monkeypatch.setattr("yfinance.Ticker", lambda t: SimpleNamespace(info=dict(info)))
    profile = security_profile("HALT")
    assert profile["average_volume"] == 0
    ok, reason = profile_eligible(profile, [])
    assert not ok and reason.startswith("average_volume_below_")


def test_a_zero_price_is_not_replaced_by_the_alternate_field(monkeypatch):
    info = {"regularMarketPrice": 0, "currentPrice": 12.0, "averageVolume": 5_000_000,
            "quoteType": "EQUITY", "currency": "USD", "exchange": "NMS",
            "industry": "Software", "sector": "Technology"}
    monkeypatch.setattr("yfinance.Ticker", lambda t: SimpleNamespace(info=dict(info)))
    profile = security_profile("ZERO")
    assert profile["price"] == 0.0
    assert not profile_eligible(profile, [])[0]






def test_fill_quality_reports_a_zero_close_as_zero_not_as_the_full_entry():
    fqr = _module_under_test("fill_quality_report")
    closes = fqr.extract_closes([{
        "kind": "trade", "con_id": CON, "symbol": "AAPL",
        "entry": {"quantity": 4, "debit": 2000.0},
        "close": {"ts": "2026-08-01T00:00:00+00:00", "close_qty": 0,
                  "fill_status": "Filled", "avg_fill_price": 6.0, "trigger_mark": 6.0},
    }])
    assert closes and closes[0]["close_qty"] == 0


def test_event_capture_keeps_a_zero_entry_debit():
    from exitmgr import event_capture
    src = inspect.getsource(event_capture.on_exit)
    assert 'je.get("debit") is None' in src
    assert 'je.get("debit") or je.get("entry_debit")' not in src


def test_cross_book_sees_contract_zero_on_both_sides(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    cross_book = _module_under_test("cross_book")
    monkeypatch.setattr(cross_book, "APP", str(tmp_path))


    (tmp_path / "exits.log").write_text(json.dumps({"con_id": 0, "contract_id": 555}) + "\n")
    (tmp_path / "trades.log").write_text("\n".join(json.dumps(r) for r in [
        {"contract_id": 0, "symbol": "AAA", "debit": 100.0},
        {"contract_id": 555, "symbol": "BBB", "debit": 200.0},
    ]) + "\n")
    assert dict(cross_book.ibkr_by_underlying()) == {"BBB": pytest.approx(200.0)}


    (tmp_path / "exits.log").write_text("")
    (tmp_path / "trades.log").write_text(
        json.dumps({"contract_id": 0, "symbol": "AAA", "debit": 100.0}) + "\n")
    assert dict(cross_book.ibkr_by_underlying()) == {"AAA": pytest.approx(100.0)}


def test_backfill_keeps_a_zero_contract_id(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    bf = _module_under_test("backfill_from_exits_log")
    p = tmp_path / "trades.log"
    p.write_text("\n".join(json.dumps(r) for r in [
        {"contract_id": 0, "conId": 777, "debit": 100.0, "ts": "2026-08-01T00:00:00+00:00"},
    ]) + "\n")
    assert set(bf._load_entry_journal(str(p))) == {0}


def test_clear_stale_latches_is_a_non_mutating_refusal_entrypoint():
    """Public API contract; production-derived narrative omitted."""
    import ast as _ast
    from tests.test_falsy_zero_defaults import _OrDefaultVisitor

    path = REPO / "clear_stale_latches.py"
    src = path.read_text()
    v = _OrDefaultVisitor("clear_stale_latches.py", src)
    v.visit(_ast.parse(src))
    assert v.hits == []
    assert "def main(" in src and "return 2" in src
    assert "os.replace" not in src and "json.dump" not in src
    assert "_first_present" not in src
