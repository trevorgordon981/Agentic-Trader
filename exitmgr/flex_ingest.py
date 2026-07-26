"""IBKR Flex Web Service history ingest (2026-07-03): backfill the operator's FULL trade history --
including MANUAL trades that never touched Alfred AND app trades older than the ~7-day
reqExecutions window -- into the trade_dataset.v2 training corpus.

WHY THIS EXISTS
---------------
exec_capture.py pulls executions via reqExecutions, which only reaches ~7 days. the operator's real
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

CASH TRANSACTIONS -> THE P&L LEDGER (2026-07-26)
------------------------------------------------
From 2026-07-26 the account receives $500/month, so the balance stops being a performance
signal. ~/pnl-tracker/pnl_net.py separates trading P&L from cash flows, but on its own it can
only compute a RESIDUAL -- "money that moved and P&L cannot explain" -- and a residual cannot
tell a $500 wire from a $500 dividend. Its own README says the shortest path from "detected" to
"proven" is to pull the Flex **Cash Transactions** section and write it out as the ledger at
~/contributions.jsonl. That is what the cash-transaction half of this module does.

  * parse: each <CashTransaction> row -> one ledger row shaped EXACTLY as pnl_net.py consumes:
        {"date": "YYYY-MM-DD", "amount": <float, signed>, "type": <str>, "note": <str>}
    plus non-conflicting provenance keys (id / ibkr_type / currency / symbol / source /
    account / date_source) that pnl_net ignores. Nothing is invented: `amount` is IBKR's own
    signed amount and `type` is derived ONLY from IBKR's own `type` attribute.
  * classify: IBKR's `type` decides capital vs income, and that distinction IS the point --
    a dividend is NOT a contribution, and calling one a contribution understates the strategy.
        Deposits/Withdrawals, transfers      -> deposit / withdrawal / transfer   (CAPITAL)
        Dividends, Payment In Lieu           -> dividend                          (INCOME)
        Broker/Bond Interest Received|Paid   -> interest                          (INCOME)
        Withholding Tax                      -> tax                               (INCOME)
        Other/Advisor/Broker Fees            -> fee                               (INCOME)
        Commissions / Commission Adjustments -> commission                        (INCOME)
        Price Adjustments                    -> adjustment                        (INCOME)
    An IBKR type this module does not recognise is emitted with its raw slug, which pnl_net
    treats as CAPITAL -- i.e. removed from performance. An unknown label must never become
    profit. Every unrecognised type is also reported so it can be mapped deliberately.
  * idempotency: append-only, keyed on IBKR's own transactionID (falling back to a content
    hash when a statement omits it). A re-run appends 0. A row whose id already exists but
    whose date/amount DISAGREES is never appended and never silently rewritten -- it is
    reported as a conflict for a human to settle.
  * NO FABRICATION: if the configured Flex query does not contain a <CashTransactions>
    section, NOTHING is written and the call fails loudly. Emitting an empty ledger would
    read as "no contributions" and would quietly corrupt the P&L report -- the single worst
    outcome available here.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
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

# --- cash-transaction ledger (feeds ~/pnl-tracker/pnl_net.py) ------------------------------
# pnl_net.py defaults to Path.home()/"contributions.jsonl"; keep them identical or the tracker
# reads nothing.  The env var exists so tests (and any dry inspection) can never write into
# the operator's real ledger.
CASH_LEDGER_ENV = "EXITMGR_CASH_LEDGER"
DEFAULT_CASH_LEDGER = os.path.join(os.path.expanduser("~"), "contributions.jsonl")
# Amounts at/below this are treated as no cash movement at all.  Emitting a $0.00 row would be
# worse than useless: pnl_net's "no ledger rows AND |residual| < epsilon -> noise" branch keys
# on ROW PRESENCE, so a zero row silently reclassifies an otherwise-quiet period.
_CASH_ZERO_USD = 0.005


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


# --------------------------------------------------------------- cash transactions -> ledger
# pnl_net.py's own vocabulary.  CAPITAL is external capital and is removed from performance
# entirely; INCOME is real (non-trading) return and stays in net P&L, reported separately.
# Mirrored here rather than imported because ~/pnl-tracker is a separate, settled tree.
CASH_CAPITAL_TYPES = ("contribution", "deposit", "withdrawal", "transfer")
CASH_INCOME_TYPES = ("dividend", "interest", "fee", "commission", "tax", "adjustment")

# IBKR's own CashTransaction `type` strings, normalised (lowercased, punctuation -> spaces).
# "deposit_or_withdrawal" is resolved by the SIGN of the amount, which is the only honest way
# to tell the operator's $500 in from a $500 out -- IBKR reports both under one type string.
_CASH_TYPE_MAP: Dict[str, str] = {
    "deposits withdrawals": "deposit_or_withdrawal",
    "deposits and withdrawals": "deposit_or_withdrawal",
    "deposits": "deposit_or_withdrawal",
    "withdrawals": "deposit_or_withdrawal",
    "deposit": "deposit_or_withdrawal",
    "withdrawal": "deposit_or_withdrawal",
    "electronic fund transfer": "deposit_or_withdrawal",
    "internal transfers": "transfer",
    "internal transfer": "transfer",
    "transfers": "transfer",
    "transfer": "transfer",
    "dividends": "dividend",
    "dividend": "dividend",
    "payment in lieu of dividends": "dividend",
    "payment in lieu of dividend": "dividend",
    "payment in lieu": "dividend",
    "broker interest received": "interest",
    "broker interest paid": "interest",
    "broker interest": "interest",
    "bond interest received": "interest",
    "bond interest paid": "interest",
    "credit interest": "interest",
    "debit interest": "interest",
    "interest": "interest",
    "withholding tax": "tax",
    "taxes": "tax",
    "tax": "tax",
    "other fees": "fee",
    "advisor fees": "fee",
    "broker fees": "fee",
    "fees": "fee",
    "fee": "fee",
    "commission adjustments": "commission",
    "commission adjustment": "commission",
    "commissions": "commission",
    "commission": "commission",
    "price adjustments": "adjustment",
    "price adjustment": "adjustment",
    "adjustments": "adjustment",
    "adjustment": "adjustment",
}

CASH_SECTION_MISSING_ACTION = (
    "The configured IBKR Activity Flex Query does not include the Cash Transactions section, so "
    "deposits/dividends/interest/fees CANNOT be distinguished and NO ledger was written (an empty "
    "ledger would read as 'no contributions' and corrupt the P&L report). To enable it: IBKR "
    "Client Portal -> Performance & Reports -> Flex Queries -> edit the Activity Flex Query -> "
    "Sections -> tick 'Cash Transactions', and inside it select the options Deposits/Withdrawals, "
    "Dividends, Payment In Lieu Of Dividends, Broker Interest Received, Broker Interest Paid, "
    "Withholding Tax and Other Fees, plus the fields Type, Amount, Currency, FXRateToBase, "
    "DateTime, SettleDate, ReportDate, Description, Symbol, TransactionID and LevelOfDetail."
)


def cash_ledger_path(path: Optional[str] = None) -> str:
    """Where the cash ledger lives.  Explicit arg > $EXITMGR_CASH_LEDGER > pnl_net's default."""
    return path or os.environ.get(CASH_LEDGER_ENV) or DEFAULT_CASH_LEDGER


def _norm_cash_type(raw: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(raw or "").strip().lower()).strip()


def _cash_date(attr: Dict[str, str]) -> Tuple[Optional[str], Optional[str]]:
    """The date IBKR credited the cash, as 'YYYY-MM-DD', plus which attribute it came from.

    settleDate is when the cash actually lands in the balance, which is what pnl_net matches
    against a net_liq snapshot; dateTime/reportDate are the fallbacks.  Returns (None, None)
    when no attribute yields a real date -- an unplaceable row is skipped, never guessed."""
    for key in ("settleDate", "dateTime", "reportDate", "date"):
        iso = _parse_flex_dt(attr.get(key))
        if iso and re.match(r"^\d{4}-\d{2}-\d{2}", iso):
            return iso[:10], key
    return None, None


def classify_cash_type(ibkr_type: Optional[str], amount: float) -> Tuple[str, bool]:
    """IBKR's `type` (+ sign, for the combined deposits/withdrawals bucket) -> a pnl_net type.

    Returns (type, recognised).  An unrecognised IBKR type yields its normalised slug, which
    pnl_net does not find in INCOME_TYPES and therefore treats as CAPITAL -- removed from
    performance.  That is the deliberate fail-safe: an unknown label must never become profit."""
    norm = _norm_cash_type(ibkr_type)
    mapped = _CASH_TYPE_MAP.get(norm)
    if mapped == "deposit_or_withdrawal":
        return ("deposit" if amount > 0 else "withdrawal"), True
    if mapped:
        return mapped, True
    return (norm.replace(" ", "_") or "unknown"), False


def normalize_cash_transaction(attr: Dict[str, str],
                               meta: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """One Flex <CashTransaction> row -> one pnl_net ledger row, or None if unusable.

    Shape is pnl_net's, exactly: date / amount / type / note.  Everything else is provenance
    pnl_net ignores.  Never raises; a malformed row is dropped so the rest of the statement
    survives."""
    try:
        amount = _num(attr.get("amount"))
        if amount is None:
            return None
        date, date_source = _cash_date(attr)
        if not date:
            return None

        currency = (attr.get("currency") or "").strip().upper() or None
        base = str((meta or {}).get("baseCurrency") or "USD").strip().upper() or "USD"
        fx = _num(attr.get("fxRateToBase"))
        # net_liq is denominated in the account's base currency, so the ledger must be too.
        # IBKR's OWN fxRateToBase does the conversion -- no rate is ever invented here.
        fx_unconverted = False
        amount_original = amount
        if currency and currency != base:
            if fx is not None and fx > 0:
                amount = amount * fx
            else:
                fx_unconverted = True

        if abs(amount) < _CASH_ZERO_USD:
            return None                                    # no cash moved; see _CASH_ZERO_USD

        ibkr_type = (attr.get("type") or "").strip()
        kind, recognised = classify_cash_type(ibkr_type, amount)

        desc = (attr.get("description") or "").strip()
        symbol = (attr.get("symbol") or "").strip() or None
        note_bits = [b for b in (ibkr_type or None, symbol, desc or None) if b]
        if fx_unconverted:
            note_bits.append(f"UNCONVERTED {currency} (no fxRateToBase)")
        if not recognised and ibkr_type:
            note_bits.append("UNMAPPED IBKR type -> treated as capital")

        row: Dict[str, Any] = {
            # ---- the four keys pnl_net.py reads ----
            "date": date,
            "amount": round(float(amount), 6),
            "type": kind,
            "note": " | ".join(note_bits) or None,
            # ---- provenance pnl_net ignores ----
            "id": (attr.get("transactionID") or "").strip() or None,
            "ibkr_type": ibkr_type or None,
            "currency": currency,
            "base_currency": base,
            "amount_original": round(float(amount_original), 6),
            "fx_rate_to_base": fx,
            "fx_unconverted": fx_unconverted or None,
            "symbol": symbol,
            "account": (attr.get("accountId") or "").strip() or None,
            "date_source": date_source,
            "type_recognised": recognised,
            "source": "ibkr_flex",
        }
        return {k: v for k, v in row.items() if v is not None or k in ("note", "amount", "date", "type")}
    except Exception:
        return None


def parse_cash_transactions(xml_text: str) -> Dict[str, Any]:
    """Parse a FlexQueryResponse -> normalised cash-ledger rows + an honest presence verdict.

    Returns {parsed_ok, section_present, rows, skipped, unmapped_types, level_of_detail, meta}.
    `section_present` is False when the query simply has no Cash Transactions section -- that
    is a CONFIGURATION failure, not an empty period, and callers must not write a ledger for
    it.  Never raises."""
    out: Dict[str, Any] = {"parsed_ok": False, "section_present": False, "rows": [],
                           "skipped": 0, "skipped_detail": [], "unmapped_types": [],
                           "level_of_detail": None, "meta": {}, "raw_rows": 0}
    try:
        root = ET.fromstring((xml_text or "").strip())
    except Exception as e:
        out["error"] = f"statement XML did not parse: {e}"
        return out
    out["parsed_ok"] = True

    meta: Dict[str, Any] = {}
    stmt = root.find(".//FlexStatement")
    if stmt is not None:
        for k in ("accountId", "fromDate", "toDate", "period", "whenGenerated", "baseCurrency"):
            if stmt.get(k):
                meta[k] = stmt.get(k)
    out["meta"] = meta

    containers = list(root.iter("CashTransactions"))
    elems = list(root.iter("CashTransaction"))
    # A row can appear without its container (and vice-versa on a genuinely empty period);
    # either one proves the section is enabled.
    out["section_present"] = bool(containers) or bool(elems)
    if not out["section_present"]:
        return out
    out["raw_rows"] = len(elems)

    # Flex can emit the same cash event at SUMMARY and DETAIL granularity.  Keeping both
    # double-counts every dividend.  Prefer DETAIL when it exists; otherwise take what there is.
    lods = {(e.get("levelOfDetail") or "").strip().upper() for e in elems}
    keep_lod = "DETAIL" if "DETAIL" in lods else None
    out["level_of_detail"] = keep_lod or (sorted(x for x in lods if x) or [None])[0]

    unmapped: Dict[str, int] = {}
    for e in elems:
        a = e.attrib
        if keep_lod and (a.get("levelOfDetail") or "").strip().upper() != keep_lod:
            continue
        row = normalize_cash_transaction(a, meta)
        if row is None:
            out["skipped"] += 1
            if len(out["skipped_detail"]) < 25:
                out["skipped_detail"].append({
                    "type": a.get("type"), "amount": a.get("amount"),
                    "dateTime": a.get("dateTime"), "settleDate": a.get("settleDate"),
                    "transactionID": a.get("transactionID"),
                    "reason": "unparseable amount/date, or zero net cash movement"})
            continue
        if row.get("type_recognised") is False:
            unmapped[str(row.get("ibkr_type") or row.get("type"))] = \
                unmapped.get(str(row.get("ibkr_type") or row.get("type")), 0) + 1
        out["rows"].append(row)

    out["rows"].sort(key=lambda r: (str(r.get("date") or ""), str(r.get("id") or ""),
                                    float(r.get("amount") or 0.0)))
    out["unmapped_types"] = [{"ibkr_type": k, "rows": v} for k, v in sorted(unmapped.items())]
    return out


def _cash_key(row: Dict[str, Any]) -> str:
    """Idempotency key.  IBKR's transactionID when present -- it is stable across re-pulls of
    the same statement.  Otherwise a content hash, so a statement that omits the id still
    cannot duplicate itself."""
    tid = str(row.get("id") or "").strip()
    if tid:
        return f"txn:{tid}"
    basis = "|".join(str(row.get(k) or "") for k in
                     ("date", "amount", "type", "ibkr_type", "symbol", "currency", "note"))
    return "sha:" + hashlib.sha256(basis.encode()).hexdigest()[:32]


def read_cash_ledger(path: Optional[str] = None) -> List[Dict[str, Any]]:
    return _read_rows(cash_ledger_path(path))


def write_cash_ledger(rows: List[Dict[str, Any]], path: Optional[str] = None,
                      dry_run: bool = False) -> Dict[str, Any]:
    """APPEND new ledger rows.  Idempotent and append-only: existing lines are never rewritten.

    A row whose key already exists is a duplicate and is dropped.  A row whose key exists but
    whose date/amount DISAGREES with what is on disk is a conflict: it is neither appended nor
    silently overwritten -- IBKR restating a cash event is a fact a human should see, and
    guessing which version is true is exactly how a ledger stops being authoritative."""
    target = cash_ledger_path(path)
    existing = _read_rows(target)
    index: Dict[str, Dict[str, Any]] = {}
    for r in existing:
        index.setdefault(_cash_key(r), r)

    fresh: List[Dict[str, Any]] = []
    duplicates = 0
    conflicts: List[Dict[str, Any]] = []
    for row in rows:
        key = _cash_key(row)
        prior = index.get(key)
        if prior is not None:
            try:
                same = (str(prior.get("date")) == str(row.get("date"))
                        and abs(float(prior.get("amount")) - float(row.get("amount"))) < 1e-6)
            except (TypeError, ValueError):
                same = False
            if same:
                duplicates += 1
            else:
                conflicts.append({"key": key, "on_disk": {"date": prior.get("date"),
                                                          "amount": prior.get("amount"),
                                                          "type": prior.get("type")},
                                  "from_statement": {"date": row.get("date"),
                                                     "amount": row.get("amount"),
                                                     "type": row.get("type")}})
            continue
        index[key] = row
        fresh.append(row)

    result = {"path": target, "existing": len(existing), "appended": len(fresh),
              "duplicates": duplicates, "conflicts": conflicts, "dry_run": dry_run,
              "appended_detail": [{"date": r["date"], "amount": r["amount"], "type": r["type"]}
                                  for r in fresh]}
    if dry_run or not fresh:
        result["final_rows"] = len(existing) + (0 if dry_run else 0)
        return result
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    with open(target, "a") as fh:
        for r in fresh:
            fh.write(json.dumps(r, default=str) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    result["final_rows"] = len(existing) + len(fresh)
    return result


def ingest_cash_transactions(*, xml_text: Optional[str] = None, token: Optional[str] = None,
                             query_id: Optional[str] = None, env_path: str = DEFAULT_ENV,
                             opener: Callable[[str], str] = _http_get,
                             ledger_path: Optional[str] = None, dry_run: bool = False,
                             tries: int = 10,
                             sleep: Callable[[float], None] = time.sleep) -> Dict[str, Any]:
    """Fetch (or accept) a Flex statement, and write its cash transactions to the pnl_net ledger.

    READ-ONLY against IBKR (HTTPS GET only).  Returns a summary with `ok`.  ok is False -- and
    NOTHING is written -- when the query has no Cash Transactions section; `action_required`
    then names exactly what to enable in IBKR's web UI.  Never raises."""
    summary: Dict[str, Any] = {"ok": False, "note": None,
                               "ledger_path": cash_ledger_path(ledger_path)}
    tok = token
    try:
        if xml_text is None:
            if not token or not query_id:
                token, query_id = load_flex_creds(env_path)
            tok = token
            if not token or not query_id:
                summary["note"] = "missing IBKR_FLEX_TOKEN / IBKR_FLEX_QUERY_ID"
                return summary
            xml_text = fetch_statement_xml(token, query_id, opener, tries=tries, sleep=sleep)

        parsed = parse_cash_transactions(xml_text)
        summary["meta"] = parsed.get("meta")
        summary["section_present"] = parsed.get("section_present")
        if not parsed.get("parsed_ok"):
            summary["note"] = parsed.get("error") or "statement XML did not parse"
            return summary
        if not parsed.get("section_present"):
            summary["note"] = "Flex query has no <CashTransactions> section"
            summary["action_required"] = CASH_SECTION_MISSING_ACTION
            summary["ledger_written"] = False
            return summary

        rows = parsed["rows"]
        summary.update({
            "cash_rows": len(rows), "raw_rows": parsed.get("raw_rows"),
            "skipped": parsed.get("skipped"), "skipped_detail": parsed.get("skipped_detail"),
            "unmapped_types": parsed.get("unmapped_types"),
            "level_of_detail": parsed.get("level_of_detail"),
            "capital_total": round(sum(r["amount"] for r in rows
                                       if r["type"] not in CASH_INCOME_TYPES), 2),
            "income_total": round(sum(r["amount"] for r in rows
                                      if r["type"] in CASH_INCOME_TYPES), 2),
            "by_type": {t: round(sum(r["amount"] for r in rows if r["type"] == t), 2)
                        for t in sorted({r["type"] for r in rows})},
        })
        written = write_cash_ledger(rows, ledger_path, dry_run=dry_run)
        summary["ledger"] = written
        summary["ledger_written"] = not dry_run
        summary["ok"] = True
        if written.get("conflicts"):
            summary["note"] = (f"{len(written['conflicts'])} ledger row(s) disagree with the "
                               "statement and were NOT appended; resolve by hand")
        return summary
    except Exception as e:
        summary["note"] = _redact(f"exception: {e}", tok if isinstance(tok, str) else None)
        return summary


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
def _cash_ledger_step(xml_text: str, ledger_path: Optional[str] = None, dry_run: bool = False,
                      enabled: bool = True, warn: bool = True) -> Dict[str, Any]:
    """Run the cash-ledger half over an already-fetched statement.

    Deliberately isolated from the trade-ingest verdict: a missing Cash Transactions section is
    a real failure for the P&L tracker but must not turn the (successful) trade archive run
    into a failure.  It is shouted to stderr instead, so the scheduled job's .err log carries
    it without anyone having to opt in to noticing."""
    if not enabled:
        return {"ok": None, "note": "cash ledger disabled for this run", "ledger_written": False}
    cash = ingest_cash_transactions(xml_text=xml_text, ledger_path=ledger_path, dry_run=dry_run)
    if warn and not cash.get("ok"):
        try:
            sys.stderr.write("WARNING flex_ingest: cash ledger NOT updated -- "
                             f"{cash.get('note')}\n")
            if cash.get("action_required"):
                sys.stderr.write(str(cash["action_required"]) + "\n")
            sys.stderr.flush()
        except Exception:
            pass
    return cash


def ingest_flex(*, token: Optional[str] = None, query_id: Optional[str] = None,
                env_path: str = DEFAULT_ENV, config=None, ddir: Optional[str] = None,
                xml_text: Optional[str] = None, opener: Callable[[str], str] = _http_get,
                dry_run: bool = False, tries: int = 10,
                sleep: Callable[[float], None] = time.sleep,
                cash_ledger: bool = True,
                cash_ledger_path: Optional[str] = None) -> Dict[str, Any]:
    """Fetch (or accept) a Flex statement, ingest its trade history, reconcile against existing
    rows, and persist. Returns a summary dict. READ-ONLY (HTTPS GET only). Never raises.

    Also writes the cash-transaction ledger pnl_net.py consumes (additive: the `cash_ledger`
    key is new, `ok` still reflects the TRADE ingest only, and every pre-existing key is
    unchanged).  Pass cash_ledger=False to skip it."""
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

        # Cash-transaction ledger for ~/pnl-tracker.  Runs whether or not the statement holds
        # trades -- a month with no trades still has a $500 deposit that the tracker must see.
        summary["cash_ledger"] = _cash_ledger_step(
            xml_text, ledger_path=cash_ledger_path, dry_run=dry_run, enabled=cash_ledger)

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
    ap.add_argument("--cash-ledger", default=None,
                    help=f"cash ledger path (default: ${CASH_LEDGER_ENV} or {DEFAULT_CASH_LEDGER})")
    ap.add_argument("--no-cash", action="store_true",
                    help="skip the cash-transaction ledger entirely")
    ap.add_argument("--cash-only", action="store_true",
                    help="write ONLY the cash ledger (no trade ingest). Exits non-zero -- and "
                         "writes nothing -- if the Flex query has no Cash Transactions section.")
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

    if args.cash_only:
        c = ingest_cash_transactions(xml_text=xml_text, env_path=args.env,
                                     ledger_path=args.cash_ledger, dry_run=args.dry_run,
                                     tries=args.tries)
        print(json.dumps(c, indent=2, default=str))
        if not c.get("ok"):
            sys.stderr.write(f"cash ledger NOT written: {c.get('note')}\n")
            if c.get("action_required"):
                sys.stderr.write(str(c["action_required"]) + "\n")
        return 0 if c.get("ok") else 1

    s = ingest_flex(env_path=args.env, config=cfg, ddir=args.ddir,
                    xml_text=xml_text, dry_run=args.dry_run, tries=args.tries,
                    cash_ledger=not args.no_cash, cash_ledger_path=args.cash_ledger)
    print(json.dumps(s, indent=2, default=str))
    return 0 if s.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(_main())
