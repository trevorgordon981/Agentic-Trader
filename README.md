# Agentic Trader

An automated trading execution and risk-governance repository separating **trade
proposals** (judgment, which can be wrong at any time) from **deterministic
execution authority** (sizing, cash/T+1 reserves, exposure caps, protective
stops, and fail-closed broker reconciliation).

> **Engineering-not-investment-performance disclaimer:** This repository is a
> sanitized software-engineering showcase of deterministic execution safety,
> state recovery, and fail-closed broker rails. Nothing in this repository
> claims profitability, investment advice, or a live trading edge. Any real
> capital pointed at automated execution code is risk capital; test offline and
> on paper accounts first.

## Concrete Problem

In automated and LLM-assisted trading systems, the hard engineering failure
modes are not generating trade ideas — they are **silent policy drift**,
**stale-state overselling**, and **corrupted counters**:

- An armed process starting with a typo'd or missing config file and silently
  falling back to defaults.
- CLI limit overrides bypassing YAML domain checks (e.g., negative or `NaN`
  daily caps).
- Partial or corrupted persisted counter files silently reading back as zero.
- A broker connection failure returning exit code `0` to a process supervisor.
- A cancel/replace protective-stop race overselling when an external partial
  fill occurs or when a broker position read times out.
- An exit alert coloring gross P&L green/red before broker commissions settle.

Every gate in `exitmgr/` and `swing_executor/` is built to **fail closed** on
those failure modes before touching a broker.

## Architecture

```text
+-----------------------------------------------------------------------------+
|                        Proposal Layer (Untrusted)                           |
|  Options LLM Proposals (exitmgr)   |   Pluggable SwingSignalProvider        |
|                                    |   (demo-only ToyCrossoverSignal)       |
+-----------------+------------------+-------------------+--------------------+
                  |                                      |
                  v                                      v
+------------------------------------+  +-------------------------------------+
|   Options Governance (exitmgr/)    |  | 2x ETF Swing Infra (swing_executor/)|
| - Strict YAML + CLI domain checks  |  | - Armed config + kill-switch guard  |
| - Armed startup requires real file |  | - Multi-slot SQLite WAL ledger      |
| - Strict JSON throttle & ledger    |  | - T+1 settled & ring-fenced cash    |
| - Protective exits & combo closes  |  | - Cross-client orderId isolation    |
| - Typed BrokerStartupError -> rc=1 |  | - Fail-closed stop cancel/replace   |
| - Host-wide order mutation lock    |  | - Graduation & neutral-fee alerts   |
+-----------------+------------------+  +-------------------+-----------------+
                  |                                         |
                  +--------------------+--------------------+
                                       v
                  +-----------------------------------------+
                  |   Broker Boundary (IBKR / Simulated)    |
                  | Fresh positions + fresh open orders     |
                  | before every stop arm, sync, or re-arm  |
                  +-----------------------------------------+
```

## Entry Authority (Options Subsystem)

Lane-specific authority rules are explicit. A model-authored daily-slate or
continuous-entry candidate can use the no-tap path when
`auto_approve_within_gates` is enabled and the refreshed exact order clears
every hard gate. No approval is awaited on that autonomous path. Other lanes
wait for their configured approval authority. Any changed candidate is fully
revalidated against fresh quotes, positions, buying power, and risk limits
before submission.

## 2x Leveraged-ETF Swing Infrastructure vs. Omitted Proprietary Signals

The `swing_executor/` package ships the sanitized execution infrastructure used
for 2x leveraged-ETF swing accounts while strictly excluding proprietary alpha:

| Included in `swing_executor/` (Public Infrastructure) | Intentionally Omitted (Proprietary Alpha) |
|---|---|
| Pluggable `SwingSignalProvider` protocol (`signals.py`) + clearly labeled synthetic demo-only `ToyCrossoverSignal` (`IS_PRODUCTION_STRATEGY = False`) | Production signal generators, regime filters, and multi-timeframe alpha formulas |
| Hardened protective stop & cancel-replace engine (`stops.py`): fresh broker position + open-order re-read before sizing; fail-closed `uncertain_holdings=True` / `rearm_deferred_uncertain_qty=True` on broker timeout; concurrent external partial-fill handling without overselling; IBKR cumulative `filled + remaining` modify semantics | Ticker-specific rules, curated ETF universes, and tuned entry/exit thresholds |
| Deterministic multi-slot reservation ledger & coordinator (`ledger.py`, `coordinator.py`): T+1 settled cash & ring-fenced reserve guard, per-slot & sector exposure caps, cross-client `(account, client_id, order_id)` / `perm_id` isolation, fail-closed counter/ledger validation | Production account IDs, live credentials, and live trade history |
| Paper-to-live graduation contract & neutral-before-fees alert formatter (`graduation.py`): excludes proxy exits, requires human approval (`auto_promoted=False`), never colors gross P&L win/loss while commissions are pending | Live capital allocation schedules and private performance metrics |

## 5-Minute Offline Demo (Zero Broker / Zero Network)

Supported Python: **Python 3.10–3.14** (CI runs on Python 3.12).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 scripts/demo_offline_simulation.py
```

Expected output:

```text
=== Agentic Trader: Offline Proposal-to-Close Simulation (No Broker / No Network) ===
Signal provider : toy_sma_crossover_demo_only (production_strategy=False)
Fail-closed gates:
  [PASS] at01_missing_armed_config_refused: config: armed startup requires an existing config file; ...
  [PASS] at02_negative_cli_cap_refused: caps.max_orders_per_day: must be >= 1, got -1
  [PASS] at03_partial_counter_refused: entry throttle record for 2026-06-06 has invalid schema ...
  [PASS] at04_broker_startup_error: Could not connect to IB

2x Swing Execution Trace:
  Spendable cash (after reserve & T+1 hold): $4,000.00
  Proposals seen: 2 | Reserved: ['res_...'] | Rejected: [{'symbol': 'SYN2X_B', 'reason': 'sector_cap_synthetic_tech_2x(...)'}]
  [ENTRY] SYN2X_A BUY 40 @ $50.00 | stop=$47.50 | strategy=toy_sma_crossover_demo_only
  Initial GTC stop: order_id=1001 qty=40 @ $47.50
  [STOP_SYNC] SYN2X_A stop $47.50 -> $51.94 | remaining_qty=30 (held=36, external_working=6)
  Broker timeout fail-closed: action=rearm_deferred_uncertain_qty uncertain_holdings=True rearm_deferred_uncertain_qty=True
  Before fees: [EXIT_PENDING_FEES] SYN2X_A 30 sh @ $52.10 (entry $50.00) | Gross $+63.00 (+4.20%) -- gross (fees pending) | trailing_stop_filled | strategy=toy_sma_crossover_demo_only [tone=NEUTRAL]
  After fees : [EXIT_WIN] SYN2X_A 30 sh @ $52.10 (entry $50.00) | Net $+62.30 (+4.15%) [gross $+63.00, fees $0.70] | trailing_stop_filled | strategy=toy_sma_crossover_demo_only [tone=WIN]
  Graduation : verified_trades=1 eligible_for_human_review=False requires_human_approval=True

[OK] Offline simulation completed cleanly.
```

Pass `--json` (`python3 scripts/demo_offline_simulation.py --json`) for a full
machine-readable execution trace.

## Recovery & Safety Verification Tests

Run the targeted fail-closed audit and 2x swing stop-race recovery suite:

```bash
PYTHONPATH=$PWD pytest -q tests/test_audit_findings_at01_at05.py tests/test_swing_executor.py
```

Run the full repository test suite (mirrors GitHub Actions CI):

```bash
PYTHONPATH=$PWD pytest -q --timeout=60 tests/
```

## Runtime Modes (Read / Preview / Paper / Live)

1. **Offline Simulation (no broker):** `python3 scripts/demo_offline_simulation.py`
2. **Dry-Run Preview (default):** `python3 main.py --config config.paper.yaml`
   (`dry_run=True` by default; no orders transmitted)
3. **Paper Account Profile:** `config.paper.yaml` targets local IBKR paper port
   `7497` with `auto_approve_within_gates: false` and explicit daily caps.
4. **Armed Startup:** passing `--arm` requires an existing config file on disk
   (missing paths fail closed with exit code `2` before broker instantiation),
   validates all CLI overrides through the same YAML domain specs, and exits
   with code `1` on broker connection failure.

> **Note on top-level operational scripts:** Several top-level scripts (such as
> `daily_recommend.py` and `close_symbol.py`) are preserved from the sanitized
> public snapshot and contain macOS `launchctl` supervisor hooks intended for
> host-managed deployments rather than standalone portable execution.

## Repository Layout

| Path | Contents |
|---|---|
| `exitmgr/` | Options exit-management, risk gates, entry reservation/throttle, broker reconciliation, and state recovery |
| `swing_executor/` | Sanitized 2x leveraged-ETF swing execution infrastructure (`signals.py`, `ledger.py`, `stops.py`, `coordinator.py`, `graduation.py`) |
| `scripts/demo_offline_simulation.py` | Zero-network, zero-broker proposal-to-close simulation |
| `config.paper.yaml` | Safe paper-gateway profile (`127.0.0.1:7497`, `auto_approve_within_gates: false`) |
| `config.yaml` | Full public schema reference configuration |
| `tests/` | Automated unit, recovery, fail-closed, and integration test suite |

## License

MIT, see [LICENSE](LICENSE).
