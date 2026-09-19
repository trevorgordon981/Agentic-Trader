import json
import os
from pathlib import Path

import pytest

from exitmgr import provenance


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def _omlx_payloads(model_view, actual_size=1200000000):
    return {
        "/health": {
            "status": "healthy",
            "default_model": "glm-5.3-flash-candidate",
        },
        "/v1/models": {
            "object": "list",
            "data": [{"id": "glm-5.3-flash-candidate"}],
        },
        "/api/status": {
            "status": "ok",
            "version": "0.0.0-public",
            "default_model": "glm-5.3-flash-candidate",
            "loaded_models": ["glm-5.3-flash-candidate"],
            "models_loaded": 1,
            "models_loading": 0,

            "active_requests": 1,
            "waiting_requests": 0,
        },
        "/v1/models/status": {
            "models": [{
                "id": "glm-5.3-flash-candidate",
                "model_path": str(model_view),
                "loaded": True,
                "is_loading": False,
                "actual_size": actual_size,
                "resident_estimated_size": 1234567890,
                "engine_type": "vlm",
                "config_model_type": "glm5_next",
                "model_context_length": 131072,
                "max_context_window": 131072,
                "max_tokens": 32768,
            }],
        },
    }


def _bind_omlx_fixture(tmp_path, monkeypatch, mutate=None):
    target = tmp_path / "model-target"
    target.mkdir()
    view = tmp_path / "model-view"
    view.mkdir()
    receipt_names = {
        "COMPLETE_RECEIPT.json": "complete",
        "STRUCTURE_RECEIPT.json": "structure",
        "CANARY_RECEIPT.json": "canary",
    }
    for name, contents in receipt_names.items():
        source = target / name
        source.write_text(contents)
        source.chmod(0o400)
        (view / name).symlink_to(source)
    original_template = target / "chat_template.jinja"
    original_template.write_text("original-template")
    original_template.chmod(0o400)
    for index in range(provenance._GLM_ARTIFACT_FILE_COUNT - len(receipt_names) - 1):
        source = target / f"artifact-{index:02d}.bin"
        source.write_bytes(bytes([index % 251]))
        source.chmod(0o400)
        (view / source.name).symlink_to(source)
    template = view / "chat_template.jinja"
    template.write_text("template")
    template.chmod(0o400)
    (view / provenance._GLM_MODEL).symlink_to(view, target_is_directory=True)
    target.chmod(0o500)
    plist = tmp_path / "ai.example.model-candidate.plist"
    plist.write_text("immutable-test-plist")
    plist.chmod(0o600)
    monkeypatch.setattr(provenance, "_GLM_MODEL_VIEW", view)
    monkeypatch.setattr(provenance, "_GLM_MODEL_TARGET", target)
    monkeypatch.setattr(provenance, "_GLM_PLIST", plist)
    monkeypatch.setattr(provenance, "_GLM_PLIST_SHA256", provenance._file_sha256(plist))
    monkeypatch.setattr(
        provenance, "_GLM_COMPLETE_RECEIPT_SHA256",
        provenance._file_sha256(target / "COMPLETE_RECEIPT.json"),
    )
    monkeypatch.setattr(
        provenance, "_GLM_STRUCTURE_RECEIPT_SHA256",
        provenance._file_sha256(target / "STRUCTURE_RECEIPT.json"),
    )
    monkeypatch.setattr(
        provenance, "_GLM_CANARY_RECEIPT_SHA256",
        provenance._file_sha256(target / "CANARY_RECEIPT.json"),
    )
    monkeypatch.setattr(
        provenance, "_GLM_CHAT_TEMPLATE_SHA256", provenance._file_sha256(template),
    )
    payloads = _omlx_payloads(view)
    if mutate:
        mutate(payloads)

    def opener(request, timeout):
        del timeout
        return _Response(payloads[Path(request.full_url).as_posix().split("18080", 1)[-1]])

    def command(argv):
        if argv[:2] == ("/bin/launchctl", "print"):
            return "state = running\npid = 4242"
        if argv[:2] == ("/bin/ps", "-p"):
            return "Thu Aug 27 21:14:55 2026"
        if argv[0] == "/usr/sbin/lsof":
            return "p4242\nf4\nn127.0.0.1:18080"
        raise AssertionError(argv)

    monkeypatch.setattr(provenance, "_command_output", command)
    return opener


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
    assert identity["system_prompt_sha256"] == provenance.sha256("system")
    assert identity["context_sha256"] == provenance.sha256("context")
    assert identity["request_sha256"] == provenance.sha256(body)

    changed = dict(runtime, startup_nonce="other")
    with pytest.raises(provenance.RuntimeIdentityError):
        provenance.request_identity(endpoint="http://x/v1/chat/completions", body=body,
                                    response={}, before=runtime, after=changed)


def test_exact_omlx_runtime_identity_passes_all_bound_surfaces(tmp_path, monkeypatch):
    opener = _bind_omlx_fixture(tmp_path, monkeypatch)
    runtime = provenance.runtime_snapshot(provenance._GLM_ENDPOINT, opener=opener)
    assert runtime["model_id"] == "glm-5.3-flash-candidate"
    assert runtime["model_realpath"] == str(tmp_path / "model-target")
    assert runtime["binding_kind"] == "omlx-openai-local"
    assert runtime["artifact_manifest_sha256"]
    assert runtime["runtime_contract_sha256"]
    assert runtime["startup_nonce"]


def test_omlx_actual_size_is_volatile_telemetry(tmp_path, monkeypatch):
    captured = {}

    def capture(payloads):
        captured["payloads"] = payloads

    opener = _bind_omlx_fixture(tmp_path, monkeypatch, capture)
    first = provenance.runtime_snapshot(
        provenance._GLM_ENDPOINT,
        opener=opener,
    )
    captured["payloads"]["/v1/models/status"]["models"][0]["actual_size"] = 1190000000
    second = provenance.runtime_snapshot(
        provenance._GLM_ENDPOINT,
        opener=opener,
    )
    assert first == second
    provenance.request_identity(
        endpoint=provenance._GLM_ENDPOINT,
        body={"messages": []},
        response={"choices": []},
        before=first,
        after=second,
    )


@pytest.mark.parametrize("bad", [None, "1190000000", True, 0, -1])
def test_omlx_invalid_actual_size_telemetry_fails_closed(
        tmp_path, monkeypatch, bad):
    opener = _bind_omlx_fixture(
        tmp_path, monkeypatch,
        lambda payloads: payloads["/v1/models/status"]["models"][0].__setitem__(
            "actual_size", bad
        ),
    )
    with pytest.raises(provenance.RuntimeIdentityError, match="actual_size telemetry"):
        provenance.runtime_snapshot(provenance._GLM_ENDPOINT, opener=opener)


def test_omlx_missing_actual_size_telemetry_fails_closed(tmp_path, monkeypatch):
    opener = _bind_omlx_fixture(
        tmp_path, monkeypatch,
        lambda payloads: payloads["/v1/models/status"]["models"][0].pop("actual_size"),
    )
    with pytest.raises(provenance.RuntimeIdentityError, match="actual_size telemetry"):
        provenance.runtime_snapshot(provenance._GLM_ENDPOINT, opener=opener)


def test_omlx_resident_size_drift_still_fails_closed(tmp_path, monkeypatch):
    opener = _bind_omlx_fixture(
        tmp_path, monkeypatch,
        lambda payloads: payloads["/v1/models/status"]["models"][0].__setitem__(
            "resident_estimated_size", 1
        ),
    )
    with pytest.raises(provenance.RuntimeIdentityError, match="loaded-model fields"):
        provenance.runtime_snapshot(provenance._GLM_ENDPOINT, opener=opener)


@pytest.mark.parametrize("field,bad", [
    ("version", "0.6.4"),
    ("loaded_models", []),
    ("models_loading", 1),
])
def test_omlx_status_drift_fails_closed(tmp_path, monkeypatch, field, bad):
    opener = _bind_omlx_fixture(
        tmp_path, monkeypatch,
        lambda payloads: payloads["/api/status"].__setitem__(field, bad),
    )
    with pytest.raises(provenance.RuntimeIdentityError, match="api/status"):
        provenance.runtime_snapshot(provenance._GLM_ENDPOINT, opener=opener)


def test_omlx_loaded_path_drift_fails_closed(tmp_path, monkeypatch):
    opener = _bind_omlx_fixture(
        tmp_path, monkeypatch,
        lambda payloads: payloads["/v1/models/status"]["models"][0].__setitem__(
            "model_path", "/tmp/wrong-model"
        ),
    )
    with pytest.raises(provenance.RuntimeIdentityError, match="loaded-model fields"):
        provenance.runtime_snapshot(provenance._GLM_ENDPOINT, opener=opener)


def test_omlx_listener_ownership_drift_fails_closed(tmp_path, monkeypatch):
    opener = _bind_omlx_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        provenance,
        "_command_output",
        lambda argv: (
            "state = running\npid = 4242" if argv[0] == "/bin/launchctl"
            else "Thu Aug 27 21:14:55 2026" if argv[0] == "/bin/ps"
            else "p9999\nn127.0.0.1:18080"
        ),
    )
    with pytest.raises(provenance.RuntimeIdentityError, match="does not own"):
        provenance.runtime_snapshot(provenance._GLM_ENDPOINT, opener=opener)
