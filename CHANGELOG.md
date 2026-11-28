# Changelog

All notable changes to `Agentic-Trader` are documented in this file.

## [0.1.0] - 2026-11-27

### Added
- **Sanitized execution & fail-closed risk-envelope architecture (`exitmgr/`, `swing_executor/`):** Separates untrusted trade proposals from deterministic execution authority with strict YAML + CLI domain validation, armed startup refusal on missing config files (`exit 2`), typed `BrokerStartupError` propagation (`exit 1`), strict JSON throttle/ledger schema validation (rejecting partial, corrupted, or null counter files), strict account binding, and host-wide order mutation locking.
- **SQLite WAL multi-slot capital reservation ledger (`swing_executor/ledger.py`, `coordinator.py`):** Enforces T+1 settled cash and ring-fenced cash reserves, per-slot and sector exposure caps, cross-client `(account, client_id, order_id)` / `perm_id` isolation, and deterministic multi-slot capital accounting.
- **Staged-upgrade & durable broker reconciliation (`swing_executor/stops.py`, `coordinator.py`, `ledger.py`):** Durable deferred-fill reconciliation and idempotent stop arming, persisted `stop_covered_qty` keeping underprotected fills retryable across restarts, live broker open-order stop-coverage verification for `FILLED` slots on reconciliation, required broker stop re-verification when migrating legacy slot ledgers, fail-closed `uncertain_holdings=True` / `rearm_deferred_uncertain_qty=True` on broker timeouts, and neutral-before-fees exit alert formatting with human-gated graduation (`graduation.py`).
- **Zero-broker offline simulation demo & CI verification (`scripts/demo_offline_simulation.py`, `.github/workflows/ci.yml`):** Broker-free, network-free proposal-to-close simulation using the clearly labeled non-production `ToyCrossoverSignal`, verified alongside the 95-test audit/swing safety suite and full 4-shard CI regression suite (`4,369 passed, 21 skipped, 5 xfailed` on Python 3.12).
