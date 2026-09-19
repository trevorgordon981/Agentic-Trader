"""Public API contract; production-derived narrative omitted."""
import json
import time
from argparse import Namespace
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from tests._admission_stub import stub_admission_reads

import pytest

import daily_recommend
from daily_recommend import apply_structure_override, submit_structure_ok, user_directed_idea
from exitmgr import entry_safety, strategist, trader as trader_mod
from exitmgr.risk import RiskLimits
from exitmgr.strategist import DEBIT_STRUCTURES, TradeIdea
from exitmgr.trader import (
    ResolvedOrder, Trader, _structure_implied_right, debit_structure_ok,
)

REPO = Path(__file__).resolve().parents[1]



BANNED = [
    "naked call", "naked put", "short call", "short put", "short strangle", "long strangle",
    "short straddle", "straddle", "iron condor", "condor", "butterfly", "ratio spread",
    "credit spread", "bull put spread", "bear call spread", "covered call", "calendar spread",
    "diagonal spread", "cash secured put", "jade lizard", "", "x", "options trade", "NAKED CALL",
    "sell a call", "call",
]






def test_allowlist_is_imported_not_redeclared():
    """Public API contract; production-derived narrative omitted."""
    assert trader_mod.DEBIT_STRUCTURES is strategist.DEBIT_STRUCTURES
    assert trader_mod._require_allowed_structure is strategist._require_allowed_structure
    for name in ("exitmgr/trader.py", "daily_recommend.py"):
        text = (REPO / name).read_text()
        assert "DEBIT_STRUCTURES = " not in text, f"{name} re-declares the allow-list"
        assert "def _require_allowed_structure" not in text, f"{name} re-implements the gate"


def test_no_allowlist_entry_names_two_rights():
    """Public API contract; production-derived narrative omitted."""
    for s in DEBIT_STRUCTURES:
        assert not ("call" in s and "put" in s), s
        assert _structure_implied_right(s) in ("C", "P", "")






def _idea(structure, direction="bullish", **kw):
    return TradeIdea(underlying="AAPL", is_index=False, direction=direction, structure=structure,
                     target_dte=30, target_delta=0.6, est_debit_usd=500.0, conviction=6,
                     thesis="t", **kw)


def _consistent(structure, direction):
    implied = _structure_implied_right(structure)
    stated = {"bullish": "C", "bearish": "P"}.get(direction)
    return not (implied and stated and implied != stated)


def test_gate_accepts_every_permitted_consistent_pair():
    for s in sorted(DEBIT_STRUCTURES):
        for d in ("bullish", "bearish"):
            ok, why = debit_structure_ok(_idea(s, d))
            assert ok is _consistent(s, d), (s, d, why)


def test_gate_refuses_every_banned_structure_on_both_directions():
    for s in BANNED:
        for d in ("bullish", "bearish"):
            ok, why = debit_structure_ok(_idea(s, d))
            assert not ok, f"{s!r} was accepted"
            assert repr(s) in why, why
            assert "long call" in why and "put debit spread" in why


def test_gate_refuses_structure_direction_contradiction():
    """Public API contract; production-derived narrative omitted."""
    ok, why = debit_structure_ok(_idea("bull call spread", "bearish"))
    assert not ok
    assert "CONTRADICTION" in why and "bull call spread" in why and "bearish" in why
    ok, why = debit_structure_ok(_idea("bear put spread", "bullish"))
    assert not ok and "CONTRADICTION" in why
    ok, _ = debit_structure_ok(_idea("long call", "bearish"))
    assert not ok
    ok, _ = debit_structure_ok(_idea("long put", "bullish"))
    assert not ok


def test_right_agnostic_structures_accept_either_direction_unchanged():
    """Public API contract; production-derived narrative omitted."""
    for s in ("long option", "debit spread"):
        for d in ("bullish", "bearish", ""):
            assert debit_structure_ok(_idea(s, d))[0], (s, d)


def test_membership_is_tested_on_the_canonical_form_only():
    """Public API contract; production-derived narrative omitted."""
    for variant in ("long  call", "Long Call", "  LONG CALL  ", "long\tcall"):
        idea = _idea(variant)
        assert debit_structure_ok(idea)[0], variant
        assert idea.structure == variant
    assert not debit_structure_ok(_idea("NAKED CALL"))[0]
    assert not debit_structure_ok(_idea("Short  Strangle"))[0]


def test_gate_is_a_no_op_for_credit_ideas():
    """Public API contract; production-derived narrative omitted."""
    csp = _idea("cash secured put", "bullish", side="credit", strike=100.0,
                collateral_usd=10000.0, net_credit_usd=150.0, max_loss_usd=9850.0)
    assert debit_structure_ok(csp) == (True, "")


def test_gate_never_coerces():
    """Public API contract; production-derived narrative omitted."""
    idea = _idea("naked call")
    ok, why = debit_structure_ok(idea)
    assert not ok and isinstance(why, str)
    assert idea.structure == "naked call"






def _args(structure="", right="C", **kw):




    d = dict(ticker="aapl", right=right, structure=structure, dte=30, delta=0.6,
             conviction=6, thesis="User-directed proposal.", tp=0.0, stop=0.0, hold_days=7)
    d.update(kw)
    return Namespace(**d)


def _legacy_user_directed_idea(args):
    """Public API contract; production-derived narrative omitted."""
    direction = "bullish" if args.right.upper() == "C" else "bearish"
    structure = args.structure or ("long call" if direction == "bullish" else "long put")
    return TradeIdea(underlying=args.ticker.upper(),
                     is_index=args.ticker.upper() in ("SPY", "QQQ", "IWM"),
                     direction=direction, structure=structure,
                     target_dte=args.dte, target_delta=args.delta,
                     est_debit_usd=0.0, conviction=int(args.conviction),
                     thesis=args.thesis, profit_target_pct=args.tp, stop_pct=args.stop)


def test_bypass1_naked_call_used_to_construct_and_now_refuses():
    """Public API contract; production-derived narrative omitted."""
    a = _args("naked call")
    legacy = _legacy_user_directed_idea(a)
    assert legacy.structure == "naked call" and legacy.direction == "bullish"
    assert "spread" not in legacy.structure
    with pytest.raises(ValueError) as exc:
        user_directed_idea(a)
    assert "naked call" in str(exc.value)
    assert "long call" in str(exc.value)


def _cmp_ignoring_hold(new_idea, legacy_idea):
    """Public API contract; production-derived narrative omitted."""
    a, b = asdict(new_idea), asdict(legacy_idea)
    a.pop("intended_hold_days", None)
    b.pop("intended_hold_days", None)
    assert a == b
    assert new_idea.intended_hold_days is not None, "post-fix builder must set a hold"
    assert new_idea.intended_hold_days > 0


def test_bypass1_differential_permitted_identical_banned_refused():
    """Public API contract; production-derived narrative omitted."""
    cases = accepted = refused = 0
    for structure in sorted(DEBIT_STRUCTURES) + BANNED:
        for right in ("C", "P", "c", "p"):
            for dte in (7, 25, 30, 45, 120):
                for delta in (0.35, 0.6, 0.75):
                    for conviction in (1, 6, 10):
                        for tp, stop in ((0.0, 0.0), (35.0, 30.0)):
                            for ticker in ("aapl", "SPY"):
                                cases += 1
                                a = _args(structure, right, dte=dte, delta=delta,
                                          conviction=conviction, tp=tp, stop=stop, ticker=ticker)
                                legacy = _legacy_user_directed_idea(a)
                                should = (legacy.structure in DEBIT_STRUCTURES
                                          and _consistent(legacy.structure, legacy.direction))
                                try:
                                    got = user_directed_idea(a)
                                except ValueError as e:
                                    refused += 1
                                    assert not should, f"{structure!r}/{right} wrongly refused: {e}"
                                    continue
                                accepted += 1
                                assert should, f"{structure!r}/{right} wrongly accepted"
                                _cmp_ignoring_hold(got, legacy)






    assert (cases, accepted, refused) == (27360, 5760, 21600), (cases, accepted, refused)


def test_bypass1_default_structure_path_is_untouched():
    """Public API contract; production-derived narrative omitted."""
    _cmp_ignoring_hold(user_directed_idea(_args("", "C")),
                       _legacy_user_directed_idea(_args("", "C")))
    assert user_directed_idea(_args("", "C")).structure == "long call"
    assert user_directed_idea(_args("", "P")).structure == "long put"


def test_bypass1_right_and_structure_must_agree():
    """Public API contract; production-derived narrative omitted."""
    legacy = _legacy_user_directed_idea(_args("long call", "P"))
    assert legacy.direction == "bearish" and legacy.structure == "long call"
    with pytest.raises(ValueError) as exc:
        user_directed_idea(_args("long call", "P"))
    assert "CONTRADICTION" in str(exc.value)


def test_bypass1_cli_fails_before_any_ibkr_connection():
    """Public API contract; production-derived narrative omitted."""
    text = (REPO / "daily_recommend.py").read_text()
    main_block = text.split('if __name__ == "__main__":')[1]
    assert main_block.index("user_directed_idea(_args)") < main_block.index("asyncio.run(run(")






def _trader(tmp_path):
    ibc = MagicMock()
    ibc.ib = MagicMock()
    ibc.ib.placeOrder.return_value.orderStatus.status = "Filled"
    ibc.ib.placeOrder.return_value.orderStatus.avgFillPrice = 1.20
    ibc.ib.placeOrder.return_value.fills = []

    stub_admission_reads(ibc)
    return Trader(ib_conn=ibc, exit_manager=MagicMock(), limits=RiskLimits(),
                  approved_names=set(), endpoint="http://x", model="m", slack_token="t",
                  slack_channel="C", approver_ids=set(), baseline_path=str(tmp_path / "b.json"),
                  audit_path=str(tmp_path / "a.jsonl"), journal_path=str(tmp_path / "trades.log"))


def _fake_queue(monkeypatch, tickets):
    class _Q:
        def __init__(self, path):
            pass

        def drain(self, *, today, max_per_name):
            return list(tickets), {}
    monkeypatch.setattr(trader_mod.reload_queue, "ReloadQueue", _Q)


TICKETS = [
    {"symbol": "SPY", "right": "C", "structure": "spread", "dte_target": 30, "original_debit": 400.0,
     "reload_conviction": 7, "thesis": "runner"},
    {"symbol": "IWM", "right": "P", "structure": "single", "dte_target": 30, "original_debit": 300.0,
     "reload_conviction": 6, "thesis": "runner"},
]


def test_bypass2_reload_tickets_unchanged(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    _fake_queue(monkeypatch, TICKETS)
    ideas = _trader(tmp_path)._drain_reload_ideas("2026-07-26")
    assert [i.structure for i in ideas] == ["debit spread", "long option"]
    assert [i.direction for i in ideas] == ["bullish", "bearish"]
    assert all(getattr(i, "is_reload", False) for i in ideas)
    assert all(debit_structure_ok(i)[0] for i in ideas)


def test_bypass2_a_regressed_mapping_is_dropped_not_shipped(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    _fake_queue(monkeypatch, TICKETS)
    real = trader_mod.TradeIdea

    def _corrupt(**kw):
        kw["structure"] = "naked call"
        return real(**kw)
    monkeypatch.setattr(trader_mod, "TradeIdea", _corrupt)
    t = _trader(tmp_path)
    assert t._drain_reload_ideas("2026-07-26") == []
    events = [json.loads(l) for l in Path(t.audit_path).read_text().splitlines()]
    rej = [e for e in events if e.get("event") == "reload_structure_rejected"]
    assert len(rej) == 2 and all("naked call" in e["reason"] for e in rej)






def _legacy_override(idea, ovr):
    """Public API contract; production-derived narrative omitted."""
    nd = idea.direction
    if ovr.get("direction") == "flip":
        nd = "bearish" if idea.direction == "bullish" else "bullish"
    elif ovr.get("direction") in ("bullish", "bearish"):
        nd = ovr["direction"]
    ns = idea.structure
    if ovr.get("structure") == "single":
        ns = "long put" if nd == "bearish" else "long call"
    elif ovr.get("structure") == "spread":
        ns = "put debit spread" if nd == "bearish" else "call debit spread"
    return replace(idea, direction=nd, structure=ns)


OVERRIDES = [{}, {"structure": "single"}, {"structure": "spread"},
             {"direction": "bullish"}, {"direction": "bearish"}, {"direction": "flip"},
             {"direction": "flip", "structure": "single"},
             {"direction": "flip", "structure": "spread"},
             {"direction": "bearish", "structure": "spread"},
             {"direction": "bullish", "structure": "single"}]


def test_bypass3_differential_order_is_never_changed():
    """Public API contract; production-derived narrative omitted."""
    cases = relabelled = identical = 0
    for s in sorted(DEBIT_STRUCTURES):
        for d in ("bullish", "bearish"):
            if not _consistent(s, d):
                continue
            for ovr in OVERRIDES:
                cases += 1
                idea = _idea(s, d)
                legacy = _legacy_override(idea, ovr)
                got, err, note = apply_structure_override(idea, ovr)
                assert not err, (s, d, ovr, err)

                assert got.direction == legacy.direction
                assert ("spread" in got.structure.lower()) == ("spread" in legacy.structure.lower())
                assert asdict(replace(got, structure="")) == asdict(replace(legacy, structure=""))
                if note:
                    relabelled += 1


                    assert not _consistent(legacy.structure, legacy.direction)
                    assert _consistent(got.structure, got.direction)
                    assert got.structure in DEBIT_STRUCTURES
                else:
                    identical += 1
                    assert got.structure == legacy.structure




    assert (cases, relabelled, identical) == (140, 20, 120), (cases, relabelled, identical)


def test_bypass3_flip_used_to_mislabel_and_now_relabels_loudly():
    """Public API contract; production-derived narrative omitted."""
    idea = _idea("bull call spread", "bullish")
    legacy = _legacy_override(idea, {"direction": "flip"})
    assert legacy.direction == "bearish" and legacy.structure == "bull call spread"
    got, err, note = apply_structure_override(idea, {"direction": "flip"})
    assert not err
    assert got.direction == "bearish" and got.structure == "put debit spread"
    assert "relabelled" in note and "bull call spread" in note
    assert ("spread" in got.structure) == ("spread" in legacy.structure)


def test_bypass3_flip_on_a_single_leg_keeps_it_single():
    got, err, note = apply_structure_override(_idea("long call", "bullish"), {"direction": "flip"})
    assert not err and got.structure == "long put" and got.direction == "bearish" and note


def test_bypass3_inherited_contradiction_is_refused():
    """Public API contract; production-derived narrative omitted."""
    got, err, note = apply_structure_override(_idea("bull call spread", "bearish"), {})
    assert err and "CONTRADICTION" in err and not note


def test_bypass3_banned_structure_is_never_laundered_by_a_relabel():
    """Public API contract; production-derived narrative omitted."""
    for ovr in ({}, {"direction": "flip"}, {"direction": "bearish"}, {"direction": "bullish"}):
        got, err, note = apply_structure_override(_idea("naked call", "bullish"), ovr)
        assert err and "naked call" in err, ovr
        assert not note, ovr
        assert got.structure == "naked call", ovr

    for ovr in ({"structure": "single"}, {"structure": "spread"},
                {"direction": "flip", "structure": "single"}):
        got, err, note = apply_structure_override(_idea("naked call", "bullish"), ovr)
        assert err and "naked call" in err and got.structure == "naked call", ovr


def test_bypass3_vocabulary_itself_was_never_the_hole():
    """Public API contract; production-derived narrative omitted."""
    from exitmgr import approval
    for text in ("make it a spread", "single", "just the long call", "flip it", "go bearish",
                 "no spread", "vertical", "puts"):
        ovr = approval.parse_structure_override(text)
        assert set(ovr) <= {"structure", "direction"}
        assert ovr.get("structure") in (None, "single", "spread")






def _contract(con_id):
    c = MagicMock()
    c.conId = con_id
    return c


def _submittable(t, **kw):
    t._entry_markers_clear = lambda: trader_mod.entry_safety.SafetyResult(True, ())
    return ResolvedOrder("SPY", "C", "20260620", 610.0, 1, 1.20, _contract(111),
                         entry_bid=1.15, entry_ask=1.25, quote_observed_at=time.monotonic(),
                         decision_id="decision-" + "a" * 32, **kw)


@pytest.mark.asyncio
async def test_submit_allows_a_permitted_structure(tmp_path):
    t = _trader(tmp_path)
    await t._submit_order(
        _submittable(t, structure="long call"),
        submission_authority=entry_safety.EntrySubmissionAuthority.autonomous())
    t.ib_conn.ib.placeOrder.assert_called_once()


@pytest.mark.asyncio
async def test_submit_allows_an_unlabelled_order(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    t = _trader(tmp_path)
    await t._submit_order(
        _submittable(t),
        submission_authority=entry_safety.EntrySubmissionAuthority.autonomous())
    t.ib_conn.ib.placeOrder.assert_called_once()


@pytest.mark.asyncio
async def test_submit_refuses_a_banned_structure_at_the_money_boundary(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    t = _trader(tmp_path)
    for s in ("naked call", "short strangle", "iron condor"):
        with pytest.raises(RuntimeError) as exc:
            await t._submit_order(
                _submittable(t, structure=s),
                submission_authority=entry_safety.EntrySubmissionAuthority.autonomous())
        assert "STRUCTURE REFUSED at submit" in str(exc.value) and s in str(exc.value)
    t.ib_conn.ib.placeOrder.assert_not_called()
    assert not (tmp_path / "trades.log").exists()


@pytest.mark.asyncio
async def test_submit_refuses_a_structure_that_names_the_wrong_right(tmp_path):
    t = _trader(tmp_path)
    with pytest.raises(RuntimeError) as exc:
        await t._submit_order(
            _submittable(t, structure="long put"),
            submission_authority=entry_safety.EntrySubmissionAuthority.autonomous())
    assert "names a put" in str(exc.value)
    t.ib_conn.ib.placeOrder.assert_not_called()


def test_resolved_order_structure_field_is_additive():
    """Public API contract; production-derived narrative omitted."""
    assert ResolvedOrder("SPY", "C", "20260620", 610.0, 1, 1.20, object()).structure == ""






@pytest.mark.asyncio
async def test_trader_resolve_order_refuses_before_touching_ibkr(tmp_path):
    t = _trader(tmp_path)
    t.ib_conn.ib.qualifyContractsAsync = AsyncMock()
    assert await t._resolve_order(_idea("naked call"), 1000.0) is None
    assert await t._resolve_order(_idea("bull call spread", "bearish"), 1000.0) is None
    t.ib_conn.ib.qualifyContractsAsync.assert_not_called()
    events = [json.loads(l) for l in Path(t.audit_path).read_text().splitlines()]
    assert len([e for e in events if e.get("event") == "debit_structure_rejected"]) == 2


@pytest.mark.asyncio
async def test_daily_recommend_resolve_refuses_before_touching_ibkr():
    ib = MagicMock()
    ib.qualifyContractsAsync = AsyncMock()
    r, why = await daily_recommend._resolve(ib, _idea("short strangle"), 5000.0)
    assert r is None and "short strangle" in why
    r, why = await daily_recommend._resolve(ib, _idea("bear put spread", "bullish"), 5000.0)
    assert r is None and "CONTRADICTION" in why
    ib.qualifyContractsAsync.assert_not_called()







def _wire_debit_chain(t, monkeypatch, *, spot=100.0, quotes=((100.0, 2.90, 3.10, 0.60),
                                                             (105.0, 0.95, 1.05, 0.35))):
    """Public API contract; production-derived narrative omitted."""
    import importlib
    import sys
    ibkr = importlib.import_module("exitmgr.ibkr")
    monkeypatch.setitem(sys.modules, "exitmgr.ibkr", ibkr)
    monkeypatch.setattr(ibkr, "Stock", lambda *a, **k: MagicMock())
    monkeypatch.setattr(ibkr, "Option", lambda *a, **k: MagicMock())
    monkeypatch.setattr(ibkr, "underlying_price", AsyncMock(return_value=spot))
    chain = MagicMock()
    chain.exchange, chain.tradingClass = "SMART", "SPY"
    chain.expirations = [(trader_mod.datetime.now(trader_mod.timezone.utc).date()
                          + trader_mod.timedelta(days=35)).strftime("%Y%m%d")]
    chain.strikes = [q[0] for q in quotes]
    tickers = []
    for i, (k, bid, ask, delta) in enumerate(quotes):
        c = MagicMock(conId=900 + i, strike=k, right="C")
        tk = MagicMock()
        tk.contract, tk.bid, tk.ask, tk.last = c, bid, ask, (bid + ask) / 2
        tk.modelGreeks = MagicMock(delta=delta, theta=-0.05, gamma=0.01, vega=0.1, impliedVol=0.30)
        tickers.append(tk)
    monkeypatch.setattr(
        ibkr, "option_contracts_for_expiry",
        AsyncMock(return_value=[tk.contract for tk in tickers]))
    t.ib_conn.ib.qualifyContractsAsync = AsyncMock(
        side_effect=[[MagicMock(conId=7)], [tk.contract for tk in tickers]])
    t.ib_conn.ib.reqSecDefOptParamsAsync = AsyncMock(return_value=[chain])
    t.ib_conn.ib.reqTickersAsync = AsyncMock(return_value=tickers)


@pytest.mark.asyncio
async def test_resolve_order_carries_the_structure_onto_a_single_leg(tmp_path, monkeypatch):
    t = _trader(tmp_path)
    _wire_debit_chain(t, monkeypatch)
    r = await t._resolve_order(_idea("long call"), per_trade_cap=5000.0)
    assert r is not None and r.short_contract is None
    assert r.structure == "long call"


@pytest.mark.asyncio
async def test_resolve_order_carries_the_structure_onto_a_spread(tmp_path, monkeypatch):
    t = _trader(tmp_path)
    _wire_debit_chain(t, monkeypatch)
    r = await t._resolve_order(_idea("call debit spread"), per_trade_cap=5000.0)
    assert r is not None and r.short_contract is not None
    assert r.structure == "call debit spread"


@pytest.mark.asyncio
async def test_daily_recommend_resolve_carries_the_structure_onto_the_order(monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    ib = MagicMock()
    monkeypatch.setattr(daily_recommend, "Stock", lambda *a, **k: MagicMock())
    monkeypatch.setattr(daily_recommend, "Option", lambda *a, **k: MagicMock())
    monkeypatch.setattr(daily_recommend, "underlying_price", AsyncMock(return_value=100.0))
    chain = MagicMock()
    chain.exchange, chain.tradingClass = "SMART", "SPY"
    chain.expirations = [(trader_mod.datetime.now(trader_mod.timezone.utc).date()
                          + trader_mod.timedelta(days=35)).strftime("%Y%m%d")]
    chain.strikes = [100.0, 105.0]
    tickers = []
    for i, (k, bid, ask, delta) in enumerate(((100.0, 2.90, 3.10, 0.60), (105.0, 0.95, 1.05, 0.35))):
        c = MagicMock(conId=900 + i, strike=k, right="C")
        tk = MagicMock()
        tk.contract, tk.bid, tk.ask, tk.last = c, bid, ask, (bid + ask) / 2
        tk.modelGreeks = MagicMock(delta=delta, theta=-0.05, gamma=0.01, vega=0.1, impliedVol=0.30)
        tickers.append(tk)
    monkeypatch.setattr(
        daily_recommend, "option_contracts_for_expiry",
        AsyncMock(return_value=[tk.contract for tk in tickers]))
    ib.qualifyContractsAsync = AsyncMock(
        side_effect=[[MagicMock(conId=7)], [tk.contract for tk in tickers]])
    ib.reqSecDefOptParamsAsync = AsyncMock(return_value=[chain])
    ib.reqTickersAsync = AsyncMock(return_value=tickers)
    r, why = await daily_recommend._resolve(ib, _idea("long call"), 5000.0)
    assert r is not None, why
    assert r.structure == "long call"


def test_daily_recommend_submit_gate():
    """Public API contract; production-derived narrative omitted."""
    ok_order = ResolvedOrder("SPY", "C", "20260620", 610.0, 1, 1.20, object(), structure="long call")
    assert submit_structure_ok(ok_order, _idea("long call")) == (True, "")

    assert submit_structure_ok(ResolvedOrder("SPY", "C", "20260620", 610.0, 1, 1.2, object()),
                               _idea("long call")) == (True, "")

    ok, why = submit_structure_ok(
        ResolvedOrder("SPY", "C", "20260620", 610.0, 1, 1.2, object(), structure="naked call"),
        _idea("naked call"))
    assert not ok and "naked call" in why

    ok, why = submit_structure_ok(
        ResolvedOrder("SPY", "P", "20260620", 610.0, 1, 1.2, object(), structure="long call"),
        _idea("long call"))
    assert not ok and "names a call" in why


def test_daily_recommend_submit_gate_is_wired_in_ahead_of_place_order():
    """Public API contract; production-derived narrative omitted."""
    text = (REPO / "daily_recommend.py").read_text()
    call = text.index("_ok_submit, _why_submit = submit_structure_ok(r, effective_idea)")
    guard = text.index("if not _ok_submit:", call)
    place = text.index("ib.placeOrder(", call)
    assert call < guard < place
    assert guard - call < 200, "the guard must immediately follow the call"







@pytest.fixture
def hermetic_entry_loop(monkeypatch):
    from exitmgr.account import PotSnapshot
    posts = []
    monkeypatch.setattr(trader_mod.research, "gather", AsyncMock(return_value={}))
    monkeypatch.setattr(trader_mod.research, "days_to_earnings", lambda *a, **k: None)
    monkeypatch.setattr(trader_mod.research, "days_to_ex_dividend", lambda *a, **k: None)
    monkeypatch.setattr(trader_mod, "_market_open", lambda: True)
    monkeypatch.setattr(trader_mod, "get_pot_snapshot",
                        AsyncMock(return_value=PotSnapshot(50000.0, 40000.0, 50000.0)))
    monkeypatch.setattr(trader_mod.approval, "post_proposal",
                        lambda *a, **k: posts.append(a[-1]) or "ts1")
    monkeypatch.setattr(trader_mod.approval, "await_approval", lambda *a, **k: "approve")
    return posts


def _spy_idea(structure, direction="bullish"):
    """Public API contract; production-derived narrative omitted."""
    return TradeIdea(underlying="SPY", is_index=True, direction=direction, structure=structure,
                     target_dte=30, target_delta=0.6, est_debit_usd=90.0, conviction=6,
                     thesis="trend")


async def _run_entry_loop(t, monkeypatch, idea):
    monkeypatch.setattr(trader_mod, "propose", lambda *a, **k: [idea])



    monkeypatch.setattr(trader_mod, "propose_intents",
                        lambda *a, **k: ((lambda *a, **k: [idea])(*a, **k), "", None, None))
    async def _stage_b_passthrough(self, intents, pot):
        return list(intents or [])
    monkeypatch.setattr(trader_mod.Trader, "_materialize_stage_b", _stage_b_passthrough)
    t.exit_manager.run_cycle = AsyncMock()
    t._entry_markers_clear = lambda: trader_mod.entry_safety.SafetyResult(True, ())
    t._resolve_order = AsyncMock(return_value=None)
    t._submit_order = AsyncMock(return_value=("Filled", []))
    t.ib_conn.get_positions = AsyncMock(return_value={})
    t.ib_conn.ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[])
    await t.run_once(dry_run=False)


@pytest.mark.asyncio
async def test_entry_loop_refuses_before_the_risk_gate_and_before_construction(
        tmp_path, monkeypatch, hermetic_entry_loop):
    """Public API contract; production-derived narrative omitted."""
    t = _trader(tmp_path)
    await _run_entry_loop(t, monkeypatch, _spy_idea("naked call"))
    t._resolve_order.assert_not_awaited()
    t._submit_order.assert_not_awaited()
    events = [json.loads(l) for l in Path(t.audit_path).read_text().splitlines()]
    rej = [e for e in events if e.get("event") == "debit_structure_rejected"]
    assert len(rej) == 1 and "naked call" in rej[0]["reason"]
    assert any("REFUSED" in m and "naked call" in m for m in hermetic_entry_loop)

    assert not [e for e in events if e.get("event") == "gated"]


@pytest.mark.asyncio
async def test_entry_loop_lets_a_permitted_structure_through_to_the_resolver(
        tmp_path, monkeypatch, hermetic_entry_loop):
    """Public API contract; production-derived narrative omitted."""
    t = _trader(tmp_path)
    await _run_entry_loop(t, monkeypatch, _spy_idea("long call"))
    t._resolve_order.assert_awaited()
    events = [json.loads(l) for l in Path(t.audit_path).read_text().splitlines()]
    assert not [e for e in events if e.get("event") == "debit_structure_rejected"]
