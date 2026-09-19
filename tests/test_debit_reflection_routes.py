"""Public API contract; production-derived narrative omitted."""
import asyncio
import copy
import json
import socket
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from exitmgr.connection import PositionData
from exitmgr.entry_reflection import capture_journal_basis, build_debit_risk_book
from exitmgr.risk import OpenPosition, RiskLimits


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("reflection tests must not open any network socket")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


def entry(**changes):
    row = dict(contract_id=101, symbol="SYMG", right="C", strike=44.0,
               expiry="20261023", quantity=7, quantity_requested=7, debit=364.0,
               entry_fill_debit=364.0, order_status="Filled", order_terminal=True,
               basis_source="fill", quantity_source="order_status", entry_remaining_qty=0,
               order_ref="alfred-entry:fixture-filled", ts="2026-09-09T06:57:30-07:00",
               spread=dict(short_con_id=102, short_strike=45.0))
    row.update(changes)
    return row


def raw_book(*, long_qty=7, short_qty=-7, **long_changes):
    long = dict(con_id=101, symbol="SYMG", right="C", quantity=long_qty,
                avg_cost=3.50, expiry="20261023", sec_type="OPT", strike=44.0)
    long.update(long_changes)
    return {101: PositionData(**long),
            102: PositionData(con_id=102, symbol="SYMG", right="C", quantity=short_qty,
                              avg_cost=2.98, expiry="20261023", sec_type="OPT", strike=45.0)}


def journal(tmp_path, rows):
    path = tmp_path / "entries.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


def built(path, raw=None):
    basis = capture_journal_basis(path)
    return build_debit_risk_book(raw_book() if raw is None else raw, basis,
                                observed_at_monotonic=time.monotonic())


def test_exact_seven_contract_spread_proves_actual_debit_and_one_slot(tmp_path):
    book = built(journal(tmp_path, [entry()]))
    assert len(book) == 1
    assert book[0].notional == 364.0
    assert book[0].is_credit is False
    proof = book[0].entry_reflection
    assert dict(proof) == dict(order_ref="alfred-entry:fixture-filled", con_id=101,
        leg_con_ids=(101, 102), contracts=7, capital_usd=364.0, symbol="SYMG",
        journal_captured_at_monotonic=proof["journal_captured_at_monotonic"],
        position_observed_at_monotonic=proof["position_observed_at_monotonic"])
    assert proof["journal_captured_at_monotonic"] <= proof["position_observed_at_monotonic"]
    assert asdict(book[0])["entry_reflection"]["capital_usd"] == 364.0


def test_single_long_option_requires_no_invented_short_leg(tmp_path):
    row = entry(spread=None)
    book = built(journal(tmp_path, [row]), {101: raw_book()[101]})
    assert book[0].entry_reflection["leg_con_ids"] == (101,)


@pytest.mark.parametrize("changes", [
    {"order_status": "Submitted"}, {"order_terminal": False},
    {"order_terminal": "true"}, {"quantity_requested": 8}, {"quantity": 6},
    {"entry_remaining_qty": 1}, {"entry_remaining_qty": None},
    {"entry_remaining_qty": False}, {"entry_fill_debit": 392},
    {"entry_fill_debit": 0}, {"entry_fill_debit": float("nan")},
    {"entry_fill_debit": float("inf")}, {"basis_source": "limit"},
    {"quantity_source": "requested"}, {"order_ref": ""},
    {"order_ref": "alfred-entry:"}, {"order_ref": "other-client:filled"},
    {"symbol": "OTHER"}, {"right": "P"}, {"expiry": "20261030"},
    {"strike": 43.0}, {"spread": {"short_con_id": 999, "short_strike": 45.0}},
    {"spread": {"short_con_id": 101, "short_strike": 45.0}},
])
def test_incomplete_or_wrong_fill_never_proves_reflection(tmp_path, changes):
    book = built(journal(tmp_path, [entry(**changes)]))
    assert len(book) == 1
    assert book[0].notional > 0
    assert book[0].entry_reflection is None


@pytest.mark.parametrize("long_qty,short_qty", [(8, -7), (6, -7), (7, -6), (7, 7), (7, 0)])
def test_signed_leg_quantities_must_match_exactly(tmp_path, long_qty, short_qty):
    book = built(journal(tmp_path, [entry()]), raw_book(long_qty=long_qty, short_qty=short_qty))
    SYMG_long = book[0]
    assert SYMG_long.entry_reflection is None


@pytest.mark.parametrize("changes", [{"symbol": "OTHER"}, {"right": "P"},
    {"expiry": "20261030"}, {"strike": 46.0}, {"sec_type": "STK"}])
def test_short_contract_identity_is_verified(tmp_path, changes):
    raw = raw_book()
    for field, value in changes.items():
        setattr(raw[102], field, value)
    assert built(journal(tmp_path, [entry()]), raw)[0].entry_reflection is None


def test_aggregated_same_contract_lots_preserve_full_basis_without_reflection(tmp_path):
    first = entry(quantity=3, quantity_requested=3, debit=156, entry_fill_debit=156,
                  order_ref="alfred-entry:first")
    second = entry(quantity=4, quantity_requested=4, debit=208, entry_fill_debit=208,
                   order_ref="alfred-entry:second")
    book = built(journal(tmp_path, [first, second]))
    assert len(book) == 1 and book[0].notional == 364
    assert book[0].entry_reflection is None


def test_recycled_contract_number_cannot_claim_old_order_ref(tmp_path):
    old = entry(expiry="20250919", order_ref="alfred-entry:old", debit=100, entry_fill_debit=100)
    book = built(journal(tmp_path, [old, entry()]))
    assert book[0].notional == 364
    assert book[0].entry_reflection["order_ref"] == "alfred-entry:fixture-filled"


def test_partial_journal_append_cannot_hide_missing_lot_under_old_net_basis(tmp_path):
    path = journal(tmp_path, [entry()])
    with path.open("a") as fh:
        fh.write('{"contract_id":')
    book = built(path)
    assert book[0].notional == 2450.0
    assert book[0].entry_reflection is None


def test_captured_basis_is_immutable_when_journal_changes(tmp_path):
    path = journal(tmp_path, [entry()])
    basis = capture_journal_basis(path)
    journal(tmp_path, [entry(debit=999, entry_fill_debit=999)])
    book = build_debit_risk_book(raw_book(), basis, observed_at_monotonic=time.monotonic())
    assert book[0].notional == 364
    assert book[0].entry_reflection["capital_usd"] == 364
    with pytest.raises(TypeError):
        basis.single_lot_entries[101]["spread"]["short_con_id"] = 555


def test_journal_after_position_snapshot_cannot_retrofit_proof(tmp_path):
    observed = time.monotonic()
    basis = capture_journal_basis(journal(tmp_path, [entry()]))
    assert basis.captured_at_monotonic > observed
    book = build_debit_risk_book(raw_book(), basis, observed_at_monotonic=observed)
    assert book[0].notional == 364
    assert book[0].entry_reflection is None


def test_missing_snapshot_provenance_is_conservative(tmp_path):
    basis = capture_journal_basis(journal(tmp_path, [entry()]))
    book = build_debit_risk_book(raw_book(), basis)
    assert book[0].notional == 364 and book[0].entry_reflection is None


def test_unknown_raw_snapshot_is_not_empty_book(tmp_path):
    basis = capture_journal_basis(journal(tmp_path, [entry()]))
    with pytest.raises((ValueError, TypeError)):
        build_debit_risk_book(None, basis, observed_at_monotonic=time.monotonic())


def test_more_broker_contracts_than_journal_uses_gross_not_partial_net_basis(tmp_path):
    book = built(journal(tmp_path, [entry()]), raw_book(long_qty=14, short_qty=-14))
    assert book[0].notional == 4900.0
    assert book[0].entry_reflection is None


@pytest.mark.parametrize("changes", [{"expiry": "20261218"}, {"symbol": "OTHER"},
                                    {"right": "P"}, {"strike": 43.0}])
def test_recycled_contract_identity_cannot_borrow_old_net_basis(tmp_path, changes):
    book = built(journal(tmp_path, [entry()]), raw_book(**changes))
    assert book[0].notional == 2450.0
    assert book[0].entry_reflection is None


def test_short_mapping_key_must_match_actual_contract_id(tmp_path):
    raw = raw_book()
    raw[102].con_id = 999
    with pytest.raises((ValueError, TypeError)):
        built(journal(tmp_path, [entry()]), raw)


@pytest.mark.parametrize("short_qty", [None, -6, 0, 7])
def test_missing_or_insufficient_short_cannot_understate_long_exposure(tmp_path, short_qty):
    raw = raw_book()
    if short_qty is None:
        raw.pop(102)
    else:
        raw[102].quantity = short_qty
    book = built(journal(tmp_path, [entry()]), raw)
    assert book[0].notional == 2450.0
    assert book[0].entry_reflection is None


def test_two_long_campaigns_cannot_spend_same_short_coverage_twice(tmp_path):
    second = entry(contract_id=103, strike=43.0, debit=500.0, entry_fill_debit=500.0,
                   order_ref="alfred-entry:second-long")
    raw = raw_book()
    raw[103] = PositionData(con_id=103, symbol="SYMG", right="C", quantity=7,
        avg_cost=4.0, expiry="20261023", sec_type="OPT", strike=43.0)
    book = built(journal(tmp_path, [entry(), second]), raw)

    assert sum(p.notional for p in book) == 2450.0 + 2800.0
    assert all(p.entry_reflection is None for p in book)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["continuous", "daily", "manual"])
@pytest.mark.parametrize("fill_during_snapshot", [False, True])
async def test_real_route_captures_journal_before_one_signed_position_snapshot(
        tmp_path, monkeypatch, route, fill_during_snapshot):



    path = journal(tmp_path, [] if fill_during_snapshot else [entry()])
    calls = []
    async def positions(**kw):
        calls.append(kw)
        assert kw == {"include_short": True}
        if fill_during_snapshot:
            journal(tmp_path, [entry()])
        await asyncio.sleep(0)
        return raw_book()
    conn = SimpleNamespace(get_positions=positions, ib=SimpleNamespace())
    if route == "continuous":
        from exitmgr.trader import Trader
        self = SimpleNamespace(ib_conn=conn, journal_path=str(path), audit_path=str(tmp_path / "audit"),
            _journal_short_context=lambda: ({102}, {}))
        read = Trader._open_positions(self, open_trades=(), strict=True)
    elif route == "daily":
        import daily_recommend as daily
        monkeypatch.setattr(daily, "JOURNAL_PATH", str(path), raising=False)
        read = daily._admission_positions_fn(conn)(())
    else:
        import place_trade
        read = place_trade._open_positions_for_risk(conn, str(path))

    if fill_during_snapshot:


        with pytest.raises(ValueError, match="uncovered short call.*UNKNOWN admission risk"):
            await read
        assert calls == [{"include_short": True}]
        return

    result = await read
    if route == "manual":
        raw, book = result


        assert set(raw) == {101}
    else:
        book = result
    assert calls == [{"include_short": True}]
    assert len(book) == 1
    long = next(position for position in book if position.primary_con_id == 101)
    assert long.notional == 364.0
    assert long.entry_reflection["order_ref"] == "alfred-entry:fixture-filled"


def dimensions(book, *, observation_start, symbol="SYML", sector_map=None, **overrides):
    from exitmgr.trader import AdmissionObservation, admission_dimensions
    from exitmgr.runtime_identity import RuntimeIdentity
    obs = AdmissionObservation(observation_start,
        SimpleNamespace(net_liq=4292.27, available_funds=5000.0), tuple(book),
        SimpleNamespace(total=0.0, visible_order_refs=frozenset()),
        SimpleNamespace(entry_order_refs=frozenset()))
    order = SimpleNamespace(underlying=symbol, side="debit", structure="long call",
        qty=1, limit=3.10, contract=SimpleNamespace(conId=201), short_contract=None,
        entry_bid=3.10, entry_ask=3.10, decision_id="fixture-candidate")
    result = admission_dimensions(order, obs, "alfred-entry:candidate",
        limits=RiskLimits(max_concurrent=20, sector_map=sector_map or {"SYMG": "CRYPTO", "COIN": "CRYPTO",
                         "SYML": "SEMIS", "SYMO": "MINERS"}, pot_cap_usd=100000),
        construction_cfg=SimpleNamespace(max_deployed_pct=0.60), markers_clear=True,
        day_orders=0, day_notional=0, max_orders_per_day=None, max_notional_per_day=None,
        runtime_identity=RuntimeIdentity("a" * 40, "b" * 64),
        campaign_conflict_symbols=())
    result.update(overrides)
    legs = tuple(sorted(set(result["leg_con_ids"])))
    final = dict(result["final_contract"])
    final.update(underlying=result["symbol"], long_con_id=result["con_id"],
                 short_con_id=next((x for x in legs if x != result["con_id"]), None),
                 quantity=result["contracts"], structure=result["structure"], side=result["side"],
                 limit=result["capital_usd"] / (100 * result["contracts"]),
                 max_loss_usd=result["capital_usd"])
    result["final_contract"] = final
    result["structured_intent"] = dict(result["structured_intent"],
        underlying=result["symbol"], side=result["side"], structure=result["structure"],
        final_contract=final, campaign_add_intent=result["campaign_add_intent"])
    result["intent"] = {"journal_template": dict(contract_id=result["con_id"],
        symbol=result["symbol"], right="C", expiry="20261023", strike=50.0)}
    if len(result["leg_con_ids"]) == 2:
        result["intent"]["journal_template"]["strike"] = 44.0
        result["intent"]["journal_template"]["spread"] = dict(short_con_id=102, short_strike=45.0, width=1.0)
    return result


@pytest.mark.parametrize("candidate,expected_name,expected_sector", [
    ("SYMG", 364.0, 364.0), ("COIN", 0.0, 364.0), ("SYML", 0.0, 0.0),
])
@pytest.mark.parametrize("lowercase_sector_config", [False, True])
def test_admission_dimensions_tie_name_and_sector_to_this_candidate_book(
        tmp_path, candidate, expected_name, expected_sector, lowercase_sector_config):
    start = time.monotonic()
    book = built(journal(tmp_path, [entry()]))
    sector_map = ({"SYMG": "crypto", "COIN": "crypto", "SYML": "semis", "SYMO": "mining_metals"}
                  if lowercase_sector_config else None)
    dims = dimensions(book, observation_start=start, symbol=candidate, sector_map=sector_map)
    assert dims["deployed_aggregate_usd"] == 364.0
    assert dims["open_position_count"] == 1
    assert dims["name_aggregate_usd"] == expected_name
    assert dims["sector_aggregate_usd"] == expected_sector
    proof, = dims["reflected_debit_positions"]
    assert proof["name_counted_usd"] == expected_name
    assert proof["sector_counted_usd"] == expected_sector
    assert proof["sector_cluster"] == "CRYPTO"
    assert dims["available_funds"] == 5000.0


@pytest.mark.parametrize("change", ["reused_old_book", "changed_notional", "changed_symbol", "credit"])
def test_proof_cannot_be_reused_in_an_unrelated_admission_observation(tmp_path, change):
    start = time.monotonic()
    book = built(journal(tmp_path, [entry()]))
    if change == "reused_old_book":
        start = time.monotonic()
    elif change == "changed_notional":
        book[0].notional += 1
    elif change == "changed_symbol":
        book[0].underlying = "SYML"
    elif change == "credit":
        book[0].is_credit = True
    assert dimensions(book, observation_start=start)["reflected_debit_positions"] == ()


@pytest.mark.parametrize("lowercase_sector_config", [False, True])
def test_real_producer_to_ledger_incident_admits_one_candidate_and_preserves_old_claim(
        tmp_path, lowercase_sector_config):
    from exitmgr.entry_reservation import EntryReservationLedger
    ledger = EntryReservationLedger(ledger_path=tmp_path / "claims.json",
                                    lock_path=tmp_path / "claims.lock")
    initial = dimensions([OpenPosition("OTHER", 1788.0, False)],
        observation_start=time.monotonic(), order_ref="alfred-entry:fixture-filled",
        envelope_id="fixture-filled", symbol="SYMG", sector_cluster="CRYPTO",
        con_id=101, leg_con_ids=(101, 102), contracts=7, capital_usd=392.0,
        collateral_usd=392.0, structure="bull call spread", name_cap_usd=None,
        sector_cap_usd=None)
    assert ledger.reserve_and_place(place=lambda: None, **initial).should_place
    ledger.resolve_intent(initial["order_ref"], outcome="journaled", filled_qty=7)
    start = time.monotonic()
    book = [OpenPosition("OTHER", 1788.0, False)] + built(journal(tmp_path, [entry()]))
    sector_map = ({"SYMG": "crypto", "SYML": "semis", "SYMO": "mining_metals"}
                  if lowercase_sector_config else None)
    dims = dimensions(book, observation_start=start, name_cap_usd=None, sector_cap_usd=None,
                      sector_map=sector_map)
    placed = []
    decision = ledger.reserve_and_place(place=lambda: placed.append("SYML"), **dims)
    assert decision.should_place, decision
    assert decision.reflected_debit_order_refs == ("alfred-entry:fixture-filled",)
    assert decision.reflected_debit_actual_usd == 364.0
    assert decision.reflected_debit_capital_usd == 392.0
    assert "alfred-entry:fixture-filled" in ledger.snapshot()["reservations"]

    second = dict(dims, order_ref="alfred-entry:second", envelope_id="second",
        con_id=301, leg_con_ids=(301,), symbol="SYMO", sector_cluster="MINERS",
        capital_usd=217.0, collateral_usd=217.0,
        intent={"journal_template": dict(contract_id=301, symbol="SYMO", right="C",
                                        expiry="20261023", strike=50.0)})
    second["final_contract"] = dict(
        second["final_contract"], underlying="SYMO", long_con_id=301,
        short_con_id=None, quantity=second["contracts"],
        limit=second["capital_usd"] / (100 * second["contracts"]),
        max_loss_usd=second["capital_usd"])
    second["structured_intent"] = dict(
        second["structured_intent"], underlying="SYMO",
        final_contract=second["final_contract"])
    refused = ledger.reserve_and_place(place=lambda: placed.append("SYMO"), **second)
    assert not refused.should_place and refused.status == "deployed_refused"
    assert placed == ["SYML"]
