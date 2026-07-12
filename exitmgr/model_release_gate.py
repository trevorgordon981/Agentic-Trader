"""Receipt-bound v3 release gate for NEW trading entries.

The gate is deliberately independent of the model name sent in an OpenAI-compatible
request.  When enabled, it proves that the *currently ready* custom-Python runtime is
the exact runtime promoted by an owner-trusted OpenSSH signature, and that the signed
promotion binds immutable general-reasoning, trading, and portfolio noninferiority
receipts.  Protective exits do not call this module.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, Mapping, Optional

from exitmgr import provenance


PROMOTION_SCHEMA = "alfred-model-promotion.v1"
SIGNER_IDENTITY = "alfred-model-promotion"
SIGNATURE_NAMESPACE = "alfred-model-promotion-v1"
EXPECTED_STAGE = "v3"
EXPECTED_BACKEND = "custom-python-mlx"
_HEX64 = re.compile(r"[0-9a-f]{64}")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}")


class ModelReleaseGateError(RuntimeError):
    """A new entry cannot prove that its active model release is promoted."""


@dataclass(frozen=True)
class ModelReleaseGateSettings:
    enabled: bool = False
    promotion_receipt: str = ""
    signature: str = ""
    allowed_signers: str = ""
    configuration_error: str = ""


def _canonical_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ModelReleaseGateError(f"{label} must be a non-empty absolute path")
    if not os.path.isabs(value) or os.path.normpath(value) != value:
        raise ModelReleaseGateError(f"{label} must be a canonical absolute path")
    if os.path.realpath(value) != value:
        raise ModelReleaseGateError(f"{label} may not traverse symlinks")
    return value


def settings_from_mapping(trading: Optional[Mapping[str, Any]]) -> ModelReleaseGateSettings:
    """Parse ``trading.model_release_gate`` without truthy-string surprises.

    Absence is the only implicit off state.  A present but misspelled/malformed block
    fails closed so an attempted activation cannot silently become disabled.
    """
    if not isinstance(trading, Mapping) or "model_release_gate" not in trading:
        return ModelReleaseGateSettings()
    raw = trading.get("model_release_gate")
    if not isinstance(raw, Mapping):
        raise ModelReleaseGateError("model_release_gate must be a mapping")
    expected = {"enabled", "promotion_receipt", "signature", "allowed_signers"}
    unknown = sorted(set(raw) - expected)
    if unknown:
        raise ModelReleaseGateError(
            "unknown model_release_gate setting(s): " + ", ".join(unknown))
    enabled = raw.get("enabled", False)
    if type(enabled) is not bool:
        raise ModelReleaseGateError("model_release_gate.enabled must be true or false")
    if not enabled:
        return ModelReleaseGateSettings()
    return ModelReleaseGateSettings(
        enabled=True,
        promotion_receipt=_canonical_path(
            raw.get("promotion_receipt"), "promotion_receipt"),
        signature=_canonical_path(raw.get("signature"), "signature"),
        allowed_signers=_canonical_path(
            raw.get("allowed_signers"), "allowed_signers"),
    )


def _read_trusted_file(path: str, label: str, *, max_bytes: int,
                       frozen: bool = False) -> bytes:
    path = _canonical_path(path, label)
    fd = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ModelReleaseGateError(f"{label} is not a regular file")
        if info.st_uid not in (os.geteuid(), 0):
            raise ModelReleaseGateError(f"{label} is not owned by this service or root")
        mode = stat.S_IMODE(info.st_mode)
        if mode & 0o022:
            raise ModelReleaseGateError(f"{label} is group/world writable")
        if frozen and mode & 0o222:
            raise ModelReleaseGateError(f"{label} is not frozen read-only")
        if info.st_size <= 0 or info.st_size > max_bytes:
            raise ModelReleaseGateError(f"{label} has an invalid size")
        chunks = []
        remaining = info.st_size
        while remaining:
            chunk = os.read(fd, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) != info.st_size:
            raise ModelReleaseGateError(f"{label} changed while being read")
        return raw
    except ModelReleaseGateError:
        raise
    except OSError as exc:
        raise ModelReleaseGateError(f"cannot read {label}: {exc}") from exc
    finally:
        if fd is not None:
            os.close(fd)


def _no_duplicate_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ModelReleaseGateError(f"promotion receipt has duplicate key: {key}")
        value[key] = item
    return value


def _parse_canonical_receipt(raw: bytes) -> Dict[str, Any]:
    try:
        text = raw.decode("ascii")
        value = json.loads(text, object_pairs_hook=_no_duplicate_object)
    except ModelReleaseGateError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelReleaseGateError("promotion receipt is not ASCII JSON") from exc
    if not isinstance(value, dict):
        raise ModelReleaseGateError("promotion receipt root must be an object")
    try:
        canonical = (json.dumps(value, sort_keys=True, separators=(",", ":"),
                                ensure_ascii=True, allow_nan=False) + "\n").encode("ascii")
    except (TypeError, ValueError) as exc:
        raise ModelReleaseGateError("promotion receipt cannot be canonicalized") from exc
    if raw != canonical:
        raise ModelReleaseGateError("promotion receipt is not canonical JSON")
    return value


def _exact_keys(value: Any, expected, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ModelReleaseGateError(f"{label} must be an object")
    actual = set(value)
    expected = set(expected)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if extra:
            detail.append("unexpected " + ", ".join(extra))
        raise ModelReleaseGateError(f"{label} fields invalid ({'; '.join(detail)})")
    return value


def _hex(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise ModelReleaseGateError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise ModelReleaseGateError(f"{label} is missing or malformed")
    return value


def _rfc3339(value: Any) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ModelReleaseGateError("promoted_at must be an RFC3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ModelReleaseGateError("promoted_at must be an RFC3339 UTC timestamp") from exc
    if parsed.utcoffset() is None:
        raise ModelReleaseGateError("promoted_at must include UTC")
    return value


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _verify_bound_file(descriptor: Mapping[str, Any], *, path_key: str,
                       digest_key: str, label: str, frozen: bool = False) -> None:
    path = _canonical_path(descriptor.get(path_key), f"{label}.{path_key}")
    expected = _hex(descriptor.get(digest_key), f"{label}.{digest_key}")
    raw = _read_trusted_file(path, label, max_bytes=16 * 1024 * 1024, frozen=frozen)
    if _sha256(raw) != expected:
        raise ModelReleaseGateError(f"{label} SHA-256 does not match promotion receipt")


def _trusted_ssh_keygen() -> str:
    candidate = shutil.which("ssh-keygen")
    if not candidate:
        raise ModelReleaseGateError("ssh-keygen is unavailable for promotion signature verification")
    candidate = os.path.realpath(candidate)
    try:
        info = os.stat(candidate)
    except OSError as exc:
        raise ModelReleaseGateError("cannot inspect ssh-keygen") from exc
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) & 0o022):
        raise ModelReleaseGateError("ssh-keygen is not a trusted root-owned executable")
    return candidate


def _verify_ssh_signature(raw: bytes, allowed_signers_raw: bytes,
                          signature_raw: bytes) -> None:
    """Verify copied, already-permission-checked inputs to avoid path TOCTOU."""
    keygen = _trusted_ssh_keygen()
    with tempfile.TemporaryDirectory(prefix="alfred-promotion-verify-") as tmp:
        allowed = os.path.join(tmp, "allowed_signers")
        signature = os.path.join(tmp, "receipt.sig")
        for path, content in ((allowed, allowed_signers_raw), (signature, signature_raw)):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                offset = 0
                while offset < len(content):
                    written = os.write(fd, content[offset:])
                    if written <= 0:
                        raise ModelReleaseGateError(
                            "could not stage promotion signature verification input")
                    offset += written
                os.fsync(fd)
            finally:
                os.close(fd)
        try:
            result = subprocess.run(
                [keygen, "-Y", "verify", "-f", allowed, "-I", SIGNER_IDENTITY,
                 "-n", SIGNATURE_NAMESPACE, "-s", signature],
                input=raw, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=5, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ModelReleaseGateError("promotion signature verification failed") from exc
    if result.returncode != 0:
        raise ModelReleaseGateError("promotion receipt signature is invalid")


def _require_local_endpoint(endpoint: str) -> None:
    try:
        parsed = urllib.parse.urlsplit(endpoint)
        host = parsed.hostname
    except (TypeError, ValueError) as exc:
        raise ModelReleaseGateError("model endpoint is malformed") from exc
    if parsed.scheme not in ("http", "https") or not host or parsed.username or parsed.password:
        raise ModelReleaseGateError("model endpoint must be an unauthenticated HTTP(S) URL")
    if host == "localhost":
        return
    try:
        if ipaddress.ip_address(host).is_loopback:
            return
    except ValueError:
        pass
    raise ModelReleaseGateError("model release gate requires the local custom-Python endpoint")


def _validate_receipt(receipt: Mapping[str, Any]) -> None:
    _exact_keys(receipt, {
        "schema", "promotion_id", "status", "promoted_at", "lineage",
        "serving_artifact", "noninferiority",
    }, "promotion receipt")
    if receipt.get("schema") != PROMOTION_SCHEMA:
        raise ModelReleaseGateError("unsupported promotion receipt schema")
    _identifier(receipt.get("promotion_id"), "promotion_id")
    if receipt.get("status") != "PROMOTED":
        raise ModelReleaseGateError("promotion status is not PROMOTED")
    _rfc3339(receipt.get("promoted_at"))

    lineage = _exact_keys(receipt.get("lineage"), {"stage", "parent_bf16"}, "lineage")
    if lineage.get("stage") != EXPECTED_STAGE:
        raise ModelReleaseGateError("promotion lineage stage is not v3")
    parent = _exact_keys(lineage.get("parent_bf16"), {
        "artifact_id", "artifact_manifest_sha256", "model_tree_sha256",
    }, "lineage.parent_bf16")
    _identifier(parent.get("artifact_id"), "parent BF16 artifact_id")
    _hex(parent.get("artifact_manifest_sha256"), "parent BF16 manifest digest")
    _hex(parent.get("model_tree_sha256"), "parent BF16 tree digest")

    serving = _exact_keys(receipt.get("serving_artifact"), {
        "backend", "binding_kind", "artifact_id", "artifact_manifest_path",
        "artifact_manifest_sha256", "model_realpath", "runtime_receipt_sha256",
        "runtime_contract_sha256", "readiness_smoke_sha256",
        "quantization_receipt_path", "quantization_receipt_sha256",
    }, "serving_artifact")
    if serving.get("backend") != EXPECTED_BACKEND:
        raise ModelReleaseGateError("serving backend is not the custom Python engine")
    if serving.get("binding_kind") not in ("pipeline-artifact", "pipeline-reference"):
        raise ModelReleaseGateError("serving binding kind is unsupported")
    _identifier(serving.get("artifact_id"), "serving artifact_id")
    _canonical_path(serving.get("model_realpath"), "serving model_realpath")
    for key in ("artifact_manifest_sha256", "runtime_receipt_sha256",
                "runtime_contract_sha256", "readiness_smoke_sha256",
                "quantization_receipt_sha256"):
        _hex(serving.get(key), f"serving_artifact.{key}")
    _verify_bound_file(serving, path_key="artifact_manifest_path",
                       digest_key="artifact_manifest_sha256", label="artifact manifest")
    _verify_bound_file(serving, path_key="quantization_receipt_path",
                       digest_key="quantization_receipt_sha256", label="quantization receipt")

    noninferiority = _exact_keys(receipt.get("noninferiority"), {
        "general_reasoning", "trading", "portfolio",
    }, "noninferiority")
    for pillar in ("general_reasoning", "trading", "portfolio"):
        descriptor = _exact_keys(noninferiority.get(pillar), {
            "receipt_path", "receipt_sha256", "decision", "frozen",
            "candidate_artifact_id", "candidate_artifact_manifest_sha256",
        }, f"noninferiority.{pillar}")
        if descriptor.get("decision") != "PASS" or descriptor.get("frozen") is not True:
            raise ModelReleaseGateError(f"{pillar} noninferiority is not frozen PASS")
        if descriptor.get("candidate_artifact_id") != serving.get("artifact_id"):
            raise ModelReleaseGateError(f"{pillar} receipt is bound to another artifact")
        if descriptor.get("candidate_artifact_manifest_sha256") != serving.get(
                "artifact_manifest_sha256"):
            raise ModelReleaseGateError(f"{pillar} receipt is bound to another manifest")
        _verify_bound_file(descriptor, path_key="receipt_path", digest_key="receipt_sha256",
                           label=f"{pillar} noninferiority receipt", frozen=True)


def _runtime_matches(receipt: Mapping[str, Any], runtime: Mapping[str, Any]) -> None:
    serving = receipt["serving_artifact"]
    expected = {
        "runtime_backend": serving["backend"],
        "binding_kind": serving["binding_kind"],
        "artifact_id": serving["artifact_id"],
        "artifact_manifest_sha256": serving["artifact_manifest_sha256"],
        "model_realpath": serving["model_realpath"],
        "runtime_receipt_sha256": serving["runtime_receipt_sha256"],
        "runtime_contract_sha256": serving["runtime_contract_sha256"],
        "readiness_smoke_sha256": serving["readiness_smoke_sha256"],
    }
    for key, value in expected.items():
        if runtime.get(key) != value:
            raise ModelReleaseGateError(f"active runtime does not match promotion ({key})")


def _decision_matches(decision_identity: Optional[Mapping[str, Any]],
                      runtime: Mapping[str, Any]) -> None:
    if decision_identity is None:
        return
    if not isinstance(decision_identity, Mapping):
        raise ModelReleaseGateError("entry model identity is malformed")
    nested = decision_identity.get("runtime")
    nested = nested if isinstance(nested, Mapping) else {}
    for key in ("artifact_id", "artifact_manifest_sha256", "runtime_receipt_sha256",
                "runtime_contract_sha256", "model_realpath"):
        value = decision_identity.get(key, nested.get(key))
        if not value or value != runtime.get(key):
            raise ModelReleaseGateError(
                f"entry decision was not produced by the promoted runtime ({key})")


def require_v3_release(
        settings: ModelReleaseGateSettings, *, endpoint: str,
        decision_identity: Optional[Mapping[str, Any]] = None,
        runtime_snapshot: Callable[[str], Mapping[str, Any]] = provenance.runtime_snapshot,
        signature_verifier: Callable[[bytes, bytes, bytes], None] = _verify_ssh_signature,
) -> Dict[str, Any]:
    """Return compact promotion evidence or raise before any NEW order is built.

    Disabled is a true no-op.  Enabled is fail-closed on every file, signature,
    lineage, noninferiority, runtime, and decision-identity mismatch.
    """
    if not isinstance(settings, ModelReleaseGateSettings):
        raise ModelReleaseGateError("model release gate settings are invalid")
    if not settings.enabled:
        return {"enabled": False}
    if settings.configuration_error:
        raise ModelReleaseGateError(
            "model release gate configuration is invalid: " + settings.configuration_error)
    _require_local_endpoint(endpoint)
    receipt_raw = _read_trusted_file(
        settings.promotion_receipt, "promotion receipt", max_bytes=1024 * 1024)
    signature_raw = _read_trusted_file(
        settings.signature, "promotion signature", max_bytes=1024 * 1024)
    allowed_raw = _read_trusted_file(
        settings.allowed_signers, "allowed signers", max_bytes=1024 * 1024, frozen=True)
    receipt = _parse_canonical_receipt(receipt_raw)
    signature_verifier(receipt_raw, allowed_raw, signature_raw)
    _validate_receipt(receipt)
    try:
        runtime = runtime_snapshot(endpoint)
    except ModelReleaseGateError:
        raise
    except Exception as exc:
        raise ModelReleaseGateError(f"cannot verify active model runtime: {exc}") from exc
    if not isinstance(runtime, Mapping):
        raise ModelReleaseGateError("active runtime identity is malformed")
    _runtime_matches(receipt, runtime)
    _decision_matches(decision_identity, runtime)
    serving = receipt["serving_artifact"]
    return {
        "enabled": True,
        "schema": receipt["schema"],
        "promotion_id": receipt["promotion_id"],
        "stage": receipt["lineage"]["stage"],
        "artifact_id": serving["artifact_id"],
        "artifact_manifest_sha256": serving["artifact_manifest_sha256"],
        "runtime_receipt_sha256": serving["runtime_receipt_sha256"],
        "runtime_contract_sha256": serving["runtime_contract_sha256"],
        "promotion_receipt_sha256": _sha256(receipt_raw),
    }
