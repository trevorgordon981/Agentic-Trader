"""Public API contract; production-derived narrative omitted."""
import asyncio
import inspect
import json
import textwrap
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import daily_recommend as dr
from exitmgr import trader as mod
from tests.test_pipeline_notices import configure_cycle
from tests.test_trader import _trader, IDEA


def row(outcome, symbol="SPY"):
    result = {"outcome": outcome, "underlying": symbol}
    if outcome in {"selector_error", "candidate_error"}:
        result["error"] = "service deadline expired"
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["candidate_error", "selector_error", "invalid_selection"])
async def test_technical_failure_is_not_a_rejection_or_training_abstention(tmp_path, monkeypatch, outcome):
    t = _trader(tmp_path)
    configure_cycle(t, monkeypatch, result=[IDEA])
    monkeypatch.setattr(mod.construction, "apply_construction_policy",
                        lambda items, **kw: SimpleNamespace(ideas=items, changes=[], dropped=[]))
    async def materialize(*args):
        t._last_stage_b_outcomes = [row(outcome)]
        return []
    t._materialize_stage_b = materialize
    t._notify_pipeline = AsyncMock()
    await t.run_once(dry_run=True)
    notices = [call for call in t._notify_pipeline.call_args_list if call.args[0] == "contract_error"]
    assert len(notices) == 1 and not notices[0].kwargs.get("recovery")
    assert "SPY" in notices[0].args[1]
    rows = [json.loads(x) for x in (tmp_path / "a.jsonl").read_text().splitlines()]
    no_trade = next(r for r in rows if r["event"] == "entry_pipeline_no_trade")
    assert no_trade["reason"] == "stage_b_error" and no_trade["training_eligible"] is False
    t._submit_order.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcomes, recovers", [
    ([], False),
    ([row("construction_rejected")], False),
    ([row("stage_b_declined")], True),
    ([row("selected"), row("selector_error", "QQQ")], False),
])
async def test_stage_a_alone_does_not_claim_contract_evaluation_recovered(
        tmp_path, monkeypatch, outcomes, recovers):
    t = _trader(tmp_path)
    configure_cycle(t, monkeypatch, result=[IDEA])
    monkeypatch.setattr(mod.construction, "apply_construction_policy",
                        lambda items, **kw: SimpleNamespace(ideas=items, changes=[], dropped=[]))
    selected = [IDEA] if any(r["outcome"] == "selected" for r in outcomes) else []
    async def materialize(*args):
        t._last_stage_b_outcomes = outcomes
        return selected
    t._materialize_stage_b = materialize
    t._drop_blocked_sectors = AsyncMock(return_value=[])
    t._notify_pipeline = AsyncMock()
    await t.run_once(dry_run=True)
    notices = [call for call in t._notify_pipeline.call_args_list if call.args[0] == "contract_error"]
    recovery = [call for call in notices if call.kwargs.get("recovery") is True]
    assert bool(recovery) is recovers
    if selected:
        assert len(notices) == 1 and "QQQ" in notices[0].args[1]

        t._drop_blocked_sectors.assert_awaited_once_with(selected)
    t._submit_order.assert_not_called()


def _daily_outcome_runner(namespace):

    source = inspect.getsource(dr.run)
    start = source.index("            _stage_b_failed =")
    end = source.index("\n        pot = await get_pot_snapshot(ib)", start)
    segment = textwrap.dedent(source[start:end])
    exec("async def outcome():\n" + textwrap.indent(segment, "    ")
         + "\n    return None\n", namespace)
    return namespace["outcome"]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcomes, ideas, expected_status, expected_reason", [
    ([row("selector_error")], [], 1, "stage_b_error"),
    ([row("candidate_error")], [], 1, "stage_b_error"),
    ([row("invalid_selection")], [], 1, "stage_b_error"),
    ([row("stage_b_declined")], [], 0, "stage_b_rejected"),
    ([row("construction_rejected")], [], 0, "stage_b_rejected"),
    ([], [], 0, "empty_slate"),
    ([row("selected"), row("selector_error", "QQQ")], [IDEA], None, None),
])
async def test_daily_exit_status_and_capture_distinguish_failure_from_decline(
        outcomes, ideas, expected_status, expected_reason):
    post, capture, audit = Mock(return_value="receipt"), Mock(), Mock()
    namespace = dict(vars(dr))
    namespace.update(
        _stage_b_outcomes=outcomes, ideas=ideas, token="synthetic", channel="synthetic",
        audit_path="/dev/null", _stage_a_intent_count=2 if outcomes else 0,
        _post_construction_intent_count=2 if outcomes else 0,
        _deferred_stage_a=[], _raw_slate="model reply", _slate_cot=None,
        brief="clean research", _slate_identity={}, _slate_identity_source="meta",
        approval=SimpleNamespace(post_proposal=post), audit=audit,
        trade_capture=SimpleNamespace(capture_no_trade=capture, dataset_dir=lambda x: "synthetic"))
    assert await _daily_outcome_runner(namespace)() == expected_status
    assert post.call_count == 1
    if expected_reason:
        assert capture.call_args.kwargs["reason"] == expected_reason
        assert capture.call_args.kwargs["training_eligible"] is (expected_reason == "empty_slate")
        assert audit.call_args.kwargs["reason"] == expected_reason
    else:
        capture.assert_not_called()
        assert namespace["ideas"] is ideas
    if expected_reason == "stage_b_error" or ideas:
        assert "technical" in post.call_args.args[2] or "incomplete" in post.call_args.args[2]
