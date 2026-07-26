"""STRUCTURE ALLOW-LIST: the three routes that BYPASSED it, and the submit-time gate.

The allow-list (strategist.DEBIT_STRUCTURES, enforced by strategist._require_allowed_structure)
only ever ran on ideas the STRATEGIST PARSED. Three routes build a TradeIdea directly and never
touch the parser, so nothing on them was ever checked:

  1. daily_recommend.py  --structure          an arbitrary CLI string -> TradeIdea()
  2. exitmgr/trader.py   _drain_reload_ideas  reload tickets -> TradeIdea()
  3. daily_recommend.py  Slack approval override -> dataclasses.replace() on the idea

WHAT THE BYPASS ACTUALLY DID -- stated accurately, because the fix depends on it. Both executors
(trader._resolve_order, daily_recommend._resolve) branch on the substring "spread" and take the
option right from `direction`, NEVER from the structure string. "naked call" contains no "spread",
so it routed to the SINGLE LONG LEG builder and BOUGHT a call. No naked short was reachable on the
debit path at all. The defect is SILENT SEMANTIC CORRUPTION: a filled long call recorded in the
journal -- and therefore in the training corpus -- under the name of a structure the account never
held. These tests are about correctness of the record, not about unbounded loss.

Every test below is paired with an entry in tests/MUTANTS_structure_allowlist.md, which names the
exact edit that makes it fail.
"""
import json
import time
from argparse import Namespace
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import daily_recommend
from daily_recommend import apply_structure_override, submit_structure_ok, user_directed_idea
from exitmgr import strategist, trader as trader_mod
from exitmgr.risk import RiskLimits
from exitmgr.strategist import DEBIT_STRUCTURES, TradeIdea
from exitmgr.trader import (
    ResolvedOrder, Trader, _structure_implied_right, debit_structure_ok,
)

REPO = Path(__file__).resolve().parents[1]

# Structures that must be REFUSED. Short-premium names are the dangerous ones; the undefined
# placeholders matter just as much, because "" and "x" are what a broken caller actually sends.
BANNED = [
    "naked call", "naked put", "short call", "short put", "short strangle", "long strangle",
    "short straddle", "straddle", "iron condor", "condor", "butterfly", "ratio spread",
    "credit spread", "bull put spread", "bear call spread", "covered call", "calendar spread",
    "diagonal spread", "cash secured put", "jade lizard", "", "x", "options trade", "NAKED CALL",
    "sell a call", "call",
]


# --------------------------------------------------------------------------------------------
# 0. ONE allow-list, in ONE place
# --------------------------------------------------------------------------------------------

def test_allowlist_is_imported_not_redeclared():
    """trader.py and daily_recommend.py must USE strategist's list, never own a copy. Two lists
    that can drift is a worse bug than one gate in the wrong place."""
    assert trader_mod.DEBIT_STRUCTURES is strategist.DEBIT_STRUCTURES
    assert trader_mod._require_allowed_structure is strategist._require_allowed_structure
    for name in ("exitmgr/trader.py", "daily_recommend.py"):
        text = (REPO / name).read_text()
        assert "DEBIT_STRUCTURES = " not in text, f"{name} re-declares the allow-list"
        assert "def _require_allowed_structure" not in text, f"{name} re-implements the gate"


def test_no_allowlist_entry_names_two_rights():
    """_structure_implied_right() is only unambiguous while no permitted structure contains both
    'call' and 'put'. If one ever does, the direction-consistency check must be rethought."""
    for s in DEBIT_STRUCTURES:
        assert not ("call" in s and "put" in s), s
        assert _structure_implied_right(s) in ("C", "P", "")


# --------------------------------------------------------------------------------------------
# 1. THE GATE ITSELF  (allow-list + structure/direction consistency)
# --------------------------------------------------------------------------------------------

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
            assert repr(s) in why, why                       # names the offending string
            assert "long call" in why and "put debit spread" in why   # names the permitted set


def test_gate_refuses_structure_direction_contradiction():
    """THE PRE-EXISTING INCONSISTENCY: 'bull call spread' + direction 'bearish' passes the
    allow-list, and the constructor -- which reads the right off `direction` -- then builds a PUT
    vertical and journals it as a bull call spread. Refused, not repaired: nothing in the idea
    says which of the two fields is the mistaken one, and deriving the right from the structure
    would silently overrule an explicit direction and flip the trade's side."""
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
    """'long option' and 'debit spread' name no right, so they constrain nothing -- exactly as
    before. These are the strings trader._drain_reload_ideas() emits."""
    for s in ("long option", "debit spread"):
        for d in ("bullish", "bearish", ""):
            assert debit_structure_ok(_idea(s, d))[0], (s, d)


def test_membership_is_tested_on_the_canonical_form_only():
    """Case and whitespace are canonicalised before the membership test -- strategist's documented
    behaviour, inherited here unchanged, NOT a hole. "long  call" and "Long Call" are the same
    structure; "NAKED CALL" is still refused. The ORIGINAL string is what stays on the idea."""
    for variant in ("long  call", "Long Call", "  LONG CALL  ", "long\tcall"):
        idea = _idea(variant)
        assert debit_structure_ok(idea)[0], variant
        assert idea.structure == variant          # never rewritten to the canonical form
    assert not debit_structure_ok(_idea("NAKED CALL"))[0]
    assert not debit_structure_ok(_idea("Short  Strangle"))[0]


def test_gate_is_a_no_op_for_credit_ideas():
    """credit_structure_ok() judges the credit side; this one must not double-judge it."""
    csp = _idea("cash secured put", "bullish", side="credit", strike=100.0,
                collateral_usd=10000.0, net_credit_usd=150.0, max_loss_usd=9850.0)
    assert debit_structure_ok(csp) == (True, "")


def test_gate_never_coerces():
    """A refusal returns (False, reason). It must never hand back a substituted structure."""
    idea = _idea("naked call")
    ok, why = debit_structure_ok(idea)
    assert not ok and isinstance(why, str)
    assert idea.structure == "naked call"       # the idea is untouched, not rewritten


# --------------------------------------------------------------------------------------------
# 2. BYPASS 1 -- daily_recommend --structure  (an arbitrary CLI string)
# --------------------------------------------------------------------------------------------

def _args(structure="", right="C", **kw):
    d = dict(ticker="aapl", right=right, structure=structure, dte=30, delta=0.6,
             conviction=6, thesis="User-directed proposal.", tp=0.0, stop=0.0)
    d.update(kw)
    return Namespace(**d)


def _legacy_user_directed_idea(args):
    """The construction EXACTLY as it stood at daily_recommend.py:732-737 before this fix."""
    direction = "bullish" if args.right.upper() == "C" else "bearish"
    structure = args.structure or ("long call" if direction == "bullish" else "long put")
    return TradeIdea(underlying=args.ticker.upper(),
                     is_index=args.ticker.upper() in ("SPY", "QQQ", "IWM"),
                     direction=direction, structure=structure,
                     target_dte=args.dte, target_delta=args.delta,
                     est_debit_usd=0.0, conviction=int(args.conviction),
                     thesis=args.thesis, profit_target_pct=args.tp, stop_pct=args.stop)


def test_bypass1_naked_call_used_to_construct_and_now_refuses():
    """`daily_recommend --ticker AAPL --structure "naked call"` -- the exact command that used to
    succeed. It BUILT an idea (proving the parser's allow-list never ran on this path), and what
    it built was a long CALL wearing the name of a naked short."""
    a = _args("naked call")
    legacy = _legacy_user_directed_idea(a)
    assert legacy.structure == "naked call" and legacy.direction == "bullish"
    assert "spread" not in legacy.structure          # -> single long leg builder
    with pytest.raises(ValueError) as exc:
        user_directed_idea(a)
    assert "naked call" in str(exc.value)
    assert "long call" in str(exc.value)             # the permitted set is named


def test_bypass1_differential_permitted_identical_banned_refused():
    """THE DIFFERENTIAL. Over the full cross-product of structures x --right x every other flag
    that reaches the constructor: an accepted idea must be FIELD-FOR-FIELD identical to the
    pre-fix construction, and only banned/contradictory pairs may change."""
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
                                assert asdict(got) == asdict(legacy)   # byte-identical idea
    # 38 structures x 4 --right spellings x 5 dte x 3 delta x 3 conviction x 2 tp/stop x 2 ticker
    # = 27,360 cases. Accepted = 180 flag combinations x 32 (structure, --right) pairs:
    #   10 right-naming permitted structures  -> consistent with 2 of the 4 --right spellings = 20
    #    2 right-agnostic ("long option", "debit spread") -> all 4                            =  8
    #    1 EMPTY --structure -> falls back to the long call/long put default, all 4            =  4
    assert (cases, accepted, refused) == (27360, 5760, 21600), (cases, accepted, refused)


def test_bypass1_default_structure_path_is_untouched():
    """No --structure at all: the historical default, unchanged."""
    assert asdict(user_directed_idea(_args("", "C"))) == asdict(_legacy_user_directed_idea(_args("", "C")))
    assert user_directed_idea(_args("", "C")).structure == "long call"
    assert user_directed_idea(_args("", "P")).structure == "long put"


def test_bypass1_right_and_structure_must_agree():
    """--right P --structure "long call" used to build a long PUT labelled "long call"."""
    legacy = _legacy_user_directed_idea(_args("long call", "P"))
    assert legacy.direction == "bearish" and legacy.structure == "long call"   # the corruption
    with pytest.raises(ValueError) as exc:
        user_directed_idea(_args("long call", "P"))
    assert "CONTRADICTION" in str(exc.value)


def test_bypass1_cli_fails_before_any_ibkr_connection():
    """The refusal is wired into __main__ ahead of asyncio.run(run(args)), so a bad --structure
    costs no gateway connect, no slate lock and no Slack post."""
    text = (REPO / "daily_recommend.py").read_text()
    main_block = text.split('if __name__ == "__main__":')[1]
    assert main_block.index("user_directed_idea(_args)") < main_block.index("asyncio.run(run(")


# --------------------------------------------------------------------------------------------
# 3. BYPASS 2 -- trader._drain_reload_ideas (reload tickets)
# --------------------------------------------------------------------------------------------

def _trader(tmp_path):
    ibc = MagicMock()
    ibc.ib = MagicMock()
    ibc.ib.placeOrder.return_value.orderStatus.status = "Filled"
    ibc.ib.placeOrder.return_value.orderStatus.avgFillPrice = 1.20
    ibc.ib.placeOrder.return_value.fills = []
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
    """The reload mapping only ever emits "debit spread" / "long option" -- both permitted, and
    neither names an option right -- so the gate changes NOTHING that is correct today."""
    _fake_queue(monkeypatch, TICKETS)
    ideas = _trader(tmp_path)._drain_reload_ideas("2026-07-26")
    assert [i.structure for i in ideas] == ["debit spread", "long option"]
    assert [i.direction for i in ideas] == ["bullish", "bearish"]
    assert all(getattr(i, "is_reload", False) for i in ideas)
    assert all(debit_structure_ok(i)[0] for i in ideas)


def test_bypass2_a_regressed_mapping_is_dropped_not_shipped(tmp_path, monkeypatch):
    """Simulate the failure this gate exists for: a future edit makes the mapping emit a
    short-premium name. The ticket must be DROPPED and audited, never rewritten to a default and
    never proposed. Without the gate the drain would hand back a "naked call" idea."""
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


# --------------------------------------------------------------------------------------------
# 4. BYPASS 3 -- daily_recommend Slack approval override
# --------------------------------------------------------------------------------------------

def _legacy_override(idea, ovr):
    """The override EXACTLY as it stood at daily_recommend.py:1062-1067 before this fix."""
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
    """THE DIFFERENTIAL for the override path. For every consistent starting idea x every override
    the Slack parser can emit, the resulting DIRECTION (which is what the constructor reads the
    option right from) and the single-vs-spread SHAPE (which is what selects the builder) must be
    identical to the pre-fix result. The only permitted difference is the structure LABEL, and
    only where the pre-fix label provably contradicted its own direction."""
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
                # the order that gets built is bit-for-bit the same trade
                assert got.direction == legacy.direction
                assert ("spread" in got.structure.lower()) == ("spread" in legacy.structure.lower())
                assert asdict(replace(got, structure="")) == asdict(replace(legacy, structure=""))
                if note:
                    relabelled += 1
                    # the legacy label was WRONG -- it named the right the legacy direction did
                    # not build. The new one is consistent.
                    assert not _consistent(legacy.structure, legacy.direction)
                    assert _consistent(got.structure, got.direction)
                    assert got.structure in DEBIT_STRUCTURES
                else:
                    identical += 1
                    assert got.structure == legacy.structure     # byte-identical
    # 14 consistent starting ideas (10 right-naming structures x 1 direction each, plus
    # "long option"/"debit spread" x 2) x 10 overrides = 140. The 20 relabels are the 10
    # right-naming structures x the 2 overrides that change the direction without naming a
    # structure ({'direction': <the opposite>} and {'direction': 'flip'}).
    assert (cases, relabelled, identical) == (140, 20, 120), (cases, relabelled, identical)


def test_bypass3_flip_used_to_mislabel_and_now_relabels_loudly():
    """A bare "flip" reply rewrote the direction and LEFT the structure alone. The trade built was
    a put vertical; the journal said bull call spread. The contract is unchanged by the fix -- only
    the name is corrected, and the correction is returned so it is audited and posted."""
    idea = _idea("bull call spread", "bullish")
    legacy = _legacy_override(idea, {"direction": "flip"})
    assert legacy.direction == "bearish" and legacy.structure == "bull call spread"  # corruption
    got, err, note = apply_structure_override(idea, {"direction": "flip"})
    assert not err
    assert got.direction == "bearish" and got.structure == "put debit spread"
    assert "relabelled" in note and "bull call spread" in note
    assert ("spread" in got.structure) == ("spread" in legacy.structure)   # same builder


def test_bypass3_flip_on_a_single_leg_keeps_it_single():
    got, err, note = apply_structure_override(_idea("long call", "bullish"), {"direction": "flip"})
    assert not err and got.structure == "long put" and got.direction == "bearish" and note


def test_bypass3_inherited_contradiction_is_refused():
    """No direction override to resolve it: the idea arrived contradicting itself, so there is
    nothing to relabel FROM and it is refused."""
    got, err, note = apply_structure_override(_idea("bull call spread", "bearish"), {})
    assert err and "CONTRADICTION" in err and not note


def test_bypass3_banned_structure_is_never_laundered_by_a_relabel():
    """REGRESSION (found by this test during the build): the relabel must only ever rewrite a
    structure that is ALREADY permitted. Relabelling an off-list one turns ("naked call", bullish)
    + a bare "flip" reply into ("long put", bearish), which then passes the gate -- laundering a
    refused string into a permitted one. A banned structure must reach the gate untouched,
    whatever the human replied."""
    for ovr in ({}, {"direction": "flip"}, {"direction": "bearish"}, {"direction": "bullish"}):
        got, err, note = apply_structure_override(_idea("naked call", "bullish"), ovr)
        assert err and "naked call" in err, ovr
        assert not note, ovr
        assert got.structure == "naked call", ovr        # never rewritten
    # ... and a structure override cannot rescue it either: the incoming idea is checked FIRST.
    for ovr in ({"structure": "single"}, {"structure": "spread"},
                {"direction": "flip", "structure": "single"}):
        got, err, note = apply_structure_override(_idea("naked call", "bullish"), ovr)
        assert err and "naked call" in err and got.structure == "naked call", ovr


def test_bypass3_vocabulary_itself_was_never_the_hole():
    """approval.parse_structure_override() only emits 'single'/'spread', and both map onto
    permitted structures. Documented so a future reader does not re-fix the wrong half."""
    from exitmgr import approval
    for text in ("make it a spread", "single", "just the long call", "flip it", "go bearish",
                 "no spread", "vertical", "puts"):
        ovr = approval.parse_structure_override(text)
        assert set(ovr) <= {"structure", "direction"}
        assert ovr.get("structure") in (None, "single", "spread")


# --------------------------------------------------------------------------------------------
# 5. SUBMIT-TIME GATE -- the debit mirror of the credit invariant re-check
# --------------------------------------------------------------------------------------------

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
    await t._submit_order(_submittable(t, structure="long call"))
    t.ib_conn.ib.placeOrder.assert_called_once()


@pytest.mark.asyncio
async def test_submit_allows_an_unlabelled_order(tmp_path):
    """"" means no structure was declared at all (place_trade.py builds an order with no TradeIdea
    behind it). That is not an allow-list failure and must stay submittable."""
    t = _trader(tmp_path)
    await t._submit_order(_submittable(t))
    t.ib_conn.ib.placeOrder.assert_called_once()


@pytest.mark.asyncio
async def test_submit_refuses_a_banned_structure_at_the_money_boundary(tmp_path):
    """The catch-all: whatever route builds the idea, nothing banned reaches placeOrder."""
    t = _trader(tmp_path)
    for s in ("naked call", "short strangle", "iron condor"):
        with pytest.raises(RuntimeError) as exc:
            await t._submit_order(_submittable(t, structure=s))
        assert "STRUCTURE REFUSED at submit" in str(exc.value) and s in str(exc.value)
    t.ib_conn.ib.placeOrder.assert_not_called()
    assert not (tmp_path / "trades.log").exists()      # nothing journalled


@pytest.mark.asyncio
async def test_submit_refuses_a_structure_that_names_the_wrong_right(tmp_path):
    t = _trader(tmp_path)
    with pytest.raises(RuntimeError) as exc:
        await t._submit_order(_submittable(t, structure="long put"))   # order right is "C"
    assert "names a put" in str(exc.value)
    t.ib_conn.ib.placeOrder.assert_not_called()


def test_resolved_order_structure_field_is_additive():
    """Positional construction -- every existing call site, including place_trade.py -- is
    unaffected, and the field defaults to ""."""
    assert ResolvedOrder("SPY", "C", "20260620", 610.0, 1, 1.20, object()).structure == ""


# --------------------------------------------------------------------------------------------
# 6. CONSTRUCTOR GATES -- nothing banned survives to a qualified contract
# --------------------------------------------------------------------------------------------

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


# --------------------------------------------------------------------------------------------
# 7. THE ORDER MUST CARRY THE STRUCTURE
#    -- otherwise the submit-time gate above is dead code that can never see anything.
# --------------------------------------------------------------------------------------------

def _wire_debit_chain(t, monkeypatch, *, spot=100.0, quotes=((100.0, 2.90, 3.10, 0.60),
                                                             (105.0, 0.95, 1.05, 0.35))):
    """Drive the REAL debit resolver: one chain, quoted strikes, greeks on the delta band."""
    import importlib
    import sys
    ibkr = importlib.import_module("exitmgr.ibkr")
    monkeypatch.setitem(sys.modules, "exitmgr.ibkr", ibkr)   # survive conftest's sys.modules patch
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
    assert r.structure == "long call"        # the submit-time gate has something to check


@pytest.mark.asyncio
async def test_resolve_order_carries_the_structure_onto_a_spread(tmp_path, monkeypatch):
    t = _trader(tmp_path)
    _wire_debit_chain(t, monkeypatch)
    r = await t._resolve_order(_idea("call debit spread"), per_trade_cap=5000.0)
    assert r is not None and r.short_contract is not None
    assert r.structure == "call debit spread"


@pytest.mark.asyncio
async def test_daily_recommend_resolve_carries_the_structure_onto_the_order(monkeypatch):
    """daily_recommend has its OWN constructor and its OWN placeOrder; its orders must carry the
    structure too, or its submit-time gate is dead code."""
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
    ib.qualifyContractsAsync = AsyncMock(
        side_effect=[[MagicMock(conId=7)], [tk.contract for tk in tickers]])
    ib.reqSecDefOptParamsAsync = AsyncMock(return_value=[chain])
    ib.reqTickersAsync = AsyncMock(return_value=tickers)
    r, why = await daily_recommend._resolve(ib, _idea("long call"), 5000.0)
    assert r is not None, why
    assert r.structure == "long call"


def test_daily_recommend_submit_gate():
    """The slate path has its own placeOrder, so it has its own money-boundary gate."""
    ok_order = ResolvedOrder("SPY", "C", "20260620", 610.0, 1, 1.20, object(), structure="long call")
    assert submit_structure_ok(ok_order, _idea("long call")) == (True, "")
    # an order with no structure declared at all is not an allow-list failure
    assert submit_structure_ok(ResolvedOrder("SPY", "C", "20260620", 610.0, 1, 1.2, object()),
                               _idea("long call")) == (True, "")
    # a banned structure that somehow survived to here
    ok, why = submit_structure_ok(
        ResolvedOrder("SPY", "C", "20260620", 610.0, 1, 1.2, object(), structure="naked call"),
        _idea("naked call"))
    assert not ok and "naked call" in why
    # the resolved order buys the right the structure does not name
    ok, why = submit_structure_ok(
        ResolvedOrder("SPY", "P", "20260620", 610.0, 1, 1.2, object(), structure="long call"),
        _idea("long call"))
    assert not ok and "names a call" in why


def test_daily_recommend_submit_gate_is_wired_in_ahead_of_place_order():
    """WIRING assertion, the same shape as the pre-connection one above. submit_structure_ok() is
    fully unit-tested, but a gate that is computed and then not acted on is not a gate -- and that
    call sits inside run()'s 700-line approval loop, which no unit test can drive. Assert the
    source fact instead: the call, its guard, and then placeOrder, in that order."""
    text = (REPO / "daily_recommend.py").read_text()
    call = text.index("_ok_submit, _why_submit = submit_structure_ok(r, effective_idea)")
    guard = text.index("if not _ok_submit:", call)
    place = text.index("ib.placeOrder(", call)
    assert call < guard < place
    assert guard - call < 200, "the guard must immediately follow the call"


# --------------------------------------------------------------------------------------------
# 8. ENTRY LOOP -- enforcement point #1, isolated (the resolver is mocked out, so only the
#    top-of-loop gate can stop this)
# --------------------------------------------------------------------------------------------

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
    """SPY is an INDEX name, so the single-name/sector caps do not bind and the risk gate lets a
    permitted idea through -- which is what makes the control test below meaningful."""
    return TradeIdea(underlying="SPY", is_index=True, direction=direction, structure=structure,
                     target_dte=30, target_delta=0.6, est_debit_usd=90.0, conviction=6,
                     thesis="trend")


async def _run_entry_loop(t, monkeypatch, idea):
    monkeypatch.setattr(trader_mod, "propose", lambda *a, **k: [idea])
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
    """Enforcement point #1: a banned structure dies at the TOP of the entry loop -- the resolver
    is never even called, and the refusal is audited and posted. Mirrors what credit_structure_ok()
    does three lines above it."""
    t = _trader(tmp_path)
    await _run_entry_loop(t, monkeypatch, _spy_idea("naked call"))
    t._resolve_order.assert_not_awaited()
    t._submit_order.assert_not_awaited()
    events = [json.loads(l) for l in Path(t.audit_path).read_text().splitlines()]
    rej = [e for e in events if e.get("event") == "debit_structure_rejected"]
    assert len(rej) == 1 and "naked call" in rej[0]["reason"]
    assert any("REFUSED" in m and "naked call" in m for m in hermetic_entry_loop)
    # and it never reached the risk gate, which is what logs "gated"
    assert not [e for e in events if e.get("event") == "gated"]


@pytest.mark.asyncio
async def test_entry_loop_lets_a_permitted_structure_through_to_the_resolver(
        tmp_path, monkeypatch, hermetic_entry_loop):
    """The control: an identical idea with a permitted structure still reaches construction."""
    t = _trader(tmp_path)
    await _run_entry_loop(t, monkeypatch, _spy_idea("long call"))
    t._resolve_order.assert_awaited()
    events = [json.loads(l) for l in Path(t.audit_path).read_text().splitlines()]
    assert not [e for e in events if e.get("event") == "debit_structure_rejected"]
