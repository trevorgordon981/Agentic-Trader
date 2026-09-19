"""Public API contract; production-derived narrative omitted."""
from __future__ import annotations

import json
import os
import re
import time









STATE = (os.environ.get("EXITMGR_ALERT_THROTTLE")
         or os.path.expanduser("~/.local/var/alert-throttle.json"))


BACKOFF = (0, 3600, 7200, 14400, 28800, 43200)




FORGET_AFTER = 26 * 3600




_VOLATILE = (
    re.compile(r"\d{4}-\d{2}-\d{2}T[\d:.+\-]+"),
    re.compile(r"age \d+ min"),
    re.compile(r"\d+/\d+"),
    re.compile(r"\(live \d+\)"),
    re.compile(r"[\d,]+\.\d{2}"),
)


def derive_key(text, channel_id=""):
    """Public API contract; production-derived narrative omitted."""
    try:
        head = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
        for rx in _VOLATILE:
            head = rx.sub("", head)
        ids = ",".join(sorted(set(re.findall(r"con_id=(\d+)", text or ""))))
        head = " ".join(head.split())[:160]
        if not head and not ids:




            return "unkeyable|%.6f|%d" % (time.time(), os.getpid())
        return "%s|%s|%s" % (channel_id, head, ids)
    except Exception:

        return "unkeyable|%f" % time.time()


def _load(path):
    try:
        with open(path) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(path, state):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = "%s.tmp.%d" % (path, os.getpid())
        with open(tmp, "w") as fh:
            json.dump(state, fh, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        pass


def consider(text, channel_id="", key=None, now=None, state_path=None, *, record=True):
    """Public API contract; production-derived narrative omitted."""
    now = time.time() if now is None else now
    path = state_path or STATE
    try:
        k = key or derive_key(text, channel_id)
        state = _load(path)

        for old, rec in list(state.items()):
            if now - float(rec.get("last_send", 0) or 0) > FORGET_AFTER:
                state.pop(old, None)

        rec = state.get(k) or {}




        prev = rec.get("last_send")
        has_sent = prev is not None
        last = float(prev or 0.0)
        sent = int(rec.get("sent", 0) or 0)
        held = int(rec.get("suppressed", 0) or 0)

        wait = BACKOFF[min(sent, len(BACKOFF) - 1)]
        if has_sent and (now - last) < wait:
            rec["suppressed"] = held + 1
            rec["last_send"] = last




            if rec.get("first_suppressed") is None:
                rec["first_suppressed"] = now
            if record:
                state[k] = rec
                _save(path, state)
            return False, held + 1, ""

        note = ""
        if held:
            mins = (now - float(rec.get("first_suppressed") or now)) / 60.0
            note = ("\n_(%d identical alert%s suppressed over the last %.0f min; this "
                    "condition has now been reported %d times and is still unfixed)_"
                    % (held, "" if held == 1 else "s", mins, sent + 1))
        if record:
            state[k] = {"last_send": now, "sent": sent + 1, "suppressed": 0,
                        "first_suppressed": None}
            _save(path, state)
        return True, held, note
    except Exception:
        return True, 0, ""


def record_delivery(text, channel_id="", key=None, now=None, state_path=None):
    """Public API contract; production-derived narrative omitted."""
    now = time.time() if now is None else now
    path = state_path or STATE
    try:
        k = key or derive_key(text, channel_id)
        state = _load(path)
        for old, rec in list(state.items()):
            if now - float(rec.get("last_send", 0) or 0) > FORGET_AFTER:
                state.pop(old, None)
        rec = state.get(k) or {}
        state[k] = {
            "last_send": now,
            "sent": int(rec.get("sent", 0) or 0) + 1,
            "suppressed": 0,
            "first_suppressed": None,
        }
        _save(path, state)
        return True
    except Exception:
        return False


def clear(key, state_path=None):
    """Public API contract; production-derived narrative omitted."""
    path = state_path or STATE
    try:
        state = _load(path)
        state.pop(str(key), None)
        _save(path, state)
        return True
    except Exception:
        return False
