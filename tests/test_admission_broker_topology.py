"""Public API contract; production-derived narrative omitted."""
import json
import time
from types import SimpleNamespace

import pytest

from exitmgr.connection import PositionData
from exitmgr.entry_reflection import capture_journal_basis, build_admission_risk_book


def _short(con_id=202, quantity=-3, right="P", strike=100.0):
    return PositionData(con_id=con_id, symbol="SYMQ", right=right, quantity=quantity,
                        avg_cost=2.0, expiry="20270115", sec_type="OPT", strike=strike)


def test_standalone_short_is_a_fresh_admission_campaign(tmp_path):
    journal = tmp_path / "trades.log"
    journal.write_text("")
    raw = {202: _short()}
    book = build_admission_risk_book(
        raw, capture_journal_basis(journal), observed_at_monotonic=time.monotonic())

    assert len(book) == 1
    assert book[0].primary_con_id == 202
    assert book[0].leg_con_ids == (202,)
    assert book[0].contracts == 3
    assert book[0].broker_signed_quantity == -3
    assert book[0].notional == 30_000.0


def test_uncovered_short_call_refuses_unknown_unbounded_risk(tmp_path):
    journal = tmp_path / "trades.log"
    journal.write_text("")

    with pytest.raises(ValueError, match="uncovered short call.*UNKNOWN admission risk"):
        build_admission_risk_book(
            {303: _short(con_id=303, quantity=-1, right="C", strike=100.0)},
            capture_journal_basis(journal), observed_at_monotonic=time.monotonic())


@pytest.mark.asyncio
async def test_continuous_admission_refuses_uncovered_short_call(tmp_path):
    from exitmgr.trader import Trader

    journal = tmp_path / "trades.log"
    journal.write_text("")
    trader = object.__new__(Trader)
    trader.journal_path = str(journal)
    trader.audit_path = str(tmp_path / "audit.jsonl")
    trader.ib_conn = SimpleNamespace(
        get_positions=lambda **kwargs: None,
        ib=SimpleNamespace(reqAllOpenOrdersAsync=lambda: None))

    async def positions(**kwargs):
        assert kwargs == {"include_short": True}
        return {303: _short(con_id=303, quantity=-1, right="C", strike=100.0)}

    trader.ib_conn.get_positions = positions
    with pytest.raises(ValueError, match="uncovered short call.*UNKNOWN admission risk"):
        await trader._positions_for_admission(())


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [None, 0.0, -1.0, float("nan")])
async def test_continuous_admission_refuses_unpriced_working_buy(tmp_path, limit):
    from exitmgr.trader import Trader

    journal = tmp_path / "trades.log"
    journal.write_text("")
    trader = object.__new__(Trader)
    trader.journal_path = str(journal)
    trader.audit_path = str(tmp_path / "audit.jsonl")

    async def positions(**kwargs):
        assert kwargs == {"include_short": True}
        return {}

    trader.ib_conn = SimpleNamespace(get_positions=positions, ib=SimpleNamespace())
    working = SimpleNamespace(
        order=SimpleNamespace(action="BUY", totalQuantity=1, lmtPrice=limit,
                              orderRef="alfred-entry:working"),
        contract=SimpleNamespace(symbol="SYMQ", conId=404, comboLegs=[]),
        orderStatus=SimpleNamespace(status="Submitted"))

    with pytest.raises(ValueError, match="working BUY SYMQ.*verifiable exposure price"):
        await trader._positions_for_admission((working,))


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["daily", "manual"])
async def test_route_refuses_malformed_journal_instead_of_omitting_topology(
        tmp_path, monkeypatch, route):
    journal = tmp_path / "trades.log"
    journal.write_text('{"contract_id":')
    conn = SimpleNamespace(get_positions=lambda **kwargs: None)

    async def positions(**kwargs):
        assert kwargs == {"include_short": True}
        return {202: _short()}

    conn.get_positions = positions
    if route == "daily":
        import daily_recommend
        monkeypatch.setattr(daily_recommend, "JOURNAL_PATH", str(journal))
        call = daily_recommend._admission_positions_fn(conn)
        with pytest.raises(ValueError, match="journal is malformed"):
            await call(())
    else:
        import place_trade
        with pytest.raises(ValueError, match="journal is malformed"):
            await place_trade._open_positions_for_risk(conn, str(journal))


def test_valid_spread_carries_both_fresh_conids_once(tmp_path):
    journal = tmp_path / "trades.log"
    row = {
        "contract_id": 101, "symbol": "SYMQ", "right": "P", "strike": 130.0,
        "expiry": "20270115", "quantity": 2, "quantity_requested": 2,
        "debit": 400.0, "entry_fill_debit": 400.0, "order_status": "Filled",
        "order_terminal": True, "basis_source": "fill", "quantity_source": "order_status",
        "entry_remaining_qty": 0, "order_ref": "alfred-entry:spread",
        "ts": "2026-09-18T12:00:00-07:00",
        "spread": {"short_con_id": 102, "short_strike": 125.0}}
    journal.write_text(json.dumps(row) + "\n")
    raw = {
        101: PositionData(101, "SYMQ", "P", 2, 3.0, "20270115", "OPT", 130.0),
        102: PositionData(102, "SYMQ", "P", -2, 1.0, "20270115", "OPT", 125.0),
    }
    book = build_admission_risk_book(
        raw, capture_journal_basis(journal), observed_at_monotonic=time.monotonic())

    assert len(book) == 1
    assert book[0].primary_con_id == 101
    assert book[0].leg_con_ids == (101, 102)
    assert book[0].contracts == 2


def test_mismatched_short_quantity_refuses_campaign_topology(tmp_path):
    journal = tmp_path / "trades.log"
    journal.write_text(json.dumps({
        "contract_id": 101, "symbol": "SYMQ", "right": "P", "strike": 130.0,
        "expiry": "20270115", "quantity": 2, "debit": 400.0,
        "ts": "2026-09-18T12:00:00-07:00",
        "spread": {"short_con_id": 102, "short_strike": 125.0}}) + "\n")
    raw = {
        101: PositionData(101, "SYMQ", "P", 2, 3.0, "20270115", "OPT", 130.0),
        102: PositionData(102, "SYMQ", "P", -1, 1.0, "20270115", "OPT", 125.0),
    }
    with pytest.raises(ValueError, match="short leg quantity/topology"):
        build_admission_risk_book(
            raw, capture_journal_basis(journal), observed_at_monotonic=time.monotonic())
