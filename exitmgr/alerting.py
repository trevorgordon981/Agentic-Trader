"""Public API contract; production-derived narrative omitted."""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

APP_DIR = os.path.expanduser("~/exitmgr-app")
CONFIG_PATH = os.path.join(APP_DIR, "config.yaml")
HERMES_ENV = os.path.expanduser("~/.hermes/.env")



_FALLBACK = {
    "slack_channel": "CHANNEL_ID_PLACEHOLDER",
    "summary_channel": "SUMMARY_CHANNEL_PLACEHOLDER",
    "alerts_channel": "ALERTS_CHANNEL_PLACEHOLDER",
    "error_channel": "ERROR_CHANNEL_PLACEHOLDER",
}
_PLACEHOLDER_PREFIXES = ("C0XXX", "U0XXX")

_cache = None


def _trading_cfg():
    """Public API contract; production-derived narrative omitted."""
    global _cache
    if _cache is None:
        try:
            import yaml
            with open(CONFIG_PATH) as f:
                _cache = (yaml.safe_load(f) or {}).get("trading", {}) or {}
        except Exception as e:
            print("alerting: config read failed (%s); using fallback IDs" % e,
                  file=sys.stderr)
            _cache = {}
    return _cache


def channel(key):
    """Public API contract; production-derived narrative omitted."""
    fallback = _FALLBACK.get(key)
    val = _trading_cfg().get(key)
    if isinstance(val, str):
        val = val.strip()
    if not val or val.startswith(_PLACEHOLDER_PREFIXES):
        if val:
            print("alerting: config %s is a PLACEHOLDER (%r) -- config.yaml has "
                  "been scrubbed again; falling back to %s" % (key, val, fallback),
                  file=sys.stderr)
        return fallback
    return val


def approvals_channel():
    return channel("slack_channel")


def positions_channel():
    return channel("summary_channel")


def alerts_channel():
    return channel("alerts_channel")


def error_channel():
    return channel("error_channel")


def approver_ids():
    """Public API contract; production-derived narrative omitted."""
    ids = _trading_cfg().get("approver_ids") or []
    clean = {str(i).strip() for i in ids
             if str(i).strip() and not str(i).strip().startswith(_PLACEHOLDER_PREFIXES)}
    if not clean:
        print("alerting: no usable approver_ids in config -- falling back to "
              "USER_ID_PLACEHOLDER", file=sys.stderr)
        return {"USER_ID_PLACEHOLDER"}
    return clean


def token():
    """Public API contract; production-derived narrative omitted."""
    tok = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if tok:
        return tok
    try:
        for line in open(HERMES_ENV):
            if line.startswith("SLACK_BOT_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception as e:
        print("alerting: token read failed:", e, file=sys.stderr)
    return None


def api(method, payload, tok=None, timeout=10, encoding="json"):
    """Public API contract; production-derived narrative omitted."""
    tok = tok or token()
    if not tok:
        return {"ok": False, "error": "no_token"}
    try:
        if encoding == "form":
            data = urllib.parse.urlencode(payload).encode()
            ctype = "application/x-www-form-urlencoded; charset=utf-8"
        else:
            data = json.dumps(payload).encode()
            ctype = "application/json; charset=utf-8"
        req = urllib.request.Request(
            "https://slack.com/api/" + method,
            data=data,
            headers={"Authorization": "Bearer " + tok, "Content-Type": ctype})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")
    except Exception as e:
        return {"ok": False, "error": "transport: %s" % e}


def check_channel(channel_id, tok=None, timeout=10):
    """Public API contract; production-derived narrative omitted."""
    b = api("conversations.info", {"channel": channel_id}, tok=tok,
            timeout=timeout, encoding="form")
    if not b.get("ok"):
        return False, "lookup failed: %s" % b.get("error", "unknown")
    c = b.get("channel", {})
    if c.get("is_archived"):
        return False, "#%s is ARCHIVED" % c.get("name")
    return True, "#%s (is_member=%s)" % (c.get("name"), c.get("is_member"))


def post(text, channel_id, tok=None, label="", fallback_channel=None,
         raise_on_error=False, timeout=10, dedup=True, dedup_key=None):
    """Public API contract; production-derived narrative omitted."""
    tag = ("[%s] " % label) if label else ""
    if not channel_id:
        msg = "%sSLACK POST FAILED: no channel resolved" % tag
        print(msg, file=sys.stderr)
        if raise_on_error:
            raise RuntimeError(msg)
        return False

    send_text = text
    if dedup:
        try:
            from exitmgr import alert_throttle
            go, held, note = alert_throttle.consider(text, channel_id, key=dedup_key)
            if not go:
                print("%sthrottled (duplicate condition, %d held) -- backing off"
                      % (tag, held), file=sys.stderr)
                return True
            send_text = text + note
        except Exception as exc:

            print("%sthrottle unavailable (%s) -- sending unthrottled" % (tag, exc),
                  file=sys.stderr)

    tok = tok or token()
    body = api("chat.postMessage", {"channel": channel_id, "text": send_text}, tok=tok,
               timeout=timeout)
    if body.get("ok"):
        return True

    err = body.get("error", "unknown")
    msg = ("%sSLACK POST FAILED channel=%s error=%s -- THIS ALERT REACHED NOBODY"
           % (tag, channel_id, err))
    print(msg, file=sys.stderr)

    if fallback_channel and fallback_channel != channel_id:
        fb = api("chat.postMessage",
                 {"channel": fallback_channel,
                  "text": (":warning: (re-routed; primary channel %s failed: %s)\n%s"
                           % (channel_id, err, send_text))}, tok=tok, timeout=timeout)
        if fb.get("ok"):
            print("%sre-routed to fallback channel %s" % (tag, fallback_channel),
                  file=sys.stderr)
            return True
        print("%sFALLBACK POST ALSO FAILED channel=%s error=%s"
              % (tag, fallback_channel, fb.get("error", "unknown")), file=sys.stderr)

    if raise_on_error:
        raise RuntimeError(msg)
    return False
