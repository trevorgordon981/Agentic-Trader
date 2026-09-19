"""Public API contract; production-derived narrative omitted."""
import asyncio
import inspect
import json
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock, MagicMock

import exitmgr.trader as trader
from exitmgr.config import Config, RulesConfig, TrailingConfig, ScaleOutConfig
from exitmgr.connection import PositionData
from exitmgr.manager import ExitManager
from exitmgr.state import InFlightClose

SYMD = 5001002
SYME = 5001004


def _mgr(tmp_path):
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
    cfg.rules = RulesConfig(profit_target_pct=30.0, stop_pct=30.0, time_stop_days=10,
                            trailing=TrailingConfig(enabled=False),
                            scale_out=ScaleOutConfig(enabled=False, first_target_pct=20.0,
                                                     trim_fraction=0.5))
    (tmp_path / "trades.log").write_text("")
    return ExitManager(cfg)


def _intent(con_id=SYMD, *, age_s, remaining=1, position_qty=1, order_id=0,
            placement_state="intent"):
    """Public API contract; production-derived narrative omitted."""
    placed = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).isoformat()
    return InFlightClose(
        con_id=con_id, order_id=order_id, remaining_qty=remaining, entry_debit=500.0,
        placed_at=placed,
        exit_context={"symbol": "SYMD", "position_qty": position_qty, "close_qty": remaining,
                      "reason": "stop", "trigger_type": "stop_loss"},
        order_ref=f"exitmgr-{con_id}-7fcb0908b4e14ab8a754", identity_version=1,
        placement_state=placement_state)


def _positions(qty, con_id=SYMD):
    return {con_id: PositionData(con_id=con_id, symbol="SYMD", right="C", quantity=qty,
                                 avg_cost=5.00, expiry="20261231")}


def _wire_history(mgr, *, readable):
    """Public API contract; production-derived narrative omitted."""
    mgr.ib_conn.ib = MagicMock()
    mgr.ib_conn.ib.trades = lambda: []
    mgr.ib_conn.ib.reqExecutionsAsync = AsyncMock(return_value=[])
    if readable:
        mgr.ib_conn.ib.reqCompletedOrdersAsync = AsyncMock(return_value=[])
    else:
        mgr.ib_conn.ib.reqCompletedOrdersAsync = AsyncMock(
            side_effect=RuntimeError("completed-order read timed out"))
    return mgr


def _passes(mgr, n, *, positions, live_orders=None):
    async def run():
        await mgr._poll_in_flight_fills(live_orders if live_orders is not None else {},
                                      live_positions=positions)
        await _finish_notices(mgr)
    for _ in range(n):
        asyncio.run(run())


async def _finish_notices(mgr):
    await asyncio.gather(*list(getattr(mgr, "_active_risk_alert_tasks", {}).values()))
    await asyncio.sleep(0)


def _held(mgr, con_id=SYMD):
    return mgr.state_manager.state.get_in_flight(con_id) is not None





def _alarm_mgr(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path)
    mgr.config.error_channel = "C_ERR"
    monkeypatch.setenv("SLACK_BOT_TOKEN", "tok")
    monkeypatch.setattr(trader, "_market_open", lambda: True)
    posts = []
    monkeypatch.setattr("exitmgr.alerting.post",
                        lambda txt, ch, **kw: posts.append(txt) or True)
    mgr.ib_conn.ib = MagicMock()
    mgr.ib_conn.ib.openTrades.return_value = []
    return mgr, posts


@pytest.mark.asyncio
async def test_an_order_id_zero_intent_is_no_longer_invisible_to_the_fill_alarm(
        tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    mgr, posts = _alarm_mgr(tmp_path, monkeypatch)
    mgr.state_manager.state.add_in_flight(_intent(age_s=7.6 * 24 * 3600))
    await mgr._alert_unfilled_orders()
    await _finish_notices(mgr)
    assert posts, "an order_id=0 intent must alarm; this is exactly what SYMD/SYME needed"
    body = posts[0]
    assert str(SYMD) in body
    assert "UNTRANSMITTED INTENT" in body and "UNPROTECTED" in body
    assert "order_ref=exitmgr-5001002-" in body, "an orderRef is a usable identity for an operator"


@pytest.mark.asyncio
async def test_both_stranded_contracts_would_have_alarmed_on_day_one(tmp_path, monkeypatch):
    mgr, posts = _alarm_mgr(tmp_path, monkeypatch)
    for cid in (SYMD, SYME):
        mgr.state_manager.state.add_in_flight(_intent(cid, age_s=30 * 60))
    await mgr._alert_unfilled_orders()
    await _finish_notices(mgr)
    body = "\n".join(posts)
    assert str(SYMD) in body and str(SYME) in body


@pytest.mark.asyncio
async def test_a_transmitted_order_still_alarms_exactly_as_before(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    mgr, posts = _alarm_mgr(tmp_path, monkeypatch)
    mgr.state_manager.state.add_in_flight(
        _intent(age_s=20 * 60, order_id=9, placement_state="submitted"))
    await mgr._alert_unfilled_orders()
    await _finish_notices(mgr)
    assert posts and "order_id=9" in posts[0]
    assert "UNTRANSMITTED INTENT" not in posts[0]


@pytest.mark.asyncio
async def test_a_filled_record_with_nothing_left_to_close_still_does_not_alarm(
        tmp_path, monkeypatch):
    mgr, posts = _alarm_mgr(tmp_path, monkeypatch)
    mgr.state_manager.state.add_in_flight(_intent(age_s=20 * 60, remaining=0))
    await mgr._alert_unfilled_orders()
    await _finish_notices(mgr)
    assert not posts, "remaining_qty<=0 is nothing to chase; widening the alarm must not spam"





def test_an_unreadable_history_release_still_requires_the_position_to_be_unchanged(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr = _wire_history(_mgr(tmp_path), readable=False)
    mgr.state_manager.state.add_in_flight(_intent(age_s=3000, remaining=2, position_qty=4))
    _passes(mgr, 12, positions=_positions(2))
    assert _held(mgr), "a partially-filled close must never have its double-close guard released"


def test_an_unreadable_history_release_still_requires_the_grace_period(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr = _wire_history(_mgr(tmp_path), readable=False)
    mgr.state_manager.state.add_in_flight(_intent(age_s=5))
    _passes(mgr, 10, positions=_positions(1))
    assert _held(mgr)
    assert mgr._INTENT_LATCH_MIN_AGE_S >= 300.0


def test_an_absent_position_is_not_evidence_of_an_absent_order(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr = _wire_history(_mgr(tmp_path), readable=False)
    mgr.state_manager.state.add_in_flight(_intent(age_s=3000))
    _passes(mgr, 12, positions={})
    assert _held(mgr)


def test_both_diagnostic_paths_go_through_the_one_evidence_helper(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    src = inspect.getsource(ExitManager._poll_in_flight_fills)
    assert src.count("self._intent_release_evidence(") == 2, (
        "both the history-readable and history-unreadable diagnostics must consult the SAME "
        "evidence helper")
    latch = src.split("_is_intent = ")[1]
    assert "old_enough" not in latch and "int(live_qty)" not in latch, (
        "the latch block must not re-derive release conditions inline")





def test_a_non_qualifying_pass_discards_diagnostic_observations(tmp_path):
    mgr = _wire_history(_mgr(tmp_path), readable=False)
    mgr.state_manager.state.add_in_flight(_intent(age_s=3000, remaining=1, position_qty=1))
    _passes(mgr, 4, positions=_positions(1))
    assert mgr._intent_unreadable_observations.get(SYMD) == 4
    _passes(mgr, 1, positions=_positions(9))
    assert SYMD not in mgr._intent_unreadable_observations
    _passes(mgr, 100, positions=_positions(1))
    assert _held(mgr), "history-unreadable observations are diagnostic, never release authority"


def test_a_resting_live_close_neither_advances_the_streak_nor_releases(tmp_path):
    mgr = _wire_history(_mgr(tmp_path), readable=False)
    mgr.state_manager.state.add_in_flight(_intent(age_s=3000))
    _passes(mgr, 12, positions=_positions(1),
            live_orders={SYMD: {"order_id": 77, "remaining": 1}})
    assert _held(mgr) and SYMD not in mgr._intent_unreadable_observations





def test_age_and_a_fresh_process_never_replace_terminal_evidence(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr = _wire_history(_mgr(tmp_path), readable=False)
    mgr.state_manager.state.add_in_flight(_intent(age_s=4000))
    assert mgr._intent_unreadable_observations == {}
    _passes(mgr, 100, positions=_positions(1))
    assert _held(mgr)


def test_the_hard_age_backstop_still_demands_the_full_evidence(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr = _wire_history(_mgr(tmp_path), readable=False)
    mgr.state_manager.state.add_in_flight(_intent(age_s=8 * 24 * 3600, remaining=2,
                                                  position_qty=4))
    _passes(mgr, 3, positions=_positions(2))
    assert _held(mgr)





def test_an_unreadable_open_order_book_can_never_advance_the_latch(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr = _wire_history(_mgr(tmp_path), readable=False)
    mgr._order_book_unreadable = "open-order read timed out"
    mgr.state_manager.state.add_in_flight(_intent(age_s=8 * 24 * 3600))
    _passes(mgr, 12, positions=_positions(1))
    assert _held(mgr) and SYMD not in mgr._intent_unreadable_observations





def test_the_latch_pages_a_human_and_never_auto_releases(tmp_path, capsys):
    mgr = _wire_history(_mgr(tmp_path), readable=False)
    mgr.state_manager.state.add_in_flight(_intent(age_s=3000))
    _passes(mgr, 1, positions=_positions(1))
    first = capsys.readouterr().out
    assert "[INTENT-PAGE]" in first and "released=False" in first
    _passes(mgr, 100, positions=_positions(1))
    assert _held(mgr)


def test_an_intent_that_cannot_be_released_pages_too(tmp_path, capsys):
    """Public API contract; production-derived narrative omitted."""
    mgr = _wire_history(_mgr(tmp_path), readable=False)
    mgr.state_manager.state.add_in_flight(_intent(age_s=7.6 * 24 * 3600, remaining=2,
                                                  position_qty=4))
    _passes(mgr, 1, positions=_positions(2))
    out = capsys.readouterr().out
    assert "[INTENT-PAGE]" in out
    assert _held(mgr)


def test_the_page_is_delivered_through_the_verified_slack_poster(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    sent = []
    monkeypatch.setattr("exitmgr.alerting.post",
                        lambda text, channel_id, **kw: sent.append((channel_id, text)) or True)
    mgr = _wire_history(_mgr(tmp_path), readable=False)
    mgr.config.error_channel = "C_ERR"
    mgr.state_manager.state.add_in_flight(_intent(age_s=3000))
    _passes(mgr, 1, positions=_positions(1))
    assert sent and sent[0][0] == "C_ERR"
    assert "UNPROTECTED" in sent[0][1] and str(SYMD) in sent[0][1]


def test_the_page_is_throttled_between_edges_so_an_outage_is_not_a_firehose(tmp_path, capsys):
    mgr = _wire_history(_mgr(tmp_path), readable=False)
    mgr.state_manager.state.add_in_flight(_intent(age_s=3000))
    _passes(mgr, 6, positions=_positions(1))
    out = capsys.readouterr().out
    assert out.count("[INTENT-PAGE]") == 1, "engage pages once, then throttles"





def test_readable_empty_history_never_releases_without_an_exact_terminal_receipt(tmp_path):
    mgr = _wire_history(_mgr(tmp_path), readable=True)
    mgr.state_manager.state.add_in_flight(_intent(age_s=45))


    asyncio.run(mgr.refresh_terminal_history_offcycle())
    _passes(mgr, 1, positions=_positions(1))
    assert _held(mgr), "readable absence is not an exact terminal-order receipt"


def test_readable_history_does_not_release_inside_the_thirty_second_grace(tmp_path):
    mgr = _wire_history(_mgr(tmp_path), readable=True)
    mgr.state_manager.state.add_in_flight(_intent(age_s=5))
    asyncio.run(mgr.refresh_terminal_history_offcycle())
    _passes(mgr, 1, positions=_positions(1))
    assert _held(mgr)


def test_a_submitted_record_is_untouched_by_the_latch(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr = _wire_history(_mgr(tmp_path), readable=False)
    mgr.state_manager.state.add_in_flight(
        _intent(age_s=8 * 24 * 3600, order_id=5, placement_state="submitted"))
    _passes(mgr, 12, positions=_positions(1))
    assert _held(mgr) and SYMD not in mgr._intent_unreadable_observations


def test_readable_history_clears_diagnostic_counter_but_not_the_latch(tmp_path):
    mgr = _wire_history(_mgr(tmp_path), readable=True)
    mgr.state_manager.state.add_in_flight(_intent(age_s=3000))
    mgr._intent_unreadable_observations[SYMD] = 99
    asyncio.run(mgr.refresh_terminal_history_offcycle())
    _passes(mgr, 1, positions=_positions(1))
    assert _held(mgr)
    assert SYMD not in mgr._intent_unreadable_observations


def test_legacy_latch_clearer_refuses_go_and_cannot_mutate_state(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    state = tmp_path / "exitmgr_state.json"
    original = json.dumps({"in_flight": {str(SYMD): {
        "con_id": SYMD, "order_id": 0, "remaining_qty": 1,
        "exit_context": {"symbol": "SYMD"}}}}, sort_keys=True)
    state.write_text(original)
    (tmp_path / "exits.log").write_text(json.dumps({
        "con_id": SYMD, "quantity": 1, "exit_price_per_share": 5.0}) + "\n")

    script = Path(__file__).resolve().parents[1] / "clear_stale_latches.py"
    proc = subprocess.run(
        [sys.executable, str(script), "--go"], cwd=tmp_path,
        text=True, capture_output=True, check=False)

    assert proc.returncode == 2
    assert "REFUSED" in proc.stderr and "exact" in proc.stderr
    assert state.read_text() == original


def test_legacy_order_id_reuse_from_another_client_cannot_release_latch(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr = _mgr(tmp_path)
    inf = InFlightClose(
        con_id=SYMD, order_id=77, remaining_qty=1, entry_debit=500.0,
        placement_state="submitted",
        exit_context={"symbol": "SYMD", "position_qty": 1, "close_qty": 1,
                      "reason": "stop", "trigger_type": "stop_loss"})
    mgr.state_manager.state.add_in_flight(inf)
    other_client_cancel = SimpleNamespace(
        contract=SimpleNamespace(conId=SYMD, secType="OPT"),
        order=SimpleNamespace(orderId=77, clientId=999, permId=0, orderRef=""),
        orderStatus=SimpleNamespace(status="Cancelled", filled=0, avgFillPrice=0),
        fills=[])
    mgr.ib_conn.ib = MagicMock()
    mgr.ib_conn.ib.trades = lambda: [other_client_cancel]
    mgr.ib_conn.ib.reqExecutionsAsync = AsyncMock(return_value=[])
    mgr.ib_conn.ib.reqCompletedOrdersAsync = AsyncMock(
        return_value=[other_client_cancel])

    assert mgr._trade_matches_in_flight(other_client_cancel, inf) is False
    asyncio.run(mgr.refresh_terminal_history_offcycle())
    asyncio.run(mgr._poll_in_flight_fills({}, live_positions=_positions(1)))

    assert mgr.state_manager.state.get_in_flight(SYMD) is inf


@pytest.mark.asyncio
async def test_fill_alarm_uses_file_token_transport_without_order_request_or_reactions(
        tmp_path, monkeypatch):
    mgr, posts = _alarm_mgr(tmp_path, monkeypatch)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    forbidden = MagicMock(side_effect=AssertionError("notification may not use approval API"))
    monkeypatch.setattr("exitmgr.approval.post_proposal", forbidden)
    mgr.ib_conn.ib.reqOpenOrdersAsync = AsyncMock(
        side_effect=AssertionError("alarm may not request broker data"))
    inf = _intent(age_s=3000, order_id=9, placement_state="transmission_ambiguous")
    mgr.state_manager.state.add_in_flight(inf)
    before = vars(inf).copy()
    await mgr._alert_unfilled_orders()
    await _finish_notices(mgr)
    assert posts and "TRANSMISSION/ACK UNKNOWN" in posts[0]
    forbidden.assert_not_called()
    mgr.ib_conn.ib.reqOpenOrdersAsync.assert_not_awaited()
    assert vars(inf) == before and mgr.state_manager.state.get_in_flight(SYMD) is inf


@pytest.mark.asyncio
async def test_failed_notice_retries_but_verified_success_throttles_exact_identity(
        tmp_path, monkeypatch, capsys):
    mgr, _ = _alarm_mgr(tmp_path, monkeypatch)
    post = MagicMock(side_effect=[False, True, True])
    monkeypatch.setattr("exitmgr.alerting.post", post)
    inf = _intent(age_s=3000, order_id=9, placement_state="transmission_ambiguous")
    mgr.state_manager.state.add_in_flight(inf)
    await mgr._alert_unfilled_orders(); await _finish_notices(mgr)
    first = capsys.readouterr().out
    assert "UNDELIVERED" in first and "[ALERT-DELIVERED]" not in first
    await mgr._alert_unfilled_orders(); await _finish_notices(mgr)
    assert "[ALERT-DELIVERED]" in capsys.readouterr().out
    await mgr._alert_unfilled_orders(); await _finish_notices(mgr)
    assert post.call_count == 2
    inf.order_ref = "different-exact-order"
    await mgr._alert_unfilled_orders(); await _finish_notices(mgr)
    assert post.call_count == 3


@pytest.mark.parametrize("readable", [False, True])
def test_ambiguous_close_pages_and_preserves_latch_for_both_history_states(
        tmp_path, monkeypatch, readable):
    mgr = _wire_history(_mgr(tmp_path), readable=readable)
    sent = []
    monkeypatch.setattr("exitmgr.alerting.post",
                        lambda text, channel, **kwargs: sent.append(text) or True)
    inf = _intent(age_s=3000, order_id=9, placement_state="transmission_ambiguous")
    mgr.state_manager.state.add_in_flight(inf)
    before = vars(inf).copy()
    if readable:
        asyncio.run(mgr.refresh_terminal_history_offcycle())
    _passes(mgr, 3, positions=_positions(1))
    assert len(sent) == 1 and "UNPROTECTED" in sent[0]
    assert vars(inf) == before and mgr.state_manager.state.get_in_flight(SYMD) is inf
