"""Public API contract; production-derived narrative omitted."""

import difflib
import math
import os
from pathlib import Path
from typing import Optional, get_args, get_origin

import typer
import yaml
from dataclasses import dataclass, field, replace
from typing_extensions import Annotated




from exitmgr.risk import UNCLASSIFIED_SECTOR



class ConfigError(ValueError):
    """Public API contract; production-derived narrative omitted."""

    def __init__(self, key_path, message):
        self.key_path = key_path
        super().__init__("%s: %s" % (key_path, message))


def _fields_of(dc):
    return {f.name for f in dc.__dataclass_fields__.values()}
































@dataclass(frozen=True)
class _Spec:
    """Public API contract; production-derived narrative omitted."""
    kind: str = ""
    lo: Optional[float] = None
    hi: Optional[float] = None
    lo_ex: bool = False
    hi_ex: bool = False
    choices: Optional[tuple] = None
    elem: Optional["_Spec"] = None
    key: Optional["_Spec"] = None
    allow_none: bool = False
    nonempty: bool = False
    forbid: Optional[tuple] = None


_KIND_WORDS = {"bool": "a boolean (true/false)", "int": "an integer", "number": "a number",
               "str": "a string", "list": "a list", "dict": "a mapping"}


def _kind_of(value):
    """Public API contract; production-derived narrative omitted."""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "dict"
    return None


def _annotation_kind(ann):
    """Public API contract; production-derived narrative omitted."""
    if isinstance(ann, str):
        return None, False
    allow_none = False
    args = get_args(ann)
    if args and type(None) in args:
        allow_none = True
        rest = [a for a in args if a is not type(None)]
        if len(rest) != 1:
            return None, allow_none
        ann = rest[0]
        args = get_args(ann)
    base = get_origin(ann) or ann
    return {bool: "bool", int: "int", float: "number", str: "str",
            list: "list", dict: "dict"}.get(base), allow_none


def _refuse(path, value, want):
    raise ConfigError(path, "expected %s, got %s %r" % (want, type(value).__name__, value))


def _check_range(path, value, spec):
    lo, hi = spec.lo, spec.hi
    lo_ok = lo is None or (value > lo if spec.lo_ex else value >= lo)
    hi_ok = hi is None or (value < hi if spec.hi_ex else value <= hi)
    if lo_ok and hi_ok:
        return
    bits = []
    if lo is not None:
        bits.append("%s %g" % (">" if spec.lo_ex else ">=", lo))
    if hi is not None:
        bits.append("%s %g" % ("<" if spec.hi_ex else "<=", hi))
    raise ConfigError(path, "must be %s, got %r" % (" and ".join(bits), value))


def _check_value(path, value, spec):
    """Public API contract; production-derived narrative omitted."""
    if value is None:
        if spec.allow_none:
            return None
        raise ConfigError(path, "must not be null -- omit the key to keep its default instead")

    kind = spec.kind
    if kind == "bool":



        if not isinstance(value, bool):
            _refuse(path, value,
                    'a boolean (true/false; note the STRING "false" is TRUE in Python)')
    elif kind == "int":
        if isinstance(value, bool):
            _refuse(path, value, "an integer, not a boolean")
        if isinstance(value, float):
            if not math.isfinite(value) or not value.is_integer():
                _refuse(path, value, "a whole number")
            value = int(value)
        elif not isinstance(value, int):
            _refuse(path, value, "an integer")
        _check_range(path, value, spec)
    elif kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            _refuse(path, value, "a number")
        if not math.isfinite(float(value)):
            raise ConfigError(path, "must be a finite number, got %r" % (value,))
        _check_range(path, value, spec)
    elif kind == "str":
        if not isinstance(value, str):
            _refuse(path, value, "a string")



        if spec.nonempty and not value.strip():
            raise ConfigError(path, "must not be blank -- omit the entry instead")
        if spec.forbid and value.strip() in spec.forbid:
            raise ConfigError(path, "must not be %r -- that value is reserved by the code that "
                                    "reads this key" % value.strip())
    elif kind == "list":
        if not isinstance(value, list):
            _refuse(path, value, "a list")
        if spec.elem is not None:
            return [_check_value("%s[%d]" % (path, i), v, spec.elem)
                    for i, v in enumerate(value)]
        return value
    elif kind == "dict":
        if not isinstance(value, dict):
            _refuse(path, value, "a mapping")
        if spec.key is None and spec.elem is None:
            return value
        out = {}
        for k, v in value.items():
            sub = "%s.%s" % (path, k)
            out[_check_value(sub, k, spec.key) if spec.key is not None else k] = (
                _check_value(sub, v, spec.elem) if spec.elem is not None else v)
        return out
    else:
        return value

    if spec.choices is not None and value not in spec.choices:
        raise ConfigError(path, "must be one of %s, got %r"
                          % (", ".join(repr(c) for c in spec.choices), value))
    return value




_FRAC = _Spec(lo=0.0, hi=1.0, lo_ex=True)
_FRAC_OPEN = _Spec(lo=0.0, hi=1.0, lo_ex=True, hi_ex=True)
_FRAC_0OK = _Spec(lo=0.0, hi=1.0, hi_ex=True)
_PCT = _Spec(lo=0.0, hi=100.0, lo_ex=True)
_GAIN_PCT = _Spec(lo=0.0, hi=1000.0, lo_ex=True)
_POS = _Spec(lo=0.0, lo_ex=True)
_POS_INT = _Spec(lo=1)
_NONNEG_INT = _Spec(lo=0)



_CONVICTION = _Spec(lo=1, hi=11)

_SPECS = {
    "ib.port": _Spec(lo=1, hi=65535),
    "ib.client_id": _NONNEG_INT,
    "ib.protective_client_id": _NONNEG_INT,
    "ib.market_data_type": _Spec(choices=(1, 2, 3, 4)),
    "loop.interval_seconds": _POS_INT,
    "scope.mode": _Spec(choices=("journal", "all")),
    "trading.broker_protection_mode": _Spec(
        choices=("disabled", "shadow", "fixed_stop", "ratcheted")),
    "caps.max_orders_per_cycle": _POS_INT,
    "caps.max_orders_per_day": _POS_INT,
    "caps.max_notional_per_day": _POS,

    "rules.profit_target_pct": _GAIN_PCT,
    "rules.stop_pct": _PCT,
    "rules.time_stop_days": _POS_INT,
    "rules.exit_slippage_floor": _FRAC,
    "rules.trailing.activation_gain_pct": _GAIN_PCT,
    "rules.trailing.giveback_fraction": _FRAC_OPEN,
    "rules.auto_trail.activation_gain_pct": _GAIN_PCT,
    "rules.auto_trail.giveback_fraction": _FRAC_OPEN,
    "rules.scale_out.first_target_pct": _GAIN_PCT,



    "rules.atr_levels.k_arm": _POS,
    "rules.atr_levels.dynamic_k_arm": _POS,
    "rules.atr_levels.quote_max_age_s": _POS,
    "rules.atr_levels.quote_max_skew_s": _POS,
    "rules.atr_levels.max_atr_age_days": _POS_INT,
    "loop.protective_cycle_timeout_seconds": _POS,
    "loop.protective_poll_seconds": _POS,
    "rules.atr_levels.k_trail": _POS,
    "rules.atr_levels.k_stop": _POS,
    "rules.atr_levels.k_stop_horizon": _POS,
    "rules.atr_levels.lapse_halflife_days": _POS,
    "rules.atr_levels.lapse_floor_days": _POS,
    "rules.atr_levels.hold_backstop_multiple": _POS,
    "rules.atr_levels.wind_down_start_dte": _POS_INT,
    "rules.atr_levels.min_stop_pct": _PCT,
    "rules.atr_levels.gb_min": _FRAC_OPEN,
    "rules.atr_levels.gb_max": _FRAC_OPEN,

    "construction.min_dte": _POS_INT,
    "construction.prefer_dte_max": _POS_INT,
    "construction.max_positions_per_expiry": _POS_INT,
    "construction.credit_min_dte": _POS_INT,
    "construction.credit_max_dte": _POS_INT,
    "construction.dte_exit_threshold": _POS_INT,
    "construction.fill_alarm_minutes": _POS_INT,
    "construction.tp_pct": _Spec(lo=0.0, hi=10.0, lo_ex=True),
    "construction.tp_min_pct": _Spec(lo=0.0, hi=10.0, lo_ex=True),
    "construction.tp_max_pct": _Spec(lo=0.0, hi=10.0, lo_ex=True),
    "construction.sl_pct": _Spec(lo=-1.0, hi=0.0, hi_ex=True),
    "construction.max_premium_pct": _FRAC,
    "construction.max_deployed_pct": _FRAC,
    "construction.max_decay_pct_per_day": _FRAC,
    "construction.max_portfolio_decay_pct_per_day": _FRAC,
    "construction.spread_width_max_pct": _FRAC,
    "construction.strike_near_spot_pct": _FRAC,
    "construction.delta_min": _FRAC_OPEN,
    "construction.delta_max": _FRAC_OPEN,
    "construction.stage_b_quote_max_age_s": _POS,
    "construction.max_entry_spread_pct": _PCT,


    "construction.max_combo_spread_pct": _PCT,
    "construction.max_leveraged_etf_combo_spread_pct": _PCT,
    "construction.earnings_hold_slip_mult": _POS,
    "construction.earnings_blackout_days": _NONNEG_INT,
    "construction.assignment_cushion_days": _NONNEG_INT,

    "trading.max_trade_pct": _FRAC,








    "trading.max_single_name_agg_pct": _FRAC,
    "trading.max_trade_pct_hard": _FRAC,
    "trading.daily_halt_pct": _FRAC,
    "trading.max_sector_agg_pct": _FRAC,
    "trading.cash_buffer_pct": _FRAC_0OK,
    "trading.entry_limit_buffer_pct": _FRAC_0OK,
    "trading.max_concurrent": _POS_INT,
    "trading.confident_conviction": _CONVICTION,
    "trading.cap_bypass_min_conviction": _CONVICTION,
    "trading.add_suggest_min_conviction": _CONVICTION,
    "trading.reload_conviction_min": _CONVICTION,
    "trading.reload_friction_k": _POS,
    "trading.reload_expected_continuation_pct": _POS,
    "trading.reload_max_per_name_per_day": _NONNEG_INT,
    "trading.reload_ttl_cycles": _POS_INT,

    "trading.manage_positions_interval_s": _Spec(kind="number", lo=0.0, lo_ex=True),
    "trading.manage_positions_max_age_s": _Spec(kind="number", lo=0.0, lo_ex=True),

    "trading.pot_cap_usd": _Spec(kind="number", lo=0.0, lo_ex=True, allow_none=True),
    "trading.intended_hold_days_fallback": _Spec(kind="number", lo=0.0, lo_ex=True,
                                                 allow_none=True),
    "trading.conviction_size_curve": _Spec(kind="dict", allow_none=True,
                                           key=_Spec(kind="int", lo=1, hi=10),
                                           elem=_Spec(kind="number", lo=0.0, hi=1.0, lo_ex=True)),
    "trading.conviction_size_multipliers": _Spec(kind="dict", allow_none=True,
                                                 key=_Spec(kind="int", lo=1, hi=10),
                                                 elem=_Spec(kind="number", lo=0.0, hi=5.0,
                                                            lo_ex=True)),
    "trading.approver_ids": _Spec(elem=_Spec(kind="str")),
    "trading.approved_names": _Spec(elem=_Spec(kind="str")),
    "trading.blocked_names": _Spec(elem=_Spec(kind="str")),
    "trading.blocked_sector_keywords": _Spec(elem=_Spec(kind="str")),



    "trading.sector_map": _Spec(key=_Spec(kind="str", nonempty=True),
                                elem=_Spec(kind="str", nonempty=True,
                                           forbid=(UNCLASSIFIED_SECTOR,))),
}


def _spec_for(path, kind, allow_none=False):
    """Public API contract; production-derived narrative omitted."""
    spec = _SPECS.get(path)
    if spec is not None:
        kind = spec.kind or kind
        allow_none = spec.allow_none or allow_none
    if kind is None:
        return None
    return (replace(spec, kind=kind, allow_none=allow_none) if spec is not None
            else _Spec(kind=kind, allow_none=allow_none))


def _validate_fields(dc, d, prefix, skip=()):
    """Public API contract; production-derived narrative omitted."""
    fields = dc.__dataclass_fields__
    for name in list(d):
        if name in skip:
            continue
        path = ("%s.%s" % (prefix, name)) if prefix else str(name)
        kind, allow_none = _annotation_kind(fields[name].type)
        spec = _spec_for(path, kind, allow_none)
        if spec is not None:
            d[name] = _check_value(path, d[name], spec)





















REQUIRED_KEYS = ("caps.max_orders_per_day", "caps.max_notional_per_day")


def require_present(data, prefix=""):
    """Public API contract; production-derived narrative omitted."""
    missing = [p for p in REQUIRED_KEYS
               if p.rpartition(".")[0] == (prefix or "")
               and p.rpartition(".")[2] not in (data or {})]
    if missing:
        raise ConfigError(missing[0],
                          "required configuration key is missing. It has no safe default: the "
                          "slate (daily_recommend.py) and the manual lane (place_trade.py) read "
                          "an absent daily ceiling as NO ceiling at all. State it explicitly. "
                          "Missing: %s" % ", ".join(missing))


def _checked(prefix, data, known):
    """Public API contract; production-derived narrative omitted."""
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(prefix or "<root>",
                          "expected a mapping, got %s" % type(data).__name__)
    for k in data:
        if k in known:
            continue
        path = ("%s.%s" % (prefix, k)) if prefix else str(k)
        near = difflib.get_close_matches(str(k), sorted(known), n=1, cutoff=0.7)
        hint = (" -- did you mean %r?" % near[0]) if near else ""
        raise ConfigError(path, "unknown configuration key%s" % hint)
    return dict(data)


def _build(dc, data, prefix, nested=None):
    """Public API contract; production-derived narrative omitted."""
    d = _checked(prefix, data, _fields_of(dc))


    require_present(d, prefix)
    nested = nested or {}



    _validate_fields(dc, d, prefix, skip=set(nested))
    for key, sub in nested.items():
        if key in d:
            d[key] = _build(sub, d[key], ("%s.%s" % (prefix, key)) if prefix else key)
    return dc(**d)


@dataclass
class IBConfig:
    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 42
    protective_client_id: int = 189
    market_data_type: int = 3


@dataclass
class JournalConfig:
    path: str = "./trades.log"


@dataclass
class StateConfig:
    path: str = "./exitmgr_state.json"


@dataclass
class KillSwitchConfig:
    path: str = "./KILL_SWITCH"


@dataclass
class LoopConfig:
    interval_seconds: int = 60
    protective_cycle_timeout_seconds: float = 25.0
    protective_poll_seconds: float = 30.0


@dataclass
class ScopeConfig:
    mode: str = "journal"


@dataclass
class CapsConfig:
    max_orders_per_cycle: int = 5
    max_orders_per_day: int = 20
    max_notional_per_day: float = 50000.0







    tp_tiers: list = field(default_factory=list)


@dataclass
class TrailingConfig:




    enabled: bool = False
    activation_gain_pct: float = 50.0
    giveback_fraction: float = 0.5


@dataclass
class AutoTrailConfig:
    """Public API contract; production-derived narrative omitted."""
    enabled: bool = True
    activation_gain_pct: float = 25.0
    giveback_fraction: float = 0.5


@dataclass
class AtrLevelsConfig:
    """Public API contract; production-derived narrative omitted."""
    enabled: bool = True
    k_arm: float = 1.5
    dynamic_activation_enabled: bool = False
    dynamic_k_arm: float = 1.0
    qualification_enabled: bool = False
    quote_max_age_s: float = 30.0
    quote_max_skew_s: float = 2.0
    max_atr_age_days: int = 4
    k_trail: float = 1.0
    k_stop: float = 2.0












    k_stop_horizon: float = 0.5
    horizon_scaling: bool = True





















































    wind_down_start_dte: int = 20
    hold_backstop_enabled: bool = True
    hold_backstop_multiple: float = 4.0
    lapse_tighten: bool = True
    lapse_halflife_days: float = 10.0
    lapse_floor_days: float = 1.0
    min_stop_pct: float = 8.0
    gb_min: float = 0.25
    gb_max: float = 0.75


@dataclass
class ScaleOutConfig:
    """Public API contract; production-derived narrative omitted."""
    enabled: bool = False
    first_target_pct: float = 20.0
    trim_fraction: float = 0.5

    def __post_init__(self):
        """Public API contract; production-derived narrative omitted."""
        if not self.enabled:
            return
        f = self.trim_fraction
        try:
            f = float(f)
        except (TypeError, ValueError):
            raise ValueError("construction/scale_out.trim_fraction must be a number in (0,1), "
                             "got %r" % (self.trim_fraction,))
        if f != f or not (0.0 < f < 1.0):
            raise ValueError("scale_out.trim_fraction must be strictly between 0 and 1 "
                             "(a trim, not a full close); got %r" % (self.trim_fraction,))


@dataclass
class ConstructionConfig:
    """Public API contract; production-derived narrative omitted."""
    min_dte: int = 25


    max_positions_per_expiry: int = 1
    prefer_dte_max: int = 170






    credit_min_dte: int = 3
    credit_max_dte: int = 45
    tp_pct: float = 0.30
    tp_min_pct: float = 0.25
    tp_max_pct: float = 0.35
    sl_pct: float = -0.30







    deterministic_construction: bool = False
    max_premium_pct: float = 0.25
    max_deployed_pct: float = 0.40
    max_decay_pct_per_day: float = 0.01
    max_portfolio_decay_pct_per_day: float = 0.04
    dte_exit_threshold: int = 10
    fill_alarm_minutes: int = 15
    delta_min: float = 0.55
    delta_max: float = 0.65





    stage_b_quote_max_age_s: float = 45.0


    max_entry_spread_pct: float = 25.0






    max_combo_spread_pct: float = 45.0



    max_leveraged_etf_combo_spread_pct: float = 50.0
    spread_width_max_pct: float = 0.08
    strike_near_spot_pct: float = 0.03
    earnings_blackout_enabled: bool = True

    earnings_block_hard: bool = False






    earnings_use_hold_window: bool = False
    earnings_hold_slip_mult: float = 2.0
    earnings_blackout_days: int = 0

    assignment_check_enabled: bool = True


    assignment_block_hard: bool = False


    assignment_cushion_days: int = 0



def construction_from_dict(d: Optional[dict]) -> ConstructionConfig:
    """Public API contract; production-derived narrative omitted."""
    key = "max_entry_spread_pct"
    qualified_key = f"construction.{key}"
    if not isinstance(d, dict) or key not in d:
        raise ValueError(f"{qualified_key} is required")

    raw_value = d[key]
    if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
        raise ValueError(f"{qualified_key} must be a finite positive number")
    value = float(raw_value)
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError(f"{qualified_key} must be a finite positive number")

    d = dict(d)
    d[key] = value





    if d.get("deterministic_construction") and not d.get("earnings_use_hold_window", False):
        print("[WARN] construction.deterministic_construction=true requires "
              "earnings_use_hold_window=true (the doctrine expiry lands past the plain earnings "
              "cutoff and would halt ALL entries). Enabling the hold-window gate.")
        d["earnings_use_hold_window"] = True




    return _build(ConstructionConfig, d, "construction")


@dataclass
class RulesConfig:
    protective_reprice_enabled: bool = False
    profit_target_pct: Optional[float] = None
    stop_pct: Optional[float] = None
    time_stop_days: Optional[int] = None
    trailing: TrailingConfig = field(default_factory=TrailingConfig)
    auto_trail: AutoTrailConfig = field(default_factory=AutoTrailConfig)
    atr_levels: AtrLevelsConfig = field(default_factory=AtrLevelsConfig)
    scale_out: ScaleOutConfig = field(default_factory=ScaleOutConfig)







    spread_exit_bid_anchor: bool = False
    exit_market_orders: bool = False

    exit_slippage_floor: float = 0.50






@dataclass
class Config:
    ib: IBConfig = field(default_factory=IBConfig)
    journal: JournalConfig = field(default_factory=JournalConfig)
    state: StateConfig = field(default_factory=StateConfig)
    kill_switch: KillSwitchConfig = field(default_factory=KillSwitchConfig)
    loop: LoopConfig = field(default_factory=LoopConfig)
    scope: ScopeConfig = field(default_factory=ScopeConfig)
    caps: CapsConfig = field(default_factory=CapsConfig)
    rules: RulesConfig = field(default_factory=RulesConfig)
    construction: ConstructionConfig = field(default_factory=ConstructionConfig)



    manage_positions: bool = True





    manage_positions_shadow_only: bool = False
    llm_endpoint: str = "http://127.0.0.1:8082/v1/chat/completions"
    llm_model: str = ""



    _SECTIONS = {"ib": IBConfig, "journal": JournalConfig, "state": StateConfig,
                 "kill_switch": KillSwitchConfig, "loop": LoopConfig, "scope": ScopeConfig,
                 "caps": CapsConfig}
    _RULES_NESTED = {"trailing": TrailingConfig, "auto_trail": AutoTrailConfig,
                     "atr_levels": AtrLevelsConfig, "scale_out": ScaleOutConfig}

    @classmethod
    def from_yaml(cls, path: str) -> "Config":
        """Public API contract; production-derived narrative omitted."""
        with open(path, "r") as f:
            data = yaml.safe_load(f)
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ConfigError("<root>", "expected a mapping at the top level, got %s"
                              % type(data).__name__)
        _checked("", data, set(cls._SECTIONS) | {"rules", "construction", "trading"})

        rules_cfg = _build(RulesConfig, data.get("rules"), "rules", cls._RULES_NESTED)

        rules_cfg.exit_market_orders = bool(rules_cfg.exit_market_orders)
        rules_cfg.spread_exit_bid_anchor = bool(rules_cfg.spread_exit_bid_anchor)
        rules_cfg.exit_slippage_floor = float(rules_cfg.exit_slippage_floor)

        cfg = cls(
            rules=rules_cfg,
            construction=construction_from_dict(data.get("construction")),
            **{name: _build(dc, data.get(name), name) for name, dc in cls._SECTIONS.items()})
        _apply_trading(cfg, data.get("trading"))
        return cfg








TRADING_DEFAULTS = [
    ('slack_channel', ''), ('approver_ids', []),
    ('alerts_channel', ''), ('summary_channel', ''),
    ('error_channel', ''),
    ('llm_endpoint', 'http://127.0.0.1:8082/v1/chat/completions'),
    ('llm_model', ''), ('manage_positions', True),
    ('manage_positions_shadow_only', False),


    ('broker_protection_mode', 'disabled'),







    ('manage_positions_interval_s', 300),
    ('manage_positions_max_age_s', 600),
    ('approved_names', []), ('allow_model_names', False),




    ('auto_approve_within_gates', False),
    ('pot_cap_usd', None), ('confident_full_size', False), ('confident_conviction', 4),
    ('cap_bypass_min_conviction', 6),

    ('cash_buffer_pct', 0.05),





    ('intended_hold_days_fallback', None),




    ('entry_limit_buffer_pct', 0.05),





    ('apewisdom_discovery', {}),
    ('add_suggest_min_conviction', 6),
    ('conviction_size_curve', None),
    ('conviction_size_multipliers', None),






    ('blocked_names', []), ('blocked_sector_keywords', []),
    ('max_sector_agg_pct', 0.25),

    ('sector_map', {}),
    ('max_trade_pct', 0.12), ('max_concurrent', 4), ('daily_halt_pct', 0.08),







    ('max_single_name_agg_pct', 0.36), ('max_trade_pct_hard', 0.25),














    ('credit_entries_enabled', False),




    ('assigned_stock_authority_enabled', False),
    ('reload_enabled', False),
    ('reload_conviction_min', 6),

    ('reload_friction_k', 1.5),

    ('reload_expected_continuation_pct', 3.0),












    ('reload_max_per_name_per_day', 2),
    ('reload_ttl_cycles', 3),















    ('external_book_path', ''),























    ('external_book_max_age_s', 259200.0),
    ('baseline_path', './day_baseline.json'), ('audit_path', './audit.jsonl')
]




_TRADING_KINDS = {k: _kind_of(v) for k, v in TRADING_DEFAULTS}


def _apply_trading(cfg, data):
    """Public API contract; production-derived narrative omitted."""
    tr = _checked("trading", data, {k for k, _ in TRADING_DEFAULTS})
    for _k, _d in TRADING_DEFAULTS:
        if _k in tr:


            _path = "trading.%s" % _k
            _spec = _spec_for(_path, _TRADING_KINDS.get(_k))
            if _spec is not None:
                tr[_k] = _check_value(_path, tr[_k], _spec)
        setattr(cfg, _k, tr.get(_k, _d))
    return cfg


def load_config(
    config_path: Annotated[str, typer.Option("--config", "-c", help="Path to config YAML")] = "config.yaml",
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Dry run mode (default true)")] = True,
    arm: Annotated[bool, typer.Option("--arm", help="Arm for live trading")] = False,
    loop: Annotated[bool, typer.Option("--loop", help="Run in loop mode")] = False,
    interval: Annotated[int, typer.Option("--interval", help="Loop interval in seconds")] = None,
    max_orders_cycle: Annotated[Optional[int], typer.Option("--max-orders-cycle")] = None,
    max_orders_day: Annotated[Optional[int], typer.Option("--max-orders-day")] = None,
    max_notional_day: Annotated[Optional[float], typer.Option("--max-notional-day")] = None,
) -> Config:
    """Public API contract; production-derived narrative omitted."""

    if os.path.exists(config_path):
        cfg = Config.from_yaml(config_path)
    else:


        cfg = Config()
        _apply_trading(cfg, None)


    if interval is not None:
        cfg.loop.interval_seconds = interval
    if max_orders_cycle is not None:
        cfg.caps.max_orders_per_cycle = max_orders_cycle
    if max_orders_day is not None:
        cfg.caps.max_orders_per_day = max_orders_day
    if max_notional_day is not None:
        cfg.caps.max_notional_per_day = max_notional_day


    cfg.dry_run = not arm
    cfg.arm = arm


    cfg.loop_mode = loop






    return cfg



Config.dry_run = True
Config.loop_mode = False
Config.arm = False
