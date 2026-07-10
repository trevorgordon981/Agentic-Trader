"""Cross-process serialization for broker order mutations on Studio."""

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import time


class OrderMutationBusy(RuntimeError):
    pass


def lock_path() -> Path:
    return Path(os.environ.get("EXITMGR_ORDER_LOCK", "/tmp/alfred-order-mutation.lock"))


@contextmanager
def order_mutation_lock(*, timeout_seconds: float = 2.0):
    """Hold one host-wide lock only around the actual ``placeOrder`` mutation."""
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise OrderMutationBusy(f"broker order mutation lock busy: {path}")
                time.sleep(0.02)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
