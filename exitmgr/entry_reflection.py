"""Public API contract; production-derived narrative omitted."""
from dataclasses import dataclass
import json
import math
from pathlib import Path
import time
from types import MappingProxyType
from typing import Mapping

from exitmgr.risk import OpenPosition, INDEX_UNDERLYINGS


@dataclass(frozen=True)
class JournalBasis:
    captured_at_monotonic: float
    debits: Mapping
    single_lot_entries: Mapping
    campaign_entries: Mapping
    campaign_receipts: Mapping



    readable: bool = True


def _positive_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("expected a numeric value")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError("expected a positive finite value")
    return number


def _positive_integer(value):
    number = _positive_number(value)
    if int(number) != number:
        raise ValueError("expected a whole number")
    return int(number)


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


def capture_journal_basis(path):
    """Public API contract; production-derived narrative omitted."""
    rows, readable = [], True
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        text = ""
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("journal row is not an object")
            rows.append(row)
        except (ValueError, TypeError):

            readable = False
    from exitmgr.manager import build_journal_campaigns, open_campaign
    campaigns = build_journal_campaigns(rows)
    debits, entries, campaign_entries, campaign_receipts = {}, {}, {}, {}
    for cid in campaigns:
        campaign = open_campaign(campaigns, cid)
        if campaign is None or not readable:
            continue
        total = campaign.total("debit")
        try:
            debits[int(cid)] = _positive_number(total)
        except ValueError:
            continue
        campaign_entries[int(cid)] = _freeze(campaign.as_journal_row())
        campaign_receipts[int(cid)] = _freeze({
            "campaign_seq": int(campaign.campaign_seq),
            "first_lot_line": int(campaign.lots[0].line_no),
            "first_lot_ts": campaign.lots[0].ts,
        })
        if readable and len(campaign.lots) == 1:
            entries[int(cid)] = _freeze(campaign.lots[0].row)
    return JournalBasis(time.monotonic(), MappingProxyType(debits), MappingProxyType(entries),
                        MappingProxyType(campaign_entries),
                        MappingProxyType(campaign_receipts), readable=readable)


def _same_contract(position, row, *, short=False):
    spread = row.get("spread") or {}
    return (
        str(getattr(position, "sec_type", "")).upper() == "OPT"
        and str(getattr(position, "symbol", "")).upper() == row["symbol"].upper()
        and getattr(position, "right", None) == row["right"]
        and getattr(position, "expiry", None) == row["expiry"]
        and _positive_number(getattr(position, "strike", None)) == _positive_number(
            spread["short_strike"] if short else row["strike"])
    )


def _basis_matches_book(raw, position, campaign, ambiguous_short_legs=()):
    """Public API contract; production-derived narrative omitted."""
    qty = _positive_integer(position.quantity)
    if _positive_integer(campaign["quantity"]) < qty or not _same_contract(position, campaign):
        return False
    if campaign.get("spread"):
        scid = _positive_integer(campaign["spread"]["short_con_id"])
        if scid in ambiguous_short_legs:
            return False
        short = raw[scid]
        if (_positive_integer(short.con_id) != scid or _positive_number(-short.quantity) != qty
                or not _same_contract(short, campaign, short=True)):
            return False
    return True


def _reflection(raw, cid, basis, notional, observed_at_monotonic):
    try:
        if observed_at_monotonic is None:
            return None
        if not (0 <= basis.captured_at_monotonic <= observed_at_monotonic <= time.monotonic()):
            return None
        row = basis.single_lot_entries[cid]
        if row.get("event") or row.get("order_status") != "Filled" or row.get("order_terminal") is not True:
            return None
        if row.get("basis_source") != "fill":
            return None
        if row.get("quantity_source") not in {"order_status", "executions"}:
            return None
        remaining = row.get("entry_remaining_qty")
        if isinstance(remaining, bool) or not isinstance(remaining, (int, float)) or remaining != 0:
            return None
        qty = _positive_integer(row["quantity"])
        if _positive_integer(row["quantity_requested"]) != qty:
            return None
        if _positive_integer(row["contract_id"]) != cid:
            return None
        ref = row["order_ref"]
        if not isinstance(ref, str) or not ref.startswith("alfred-entry:") or not ref[13:]:
            return None
        debit = _positive_number(row["entry_fill_debit"])
        if abs(debit - _positive_number(row["debit"])) > 0.005 or abs(debit - notional) > 0.005:
            return None
        pos = raw[cid]
        if _positive_integer(pos.quantity) != qty or not _same_contract(pos, row):
            return None
        legs = [cid]
        if row.get("spread"):
            scid = _positive_integer(row["spread"]["short_con_id"])
            if scid == cid:
                return None
            short = raw[scid]
            if (_positive_integer(short.con_id) != scid or _positive_number(-short.quantity) != qty
                    or not _same_contract(short, row, short=True)):
                return None
            legs.append(scid)
        return dict(
            order_ref=ref, con_id=cid, leg_con_ids=tuple(legs), contracts=qty,
            capital_usd=round(debit, 2), symbol=row["symbol"].upper(),
            journal_captured_at_monotonic=basis.captured_at_monotonic,
            position_observed_at_monotonic=observed_at_monotonic)
    except (KeyError, AttributeError, TypeError, ValueError, OverflowError):
        return None


def build_debit_risk_book(raw_positions, basis, *, observed_at_monotonic=None):
    """Public API contract; production-derived narrative omitted."""
    if not isinstance(raw_positions, Mapping):
        raise ValueError("broker position book is UNKNOWN")
    short_owners = {}
    for cid, campaign in basis.campaign_entries.items():
        try:
            if cid not in raw_positions or raw_positions[cid].quantity <= 0 or not campaign.get("spread"):
                continue
            scid = _positive_integer(campaign["spread"]["short_con_id"])
            short_owners.setdefault(scid, set()).add(cid)
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    ambiguous_shorts = {cid for cid, owners in short_owners.items() if len(owners) > 1}
    out = []
    for key, position in raw_positions.items():
        qty = getattr(position, "quantity", None)
        if isinstance(qty, bool) or not isinstance(qty, (int, float)) or not math.isfinite(qty):
            raise ValueError("unreadable broker position quantity")
        cid = _positive_integer(getattr(position, "con_id", None))
        if _positive_integer(key) != cid:
            raise ValueError("broker position key/contract mismatch")
        if qty <= 0:
            continue
        symbol = str(getattr(position, "symbol", "") or "").upper()
        gross = abs(float(getattr(position, "avg_cost", 0.0))) * 100 * qty
        notional = gross
        basis_valid = False
        campaign = {}
        try:
            campaign = basis.campaign_entries[cid]



            if _basis_matches_book(raw_positions, position, campaign, ambiguous_shorts):
                notional = basis.debits[cid]
                basis_valid = True
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            pass
        if not math.isfinite(notional) or notional <= 0:
            raise ValueError("unreadable broker position exposure")
        legs = [cid]
        try:
            scid = _positive_integer(campaign.get("spread", {}).get("short_con_id"))
            legs.append(scid)
        except (ValueError, TypeError, AttributeError):
            pass
        receipt = basis.campaign_receipts.get(cid) or {}
        campaign_id = ("contract:%d:campaign:%s:first-line:%s" % (
            cid, receipt.get("campaign_seq", "unknown"),
            receipt.get("first_lot_line", "unknown")))
        p = OpenPosition(
            symbol, notional, symbol in INDEX_UNDERLYINGS,
            primary_con_id=cid, leg_con_ids=tuple(legs), contracts=int(qty),
            campaign_id=campaign_id)
        p.entry_reflection = (_reflection(raw_positions, cid, basis, notional, observed_at_monotonic)
                              if basis_valid else None)
        out.append(p)
    return out


def build_admission_risk_book(raw_positions, basis, *, observed_at_monotonic=None):
    """Public API contract; production-derived narrative omitted."""
    if not getattr(basis, "readable", False):
        raise ValueError("journal is malformed; open option campaign topology is UNKNOWN")
    if not isinstance(raw_positions, Mapping):
        raise ValueError("broker position book is UNKNOWN")

    book = build_debit_risk_book(
        raw_positions, basis, observed_at_monotonic=observed_at_monotonic)
    covered = set()
    leg_owner = {}
    for position in book:
        primary = _positive_integer(position.primary_con_id)
        legs = tuple(_positive_integer(value) for value in position.leg_con_ids)
        if primary not in legs:
            raise ValueError("admission campaign primary is absent from its legs")
        long_row = raw_positions.get(primary)
        long_qty = _positive_integer(getattr(long_row, "quantity", None))
        if long_qty != _positive_integer(position.contracts):
            raise ValueError("fresh long quantity disagrees with admission campaign")
        if len(legs) > 1:
            try:
                campaign = basis.campaign_entries[primary]
                if not _same_contract(long_row, campaign):
                    raise ValueError("journal campaign primary identity disagrees with broker")
                expected_short = _positive_integer(campaign["spread"]["short_con_id"])
                if set(legs) != {primary, expected_short}:
                    raise ValueError("journal campaign topology disagrees with admission legs")
                if not _same_contract(raw_positions[expected_short], campaign, short=True):
                    raise ValueError("journal campaign short identity disagrees with broker")
            except (KeyError, TypeError, AttributeError) as exc:
                raise ValueError("journal campaign topology is incomplete") from exc
        for leg_id in legs:
            if leg_id in leg_owner and leg_owner[leg_id] != primary:
                raise ValueError("one broker option leg is claimed by multiple campaigns")
            leg_owner[leg_id] = primary
            row = raw_positions.get(leg_id)
            if row is None:
                raise ValueError("journal campaign leg is absent from the fresh broker book")
            qty = getattr(row, "quantity", None)
            if (isinstance(qty, bool) or not isinstance(qty, (int, float))
                    or not math.isfinite(qty) or qty == 0):
                raise ValueError("journal campaign leg has unreadable broker quantity")
            if leg_id == primary:
                if qty <= 0 or int(qty) != long_qty:
                    raise ValueError("journal campaign primary has invalid signed quantity")
            elif qty >= 0 or int(-qty) != long_qty:
                raise ValueError("journal campaign short leg quantity/topology is inconsistent")
            covered.add(leg_id)






    for key, row in raw_positions.items():
        cid = _positive_integer(getattr(row, "con_id", None))
        if _positive_integer(key) != cid:
            raise ValueError("broker position key/contract mismatch")
        qty = getattr(row, "quantity", None)
        if (isinstance(qty, bool) or not isinstance(qty, (int, float))
                or not math.isfinite(qty) or int(qty) != qty or qty == 0):
            raise ValueError("broker option position has unreadable signed quantity")
        if cid in covered:
            continue
        if qty > 0:

            raise ValueError("fresh broker long is absent from the admission risk book")
        symbol = str(getattr(row, "symbol", "") or "").strip().upper()
        if not symbol:
            raise ValueError("broker option position has no underlying symbol")
        right = str(getattr(row, "right", "") or "").upper()[:1]
        if right == "C":
            raise ValueError(
                f"uncovered short call con_id={cid} has unbounded UNKNOWN admission risk")
        if right != "P":
            raise ValueError(f"short option con_id={cid} has unreadable right")
        notional = 0.0
        strike = _positive_number(getattr(row, "strike", None))
        notional = strike * 100.0 * abs(int(qty))
        position = OpenPosition(
            symbol, float(notional), symbol in INDEX_UNDERLYINGS, is_credit=True,
            primary_con_id=cid, leg_con_ids=(cid,), contracts=abs(int(qty)),
            campaign_id=f"contract:{cid}:broker-short")


        position.broker_signed_quantity = int(qty)
        book.append(position)
        covered.add(cid)

    if covered != {_positive_integer(key) for key in raw_positions}:
        raise ValueError("not every fresh broker option contract reached admission topology")
    return book
