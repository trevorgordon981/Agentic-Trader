"""Public API contract; production-derived narrative omitted."""
import json
import os
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from unittest.mock import AsyncMock, MagicMock

import exitmgr.trader as trader
from exitmgr import entry_safety
from exitmgr.account import PotSnapshot
from exitmgr.connection import PositionData
from exitmgr.entry_reservation import EntryReservationLedger, EntryReservationError
from exitmgr.risk import RiskLimits
from exitmgr.trader import (
    ResolvedOrder, Trader, broker_entry_order_view,
)


REPO = Path(__file__).resolve().parents[1]
CODE_VERSION = "a" * 40
POLICY_VERSION = "b" * 64
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"




def _dte_str(days: int) -> str:
    return (datetime.now(timezone.utc).date() + timedelta(days=days)).strftime("%Y%m%d")


def _contract(con_id=111, strike=610.0, right="C", symbol="SPY", sec_type="OPT"):
    c = MagicMock()
    c.conId = con_id
    c.strike = strike
    c.right = right
    c.symbol = symbol
    c.secType = sec_type
    c.lastTradeDateOrContractMonth = _dte_str(30)
    return c


class _CapturedOrder:
    """Public API contract; production-derived narrative omitted."""

    instances = []

    def __init__(self, **kw):
        self.__dict__.update(kw)
        self.orderRef = None
        self.orderId = 0
        _CapturedOrder.instances.append(self)


def _pin_ibkr(monkeypatch):
    import importlib
    mod = importlib.import_module("exitmgr.ibkr")
    monkeypatch.setitem(sys.modules, "exitmgr.ibkr", mod)
    return mod


def _long_position(con_id, symbol, debit_usd):
    return PositionData(con_id=con_id, symbol=symbol, right="C", quantity=1,
                        avg_cost=debit_usd / 100.0, expiry=_dte_str(45),
                        sec_type="OPT", strike=100.0)


def _debit_order(*, con_id=111, symbol="SPY", qty=1, limit=1.20,
                 decision_id="decision-" + "d" * 32):
    return ResolvedOrder(
        symbol, "C", _dte_str(30), 610.0, qty, limit,
        _contract(con_id, 610.0, "C", symbol),
        entry_bid=limit - 0.05, entry_ask=limit + 0.05,
        quote_observed_at=time.monotonic(), decision_id=decision_id, dte=30)


def _ledger(tmp_path, **kw):
    return EntryReservationLedger(ledger_path=tmp_path / "reservations.json",
                                  lock_path=tmp_path / "reservations.lock", **kw)


def _empty_campaign_quarantine(tmp_path, *journal_names):
    for name in journal_names:
        (tmp_path / name).write_text("")
    (tmp_path / ".campaign-journal-conflicts.json").write_text(json.dumps({
        "schema": "campaign_conflicts.v2", "journal_sha256": EMPTY_SHA256,
        "conditions": {},
    }))


def _trader(tmp_path, monkeypatch, *, net_liq=100_000.0, available=60_000.0,
            positions=None, max_concurrent=3, ledger=None):
    """Public API contract; production-derived narrative omitted."""
    monkeypatch.setenv("EXITMGR_ORDER_LOCK", str(tmp_path / "order.lock"))
    _empty_campaign_quarantine(tmp_path, "trades.log")
    _CapturedOrder.instances = []
    monkeypatch.setattr(_pin_ibkr(monkeypatch), "Order", _CapturedOrder)
    monkeypatch.setattr(trader, "get_pot_snapshot",
                        AsyncMock(return_value=PotSnapshot(net_liq, available, available)))

    placed = MagicMock()
    placed.orderStatus.status = "Filled"
    placed.orderStatus.avgFillPrice = 1.20
    placed.log = []
    placed.fills = []
    placed.order.orderRef = "alfred-entry:x"
    placed.order.orderId = 42

    book = dict(positions or {})
    ibc = MagicMock()
    ibc.ib = MagicMock()
    ibc.ib.placeOrder.return_value = placed
    ibc.ib.reqPositionsAsync = AsyncMock(return_value=[])
    ibc.ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[])
    ibc.get_positions = AsyncMock(return_value=book)

    t = Trader(ib_conn=ibc, exit_manager=MagicMock(),
               limits=RiskLimits(max_concurrent=max_concurrent, allow_any_name=True),
               approved_names={"SPY", "MUTX"}, endpoint="http://x", model="m",
               slack_token="tok", slack_channel="C1", approver_ids={"OWNER"},
               baseline_path=str(tmp_path / "b.json"), audit_path=str(tmp_path / "a.jsonl"),
               journal_path=str(tmp_path / "trades.log"),
               config_path=str(tmp_path / "config.yaml"),
               trading_down_path=str(tmp_path / "TRADING_DOWN"),
               kill_switch_path=str(tmp_path / "KILL_SWITCH"),
               entry_reservation_ledger=ledger or _ledger(tmp_path))
    return t


def _audit_events(tmp_path):
    p = tmp_path / "a.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]












_CHILD_SRC = r'''
import asyncio, json, os, sys, time, types
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

TAG = sys.argv[1]
REPO = os.environ["ADMISSION_REPO"]
PLACEMENTS = os.environ["ADMISSION_PLACEMENTS"]
RESULT = os.environ["ADMISSION_RESULT"] + "." + TAG
DISABLE_SLOT = os.environ.get("ADMISSION_DISABLE_SLOT") == "1"

class _Order:
    def __init__(self, **kw):
        self.__dict__.update(kw)
        self.orderRef = None
        self.orderId = 0

_stub = types.ModuleType("ib_async")
for _n in ("IB", "ComboLeg", "Contract", "Index", "Option", "Position", "Stock", "Ticker"):
    setattr(_stub, _n, MagicMock())
_stub.Order = _Order
sys.modules["ib_async"] = _stub

sys.path.insert(0, REPO)
os.chdir(REPO)

import exitmgr.trader as trader
from exitmgr import entry_safety
from exitmgr.account import PotSnapshot
from exitmgr.connection import PositionData
from exitmgr.entry_reservation import EntryReservationLedger
from exitmgr.risk import RiskLimits
from exitmgr.trader import ResolvedOrder, Trader

def _dte(days):
    return (datetime.now(timezone.utc).date() + timedelta(days=days)).strftime("%Y%m%d")

def _contract(con_id, symbol):
    c = MagicMock()
    c.conId = con_id; c.strike = 610.0; c.right = "C"; c.symbol = symbol; c.secType = "OPT"
    c.lastTradeDateOrContractMonth = _dte(30)
    return c

def _place(contract, order):
    # ONE line per real transmission, appended O_APPEND so two processes cannot lose a write.
    with open(PLACEMENTS, "a") as fh:
        fh.write(json.dumps({"tag": TAG, "order_ref": getattr(order, "orderRef", None)}) + "\n")
        fh.flush()
    tr = MagicMock()
    tr.orderStatus.status = "Filled"
    tr.orderStatus.avgFillPrice = 1.20
    tr.log = []
    tr.fills = []
    tr.order = order
    return tr

tmp = os.environ["ADMISSION_TMP"]
book = {901: PositionData(901, "AAPL", "C", 1, 3.0, _dte(45), "OPT", 100.0),
        902: PositionData(902, "TSLA", "C", 1, 3.0, _dte(45), "OPT", 100.0)}

trader.get_pot_snapshot = AsyncMock(return_value=PotSnapshot(100000.0, 60000.0, 60000.0))
import exitmgr.ibkr as _ibkr
_ibkr.Order = _Order

ibc = MagicMock()
ibc.ib = MagicMock()
ibc.ib.placeOrder = _place
ibc.ib.reqPositionsAsync = AsyncMock(return_value=[])
ibc.ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[])
ibc.get_positions = AsyncMock(return_value=book)

if DISABLE_SLOT:
    # NEGATIVE CONTROL: the slot predicate is the only thing removed.
    import exitmgr.entry_reservation as er
    er.SLOT_PREDICATE_ENABLED = False

t = Trader(ib_conn=ibc, exit_manager=MagicMock(),
           limits=RiskLimits(max_concurrent=3, allow_any_name=True),
           approved_names={"SPY"}, endpoint="http://x", model="m", slack_token="",
           slack_channel="", approver_ids=set(),
           baseline_path=os.path.join(tmp, "b.json"),
           audit_path=os.path.join(tmp, "audit-%s.jsonl" % TAG),
           journal_path=os.path.join(tmp, "trades-%s.log" % TAG),
           config_path=os.path.join(tmp, "config.yaml"),
           trading_down_path=os.path.join(tmp, "TRADING_DOWN"),
           kill_switch_path=os.path.join(tmp, "KILL_SWITCH"),
           entry_reservation_ledger=EntryReservationLedger(
               ledger_path=os.path.join(tmp, "reservations.json"),
               lock_path=os.path.join(tmp, "reservations.lock"),
               lock_timeout_seconds=10.0))

r = ResolvedOrder("SPY", "C", _dte(30), 610.0, 1, 1.20, _contract(111 + int(TAG[-1]), "SPY"),
                  entry_bid=1.15, entry_ask=1.25, quote_observed_at=time.monotonic(),
                  decision_id="decision-" + TAG[-1] * 32, dte=30)

sys.stdout.write("ready\n"); sys.stdout.flush()
assert sys.stdin.readline().strip() == "go"

out = {"tag": TAG}
try:
    status, reasons = asyncio.run(t._submit_order_unlocked(
        r,
        submission_authority=entry_safety.EntrySubmissionAuthority.autonomous()))
    out["status"] = status
    out["reasons"] = [str(x) for x in reasons]
except BaseException as exc:          # a refusal raises on this path; record it, never swallow
    out["error"] = "%s: %s" % (type(exc).__name__, exc)
with open(RESULT, "w") as fh:
    json.dump(out, fh)
sys.stdout.write("done\n"); sys.stdout.flush()
'''


def _run_barrier(tmp_path, *, disable_slot=False):
    """Public API contract; production-derived narrative omitted."""
    placements = tmp_path / "placements.jsonl"
    placements.write_text("")
    _empty_campaign_quarantine(tmp_path, "trades-child1.log", "trades-child2.log")
    env = dict(os.environ)
    env.update({
        "ADMISSION_REPO": str(REPO),
        "ADMISSION_TMP": str(tmp_path),
        "ADMISSION_PLACEMENTS": str(placements),
        "ADMISSION_RESULT": str(tmp_path / "result"),
        "EXITMGR_ORDER_LOCK": str(tmp_path / "order.lock"),
        "EXITMGR_ENTRY_RESERVATIONS": str(tmp_path / "reservations.json"),
        "EXITMGR_ENTRY_RESERVATION_LOCK": str(tmp_path / "reservations.lock"),
        "PYTHONPATH": str(REPO),
    })
    if disable_slot:
        env["ADMISSION_DISABLE_SLOT"] = "1"
    kids = []
    try:
        for tag in ("child1", "child2"):
            kids.append(subprocess.Popen(
                [sys.executable, "-c", _CHILD_SRC, tag], env=env, cwd=str(REPO),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True))
        for kid in kids:
            line = kid.stdout.readline().strip()
            if line != "ready":
                raise AssertionError("child never became ready: %r / %s"
                                     % (line, kid.stderr.read()))
        for kid in kids:
            kid.stdin.write("go\n")
            kid.stdin.flush()
        for kid in kids:
            try:
                kid.wait(timeout=120)
            except subprocess.TimeoutExpired:
                kid.kill()
                raise
    finally:
        for kid in kids:
            if kid.poll() is None:
                kid.kill()
            kid.wait(timeout=10)
    lines = [json.loads(l) for l in placements.read_text().splitlines() if l.strip()]
    results = []
    for tag in ("child1", "child2"):
        p = tmp_path / ("result." + tag)
        results.append(json.loads(p.read_text()) if p.exists() else {"tag": tag, "missing": True})
    return lines, results


def test_two_contenders_for_one_slot_invoke_placeOrder_at_most_once(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    placements, results = _run_barrier(tmp_path)
    assert len(placements) <= 1, (
        "two contenders for one slot both transmitted: %s (results=%s)" % (placements, results))
    assert len(placements) == 1, "one of the two SHOULD have been admitted: %s" % (results,)
    losers = [r for r in results if "error" in r]
    assert len(losers) == 1, results
    assert "slot_refused" in losers[0]["error"], losers[0]


def test_NEGATIVE_CONTROL_without_the_slot_predicate_both_children_place(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    placements, results = _run_barrier(tmp_path, disable_slot=True)
    assert len(placements) == 2, (
        "the negative control did not reproduce the race, so the test above proves nothing: "
        "%s / %s" % (placements, results))




@pytest.mark.asyncio
async def test_a_kill_switch_flipped_after_admission_but_before_transmit_blocks_the_order(
        tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    t = _trader(tmp_path, monkeypatch)
    real_markers = t._entry_markers_clear
    flipped = {"n": 0}

    def _flip_then_report():
        flipped["n"] += 1
        if flipped["n"] >= 2:
            (tmp_path / "KILL_SWITCH").write_text("halt")
        return real_markers()

    monkeypatch.setattr(t, "_entry_markers_clear", _flip_then_report)
    with pytest.raises(RuntimeError, match="marker"):
        await t._submit_order_unlocked(
            _debit_order(),
            submission_authority=entry_safety.EntrySubmissionAuthority.autonomous())
    t.ib_conn.ib.placeOrder.assert_not_called()


@pytest.mark.asyncio
async def test_a_kill_switch_flipped_INSIDE_the_boundary_still_blocks_the_transmit(
        tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    t = _trader(tmp_path, monkeypatch)
    real_markers = t._entry_markers_clear
    calls = {"n": 0}

    def _flip_at_the_boundary():
        calls["n"] += 1
        if calls["n"] >= 3:
            (tmp_path / "KILL_SWITCH").write_text("halt")
        return real_markers()

    monkeypatch.setattr(t, "_entry_markers_clear", _flip_at_the_boundary)
    with pytest.raises(RuntimeError, match="markers_set"):
        await t._submit_order_unlocked(
            _debit_order(),
            submission_authority=entry_safety.EntrySubmissionAuthority.autonomous())
    t.ib_conn.ib.placeOrder.assert_not_called()
    assert calls["n"] >= 3, "the in-boundary recheck never ran"


@pytest.mark.asyncio
async def test_broker_entry_order_view_is_UNREADABLE_when_the_read_raises():
    """Public API contract; production-derived narrative omitted."""
    ib = MagicMock()
    ib.reqAllOpenOrdersAsync = AsyncMock(side_effect=TimeoutError("gateway down"))
    view = await broker_entry_order_view(ib)
    assert view.readable is False
    assert view.trades == ()


@pytest.mark.asyncio
async def test_NEGATIVE_CONTROL_a_readable_empty_order_book_is_KNOWN_empty():
    ib = MagicMock()
    ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[])
    view = await broker_entry_order_view(ib)
    assert view.readable is True
    assert view.trades == () and view.entry_order_refs == frozenset()


@pytest.mark.asyncio
async def test_NEGATIVE_CONTROL_a_clear_kill_switch_still_places(tmp_path, monkeypatch):
    t = _trader(tmp_path, monkeypatch)
    status, _ = await t._submit_order_unlocked(
        _debit_order(),
        submission_authority=entry_safety.EntrySubmissionAuthority.autonomous())
    assert status == "Filled"
    t.ib_conn.ib.placeOrder.assert_called_once()
    transmitted = t.ib_conn.ib.placeOrder.call_args.args[1]
    admission = next(e for e in _audit_events(tmp_path) if e["event"] == "entry_admission")
    assert transmitted.lmtPrice == 1.25
    assert admission["admission_receipt"]["final_contract"]["limit"] == 1.25
    assert admission["admission_receipt"]["candidate"]["capital_usd"] == 125.0




def test_a_stale_account_observation_is_refused_not_trusted(tmp_path):
    led = _ledger(tmp_path)
    decision = led.reserve_and_place(
        place=lambda: pytest.fail("placed on a stale observation"),
        **_admission_kwargs(observed_at_monotonic=time.monotonic() - 600.0))
    assert decision.status == "stale_observation"
    assert not decision.allowed and not decision.should_place


def test_NEGATIVE_CONTROL_a_fresh_observation_is_admitted(tmp_path):
    led = _ledger(tmp_path)
    calls = []
    decision = led.reserve_and_place(place=lambda: calls.append(1) or "trade",
                                     **_admission_kwargs())
    assert decision.status == "reserved" and decision.should_place
    assert calls == [1]


def _admission_kwargs(**over):
    """Public API contract; production-derived narrative omitted."""
    kw = dict(
        order_ref="alfred-entry:" + "a" * 32,
        envelope_id="env-1",
        code_version=CODE_VERSION,
        policy_version=POLICY_VERSION,
        con_id=111,
        leg_con_ids=(111,),
        symbol="SPY",
        sector_cluster="INDEX",
        structure="long call",
        side="debit",
        capital_usd=1200.0,
        collateral_usd=1200.0,
        contracts=1,
        net_liq=100_000.0,
        available_funds=60_000.0,
        broker_deployed_usd=0.0,
        observation_readable=True,
        observed_at_monotonic=time.monotonic(),
        max_observation_age_s=60.0,
        open_position_count=0,
        max_concurrent=8,
        name_aggregate_usd=0.0,
        name_cap_usd=36_000.0,
        sector_aggregate_usd=0.0,
        sector_cap_usd=25_000.0,
        deployed_aggregate_usd=0.0,
        deployed_cap_usd=40_000.0,
        day_orders=0,
        max_orders_per_day=None,
        day_notional=0.0,
        max_notional_per_day=None,
        open_campaign_con_ids=(),
        open_campaigns=(),
        campaign_conflict_symbols=(),
        campaign_add_intent=None,
        markers_clear=True,
        visible_order_refs=(),
    )
    kw.update(over)
    legs = tuple(sorted(set(kw["leg_con_ids"])))
    final = dict(underlying=kw["symbol"], right="C", expiry="20261218",
                 long_con_id=kw["con_id"], long_strike=600.0,
                 short_con_id=(next((x for x in legs if x != kw["con_id"]), None)),
                 short_strike=(610.0 if len(legs) == 2 else None), quantity=kw["contracts"],
                 side=kw["side"], structure=kw["structure"],
                 limit=round((1.0 if kw["side"] == "credit" else kw["capital_usd"])
                             / (100 * kw["contracts"]), 4),
                 max_loss_usd=(kw["capital_usd"] - 1
                               if kw["side"] == "credit" else kw["capital_usd"]))
    if kw["side"] == "credit":
        final.update(collateral_usd=kw["capital_usd"], net_credit_usd=1.0)
    kw.setdefault("final_contract", final)
    kw.setdefault("structured_intent", dict(
        schema="structured_entry_intent.v1", underlying=kw["symbol"], side=kw["side"],
        structure=kw["structure"], campaign_add_intent=kw["campaign_add_intent"],
        stage_a_intent=None, stage_b_candidate=None, final_contract=kw["final_contract"]))
    if "intent" not in over:
        kw["intent"] = {
            "order_ref": kw["order_ref"], "con_id": kw["con_id"],
            "symbol": kw["symbol"], "side": kw["side"],
            "requested_qty": kw["contracts"], "journal_template": {
                "contract_id": kw["con_id"], "symbol": kw["symbol"],
                "right": "C", "expiry": "20261218", "strike": 600.0},
        }
        legs = tuple(dict.fromkeys(kw["leg_con_ids"]))
        if len(legs) == 2 and kw["con_id"] in legs:
            short_con_id = next(leg for leg in legs if leg != kw["con_id"])
            kw["intent"]["journal_template"]["spread"] = {
                "short_con_id": short_con_id, "short_strike": 610.0, "width": 10.0}
    return kw


def _open_campaign_add(**over):
    add = dict(schema="campaign_add.v1", action="scale_in", intent_id="add-test-1",
               campaign_id="contract:111:campaign:1:first-line:1",
               primary_con_id=111, leg_con_ids=[111], max_campaign_contracts=3,
               max_campaign_risk_usd=2000.0, reason="thesis strengthened within risk bounds")
    add.update(over)
    return add


def test_live_exact_contract_is_blocked_without_explicit_add_intent(tmp_path):
    dims = _admission_kwargs(
        open_campaign_con_ids=(111,), open_position_count=1,
        open_campaigns=({"campaign_id": "contract:111:campaign:1:first-line:1",
                         "primary_con_id": 111, "leg_con_ids": [111], "symbol": "SPY",
                         "contracts": 1, "risk_usd": 700.0, "risk_known": True},))
    decision = _ledger(tmp_path).reserve_and_place(
        place=lambda: pytest.fail("overlap placed without add authority"), **dims)
    assert decision.status == "campaign_overlap_refused"


def test_explicit_bounded_add_is_durable_and_receipted(tmp_path):
    add = _open_campaign_add()
    dims = _admission_kwargs(
        capital_usd=500.0, collateral_usd=500.0, open_position_count=1,
        name_aggregate_usd=700.0, sector_aggregate_usd=700.0,
        deployed_aggregate_usd=700.0, open_campaign_con_ids=(111,),
        open_campaigns=({"campaign_id": add["campaign_id"], "primary_con_id": 111,
                         "leg_con_ids": [111], "symbol": "SPY", "contracts": 1,
                         "risk_usd": 700.0, "risk_known": True},),
        campaign_add_intent=add)
    led = _ledger(tmp_path)
    placed = []
    decision = led.reserve_and_place(place=lambda: placed.append(True), **dims)
    assert decision.should_place and placed == [True]
    assert decision.admission_receipt["name"] == {
        "symbol": "SPY", "broker_usd": 700.0, "pending_usd": 0,
        "this_usd": 500.0, "post_usd": 1200.0, "cap_usd": 36000.0,
        "headroom_usd": 34800.0}
    state = led.snapshot()
    assert state["intents"][dims["order_ref"]]["campaign_add_intent"] == add
    assert state["intents"][dims["order_ref"]]["admission_receipt"]["net_liq_usd"] == 100000.0


def test_campaign_add_bound_and_final_contract_binding_fail_closed(tmp_path):
    campaign = ({"campaign_id": "contract:111:campaign:1:first-line:1",
                 "primary_con_id": 111, "leg_con_ids": [111], "symbol": "SPY",
                 "contracts": 1, "risk_usd": 700.0, "risk_known": True},)
    bounded = _admission_kwargs(
        open_campaign_con_ids=(111,), open_campaigns=campaign, open_position_count=1,
        campaign_add_intent=_open_campaign_add(max_campaign_contracts=1))
    assert _ledger(tmp_path).reserve_and_place(place=lambda: None, **bounded).status == (
        "campaign_add_bound_refused")
    bad = _admission_kwargs(final_contract={
        "underlying": "SPY", "right": "C", "expiry": "20261218",
        "long_con_id": 999, "long_strike": 600.0, "short_con_id": None,
        "short_strike": None, "quantity": 1, "side": "debit", "structure": "long call",
        "limit": 12.0, "max_loss_usd": 1200.0})
    refused = _ledger(tmp_path / "bad").reserve_and_place(place=lambda: None, **bad)
    assert refused.status == "ledger_error" and "final_contract" in refused.reasons[0]


def test_final_contract_dollars_cannot_exceed_admission_cap_arithmetic(tmp_path):
    dims = _admission_kwargs(
        capital_usd=100.0, collateral_usd=100.0, deployed_cap_usd=150.0)
    dims["final_contract"] = dict(
        dims["final_contract"], limit=10.0, max_loss_usd=1000.0)
    dims["structured_intent"] = dict(
        dims["structured_intent"], final_contract=dims["final_contract"])
    decision = _ledger(tmp_path).reserve_and_place(
        place=lambda: pytest.fail("placed a $1,000 order accounted as $100"), **dims)
    assert not decision.allowed and not decision.should_place
    assert decision.status == "ledger_error"
    assert "economics" in decision.reasons[0]


def test_same_symbol_campaign_conflict_blocks_different_contract(tmp_path):
    dims = _admission_kwargs(
        con_id=222, leg_con_ids=(222,), symbol="SYMQ",
        campaign_conflict_symbols=("SYMQ",))
    decision = _ledger(tmp_path).reserve_and_place(
        place=lambda: pytest.fail("quarantined symbol reached placeOrder"), **dims)
    assert not decision.allowed and not decision.should_place
    assert decision.status == "campaign_conflict_refused"


def test_campaign_conflict_dimension_is_required_at_money_boundary(tmp_path):
    dims = _admission_kwargs()
    dims.pop("campaign_conflict_symbols")
    decision = _ledger(tmp_path).reserve_and_place(
        place=lambda: pytest.fail("missing quarantine view failed open"), **dims)
    assert decision.status == "ledger_error"
    assert "campaign_conflict_symbols" in decision.reasons[0]




def test_a_debit_reserves_against_the_same_ledger_the_credit_path_uses(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    a, b = _ledger(tmp_path), _ledger(tmp_path)
    first = a.reserve(order_ref="alfred-entry:credit", con_id=901, collateral_usd=5_000.0,
                      net_liq=100_000.0, available_funds=6_000.0, broker_deployed_usd=0.0)
    assert first.should_place
    second = b.reserve_and_place(
        place=lambda: pytest.fail("placed against capital the credit path already reserved"),
        **_admission_kwargs(order_ref="alfred-entry:debit", capital_usd=2_000.0,
                            collateral_usd=2_000.0, available_funds=6_000.0))
    assert second.status == "capacity_refused"
    assert second.outstanding_unreflected_usd == 5_000.0


def test_debit_is_not_charged_against_the_credit_collateral_cap(tmp_path):
    led = _ledger(tmp_path)
    decision = led.reserve_and_place(
        place=lambda: "trade",
        **_admission_kwargs(
            capital_usd=470.0,
            collateral_usd=470.0,
            net_liq=4_772.73,
            available_funds=3_755.97,
            broker_deployed_usd=4_500.0,
            deployed_aggregate_usd=985.0,
            deployed_cap_usd=2_863.64,
        ))
    assert decision.status == "reserved"
    assert decision.allowed and decision.should_place


def test_credit_still_obeys_the_80pct_collateral_cap(tmp_path):
    led = _ledger(tmp_path)
    kwargs = _admission_kwargs(
        side="credit",
        structure="cash-secured put",
        capital_usd=470.0,
        collateral_usd=470.0,
        net_liq=4_772.73,
        available_funds=3_755.97,
        broker_deployed_usd=3_500.0,
    )
    kwargs["intent"]["estimated_debit"] = 420.0
    kwargs["intent"]["journal_template"].update({
        "side": "credit", "action": "SELL", "collateral_usd": 470.0,
        "max_loss_usd": 420.0, "debit": 420.0, "net_credit_usd": 50.0,
    })
    kwargs["final_contract"].update(
        limit=0.50, collateral_usd=470.0, max_loss_usd=420.0,
        net_credit_usd=50.0)
    kwargs["structured_intent"]["final_contract"] = kwargs["final_contract"]
    decision = led.reserve_and_place(
        place=lambda: pytest.fail("credit escaped its collateral cap"),
        **kwargs)
    assert decision.status == "capacity_refused"
    assert "collateral cap" in decision.reasons[-1]




@pytest.mark.parametrize("over,expected", [
    (dict(open_position_count=8, max_concurrent=8), "slot_refused"),
    (dict(name_aggregate_usd=35_500.0, name_cap_usd=36_000.0, capital_usd=1_200.0),
     "name_refused"),
    (dict(sector_aggregate_usd=24_500.0, sector_cap_usd=25_000.0, capital_usd=1_200.0),
     "sector_refused"),
    (dict(deployed_aggregate_usd=39_500.0, deployed_cap_usd=40_000.0, capital_usd=1_200.0),
     "deployed_refused"),
    (dict(day_orders=3, max_orders_per_day=3), "throttle_refused"),
    (dict(day_notional=9_500.0, max_notional_per_day=10_000.0, capital_usd=1_200.0),
     "throttle_refused"),
    (dict(open_campaign_con_ids=(111,)), "campaign_overlap_refused"),
    (dict(markers_clear=False), "markers_set"),
    (dict(observation_readable=False), "observation_unreadable"),
])
def test_each_admission_dimension_refuses_and_places_nothing(tmp_path, over, expected):
    led = _ledger(tmp_path)
    decision = led.reserve_and_place(
        place=lambda: pytest.fail("placed through the %s predicate" % expected),
        **_admission_kwargs(**over))
    assert decision.status == expected, decision
    assert not decision.allowed and not decision.should_place
    assert led.snapshot()["reservations"] == {}


def test_a_reservation_consumes_a_slot_for_the_next_contender(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    led = _ledger(tmp_path)
    first = led.reserve_and_place(place=lambda: "t1", **_admission_kwargs(
        order_ref="alfred-entry:one", con_id=111, leg_con_ids=(111,),
        open_position_count=7, max_concurrent=8))
    assert first.status == "reserved"
    second = led.reserve_and_place(
        place=lambda: pytest.fail("second contender took a slot the first already reserved"),
        **_admission_kwargs(order_ref="alfred-entry:two", con_id=222, leg_con_ids=(222,),
                            open_position_count=7, max_concurrent=8))
    assert second.status == "slot_refused"


def test_an_overlapping_leg_conid_is_refused_while_another_reservation_holds_it(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    led = _ledger(tmp_path)
    first = led.reserve_and_place(place=lambda: "t1", **_admission_kwargs(
        order_ref="alfred-entry:one", con_id=111, leg_con_ids=(111, 222)))
    assert first.status == "reserved"
    second = led.reserve_and_place(
        place=lambda: pytest.fail("scaled into a contract another admission is already buying"),
        **_admission_kwargs(order_ref="alfred-entry:two", con_id=222, leg_con_ids=(222, 333)))
    assert second.status == "campaign_overlap_refused"


def test_reserve_and_place_refuses_a_MISSING_dimension_rather_than_defaulting_it(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    led = _ledger(tmp_path)
    kw = _admission_kwargs()
    kw.pop("max_concurrent")
    decision = led.reserve_and_place(
        place=lambda: pytest.fail("placed with the slot dimension unspecified"), **kw)
    assert decision.status == "ledger_error"
    assert any("max_concurrent" in r for r in decision.reasons), decision.reasons


@pytest.mark.parametrize("missing", ["code_version", "policy_version"])
def test_missing_runtime_identity_refuses_before_transmission(tmp_path, missing):
    led = _ledger(tmp_path)
    kw = _admission_kwargs()
    kw.pop(missing)
    called = []
    decision = led.reserve_and_place(place=lambda: called.append(True), **kw)
    assert decision.status == "ledger_error" and called == []
    assert led.snapshot()["reservations"] == {}
    assert led.open_intents() == ()


@pytest.mark.parametrize("field,value", [
    ("code_version", "A" * 40),
    ("code_version", "a" * 39),
    ("policy_version", "g" * 64),
    ("policy_version", "b" * 63),
])
def test_malformed_runtime_identity_refuses_before_transmission(tmp_path, field, value):
    led = _ledger(tmp_path)
    called = []
    decision = led.reserve_and_place(
        place=lambda: called.append(True), **_admission_kwargs(**{field: value}))
    assert decision.status == "ledger_error" and called == []
    assert led.snapshot()["reservations"] == {}


def test_reservation_and_intent_are_atomically_stamped_before_place(tmp_path):
    led = _ledger(tmp_path)
    observed = {}

    def place():
        state = json.loads(led.ledger_path.read_text())
        observed.update(state)
        return "trade"

    decision = led.reserve_and_place(place=place, **_admission_kwargs())
    assert decision.should_place
    ref = "alfred-entry:" + "a" * 32
    for collection in ("reservations", "intents"):
        assert observed[collection][ref]["code_version"] == CODE_VERSION
        assert observed[collection][ref]["policy_version"] == POLICY_VERSION
    assert observed["intents"][ref]["transmitted"] == "unknown"
    assert led.open_intents()[0]["transmitted"] == "yes"


def test_post_place_persistence_failure_never_downgrades_a_live_order_to_ledger_refusal(
        tmp_path, monkeypatch):
    legacy = _ledger(tmp_path / "legacy")
    legacy._write_state(legacy._empty_state())
    led = _ledger(tmp_path / "durable")
    legacy_lock = tmp_path / "legacy-cutover.lock"
    receipt = led.migration_receipt_path
    led._bootstrap_default_storage(
        legacy_entry_processes_stopped=True, absent_legacy_verified_empty=False,
        legacy_ledger=legacy.ledger_path, legacy_lock=legacy_lock, receipt_path=receipt)
    led._production_default = True
    led._legacy_ledger_path = legacy.ledger_path
    led._legacy_lock_path = legacy_lock
    original_write = led._write_json_atomic
    placed = []

    def fail_post_place_receipt_update(path, payload):
        if Path(path) == receipt and payload.get("active_generation") == 2:
            raise OSError("simulated post-place receipt update failure")
        return original_write(path, payload)

    monkeypatch.setattr(led, "_write_json_atomic", fail_post_place_receipt_update)
    decision = led.reserve_and_place(
        place=lambda: placed.append("broker-trade") or "broker-trade", **_admission_kwargs())

    assert placed == ["broker-trade"]
    assert decision.allowed and decision.should_place and decision.place_invoked
    assert decision.status == "placed_persistence_pending"
    assert json.loads(receipt.read_text())["active_generation"] == 1

    monkeypatch.setattr(led, "_write_json_atomic", original_write)
    assert led.open_intents()[0]["transmitted"] == "yes"
    assert json.loads(receipt.read_text())["active_generation"] == 2


def test_ambiguous_place_plus_persistence_failure_preserves_place_invoked_marker(
        tmp_path, monkeypatch):
    led = _ledger(tmp_path)
    original_write = led._write_state
    writes = {"count": 0}

    def fail_ambiguous_receipt_update(state):
        writes["count"] += 1
        if writes["count"] == 2:
            raise OSError("simulated ambiguous receipt update failure")
        return original_write(state)

    def ambiguous_place():
        raise RuntimeError("broker ACK lost after placeOrder")

    monkeypatch.setattr(led, "_write_state", fail_ambiguous_receipt_update)
    with pytest.raises(OSError, match="ambiguous receipt") as raised:
        led.reserve_and_place(place=ambiguous_place, **_admission_kwargs())

    assert raised.value.alfred_place_invoked is True
    assert led.open_intents()[0]["transmitted"] == "unknown"


def test_conflicting_intent_identity_refuses_without_writing(tmp_path):
    led = _ledger(tmp_path)
    kw = _admission_kwargs()
    kw["intent"]["code_version"] = "c" * 40
    called = []
    decision = led.reserve_and_place(place=lambda: called.append(True), **kw)
    assert decision.status == "ledger_error" and called == []
    assert led.snapshot()["reservations"] == {} and led.open_intents() == ()


@pytest.mark.parametrize("field,value", [
    ("con_id", 222),
    ("leg_con_ids", (222,)),
    ("symbol", "QQQ"),
    ("side", "credit"),
    ("structure", "vertical"),
    ("requested_qty", 2),
    ("estimated_debit", 99.0),
])
def test_conflicting_intent_order_facts_refuse_without_writing(tmp_path, field, value):
    led = _ledger(tmp_path)
    kw = _admission_kwargs()
    kw["intent"][field] = value
    called = []
    decision = led.reserve_and_place(place=lambda: called.append(True), **kw)
    assert decision.status == "ledger_error" and called == []
    assert led.snapshot()["reservations"] == {} and led.open_intents() == ()


@pytest.mark.parametrize("template", [None, {}, [], {"contract_id": 222, "symbol": "SPY"},
                                       {"contract_id": 111, "symbol": "QQQ"}])
def test_unusable_intent_journal_template_refuses_before_transmission(tmp_path, template):
    led = _ledger(tmp_path)
    kw = _admission_kwargs()
    kw["intent"]["journal_template"] = template
    called = []
    decision = led.reserve_and_place(place=lambda: called.append(True), **kw)
    assert decision.status == "ledger_error" and called == []
    assert led.snapshot()["reservations"] == {} and led.open_intents() == ()


@pytest.mark.parametrize("method", ["admit", "reserve_and_place"])
def test_a_durable_intent_is_mandatory_at_the_shared_boundary(tmp_path, method):
    led = _ledger(tmp_path)
    kw = _admission_kwargs(intent=None)
    called = []
    if method == "admit":
        decision = led.admit(**kw)
    else:
        decision = led.reserve_and_place(place=lambda: called.append(True), **kw)
    assert decision.status == "ledger_error" and called == []
    assert led.snapshot()["reservations"] == {} and led.open_intents() == ()


def test_an_orphan_intent_blocks_same_ref_replay_after_reservation_ttl(tmp_path):
    clock = [1_000.0]
    led = _ledger(tmp_path, ttl_seconds=1.0, clock=lambda: clock[0])
    ref = "alfred-entry:" + "7" * 32
    placements = []

    def ambiguous():
        placements.append(ref)
        raise RuntimeError("connection reset after broker submission")

    with pytest.raises(RuntimeError):
        led.reserve_and_place(place=ambiguous, **_admission_kwargs(order_ref=ref))
    clock[0] += 2.0

    decision = led.reserve_and_place(
        place=lambda: placements.append("duplicate"),
        **_admission_kwargs(order_ref=ref, con_id=333, leg_con_ids=(333,)))

    assert decision.status == "intent_unresolved" and placements == [ref]
    assert led.snapshot()["reservations"] == {}
    assert tuple(row["order_ref"] for row in led.open_intents()) == (ref,)


def test_an_orphan_intent_blocks_new_ref_over_same_leg_after_reservation_ttl(tmp_path):
    clock = [2_000.0]
    led = _ledger(tmp_path, ttl_seconds=1.0, clock=lambda: clock[0])
    old_ref = "alfred-entry:" + "8" * 32
    new_ref = "alfred-entry:" + "9" * 32
    placements = []

    def ambiguous():
        placements.append(old_ref)
        raise RuntimeError("unknown placement outcome")

    with pytest.raises(RuntimeError):
        led.reserve_and_place(place=ambiguous, **_admission_kwargs(order_ref=old_ref))
    clock[0] += 2.0
    decision = led.reserve_and_place(
        place=lambda: placements.append("duplicate"),
        **_admission_kwargs(order_ref=new_ref, con_id=111, leg_con_ids=(111, 444)))

    assert decision.status == "intent_unresolved" and placements == [old_ref]
    assert led.snapshot()["reservations"] == {}
    assert tuple(row["order_ref"] for row in led.open_intents()) == (old_ref,)


def test_a_source_bound_two_leg_recovery_template_is_admitted(tmp_path):
    led = _ledger(tmp_path)
    decision = led.reserve_and_place(
        place=lambda: "placed",
        **_admission_kwargs(con_id=111, leg_con_ids=(111, 222),
                            structure="debit spread"))
    assert decision.should_place and decision.status == "reserved"


@pytest.mark.parametrize("mutate", [
    lambda kw: kw["intent"]["journal_template"].pop("spread"),
    lambda kw: kw["intent"]["journal_template"]["spread"].update(short_con_id=333),
    lambda kw: kw["intent"]["journal_template"]["spread"].update(short_strike=0),
    lambda kw: kw["intent"]["journal_template"]["spread"].update(width=9.0),
])
def test_bad_two_leg_recovery_facts_refuse_before_transmission(tmp_path, mutate):
    led = _ledger(tmp_path)
    kw = _admission_kwargs(con_id=111, leg_con_ids=(111, 222), structure="debit spread")
    mutate(kw)
    placed = []
    decision = led.reserve_and_place(place=lambda: placed.append(True), **kw)
    assert decision.status == "ledger_error" and placed == []
    assert led.snapshot()["reservations"] == {} and led.open_intents() == ()


def test_primary_contract_must_be_one_of_the_admitted_legs(tmp_path):
    led = _ledger(tmp_path)
    placed = []
    decision = led.reserve_and_place(
        place=lambda: placed.append(True),
        **_admission_kwargs(con_id=111, leg_con_ids=(222,)))
    assert decision.status == "ledger_error" and placed == []


def test_single_leg_intent_refuses_spread_recovery_facts(tmp_path):
    led = _ledger(tmp_path)
    kw = _admission_kwargs()
    kw["intent"]["journal_template"]["spread"] = {
        "short_con_id": 222, "short_strike": 610.0, "width": 10.0}
    placed = []
    decision = led.reserve_and_place(place=lambda: placed.append(True), **kw)
    assert decision.status == "ledger_error" and placed == []




@pytest.mark.asyncio
async def test_an_unreadable_open_order_book_refuses_admission_instead_of_reading_it_as_empty(
        tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    t = _trader(tmp_path, monkeypatch)
    t.ib_conn.ib.reqAllOpenOrdersAsync = AsyncMock(side_effect=TimeoutError("gateway down"))
    with pytest.raises(RuntimeError, match="admission"):
        await t._submit_order_unlocked(
            _debit_order(),
            submission_authority=entry_safety.EntrySubmissionAuthority.autonomous())
    t.ib_conn.ib.placeOrder.assert_not_called()
    assert any(e["event"] == "admission_observation_unreadable" for e in _audit_events(tmp_path))


@pytest.mark.asyncio
async def test_a_None_open_order_response_is_unknown_not_known_empty(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    t = _trader(tmp_path, monkeypatch)
    t.ib_conn.ib.reqAllOpenOrdersAsync = AsyncMock(return_value=None)
    with pytest.raises(RuntimeError, match="admission"):
        await t._submit_order_unlocked(
            _debit_order(),
            submission_authority=entry_safety.EntrySubmissionAuthority.autonomous())
    t.ib_conn.ib.placeOrder.assert_not_called()



    assert any(e["event"] == "admission_observation_unreadable"
               and e.get("stage") == "open_orders"
               and "None" in str(e.get("error"))
               for e in _audit_events(tmp_path)), _audit_events(tmp_path)


@pytest.mark.asyncio
async def test_an_unreadable_short_position_read_refuses_admission(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    t = _trader(tmp_path, monkeypatch)

    async def _positions(include_short=False, include_stock=False):
        if include_short:
            raise ConnectionError("gateway flap")
        return {}

    t.ib_conn.get_positions = _positions
    with pytest.raises(RuntimeError, match="admission"):
        await t._submit_order_unlocked(
            _debit_order(),
            submission_authority=entry_safety.EntrySubmissionAuthority.autonomous())
    t.ib_conn.ib.placeOrder.assert_not_called()
    assert any(e["event"] == "admission_observation_unreadable"
               and e.get("stage") == "positions" for e in _audit_events(tmp_path))


@pytest.mark.asyncio
async def test_NEGATIVE_CONTROL_a_readable_EMPTY_open_order_book_still_admits(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    t = _trader(tmp_path, monkeypatch)
    t.ib_conn.ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[])
    status, _ = await t._submit_order_unlocked(
        _debit_order(),
        submission_authority=entry_safety.EntrySubmissionAuthority.autonomous())
    assert status == "Filled"
    t.ib_conn.ib.placeOrder.assert_called_once()


def test_an_unreadable_visibility_feed_never_expires_a_reservation(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    led = _ledger(tmp_path)
    led.reserve_and_place(place=lambda: "t", **_admission_kwargs(order_ref="alfred-entry:keep"))
    assert led.snapshot()["reservations"]
    with pytest.raises(EntryReservationError):
        led.reconcile(broker_deployed_usd=0.0, visible_order_refs=None, readable=False)
    assert "alfred-entry:keep" in led.snapshot()["reservations"]




def test_every_executable_placeorder_site_is_inside_an_admission_transaction():
    """Public API contract; production-derived narrative omitted."""
    for rel in ("daily_recommend.py", "place_trade.py", "exitmgr/trader.py"):
        source = (REPO / rel).read_text()
        assert "reserve_and_place" in source, "%s places orders outside the admission transaction" % rel


def test_no_entry_route_calls_the_dollars_only_reserve_directly():
    """Public API contract; production-derived narrative omitted."""
    for rel in ("daily_recommend.py", "place_trade.py", "exitmgr/trader.py"):
        source = (REPO / rel).read_text()
        assert "ledger.reserve(" not in source, rel
        assert ".reserve(order_ref" not in source, rel
