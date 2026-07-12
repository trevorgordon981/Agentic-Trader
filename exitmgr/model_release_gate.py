"""Receipt-bound v3 release gate for NEW trading entries.

The gate is deliberately independent of the model name sent in an OpenAI-compatible
request.  When enabled, it proves that the *currently ready* custom-Python runtime is
the exact runtime promoted by an owner-trusted OpenSSH signature, and that the signed
promotion binds immutable general-reasoning, trading, and portfolio noninferiority
receipts.  Protective exits do not call this module.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Mapping, Optional

from exitmgr import provenance


PROMOTION_SCHEMA = "alfred-model-promotion.v1"
SIGNER_IDENTITY = "alfred-model-promotion"
SIGNATURE_NAMESPACE = "alfred-model-promotion-v1"
EXPECTED_STAGE = "v3"
EXPECTED_BACKEND = "custom-python-mlx"
EXPECTED_RUNTIME_SCHEMA = "pipeline-m3-runtime.v1"
ACTIVATION_SCHEMA = "alfred-model-activation.v1"
MANUAL_PROOF_SCHEMA = "alfred-manual-decision.v1"
MAX_RELEASE_LIFETIME = timedelta(days=7)
_HEX64 = re.compile(r"[0-9a-f]{64}")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}")
_LEDGER_RECORD = re.compile(
    r"(?P<sequence>[0-9]{20})-(?P<action>activate|revoke)-(?P<digest>[0-9a-f]{64})\.json")
_ZERO_DIGEST = "0" * 64
_PREFLIGHT_CAPABILITY = object()


class ModelReleaseGateError(RuntimeError):
    """A new entry cannot prove that its active model release is promoted."""


@dataclass(frozen=True)
class ModelReleaseGateSettings:
    enabled: bool = False
    promotion_receipt: str = ""
    signature: str = ""
    allowed_signers: str = ""
    activation_ledger: str = ""
    manual_provenance_key: str = ""
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
    expected = {
        "enabled", "promotion_receipt", "signature", "allowed_signers",
        "activation_ledger", "manual_provenance_key",
    }
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
        activation_ledger=_canonical_path(
            raw.get("activation_ledger"), "activation_ledger"),
        manual_provenance_key=_canonical_path(
            raw.get("manual_provenance_key"), "manual_provenance_key"),
    )


def _read_trusted_file(path: str, label: str, *, max_bytes: int,
                       frozen: bool = False, required_uid: Optional[int] = None,
                       owner_only: bool = False) -> bytes:
    path = _canonical_path(path, label)
    fd = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ModelReleaseGateError(f"{label} is not a regular file")
        if required_uid is not None and info.st_uid != required_uid:
            raise ModelReleaseGateError(f"{label} is not owned by the trusted authority")
        if required_uid is None and info.st_uid not in (os.geteuid(), 0):
            raise ModelReleaseGateError(f"{label} is not owned by this service or root")
        mode = stat.S_IMODE(info.st_mode)
        if mode & 0o022:
            raise ModelReleaseGateError(f"{label} is group/world writable")
        if owner_only and mode & 0o077:
            raise ModelReleaseGateError(f"{label} must be owner-only")
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


def _parse_canonical_json(raw: bytes, label: str) -> Dict[str, Any]:
    try:
        text = raw.decode("ascii")
        value = json.loads(text, object_pairs_hook=_no_duplicate_object)
    except ModelReleaseGateError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelReleaseGateError(f"{label} is not ASCII JSON") from exc
    if not isinstance(value, dict):
        raise ModelReleaseGateError(f"{label} root must be an object")
    try:
        canonical = (json.dumps(value, sort_keys=True, separators=(",", ":"),
                                ensure_ascii=True, allow_nan=False) + "\n").encode("ascii")
    except (TypeError, ValueError) as exc:
        raise ModelReleaseGateError(f"{label} cannot be canonicalized") from exc
    if raw != canonical:
        raise ModelReleaseGateError(f"{label} is not canonical JSON")
    return value


def _parse_canonical_receipt(raw: bytes) -> Dict[str, Any]:
    return _parse_canonical_json(raw, "promotion receipt")


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


def _rfc3339_datetime(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ModelReleaseGateError(f"{label} must be an RFC3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ModelReleaseGateError(f"{label} must be an RFC3339 UTC timestamp") from exc
    if parsed.utcoffset() != timedelta(0):
        raise ModelReleaseGateError(f"{label} must include UTC")
    return parsed


def _rfc3339(value: Any, label: str = "promoted_at") -> str:
    _rfc3339_datetime(value, label)
    return value


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True, allow_nan=False) + "\n").encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ModelReleaseGateError("security evidence cannot be canonicalized") from exc


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _verify_bound_file(descriptor: Mapping[str, Any], *, path_key: str,
                       digest_key: str, label: str, frozen: bool = False) -> None:
    path = _canonical_path(descriptor.get(path_key), f"{label}.{path_key}")
    expected = _hex(descriptor.get(digest_key), f"{label}.{digest_key}")
    raw = _read_trusted_file(path, label, max_bytes=16 * 1024 * 1024, frozen=frozen)
    if _sha256(raw) != expected:
        raise ModelReleaseGateError(f"{label} SHA-256 does not match promotion receipt")


def _activation_head_from_ledger(path: str, *, required_uid: int = 0) -> Mapping[str, Any]:
    """Read the append-only, root-owned activation ledger and return its head.

    The service account can read this directory but cannot replace, truncate, or
    remove records.  That external monotonic state is what makes an otherwise
    valid older signed promotion receipt non-replayable.
    """
    path = _canonical_path(path, "activation_ledger")
    try:
        info = os.stat(path)
    except OSError as exc:
        raise ModelReleaseGateError(f"cannot inspect activation ledger: {exc}") from exc
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != required_uid:
        raise ModelReleaseGateError("activation ledger must be a root-owned directory")
    if stat.S_IMODE(info.st_mode) & 0o022:
        raise ModelReleaseGateError("activation ledger directory is group/world writable")
    try:
        names = sorted(os.listdir(path))
    except OSError as exc:
        raise ModelReleaseGateError(f"cannot list activation ledger: {exc}") from exc
    if not names or len(names) > 4096:
        raise ModelReleaseGateError("activation ledger has an invalid record count")

    previous_digest = _ZERO_DIGEST
    previous_time: Optional[datetime] = None
    head: Optional[Mapping[str, Any]] = None
    for expected_sequence, name in enumerate(names, 1):
        match = _LEDGER_RECORD.fullmatch(name)
        if match is None:
            raise ModelReleaseGateError(f"activation ledger contains an invalid record: {name}")
        sequence = int(match.group("sequence"))
        if sequence != expected_sequence:
            raise ModelReleaseGateError("activation ledger sequence is not contiguous")
        record_path = os.path.join(path, name)
        raw = _read_trusted_file(
            record_path, "activation ledger record", max_bytes=64 * 1024,
            frozen=True, required_uid=required_uid)
        digest = _sha256(raw)
        if digest != match.group("digest"):
            raise ModelReleaseGateError("activation ledger filename digest does not match")
        record = _parse_canonical_json(raw, "activation ledger record")
        _exact_keys(record, {
            "schema", "sequence", "action", "promotion_id",
            "promotion_receipt_sha256", "activation_nonce", "recorded_at",
            "expires_at", "previous_record_sha256",
        }, "activation ledger record")
        if record.get("schema") != ACTIVATION_SCHEMA:
            raise ModelReleaseGateError("activation ledger schema is unsupported")
        if type(record.get("sequence")) is not int or record["sequence"] != sequence:
            raise ModelReleaseGateError("activation ledger record sequence is invalid")
        action = record.get("action")
        if action not in ("ACTIVATE", "REVOKE") or action.lower() != match.group("action"):
            raise ModelReleaseGateError("activation ledger action is invalid")
        _identifier(record.get("promotion_id"), "activation promotion_id")
        _hex(record.get("promotion_receipt_sha256"), "activation receipt digest")
        _hex(record.get("activation_nonce"), "activation nonce")
        _hex(record.get("previous_record_sha256"), "previous activation record digest")
        if record["previous_record_sha256"] != previous_digest:
            raise ModelReleaseGateError("activation ledger hash chain is broken")
        recorded_at = _rfc3339_datetime(record.get("recorded_at"), "recorded_at")
        _rfc3339(record.get("expires_at"), "activation expires_at")
        if previous_time is not None and recorded_at <= previous_time:
            raise ModelReleaseGateError("activation ledger timestamps are not monotonic")
        previous_time = recorded_at
        previous_digest = digest
        head = record
    assert head is not None
    return head


def _validate_activation(receipt: Mapping[str, Any], receipt_digest: str,
                         head: Mapping[str, Any], *, now: Optional[datetime] = None) -> None:
    _exact_keys(head, {
        "schema", "sequence", "action", "promotion_id",
        "promotion_receipt_sha256", "activation_nonce", "recorded_at",
        "expires_at", "previous_record_sha256",
    }, "activation ledger head")
    if head.get("schema") != ACTIVATION_SCHEMA:
        raise ModelReleaseGateError("activation ledger schema is unsupported")
    if head.get("action") == "REVOKE":
        raise ModelReleaseGateError("the latest model promotion has been revoked")
    if head.get("action") != "ACTIVATE":
        raise ModelReleaseGateError("activation ledger head is not an activation")
    bindings = {
        "sequence": receipt.get("release_sequence"),
        "promotion_id": receipt.get("promotion_id"),
        "promotion_receipt_sha256": receipt_digest,
        "activation_nonce": receipt.get("activation_nonce"),
        "expires_at": receipt.get("expires_at"),
    }
    for key, expected in bindings.items():
        if head.get(key) != expected:
            raise ModelReleaseGateError(
                f"promotion receipt is not the monotonic activation ledger head ({key})")
    _hex(head.get("previous_record_sha256"), "previous activation record digest")
    recorded_at = _rfc3339_datetime(head.get("recorded_at"), "recorded_at")
    current = now or datetime.now(timezone.utc)
    not_before = _rfc3339_datetime(receipt.get("not_before"), "not_before")
    promoted_at = _rfc3339_datetime(receipt.get("promoted_at"), "promoted_at")
    expires_at = _rfc3339_datetime(receipt.get("expires_at"), "expires_at")
    if recorded_at < promoted_at or recorded_at >= expires_at:
        raise ModelReleaseGateError("activation time is outside the signed release window")
    if recorded_at > current + timedelta(seconds=5):
        raise ModelReleaseGateError("activation time is in the future")
    if current < max(not_before, promoted_at):
        raise ModelReleaseGateError("model promotion is not active yet")
    if current >= expires_at:
        raise ModelReleaseGateError("model promotion has expired")


def manual_order_intent(*, decision_id: str, symbol: str, right: str, expiry: str,
                        strike: float, quantity: int, limit_price: float,
                        contract_id: int) -> Dict[str, Any]:
    """Return the exact, canonical manual order facts covered by a proof."""
    if not isinstance(decision_id, str) or not decision_id:
        raise ModelReleaseGateError("manual decision_id is missing")
    if not isinstance(symbol, str) or not symbol.strip():
        raise ModelReleaseGateError("manual symbol is missing")
    if right not in ("C", "P"):
        raise ModelReleaseGateError("manual option right is invalid")
    if not isinstance(expiry, str) or not re.fullmatch(r"[0-9]{8}", expiry):
        raise ModelReleaseGateError("manual expiry is invalid")
    if (type(quantity) is not int or quantity <= 0 or type(contract_id) is not int
            or contract_id <= 0):
        raise ModelReleaseGateError("manual order quantity/contract is invalid")
    if (isinstance(strike, bool) or isinstance(limit_price, bool)
            or not isinstance(strike, (int, float))
            or not isinstance(limit_price, (int, float))):
        raise ModelReleaseGateError("manual order prices are invalid")
    if not math.isfinite(float(strike)) or not math.isfinite(float(limit_price)) \
            or strike <= 0 or limit_price <= 0:
        raise ModelReleaseGateError("manual order prices must be positive")
    return {
        "decision_id": decision_id,
        "symbol": symbol.strip().upper(),
        "right": right,
        "expiry": expiry,
        "strike": float(strike),
        "quantity": quantity,
        "limit_price": float(limit_price),
        "contract_id": contract_id,
    }


def _manual_key(settings: ModelReleaseGateSettings) -> bytes:
    raw = _read_trusted_file(
        settings.manual_provenance_key, "manual provenance key", max_bytes=4096,
        owner_only=True)
    key = raw.strip()
    if len(key) < 32:
        raise ModelReleaseGateError("manual provenance key must contain at least 32 bytes")
    return key


def issue_manual_decision_proof(settings: ModelReleaseGateSettings, *,
                                intent: Mapping[str, Any], approved: bool,
                                ttl_seconds: int = 120,
                                now: Optional[datetime] = None) -> Dict[str, Any]:
    """Mint a short-lived HMAC proof only after the manual approval call path."""
    if approved is not True:
        raise ModelReleaseGateError("manual proof requires an explicit approval")
    if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 300:
        raise ModelReleaseGateError("manual proof TTL must be between 1 and 300 seconds")
    current = now or datetime.now(timezone.utc)
    payload = {
        "schema": MANUAL_PROOF_SCHEMA,
        "source": "place_trade",
        "issued_at": current.isoformat().replace("+00:00", "Z"),
        "expires_at": (current + timedelta(seconds=ttl_seconds)).isoformat().replace(
            "+00:00", "Z"),
        "nonce": secrets.token_hex(32),
        "intent_sha256": _sha256(_canonical_bytes(dict(intent))),
    }
    mac = hmac.new(_manual_key(settings), _canonical_bytes(payload), hashlib.sha256).hexdigest()
    return {"payload": payload, "hmac_sha256": mac}


def _verify_manual_decision_proof(settings: ModelReleaseGateSettings,
                                  proof: Any, intent: Any,
                                  *, now: Optional[datetime] = None) -> None:
    _exact_keys(proof, {"payload", "hmac_sha256"}, "manual decision proof")
    payload = _exact_keys(proof.get("payload"), {
        "schema", "source", "issued_at", "expires_at", "nonce", "intent_sha256",
    }, "manual decision proof payload")
    if payload.get("schema") != MANUAL_PROOF_SCHEMA or payload.get("source") != "place_trade":
        raise ModelReleaseGateError("manual decision proof source/schema is invalid")
    _hex(payload.get("nonce"), "manual proof nonce")
    _hex(payload.get("intent_sha256"), "manual proof intent digest")
    supplied_mac = _hex(proof.get("hmac_sha256"), "manual proof HMAC")
    expected_mac = hmac.new(
        _manual_key(settings), _canonical_bytes(dict(payload)), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(supplied_mac, expected_mac):
        raise ModelReleaseGateError("manual decision proof HMAC is invalid")
    if not isinstance(intent, Mapping):
        raise ModelReleaseGateError("manual order intent is missing")
    if not hmac.compare_digest(payload["intent_sha256"], _sha256(_canonical_bytes(dict(intent)))):
        raise ModelReleaseGateError("manual decision proof is bound to another order")
    issued_at = _rfc3339_datetime(payload.get("issued_at"), "manual proof issued_at")
    expires_at = _rfc3339_datetime(payload.get("expires_at"), "manual proof expires_at")
    current = now or datetime.now(timezone.utc)
    if expires_at <= issued_at or expires_at - issued_at > timedelta(seconds=300):
        raise ModelReleaseGateError("manual decision proof lifetime is invalid")
    if current < issued_at - timedelta(seconds=5) or current >= expires_at:
        raise ModelReleaseGateError("manual decision proof is not currently valid")


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
        "serving_artifact", "noninferiority", "release_sequence",
        "activation_nonce", "not_before", "expires_at",
    }, "promotion receipt")
    if receipt.get("schema") != PROMOTION_SCHEMA:
        raise ModelReleaseGateError("unsupported promotion receipt schema")
    _identifier(receipt.get("promotion_id"), "promotion_id")
    if receipt.get("status") != "PROMOTED":
        raise ModelReleaseGateError("promotion status is not PROMOTED")
    promoted_at = _rfc3339_datetime(receipt.get("promoted_at"), "promoted_at")
    if type(receipt.get("release_sequence")) is not int or receipt["release_sequence"] <= 0:
        raise ModelReleaseGateError("release_sequence must be a positive integer")
    _hex(receipt.get("activation_nonce"), "activation_nonce")
    not_before = _rfc3339_datetime(receipt.get("not_before"), "not_before")
    expires_at = _rfc3339_datetime(receipt.get("expires_at"), "expires_at")
    if not_before > promoted_at or promoted_at >= expires_at:
        raise ModelReleaseGateError("promotion timestamps are inconsistent")
    if expires_at - not_before > MAX_RELEASE_LIFETIME:
        raise ModelReleaseGateError("promotion validity exceeds seven days")

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
        "runtime_schema", "startup_nonce",
    }, "serving_artifact")
    if serving.get("backend") != EXPECTED_BACKEND:
        raise ModelReleaseGateError("serving backend is not the custom Python engine")
    if serving.get("binding_kind") not in ("pipeline-artifact", "pipeline-reference"):
        raise ModelReleaseGateError("serving binding kind is unsupported")
    if serving.get("runtime_schema") != EXPECTED_RUNTIME_SCHEMA:
        raise ModelReleaseGateError("serving runtime receipt schema is unsupported")
    _identifier(serving.get("artifact_id"), "serving artifact_id")
    _identifier(serving.get("startup_nonce"), "serving startup_nonce")
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
        "runtime_schema": serving["runtime_schema"],
        "binding_kind": serving["binding_kind"],
        "artifact_id": serving["artifact_id"],
        "artifact_manifest_sha256": serving["artifact_manifest_sha256"],
        "model_realpath": serving["model_realpath"],
        "runtime_receipt_sha256": serving["runtime_receipt_sha256"],
        "runtime_contract_sha256": serving["runtime_contract_sha256"],
        "readiness_smoke_sha256": serving["readiness_smoke_sha256"],
        "startup_nonce": serving["startup_nonce"],
    }
    for key, value in expected.items():
        if runtime.get(key) != value:
            raise ModelReleaseGateError(f"active runtime does not match promotion ({key})")


def _decision_matches(settings: ModelReleaseGateSettings,
                      decision_identity: Optional[Mapping[str, Any]],
                      runtime: Mapping[str, Any], *, decision_origin: str,
                      manual_proof: Any, manual_intent: Any) -> None:
    if decision_origin == "manual":
        if decision_identity is not None:
            raise ModelReleaseGateError("manual entry may not carry ambiguous model provenance")
        _verify_manual_decision_proof(settings, manual_proof, manual_intent)
        return
    if decision_origin != "model":
        raise ModelReleaseGateError("entry decision origin must be explicitly model or manual")
    if manual_proof is not None or manual_intent is not None:
        raise ModelReleaseGateError("model entry may not use a manual-provenance bypass")
    if not isinstance(decision_identity, Mapping):
        raise ModelReleaseGateError("model-generated entry is missing runtime identity")
    if (decision_identity.get("schema") != provenance.IDENTITY_SCHEMA
            or decision_identity.get("verified") is not True):
        raise ModelReleaseGateError("entry model identity is not a verified request receipt")
    nested = decision_identity.get("runtime")
    if not isinstance(nested, Mapping):
        raise ModelReleaseGateError("entry model identity has no verified runtime snapshot")
    for key in (
            "runtime_backend", "runtime_schema", "binding_kind", "artifact_id",
            "artifact_manifest_sha256", "runtime_receipt_sha256",
            "runtime_contract_sha256", "readiness_smoke_sha256", "model_realpath",
            "startup_nonce"):
        if nested.get(key) != runtime.get(key):
            raise ModelReleaseGateError(
                f"entry decision was not produced by the promoted runtime ({key})")
    for key in ("artifact_id", "artifact_manifest_sha256", "runtime_receipt_sha256",
                "runtime_contract_sha256", "model_realpath"):
        if decision_identity.get(key) != nested.get(key):
            raise ModelReleaseGateError(f"entry model identity is internally inconsistent ({key})")


def require_v3_release(
        settings: ModelReleaseGateSettings, *, endpoint: str,
        decision_identity: Optional[Mapping[str, Any]] = None,
        decision_origin: str = "model", manual_proof: Any = None,
        manual_intent: Any = None,
        runtime_snapshot: Callable[[str], Mapping[str, Any]] = provenance.runtime_snapshot,
        signature_verifier: Callable[[bytes, bytes, bytes], None] = _verify_ssh_signature,
        activation_reader: Optional[Callable[[str], Mapping[str, Any]]] = None,
        now: Optional[datetime] = None,
        _preflight_capability: Any = None,
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
    reader = activation_reader or _activation_head_from_ledger
    try:
        activation_head = reader(settings.activation_ledger)
    except ModelReleaseGateError:
        raise
    except Exception as exc:
        raise ModelReleaseGateError(f"cannot verify activation ledger: {exc}") from exc
    _validate_activation(receipt, _sha256(receipt_raw), activation_head, now=now)
    try:
        runtime = runtime_snapshot(endpoint)
    except ModelReleaseGateError:
        raise
    except Exception as exc:
        raise ModelReleaseGateError(f"cannot verify active model runtime: {exc}") from exc
    if not isinstance(runtime, Mapping):
        raise ModelReleaseGateError("active runtime identity is malformed")
    _runtime_matches(receipt, runtime)
    if _preflight_capability is _PREFLIGHT_CAPABILITY:
        if decision_identity is not None or manual_proof is not None or manual_intent is not None:
            raise ModelReleaseGateError("preflight may not accept entry provenance")
    else:
        _decision_matches(
            settings, decision_identity, runtime, decision_origin=decision_origin,
            manual_proof=manual_proof, manual_intent=manual_intent)
    serving = receipt["serving_artifact"]
    return {
        "enabled": True,
        "schema": receipt["schema"],
        "promotion_id": receipt["promotion_id"],
        "release_sequence": receipt["release_sequence"],
        "activation_nonce": receipt["activation_nonce"],
        "expires_at": receipt["expires_at"],
        "stage": receipt["lineage"]["stage"],
        "runtime_backend": serving["backend"],
        "runtime_schema": serving["runtime_schema"],
        "binding_kind": serving["binding_kind"],
        "artifact_id": serving["artifact_id"],
        "artifact_manifest_sha256": serving["artifact_manifest_sha256"],
        "model_realpath": serving["model_realpath"],
        "runtime_receipt_sha256": serving["runtime_receipt_sha256"],
        "runtime_contract_sha256": serving["runtime_contract_sha256"],
        "readiness_smoke_sha256": serving["readiness_smoke_sha256"],
        "startup_nonce": serving["startup_nonce"],
        "promotion_receipt_sha256": _sha256(receipt_raw),
    }


def preflight_v3_release(settings: ModelReleaseGateSettings, *, endpoint: str) -> Dict[str, Any]:
    """Verify release material/runtime without manufacturing an entry decision."""
    return require_v3_release(
        settings, endpoint=endpoint, _preflight_capability=_PREFLIGHT_CAPABILITY)


def revalidate_v3_release(expected: Mapping[str, Any],
                          settings: ModelReleaseGateSettings, *, endpoint: str,
                          decision_identity: Optional[Mapping[str, Any]] = None,
                          decision_origin: str = "model", manual_proof: Any = None,
                          manual_intent: Any = None,
                          runtime_snapshot: Callable[[str], Mapping[str, Any]] = provenance.runtime_snapshot,
                          signature_verifier: Callable[[bytes, bytes, bytes], None] = _verify_ssh_signature,
                          activation_reader: Optional[
                              Callable[[str], Mapping[str, Any]]] = None,
                          now: Optional[datetime] = None) -> Dict[str, Any]:
    """Repeat every proof under the broker mutation lock immediately before submit.

    This second pass catches a receipt revocation, ledger advance, file mutation,
    expiry, or runtime/startup swap that occurs after the earlier approval-time
    validation.  The caller must make ``placeOrder`` the next operation.
    """
    fresh = require_v3_release(
        settings, endpoint=endpoint, decision_identity=decision_identity,
        decision_origin=decision_origin, manual_proof=manual_proof,
        manual_intent=manual_intent, runtime_snapshot=runtime_snapshot,
        signature_verifier=signature_verifier,
        activation_reader=activation_reader, now=now)
    if not isinstance(expected, Mapping) or not hmac.compare_digest(
            _canonical_bytes(dict(expected)), _canonical_bytes(fresh)):
        raise ModelReleaseGateError("model release changed between approval and submission")
    return fresh
