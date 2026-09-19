"""Public API contract; production-derived narrative omitted."""
import json, os, sys
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
COHORT_START = "2037-08-18"


DOCTRINE_MIN_COMPLIANCE_PCT = 95.0












ENTRY_JOURNAL = "trades.log"


def load(path):
    rows = []
    if not os.path.exists(path):
        return rows
    for line in open(path):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    return rows


def load_with_status(path):
    """Public API contract; production-derived narrative omitted."""
    if not os.path.exists(path):
        return [], "%s is missing" % os.path.basename(path)
    rows = []
    try:
        with open(path) as fh:
            for line_no, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError as exc:
                    return rows, "%s line %d is not complete JSON" % (
                        os.path.basename(path), line_no)
                if not isinstance(row, dict):
                    return rows, "%s line %d is not a JSON object" % (
                        os.path.basename(path), line_no)
                rows.append(row)
    except OSError as exc:
        return rows, "%s is unreadable (%s)" % (os.path.basename(path), exc)
    return rows, None


def _int_or_none(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None














































_POLICY_HISTORY = [
    (
        "0000-01-01T00:00:00+00:00",
        "conviction slide (<=6 -> 8x, 7 -> 6x, 8 -> 5x, 9/10 -> 4x)",


        lambda conviction: (8 if conviction <= 6 else
                            6 if conviction == 7 else
                            5 if conviction == 8 else 4),
        8,
    ),
    (


        "2037-08-22T03:54:05+00:00",
        "flat 8x for every conviction",
        lambda conviction: 8,
        8,
    ),
]



_HISTORY_CURRENT_MULTIPLE = _POLICY_HISTORY[-1][3]


def _policy_era(ts):
    """Public API contract; production-derived narrative omitted."""
    stamp = str(ts or "")
    era, index = _POLICY_HISTORY[0], 0
    for i, entry in enumerate(_POLICY_HISTORY):
        if stamp >= entry[0]:
            era, index = entry, i
    return era[2], era[3], (index == len(_POLICY_HISTORY) - 1)


def required_floor_dte(row, live_multiple):
    """Public API contract; production-derived narrative omitted."""
    dte = _int_or_none(row.get("dte_at_entry"))
    if dte is None:
        return None, "no_hold"

    stamped_floor = _int_or_none(row.get("doctrine_dte_floor"))
    if stamped_floor is not None and stamped_floor > 0:
        return stamped_floor, "stamp"

    hold = _int_or_none(row.get("intended_hold_days"))
    if hold is None or hold <= 0:
        return None, "no_hold"

    stamped_multiple = _int_or_none(row.get("doctrine_hold_multiple"))
    if stamped_multiple is not None and stamped_multiple > 0:
        return hold * stamped_multiple, "stamp"

    multiple_fn, strictest, is_current = _policy_era(row.get("ts"))
    if is_current and live_multiple != _HISTORY_CURRENT_MULTIPLE:





        return None, "unknown_policy"
    conviction = _int_or_none(row.get("conviction"))
    if conviction is None:
        return hold * int(strictest), "reconstructed_strict"
    multiple = multiple_fn(conviction)
    if not multiple or multiple <= 0:
        return None, "unknown_policy"
    return hold * multiple, "reconstructed"


def doctrine_compliance(journal_path, cohort_start):
    """Public API contract; production-derived narrative omitted."""
    try:
        from exitmgr.entry_builder import DEBIT_HOLD_FLOOR_MULTIPLE as multiple
    except Exception as exc:
        return None, "UNEVALUATED", "doctrine multiple unreadable (%s)" % exc
    multiple = _int_or_none(multiple)
    if not multiple or multiple <= 0:
        return None, "UNEVALUATED", "doctrine multiple is not a positive integer"
    if not os.path.exists(journal_path):
        return None, "UNEVALUATED", "entry journal %s not found" % os.path.basename(journal_path)

    journal_rows, journal_error = load_with_status(journal_path)
    if journal_error:
        return None, "UNEVALUATED", journal_error
    entries = [r for r in journal_rows
               if r.get("dte_at_entry") is not None
               and str(r.get("ts") or "")[:10] >= cohort_start]
    if not entries:
        return None, "UNEVALUATED", (
            "no entries on/after %s carry dte_at_entry in %s"
            % (cohort_start, os.path.basename(journal_path)))

    compliant, short_dte, unmeasurable, unknown_policy = 0, [], [], []
    stamped, reconstructed, strict = 0, 0, 0
    for r in entries:
        tag = "%s %s" % (r.get("symbol") or "?", str(r.get("ts") or "")[:10])
        dte = _int_or_none(r.get("dte_at_entry"))
        floor, basis = required_floor_dte(r, multiple)
        if basis == "no_hold":
            unmeasurable.append(tag)
            continue
        if basis == "unknown_policy" or floor is None:
            unknown_policy.append(tag)
            continue
        if basis == "stamp":
            stamped += 1
        else:
            reconstructed += 1
            if basis == "reconstructed_strict":
                strict += 1
        if dte is not None and dte >= floor:
            compliant += 1
        else:
            short_dte.append("%s %dd/%dd(%s)" % (tag, dte or 0, floor, basis))
    n = len(entries)

    provenance = "%d stamped at entry, %d reconstructed from the policy in force" % (
        stamped, reconstructed)
    if strict:
        provenance += (" of which %d recorded no conviction and were graded at the STRICTEST "
                       "multiple of their era" % strict)
    if unknown_policy:
        detail = ("UNEVALUATED: %d of %d entries were made under a doctrine this file cannot "
                  "name (unstamped, current era, and _POLICY_HISTORY disagrees with the live "
                  "multiple %d): %s. Extend _POLICY_HISTORY, or stamp the entry path."
                  % (len(unknown_policy), n, multiple, ", ".join(unknown_policy)))
        return None, "UNEVALUATED", detail

    pct = 100.0 * compliant / n
    detail = ("%d/%d entries cleared the DTE floor IN FORCE AT THEIR ENTRY (%s; live multiple "
              "is %dx)" % (compliant, n, provenance, multiple))
    if short_dte:
        detail += "; under floor: " + ", ".join(short_dte)
    if unmeasurable:
        detail += ("; no intended_hold_days (counted as NON-compliant): "
                   + ", ".join(unmeasurable))
    return (pct >= DOCTRINE_MIN_COMPLIANCE_PCT), "%.0f%% (%d/%d)" % (pct, compliant, n), detail


def main():
    exit_rows, exits_error = load_with_status(os.path.join(HERE, "exits.log"))
    exits = [r for r in exit_rows
             if (r.get("realized_pnl") is not None
                 or r.get("realized_pnl_net") is not None)
             and str(r.get("entry_ts") or "")[:10] >= COHORT_START]




    from exitmgr.pnl_semantics import ledger_net, net_realized
    pnl_summary = ledger_net(exits)
    canonical_net = None if exits_error else pnl_summary["canonical_total"]
    per_row_net = [(r, net_realized(r)[0]) for r in exits]
    n = len(exits)
    net = canonical_net
    exp = (net / n) if net is not None and n else (0.0 if not n else None)

    expired_rows = [(r, v) for r, v in per_row_net if r.get("reason") == "expired"]
    expired_unknown = bool(exits_error) or any(v is None for _r, v in expired_rows)
    expired = (None if expired_unknown else sum(1 for _r, v in expired_rows if v < 0))
    expired_pct = (None if expired_unknown else
                   (100.0 * expired / n if n else 0.0))


    contribution_rows, contributions_error = load_with_status(
        os.path.expanduser("~/contributions.jsonl"))
    contribs = [r for r in contribution_rows
                if str(r.get("type") or "").lower() == "deposit"
                and str(r.get("date") or "")[:10] >= COHORT_START]
    try:
        added = (None if contributions_error else
                 sum(float(r.get("amount") or 0) for r in contribs))
    except (TypeError, ValueError):
        added = None
        contributions_error = contributions_error or "contributions contain a non-numeric amount"

    audit_rows, audit_error = load_with_status(os.path.join(HERE, "audit.jsonl"))
    net_liq = None
    for r in audit_rows if not audit_error else ():
        if r.get("event") == "cycle_start" and r.get("net_liq"):
            try:
                candidate = float(r["net_liq"])
                if candidate == candidate and candidate > 0:
                    net_liq = candidate
            except (TypeError, ValueError):
                pass
    book_pct = None
    try:
        if net_liq is None:
            raise ValueError(audit_error or "no positive net_liq in audit cycle_start")
        from book_return import book_return
        bk = book_return(net_liq)
        candidate = float(bk.get("pnl_pct"))
        if candidate == candidate and candidate not in (float("inf"), float("-inf")):
            book_pct = candidate
    except Exception:
        book_pct = None




    streak = 0
    streak_known = not bool(exits_error)
    for _r, value in reversed(sorted(per_row_net,
                                     key=lambda x: str(x[0].get("close_ts") or ""))):
        if value is None:
            streak_known = False
            break
        if value > 0:
            break
        streak += 1
    known_pn = [v for _r, v in per_row_net if v is not None]
    worst = min(known_pn) if known_pn else None
    worst_pct = (100.0 * abs(worst) / net_liq
                 if worst is not None and net_liq is not None else None)
    worst_complete = not exits_error and not pnl_summary["rows_unknown"]

    doctrine_ok, doctrine_val, doctrine_detail = doctrine_compliance(
        os.path.join(HERE, ENTRY_JOURNAL), COHORT_START)

    crit = [
        ("1 closed trades >= 30",
         None if exits_error else n >= 30,
         ("UNEVALUATED (%s; %d partial row%s parsed)" %
          (exits_error, n, "" if n == 1 else "s")) if exits_error else "%d" % n),
        ("2 expectancy > $0",
         None if exp is None else (exp > 0 and n > 0),
         ("UNEVALUATED (%d/%d rows have canonical net; known subtotal $%+.2f is NOT a total)"
          % (pnl_summary["rows_known"], n, pnl_summary["known_subtotal"]))
         if exp is None else "$%+.2f/trade" % exp),
        ("3 cohort net realized > $0",
         None if net is None else (net > 0 and n > 0),
         ("UNEVALUATED (%d row%s missing canonical net)"
          % (pnl_summary["rows_unknown"],
             "" if pnl_summary["rows_unknown"] == 1 else "s"))
         if net is None else "$%+.2f" % net),
        ("4 expiry losses <= 10%",
         None if expired_pct is None else expired_pct <= 10.0,
         "UNEVALUATED (an expired row has unknown net P&L)" if expired_pct is None
         else "%.0f%% (%d)" % (expired_pct, expired)),
        ("5 doctrine compliance >= 95%", doctrine_ok,         doctrine_val),
        ("6 no capital added",
         None if added is None else added == 0,
         ("UNEVALUATED (%s)" % contributions_error) if added is None else "$%.2f" % added),
    ]
    halts = [
        ("book P&L <= -35%",
         None if book_pct is None else book_pct <= -35.0,
         "UNEVALUATED" if book_pct is None else "%.1f%%" % book_pct),
        ("10 consecutive losses",        None if not streak_known else streak >= 10,
         "UNEVALUATED" if not streak_known else "%d" % streak),
        ("single loss > 15% of net liq",
         (None if worst_pct is None else
          ((worst_pct > 15.0) if worst_complete or worst_pct > 15.0 else None)),
         ("UNEVALUATED" if worst_pct is None else
          (("UNEVALUATED (known worst %.1f%%; unknown rows remain)" % worst_pct)
           if not worst_complete and worst_pct <= 15.0 else "%.1f%%" % worst_pct))),
    ]

    print("RE-ARM BAR  (cohort: entries on/after %s)" % COHORT_START)
    print("-" * 58)
    for name, ok, val in crit:
        flag = "PASS" if ok is True else ("UNEV" if ok is None else "    ")
        print("  [%s] %-30s %s" % (flag, name, val))
    print("\nHALT TRIGGERS (any true = stop now)")
    for name, fired, val in halts:
        flag = "FIRED" if fired is True else ("UNEV" if fired is None else "  ok")
        print("  [%s] %-30s %s" % (flag, name, val))


    unevaluated = [name for name, ok, _ in crit if ok is None]
    unevaluated_halts = [name for name, fired, _ in halts if fired is None]
    passed = (all(ok is True for _, ok, _ in crit)
              and all(fired is not None for _, fired, _ in halts))
    fired = any(h[1] is True for h in halts)




    print("\n  criterion 5 evidence (%s): %s" % (ENTRY_JOURNAL, doctrine_detail))
    print("  criteria evaluated: %d of %d" % (len(crit) - len(unevaluated), len(crit)))
    if unevaluated:
        print("  !! UNEVALUATED: %s" % "; ".join(unevaluated))
        print("  !! The bar requires ALL SIX. An unevaluated criterion is NOT a pass, so this")
        print("  !! run cannot return RE-ARM ELIGIBLE. Do not redeploy capital on it.")
    if unevaluated_halts:
        print("  !! UNEVALUATED HALT INPUTS: %s" % "; ".join(unevaluated_halts))
        print("  !! Unknown safety-halt evidence blocks RE-ARM ELIGIBLE; it is never read as OK.")

    print("\n  VERDICT: %s" % ("HALT" if fired else ("RE-ARM ELIGIBLE" if passed else "NOT YET")))
    return 2 if fired else (0 if passed else 1)


if __name__ == "__main__":
    sys.exit(main())
