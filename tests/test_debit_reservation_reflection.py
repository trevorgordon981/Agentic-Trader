"""Public API contract; production-derived narrative omitted."""
from dataclasses import asdict
import json
import multiprocessing
from pathlib import Path
import tempfile
import time
import unittest

from exitmgr.entry_reservation import EntryReservationLedger


def dimensions(**changes):
    dims = dict(
        order_ref="entry:candidate", envelope_id="candidate", code_version="a" * 40,
        policy_version="b" * 64, con_id=201, leg_con_ids=(201,), symbol="SYML",
        sector_cluster="SEMIS", structure="long call", side="debit", capital_usd=310.0,
        collateral_usd=310.0, contracts=1, net_liq=4292.27, available_funds=5000.0,
        broker_deployed_usd=0.0, observation_readable=True,
        observed_at_monotonic=time.monotonic(), max_observation_age_s=60.0,
        open_position_count=7, max_concurrent=20, name_aggregate_usd=0.0,
        name_cap_usd=None, sector_aggregate_usd=0.0, sector_cap_usd=None,
        deployed_aggregate_usd=2152.0, deployed_cap_usd=2575.36, day_orders=0,
        max_orders_per_day=None, day_notional=0.0, max_notional_per_day=None,
        open_campaign_con_ids=(), open_campaigns=(), campaign_conflict_symbols=(),
        campaign_add_intent=None,
        markers_clear=True, visible_order_refs=(),
    )
    dims.update(changes)
    legs = tuple(sorted(set(dims["leg_con_ids"])))
    final = dict(underlying=dims["symbol"], right="C", expiry="20261218",
                 long_con_id=dims["con_id"], long_strike=50.0,
                 short_con_id=next((x for x in legs if x != dims["con_id"]), None),
                 short_strike=(55.0 if len(legs) == 2 else None), quantity=dims["contracts"],
                 side=dims["side"], structure=dims["structure"],
                 limit=round((1.0 if dims["side"] == "credit" else dims["capital_usd"])
                             / (100 * dims["contracts"]), 4),
                 max_loss_usd=(dims["capital_usd"] - 1
                               if dims["side"] == "credit" else dims["capital_usd"]))
    if dims["side"] == "credit":
        final.update(collateral_usd=dims["capital_usd"], net_credit_usd=1.0)
    dims.setdefault("final_contract", final)
    dims.setdefault("structured_intent", dict(
        schema="structured_entry_intent.v1", underlying=dims["symbol"], side=dims["side"],
        structure=dims["structure"], campaign_add_intent=dims["campaign_add_intent"],
        stage_a_intent=None, stage_b_candidate=None, final_contract=dims["final_contract"]))
    template = dict(contract_id=dims["con_id"], symbol=dims["symbol"], right="C",
                    expiry="20261218", strike=50.0)
    if len(dims["leg_con_ids"]) == 2:
        short = next(cid for cid in dims["leg_con_ids"] if cid != dims["con_id"])
        template["spread"] = dict(short_con_id=short, short_strike=55.0, width=5.0)
    if dims["side"] == "credit":
        template.update(side="credit", action="SELL", right="P",
                        collateral_usd=dims["capital_usd"],
                        max_loss_usd=dims["capital_usd"] - 1,
                        debit=dims["capital_usd"] - 1, net_credit_usd=1)
    dims["intent"] = dict(journal_template=template)
    if dims["side"] == "credit":
        dims["intent"]["estimated_debit"] = dims["capital_usd"] - 1
    return dims


def proof(**changes):
    result = dict(order_ref="entry:SYMG", con_id=101, leg_con_ids=(101, 102),
                  contracts=7, capital_usd=364.0, symbol="SYMG", sector_cluster="CRYPTO")
    result.update(changes)
    return result


def old_observer(ledger_path, channel):
    """Public API contract; production-derived narrative omitted."""
    ledger = EntryReservationLedger(ledger_path=ledger_path)
    old = dimensions(order_ref="entry:old", con_id=401, leg_con_ids=(401,),
                     capital_usd=500.0, collateral_usd=500.0,
                     deployed_aggregate_usd=1788.0, open_position_count=6)
    channel.send("observed_before_fill")
    channel.recv()
    placed = []
    decision = ledger.reserve_and_place(place=lambda: placed.append(True), **old)
    channel.send((asdict(decision), placed))
    channel.close()


class TestDebitReservationReflection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger_path = Path(self.tmp.name) / "claims.json"
        self.ledger = EntryReservationLedger(ledger_path=self.ledger_path)
        self.placed = []
        decision = self.admit(dimensions(
            order_ref="entry:SYMG", envelope_id="SYMG", con_id=101, leg_con_ids=(101, 102),
            symbol="SYMG", sector_cluster="CRYPTO", structure="bull call spread", contracts=7,
            capital_usd=392.0, collateral_usd=392.0,
            deployed_aggregate_usd=1788.0, open_position_count=6))
        self.assertTrue(decision.should_place, decision)
        self.ledger.resolve_intent("entry:SYMG", outcome="journaled", filled_qty=7)
        self.placed.clear()

    def admit(self, dims, **options):
        return self.ledger.reserve_and_place(
            place=lambda: self.placed.append(dims["order_ref"]), **options, **dims)

    def reservations(self):
        return self.ledger.snapshot()["reservations"]

    def assert_no_reflection(self, data):
        decision = self.admit(dimensions(reflected_debit_positions=data))
        self.assertFalse(decision.should_place, decision)
        self.assertEqual(decision.status, "deployed_refused", decision)
        self.assertEqual(decision.reflected_debit_order_refs, ())
        self.assertIn("entry:SYMG", self.reservations())
        self.assertEqual(self.placed, [])

    def test_incident_392_estimate_364_fill_reflected_once_and_both_candidates_still_fail(self):
        decision = self.admit(dimensions(reflected_debit_positions=(proof(),)))
        self.assertTrue(decision.should_place, decision)
        self.assertEqual(decision.reflected_debit_order_refs, ("entry:SYMG",))
        self.assertEqual(decision.reflected_debit_capital_usd, 392.0)
        self.assertEqual(decision.reflected_debit_actual_usd, 364.0)
        self.assertEqual(decision.effective_open_positions, 8)
        self.assertIn("entry:SYMG", self.reservations())
        second = self.admit(dimensions(
            order_ref="entry:SYMO", con_id=301, leg_con_ids=(301,), symbol="SYMO",
            sector_cluster="MINERS", capital_usd=217.0, collateral_usd=217.0,
            reflected_debit_positions=(proof(),)))
        self.assertFalse(second.should_place)
        self.assertEqual(second.status, "deployed_refused")
        self.assertTrue(any("2,679.00" in reason for reason in second.reasons))
        self.assertEqual(self.placed, ["entry:candidate"])

    def test_old_caller_without_proof_and_unfilled_claim_remain_reserved(self):
        self.assert_no_reflection(())

    def test_same_ref_retry_never_retransmits_even_with_reflection(self):
        decision = self.admit(dimensions(
            order_ref="entry:SYMG", envelope_id="SYMG", con_id=101, leg_con_ids=(101, 102),
            symbol="SYMG", sector_cluster="CRYPTO", contracts=7,
            capital_usd=392.0, collateral_usd=392.0,
            reflected_debit_positions=(proof(),)))
        self.assertTrue(decision.allowed)
        self.assertFalse(decision.should_place)
        self.assertEqual(decision.status, "already_reserved")
        self.assertEqual(self.placed, [])

    def test_partial_or_identity_mismatch_proofs_cannot_release_full_claim(self):
        for change in (
            dict(contracts=3), dict(contracts=0), dict(contracts=7.1), dict(contracts=True),
            dict(con_id=102), dict(con_id=101.5), dict(leg_con_ids=(101,)),
            dict(leg_con_ids=(101, 103)), dict(leg_con_ids=(101, 101)),
            dict(order_ref="entry:other"), dict(symbol="COIN"),
            dict(sector_cluster="OTHER"), dict(capital_usd=0),
            dict(capital_usd=float("nan")), dict(capital_usd=float("inf")),
            dict(capital_usd=-1), dict(capital_usd=True),
            dict(name_counted_usd=1), dict(sector_counted_usd=365),
        ):
            with self.subTest(change=change):
                self.assert_no_reflection((proof(**change),))

    def test_missing_unknown_and_malformed_evidence_is_conservative(self):
        for invalid in (None, "UNKNOWN", True, {}, (None,), ({},), (proof(), {})):
            with self.subTest(invalid=invalid):
                self.assert_no_reflection(invalid)

    def test_duplicate_and_overlapping_proof_sets_are_rejected(self):
        for invalid in ((proof(), proof()),
                        (proof(), proof(order_ref="entry:other", con_id=102,
                                        leg_con_ids=(102, 103)))):
            with self.subTest(invalid=invalid):
                self.assert_no_reflection(invalid)

    def test_an_overlapping_legacy_reservation_makes_lot_ownership_ambiguous(self):
        with self.ledger._locked():
            state = self.ledger._load_state()
            state["reservations"]["entry:another-lot"] = dict(
                state["reservations"]["entry:SYMG"], order_ref="entry:another-lot")
            self.ledger._write_state(state)
        self.assert_no_reflection((proof(),))

    def test_credit_reservation_cannot_be_reflected_by_a_debit_attestation(self):
        with self.ledger._locked():
            state = self.ledger._load_state()
            state["reservations"]["entry:SYMG"]["side"] = "credit"
            self.ledger._write_state(state)
        self.assert_no_reflection((proof(),))

    def test_combined_actual_proofs_cannot_exceed_the_book(self):
        seeded = self.admit(dimensions(
            order_ref="entry:other", con_id=501, leg_con_ids=(501, 502),
            symbol="MUTX", sector_cluster="SEMIS", contracts=7,
            capital_usd=392, collateral_usd=392, deployed_cap_usd=None))
        self.assertTrue(seeded.should_place, seeded)
        self.placed.clear()
        second_proof = proof(order_ref="entry:other", con_id=501,
                             leg_con_ids=(501, 502), symbol="MUTX", sector_cluster="SEMIS")
        decision = self.admit(dimensions(
            deployed_aggregate_usd=500, deployed_cap_usd=1000,
            reflected_debit_positions=(proof(), second_proof)))
        self.assertFalse(decision.should_place, decision)
        self.assertEqual(decision.status, "deployed_refused")
        self.assertEqual(decision.reflected_debit_order_refs, ())
        self.assertEqual(self.placed, [])

    def test_proof_notional_and_position_count_cannot_exceed_observation(self):
        for changes in (dict(deployed_aggregate_usd=363.99, deployed_cap_usd=800.0),
                        dict(open_position_count=0)):
            with self.subTest(changes=changes):
                decision = self.admit(dimensions(reflected_debit_positions=(proof(),), **changes))
                self.assertFalse(decision.should_place, decision)
                self.assertEqual(decision.reflected_debit_order_refs, ())
                self.assertIn("entry:SYMG", self.reservations())

    def test_unreadable_stale_future_and_halted_observations_refuse_before_reflection(self):
        for changes, expected in (
            (dict(observation_readable=False), "observation_unreadable"),
            (dict(observed_at_monotonic=time.monotonic() - 1000), "stale_observation"),
            (dict(observed_at_monotonic=time.monotonic() + 1000), "stale_observation"),
            (dict(markers_clear=False), "markers_set"),
        ):
            with self.subTest(expected=expected):
                decision = self.admit(dimensions(reflected_debit_positions=(proof(),), **changes))
                self.assertEqual(decision.status, expected)
                self.assertFalse(decision.should_place)
                self.assertEqual(decision.reflected_debit_order_refs, ())
                self.assertIn("entry:SYMG", self.reservations())

    def test_same_book_slot_is_not_double_counted_but_real_slot_cap_binds(self):
        decision = self.admit(dimensions(max_concurrent=8,
                                        reflected_debit_positions=(proof(),)))
        self.assertTrue(decision.should_place, decision)
        second = self.admit(dimensions(order_ref="entry:second", con_id=301,
                                       leg_con_ids=(301,), max_concurrent=8,
                                       capital_usd=1, collateral_usd=1,
                                       reflected_debit_positions=(proof(),)))
        self.assertFalse(second.should_place)
        self.assertEqual(second.status, "slot_refused")

    def test_deployed_limit_still_binds_to_the_cent(self):
        above = self.admit(dimensions(capital_usd=423.37, collateral_usd=423.37,
                                      reflected_debit_positions=(proof(),)))
        self.assertEqual(above.status, "deployed_refused")
        exact = self.admit(dimensions(capital_usd=423.36, collateral_usd=423.36,
                                      reflected_debit_positions=(proof(),)))
        self.assertTrue(exact.should_place, exact)

    def test_name_and_sector_need_explicit_counted_amount_and_still_bind(self):
        base = dict(symbol="SYMG", sector_cluster="CRYPTO", name_aggregate_usd=364,
                    name_cap_usd=700, sector_aggregate_usd=364, sector_cap_usd=700)
        no_dimension_proof = self.admit(dimensions(
            reflected_debit_positions=(proof(),), **base))
        self.assertEqual(no_dimension_proof.status, "name_refused")
        p = proof(name_counted_usd=364, sector_counted_usd=364)
        allowed = self.admit(dimensions(reflected_debit_positions=(p,), **base))
        self.assertTrue(allowed.should_place, allowed)
        refused = self.admit(dimensions(
            order_ref="entry:next", con_id=301, leg_con_ids=(301,),
            capital_usd=27, collateral_usd=27, reflected_debit_positions=(p,), **base))
        self.assertFalse(refused.should_place)
        self.assertTrue(any("single-name" in reason for reason in refused.reasons))
        self.assertTrue(any("sector" in reason for reason in refused.reasons))

    def test_name_or_sector_aggregate_cannot_cover_only_part_of_claim(self):
        for changes in (dict(name_aggregate_usd=363, name_cap_usd=700),
                        dict(sector_aggregate_usd=363, sector_cap_usd=700)):
            with self.subTest(changes=changes):
                decision = self.admit(dimensions(
                    symbol="SYMG", sector_cluster="CRYPTO",
                    reflected_debit_positions=(proof(name_counted_usd=364,
                                                      sector_counted_usd=364),), **changes))
                self.assertFalse(decision.should_place)
                self.assertIn(decision.status, ("name_refused", "sector_refused"))

    def test_cash_throttle_campaign_and_credit_cap_keep_binding(self):
        for changes, expected in (
            (dict(available_funds=701), "capacity_refused"),
            (dict(max_orders_per_day=1), "throttle_refused"),
            (dict(max_notional_per_day=701), "throttle_refused"),
            (dict(open_campaign_con_ids=(201,)), "campaign_overlap_refused"),
            (dict(side="credit", structure="cash-secured put", broker_deployed_usd=3200),
             "capacity_refused"),
        ):
            with self.subTest(expected=expected):
                decision = self.admit(dimensions(reflected_debit_positions=(proof(),), **changes))
                self.assertFalse(decision.should_place, decision)
                self.assertEqual(decision.status, expected, decision)
        self.assertEqual(self.placed, [])

    def test_working_visible_debit_not_released_by_unrelated_csp_collateral(self):
        result = self.ledger.reconcile(
            broker_deployed_usd=10000, visible_order_refs=("entry:SYMG",))
        self.assertEqual(result.removed_visible, ())
        self.assertIn("entry:SYMG", self.reservations())
        self.assert_no_reflection(())

    def test_mixed_credit_reconciliation_keeps_partial_debit_claim(self):
        credit = self.ledger.reserve(order_ref="entry:csp", con_id=901,
                                     collateral_usd=1000, net_liq=10000,
                                     available_funds=10000, broker_deployed_usd=0)
        self.assertTrue(credit.should_place, credit)
        result = self.ledger.reconcile(
            broker_deployed_usd=1000, visible_order_refs=("entry:csp", "entry:SYMG"))
        self.assertEqual(result.removed_visible, ("entry:csp",))
        self.assertIn("entry:SYMG", self.reservations())
        self.assert_no_reflection((proof(contracts=3),))

    def test_insufficient_csp_collateral_still_retains_credit_claim(self):
        credit = self.ledger.reserve(order_ref="entry:csp", con_id=901,
                                     collateral_usd=1000, net_liq=10000,
                                     available_funds=10000, broker_deployed_usd=0)
        self.assertTrue(credit.should_place)
        result = self.ledger.reconcile(
            broker_deployed_usd=999, visible_order_refs=("entry:csp", "entry:SYMG"))
        self.assertEqual(result.removed_visible, ())
        self.assertEqual(set(self.reservations()), {"entry:SYMG", "entry:csp"})

    def test_reflection_metadata_is_not_persisted_and_cash_still_counts_full_claim(self):
        original = self.reservations()["entry:SYMG"]
        decision = self.admit(dimensions(reflected_debit_positions=(proof(),)))
        self.assertTrue(decision.should_place)
        self.assertEqual(decision.outstanding_unreflected_usd, 702.0)
        self.assertEqual(decision.effective_available_funds, 4298.0)
        self.assertEqual(original, self.reservations()["entry:SYMG"])
        persisted = self.ledger_path.read_text()
        self.assertNotIn("reflected_debit", persisted)
        self.assertNotIn("name_counted_usd", persisted)
        self.assertEqual(json.loads(persisted)["version"], 1)

    def test_unknown_dimension_still_refuses_and_never_places(self):
        decision = self.admit(dimensions(reflected_debit_position=(proof(),)))
        self.assertEqual(decision.status, "ledger_error")
        self.assertFalse(decision.should_place)
        self.assertEqual(self.placed, [])

    def test_barrier_old_observer_counts_claim_after_fresh_observer_recognizes_fill(self):
        ctx = multiprocessing.get_context("spawn")
        parent, child = ctx.Pipe()
        process = ctx.Process(target=old_observer, args=(str(self.ledger_path), child))
        process.start()
        child.close()
        try:
            self.assertTrue(parent.poll(10), "old observer never reached barrier")
            self.assertEqual(parent.recv(), "observed_before_fill")
            fresh = self.admit(dimensions(
                order_ref="entry:fresh", con_id=301, leg_con_ids=(301,),
                capital_usd=50, collateral_usd=50,
                reflected_debit_positions=(proof(),)))
            self.assertTrue(fresh.should_place, fresh)
            parent.send("admit_old_snapshot")
            self.assertTrue(parent.poll(10), "old observer did not complete")
            decision, placed = parent.recv()
            self.assertEqual(decision["status"], "deployed_refused", decision)
            self.assertEqual(placed, [])
            self.assertEqual(decision["reflected_debit_order_refs"], ())
            self.assertIn("entry:SYMG", self.reservations())
        finally:
            parent.close()
            process.join(3)
            if process.is_alive():
                process.terminate()
                process.join(3)
        self.assertEqual(process.exitcode, 0)

    def test_global_deletion_negative_control_admits_unsafe_old_snapshot(self):

        with self.ledger._locked():
            state = self.ledger._load_state()
            state["reservations"].pop("entry:SYMG")
            self.ledger._write_state(state)
        decision = self.admit(dimensions(capital_usd=500, collateral_usd=500,
                                        deployed_aggregate_usd=1788,
                                        open_position_count=6))
        self.assertTrue(decision.should_place, decision)
        self.assertEqual(self.placed, ["entry:candidate"])


if __name__ == "__main__":
    unittest.main()
