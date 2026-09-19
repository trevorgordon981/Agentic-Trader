#!/usr/bin/env python3
"""Public API contract; production-derived narrative omitted."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import fcntl
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Iterator


DEFAULT_LOCK_DIR = Path("/opt/agentic-trader/.local/state/model-admission")
LOCK_DIR = Path(os.environ.get("MODEL_ADMISSION_DIR", str(DEFAULT_LOCK_DIR)))
SLOT_NAME = "slot.lock"
TRADER_INTENT_NAME = "trader-intent.lock"
GLM_BASE_URL = "http://127.0.0.1:18080/v1"
GLM_ENDPOINT = f"{GLM_BASE_URL}/chat/completions"
GLM_MODEL = "glm-5.3-flash-candidate"
GLM_IDENTITY_MATCH = (
    "/opt/agentic-trader/example-model-view/model-view/"
    "glm-5.3-flash-candidate"
)


class LeaseError(RuntimeError):
    """Public API contract; production-derived narrative omitted."""


class SlotBusy(LeaseError):
    """Public API contract; production-derived narrative omitted."""


class TraderWaiting(LeaseError):
    """Public API contract; production-derived narrative omitted."""


def _require_private_directory() -> int:
    """Public API contract; production-derived narrative omitted."""
    LOCK_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    dirfd = os.open(LOCK_DIR, flags)
    try:
        info = os.fstat(dirfd)
        if not stat.S_ISDIR(info.st_mode):
            raise LeaseError("GLM admission root is not a directory")
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise LeaseError("GLM admission root ownership/mode drift")
        return dirfd
    except BaseException:
        os.close(dirfd)
        raise


def _open_lock(name: str) -> int:
    if name not in {SLOT_NAME, TRADER_INTENT_NAME}:
        raise LeaseError("unrecognized GLM admission lock")
    dirfd = _require_private_directory()
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(name, flags, 0o600, dir_fd=dirfd)
    finally:
        os.close(dirfd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise LeaseError(f"{name} is not a regular file")
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise LeaseError(f"{name} ownership/mode drift")
        return fd
    except BaseException:
        os.close(fd)
        raise


@dataclass
class RetainedFlock:
    """Public API contract; production-derived narrative omitted."""

    fd: int
    name: str
    shared: bool
    _released: bool = False

    @classmethod
    def try_acquire(cls, name: str, *, shared: bool = False) -> "RetainedFlock":
        try:
            fd = _open_lock(name)
        except LeaseError:
            raise
        except OSError as exc:

            raise LeaseError(
                f"cannot prove {name} can be opened: errno={exc.errno}"
            ) from exc
        op = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        try:
            fcntl.flock(fd, op | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                if name == TRADER_INTENT_NAME and not shared:
                    raise TraderWaiting("trader intent is active") from exc
                raise SlotBusy(f"{name} is busy") from exc

            raise LeaseError(f"cannot prove {name} is available: errno={exc.errno}") from exc
        return cls(fd=fd, name=name, shared=shared)

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)

    def __enter__(self) -> "RetainedFlock":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def _probe_available(name: str, *, shared: bool = False) -> bool:
    """Public API contract; production-derived narrative omitted."""
    try:
        probe = RetainedFlock.try_acquire(name, shared=shared)
    except LeaseError:
        return False
    probe.release()
    return True


def trader_glm_lease_active() -> bool:
    """Public API contract; production-derived narrative omitted."""
    return not _probe_available(SLOT_NAME)


def trader_priority_active() -> bool:
    """Public API contract; production-derived narrative omitted."""
    return not _probe_available(TRADER_INTENT_NAME)


@contextlib.contextmanager
def trader_priority_intent() -> Iterator[RetainedFlock]:
    """Public API contract; production-derived narrative omitted."""
    intent = RetainedFlock.try_acquire(TRADER_INTENT_NAME, shared=True)
    try:
        yield intent
    finally:
        intent.release()


@contextlib.contextmanager
def chat_glm_admission() -> Iterator[RetainedFlock]:
    """Public API contract; production-derived narrative omitted."""
    slot = claim_chat_glm_slot()
    try:
        yield slot
    finally:
        slot.release()


def claim_chat_glm_slot() -> RetainedFlock:
    """Public API contract; production-derived narrative omitted."""
    gate = RetainedFlock.try_acquire(TRADER_INTENT_NAME, shared=False)
    slot: RetainedFlock | None = None
    try:
        slot = RetainedFlock.try_acquire(SLOT_NAME, shared=False)
    finally:
        gate.release()
    return slot


@contextlib.contextmanager
def trader_glm_lease(timeout_seconds: float = 0.0) -> Iterator[RetainedFlock]:
    """Public API contract; production-derived narrative omitted."""
    timeout = max(0.0, float(timeout_seconds))
    deadline = time.monotonic() + timeout
    intent: RetainedFlock | None = None
    slot: RetainedFlock | None = None
    try:
        while intent is None:
            try:
                intent = RetainedFlock.try_acquire(TRADER_INTENT_NAME, shared=True)
            except SlotBusy:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
        while slot is None:
            try:
                slot = RetainedFlock.try_acquire(SLOT_NAME, shared=False)
            except SlotBusy:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
        intent.release()
        intent = None
        yield slot
    finally:
        if slot is not None:
            slot.release()
        if intent is not None:
            intent.release()


@contextlib.asynccontextmanager
async def async_chat_glm_admission() -> AsyncIterator[RetainedFlock]:
    """Public API contract; production-derived narrative omitted."""
    lease_cm = chat_glm_admission()
    lease: RetainedFlock | None = None
    try:
        lease = lease_cm.__enter__()
        yield lease
    finally:
        if lease is not None:
            lease_cm.__exit__(None, None, None)


@contextlib.asynccontextmanager
async def async_trader_glm_admission(timeout_seconds: float) -> AsyncIterator[RetainedFlock]:
    """Public API contract; production-derived narrative omitted."""
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    intent: RetainedFlock | None = None
    slot: RetainedFlock | None = None
    try:
        while intent is None:
            try:
                intent = RetainedFlock.try_acquire(TRADER_INTENT_NAME, shared=True)
            except SlotBusy:
                if time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
        while slot is None:
            try:
                slot = RetainedFlock.try_acquire(SLOT_NAME, shared=False)
            except SlotBusy:
                if time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
        intent.release()
        intent = None
        yield slot
    finally:
        if slot is not None:
            slot.release()
        if intent is not None:
            intent.release()
