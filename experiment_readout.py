#!/usr/bin/env python3
"""Public API contract; production-derived narrative omitted."""
from __future__ import annotations
import json, os, math, statistics, datetime as dt, urllib.request

APP = os.path.expanduser("~/exitmgr-app")
MARKS = os.path.expanduser("~/trade-capture/events.jsonl")
CHANNEL = "ERROR_CHANNEL_PLACEHOLDER"
CONFIG_PATH = os.path.join(APP, "config.yaml")



CEILING_SET_DATE = "2026-08-21"
PROBE_WINDOW_START = "2026-08-12"
PAIRED_TARGET = 28





SHADOW_LEDGER_ENV = "EXITMGR_SHADOW_EXPERIMENT_PATH"
SHADOW_LEDGER_DEFAULT = os.path.expanduser("~/.local/var/exitmgr/shadow-experiment.json")




REMIT_ACTIONS = ("hold", "cut")
MAX_CLOSES = 60
MAX_DAYS = 90
GATE_MIN_EPISODES = 12
GATE_MIN_SYMBOLS = 8
GATE_MIN_ENTRY_WEEKS = 4


def _num(v):
    """Public API contract; production-derived narrative omitted."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _rows(path):
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("{"):
                    try:
                        yield json.loads(line)
                    except Exception:
                        continue
    except FileNotFoundError:
        return


def is_phantom(c):
    px = _num(c.get("exit_price_per_share"))
    return (c.get("realized_pnl_pct") == -100.0 and px == 0.0
            and c.get("reason") != "expired")


def live_combo_limit(path=None):
    """Public API contract; production-derived narrative omitted."""
    try:
        from exitmgr.config import Config, ConstructionConfig
    except Exception as exc:
        return (None, None, "cannot import exitmgr.config (%s: %s)" % (type(exc).__name__, exc))
    _f = ConstructionConfig.__dataclass_fields__.get("max_combo_spread_pct")
    baseline = _num(getattr(_f, "default", None))
    try:
        cfg = Config.from_yaml(path or CONFIG_PATH)
    except Exception as exc:
        return (None, baseline, "%s: %s" % (type(exc).__name__, exc))
    limit = _num(getattr(getattr(cfg, "construction", None), "max_combo_spread_pct", None))
    if limit is None:
        return (None, baseline, "construction.max_combo_spread_pct is absent or not a number")
    return (limit, baseline, None)


def _entries():
    return [r for r in _rows(os.path.join(APP, "trades.log"))
            if r.get("debit") is not None and r.get("event") is None]


def _spreads(rs):
    return [s for s in (_num(r.get("entry_spread_pct")) for r in rs) if s is not None]


def _mean_pct(xs):
    return "%.1f%%" % statistics.mean(xs) if xs else "n/a"


def spread_experiment():
    """Public API contract; production-derived narrative omitted."""
    limit, baseline, err = live_combo_limit()
    if limit is None:
        return ["*Spread gate* — `max_combo_spread_pct`: *cannot read the live ceiling* — %s"
                % err,
                "  → reporting nothing. A threshold this post grades live entries against is "
                "read from the config or not used at all."]

    after = [r for r in _entries() if str(r.get("ts"))[:10] > CEILING_SET_DATE]
    sa = _spreads(after)
    probing = baseline is not None and abs(limit - baseline) >= 1e-9

    if probing:
        out = ["*Spread gate* — `max_combo_spread_pct` = %.1f%% (live config; the code ships "
               "%.1f%%) — a probe IS in force" % (limit, baseline)]
        before = [r for r in _entries()
                  if PROBE_WINDOW_START <= str(r.get("ts"))[:10] <= CEILING_SET_DATE]
        sb = _spreads(before)
        out.append("  entries %s..%s: %d (mean spread %s)"
                   % (PROBE_WINDOW_START, CEILING_SET_DATE, len(before), _mean_pct(sb)))
    elif baseline is None:
        out = ["*Spread gate* — `max_combo_spread_pct` = %.1f%% (live config)" % limit,
               "  → the shipped default is unreadable, so this cannot say whether a probe is "
               "running. Ceiling integrity only."]
    else:
        out = ["*Spread gate* — `max_combo_spread_pct` = %.1f%% (live config; the shipped "
               "default)" % limit,
               "  → *no experiment running.* The 45 → 35 probe of %s was reverted the same "
               "evening (2c2c433 — n=13 over 5 days, r=-0.29, split point chosen in-sample), "
               "so there is no before/after to compare and nothing to say about fill rate."
               % CEILING_SET_DATE]

    out.append("  entries since %s: %d (mean spread %s)"
               % (CEILING_SET_DATE, len(after), _mean_pct(sa)))
    if probing and len(after) < 5:
        out.append("  → *too early.* Need ~5 entries under the new ceiling to compare fill "
                   "rate; have %d." % len(after))




    over = [s for s in sa if s > limit + 1e-9]
    out.append("  above the %.1f%% ceiling in force: %d%s"
               % (limit, len(over),
                  "" if not over else "  ← *the gate leaks* (worst %.1f%%)" % max(over)))
    return out


def _ledger_path():
    return os.environ.get(SHADOW_LEDGER_ENV) or SHADOW_LEDGER_DEFAULT


def _load_ledger(path):
    """Public API contract; production-derived narrative omitted."""
    try:
        with open(path) as fh:
            led = json.load(fh)
        return led if isinstance(led, dict) else {}
    except Exception:
        return {}


def completed_closes(started_at=None):
    """Public API contract; production-derived narrative omitted."""
    seen = set()
    for r in _rows(os.path.join(APP, "exits.log")):
        if r.get("realized_pnl") is None or is_phantom(r):
            continue
        ts = str(r.get("close_ts") or r.get("ts") or "")
        if started_at and ts and ts < str(started_at):
            continue


        cid = next((r[k] for k in ("con_id", "contract_id", "conId")
                    if r.get(k) is not None), None)
        try:
            seen.add(int(cid))
        except (TypeError, ValueError):
            continue
    return len(seen)


def _elapsed_days(started_at):
    try:
        st = dt.datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
    except Exception:
        return None
    now = dt.datetime.now(st.tzinfo) if st.tzinfo else dt.datetime.now()
    return round((now - st).total_seconds() / 86400.0, 2)


def paired_delta():
    """Public API contract; production-derived narrative omitted."""
    dec = {}
    for r in _rows(MARKS):
        if r.get("event_type") != "position_path":
            continue
        cid = r.get("con_id")
        if cid in (101, 102) or cid is None:
            continue
        dec.setdefault(cid, []).append(r)
    for cid in dec:
        dec[cid].sort(key=lambda x: str(x.get("ts")))
    closes = {}
    for r in _rows(os.path.join(APP, "exits.log")):
        if r.get("realized_pnl") is None or is_phantom(r):
            continue
        cid = next((r[k] for k in ("con_id", "contract_id") if r.get(k) is not None), None)
        if cid is not None:
            closes[int(cid)] = r
    deltas = []
    for cid, rows in dec.items():
        if cid not in closes:
            continue
        actual = _num(closes[cid].get("realized_pnl_pct"))
        if actual is None:
            continue
        sig = next((x for x in rows if x.get("mgmt_action") == "cut"), None)
        if sig is None:
            deltas.append(0.0)
            continue
        m = _num(sig.get("pnl_pct"))
        if m is None:
            continue
        deltas.append(m - actual)
    n = len(deltas)
    if n < 3:
        return (n, None, None, None)
    MUTX = statistics.mean(deltas)
    se = statistics.pstdev(deltas) / math.sqrt(n)
    return (n, MUTX, MUTX - 1.96 * se, MUTX + 1.96 * se)


def refused_marks():
    """Public API contract; production-derived narrative omitted."""
    counts = {}
    for r in _rows(MARKS):
        if r.get("event_type") != "position_path":
            continue
        if r.get("mgmt_action") != "out_of_remit":
            continue
        v = str(r.get("mgmt_proposed_action") or "?")
        counts[v] = counts.get(v, 0) + 1
    return counts


def shadow_experiment():
    path = _ledger_path()
    led = _load_ledger(path)
    out = ["*Exit assessor* — shadow experiment, remit = %s (a thesis-break cut is the only"
           " verb with authority)" % "/".join(REMIT_ACTIONS)]
    out.append("  ledger: %s" % path)

    started = led.get("started_at")
    if not started:
        out.append("  → *clock not started.* No ledger yet — the live loop stamps started_at on "
                   "its first shadow cycle. Nothing to report and nothing is wrong.")
        return out

    eps = led.get("episodes") or {}
    n_eps = len(eps)
    symbols = len({e.get("symbol") for e in eps.values() if isinstance(e, dict) and e.get("symbol")})
    weeks = len({e.get("entry_week") for e in eps.values()
                 if isinstance(e, dict) and e.get("entry_week")})
    days = _elapsed_days(started)
    closes = completed_closes(started)

    out.append("  started %s" % started)
    out.append("  elapsed %s / %d days" % ("n/a" if days is None else "%.1f" % days, MAX_DAYS))
    out.append("  completed closes %d / %d   (campaigns, deduped by contract, phantoms excluded)"
               % (closes, MAX_CLOSES))
    out.append("  binding divergence episodes %d   (model said cut AND no rule fired that cycle; "
               "ONE per campaign — repeated 30s marks are not samples)" % n_eps)
    out.append("    across %d symbols / %d entry weeks   (gate needs >=%d episodes, >=%d symbols, "
               ">=%d entry weeks)"
               % (symbols, weeks, GATE_MIN_EPISODES, GATE_MIN_SYMBOLS, GATE_MIN_ENTRY_WEEKS))

    refused = refused_marks()
    if refused:
        out.append("  out-of-remit proposals still arriving (marks, not episodes): %s"
                   % ", ".join("%s %d" % (k, v) for k, v in sorted(refused.items(),
                                                                  key=lambda kv: -kv[1])))

    n, MUTX, lo, hi = paired_delta()
    if MUTX is None:
        out.append("  paired delta: %d / %d pairs — *not enough data* (informational only; the "
                   "gate is settled out of process)" % (n, PAIRED_TARGET))
    else:
        out.append("  paired delta %+.2f pp  95%% CI %+.2f..%+.2f  (n=%d / %d; informational "
                   "only — a live loop never grades its own experiment)"
                   % (MUTX, lo, hi, n, PAIRED_TARGET))


    if led.get("retired_at"):
        out.append("  → *RETIRED* %s — %s" % (led.get("retired_at"),
                                              led.get("retired_reason") or "reason not recorded"))
        return out
    if led.get("gate_passed"):
        out.append("  → gate recorded as PASSED by a human; the cap no longer retires it.")
        return out
    expired = closes >= MAX_CLOSES or (days is not None and days >= MAX_DAYS)
    if expired:
        verdict = ("operationally redundant (%d < %d episodes)" % (n_eps, GATE_MIN_EPISODES)
                   if n_eps < GATE_MIN_EPISODES else "gate review required")
        out.append("  → *CAP REACHED* — %s. It retires on the next cycle unless a human has "
                   "recorded a gate pass. Preserve the ledger; do not extend the cap." % verdict)
    else:
        out.append("  → running. Retires at the EARLIER of %d closes (%d to go) or %d days (%s to "
                   "go)." % (MAX_CLOSES, max(0, MAX_CLOSES - closes), MAX_DAYS,
                             "n/a" if days is None else "%.0f" % max(0.0, MAX_DAYS - days)))
    return out


def post(text):
    tok = os.environ.get("SLACK_BOT_TOKEN", "")
    if not tok:
        env = os.path.expanduser("~/.hermes/.env")
        if os.path.exists(env):
            for line in open(env):
                if line.strip().startswith("SLACK_BOT_TOKEN="):
                    tok = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not tok:
        print("no SLACK_BOT_TOKEN; printing only")
        return False
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=json.dumps({"channel": CHANNEL, "text": text, "unfurl_links": False}).encode(),
        headers={"Content-Type": "application/json; charset=utf-8",
                 "Authorization": "Bearer %s" % tok})
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=30).read().decode())
        return bool(r.get("ok"))
    except Exception as exc:
        print("slack post failed: %s" % exc)
        return False


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    lines = ["*Trading experiment readout* — %s" % dt.date.today().isoformat(), ""]
    lines += spread_experiment() + [""] + shadow_experiment()
    text = "\n".join(lines)
    print(text)
    if not a.dry_run:
        print("posted" if post(text) else "NOT posted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
