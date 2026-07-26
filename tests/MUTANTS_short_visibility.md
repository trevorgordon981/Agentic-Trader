# Mutation record — short position visibility (2026-07-26)

Every fix that makes a filled cash-secured put visible was reverted, one at a time, in a scratch
copy of the repo (`/tmp/mutcheck`, an rsync of `~/exitmgr-app`). `tests/test_short_position_visibility.py`
was then run against the mutant. A mutant that survives means the test suite does not actually
pin the behaviour it claims to.

**Result: 18 mutants, 18 killed, 0 survived.** Baseline (unmutated) run: 41 passed.

Harness: `/tmp/mutate_shortvis.py` on studio (also kept at
`scratchpad/mutate_shortvis.py`). It restores both files from the in-memory original after every
mutant and re-verifies the clean baseline at the end.

| # | File | Reverted fix (the bug it re-introduces) | Result | Killed by |
|---|---|---|---|---|
| M1 | connection.py | `include_short` branch disabled — **the original defect restored**: a filled CSP is invisible | KILLED (8) | `test_short_put_appears_with_negative_quantity`, `test_short_quantity_is_never_abs_ed`, `test_short_with_unreadable_sectype_is_still_reported`, `test_fetch_position_book_splits_and_the_long_book_is_unchanged`, `test_csp_is_reported_on_every_cycle_not_only_the_fill_cycle`, `test_short_report_counts_a_csp_toward_the_concurrent_book`, `test_an_unjournaled_short_is_still_reported`, `test_assignment_transition_runs_clean_through_a_whole_cycle` |
| M2 | connection.py | `quantity=abs(position_qty)` — sign stripped, a short looks like a long | KILLED (10) | `test_short_quantity_is_never_abs_ed`, `test_short_put_appears_with_negative_quantity`, `test_long_positions_are_byte_identical_differential`, `test_scope_selection_never_includes_a_short`, +6 |
| M3 | connection.py | secType guard removed on the short branch — assigned STOCK carrying residual `right`/`strike` read as a short option | KILLED (1) | `test_assigned_stock_with_residual_option_fields_is_not_treated_as_a_short` |
| M4 | connection.py | `include_stock` branch disabled — assignment has no evidence | KILLED (4) | `test_stock_is_excluded_unless_explicitly_requested`, `test_assignment_transition_runs_clean_through_a_whole_cycle`, `test_assigned_stock_is_never_managed_as_an_option`, `test_fetch_position_book_splits_and_the_long_book_is_unchanged` |
| M5 | connection.py | LONG filter widened `> 0` → `!= 0` — the naive "fix"; shorts leak into every legacy caller | KILLED (5) | `test_default_call_still_excludes_every_short`, `test_long_positions_are_byte_identical_differential`, `test_trader_open_positions_is_untouched_by_the_default`, +2 |
| M6 | manager.py | `_fetch_position_book` stops splitting — shorts flow into the long-only pipeline | KILLED (6) | `test_fetch_position_book_splits_and_the_long_book_is_unchanged`, `test_scope_selection_never_includes_a_short`, `test_csp_is_reported_on_every_cycle_not_only_the_fill_cycle`, +3 |
| M7 | manager.py | `_log_exit` uses the LONG formula on a credit row — **the −100% bug**: a near-perfect CSP booked as a total loss | KILLED (5) | `test_log_exit_books_a_cheap_buyback_as_a_profit`, `test_log_exit_books_an_expensive_buyback_as_a_loss`, `test_expired_worthless_short_keeps_the_whole_credit`, `test_expired_itm_short_is_flagged_as_an_assignment`, `test_assignment_emits_a_terminal_row_and_does_not_orphan_the_journal` |
| M8 | manager.py | `short_pnl` sign flipped (`cost - credit`) — a winning CSP reads as a loser | KILLED (5) | `test_short_pnl_a_csp_whose_price_fell_is_a_GAIN`, `test_short_pnl_a_csp_whose_price_rose_is_a_LOSS`, `test_short_pnl_is_monotonically_decreasing_in_price`, `test_short_pnl_scales_with_contracts`, `test_csp_is_reported_on_every_cycle_not_only_the_fill_cycle` |
| M9 | manager.py | `short_pnl` fabricates `0.0` instead of `None` on an unknown mark — a guess indistinguishable from a real flat | KILLED (2) | `test_short_pnl_refuses_to_guess`, `test_an_unjournaled_short_is_still_reported` |
| M10 | manager.py | `_process_expiries` ignores the short book — a **live** CSP booked as expired | KILLED (1) | `test_a_live_short_is_never_treated_as_expired` |
| M11 | manager.py | `_process_short_assignments` not called from `run_cycle` | KILLED (1) | `test_assignment_transition_runs_clean_through_a_whole_cycle` |
| M12 | manager.py | `_report_short_positions` not called from `run_cycle` — visible in code, silent in production | KILLED (4) | `test_csp_is_reported_on_every_cycle_not_only_the_fill_cycle`, `test_short_report_counts_a_csp_toward_the_concurrent_book`, `test_an_unjournaled_short_is_still_reported`, `test_assignment_transition_runs_clean_through_a_whole_cycle` |
| M13 | manager.py | assignment declared with no stock evidence — fabricated terminal row on a manual buy-back | KILLED (1) | `test_disappearance_without_stock_is_not_declared_an_assignment` |
| M14 | manager.py | long-only management backstop removed | KILLED (1) | `test_backstop_refuses_a_short_that_reaches_managed_positions` |
| M15 | manager.py | `_is_credit_row` no longer detects `side == "credit"` | KILLED (1) | `test_is_credit_row_detects_a_short_by_either_signal` |
| M16 | manager.py | `_emit_expiry_close` treats a short expiry as a long one | KILLED (2) | `test_expired_worthless_short_keeps_the_whole_credit`, `test_expired_itm_short_is_flagged_as_an_assignment` |
| M17 | manager.py | `_credit_received_usd` ignores the journaled `net_credit_usd` | KILLED (1) | `test_short_pnl_scales_with_contracts` |
| M18 | manager.py | `_credit_con_ids` never populated — the assignment detector is blind | KILLED (5) | `test_credit_con_ids_tracks_the_journal`, `test_assignment_emits_a_terminal_row_and_does_not_orphan_the_journal`, `test_assignment_is_recorded_exactly_once`, `test_disappearance_without_stock_is_not_declared_an_assignment`, `test_assignment_transition_runs_clean_through_a_whole_cycle` |

## Notes on the weaker kills

- **M17** is killed by a single test because `_credit_received_usd` has a documented fallback
  chain (`net_credit_usd` → `collateral_usd - max_loss_usd` → fill/limit price × 100 × contracts).
  On a fully-populated journal row the fallback yields the same $175, so only the multi-contract
  case (where the numbers diverge) distinguishes them. That is the fallback behaving as designed,
  not a hole — but it does mean a regression in the *primary* source alone would only be caught
  by that one test.
- **M3, M10, M13, M14, M15** are each killed by exactly one test. Each is a single-purpose guard
  with a single-purpose test; the narrowness is intentional, not thin coverage.

## Reproducing

```
ssh studio
rm -rf /tmp/mutcheck && mkdir -p /tmp/mutcheck
cd ~/exitmgr-app && rsync -a --exclude .git --exclude data --exclude __pycache__ \
    --exclude '*.bak*' --exclude '*.jsonl' --exclude '*.log' ./ /tmp/mutcheck/
/path/to/home /tmp/mutate_shortvis.py
```
