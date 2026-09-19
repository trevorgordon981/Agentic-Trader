"""Public API contract; production-derived narrative omitted."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional


IDENTITY_SCHEMA = "trader-model-request.v1"
_RUNTIME_KEYS = (
    "artifact_id", "artifact_manifest_sha256", "runtime_receipt_sha256",
    "runtime_contract_sha256", "model_realpath", "model_id", "binding_kind",
    "started_unix", "startup_nonce", "readiness_smoke_sha256",
)






_GLM_ENDPOINT = "http://127.0.0.1:18080/v1/chat/completions"
_GLM_MODEL = "glm-5.3-flash-candidate"
_GLM_MODEL_VIEW = Path(
    "/opt/agentic-trader/example-model-view/model-view/"
    "glm-5.3-flash-candidate"
)
_GLM_MODEL_TARGET = Path(
    "/opt/agentic-trader/models/"
    "example-model-artifact"
)
_GLM_LABEL = "ai.example.model-candidate"
_GLM_PLIST = Path(
    "/opt/agentic-trader/Library/LaunchAgents/"
    "ai.example.model-candidate.plist"
)
_GLM_PLIST_SHA256 = "2222222222222222222222222222222222222222222222222222222222222222"
_GLM_COMPLETE_RECEIPT_SHA256 = "3333333333333333333333333333333333333333333333333333333333333333"
_GLM_STRUCTURE_RECEIPT_SHA256 = "4444444444444444444444444444444444444444444444444444444444444444"
_GLM_CANARY_RECEIPT_SHA256 = "5555555555555555555555555555555555555555555555555555555555555555"
_GLM_CHAT_TEMPLATE_SHA256 = "6666666666666666666666666666666666666666666666666666666666666666"
_GLM_OMLX_VERSION = "0.0.0-public"
_GLM_CONTEXT_LENGTH = 131072
_GLM_RESIDENT_SIZE = 1234567890
_GLM_ARTIFACT_FILE_COUNT = 12


class RuntimeIdentityError(RuntimeError):
    pass


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str,
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def sha256(value: Any) -> str:
    raw = value.encode() if isinstance(value, str) else canonical_bytes(value)
    return hashlib.sha256(raw).hexdigest()


def health_url(endpoint: str) -> str:
    return _server_url(endpoint, "/health")


def _server_url(endpoint: str, path: str) -> str:
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise RuntimeIdentityError("LLM endpoint is not an HTTP(S) URL")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _fetch(url: str, timeout: float, opener, *, as_json: bool) -> Any:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with opener(req, timeout=timeout) as response:
        raw = response.read().decode()
    return json.loads(raw) if as_json else raw


def _metric_value(metrics_text: str, name: str) -> Optional[float]:
    for line in metrics_text.splitlines():
        if line.startswith("#") or not line.startswith(name):
            continue
        rest = line[len(name):]
        if rest[:1] not in ("", " ", "\t"):
            continue
        try:
            return float(rest.strip())
        except ValueError:
            return None
    return None


def _metric_labels(metrics_text: str, name: str) -> Optional[str]:
    """Public API contract; production-derived narrative omitted."""
    for line in metrics_text.splitlines():
        if line.startswith("#") or not line.startswith(name + "{"):
            continue
        end = line.rfind("}")
        if end > 0:
            return line[len(name) + 1:end]
    return None


def _file_sha256(path: Path) -> str:
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise RuntimeIdentityError(f"untrusted GLM identity file: {path}")
        if stat.S_IMODE(info.st_mode) & 0o022:
            raise RuntimeIdentityError(f"writable GLM identity file: {path}")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        return digest.hexdigest()
    except RuntimeIdentityError:
        raise
    except OSError as exc:
        raise RuntimeIdentityError(f"cannot read GLM identity file {path}: {exc}") from exc
    finally:
        if fd is not None:
            os.close(fd)


def _command_output(argv) -> str:
    try:
        result = subprocess.run(
            list(argv), check=True, capture_output=True, text=True, timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeIdentityError(f"cannot verify local GLM process: {exc}") from exc
    return result.stdout.strip()


def _bind_omlx_model_view() -> tuple[str, str]:
    """Public API contract; production-derived narrative omitted."""
    try:
        target = _GLM_MODEL_TARGET.resolve(strict=True)
        view = _GLM_MODEL_VIEW.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RuntimeIdentityError(f"cannot resolve local oMLX model path: {exc}") from exc
    if not target.is_dir() or not view.is_dir():
        raise RuntimeIdentityError("local oMLX model path is not a directory")
    target_info = target.stat()
    if target_info.st_uid != os.geteuid() or stat.S_IMODE(target_info.st_mode) & 0o222:
        raise RuntimeIdentityError("local oMLX artifact directory is not owner-bound/read-only")

    receipts = {
        "COMPLETE_RECEIPT.json": _GLM_COMPLETE_RECEIPT_SHA256,
        "STRUCTURE_RECEIPT.json": _GLM_STRUCTURE_RECEIPT_SHA256,
        "CANARY_RECEIPT.json": _GLM_CANARY_RECEIPT_SHA256,
    }
    for name, expected in receipts.items():
        if _file_sha256(target / name) != expected:
            raise RuntimeIdentityError(f"local oMLX {name} drift")
    template = view / "chat_template.jinja"
    if template.is_symlink() or _file_sha256(template) != _GLM_CHAT_TEMPLATE_SHA256:
        raise RuntimeIdentityError("local oMLX chat template drift")

    try:
        target_children = {child.name: child for child in target.iterdir()}
        view_children = {child.name: child for child in view.iterdir()}
    except OSError as exc:
        raise RuntimeIdentityError(f"cannot enumerate local oMLX model view: {exc}") from exc
    if len(target_children) != _GLM_ARTIFACT_FILE_COUNT:
        raise RuntimeIdentityError("local oMLX artifact file census drift")
    if set(view_children) != set(target_children) | {_GLM_MODEL}:
        raise RuntimeIdentityError("local oMLX model-view census drift")
    alias_link = view_children[_GLM_MODEL]
    try:
        alias_target = alias_link.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RuntimeIdentityError(f"cannot resolve local oMLX alias link: {exc}") from exc
    if not alias_link.is_symlink() or alias_target != view:
        raise RuntimeIdentityError("local oMLX alias link drift")

    manifest = []
    for name, target_child in sorted(target_children.items()):
        view_child = view_children[name]
        if name == "chat_template.jinja":
            manifest.append({
                "name": name,
                "resolved": str(view_child),
                "size": view_child.stat().st_size,
                "overlay_sha256": _GLM_CHAT_TEMPLATE_SHA256,
            })
            continue
        if not view_child.is_symlink():
            raise RuntimeIdentityError(f"local oMLX model-view entry is not a symlink: {name}")
        try:
            resolved = view_child.resolve(strict=True)
            expected = target_child.resolve(strict=True)
            info = expected.stat()
        except (OSError, RuntimeError) as exc:
            raise RuntimeIdentityError(f"cannot resolve local oMLX model-view entry {name}: {exc}") from exc
        if resolved != expected or not expected.is_file():
            raise RuntimeIdentityError(f"local oMLX model-view target drift: {name}")
        manifest.append({"name": name, "resolved": str(resolved), "size": info.st_size})
    return str(target), sha256({
        "artifact_files": manifest,
        "complete_receipt_sha256": _GLM_COMPLETE_RECEIPT_SHA256,
        "structure_receipt_sha256": _GLM_STRUCTURE_RECEIPT_SHA256,
        "canary_receipt_sha256": _GLM_CANARY_RECEIPT_SHA256,
        "chat_template_sha256": _GLM_CHAT_TEMPLATE_SHA256,
        "alias_link": _GLM_MODEL,
    })


def _local_glm_snapshot(endpoint: str, health: Dict[str, Any], timeout: float,
                        opener) -> Dict[str, Any]:
    if endpoint != _GLM_ENDPOINT:
        raise RuntimeIdentityError("unexpected local GLM endpoint")
    if _file_sha256(_GLM_PLIST) != _GLM_PLIST_SHA256:
        raise RuntimeIdentityError("local oMLX launchd plist drift")

    expected_health = {"status": "healthy", "default_model": _GLM_MODEL}
    if any(health.get(key) != value for key, value in expected_health.items()):
        raise RuntimeIdentityError("local GLM HTTP health identity drifted")
    try:
        models = _fetch(_server_url(endpoint, "/v1/models"), timeout, opener, as_json=True)
        status = _fetch(_server_url(endpoint, "/api/status"), timeout, opener, as_json=True)
        model_status = _fetch(
            _server_url(endpoint, "/v1/models/status"), timeout, opener, as_json=True,
        )
    except Exception as exc:
        raise RuntimeIdentityError(f"cannot read local oMLX identity: {exc}") from exc

    entries = models.get("data") if isinstance(models, dict) else None
    advertised = [row.get("id") for row in entries or [] if isinstance(row, dict)]
    if advertised != [_GLM_MODEL]:
        raise RuntimeIdentityError("local oMLX /v1/models identity drifted")
    if (not isinstance(status, dict)
            or status.get("status") != "ok"
            or status.get("version") != _GLM_OMLX_VERSION
            or status.get("default_model") != _GLM_MODEL
            or status.get("loaded_models") != [_GLM_MODEL]
            or status.get("models_loaded") != 1
            or status.get("models_loading") != 0):
        raise RuntimeIdentityError("local oMLX /api/status identity drifted")
    details = model_status.get("models") if isinstance(model_status, dict) else None
    exact = [row for row in details or []
             if isinstance(row, dict) and row.get("id") == _GLM_MODEL]
    if len(exact) != 1:
        raise RuntimeIdentityError("local oMLX model-status identity drifted")
    detail = exact[0]
    expected_detail = {
        "model_path": str(_GLM_MODEL_VIEW),
        "loaded": True,
        "is_loading": False,
        "resident_estimated_size": _GLM_RESIDENT_SIZE,
        "engine_type": "vlm",
        "config_model_type": "glm5_next",
        "model_context_length": _GLM_CONTEXT_LENGTH,
        "max_context_window": _GLM_CONTEXT_LENGTH,
        "max_tokens": 32768,
    }
    if any(detail.get(key) != value for key, value in expected_detail.items()):
        raise RuntimeIdentityError("local oMLX loaded-model fields drifted")



    observed_actual_size = detail.get("actual_size")
    if (isinstance(observed_actual_size, bool)
            or not isinstance(observed_actual_size, int)
            or observed_actual_size <= 0):
        raise RuntimeIdentityError("local oMLX actual_size telemetry is invalid")
    model_target_realpath, model_view_manifest_sha256 = _bind_omlx_model_view()

    label = f"gui/{os.getuid()}/{_GLM_LABEL}"
    launch = _command_output(("/bin/launchctl", "print", label))
    match = re.search(r"(?m)^\s*pid = (\d+)\s*$", launch)
    if not match or "state = running" not in launch:
        raise RuntimeIdentityError("local GLM launchd job is not running")
    pid = int(match.group(1))
    started_text = _command_output(("/bin/ps", "-p", str(pid), "-o", "lstart="))
    listener = _command_output((
        "/usr/sbin/lsof", "-nP", "-a", "-p", str(pid),
        "-iTCP:18080", "-sTCP:LISTEN", "-Fn",
    ))
    if f"p{pid}" not in listener.splitlines() or "n127.0.0.1:18080" not in listener.splitlines():
        raise RuntimeIdentityError("local oMLX launchd PID does not own loopback listener")
    try:
        started = datetime.strptime(started_text, "%a %b %d %H:%M:%S %Y")
        started_unix = int(started.replace(tzinfo=datetime.now().astimezone().tzinfo).timestamp())
    except ValueError as exc:
        raise RuntimeIdentityError("cannot parse local GLM process start") from exc

    stable_model_entry = {
        key: detail.get(key) for key in expected_detail
    } | {
        "id": _GLM_MODEL,
        "resolved_model_path": model_target_realpath,
        "model_view_manifest_sha256": model_view_manifest_sha256,
    }
    contract = {
        "launchd_label": _GLM_LABEL,
        "plist_sha256": _GLM_PLIST_SHA256,
        "omlx_version": _GLM_OMLX_VERSION,
        "health": expected_health,
        "model": stable_model_entry,
    }
    readiness = sha256({
        "health": expected_health,
        "models": advertised,
        "status": {
            "status": status.get("status"),
            "version": status.get("version"),
            "default_model": status.get("default_model"),
            "loaded_models": status.get("loaded_models"),
            "models_loaded": status.get("models_loaded"),
            "models_loading": status.get("models_loading"),
        },
        "model": stable_model_entry,
    })
    return {
        "artifact_id": f"omlx-glm53:{_GLM_MODEL}@{started_unix}",
        "artifact_manifest_sha256": sha256(stable_model_entry),
        "runtime_receipt_sha256": sha256({
            "plist_sha256": _GLM_PLIST_SHA256,
            "omlx_version": _GLM_OMLX_VERSION,
            "pid": pid,
            "started": started_text,
            "model": stable_model_entry,
        }),
        "runtime_contract_sha256": sha256(contract),
        "model_realpath": model_target_realpath,
        "model_id": _GLM_MODEL,
        "binding_kind": "omlx-openai-local",
        "started_unix": started_unix,
        "startup_nonce": sha256(
            f"omlx-glm53|{pid}|{started_text}|{_GLM_PLIST_SHA256}"
        ),
        "readiness_smoke_sha256": readiness,
        "health_url": health_url(endpoint),
    }


def _vllm_snapshot(endpoint: str, timeout: float, opener) -> Dict[str, Any]:
    """Public API contract; production-derived narrative omitted."""
    try:
        models = _fetch(_server_url(endpoint, "/v1/models"), timeout, opener, as_json=True)
        entries = models.get("data") if isinstance(models, dict) else None
        entry = entries[0] if entries else None
        if not isinstance(entry, dict):
            raise RuntimeIdentityError("vLLM /v1/models returned no served model")
        metrics = _fetch(_server_url(endpoint, "/metrics"), timeout, opener, as_json=False)
        version = _fetch(_server_url(endpoint, "/version"), timeout, opener, as_json=True)
    except RuntimeIdentityError:
        raise
    except Exception as exc:
        raise RuntimeIdentityError(f"cannot read vLLM runtime identity: {exc}") from exc

    started = _metric_value(metrics, "process_start_time_seconds")
    if not started:
        raise RuntimeIdentityError("vLLM /metrics has no process_start_time_seconds")
    cache_config = _metric_labels(metrics, "vllm:cache_config_info")
    if not cache_config:
        raise RuntimeIdentityError("vLLM /metrics has no vllm:cache_config_info")

    model_id = entry.get("id")
    realpath = entry.get("root")
    if not model_id or not realpath:
        raise RuntimeIdentityError("vLLM /v1/models entry has no id/root")




    stable_entry = {k: entry.get(k) for k in ("id", "object", "root", "parent", "owned_by")}
    build = version.get("version") if isinstance(version, dict) else str(version)
    started_unix = int(started)

    return {
        "artifact_id": f"vllm:{model_id}@{started_unix}",
        "artifact_manifest_sha256": sha256(stable_entry),
        "runtime_receipt_sha256": sha256({"build": build, "model": stable_entry,
                                          "started_unix": started_unix}),
        "runtime_contract_sha256": sha256({"build": build, "cache_config": cache_config}),
        "model_realpath": realpath,
        "model_id": model_id,
        "binding_kind": "vllm-openai",
        "started_unix": started_unix,
        "startup_nonce": sha256(f"vllm|{realpath}|{started_unix}|{build}"),
        "readiness_smoke_sha256": None,
        "health_url": health_url(endpoint),
    }


def runtime_snapshot(endpoint: str, timeout: float = 3.0,
                     opener=urllib.request.urlopen) -> Dict[str, Any]:
    """Public API contract; production-derived narrative omitted."""
    try:
        payload = _fetch(health_url(endpoint), timeout, opener, as_json=True)
    except Exception as exc:
        try:
            return _vllm_snapshot(endpoint, timeout, opener)
        except RuntimeIdentityError:
            raise RuntimeIdentityError(f"cannot read model runtime identity: {exc}") from exc




    if endpoint == _GLM_ENDPOINT:
        if isinstance(payload, dict) and payload.get("default_model") == "example-mtp-model":
            from exitmgr.dsv41_runtime_identity import snapshot
            try:
                return snapshot(endpoint, payload, timeout, opener)
            except RuntimeIdentityError:
                raise
            except Exception as exc:
                raise RuntimeIdentityError(f"cannot verify exact DeepSeek runtime: {exc}") from exc
        if not isinstance(payload, dict):
            raise RuntimeIdentityError("local GLM health payload is not an object")
        return _local_glm_snapshot(endpoint, payload, timeout, opener)
    if not isinstance(payload, dict) or payload.get("ready") is not True:
        try:
            return _vllm_snapshot(endpoint, timeout, opener)
        except RuntimeIdentityError:
            raise RuntimeIdentityError("model runtime is not ready")
    identity = {key: payload.get(key) for key in _RUNTIME_KEYS}
    required = ("artifact_id", "artifact_manifest_sha256", "runtime_receipt_sha256",
                "runtime_contract_sha256", "model_realpath", "startup_nonce")
    missing = [key for key in required if not identity.get(key)]
    if missing:
        raise RuntimeIdentityError("model runtime identity missing: " + ", ".join(missing))
    identity["health_url"] = health_url(endpoint)
    return identity


def request_identity(*, endpoint: str, body: Dict[str, Any], response: Dict[str, Any],
                     before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    """Public API contract; production-derived narrative omitted."""
    for key in ("artifact_id", "artifact_manifest_sha256", "runtime_receipt_sha256",
                "runtime_contract_sha256", "model_realpath", "startup_nonce"):
        if before.get(key) != after.get(key):
            raise RuntimeIdentityError(f"model runtime changed across request ({key})")
    messages = body.get("messages") or []
    system = next((m.get("content") for m in messages if m.get("role") == "system"), "")
    user = next((m.get("content") for m in messages if m.get("role") == "user"), "")
    settings = {key: value for key, value in body.items() if key != "messages"}
    return {
        "schema": IDENTITY_SCHEMA,
        "endpoint": endpoint,
        "runtime": dict(after),
        "artifact_id": after.get("artifact_id"),
        "artifact_manifest_sha256": after.get("artifact_manifest_sha256"),
        "runtime_receipt_sha256": after.get("runtime_receipt_sha256"),
        "runtime_contract_sha256": after.get("runtime_contract_sha256"),
        "model_realpath": after.get("model_realpath"),
        "system_prompt_sha256": sha256(system or ""),
        "context_sha256": sha256(user or ""),
        "request_settings_sha256": sha256(settings),
        "request_sha256": sha256(body),
        "response_sha256": sha256(response),
    }


def identity_required() -> bool:
    value = os.environ.get("TRADER_REQUIRE_RUNTIME_IDENTITY", "0").strip().lower()
    return value not in ("0", "false", "no", "off")


def _read_owner_token(path: str) -> Optional[str]:
    if not path:
        return None
    fd = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(os.path.expanduser(path), flags)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > 4096):
            return None
        token = os.read(fd, 4097).decode("utf-8").strip()
        return token if 32 <= len(token) <= 4096 else None
    except (OSError, UnicodeDecodeError):
        return None
    finally:
        if fd is not None:
            os.close(fd)


def priority_headers(priority: int = 0) -> Dict[str, str]:
    """Public API contract; production-derived narrative omitted."""
    if int(priority) != 0:
        return {"X-M3-Priority": str(int(priority))}
    token = _read_owner_token(os.environ.get("M3_PRIORITY_TOKEN_FILE", ""))
    if token is None:
        if os.environ.get("TRADER_REQUIRE_PRIORITY_TOKEN", "0").lower() not in ("0", "false", "no"):
            raise RuntimeIdentityError("urgent priority requires owner-only M3_PRIORITY_TOKEN_FILE")
        return {}
    return {"X-M3-Priority": "0", "X-M3-Priority-Token": token}
