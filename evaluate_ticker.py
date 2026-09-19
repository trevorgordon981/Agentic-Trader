#!/usr/bin/env python3
"""Public API contract; production-derived narrative omitted."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.expanduser("~/exitmgr-app"))

from exitmgr import research
from exitmgr.account import get_pot_snapshot
from exitmgr.config import load_config
from exitmgr.connection import IBConnection
from exitmgr.market import fetch_universe_quotes, format_context
from exitmgr.strategist import propose_one


def _add_lane_min_conviction(config_path, default=7):
    """Public API contract; production-derived narrative omitted."""
    try:
        import yaml
        raw = yaml.safe_load(open(os.path.expanduser(config_path))) or {}
        v = (raw.get("trading") or {}).get("add_suggest_min_conviction")
        return int(v) if v is not None else default
    except Exception:
        return default


async def build_brief(ib, cfg, approved: set[str]) -> str:
    """Public API contract; production-derived narrative omitted."""
    names = sorted({"SPY", "QQQ", "IWM"} | approved)
    today = str(datetime.now(timezone.utc).date())






    pot = await get_pot_snapshot(ib)
    print("  account: net_liq=%.2f available_funds=%.2f" % (pot.net_liq, pot.available_funds))
    if not (pot.net_liq > 0):
        raise RuntimeError(
            "account snapshot unusable (net_liq=%r) -- ABORTING rather than asking the model "
            "to judge a name it will refuse to size. A PASS from here means nothing."
            % pot.net_liq)
    try:
        quotes = await fetch_universe_quotes(ib, names)
    except Exception as exc:
        print("  [warn] quotes unavailable: %s" % exc)
        quotes = {}
    allow_any = bool(getattr(getattr(cfg, "limits", None), "allow_any_name", False))
    try:
        data = await research.gather(ib, names, single_names=sorted(approved))
        return research.build_brief(
            today=today, quotes=quotes, universe=names, allow_any_name=allow_any,
            book=None, book_detail=None, day_pnl_pct=None,
            net_liq=pot.net_liq, available_funds=pot.available_funds,
            **data)
    except Exception as exc:


        print("  [warn] research layer failed (%s) -- falling back to bare quotes" % exc)




        return research.with_account_sizing_snapshot(
            format_context(quotes, names, today, allow_any_name=allow_any),
            net_liq=pot.net_liq, available_funds=pot.available_funds)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker")
    ap.add_argument("--client-id", type=int, default=77,
                    help="MUST differ from the live loops (entry=1, protective=189)")
    ap.add_argument("--config", default=os.path.expanduser("~/exitmgr-app/config.yaml"))
    ap.add_argument("--show-brief", action="store_true")
    a = ap.parse_args()
    sym = a.ticker.strip().upper()

    cfg = load_config(config_path=a.config, arm=False, loop=False, interval=900)
    approved = set(getattr(cfg, "approved_names", []) or [])
    if sym not in approved:
        print("NOTE: %s is not in approved_names -- the live trader could not act on this "
              "verdict at all, whatever it says." % sym)

    endpoint = getattr(cfg, "llm_endpoint", "")
    model = getattr(cfg, "llm_model", "")
    if a.client_id in (int(getattr(cfg.ib, "client_id", 1)),
                       int(getattr(cfg.ib, "protective_client_id", 189))):
        print("REFUSING: client id %d belongs to a live trading loop." % a.client_id)
        return 2

    conn = IBConnection(host=cfg.ib.host, port=cfg.ib.port, client_id=a.client_id,
                        market_data_type=3)
    if not await conn.connect(retries=1, retry_delay=5.0):
        print("could not reach IB Gateway at %s:%s" % (cfg.ib.host, cfg.ib.port))
        return 1
    try:
        print("building the production brief (%d names)..." % (len(approved) + 3))
        brief = await build_brief(conn.ib, cfg, approved)
        if a.show_brief:
            print("-" * 70); print(brief); print("-" * 70)
        print("asking %s for its read on %s (thinking ON, it may decline)...\n" % (model, sym))
        idea, raw, cot = await asyncio.to_thread(
            propose_one, endpoint, model, brief, sym,
            1800, "enabled", False, True)
    finally:
        try:
            await conn.disconnect()
        except Exception:
            pass

    print("=" * 70)
    if idea is None:
        print("VERDICT: PASS -- the model declined to propose a trade on %s." % sym)
        print("That is a real answer, not a failure. It saw the name and found no edge.")
    else:




        add_lane_min = _add_lane_min_conviction(a.config)
        print("VERDICT: %s  conviction %s/10   (self-score -- NOT a trade gate)"
              % (getattr(idea, "underlying", sym), getattr(idea, "conviction", "?")))
        for f in ("direction", "structure", "expiry", "long_strike", "short_strike",
                  "horizon_days", "thesis", "reason"):
            v = getattr(idea, f, None)
            if v not in (None, ""):
                print("  %-14s %s" % (f, v))
        print("\n  The live trader has NO general conviction bar: every value 1-10 is traded")
        print("  if the mechanical gates pass. Conviction sets no size, no expiry and no")
        print("  admission -- entry_builder.entry_policy is deterministic and never reads it.")
        try:
            if int(getattr(idea, "conviction", 0)) < add_lane_min:
                print("  Below trading.add_suggest_min_conviction (%d), which binds ONLY the"
                      % add_lane_min)
                print("  same-day add-name lane in daily_recommend.py -- not this verdict.")
        except Exception:
            pass
    print("=" * 70)
    if cot:
        print("\n--- reasoning ---\n%s" % str(cot)[:4000])
    elif raw:
        print("\n--- raw ---\n%s" % str(raw)[:2000])
    print("\n(read-only: no order was placed, no shared state written)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
