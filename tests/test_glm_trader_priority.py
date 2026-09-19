import ast
import hashlib
import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from .test_strategist_fallback import strategist

import glm_trader_lease as lease


ROOT = Path(__file__).parents[1]


def _gateway_root():
    explicit = os.environ.get("EXITMGR_TEST_GATEWAY_ROOT")
    path = Path(explicit).expanduser() if explicit else Path.home() / "model-gateway"
    if not explicit and not path.exists():
        pytest.skip("deployment check: supply EXITMGR_TEST_GATEWAY_ROOT or run on gateway host")
    assert path.is_dir(), f"configured gateway deployment is missing: {path}"
    return path


def test_trader_context_releases_intent_and_slot_on_exception():
    intent = SimpleNamespace(release=mock.Mock())
    slot = SimpleNamespace(release=mock.Mock())
    with mock.patch.object(
            lease.RetainedFlock, "try_acquire", side_effect=[intent, slot]):
        with pytest.raises(RuntimeError, match="probe"):
            with lease.trader_glm_lease(0):
                intent.release.assert_called_once()
                raise RuntimeError("probe")
    slot.release.assert_called_once()


def test_chat_fails_closed_while_trader_intent_is_present():
    with mock.patch.object(
            lease.RetainedFlock, "try_acquire",
            side_effect=lease.TraderWaiting("trader intent is active")):
        with pytest.raises(lease.TraderWaiting):
            lease.claim_chat_glm_slot()


def test_kernel_lock_protocol_replaces_pid_json_cleanup():
    source = (ROOT / "glm_trader_lease.py").read_text()
    assert "fcntl.flock" in source
    assert "active.json" not in source
    assert "_process_start" not in source


def test_app_and_gateway_share_identical_admission_module():
    app = (ROOT / "glm_trader_lease.py").read_bytes()
    gateway = (_gateway_root() / "glm_trader_lease.py").read_bytes()
    assert hashlib.sha256(app).digest() == hashlib.sha256(gateway).digest()


def test_gateway_checks_both_chat_surfaces():
    """Public API contract; production-derived narrative omitted."""
    source = (_gateway_root() / "gateway.py").read_text()
    tree = ast.parse(source)
    functions = {node.name: node for node in tree.body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}

    def calls(name, target):
        return [node for node in ast.walk(functions[name])
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == target]

    assert len(calls("chat_completions", "_resolve_request_chain")) == 1
    assert len(calls("messages", "_resolve_request_chain")) == 1
    assert calls("_resolve_request_chain", "prioritize_exact_glm_trader")
    assert calls("_resolve_request_chain", "_request_has_images")
    resolver = ast.get_source_segment(source, functions["_resolve_request_chain"])
    assert "_EXACT_DSV41_BACKEND" in resolver
    assert 'http://127.0.0.1:18080/v1' in resolver
    assert "/opt/agentic-trader/models/example-mtp-artifact" in resolver
    assert any(
        isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Call)
        and isinstance(node.exc.func, ast.Name)
        and node.exc.func.id == "HTTPException"
        and any(keyword.arg == "status_code"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value == 503
                for keyword in node.exc.keywords)
        for node in ast.walk(functions["_resolve_request_chain"])
    )


def test_exact_trader_route_has_behavioral_admission_and_identity_contract(
        strategist, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    from .test_strategist_fallback import _Response, _body, _completion

    runtime = {
        "artifact_id": "exact-runtime", "artifact_manifest_sha256": "a" * 64,
        "runtime_receipt_sha256": "b" * 64, "runtime_contract_sha256": "c" * 64,
        "model_realpath": "/model", "startup_nonce": "nonce",
    }
    events = []

    @contextmanager
    def admission(timeout):
        assert timeout > 0
        events.append("acquire")
        try:
            yield
        finally:
            events.append("release")

    def respond(request, timeout):
        assert timeout > 0
        events.append("http")
        return _Response(_completion())

    monkeypatch.setattr(strategist, "trader_glm_lease", admission)
    monkeypatch.setattr(strategist.provenance, "runtime_snapshot",
                        lambda *a, **k: dict(runtime))
    monkeypatch.setattr(strategist.urllib.request, "urlopen", respond)
    result, identity = strategist._post_json_one(
        lease.GLM_ENDPOINT, _body(strategist), 10, retries=1, return_identity=True)

    assert result["choices"][0]["message"]["content"] == "decision"
    assert events == ["acquire", "http", "release"]
    assert identity["verified"] is True
    assert identity["artifact_id"] == "exact-runtime"


@pytest.mark.parametrize("endpoint,model,expected_leases", [
    (lease.GLM_ENDPOINT, lease.GLM_MODEL, 1),
    (lease.GLM_ENDPOINT, "example-mtp-model", 1),
    (lease.GLM_ENDPOINT, "unqualified-model", 0),
    ("http://spark.invalid:8888/v1/chat/completions", "example-mtp-model", 0),
])
def test_trader_leases_only_exact_trader_host_attempts(strategist, monkeypatch,
                                                  endpoint, model, expected_leases):
    from .test_strategist_fallback import _Response, _completion
    events = []
    @contextmanager
    def admission(timeout):
        assert timeout > 0
        events.append("acquire")
        try:
            yield
        finally:
            events.append("release")
    def respond(request, timeout):
        events.append("http")
        return _Response(_completion())
    monkeypatch.setattr(strategist, "trader_glm_lease", admission)
    monkeypatch.setattr(strategist.urllib.request, "urlopen", respond)
    strategist._post_json_one(endpoint, {"model": model}, 10, retries=1)
    assert events == (["acquire", "http", "release"] if expected_leases else ["http"])


def test_portfolio_delegates_to_single_admission_boundary():
    portfolio = (ROOT / "portfolio.py").read_text()
    assert "from exitmgr.strategist import _post_json" in portfolio
    assert "result = _post_json(ENDPOINT, body, timeout, retries=5)" in portfolio
    assert "with trader_glm_lease" not in portfolio


def test_all_trader_glm_route_constants_are_exactly_aligned():
    from exitmgr import provenance, strategist

    expected_endpoint = "http://127.0.0.1:18080/v1/chat/completions"
    expected_model = "glm-5.3-flash-candidate"
    assert lease.GLM_ENDPOINT == expected_endpoint
    assert lease.GLM_MODEL == expected_model
    assert provenance._GLM_ENDPOINT == expected_endpoint
    assert provenance._GLM_MODEL == expected_model
    assert strategist._GLM_PRIMARY_ENDPOINT == expected_endpoint
    assert strategist._GLM_PRIMARY_MODEL == expected_model
    config = (ROOT / "config.yaml").read_text()
    assert f"llm_endpoint: {expected_endpoint}" in config

    assert strategist._DSV41_PRIMARY_MODEL == "example-mtp-model"
    assert f"llm_model: {strategist._DSV41_PRIMARY_MODEL}" in config
