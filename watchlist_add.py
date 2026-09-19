#!/usr/bin/env python3
"""Public API contract; production-derived narrative omitted."""
import argparse
import os
import re
import shutil
import sys
from datetime import datetime

CONFIG_PATH = os.path.expanduser("~/exitmgr-app/config.yaml")
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,6}$")
APPROVED_RE = re.compile(r"(  approved_names: \[)([^\]]*)(\])")




CURATED_CLUSTER = {

    "AMUU": "semis", "ARMG": "semis", "ASMG": "semis", "AVL": "semis",
    "LRCU": "semis", "MUD": "semis", "MULL": "semis", "MUU": "semis",
    "NVDL": "semis", "SNXX": "semis", "SNDU": "semis", "TSMU": "semis",
    "TSXU": "semis", "RAM": "semis", "SKHY": "semis",

    "ORCX": "enterprise_soft",
    "NOWL": "enterprise_soft",
    "SNOU": "enterprise_soft",
    "PALU": "cybersecurity",
    "CONL": "crypto",
    "BITX": "crypto",
    "MSTX": "crypto",
    "SOFX": "fintech",
    "OSCX": "healthcare",
    "APPX": "adtech",
    "SECZ": "crypto",

    "UCO": "energy", "UGA": "energy", "USO": "energy", "UNG": "energy",
    "DBO": "energy", "SYMT": "energy", "AGQ": "mining_metals",
    "BDRY": "energy",

    "WWR": "materials",
    "LAC": "mining_metals",

    "AMPX": "cleantech",

    "DDM": "index_levered", "QLD": "index_levered", "SSO": "index_levered",



    "AAPL": "megacap_tech", "MSFT": "megacap_tech", "GOOG": "megacap_tech",
    "GOOGL": "megacap_tech", "AMZN": "megacap_tech", "META": "megacap_tech",
    "SYMY": "megacap_tech", "NVDA": "semis", "AMD": "semis", "AVGO": "semis",
    "SYML": "semis", "MUTX": "semis", "TSM": "semis", "LRCX": "semis",
    "MRVL": "semis", "QCOM": "semis", "AMAT": "semis", "KLAC": "semis",
    "ASML": "semis", "SYMC": "semis", "SYN1": "semis", "WDC": "semis",
    "STX": "semis", "DRAQ": "semis", "MRAM": "semis", "POET": "semis",

    "CRWD": "cybersecurity", "FTNT": "cybersecurity", "OKTA": "cybersecurity",
    "PANW": "cybersecurity", "ZS": "cybersecurity", "S": "cybersecurity",

    "COIN": "crypto", "MSTR": "crypto", "RIOT": "crypto", "SYMA": "crypto",
    "CIFR": "crypto", "SYMU": "crypto", "SYMG": "crypto", "ARKB": "crypto",

    "SOFI": "fintech", "HOOD": "fintech", "BULL": "fintech",

    "ORCL": "enterprise_soft", "CRM": "enterprise_soft", "NOW": "enterprise_soft",
    "SNOW": "enterprise_soft", "TEAM": "enterprise_soft", "WDAY": "enterprise_soft",
    "INTU": "enterprise_soft", "ADBE": "enterprise_soft", "FIG": "enterprise_soft",

    "SYMB": "ai_software", "AI": "ai_software", "BBAI": "ai_software",
    "SOUN": "ai_software",

    "QUBT": "quantum_computing", "RGTI": "quantum_computing", "QBTS": "quantum_computing",
    "IONQ": "quantum_computing", "BTQ": "quantum_computing",
    "ARQQ": "quantum_computing", "QTUM": "quantum_computing",

    "VRT": "ai_power", "OKLO": "ai_power", "BE": "ai_power", "SYMI": "ai_power",
    "SYMP": "ai_power", "GEV": "ai_power", "VST": "ai_power", "LEU": "ai_power",
    "NNE": "ai_power", "UUUU": "ai_power",
}






GICS_SECTOR_TO_CLUSTER = {
    "Consumer Cyclical": "consumer",
    "Consumer Defensive": "consumer",
    "Banks": "financials",
    "Energy": "energy",
    "Industrials": "industrials",
    "Utilities": "utilities",
    "Materials": "materials",
    "Healthcare": "healthcare",
}


GICS_INDUSTRY_TO_CLUSTER = {
    "Semiconductors": "semis",
    "Semiconductor Equipment & Materials": "semis",
    "Computer Hardware": "hardware_mfg",
    "Computer Storage": "semis",
    "Information Technology Services": "enterprise_soft",
    "Software - Infrastructure": "ai_software",
    "Software - Application": "enterprise_soft",
    "Electronic Components": "hardware_mfg",
    "Communication Equipment": "telecom",
    "Aerospace & Defense": "space",
    "Health Information Services": "healthcare",
    "Biotechnology": "healthcare",
    "Drug Manufacturers - General": "healthcare",
    "Drug Manufacturers - Specialty & Generic": "healthcare",
    "Banks - Diversified": "financials",
    "Banks - Regional": "financials",
    "Capital Markets": "financials",
    "Asset Management": "financials",
    "Gold": "mining_metals",
    "Other Precious Metals & Mining": "mining_metals",
    "Uranium": "ai_power",
    "Utilities - Independent Power Producers": "ai_power",
    "Utilities - Regulated Electric": "utilities",
    "Oil & Gas E&P": "energy",
    "Oil & Gas Integrated": "energy",
    "Oil & Gas Midstream": "energy",
    "Oil & Gas Refining & Marketing": "energy",
    "Oil & Gas Drilling": "energy",
    "Restaurants": "consumer",
    "Beverages - Non-Alcoholic": "consumer",
    "Department Stores": "consumer",
    "Apparel Retail": "consumer",
    "Apparel Manufacturing": "consumer",
    "Footwear & Accessories": "consumer",
    "Household & Personal Products": "consumer",
    "Resorts & Casinos": "consumer",
}


def parse_tickers(args):
    """Public API contract; production-derived narrative omitted."""
    raw = []
    for a in args:
        raw.extend(p for p in re.split(r"[,\s]+", a) if p)
    out, seen = [], set()
    for t in (x.strip().upper() for x in raw):
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def current_approved(s):
    m = APPROVED_RE.search(s)
    if not m:
        return None, None
    cur = [x.strip() for x in m.group(2).split(",") if x.strip()]
    return cur, m


def current_blocked(s):
    """Public API contract; production-derived narrative omitted."""
    blocked = []
    in_block = False
    for line in s.splitlines():
        if re.match(r"^\s*blocked_names:\s*$", line):
            in_block = True
            continue
        if in_block:
            m = re.match(r"^\s*-\s*([A-Za-z0-9.\-]+)\s*$", line)
            if m:
                blocked.append(m.group(1).strip().upper())
            elif line.strip() and not line.lstrip().startswith("#"):
                break
    return set(blocked)


def _yf_sector_cluster(ticker):
    """Public API contract; production-derived narrative omitted."""
    try:
        import yfinance as yf
        info = (yf.Ticker(ticker).info) or {}
        industry = (info.get("industry") or "").strip()
        sector = (info.get("sector") or "").strip()
    except Exception:
        return None
    if industry:
        c = GICS_INDUSTRY_TO_CLUSTER.get(industry)
        if c:
            return c
    if sector:
        c = GICS_SECTOR_TO_CLUSTER.get(sector)
        if c:
            return c
    return None


def cluster_for(ticker):
    """Public API contract; production-derived narrative omitted."""
    c = CURATED_CLUSTER.get(ticker)
    if c:
        return c
    return _yf_sector_cluster(ticker)


def current_sector_map(s):
    """Public API contract; production-derived narrative omitted."""
    mapping = {}
    in_map = False
    for line in s.splitlines():
        if re.match(r"^\s+sector_map:\s*$", line):
            in_map = True
            continue
        if in_map:
            m = re.match(r"^\s+([A-Z][A-Z0-9.\-]{0,6}):\s*(\S+)", line)
            if m and not line.lstrip().startswith("#"):
                mapping[m.group(1)] = m.group(2)
            elif re.match(r"^\s+[a-z_]+:\s*", line) and not line.lstrip().startswith("#"):
                break
    return mapping


def add_sector_entries(s, entries):
    """Public API contract; production-derived narrative omitted."""
    if not entries:
        return s
    lines = s.splitlines()
    idx = None
    indent = "  "
    for i, line in enumerate(lines):
        if re.match(r"^\s+pot_cap_usd:\s", line):
            idx = i
            break
    if idx is None:
        return s
    pad = indent + "  "
    block = "".join(f"{pad}{sym}: {cls}\n" for sym, cls in sorted(entries.items()))
    lines.insert(idx, block.rstrip("\n"))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Add ticker(s) to the trading watchlist (approved_names).")
    ap.add_argument("tickers", nargs="+", help="Tickers, space- or comma-separated (e.g. MUTX NVDA DELL)")
    ap.add_argument("--config", default=CONFIG_PATH, help="Path to config.yaml")
    args = ap.parse_args()

    config_path = os.path.expanduser(args.config)
    requested = parse_tickers(args.tickers)
    if not requested:
        print("No tickers given.")
        return 1

    s = open(config_path).read()
    cur, m = current_approved(s)
    if cur is None:
        print(f"ERROR: could not find `approved_names: [...]` in {config_path}", file=sys.stderr)
        return 2
    cur_up = {c.upper() for c in cur}
    blocked = current_blocked(s)
    sm_before = current_sector_map(s)

    added, already, blocked_skip, invalid = [], [], [], []
    to_add = []
    for t in requested:
        if not TICKER_RE.match(t):
            invalid.append(t)
        elif t in blocked:
            blocked_skip.append(t)
        elif t in cur_up:
            already.append(t)
        else:
            to_add.append(t)
            added.append(t)
            cur_up.add(t)


    classify = {}
    for t in requested:
        if t in blocked or t in invalid:
            continue
        if t in sm_before:
            continue
        c = cluster_for(t)
        if c:
            classify[t] = c
        else:
            classify[t] = "__unclassified__"

    if to_add or classify:
        bak = config_path + ".bak." + datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(config_path, bak)
        if to_add:
            new_inner = ", ".join(cur + to_add)
            s = s[:m.start()] + "  approved_names: [" + new_inner + "]" + s[m.end():]
        if classify:
            s = add_sector_entries(s, classify)
        open(config_path, "w").write(s)
        try:
            import yaml
            with open(config_path) as f:
                yaml.safe_load(f)
        except Exception as e:
            shutil.copy2(bak, config_path)
            print(f"ERROR: edit produced invalid YAML, rolled back from {bak}: {e}", file=sys.stderr)
            return 3
        print(f"Backed up config -> {bak}")

    class_report = []
    unclass = []
    for t in sorted(classify):
        if classify[t] == "__unclassified__":
            unclass.append(t)
        else:
            class_report.append(f"{t} -> {classify[t]}")

    new_count = len(cur) + len(to_add)
    print("Watchlist update:")
    if added:
        print(f"  added ({len(added)}):           {', '.join(added)}")
    if already:
        print(f"  already present ({len(already)}): {', '.join(already)}")
    if blocked_skip:
        print(f"  SKIPPED (blocked) ({len(blocked_skip)}): {', '.join(blocked_skip)}")
    if invalid:
        print(f"  SKIPPED (not a valid ticker) ({len(invalid)}): {', '.join(invalid)}")
    print(f"  approved_names count: {len(cur)} -> {new_count}")
    if class_report:
        print(f"  classified ({len(class_report)}):")
        for line in class_report:
            print(f"    {line}")
    if unclass:
        print(f"  UNCLASSIFIED (could not auto-determine category; left __unclassified__): "
              f"{', '.join(unclass)}")
        print("    (re-run anytime; or tell Alfred the category and it will backfill)")
    if added:
        print("  (config reloads on the next slate/loop; to apply now: "
              "launchctl kickstart -k gui/$(id -u)/ai.alfred.trader)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
