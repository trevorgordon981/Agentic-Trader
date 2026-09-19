# Agentic Trader

Agentic Trader is an experimental options-trading service. An LLM has no proven trading edge;
treat its capital as risk capital. Deterministic code owns sizing, exposure, loss, quote-quality,
and broker-state gates. Paper-test the system and inspect its receipts before arming it.

## Entry authority

Lane-specific authority rules are explicit. A model-authored daily-slate or continuous-entry
candidate can use the no-tap path when `auto_approve_within_gates` is enabled and the refreshed
exact order clears every hard gate. No approval is awaited on that autonomous path. Other lanes
wait for their configured approval authority. Any changed candidate is fully revalidated against
fresh quotes, positions, buying power, and risk limits before submission.

## Exit safety

Protective exits run independently from model inference. Long-premium structures retain the
configured hard-loss trigger, while winner management can arm a durable trailing floor. Spread
closes prefer a marketable combo limit derived from a fresh executable bid; the configured
fail-safe can use a combo market order when that bid is unavailable. Both paths fail closed when
the broker cannot provide terminal order evidence or unambiguous live quantity. Manual close and
liquidation requests are serialized under the same host-wide order-mutation lock and are queued
for the armed service; they never race a second broker writer.

## Install and test

Install the pinned dependencies from `requirements.txt`, review `config.yaml`, and keep
credentials outside the repository. Run the exhaustive suite with `pytest -q`. The public export
contains synthetic fixtures only; production account history, host identities, and credentials are
excluded.
