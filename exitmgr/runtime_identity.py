"""Public API contract; production-derived narrative omitted."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import subprocess
from collections.abc import Mapping
from typing import Any


_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class RuntimeIdentityError(RuntimeError):
    """Public API contract; production-derived narrative omitted."""


@dataclasses.dataclass(frozen=True)
class RuntimeIdentity:
    code_version: str
    policy_version: str

    def __post_init__(self):
        if not _GIT_SHA.fullmatch(str(self.code_version or "")):
            raise RuntimeIdentityError("code_version must be an exact lowercase 40-hex Git HEAD")
        if not _SHA256.fullmatch(str(self.policy_version or "")):
            raise RuntimeIdentityError("policy_version must be a lowercase SHA-256")

    def as_dict(self):
        return {"code_version": self.code_version, "policy_version": self.policy_version}


def _plain(value: Any):
    """Public API contract; production-derived narrative omitted."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):





        runtime_only = {"dry_run", "arm", "loop_mode"}
        return {key: _plain(val) for key, val in vars(value).items()
                if not key.startswith("_") and key not in runtime_only}
    if isinstance(value, Mapping):
        return {str(key): _plain(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(val) for val in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_plain(val) for val in value), key=lambda val: json.dumps(
            val, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False))
    if isinstance(value, os.PathLike):
        return os.fspath(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise RuntimeIdentityError("resolved config contains unsupported %s" % type(value).__name__)


def resolved_config_dict(config) -> dict:
    payload = _plain(config)
    if not isinstance(payload, dict):
        raise RuntimeIdentityError("resolved config must be a mapping")
    return payload


def execution_config_dict(config) -> dict:
    """Public API contract; production-derived narrative omitted."""
    from exitmgr.config import TRADING_DEFAULTS

    resolved = resolved_config_dict(config)
    sections = ("ib", "journal", "state", "kill_switch", "loop", "scope", "caps",
                "rules", "construction")
    missing = [name for name in sections if name not in resolved]
    if missing:
        raise RuntimeIdentityError(
            "resolved config is missing required sections: %s" % ", ".join(missing))
    missing_trading = [key for key, _default in TRADING_DEFAULTS if key not in resolved]
    if missing_trading:
        raise RuntimeIdentityError(
            "resolved config is missing registered trading keys: %s" %
            ", ".join(missing_trading))
    payload = {name: resolved[name] for name in sections}
    payload["trading"] = {key: resolved[key] for key, _default in TRADING_DEFAULTS}
    return payload


def resolved_config_sha256(config) -> str:
    try:
        raw = json.dumps(resolved_config_dict(config), sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True, allow_nan=False).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise RuntimeIdentityError("resolved config is not canonical JSON: %s" % exc) from exc
    return hashlib.sha256(raw).hexdigest()


_CODE_PATHSPECS = (
    ":(glob)**/*.py", ":(glob)**/*.pyi", ":(glob)**/*.sh",
    ":(glob)**/pyproject.toml", ":(glob)**/requirements*.txt",
)


def git_head(repo_root=None, *, require_clean_code: bool = False) -> str:
    root = repo_root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        result = subprocess.run(
            ["git", "-C", os.fspath(root), "rev-parse", "HEAD"], capture_output=True,
            text=True, timeout=5, check=True)
    except Exception as exc:
        raise RuntimeIdentityError("cannot resolve exact Git HEAD: %s" % exc) from exc
    head = result.stdout.strip().lower()
    if not _GIT_SHA.fullmatch(head):
        raise RuntimeIdentityError("git rev-parse HEAD did not return lowercase 40-hex")
    if require_clean_code:
        try:
            status = subprocess.run(
                ["git", "-C", os.fspath(root), "status", "--porcelain",
                 "--untracked-files=all", "--", *_CODE_PATHSPECS],
                capture_output=True, text=True, timeout=5, check=True)
        except Exception as exc:
            raise RuntimeIdentityError(
                "cannot verify that executable source matches Git HEAD: %s" % exc) from exc
        dirty = [line.rstrip() for line in status.stdout.splitlines() if line.strip()]
        if dirty:
            raise RuntimeIdentityError(
                "executable source does not match Git HEAD: %s" % "; ".join(dirty[:10]))
    return head


def freeze_runtime_identity(config, repo_root=None, *,
                            require_clean_code: bool = False) -> RuntimeIdentity:
    """Public API contract; production-derived narrative omitted."""
    return RuntimeIdentity(code_version=git_head(
                               repo_root, require_clean_code=require_clean_code),
                           policy_version=resolved_config_sha256(config))


def identity_fields(identity: RuntimeIdentity) -> dict:
    if not isinstance(identity, RuntimeIdentity):
        raise RuntimeIdentityError("a frozen RuntimeIdentity is required")
    return identity.as_dict()
