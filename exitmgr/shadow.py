"""Public API contract; production-derived narrative omitted."""
from __future__ import annotations

import json
import os
import statistics
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

SCHEMA = "shadow_paired_decision.v1"



DEFAULT_LOG = (os.environ.get("EXITMGR_SHADOW_LOG")
               or os.path.expanduser("~/trade-capture/shadow/decisions.jsonl"))
DEFAULT_EXITS_LOG = (os.environ.get("EXITMGR_EXITS_LOG")
                     or os.path.expanduser("~/exitmgr-app/exits.log"))




@dataclass
class ArmResult:
    """Public API contract; production-derived narrative omitted."""
    arm: str
    role: str
    label: str
    endpoint: str
    model: str
    thinking: str
    prompt: str
    structured_output: bool
    priority: int
    started_at: str = ""
    finished_at: str = ""
    latency_s: Optional[float] = None
    ok: bool = False
    error: Optional[str] = None
    error_type: Optional[str] = None
    n_intents: Optional[int] = None
    traded: Optional[bool] = None
    intents: Optional[List[Dict[str, Any]]] = None
    raw_response: Optional[str] = None
    cot: Optional[str] = None
    identity: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PairedDecision:
    record_id: str
    ts: str
    trading_day_et: str
    market_open: bool
    host: str
    app_dir: str
    config_path: str
    brief: str
    brief_sha256: str
    regime: Optional[Dict[str, Any]]
    account: Dict[str, Any]
    book: List[Dict[str, Any]]
    universe: List[str]
    arms: Dict[str, Dict[str, Any]]
    arm_order: List[str]
    runner: Dict[str, Any] = field(default_factory=dict)
    schema: str = SCHEMA

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["schema"] = self.schema
        return d


def append_record(path: str, record: Dict[str, Any]) -> str:
    """Public API contract; production-derived narrative omitted."""
    path = os.path.expanduser(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())
    return path


def load_records(path: str = DEFAULT_LOG) -> List[Dict[str, Any]]:
    """Public API contract; production-derived narrative omitted."""
    path = os.path.expanduser(path)
    out: List[Dict[str, Any]] = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict) and rec.get("schema") == SCHEMA:
                out.append(rec)
    return out




def _arm(rec: Dict[str, Any], which: str) -> Dict[str, Any]:
    return (rec.get("arms") or {}).get(which) or {}


def intent_key(intent: Dict[str, Any]) -> Tuple[str, str]:
    """Public API contract; production-derived narrative omitted."""
    return (str(intent.get("underlying", "")).upper(), str(intent.get("direction", "")).lower())


def dte_multiple(intent: Dict[str, Any]) -> Optional[float]:
    """Public API contract; production-derived narrative omitted."""
    try:
        dte = float(intent.get("target_dte"))
        hold = float(intent.get("intended_hold_days"))
    except (TypeError, ValueError):
        return None
    if hold <= 0:
        return None
    return dte / hold


def summarize_intent(intent: Dict[str, Any]) -> str:
    mult = dte_multiple(intent)
    return ("{u} {d} {s} dte={dte} hold={h} ({m}) delta={dl} conv={c} alloc={a}%".format(
        u=intent.get("underlying"), d=intent.get("direction"), s=intent.get("structure"),
        dte=intent.get("target_dte"), h=intent.get("intended_hold_days"),
        m=("%.1fx" % mult) if mult is not None else "?x",
        dl=intent.get("target_delta"), c=intent.get("conviction"),
        a=intent.get("allocation_pct_net_liq")))


def _mean(values: List[float]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return round(statistics.fmean(vals), 3) if vals else None


def _arm_stats(arms: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Public API contract; production-derived narrative omitted."""
    n = len(arms)
    traded = [a for a in arms if a.get("traded")]
    intents = [i for a in traded for i in (a.get("intents") or [])]
    convs, mults, dtes, holds, allocs = [], [], [], [], []
    for i in intents:
        try:
            convs.append(float(i.get("conviction")))
        except (TypeError, ValueError):
            pass
        m = dte_multiple(i)
        if m is not None:
            mults.append(m)
        for key, bucket in (("target_dte", dtes), ("intended_hold_days", holds),
                            ("allocation_pct_net_liq", allocs)):
            try:
                bucket.append(float(i.get(key)))
            except (TypeError, ValueError):
                pass
    structures: Dict[str, int] = {}
    directions: Dict[str, int] = {}
    names: Dict[str, int] = {}
    for i in intents:
        structures[str(i.get("structure"))] = structures.get(str(i.get("structure")), 0) + 1
        directions[str(i.get("direction"))] = directions.get(str(i.get("direction")), 0) + 1
        names[str(i.get("underlying"))] = names.get(str(i.get("underlying")), 0) + 1
    return {
        "decisions": n,
        "traded_decisions": len(traded),
        "trade_rate": round(len(traded) / n, 3) if n else None,
        "intents": len(intents),
        "mean_conviction": _mean(convs),
        "mean_dte_multiple": _mean(mults),
        "mean_target_dte": _mean(dtes),
        "mean_intended_hold_days": _mean(holds),
        "mean_allocation_pct_net_liq": _mean(allocs),
        "mean_latency_s": _mean([a.get("latency_s") for a in arms]),
        "structures": structures,
        "directions": directions,
        "names": names,
    }


def score(records: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Public API contract; production-derived narrative omitted."""
    records = list(records)
    scorable, errored = [], []
    for rec in records:
        a, b = _arm(rec, "A"), _arm(rec, "B")
        (scorable if (a.get("ok") and b.get("ok")) else errored).append(rec)

    both_traded_same, both_traded_diff, both_passed, disagreed = [], [], [], []
    for rec in scorable:
        a, b = _arm(rec, "A"), _arm(rec, "B")
        at, bt = bool(a.get("traded")), bool(b.get("traded"))
        if at and bt:
            ka = {intent_key(i) for i in (a.get("intents") or [])}
            kb = {intent_key(i) for i in (b.get("intents") or [])}
            (both_traded_same if ka == kb else both_traded_diff).append(rec)
        elif not at and not bt:
            both_passed.append(rec)
        else:
            disagreed.append(rec)

    n = len(scorable)
    agree_n = len(both_traded_same) + len(both_passed)
    pairings: Dict[str, int] = {}
    for rec in records:
        key = "{} vs {}".format(_arm(rec, "A").get("label"), _arm(rec, "B").get("label"))
        pairings[key] = pairings.get(key, 0) + 1

    return {
        "records": len(records),
        "scorable": n,
        "errored": len(errored),
        "error_detail": [
            {"ts": r.get("ts"),
             "A": _arm(r, "A").get("error_type") if not _arm(r, "A").get("ok") else None,
             "B": _arm(r, "B").get("error_type") if not _arm(r, "B").get("ok") else None}
            for r in errored],
        "pairings": pairings,
        "agreement": {
            "both_traded_same_names": len(both_traded_same),
            "both_traded_different_names": len(both_traded_diff),
            "both_passed": len(both_passed),
            "disagreed_trade_vs_pass": len(disagreed),
            "agreement_rate": round(agree_n / n, 3) if n else None,
            "trade_pass_disagreement_rate": round(len(disagreed) / n, 3) if n else None,
        },
        "arms": {
            "A": _arm_stats([_arm(r, "A") for r in scorable]),
            "B": _arm_stats([_arm(r, "B") for r in scorable]),
        },
        "disagreements": [_disagreement_case(r) for r in (disagreed + both_traded_diff)],
        "outcome_join": "NOT COMPUTED -- see exitmgr.shadow.join_outcomes",
    }


def _disagreement_case(rec: Dict[str, Any]) -> Dict[str, Any]:
    a, b = _arm(rec, "A"), _arm(rec, "B")
    return {
        "ts": rec.get("ts"),
        "record_id": rec.get("record_id"),
        "brief_sha256": rec.get("brief_sha256"),
        "regime": (rec.get("regime") or {}).get("regime"),
        "A": {"label": a.get("label"),
              "decision": [summarize_intent(i) for i in (a.get("intents") or [])] or ["PASS"]},
        "B": {"label": b.get("label"),
              "decision": [summarize_intent(i) for i in (b.get("intents") or [])] or ["PASS"]},
    }


def format_score(stats: Dict[str, Any]) -> str:
    """Public API contract; production-derived narrative omitted."""
    L: List[str] = []
    L.append("SHADOW PAIRED-DECISION SCORE  (%s)" % SCHEMA)
    L.append("=" * 78)
    L.append("records=%s  scorable=%s  errored=%s"
             % (stats["records"], stats["scorable"], stats["errored"]))
    for pair, count in stats["pairings"].items():
        L.append("  pairing: %s  (n=%d)" % (pair, count))
    for err in stats["error_detail"]:
        L.append("  ERROR  %s  A=%s  B=%s" % (err["ts"], err["A"], err["B"]))
    ag = stats["agreement"]
    L.append("")
    L.append("AGREEMENT (n=%s scorable)" % stats["scorable"])
    L.append("  both traded, same name+direction : %s" % ag["both_traded_same_names"])
    L.append("  both traded, different names     : %s" % ag["both_traded_different_names"])
    L.append("  both passed                      : %s" % ag["both_passed"])
    L.append("  disagreed (one traded, one passed): %s" % ag["disagreed_trade_vs_pass"])
    L.append("  agreement rate                   : %s" % ag["agreement_rate"])
    L.append("")
    L.append("PER-ARM BEHAVIOUR")
    header = "  %-26s %10s %10s" % ("", "A", "B")
    L.append(header)
    rows = [("trade rate", "trade_rate"), ("traded decisions", "traded_decisions"),
            ("intents emitted", "intents"), ("mean conviction", "mean_conviction"),
            ("mean DTE multiple", "mean_dte_multiple"), ("mean target_dte", "mean_target_dte"),
            ("mean intended_hold_days", "mean_intended_hold_days"),
            ("mean alloc %netliq", "mean_allocation_pct_net_liq"),
            ("mean latency (s)", "mean_latency_s")]
    for name, key in rows:
        L.append("  %-26s %10s %10s" % (name, stats["arms"]["A"][key], stats["arms"]["B"][key]))
    for side in ("A", "B"):
        s = stats["arms"][side]
        L.append("  %s structures=%s directions=%s names=%s"
                 % (side, s["structures"], s["directions"], s["names"]))
    L.append("")
    L.append("DISAGREEMENTS (%d) -- the interesting set" % len(stats["disagreements"]))
    if not stats["disagreements"]:
        L.append("  (none)")
    for case in stats["disagreements"]:
        L.append("  %s  brief=%s  regime=%s" % (case["ts"], (case["brief_sha256"] or "")[:12],
                                                case["regime"]))
        L.append("    A [%s]: %s" % (case["A"]["label"], "; ".join(case["A"]["decision"])))
        L.append("    B [%s]: %s" % (case["B"]["label"], "; ".join(case["B"]["decision"])))
    L.append("")
    L.append("P&L: NOT COMPUTED. Outcomes are not joinable to these records yet -- "
             "see exitmgr.shadow.join_outcomes for the documented hook.")
    return "\n".join(L)




OUTCOME_JOIN_NOTE = """
JOINING SHADOW DECISIONS TO OUTCOMES -- the deliberately unimplemented hook.

Nothing in the shadow log was ever executed, so there is no realized P&L belonging to a shadow
decision. There are exactly three honest joins, and each answers a different question. Whoever
implements this must pick one and say which.

1. PRODUCTION-ARM CONFIRMATION JOIN (weakest, available today).
   Arm A is the production configuration; when the live trader ran its own cycle near the same
   moment on the same tape, its entries land in ~/exitmgr-app/trades.log and their closes in
   ~/exitmgr-app/exits.log. Join shadow.record.ts -> exits.log entries whose `entry_ts` falls in
   the same session AND whose `symbol` appears in arm A's intents. This tells you what the
   PRODUCTION arm's kind of decision actually earned. It says nothing about arm B, and it is a
   loose join (shadow cycles and trader cycles are not the same cycle). exits.log fields to use:
   symbol, right, strike, entry_ts, entry_debit, close_ts, realized_pnl, realized_pnl_pct,
   holding_days, reason.

2. COUNTERFACTUAL MARK-OUT JOIN (the real measurement, needs a pricing source).
   For each intent in each arm, price the structure it described at decision time and again at
   decision time + intended_hold_days, using the same option pricer the bench uses, and compare.
   This is the only join that scores BOTH arms on the same footing, because neither arm's trade
   was executed. It requires an option-price history the live system does not currently retain --
   that is the missing input, not the missing code. Retaining a daily option-chain snapshot for
   the shadow universe is the prerequisite.

3. UNDERLYING DIRECTIONAL MARK-OUT (cheap proxy, weaker claim).
   Score only (underlying, direction, intended_hold_days) against the underlying's forward return
   from the equity price cache. Cheap and available, but it grades the DIRECTIONAL CALL, not the
   trade: it is blind to structure, DTE, delta and theta -- the doctrine variables this system is
   actually arguing about. Do not let a win here be reported as "the change helped".

The join is left unimplemented on purpose: a wrong join silently manufactures a P&L number that
looks authoritative. brief_sha256 + record_id + ts are stable keys; build the join when the
pricing input for (2) exists.
"""


def join_outcomes(records: Iterable[Dict[str, Any]],
                  exits_log_path: str = DEFAULT_EXITS_LOG) -> None:
    """Public API contract; production-derived narrative omitted."""
    raise NotImplementedError(OUTCOME_JOIN_NOTE)
