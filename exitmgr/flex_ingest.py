"""IBKR Flex Web Service history ingest (2026-07-03): backfill Trevor's FULL trade history --
including MANUAL trades that never touched Alfred AND app trades older than the ~7-day
reqExecutions window -- into the trade_dataset.v2 training corpus.

WHY THIS EXISTS
---------------
exec_capture.py pulls executions via reqExecutions, which only reaches ~7 days. Trevor's real
edge is trades he punches straight into TWS / the mobile app; anything older than a week is
invisible to reqExecutions. The IBKR **Flex Web Service** serves a 365-day Activity Flex Query
(real execIDs, real commissions, real fifoPnlRealized) -- the authoritative history. This module
fetches that statement, normalizes every <Trade> fill into the SAME dict shape
exec_capture.normalize_fill() produces, and REUSES exec_capture's per-contract pairing / P&L /
uuid5 identity / dataset dedup to fold the history in as `source:"flex_history"` rows.

SAFETY (LIVE real-money account)
--------------------------------
  * READ-ONLY reporting ingest. The ONLY network calls are HTTPS GETs to the Flex Web Service
    (SendRequest + GetStatement). There is NO IBKR order path here -- nothing is placed, cancelled,
    modified, or transmitted, and no IB TWS/Gateway socket is opened at all.
  * The Flex token is read from ~/.hermes/.env and is NEVER printed or logged (redacted).

DESIGN
------
  * fetch: SendRequest -> ReferenceCode + Url -> poll GetStatement with backoff until the XML is
    ready (handles the "generation in progress" / ErrorCode 1019 warn).
  * parse: each <Trade> execution row -> a normalize_fill()-shaped dict. open vs close is taken
    from Flex's openCloseIndicator (O -> realized_pnl_ib None so exec_capture treats it as an OPEN;
    C -> fifoPnlRealized, the REAL IBKR realized P&L). Commissions are the REAL per-fill ibCommission
    (Flex reports them negative; normalized to a positive fee like reqExecutions' commissionReport).
  * pairing / P&L / identity: exec_capture.build_rows_for_contract() -- unchanged -- pairs each
    contract's fills, computes gross + net-of-commission P&L, and stamps the deterministic uuid5
    trade_uid / trade_instance_uid. Rows are retagged source:"flex_history".
  * reconcile (supersede): the dataset already holds ESTIMATE rows backfilled from exits.log
    (unknown commission / estimated net). A Flex trade row carrying the SAME uuid5 identity carries
    RICHER truth (real execIDs + real commissions + real fifoPnlRealized), so the estimate row is
    DROPPED and replaced by the Flex row -- one best row per real trade. Only `backfilled` estimate
    rows are ever superseded; app / reqExecutions rows are never touched. Flex rows whose execIDs
    already appear in a kept (reqExecutions/app) row are skipped (no double count).
  * honesty: thesis / chain-of-thought / technical_card / conviction / decision are NULL on every
    flex_history row (it never went through Alfred). NOTHING is fabricated. commission_unknown /
    pnl_is_estimate survive only where a fee is genuinely missing.
  * open positions: an unpaired opening contract (e.g. the Agilent A C135/C150 spread still open)
    becomes a `kind:"position"` snapshot -- never a fake close.
  * idempotency: flex rows carry the same uuid5-based `_dedup_key` the rest of the corpus uses, so a
    re-run appends 0; terminal-trade execIDs are also folded into the shared exec_capture watermark
    so a later reqExecutions run won't re-add the same history.

This is a MANUAL / periodic archive + reconcile tool (not run every exit cycle -- reqExecutions
already covers going-forward). CLI: `python -m exitmgr.flex_ingest`.
"""
import argparse
import hashlib
import json
import os
import shutil
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

from exitmgr import exec_capture as _ec
from exitmgr import trade_capture as _tc
from exitmgr import dataset_integrity as _di

FLEX_BASE = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
SEND_URL = FLEX_BASE + "/SendRequest"
DEFAULT_ENV = os.path.expanduser("~/.hermes/.env")
SOURCE_TAG = "flex_history"
_INPROGRESS_CODE = "1019"  # "Statement generation in progress. Please try again shortly."
_BAK_SUFFIX = "bak-flexingest-20260703"
_STRATEGY_CLOSE_WINDOW_S = 24 * 60 * 60
_PNL_TOLERANCE_USD = 0.05


# --------------------------------------------------------------------------- small utils
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _num(v):
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def _redact(text: str, token: Optional[str]) -> str:
    """Strip the Flex token out of any string before it can be printed/logged."""
    if not text:
        return text
    out = text
    if token:
        out = out.replace(token, "***")
    return out


# --------------------------------------------------------------------------- credentials
def load_flex_creds(env_path: str = DEFAULT_ENV) -> Tuple[Optional[str], Optional[str]]:
    """Read IBKR_FLEX_TOKEN + IBKR_FLEX_QUERY_ID. Process env wins; else parse a KEY=VALUE .env.
    Returns (token, query_id). Never raises. NEVER prints the token."""
    token = os.environ.get("IBKR_FLEX_TOKEN")
    qid = os.environ.get("IBKR_FLEX_QUERY_ID")
    try:
        if (not token or not qid) and env_path and os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k == "IBKR_FLEX_TOKEN" and not token:
                        token = v
                    elif k == "IBKR_FLEX_QUERY_ID" and not qid:
                        qid = v
    except Exception:
        pass
    return token, qid


# --------------------------------------------------------------------------- HTTP (READ-ONLY)
def _http_get(url: str, timeout: int = 90) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "exitmgr-flex-ingest/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _parse_send_response(xml_text: str) -> Dict[str, Optional[str]]:
    """Parse a SendRequest FlexStatementResponse -> {status, reference_code, url, error_code,
    error_message}. Never raises."""
    out = {"status": None, "reference_code": None, "url": None,
           "error_code": None, "error_message": None}
    try:
        root = ET.fromstring(xml_text.strip())
        for tag, key in (("Status", "status"), ("ReferenceCode", "reference_code"),
                         ("Url", "url"), ("ErrorCode", "error_code"),
                         ("ErrorMessage", "error_message")):
            el = root.find(tag)
            if el is not None and el.text is not None:
                out[key] = el.text.strip()
    except Exception:
        pass
    return out


def send_request(token: str, query_id: str, opener: Callable[[str], str] = _http_get) -> Dict[str, Optional[str]]:
    """Kick off Flex statement generation. Returns the parsed SendRequest response
    (reference_code + url on success). Raises RuntimeError with a REDACTED message on failure."""
    url = f"{SEND_URL}?t={token}&q={query_id}&v=3"
    xml_text = opener(url)
    parsed = _parse_send_response(xml_text)
    if (parsed.get("status") or "").lower() != "success" or not parsed.get("reference_code") \
            or not parsed.get("url"):
        raise RuntimeError(_redact(
            f"SendRequest failed: status={parsed.get('status')} "
            f"code={parsed.get('error_code')} msg={parsed.get('error_message')}", token))
    return parsed


def get_statement(url: str, reference_code: str, token: str,
                  opener: Callable[[str], str] = _http_get) -> str:
    return opener(f"{url}?q={reference_code}&t={token}&v=3")


def _is_ready(xml_text: str) -> bool:
    return "<FlexQueryResponse" in (xml_text or "")


def poll_statement(url: str, reference_code: str, token: str,
                   opener: Callable[[str], str] = _http_get,
                   tries: int = 10, delays: Optional[List[float]] = None,
                   sleep: Callable[[float], None] = time.sleep) -> str:
    """Poll GetStatement until the statement XML is ready. Backs off on the 1019
    'generation in progress' warn. Returns the FlexQueryResponse XML. Raises on hard error."""
    if delays is None:
        delays = [2, 3, 3, 5, 5, 8, 8, 10, 12, 15]
    last = ""
    for i in range(max(1, tries)):
        xml_text = get_statement(url, reference_code, token, opener)
        last = xml_text
        if _is_ready(xml_text):
            return xml_text
        parsed = _parse_send_response(xml_text)  # a warn/error also comes back as FlexStatementResponse
        code = parsed.get("error_code")
        if code and code != _INPROGRESS_CODE:
            raise RuntimeError(_redact(
                f"GetStatement error {code}: {parsed.get('error_message')}", token))
        if i < tries - 1:
            sleep(delays[min(i, len(delays) - 1)])
    raise RuntimeError(_redact("Flex statement not ready after polling (still generating?)", token))


def fetch_statement_xml(token: str, query_id: str, opener: Callable[[str], str] = _http_get,
                        tries: int = 10, sleep: Callable[[float], None] = time.sleep) -> str:
    """Full fetch flow: SendRequest -> poll GetStatement -> ready XML. READ-ONLY."""
    sent = send_request(token, query_id, opener)
    return poll_statement(sent["url"], sent["reference_code"], token, opener,
                          tries=tries, sleep=sleep)


# --------------------------------------------------------------------------- parse
def _parse_flex_dt(s: Optional[str]) -> Optional[str]:
    """Flex 'YYYYMMDD;HHMMSS' (or 'YYYYMMDD') -> ISO 'YYYY-MM-DDTHH:MM:SS'. None-safe."""
    if not s:
        return None
    try:
        s = s.strip().replace(",", ";")
        if ";" in s:
            d, t = s.split(";", 1)
        else:
            d, t = s, ""
        d = d.strip()
        iso = f"{d[0:4]}-{d[4:6]}-{d[6:8]}"
        t = t.strip()
        if len(t) >= 6:
            iso += f"T{t[0:2]}:{t[2:4]}:{t[4:6]}"
        elif t:
            iso += f"T{t}"
        return iso
    except Exception:
        return str(s)


def normalize_flex_trade(attr: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """One Flex <Trade> execution row -> a dict shaped EXACTLY like exec_capture.normalize_fill()
    output (so build_rows_for_contract can consume it), plus a few flex-only extras
    (api_order/trade_id). open vs close is driven by openCloseIndicator. Returns None if there is
    no usable ibExecID. Never raises."""
    try:
        exec_id = (attr.get("ibExecID") or "").strip()
        if not exec_id:
            return None
        oc = (attr.get("openCloseIndicator") or "").strip().upper()
        fifo = _num(attr.get("fifoPnlRealized"))
        # OPEN -> realized_pnl_ib None (exec_capture treats a None-realized fill as an opener).
        # CLOSE -> the REAL IBKR fifoPnlRealized (already net of commissions).
        realized_pnl_ib = None if oc == "O" else fifo
        buy_sell = (attr.get("buySell") or "").strip().upper()
        side = "BOT" if buy_sell.startswith("B") else "SLD"
        ib_comm = _num(attr.get("ibCommission"))
        # Flex reports commission NEGATIVE (a fee); reqExecutions' commissionReport is a POSITIVE
        # magnitude and exec_capture subtracts it. Normalize to a positive fee.
        commission = abs(ib_comm) if ib_comm is not None else None
        mult = _num(attr.get("multiplier")) or (100.0 if (attr.get("assetCategory") == "OPT") else 1.0)
        api = (attr.get("isAPIOrder") or "").strip().upper()
        return {
            "exec_id": exec_id,
            "order_id": int(_num(attr.get("ibOrderID")) or 0),
            "perm_id": 0,  # Flex does not expose permId
            "client_id": 0,  # unknown from Flex; the flex path does not classify on clientId
            "acct": attr.get("accountId"),
            "con_id": int(_num(attr.get("conid")) or 0),
            "symbol": attr.get("underlyingSymbol") or attr.get("symbol"),
            "sec_type": attr.get("assetCategory") or "",
            "right": (attr.get("putCall") or "").strip(),
            "strike": _num(attr.get("strike")),
            "expiry": (attr.get("expiry") or "").strip(),
            "side": side,
            "shares": abs(_num(attr.get("quantity")) or 0.0),
            "price": _num(attr.get("tradePrice")) or 0.0,
            "time": _parse_flex_dt(attr.get("dateTime")),
            "mult": mult,
            "commission": commission,
            "commission_ccy": attr.get("ibCommissionCurrency"),
            "realized_pnl_ib": realized_pnl_ib,
            # flex-only extras (ignored by build_rows_for_contract, used for tagging/provenance)
            "api_order": (True if api == "Y" else (False if api == "N" else None)),
            "trade_id": (attr.get("tradeID") or "").strip() or None,
            "open_close": oc or None,
        }
    except Exception:
        return None


def parse_statement(xml_text: str) -> Dict[str, Any]:
    """Parse a FlexQueryResponse -> {fills: [...], meta: {...}}. Only EXECUTION-level <Trade> rows
    are kept (so ORDER-level summary rows can't double-count). Never raises."""
    fills: List[Dict[str, Any]] = []
    meta: Dict[str, Any] = {}
    try:
        root = ET.fromstring(xml_text.strip())
        stmt = root.find(".//FlexStatement")
        if stmt is not None:
            for k in ("accountId", "fromDate", "toDate", "period", "whenGenerated"):
                if stmt.get(k):
                    meta[k] = stmt.get(k)
        for t in root.iter("Trade"):
            a = t.attrib
            lod = (a.get("levelOfDetail") or "").strip().upper()
            if lod and lod != "EXECUTION":
                continue
            n = normalize_flex_trade(a)
            if n is not None:
                fills.append(n)
    except Exception:
        pass
    return {"fills": fills, "meta": meta}


# --------------------------------------------------------------------------- build flex rows
def _retag(row: Dict[str, Any], manual: Optional[bool], meta: Dict[str, Any],
           fills: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Retag an exec_capture-built row as flex_history (source + honest manual flag + provenance).
    Identity + _dedup_key are uuid5/exec-based and are left untouched (idempotency)."""
    row["source"] = SOURCE_TAG
    row["manual"] = manual  # True only if NO api-order fill; False if any api order; None if unknown
    row["reasoning_available"] = False
    prov = row.get("provenance") or {}
    prov["capture_source"] = "ibkr_flex_web_service"
    prov["flex_period"] = meta.get("period")
    prov["flex_when_generated"] = meta.get("whenGenerated")
    prov["trade_ids"] = sorted({f["trade_id"] for f in fills if f.get("trade_id")})
    prov["api_order_flags"] = [f.get("api_order") for f in fills]
    row["provenance"] = prov
    return row


def _parse_iso(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except (TypeError, ValueError):
        return None


def _same_strategy_window(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """Conservative two-leg strategy matcher.

    IB Flex assigns distinct order/trade ids to combo legs, so the durable common evidence is the
    exact opening timestamp plus contract shape. We additionally require matching quantity,
    underlying/right/expiry, opposite opening sides, and closes within one day. This permits a
    broker-managed leg-out while refusing unrelated later round trips. Ambiguous
    groups are deliberately left as single legs instead of fabricating a spread.
    """
    ae, be = a.get("entry") or {}, b.get("entry") or {}
    ac, bc = a.get("close") or {}, b.get("close") or {}
    if not (ae.get("ts") and ae.get("ts") == be.get("ts")):
        return False
    if (a.get("symbol"), ae.get("right"), ae.get("expiry"), ae.get("quantity")) != (
            b.get("symbol"), be.get("right"), be.get("expiry"), be.get("quantity")):
        return False
    if {ae.get("direction"), be.get("direction")} != {"long", "short"}:
        return False
    at, bt = _parse_iso(ac.get("ts")), _parse_iso(bc.get("ts"))
    if at is None or bt is None or abs((at - bt).total_seconds()) > _STRATEGY_CLOSE_WINDOW_S:
        return False
    return not bool(ac.get("partial") or bc.get("partial"))


def _strategy_kind(long_leg: Dict[str, Any], short_leg: Dict[str, Any]) -> str:
    le, se = long_leg.get("entry") or {}, short_leg.get("entry") or {}
    right = str(le.get("right") or "").upper()
    try:
        ls, ss = float(le.get("strike")), float(se.get("strike"))
    except (TypeError, ValueError):
        return "option_spread"
    if (right == "C" and ls < ss) or (right == "P" and ls > ss):
        return "call_debit_spread" if right == "C" else "put_debit_spread"
    return "option_spread"


def _aggregate_pair(long_leg: Dict[str, Any], short_leg: Dict[str, Any]) -> Dict[str, Any]:
    """Collapse two independently reported Flex legs into one strategy/outcome row."""
    le, se = long_leg["entry"], short_leg["entry"]
    lc, sc = long_leg["close"], short_leg["close"]
    legs = [long_leg, short_leg]
    exec_ids = sorted({str(x) for row in legs
                       for x in ((row.get("provenance") or {}).get("exec_ids") or [])})
    strategy_digest = hashlib.sha256("\n".join(exec_ids).encode()).hexdigest()
    entry_cashflow = round(sum(float((row["entry"].get("entry_cashflow") or 0.0))
                               for row in legs), 2)
    net_debit = round(-entry_cashflow, 2) if entry_cashflow < 0 else None
    net_credit = round(entry_cashflow, 2) if entry_cashflow > 0 else None
    ib_realized = round(sum(float(row["close"]["realized_pnl_ib"]) for row in legs), 2)
    computed_values = [(row["close"].get("pnl_validation") or {}).get("computed_net")
                       for row in legs]
    computed_net = (round(sum(float(x) for x in computed_values), 2)
                    if all(x is not None for x in computed_values) else None)
    difference = (round(computed_net - ib_realized, 2) if computed_net is not None else None)
    valid = (all(row["close"].get("pnl_valid") is True for row in legs)
             and (difference is None or abs(difference) <= _PNL_TOLERANCE_USD))
    pnl_net = ib_realized if valid else None
    outcome = _ec._outcome(pnl_net)
    entry_commission = round(sum(float(row["close"].get("entry_commission") or 0.0)
                                 for row in legs), 4)
    exit_commission = round(sum(float(row["close"].get("exit_commission") or 0.0)
                                for row in legs), 4)
    structure = _strategy_kind(long_leg, short_leg)
    close_ts = max(str(lc.get("ts") or ""), str(sc.get("ts") or ""))
    provenance = {
        "capture_source": "ibkr_flex_web_service",
        "exec_ids": exec_ids,
        "order_ids": sorted({x for row in legs
                              for x in ((row.get("provenance") or {}).get("order_ids") or [])}),
        "trade_ids": sorted({x for row in legs
                              for x in ((row.get("provenance") or {}).get("trade_ids") or [])}),
        "strategy_match": "exact-open-ts+shape+opposite-side+bounded-close-ts",
        "leg_trade_uids": [row.get("trade_uid") for row in legs],
    }
    leg_payload = [{
        "con_id": row.get("con_id"),
        "direction": row["entry"].get("direction"),
        "open_side": row["entry"].get("open_side"),
        "close_side": row["entry"].get("close_side"),
        "strike": row["entry"].get("strike"),
        "quantity": row["entry"].get("quantity"),
        "entry_price": (row["entry"].get("debit") or row["entry"].get("credit")),
        "entry_cashflow": row["entry"].get("entry_cashflow"),
        "close_cashflow": row["close"].get("close_cashflow"),
        "realized_pnl_ib": row["close"].get("realized_pnl_ib"),
    } for row in legs]
    row = {
        "schema": "trade_dataset.v2",
        "kind": "trade",
        "source": SOURCE_TAG,
        "manual": (True if all(r.get("manual") is True for r in legs) else
                   (False if any(r.get("manual") is False for r in legs) else None)),
        "reasoning_available": False,
        "ts": _now(),
        "trade_uid": f"flex-strategy:{strategy_digest}",
        "trade_instance_uid": f"flex-strategy:{strategy_digest}",
        "con_id": long_leg.get("con_id"),
        "con_ids": [long_leg.get("con_id"), short_leg.get("con_id")],
        "symbol": long_leg.get("symbol"),
        "decision": None,
        "entry": {
            "ts": le.get("ts"), "symbol": long_leg.get("symbol"),
            "right": le.get("right"), "expiry": le.get("expiry"),
            "structure": structure, "quantity": le.get("quantity"),
            "debit": net_debit, "credit": net_credit, "entry_cashflow": entry_cashflow,
            "spread": {"long_con_id": long_leg.get("con_id"),
                       "short_con_id": short_leg.get("con_id"),
                       "long_strike": le.get("strike"), "short_strike": se.get("strike"),
                       "legs": leg_payload},
            "profit_target_pct": None, "stop_pct": None, "conviction": None,
            "thesis": None, "entry_outside_window": False,
            "basis_source": "IBKR Flex strategy aggregation; signed leg cashflows",
        },
        "lifecycle": {"mark_path": [], "marks": 0, "mfe_pct": None, "mae_pct": None,
                      "drawdown_from_peak_pct": None},
        "close": {
            "ts": close_ts, "reason": "manual_close", "rule_fired": None,
            "realized_pnl": computed_net, "realized_pnl_net": pnl_net,
            "realized_pnl_ib": ib_realized,
            "realized_pnl_pct": (round(pnl_net / net_debit * 100, 2)
                                 if pnl_net is not None and net_debit else None),
            "entry_commission": entry_commission, "exit_commission": exit_commission,
            "commission_unknown": any(bool(row["close"].get("commission_unknown")) for row in legs),
            "pnl_is_estimate": False, "pnl_valid": valid, "pnl_quarantined": not valid,
            "pnl_validation": {"status": "valid" if valid else "ib_disagreement",
                               "tolerance_usd": _PNL_TOLERANCE_USD,
                               "computed_net": computed_net, "ib_realized": ib_realized,
                               "difference": difference},
            "holding_days": _ec._holding_days(le.get("ts"), close_ts),
            "fill_status": "filled", "tp_hit": None, "sl_hit": None, "partial": False,
            "basis_source": "IBKR Flex strategy aggregation; IB realized P&L authoritative",
        },
        "labels": {"outcome": outcome, "win": ((outcome == "win") if outcome else None),
                   "round_trip": None},
        "review": None,
        "provenance": provenance,
    }
    row["_dedup_key"] = f"flex-strategy:{strategy_digest}"
    return row


def _aggregate_strategy_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    used: set = set()
    out: List[Dict[str, Any]] = []
    for i, row in enumerate(rows):
        if i in used or (row.get("entry") or {}).get("direction") != "long":
            continue
        matches = [j for j, candidate in enumerate(rows)
                   if j not in used and j != i
                   and (candidate.get("entry") or {}).get("direction") == "short"
                   and _same_strategy_window(row, candidate)]
        if len(matches) == 1:
            j = matches[0]
            out.append(_aggregate_pair(row, rows[j]))
            used.update((i, j))
    out.extend(row for i, row in enumerate(rows) if i not in used)
    return out


def _split_contract_episodes(fills: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Split repeated round trips in the same conId using Flex's explicit O/C indicator.

    A contract can be closed and later reopened inside one statement. Grouping the entire conId
    blends opposite directions and commissions into one fake trade. Each time cumulative closing
    quantity flattens the episode, seal it and start a new instance.
    """
    if not any(fill.get("open_close") in ("O", "C") for fill in fills):
        return [fills]
    ordered = sorted(fills, key=lambda fill: (
        str(fill.get("time") or ""), 0 if fill.get("open_close") == "O" else 1,
        str(fill.get("exec_id") or "")))
    episodes: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    opened = closed = 0.0
    for fill in ordered:
        current.append(fill)
        qty = float(fill.get("shares") or 0.0)
        if fill.get("open_close") == "O":
            opened += qty
        elif fill.get("open_close") == "C":
            closed += qty
        if opened > 0 and closed >= opened - 1e-9:
            episodes.append(current)
            current, opened, closed = [], 0.0, 0.0
    if current:
        episodes.append(current)
    return episodes


def build_flex_rows(fills: List[Dict[str, Any]], meta: Dict[str, Any]) -> Dict[str, Any]:
    """Build side-correct contract rows, conservatively aggregate strategy legs, and quarantine
    any row whose fill-derived net disagrees with IB's authoritative realized P&L."""
    by_con: Dict[int, List[dict]] = {}
    for f in fills:
        by_con.setdefault(int(f.get("con_id") or 0), []).append(f)
    trade_rows, position_rows = [], []
    terminal_exec_ids: set = set()
    for con_id, cfills in by_con.items():
        for episode in _split_contract_episodes(cfills):
            built = _ec.build_rows_for_contract(con_id, episode)
            # honest manual flag: True only if every fill is a non-API (manual) order
            flags = [f.get("api_order") for f in episode]
            manual = True if all(x is False for x in flags) else (
                False if any(x is True for x in flags) else None)
            if built["trade"] is not None:
                trade_rows.append(_retag(built["trade"], manual, meta, episode))
                if built["terminal"]:
                    terminal_exec_ids.update(built["exec_ids"])
            if built["position"] is not None:
                position_rows.append(_retag(built["position"], manual, meta, episode))
    strategy_rows = _aggregate_strategy_rows(trade_rows)
    quarantined_rows = []
    valid_rows = []
    for row in strategy_rows:
        if (row.get("close") or {}).get("pnl_valid") is False:
            row = dict(row)
            row["quarantine_reason"] = "computed P&L disagrees with authoritative IB realized P&L"
            _di.mark(row, status=_di.INVALID, training=False, pnl=False,
                     reason=row["quarantine_reason"])
            quarantined_rows.append(row)
        else:
            _di.mark(row, status=_di.CANONICAL, training=False, pnl=True,
                     reason="manual/Flex execution has no attributable model decision")
            valid_rows.append(row)
    for row in position_rows:
        _di.mark(row, status=_di.CANONICAL, training=False, pnl=False,
                 reason="open position snapshot has no terminal outcome")
    return {"trade_rows": valid_rows, "position_rows": position_rows,
            "quarantined_rows": quarantined_rows,
            "terminal_exec_ids": terminal_exec_ids, "contracts": len(by_con),
            "strategies": sum(bool((row.get("entry") or {}).get("spread")) for row in valid_rows)}


# --------------------------------------------------------------------------- reconcile + write
def _read_rows(path: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    try:
        if not os.path.exists(path):
            return rows
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        pass
    return rows


def _row_key(row: Dict[str, Any]) -> Optional[str]:
    return row.get("_dedup_key") or _tc._dedup_key(row)


def _is_estimate_row(row: Dict[str, Any]) -> bool:
    """True for a backfilled/estimate row that a richer Flex row may supersede. NEVER matches a
    flex_history row (so a re-run can't supersede its own output) nor a real app/reqExecutions row."""
    if row.get("source") == SOURCE_TAG:
        return False
    if row.get("backfilled"):
        return True
    if "backfill" in str(row.get("backfill_source") or "").lower():
        return True
    return False


def _row_exec_ids(row: Dict[str, Any]) -> set:
    out: set = set()
    _ec._harvest_ids(row, {"exec_ids": out, "order_ids": set(),
                           "perm_ids": set(), "trade_uids": set()})
    return out


def _invalid_pnl(row: Dict[str, Any]) -> bool:
    close = row.get("close") or {}
    if close.get("pnl_valid") is False or close.get("pnl_quarantined") is True:
        return True
    net, ib = close.get("realized_pnl_net"), close.get("realized_pnl_ib")
    try:
        return net is not None and ib is not None and abs(float(net) - float(ib)) > _PNL_TOLERANCE_USD
    except (TypeError, ValueError):
        return True


def _quarantine_path(dataset_path: str) -> str:
    stem, _ = os.path.splitext(dataset_path)
    return stem + ".quarantine.jsonl"


def _append_quarantine(dataset_path: str, rows: List[Dict[str, Any]], dry_run: bool = False) -> int:
    """Persist invalid factual rows outside the training dataset, idempotently."""
    if not rows or dry_run:
        return len(rows)
    path = _quarantine_path(dataset_path)
    existing = {_row_key(r) for r in _read_rows(path)}
    fresh = []
    for source in rows:
        row = dict(source)
        original = _row_key(row) or hashlib.sha256(
            json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()
        key = f"quarantine:{original}"
        if key in existing:
            continue
        row["quarantined_at"] = _now()
        row["quarantine_original_key"] = original
        row["_dedup_key"] = key
        fresh.append(row)
        existing.add(key)
    if fresh:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a") as stream:
            for row in fresh:
                stream.write(json.dumps(row, default=str) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    return len(fresh)


def _fuzzy_match(row: Dict[str, Any], trade_rows: List[Dict[str, Any]]) -> bool:
    """Fallback identity match by underlying+expiry+right+strike+approx entry date (<=4d) when the
    uuid5 identity differs. Belt-and-suspenders for backfilled rows built before the uuid existed."""
    e = row.get("entry") or {}
    sym, right, strike, expiry = row.get("symbol"), e.get("right"), e.get("strike"), e.get("expiry")
    day = str(e.get("ts") or "")[:10]
    for fr in trade_rows:
        fe = fr.get("entry") or {}
        if (fr.get("symbol") == sym and fe.get("right") == right and fe.get("strike") == strike
                and fe.get("expiry") == expiry):
            fday = str(fe.get("ts") or "")[:10]
            if not day or not fday:
                return True
            try:
                da = datetime.fromisoformat(day)
                db = datetime.fromisoformat(fday)
                if abs((da - db).days) <= 4:
                    return True
            except Exception:
                return True
    return False


def reconcile_and_write(dataset_path: str, trade_rows: List[Dict[str, Any]],
                        position_rows: List[Dict[str, Any]], dry_run: bool = False,
                        backup: bool = True,
                        quarantined_rows: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Supersede estimate rows with richer Flex rows, dedup, and rewrite the dataset atomically.
    Returns a summary. Idempotent: a second run supersedes 0 and appends 0."""
    existing = _read_rows(dataset_path)
    replacements = list(trade_rows) + list(quarantined_rows or [])
    flex_uids = {r.get("trade_uid") for r in replacements if r.get("trade_uid")}
    flex_inst = {r.get("trade_instance_uid") for r in replacements if r.get("trade_instance_uid")}
    incoming_exec_ids: set = set()
    incoming_keys = {_row_key(row) for row in list(trade_rows) + list(position_rows)}
    for row in replacements + list(position_rows):
        incoming_exec_ids |= _row_exec_ids(row)

    kept: List[Dict[str, Any]] = []
    superseded: List[Dict[str, Any]] = []
    invalid_existing: List[Dict[str, Any]] = []
    for row in existing:
        # Replace a prior per-leg Flex representation with the current strategy-aware row whenever
        # they share executions. Invalid legacy rows are first copied to the quarantine sidecar.
        if (row.get("source") == SOURCE_TAG and (_row_exec_ids(row) & incoming_exec_ids)
                and _row_key(row) not in incoming_keys):
            superseded.append(row)
            if _invalid_pnl(row):
                bad = dict(row)
                bad["quarantine_reason"] = "legacy Flex P&L disagrees with authoritative IB P&L"
                _di.mark(bad, status=_di.INVALID, training=False, pnl=False,
                         reason=bad["quarantine_reason"])
                invalid_existing.append(bad)
            else:
                old = dict(row)
                old["quarantine_reason"] = "superseded by canonical strategy-aware Flex row"
                _di.mark(old, status=_di.LEGACY, training=False, pnl=False,
                         reason=old["quarantine_reason"])
                invalid_existing.append(old)
            continue
        if _is_estimate_row(row) and (
                row.get("trade_instance_uid") in flex_inst
                or row.get("trade_uid") in flex_uids
                or _fuzzy_match(row, replacements)):
            superseded.append(row)
            continue
        kept.append(row)

    kept_keys = {k for k in (_row_key(r) for r in kept) if k}
    kept_exec_ids: set = set()
    for r in kept:
        kept_exec_ids |= _row_exec_ids(r)

    fresh: List[Dict[str, Any]] = []
    seen_keys: set = set()
    skipped_execdup = 0
    for r in list(trade_rows) + list(position_rows):
        k = r.get("_dedup_key")
        if k and (k in kept_keys or k in seen_keys):
            continue
        rexec = {str(e) for e in (r.get("provenance", {}) or {}).get("exec_ids", [])}
        if rexec and (rexec & kept_exec_ids):
            skipped_execdup += 1  # already captured by a reqExecutions/app row
            continue
        if k:
            seen_keys.add(k)
        fresh.append(r)

    new_rows = kept + fresh
    result = {
        "existing": len(existing),
        "kept": len(kept),
        "superseded": len(superseded),
        "superseded_detail": [
            {"symbol": r.get("symbol"),
             "trade_uid": r.get("trade_uid"),
             "realized_pnl": (r.get("close") or {}).get("realized_pnl"),
             "backfill_source": r.get("backfill_source")}
            for r in superseded],
        "appended_trades": sum(1 for r in fresh if r.get("kind") == "trade"),
        "appended_positions": sum(1 for r in fresh if r.get("kind") == "position"),
        "skipped_execdup": skipped_execdup,
        "final_rows": len(new_rows),
        "dry_run": dry_run,
        "quarantined_existing": _append_quarantine(
            dataset_path, invalid_existing, dry_run=dry_run),
        "quarantine_path": _quarantine_path(dataset_path),
    }
    if dry_run:
        return result
    changed = bool(superseded) or bool(fresh)
    if changed and backup and os.path.exists(dataset_path):
        try:
            shutil.copy2(dataset_path, f"{dataset_path}.{_BAK_SUFFIX}")
        except Exception:
            pass
    if changed:
        os.makedirs(os.path.dirname(dataset_path) or ".", exist_ok=True)
        tmp = dataset_path + ".tmp"
        with open(tmp, "w") as f:
            for r in new_rows:
                f.write(json.dumps(r, default=str) + "\n")
        os.replace(tmp, dataset_path)
    return result


# --------------------------------------------------------------------------- per-underlying summary
def _underlying_summary(trade_rows: List[Dict[str, Any]],
                        position_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    per: Dict[str, Dict[str, Any]] = {}
    for r in trade_rows:
        sym = r.get("symbol") or "?"
        d = per.setdefault(sym, {"trades": 0, "realized_pnl_ib": 0.0, "open_positions": 0})
        d["trades"] += 1
        # fifoPnlRealized is IBKR's authoritative realized P&L (net of commissions)
        pnl = (r.get("close") or {}).get("realized_pnl_ib")
        if pnl is None:
            pnl = (r.get("close") or {}).get("realized_pnl_net")
        if pnl is None:
            pnl = (r.get("close") or {}).get("realized_pnl")
        if pnl is not None:
            d["realized_pnl_ib"] = round(d["realized_pnl_ib"] + float(pnl), 2)
    for r in position_rows:
        sym = r.get("symbol") or "?"
        d = per.setdefault(sym, {"trades": 0, "realized_pnl_ib": 0.0, "open_positions": 0})
        d["open_positions"] += 1
    total = round(sum(v["realized_pnl_ib"] for v in per.values()), 2)
    return {"per_underlying": per, "total_realized_pnl_ib": total}


# --------------------------------------------------------------------------- public entry point
def ingest_flex(*, token: Optional[str] = None, query_id: Optional[str] = None,
                env_path: str = DEFAULT_ENV, config=None, ddir: Optional[str] = None,
                xml_text: Optional[str] = None, opener: Callable[[str], str] = _http_get,
                dry_run: bool = False, tries: int = 10,
                sleep: Callable[[float], None] = time.sleep) -> Dict[str, Any]:
    """Fetch (or accept) a Flex statement, ingest its trade history, reconcile against existing
    rows, and persist. Returns a summary dict. READ-ONLY (HTTPS GET only). Never raises."""
    summary: Dict[str, Any] = {"ok": False, "note": None}
    try:
        # resolve dataset dir / path
        if ddir is None:
            journal = getattr(getattr(config, "journal", None), "path", None) if config else None
            ddir = _tc.dataset_dir(journal)
        dpath = _tc.dataset_path(ddir)

        # obtain statement XML (fetch live unless one was injected for tests)
        meta: Dict[str, Any] = {}
        if xml_text is None:
            if not token or not query_id:
                token, query_id = load_flex_creds(env_path)
            if not token or not query_id:
                summary["note"] = "missing IBKR_FLEX_TOKEN / IBKR_FLEX_QUERY_ID"
                return summary
            xml_text = fetch_statement_xml(token, query_id, opener, tries=tries, sleep=sleep)

        parsed = parse_statement(xml_text)
        fills, meta = parsed["fills"], parsed["meta"]
        if not fills:
            summary.update({"ok": True, "note": "no trades in statement",
                            "fills": 0, "meta": meta})
            return summary

        built = build_flex_rows(fills, meta)
        trade_rows = built["trade_rows"]
        position_rows = built["position_rows"]
        quarantined_rows = built["quarantined_rows"]

        migration = _di.migrate_ledger(dpath, dry_run=dry_run)

        quarantined_new = _append_quarantine(dpath, quarantined_rows, dry_run=dry_run)

        recon = reconcile_and_write(
            dpath, trade_rows, position_rows, dry_run=dry_run,
            quarantined_rows=quarantined_rows)

        # fold terminal-trade execIDs into the SHARED exec_capture watermark so a later
        # reqExecutions run won't re-add this same history.
        if not dry_run and built["terminal_exec_ids"]:
            wm = _ec.load_watermark(ddir)
            wm["runs"] = int(wm.get("runs", 0)) + 1
            wm["_processed"] |= built["terminal_exec_ids"]
            _ec.save_watermark(ddir, wm)

        usum = _underlying_summary(trade_rows, position_rows)
        summary.update({
            "ok": True,
            "dataset_path": dpath,
            "meta": meta,
            "fills": len(fills),
            "contracts": built["contracts"],
            "strategies": built["strategies"],
            "flex_trade_rows": len(trade_rows),
            "flex_position_rows": len(position_rows),
            "quarantined_rows": len(quarantined_rows),
            "quarantined_written": quarantined_new,
            "quarantine_path": _quarantine_path(dpath),
            "canonical_migration": migration,
            "reconcile": recon,
            "summary": usum,
        })
        return summary
    except Exception as e:
        # scrub the token out of any error text
        tok = token if isinstance(token, str) else None
        summary["note"] = _redact(f"exception: {e}", tok)
        return summary


# --------------------------------------------------------------------------- CLI
def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Ingest IBKR Flex Web Service trade history into the trade dataset "
                    "(READ-ONLY reporting; manual/periodic archive + reconcile tool -- "
                    "reqExecutions already covers going-forward).")
    ap.add_argument("--env", default=DEFAULT_ENV, help="path to .env with IBKR_FLEX_* creds")
    ap.add_argument("--ddir", default=None, help="dataset dir (default: resolved from config/journal)")
    ap.add_argument("--xml", default=None, help="ingest a saved statement XML instead of fetching")
    ap.add_argument("--tries", type=int, default=10, help="GetStatement poll attempts")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    cfg = None
    try:
        from exitmgr.config import Config
        for _p in ("config.yaml", os.path.join(os.path.dirname(__file__), "..", "config.yaml")):
            if os.path.exists(_p):
                cfg = Config.from_yaml(_p)
                break
    except Exception:
        cfg = None

    xml_text = None
    if args.xml:
        with open(args.xml) as f:
            xml_text = f.read()

    s = ingest_flex(env_path=args.env, config=cfg, ddir=args.ddir,
                    xml_text=xml_text, dry_run=args.dry_run, tries=args.tries)
    print(json.dumps(s, indent=2, default=str))
    return 0 if s.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(_main())
