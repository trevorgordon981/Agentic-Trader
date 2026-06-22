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


@dataclass
class TrailingConfig:
    enabled: bool = False
    activation_gain_pct: float = 50.0
    giveback_fraction: float = 0.5


@dataclass
class RulesConfig:
    profit_target_pct: Optional[float] = None  # None = disabled
    stop_pct: Optional[float] = None
    time_stop_days: Optional[int] = None
    trailing: TrailingConfig = field(default_factory=TrailingConfig)
    exit_market_orders: bool = False  # True = close with MARKET orders so a triggered stop/target
                                      # always fills (option bid/ask don't stream cleanly here)


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
        rules_cfg = RulesConfig(
            profit_target_pct=rules_data.get("profit_target_pct"),
            stop_pct=rules_data.get("stop_pct"),
            time_stop_days=rules_data.get("time_stop_days"),
            trailing=trailing_cfg,
            exit_market_orders=bool(rules_data.get("exit_market_orders", False)),
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
            manage_positions=bool(data.get("manage_positions", True)),
            llm_endpoint=data.get("llm_endpoint", "http://127.0.0.1:8082/v1/chat/completions"),
            llm_model=data.get("llm_model", ""),
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

    # trading-orchestrator fields (from the optional 'trading:' section of the YAML)
    _tr = {}
    if os.path.exists(config_path):
        try:
            _tr = (yaml.safe_load(open(config_path)) or {}).get('trading', {}) or {}
        except Exception:
            _tr = {}
    for _k, _d in [('slack_channel', ''), ('approver_ids', []),
                   ('alerts_channel', ''), ('summary_channel', ''),
                   ('llm_endpoint', 'http://127.0.0.1:8082/v1/chat/completions'),
                   ('llm_model', ''), ('approved_names', []), ('allow_model_names', False),
                   ('pot_cap_usd', None), ('confident_full_size', False), ('confident_conviction', 4),
                   ('blocked_names', []), ('blocked_sector_keywords', []),
                   ('max_trade_pct', 0.12), ('max_concurrent', 4), ('daily_halt_pct', 0.08),
                   ('baseline_path', './day_baseline.json'), ('audit_path', './audit.jsonl')]:
        setattr(cfg, _k, _tr.get(_k, _d))

    return cfg


# Make dry_run accessible as attribute (not in yaml, set by CLI)
Config.dry_run = True
Config.loop_mode = False
Config.arm = False
