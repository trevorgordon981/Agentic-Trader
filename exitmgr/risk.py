"""Risk policy summary. Live configuration allows concurrent positions 10 and keeps the
universe OPEN because allow_model_names is TRUE. The HARD ceiling 25% of pot sits above
the soft cap 18.75% of pot; BULL_LONG_SIZE_MULT can lift that to 28.125% before the hard
ceiling clamps it. The daily breaker -20% on the day and cash buffer 5% of pot remain
deterministic. The single-name aggregate 36% of pot and sector aggregate 40% of pot
remain deterministic. Index
underlyings are SPY, QQQ, and IWM; NO_EARNINGS_ETFS is the wider exemption set. Bare
dataclass fallbacks are 12% / 4 / -8%, not the configured policy. The field
max_cross_book_name_agg_pct is not configurable. KEEP THIS BLOCK HONEST.
"""
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from exitmgr import regime as regime_mod





INDEX_UNDERLYINGS: Set[str] = {"SPY", "QQQ", "IWM"}





DEFAULT_CONVICTION_SIZE_CURVE: Dict[int, float] = {
    1: 0.12, 2: 0.12, 3: 0.12, 4: 0.12, 5: 0.12,
    6: 0.12, 7: 0.12, 8: 0.12, 9: 0.12, 10: 0.12,
}








EXTERNAL_BOOK_OK = "ok"
EXTERNAL_BOOK_ABSENT = "absent"
EXTERNAL_BOOK_STALE = "stale"
EXTERNAL_BOOK_UNREADABLE = "unreadable"


EXTERNAL_BOOK_NOT_SUPPLIED = "not_supplied"







EXTERNAL_BOOK_SCHEMA = "cross-book-snapshot/1"






DEFAULT_EXTERNAL_BOOK_MAX_AGE_S: float = 72 * 3600.0





EXTERNAL_BOOK_FUTURE_TOLERANCE_S: float = 300.0


class ExternalBookUnreadable(RuntimeError):
    """Public API contract; production-derived narrative omitted."""


def external_book_digest(payload) -> str:
    """Public API contract; production-derived narrative omitted."""
    body = {k: v for k, v in dict(payload).items() if k != "digest"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ExternalBook:
    """Public API contract; production-derived narrative omitted."""

    by_underlying: Dict[str, float]
    state: str
    as_of: Optional[float] = None




    generated_at: Optional[float] = None
    age_s: Optional[float] = None
    max_age_s: Optional[float] = None
    n_positions: int = 0


    source: Optional[str] = None
    origin: Optional[str] = None
    digest: Optional[str] = None
    error: Optional[str] = None

    def __bool__(self):
        raise TypeError(
            "ExternalBook has no truth value -- check .readable explicitly. An external book "
            "that is absent, stale or unreadable is UNKNOWN, never empty.")

    @property
    def readable(self) -> bool:
        """Public API contract; production-derived narrative omitted."""
        return self.state == EXTERNAL_BOOK_OK

    def exposure(self, symbol: str) -> float:
        """Public API contract; production-derived narrative omitted."""
        if not self.readable:
            raise ExternalBookUnreadable(self.error or f"external book is {self.state}")
        return float(self.by_underlying.get(str(symbol or "").strip().upper(), 0.0))

    def describe(self) -> Dict[str, object]:
        """Public API contract; production-derived narrative omitted."""
        return {
            "state": self.state,
            "readable": self.readable,
            "as_of": self.as_of,
            "generated_at": self.generated_at,
            "age_s": self.age_s,
            "max_age_s": self.max_age_s,
            "n_positions": self.n_positions,
            "source": self.source,
            "origin": self.origin,
            "digest": self.digest,
            "error": self.error,
        }



    @classmethod
    def known(cls, by_underlying, **kw) -> "ExternalBook":
        clean = {str(k).strip().upper(): float(v) for k, v in dict(by_underlying or {}).items()}
        kw.pop("n_positions", None)
        return cls(by_underlying=clean, state=EXTERNAL_BOOK_OK, n_positions=len(clean), **kw)

    @classmethod
    def absent(cls, error: str, **kw) -> "ExternalBook":
        return cls(by_underlying={}, state=EXTERNAL_BOOK_ABSENT,
                   error=error or "no external-book snapshot", **kw)

    @classmethod
    def stale(cls, error: str, **kw) -> "ExternalBook":
        return cls(by_underlying={}, state=EXTERNAL_BOOK_STALE,
                   error=error or "external-book snapshot is stale", **kw)

    @classmethod
    def unreadable(cls, error: str, **kw) -> "ExternalBook":
        return cls(by_underlying={}, state=EXTERNAL_BOOK_UNREADABLE,
                   error=error or "external-book snapshot could not be verified", **kw)

    @classmethod
    def not_supplied(cls) -> "ExternalBook":
        return cls(by_underlying={}, state=EXTERNAL_BOOK_NOT_SUPPLIED,
                   error="no external book was supplied to the gate")


def _finite_float(value) -> Optional[float]:
    """Public API contract; production-derived narrative omitted."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


def bounded_max_age_s(max_age_s) -> float:
    """Public API contract; production-derived narrative omitted."""
    v = _finite_float(max_age_s)
    if v is None or v <= 0:
        return DEFAULT_EXTERNAL_BOOK_MAX_AGE_S
    return v


def load_external_book(path, *, max_age_s: Optional[float] = None, now=None) -> ExternalBook:
    """Public API contract; production-derived narrative omitted."""
    bound = bounded_max_age_s(max_age_s)
    t_now = _finite_float(now)
    if t_now is None:
        t_now = time.time()

    if not path:
        return ExternalBook.absent("no external_book_path configured", max_age_s=bound)
    p = os.path.expanduser(str(path))

    try:
        raw = open(p, "rb").read()
    except FileNotFoundError:
        return ExternalBook.absent(f"no snapshot at {p}", source=p, max_age_s=bound)
    except IsADirectoryError:
        return ExternalBook.unreadable(f"{p} is a directory, not a snapshot",
                                       source=p, max_age_s=bound)
    except OSError as exc:
        return ExternalBook.unreadable(f"snapshot unreadable: {exc!r}", source=p, max_age_s=bound)

    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:


        return ExternalBook.unreadable(f"snapshot is not valid JSON ({exc!r})",
                                       source=p, max_age_s=bound)

    if not isinstance(payload, dict):
        return ExternalBook.unreadable("snapshot is not a JSON object", source=p, max_age_s=bound)

    if payload.get("schema") != EXTERNAL_BOOK_SCHEMA:
        return ExternalBook.unreadable(
            f"unknown snapshot schema {payload.get('schema')!r} (want {EXTERNAL_BOOK_SCHEMA!r})",
            source=p, max_age_s=bound)

    claimed = payload.get("digest")
    actual = external_book_digest(payload)
    if not isinstance(claimed, str) or claimed != actual:
        return ExternalBook.unreadable(
            f"digest mismatch: file claims {claimed!r}, content hashes to {actual}",
            source=p, max_age_s=bound)

    positions = payload.get("positions")
    if not isinstance(positions, dict):
        return ExternalBook.unreadable("snapshot has no positions object",
                                       source=p, digest=actual, max_age_s=bound)
    by_underlying: Dict[str, float] = {}
    for k, v in positions.items():
        amount = _finite_float(v)
        if not isinstance(k, str) or not k.strip() or amount is None or amount < 0:



            return ExternalBook.unreadable(
                f"position {k!r} has an unusable value {v!r}",
                source=p, digest=actual, max_age_s=bound)
        by_underlying[k.strip().upper()] = amount

    as_of = _finite_float(payload.get("as_of"))
    generated_at = _finite_float(payload.get("generated_at"))
    if as_of is None:
        return ExternalBook.unreadable("snapshot has no usable as_of timestamp",
                                       source=p, digest=actual, generated_at=generated_at,
                                       n_positions=len(by_underlying), max_age_s=bound)

    age = t_now - as_of
    common = dict(as_of=as_of, generated_at=generated_at, age_s=age, max_age_s=bound,
                  source=p, origin=payload.get("origin"), digest=actual,
                  n_positions=len(by_underlying))
    if age < -EXTERNAL_BOOK_FUTURE_TOLERANCE_S:
        return ExternalBook.unreadable(
            f"snapshot as_of is {-age:,.0f}s in the FUTURE (tolerance "
            f"{EXTERNAL_BOOK_FUTURE_TOLERANCE_S:,.0f}s) -- clock disagreement, so the freshness "
            "bound cannot be enforced", **common)
    if age > bound:
        return ExternalBook.stale(
            f"external book data is {age / 3600.0:,.1f}h old (max {bound / 3600.0:,.1f}h)",
            **common)
    common.pop("n_positions", None)
    return ExternalBook.known(by_underlying, **common)


@dataclass
class RiskLimits:
    max_trade_pct: float = 0.12

    max_trade_pct_hard: float = 0.25


    max_concurrent: int = 4
    daily_halt_pct: float = 0.08

    max_single_name_agg_pct: float = 0.36
    max_sector_agg_pct: float = 0.25

















    sector_map: Dict[str, str] = field(default_factory=dict)








    pot_cap_usd: Optional[float] = None
    cash_buffer_pct: float = 0.05



    allow_any_name: bool = False

    confident_full_size: bool = False

    cap_bypass_min_conviction: int = 6



    conviction_size_curve: Optional[Dict[int, float]] = None






    conviction_size_multipliers: Optional[Dict[int, float]] = None









    blocked_names: Set[str] = field(default_factory=set)


    external_book_path: Optional[str] = None










    external_book_max_age_s: float = DEFAULT_EXTERNAL_BOOK_MAX_AGE_S


    max_cross_book_name_agg_pct: Optional[float] = None

















@dataclass
class OpenPosition:
    underlying: str
    notional: float
    is_index: bool











    is_credit: bool = False

    entry_reflection: Optional[dict] = field(default=None, repr=False, compare=False)




    primary_con_id: Optional[int] = field(default=None, repr=False, compare=False)
    leg_con_ids: tuple[int, ...] = field(default=(), repr=False, compare=False)
    contracts: int = field(default=0, repr=False, compare=False)
    campaign_id: str = field(default="", repr=False, compare=False)


@dataclass
class ProposedTrade:
    underlying: str
    notional: float
    is_index: bool
    conviction: int = 1














    is_long: bool = True
    profit_target_pct: Optional[float] = None





    stop_pct: float = 0.0




@dataclass
class GateDecision:
    approved: bool
    reasons: List[str] = field(default_factory=list)
    pot_value: float = 0.0
    per_trade_cap: float = 0.0








    cross_book: Dict[str, object] = field(default_factory=dict)


def _explicit_pct(value) -> Optional[float]:
    """Public API contract; production-derived narrative omitted."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v:
        return None
    return v


def effective_pot(net_liq: float, pot_cap_usd: Optional[float]) -> float:
    """Public API contract; production-derived narrative omitted."""
    if pot_cap_usd and pot_cap_usd > 0:
        return min(net_liq, pot_cap_usd)
    return net_liq


def day_pnl_pct(pot_now: float, pot_day_start: float) -> float:
    if pot_day_start <= 0:
        return 0.0
    return (pot_now - pot_day_start) / pot_day_start






UNCLASSIFIED_SECTOR = "__unclassified__"


def sector_of(symbol: str, sector_map: Optional[Dict[str, str]]) -> str:
    """Public API contract; production-derived narrative omitted."""
    s = (symbol or "").upper()
    if not sector_map:
        return s
    mapped = sector_map.get(s)





    if mapped is not None and str(mapped).strip():
        return str(mapped)
    if s in INDEX_UNDERLYINGS:
        return s



    return UNCLASSIFIED_SECTOR


def sector_exposure(
    open_positions: List["OpenPosition"],
    candidate_symbol: str,
    candidate_debit: float,
    sector_map: Optional[Dict[str, str]],
) -> Dict[str, float]:
    """Public API contract; production-derived narrative omitted."""
    agg: Dict[str, float] = {}
    for p in open_positions:
        if getattr(p, "is_index", False):
            continue
        sec = sector_of(p.underlying, sector_map)
        agg[sec] = agg.get(sec, 0.0) + p.notional
    if candidate_symbol:
        sec = sector_of(candidate_symbol, sector_map)
        agg[sec] = agg.get(sec, 0.0) + candidate_debit
    return agg


def same_name_notional(open_positions: List["OpenPosition"], underlying: str) -> float:
    """Public API contract; production-derived narrative omitted."""
    u = str(underlying or "").strip().upper()

    def _same_name(p):
        raw = getattr(p, "underlying", None)
        if raw is None:
            return True
        try:
            return str(raw).strip().upper() == u
        except Exception:
            return True

    return sum(p.notional for p in open_positions if not p.is_index and _same_name(p))


def external_book_for(limits: "RiskLimits", *, now=None) -> ExternalBook:
    """Public API contract; production-derived narrative omitted."""
    return load_external_book(getattr(limits, "external_book_path", None),
                              max_age_s=getattr(limits, "external_book_max_age_s", None),
                              now=now)


def cross_book_authority(limits: "RiskLimits", *, now=None) -> Dict[str, object]:
    """Public API contract; production-derived narrative omitted."""
    book = external_book_for(limits, now=now)
    return {
        "armed": bool(getattr(limits, "external_book_path", None)),
        "path": getattr(limits, "external_book_path", None),
        "max_age_s": bounded_max_age_s(getattr(limits, "external_book_max_age_s", None)),
        "cap_pct": float(getattr(limits, "max_cross_book_name_agg_pct", None)
                         if getattr(limits, "max_cross_book_name_agg_pct", None) is not None
                         else limits.max_single_name_agg_pct),
        "state": book.state,
        "readable": book.readable,
        "age_s": book.age_s,
        "n_positions": book.n_positions,
        "error": book.error,
    }


def curve_fraction(conviction: int, curve: Optional[Dict[int, float]], fallback: float) -> float:
    """Public API contract; production-derived narrative omitted."""
    if not curve:
        return fallback
    try:
        c = int(conviction)
    except (TypeError, ValueError):
        return fallback
    if c in curve:
        return float(curve[c])

    keys = sorted(int(k) for k in curve.keys())
    if not keys:
        return fallback
    if c < keys[0]:
        return float(curve[keys[0]])
    if c > keys[-1]:
        return float(curve[keys[-1]])
    return fallback


def conviction_multiplier(conviction: int, mult_map: Optional[Dict[int, float]]) -> float:
    """Public API contract; production-derived narrative omitted."""
    if not mult_map:
        return 1.0
    try:
        c = int(conviction)
    except (TypeError, ValueError):
        return 1.0
    if c in mult_map:
        return float(mult_map[c])
    keys = sorted(int(k) for k in mult_map.keys())
    if not keys:
        return 1.0
    if c < keys[0]:
        return float(mult_map[keys[0]])
    if c > keys[-1]:
        return float(mult_map[keys[-1]])
    return 1.0


def soft_size_fraction(conviction, limits: RiskLimits) -> tuple:
    """Public API contract; production-derived narrative omitted."""
    confident = (limits.confident_full_size
                 and conviction >= limits.cap_bypass_min_conviction)
    curve_pct = curve_fraction(conviction, limits.conviction_size_curve, limits.max_trade_pct)
    soft_pct = curve_pct if confident else min(curve_pct, limits.max_trade_pct)
    conv_size_mult = conviction_multiplier(
        conviction, getattr(limits, "conviction_size_multipliers", None))
    if conv_size_mult != 1.0:
        soft_pct = soft_pct * conv_size_mult
    return soft_pct, confident


def conviction_size_authority(limits: RiskLimits) -> Dict[str, object]:
    """Public API contract; production-derived narrative omitted."""
    per_conviction = {c: soft_size_fraction(c, limits)[0] for c in range(1, 11)}
    values = list(per_conviction.values())
    low, high = min(values), max(values)
    return {
        "per_conviction_soft_pct": per_conviction,
        "min_soft_pct": low,
        "max_soft_pct": high,
        "spread_pct_points": high - low,
        "is_flat": (high - low) <= 1e-12,
        "bypass_reachable": bool(limits.confident_full_size)
                            and int(limits.cap_bypass_min_conviction) <= 10,
    }


def evaluate_trade(
    trade: ProposedTrade,
    *,
    net_liq: float,
    available_funds: float,
    open_positions: List[OpenPosition],
    pot_day_start: float,
    approved_names: Set[str],
    limits: RiskLimits,
    regime_info: Optional[dict] = None,
    external_book: Optional[ExternalBook] = None,
) -> GateDecision:
    """Public API contract; production-derived narrative omitted."""
    reasons: List[str] = []
    pot = effective_pot(net_liq, limits.pot_cap_usd)
    EPS = 1e-9
    u = trade.underlying.upper()

















    soft_pct, confident = soft_size_fraction(trade.conviction, limits)



    size_mult = regime_mod.size_multiplier(regime_info, getattr(trade, "is_long", True))
    per_trade_cap = min(soft_pct * pot * size_mult, available_funds)


    hard_cap = limits.max_trade_pct_hard * pot
    per_trade_cap = min(per_trade_cap, hard_cap, available_funds)



    cash_floor = max(0.0, limits.cash_buffer_pct) * pot
    deployable = max(0.0, available_funds - cash_floor)
    per_trade_cap = min(per_trade_cap, deployable)


    if u not in INDEX_UNDERLYINGS and u in {n.upper() for n in limits.blocked_names}:
        reasons.append(f"{u} is on the blocklist (excluded single name)")



    if (not limits.allow_any_name
            and u not in INDEX_UNDERLYINGS
            and u not in {n.upper() for n in approved_names}):
        reasons.append(f"{u} not in allowed universe (SPY/QQQ/IWM or an approved single name)")





    if trade.notional > per_trade_cap + EPS:
        binding_pct = limits.max_trade_pct if not confident else limits.max_trade_pct_hard
        reasons.append(f"notional ${trade.notional:,.0f} exceeds {binding_pct:.0%}-of-pot cap ${per_trade_cap:,.0f}")




    if trade.notional > available_funds + EPS:
        reasons.append(f"notional ${trade.notional:,.0f} exceeds available funds ${available_funds:,.0f}")





    if len(open_positions) >= limits.max_concurrent:
        reasons.append(f"at max concurrent positions ({len(open_positions)}/{limits.max_concurrent})")


    dp = day_pnl_pct(pot, pot_day_start) if pot_day_start > 0 else 0.0
    if dp <= -limits.daily_halt_pct + EPS:
        reasons.append(f"daily circuit breaker: pot down {dp:.1%} (halt at -{limits.daily_halt_pct:.0%}) - no new entries")


    name_exposure_open = same_name_notional(open_positions, u)




    if not trade.is_index:








        name_exposure = name_exposure_open + trade.notional
        name_cap = limits.max_single_name_agg_pct * pot
        if name_exposure > name_cap + EPS:
            reasons.append(f"single-name exposure ${name_exposure:,.0f} would exceed {limits.max_single_name_agg_pct:.0%}-of-pot cap ${name_cap:,.0f}")










    if (not trade.is_index
            and limits.sector_map and limits.max_sector_agg_pct > 0):
        sec = sector_of(u, limits.sector_map)
        sec_exposure = sector_exposure(open_positions, u, trade.notional, limits.sector_map).get(sec, 0.0)
        sec_cap = limits.max_sector_agg_pct * pot
        if sec_exposure > sec_cap + EPS:



            if sec == UNCLASSIFIED_SECTOR:
                reasons.append(
                    f"unclassified exposure ${sec_exposure:,.0f} would exceed the "
                    f"{limits.max_sector_agg_pct:.0%}-of-pot cluster cap ${sec_cap:,.0f} -- {u} is not in "
                    f"trading.sector_map, so it is pooled with every other name whose correlation was "
                    f"never written down; classify it there to give it its own cluster")
            else:
                reasons.append(f"sector '{sec}' exposure ${sec_exposure:,.0f} would exceed {limits.max_sector_agg_pct:.0%}-of-pot cap ${sec_cap:,.0f}")



























    xb = external_book if external_book is not None else external_book_for(limits)
    xb_armed = bool(getattr(limits, "external_book_path", None)) or external_book is not None
    xb_cap_pct = (limits.max_cross_book_name_agg_pct
                  if getattr(limits, "max_cross_book_name_agg_pct", None) is not None
                  else limits.max_single_name_agg_pct)
    xb_record: Dict[str, object] = dict(xb.describe())
    xb_record.update({"armed": xb_armed, "enforced": False, "applies": (not trade.is_index),
                      "cap_pct": float(xb_cap_pct), "cap_usd": float(xb_cap_pct) * pot,
                      "ibkr_name_exposure": float(name_exposure_open),
                      "external_name_exposure": None, "combined_name_exposure": None})
    if xb_armed and not trade.is_index:
        if xb.readable:
            ext = xb.exposure(u)
            combined = name_exposure_open + ext + trade.notional
            xb_record["external_name_exposure"] = float(ext)
            xb_record["combined_name_exposure"] = float(combined)
            xb_record["enforced"] = True
            if ext > 0 and combined > xb_record["cap_usd"] + EPS:
                reasons.append(
                    f"cross-book {u} exposure ${combined:,.0f} "
                    f"(here ${name_exposure_open:,.0f} + external ${ext:,.0f} + this "
                    f"${trade.notional:,.0f}) would exceed {float(xb_cap_pct):.0%}-of-pot cap "
                    f"${xb_record['cap_usd']:,.0f}")
        elif name_exposure_open > 0:
            xb_record["enforced"] = True
            reasons.append(
                f"external book {xb.state} ({xb.error}) - refusing to ADD to {u}, which this "
                f"book already holds ${name_exposure_open:,.0f} of; cross-book exposure cannot "
                f"be verified")










    tp = _explicit_pct(getattr(trade, "profit_target_pct", None))
    sl = _explicit_pct(getattr(trade, "stop_pct", None))
    if tp is not None and sl is not None and tp > 0 and sl > 0 and sl > tp + EPS:
        reasons.append(f"reward:risk inverted: stop {sl:.0f}% exceeds profit target {tp:.0f}% (need target >= stop)")

    return GateDecision(approved=not reasons, reasons=reasons, pot_value=pot,
                        per_trade_cap=per_trade_cap, cross_book=xb_record)
