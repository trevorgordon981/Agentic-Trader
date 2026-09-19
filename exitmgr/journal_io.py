"""Public API contract; production-derived narrative omitted."""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def journal_lock(path):
    """Public API contract; production-derived narrative omitted."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(str(path) + ".lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield path
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def append_jsonl_locked(path, row) -> bytes:
    """Public API contract; production-derived narrative omitted."""
    encoded = (json.dumps(row, sort_keys=True, separators=(",", ":"),
                          allow_nan=False, default=str) + "\n").encode()
    with journal_lock(path) as journal:
        fd = os.open(journal, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.fchmod(fd, 0o600)
            written = os.write(fd, encoded)
            if written != len(encoded):
                raise OSError(f"short journal append: {written}/{len(encoded)} bytes")
            os.fsync(fd)
        finally:
            os.close(fd)
    return encoded
