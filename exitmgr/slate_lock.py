"""Public API contract; production-derived narrative omitted."""
import json
import os
import time


FLAG_PATH = os.environ.get(
    "SLATE_ACTIVE_FLAG",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".slate_active"),
)
SLATE_STALE_S = int(os.environ.get("SLATE_STALE_S", "900"))


def _path():

    return os.environ.get("SLATE_ACTIVE_FLAG", FLAG_PATH)


def mark_slate_active(path=None):
    """Public API contract; production-derived narrative omitted."""
    p = path or _path()
    try:
        with open(p, "w") as f:
            json.dump({"pid": os.getpid(), "ts": time.time()}, f)
        return p
    except OSError:
        return None


def clear_slate_active(path=None):
    """Public API contract; production-derived narrative omitted."""
    p = path or _path()
    try:
        os.unlink(p)
    except OSError:
        pass


def slate_active(path=None, stale_s=None):
    """Public API contract; production-derived narrative omitted."""
    p = path or _path()
    s = SLATE_STALE_S if stale_s is None else stale_s
    try:
        with open(p) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False
    ts = data.get("ts")
    if not isinstance(ts, (int, float)):
        return False
    if (time.time() - ts) > s:
        clear_slate_active(p)
        return False
    return True


class slate_active_guard:
    """Public API contract; production-derived narrative omitted."""

    def __init__(self, path=None):
        self.path = path

    def __enter__(self):
        mark_slate_active(self.path)
        return self

    def __exit__(self, *exc):
        clear_slate_active(self.path)
        return False
