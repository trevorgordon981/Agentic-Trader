#!/usr/bin/env python3
"""Public API contract; production-derived narrative omitted."""
from __future__ import annotations
import argparse, collections, datetime, glob, hashlib, json, math, os, re, sys
import xml.etree.ElementTree as ET
import urllib.request
from zoneinfo import ZoneInfo

APP = os.path.expanduser("~/exitmgr-app")
ARCHIVE = os.path.expanduser("~/flex-archive")
CHANNEL = "ERROR_CHANNEL_PLACEHOLDER"
TOLERANCE = 25.0



LIVE_DATA = ("exits.log", "trades.log", "exitmgr_state.json", "events.jsonl")

_WINDOW_RE = re.compile(rb'fromDate="(\d{8})"\s+toDate="(\d{8})"')



APPLY_NOTE = (
    "NOT applied. An operator applies a broker-sourced proposal with the trader stopped. "
    "The proposal binds quantity and cost basis to broker evidence rather than a trigger mark."
)


def _iso(yyyymmdd: str) -> str:
    return "%s-%s-%s" % (yyyymmdd[:4], yyyymmdd[4:6], yyyymmdd[6:8])


def statement_window(path):
    """Public API contract; production-derived narrative omitted."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(65536)
        m = _WINDOW_RE.search(head)
        if m:
            return _iso(m.group(1).decode()), _iso(m.group(2).decode())
    except OSError:
        return None, None
    try:
        for el in ET.parse(path).getroot().iter():
            fd, td = el.get("fromDate"), el.get("toDate")
            if fd and td:
                return _iso(fd), _iso(td)
    except Exception:
        pass
    return None, None


def newest_statement(archive=None):
    """Public API contract; production-derived narrative omitted."""
    files = sorted(glob.glob(os.path.join(archive or ARCHIVE, "flex-statement-*.xml")))
    if not files:
        return None
    return max(files, key=lambda p: ((statement_window(p)[1] or ""), os.path.basename(p)))


def broker_by_underlying(path):
    """Public API contract; production-derived narrative omitted."""
    out = collections.defaultdict(float)
    seen = collections.defaultdict(int)
    for t in ET.parse(path).getroot().findall(".//Trade"):
        u = t.get("underlyingSymbol") or t.get("symbol") or "?"
        try:
            out[u] += float(t.get("fifoPnlRealized") or 0)
        except ValueError:
            continue
        seen[u] += 1
    return dict(out), dict(seen)


def our_rows(path=None):
    """Public API contract; production-derived narrative omitted."""
    rows = []
    p = path or os.path.join(APP, "exits.log")
    fh = open(p)
    with fh:
        for line_no, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except Exception as exc:
                raise ValueError("%s line %d is not complete JSON" %
                                 (os.path.basename(p), line_no)) from exc
            if not isinstance(r, dict):
                raise ValueError("%s line %d is not a JSON object" %
                                 (os.path.basename(p), line_no))
            if r.get("realized_pnl") is None and r.get("realized_pnl_net") is None:
                continue
            rows.append((str(r.get("close_ts") or r.get("ts") or "")[:10], r))
    return rows


def ours_by_underlying(window_start=None, window_end=None, path=None):
    """Public API contract; production-derived narrative omitted."""
    out = collections.defaultdict(float)
    uncovered = []
    for closed, r in our_rows(path):
        sym = r.get("symbol") or "?"
        raw = r.get("realized_pnl")
        pnl = float(r.get("realized_pnl_net") if raw is None else raw)
        if window_end and closed and closed > window_end:
            uncovered.append((sym, closed, pnl, "closed after the statement window"))
            continue
        if window_start and closed and closed < window_start:
            uncovered.append((sym, closed, pnl, "closed before the statement window"))
            continue
        out[sym] += pnl
    return dict(out), uncovered


def compare(stmt, tolerance=TOLERANCE, exits_path=None):
    """Public API contract; production-derived narrative omitted."""
    bro, counts = broker_by_underlying(stmt)
    win_from, win_to = statement_window(stmt)
    mine, uncovered = ours_by_underlying(win_from, win_to, exits_path)
    rows = []
    for u in sorted(set(list(bro) + list(mine))):
        b, m = bro.get(u), mine.get(u)
        if b is None and m is not None:
            rows.append((u, None, m, None, "no broker rows - we booked a trade the broker never saw"))
        elif m is None:
            continue
        else:
            d = b - m
            if abs(d) > tolerance:
                rows.append((u, b, m, d, "netted across %d leg rows" % counts.get(u, 0)))
    return rows, uncovered, (win_from, win_to), (bro, mine)


def aggregate_reconciliation(broker, local, tolerance=TOLERANCE):
    """Public API contract; production-derived narrative omitted."""
    matched = sorted(set(broker) & set(local))
    broker_total = sum(float(broker[u]) for u in matched)
    local_total = sum(float(local[u]) for u in matched)
    residual = broker_total - local_total
    comparable = bool(matched)
    return {
        "matched_underlyings": len(matched),
        "broker_total": broker_total,
        "local_total": local_total,
        "residual": residual,
        "comparable": comparable,
        "within_tolerance": (abs(residual) <= float(tolerance)) if comparable else None,
    }



def _num(r, *keys):
    """Public API contract; production-derived narrative omitted."""
    for k in keys:
        v = r.get(k)
        if v is not None:
            return float(v)
    return None


def _flex_execution_time(row):
    raw = str(row.get("dateTime") or "")
    try:
        local = datetime.datetime.strptime(raw, "%Y%m%d;%H%M%S").replace(
            tzinfo=ZoneInfo("America/New_York"))
        return local.astimezone(datetime.timezone.utc)
    except (TypeError, ValueError):
        return None


def _iso_time(value):
    try:
        parsed = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        return parsed.astimezone(datetime.timezone.utc)
    except (TypeError, ValueError):
        return None


def _finite_float(value, *, positive=False):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or (positive and number <= 0):
        return None
    return number


def _positive_integral(value):
    number = _finite_float(value, positive=True)
    if number is None or not number.is_integer():
        return None
    return int(number)


def propose_missing_campaign_closes(stmt, *, exits_path=None, trades_path=None,
                                    state_path=None):
    """Public API contract; production-derived narrative omitted."""
    exits_path = exits_path or os.path.join(APP, "exits.log")
    trades_path = trades_path or os.path.join(APP, "trades.log")
    state_path = state_path or os.path.join(APP, "exitmgr_state.json")
    exits = _strict_jsonl(exits_path)
    trades = _strict_jsonl(trades_path)
    try:
        state = json.load(open(state_path))
    except Exception as exc:
        return {"append_proposals": [], "state_retire_proposals": [],
                "refused": [{"symbol": "?", "why": "state unavailable: %s" % exc}]}

    closed_ids = {_cid(row) for row in exits
                  if _cid(row) and (row.get("realized_pnl") is not None
                                    or row.get("realized_pnl_net") is not None)}
    used_close_exec_ids = {
        str(exec_id) for row in exits for exec_id in (row.get("broker_close_exec_ids") or [])
        if str(exec_id or "").strip()
    }
    entries = [row for row in trades
               if _cid(row) and not str(row.get("event") or "").startswith("closed")]
    primary_counts = collections.Counter(_cid(row) for row in entries)
    flex = [dict(el.attrib) for el in ET.parse(stmt).getroot().findall(".//Trade")]
    in_flight = dict(state.get("in_flight") or {})
    source = "ibkr_flex_" + os.path.basename(stmt).replace(
        "flex-statement-", "").replace(".xml", "")
    append, retire, refused = [], [], []

    def rows_for(cid, oc):
        return [r for r in flex
                if str(r.get("conid") or "") == str(cid)
                and str(r.get("openCloseIndicator") or "").upper() == oc]

    for entry in entries:
        cid = _cid(entry)
        if cid in closed_ids:
            continue
        spread = entry.get("spread") or {}
        try:
            short_id = int(spread.get("short_con_id"))
        except (TypeError, ValueError):
            continue
        symbol = str(entry.get("symbol") or "?")
        qty = _positive_integral(
            entry.get("filled_qty") if entry.get("filled_qty") is not None
            else entry.get("quantity"))
        debit = _finite_float(entry.get("entry_fill_debit"), positive=True)
        if qty is None or debit is None:
            refused.append({"symbol": symbol, "con_id": cid,
                            "why": "journal quantity/debit is not finite, positive and integral"})
            continue
        if primary_counts[cid] != 1:
            refused.append({"symbol": symbol, "con_id": cid,
                            "why": "primary con_id occurs in %d journal entry rows" %
                                   primary_counts[cid]})
            continue
        inf = in_flight.get(str(cid))
        if not isinstance(inf, dict):
            continue
        submitted = inf.get("submitted_close") or {}
        submitted_legs = submitted.get("legs") or []
        try:
            submitted_shape = {
                (int(x.get("con_id")), _positive_integral(x.get("ratio")),
                 str(x.get("expected_side") or "").upper(),
                 _positive_integral(x.get("multiplier")))
                for x in submitted_legs
            }
        except (TypeError, ValueError, AttributeError):
            submitted_shape = set()
        expected_shape = {(cid, 1, "SLD", 100), (short_id, 1, "BOT", 100)}
        if (str(submitted.get("sec_type") or "").upper() != "BAG"
                or str(submitted.get("action") or "").upper() != "SELL"
                or len(submitted_legs) != 2
                or submitted_shape != expected_shape
                or _positive_integral(submitted.get("combo_qty")) != qty):
            refused.append({"symbol": symbol, "con_id": cid,
                            "why": "durable submitted-close BAG/action/legs/sides/ratios/"
                                   "multipliers/quantity do not match journal"})
            continue

        entry_ref = str(entry.get("order_ref") or "").strip()
        close_ref = str(inf.get("order_ref") or "").strip()
        if not entry_ref or not close_ref:
            refused.append({"symbol": symbol, "con_id": cid,
                            "why": "journal entry or in-flight close lacks an exact order_ref"})
            continue

        opened_long, opened_short = rows_for(cid, "O"), rows_for(short_id, "O")
        closed_long, closed_short = rows_for(cid, "C"), rows_for(short_id, "C")
        if not closed_long and not closed_short:
            continue
        facts = ((opened_long, "BUY", "opening long"),
                 (opened_short, "SELL", "opening short"),
                 (closed_long, "SELL", "closing long"),
                 (closed_short, "BUY", "closing short"))
        bad = None
        for rows, side, label in facts:
            if not rows:
                bad = "%s Flex execution is absent" % label
                break
            if {str(r.get("buySell") or "").upper() for r in rows} != {side}:
                bad = "%s side is not exactly %s" % (label, side)
                break
            quantities = [_finite_float(r.get("quantity")) for r in rows]
            if any(q is None or q == 0 for q in quantities):
                bad = "%s quantity is unreadable" % label
                break
            covered = sum(abs(q) for q in quantities)
            if abs(covered - qty) > 1e-9:
                bad = "%s covers %s contracts, journal requires %s" % (label, covered, qty)
                break
        if bad:
            refused.append({"symbol": symbol, "con_id": cid, "why": bad})
            continue

        opening_rows = opened_long + opened_short
        closing_rows = closed_long + closed_short
        opening_refs = {str(r.get("orderReference") or "").strip() for r in opening_rows}
        closing_refs = {str(r.get("orderReference") or "").strip() for r in closing_rows}
        if opening_refs != {entry_ref}:
            refused.append({"symbol": symbol, "con_id": cid,
                            "why": "Flex opening orderReference does not exactly match journal"})
            continue
        if closing_refs != {close_ref}:
            refused.append({"symbol": symbol, "con_id": cid,
                            "why": "Flex closing orderReference does not exactly match in-flight"})
            continue
        account_ids = {str(r.get("accountId") or "").strip()
                       for r in opening_rows + closing_rows}
        if len(account_ids) != 1 or "" in account_ids:
            refused.append({"symbol": symbol, "con_id": cid,
                            "why": "Flex executions do not bind one nonempty accountId"})
            continue
        all_exec_ids = [str(r.get("ibExecID") or "").strip()
                        for r in opening_rows + closing_rows]
        if any(not value for value in all_exec_ids) or len(set(all_exec_ids)) != len(all_exec_ids):
            refused.append({"symbol": symbol, "con_id": cid,
                            "why": "Flex execution IDs are absent or duplicated"})
            continue

        entered_at = _iso_time(entry.get("fill_ts") or entry.get("ts"))
        opening_times = [_flex_execution_time(r) for r in opening_rows]
        if (entered_at is None or any(t is None for t in opening_times)
                or (max(opening_times) - min(opening_times)).total_seconds() > 5
                or abs((max(opening_times) - entered_at).total_seconds()) > 5):
            refused.append({"symbol": symbol, "con_id": cid,
                            "why": "Flex opening legs do not share and match journal fill time"})
            continue

        long_open_values = [(_finite_float(r.get("quantity")),
                             _finite_float(r.get("tradePrice"), positive=True))
                            for r in opened_long]
        short_open_values = [(_finite_float(r.get("quantity")),
                              _finite_float(r.get("tradePrice"), positive=True))
                             for r in opened_short]
        if any(q is None or p is None for q, p in long_open_values + short_open_values):
            open_debit = None
        else:
            open_debit = round((
                sum(abs(q) * p for q, p in long_open_values)
                - sum(abs(q) * p for q, p in short_open_values)
            ) * 100.0, 2)
        if open_debit is None or abs(open_debit - debit) > 0.005:
            refused.append({"symbol": symbol, "con_id": cid,
                            "why": "Flex opening legs produce debit %r, journal records %.2f" %
                                   (open_debit, debit)})
            continue

        close_times = [_flex_execution_time(r) for r in closing_rows]
        if any(t is None for t in close_times) or \
                (max(close_times) - min(close_times)).total_seconds() > 5:
            refused.append({"symbol": symbol, "con_id": cid,
                            "why": "leg closes do not share one exact broker timestamp"})
            continue
        close_at = max(close_times)
        placed_at = _iso_time(inf.get("placed_at"))
        if close_at <= entered_at or placed_at is None or close_at < placed_at:
            refused.append({"symbol": symbol, "con_id": cid,
                            "why": "entry/submission/close chronology is not exact"})
            continue
        broker_values = [_finite_float(r.get("fifoPnlRealized") or 0)
                         for r in closing_rows]
        if any(value is None for value in broker_values):
            refused.append({"symbol": symbol, "con_id": cid,
                            "why": "closing fifoPnlRealized is unreadable"})
            continue
        broker_exact = sum(broker_values)
        if any(_finite_float(r.get("tradePrice"), positive=True) is None
               for r in closing_rows):
            refused.append({"symbol": symbol, "con_id": cid,
                            "why": "closing tradePrice is not finite and positive"})
            continue
        pnl = round(broker_exact, 2)
        exit_price = round((debit + pnl) / (100.0 * qty), 6)
        check = round(exit_price * 100.0 * qty - debit, 2)
        if not math.isfinite(exit_price) or exit_price < 0 or abs(check - pnl) > 0.005:
            refused.append({"symbol": symbol, "con_id": cid,
                            "why": "derived net exit price does not reproduce broker P&L"})
            continue
        exec_ids = sorted(str(r.get("ibExecID")).strip() for r in closing_rows)
        if set(exec_ids) & used_close_exec_ids:
            refused.append({"symbol": symbol, "con_id": cid,
                            "why": "one or more closing Flex executions are already journalled"})
            continue
        open_exec_ids = sorted(str(r.get("ibExecID")).strip() for r in opening_rows)
        open_ib_order_ids = sorted(str(r.get("ibOrderID") or "").strip()
                                   for r in opening_rows)
        close_ib_order_ids = sorted(str(r.get("ibOrderID") or "").strip()
                                    for r in closing_rows)
        if any(not value for value in open_ib_order_ids + close_ib_order_ids):
            refused.append({"symbol": symbol, "con_id": cid,
                            "why": "Flex opening or closing ibOrderID is absent"})
            continue
        binding_doc = {"account_id": next(iter(account_ids)),
                       "order_ref": close_ref, "exec_ids": exec_ids}
        binding = hashlib.sha256(json.dumps(
            binding_doc, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        close_identity = "ibkr-flex:%s" % binding
        if any(str(row.get("close_identity") or "") == close_identity for row in exits):
            refused.append({"symbol": symbol, "con_id": cid,
                            "why": "exact broker close identity is already journalled"})
            continue
        context = inf.get("exit_context") or {}
        extra = context.get("extra") or {}
        holding_days = round((close_at - entered_at).total_seconds() / 86400.0, 3)
        row = {
            "ts": close_at.isoformat(), "close_ts": close_at.isoformat(),
            "decision_id": entry.get("decision_id"),
            "model_identity": entry.get("model_identity"),
            "contract_id": cid, "conId": cid, "symbol": symbol,
            "right": entry.get("right"), "strike": entry.get("strike"),
            "structure": "spread", "spread": spread,
            "quantity": qty, "close_qty": qty,
            "entry_debit": debit, "entry_fill_debit": debit,
            "basis_source": entry.get("basis_source"),
            "exit_price_per_share": exit_price, "proceeds": round(debit + pnl, 2),
            "realized_pnl": pnl, "realizedPNL": pnl, "realized_pnl_net": None,
            "realized_pnl_pct": round(pnl / debit * 100.0, 2) if debit else None,
            "reason": context.get("reason") or "broker_reconciled_missing_close",
            "rule_fired": extra.get("rule_fired") or context.get("trigger_type"),
            "exit_reasoning": extra.get("exit_reasoning") or context.get("trigger_message"),
            "entry_ts": entry.get("fill_ts") or entry.get("ts"),
            "holding_days": holding_days, "conviction": entry.get("conviction"),
            "fill_status": "Filled", "terminal_order_status": "Filled",
            "order_id": inf.get("order_id"), "perm_id": inf.get("perm_id"),
            "client_id": inf.get("client_id"), "order_ref": inf.get("order_ref"),
            "close_identity": close_identity,
            "exit_basis_source": "ibkr_flex_netted_legs",
            "exit_basis_note": "append-only recovery of a missing campaign close; exact Flex "
                               "opening legs reproduce entry debit and exact closing legs cover "
                               "the durable submitted-close quantity",
            "commission_unknown": True,
            "broker_realized_exact": broker_exact,
            "broker_account_id_sha256": hashlib.sha256(
                next(iter(account_ids)).encode()).hexdigest(),
            "broker_open_order_ref": entry_ref,
            "broker_close_order_ref": close_ref,
            "broker_open_exec_ids": open_exec_ids,
            "broker_close_exec_ids": exec_ids,
            "broker_open_ib_order_ids": open_ib_order_ids,
            "broker_close_ib_order_ids": close_ib_order_ids,
            "broker_close_binding_sha256": binding,
        }
        append.append({
            "symbol": symbol, "con_id": cid, "row": row,
            "proof": "Flex open ref=%s reproduces $%.2f journal debit; close ref=%s; "
                     "%.6f*100*%d - %.2f = %.2f; both exact legs closed qty %d" %
                     (entry_ref, open_debit, close_ref, exit_price, qty, debit, check, qty),
            "apply": "append to exits.log and backfill the closed-trade dataset atomically",
        })
        state_sha = hashlib.sha256(json.dumps(inf, sort_keys=True,
                                              separators=(",", ":")).encode()).hexdigest()
        retire.append({
            "con_id": cid, "expected_in_flight_sha256": state_sha,
            "expected_order_ref": inf.get("order_ref"),
            "expected_close_identity": close_identity,
            "apply_after": "exit+dataset rows are durable and the SYML/campaign delta is zero",
            "action": "retire this exact in_flight latch; never clear by con_id alone",
        })
    return {"append_proposals": append, "state_retire_proposals": retire,
            "refused": refused, "repair_source": source}


def propose_repairs(rows, uncovered, bro, stmt, exits_path=None, trades_path=None,
                    state_path=None):
    """Public API contract; production-derived narrative omitted."""
    stamp = "ibkr_flex_" + os.path.basename(stmt).replace("flex-statement-", "").replace(".xml", "")
    today = datetime.date.today().strftime("%Y%m%d")
    by_symbol = collections.defaultdict(list)
    for closed, r in our_rows(exits_path):
        by_symbol[r.get("symbol") or "?"].append((closed, r))





    if exits_path is None or (trades_path is not None and state_path is not None):
        missing = propose_missing_campaign_closes(
            stmt, exits_path=exits_path, trades_path=trades_path, state_path=state_path)
    else:
        missing = {"append_proposals": [], "state_retire_proposals": [], "refused": []}
    missing_symbols = {p["symbol"] for p in missing["append_proposals"]}
    proposals, refused = [], []
    for u, b, m, d, _why in rows:
        if u in missing_symbols:
            refused.append((u, "a second exact journal campaign is fully closed at the broker "
                               "but absent locally; append that campaign instead of corrupting "
                               "the existing correctly-accounted row"))
            continue
        candidates = by_symbol.get(u, [])
        if b is None:
            refused.append((u, "the broker has no rows for this underlying at all -- this is a "
                               "trade we booked and IBKR never saw; a P&L edit cannot fix that"))
            continue
        if len(candidates) != 1:
            refused.append((u, "%d of our rows share this underlying; the broker figure is netted "
                               "across legs and cannot be attributed to one row" % len(candidates)))
            continue
        _closed, r = candidates[0]
        qty = _num(r, "close_qty", "quantity")
        debit = _num(r, "entry_fill_debit", "entry_debit")
        if not qty or debit is None:
            refused.append((u, "row lacks a usable close_qty / entry_fill_debit"))
            continue
        pnl = round(b, 2)
        price = round((debit + pnl) / (100.0 * qty), 4)
        check = round(price * 100.0 * qty - debit, 2)
        if abs(check - pnl) > 0.005:
            refused.append((u, "does not reconcile: %.4f*100*%g - %.2f = %.2f, expected %.2f"
                               % (price, qty, debit, check, pnl)))
            continue
        proposals.append({
            "symbol": u, "close_identity": r.get("close_identity"),
            "close_ts": r.get("close_ts") or r.get("ts"), "perm_id": r.get("perm_id"),
            "current": {"realized_pnl": r.get("realized_pnl"),
                        "avg_fill_price": r.get("avg_fill_price"),
                        "exit_price_per_share": r.get("exit_price_per_share")},
            "proposed": {"realized_pnl": pnl,
                         "avg_fill_price": price,
                         "exit_price_per_share": price,
                         "pnl_repair_source": stamp,
                         "avg_fill_price_source": stamp,
                         "realized_pnl_pre_repair_%s" % today: r.get("realized_pnl"),
                         "avg_fill_price_pre_repair_%s" % today: r.get("avg_fill_price")},
            "broker_realized_exact": b,
            "proof": "%.4f * 100 * %g - %.2f = %.2f == round(broker %.6f, 2)"
                     % (price, qty, debit, check, b),
            "derived_from": ["ibkr_flex.fifoPnlRealized", "exits.log.entry_fill_debit",
                             "exits.log.close_qty"],
        })
    return {
        "schema": "flex-repair-proposal.v1",
        "generated_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "statement": os.path.basename(stmt),
        "repair_source": stamp,
        "apply_to": ["~/exitmgr-app/exits.log", "~/trade-capture/events.jsonl"],
        "apply_note": APPLY_NOTE,
        "proposals": proposals,
        "append_proposals": missing["append_proposals"],
        "state_retire_proposals": missing["state_retire_proposals"],
        "refused": ([{"symbol": s, "why": w} for s, w in refused]
                    + list(missing["refused"])),
        "uncovered_rows": [{"symbol": s, "close_date": c, "realized_pnl": p, "why": w}
                           for s, c, p, w in uncovered],
    }


def write_proposal(doc, out_path):
    """Public API contract; production-derived narrative omitted."""
    out_path = os.path.abspath(os.path.expanduser(out_path))
    if os.path.basename(out_path) in LIVE_DATA:
        raise SystemExit("refusing to write a proposal over live data: %s" % out_path)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
    return out_path



def post(text):
    tok = os.environ.get("SLACK_BOT_TOKEN", "")
    if not tok:
        env = os.path.expanduser("~/.hermes/.env")
        if os.path.exists(env):
            for line in open(env):
                if line.strip().startswith("SLACK_BOT_TOKEN="):
                    tok = line.split("=", 1)[1].strip().strip('"').strip("'"); break
    if not tok:
        return False
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=json.dumps({"channel": CHANNEL, "text": text}).encode(),
        headers={"Content-Type": "application/json; charset=utf-8",
                 "Authorization": "Bearer %s" % tok})
    try:
        return bool(json.loads(urllib.request.urlopen(req, timeout=30).read().decode()).get("ok"))
    except Exception:
        return False


def _cid(row):
    for key in ("conId", "contract_id", "con_id"):
        if row.get(key) is not None:
            try:
                return int(row[key])
            except (TypeError, ValueError):
                return None
    return None


def _strict_jsonl(path):
    """Public API contract; production-derived narrative omitted."""
    rows = []
    with open(path) as fh:
        for line_no, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except Exception as exc:
                raise ValueError("%s line %d is not complete JSON" %
                                 (os.path.basename(path), line_no)) from exc
            if not isinstance(row, dict):
                raise ValueError("%s line %d is not a JSON object" %
                                 (os.path.basename(path), line_no))
            rows.append(row)
    return rows


def position_coverage_result(statement=None, *, now=None, max_age_hours=36.0):
    """Public API contract; production-derived narrative omitted."""
    def failed(message):
        return {"blocking": True, "status": "unavailable", "lines": [message],
                "unmanaged": (), "quantity_mismatches": (), "phantom": ()}

    try:
        import flex_positions as fpos
    except Exception as exc:
        return failed("_positions check unavailable (%s)_" % exc)

    stmt = statement or fpos.newest_positions_statement()
    if not stmt:
        return failed("_no positions statement archived yet -- run `flex_positions.py --archive`_")
    try:
        xml_text = open(stmt).read()
        root = ET.fromstring(xml_text)
        section = root.find(".//OpenPositions")
        if section is None:
            raise ValueError("statement has no OpenPositions section")
        fs = root.find(".//FlexStatement")
        generated = fs.get("whenGenerated") if fs is not None else None
        if not generated:
            raise ValueError("statement has no whenGenerated timestamp")
        generated_at = datetime.datetime.strptime(generated, "%Y%m%d;%H%M%S").replace(
            tzinfo=datetime.timezone.utc)
        observed_now = now or datetime.datetime.now(datetime.timezone.utc)
        if observed_now.tzinfo is None:
            observed_now = observed_now.replace(tzinfo=datetime.timezone.utc)
        age_hours = (observed_now.astimezone(datetime.timezone.utc) - generated_at).total_seconds() / 3600.0
        if age_hours < -0.1 or age_hours > float(max_age_hours):
            raise ValueError("statement is stale or future-dated (age %.1f hours; limit %.1f)"
                             % (age_hours, max_age_hours))
        rows = fpos.parse_open_positions(xml_text)
        exits = _strict_jsonl(os.path.join(APP, "exits.log"))
        trades = _strict_jsonl(os.path.join(APP, "trades.log"))
    except Exception as exc:
        return failed("_positions evidence unavailable (%s)_" % exc)

    broker_qty = collections.defaultdict(float)
    broker_by_id = {}
    for row in rows:
        cid = row.get("con_id")
        qty = row.get("position")
        if cid is None or qty is None:
            return failed("_positions evidence unavailable (broker row lacks con_id/position)_")
        broker_qty[int(cid)] += float(qty)
        broker_by_id[int(cid)] = row

    closed = {cid for cid in (_cid(row) for row in exits)
              if cid and (row.get("realized_pnl") is not None
                          or row.get("realized_pnl_net") is not None)}
    ours_qty = collections.defaultdict(float)
    ours_symbol = {}
    ours_long = set()
    for row in trades:
        if str(row.get("event") or "").startswith("closed"):
            continue
        cid = _cid(row)
        if not cid or cid in closed:
            continue
        raw_qty = row.get("filled_qty")
        if raw_qty is None:
            raw_qty = row.get("quantity")
        try:
            qty = abs(float(raw_qty))
        except (TypeError, ValueError):
            return failed("_positions evidence unavailable (journal row %s has no quantity)_" % cid)
        if qty <= 0:
            return failed("_positions evidence unavailable (journal row %s has nonpositive quantity)_" % cid)
        primary_sign = -1.0 if (str(row.get("side") or "").lower() == "credit"
                                or (str(row.get("action") or "").upper() == "SELL"
                                    and not row.get("spread"))) else 1.0
        ours_qty[cid] += primary_sign * qty
        ours_symbol[cid] = row.get("symbol") or "?"
        ours_long.add(cid)
        short_id = (row.get("spread") or {}).get("short_con_id")
        if short_id:
            try:
                short_id = int(short_id)
            except (TypeError, ValueError):
                return failed("_positions evidence unavailable (journal spread has invalid short con_id)_")
            ours_qty[short_id] -= qty
            ours_symbol[short_id] = row.get("symbol") or "?"

    broker_ids, ours_ids = set(broker_qty), set(ours_qty)
    unmanaged = tuple(sorted(broker_ids - ours_ids))
    phantom = tuple(sorted((ours_ids - broker_ids) & ours_long))
    mismatches = tuple(sorted(
        (cid, broker_qty[cid], ours_qty[cid]) for cid in broker_ids & ours_ids
        if abs(broker_qty[cid] - ours_qty[cid]) > 1e-9))

    lines = []
    if unmanaged:
        lines.append(":rotating_light: %d BROKER position(s) the journal does not manage -- no "
                     "stop, no time exit, no trail is pointed at these:" % len(unmanaged))
        for cid in unmanaged[:8]:
            row = broker_by_id.get(cid, {})
            lines.append("    %-7s con_id=%s %s %s%s  value %s" % (
                row.get("underlying") or "?", cid, "%+g" % broker_qty[cid],
                row.get("put_call") or "", row.get("strike") or "", row.get("position_value")))
    if mismatches:
        lines.append(":rotating_light: %d broker/journal quantity or sign mismatch(es):" %
                     len(mismatches))
        for cid, broker_value, journal_value in mismatches[:8]:
            lines.append("    %-7s con_id=%s broker=%+g journal=%+g" %
                         (ours_symbol.get(cid) or broker_by_id.get(cid, {}).get("underlying") or "?",
                          cid, broker_value, journal_value))
    if phantom:
        lines.append(":mag: %d journal row(s) the broker does not hold -- most likely EXPIRED: %s"
                     % (len(phantom), ", ".join("%s(%s)" % (ours_symbol[c], c)
                                               for c in phantom[:8])))
    blocking = bool(unmanaged or mismatches)
    if not lines:
        lines.append(":white_check_mark: positions: all %d broker legs match the journal in "
                     "contract, quantity, and sign." % len(broker_ids))
    return {"blocking": blocking, "status": "mismatch" if blocking else "ok",
            "lines": lines, "unmanaged": unmanaged,
            "quantity_mismatches": mismatches, "phantom": phantom,
            "statement": stmt, "age_hours": age_hours}


def position_coverage():
    """Public API contract; production-derived narrative omitted."""
    return position_coverage_result()["lines"]


def _frozen_query_note(win_to):
    """Public API contract; production-derived narrative omitted."""
    try:
        from exitmgr.flex_ingest import flex_window_staleness
        _, msg = flex_window_staleness({"toDate": win_to, "fromDate": None})
        if not msg:
            return None
    except Exception:
        return None
    return ("ROOT CAUSE: the statement window has stopped moving. The Flex Web Service cannot "
            "choose a period -- it is fixed in the query definition -- so this is "
            "IBKR_FLEX_QUERY_ID pointing at a query pinned to a CUSTOM DATE RANGE. Every pull "
            "returns the same dead window and this gap grows every week. REMEDY: edit that "
            "query's period in IBKR Account Management to a rolling one (e.g. Last 365 Calendar "
            "Days). No code change can do it.")


def render(rows, uncovered, window, tolerance, statement=None, aggregate=None, positions=None):
    """Public API contract; production-derived narrative omitted."""
    win_from, win_to = window
    lines = ["*Broker reconciliation* - window %s..%s%s"
             % (win_from or "?", win_to or "?",
                ("   [%s]" % os.path.basename(statement)) if statement else "")]
    if rows:
        lines += [":warning: %d underlying(s) disagree with the broker:" % len(rows), "```",
                  "%-7s %11s %11s %11s" % ("sym", "IBKR", "ours", "diff")]
        for u, b, m, d, why in rows:
            lines.append("%-7s %11s %11.2f %11s  %s" % (
                u, "n/a" if b is None else "%.2f" % b, m,
                "n/a" if d is None else "%.2f" % d, why[:40]))
        lines += ["```", "_exits.log is our reconstruction; IBKR is the book. Trust the broker._"]
    if uncovered:
        worst = sorted(uncovered, key=lambda x: -abs(x[2]))
        lines += [":mag: %d of our closed rows are OUTSIDE this statement's window and were NOT "
                  "checked against the broker (%.2f of realized P&L unexamined):"
                  % (len(uncovered), sum(abs(x[2]) for x in uncovered)), "```"]
        _root = _frozen_query_note(win_to)
        for sym, closed, pnl, why in worst[:8]:
            lines.append("%-7s %10s %11.2f  %s" % (sym, closed, pnl, why))
        if len(worst) > 8:
            lines.append("... and %d more" % (len(worst) - 8))
        if _root:
            lines += ["```", _root, "```"]
        lines += ["```",
                  "_The Flex query's window does not reach these closes, so this run cannot say "
                  "whether they agree. Re-point IBKR_FLEX_QUERY_ID at a rolling window (the "
                  "configured query is frozen at a range resolved at an earlier fixed date) and re-run._"]
    aggregate_bad = aggregate is not None and aggregate["within_tolerance"] is False
    aggregate_unknown = aggregate is not None and aggregate["within_tolerance"] is None
    if aggregate_bad:
        lines += [
            ":warning: aggregate money mismatch across %d comparable underlying(s): "
            "IBKR $%+.2f vs local $%+.2f = $%+.2f residual (limit $%.2f)."
            % (aggregate["matched_underlyings"], aggregate["broker_total"],
               aggregate["local_total"], aggregate["residual"], tolerance),
            "_Small per-symbol residuals can add up; green requires BOTH row-level and "
            "aggregate agreement._",
        ]
    if aggregate_unknown:
        lines.append(":warning: aggregate money comparison is UNEVALUATED — no underlying is "
                     "present in both broker and local closed-P&L evidence.")
    positions = positions or position_coverage_result()
    if not rows and not uncovered and not aggregate_bad and not aggregate_unknown \
            and not positions["blocking"]:
        lines.append(":white_check_mark: every underlying agrees with IBKR within $%.0f, and "
                     "every closed row we hold is inside the window." % tolerance)
    lines += [""] + list(positions["lines"])
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--statement")
    ap.add_argument("--tolerance", type=float, default=TOLERANCE)
    ap.add_argument("--dry-run", action="store_true", help="print, do not post to Slack")
    ap.add_argument("--propose", metavar="PATH", nargs="?", const="", default=None,
                    help="write a reviewed repair patch for the divergences. Never touches live "
                         "data; an operator applies it with the trader stopped.")
    a = ap.parse_args(argv)
    stmt = a.statement or newest_statement()
    if not stmt:
        print("no Flex statement archived; run `python -m exitmgr.flex_ingest` first")
        return 2
    try:
        rows, uncovered, window, (bro, _mine) = compare(stmt, a.tolerance)
    except Exception as exc:
        print("closed-P&L evidence unavailable: %s" % exc)
        return 2
    aggregate = aggregate_reconciliation(bro, _mine, a.tolerance)
    positions = position_coverage_result()
    text = render(rows, uncovered, window, a.tolerance, stmt, aggregate=aggregate,
                  positions=positions)
    print(text)
    if a.propose is not None:
        out = a.propose or os.path.join(
            APP, "logs", "flex-repair-proposal-%s.json"
            % datetime.datetime.now().strftime("%Y%m%dT%H%M%S"))
        doc = propose_repairs(rows, uncovered, bro, stmt)
        print("proposal written (NOT applied): %s  [%d edits, %d appends, %d latch retires, "
              "%d refused]" %
              (write_proposal(doc, out), len(doc["proposals"]),
               len(doc.get("append_proposals", [])),
               len(doc.get("state_retire_proposals", [])), len(doc["refused"])))
    elif not a.dry_run:
        print("posted" if post(text) else "NOT posted")
    return 1 if (rows or uncovered or aggregate["within_tolerance"] is not True
                 or positions["blocking"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
