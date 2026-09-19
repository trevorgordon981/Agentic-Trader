"""Public API contract; production-derived narrative omitted."""

import inspect
from types import SimpleNamespace

import pytest

import close_symbol as cs
import morning_review as mr
from exitmgr.strategist import EntryContractError


QUEUE_PREFIX = "QUEUED/PENDING — no broker order was placed by this tool; protective="


def _completion(content):
    return {"choices": [{"message": {"content": content}}]}


def test_thesis_review_requests_strict_json_and_validates_before_accepting(monkeypatch):
    seen = {}
    payload = '{"eroded":false,"reason":"trend remains intact","action":"HOLD"}'

    def _post(endpoint, body, timeout, **kwargs):
        seen.update(endpoint=endpoint, body=body, timeout=timeout, kwargs=kwargs)
        result = _completion(payload)
        kwargs["response_validator"](result)
        return result

    monkeypatch.setattr(mr, "_post_json", _post)
    verdict = mr.judge_thesis("http://model", "m", "position", "thesis", "brief")

    assert seen["body"]["response_format"] == mr.REVIEW_RESPONSE_FORMAT
    assert seen["body"]["thinking"] == "disabled"
    assert seen["kwargs"]["response_validator"] is mr._validate_verdict_completion
    assert verdict["_ok"] is True


def test_truncated_thesis_reply_is_route_failure_not_synthetic_hold():
    with pytest.raises(EntryContractError):
        mr._validate_verdict_completion(
            _completion('{"eroded":false,"reason":"unfinished'))

    with pytest.raises(EntryContractError, match="truncated"):
        mr._validate_verdict_completion({
            "choices": [{
                "finish_reason": "length",
                "message": {"content": '{"eroded":false,"reason":"intact"}'},
            }],
        })


@pytest.mark.parametrize("payload", [
    '{"eroded":"false","reason":"intact","action":"HOLD"}',
    '{"eroded":false,"reason":"intact"}',
    '{"eroded":false,"reason":"intact","action":"HOLD","extra":1}',
    '{"eroded":false,"reason":"intact","action":"CONSIDER_SELL"}',
])
def test_invalid_thesis_contract_is_route_failure(payload):
    with pytest.raises(EntryContractError):
        mr._validate_verdict_completion(_completion(payload))


def test_invalid_thesis_reviews_are_reported_unknown_not_still_holds():
    source = inspect.getsource(mr.run)
    assert "unknown.append((it, v))" in source
    assert "Morning thesis review incomplete" in source
    assert "judged and were not treated as HOLD" in source
    assert "emit_reviews(journal_path, [*eroded, *intact]" in source


def test_running_owner_is_pending_not_done():
    status = mr.classify_close_result(0, QUEUE_PREFIX + "RUNNING")
    message = mr.close_status_message("SYMK", status, "[INFO] noisy\n" + QUEUE_PREFIX + "RUNNING")

    assert status == mr.CLOSE_QUEUED
    assert ":hourglass_flowing_sand:" in message
    assert "not a fill" in message
    assert "If and when IBKR confirms" in message
    assert ":white_check_mark:" not in message
    assert "done" not in message.lower()
    assert "[INFO]" not in message


def test_owner_down_is_queued_warning_not_failure():
    status = mr.classify_close_result(75, QUEUE_PREFIX + "NOT_RUNNING")
    message = mr.close_status_message("SYMJ", status)

    assert status == mr.CLOSE_QUEUED_OWNER_DOWN
    assert ":warning:" in message
    assert "exit queued" in message
    assert "no broker order was submitted" in message
    assert ":x:" not in message
    assert "done" not in message.lower()


def test_no_matching_position_does_not_overclaim_broker_flatness():
    output = "no matching open position for symbol='SYMK' con_id=None"
    status = mr.classify_close_result(0, output)
    message = mr.close_status_message("SYMK", status, output)

    assert status == mr.CLOSE_ALREADY_FLAT
    assert "No eligible SYMK position was found for this exit path" in message
    assert "already flat" not in message
    assert "no exit request" in message
    assert "queued" not in message


def test_bare_zero_fails_closed_instead_of_claiming_success():
    status = mr.classify_close_result(0, "unexpected output")
    message = mr.close_status_message("SYMK", status, "unexpected output")

    assert status == mr.CLOSE_REQUEST_FAILED
    assert ":x:" in message
    assert "no fill is being claimed" in message
    assert ":white_check_mark:" not in message
    assert "done" not in message.lower()


def test_nonzero_refusal_has_bounded_diagnostic_only():
    detail = "REFUSED: exact campaign identity changed"
    status = mr.classify_close_result(2, "[INFO] setup\n" + detail)
    message = mr.close_status_message("SYMK", status, "[INFO] setup\n" + detail)

    assert status == mr.CLOSE_REQUEST_FAILED
    assert detail in message
    assert "[INFO]" not in message


def test_run_close_returns_explicit_queue_status_and_suppresses_duplicate(monkeypatch):
    monkeypatch.setattr(mr.os.path, "exists", lambda _path: True)
    seen = {}

    def _run(cmd, **kwargs):
        seen["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout=QUEUE_PREFIX + "RUNNING\n", stderr="")

    monkeypatch.setattr(
        mr.subprocess,
        "run",
        _run,
    )

    status, output = mr.run_close("SYMK")

    assert status == mr.CLOSE_QUEUED
    assert QUEUE_PREFIX in output
    assert "--suppress-slack-status" in seen["cmd"]


def test_run_close_timeout_is_request_failure(monkeypatch):
    monkeypatch.setattr(mr.os.path, "exists", lambda _path: True)

    def _timeout(*args, **kwargs):
        raise TimeoutError("bounded wait elapsed")

    monkeypatch.setattr(mr.subprocess, "run", _timeout)
    status, output = mr.run_close("SYMK")

    assert status == mr.CLOSE_REQUEST_FAILED
    assert "TimeoutError" in output


@pytest.mark.asyncio
async def test_close_symbol_caller_notifies_suppresses_its_duplicate_slack(monkeypatch, tmp_path):
    cfg = SimpleNamespace(
        ib=SimpleNamespace(host="127.0.0.1", port=4001, market_data_type=1),
        journal=SimpleNamespace(path=str(tmp_path / "trades.log")),
    )

    class _IB:
        async def reqPositionsAsync(self):
            return []

    class _Connection:
        def __init__(self, *args, **kwargs):
            self.ib = _IB()

        async def connect(self):
            return True

        async def disconnect(self):
            return None

    queued = []

    class _Queue:
        def __init__(self, path):
            self.path = path

        def add(self, requests):
            queued.extend(requests)

    request = {"parent_con_id": 8001001}
    posts = []
    monkeypatch.setattr(cs, "load_config", lambda _path: cfg)
    monkeypatch.setattr(cs, "freeze_runtime_identity", lambda *a, **k: object())
    monkeypatch.setattr(cs, "IBConnection", _Connection)
    monkeypatch.setattr(cs, "bound_requests", lambda *a, **k: [request])
    monkeypatch.setattr(cs, "ManualExitQueue", _Queue)
    monkeypatch.setattr(cs, "_protective_running", lambda: True)
    monkeypatch.setattr(cs, "slack", lambda message: posts.append(message))

    rc = await cs.run(
        True, 91, symbol="SYMK", config_path=str(tmp_path / "config.yaml"),
        slack_status=False,
    )

    assert rc == 0
    assert queued == [request]
    assert posts == []
