"""Immutable model/request provenance and authenticated custom-server priority headers.

This module never calls a model. It reads the custom server's lightweight `/health` identity and
hashes the exact request/response material around a caller's existing generation.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional


IDENTITY_SCHEMA = "trader-model-request.v1"
_RUNTIME_KEYS = (
    "artifact_id", "artifact_manifest_sha256", "runtime_receipt_sha256",
    "runtime_contract_sha256", "model_realpath", "model_id", "binding_kind",
    "started_unix", "startup_nonce", "readiness_smoke_sha256",
)


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
    """Return the raw label set of the first sample of ``name`` (a vLLM *_info gauge)."""
    for line in metrics_text.splitlines():
        if line.startswith("#") or not line.startswith(name + "{"):
            continue
        end = line.rfind("}")
        if end > 0:
            return line[len(name) + 1:end]
    return None


def _vllm_snapshot(endpoint: str, timeout: float, opener) -> Dict[str, Any]:
    """Derive an immutable runtime identity from a vLLM OpenAI server.

    vLLM has no /health identity payload -- it answers an empty 200 -- so the fields the M3
    server publishes there are reconstructed from surfaces vLLM does expose, choosing only
    values that are constant for the life of one engine process:

      * /v1/models    -> the served id and ``root``, the realpath the weights loaded from.
                         Its ``created`` field is stamped PER REQUEST, not at startup, and is
                         therefore excluded from every hash below -- including it would fail
                         the before/after immutability check on every cycle.
      * /metrics      -> ``process_start_time_seconds`` (the true process nonce; it changes if
                         and only if the server restarted) and ``vllm:cache_config_info``,
                         whose label set is the resolved engine contract (dtype, block size,
                         prefix caching, TP-visible block counts).
      * /version      -> the vLLM build, including the git describe suffix.

    A restart, a weight-path change, or an engine-config change all move at least one of
    artifact_id, startup_nonce, runtime_contract_sha256 and runtime_receipt_sha256, so the
    binding request_identity() enforces is as strong here as on the custom server.
    """
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

    # Hash an ALLOWLIST, not the whole entry: vLLM regenerates `created` (request time) and a
    # random `modelperm-*` id on every /v1/models read, so a blacklist leaves volatile material
    # in the hash and fails the before/after immutability check on every cycle.
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
    """Read one server's immutable identity, custom-server first, then vLLM.

    The custom (m3_serve_batched) server publishes the whole identity on /health. vLLM does
    not, so a /health that is unreadable or non-conforming falls through to _vllm_snapshot
    rather than failing the cycle. Anything that answers /health with a conforming payload
    keeps its exact previous behaviour, including its error messages.
    """
    try:
        payload = _fetch(health_url(endpoint), timeout, opener, as_json=True)
    except Exception as exc:
        try:
            return _vllm_snapshot(endpoint, timeout, opener)
        except RuntimeIdentityError:
            raise RuntimeIdentityError(f"cannot read model runtime identity: {exc}") from exc
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
    """Bind one exact request/response to an unchanged immutable runtime."""
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
    """Return urgent headers only when the same owner-only token file used by the server is valid."""
    if int(priority) != 0:
        return {"X-M3-Priority": str(int(priority))}
    token = _read_owner_token(os.environ.get("M3_PRIORITY_TOKEN_FILE", ""))
    if token is None:
        if os.environ.get("TRADER_REQUIRE_PRIORITY_TOKEN", "0").lower() not in ("0", "false", "no"):
            raise RuntimeIdentityError("urgent priority requires owner-only M3_PRIORITY_TOKEN_FILE")
        return {}
    return {"X-M3-Priority": "0", "X-M3-Priority-Token": token}
