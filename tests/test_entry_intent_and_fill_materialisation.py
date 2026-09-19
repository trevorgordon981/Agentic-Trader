"""Public API contract; production-derived narrative omitted."""
import json
import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._admission_stub import stub_admission_reads
from exitmgr import entry_safety
from exitmgr.entry_reservation import (
    EntryReservationError, EntryReservationLedger)
from exitmgr.exec_capture import (
    entry_fill_fields_from_executions, entry_journal_fields, fetch_fills,
    fill_facts_from_trade)
from exitmgr.risk import RiskLimits
from exitmgr.runtime_identity import RuntimeIdentity, RuntimeIdentityError
from exitmgr.trader import (
    EntryOrderView, ResolvedOrder, Trader, WorkingEntryOrder,
    protective_sell_gate, reconcile_entry_intents)


CODE_VERSION = "a" * 40
POLICY_VERSION = "b" * 64



class _Status:
    """Public API contract; production-derived narrative omitted."""

    def __init__(self, status, filled=None, remaining=None, avg=None):
        self.status = status
        if filled is not None:
            self.filled = filled
        if remaining is not None:
            self.remaining = remaining
        if avg is not None:
            self.avgFillPrice = avg


class _Order:
    def __init__(self, ref="alfred-entry:" + "a" * 32, qty=3, order_id=7):
        self.orderRef = ref
        self.totalQuantity = qty
        self.orderId = order_id
        self.action = "BUY"


class _Trade:
    def __init__(self, status, *, filled=None, remaining=None, avg=None, qty=3, fills=()):
        self.orderStatus = _Status(status, filled, remaining, avg)
        self.order = _Order(qty=qty)
        self.fills = list(fills)
        self.log = []


def _contract(con_id):
    c = MagicMock()
    c.conId = con_id
    return c


def _trader(tmp_path, trade):
    ibc = MagicMock()
    ibc.ib = MagicMock()
    ibc.ib.placeOrder.side_effect = lambda _c, _o: trade
    stub_admission_reads(ibc)
    ledger = EntryReservationLedger(ledger_path=tmp_path / "ledger.json")
    return Trader(
        ib_conn=ibc, exit_manager=MagicMock(), limits=RiskLimits(), approved_names=set(),
        endpoint="http://x", model="m", slack_token="t", slack_channel="C", approver_ids=set(),
        baseline_path=str(tmp_path / "b.json"), audit_path=str(tmp_path / "a.jsonl"),
        journal_path=str(tmp_path / "trades.log"), entry_reservation_ledger=ledger)


def _order(qty=3, con_id=111):
    return ResolvedOrder(
        "SPY", "C", "20260620", 610.0, qty, 1.20, _contract(con_id),
        entry_bid=1.15, entry_ask=1.25, quote_observed_at=time.monotonic(),
        decision_id="decision-" + "a" * 32)


def _journal_rows(tmp_path):
    path = tmp_path / "trades.log"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]



@pytest.mark.asyncio
async def test_a_rejected_order_creates_no_active_position(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    trade = _Trade("Rejected", filled=0, remaining=0, qty=3)
    t = _trader(tmp_path, trade)

    status, _ = await t._submit_order(
        _order(qty=3),
        submission_authority=entry_safety.EntrySubmissionAuthority.autonomous())

    assert status == "Rejected"
    assert _journal_rows(tmp_path) == [], "a rejected order journalled a phantom position"


    assert t.entry_reservation_ledger.open_intents() == ()


def test_the_rule_itself_refuses_every_non_fill():
    """Public API contract; production-derived narrative omitted."""
    for status in ("Rejected", "Cancelled", "ApiCancelled", "Inactive"):
        ok, _, facts = entry_journal_fields(
            _Trade(status, filled=0, remaining=0), requested_qty=3, estimated_debit=360.0)
        assert not ok and facts.terminal and facts.filled_qty == 0

    ok, _, facts = entry_journal_fields(
        _Trade("Submitted", filled=0, remaining=3), requested_qty=3, estimated_debit=360.0)
    assert not ok and not facts.terminal


def test_a_mock_cannot_masquerade_as_a_broker_fill():
    """Public API contract; production-derived narrative omitted."""
    mocked = MagicMock()
    mocked.orderStatus.status = "Filled"
    mocked.fills = []
    ok, _, facts = entry_journal_fields(mocked, requested_qty=3, estimated_debit=360.0)
    assert facts.filled_qty_source == "unobserved"
    assert not ok, "a MagicMock's __float__ was accepted as a broker-reported quantity"


def test_place_trade_no_longer_journals_unconditionally():
    """Public API contract; production-derived narrative omitted."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "place_trade.py")).read()
    assert "if _journalable:" in src
    head = src.split("if _journalable:", 1)[0]
    assert 'with open(_jpath, "a")' not in head, "the manual path still writes before the gate"



@pytest.mark.asyncio
async def test_qty3_filled1_remaining2_journals_nothing_until_the_remainder_is_terminal(
        tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    trade = _Trade("Submitted", filled=1, remaining=2, avg=1.25, qty=3)
    t = _trader(tmp_path, trade)

    await t._submit_order(
        _order(qty=3),
        submission_authority=entry_safety.EntrySubmissionAuthority.autonomous())

    assert _journal_rows(tmp_path) == [], "a mid-fill order was journalled"
    intents = t.entry_reservation_ledger.open_intents()
    assert len(intents) == 1, "the partial fill left no durable record of itself"
    ref = intents[0]["order_ref"]
    assert intents[0]["requested_qty"] == 3


    written = []
    fills = [{"con_id": 111, "order_ref": ref, "side": "BOT", "shares": 1, "price": 1.25,
              "mult": 100, "commission": 0.65, "time": "2026-08-22T10:00:00+00:00"}]
    result = await reconcile_entry_intents(
        MagicMock(), ledger=t.entry_reservation_ledger, journal_append=written.append,
        view=EntryOrderView(True, (), frozenset(), 0, None, ()), fills=fills)

    assert result.journaled == (ref,)
    assert len(written) == 1
    row = written[0]
    assert row["quantity"] == 1, "the reconciled row must carry the FILLED quantity, not 3"
    assert row["quantity_requested"] == 3
    assert row["debit"] == 125.0 and row["basis_source"] == "fill"
    assert row["entry_commission"] == 0.65


@pytest.mark.asyncio
async def test_a_fully_filled_order_still_journals_immediately(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    trade = _Trade("Filled", filled=3, remaining=0, avg=1.10, qty=3)
    t = _trader(tmp_path, trade)

    await t._submit_order(
        _order(qty=3),
        submission_authority=entry_safety.EntrySubmissionAuthority.autonomous())

    rows = _journal_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["quantity"] == 3 and rows[0]["quantity_source"] == "order_status"
    assert rows[0]["order_terminal"] is True

    assert rows[0]["debit"] == 330.0 and rows[0]["basis_source"] == "fill"
    assert t.entry_reservation_ledger.open_intents() == ()


def test_a_partially_filled_working_order_is_not_journalable_and_says_why():
    ok, fields, facts = entry_journal_fields(
        _Trade("Submitted", filled=1, remaining=2), requested_qty=3, estimated_debit=360.0)
    assert not ok and facts.partial_working
    assert fields["entry_remaining_qty"] == 2 and fields["order_terminal"] is False
    assert "quantity" not in fields, "a non-journalable result must not carry a quantity at all"


def test_a_partial_fill_that_then_terminates_is_valued_at_the_part_that_filled():
    """Public API contract; production-derived narrative omitted."""
    ok, fields, _ = entry_journal_fields(
        _Trade("Cancelled", filled=1, remaining=0), requested_qty=3, estimated_debit=360.0)
    assert ok and fields["quantity"] == 1
    assert fields["debit"] == 120.0
    assert fields["basis_source"] == "estimate_prorated"
    assert fields["basis_error"] is True, "an unobservable basis on an ACTIVE row is an incident"


def test_a_two_leg_combo_is_not_double_counted():
    """Public API contract; production-derived narrative omitted."""
    fills = [
        {"con_id": 111, "side": "BOT", "shares": 1, "price": 3.00, "mult": 100, "time": "t1"},
        {"con_id": 222, "side": "SLD", "shares": 1, "price": 1.75, "mult": 100, "time": "t2"},
    ]
    ok, fields = entry_fill_fields_from_executions(
        fills, primary_con_id=111, requested_qty=3, estimated_debit=375.0,
        leg_con_ids=(111, 222))
    assert ok and fields["quantity"] == 1, "a two-leg combo was counted as two contracts"
    assert fields["debit"] == 125.0, "the basis must be the NET of both legs, not the long leg"


@pytest.mark.parametrize("fills", [
    [{"con_id": 111, "side": "BOT", "shares": 1, "price": 3.0, "mult": 100}],
    [{"con_id": 111, "side": "BOT", "shares": 2, "price": 3.0, "mult": 100},
     {"con_id": 222, "side": "SLD", "shares": 1, "price": 1.75, "mult": 100}],
])
def test_spread_recovery_waits_for_every_equal_leg(fills):
    ok, fields = entry_fill_fields_from_executions(
        fills, primary_con_id=111, requested_qty=2, estimated_debit=250.0,
        leg_con_ids=(111, 222))
    assert ok is False
    assert fields["recovery_error"] == "incomplete_or_unequal_combo_leg_executions"


def test_recovered_journal_append_and_intent_resolution_are_idempotent(tmp_path):
    ledger = EntryReservationLedger(ledger_path=tmp_path / "ledger.json")
    ref = "alfred-entry:" + "7" * 32
    ledger.record_intent(order_ref=ref, con_id=111, symbol="SPY", side="debit",
                         requested_qty=1, estimated_debit=125.0)
    journal = tmp_path / "trades.log"

    def has_ref(order_ref):
        if not journal.exists():
            return False
        return any(json.loads(line).get("order_ref") == order_ref
                   for line in journal.read_text().splitlines() if line.strip())

    def append(row):
        with journal.open("a") as handle:
            handle.write(json.dumps(row) + "\n")

    first = ledger.journal_and_resolve_intent(
        ref, row={"order_ref": ref}, filled_qty=1,
        journal_append=append, already_journalled=has_ref)
    second = ledger.journal_and_resolve_intent(
        ref, row={"order_ref": ref}, filled_qty=1,
        journal_append=append, already_journalled=has_ref)
    assert first == {"resolved": True, "duplicate": False, "filled_qty": 1}
    assert second == {"resolved": False, "duplicate": True, "filled_qty": 1}
    assert len(journal.read_text().splitlines()) == 1



def _working(ref, con_id, remaining=2.0, status="Submitted", action="BUY"):
    return WorkingEntryOrder(order_ref=ref, action=action, status=status,
                             con_ids=frozenset({con_id}), remaining=remaining, filled=1.0)


def test_no_protective_sell_while_the_opening_remainder_is_still_working():
    ref = "alfred-entry:" + "a" * 32
    row = {"order_ref": ref, "contract_id": 111, "quantity": 1}
    view = EntryOrderView(True, (), frozenset({ref}), 1, None, (_working(ref, 111),))

    gate = protective_sell_gate(row, view)

    assert gate.allowed is False
    assert gate.remaining_qty == 2.0
    assert "RECREATES" in gate.reasons[0]


def test_the_sell_is_allowed_once_the_opening_order_is_terminal_and_manages_the_filled_qty():
    ref = "alfred-entry:" + "a" * 32
    row = {"order_ref": ref, "contract_id": 111, "quantity": 1}
    view = EntryOrderView(True, (), frozenset(), 0, None, ())

    gate = protective_sell_gate(row, view)

    assert gate.allowed is True
    assert gate.manageable_qty == 1, "the manager must size the exit to the FILLED quantity"


def test_an_unreadable_order_book_refuses_the_sell():
    """Public API contract; production-derived narrative omitted."""
    gate = protective_sell_gate({"order_ref": "x", "quantity": 1},
                                EntryOrderView(False, error="timed out"))
    assert gate.allowed is False and "never an empty one" in gate.reasons[0]


def test_a_legacy_row_with_no_order_ref_is_still_protected_by_contract():
    """Public API contract; production-derived narrative omitted."""
    view = EntryOrderView(True, (), frozenset(), 1, None,
                          (_working("", 111),))
    gate = protective_sell_gate({"contract_id": 111, "quantity": 2}, view)
    assert gate.allowed is False and "still working on contract 111" in gate.reasons[0]


def test_a_resting_SELL_does_not_block_the_protective_path():
    """Public API contract; production-derived narrative omitted."""
    ref = "alfred-entry:" + "b" * 32
    view = EntryOrderView(True, (), frozenset(), 0, None,
                          (_working(ref, 111, remaining=1.0, action="SELL"),))
    assert protective_sell_gate({"order_ref": ref, "quantity": 1}, view).allowed is True



class _BoomLock(Exception):
    pass


def _admit(ledger, *, ref, con_id=111, qty=3, place):
    return ledger.reserve_and_place(
        place=place,
        intent={"order_ref": ref, "con_id": con_id, "symbol": "SPY", "side": "debit",
                "source": "test", "requested_qty": qty, "estimated_debit": 375.0,
                "journal_template": {"contract_id": con_id, "symbol": "SPY", "right": "C",
                                     "expiry": "20260620", "strike": 610.0,
                                     "stop_pct": 30.0, "order_ref": ref}},
        order_ref=ref, envelope_id="", code_version=CODE_VERSION,
        policy_version=POLICY_VERSION, con_id=con_id, leg_con_ids=(con_id,), symbol="SPY",
        sector_cluster="TECH", structure="", side="debit", capital_usd=375.0,
        collateral_usd=375.0, contracts=qty, net_liq=100_000.0, available_funds=60_000.0,
        broker_deployed_usd=0.0, observation_readable=True,
        observed_at_monotonic=time.monotonic(), max_observation_age_s=90.0,
        open_position_count=0, max_concurrent=8, name_aggregate_usd=0.0, name_cap_usd=None,
        sector_aggregate_usd=0.0, sector_cap_usd=None, deployed_aggregate_usd=0.0,
        deployed_cap_usd=None, day_orders=0, max_orders_per_day=None, day_notional=0.0,
        max_notional_per_day=None, open_campaign_con_ids=(), open_campaigns=(),
        campaign_conflict_symbols=(),
        campaign_add_intent=None,
        final_contract={"underlying": "SPY", "right": "C", "expiry": "20260620",
                        "long_con_id": con_id, "long_strike": 610.0,
                        "short_con_id": None, "short_strike": None, "quantity": qty,
                        "side": "debit", "structure": "", "limit": 375.0 / (100 * qty),
                        "max_loss_usd": 375.0},
        structured_intent={"schema": "structured_entry_intent.v1", "underlying": "SPY",
                           "side": "debit", "structure": "", "campaign_add_intent": None,
                           "stage_a_intent": None, "stage_b_candidate": None,
                           "final_contract": {"underlying": "SPY", "right": "C",
                                              "expiry": "20260620", "long_con_id": con_id,
                                              "long_strike": 610.0, "short_con_id": None,
                                              "short_strike": None, "quantity": qty,
                                              "side": "debit", "structure": "",
                                              "limit": 375.0 / (100 * qty),
                                              "max_loss_usd": 375.0}},
        markers_clear=True,
        visible_order_refs=())


@pytest.mark.asyncio
async def test_accepted_then_raised_reconciles_to_a_fill_backed_row_and_never_resubmits(tmp_path):
    ledger = EntryReservationLedger(ledger_path=tmp_path / "ledger.json")
    ref = "alfred-entry:" + "c" * 32



    def _place():
        raise RuntimeError("connection reset after the order was transmitted")

    with pytest.raises(RuntimeError):
        _admit(ledger, ref=ref, place=_place)


    intents = ledger.open_intents()
    assert len(intents) == 1 and intents[0]["order_ref"] == ref
    assert intents[0]["transmitted"] == "unknown"


    written = []
    ib = MagicMock()
    fills = [{"con_id": 111, "order_ref": ref, "side": "BOT", "shares": 1, "price": 1.30,
              "mult": 100, "commission": 1.05, "time": "2026-08-22T10:00:00+00:00"}]
    result = await reconcile_entry_intents(
        ib, ledger=ledger, journal_append=written.append,
        view=EntryOrderView(True, (), frozenset(), 0, None, ()), fills=fills)

    assert result.readable and result.journaled == (ref,)
    assert len(written) == 1
    row = written[0]
    assert row["quantity"] == 1 and row["quantity_source"] == "executions"
    assert row["debit"] == 130.0 and row["basis_source"] == "fill"
    assert row["entry_commission"] == 1.05
    assert row["contract_id"] == 111 and row["stop_pct"] == 30.0, "not protectable"
    assert row["entry_reconciled_from_intent"] is True

    assert ledger.open_intents() == ()
    ib.placeOrder.assert_not_called()


@pytest.mark.asyncio
async def test_reconciliation_refuses_wholesale_when_the_order_book_is_unreadable(tmp_path):
    ledger = EntryReservationLedger(ledger_path=tmp_path / "ledger.json")
    ref = "alfred-entry:" + "d" * 32
    with pytest.raises(RuntimeError):
        _admit(ledger, ref=ref, place=lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    written = []




    result = await reconcile_entry_intents(
        MagicMock(), ledger=ledger, journal_append=written.append,
        view=EntryOrderView(False, error="timed out"), fills=[], min_settle_age_s=0.0)

    assert result.readable is False and result.retained == (ref,)
    assert written == []
    assert len(ledger.open_intents()) == 1, "an intent was released against an unreadable book"


@pytest.mark.asyncio
async def test_an_intent_whose_order_is_still_working_is_never_released(tmp_path):
    ledger = EntryReservationLedger(ledger_path=tmp_path / "ledger.json")
    ref = "alfred-entry:" + "e" * 32
    with pytest.raises(RuntimeError):
        _admit(ledger, ref=ref, place=lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    result = await reconcile_entry_intents(
        MagicMock(), ledger=ledger, journal_append=lambda _r: None,
        view=EntryOrderView(True, (), frozenset({ref}), 1, None, (_working(ref, 111),)),
        fills=[])

    assert result.still_working == (ref,) and result.released == ()
    assert len(ledger.open_intents()) == 1


def test_a_definite_pre_transmit_failure_drops_the_intent(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    from exitmgr.order_lock import OrderMutationBusy
    ledger = EntryReservationLedger(ledger_path=tmp_path / "ledger.json")
    ref = "alfred-entry:" + "f" * 32

    def _busy():
        raise OrderMutationBusy("another mutation holds the lock")

    with pytest.raises(OrderMutationBusy):
        _admit(ledger, ref=ref, place=_busy)
    assert ledger.open_intents() == (), "an untransmitted order left a durable intent behind"


def test_an_intent_may_not_be_released_for_any_reason_other_than_a_broker_fact(tmp_path):
    ledger = EntryReservationLedger(ledger_path=tmp_path / "ledger.json")
    ref = "alfred-entry:" + "0" * 32
    ledger.record_intent(order_ref=ref, con_id=111, symbol="SPY", side="debit",
                         requested_qty=3, estimated_debit=375.0)
    for bad in ("expired", "stale", "unreadable", "", None):
        with pytest.raises(EntryReservationError):
            ledger.resolve_intent(ref, outcome=bad)

    with pytest.raises(EntryReservationError):
        ledger.resolve_intent(ref, outcome="terminal_no_fill", filled_qty=2)
    with pytest.raises(EntryReservationError):
        ledger.resolve_intent(ref, outcome="journaled", filled_qty=0)
    assert len(ledger.open_intents()) == 1


def test_the_capacity_reconciler_never_takes_an_intent_with_it(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    now = [time.time()]
    ledger = EntryReservationLedger(ledger_path=tmp_path / "ledger.json", clock=lambda: now[0])
    ref = "alfred-entry:" + "1" * 32
    _admit(ledger, ref=ref, place=lambda: None)
    assert ref in ledger.snapshot()["reservations"]

    ledger.reconcile(broker_deployed_usd=10_000.0, visible_order_refs=(ref,), readable=True)
    assert ref in ledger.snapshot()["reservations"], "unrelated CSP collateral released a debit"
    assert len(ledger.open_intents()) == 1
    now[0] += ledger.ttl_seconds + 1
    ledger.reconcile(broker_deployed_usd=0.0, visible_order_refs=(), readable=True)
    assert ledger.snapshot()["reservations"] == {}
    assert len(ledger.open_intents()) == 1, "the capacity reconciler deleted the durable intent"


def test_the_reconciler_contains_no_order_placement_at_all():
    """Public API contract; production-derived narrative omitted."""
    import inspect
    from exitmgr import trader as _t
    src = inspect.getsource(_t.reconcile_entry_intents)
    for forbidden in ("placeOrder", "cancelOrder", "order_mutation_lock", "Order("):
        assert forbidden not in src, f"the intent reconciler references {forbidden}"


@pytest.mark.asyncio
async def test_unreadable_execution_feed_retains_every_intent(tmp_path):
    ledger = EntryReservationLedger(ledger_path=tmp_path / "ledger.json")
    ref = "alfred-entry:" + "2" * 32
    with pytest.raises(RuntimeError):
        _admit(ledger, ref=ref,
               place=lambda: (_ for _ in ()).throw(RuntimeError("ambiguous submit")))
    ib = MagicMock()
    ib.reqExecutionsAsync = AsyncMock(side_effect=TimeoutError("execution timeout"))

    result = await reconcile_entry_intents(
        ib, ledger=ledger, journal_append=lambda _row: None,
        view=EntryOrderView(True, (), frozenset(), 0, None, ()), min_settle_age_s=0.0)

    assert result.readable is False and result.retained == (ref,)
    assert tuple(row["order_ref"] for row in ledger.open_intents()) == (ref,)


@pytest.mark.asyncio
async def test_strict_fill_read_raises_while_best_effort_remains_empty():
    ib = MagicMock()
    ib.reqExecutionsAsync = AsyncMock(side_effect=TimeoutError("execution timeout"))
    assert await fetch_fills(ib) == []
    with pytest.raises(RuntimeError, match="execution feed unreadable"):
        await fetch_fills(ib, strict=True)


def _raw_fill(order_ref, exec_id):
    return SimpleNamespace(
        execution=SimpleNamespace(
            execId=exec_id, orderRef=order_ref, orderId=7, permId=8, clientId=9,
            acctNumber="DU123", side="BOT", shares=1, price=1.25,
            time="2026-08-25T12:00:00+00:00"),
        contract=SimpleNamespace(
            conId=111, symbol="SPY", secType="OPT", multiplier="100", right="C",
            strike=500, lastTradeDateOrContractMonth="20261218"),
        commissionReport=None,
    )


@pytest.mark.asyncio
async def test_strict_fill_read_ignores_only_provably_unrelated_malformed_rows():
    wanted = "alfred-entry:" + "7" * 32
    ib = MagicMock()
    ib.reqExecutionsAsync = AsyncMock(return_value=[
        _raw_fill("manual-unrelated", ""),
        _raw_fill(wanted, "exec-1"),
    ])
    rows = await fetch_fills(ib, strict=True, relevant_order_refs={wanted})
    assert [row["order_ref"] for row in rows] == [wanted]


@pytest.mark.asyncio
async def test_strict_fill_read_returns_scoped_error_evidence_for_a_matching_malformed_row():
    wanted = "alfred-entry:" + "8" * 32
    ib = MagicMock()
    ib.reqExecutionsAsync = AsyncMock(return_value=[_raw_fill(wanted, "")])
    rows = await fetch_fills(ib, strict=True, relevant_order_refs={wanted})
    assert rows == [{"order_ref": wanted, "_normalization_error": True}]


@pytest.mark.asyncio
async def test_strict_fill_read_blocks_an_unidentifiable_malformed_row():
    wanted = "alfred-entry:" + "8" * 32
    ib = MagicMock()
    ib.reqExecutionsAsync = AsyncMock(return_value=[_raw_fill(None, "")])
    with pytest.raises(RuntimeError, match="unnormalizable fill relevant"):
        await fetch_fills(ib, strict=True, relevant_order_refs={wanted})


@pytest.mark.asyncio
async def test_malformed_a_does_not_strand_valid_b(tmp_path):
    ledger = EntryReservationLedger(ledger_path=tmp_path / "ledger.json")
    ref_a = "alfred-entry:" + "a" * 32
    ref_b = "alfred-entry:" + "b" * 32
    for ref, con_id in ((ref_a, 111), (ref_b, 222)):
        with pytest.raises(RuntimeError):
            _admit(ledger, ref=ref, con_id=con_id, qty=1,
                   place=lambda: (_ for _ in ()).throw(RuntimeError("ambiguous submit")))
    fills = [
        {"order_ref": ref_a, "_normalization_error": True},
        {"con_id": 222, "order_ref": ref_b, "side": "BOT", "shares": 1,
         "price": 1.25, "mult": 100, "commission": 0.65,
         "time": "2026-08-25T12:00:00+00:00"},
    ]
    written = []
    result = await reconcile_entry_intents(
        MagicMock(), ledger=ledger, journal_append=written.append,
        view=EntryOrderView(True, (), frozenset(), 0, None, ()), fills=fills)
    assert result.journaled == (ref_b,)
    assert result.retained == (ref_a,)
    assert [row["order_ref"] for row in written] == [ref_b]
    assert [row["order_ref"] for row in ledger.open_intents()] == [ref_a]


@pytest.mark.asyncio
async def test_reconciler_scopes_strict_execution_read_to_its_pending_refs(tmp_path, monkeypatch):
    ledger = EntryReservationLedger(ledger_path=tmp_path / "ledger.json")
    ref = "alfred-entry:" + "9" * 32
    with pytest.raises(RuntimeError):
        _admit(ledger, ref=ref,
               place=lambda: (_ for _ in ()).throw(RuntimeError("ambiguous submit")))
    observed = {}

    async def _fetch(_ib, _days, *, strict, relevant_order_refs):
        observed.update(strict=strict, refs=set(relevant_order_refs))
        return []

    from exitmgr import exec_capture
    monkeypatch.setattr(exec_capture, "fetch_fills", _fetch)
    await reconcile_entry_intents(
        MagicMock(), ledger=ledger, journal_append=lambda _row: None,
        view=EntryOrderView(True, (), frozenset(), 0, None, ()))
    assert observed == {"strict": True, "refs": {ref}}


@pytest.mark.asyncio
@pytest.mark.parametrize("membership", [lambda _ref: None,
                                         lambda _ref: (_ for _ in ()).throw(OSError("EIO"))])
async def test_unknown_journal_membership_retains_the_whole_batch(tmp_path, membership):
    ledger = EntryReservationLedger(ledger_path=tmp_path / "ledger.json")
    ref = "alfred-entry:" + "3" * 32
    with pytest.raises(RuntimeError):
        _admit(ledger, ref=ref,
               place=lambda: (_ for _ in ()).throw(RuntimeError("ambiguous submit")))
    fills = [{"con_id": 111, "order_ref": ref, "side": "BOT", "shares": 1,
              "price": 1.30, "mult": 100, "commission": 1.05,
              "time": "2026-08-22T10:00:00+00:00"}]
    written = []

    result = await reconcile_entry_intents(
        MagicMock(), ledger=ledger, journal_append=written.append,
        view=EntryOrderView(True, (), frozenset(), 0, None, ()), fills=fills,
        already_journalled=membership)

    assert result.readable is False and result.retained == (ref,)
    assert written == [] and len(ledger.open_intents()) == 1


def test_malformed_journal_content_is_unknown_not_known_absent(tmp_path):
    trader = Trader.__new__(Trader)
    trader.journal_path = str(tmp_path / "trades.log")
    (tmp_path / "trades.log").write_text('{"truncated":\n')
    assert trader._journal_has_order_ref("alfred-entry:" + "4" * 32) is None


def test_recovered_fill_preserves_intent_identity_not_current_process_identity(tmp_path):
    trader = Trader.__new__(Trader)
    trader.journal_path = str(tmp_path / "trades.log")
    trader.runtime_identity = RuntimeIdentity("c" * 40, "d" * 64)
    trader._append_reconciled_journal({
        "order_ref": "alfred-entry:" + "5" * 32,
        "code_version": CODE_VERSION, "policy_version": POLICY_VERSION})
    row = json.loads((tmp_path / "trades.log").read_text())
    assert row["code_version"] == CODE_VERSION
    assert row["policy_version"] == POLICY_VERSION
    assert row["provenance_status"] == "source_bound_intent"


def test_recovered_legacy_fill_stays_explicitly_unknown(tmp_path):
    trader = Trader.__new__(Trader)
    trader.journal_path = str(tmp_path / "trades.log")
    trader.runtime_identity = RuntimeIdentity("c" * 40, "d" * 64)
    trader._append_reconciled_journal({"order_ref": "legacy", "code_version": "",
                                       "policy_version": ""})
    row = json.loads((tmp_path / "trades.log").read_text())
    assert row["code_version"] == row["policy_version"] == ""
    assert row["provenance_status"] == "legacy_unknown"


def test_recovered_fill_with_half_an_identity_is_refused(tmp_path):
    trader = Trader.__new__(Trader)
    trader.journal_path = str(tmp_path / "trades.log")
    trader.runtime_identity = RuntimeIdentity("c" * 40, "d" * 64)
    with pytest.raises(RuntimeIdentityError):
        trader._append_reconciled_journal({"order_ref": "bad",
                                           "code_version": CODE_VERSION,
                                           "policy_version": ""})
    assert not (tmp_path / "trades.log").exists()


@pytest.mark.asyncio
async def test_readable_empty_books_do_not_claim_terminal_no_fill(tmp_path):
    ledger = EntryReservationLedger(ledger_path=tmp_path / "ledger.json")
    ref = "alfred-entry:" + "6" * 32
    with pytest.raises(RuntimeError):
        _admit(ledger, ref=ref,
               place=lambda: (_ for _ in ()).throw(RuntimeError("ambiguous submit")))
    created = ledger.open_intents()[0]["created_at"]
    result = await reconcile_entry_intents(
        MagicMock(), ledger=ledger, journal_append=lambda _row: None,
        view=EntryOrderView(True, (), frozenset(), 0, None, ()), fills=[],
        min_settle_age_s=0.0, now=created + 86_400)
    assert result.released == () and result.retained == (ref,)
    assert len(ledger.open_intents()) == 1


def test_fill_facts_never_raise_on_a_hostile_trade_object():
    class _Boom:
        def __getattr__(self, _name):
            raise RuntimeError("exploding trade")

    facts = fill_facts_from_trade(_Boom())
    assert facts.filled_qty_source == "unobserved" and not facts.journalable



def _journal(tmp_path, rows):
    path = tmp_path / "trades.log"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def test_a_scaled_campaign_reports_the_capital_of_every_lot(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    _journal(tmp_path, [
        {"contract_id": 555, "symbol": "SYMF", "quantity": 1, "debit": 260.0},
        {"contract_id": 555, "symbol": "SYMF", "quantity": 1, "debit": 265.0},
    ])
    t = _trader(tmp_path, _Trade("Filled", filled=1, remaining=0, avg=1.0))
    assert t._load_journal_debits()[555] == 525.0


def test_a_con_id_reused_after_a_flat_reports_only_the_CURRENT_campaign(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    _journal(tmp_path, [
        {"contract_id": 555, "symbol": "SYMF", "quantity": 1, "debit": 900.0},
        {"event": "closed_by_tool", "contract_id": 555, "symbol": "SYMF", "quantity": 1},
        {"contract_id": 555, "symbol": "SYMF", "quantity": 1, "debit": 260.0},
    ])
    t = _trader(tmp_path, _Trade("Filled", filled=1, remaining=0, avg=1.0))
    assert t._load_journal_debits()[555] == 260.0, "a closed campaign was added to the open one"
