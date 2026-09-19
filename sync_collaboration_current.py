#!/usr/bin/env python3
"""Public API contract; production-derived narrative omitted."""
from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import build_collaboration_snapshot as snapshot


HOME = Path.home()
SSH = Path("/usr/bin/ssh")
IDENTITY = HOME / ".ssh" / "id_ed25519"
SOURCE_HOST = "source_host"
SOURCE_PATH = "/home/example-user/ai-collaboration/CURRENT.md"
SOURCE_KEY_FINGERPRINT = "SHA256:rgOQdEWK8F5BUQ5eAgl0KE5A4e/evqP9Gyvlxdfz7/I"
OUTPUT = HOME / "rag-journal-stage" / snapshot.OUTPUT_NAME
RECEIPT = HOME / ".hermes" / "rag-staging" / "collaboration-current.receipt.json"
MAX_ENTRIES = 12
MAX_CHARS = 24_000
FETCH_TIMEOUT_SECONDS = 30

_REMOTE_READER = r"""
import os, stat, sys
p = sys.argv[1]
flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
fd = os.open(p, flags)
try:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise SystemExit("source is not a regular file")
    if info.st_size <= 0 or info.st_size > 2_000_000:
        raise SystemExit("source size outside allowed bounds")
    chunks = []
    remaining = info.st_size
    while remaining:
        block = os.read(fd, min(1 << 20, remaining))
        if not block:
            raise SystemExit("source changed or was truncated while reading")
        chunks.append(block)
        remaining -= len(block)
    final = os.fstat(fd)
    if (final.st_size, final.st_mtime_ns, final.st_ino) != (
        info.st_size, info.st_mtime_ns, info.st_ino
    ):
        raise SystemExit("source changed while reading")
    sys.stdout.buffer.write(b"".join(chunks))
finally:
    os.close(fd)
""".strip()


class SyncError(RuntimeError):
    """Public API contract; production-derived narrative omitted."""


def _load_existing_receipt() -> dict | None:
    """Public API contract; production-derived narrative omitted."""
    try:
        raw = snapshot._read_regular_nofollow(RECEIPT)
        parsed = json.loads(raw.decode("utf-8", "strict"))
    except (snapshot.SnapshotError, OSError, UnicodeError, ValueError, TypeError):
        return None
    if not isinstance(parsed, dict) or parsed.get("schema") != snapshot.SCHEMA:
        return None
    expected = parsed.get("output_sha256")
    if not isinstance(expected, str) or len(expected) != 64:
        return None
    try:
        actual = snapshot.sha256(snapshot._read_regular_nofollow(OUTPUT))
    except (snapshot.SnapshotError, OSError):
        return None
    return parsed if actual == expected else None


def _validate_identity() -> None:
    try:
        info = IDENTITY.lstat()
    except OSError as exc:
        raise SyncError(f"missing pinned SSH identity: {exc}") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise SyncError("pinned SSH identity is not a regular non-symlink file")
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise SyncError("pinned SSH identity ownership or mode is unsafe")


def fetch_source(*, runner=subprocess.run) -> bytes:
    _validate_identity()
    remote = shlex.join(["/usr/bin/python3", "-c", _REMOTE_READER, SOURCE_PATH])
    command = [
        str(SSH),
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", f"IdentityFile={IDENTITY}",
        "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=5",
        "-o", "ServerAliveCountMax=2",
        SOURCE_HOST,
        remote,
    ]
    try:
        result = runner(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=FETCH_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SyncError(f"authoritative CURRENT fetch failed: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()[:400]
        raise SyncError(f"authoritative CURRENT fetch exited {result.returncode}: {detail}")
    data = bytes(result.stdout)
    if not data or len(data) > snapshot.MAX_SOURCE_BYTES:
        raise SyncError("fetched CURRENT size outside allowed bounds")
    return data


def publish(data: bytes, *, generated_at: datetime | None = None) -> dict:
    generated_at = generated_at or datetime.now(timezone.utc)
    fd, temporary = tempfile.mkstemp(prefix="collaboration-current-source.")
    temp_path = Path(temporary)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, data)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        rendered, receipt = snapshot.build_snapshot(
            temp_path,
            max_entries=MAX_ENTRIES,
            max_chars=MAX_CHARS,
            generated_at=generated_at,
        )
        receipt.update({
            "source": f"{SOURCE_HOST}:{SOURCE_PATH}",
            "source_transport": "ssh-pinned-identity",
            "source_key_fingerprint": SOURCE_KEY_FINGERPRINT,
        })
        snapshot._atomic_write(OUTPUT, rendered)
        snapshot._atomic_write(
            RECEIPT,
            (json.dumps(receipt, sort_keys=True) + "\n").encode("utf-8"),
        )
        return receipt
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def sync_current(
    data: bytes | None = None,
    *,
    generated_at: datetime | None = None,
) -> tuple[dict, bool]:
    """Public API contract; production-derived narrative omitted."""
    data = fetch_source() if data is None else data
    source_hash = snapshot.sha256(data)
    existing = _load_existing_receipt()
    if existing is not None and existing.get("source_sha256") == source_hash:
        return existing, False
    return publish(data, generated_at=generated_at), True


def main() -> int:
    try:
        receipt, changed = sync_current()
    except (SyncError, snapshot.SnapshotError, OSError, UnicodeError) as exc:
        print(f"collaboration-current sync failed; last known-good retained: {exc}", file=sys.stderr)
        return 1
    result = dict(receipt)
    result["sync_changed"] = changed
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
