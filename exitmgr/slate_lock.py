"""Cooperative soft-mutex between the daily slate and the trader loop (2026-06-23).

The local M3 model server (~/m3_serve.py) serializes ALL generation behind one GEN_LOCK -- only one
generation runs at a time. The (thinking-on, multi-minute) daily slate and the 15-min trader loop
were both demanding the model and colliding into long queues / 503s. This is the COOPERATIVE half of
the fix: while the slate is generating it writes a flag file; the trader checks the flag at the top of
its cycle and, if the slate is active, DEFERS its (non-urgent, exit-management) model call to the next
tick rather than queueing behind the slate. The server-side bounded queue (M3_LOCK_WAIT_S) is the
safety net; this mutex avoids the wait in the common case.

Staleness: the flag carries a PID + epoch timestamp and is ignored once older than SLATE_STALE_S
(default 15min) so a crashed/killed slate can never block the trader forever. Best-effort throughout:
any FS error -> treat as "slate not active" (the trader proceeds; worst case it queues server-side).
"""
import json
import os
import time

# Default beside the app's other runtime files (KILL_SWITCH etc.). Overridable for tests / relocation.
FLAG_PATH = os.environ.get(
    "SLATE_ACTIVE_FLAG",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".slate_active"),
)
SLATE_STALE_S = int(os.environ.get("SLATE_STALE_S", "900"))  # 15 min


def _path():
    # Read env at call time so tests can monkeypatch SLATE_ACTIVE_FLAG via env.
    return os.environ.get("SLATE_ACTIVE_FLAG", FLAG_PATH)


def mark_slate_active(path=None):
    """Write the slate-active flag (PID + timestamp). Best-effort; returns the path written or None."""
    p = path or _path()
    try:
        with open(p, "w") as f:
            json.dump({"pid": os.getpid(), "ts": time.time()}, f)
        return p
    except OSError:
        return None


def clear_slate_active(path=None):
    """Remove the slate-active flag. Best-effort (a missing file is fine)."""
    p = path or _path()
    try:
        os.unlink(p)
    except OSError:
        pass


def slate_active(path=None, stale_s=None):
    """True iff a FRESH slate-active flag exists. A flag older than stale_s (default SLATE_STALE_S)
    is treated as stale (crashed slate) -> False, and proactively removed so it stops lying."""
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
        clear_slate_active(p)  # stale -> clear so it can't block forever
        return False
    return True


class slate_active_guard:
    """Context manager: mark the slate active for the duration of the model-generating phase,
    and always clear it on exit (incl. exceptions). Used by daily_recommend around discover/propose."""

    def __init__(self, path=None):
        self.path = path

    def __enter__(self):
        mark_slate_active(self.path)
        return self

    def __exit__(self, *exc):
        clear_slate_active(self.path)
        return False
