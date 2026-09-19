"""Public API contract; production-derived narrative omitted."""

from contextlib import nullcontext
import http.client
import importlib.util
import json
from pathlib import Path
import sys
import types
import urllib.error

import pytest


STAGED_STRATEGIST = Path(__file__).parents[1] / "exitmgr" / "strategist.py"


class _Response:
    def __init__(self, payload=None, read_error=None, headers=None):
        self.payload = payload
        self.read_error = read_error
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        if self.read_error is not None:
            raise self.read_error
        return self.payload


@pytest.fixture
def strategist(monkeypatch):
    package = types.ModuleType("exitmgr")
    package.__path__ = []

    risk = types.ModuleType("exitmgr.risk")
    risk.INDEX_UNDERLYINGS = {"SPY", "QQQ", "IWM"}

    provenance = types.ModuleType("exitmgr.provenance")

    class RuntimeIdentityError(RuntimeError):
        pass

    provenance.RuntimeIdentityError = RuntimeIdentityError
    provenance.IDENTITY_SCHEMA = "test"
    provenance.priority_headers = lambda _priority: {}
    provenance.identity_required = lambda: False
    provenance.runtime_snapshot = lambda _endpoint, **_kwargs: None
    provenance.request_identity = lambda **kwargs: {"verified": True, **kwargs["after"]}
    provenance.sha256 = lambda _value: "test-sha"

    entry_contract = types.ModuleType("exitmgr.entry_contract")
    entry_contract.CONTRACT_VERSION = "test"
    entry_contract.DEBIT_HOLD_FLOOR_MULTIPLE = 8
    entry_contract.EntryContractError = type("EntryContractError", (ValueError,), {})
    entry_contract.RuntimeCandidate = type("RuntimeCandidate", (), {})
    entry_contract.StageAIntent = type("StageAIntent", (), {})
    entry_contract.parse_json_document = json.loads
    entry_contract.parse_stage_a = lambda _value: []
    entry_contract.parse_stage_b = lambda _value, _candidates: None
    entry_contract.validate_candidates = lambda _intent_id, _intent, candidates: candidates

    lease = types.ModuleType("glm_trader_lease")

    class LeaseError(RuntimeError):
        pass

    lease.GLM_ENDPOINT = "http://127.0.0.1:18080/v1/chat/completions"
    lease.GLM_MODEL = "glm-5.3-flash-candidate"
    lease.LeaseError = LeaseError
    lease.trader_glm_lease = lambda _timeout: nullcontext()

    package.provenance = provenance
    for name, module in (
        ("exitmgr", package),
        ("exitmgr.risk", risk),
        ("exitmgr.provenance", provenance),
        ("exitmgr.entry_contract", entry_contract),
        ("glm_trader_lease", lease),
    ):
        monkeypatch.setitem(sys.modules, name, module)

    module_name = "staged_strategist_under_test"
    spec = importlib.util.spec_from_file_location(module_name, STAGED_STRATEGIST)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    monkeypatch.delenv("SLATE_POST_RETRIES", raising=False)
    monkeypatch.delenv("TRADER_GLM_ONLY", raising=False)
    return module


def _body(strategist):
    return {
        "model": strategist._GLM_PRIMARY_MODEL,
        "messages": [
            {"role": "system", "content": "locked trading authority"},
            {"role": "user", "content": "market context"},
        ],
        "thinking": "enabled",
        "temperature": 0.2,
        "max_tokens": 777,
    }


def _completion(content="decision"):
    return json.dumps({"choices": [{"message": {"content": content}}]}).encode()


@pytest.mark.parametrize(
    ("setting", "enabled", "effort"),
    [("disabled", False, "none"), ("enabled", True, "high")],
)
def test_glm_wire_uses_supported_template_thinking_flag(
        strategist, setting, enabled, effort):
    body = _body(strategist)
    body["thinking"] = setting
    body["enable_thinking"] = not enabled
    body["chat_template_kwargs"] = {"preserve_me": "yes", "thinking": not enabled}

    translated = strategist._glm_request_body(body)

    assert "thinking" not in translated
    assert "enable_thinking" not in translated
    assert translated["reasoning_effort"] == effort
    assert translated["chat_template_kwargs"] == {
        "preserve_me": "yes",
        "enable_thinking": enabled,
    }


def test_glm_wire_rejects_non_object_template_kwargs(strategist):
    body = _body(strategist)
    body["chat_template_kwargs"] = "invalid"

    with pytest.raises(ValueError, match="chat_template_kwargs must be an object"):
        strategist._glm_request_body(body)


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("spark_fallback", [False, True])
def test_dsv41_normalizes_conflicting_wire_aliases_without_mutating_caller(
        strategist, enabled, spark_fallback):
    body = _body(strategist)
    body.update(model=strategist._DSV41_PRIMARY_MODEL,
                thinking="enabled" if enabled else "disabled",
                enable_thinking=not enabled, thinking_mode="stale", think=not enabled,
                reasoning_effort="none" if enabled else "high",
                chat_template_kwargs={"thinking": not enabled, "enable_thinking": not enabled,
                                      "thinking_mode": "stale", "think": not enabled,
                                      "preserve_me": 7})
    before = json.dumps(body, sort_keys=True)
    translated = strategist._dsv41_request_body(body, spark_fallback=spark_fallback)
    expected = ({"thinking": enabled} if spark_fallback else
                {"enable_thinking": enabled, "thinking_mode": "thinking" if enabled else "chat"})
    assert translated["chat_template_kwargs"] == {"preserve_me": 7, **expected}
    assert translated["reasoning_effort"] == ("high" if enabled else "none")
    assert all(key not in translated for key in ("thinking", "enable_thinking", "thinking_mode", "think"))
    for key in ("model", "messages", "temperature", "max_tokens"):
        assert translated[key] == body[key]
    assert json.dumps(body, sort_keys=True) == before


@pytest.mark.parametrize("bad_kwargs", ["bad", [], False])
def test_dsv41_rejects_non_object_kwargs(strategist, bad_kwargs):
    with pytest.raises(ValueError, match="chat_template_kwargs must be an object"):
        strategist._dsv41_request_body({"thinking": "enabled", "chat_template_kwargs": bad_kwargs})


def test_dsv41_requires_explicit_stage_setting(strategist):
    with pytest.raises(ValueError, match="no explicit thinking setting"):
        strategist._dsv41_request_body({"model": strategist._DSV41_PRIMARY_MODEL})


@pytest.mark.parametrize("stage,enabled,budget", [
    ("continuous", False, 1400), ("slate", True, 24000),
    ("directed", True, 24000), ("selection", True, 12000),
    ("discovery", True, 12000),
])
@pytest.mark.parametrize("fallback", [False, True])
def test_stage_calls_reach_exact_server_with_intended_reasoning(
        strategist, monkeypatch, stage, enabled, budget, fallback):
    monkeypatch.delenv("SLATE_THINKING", raising=False)
    calls = []
    def fake_urlopen(request, timeout):
        calls.append((request.full_url, json.loads(request.data)))
        if fallback and len(calls) == 1:
            raise _http_error(503)
        return _Response(_completion('{"intents":[],"candidates":[]}'))
    monkeypatch.setattr(strategist.urllib.request, "urlopen", fake_urlopen)


    original = strategist._post_json
    monkeypatch.setattr(strategist, "_post_json", lambda *a, **kw: original(*a, **kw, retries=1))
    args = (strategist._GLM_PRIMARY_ENDPOINT, strategist._DSV41_PRIMARY_MODEL)
    if stage == "selection":
        monkeypatch.setattr(strategist, "_stage_b_payload", lambda *a: ({"intent_id": "test"}, [], {}))
        strategist.select_candidate(*args, None, [])
    elif stage == "discovery":
        strategist.discover_names(*args, "synthetic context", exclude=[])
    else:
        kwargs = ({"thinking": "disabled"} if stage == "continuous" else
                  {"recommend": True} if stage == "slate" else {"ticker": "TEST"})
        strategist.propose_intents(*args, "synthetic context", **kwargs)
    assert len(calls) == (2 if fallback else 1)
    assert calls[0][0] == strategist._GLM_PRIMARY_ENDPOINT
    for index, (endpoint, body) in enumerate(calls):
        assert strategist._thinking_enabled(body) is enabled
        assert body["max_tokens"] == budget
        assert body["reasoning_effort"] == ("high" if enabled else "none")
        if index == 0:
            assert body["model"] == strategist._DSV41_PRIMARY_MODEL
            assert body["chat_template_kwargs"] == {
                "enable_thinking": enabled, "thinking_mode": "thinking" if enabled else "chat"}
        else:
            assert endpoint == strategist._DEEPSEEK_FALLBACK_ENDPOINT
            assert body["model"] == strategist._DEEPSEEK_FALLBACK_MODEL
            assert body["chat_template_kwargs"] == {"thinking": enabled}
            assert body["messages"] == calls[0][1]["messages"]


def _http_error(status):
    return urllib.error.HTTPError("http://model", status, "failure", None, None)


def _route(strategist, monkeypatch, outcomes, *, retries=1):
    pending = list(outcomes)
    calls = []

    def fake_urlopen(request, timeout):
        calls.append((request.full_url, json.loads(request.data), timeout))
        outcome = pending.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(strategist.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(strategist.time, "sleep", lambda _seconds: None)
    result = strategist._post_json(
        strategist._GLM_PRIMARY_ENDPOINT,
        _body(strategist),
        timeout=9,
        retries=retries,
        backoff=0,
    )
    return result, calls


@pytest.mark.parametrize("status", [500, 501, 503, 507, 599, 429])
def test_retryable_http_status_falls_back_to_exact_deepseek(
        strategist, monkeypatch, status):
    result, calls = _route(
        strategist,
        monkeypatch,
        [_http_error(status), _Response(_completion("fallback"))],
    )

    assert result["choices"][0]["message"]["content"] == "fallback"
    assert [call[0] for call in calls] == [
        strategist._GLM_PRIMARY_ENDPOINT,
        strategist._DEEPSEEK_FALLBACK_ENDPOINT,
    ]


def test_retryable_5xx_retries_primary_before_fallback(strategist, monkeypatch):
    result, calls = _route(
        strategist,
        monkeypatch,
        [_http_error(500), _http_error(500), _Response(_completion("fallback"))],
        retries=2,
    )

    assert result["choices"][0]["message"]["content"] == "fallback"
    assert [call[0] for call in calls] == [
        strategist._GLM_PRIMARY_ENDPOINT,
        strategist._GLM_PRIMARY_ENDPOINT,
        strategist._DEEPSEEK_FALLBACK_ENDPOINT,
    ]


@pytest.mark.parametrize(
    "failure",
    [
        urllib.error.URLError("offline"),
        TimeoutError("timed out"),
        ConnectionResetError("reset"),
        http.client.BadStatusLine("bad status"),
    ],
    ids=["url-error", "timeout", "connection-reset", "http-protocol"],
)
def test_transport_or_timeout_falls_back(strategist, monkeypatch, failure):
    result, calls = _route(
        strategist,
        monkeypatch,
        [failure, _Response(_completion("fallback"))],
    )

    assert result["choices"][0]["message"]["content"] == "fallback"
    assert calls[-1][0] == strategist._DEEPSEEK_FALLBACK_ENDPOINT


@pytest.mark.parametrize(
    "bad_response",
    [
        _Response(b'{"choices": ['),
        _Response(b"\xff"),
        _Response(read_error=http.client.IncompleteRead(b'{"choices":', 20)),
    ],
    ids=["malformed-json", "invalid-utf8", "truncated-http-body"],
)
def test_malformed_or_truncated_response_falls_back(
        strategist, monkeypatch, bad_response):
    result, calls = _route(
        strategist,
        monkeypatch,
        [bad_response, _Response(_completion("fallback"))],
    )

    assert result["choices"][0]["message"]["content"] == "fallback"
    assert calls[-1][0] == strategist._DEEPSEEK_FALLBACK_ENDPOINT


@pytest.mark.parametrize(
    "payload",
    [
        b"[]",
        b"{}",
        b'{"choices": {}}',
        b'{"choices": [{"message": "not-an-object"}]}',
    ],
    ids=["top-level-list", "missing-choices", "choices-not-list", "message-not-object"],
)
def test_invalid_completion_shape_falls_back(strategist, monkeypatch, payload):
    result, _calls = _route(
        strategist,
        monkeypatch,
        [_Response(payload), _Response(_completion("fallback"))],
    )
    assert result["choices"][0]["message"]["content"] == "fallback"


def test_nonretryable_4xx_fails_closed_without_fallback(strategist, monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        raise _http_error(401)

    monkeypatch.setattr(strategist.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(urllib.error.HTTPError) as raised:
        strategist._post_json(
            strategist._GLM_PRIMARY_ENDPOINT,
            _body(strategist),
            timeout=9,
            retries=1,
            backoff=0,
        )

    assert raised.value.code == 401
    assert calls == [strategist._GLM_PRIMARY_ENDPOINT]


def test_strict_schema_warning_rejects_primary_and_uses_fallback(
        strategist, monkeypatch):
    body = _body(strategist)
    body["response_format"] = {
        "type": "json_schema",
        "json_schema": {"name": "stage_a", "strict": True, "schema": {}},
    }
    calls = []
    outcomes = [
        _Response(
            _completion("best effort"),
            headers={"Warning": '199 omlx "strict json_schema not enforced"'},
        ),
        _Response(_completion("enforced fallback")),
    ]

    def fake_urlopen(request, timeout):
        del timeout
        calls.append(request.full_url)
        return outcomes.pop(0)

    monkeypatch.setattr(strategist.urllib.request, "urlopen", fake_urlopen)
    result = strategist._post_json(
        strategist._GLM_PRIMARY_ENDPOINT, body, timeout=9, retries=1, backoff=0,
    )

    assert result["choices"][0]["message"]["content"] == "enforced fallback"
    assert calls == [
        strategist._GLM_PRIMARY_ENDPOINT,
        strategist._DEEPSEEK_FALLBACK_ENDPOINT,
    ]


def test_contract_invalid_primary_uses_exact_fallback(strategist, monkeypatch):
    seen = []

    def validator(posted):
        content = posted["choices"][0]["message"]["content"]
        seen.append(content)
        if content == "invalid":
            raise strategist.EntryContractError("unexpected key")

    outcomes = [_Response(_completion("invalid")), _Response(_completion("valid"))]
    calls = []

    def fake_urlopen(request, timeout):
        del timeout
        calls.append(request.full_url)
        return outcomes.pop(0)

    monkeypatch.setattr(strategist.urllib.request, "urlopen", fake_urlopen)
    result = strategist._post_json(
        strategist._GLM_PRIMARY_ENDPOINT,
        _body(strategist),
        timeout=9,
        retries=1,
        backoff=0,
        response_validator=validator,
    )

    assert result["choices"][0]["message"]["content"] == "valid"
    assert seen == ["invalid", "valid"]
    assert calls == [
        strategist._GLM_PRIMARY_ENDPOINT,
        strategist._DEEPSEEK_FALLBACK_ENDPOINT,
    ]


def test_contract_invalid_output_is_never_normalized(strategist, monkeypatch):
    raw = '{"intents":[],"conviction_note_used":true}'
    fallback = '{"intents":[]}'

    def validator(posted):
        content = posted["choices"][0]["message"]["content"]
        if "conviction_note_used" in content:
            raise strategist.EntryContractError(
                "intents[0] keys mismatch; extra=['conviction_note_used']")

    outcomes = [_Response(_completion(raw)), _Response(_completion(fallback))]
    monkeypatch.setattr(
        strategist.urllib.request, "urlopen",
        lambda request, timeout: outcomes.pop(0),
    )
    result = strategist._post_json(
        strategist._GLM_PRIMARY_ENDPOINT,
        _body(strategist),
        timeout=9,
        retries=1,
        backoff=0,
        response_validator=validator,
    )

    assert result["choices"][0]["message"]["content"] == fallback
    assert "conviction_note_used" not in result["choices"][0]["message"]["content"]


def test_both_routes_contract_invalid_fail_closed_with_both_causes(
        strategist, monkeypatch):
    outcomes = [_Response(_completion("bad-primary")), _Response(_completion("bad-fallback"))]
    monkeypatch.setattr(
        strategist.urllib.request, "urlopen",
        lambda request, timeout: outcomes.pop(0),
    )

    def validator(posted):
        content = posted["choices"][0]["message"]["content"]
        raise strategist.EntryContractError(f"invalid {content}")

    with pytest.raises(strategist.ModelRouteUnavailable) as raised:
        strategist._post_json(
            strategist._GLM_PRIMARY_ENDPOINT,
            _body(strategist),
            timeout=9,
            retries=1,
            backoff=0,
            response_validator=validator,
        )

    message = str(raised.value)
    assert "primary:" in message
    assert "deepseek-fallback:" in message
    assert "bad-primary" in message
    assert "bad-fallback" in message


def test_contract_invalid_glm_only_fails_closed(strategist, monkeypatch):
    monkeypatch.setenv("TRADER_GLM_ONLY", "1")
    calls = []

    def fake_urlopen(request, timeout):
        del timeout
        calls.append(request.full_url)
        return _Response(_completion("bad-primary"))

    monkeypatch.setattr(strategist.urllib.request, "urlopen", fake_urlopen)

    def validator(posted):
        del posted
        raise strategist.EntryContractError("invalid exact output")

    with pytest.raises(strategist.ModelRouteUnavailable, match="contract-invalid"):
        strategist._post_json(
            strategist._GLM_PRIMARY_ENDPOINT,
            _body(strategist),
            timeout=9,
            retries=1,
            backoff=0,
            response_validator=validator,
        )

    assert calls == [strategist._GLM_PRIMARY_ENDPOINT]


def test_contract_valid_primary_does_not_fallback(strategist, monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        del timeout
        calls.append(request.full_url)
        return _Response(_completion("valid-primary"))

    monkeypatch.setattr(strategist.urllib.request, "urlopen", fake_urlopen)
    result = strategist._post_json(
        strategist._GLM_PRIMARY_ENDPOINT,
        _body(strategist),
        timeout=9,
        retries=1,
        backoff=0,
        response_validator=lambda posted: None,
    )

    assert result["choices"][0]["message"]["content"] == "valid-primary"
    assert calls == [strategist._GLM_PRIMARY_ENDPOINT]


def test_fallback_changes_only_endpoint_and_model_authority(strategist, monkeypatch):
    original = _body(strategist)
    _result, calls = _route(
        strategist,
        monkeypatch,
        [TimeoutError("offline"), _Response(_completion("fallback"))],
    )

    fallback_body = calls[1][1]
    assert calls[1][0] == strategist._DEEPSEEK_FALLBACK_ENDPOINT
    assert fallback_body["model"] == strategist._DEEPSEEK_FALLBACK_MODEL
    assert {key: value for key, value in fallback_body.items() if key != "model"} == {
        key: value for key, value in original.items() if key != "model"
    }


def test_glm_only_switch_still_fails_closed(strategist, monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        raise TimeoutError("offline")

    monkeypatch.setenv("TRADER_GLM_ONLY", "1")
    monkeypatch.setattr(strategist.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(strategist.ModelRouteUnavailable):
        strategist._post_json(
            strategist._GLM_PRIMARY_ENDPOINT,
            _body(strategist),
            timeout=9,
            retries=1,
            backoff=0,
        )

    assert calls == [strategist._GLM_PRIMARY_ENDPOINT]


def test_blank_content_with_tool_call_remains_usable(strategist, monkeypatch):
    tool_call = {
        "choices": [{
            "message": {
                "content": "",
                "tool_calls": [{"id": "call-1", "type": "function"}],
            },
        }],
    }
    result, calls = _route(
        strategist,
        monkeypatch,
        [_Response(json.dumps(tool_call).encode())],
    )

    assert result == tool_call
    assert [call[0] for call in calls] == [strategist._GLM_PRIMARY_ENDPOINT]


def test_identity_snapshot_retries_do_not_reissue_model_request(
        strategist, monkeypatch):
    class RuntimeIdentityError(strategist.provenance.RuntimeIdentityError):
        pass

    runtime = {
        "artifact_id": "artifact", "artifact_manifest_sha256": "a" * 64,
        "runtime_receipt_sha256": "b" * 64,
        "runtime_contract_sha256": "c" * 64,
        "model_realpath": "/model", "startup_nonce": "nonce",
    }
    snapshots = [
        RuntimeIdentityError("before transient"), runtime,
        RuntimeIdentityError("after transient"), runtime,
    ]
    snapshot_calls = []

    def runtime_snapshot(endpoint, **_kwargs):
        snapshot_calls.append(endpoint)
        outcome = snapshots.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    posts = []

    def fake_urlopen(request, timeout):
        del timeout
        posts.append(request.full_url)
        return _Response(_completion())

    monkeypatch.setattr(strategist.provenance, "runtime_snapshot", runtime_snapshot)
    monkeypatch.setattr(strategist.provenance, "identity_required", lambda: True)
    monkeypatch.setattr(strategist.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(strategist.time, "sleep", lambda _seconds: None)
    result, identity = strategist._post_json(
        strategist._GLM_PRIMARY_ENDPOINT,
        _body(strategist),
        timeout=9,
        retries=1,
        backoff=0,
        return_identity=True,
    )
    assert result["choices"][0]["message"]["content"] == "decision"
    assert identity["verified"] is True
    assert len(snapshot_calls) == 4
    assert posts == [strategist._GLM_PRIMARY_ENDPOINT]


def test_both_route_failures_report_primary_and_fallback(
        strategist, monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        del timeout
        calls.append(request.full_url)
        raise TimeoutError("offline")

    monkeypatch.setattr(strategist.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(strategist.time, "sleep", lambda _seconds: None)
    with pytest.raises(strategist.ModelRouteUnavailable) as raised:
        strategist._post_json(
            strategist._GLM_PRIMARY_ENDPOINT,
            _body(strategist),
            timeout=9,
            retries=1,
            backoff=0,
        )
    message = str(raised.value)
    assert "primary: ModelRouteUnavailable" in message
    assert "deepseek-fallback: ModelRouteUnavailable" in message
    assert calls == [
        strategist._GLM_PRIMARY_ENDPOINT,
        strategist._DEEPSEEK_FALLBACK_ENDPOINT,
    ]


def test_fallback_nonretryable_4xx_retains_primary_failure(
        strategist, monkeypatch):
    outcomes = [TimeoutError("primary offline"), _http_error(401)]
    calls = []

    def fake_urlopen(request, timeout):
        del timeout
        calls.append(request.full_url)
        raise outcomes.pop(0)

    monkeypatch.setattr(strategist.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(strategist.time, "sleep", lambda _seconds: None)
    with pytest.raises(strategist.ModelRouteUnavailable) as raised:
        strategist._post_json(
            strategist._GLM_PRIMARY_ENDPOINT,
            _body(strategist),
            timeout=9,
            retries=1,
            backoff=0,
        )
    message = str(raised.value)
    assert "primary: ModelRouteUnavailable" in message
    assert "deepseek-fallback: HTTPError" in message
    assert calls == [
        strategist._GLM_PRIMARY_ENDPOINT,
        strategist._DEEPSEEK_FALLBACK_ENDPOINT,
    ]
