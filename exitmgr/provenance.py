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
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise RuntimeIdentityError("LLM endpoint is not an HTTP(S) URL")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/health", "", ""))


def runtime_snapshot(endpoint: str, timeout: float = 3.0,
                     opener=urllib.request.urlopen) -> Dict[str, Any]:
    req = urllib.request.Request(health_url(endpoint), headers={"Accept": "application/json"})
    try:
        with opener(req, timeout=timeout) as response:
            payload = json.loads(response.read().decode())
    except Exception as exc:
        raise RuntimeIdentityError(f"cannot read model runtime identity: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("ready") is not True:
        raise RuntimeIdentityError("model runtime is not ready")
    identity = {key: payload.get(key) for key in _RUNTIME_KEYS}
    # This is emitted by the custom Python server inside its immutable runtime
    # receipt.  Surface it as a compact identity field so safety gates can prove
    # the backend instead of trusting a model-name string or endpoint label.
    runtime_receipt = payload.get("runtime_receipt")
    contract = runtime_receipt.get("contract") if isinstance(runtime_receipt, dict) else None
    identity["runtime_backend"] = (
        contract.get("backend") if isinstance(contract, dict) else None)
    identity["runtime_schema"] = (
        runtime_receipt.get("schema") if isinstance(runtime_receipt, dict) else None)
    required = ("artifact_id", "artifact_manifest_sha256", "runtime_receipt_sha256",
                "runtime_contract_sha256", "model_realpath", "startup_nonce",
                "runtime_backend")
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
