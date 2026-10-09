# Agentic Trader

An options swing-trading service where a language model proposes trades and
deterministic code decides whether any of them are allowed to reach the broker.
The model's output is an opinion. Sizing, exposure, loss limits, quote quality,
and broker-state checks are ordinary code with tests, and they have the final
say on every order.

This is the sanitized public export of a system that runs against a live
Interactive Brokers account. An LLM has no proven trading edge. Treat any
capital pointed at this as risk capital, paper-test it first, and read the
receipts it writes before arming it.

## Why it is built this way

The interesting problem in an LLM trading system is not getting the model to
produce a plausible trade. It is making the blast radius of a wrong one
bounded and auditable. So the design separates two things that are easy to
conflate:

- **Judgment**, which the model supplies and which can be wrong at any time.
- **Authority**, which lives in deterministic gates the model cannot reach,
  reason around, or talk its way past.

Everything below follows from that split.

## Entry authority

Lane-specific authority rules are explicit. A model-authored daily-slate or
continuous-entry candidate can use the no-tap path when
`auto_approve_within_gates` is enabled and the refreshed exact order clears
every hard gate. No approval is awaited on that autonomous path. Other lanes
wait for their configured approval authority. Any changed candidate is fully
revalidated against fresh quotes, positions, buying power, and risk limits
before submission.

Hard caps are config, not model-adjustable: `max_orders_per_cycle`,
`max_orders_per_day`, and `max_notional_per_day`.

## Exit safety

Protective exits run independently from model inference, so a hung or
unavailable model cannot strand an open position. Long-premium structures
retain the configured hard-loss trigger, while winner management can arm a
durable trailing floor. Spread closes prefer a marketable combo limit derived
from a fresh executable bid; the configured fail-safe can use a combo market
order when that bid is unavailable. Both paths fail closed when the broker
cannot provide terminal order evidence or unambiguous live quantity.

Manual close and liquidation requests are serialized under the same host-wide
order-mutation lock and are queued for the armed service, so they never race a
second broker writer.

## Layout

| Path | What is in it |
|---|---|
| `exitmgr/` | 59 modules: broker connection, entry gating, risk, exit rules, reconciliation, journaling, alerting |
| `tests/` | 229 files, 2,848 test functions |
| top level | 59 operational scripts: daily slate, ticker evaluation, reconciliation, reporting, calibration |
| `config.yaml` | every gate, cap, and rule threshold |

Representative `exitmgr` modules: `entry_safety.py`, `entry_throttle.py`,
`entry_reservation.py`, `risk.py`, `construction.py`, `connection.py`,
`exec_capture.py`, `exit_recovery.py`, `flex_ingest.py`, `approval.py`.

## Tests

The suite is 2,848 test functions across 229 files, roughly 49,000 of the
109,000 lines in the repository. That ratio is deliberate. The gates are the
product, so the gates are what gets tested: order caps, notional ceilings,
quote staleness, account-snapshot validity, reconnect and client-id handling,
exit-trigger arithmetic, and the fail-closed behaviour on ambiguous broker
state.

```bash
pytest -q
```

Counts above are from an AST walk of `tests/`, counting module- and
class-level `test_` functions. They are a measure of coverage surface, not of
profitability, and nothing in this repository claims a return.

## Install

Install the pinned dependencies from `requirements.txt` and review
`config.yaml` before anything else. Keep credentials outside the repository.

## About this export

This is a sanitized snapshot, not the live tree. Production account history,
host identities, and credentials are excluded, and all fixtures are synthetic.
Module and function docstrings were stripped by the sanitizer and replaced
with a placeholder line, so the code here is less self-documenting than the
original. Names and tests are the guide.

## License

MIT, see [LICENSE](LICENSE).
