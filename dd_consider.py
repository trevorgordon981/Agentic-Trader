#!/usr/bin/env python
"""Public API contract; production-derived narrative omitted."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

import daily_recommend as dr
from exitmgr import approval, dd_evidence, entry_safety, research
from exitmgr.account import get_pot_snapshot
from exitmgr.config import construction_from_dict
from exitmgr.connection import IBConnection
from exitmgr.dd_evidence import DDEvidenceError
from exitmgr.market import fetch_universe_quotes, usable_price
from exitmgr.slate_lock import slate_active_guard
from exitmgr.strategist import propose_intents
from exitmgr.trader import _market_open, audit



CLIENT_ID = 972

ITEMS_PATH = os.path.expanduser("~/trade-capture/dd/items.jsonl")
STATE_PATH = os.path.expanduser("~/trade-capture/dd/consider_state.json")
STATE_SCHEMA = "dd_consider.v1"



TERMINAL_STATUSES = {
    "stage1_rejected", "no_tradeable_thesis", "not_time_sensitive",
    "stage2_no_intent", "stage2_below_bar", "stage2_declined", "nominated", "no_ticker",
}



def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_items(path: str) -> list:
    """Public API contract; production-derived narrative omitted."""
    out = []
    p = Path(path)
    if not p.exists():
        return out
    for lineno, line in enumerate(p.read_text().splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"[WARN] {path}:{lineno} is not JSON ({exc}); skipped")
            continue
        if isinstance(rec, dict):
            rec["_lineno"] = lineno
            out.append(rec)
    return out


def item_key(item: dict) -> str:
    """Public API contract; production-derived narrative omitted."""
    key = item.get("capture_id") or item.get("slack_ts")
    return str(key) if key else ""


def load_state(path: str) -> dict:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {"schema": STATE_SCHEMA, "items": {}}
    if not isinstance(data, dict) or not isinstance(data.get("items"), dict):
        return {"schema": STATE_SCHEMA, "items": {}}
    data.setdefault("schema", STATE_SCHEMA)
    return data


def save_state(path: str, state: dict) -> None:
    """Public API contract; production-derived narrative omitted."""
    state["schema"] = STATE_SCHEMA
    state["updated_at"] = _now_iso()
    tmp = f"{path}.tmp.{os.getpid()}"
    Path(tmp).write_text(json.dumps(state, indent=2, sort_keys=True))
    os.replace(tmp, path)



def _hr(title: str = "") -> None:
    print("\n" + ("-" * 78))
    if title:
        print(title)
        print("-" * 78)



def run_stage1(item: dict, tr: dict, args) -> tuple:
    """Public API contract; production-derived narrative omitted."""
    ticker = (item.get("tickers") or [None])[0]
    urls = [u for u in (item.get("urls") or []) if isinstance(u, str)]
    if not urls:
        raise DDEvidenceError("item has no URL to read")
    url = urls[0]

    local_html = item.get("_local_article_html")
    if local_html is not None:



        text = dd_evidence.html_to_text(local_html)
        meta = {"url": url, "final_url": url, "http_status": None,
                "content_type": "text/html (local fixture)", "bytes": len(local_html),
                "text_chars": len(text), "truncated": False, "fixture": True}
    else:
        text, meta = dd_evidence.fetch_article(url, timeout=args.fetch_timeout)

    print(f"  fetched: {meta['text_chars']} chars of plain text "
          f"(from {meta['bytes']} bytes, {meta['content_type']}, "
          f"status={meta['http_status']}, truncated={meta['truncated']})")
    print(f"  url: {meta['final_url']}")
    if args.show_article:
        print("  --- extracted text (first 1200 chars) ---")
        print("  " + text[:1200].replace("\n", "\n  "))
        print("  --- end ---")

    evidence, debug = dd_evidence.extract(
        endpoint=tr.get("llm_endpoint"), model=tr.get("llm_model"),
        ticker=ticker, article_text=text, source_url=meta["final_url"],
        timeout=args.stage1_timeout, thinking=args.thinking,
        extracted_at=_now_iso(), fetch_meta=meta, return_debug=True)
    return evidence, {"fetch": meta, "debug": debug, "text_chars": len(text)}



async def run_stage2(ib, cfg, tr, evidence, args, audit_path):
    """Public API contract; production-derived narrative omitted."""
    ticker = evidence.ticker

    pot = await get_pot_snapshot(ib)
    acct = entry_safety.account_snapshot_valid(pot)
    if not acct.allowed:
        return {"status": "blocked", "reason": "account snapshot invalid: " + "; ".join(acct.reasons)}
    print(f"  account snapshot OK — net_liq ${pot.net_liq:,.2f}, "
          f"available ${pot.available_funds:,.2f}")



    names = ["SPY", "QQQ", "IWM", ticker]
    quotes = await fetch_universe_quotes(ib, names)
    last = (quotes.get(ticker) or {}).get("last")
    if not usable_price(last):
        return {"status": "blocked", "reason": f"fresh underlying quote unavailable for {ticker}"}
    data = await research.gather(ib, names, single_names=[ticker])
    book = await dr._open_positions_for_risk()
    brief = research.build_brief(
        today=str(datetime.now(timezone.utc).date()),
        quotes=quotes, universe=names, allow_any_name=False, book=book,
        net_liq=pot.net_liq, available_funds=pot.available_funds, **data)
    print(f"  fresh brief built: {len(brief)} chars, {ticker} last {last}")

    block = dd_evidence.evidence_block(evidence)
    brief_with_evidence = dd_evidence.append_evidence(brief, evidence)

    _hr("EXACT EVIDENCE BLOCK APPENDED TO THE STAGE 2 BRIEF")
    print(block)
    _hr()




    dd_evidence.assert_block_clean(
        brief_with_evidence[brief_with_evidence.index(dd_evidence.BLOCK_BEGIN):])

    if args.dump_prompt:
        Path(args.dump_prompt).write_text(brief_with_evidence)
        print(f"  [dump] exact Stage 2 user prompt written to {args.dump_prompt}")

    if args.stage2_stop_before_model:
        return {"status": "dry_stopped", "reason": "--stage2-stop-before-model",
                "brief_chars": len(brief_with_evidence)}

    try:
        with slate_active_guard():
            result = propose_intents(
                tr.get("llm_endpoint"), tr.get("llm_model"), brief_with_evidence,
                ticker=ticker, timeout=args.stage2_timeout,
                return_cot=True, return_identity=True)
    except Exception as exc:
        return {"status": "error", "reason": f"Stage A failed: {type(exc).__name__}: {exc}"}

    intents, raw, cot, identity = (list(result) + [None] * 4)[:4] if isinstance(result, tuple) \
        else (result, None, None, None)
    print(f"  Stage A returned {len(intents or [])} intent(s)")
    if not intents:
        return {"status": "stage2_no_intent",
                "reason": "the strategist declined to propose anything on this name"}
    intent = intents[0]
    print(f"  intent: {intent.direction} {intent.structure} {intent.target_dte}DTE "
          f"delta {intent.target_delta} conviction {intent.conviction}/10")
    print(f"  alpha: {getattr(intent, 'alpha', '')}")
    print(f"  thesis: {getattr(intent, 'thesis', '')}")

    ideas = await dr._materialize_stage_b(ib, intents, pot, tr, audit_path)
    if not ideas:
        return {"status": "stage2_declined",
                "reason": "Stage B produced no executable candidate (declined or too few "
                          "prefiltered candidates)",
                "conviction": intent.conviction, "direction": intent.direction,
                "structure": intent.structure}
    idea = ideas[0]

    min_conv = float(tr.get("add_suggest_min_conviction", 6))
    verdict = {"conviction": idea.conviction, "direction": idea.direction,
               "structure": idea.structure, "dte": idea.target_dte,
               "thesis": str(getattr(idea, "thesis", "") or "")[:400],
               "min_conviction": min_conv, "idea": idea, "raw": raw, "cot": cot,
               "identity": identity, "pot": pot}
    if idea.conviction < min_conv:
        verdict["status"] = "stage2_below_bar"
        verdict["reason"] = (f"conviction {idea.conviction}/10 is below the same-day bar "
                             f"({min_conv:.0f}); it can ride the daily slate")
        return verdict
    verdict["status"] = "nominate"
    verdict["reason"] = f"conviction {idea.conviction}/10 clears the same-day bar ({min_conv:.0f})"
    return verdict



def nomination_text(evidence, verdict) -> str:
    idea = verdict["idea"]
    facts = "; ".join(evidence.hard_facts[:3]) or "no hard figures stated"
    return (
        f":mag: *DD nomination — {evidence.ticker}* (from a link you dropped in #dd)\n"
        f"Best same-day idea after a *fresh* market brief: *{idea.direction} {idea.structure}*, "
        f"~{idea.target_dte} DTE, conviction *{idea.conviction}/10* "
        f"(same-day bar {verdict['min_conviction']:.0f}).\n"
        f"_Thesis:_ {str(getattr(idea, 'thesis', '') or '')[:300]}\n"
        f"_Article claim ({evidence.source_kind}, {evidence.time_sensitivity}"
        f"{', already priced in' if evidence.already_priced_in else ''}):_ "
        f"{evidence.claim_summary}\n"
        f"_Stated figures:_ {facts}\n"
        f"_Source:_ {evidence.source_url}\n"
        f":lock: *Nothing is pending and no order exists.* This is a nomination only — the article "
        f"was machine-summarised into typed fields and never entered the trading prompt verbatim. "
        f"To act, run the normal entry path so the live one-tap approval and its watcher are "
        f"actually running:\n"
        f"`~/ib-grader-venv/bin/python ~/exitmgr-app/daily_recommend.py --watch-mins 60`"
    )



async def process(args) -> int:
    cfg = yaml.safe_load(open(args.config)) or {}
    ibc, tr = cfg.get("ib", {}) or {}, cfg.get("trading", {}) or {}
    audit_path = tr.get("audit_path", "./audit.jsonl")
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    channel = tr.get("slack_channel", "")



    dr.CASH_BUFFER_PCT = float(tr.get("cash_buffer_pct", 0.05))
    dr.CONS = construction_from_dict(cfg.get("construction"))
    dr._RISK_LIMITS = entry_safety.risk_limits_from_config(tr)
    dr.JOURNAL_PATH = (cfg.get("journal", {}) or {}).get("path", "./trades.log")
    dr.ERROR_CHANNEL = tr.get("error_channel", "") or tr.get("alerts_channel", "")

    armed = bool(args.arm)
    mode = "ARMED (may post to Slack)" if armed else "DRY RUN (posts nothing, writes no state)"
    print(f"[dd_consider] {_now_iso()} — {mode}")
    print(f"[dd_consider] model {tr.get('llm_model')} @ {tr.get('llm_endpoint')}")

    if armed:

        markers = entry_safety.entry_markers_clear(
            config_path=args.config,
            kill_switch_path=(cfg.get("kill_switch") or {}).get("path"))
        if not markers.allowed:
            print("[BLOCKED] entry stand-down active: " + "; ".join(markers.reasons))
            return 2
        if not token or not channel:
            print("[BLOCKED] --arm needs SLACK_BOT_TOKEN and trading.slack_channel")
            return 2

    market_open = _market_open() if not args.assume_market_open else True
    print(f"[dd_consider] market_open={market_open}"
          + (" (forced by --assume-market-open)" if args.assume_market_open else ""))

    if args.fixture:
        items = json.loads(Path(args.fixture).read_text())
        items = items if isinstance(items, list) else [items]
        print(f"[dd_consider] FIXTURE MODE — {len(items)} synthetic item(s) from {args.fixture}")
    else:
        items = load_items(args.items)
        print(f"[dd_consider] {len(items)} captured item(s) in {args.items}")

    state = load_state(args.state)
    todo = []
    for item in items:
        key = item_key(item)
        if not key:
            print(f"[SKIP] item at line {item.get('_lineno')} has no capture_id/slack_ts")
            continue
        if args.only and key != args.only and (item.get("tickers") or [None])[0] != args.only:
            continue
        prior = (state.get("items") or {}).get(key)
        if prior and prior.get("status") in TERMINAL_STATUSES and not args.reprocess:
            print(f"[SKIP] {key} already processed: {prior.get('status')} "
                  f"({prior.get('reason', '')})")
            continue
        if not (item.get("tickers") or []):
            print(f"[SKIP] {key} has no ticker yet (dd_watcher marks it needs_ticker)")
            state.setdefault("items", {})[key] = {
                "status": "no_ticker", "decided_at": _now_iso(), "reason": "no ticker extracted"}
            continue
        todo.append(item)

    if not todo:
        print("[dd_consider] nothing to consider.")
        if armed:
            save_state(args.state, state)
        return 0

    todo = todo[:args.limit] if args.limit else todo



    conn = None
    ib = None
    exit_code = 0
    try:
        for item in todo:
            key = item_key(item)
            ticker = (item.get("tickers") or [None])[0]
            _hr(f"ITEM {key} — {ticker} — captured {item.get('captured_at')}")

            record = {"ticker": ticker, "decided_at": _now_iso(), "capture_id": key,
                      "slack_ts": item.get("slack_ts")}


            print("STAGE 1 — fetch + sanitising extraction (untrusted article -> typed fields)")
            try:
                evidence, info = run_stage1(item, tr, args)
            except DDEvidenceError as exc:
                print(f"  [REJECTED] {exc}")
                print("  -> Stage 2 will NOT run for this item. Nothing from the article crosses.")
                record.update({"status": "stage1_rejected", "reason": str(exc)})
                state.setdefault("items", {})[key] = record
                if armed:
                    audit(audit_path, "dd_stage1_rejected", capture_id=key, ticker=ticker,
                          reason=str(exc))
                continue
            except Exception as exc:
                print(f"  [ERROR] {type(exc).__name__}: {exc}")
                record.update({"status": "error", "reason": f"{type(exc).__name__}: {exc}"})
                state.setdefault("items", {})[key] = record
                exit_code = 1
                continue

            print("  STAGE 1 TYPED JSON (the ONLY thing that may cross the boundary):")
            print("  " + json.dumps(evidence.as_dict(), indent=2).replace("\n", "\n  "))
            if args.show_cot and info["debug"].get("cot"):
                print("  [stage1 cot] " + str(info["debug"]["cot"])[:2000])
            record["evidence"] = evidence.as_dict()

            if not evidence.tradeable_thesis:
                print("  -> tradeable_thesis=false: nothing dated/checkable here. No Stage 2.")
                record.update({"status": "no_tradeable_thesis",
                               "reason": "Stage 1 found no specific, dated, checkable claim"})
                state.setdefault("items", {})[key] = record
                if armed:
                    audit(audit_path, "dd_no_thesis", capture_id=key, ticker=ticker)
                continue


            if evidence.time_sensitivity == "not_time_sensitive" and not args.force_stage2:
                print("  -> time_sensitivity=not_time_sensitive: no same-day case. "
                      "It can ride the ordinary daily slate.")
                record.update({"status": "not_time_sensitive",
                               "reason": "no same-day urgency; deferred to the daily slate"})
                state.setdefault("items", {})[key] = record
                continue
            if not market_open and not args.force_stage2:
                print("  -> market CLOSED. Queued for the next session; nothing is posted "
                      "overnight (a stale-book proposal would expire before you saw it).")
                record.update({"status": "queued_next_session",
                               "reason": "market closed at consideration time"})
                state.setdefault("items", {})[key] = record
                if armed:
                    audit(audit_path, "dd_queued_next_session", capture_id=key, ticker=ticker,
                          time_sensitivity=evidence.time_sensitivity)
                continue
            if not market_open and args.force_stage2:
                print("  [NOTE] market is CLOSED but --force-stage2 was passed: running Stage 2 "
                      "for inspection. Quotes are stale; NOTHING will be posted.")


            print("\nSTAGE 2 — audited same-day add-name route (fresh brief + typed evidence)")
            if conn is None:
                conn = IBConnection(host=ibc.get("host", "127.0.0.1"),
                                    port=ibc.get("port", 4001),
                                    client_id=args.client_id,
                                    market_data_type=ibc.get("market_data_type", 1))
                if not await conn.connect(retries=3, retry_delay=10):
                    print("  [ERROR] no IBKR connection; cannot build a fresh brief. "
                          "NOT restarting the gateway (that is an operator's call).")
                    record.update({"status": "queued_next_session",
                                   "reason": "IBKR gateway unreachable"})
                    state.setdefault("items", {})[key] = record
                    exit_code = 1
                    break
                ib = conn.ib
                dr.CONN = conn

            try:
                verdict = await run_stage2(ib, cfg, tr, evidence, args, audit_path)
            except Exception as exc:
                print(f"  [ERROR] Stage 2 failed: {type(exc).__name__}: {exc}")
                record.update({"status": "error", "reason": f"stage2: {type(exc).__name__}: {exc}"})
                state.setdefault("items", {})[key] = record
                exit_code = 1
                continue

            status, reason = verdict["status"], verdict.get("reason", "")
            print(f"\n  STAGE 2 OUTCOME: {status} — {reason}")
            record.update({"status": status if status != "nominate" else "nominated",
                           "reason": reason,
                           "conviction": verdict.get("conviction"),
                           "direction": verdict.get("direction"),
                           "structure": verdict.get("structure")})

            if status != "nominate":
                state.setdefault("items", {})[key] = record
                if armed and status in ("stage2_below_bar", "stage2_no_intent", "stage2_declined"):
                    audit(audit_path, "dd_stage2_pass", capture_id=key, ticker=ticker,
                          status=status, reason=reason, conviction=verdict.get("conviction"))
                continue

            text = nomination_text(evidence, verdict)
            if not armed:
                print("\n  WOULD POST to Slack "
                      f"{channel or '(trading.slack_channel unset)'}:\n")
                print("  " + text.replace("\n", "\n  "))
                print("\n  (dry run — nothing posted, no state written)")
                continue
            ts = approval.post_proposal(token, channel, text)
            print(f"  posted nomination to {channel} (ts={ts})")
            record["slack_post_ts"] = ts
            audit(audit_path, "dd_nominated", capture_id=key, ticker=ticker,
                  conviction=verdict.get("conviction"), direction=verdict.get("direction"),
                  structure=verdict.get("structure"), source_url=evidence.source_url,
                  slack_ts=ts)
            state.setdefault("items", {})[key] = record
    finally:
        if conn is not None:
            try:
                await conn.disconnect()
            except Exception as exc:
                print(f"[WARN] IBKR disconnect: {exc}")

    if armed:
        save_state(args.state, state)
        print(f"\n[dd_consider] state written to {args.state}")
    else:
        print("\n[dd_consider] DRY RUN complete — no Slack post, no state write, no orders.")
    return exit_code


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Consume Slack #dd research captures; nominate names for the audited entry path.")
    ap.add_argument("--arm", action="store_true",
                    help="allow Slack posts and state writes (DEFAULT IS DRY RUN)")
    ap.add_argument("--config", default=os.path.join(_APP_DIR, "config.yaml"))
    ap.add_argument("--items", default=ITEMS_PATH)
    ap.add_argument("--state", default=STATE_PATH)
    ap.add_argument("--fixture", default=None,
                    help="TEST ONLY: JSON file of synthetic item(s) with _local_article_html")
    ap.add_argument("--only", default=None, help="process only this capture_id or ticker")
    ap.add_argument("--limit", type=int, default=0, help="max items this run (0 = all)")
    ap.add_argument("--reprocess", action="store_true",
                    help="ignore the processed-state sidecar (re-run items already decided)")
    ap.add_argument("--client-id", type=int, default=CLIENT_ID, dest="client_id",
                    help=f"IBKR clientId (default {CLIENT_ID}; band 960-990 reserved for DD)")
    ap.add_argument("--fetch-timeout", type=int, default=dd_evidence.FETCH_TIMEOUT_S)
    ap.add_argument("--stage1-timeout", type=int, default=300)
    ap.add_argument("--stage2-timeout", type=int, default=400)
    ap.add_argument("--thinking", default="enabled", choices=["enabled", "disabled", "adaptive"])
    ap.add_argument("--force-stage2", action="store_true",
                    help="run Stage 2 even when the market is closed / the item is not time "
                         "sensitive (inspection only; an armed run still refuses to post)")
    ap.add_argument("--assume-market-open", action="store_true",
                    help="treat the market as open (testing the timing branch only)")
    ap.add_argument("--stage2-stop-before-model", action="store_true",
                    help="build the fresh brief + evidence block, then stop before Stage A")
    ap.add_argument("--dump-prompt", default=None,
                    help="write the exact Stage 2 user prompt to this path (review aid)")
    ap.add_argument("--show-article", action="store_true", help="print extracted article text")
    ap.add_argument("--show-cot", action="store_true", help="print the Stage 1 reasoning trace")
    args = ap.parse_args()



    if args.arm and (args.force_stage2 or args.assume_market_open or args.fixture):
        print("[REFUSED] --arm cannot be combined with --force-stage2 / --assume-market-open / "
              "--fixture. Those are inspection modes; arming them would post off a bypassed gate.")
        return 2
    return asyncio.run(process(args))


if __name__ == "__main__":
    raise SystemExit(main())
