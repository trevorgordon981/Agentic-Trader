#!/usr/bin/env python
"""Public API contract; production-derived narrative omitted."""
import asyncio
import json
import os
import sys

sys.path.insert(0, "/opt/agentic-trader/exitmgr-app")

from ib_async import IB, Stock
from exitmgr import entry_safety, research
from exitmgr.config import load_config


async def main():
    cfg = load_config(config_path="/opt/agentic-trader/exitmgr-app/config.yaml",
                      arm=False, loop=False, interval=900)
    approved = sorted({str(t).upper() for t in (getattr(cfg, "approved_names", []) or [])})
    exempt = {t.upper() for t in entry_safety.NO_EARNINGS_ETFS}
    proxied = {t.upper() for t in research.EARNINGS_PROXY_SYMBOLS}
    overlap = sorted(exempt & proxied)
    sector = {k.upper(): v for k, v in (getattr(cfg, "sector_map", {}) or {}).items()}

    ib = IB()
    await ib.connectAsync("127.0.0.1", 4001, clientId=92, timeout=20)
    kinds = {}
    try:
        for sym in approved:
            try:
                cds = await asyncio.wait_for(
                    ib.reqContractDetailsAsync(Stock(sym, "SMART", "USD")), 15)
                kinds[sym] = (getattr(cds[0], "stockType", "") or "").strip().upper() if cds else "?"
            except Exception:
                kinds[sym] = "?"
    finally:
        ib.disconnect()

    etfs = {s for s, k in kinds.items() if k == "ETF"}
    missing = sorted(etfs - exempt - proxied)
    stale = sorted(s for s in exempt if s in kinds and kinds[s] not in ("ETF", "?"))
    unknown = sorted(s for s, k in kinds.items() if k == "?")
    unclassified = sorted(s for s in approved if s not in sector)

    L = []
    L.append("approved=%d  ETFs=%d  no-earnings=%d  earnings-proxied=%d" %
             (len(approved), len(etfs), len(exempt), len(proxied)))
    L.append("")
    if overlap:
        L.append("INVALID EARNINGS CLASSIFICATION (both no-earnings and proxied): " +
                 ", ".join(overlap))
        L.append("")
    if missing:
        L.append("ETFs MISSING the earnings exemption (these WILL be blocked at the final gate):")
        for s in missing:
            L.append("    %-6s stockType=ETF  cluster=%s" % (s, sector.get(s, "UNCLASSIFIED")))
        L.append("")
        L.append("  classify each explicitly: broad/index fund -> NO_EARNINGS_ETFS; "
                 "single-stock fund -> research.EARNINGS_PROXY_SYMBOLS")
    else:
        L.append("No ETFs missing the exemption.")
    if stale:
        L.append("")
        L.append("EXEMPT BUT NOT AN ETF -- a safety gate is disarmed on a single name, REMOVE:")
        for s in stale:
            L.append("    %-6s stockType=%s" % (s, kinds[s]))
    if unknown:
        L.append("")
        L.append("Could not classify (left OUT of the exemption, fail-closed): " + ", ".join(unknown))
    if unclassified:
        L.append("")
        L.append("Approved but no sector cluster (sector cap does nothing for these): "
                 + ", ".join(unclassified))

    body = "\n".join(L)
    print(body)
    json.dump({"kinds": kinds, "missing": missing, "stale": stale, "overlap": overlap,
               "earnings_proxies": research.EARNINGS_PROXY_SYMBOLS},
              open("/tmp/etf_scan.json", "w"), indent=1)

    tok = ""
    try:
        for ln in open(os.path.expanduser("~/.hermes/.env"), errors="replace"):
            if ln.startswith("SLACK_BOT_TOKEN="):
                tok = ln.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    if tok and (missing or stale or overlap):
        import urllib.request
        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage",
            json.dumps({"channel": "CHANNEL_ID_PLACEHOLDER",
                        "text": "*weekly ETF / earnings-gate scan*\n```\n%s\n```" % body}).encode(),
            {"Content-Type": "application/json", "Authorization": "Bearer " + tok})
        try:
            urllib.request.urlopen(req, timeout=30)
            print("\n  posted to Slack")
        except Exception as e:
            print("\n  slack post failed: %s" % e)


asyncio.run(main())
