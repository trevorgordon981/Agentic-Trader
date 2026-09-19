#!/usr/bin/env python3
"""Public API contract; production-derived narrative omitted."""
import argparse, collections, glob, json, os, sys
import xml.etree.ElementTree as ET

APP = os.path.expanduser("~/exitmgr-app")
ARCHIVE = os.path.expanduser("~/flex-archive")


def newest_statement():
    """Public API contract; production-derived narrative omitted."""
    sys.path.insert(0, APP)
    import flex_reconcile as _F
    return _F.newest_statement()


def flex_legs(path):
    """Public API contract; production-derived narrative omitted."""
    out = collections.defaultdict(lambda: {"O": [], "C": []})
    for t in ET.parse(path).getroot().findall(".//Trade"):
        a = t.attrib
        try:
            cid = int(a.get("conid") or 0)


            _c = a.get("ibCommission")
            comm = float(_c) if _c not in (None, "") else 0.0
            _q = a.get("quantity")
            qty = abs(float(_q)) if _q not in (None, "") else 0.0
        except (TypeError, ValueError):
            continue
        oc = (a.get("openCloseIndicator") or "").strip().upper()
        if not cid or oc not in ("O", "C"):
            continue
        out[cid][oc].append({"trade_id": a.get("tradeID"), "date": a.get("tradeDate"),
                             "qty": qty, "commission": round(abs(comm), 4),
                             "symbol": a.get("underlyingSymbol"),
                             "buy_sell": a.get("buySell")})
    return out


def legs_for(idx, con_ids, side, want_qty):
    """Public API contract; production-derived narrative omitted."""
    total, seen, missing = 0.0, [], []
    for cid in con_ids:
        trades = idx.get(cid, {}).get(side, [])
        if not trades:
            missing.append(cid)
            continue
        q = sum(t["qty"] for t in trades)
        if want_qty and abs(q - want_qty) > 1e-6:
            return None, ("leg %d has qty %g on side %s but the row closes %g -- partial or "
                          "scaled, so attributing a fee is inference" % (cid, q, side, want_qty))
        total += sum(t["commission"] for t in trades)
        seen += [t["trade_id"] for t in trades]
    if missing:
        return None, "no %s trades in the statement for leg(s) %s" % (
            "opening" if side == "O" else "closing", missing)
    return {"commission": round(total, 4), "trade_ids": seen}, None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--statement", default=None)
    ap.add_argument("--exits", default=os.path.join(APP, "exits.log"))
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    stmt = a.statement or newest_statement()
    if not stmt:
        print("no Flex statement found", file=sys.stderr)
        return 2
    idx = flex_legs(stmt)
    rows = [json.loads(l) for l in open(a.exits) if l.strip()]



    reconciled = set()
    try:
        sys.path.insert(0, APP)
        import flex_reconcile as _F
        _bro, _ = _F.broker_by_underlying(stmt)
        _ours = collections.Counter()
        for _r in rows:
            if _r.get("realized_pnl") is not None:
                _ours[_r.get("symbol")] += float(_r["realized_pnl"])
        reconciled = {s for s in _ours
                      if s in _bro and abs(float(_bro[s]) - _ours[s]) < 0.02}
    except Exception as _exc:
        print("REFUSING EVERYTHING: cannot read the broker to tell net from gross (%s)" % _exc,
              file=sys.stderr)
        reconciled = None

    proposals, refused, skipped = [], [], []
    for i, r in enumerate(rows):
        if r.get("realized_pnl") is None:
            continue
        sym = r.get("symbol")
        try:



            _cid = next((r[k] for k in ("conId", "contract_id") if r.get(k) is not None), None)
            long_cid = int(_cid) if _cid is not None else 0
        except (TypeError, ValueError):
            long_cid = 0
        sp = r.get("spread") or {}
        short_cid = sp.get("short_con_id")
        if not long_cid:
            refused.append({"row": i, "symbol": sym, "why": "no conId on the row"})
            continue
        con_ids = [long_cid] + ([int(short_cid)] if short_cid else [])
        if sp and not short_cid:
            refused.append({"row": i, "symbol": sym, "why": "spread with no short_con_id"})
            continue

        try:
            _rq = r.get("quantity")
            qty = float(_rq) if _rq is not None else 0.0
        except (TypeError, ValueError):
            qty = 0.0













        if reconciled is None or sym in reconciled:
            refused.append({"row": i, "symbol": sym,
                            "why": ("broker unreadable -- refusing rather than guessing"
                                    if reconciled is None else
                                    "realized_pnl already reconciles to the broker, so it is "
                                    "ALREADY NET -- attaching fees would net it twice")})
            continue

        prop = {"row": i, "symbol": sym, "ts": r.get("ts"), "con_ids": con_ids,
                "quantity": qty, "changes": {}, "evidence": {}}
        for field, side in (("entry_commission", "O"), ("exit_commission", "C")):
            if r.get(field) is not None:
                skipped.append({"row": i, "symbol": sym, "field": field,
                                "why": "already present (%s) -- never overwritten" % r[field]})
                continue
            got, why = legs_for(idx, con_ids, side, qty)
            if got is None:
                refused.append({"row": i, "symbol": sym, "field": field, "why": why})
                continue
            prop["changes"][field] = got["commission"]
            prop["evidence"][field] = {"trade_ids": got["trade_ids"],
                                       "source": "ibkr_flex:" + os.path.basename(stmt)}
        if prop["changes"]:
            proposals.append(prop)













    try:
        from exitmgr.pnl_semantics import net_realized as _net
        by_sym = collections.defaultdict(list)
        for p in proposals:
            by_sym[p["symbol"]].append(p)
        kept = []
        for sym, props in by_sym.items():
            if sym not in (_bro or {}):
                for p in props:
                    refused.append({"row": p["row"], "symbol": sym,
                                    "why": "no broker figure to verify the result against"})
                continue
            changes = {p["row"]: p["changes"] for p in props}
            total, unknown = 0.0, 0
            for i, r in enumerate(rows):
                if r.get("symbol") != sym or r.get("realized_pnl") is None:
                    continue
                sim = dict(r)
                if i in changes:
                    sim.update(changes[i])
                n, _b = _net(sim)
                if n is None:
                    unknown += 1
                    n = float(sim["realized_pnl"])
                total += n
            residual = float(_bro[sym]) - total
            if abs(residual) < 0.02:
                kept.extend(props)
            else:
                for p in props:
                    refused.append({
                        "row": p["row"], "symbol": sym,
                        "why": ("applying this leaves %s at %.2f against a broker %.2f "
                                "(residual %.2f) -- the repair does not reconcile, so at least "
                                "one row here is already net and its fees would be counted twice"
                                % (sym, total, float(_bro[sym]), residual))})
        proposals = kept
    except Exception as _exc:
        print("REFUSING EVERYTHING: after-check could not run (%s)" % _exc, file=sys.stderr)
        refused.extend({"row": p["row"], "symbol": p["symbol"],
                        "why": "after-check unavailable -- refusing rather than guessing"}
                       for p in proposals)
        proposals = []

    doc = {"statement": os.path.basename(stmt), "exits": a.exits,
           "rows_examined": sum(1 for r in rows if r.get("realized_pnl") is not None),
           "proposals": proposals, "refused": refused, "skipped": skipped,
           "note": "NOT APPLIED. Apply only with the armed loops stopped."}
    out = a.out or os.path.join(APP, "data", "commission_backfill_proposal.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = out + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(doc, fh, indent=1)
    os.replace(tmp, out)
    print("proposal written (NOT applied): %s" % out)
    print("  rows examined : %d" % doc["rows_examined"])
    print("  proposed      : %d rows, %d field(s)"
          % (len(proposals), sum(len(p["changes"]) for p in proposals)))
    print("  refused       : %d" % len(refused))
    print("  skipped       : %d (already present)" % len(skipped))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
