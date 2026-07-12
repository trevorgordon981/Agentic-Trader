"""Configuration loading and CLI argument parsing."""

import os
from pathlib import Path
from typing import Optional

import typer
import yaml
from dataclasses import dataclass, field
from typing_extensions import Annotated


@dataclass
class IBConfig:
    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 42
    protective_client_id: int = 189  # dedicated CPU-only protective service; must differ from entry
    market_data_type: int = 3  # 1=live (paid subscription), 3=delayed (free, ~15min lag)


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


@dataclass
class ScopeConfig:
    mode: str = "journal"  # "journal" or "all"


@dataclass
class CapsConfig:
    max_orders_per_cycle: int = 5
    max_orders_per_day: int = 20
    max_notional_per_day: float = 50000.0
    # POT-TIERED TAKE-PROFIT RUNNER CEILING (2026-07-03). Ordered list of rows, each a dict
    # {min_pot, tp_max_pct, tp_pct}, scaling the TP ceiling (runner leash) + default target with
    # account size: bank profits fast when the pot is small, let runners run longer as it grows.
    # ONLY the take-profit side scales -- the protective STOP is never touched by any tier. An
    # EMPTY/absent list is a full NO-OP (today's flat construction.tp_max_pct/tp_pct behavior), so
    # clearing config reverts instantly. Consumed via construction.tp_tier_for_pot() at ENTRY only,
    # so each open trade keeps its entry-time target for life; only NEW trades scale.
    tp_tiers: list = field(default_factory=list)


@dataclass
class TrailingConfig:
    # Trailing stop that protects REALIZED gains (see rules.evaluate_trailing_stop).
    # activation_gain_pct: arm once the peak gain reaches this %.
    # giveback_fraction: once armed, exit if price gives back more than this fraction of the
    #   PEAK GAIN ABOVE ENTRY (0.4 => always keep >=60% of the peak gain). PRIOR, unvalidated.
    enabled: bool = False
    activation_gain_pct: float = 50.0
    giveback_fraction: float = 0.5


@dataclass
class AutoTrailConfig:
    """GAIN-PROTECTING AUTO-TRAIL SAFETY FLOOR (2026-07-03 Part 2). A WIDE trailing stop that
    auto-arms on ANY winner once its PEAK gain clears activation_gain_pct -- EVEN IF the model never
    says arm_trail and EVEN IF the global rules.trailing toggle is off. It runs UNDER the model's
    judgment: it only ever ADDS or WIDENS downside protection (a wide giveback so it does not choke a
    runner on option-vol noise) and rides the monotonic peak UP; the model can still WIDEN it or
    take_profit early. It NEVER suppresses the take-profit ceiling (only the model's arm_trail does
    that) and NEVER touches the protective stop. Purpose: stop an up-big winner round-tripping to
    breakeven when the model returns 'hold'. Shipped ENABLED with a wide default per Trevor's
    'protect gains by default' ask; tune the knobs in config.yaml `rules.auto_trail:`. Disable
    (enabled: false) for an exact no-op (byte-identical to the pre-feature behavior)."""
    enabled: bool = True
    activation_gain_pct: float = 25.0   # auto-arm a protective trail once PEAK gain >= this %
    giveback_fraction: float = 0.5      # wide floor: keep >= (1-this) of the peak gain-above-entry


@dataclass
class ScaleOutConfig:
    """Partial-trim / scale-out at a FIRST target below the full profit target. Consumed by
    rules.evaluate_scale_out; the manager acts on ExitTrigger.quantity_fraction. Priors below
    are UNVALIDATED until live MFE/MAE logging accrues -- eyeball before trading resumes."""
    enabled: bool = False
    first_target_pct: float = 20.0   # trim once gain >= this % (sits below rules.profit_target_pct)
    trim_fraction: float = 0.5       # fraction of CURRENT qty to close at the first target


@dataclass
class ConstructionConfig:
    """Trade-CONSTRUCTION rulebook thresholds (2026-07-01 journal-audit rework).
    All consumed via exitmgr.construction; tune in config.yaml `construction:`, not here."""
    min_dte: int = 25                          # floor on DTE at entry (prefer 25-45)
    prefer_dte_max: int = 45
    tp_pct: float = 0.30                       # default take-profit = +30% of debit
    tp_min_pct: float = 0.25                   # model TP clamped into [tp_min, tp_max]
    tp_max_pct: float = 0.35
    sl_pct: float = -0.30                      # default stop = -30% of debit (was -50%)
    max_premium_pct: float = 0.15              # premium per trade <= 15% of net-liq
    max_deployed_pct: float = 0.40             # total deployed premium <= 40% of net-liq
    max_decay_pct_per_day: float = 0.01        # (debit/DTE)/net-liq <= 1%/day per trade
    max_portfolio_decay_pct_per_day: float = 0.04
    dte_exit_threshold: int = 10               # exit/roll evaluation at DTE<=10 (keep = rules.time_stop_days)
    fill_alarm_minutes: int = 15               # unfilled-order Slack alarm (RTH)
    delta_min: float = 0.55                    # long-leg target-delta band
    delta_max: float = 0.65
    spread_width_max_pct: float = 0.08         # no-IV fallback: spread width <= 8% of spot
    strike_near_spot_pct: float = 0.03         # no-IV fallback: long strike within 3% of spot
    earnings_blackout_enabled: bool = True     # block DEBIT trades that hold THROUGH an earnings
                                               # print (IV-crush loser by construction). Fail-open
                                               # when the earnings date is unknown (caller flags it).
    earnings_blackout_days: int = 0            # cushion days BEYOND expiry also treated as blackout
                                               # (0 => block iff earnings <= expiry).
    assignment_check_enabled: bool = True      # gate A6: SURFACE early-assignment / ex-dividend risk
                                               # on a DEBIT SPREAD whose ITM short leg heads into an
                                               # ex-div date. Fail-open on unknown ex-div (caller flags).
    assignment_block_hard: bool = False        # A6 disposition: default WARN-only (pass + surface the
                                               # risk). True => hard-block (reject) instead -- early
                                               # assignment is manageable, so warn is the default.
    assignment_cushion_days: int = 0           # A6 cushion days BEYOND expiry also treated as at-risk
                                               # (0 => at-risk iff ex-div <= expiry).


def construction_from_dict(d: Optional[dict]) -> ConstructionConfig:
    """Build a ConstructionConfig from a raw YAML dict, ignoring unknown keys (so a config
    typo or future key never crashes the trader)."""
    d = d or {}
    known = {f.name for f in ConstructionConfig.__dataclass_fields__.values()}
    return ConstructionConfig(**{k: v for k, v in d.items() if k in known})


@dataclass
class RulesConfig:
    profit_target_pct: Optional[float] = None  # None = disabled
    stop_pct: Optional[float] = None
    time_stop_days: Optional[int] = None
    trailing: TrailingConfig = field(default_factory=TrailingConfig)
    auto_trail: AutoTrailConfig = field(default_factory=AutoTrailConfig)
    scale_out: ScaleOutConfig = field(default_factory=ScaleOutConfig)
    exit_market_orders: bool = False  # True = close with MARKET orders so a triggered stop/target
                                      # always fills (option bid/ask don't stream cleanly here)
    exit_slippage_floor: float = 0.50  # bid-anchored triggered SELL-to-close is never priced below
                                       # mark*(1-this) (catastrophe guard vs a broken/stub bid).
                                       # 0.50 == order.py's prior hardcoded EXIT_SLIPPAGE_FLOOR
                                       # (byte-identical default). Threaded into OrderManager;
                                       # tuned by tune_exit_floor.py from logged fill quality.


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
    # Model-driven position management: each cycle the LLM assesses open positions and
    # may arm/tighten trailing stops or exit (take-profit/cut), applied with monotonic
    # guardrails over the static rules above. Set False to fall back to static rules only.
    manage_positions: bool = True
    llm_endpoint: str = "http://127.0.0.1:8082/v1/chat/completions"
    llm_model: str = ""

    @classmethod
    def from_yaml(cls, path: str) -> "Config":
        """Load configuration from YAML file."""
        with open(path, "r") as f:
            data = yaml.safe_load(f)

        # Build nested configs
        ib_cfg = IBConfig(**data.get("ib", {}))
        journal_cfg = JournalConfig(**data.get("journal", {}))
        state_cfg = StateConfig(**data.get("state", {}))
        kill_switch_cfg = KillSwitchConfig(**data.get("kill_switch", {}))
        loop_cfg = LoopConfig(**data.get("loop", {}))
        scope_cfg = ScopeConfig(**data.get("scope", {}))
        caps_cfg = CapsConfig(**data.get("caps", {}))

        # Rules and trailing
        rules_data = data.get("rules", {})
        trailing_data = rules_data.get("trailing", {})
        trailing_cfg = TrailingConfig(**trailing_data)
        # scale_out: ignore unknown keys so a config typo can't crash the trader
        _so_raw = rules_data.get("scale_out", {}) or {}
        _so_known = {f.name for f in ScaleOutConfig.__dataclass_fields__.values()}
        scale_out_cfg = ScaleOutConfig(**{k: v for k, v in _so_raw.items() if k in _so_known})
        # auto_trail: ignore unknown keys so a config typo can't crash the trader
        _at_raw = rules_data.get("auto_trail", {}) or {}
        _at_known = {f.name for f in AutoTrailConfig.__dataclass_fields__.values()}
        auto_trail_cfg = AutoTrailConfig(**{k: v for k, v in _at_raw.items() if k in _at_known})
        rules_cfg = RulesConfig(
            profit_target_pct=rules_data.get("profit_target_pct"),
            stop_pct=rules_data.get("stop_pct"),
            time_stop_days=rules_data.get("time_stop_days"),
            trailing=trailing_cfg,
            auto_trail=auto_trail_cfg,
            scale_out=scale_out_cfg,
            exit_market_orders=bool(rules_data.get("exit_market_orders", False)),
            exit_slippage_floor=float(rules_data.get("exit_slippage_floor", 0.50)),
        )

        return cls(
            ib=ib_cfg,
            journal=journal_cfg,
            state=state_cfg,
            kill_switch=kill_switch_cfg,
            loop=loop_cfg,
            scope=scope_cfg,
            caps=caps_cfg,
            rules=rules_cfg,
            construction=construction_from_dict(data.get("construction")),
        )


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
    """Load and merge configuration from YAML and CLI overrides."""
    # Load base config from YAML if file exists
    if os.path.exists(config_path):
        cfg = Config.from_yaml(config_path)
    else:
        cfg = Config()

    # CLI overrides
    if interval is not None:
        cfg.loop.interval_seconds = interval
    if max_orders_cycle is not None:
        cfg.caps.max_orders_per_cycle = max_orders_cycle
    if max_orders_day is not None:
        cfg.caps.max_orders_per_day = max_orders_day
    if max_notional_day is not None:
        cfg.caps.max_notional_per_day = max_notional_day

    # dry_run is always True by default; --arm makes it False
    cfg.dry_run = not arm
    cfg.arm = arm

    # loop mode
    cfg.loop_mode = loop

    # trading-orchestrator fields (from the optional 'trading:' section of the YAML). Read the file
    # ONCE via a context manager (the old `yaml.safe_load(open(...))` leaked the handle) and surface
    # a parse failure LOUDLY (2026-07-03): the old bare `except: _tr = {}` silently discarded EVERY
    # trading field -- slack channel, approver_ids, caps, approved/blocked names -- on any YAML
    # hiccup, so a broken config could arm live trading with empty approvers and no blocklist and
    # nobody would know. Defaults still apply on failure, but now it is visible.
    _tr = {}
    if os.path.exists(config_path):
        try:
            with open(config_path) as _cf:
                _tr = (yaml.safe_load(_cf) or {}).get('trading', {}) or {}
        except Exception as _ce:
            print(f"[WARN] could not parse 'trading:' section of {config_path}: {_ce} "
                  f"-- falling back to orchestrator DEFAULTS (check the config!)")
            _tr = {}
    for _k, _d in [('slack_channel', ''), ('approver_ids', []),
                   ('alerts_channel', ''), ('summary_channel', ''),
                   ('error_channel', ''),  # #error-logs -- unfilled-order fill alarms (2026-07-01)
                   ('llm_endpoint', 'http://127.0.0.1:8082/v1/chat/completions'),
                   ('llm_model', ''), ('manage_positions', True),
                   ('model_release_gate', None),  # signed v3 promotion gate; absent/off by default
                   ('approved_names', []), ('allow_model_names', False),
                   ('pot_cap_usd', None), ('confident_full_size', False), ('confident_conviction', 4),
                   ('cap_bypass_min_conviction', 6),  # conviction >= this may exceed the soft 12% cap
                                                      # (raised 2026-06-22 from the old effective 4)
                   ('cash_buffer_pct', 0.05),         # always keep 5% of NetLiq liquid (account value)
                   ('conviction_size_curve', None),   # PENDING CALIBRATION; None -> flat-0.12 default
                   ('conviction_size_multipliers', None),  # EMPIRICAL, GATED conviction->size
                                                           # multiplier proposed by
                                                           # calibrate_conviction_sizing.py; None/
                                                           # empty => 1.0 (flat, unchanged). PROPOSE-
                                                           # ONLY: apply only after Trevor opts in
                                                           # (also wire it in run_trader.py, mirroring
                                                           # conviction_size_curve).
                   ('blocked_names', []), ('blocked_sector_keywords', []),
                   ('max_sector_agg_pct', 0.25),  # cap on a correlated sector/cluster aggregate
                                                  # (subset of the single-name book); 0/empty-map => no-op
                   ('sector_map', {}),            # static symbol->sector/cluster dict (empty => no clustering)
                   ('max_trade_pct', 0.12), ('max_concurrent', 4), ('daily_halt_pct', 0.08),
                   # TAKE-PROFIT-AND-RELOAD (2026-07-03). Encode Trevor's serial-reload exit style:
                   # when the MODEL banks a winner (take_profit) AND still sees room, bank it and
                   # SUGGEST a fresh same-name entry through the normal propose->approve->submit
                   # path (each reload re-anchors its own 30% stop to the new basis). OFF by default
                   # -- reload_enabled=False is a pure no-op (today's behavior); Trevor flips it on
                   # deliberately after re-arm + validation. The other knobs only bind when enabled.
                   ('reload_enabled', False),          # master flag; False => feature is a full no-op
                   ('reload_conviction_min', 6),       # friction gate: reload clears only if the
                                                       # model's reload_conviction >= this
                   ('reload_friction_k', 1.5),         # friction gate: expected continuation must
                                                       # exceed k x (commission + 1-cycle theta + slippage)
                   ('reload_max_per_name_per_day', 2), # anti-churn: <= this many reloads per name/day
                   ('reload_ttl_cycles', 3),           # a reload ticket expires after N exit cycles
                                                       # (stale continuation signal is dropped, never fired)
                   ('baseline_path', './day_baseline.json'), ('audit_path', './audit.jsonl')]:
        setattr(cfg, _k, _tr.get(_k, _d))

    return cfg


# Make dry_run accessible as attribute (not in yaml, set by CLI)
Config.dry_run = True
Config.loop_mode = False
Config.arm = False
