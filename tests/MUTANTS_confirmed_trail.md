# Mutation manifest -- confirmed trailing stop (Sol audit R5 section R1)

Target: `exitmgr/rules.py`, `exitmgr/state.py`, `exitmgr/manager.py` (confirmed-trail limbs only).
Suite: `tests/test_confirmed_trail.py` (19 tests). Harness: `scratchpad/mutate.py`, run in an isolated copy of the tree at `/tmp/trail-mutation` -- the live repo was never mutated.

## Grading

| verdict | meaning |
| --- | --- |
| KILLED | at least one test failed with an **AssertionError** -- the suite detected wrong *behavior*. |
| CRASHED | the suite produced only tracebacks (TypeError/AttributeError/collection error) and **no** AssertionError. **NOT counted as a kill** -- that is the mutant breaking the code, not the tests proving anything. |
| SURVIVED | the suite passed. That behavior is not pinned. |

A mutant showing both an AssertionError and a traceback is KILLED; the traceback is still listed in Detected-as.

## Score

* **KILLED (semantic): 30 / 31**
* CRASHED (graded separately, not kills): 0
* SURVIVED: 1
* NOT-APPLIED (anchor missing): 0

## Mutants

| id | file | mutation | verdict | detected as |
| --- | --- | --- | --- | --- |
| M01 | `rules.py` | evaluate_trailing_stop: drop the armed gate (arming inferred again) | **KILLED** | AssertionError |
| M02 | `rules.py` | evaluate_position: fire the trail without the armed bit | **KILLED** | AssertionError |
| M03 | `rules.py` | evaluate_position: measure the floor off the LIFETIME peak | **KILLED** | AssertionError |
| M04 | `rules.py` | calendar: Friday->Tuesday counts as consecutive | **KILLED** | AssertionError |
| M05 | `rules.py` | calendar: Independence Day not observed (holiday adjacency wrong) | **KILLED** | AssertionError |
| M06 | `rules.py` | calendar: weekends are trading sessions | **KILLED** | AssertionError |
| M07 | `state.py` | arm on ONE qualifying close instead of two | **KILLED** | AssertionError |
| M08 | `state.py` | re-arm on every later close (re-seeds the floor) | **KILLED** | AssertionError |
| M09 | `state.py` | every close qualifies (threshold ignored) | **KILLED** | AssertionError |
| M10 | `state.py` | sub-threshold close does not reset the streak | **KILLED** | AssertionError |
| M11 | `state.py` | streak continues across a GAP (missing close counted) | **KILLED** | AssertionError |
| M12 | `state.py` | same session can be recorded twice (double-count into an arm) | **KILLED** | AssertionError |
| M13 | `state.py` | peak_since_arm ratchets while UNARMED (pre-arm spike poisons floor) | **KILLED** | AssertionError |
| M14 | `state.py` | ratchet reverses (peak_since_arm follows price down) | **KILLED** | AssertionError |
| M15 | `state.py` | arming seeds the floor above the close (spike-like poisoning) | **KILLED** | AssertionError |
| M16 | `state.py` | unusable/NaN close treated as a qualifying one | **KILLED** | AssertionError |
| M17 | `state.py` | prune/clear leaves the confirmation behind | **KILLED** | AssertionError |
| M18 | `state.py` | clear_trail_state leaves the confirmation behind | **KILLED** | AssertionError |
| M19 | `state.py` | MIGRATION: legacy trail_armed backfilled as a real ARM | **KILLED** | AssertionError |
| M20 | `state.py` | armed_at with no ratchet price stays 'armed' | **KILLED** | AssertionError |
| M21 | `state.py` | save() drops the legacy rollback mirror | **KILLED** | AssertionError |
| M22 | `manager.py` | view reports the FEATURE TOGGLE as armed (the original defect) | **KILLED** | AssertionError |
| M23 | `manager.py` | model arm_trail sets the ARMED bit | **KILLED** | AssertionError |
| M24 | `manager.py` | auto-trail bypasses the confirmation contract | **KILLED** | AssertionError |
| M25 | `manager.py` | auto-trail gates on the LIFETIME peak again | **KILLED** | AssertionError |
| M26 | `manager.py` | session close accepted mid-session (intraday tick counts) | **KILLED** | AssertionError |
| M27 | `manager.py` | session close accepted on weekends/holidays | **KILLED** | AssertionError |
| M28 | `manager.py` | a streaming-quote fallback counts as an official close | **KILLED** | AssertionError |
| M29 | `manager.py` | close/prune no longer purges the confirmation | **KILLED** | AssertionError |
| M30 | `manager.py` | eval loop passes armed=True unconditionally | **SURVIVED** | - |
| M31 | `manager.py` | eval loop reverts to pre-R1 behavior: armed off the LIFETIME peak (both gates defeated) | **KILLED** | AssertionError |


## The one survivor, and why it is equivalent rather than a gap

**M30** (`_trail_armed = True` in the eval loop) survives because the trailing rule has TWO
independent gates: the armed bit AND a non-null `peak_since_arm`. `peak_since_arm` is written only
by the arming branch of `record_session_close`, so an unconfirmed position has none, and forcing
the armed flag alone still fires nothing. That is defense in depth working as intended, not an
untested path.

**M31** proves the point: defeat BOTH gates -- force `armed=True` *and* feed the monotonic lifetime
peak as the ratchet, which is exactly the pre-R1 behavior -- and the end-to-end cycle test
(`test_e2e_unconfirmed_winner_is_not_trailed_out_by_the_real_cycle`) kills it with an assertion.
The loop's real wiring is therefore pinned.

## What "crash" would have meant here

Zero mutants were detected only by a traceback. Every kill above is an `AssertionError` raised by a
behavioral assertion, taken from `call.excinfo.type.__name__` inside a pytest hook rather than
scraped from traceback text -- text scraping is unreliable in both directions: pytest prints a bare
`assert 2 == 0` (no class name) for a rewritten assert, so an earlier revision of this very harness
graded four genuine kills as crashes.
