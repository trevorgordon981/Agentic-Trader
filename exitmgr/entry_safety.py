"""Fail-closed primitives shared by every BUY-to-open path.

The functions in this module are deliberately broker-free.  Entry callers perform their live
account/contract/quote reads, then pass the resulting values through these pure checks immediately
before ``placeOrder``.  Protective SELL-to-close orders do not use this module: a stand-down must
stop new risk without disarming exits.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import time
import uuid
from typing import Iterable, Mapping, Optional, Sequence, Tuple

from exitmgr import risk


DEFAULT_APPROVAL_TTL_SECONDS = 300
DEFAULT_MATERIAL_PRICE_PCT = 0.03
DEFAULT_NBBO_MAX_AGE_SECONDS = 10.0


@dataclass(frozen=True)
class SafetyResult:
    allowed: bool
    reasons: Tuple[str, ...] = ()


def _configured_path(raw: object, *, config_path: str, label: str) -> Tuple[Optional[Path], Optional[str]]:
    """Resolve a marker path relative to the config directory, refusing empty/invalid paths."""
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
    """Return (present, error).  Only ENOENT means clear; every other stat error blocks."""
    try:
        # lstat treats a dangling symlink as present; a broken marker link must not silently clear
        # a stand-down.
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
    """Fail closed unless both entry-halt markers are verifiably absent."""
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
    """Reject missing, non-finite, or nonsensical live account values."""
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
    """Require a fresh, two-sided executable quote captured by the caller's final request."""
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
    """The maximum BUY debit that crosses the final observed NBBO."""
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


def material_changes(
    original: object,
    refreshed: object,
    *,
    max_price_change_pct: float = DEFAULT_MATERIAL_PRICE_PCT,
) -> Tuple[str, ...]:
    """Describe changes that invalidate the original human approval."""
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
    """Build the same hard limits for slate/manual/trader paths from ``trading:``."""
    curve = trading.get("conviction_size_curve")
    multipliers = trading.get("conviction_size_multipliers")

    def _int_float_map(raw):
        if not raw:
            return None
        return {int(k): float(v) for k, v in dict(raw).items()}

    return risk.RiskLimits(
        max_trade_pct=float(trading.get("max_trade_pct", 0.12)),
        max_trade_pct_hard=float(trading.get("max_trade_pct_hard", 0.25)),
        max_concurrent=int(trading.get("max_concurrent", 4)),
        daily_halt_pct=float(trading.get("daily_halt_pct", 0.08)),
        max_single_name_agg_pct=float(trading.get("max_single_name_agg_pct", 0.36)),
        max_sector_agg_pct=float(trading.get("max_sector_agg_pct", 0.25)),
        sector_map={str(k).upper(): str(v) for k, v in dict(trading.get("sector_map") or {}).items()},
        pot_cap_usd=trading.get("pot_cap_usd"),
        cash_buffer_pct=float(trading.get("cash_buffer_pct", 0.05)),
        allow_any_name=bool(trading.get("allow_model_names", False)),
        confident_full_size=bool(trading.get("confident_full_size", False)),
        cap_bypass_min_conviction=int(trading.get("cap_bypass_min_conviction", 6)),
        conviction_size_curve=_int_float_map(curve),
        conviction_size_multipliers=_int_float_map(multipliers),
        blocked_names={str(n).upper() for n in (trading.get("blocked_names") or [])},
    )


def day_start_value(path: object, trading_day: str) -> SafetyResult | float:
    """Read the immutable daily risk baseline. Missing/corrupt/current-day-missing blocks entry."""
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
    """Marker-only preflight for shell entry points. It performs no broker or network I/O."""
    import argparse
    import yaml

    ap = argparse.ArgumentParser(description="fail-closed entry marker preflight")
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()
    try:
        with open(args.config) as fh:
            cfg = yaml.safe_load(fh)
        if not isinstance(cfg, dict):
            raise ValueError("config root must be a mapping")
        kill = (cfg.get("kill_switch") or {}).get("path")
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
