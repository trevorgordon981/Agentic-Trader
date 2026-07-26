# Mutation testing artifact — `tests/test_credit_contract.py`

**Run:** 2026-07-26, studio (`/path/to/home`), Python 3.9.6, pytest 8.4.2.
**Result: 23 mutants, 23 KILLED, 0 survived, 0 inconclusive.**

This file exists because the previous *"9 mutants killed"* claim had **no named mutant list and
no artifact** and was therefore unauditable (audit R4 / S2). Everything below is reproducible
from the runner and the exact find/replace strings recorded here.

## Subject under test

| artifact | SHA-256 |
| --- | --- |
| `exitmgr/strategist.py` (mutated) | `c10e239c6b3935efe98490bc6424cd98f1121f7a0b3909c6be8314f0635d0e55` |
| `tests/test_credit_contract.py` (oracle) | `709face2c78d080b56357fcc57b6af03a93d596f2f91a29f00fd7d65f9e0a26c` |
| pre-R4 `exitmgr/strategist.py` (the file these mutants revert *to*) | `d54b571c4543da5d7930cc5a0cf90b4c7e420de8e01d7c93a31654018be52237` |

`exitmgr/strategist.py` SHA-256 **before** the mutation run == **after** the run
(`c10e239c…`). **No mutant ever touched the live file.**

## Method

Runner: `mutate_credit_contract.py` (scratch, run from `/tmp`; not committed to the repo — it
mutates the repo and does not belong inside it). For each mutant it:

1. builds a **symlink farm** of the whole repo in a fresh temp dir — every entry symlinked,
   `__pycache__` deliberately excluded, `PYTHONDONTWRITEBYTECODE=1` — with `exitmgr/` as a real
   directory of symlinks whose **only** real file is the mutated `strategist.py`;
2. asserts the farm is isolated (`exitmgr.strategist.__file__` resolves *inside* the temp dir,
   not into the live tree) before anything runs;
3. asserts the mutation site matches **exactly once** in the live source, so no mutant is
   silently a no-op;
4. asserts the mutant still imports (a `SyntaxError` is not a kill);
5. runs that mutant's designated test node-ids and records the pytest summary line.

**Baseline (control):** the *unmutated* farm runs `333 passed, 1 skipped`. Without this, "the
test failed" would prove nothing about the mutant.

A mutant is **KILLED** when its designated tests fail. `Fix` names which R4 remediation the
mutant reverts.

## Results

| ID | Fix reverted | Mutation | Verdict | pytest |
| --- | --- | --- | --- | --- |
| M1 | hole 1 — allow-list on the DEBIT path | delete the debit-path allow-list call (**the exact pre-R4 state**) | **KILLED** | 111 failed |
| M2 | hole 1 — allow-list membership test | `if canon not in allowed:` → `if False:` (accept everything, both paths) | **KILLED** | 18 failed |
| M3 | hole 1 — allow-list on the CREDIT path | delete the credit-path allow-list call (the historical CSP gate) | **KILLED** | 17 failed |
| M4 | hole 1 — allow-list gates, never rewrites | store `_canonical_structure(...)` instead of the model's own string | **KILLED** | 3 failed, 12 passed |
| M5 | hole 2 — collateral/strike binding | drop the `_implied_contracts` call (**the exact pre-R4 state**) | **KILLED** | 8 failed, 1 passed |
| M6 | hole 2 — whole-multiple requirement | accept any remainder → a fraction of a contract | **KILLED** | 6 failed, 1 passed |
| M7 | hole 2 — at least one whole contract | `if contracts < 1:` → `if False:` | **KILLED** | 1 failed |
| M8 | hole 3 — collateral must exceed credit | `if net_credit_c >= collateral_c:` → `if False:` (**the exact pre-R4 state**) | **KILLED** | 2 failed, 3 passed |
| M9 | hole 3 — STRICT inequality | `>=` → `>` (a zero-max-loss CSP becomes legal) | **KILLED** | 1 failed, 3 passed |
| M10 | hole 4 — cent-safe `max_loss` comparison | restore the binary-float tolerance (**the exact pre-R4 state**) | **KILLED** | 1 failed |
| M11 | hole 4 — declared tolerance width | `_MAX_LOSS_TOL_CENTS = 1` → `100` ($1.00) | **KILLED** | 2 failed |
| M12 | hole 4 — boolean rejection | `_reject_bool` → no-op (booleans coerce to 1.0/0.0 again) | **KILLED** | 10 failed, 14 passed |
| M13 | hole 4 — boolean rejection in `_clamp_pct` | remove the bool guard so `true` clamps up to the 10%/20% floor | **KILLED** | 1 failed |
| M14 | hole 4 — exact decimal reading | `Decimal(str(v))` → `Decimal(float(v))` (read the binary approximation) | **KILLED** | 1 failed, 1 passed |
| M15 | hole 4 — non-finite refusal in `_cents` | delete the `is_finite` guard | **KILLED** | 1 failed |
| M16 | S4 — credit-scoped CSP direction carve-out | revert to the generic `put → bearish` mapping on the credit path | **KILLED** | 19 failed |
| M17 | S4 — fail closed on an explicit bearish CSP | silently *correct* a bearish CSP to bullish instead of refusing it | **KILLED** | 11 failed |
| M18 | S4 — the carve-out stays CREDIT-scoped | apply the bullish inference to **every** structure (long puts included) | **KILLED** | 2 failed |
| M19 | pre-existing — credit-field positivity/finiteness | `if not math.isfinite(v) or v <= 0:` → `if False:` | **KILLED** | 2 failed, 43 passed |
| M20 | pre-existing — `normalize_debit` must not touch credit fields | rescale the net credit ×100 like a debit | **KILLED** | 2 failed |
| M21 | pre-existing — an unknown `side` drops the idea | map an unrecognised side to `"debit"` | **KILLED** | 10 failed, 1 skipped |
| M22 | pre-existing — a debit idea needs a positive debit | stop dropping `est_debit_usd <= 0` | **KILLED** | 3 failed, 12 passed |
| M23 | pre-existing — `est_debit_usd` not required on a credit idea | require a debit on the credit path too | **KILLED** | 2 failed |

## Two mutants that survived the FIRST run — and what they exposed

Recorded because a mutation artifact that only shows the final green run is worth very little.

**M7 survived initially.** Every hole-2 test rejected its payload through the *whole-multiple*
check (M6's gate), so deleting the `contracts >= 1` floor changed nothing observable. The gap
was real: a collateral inside the one-cent rounding slop of zero (`strike=120,
collateral_usd=0.01`) passes the multiple test with remainder 0 and implies **zero contracts**.
Fixed by adding `test_gap_m7_collateral_of_a_single_cent_implies_zero_contracts`.

**M19 survived initially.** The newer cent gates shadowed the positivity gate for every value
in the existing `0 / -1 / None / "" / nan / inf` parametrisation. The gap was real and worse: a
**negative** `net_credit_usd` is self-consistent under every later gate —
`collateral 12000 − (−100) = 12100 = max_loss` exactly, the collateral is a clean 1×120
multiple, and `−100 < 12000` — so with the positivity gate removed, a *debit wearing a
credit's name* parsed as a valid CSP. Fixed by
`test_gap_m19_a_negative_but_self_consistent_net_credit_is_rejected` plus a four-case
negative-field parametrisation.

**M15 also survived initially, and the reason is worth recording.**
`Decimal("nan").quantize(Decimal("0.01"))` **returns `NaN` — it does not raise** (unlike
`Infinity`, which does raise `InvalidOperation`). Removing the `is_finite` guard therefore let
a NaN travel one step further, where `int(Decimal("NaN") * 100)` blew up with an *unrelated*
`ValueError`. Same accept/reject outcome, different and undiagnosable reason. The guard's real
job is an **attributable** refusal, so the oracle now asserts the message
(`pytest.raises(ValueError, match="must be finite")`), which kills it.

## Reproducing

```
python3 /tmp/mutate_credit_contract.py --json /tmp/mutants_r4.json
```

Exit status 0 requires: baseline green, zero survivors, zero inconclusive, and the live
`strategist.py` SHA-256 unchanged. The mutation strings are the `find`/`repl` pairs in
`MUTANTS` inside the runner; they are literal, single-occurrence, whitespace-exact edits
against `c10e239c…`, so they will need re-anchoring if that file changes.
