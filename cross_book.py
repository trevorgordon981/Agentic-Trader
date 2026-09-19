#!/usr/bin/env python3
"""Public API contract; production-derived narrative omitted."""
from __future__ import annotations
import argparse, collections, csv, glob, hashlib, io, json, os, re, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from exitmgr import risk




APP = os.path.expanduser("~/exitmgr-app")
DEFAULT_GLOB = os.path.expanduser("~/Downloads/Accounts_History*.csv")




DEFAULT_CURATED = os.path.join(APP, "data", "fidelity_gold_trades.jsonl")


CURATED_OPEN = "open"


_MEASURE = {
    "curated_open_positions":
        "debit still committed on open positions in data/fidelity_gold_trades.jsonl, net of "
        "pro-rata partial closes; see cross_book.held_debit",
    "export_net_premium":
        "FALLBACK, NOT A POSITION COUNT: net premium paid out over one export window. Errs in "
        "BOTH directions -- see cross_book.snapshot_payload",
}
DATE = re.compile(r"^\d{2}/\d{2}/\d{4}$")


DOWNLOADED = re.compile(r"Date downloaded\s+(\d{2}/\d{2}/\d{4})\s+(\d{1,2}:\d{2})\s*([ap]m)", re.I)
TICK = re.compile(r"\(([A-Z]{1,6})\)")



def _first_present(mapping, *keys):
    """Public API contract; production-derived narrative omitted."""
    for k in keys:
        v = mapping.get(k)
        if v is not None:
            return v
    return None


def newest_export(pattern=DEFAULT_GLOB):
    """Public API contract; production-derived narrative omitted."""
    files = sorted(glob.glob(pattern), key=os.path.getmtime)
    return files[-1] if files else None


def _eod_epoch(mmddyyyy):
    """Public API contract; production-derived narrative omitted."""
    try:
        m, d, y = (int(x) for x in str(mmddyyyy).split("/"))
    except (TypeError, ValueError):
        return None
    try:
        import datetime
        return datetime.datetime(y, m, d, 23, 59, 59).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def downloaded_at(path):
    """Public API contract; production-derived narrative omitted."""
    try:
        tail = open(path, encoding="utf-8-sig", errors="replace").read()[-4000:]
    except OSError:
        return os.path.getmtime(path)
    m = DOWNLOADED.search(tail)
    if not m:
        return os.path.getmtime(path)
    try:
        import datetime
        hh, mm = (int(x) for x in m.group(2).split(":"))
        hh = hh % 12 + (12 if m.group(3).lower() == "pm" else 0)
        mo, d, y = (int(x) for x in m.group(1).split("/"))
        return datetime.datetime(y, mo, d, hh, mm).timestamp()
    except (ValueError, OverflowError, OSError):
        return os.path.getmtime(path)


def export_window(path):
    """Public API contract; production-derived narrative omitted."""
    _, d0, d1 = fidelity_by_underlying(path)
    return d0, d1


def best_export(pattern=DEFAULT_GLOB):
    """Public API contract; production-derived narrative omitted."""
    best, key = None, None
    for f in sorted(glob.glob(pattern)):
        try:
            _, _, d1 = fidelity_by_underlying(f)
        except Exception:
            continue
        end = _eod_epoch(d1)
        if end is None:
            continue
        k = (end, downloaded_at(f))
        if key is None or k > key:
            best, key = f, k
    return best


def fidelity_by_underlying(path):
    """Public API contract; production-derived narrative omitted."""
    lines = open(path, encoding="utf-8-sig").read().splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.startswith("Run Date"))
    except StopIteration:
        return {}, None, None
    rdr = csv.DictReader(io.StringIO("\n".join(lines[start:])))
    rows = [r for r in rdr if DATE.match((r.get("Run Date") or "").strip())]
    if not rows:
        return {}, None, None
    out = collections.defaultdict(lambda: {"net_premium": 0.0, "opens": 0, "closes": 0})
    for r in rows:
        act = re.sub(r"\s+", " ", r.get("Action") or "")
        if "OPTION" not in act.upper() and "CALL (" not in act and "PUT (" not in act:
            continue
        m = TICK.search(act)
        if not m:
            continue
        u = m.group(1)
        try:
            amt = float((r.get("Amount") or "0").replace(",", "").strip('"') or 0)
        except ValueError:
            amt = 0.0
        out[u]["net_premium"] += amt
        if "OPENING" in act.upper():
            out[u]["opens"] += 1
        elif "CLOSING" in act.upper():
            out[u]["closes"] += 1
    ds = sorted(r["Run Date"] for r in rows)
    return dict(out), ds[0], ds[-1]


def ibkr_by_underlying():
    """Public API contract; production-derived narrative omitted."""
    closed = set()
    try:
        for line in open(os.path.join(APP, "exits.log")):
            if line.strip().startswith("{"):
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                cid = _first_present(r, "con_id", "contract_id")
                if cid is not None:
                    closed.add(int(cid))
    except FileNotFoundError:
        pass
    out = collections.defaultdict(float)
    try:
        for line in open(os.path.join(APP, "trades.log")):
            if not line.strip().startswith("{"):
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            cid, deb = r.get("contract_id"), r.get("debit")



            if cid is None or deb is None or r.get("event"):
                continue
            if int(cid) in closed:
                continue
            out[r.get("symbol") or "?"] += float(deb)
    except FileNotFoundError:
        pass
    return dict(out)




def _finite(value):
    """Public API contract; production-derived narrative omitted."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


def _iso_eod_epoch(iso_date):
    """Public API contract; production-derived narrative omitted."""
    try:
        y, m, d = (int(x) for x in str(iso_date).split("-"))
    except (TypeError, ValueError):
        return None
    try:
        import datetime
        return datetime.datetime(y, m, d, 23, 59, 59).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def held_debit(row):
    """Public API contract; production-derived narrative omitted."""
    cb = _finite(row.get("cost_basis"))
    if cb is None:
        raise ValueError("curated row for %r has no usable cost_basis: %r"
                         % (row.get("ticker"), row.get("cost_basis")))
    proceeds = _finite(row.get("proceeds"))
    if proceeds is None:
        proceeds = 0.0
    realized = _finite(row.get("realized_pnl"))



    closed_cost_basis = 0.0 if realized is None else realized - proceeds
    held = -(cb - closed_cost_basis)




    if abs(held) > abs(cb) + 0.01:
        raise ValueError(
            "curated row for %r releases more basis than it opened with "
            "(cost_basis %.2f, proceeds %.2f, realized_pnl %s -> held %.2f)"
            % (row.get("ticker"), cb, proceeds, row.get("realized_pnl"), held))
    return held


def curated_open_positions(curated_path, *, as_of=None):
    """Public API contract; production-derived narrative omitted."""
    p = os.path.expanduser(str(curated_path))
    rows = []
    with open(p, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except ValueError as exc:
                raise ValueError("%s line %d is not JSON: %s" % (p, n, exc))
            if not isinstance(obj, dict):
                raise ValueError("%s line %d is not a JSON object" % (p, n))
            rows.append(obj)
    if not rows:
        raise ValueError("%s has no rows; refusing to mint a snapshot from an empty book" % p)

    cutoff = _finite(as_of)
    if cutoff is None:
        cutoff = time.time()

    positions, unpriced, lapsed, accounts = {}, set(), [], {}
    coverage_end = None
    n_open = 0
    for r in rows:
        for k in ("entry_date", "exit_date"):
            e = _iso_eod_epoch(r.get(k))
            if e is not None and (coverage_end is None or e > coverage_end):
                coverage_end = e
        acct = next((str(r[k]).strip() for k in ("acct_no", "account_number")
                     if r.get(k) is not None), "")
        if acct:
            label = next((str(r[k]) for k in ("account",) if r.get(k) is not None), "?")
            accounts.setdefault(acct, label)
        close_type = next((str(r[k]) for k in ("close_type",) if r.get(k) is not None), "")
        if close_type.strip().lower() != CURATED_OPEN:
            continue
        n_open += 1
        u = str(next((r[k] for k in ("ticker", "symbol", "underlying")
                      if r.get(k) is not None), "")).strip().upper()
        if not u:
            raise ValueError("curated open row has no ticker: %r" % (r,))
        exp = _iso_eod_epoch(r.get("expiry"))
        if exp is not None and exp < cutoff:
            lapsed.append("%s %s %s" % (u, r.get("right"), r.get("expiry")))
            continue
        held = held_debit(r)
        if held > 0:
            positions[u] = round(positions.get(u, 0.0) + held, 2)
        elif held < 0:
            unpriced.add(u)


    positions = {u: v for u, v in positions.items() if v > 0}
    meta = {
        "curated_rows": len(rows),
        "curated_open_rows": n_open,
        "curated_sha256": _sha256_file(p),
        "curated_bytes": int(os.path.getsize(p)),


        "curated_coverage_end": coverage_end,
        "lapsed_open_rows": sorted(lapsed),
        "accounts": sorted("%s (%s)" % (a, lbl) for a, lbl in accounts.items()),
    }
    return positions, sorted(unpriced), meta


def declared_curated_accounts():
    """Public API contract; production-derived narrative omitted."""
    try:
        import fidelity_curate
        declared = getattr(fidelity_curate, "CURATED_ACCOUNTS", None)
        return None if declared is None else sorted(str(a) for a in declared)
    except Exception:
        return None


DEFAULT_SNAPSHOT = os.path.join(APP, "data", "external_book_snapshot.json")
ALERT_STATE = os.path.join(APP, ".external_book_freshness_state.json")


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_alert_state(path=ALERT_STATE):
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as fh:
            value = json.load(fh)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _write_alert_state(status, book, path=ALERT_STATE):
    """Public API contract; production-derived narrative omitted."""
    target = os.path.expanduser(path)
    os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
    tmp = "%s.%d.tmp" % (target, os.getpid())
    payload = {
        "status": status,
        "state": book.state,
        "age_s": book.age_s,
        "max_age_s": book.max_age_s,
        "error": book.error,
        "updated_at": time.time(),
    }
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, target)


def notify_external_book(book, *, state_path=ALERT_STATE, poster=None, host=None):
    """Public API contract; production-derived narrative omitted."""
    status = "OK" if book.readable else str(book.state or "UNKNOWN").upper()
    previous = str(_read_alert_state(state_path).get("status") or "")
    transition = status != previous
    should_post = transition and (not book.readable or (previous and previous != "OK"))
    if should_post:
        host = host or __import__("socket").gethostname().split(".")[0]
        if book.readable:
            message = (
                ":white_check_mark: *External Fidelity book is current again* (%s)\n"
                "The cross-book same-name gate can verify exposure again." % host
            )
        else:
            age = "unknown" if book.age_s is None else "%.1fh" % (float(book.age_s) / 3600.0)
            bound = (
                "unknown" if book.max_age_s is None
                else "%.1fh" % (float(book.max_age_s) / 3600.0)
            )
            message = (
                ":rotating_light: *External Fidelity book is %s* (%s)\n"
                "Age %s; maximum %s. The cross-book gate is correctly refusing same-name "
                "additions until a fresh Fidelity export arrives.\n"
                "Reason: %s\n"
                "Fix: download a fresh Accounts_History CSV; the sync and snapshot jobs run "
                "automatically." % (status, host, age, bound, book.error or status)
            )
        if poster is None:
            from exitmgr import alerting
            poster = lambda text: alerting.post(
                text, alerting.alerts_channel(), label="external_book_freshness",
                fallback_channel=alerting.error_channel(), dedup=False)
        if not poster(message):
            return False, True
    if transition:
        try:
            _write_alert_state(status, book, state_path)
        except Exception as exc:
            print("external-book alert state write failed: %s" % exc, file=sys.stderr)
            return False, should_post
    return True, should_post


def snapshot_payload(export_path, *, generated_at=None, curated_path=None):
    """Public API contract; production-derived narrative omitted."""
    fid, d0, d1 = fidelity_by_underlying(export_path)
    window_end = _eod_epoch(d1)
    if window_end is None:



        raise ValueError("%s has no dated transaction rows; refusing to write a snapshot"
                         % os.path.basename(export_path))
    dl = float(downloaded_at(export_path))
    bounds = [dl, float(window_end)]
    curated_meta, source = {}, "export_net_premium"
    if curated_path is None:
        positions, unpriced = {}, []
        for u, v in fid.items():
            committed = -float(v["net_premium"])
            if committed > 0:
                positions[str(u).strip().upper()] = round(committed, 2)
            elif float(v["net_premium"]) > 0:
                unpriced.append(str(u).strip().upper())
    else:
        source = "curated_open_positions"


        positions, unpriced, curated_meta = curated_open_positions(
            curated_path, as_of=min(bounds))
        coverage_end = curated_meta.get("curated_coverage_end")
        if coverage_end is None:
            raise ValueError("%s carries no dated rows; refusing to mint a snapshot from a book "
                             "whose coverage cannot be bounded" % curated_path)


        bounds.append(float(coverage_end))
    payload = {
        "schema": risk.EXTERNAL_BOOK_SCHEMA,


















        "as_of": float(min(bounds)),
        "downloaded_at": dl,
        "window_end_epoch": float(window_end),
        "generated_at": float(time.time() if generated_at is None else generated_at),
        "origin": os.path.basename(export_path),
        "origin_sha256": _sha256_file(export_path),
        "origin_bytes": int(os.path.getsize(export_path)),
        "window_start": d0,
        "window_end": d1,
        "positions": positions,
        "unpriced": sorted(unpriced),


        "positions_source": source,
        "measure": _MEASURE[source],
    }
    if curated_path is not None:
        payload["curated_origin"] = os.path.basename(os.path.expanduser(str(curated_path)))
        payload["curated_sha256"] = curated_meta["curated_sha256"]
        payload["curated_bytes"] = curated_meta["curated_bytes"]
        payload["curated_rows"] = curated_meta["curated_rows"]
        payload["curated_open_rows"] = curated_meta["curated_open_rows"]
        payload["curated_coverage_end"] = float(curated_meta["curated_coverage_end"])


        payload["curated_mtime"] = float(os.path.getmtime(os.path.expanduser(str(curated_path))))
        payload["lapsed_open_rows"] = curated_meta["lapsed_open_rows"]









        payload["accounts"] = curated_meta["accounts"]
        payload["accounts_declared"] = declared_curated_accounts()
    payload["digest"] = risk.external_book_digest(payload)
    return payload


def write_snapshot(path, payload):
    """Public API contract; production-derived narrative omitted."""
    path = os.path.expanduser(str(path))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w") as fh:
        json.dump(payload, fh, sort_keys=True, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", default=None)
    ap.add_argument("--write-snapshot", nargs="?", const=DEFAULT_SNAPSHOT, default=None,
                    metavar="PATH",
                    help="write the signed snapshot the risk gate reads (default: %s)"
                         % DEFAULT_SNAPSHOT)
    ap.add_argument("--check", nargs="?", const=DEFAULT_SNAPSHOT, default=None, metavar="PATH",
                    help="report the state the risk gate would see for a snapshot, and exit "
                         "non-zero unless it is OK")
    ap.add_argument("--notify", action="store_true",
                    help="edge-trigger Slack on check failure/recovery; failed delivery retries")
    ap.add_argument("--config", default=os.path.join(APP, "config.yaml"),
                    help="typed config supplying the gate's snapshot path and max age")
    ap.add_argument("--curated", default=DEFAULT_CURATED, metavar="PATH",
                    help="curated manual book the snapshot's POSITIONS come from "
                         "(default: %(default)s)")
    ap.add_argument("--no-curated", action="store_true",
                    help="build positions from the export's net premium instead. NOT FOR "
                         "PRODUCTION: that measure counts closed round-trips as open exposure "
                         "and missed 10 of 13 names on 2026-08-22")
    a = ap.parse_args()
    curated = None if a.no_curated else a.curated

    if a.check is not None:
        check_path, max_age = a.check, None
        try:
            from exitmgr.config import load_config
            from exitmgr.entry_safety import risk_limits_from_config
            limits = risk_limits_from_config(load_config(a.config))
            if check_path == DEFAULT_SNAPSHOT:
                check_path = limits.external_book_path or check_path
            max_age = limits.external_book_max_age_s
        except Exception as exc:
            print("external-book config validation FAILED: %s" % exc, file=sys.stderr)
            return 1
        book = risk.load_external_book(check_path, max_age_s=max_age)
        d = book.describe()
        print("external-book snapshot: %s" % check_path)
        for k in ("state", "readable", "age_s", "max_age_s", "n_positions", "origin", "digest",
                  "error"):
            print("  %-14s %s" % (k, d.get(k)))
        notice_ok = True
        if a.notify:
            notice_ok, posted = notify_external_book(book)
            print("  notification %s" % ("posted" if posted else "unchanged"))

        return 0 if book.readable and notice_ok else 1

    path = a.export or best_export()
    if not path:
        print("no Fidelity export found under ~/Downloads/Accounts_History*.csv")
        return 2

    if a.write_snapshot is not None:
        try:
            payload = snapshot_payload(path, curated_path=curated)
        except (OSError, ValueError) as exc:




            print("REFUSING to write a snapshot: %s" % exc)
            print("  previous snapshot left untouched -- it will age into STALE rather than be "
                  "replaced by a wrong one")
            return 3
        out = write_snapshot(a.write_snapshot, payload)
        print("wrote %s" % out)
        print("  origin      %s (%s .. %s)" % (payload["origin"], payload["window_start"],
                                               payload["window_end"]))
        print("  positions   from %s" % payload["positions_source"])
        if payload.get("curated_origin"):
            print("  curated     %s (%d rows, %d open, %d lapsed)"
                  % (payload["curated_origin"], payload["curated_rows"],
                     payload["curated_open_rows"], len(payload["lapsed_open_rows"])))
            print("  accounts    %s" % ", ".join(payload["accounts"]))

        print("  as_of       %s  (earliest of download %s / window end %s%s)"
              % (time.strftime("%Y-%m-%d %H:%M", time.localtime(payload["as_of"])),
                 time.strftime("%m-%d %H:%M", time.localtime(payload["downloaded_at"])),
                 time.strftime("%m-%d %H:%M", time.localtime(payload["window_end_epoch"])),
                 ("" if payload.get("curated_coverage_end") is None else
                  " / curated coverage %s" % time.strftime(
                      "%m-%d %H:%M", time.localtime(payload["curated_coverage_end"])))))
        print("  positions   %d  unpriced %d" % (len(payload["positions"]),
                                                 len(payload["unpriced"])))
        print("  digest      %s" % payload["digest"])
        print("  gate sees   %s" % risk.load_external_book(out).state)
        return 0
    fid, d0, d1 = fidelity_by_underlying(path)
    ib = ibkr_by_underlying()
    print("Cross-book exposure")
    print("  Fidelity export : %s  (%s .. %s)" % (os.path.basename(path), d0, d1))
    print("  chosen by WINDOW END, not download time -- see newest_export()'s docstring.")
    print("  NOTE: Fidelity figures are NET PREMIUM over that window, not a live position count.")
    print("        Anything opened before the window is invisible here.")
    print()
    both = sorted(set(fid) & set(ib))
    if both:
        print("  ** HELD IN BOTH BOOKS **")
        print("  %-7s %14s %16s %10s" % ("sym", "IBKR debit", "Fidelity net", "ratio"))
        for u in both:
            i = ib[u]; f = abs(fid[u]["net_premium"])
            ratio = ("%.1fx" % (f / i)) if i else "n/a"
            print("  %-7s %14.2f %16.2f %10s" % (u, i, -abs(f), ratio))
        print()
        print("  Alfred's concentration caps read the IBKR column ONLY.")
    else:
        print("  no underlying appears in both books over this window.")
    print()
    print("  IBKR-only  : %s" % (", ".join(sorted(set(ib) - set(fid))) or "none"))
    top = sorted(fid.items(), key=lambda kv: -abs(kv[1]["net_premium"]))[:8]
    print("  Fidelity top: %s" % ", ".join("%s %.0f" % (k, v["net_premium"]) for k, v in top))


    if curated is not None:
        print()
        try:
            held, unpriced, meta = curated_open_positions(curated)
        except (OSError, ValueError) as exc:
            print("  STILL OPEN (curated): unavailable -- %s" % exc)
        else:
            print("  ** STILL OPEN, per %s -- these are POSITIONS, not flow **"
                  % os.path.basename(curated))
            for u, v in sorted(held.items(), key=lambda kv: -kv[1]):
                print("  %-7s %14.2f%s" % (u, v, "   (also has an open short leg)"
                                           if u in unpriced else ""))
            print("  short-premium / unvalued: %s" % (", ".join(unpriced) or "none"))
            print("  accounts: %s" % ", ".join(meta["accounts"]))
            ghosts = sorted(set(fid) - set(held) - set(unpriced))
            print("  in the window but NOT open: %s" % (", ".join(ghosts) or "none"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
