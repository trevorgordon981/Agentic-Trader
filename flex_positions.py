#!/usr/bin/env python3
"""Public API contract; production-derived narrative omitted."""
import argparse, collections, glob, os, sys
import xml.etree.ElementTree as ET

APP = os.path.expanduser("~/exitmgr-app")
sys.path.insert(0, APP)

from exitmgr import flex_ingest as fi


def load_positions_query_id(env_path=None):
    """Public API contract; production-derived narrative omitted."""
    qid = os.environ.get("IBKR_FLEX_POSITIONS_QUERY_ID")
    env_path = env_path or os.path.expanduser("~/.hermes/.env")
    if not qid and os.path.exists(env_path):
        for line in open(env_path):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == "IBKR_FLEX_POSITIONS_QUERY_ID":
                qid = v.strip().strip('"').strip("'")
                break
    return qid


def parse_open_positions(xml_text):
    """Public API contract; production-derived narrative omitted."""
    root = ET.fromstring(xml_text)
    out = []
    for p in root.findall(".//OpenPosition"):
        a = p.attrib
        def num(k):
            try:
                return float(a.get(k)) if a.get(k) not in (None, "") else None
            except (TypeError, ValueError):
                return None
        out.append({
            "con_id": int(a["conid"]) if a.get("conid") else None,
            "symbol": a.get("symbol"),
            "underlying": a.get("underlyingSymbol"),
            "asset_class": a.get("assetCategory"),
            "position": num("position"),
            "mark_price": num("markPrice"),
            "position_value": num("positionValue"),
            "cost_basis": num("costBasisMoney"),
            "unrealized_pnl": num("fifoPnlUnrealized"),
            "strike": num("strike"),
            "expiry": a.get("expiry"),
            "put_call": a.get("putCall"),
            "multiplier": num("multiplier"),
        })
    return out


def netted_from_trades(path):
    """Public API contract; production-derived narrative omitted."""
    root = ET.parse(path).getroot()
    net = collections.defaultdict(float)
    meta = {}
    for t in root.findall(".//Trade"):
        a = t.attrib
        try:
            cid = int(a.get("conid") or 0); q = float(a.get("quantity") or 0)
        except (TypeError, ValueError):
            continue
        if cid:
            net[cid] += q
            meta[cid] = a
    return {c: (q, meta[c]) for c, q in net.items() if abs(q) > 1e-9}








POSITIONS_PREFIX = "flex-positions-"


def positions_archive_dir():
    return (os.environ.get("EXITMGR_FLEX_ARCHIVE")
            or os.path.expanduser("~/flex-archive"))


def archive_positions_xml(xml_text):
    """Public API contract; production-derived narrative omitted."""
    try:
        import datetime
        base = positions_archive_dir()
        os.makedirs(base, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
        path = os.path.join(base, "%s%s.xml" % (POSITIONS_PREFIX, stamp))
        with open(path, "w") as fh:
            fh.write(xml_text)
        return path
    except Exception:
        return None


def newest_positions_statement(archive=None):
    """Public API contract; production-derived narrative omitted."""
    base = archive or positions_archive_dir()
    best, best_key = None, None
    for p in glob.glob(os.path.join(base, POSITIONS_PREFIX + "*.xml")):
        try:
            fs = ET.parse(p).getroot().find(".//FlexStatement")
            key = (fs.get("toDate") or "", fs.get("whenGenerated") or "")
        except Exception:
            continue
        if best_key is None or key > best_key:
            best, best_key = p, key
    return best


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--xml", help="parse a saved statement instead of fetching")
    ap.add_argument("--compare", action="store_true",
                    help="also show the netted-from-trades view for comparison")
    ap.add_argument("--archive", action="store_true",
                    help="save the fetched statement so flex_reconcile can read it without fetching")
    a = ap.parse_args(argv)

    if a.xml:
        xml_text = open(a.xml).read()
    else:
        token, _ = fi.load_flex_creds()
        qid = load_positions_query_id()
        if not token:
            print("no IBKR_FLEX_TOKEN", file=sys.stderr); return 2
        if not qid:
            print("IBKR_FLEX_POSITIONS_QUERY_ID is not set.\n"
                  "Create an Activity Flex Query with the Open Positions section (see this "
                  "file's docstring) and add its id to ~/.hermes/.env.", file=sys.stderr)
            return 2
        xml_text = fi.fetch_statement_xml(token, qid)

    if a.archive and not a.xml:
        path = archive_positions_xml(xml_text)
        print("  archived: %s" % (path or "FAILED"))

    rows = parse_open_positions(xml_text)
    if not rows:
        print("no OpenPosition records in this statement -- is the query's Open Positions "
              "section enabled?")
    by = collections.defaultdict(list)
    for r in rows:
        by[r["underlying"] or r["symbol"]].append(r)
    print("  %-7s %5s %10s %12s %12s" % ("sym", "legs", "position", "value", "unrealized"))
    for sym in sorted(by):
        legs = by[sym]
        print("  %-7s %5d %10s %12s %12s" % (
            sym, len(legs),
            "/".join("%+g" % (l["position"] or 0) for l in legs[:4]),
            "%.2f" % sum(l["position_value"] or 0 for l in legs),
            "%.2f" % sum(l["unrealized_pnl"] or 0 for l in legs)))
    print("\n  underlyings: %d   legs: %d" % (len(by), len(rows)))

    if a.compare:
        arch = sorted(glob.glob(os.path.expanduser("~/flex-archive/*.xml")), key=os.path.getmtime)
        if arch:
            netted = netted_from_trades(arch[-1])
            nby = collections.defaultdict(int)
            for _c, (_q, m) in netted.items():
                nby[m.get("underlyingSymbol")] += 1
            print("\n  --- netted-from-trades (what we infer today) ---")
            for sym in sorted(nby):
                mark = "" if sym in by else "   <- broker reports NO position; inference is stale"
                print("    %-7s %d leg(s)%s" % (sym, nby[sym], mark))
            for sym in sorted(by):
                if sym not in nby:
                    print("    %-7s MISSING from the netted view -- opened before the trade window"
                          % sym)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
