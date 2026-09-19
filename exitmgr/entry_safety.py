"""Public API contract; production-derived narrative omitted."""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import time
import uuid
from typing import Iterable, Mapping, Optional, Sequence, Tuple

from exitmgr import risk





DEFAULT_APPROVAL_TTL_SECONDS = 1800
DEFAULT_MATERIAL_PRICE_PCT = 0.03










DEFAULT_MATERIAL_PROTECTIVE_PTS = 0.5
DEFAULT_NBBO_MAX_AGE_SECONDS = 10.0

AUTONOMOUS_WITHIN_RISK_GATES = "autonomous_within_risk_gates"
HUMAN_SLACK_APPROVAL = "human_slack_approval"


@dataclass(frozen=True)
class SafetyResult:
    allowed: bool
    reasons: Tuple[str, ...] = ()


@dataclass(frozen=True)
class EntrySubmissionAuthority:
    """Public API contract; production-derived narrative omitted."""

    mode: str
    approval_deadline_monotonic: Optional[float] = None

    @classmethod
    def autonomous(cls) -> "EntrySubmissionAuthority":
        return cls(AUTONOMOUS_WITHIN_RISK_GATES)

    @classmethod
    def human(
        cls,
        approved_at_monotonic: float,
        *,
        ttl_seconds: int = DEFAULT_APPROVAL_TTL_SECONDS,
    ) -> "EntrySubmissionAuthority":
        approved_at = float(approved_at_monotonic)
        ttl = max(1, int(ttl_seconds))
        deadline = approved_at + ttl
        if not math.isfinite(approved_at) or approved_at <= 0 or not math.isfinite(deadline):
            raise ValueError("human approval timestamp/deadline must be finite and positive")
        return cls(HUMAN_SLACK_APPROVAL, deadline)


class ApprovalExpiredAtSubmit(RuntimeError):
    """Public API contract; production-derived narrative omitted."""

    alfred_place_invoked = False


def submission_authority_valid(
    authority: object,
    *,
    now_monotonic: Optional[float] = None,
) -> SafetyResult:
    """Public API contract; production-derived narrative omitted."""
    if not isinstance(authority, EntrySubmissionAuthority):
        return SafetyResult(False, ("entry submission authority is missing or malformed",))
    if authority.mode == AUTONOMOUS_WITHIN_RISK_GATES:
        if authority.approval_deadline_monotonic is not None:
            return SafetyResult(False, ("autonomous authority must not carry a human deadline",))
        return SafetyResult(True)
    if authority.mode != HUMAN_SLACK_APPROVAL:
        return SafetyResult(False, (f"unknown entry submission authority {authority.mode!r}",))
    try:
        deadline = float(authority.approval_deadline_monotonic)
        now = time.monotonic() if now_monotonic is None else float(now_monotonic)
    except Exception as exc:
        return SafetyResult(False, (f"human approval deadline unavailable: {exc}",))
    if not math.isfinite(deadline) or deadline <= 0 or not math.isfinite(now):
        return SafetyResult(False, ("human approval deadline/clock is invalid",))
    if now > deadline:
        return SafetyResult(
            False,
            (f"human approval expired before broker submit by {now - deadline:.3f}s",),
        )
    return SafetyResult(True)


def place_with_live_authority(authority: object, place_callable):
    """Public API contract; production-derived narrative omitted."""
    checked = submission_authority_valid(authority)
    if not checked.allowed:
        raise ApprovalExpiredAtSubmit("; ".join(checked.reasons))
    return place_callable()


def short_option_entry_authority(
    *,
    credit: object,
    credit_entries_enabled: object,
    assigned_stock_authority_enabled: object,
) -> SafetyResult:
    """Public API contract; production-derived narrative omitted."""
    if credit is not True:
        return SafetyResult(True)
    reasons = []
    if credit_entries_enabled is not True:
        reasons.append("credit entries are disabled (credit_entries_enabled=false)")
    if assigned_stock_authority_enabled is not True:
        reasons.append(
            "short-option entry is disabled because assigned-stock authority is not separately "
            "qualified (assigned_stock_authority_enabled=false)"
        )
    return SafetyResult(not reasons, tuple(reasons))















NO_EARNINGS_ETFS = frozenset({
    "SPY", "QQQ", "IWM", "DIA", "SMH", "SOXL", "SYMX", "SCHD", "XLF", "XLK", "DRAQ",
    "SYMG",
    "XLV", "XLE", "XLB",
    "USD",
})





LEVERAGED_ETFS = frozenset({"NVDX", "SOXL", "SYMX", "USD"})


def is_no_earnings_etf(symbol) -> bool:
    """Public API contract; production-derived narrative omitted."""
    try:
        return str(symbol or "").strip().upper() in NO_EARNINGS_ETFS
    except Exception:
        return False


def is_leveraged_etf(symbol) -> bool:
    """Public API contract; production-derived narrative omitted."""
    try:
        return str(symbol or "").strip().upper() in LEVERAGED_ETFS
    except Exception:
        return False


def _configured_path(raw: object, *, config_path: str, label: str) -> Tuple[Optional[Path], Optional[str]]:
    """Public API contract; production-derived narrative omitted."""
    if raw is None:
        return None, f"{label} path is missing"
    if not isinstance(raw, (str, os.PathLike)):
        return None, f"{label} path must be a filesystem path"
    try:
        text = str(raw).strip()
    except Exception as exc:
        return None, f"{label} path unreadable: {exc}"
    if not text:
        return None, f"{label} path is empty"
    try:
        p = Path(text).expanduser()
        if not p.is_absolute():
            p = Path(config_path).expanduser().resolve().parent / p
        return p, None
    except Exception as exc:
        return None, f"{label} path invalid: {exc}"


def _marker_state(path: Path, label: str) -> Tuple[bool, Optional[str]]:
    """Public API contract; production-derived narrative omitted."""
    try:


        path.lstat()
        return True, None
    except FileNotFoundError:
        return False, None
    except OSError as exc:
        return False, f"cannot verify {label} at {path}: {exc}"


def entry_markers_clear(
    *,
    config_path: str,
    kill_switch_path: object,
    trading_down_path: Optional[object] = None,
) -> SafetyResult:
    """Public API contract; production-derived narrative omitted."""
    cfg = Path(config_path).expanduser().resolve()
    raw_td = trading_down_path if trading_down_path is not None else cfg.parent / "TRADING_DOWN"
    reasons = []
    for raw, label in ((raw_td, "TRADING_DOWN"), (kill_switch_path, "KILL_SWITCH")):
        p, err = _configured_path(raw, config_path=str(cfg), label=label)
        if err:
            reasons.append(err)
            continue
        present, stat_err = _marker_state(p, label)
        if stat_err:
            reasons.append(stat_err)
        elif present:
            reasons.append(f"{label} active ({p})")
    return SafetyResult(not reasons, tuple(reasons))


def account_snapshot_valid(snapshot: object) -> SafetyResult:
    """Public API contract; production-derived narrative omitted."""
    reasons = []
    values = {}
    for name in ("net_liq", "available_funds", "cash"):
        try:
            value = float(getattr(snapshot, name))
        except Exception as exc:
            reasons.append(f"account {name} unavailable: {exc}")
            continue
        values[name] = value
        if not math.isfinite(value):
            reasons.append(f"account {name} is non-finite")
    if values.get("net_liq", 0.0) <= 0:
        reasons.append("account net_liq must be positive")
    if values.get("available_funds", -1.0) < 0:
        reasons.append("account available_funds must be non-negative")
    return SafetyResult(not reasons, tuple(reasons))


def nbbo_valid(
    resolved: object,
    *,
    max_age_seconds: float = DEFAULT_NBBO_MAX_AGE_SECONDS,
    now_monotonic: Optional[float] = None,
) -> SafetyResult:
    """Public API contract; production-derived narrative omitted."""
    reasons = []
    try:
        bid = float(getattr(resolved, "entry_bid"))
        ask = float(getattr(resolved, "entry_ask"))
    except Exception as exc:
        return SafetyResult(False, (f"fresh NBBO unavailable: {exc}",))
    if not math.isfinite(bid) or bid <= 0:
        reasons.append("fresh NBBO bid is missing/non-positive")
    if not math.isfinite(ask) or ask <= 0:
        reasons.append("fresh NBBO ask is missing/non-positive")
    if not reasons and ask < bid:
        reasons.append(f"fresh NBBO is crossed (bid {bid:g} > ask {ask:g})")
    observed = getattr(resolved, "quote_observed_at", None)
    try:
        observed_f = float(observed)
        if not math.isfinite(observed_f) or observed_f <= 0:
            raise ValueError("invalid timestamp")
        now_f = time.monotonic() if now_monotonic is None else float(now_monotonic)
        age = now_f - observed_f
        if age < 0:
            reasons.append("fresh NBBO clock moved backwards")
        elif age > float(max_age_seconds):
            reasons.append(
                f"fresh NBBO is stale ({age:.1f}s > {float(max_age_seconds):.1f}s)")
    except Exception:
        reasons.append("fresh NBBO observation timestamp is missing")
    return SafetyResult(not reasons, tuple(reasons))


def executable_price(resolved: object) -> float:
    """Public API contract; production-derived narrative omitted."""
    return round(float(getattr(resolved, "entry_ask")), 2)


def contract_fingerprint(resolved: object) -> Tuple[object, ...]:
    long_contract = getattr(resolved, "contract", None)
    short_contract = getattr(resolved, "short_contract", None)
    return (
        str(getattr(resolved, "underlying", "")).upper(),
        str(getattr(resolved, "right", "")).upper(),
        str(getattr(resolved, "expiry", "")),
        float(getattr(resolved, "strike", 0.0)),
        int(getattr(long_contract, "conId", 0) or 0),
        float(getattr(resolved, "short_strike", 0.0) or 0.0),
        int(getattr(short_contract, "conId", 0) or 0),
    )


def _protective_pct(order: object, field: str) -> Tuple[bool, float]:
    """Public API contract; production-derived narrative omitted."""
    raw = getattr(order, field, None)
    if raw is None:
        return False, 0.0
    value = float(raw)
    if not math.isfinite(value) or value <= 0.0:
        return False, 0.0
    return True, value


def _protective_text(is_set: bool, value: float) -> str:
    return ("%.1f%%" % value) if is_set else "default rule"


def material_changes(
    original: object,
    refreshed: object,
    *,
    max_price_change_pct: float = DEFAULT_MATERIAL_PRICE_PCT,
    max_protective_change_pts: float = DEFAULT_MATERIAL_PROTECTIVE_PTS,
) -> Tuple[str, ...]:
    """Public API contract; production-derived narrative omitted."""
    changes = []
    try:
        if contract_fingerprint(original) != contract_fingerprint(refreshed):
            changes.append("contract/structure changed")
    except Exception as exc:
        changes.append(f"contract identity could not be compared: {exc}")
    try:
        if int(getattr(original, "qty")) != int(getattr(refreshed, "qty")):
            changes.append(f"quantity changed {getattr(original, 'qty')} -> {getattr(refreshed, 'qty')}")
    except Exception as exc:
        changes.append(f"quantity could not be compared: {exc}")
    try:
        old = executable_price(original)
        new = executable_price(refreshed)
        if old <= 0 or new <= 0:
            raise ValueError("non-positive executable price")
        move = abs(new - old) / old
        if move > max(0.0, float(max_price_change_pct)) + 1e-12:
            changes.append(f"executable price changed {move:.1%} (${old:.2f} -> ${new:.2f})")
    except Exception as exc:
        changes.append(f"executable price could not be compared: {exc}")
    band = max(0.0, float(max_protective_change_pts))
    for field, label in (("tp_pct", "take-profit"), ("sl_pct", "stop-loss")):
        try:
            had, old_pct = _protective_pct(original, field)
            has, new_pct = _protective_pct(refreshed, field)
            if had != has:



                changes.append(
                    f"{label} changed {_protective_text(had, old_pct)} -> "
                    f"{_protective_text(has, new_pct)}")
            elif had and abs(new_pct - old_pct) > band + 1e-12:
                changes.append(
                    f"{label} changed {old_pct:.1f}% -> {new_pct:.1f}% "
                    f"({new_pct - old_pct:+.1f} pts, band {band:.1f} pts)")
        except Exception as exc:
            changes.append(f"{label} could not be compared: {exc}")
    return tuple(changes)


def approval_expired(
    posted_monotonic: float,
    *,
    ttl_seconds: int = DEFAULT_APPROVAL_TTL_SECONDS,
    now_monotonic: Optional[float] = None,
) -> SafetyResult:
    try:
        age = (time.monotonic() if now_monotonic is None else float(now_monotonic)) - float(posted_monotonic)
        ttl = max(1, int(ttl_seconds))
    except Exception as exc:
        return SafetyResult(False, (f"approval age unavailable: {exc}",))
    if age < 0:
        return SafetyResult(False, ("approval clock moved backwards",))
    if age > ttl:
        return SafetyResult(False, (f"approval expired after {age:.0f}s (TTL {ttl}s)",))
    return SafetyResult(True)


def new_decision_id() -> str:
    return f"decision-{uuid.uuid4().hex}"


def decision_order_ref(decision_id: str) -> str:
    text = str(decision_id)
    if not text.startswith("decision-") or len(text) != 41:
        raise ValueError("invalid decision_id")
    return f"alfred-entry:{text[9:]}"


def risk_limits_from_config(trading: Mapping[str, object]) -> risk.RiskLimits:
    """Public API contract; production-derived narrative omitted."""
    get = trading.get if isinstance(trading, Mapping) else lambda key, default=None: getattr(
        trading, key, default)
    curve = get("conviction_size_curve")
    multipliers = get("conviction_size_multipliers")

    def _int_float_map(raw):
        if not raw:
            return None
        return {int(k): float(v) for k, v in dict(raw).items()}













    xb_path = str(get("external_book_path", "") or "").strip()






    xb_age = get("external_book_max_age_s", risk.DEFAULT_EXTERNAL_BOOK_MAX_AGE_S)
    try:
        xb_age = float(xb_age)
    except (TypeError, ValueError):
        xb_age = risk.DEFAULT_EXTERNAL_BOOK_MAX_AGE_S

    return risk.RiskLimits(
        max_trade_pct=float(get("max_trade_pct", 0.12)),
        max_trade_pct_hard=float(get("max_trade_pct_hard", 0.25)),
        max_concurrent=int(get("max_concurrent", 4)),
        daily_halt_pct=float(get("daily_halt_pct", 0.08)),
        max_single_name_agg_pct=float(get("max_single_name_agg_pct", 0.36)),
        max_sector_agg_pct=float(get("max_sector_agg_pct", 0.25)),
        sector_map={str(k).upper(): str(v) for k, v in dict(get("sector_map") or {}).items()},
        pot_cap_usd=get("pot_cap_usd"),
        cash_buffer_pct=float(get("cash_buffer_pct", 0.05)),
        allow_any_name=bool(get("allow_model_names", False)),
        confident_full_size=bool(get("confident_full_size", False)),
        cap_bypass_min_conviction=int(get(
            "cap_bypass_min_conviction", get("confident_conviction", 6))),
        conviction_size_curve=_int_float_map(curve),
        conviction_size_multipliers=_int_float_map(multipliers),
        blocked_names={str(n).upper() for n in (get("blocked_names") or [])},
        external_book_path=xb_path if xb_path else None,
        external_book_max_age_s=xb_age,
    )


def day_start_value(path: object, trading_day: str) -> SafetyResult | float:
    """Public API contract; production-derived narrative omitted."""
    import json

    try:
        data = json.loads(Path(path).read_text())
        value = float(data[trading_day])
        if not math.isfinite(value) or value <= 0:
            raise ValueError("baseline must be positive and finite")
        return value
    except Exception as exc:
        return SafetyResult(False, (f"daily risk baseline unavailable for {trading_day}: {exc}",))


def _cli() -> int:
    """Public API contract; production-derived narrative omitted."""
    import argparse
    from exitmgr.config import load_config

    ap = argparse.ArgumentParser(description="fail-closed entry marker preflight")
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()
    try:
        cfg = load_config(args.config)
        kill = cfg.kill_switch.path
    except Exception as exc:
        print(f"[entry-safety] BLOCKED: config unavailable/invalid: {exc}")
        return 2
    result = entry_markers_clear(config_path=args.config, kill_switch_path=kill)
    if not result.allowed:
        print("[entry-safety] BLOCKED: " + "; ".join(result.reasons))
        return 2
    print("[entry-safety] entry markers clear")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
