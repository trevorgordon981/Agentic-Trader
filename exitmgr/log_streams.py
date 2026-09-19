"""Public API contract; production-derived narrative omitted."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sys
import threading
from typing import Optional, TextIO


DEFAULT_MAX_BYTES = 25 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 5


def _utf8_prefix_length(data: bytes, limit: int) -> int:
    """Public API contract; production-derived narrative omitted."""
    if limit >= len(data):
        return len(data)
    end = max(0, limit)
    while end > 0 and data[end] & 0xC0 == 0x80:
        end -= 1
    return end


class BoundedRotatingTextStream:
    """Public API contract; production-derived narrative omitted."""

    encoding = "utf-8"
    errors = "backslashreplace"
    line_buffering = True
    write_through = True

    def __init__(self, path, *, max_bytes=DEFAULT_MAX_BYTES,
                 backup_count=DEFAULT_BACKUP_COUNT, fallback: Optional[TextIO] = None):
        self.path = Path(path)
        self.max_bytes = int(max_bytes)
        self.backup_count = int(backup_count)
        if self.max_bytes < 4:
            raise ValueError("max_bytes must be at least four")
        if self.backup_count < 0:
            raise ValueError("backup_count cannot be negative")
        self._fallback = fallback
        self._lock = threading.RLock()
        self._closed = False
        self._stream = None
        self._size = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._open()

    @property
    def closed(self):
        return self._closed

    def writable(self):
        return True

    def isatty(self):
        return False

    def fileno(self):
        if self._closed or self._stream is None:
            raise ValueError("I/O operation on closed log stream")
        return self._stream.fileno()

    def _open(self):
        fd = os.open(self.path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        self._stream = os.fdopen(fd, "ab", buffering=0)
        self._size = self.path.stat().st_size

    def _rotate(self):
        self._stream.close()
        self._stream = None
        if self.backup_count:
            oldest = Path(f"{self.path}.{self.backup_count}")
            if oldest.exists():
                oldest.unlink()
            for index in range(self.backup_count - 1, 0, -1):
                source = Path(f"{self.path}.{index}")
                if source.exists():
                    os.replace(source, Path(f"{self.path}.{index + 1}"))
            if self.path.exists():
                os.replace(self.path, Path(f"{self.path}.1"))
        elif self.path.exists():
            self.path.unlink()
        self._open()

    def _write_bytes(self, data: bytes):
        while data:
            if self._size >= self.max_bytes:
                self._rotate()
            room = self.max_bytes - self._size
            take = _utf8_prefix_length(data, min(len(data), room))
            if take <= 0:
                self._rotate()
                continue
            self._stream.write(data[:take])
            self._size += take
            data = data[take:]
            if data:
                self._rotate()

    def write(self, text):
        if self._closed:
            raise ValueError("I/O operation on closed log stream")
        if not isinstance(text, str):
            raise TypeError("write() argument must be str")
        if not text:
            return 0
        try:
            with self._lock:
                self._write_bytes(text.encode(self.encoding, self.errors))
        except Exception:


            if self._fallback is None:
                raise
            try:
                self._fallback.write(text)
                self._fallback.flush()
            except Exception:
                pass
        return len(text)

    def flush(self):
        if self._closed:
            return
        try:
            with self._lock:
                self._stream.flush()
        except Exception:
            if self._fallback is not None:
                self._fallback.flush()

    def close(self):
        if self._closed:
            return
        with self._lock:
            stream = self._stream
            self._stream = None
            self._closed = True
            if stream is None:
                return
            try:
                stream.flush()
            finally:
                stream.close()


@dataclass
class InstalledLogStreams:
    stdout: BoundedRotatingTextStream
    stderr: BoundedRotatingTextStream
    original_stdout: TextIO
    original_stderr: TextIO

    def restore(self):
        """Public API contract; production-derived narrative omitted."""
        if sys.stdout is self.stdout:
            sys.stdout = self.original_stdout
        if sys.stderr is self.stderr:
            sys.stderr = self.original_stderr
        self.stdout.close()
        self.stderr.close()


def install_standard_stream_rotation(stdout_path, stderr_path, *,
                                     max_bytes=DEFAULT_MAX_BYTES,
                                     backup_count=DEFAULT_BACKUP_COUNT):
    """Public API contract; production-derived narrative omitted."""
    out_path = Path(stdout_path).resolve()
    err_path = Path(stderr_path).resolve()
    original_stdout, original_stderr = sys.stdout, sys.stderr
    if out_path == err_path:
        try:
            original_stderr.write(
                "[log-rotation] disabled: stdout and stderr rotation paths must differ\n")
            original_stderr.flush()
        except Exception:
            pass
        return None
    stdout = stderr = None
    try:
        stdout = BoundedRotatingTextStream(
            out_path, max_bytes=max_bytes, backup_count=backup_count,
            fallback=original_stdout)
        stderr = BoundedRotatingTextStream(
            err_path, max_bytes=max_bytes, backup_count=backup_count,
            fallback=original_stderr)
    except Exception as exc:
        if stdout is not None:
            stdout.close()
        if stderr is not None:
            stderr.close()
        try:
            original_stderr.write(f"[log-rotation] disabled: {exc}\n")
            original_stderr.flush()
        except Exception:
            pass
        return None
    sys.stdout, sys.stderr = stdout, stderr
    return InstalledLogStreams(stdout, stderr, original_stdout, original_stderr)
