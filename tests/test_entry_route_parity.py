"""Public API contract; production-derived narrative omitted."""

import ast
import inspect
import os
from types import SimpleNamespace

import pytest

import daily_recommend as dr
import place_trade as pt
from exitmgr import construction, entry_safety
from exitmgr import trader as trader_mod
from exitmgr.trader import Trader, autonomous_entry_blockers
from exitmgr.risk import RiskLimits

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel):
    with open(os.path.join(REPO, rel)) as f:
        return f.read()


def _intent(**kw):
    base = dict(underlying="SPY", side="debit", intended_hold_days=10,
                target_dte=17, target_delta=0.35)
    base.update(kw)
    return SimpleNamespace(**base)


def test_model_authored_routes_reconsider_new_same_day_earnings_before_terms_or_submit():
    trader_src = inspect.getsource(Trader._run_once)
    daily_src = _src("daily_recommend.py")
    assert trader_src.index("reconsider_new_same_day_earnings(") < trader_src.index(
        "credit_material_changes(resolved, fresh)")
    assert daily_src.index("await reconsider_new_same_day_earnings(") < daily_src.index(
        "credit_material_changes(r, fresh_r)")
    assert "force_refresh=True" in inspect.getsource(Trader._refresh_approved_entry)
    assert daily_src.count("force_refresh=True") >= 2
    assert "fresh_r.earnings_reconsideration_order_snapshot = getattr(" in daily_src
    assert daily_src.index("earnings_reconsideration_receipt_valid(fresh_r)") < daily_src.rindex(
        "r = fresh_r")






def test_both_routes_call_the_one_transform_and_neither_calls_the_primitive():
    """Public API contract; production-derived narrative omitted."""
    trader_src = inspect.getsource(Trader._run_once)
    daily_src = inspect.getsource(dr._materialize_stage_b)
    for name, src in (("trader.run_once", trader_src),
                      ("daily._materialize_stage_b", daily_src)):
        assert "apply_construction_policy" in src, f"{name} does not run the shared transform"
        assert "apply_deterministic_construction" not in src, (
            f"{name} calls the fail-open primitive directly")


def test_an_exception_in_the_transform_drops_the_idea_rather_than_shipping_the_model_expiry(
        monkeypatch):
    def _boom(idea, min_dte, enabled=True):
        raise RuntimeError("frozen carrier")

    monkeypatch.setattr(construction, "apply_deterministic_construction", _boom)
    out = construction.apply_construction_policy([_intent()], min_dte=25, enabled=True)
    assert out.ideas == ()
    assert len(out.dropped) == 1
    assert "raised" in out.dropped[0][1]


def test_a_transform_that_silently_does_not_take_also_drops_the_idea(monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    monkeypatch.setattr(construction, "apply_deterministic_construction",
                        lambda idea, min_dte, enabled=True: idea)
    idea = _intent()
    out = construction.apply_construction_policy([idea], min_dte=25, enabled=True)
    assert out.ideas == ()
    assert "did not take" in out.dropped[0][1]
    assert idea.target_dte == 17, "the model's number is still there -- it just cannot be ordered"


def test_an_unusable_min_dte_is_a_refusal_while_the_rule_is_enabled():
    out = construction.apply_construction_policy([_intent()], min_dte="soon", enabled=True)
    assert out.ideas == () and "min_dte" in out.dropped[0][1]


def test_the_rule_applied_cleanly_rewrites_expiry_and_delta_and_reports_the_change():
    idea = _intent(intended_hold_days=10, target_dte=17, target_delta=0.35)
    out = construction.apply_construction_policy([idea], min_dte=25, enabled=True)
    assert out.ideas == (idea,) and not out.dropped
    assert idea.target_dte == 80 == 10 * construction.DOCTRINE_HOLD_MULTIPLE
    assert idea.target_delta == construction.DOCTRINE_LONG_DELTA
    assert out.changes[0]["model_target_dte"] == 17 and out.changes[0]["rule_target_dte"] == 80


def test_disabled_is_a_documented_pass_through_not_a_refusal():
    idea = _intent()
    out = construction.apply_construction_policy([idea], min_dte=25, enabled=False)
    assert out.ideas == (idea,) and not out.dropped and idea.target_dte == 17


def test_a_credit_idea_is_untouched_and_never_dropped():
    """Public API contract; production-derived narrative omitted."""
    idea = _intent(side="credit", target_dte=17)
    out = construction.apply_construction_policy([idea], min_dte=25, enabled=True)
    assert out.ideas == (idea,) and not out.dropped and idea.target_dte == 17


@pytest.mark.asyncio
async def test_the_slate_route_builds_no_candidate_at_all_when_the_transform_fails(monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    built = []

    async def _never(*a, **k):
        built.append(a)
        return []

    monkeypatch.setattr(dr, "CONS", SimpleNamespace(
        min_dte=25, deterministic_construction=True, stage_b_quote_max_age_s=45.0))
    monkeypatch.setattr(dr, "CASH_BUFFER_PCT", 0.05)
    monkeypatch.setattr(dr, "build_entry_candidates", _never)
    monkeypatch.setattr(construction, "apply_deterministic_construction",
                        lambda idea, min_dte, enabled=True: (_ for _ in ()).throw(
                            RuntimeError("frozen carrier")))

    ideas = await dr._materialize_stage_b(
        object(), [_intent()], SimpleNamespace(net_liq=100_000.0, available_funds=50_000.0),
        {"llm_endpoint": "http://x", "llm_model": "m"}, os.devnull)
    assert ideas == [] and built == []






def test_the_freshness_kernel_reads_the_configured_window():
    assert construction.stage_b_quote_max_age_s(SimpleNamespace(stage_b_quote_max_age_s=45.0)) \
        == 45.0


@pytest.mark.parametrize("bad", [0, 0.0, None, "", "soon", float("nan"), float("inf"), -1.0])
def test_an_unusable_window_falls_back_to_the_documented_default(bad):
    """Public API contract; production-derived narrative omitted."""
    got = construction.stage_b_quote_max_age_s(SimpleNamespace(stage_b_quote_max_age_s=bad))
    assert got == entry_safety.DEFAULT_NBBO_MAX_AGE_SECONDS


def test_the_live_config_window_is_the_build_window_not_the_submission_window():
    from exitmgr.config import load_config
    cons = load_config(os.path.join(REPO, "config.yaml")).construction
    assert construction.stage_b_quote_max_age_s(cons) == cons.stage_b_quote_max_age_s
    assert construction.stage_b_quote_max_age_s(cons) > entry_safety.DEFAULT_NBBO_MAX_AGE_SECONDS


def test_neither_route_hardcodes_the_submission_constant_as_a_prefilter_bound():
    for rel in ("daily_recommend.py", "exitmgr/trader.py"):
        src = _src(rel)
        assert "max_age_seconds=entry_safety.DEFAULT_NBBO_MAX_AGE_SECONDS" not in src, rel
    assert "stage_b_quote_max_age_s(" in _src("daily_recommend.py")


@pytest.mark.asyncio
async def test_the_slate_prefilters_on_the_configured_window(monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    seen = {}

    async def _build(*a, **k):
        return [object(), object(), object()]

    def _filter(bindings, max_age_seconds=None):
        seen["max_age_seconds"] = max_age_seconds
        return []

    monkeypatch.setattr(dr, "CONS", SimpleNamespace(
        min_dte=25, deterministic_construction=True, stage_b_quote_max_age_s=45.0))
    monkeypatch.setattr(dr, "CASH_BUFFER_PCT", 0.05)
    monkeypatch.setattr(dr, "build_entry_candidates", _build)
    monkeypatch.setattr(dr, "bindings_for_stage_b", _filter)

    await dr._materialize_stage_b(
        object(), [_intent()], SimpleNamespace(net_liq=100_000.0, available_funds=50_000.0),
        {"llm_endpoint": "http://x", "llm_model": "m"}, os.devnull)
    assert seen["max_age_seconds"] == 45.0, (
        "the slate must prefilter on the configured build window, not the 10s fill window")






_CONTRACT_KWARGS = {"thinking", "return_cot", "return_identity", "return_raw"}
_MODEL_CALLS = {"propose_intents", "select_candidate"}


def _model_call_sites(rel):
    tree = ast.parse(_src(rel))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = getattr(fn, "id", None) or getattr(fn, "attr", None)
        if name in _MODEL_CALLS:
            yield rel, name, node

        elif name in {"to_thread", "_await_slate_model"} and node.args:
            first = getattr(node.args[0], "id", None) or getattr(node.args[0], "attr", None)
            if first in _MODEL_CALLS:
                yield rel, first, node


def test_no_strategist_call_site_spells_its_own_request_out():
    """Public API contract; production-derived narrative omitted."""
    offenders = []
    starred = []
    for rel, name, node in list(_model_call_sites("exitmgr/trader.py")) + \
            list(_model_call_sites("daily_recommend.py")):
        kwds = {k.arg for k in node.keywords if k.arg}
        bad = kwds & _CONTRACT_KWARGS
        if bad:
            offenders.append((rel, name, sorted(bad), node.lineno))
        if any(k.arg is None for k in node.keywords):
            starred.append((rel, name, node.lineno))
    assert not offenders, (
        "these call sites build their own model request instead of using a bound contract "
        "(trader.STAGE_A_REQUEST_* / STAGE_B_REQUEST): %r" % (offenders,))
    assert len(starred) >= 5, (
        "expected every strategist call site to splat a bound contract; found %r" % (starred,))


def test_stage_b_asks_for_thinking_on_and_for_its_reasoning():
    assert trader_mod.STAGE_B_REQUEST["thinking"] == "enabled"
    assert trader_mod.STAGE_B_REQUEST["return_cot"] is True
    assert trader_mod.STAGE_B_REQUEST["return_raw"] is True
    assert trader_mod.STAGE_B_REQUEST["timeout"] == 600


def test_the_two_stage_a_contracts_keep_their_own_measured_settings():
    """Public API contract; production-derived narrative omitted."""
    assert trader_mod.STAGE_A_REQUEST_CONTINUOUS["thinking"] == "disabled"
    assert trader_mod.STAGE_A_REQUEST_SLATE["thinking"] == "enabled"
    for c in (trader_mod.STAGE_A_REQUEST_CONTINUOUS, trader_mod.STAGE_A_REQUEST_SLATE):
        assert c["return_cot"] is True and c["return_identity"] is True

    assert set(trader_mod.STAGE_A_REQUEST_DIRECTED) == {"thinking"}


@pytest.mark.asyncio
async def test_the_slate_stage_b_now_asks_for_thinking(monkeypatch):
    seen = {}

    async def _build(*a, **k):
        return [object(), object(), object()]

    def _filter(bindings, max_age_seconds=None):
        return [SimpleNamespace(candidate=SimpleNamespace(candidate_id="c%d" % i))
                for i in range(3)]

    def _select(endpoint, model, intent, candidates, **kw):
        seen.update(kw)
        return None

    monkeypatch.setattr(dr, "CONS", SimpleNamespace(
        min_dte=25, deterministic_construction=True, stage_b_quote_max_age_s=45.0))
    monkeypatch.setattr(dr, "CASH_BUFFER_PCT", 0.05)
    monkeypatch.setattr(dr, "build_entry_candidates", _build)
    monkeypatch.setattr(dr, "bindings_for_stage_b", _filter)
    monkeypatch.setattr(dr, "select_candidate", _select)

    await dr._materialize_stage_b(
        object(), [_intent()], SimpleNamespace(net_liq=100_000.0, available_funds=50_000.0),
        {"llm_endpoint": "http://x", "llm_model": "m"}, os.devnull)
    assert seen.get("thinking") == "enabled", seen
    assert seen.get("return_cot") is True and seen.get("return_identity") is True
    assert seen.get("timeout") == 600, "daily Stage B must share the foreground thinking budget"


def test_daily_reasoning_budgets_cover_existing_token_ceilings():
    """Public API contract; production-derived narrative omitted."""
    tree = ast.parse(_src("daily_recommend.py"))
    seen = {"propose_intents": [], "discover_names": []}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None)
        if (getattr(node.func, "attr", None) == "to_thread" or name == "_await_slate_model") and node.args:
            name = getattr(node.args[0], "id", None)
        if name in seen:
            values = [keyword.value.value for keyword in node.keywords
                      if keyword.arg == "timeout" and isinstance(keyword.value, ast.Constant)]
            seen[name].extend(values)
    assert seen == {"propose_intents": [1200, 1200, 1200], "discover_names": [600, 600]}
    from exitmgr.strategist import propose_intents, discover_names
    assert inspect.signature(propose_intents).parameters["timeout"].default == 300
    assert inspect.signature(discover_names).parameters["timeout"].default == 600






def test_the_directed_lane_no_longer_lets_the_model_overwrite_the_hold():
    """Public API contract; production-derived narrative omitted."""
    src = _src("daily_recommend.py")
    assert "_ud_idea.intended_hold_days = _mi_hold" not in src, (
        "the model's hold is being written back onto a user-directed idea")
    assert '_ud_idea._intended_hold_days_source = "model"' not in src, (
        "a user-directed hold is being relabelled as the model's")
    assert "_ud_idea._model_intended_hold_days = _mi_hold" in src, (
        "the model's suggestion must still be RECORDED, just not substituted")
    assert '"model_intended_hold_days"' in src, "the journal must carry the suggestion"


def test_the_journal_records_the_suggestion_beside_the_human_value_on_both_templates():
    src = _src("daily_recommend.py")
    assert src.count('"model_intended_hold_days": positive_hold_days(') == 2
    assert src.count('"intended_hold_days_source": getattr(') == 2


def test_a_stated_hold_survives_construction_and_drives_the_doctrine_floor():
    """Public API contract; production-derived narrative omitted."""
    args = SimpleNamespace(ticker="spy", right="C", structure=None, dte=300, delta=0.6,
                           conviction=7, thesis="mine", tp=0.0, stop=0.0, hold_days=10)
    idea = dr.user_directed_idea(args)
    assert idea.intended_hold_days == 10
    assert idea._intended_hold_days_source == "explicit"
    mine = dr.doctrine_stamp(idea.intended_hold_days, credit=False)
    theirs = dr.doctrine_stamp(30, credit=False)
    assert mine["doctrine_dte_floor"] != theirs["doctrine_dte_floor"], (
        "if the two holds graded identically this finding would not matter")






def _limits(**kw):
    kw.setdefault("sector_map", {"NVDA": "semis", "AMD": "semis"})
    return RiskLimits(**kw)


def test_an_approved_and_classified_name_may_auto_submit():
    assert autonomous_entry_blockers("NVDA", approved_names={"NVDA"}, limits=_limits()) == ()


def test_an_unclassified_name_may_be_proposed_but_not_auto_submitted():
    """Public API contract; production-derived narrative omitted."""
    why = autonomous_entry_blockers("SYMR", approved_names={"SYMR"}, limits=_limits())
    assert why and any("sector_map" in r for r in why)


def test_a_name_the_model_invented_may_be_proposed_but_not_auto_submitted():
    """Public API contract; production-derived narrative omitted."""
    why = autonomous_entry_blockers(
        "ZZZZ", approved_names={"NVDA"}, limits=_limits(allow_any_name=True))
    assert why and any("approved_names" in r for r in why)


def test_an_index_underlying_is_exempt_because_the_sector_gate_excludes_it_by_design():
    assert autonomous_entry_blockers("SPY", approved_names=set(), limits=_limits()) == ()


def test_an_empty_sector_map_withholds_autonomy_from_every_single_name():
    """Public API contract; production-derived narrative omitted."""
    why = autonomous_entry_blockers("NVDA", approved_names={"NVDA"},
                                    limits=RiskLimits(sector_map={}))
    assert why and any("sector_map" in r for r in why)


def test_the_auto_approve_conjunction_actually_binds_the_blockers():
    src = inspect.getsource(Trader._run_once)
    assert "_auto_blockers = autonomous_entry_blockers(" in src
    assert "_auto_authority_gate = autonomous_execution_gate(" in src
    assert "blockers=_auto_blockers" in src, "the blockers are computed but not bound"
    assert "_auto_ok = _auto_authority_gate.allowed" in src
    assert "auto_approve_withheld" in src, "a withheld autonomy must be auditable"






def test_degraded_research_withholds_autonomy_for_any_symbol():
    assert autonomous_entry_blockers(
        "NVDA", approved_names={"NVDA"}, limits=_limits(),
        research_degraded="yfinance timeout") != ()
    assert autonomous_entry_blockers(
        "SPY", approved_names=set(), limits=_limits(),
        research_degraded="yfinance timeout") != ()


@pytest.mark.asyncio
async def test_a_research_failure_sets_the_flag_and_a_full_brief_clears_it(monkeypatch, tmp_path):
    from unittest.mock import MagicMock
    from exitmgr import market as market_mod

    async def _quotes(ib, names):
        return {n: {"last": 100.0} for n in names}

    monkeypatch.setattr(market_mod, "fetch_universe_quotes", _quotes)
    monkeypatch.setattr(market_mod, "format_context",
                        lambda *a, **k: "QUOTES ONLY")
    monkeypatch.setattr(trader_mod.research, "with_account_sizing_snapshot",
                        lambda text, **k: text)

    ibc = MagicMock()
    ibc.ib = MagicMock()
    t = Trader(ib_conn=ibc, exit_manager=MagicMock(), limits=RiskLimits(),
               approved_names={"NVDA"}, endpoint="http://x", model="m", slack_token="t",
               slack_channel="C", approver_ids=set(),
               baseline_path=str(tmp_path / "b.json"), audit_path=str(tmp_path / "a.jsonl"),
               journal_path=str(tmp_path / "trades.log"))
    assert t._research_degraded is None

    async def _boom(*a, **k):
        raise RuntimeError("yfinance timeout")

    monkeypatch.setattr(trader_mod.research, "gather", _boom)
    got = await t._market_context([], None)
    assert got == "QUOTES ONLY"
    assert t._research_degraded and "yfinance timeout" in t._research_degraded
    assert autonomous_entry_blockers("NVDA", approved_names={"NVDA"}, limits=_limits(),
                                     research_degraded=t._research_degraded) != ()

    async def _ok(*a, **k):
        return {"price_stats": {}, "vix": 15.0}

    monkeypatch.setattr(trader_mod.research, "gather", _ok)
    monkeypatch.setattr(trader_mod.research, "build_brief", lambda **k: "FULL BRIEF")
    monkeypatch.setattr(trader_mod.regime, "classify_regime", lambda *a, **k: {})
    assert await t._market_context([], None) == "FULL BRIEF"
    assert t._research_degraded is None, "degradation must not latch past a recovered brief"






def test_every_route_feeds_the_daily_caps_into_its_admission_dimensions():
    for rel in ("daily_recommend.py", "place_trade.py", "exitmgr/trader.py"):
        src = _src(rel)
        assert "max_orders_per_day" in src and "max_notional_per_day" in src, rel


def test_every_entry_route_binds_campaign_conflict_quarantine():
    trader_src = _src("exitmgr/trader.py")
    daily_src = _src("daily_recommend.py")
    manual_src = _src("place_trade.py")
    ledger_src = _src("exitmgr/entry_reservation.py")
    assert "campaign_conflict_symbols=self.exit_manager.campaign_conflict_symbols()" in trader_src
    assert "active_campaign_conflict_symbols(self.journal_path)" in trader_src
    assert "active_campaign_conflict_symbols(JOURNAL_PATH)" in daily_src
    assert "active_campaign_conflict_symbols(journal_path)" in manual_src
    assert 'n["symbol"] in n["campaign_conflict_symbols"]' in ledger_src


def test_every_route_refuses_rather_than_reading_the_counters_as_zero():
    for rel in ("daily_recommend.py", "place_trade.py"):
        src = _src(rel)
        assert "except EntryThrottleUnreadable" in src, rel
    assert "except EntryThrottleUnreadable" in inspect.getsource(Trader._run_once)
    assert "except Exception:\n            return 0, 0.0" not in \
        inspect.getsource(Trader._day_open_counts_now)






def _cons(**kw):
    base = dict(earnings_blackout_enabled=True, earnings_blackout_days=0,
                earnings_use_hold_window=True, earnings_hold_slip_mult=2.0)
    base.update(kw)
    return SimpleNamespace(**base)


def test_the_hold_is_what_makes_the_advisory_warn_or_clear():
    """Public API contract; production-derived narrative omitted."""
    from datetime import date
    entry, expiry, earnings = date(2026, 8, 22), date(2027, 6, 17), date(2026, 11, 5)
    without, why = construction.earnings_ok(entry, expiry, earnings, _cons())
    assert without and "expiry" in why
    with_hold, hold_note = construction.earnings_ok(
        entry, expiry, earnings, _cons(), hold_days=10)
    assert with_hold and hold_note == "", (
        "a 10-day hold cannot overlap an earnings print 75 days out")


def test_no_entry_route_calls_the_earnings_gate_without_a_hold():
    calls = 0
    for rel in ("daily_recommend.py", "place_trade.py", "exitmgr/trader.py"):
        tree = ast.parse(_src(rel))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "attr", None) != "earnings_ok":
                continue
            calls += 1
            assert "hold_days" in {k.arg for k in node.keywords}, (
                "%s:%d calls earnings_ok without hold_days" % (rel, node.lineno))
    assert calls >= 4, "expected every route's earnings gate to be covered, found %d" % calls


def test_the_manual_route_states_a_hold_and_never_derives_one():
    """Public API contract; production-derived narrative omitted."""
    assert pt._positive_hold_days(None) is None
    assert pt._positive_hold_days(0) is None
    assert pt._positive_hold_days(True) is None
    assert pt._positive_hold_days("12") == 12
    src = _src("place_trade.py")
    assert "--hold-days" in src
    assert "ceil(" not in src and "math.ceil" not in src, (
        "a hold derived from the expiry is a number nobody chose")


def test_manual_route_persists_final_earnings_context_in_fill_template():
    src = _src("place_trade.py")
    for assignment in (
        "fresh.earnings_date = earnings_date.isoformat()",
        "fresh.earnings_unchecked = True",
        "fresh.earnings_warn = earnings_reason",
        "fresh.earnings_day_warning = (earnings_date == entry_date)",
    ):
        assert assignment in src
    for field in (
        '\"earnings_date\": getattr(fresh, \"earnings_date\", None)',
        '\"earnings_unchecked\": bool(',
        '\"earnings_overlap_warning\": (',
        '\"earnings_day_warning\": bool(',
    ):
        assert field in src


def test_every_entry_route_persists_the_same_day_warning_flag():
    for rel in ("daily_recommend.py", "place_trade.py", "exitmgr/trader.py"):
        src = _src(rel)
        assert "earnings_day_warning" in src
        assert "earnings_same_day_warning" in src
        assert "EARNINGS TODAY — ENTRY WARNING" in src


def test_final_same_day_discovery_is_visible_before_every_entry_route_submit():
    for rel in ("daily_recommend.py", "place_trade.py", "exitmgr/trader.py"):
        src = _src(rel)
        assert "EARNINGS TODAY — FINAL ENTRY WARNING" in src
        assert "earnings_same_day_warning_notice" in src
        assert "seed_reactions=False" in src
        assert "notice_posted=bool(_notice_ts)" in src






def test_no_approval_card_claims_five_minutes():
    """Public API contract; production-derived narrative omitted."""
    for rel in ("daily_recommend.py", "place_trade.py", "exitmgr/trader.py"):
        src = _src(rel)
        for bad in ("expires in 5 minutes._", "within 5 minutes", "(5 min)"):
            assert bad not in src, "%s still shows a card that claims %r" % (rel, bad)


def test_the_stated_ttl_is_derived_from_the_enforced_one():
    assert dr._APPROVAL_TTL_MINUTES == pt._APPROVAL_TTL_MINUTES
    assert dr._APPROVAL_TTL_MINUTES == entry_safety.DEFAULT_APPROVAL_TTL_SECONDS // 60 == 30


def test_the_circuit_breaker_docstring_no_longer_states_a_number_that_is_not_live():
    import yaml
    with open(os.path.join(REPO, "config.yaml")) as f:
        halt = float((yaml.safe_load(f).get("trading") or {})["daily_halt_pct"])
    doc = inspect.getdoc(trader_mod.day_start_pot) or ""
    assert "-8%" not in doc, "the docstring cited the RiskLimits dataclass default, not the gate"
    assert ("%g" % halt) in doc, "the docstring must name the live daily_halt_pct"
    assert ("-%d%%" % round(halt * 100)) in doc
