"""Public API contract; production-derived narrative omitted."""
from __future__ import annotations

import hashlib
import html
import json
import os
from pathlib import Path
import re
import time
import urllib.request


def safe_detail(value: str) -> str:
    text = str(value)
    text = re.sub(r"https?://\S+", "[service endpoint]", text)
    text = re.sub(r"\b(?:DU|U)\d{5,}\b", "[account]", text)
    text = re.sub(r"\bxox[a-z]-[\w-]+", "[token]", text)
    text = re.sub(r"(?i)bearer\s+\S+", "Bearer [redacted]", text)
    return html.escape(" ".join(text.split())[:700])


def _post(token: str, channel: str, text: str, message_id: str) -> str | None:
    """Public API contract; production-derived narrative omitted."""
    request = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=json.dumps({"channel": channel, "text": text,
                         "client_msg_id": message_id}).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        raw = response.read(65537)
    if len(raw) > 65536:
        return None
    result = json.loads(raw)
    ts = result.get("ts")
    return ts if result.get("ok") is True and isinstance(ts, str) and ts else None


def deliver(*, path: str, token: str, channel: str, kind: str, text: str,
            symbol: str = "", recovery: bool = False, now: float | None = None,
            post=None) -> dict:
    """Public API contract; production-derived narrative omitted."""
    now = time.time() if now is None else now
    post = _post if post is None else post
    target = Path(path)
    key = f"{channel}:{kind}:{symbol}"
    try:
        state = json.loads(target.read_text())
        if not isinstance(state, dict):
            state = {}
    except (OSError, ValueError):
        state = {}
    prior = state.get(key)
    if not isinstance(prior, dict):
        prior = {}
    if recovery and not prior:
        return {"status": "no_previous_failure"}

    fingerprint = hashlib.sha256(re.sub(r"\d+(?:\.\d+)?", "#", text).encode()).hexdigest()
    if not recovery and prior.get("fingerprint") == fingerprint:
        try:
            elapsed = now - float(prior["sent_at"])
            if 0 <= elapsed < 3600:
                return {"status": "suppressed"}
        except (KeyError, TypeError, ValueError):
            pass
    if not token or not channel:
        return {"status": "failed", "reason": "notification_not_configured"}

    import uuid
    message_id = str(uuid.uuid4())
    try:
        ts = post(token, channel, text, message_id)
    except Exception as exc:
        return {"status": "failed", "reason": type(exc).__name__}
    if not ts:
        return {"status": "failed", "reason": "no_delivery_receipt"}
    if recovery:
        state.pop(key, None)
    else:
        state[key] = {"fingerprint": fingerprint, "sent_at": now, "slack_ts": ts}

    state = {k: v for k, v in state.items() if isinstance(v, dict)
             and isinstance(v.get("sent_at"), (int, float))
             and 0 <= now - v["sent_at"] < 7 * 86400}
    temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(state, stream, sort_keys=True)
        os.replace(temp, target)
    except OSError:
        return {"status": "sent", "slack_ts": ts, "dedup_saved": False}
    return {"status": "sent", "slack_ts": ts, "dedup_saved": True}
