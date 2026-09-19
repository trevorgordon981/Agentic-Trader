#!/usr/bin/env python3
"""Public API contract; production-derived narrative omitted."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

RUNNER_VERSION = "shadow_decide.v1"






FORBIDDEN_MODULES = (
    "exitmgr.trader", "exitmgr.order", "exitmgr.manager", "exitmgr.approval",
    "exitmgr.entry_builder", "exitmgr.position_manager", "exitmgr.order_lock",
    "place_trade", "daily_recommend", "liquidate", "close_symbol",
)


def assert_observer_only() -> None:
    leaked = [m for m in FORBIDDEN_MODULES if m in sys.modules]
    if leaked:
        raise SystemExit("[shadow] REFUSING TO RUN: execution-capable modules imported: %s"
                         % ", ".join(leaked))


from exitmgr import provenance, regime as regime_mod, research, slate_lock
from exitmgr import shadow
from exitmgr.account import get_pot_snapshot
from exitmgr.config import load_config
from exitmgr.connection import IBConnection
from exitmgr.market import fetch_universe_quotes
from exitmgr.risk import INDEX_UNDERLYINGS, OpenPosition, day_pnl_pct
from exitmgr.strategist import propose_intents

assert_observer_only()




def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _trading_day_et(now=None) -> str:
    """Public API contract; production-derived narrative omitted."""
    try:
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        n = now or datetime.now(et)
        if getattr(n, "tzinfo", None) is None:
            n = n.replace(tzinfo=timezone.utc)
        return str(n.astimezone(et).date())
    except Exception:
        return str(datetime.now(timezone.utc).date())


def _market_open() -> bool:
    """Public API contract; production-derived narrative omitted."""
    t = datetime.now(timezone.utc)
    if t.weekday() >= 5:
        return False
    mins = t.hour * 60 + t.minute
    return 13 * 60 + 30 <= mins <= 20 * 60


def _log(msg: str) -> None:
    print("[shadow %s] %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)




def _journal_debits(journal_path: str) -> Dict[int, float]:
    """Public API contract; production-derived narrative omitted."""
    debits: Dict[int, float] = {}
    p = Path(os.path.expanduser(journal_path))
    if not p.exists():
        return debits
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        cid, debit = rec.get("contract_id"), rec.get("debit")
        if cid is None or debit is None:
            continue
        try:
            debits[int(cid)] = float(debit)
        except (TypeError, ValueError):
            continue
    return debits


async def _open_positions(ib_conn: IBConnection, journal_path: str) -> List[OpenPosition]:
    """Public API contract; production-derived narrative omitted."""
    raw = await ib_conn.get_positions()
    debits = _journal_debits(journal_path)
    out: List[OpenPosition] = []
    for pd in raw.values():
        symbol = pd.symbol.upper()
        con_id = getattr(pd, "con_id", None)
        gross = abs(pd.avg_cost) * 100 * abs(pd.quantity)
        net_debit = debits.get(int(con_id)) if con_id is not None else None
        out.append(OpenPosition(symbol, float(net_debit) if net_debit is not None else gross,
                                symbol in INDEX_UNDERLYINGS))
    return out


def _day_baseline(baseline_path: str, day: str) -> Optional[float]:
    """Public API contract; production-derived narrative omitted."""
    try:
        with open(os.path.expanduser(baseline_path)) as fh:
            data = json.load(fh)
        value = float(data.get(day))
        return value if value > 0 else None
    except Exception:
        return None


async def build_brief(cfg, config_path: str, client_id: int) -> Dict[str, Any]:
    """Public API contract; production-derived narrative omitted."""
    approved = {str(s).upper() for s in getattr(cfg, "approved_names", [])}
    names = sorted({"SPY", "QQQ", "IWM"} | approved)
    today = str(datetime.now(timezone.utc).date())

    ib_conn = IBConnection(host=cfg.ib.host, port=cfg.ib.port, client_id=client_id,
                           market_data_type=getattr(cfg.ib, "market_data_type", 3))
    if not await ib_conn.connect(retries=2, retry_delay=10):
        raise RuntimeError("could not connect to IBKR at %s:%s (clientId=%s)"
                           % (cfg.ib.host, cfg.ib.port, client_id))
    try:
        pot = await get_pot_snapshot(ib_conn.ib)
        positions = await _open_positions(ib_conn, cfg.journal.path)
        baseline = _day_baseline(getattr(cfg, "baseline_path", "./day_baseline.json"),
                                 _trading_day_et())
        dp = day_pnl_pct(pot.net_liq, baseline) if baseline else None

        try:
            quotes = await fetch_universe_quotes(ib_conn.ib, names)
        except Exception as exc:
            _log("quote fetch failed: %s" % exc)
            quotes = {}

        single_names = sorted(approved | {p.underlying for p in positions if not p.is_index})
        data = await research.gather(ib_conn.ib, names, single_names=single_names)
        ps = data.get("price_stats") or {}
        regime_info = regime_mod.classify_regime(
            [ps.get("SPY"), ps.get("QQQ"), ps.get("IWM")], data.get("vix"))
        brief = research.build_brief(
            today=today, quotes=quotes, universe=names,
            allow_any_name=bool(getattr(cfg, "allow_model_names", False)),
            book=positions, day_pnl_pct=dp,
            net_liq=pot.net_liq, available_funds=pot.available_funds, **data)
    finally:
        try:
            await ib_conn.disconnect()
        except Exception:
            pass

    return {
        "brief": brief,
        "brief_sha256": provenance.sha256(brief),
        "regime": regime_info,
        "account": {"net_liq": pot.net_liq, "available_funds": pot.available_funds,
                    "day_start_baseline": baseline, "day_pnl_pct": dp},
        "book": [{"underlying": p.underlying, "notional": p.notional, "is_index": p.is_index}
                 for p in positions],
        "universe": names,
        "config_path": os.path.abspath(config_path),
    }




class ArmSpec:
    """Public API contract; production-derived narrative omitted."""

    def __init__(self, arm: str, role: str, label: str, endpoint: str, model: str,
                 thinking: str, prompt: str, structured: bool, priority: int, timeout: int):
        self.arm, self.role, self.label = arm, role, label
        self.endpoint, self.model = endpoint, model
        self.thinking, self.prompt = thinking, prompt
        self.structured, self.priority, self.timeout = structured, priority, timeout

    @property
    def recommend(self) -> bool:
        return self.prompt == "stage_a_recommend"

    def describe(self) -> str:
        return ("%s[%s] %s @ %s thinking=%s prompt=%s guided=%s"
                % (self.arm, self.role, self.model, self.endpoint, self.thinking,
                   self.prompt, self.structured))


def run_arm(spec: ArmSpec, brief: str) -> shadow.ArmResult:
    """Public API contract; production-derived narrative omitted."""
    os.environ["TRADER_STRUCTURED_OUTPUT"] = "1" if spec.structured else "0"
    os.environ["TRADER_LLM_PRIORITY"] = str(spec.priority)

    result = shadow.ArmResult(
        arm=spec.arm, role=spec.role, label=spec.label, endpoint=spec.endpoint, model=spec.model,
        thinking=spec.thinking, prompt=spec.prompt, structured_output=spec.structured,
        priority=spec.priority, started_at=_now_iso())
    t0 = time.monotonic()
    try:
        res = propose_intents(spec.endpoint, spec.model, brief, timeout=spec.timeout,
                              recommend=spec.recommend, thinking=spec.thinking,
                              return_cot=True, return_identity=True)
        intents, raw, cot, identity = res
        result.ok = True
        result.intents = [i.to_dict() for i in intents]
        result.n_intents = len(result.intents)
        result.traded = bool(result.intents)
        result.raw_response = raw
        result.cot = cot
        result.identity = identity
    except Exception as exc:
        result.ok = False
        result.error = str(exc)
        result.error_type = type(exc).__name__
    finally:
        result.latency_s = round(time.monotonic() - t0, 3)
        result.finished_at = _now_iso()
    return result


def wait_for_slate(max_wait_s: int, poll_s: int = 15) -> bool:
    """Public API contract; production-derived narrative omitted."""
    if not slate_lock.slate_active():
        return True
    waited = 0
    _log("daily slate is generating -- waiting up to %ds rather than queueing behind it" % max_wait_s)
    while waited < max_wait_s:
        time.sleep(poll_s)
        waited += poll_s
        if not slate_lock.slate_active():
            _log("slate clear after %ds" % waited)
            return True
    return False




async def one_cycle(cfg, args, arm_a: ArmSpec, arm_b: ArmSpec, cycle_index: int) -> Optional[str]:
    if args.require_market_open and not _market_open():
        _log("market closed and --require-market-open set: skipping cycle")
        return None

    _log("building production brief (IBKR clientId=%d, read-only)" % args.client_id)
    ctx = await build_brief(cfg, args.config, args.client_id)
    _log("brief built: %d bytes  sha256=%s  regime=%s"
         % (len(ctx["brief"]), ctx["brief_sha256"][:16], ctx["regime"]))

    if not wait_for_slate(args.slate_wait_s):
        _log("slate still active after %ds: skipping this cycle (no contention)" % args.slate_wait_s)
        return None



    order = [arm_a, arm_b]
    if args.alternate and cycle_index % 2 == 1:
        order = [arm_b, arm_a]

    results: Dict[str, shadow.ArmResult] = {}
    for spec in order:
        _log("running arm %s" % spec.describe())
        results[spec.arm] = run_arm(spec, ctx["brief"])
        r = results[spec.arm]
        _log("  arm %s: ok=%s intents=%s latency=%.1fs%s"
             % (spec.arm, r.ok, r.n_intents, r.latency_s or 0.0,
                (" error=%s" % r.error_type) if not r.ok else ""))

    record = shadow.PairedDecision(
        record_id=str(uuid.uuid4()),
        ts=_now_iso(),
        trading_day_et=_trading_day_et(),
        market_open=_market_open(),
        host=socket.gethostname(),
        app_dir=APP_DIR,
        config_path=ctx["config_path"],
        brief=ctx["brief"],
        brief_sha256=ctx["brief_sha256"],
        regime=ctx["regime"],
        account=ctx["account"],
        book=ctx["book"],
        universe=ctx["universe"],
        arms={"A": results["A"].to_dict(), "B": results["B"].to_dict()},
        arm_order=[s.arm for s in order],
        runner={"version": RUNNER_VERSION, "python": sys.version.split()[0],
                "executable": sys.executable, "argv": sys.argv,
                "observer": "no order/approval/Slack surface exists in this program"},
    ).to_dict()

    path = shadow.append_record(args.out, record)
    _log("record %s written to %s" % (record["record_id"][:8], path))
    return record["record_id"]


def build_arms(cfg, args):
    prod_endpoint = getattr(cfg, "llm_endpoint", "")
    prod_model = getattr(cfg, "llm_model", "")
    if not prod_endpoint or not prod_model:
        raise SystemExit("[shadow] config has no trading.llm_endpoint/llm_model to shadow")


    arm_a = ArmSpec("A", "production", args.a_label or ("prod:%s" % prod_model),
                    prod_endpoint, prod_model, thinking=args.a_thinking, prompt=args.a_prompt,
                    structured=(args.a_structured == "on"), priority=args.priority,
                    timeout=args.timeout)



    b_endpoint = args.b_endpoint or prod_endpoint
    b_model = args.b_model or (prod_model if not args.b_endpoint else "")
    if args.b_endpoint and not args.b_model:
        raise SystemExit("[shadow] --b-endpoint requires --b-model")
    b_thinking = args.b_thinking or ("enabled" if (args.b_endpoint or args.b_model)
                                     else "disabled")
    b_prompt = args.b_prompt or args.a_prompt
    b_structured = (args.b_structured or args.a_structured) == "on"

    if args.b_label:
        label = args.b_label
    elif args.b_endpoint or args.b_model:
        label = "variant:%s" % b_model
    else:
        bits = []
        if b_thinking != args.a_thinking:
            bits.append("thinking_%s" % b_thinking)
        if b_prompt != args.a_prompt:
            bits.append(b_prompt)
        if b_structured != (args.a_structured == "on"):
            bits.append("guided_%s" % ("on" if b_structured else "off"))
        label = "flagvar:" + ("+".join(bits) if bits else "identical_to_A")

    arm_b = ArmSpec("B", "variant", label, b_endpoint, b_model, thinking=b_thinking,
                    prompt=b_prompt, structured=b_structured, priority=args.priority,
                    timeout=args.timeout)
    return arm_a, arm_b


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Paired forward-decision observer. Records two arms' decisions over one brief. "
                    "It cannot trade.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=os.path.join(APP_DIR, "config.yaml"),
                   help="production config to read endpoint/model/universe from (never written)")
    p.add_argument("--out", default=shadow.DEFAULT_LOG, help="paired-decision log (JSONL)")
    p.add_argument("--cycles", type=int, default=1,
                   help="paired decisions to record this run (default 1: conservative)")
    p.add_argument("--interval", type=int, default=3600,
                   help="seconds between cycles (default 3600; two generations per cycle is real "
                        "load on a single-generation endpoint)")
    p.add_argument("--client-id", type=int, default=947,
                   help="IBKR clientId, reserved band 940-959 (trader=1, protective=189, "
                        "dd_consider=972)")
    p.add_argument("--timeout", type=int, default=300, help="per-generation timeout (production 300)")
    p.add_argument("--priority", type=int, default=1,
                   help="X-M3-Priority class; 1 = NON-urgent so the armed trader always wins")
    p.add_argument("--slate-wait-s", type=int, default=300,
                   help="max seconds to wait out an active daily slate before skipping the cycle")
    p.add_argument("--require-market-open", action="store_true",
                   help="only decide during the US regular session")
    p.add_argument("--alternate", action="store_true",
                   help="alternate which arm generates first across cycles")

    p.add_argument("--a-thinking", default="enabled", choices=["enabled", "disabled", "adaptive"])
    p.add_argument("--a-prompt", default="stage_a_entry",
                   choices=["stage_a_entry", "stage_a_recommend"])
    p.add_argument("--a-structured", default="on", choices=["on", "off"],
                   help="guided decoding for arm A (production runs TRADER_STRUCTURED_OUTPUT=1)")
    p.add_argument("--a-label", default=None)

    p.add_argument("--b-endpoint", default=None, help="different endpoint for arm B")
    p.add_argument("--b-model", default=None, help="different model id for arm B")
    p.add_argument("--b-thinking", default=None, choices=["enabled", "disabled", "adaptive"])
    p.add_argument("--b-prompt", default=None, choices=["stage_a_entry", "stage_a_recommend"])
    p.add_argument("--b-structured", default=None, choices=["on", "off"])
    p.add_argument("--b-label", default=None)

    p.add_argument("--score", action="store_true", help="score the existing log and exit")
    p.add_argument("--score-json", action="store_true", help="with --score, emit raw JSON")
    p.add_argument("--since", default=None, help="with --score, only records with ts >= this prefix")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    assert_observer_only()

    if args.score:
        records = shadow.load_records(args.out)
        if args.since:
            records = [r for r in records if str(r.get("ts", "")) >= args.since]
        stats = shadow.score(records)
        print(json.dumps(stats, indent=2, default=str) if args.score_json
              else shadow.format_score(stats))
        return 0




    if os.environ.pop("SLATE_THINKING", None) is not None:
        _log("dropped inherited SLATE_THINKING so it cannot override either arm")

    if not 940 <= args.client_id <= 959:
        raise SystemExit("[shadow] --client-id must be in the reserved 940-959 band")

    cfg = load_config(config_path=args.config)
    arm_a, arm_b = build_arms(cfg, args)
    _log("ARM A  %s" % arm_a.describe())
    _log("ARM B  %s" % arm_b.describe())
    _log("log: %s | cycles=%d interval=%ds" % (args.out, args.cycles, args.interval))

    written = 0
    for i in range(args.cycles):
        if i:
            _log("sleeping %ds before cycle %d/%d" % (args.interval, i + 1, args.cycles))
            time.sleep(args.interval)
        try:
            if asyncio.run(one_cycle(cfg, args, arm_a, arm_b, i)):
                written += 1
        except KeyboardInterrupt:
            _log("interrupted")
            break
        except Exception as exc:
            _log("cycle %d failed: %s: %s" % (i + 1, type(exc).__name__, exc))
    _log("done: %d/%d paired decisions recorded" % (written, args.cycles))
    return 0


if __name__ == "__main__":
    sys.exit(main())
