#!/usr/bin/env python3
"""Public API contract; production-derived narrative omitted."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path


SCHEMA = "alfred-collaboration-current-snapshot-v2"
OUTPUT_NAME = "collaboration-current.md"
MAX_SOURCE_BYTES = 2_000_000
DEFAULT_MAX_ENTRIES = 12
DEFAULT_MAX_CHARS = 24_000
_UPDATED = re.compile(r"^Updated:\s+\*\*\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s+[A-Z]{2,5}\*\*")
_SECRET_PATTERNS = (
    re.compile(r"(?i)authorization\s*:\s*bearer\s+\S+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\b(?:api[_-]?key|client[_-]?secret|access[_-]?token)\s*[:=]\s*['\"]?[A-Za-z0-9_./+=-]{16,}"),
)


class SnapshotError(RuntimeError):
    """Public API contract; production-derived narrative omitted."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_regular_nofollow(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise SnapshotError(f"cannot safely open source: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SnapshotError("source is not a regular file")
        if info.st_size <= 0 or info.st_size > MAX_SOURCE_BYTES:
            raise SnapshotError("source size outside allowed bounds")
        data = os.read(fd, MAX_SOURCE_BYTES + 1)
        if len(data) != info.st_size:
            raise SnapshotError("source changed or was truncated while reading")
        return data
    finally:
        os.close(fd)


def _leading_updates(text: str, max_entries: int, max_chars: int) -> list[str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "# Current shared state":
        raise SnapshotError("unexpected CURRENT header")
    entries: list[str] = []
    used = 0
    saw_update = False
    for line in lines[1:]:
        if not line.strip():
            continue
        if _UPDATED.match(line):
            saw_update = True
            if any(pattern.search(line) for pattern in _SECRET_PATTERNS):
                raise SnapshotError("secret-shaped value found in selected CURRENT record")
            projected = used + len(line) + 2
            if len(entries) >= max_entries or projected > max_chars:
                break
            entries.append(line)
            used = projected
            continue
        if saw_update:
            break
        raise SnapshotError("unexpected content before first CURRENT update")
    if not entries:
        raise SnapshotError("no bounded leading CURRENT updates found")
    return entries


def build_snapshot(source: Path, *, max_entries: int, max_chars: int,
                   generated_at: datetime) -> tuple[bytes, dict]:
    if not (1 <= max_entries <= 50):
        raise SnapshotError("max_entries outside 1..50")
    if not (2_000 <= max_chars <= 100_000):
        raise SnapshotError("max_chars outside 2000..100000")
    raw = _read_regular_nofollow(source)
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise SnapshotError("CURRENT is not strict UTF-8") from exc
    entries = _leading_updates(text, max_entries, max_chars)
    stamp = generated_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    source_hash = sha256(raw)
    header = (
        "---\n"
        f"schema: {SCHEMA}\n"
        "authority: generated-current-state\n"
        "supersedes: collaboration-current\n"
        "source: ai-collaboration/CURRENT.md\n"
        f"source_sha256: {source_hash}\n"
        f"generated_at: {stamp}\n"
        f"selected_entries: {len(entries)}\n"
        "raw_work_log_ingested: false\n"
        "---\n\n"
        "# Current collaboration state\n\n"
        "Machine-generated bounded snapshot. It replaces every earlier file with this identity; "
        "the append-only WORK_LOG is deliberately excluded.\n\n"
        f"Authoritative source identity: source_sha256: {source_hash}\n\n"
    )
    rendered = (header + "\n\n".join(entries) + "\n").encode("utf-8")
    receipt = {
        "schema": SCHEMA,
        "status": "COMPLETE",
        "source": str(source),
        "source_sha256": source_hash,
        "output_name": OUTPUT_NAME,
        "output_sha256": sha256(rendered),
        "generated_at": stamp,
        "selected_entries": len(entries),
        "output_bytes": len(rendered),
        "raw_work_log_ingested": False,
    }
    return rendered, receipt


def _atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent_info = path.parent.lstat()
    if not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode):
        raise SnapshotError("output parent is not a real directory")
    if path.is_symlink():
        raise SnapshotError("output path is a symlink")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        os.write(fd, data)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(temporary, path)
        parent_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--max-entries", type=int, default=DEFAULT_MAX_ENTRIES)
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    args = parser.parse_args()
    rendered, receipt = build_snapshot(
        args.source,
        max_entries=args.max_entries,
        max_chars=args.max_chars,
        generated_at=datetime.now(timezone.utc),
    )
    output = args.output_dir / OUTPUT_NAME
    _atomic_write(output, rendered)
    _atomic_write(args.receipt, (json.dumps(receipt, sort_keys=True) + "\n").encode("utf-8"))
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
