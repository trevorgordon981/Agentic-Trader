"""Public API contract; production-derived narrative omitted."""

import json
import math
import os
import fcntl
import hashlib
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timezone
from typing import Optional, Dict, List
from pathlib import Path



TRAIL_CONFIRMATION_FIELDS = ("last_session", "consecutive_qualifying_closes",
                             "armed_at", "peak_since_arm")


def _normalize_trail_confirmation(raw) -> dict:
    """Public API contract; production-derived narrative omitted."""
    rec = {"last_session": None, "consecutive_qualifying_closes": 0,
           "armed_at": None, "peak_since_arm": None,
           "pinned_activation_gain_pct": None, "pinned_giveback_fraction": None,
           "protected_floor_price": None, "protected_entry_per_share": None}
    if not isinstance(raw, dict):
        return rec


    for field_name in ("protected_floor_price", "protected_entry_per_share"):
        try:
            value = float(raw.get(field_name))
            if math.isfinite(value) and value > 0:
                rec[field_name] = value
        except (TypeError, ValueError, OverflowError):
            pass




    try:
        _pgb = raw.get("pinned_giveback_fraction")
        _pgb = None if _pgb is None else float(_pgb)
        if _pgb is not None and (_pgb != _pgb or not (0.0 < _pgb <= 1.0)):
            _pgb = None
        rec["pinned_giveback_fraction"] = _pgb
    except (TypeError, ValueError):
        rec["pinned_giveback_fraction"] = None
    try:
        _pact = raw.get("pinned_activation_gain_pct")
        _pact = None if _pact is None else float(_pact)
        if _pact is not None and _pact != _pact:
            _pact = None
        rec["pinned_activation_gain_pct"] = _pact
    except (TypeError, ValueError):
        rec["pinned_activation_gain_pct"] = None


    if rec["pinned_giveback_fraction"] is None:
        rec["pinned_activation_gain_pct"] = None
    ls = raw.get("last_session")
    if isinstance(ls, str) and ls:
        rec["last_session"] = ls[:10]
    try:
        rec["consecutive_qualifying_closes"] = max(0, min(2, int(raw.get(
            "consecutive_qualifying_closes", 0) or 0)))
    except (TypeError, ValueError):
        rec["consecutive_qualifying_closes"] = 0
    at = raw.get("armed_at")
    if isinstance(at, str) and at:
        rec["armed_at"] = at
    try:
        pk = raw.get("peak_since_arm")
        pk = None if pk is None else float(pk)
        if pk is not None and (pk != pk or pk <= 0):
            pk = None
        rec["peak_since_arm"] = pk
    except (TypeError, ValueError):
        rec["peak_since_arm"] = None


    if rec["armed_at"] is not None and rec["peak_since_arm"] is None:
        rec["armed_at"] = None
    return rec




ATR_STOP_FIELDS = ("basis", "grandfathered", "stop_pct", "decided_at")

STATE_SCHEMA = "exitmgr_state.v2"
STATE_FIELDS = {
    "state_schema", "in_flight", "daily_stats", "last_cycle", "peak_prices",
    "mfe_pct", "mae_pct", "mfe_ts", "mae_ts", "mark_path", "scaled_out",
    "trail_configured", "trail_armed", "trail_confirmation",
    "atr_stop_introduction", "resident_protection", "campaign_bindings",
}


LEGACY_REQUIRED_FIELDS = {"in_flight", "daily_stats", "last_cycle", "peak_prices"}






MAX_MARK_PATH_ROWS = 500
MAX_MARK_PATH_POSITIONS = 256
MAX_MARK_BYTES = 16 * 1024
MAX_MARK_VALUE_BYTES = 4 * 1024
MAX_MARK_TEXT_CHARS = 2_000
MAX_MARK_PATH_TOTAL_BYTES = 8 * 1024 * 1024
MAX_DAILY_STATS_DAYS = 400
MAX_STATE_BYTES = 16 * 1024 * 1024
_CAPTURE_ONLY_MARK_FIELDS = frozenset({"mgmt_raw", "mgmt_input"})


class StateCorruptionError(RuntimeError):
    """Public API contract; production-derived narrative omitted."""


def _durable_int(value, *, field_name: str) -> int:
    """Public API contract; production-derived narrative omitted."""
    if isinstance(value, bool):
        raise TypeError(f"{field_name} must be an integer, not a boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isfinite(value) and value.is_integer():
            return int(value)
        raise TypeError(f"{field_name} must be an exact integer")
    if isinstance(value, str):
        text = value.strip()
        digits = text[1:] if text[:1] in ("+", "-") else text
        if digits and digits.isdecimal():
            return int(text)
    raise TypeError(f"{field_name} must be an exact integer")


def _canonical_con_id_key(value, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} key must be a canonical decimal string")
    parsed = _durable_int(value, field_name=f"{field_name} key")
    if parsed <= 0 or str(parsed) != value:
        raise TypeError(f"{field_name} key must be a canonical positive conId")
    return value


def _finite_number(value, *, field_name: str, allow_zero: bool = True) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{field_name} must be numeric, not boolean")
    number = float(value)
    if not math.isfinite(number) or (number < 0 if allow_zero else number <= 0):
        raise TypeError(f"{field_name} is out of range")
    return number


def _iso_datetime(value, *, field_name: str, allow_none: bool = True):
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{field_name} must be an ISO timestamp")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TypeError(f"{field_name} must be an ISO timestamp") from exc
    return value


def _json_size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8"))


def _bounded_mark_value(value):
    """Public API contract; production-derived narrative omitted."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:MAX_MARK_TEXT_CHARS]
    if isinstance(value, (dict, list)):
        try:
            return value if _json_size(value) <= MAX_MARK_VALUE_BYTES else None
        except (TypeError, ValueError, OverflowError):
            return None
    return None


def _compact_mark(row: dict) -> dict:
    """Public API contract; production-derived narrative omitted."""
    compact = {}
    for key, value in row.items():
        if key in _CAPTURE_ONLY_MARK_FIELDS:
            continue
        bounded = _bounded_mark_value(value)
        if bounded is None and value is not None:
            continue
        candidate = dict(compact)
        candidate[key] = bounded
        try:
            if _json_size(candidate) <= MAX_MARK_BYTES:
                compact = candidate
        except (TypeError, ValueError, OverflowError):
            continue
    return compact


def _bounded_mark_paths(paths: dict) -> dict:
    """Public API contract; production-derived narrative omitted."""
    if not paths:
        return {}
    items = list(paths.items())



    items.sort(key=lambda item: str((item[1][-1] if item[1] else {}).get("ts", "")),
               reverse=True)
    items = items[:MAX_MARK_PATH_POSITIONS]
    per_path_budget = max(MAX_MARK_BYTES, MAX_MARK_PATH_TOTAL_BYTES // max(1, len(items)))
    bounded = {}
    for key, rows in items:
        selected = []
        used = 2
        for raw in reversed(list(rows)[-MAX_MARK_PATH_ROWS:]):
            mark = _compact_mark(raw)
            size = _json_size(mark) + 1
            if used + size > per_path_budget:
                continue
            selected.append(mark)
            used += size
        bounded[key] = list(reversed(selected))
    return bounded


def _bounded_daily_stats(stats: dict) -> dict:
    """Public API contract; production-derived narrative omitted."""
    return dict(sorted(stats.items())[-MAX_DAILY_STATS_DAYS:])


def _campaign_binding(raw, *, field_name: str) -> dict:
    required = {"campaign_seq", "identity", "first_lot_line", "first_lot_ts"}
    if not isinstance(raw, dict) or set(raw) != required:
        raise TypeError(f"{field_name} must be an exact campaign binding")
    seq = _durable_int(raw["campaign_seq"], field_name=f"{field_name}.campaign_seq")
    line = _durable_int(raw["first_lot_line"], field_name=f"{field_name}.first_lot_line")
    identity = raw["identity"]
    if seq <= 0 or line <= 0 or not isinstance(identity, list) or len(identity) != 4:
        raise TypeError(f"{field_name} has invalid sequence/identity/line")
    for value in identity[:3]:
        if value is not None and not isinstance(value, str):
            raise TypeError(f"{field_name}.identity has invalid text field")
    strike = identity[3]
    if strike is not None:
        strike = _finite_number(strike, field_name=f"{field_name}.identity.strike")
    first_ts = raw["first_lot_ts"]
    if first_ts is not None:
        _iso_datetime(first_ts, field_name=f"{field_name}.first_lot_ts", allow_none=False)
    return {"campaign_seq": seq, "identity": list(identity[:3]) + [strike],
            "first_lot_line": line, "first_lot_ts": first_ts}


def atr_stop_basis(decision_id, entry_per_share) -> Optional[str]:
    """Public API contract; production-derived narrative omitted."""
    try:
        eps = float(entry_per_share)
    except (TypeError, ValueError):
        return None
    if eps != eps or eps <= 0:
        return None
    did = str(decision_id or "").strip() or "-"
    return f"{did}|{eps:.4f}"


def _normalize_atr_stop_record(raw, basis) -> Optional[dict]:
    """Public API contract; production-derived narrative omitted."""
    if not isinstance(raw, dict):
        return None
    if not basis or str(raw.get("basis") or "") != str(basis):
        return None
    gf = bool(raw.get("grandfathered"))
    try:
        sp = raw.get("stop_pct")
        sp = None if sp is None else float(sp)
    except (TypeError, ValueError):
        sp = None
    if sp is not None and (sp != sp or sp <= 0):
        sp = None


    if not gf and sp is None:
        return None
    at = raw.get("decided_at")
    return {"basis": str(basis), "grandfathered": gf,
            "stop_pct": None if gf else round(sp, 1),
            "decided_at": at if isinstance(at, str) and at else None}


@dataclass
class InFlightClose:
    """Public API contract; production-derived narrative omitted."""
    con_id: int
    order_id: int
    remaining_qty: int
    entry_debit: float
    order_price: Optional[float] = None
    placed_at: Optional[str] = None
    exit_context: Dict[str, object] = field(default_factory=dict)



    perm_id: int = 0


    client_id: Optional[int] = None
    order_ref: Optional[str] = None
    identity_version: int = 0



    placement_state: str = "submitted"
    fill_key: Optional[str] = None



    side_effects: Dict[str, bool] = field(default_factory=dict)




    submitted_close: Dict[str, object] = field(default_factory=dict)


    fill_evidence: Dict[str, object] = field(default_factory=dict)


@dataclass
class DailyStats:
    """Public API contract; production-derived narrative omitted."""
    date: str
    orders_placed: int = 0
    notional_closed: float = 0.0




    orders_opened: int = 0
    notional_opened: float = 0.0


@dataclass
class State:
    """Public API contract; production-derived narrative omitted."""
    in_flight: Dict[str, InFlightClose] = field(default_factory=dict)
    daily_stats: Dict[str, DailyStats] = field(default_factory=dict)
    last_cycle: Optional[str] = None
    peak_prices: Dict[str, float] = field(default_factory=dict)
    mfe_pct: Dict[str, float] = field(default_factory=dict)






    mae_pct: Dict[str, float] = field(default_factory=dict)
    mfe_ts: Dict[str, str] = field(default_factory=dict)
    mae_ts: Dict[str, str] = field(default_factory=dict)
    mark_path: Dict[str, List[dict]] = field(default_factory=dict)




    scaled_out: Dict[str, bool] = field(default_factory=dict)







    trail_configured: Dict[str, bool] = field(default_factory=dict)












    trail_confirmation: Dict[str, dict] = field(default_factory=dict)























    atr_stop_introduction: Dict[str, dict] = field(default_factory=dict)


    resident_protection: Dict[str, dict] = field(default_factory=dict)



    campaign_bindings: Dict[str, dict] = field(default_factory=dict)

    def atr_stop_decision(self, con_id, basis) -> Optional[dict]:
        """Public API contract; production-derived narrative omitted."""
        return _normalize_atr_stop_record(self.atr_stop_introduction.get(str(con_id)), basis)

    def introduce_atr_stop(self, con_id, basis, *, grandfathered,
                           stop_pct=None) -> Optional[dict]:
        """Public API contract; production-derived narrative omitted."""
        if not basis:
            return None
        if not grandfathered:
            try:
                sp = round(float(stop_pct), 1)
            except (TypeError, ValueError):
                return None
            if sp != sp or sp <= 0:
                return None
        else:
            sp = None
        rec = {"basis": str(basis), "grandfathered": bool(grandfathered), "stop_pct": sp,
               "decided_at": datetime.now().astimezone().isoformat()}
        self.atr_stop_introduction[str(con_id)] = rec
        return rec

    def tighten_atr_stop(self, con_id, basis, stop_pct) -> bool:
        """Public API contract; production-derived narrative omitted."""
        rec = self.atr_stop_decision(con_id, basis)
        if rec is None or rec["grandfathered"] or rec["stop_pct"] is None:
            return False
        try:
            new = round(float(stop_pct), 1)
        except (TypeError, ValueError):
            return False


        if new != new or new <= 0 or new >= float(rec["stop_pct"]):
            return False
        rec["stop_pct"] = new
        rec["decided_at"] = datetime.now().astimezone().isoformat()
        self.atr_stop_introduction[str(con_id)] = rec
        return True

    def normalize_bounded_history(self) -> None:
        """Public API contract; production-derived narrative omitted."""
        self.mark_path = _bounded_mark_paths(self.mark_path)
        self.daily_stats = _bounded_daily_stats(self.daily_stats)

    def record_mark(self, con_id: int, current_price: float, entry_debit: Optional[float],
                    quantity: int, ts: Optional[str] = None, path_cap: int = MAX_MARK_PATH_ROWS,
                    enrich: Optional[dict] = None) -> Optional[float]:
        """Public API contract; production-derived narrative omitted."""
        k = str(con_id)
        if ts is None:
            ts = datetime.now().astimezone().isoformat()

        if k not in self.peak_prices or current_price > self.peak_prices[k]:
            self.peak_prices[k] = current_price
        exc = None
        try:
            ed = float(entry_debit) if entry_debit is not None else None
        except (TypeError, ValueError):
            ed = None
        if ed and ed > 0:
            exc = round((current_price * 100 * quantity - ed) / ed * 100, 2)
            if k not in self.mfe_pct or exc > self.mfe_pct[k]:
                self.mfe_pct[k] = exc
                self.mfe_ts[k] = ts
            if k not in self.mae_pct or exc < self.mae_pct[k]:
                self.mae_pct[k] = exc
                self.mae_ts[k] = ts
            mp = self.mark_path.setdefault(k, [])










            if True:
                mark = {"ts": ts, "price": round(current_price, 4),
                        "value": round(current_price * 100 * quantity, 2),
                        "pnl_pct": exc, "underlying": None}
                if enrich:
                    try:
                        for kk, vv in enrich.items():






                            if kk in _CAPTURE_ONLY_MARK_FIELDS:
                                continue
                            if vv is not None:
                                mark[kk] = vv
                    except Exception:
                        pass
                mp.append(_compact_mark(mark))
                try:
                    requested_cap = int(path_cap)
                except (TypeError, ValueError, OverflowError):
                    requested_cap = MAX_MARK_PATH_ROWS
                effective_cap = min(MAX_MARK_PATH_ROWS, max(1, requested_cap))
                if len(mp) > effective_cap:
                    del mp[:-effective_cap]
        return exc


    def trail_confirmation_for(self, con_id) -> dict:
        """Public API contract; production-derived narrative omitted."""
        return _normalize_trail_confirmation(self.trail_confirmation.get(str(con_id)))

    def is_trail_armed(self, con_id) -> bool:
        """Public API contract; production-derived narrative omitted."""
        return self.trail_confirmation_for(con_id)["armed_at"] is not None

    def trail_peak_since_arm(self, con_id) -> Optional[float]:
        """Public API contract; production-derived narrative omitted."""
        return self.trail_confirmation_for(con_id)["peak_since_arm"]

    def record_session_close(self, con_id, session_date, close_price, entry_debit, quantity,
                             activation_gain_pct, ts: Optional[str] = None) -> dict:
        """Public API contract; production-derived narrative omitted."""
        k = str(con_id)
        rec = self.trail_confirmation_for(con_id)

        from exitmgr.rules import _as_date, is_next_trading_session
        sd = _as_date(session_date)
        if sd is None:
            return rec
        sd = str(sd)

        try:
            q = int(quantity)
            ed = float(entry_debit)
            px = float(close_price)
            act = float(activation_gain_pct)
        except (TypeError, ValueError):
            return rec
        if q <= 0 or ed <= 0 or px != px or px <= 0 or ed != ed:
            return rec
        entry_per_share = ed / (100.0 * q)
        if entry_per_share <= 0:
            return rec

        last = rec["last_session"]
        if last is not None and sd <= last:

            return rec

        qualifies = px >= entry_per_share * (1.0 + act / 100.0)
        if not qualifies:
            n = 0
        elif last is not None and is_next_trading_session(last, sd):
            n = int(rec["consecutive_qualifying_closes"]) + 1
        else:


            n = 1

        rec["last_session"] = sd
        rec["consecutive_qualifying_closes"] = min(n, 2)
        if n >= 2 and rec["armed_at"] is None:
            rec["armed_at"] = ts or datetime.now().astimezone().isoformat()
            rec["peak_since_arm"] = px
        self.trail_confirmation[k] = rec
        return rec

    def arm_on_peak_gain(self, con_id, peak_price, entry_debit, quantity,
                         activation_gain_pct, ts: Optional[str] = None) -> bool:
        """Public API contract; production-derived narrative omitted."""
        rec = self.trail_confirmation_for(con_id)
        if rec["armed_at"] is not None:
            return False
        try:
            q = int(quantity)
            ed = float(entry_debit)
            pk = float(peak_price)
            act = float(activation_gain_pct)
        except (TypeError, ValueError):
            return False


        if q <= 0 or ed <= 0 or pk != pk or pk <= 0 or ed != ed or act != act:
            return False
        entry_per_share = ed / (100.0 * q)
        if entry_per_share <= 0:
            return False
        if pk < entry_per_share * (1.0 + act / 100.0):
            return False

        rec["armed_at"] = ts or datetime.now().astimezone().isoformat()
        rec["peak_since_arm"] = pk
        self.trail_confirmation[str(con_id)] = rec
        return True

    def record_trail_peak(self, con_id, current_price) -> Optional[float]:
        """Public API contract; production-derived narrative omitted."""
        rec = self.trail_confirmation_for(con_id)
        if rec["armed_at"] is None:
            return None
        try:
            px = float(current_price)
        except (TypeError, ValueError):
            return rec["peak_since_arm"]
        if px != px or px <= 0:
            return rec["peak_since_arm"]
        if rec["peak_since_arm"] is None or px > rec["peak_since_arm"]:
            rec["peak_since_arm"] = px
            self.trail_confirmation[str(con_id)] = rec
        return rec["peak_since_arm"]

    def trail_protected_floor(self, con_id) -> Optional[float]:
        rec = self.trail_confirmation_for(con_id)
        return rec.get("protected_floor_price") if rec["armed_at"] is not None else None

    def ratchet_trail_floor(self, con_id, entry_debit, quantity,
                            giveback_fraction) -> Optional[float]:
        """Public API contract; production-derived narrative omitted."""
        rec = self.trail_confirmation_for(con_id)
        if rec["armed_at"] is None:
            return None
        old = rec.get("protected_floor_price")
        try:
            eps = float(entry_debit) / (100 * int(quantity))
            peak = float(rec["peak_since_arm"])
            gb = float(giveback_fraction)
            if not all(math.isfinite(v) for v in (eps, peak, gb)) or eps <= 0 or not 0 < gb <= 1:
                return old
            anchor = rec.get("protected_entry_per_share")
            if anchor is None:
                if peak <= eps:
                    return old
                anchor = eps
                rec["protected_entry_per_share"] = anchor
            if peak <= anchor:
                return old
            candidate = peak - (peak - anchor) * gb
            rec["protected_floor_price"] = max(old or 0., candidate)
            self.trail_confirmation[str(con_id)] = rec
            return rec["protected_floor_price"]
        except (TypeError, ValueError, OverflowError, ZeroDivisionError):
            return old

    def pin_trail_params(self, con_id, activation_gain_pct, giveback_fraction) -> bool:
        """Public API contract; production-derived narrative omitted."""
        rec = self.trail_confirmation_for(con_id)
        if rec["armed_at"] is None:
            return False
        if rec.get("pinned_giveback_fraction") is not None:
            return False
        try:
            act = float(activation_gain_pct)
            gb = float(giveback_fraction)
        except (TypeError, ValueError):
            return False
        if act != act or gb != gb:
            return False
        rec["pinned_activation_gain_pct"] = act
        rec["pinned_giveback_fraction"] = max(0.1, min(0.9, gb))
        self.trail_confirmation[str(con_id)] = rec
        return True

    def ratchet_trail_params(self, con_id, activation_gain_pct, giveback_fraction) -> bool:
        """Public API contract; production-derived narrative omitted."""
        rec = self.trail_confirmation_for(con_id)
        if rec["armed_at"] is None:
            return False
        try:
            gb_new = float(giveback_fraction)
        except (TypeError, ValueError):
            return False
        if gb_new != gb_new:
            return False
        gb_new = max(0.1, min(0.9, gb_new))
        gb_old = rec.get("pinned_giveback_fraction")
        if gb_old is None:
            rec["pinned_giveback_fraction"] = gb_new
            try:
                rec["pinned_activation_gain_pct"] = float(activation_gain_pct)
            except (TypeError, ValueError):
                pass
            self.trail_confirmation[str(con_id)] = rec
            return True
        if gb_new >= float(gb_old):
            return False
        rec["pinned_giveback_fraction"] = gb_new
        self.trail_confirmation[str(con_id)] = rec
        return True

    def pinned_trail_params(self, con_id):
        """Public API contract; production-derived narrative omitted."""
        rec = self.trail_confirmation_for(con_id)
        gb = rec.get("pinned_giveback_fraction")
        if gb is None:
            return None
        return {"activation_gain_pct": rec.get("pinned_activation_gain_pct"),
                "giveback_fraction": gb}

    def clear_trail_state(self, con_id) -> None:
        """Public API contract; production-derived narrative omitted."""
        k = str(con_id)
        self.trail_confirmation.pop(k, None)
        self.trail_configured.pop(k, None)

    def clear_position_tracking(self, con_id, *, clear_campaign_binding=True) -> bool:
        """Public API contract; production-derived narrative omitted."""
        k = str(con_id)
        changed = False
        fields = (self.peak_prices, self.mfe_pct, self.mae_pct, self.mfe_ts, self.mae_ts,
                  self.mark_path, self.scaled_out, self.trail_configured,
                  self.trail_confirmation, self.atr_stop_introduction,
                  self.resident_protection)
        if clear_campaign_binding:
            fields = fields + (self.campaign_bindings,)
        for values in fields:
            if k in values:
                del values[k]
                changed = True
        return changed

    def prune_tracking(self, active_con_ids) -> None:
        """Public API contract; production-derived narrative omitted."""
        active = {str(c) for c in (active_con_ids or [])}
        for d in (self.peak_prices, self.mfe_pct, self.mae_pct,
                  self.mfe_ts, self.mae_ts, self.mark_path, self.scaled_out,
                  self.trail_configured, self.trail_confirmation,



                  self.atr_stop_introduction, self.resident_protection,
                  self.campaign_bindings):
            for kk in [k for k in d if k not in active]:
                del d[kk]

    def get_in_flight(self, con_id: int) -> Optional[InFlightClose]:
        return self.in_flight.get(str(con_id))

    def add_in_flight(self, close: InFlightClose) -> None:
        self.in_flight[str(close.con_id)] = close

    def remove_in_flight(self, con_id: int) -> None:
        if str(con_id) in self.in_flight:
            del self.in_flight[str(con_id)]

    def update_daily_stats(self, date_str: str, order_count: int, notional: float) -> None:
        if date_str not in self.daily_stats:
            self.daily_stats[date_str] = DailyStats(date=date_str)
        stats = self.daily_stats[date_str]
        stats.orders_placed += order_count
        stats.notional_closed += notional
        self.daily_stats = _bounded_daily_stats(self.daily_stats)

    def update_daily_open_stats(self, date_str: str, order_count: int, notional: float) -> None:
        """Public API contract; production-derived narrative omitted."""
        if date_str not in self.daily_stats:
            self.daily_stats[date_str] = DailyStats(date=date_str)
        stats = self.daily_stats[date_str]
        stats.orders_opened += order_count
        stats.notional_opened += notional
        self.daily_stats = _bounded_daily_stats(self.daily_stats)


class StateManager:
    """Public API contract; production-derived narrative omitted."""

    def __init__(self, state_path: str, persist: bool = True,
                 require_existing: bool = False):
        self.state_path = Path(state_path)
        self.backup_path = Path(str(self.state_path) + ".last-good")
        self._state: Optional[State] = None
        self._recovered_from_backup = False
        self._loaded_primary_sha: Optional[str] = None



















        self.persist = bool(persist)
        self.require_existing = bool(require_existing)

        self.suppressed_saves = 0

    @property
    def state(self) -> State:
        if self._state is None:
            self._state = self._load()
        return self._state

    def reload(self) -> State:
        """Public API contract; production-derived narrative omitted."""
        self._state = self._load()
        return self._state

    @staticmethod
    def _decode(data) -> State:
        if not isinstance(data, dict):
            raise TypeError("state root must be a mapping")
        schema = data.get("state_schema")
        if schema is not None:
            if schema != STATE_SCHEMA or set(data) != STATE_FIELDS:
                raise TypeError("state schema or top-level field set is invalid")
        elif not LEGACY_REQUIRED_FIELDS.issubset(data):
            raise TypeError("legacy state is incomplete; refusing an empty/truncated authority")

        in_flight = {}
        raw_in_flight = data.get("in_flight", {})
        if not isinstance(raw_in_flight, dict):
            raise TypeError("in_flight must be a mapping")
        for con_id_str, close_data in raw_in_flight.items():
            if not isinstance(close_data, dict):
                raise TypeError(f"in_flight[{con_id_str!r}] must be a mapping")
            try:
                key_con_id = _durable_int(con_id_str, field_name="in_flight key")
                normalized_close = dict(close_data)
                for name in ("con_id", "order_id", "remaining_qty", "perm_id",
                             "identity_version"):
                    if name in normalized_close:
                        normalized_close[name] = _durable_int(
                            normalized_close[name], field_name=f"in_flight.{name}")
                if normalized_close.get("client_id") is not None:
                    normalized_close["client_id"] = _durable_int(
                        normalized_close["client_id"], field_name="in_flight.client_id")
                normalized_close["entry_debit"] = _finite_number(
                    normalized_close["entry_debit"], field_name="in_flight.entry_debit")
                if normalized_close.get("order_price") is not None:
                    normalized_close["order_price"] = _finite_number(
                        normalized_close["order_price"], field_name="in_flight.order_price")
                close = InFlightClose(**normalized_close)
                if (close.con_id != key_con_id or key_con_id <= 0
                        or not isinstance(con_id_str, str) or str(key_con_id) != con_id_str
                        or close.order_id < 0 or close.remaining_qty < 0
                        or close.perm_id < 0 or close.identity_version not in (0, 1)
                        or close.client_id is not None and close.client_id < 0
                        or not isinstance(close.exit_context, dict)
                        or not isinstance(close.side_effects, dict)
                        or not isinstance(close.submitted_close, dict)
                        or not isinstance(close.fill_evidence, dict)
                        or any(not isinstance(v, bool) for v in close.side_effects.values())):
                    raise ValueError
                if close.order_price is not None and not math.isfinite(close.order_price):
                    raise ValueError
                if close.placed_at is not None:
                    _iso_datetime(close.placed_at, field_name="in_flight.placed_at",
                                  allow_none=False)
                if close.order_ref is not None and (not isinstance(close.order_ref, str)
                                                    or not close.order_ref.strip()):
                    raise ValueError
                if close.fill_key is not None and (not isinstance(close.fill_key, str)
                                                   or not close.fill_key.strip()):
                    raise ValueError
                if close.placement_state not in (
                        "intent", "transmission_ambiguous", "submitted"):
                    raise ValueError
            except (TypeError, ValueError, OverflowError) as exc:
                raise TypeError(f"in_flight[{con_id_str!r}] has invalid authority fields") from exc
            in_flight[str(key_con_id)] = close

        daily_stats = {}
        raw_daily = data.get("daily_stats", {})
        if not isinstance(raw_daily, dict):
            raise TypeError("daily_stats must be a mapping")
        for date_str, stats_data in raw_daily.items():
            if not isinstance(stats_data, dict):
                raise TypeError(f"daily_stats[{date_str!r}] must be a mapping")
            try:
                if not isinstance(date_str, str) or date.fromisoformat(date_str).isoformat() != date_str:
                    raise ValueError
                normalized_stats = dict(stats_data)
                if normalized_stats.get("date") != date_str:
                    raise ValueError
                for name in ("orders_placed", "orders_opened"):
                    if name in normalized_stats:
                        normalized_stats[name] = _durable_int(
                            normalized_stats[name], field_name=f"daily_stats.{name}")
                        if normalized_stats[name] < 0:
                            raise ValueError
                for name in ("notional_closed", "notional_opened"):
                    if name in normalized_stats:
                        normalized_stats[name] = _finite_number(
                            normalized_stats[name], field_name=f"daily_stats.{name}")
                daily_stats[date_str] = DailyStats(**normalized_stats)
            except (TypeError, ValueError, OverflowError) as exc:
                raise TypeError(f"daily_stats[{date_str!r}] is malformed") from exc
        daily_stats = _bounded_daily_stats(daily_stats)

        def _mapping(name, *, fallback=None):
            value = data.get(name, {} if fallback is None else fallback)
            if value is None and fallback is not None:
                value = fallback
            if not isinstance(value, dict):
                raise TypeError(f"{name} must be a mapping")
            return value

        def _numeric_map(name, *, positive=False):
            out = {}
            for key, raw in _mapping(name).items():
                _canonical_con_id_key(key, field_name=name)
                if isinstance(raw, bool):
                    raise TypeError(f"{name}[{key!r}] must be numeric")
                try:
                    value = float(raw)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise TypeError(f"{name}[{key!r}] must be numeric") from exc
                if not math.isfinite(value) or (positive and value <= 0):
                    raise TypeError(f"{name}[{key!r}] is out of range")
                out[key] = value
            return out

        def _timestamp_map(name):
            out = {}
            for key, raw in _mapping(name).items():
                _canonical_con_id_key(key, field_name=name)
                out[key] = _iso_datetime(raw, field_name=f"{name}[{key!r}]",
                                         allow_none=False)
            return out

        normalized_mark_path = {}
        for key, rows in _mapping("mark_path").items():
            _canonical_con_id_key(key, field_name="mark_path")
            if not isinstance(rows, list):
                raise TypeError(f"mark_path[{key!r}] must be a list")
            normalized_rows = []
            for index, row in enumerate(rows):
                if not isinstance(row, dict):
                    raise TypeError(f"mark_path[{key!r}][{index}] must be a mapping")
                normalized = dict(row)
                if "ts" not in normalized:
                    raise TypeError(f"mark_path[{key!r}][{index}] lacks ts")
                normalized["ts"] = _iso_datetime(
                    normalized["ts"], field_name=f"mark_path[{key!r}][{index}].ts",
                    allow_none=False)
                for field_name in ("price", "value", "pnl_pct", "underlying"):
                    if field_name not in normalized or normalized[field_name] is None:
                        continue
                    if isinstance(normalized[field_name], bool):
                        raise TypeError(
                            f"mark_path[{key!r}][{index}].{field_name} must be numeric")
                    try:
                        value = float(normalized[field_name])
                    except (TypeError, ValueError, OverflowError) as exc:
                        raise TypeError(
                            f"mark_path[{key!r}][{index}].{field_name} must be numeric") from exc
                    if not math.isfinite(value):
                        raise TypeError(
                            f"mark_path[{key!r}][{index}].{field_name} is non-finite")
                    normalized[field_name] = value
                normalized_rows.append(normalized)
            normalized_mark_path[key] = normalized_rows




        normalized_mark_path = _bounded_mark_paths(normalized_mark_path)

        last_cycle = data.get("last_cycle")
        if last_cycle is not None:
            last_cycle = _iso_datetime(
                last_cycle, field_name="last_cycle", allow_none=False)


        normalized_peak_prices = _numeric_map("peak_prices")
        normalized_mfe_pct = _numeric_map("mfe_pct")
        normalized_mae_pct = _numeric_map("mae_pct")
        normalized_mfe_ts = _timestamp_map("mfe_ts")
        normalized_mae_ts = _timestamp_map("mae_ts")

        configured_present = "trail_configured" in data and data.get("trail_configured") is not None
        legacy_present = "trail_armed" in data and data.get("trail_armed") is not None
        trail_configured = (data.get("trail_configured") if configured_present
                            else data.get("trail_armed", {}))
        if not isinstance(trail_configured, dict):
            raise TypeError("trail_configured must be a mapping")
        if configured_present and legacy_present and data["trail_configured"] != data["trail_armed"]:
            raise TypeError("trail_configured and rollback mirror trail_armed disagree")
        normalized_trail_configured = {}
        for key, value in trail_configured.items():
            _canonical_con_id_key(key, field_name="trail_configured")
            if not isinstance(value, bool):
                raise TypeError(f"trail_configured[{key!r}] must be boolean")
            normalized_trail_configured[key] = value
        raw_scaled_out = _mapping("scaled_out")
        normalized_scaled_out = {}
        for key, value in raw_scaled_out.items():
            _canonical_con_id_key(key, field_name="scaled_out")
            if not isinstance(value, bool):
                raise TypeError(f"scaled_out[{key!r}] must be boolean")
            normalized_scaled_out[key] = value
        raw_trail_confirmation = _mapping("trail_confirmation")
        raw_atr_introduction = _mapping("atr_stop_introduction")
        normalized_trails = {}
        for key, raw in raw_trail_confirmation.items():
            _canonical_con_id_key(key, field_name="trail_confirmation")
            if not isinstance(raw, dict):
                raise TypeError(f"trail_confirmation[{key!r}] must be a mapping")
            try:
                missing = [name for name in TRAIL_CONFIRMATION_FIELDS if name not in raw]
                if missing:
                    raise ValueError(f"missing fields: {missing}")
                allowed_fields = set(TRAIL_CONFIRMATION_FIELDS)


                core_fields = set(raw) - {"protected_floor_price", "protected_entry_per_share"}
                if core_fields not in (allowed_fields, allowed_fields | {
                        "pinned_activation_gain_pct", "pinned_giveback_fraction"}):
                    raise ValueError("unexpected or half-present trail fields")
                streak = _durable_int(raw["consecutive_qualifying_closes"],
                                      field_name="consecutive_qualifying_closes")
                if streak < 0 or streak > 2:
                    raise ValueError
                last_session = raw["last_session"]
                armed_at = raw["armed_at"]
                peak_raw = raw["peak_since_arm"]
                if last_session is not None:
                    if not isinstance(last_session, str) or len(last_session) != 10:
                        raise ValueError
                    if date.fromisoformat(last_session).isoformat() != last_session:
                        raise ValueError
                if streak and last_session is None:
                    raise ValueError
                if armed_at is not None and (not isinstance(armed_at, str)
                                             or not armed_at.strip()):
                    raise ValueError
                if armed_at is not None:
                    _iso_datetime(armed_at, field_name="trail_confirmation.armed_at",
                                  allow_none=False)
                if peak_raw is not None:
                    peak = _finite_number(
                        peak_raw, field_name="trail_confirmation.peak_since_arm",
                        allow_zero=False)
                else:
                    peak = None


                if (armed_at is None) != (peak is None) or (streak == 2 and armed_at is None):
                    raise ValueError
                activation = raw.get("pinned_activation_gain_pct")
                giveback = raw.get("pinned_giveback_fraction")
                if activation is not None:
                    activation = _finite_number(
                        activation, field_name="trail_confirmation.pinned_activation_gain_pct",
                        allow_zero=False)
                if giveback is not None:
                    giveback = _finite_number(
                        giveback, field_name="trail_confirmation.pinned_giveback_fraction",
                        allow_zero=False)
                    if not (0.1 <= giveback <= 0.9):
                        raise ValueError
                if (activation is None) != (giveback is None):
                    raise ValueError
                if armed_at is None and (activation is not None or giveback is not None):
                    raise ValueError
                normalized = _normalize_trail_confirmation(raw)
                if normalized["consecutive_qualifying_closes"] != streak:
                    raise ValueError
                if armed_at and not normalized.get("armed_at"):
                    raise ValueError
            except (TypeError, ValueError, OverflowError) as exc:
                raise TypeError(f"trail_confirmation[{key!r}] is malformed") from exc
            normalized_trails[str(key)] = normalized

        normalized_atr = {}
        for key, raw in raw_atr_introduction.items():
            _canonical_con_id_key(key, field_name="atr_stop_introduction")
            if not isinstance(raw, dict):
                raise TypeError(f"atr_stop_introduction[{key!r}] must be a mapping")
            try:
                if set(raw) != set(ATR_STOP_FIELDS):
                    raise ValueError
                basis_raw = raw.get("basis")
                if not isinstance(basis_raw, str):
                    raise ValueError
                basis = basis_raw.strip()
                grandfathered = raw.get("grandfathered")
                stop = raw.get("stop_pct")
                if not basis or not isinstance(grandfathered, bool):
                    raise ValueError
                if grandfathered:
                    if stop is not None:
                        raise ValueError
                else:
                    stop = _finite_number(
                        stop, field_name="atr_stop_introduction.stop_pct",
                        allow_zero=False)
                    if stop > 30:
                        raise ValueError
                if raw.get("decided_at") is not None:
                    _iso_datetime(raw.get("decided_at"),
                                  field_name="atr_stop_introduction.decided_at",
                                  allow_none=False)
            except (TypeError, ValueError, OverflowError) as exc:
                raise TypeError(f"atr_stop_introduction[{key!r}] is malformed") from exc
            normalized_atr[str(key)] = {
                "basis": basis,
                "grandfathered": grandfathered,
                "stop_pct": None if grandfathered else stop,
                "decided_at": raw.get("decided_at"),
            }

        raw_campaign_bindings = _mapping("campaign_bindings")
        normalized_campaign_bindings = {}
        for key, raw in raw_campaign_bindings.items():
            _canonical_con_id_key(key, field_name="campaign_bindings")
            normalized_campaign_bindings[key] = _campaign_binding(
                raw, field_name=f"campaign_bindings[{key!r}]")

        return State(
                in_flight=in_flight,
                daily_stats=daily_stats,
                last_cycle=last_cycle,
                peak_prices=normalized_peak_prices,
                mfe_pct=normalized_mfe_pct,
                mae_pct=normalized_mae_pct,
                mfe_ts=normalized_mfe_ts,
                mae_ts=normalized_mae_ts,
                mark_path=normalized_mark_path,
                scaled_out=normalized_scaled_out,




                trail_configured=normalized_trail_configured,



                trail_confirmation=normalized_trails,



                atr_stop_introduction=normalized_atr,
                resident_protection=_mapping("resident_protection"),
                campaign_bindings=normalized_campaign_bindings,
            )

    @classmethod
    def _decode_bytes(cls, raw: bytes) -> State:
        def _reject_constant(value):
            raise ValueError(f"non-finite JSON constant {value}")
        def _pairs(items):
            out = {}
            for key, value in items:
                if key in out:
                    raise ValueError(f"duplicate JSON object key {key!r}")
                out[key] = value
            return out
        data = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant,
                          object_pairs_hook=_pairs)

        def _finite_tree(value):
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("non-finite numeric JSON leaf")
            if isinstance(value, dict):
                for child in value.values():
                    _finite_tree(child)
            elif isinstance(value, list):
                for child in value:
                    _finite_tree(child)
        _finite_tree(data)
        return cls._decode(data)

    @classmethod
    def _read_path(cls, path: Path) -> State:
        return cls._decode_bytes(Path(path).read_bytes())

    def _load(self) -> State:
        """Public API contract; production-derived narrative omitted."""
        if not self.state_path.exists() and not self.backup_path.exists():
            if self.require_existing:
                raise StateCorruptionError(
                    "armed exit authority has no primary or last-known-good state; "
                    "initialize explicitly in an unarmed run instead of assuming empty")
            return State()
        primary_error = None
        if self.state_path.exists():
            try:
                self._recovered_from_backup = False
                raw = self.state_path.read_bytes()
                state = self._decode_bytes(raw)
                self._loaded_primary_sha = hashlib.sha256(raw).hexdigest()
                return state
            except (OSError, json.JSONDecodeError, TypeError, KeyError, ValueError) as e:
                primary_error = e
        if self.backup_path.exists():
            try:




                self._read_path(self.backup_path)
                raise StateCorruptionError(
                    f"durable exit state unreadable: primary={self.state_path}: "
                    f"{type(primary_error).__name__}: {primary_error}; a parseable prior "
                    f"generation exists at {self.backup_path} but was NOT auto-adopted because "
                    "it may omit a newer trail/order latch") from primary_error
            except StateCorruptionError:
                raise
            except (OSError, json.JSONDecodeError, TypeError, KeyError, ValueError) as backup_error:
                raise StateCorruptionError(
                    f"durable exit state unreadable: primary={self.state_path}: "
                    f"{type(primary_error).__name__}: {primary_error}; backup={self.backup_path}: "
                    f"{type(backup_error).__name__}: {backup_error}") from backup_error
        raise StateCorruptionError(
            f"durable exit state unreadable: {self.state_path}: "
            f"{type(primary_error).__name__}: {primary_error}; no last-known-good copy") from primary_error

    def save(self) -> None:
        """Public API contract; production-derived narrative omitted."""
        if not self.persist:
            self.suppressed_saves += 1
            return



        self.state.normalize_bounded_history()

        self.state_path.parent.mkdir(parents=True, exist_ok=True)


        data = {
            "state_schema": STATE_SCHEMA,
            "in_flight": {k: asdict(v) for k, v in self.state.in_flight.items()},
            "daily_stats": {k: asdict(v) for k, v in self.state.daily_stats.items()},
            "last_cycle": self.state.last_cycle,
            "peak_prices": self.state.peak_prices,
            "mfe_pct": self.state.mfe_pct,
            "mae_pct": self.state.mae_pct,
            "mfe_ts": self.state.mfe_ts,
            "mae_ts": self.state.mae_ts,
            "mark_path": self.state.mark_path,
            "scaled_out": self.state.scaled_out,
            "trail_configured": self.state.trail_configured,




            "trail_armed": self.state.trail_configured,
            "trail_confirmation": self.state.trail_confirmation,
            "atr_stop_introduction": self.state.atr_stop_introduction,
            "resident_protection": self.state.resident_protection,
            "campaign_bindings": self.state.campaign_bindings,
        }




        self._decode(data)
        encoded = json.dumps(data, indent=2, allow_nan=False).encode()
        if len(encoded) > MAX_STATE_BYTES:
            raise StateCorruptionError(
                f"bounded exit state is {len(encoded)} bytes (limit {MAX_STATE_BYTES}); "
                "refusing to replace the last durable authority generation")
        lock_path = Path(str(self.state_path) + ".lock")
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.fchmod(lock_fd, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            self._commit_state_bytes(encoded)
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)

    def _fsync_state_directory(self) -> None:
        """Public API contract; production-derived narrative omitted."""
        dfd = os.open(self.state_path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)

    def _commit_state_bytes(self, encoded: bytes) -> None:
        """Public API contract; production-derived narrative omitted."""
        temp_path = self.state_path.with_suffix(".tmp")
        backup_tmp = Path(str(self.backup_path) + ".tmp")
        current_exists = self.state_path.exists()
        try:
            if current_exists:
                current_raw = self.state_path.read_bytes()
                try:
                    self._decode_bytes(current_raw)
                except (OSError, json.JSONDecodeError, TypeError, KeyError, ValueError) as exc:
                    raise StateCorruptionError(
                        "refusing state update because the current primary is unreadable; "
                        "the existing last-known-good backup was preserved") from exc
                current_sha = hashlib.sha256(current_raw).hexdigest()
                if self._loaded_primary_sha is None or current_sha != self._loaded_primary_sha:
                    raise StateCorruptionError(
                        "refusing stale state write: durable primary changed since this owner "
                        "loaded it")
            elif self._loaded_primary_sha is not None:
                raise StateCorruptionError(
                    "refusing state write: the durable primary disappeared after load")

            fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())

            if current_exists and not self._recovered_from_backup:
                try:
                    backup_tmp.unlink()
                except FileNotFoundError:
                    pass
                os.link(self.state_path, backup_tmp)
                os.replace(backup_tmp, self.backup_path)


                self._fsync_state_directory()

            os.replace(temp_path, self.state_path)
            self._fsync_state_directory()
            self._loaded_primary_sha = hashlib.sha256(encoded).hexdigest()
            self._recovered_from_backup = False
        except Exception:
            for path in (temp_path, backup_tmp):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            raise

    def update_last_cycle(self) -> None:
        """Public API contract; production-derived narrative omitted."""
        self.state.last_cycle = datetime.now(timezone.utc).isoformat()
        self.save()


def reconcile_state(
    state: State,
    live_positions: Dict[int, dict],
    live_open_orders: Dict[int, dict],
    journal_entries: Dict[int, dict],
    journal_qtys: Optional[Dict[int, int]] = None,
    detail: Optional[dict] = None,




) -> tuple[bool, List[str]]:
    """Public API contract; production-derived narrative omitted."""
    alerts: List[str] = []
    safe = True
    journal_qtys = journal_qtys or {}



    inconsistent_con_ids: set = set()
    closed_con_ids: set = set()



    unjournaled_con_ids: set = set()

    def _pos_consistent_with_order(con_id: int, order_remaining) -> bool:
        """Public API contract; production-derived narrative omitted."""
        jq = journal_qtys.get(con_id)
        pq = (live_positions.get(con_id) or {}).get("qty")
        if jq is None or pq is None or order_remaining is None:
            return False
        try:
            jq = int(jq); pq = int(pq); r = int(order_remaining)
        except (TypeError, ValueError):
            return False
        return pq == r or pq == jq - r

    def _client_id(value):
        if isinstance(value, bool):
            return None
        if isinstance(value, int) and value >= 0:
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
        return None

    def _same_order_identity(in_flight: InFlightClose, live_order: dict) -> bool:
        """Public API contract; production-derived narrative omitted."""
        iperm = int(getattr(in_flight, "perm_id", 0) or 0)
        lperm = int(live_order.get("perm_id", 0) or 0)
        if iperm and lperm:
            return iperm == lperm
        iref = getattr(in_flight, "order_ref", None)
        lref = live_order.get("order_ref")
        if iref and lref:
            return str(iref) == str(lref)
        ioid = int(getattr(in_flight, "order_id", 0) or 0)
        loid = int(live_order.get("order_id", 0) or 0)
        iclient = _client_id(getattr(in_flight, "client_id", None))
        lclient = _client_id(live_order.get("client_id"))
        strong_client = bool(getattr(in_flight, "identity_version", 0) >= 1
                             or iclient not in (None, 0))
        if strong_client:
            return bool(ioid and loid and ioid == loid
                        and lclient is not None and iclient == lclient)




        return False


    in_flight_con_ids = set(int(k) for k in state.in_flight.keys())
    live_position_con_ids = set(live_positions.keys())
    live_order_con_ids = set(live_open_orders.keys())
    journal_con_ids = set(journal_entries.keys())








    for con_id in in_flight_con_ids:
        if con_id not in live_position_con_ids and con_id not in live_order_con_ids:
            in_flight = state.get_in_flight(con_id)
            if in_flight is not None and in_flight.exit_context:
                alerts.append(
                    f"[INFO] Position con_id={con_id} and close order are no longer live; "
                    f"retaining in_flight until the terminal fill is confirmed and finalized."
                )
            else:
                alerts.append(
                    f"[WARN] con_id={con_id}: context-free legacy close has no live position "
                    f"or order; retaining in_flight because absence is not terminal evidence."
                )


    for con_id in in_flight_con_ids:
        if con_id in live_position_con_ids and con_id not in live_order_con_ids:

            in_flight = state.get_in_flight(con_id)
            live_qty = live_positions[con_id].get("qty", 0)
            if in_flight and in_flight.remaining_qty != live_qty:

                alerts.append(
                    f"[ERROR] con_id={con_id}: in_flight remaining_qty={in_flight.remaining_qty}, "
                    f"live position qty={live_qty}. Cannot reconcile safely."
                )
                safe = False
                inconsistent_con_ids.add(con_id)
            elif in_flight and in_flight.exit_context:




                alerts.append(
                    f"[INFO] con_id={con_id}: context-rich close order no longer live while "
                    f"position remains; retaining in_flight pending terminal broker status."
                )
            else:







                alerts.append(
                    f"[WARN] con_id={con_id}: context-free legacy close order is no longer "
                    f"live while the position remains; retaining in_flight pending exact terminal "
                    f"broker evidence or operator adjudication. The per-contract latch continues "
                    f"to block any duplicate close; unrelated contracts may proceed."
                )


    for con_id in in_flight_con_ids:
        if con_id in live_order_con_ids:
            in_flight = state.get_in_flight(con_id)
            live_order = live_open_orders[con_id]
            live_order_id = live_order.get("order_id", 0)
            live_remaining = live_order.get("remaining", 0)

            if in_flight and not _same_order_identity(in_flight, live_order):
                alerts.append(
                    f"[ERROR] con_id={con_id}: durable close identity mismatch with the live "
                    f"order (stored order_id={in_flight.order_id}, live order_id={live_order_id}). "
                    f"Cannot reconcile safely."
                )
                safe = False
                inconsistent_con_ids.add(con_id)
            elif in_flight:


                in_flight.order_id = int(live_order_id or in_flight.order_id or 0)
                in_flight.perm_id = int(live_order.get("perm_id", 0)
                                        or getattr(in_flight, "perm_id", 0) or 0)
                live_client = _client_id(live_order.get("client_id"))
                if live_client is not None:
                    in_flight.client_id = live_client
                in_flight.order_ref = (live_order.get("order_ref")
                                       or getattr(in_flight, "order_ref", None))
                in_flight.placement_state = "submitted"
                if (in_flight.perm_id or in_flight.order_ref
                        or in_flight.client_id is not None):
                    in_flight.identity_version = 1

            if in_flight and in_flight.remaining_qty != live_remaining:

                if in_flight.remaining_qty > live_remaining:

                    alerts.append(
                        f"[INFO] con_id={con_id}: partial fill detected, "
                        f"remaining_qty updated from {in_flight.remaining_qty} to {live_remaining}."
                    )
                    in_flight.remaining_qty = live_remaining
                elif _pos_consistent_with_order(con_id, live_remaining):



                    alerts.append(
                        f"[INFO] con_id={con_id}: in_flight remaining_qty={in_flight.remaining_qty} "
                        f"< live order remaining={live_remaining}, but the live position is consistent "
                        f"with (journal qty - remaining); syncing in_flight and continuing."
                    )
                    in_flight.remaining_qty = live_remaining
                else:
                    alerts.append(
                        f"[ERROR] con_id={con_id}: in_flight remaining_qty={in_flight.remaining_qty}, "
                        f"live order remaining={live_remaining}. Cannot reconcile safely."
                    )
                    safe = False
                    inconsistent_con_ids.add(con_id)







    for con_id in live_position_con_ids:
        if con_id not in in_flight_con_ids and con_id not in journal_con_ids:
            if con_id in live_order_con_ids:
                alerts.append(
                    f"[ERROR] con_id={con_id}: live position exists but NOT in journal and NOT in "
                    f"in_flight, AND a live order rests on it. Double-order risk -- aborting for safety."
                )
                safe = False
                inconsistent_con_ids.add(con_id)
            else:

































                unjournaled_con_ids.add(con_id)
                alerts.append(
                    f"[WARN] con_id={con_id}: unexpected live position (not in journal / in_flight), "
                    f"but no in-flight or live order on it -- no double-order risk. Not treating as fatal; "
                    f"clean positions are still protected. NEW ENTRIES are halted while it is "
                    f"unaccounted for; its own exits are NOT."
                )


    for con_id in live_order_con_ids:
        if con_id in in_flight_con_ids:
            continue






        if con_id not in live_position_con_ids:
            alerts.append(
                f"[WARN] con_id={con_id}: live order exists with NO live position "
                f"(order-without-position: a close finishing/cancelling or an unfilled entry). "
                f"Not treating as fatal."
            )
            continue
        alerts.append(
            f"[ERROR] con_id={con_id}: live order exists but NOT in in_flight. "
            f"Cannot reconcile safely. Aborting for safety."
        )
        safe = False
        inconsistent_con_ids.add(con_id)

    if detail is not None:
        detail["inconsistent"] = inconsistent_con_ids
        detail["closed"] = closed_con_ids



        detail["unjournaled"] = unjournaled_con_ids
    return safe, alerts
