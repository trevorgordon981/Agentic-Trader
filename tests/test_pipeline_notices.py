import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from exitmgr import pipeline_notice as notice
from exitmgr import trader as mod
from exitmgr.account import PotSnapshot
from tests.test_trader import _trader


def args(tmp_path, **overrides):
    result = dict(path=str(tmp_path / "notices.json"), token="fake", channel="C1",
                  kind="model_error", text="Invalid thesis length", now=100.0)
    result.update(overrides)
    return result


def test_receipt_is_required_before_deduplication(tmp_path):
    send = Mock(side_effect=[None, "123.4", "124.5"])
    assert notice.deliver(**args(tmp_path), post=send)["status"] == "failed"
    assert not (tmp_path / "notices.json").exists()
    assert notice.deliver(**args(tmp_path, now=200), post=send)["status"] == "sent"
    assert notice.deliver(**args(tmp_path, now=300), post=send)["status"] == "suppressed"
    assert notice.deliver(**args(tmp_path, now=3800), post=send)["status"] == "sent"
    assert send.call_count == 3
    assert (tmp_path / "notices.json").stat().st_mode & 0o777 == 0o600


def test_failed_recovery_is_retried_and_success_clears_error(tmp_path):
    send = Mock(side_effect=["123", None, "124", "125"])
    notice.deliver(**args(tmp_path), post=send)
    recovery = args(tmp_path, text="Responses recovered", recovery=True, now=200)
    assert notice.deliver(**recovery, post=send)["status"] == "failed"
    assert notice.deliver(**recovery, post=send)["status"] == "sent"
    assert notice.deliver(**recovery, post=send)["status"] == "no_previous_failure"
    assert notice.deliver(**args(tmp_path, now=300), post=send)["status"] == "sent"
    assert len({call.args[3] for call in send.call_args_list}) == 4


def test_changed_problem_delivers_but_quote_fluctuations_do_not(tmp_path):
    send = Mock(return_value="123")
    notice.deliver(**args(tmp_path, text="wide spread47.6 max45.0"), post=send)
    assert notice.deliver(**args(tmp_path, text="wide spread49.1 max45.0", now=200),
                          post=send)["status"] == "suppressed"
    assert notice.deliver(**args(tmp_path, text="quote missing", now=300),
                          post=send)["status"] == "sent"


def test_corrupt_cache_does_not_hide_failure_and_transport_exception_not_saved(tmp_path):
    (tmp_path / "notices.json").write_text("not json")
    assert notice.deliver(**args(tmp_path), post=Mock(return_value="123"))["status"] == "sent"
    result = notice.deliver(**args(tmp_path, kind="other"),
                            post=Mock(side_effect=TimeoutError("private failure")))
    assert result == {"status": "failed", "reason": "TimeoutError"}
    assert "other" not in (tmp_path / "notices.json").read_text()


def test_details_redact_credentials_account_and_mentions():
    text = notice.safe_detail("https://user:secret@host/?token=x U12345678 Bearer abc xoxb-123-xxx <@U1>")
    assert "secret" not in text and "12345678" not in text and "abc" not in text
    assert "xoxb" not in text and "<@" not in text


def configure_cycle(t, monkeypatch, result=None, error=None):
    t.exit_manager.state_manager.persist = False
    monkeypatch.setattr(mod, "_market_open", lambda: True)
    monkeypatch.setattr(mod, "get_pot_snapshot", AsyncMock(return_value=PotSnapshot(5000, 2000, 2000)))
    t._market_context = AsyncMock(return_value="test context")
    t._open_positions = AsyncMock(return_value=[])
    t._shadow_cot_capture = AsyncMock()
    t._entry_markers_clear = Mock(return_value=SimpleNamespace(allowed=True))
    t.reconcile_open_entry_intents = AsyncMock(return_value=SimpleNamespace(
        journaled=(), released=(), retained=(), still_working=(), readable=True, reasons=()))
    monkeypatch.setattr(mod, "propose_intents", Mock(side_effect=error, return_value=result or []))
    return mod.propose_intents


@pytest.mark.asyncio
async def test_model_error_is_reported_with_no_order(tmp_path, monkeypatch):
    t = _trader(tmp_path)
    configure_cycle(t, monkeypatch, error=RuntimeError("invalid thesis length"))
    send = Mock(return_value="receipt")
    monkeypatch.setattr(notice, "_post", send)
    await t.run_once(dry_run=True)
    assert send.call_count == 1 and "evaluation failed" in send.call_args.args[2]
    t._submit_order.assert_not_called()
    rows = [json.loads(x) for x in (tmp_path / "a.jsonl").read_text().splitlines()]
    assert any(r["event"] == "entry_status_notice" and r["status"] == "sent" for r in rows)
    assert any(r["event"] == "entry_pipeline_no_trade" and r["reason"] == "entry_pipeline_error" for r in rows)


@pytest.mark.asyncio
async def test_valid_empty_decline_is_not_retried_or_alerted(tmp_path, monkeypatch):
    t = _trader(tmp_path)
    proposal = configure_cycle(t, monkeypatch)
    t._materialize_stage_b = AsyncMock(return_value=[])
    send = Mock(return_value="receipt")
    monkeypatch.setattr(notice, "_post", send)
    await t.run_once(dry_run=True)
    proposal.assert_called_once()
    send.assert_not_called()
    t._submit_order.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("refresh", [False, RuntimeError("read failure")])
async def test_split_refresh_failure_blocks_model_without_running_exits(tmp_path, monkeypatch, refresh):
    t = _trader(tmp_path)
    proposal = configure_cycle(t, monkeypatch)
    t.exit_manager.refresh_entry_reconciliation = AsyncMock(
        side_effect=refresh if isinstance(refresh, Exception) else None, return_value=False)
    await t.run_once(dry_run=True, skip_exit_cycle=True)
    proposal.assert_not_called()
    t.exit_manager.run_cycle.assert_not_called()
    t._submit_order.assert_not_called()
    assert t.exit_manager._reconcile_ok is False


@pytest.mark.asyncio
async def test_split_refresh_occurs_after_journal_recovery_before_model(tmp_path, monkeypatch):
    t = _trader(tmp_path)
    configure_cycle(t, monkeypatch)
    order = []
    original = t.reconcile_open_entry_intents
    async def recover(**kwargs):
        order.append("journal")
        return await original(**kwargs)
    async def refresh():
        order.append("refresh")
        return True
    def propose(*a, **k):
        order.append("model")
        return []
    t.reconcile_open_entry_intents = recover
    t.exit_manager.refresh_entry_reconciliation = refresh
    t._materialize_stage_b = AsyncMock(return_value=[])
    monkeypatch.setattr(mod, "propose_intents", propose)
    await t.run_once(dry_run=True, skip_exit_cycle=True)
    assert order == ["journal", "refresh", "model"]
    t.exit_manager.run_cycle.assert_not_called()
    t._submit_order.assert_not_called()


@pytest.mark.asyncio
async def test_combined_writer_does_not_run_entry_refresh(tmp_path, monkeypatch):
    t = _trader(tmp_path)
    configure_cycle(t, monkeypatch)
    t.exit_manager.state_manager.persist = True
    t.exit_manager.refresh_entry_reconciliation = AsyncMock()
    t._materialize_stage_b = AsyncMock(return_value=[])
    await t.run_once(dry_run=True, skip_exit_cycle=True)
    t.exit_manager.refresh_entry_reconciliation.assert_not_called()
    t._submit_order.assert_not_called()
