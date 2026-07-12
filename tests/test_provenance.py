import json
import os

import pytest

from exitmgr import provenance


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def test_priority_zero_requires_owner_only_token_file(tmp_path, monkeypatch):
    token = tmp_path / "priority-token"
    token.write_text("x" * 40)
    token.chmod(0o600)
    monkeypatch.setenv("M3_PRIORITY_TOKEN_FILE", str(token))
    headers = provenance.priority_headers(0)
    assert headers["X-M3-Priority"] == "0"
    assert headers["X-M3-Priority-Token"] == "x" * 40

    token.chmod(0o644)
    monkeypatch.setenv("TRADER_REQUIRE_PRIORITY_TOKEN", "1")
    with pytest.raises(provenance.RuntimeIdentityError):
        provenance.priority_headers(0)


def test_request_identity_binds_runtime_and_exact_material():
    runtime = {
        "artifact_id": "artifact-a", "artifact_manifest_sha256": "a" * 64,
        "runtime_receipt_sha256": "b" * 64, "runtime_contract_sha256": "c" * 64,
        "model_realpath": "/models/a", "startup_nonce": "nonce",
    }
    body = {"model": "ignored-label", "messages": [
        {"role": "system", "content": "system"}, {"role": "user", "content": "context"}],
        "temperature": 0.4}
    identity = provenance.request_identity(
        endpoint="http://127.0.0.1:8082/v1/chat/completions", body=body,
        response={"choices": []}, before=runtime, after=runtime)
    assert identity["artifact_id"] == "artifact-a"
    assert identity["verified"] is True
    assert identity["system_prompt_sha256"] == provenance.sha256("system")
    assert identity["context_sha256"] == provenance.sha256("context")
    assert identity["request_sha256"] == provenance.sha256(body)

    changed = dict(runtime, startup_nonce="other")
    with pytest.raises(provenance.RuntimeIdentityError):
        provenance.request_identity(endpoint="http://x/v1/chat/completions", body=body,
                                    response={}, before=runtime, after=changed)


def test_runtime_snapshot_proves_custom_backend_from_runtime_receipt():
    payload = {
        "ready": True,
        "artifact_id": "artifact-a",
        "artifact_manifest_sha256": "a" * 64,
        "runtime_receipt_sha256": "b" * 64,
        "runtime_contract_sha256": "c" * 64,
        "readiness_smoke_sha256": "d" * 64,
        "binding_kind": "pipeline-artifact",
        "model_realpath": "/models/a",
        "startup_nonce": "nonce",
        "runtime_receipt": {
            "schema": "pipeline-m3-runtime.v1",
            "contract": {"backend": "custom-python-mlx"},
        },
    }
    identity = provenance.runtime_snapshot(
        "http://127.0.0.1:8082/v1/chat/completions",
        opener=lambda request, timeout: _Response(payload))
    assert identity["runtime_backend"] == "custom-python-mlx"
    assert identity["runtime_schema"] == "pipeline-m3-runtime.v1"

    payload["runtime_receipt"] = None
    with pytest.raises(provenance.RuntimeIdentityError, match="runtime_backend"):
        provenance.runtime_snapshot(
            "http://127.0.0.1:8082/v1/chat/completions",
            opener=lambda request, timeout: _Response(payload))
