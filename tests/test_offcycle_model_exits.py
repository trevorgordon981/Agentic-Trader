"""Public API contract; production-derived narrative omitted."""

import ast
import asyncio
import io
import os
import time
import tokenize
from pathlib import Path

import pytest

from exitmgr import manager as manager_mod
from exitmgr.config import Config, JournalConfig, StateConfig
from exitmgr.connection import PositionData
from exitmgr.manager import ExitManager

SYMC, SYMC_SHORT = 4001001, 4001002
SYMD, SYMD_SHORT = 5001002, 5001003




def _mgr(tmp_path):
    cfg = Config()
    cfg.state = StateConfig(path=os.path.join(str(tmp_path), "exitmgr_state.json"))
    cfg.journal = JournalConfig(path=os.path.join(str(tmp_path), "trades.log"))
    cfg.manage_positions = True
    cfg.llm_endpoint = "http://127.0.0.1:9/v1/chat/completions"
    cfg.llm_model = "test-model"
    mgr = ExitManager(cfg)
    mgr._journal_entries = {
        SYMC: {"symbol": "SYMC", "debit": 800.0, "quantity": 4, "stop_pct": 30.0,
               "conviction": 6, "intended_hold_days": 7, "fill_ts": "2037-08-12T14:06:31",
               "thesis": "momentum", "spread": {"short_con_id": SYMC_SHORT}},
        SYMD: {"symbol": "SYMD", "debit": 300.0, "quantity": 1, "stop_pct": 30.0,
               "conviction": 6, "intended_hold_days": 5, "fill_ts": "2037-08-12T18:11:47",
               "thesis": "20d high", "spread": {"short_con_id": SYMD_SHORT}},
    }
    return mgr


def _positions(*con_ids, qty=None):
    sizes = {SYMC: 4, SYMD: 1}
    out = []
    for cid in con_ids:
        out.append(PositionData(con_id=cid, symbol={SYMC: "SYMC", SYMD: "SYMD"}[cid], right="C",
                                quantity=(qty if qty is not None else sizes[cid]),
                                avg_cost=200.0, expiry="20300117", strike=40.0))
    return out


def _quotes():
    return {SYMC: {"price": 2.80}, SYMC_SHORT: {"price": 1.20},
            SYMD: {"price": 3.50}, SYMD_SHORT: {"price": 1.00}}


def _publish(mgr, *con_ids, qty=None):
    """Public API contract; production-derived narrative omitted."""
    positions = _positions(*con_ids, qty=qty)
    views = mgr._build_position_views(positions, _quotes())
    mgr.publish_mgmt_views(views, regime={"regime": "bull"},
                           qty_by_cid={p.con_id: p.quantity for p in positions})
    return views, {v["con_id"]: v for v in views}, {p.con_id: p.quantity for p in positions}


def _patch_model(monkeypatch, decisions, raw="{...}", identity=None, capture=None):
    def _fake(endpoint, model, positions, market_regime=None, timeout=75, return_meta=False):
        if capture is not None:
            capture.append({"positions": positions, "market_regime": market_regime,
                            "endpoint": endpoint, "model": model})
        return (dict(decisions), {"raw": raw, "model_identity": identity})
    monkeypatch.setattr(manager_mod, "assess_positions", _fake)
    return capture




def test_offcycle_assessment_caches_decisions_the_next_cycle_consumes(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path)
    _publish(mgr, SYMC)



    _patch_model(monkeypatch, {SYMC: {"action": "cut", "reason": "thesis broken"}},
                 identity={"model_id": "deepseek-v4-flash-0731"})

    assert asyncio.run(mgr.assess_positions_offcycle()) is True

    _, views_by_cid, qty_by_cid = _publish(mgr, SYMC)
    decisions, raw, identity = mgr._consume_model_decisions(views_by_cid, qty_by_cid)
    assert decisions == {SYMC: {"action": "cut", "reason": "thesis broken"}}
    assert raw == "{...}"
    assert identity == {"model_id": "deepseek-v4-flash-0731"}


def test_decisions_stay_usable_across_several_cycles_inside_the_age_bound(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    mgr = _mgr(tmp_path)
    _publish(mgr, SYMC)
    _patch_model(monkeypatch, {SYMC: {"action": "cut", "reason": "thesis broken"}})
    assert asyncio.run(mgr.assess_positions_offcycle()) is True

    for _ in range(10):
        _, views_by_cid, qty_by_cid = _publish(mgr, SYMC)
        decisions, _, _ = mgr._consume_model_decisions(views_by_cid, qty_by_cid)
        assert decisions[SYMC]["action"] == "cut"




def test_published_views_carry_the_underwriting_window(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    mgr = _mgr(tmp_path)
    _publish(mgr, SYMC, SYMD)
    seen = _patch_model(monkeypatch, {}, capture=[])

    assert asyncio.run(mgr.assess_positions_offcycle()) is True

    sent = {v["con_id"]: v for v in seen[0]["positions"]}
    assert set(sent) == {SYMC, SYMD}
    for cid, hold in ((SYMC, 7), (SYMD, 5)):
        view = sent[cid]
        assert view["intended_hold_days"] == hold
        assert view["calendar_days_elapsed"] is not None
        assert view["window_fraction"] == pytest.approx(
            round(view["calendar_days_elapsed"] / hold, 3))
        assert view["entry_conviction"] == 6
    assert seen[0]["market_regime"] == {"regime": "bull"}


def test_a_legacy_position_reports_an_unknown_window_rather_than_a_guess(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    mgr = _mgr(tmp_path)
    mgr._journal_entries[SYMC].pop("intended_hold_days")
    _, views_by_cid, _ = _publish(mgr, SYMC)
    assert views_by_cid[SYMC]["intended_hold_days"] is None
    assert views_by_cid[SYMC]["window_fraction"] is None




def test_a_stale_cache_is_discarded_rather_than_acted_on(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    mgr = _mgr(tmp_path)
    _publish(mgr, SYMC)
    _patch_model(monkeypatch, {SYMC: {"action": "cut", "reason": "thesis broken"}})
    assert asyncio.run(mgr.assess_positions_offcycle()) is True

    mgr._mgmt_cache["ts"] = time.time() - (mgr._mgmt_max_age_s() + 1)
    _, views_by_cid, qty_by_cid = _publish(mgr, SYMC)
    decisions, raw, _ = mgr._consume_model_decisions(views_by_cid, qty_by_cid)
    assert decisions == {} and raw is None
    assert mgr._mgmt_cache is None, "an expired cache must be dropped, not left to be re-checked"


def test_a_resized_position_falls_back_to_static_rules(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    mgr = _mgr(tmp_path)
    _publish(mgr, SYMC)
    _patch_model(monkeypatch, {SYMC: {"action": "take_profit", "reason": "banked"}})
    assert asyncio.run(mgr.assess_positions_offcycle()) is True

    _, views_by_cid, qty_by_cid = _publish(mgr, SYMC, qty=1)
    decisions, _, _ = mgr._consume_model_decisions(views_by_cid, qty_by_cid)
    assert decisions == {}


def test_a_position_absent_this_cycle_is_dropped(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path)
    _publish(mgr, SYMC, SYMD)
    _patch_model(monkeypatch, {SYMC: {"action": "cut"}, SYMD: {"action": "cut"}})
    assert asyncio.run(mgr.assess_positions_offcycle()) is True

    _, views_by_cid, qty_by_cid = _publish(mgr, SYMD)
    decisions, _, _ = mgr._consume_model_decisions(views_by_cid, qty_by_cid)
    assert set(decisions) == {SYMD}


def test_raw_is_withheld_when_the_assessment_predates_a_new_position(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    mgr = _mgr(tmp_path)
    _publish(mgr, SYMC)
    _patch_model(monkeypatch, {SYMC: {"action": "cut"}}, raw="RAW")
    assert asyncio.run(mgr.assess_positions_offcycle()) is True

    _, views_by_cid, qty_by_cid = _publish(mgr, SYMC, SYMD)
    decisions, raw, _ = mgr._consume_model_decisions(views_by_cid, qty_by_cid)
    assert decisions == {SYMC: {"action": "cut"}}, "covered positions are still managed"
    assert raw is None, "an uncovered book must not fabricate implicit holds"


def test_a_model_failure_leaves_static_rules_in_force(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path)
    _publish(mgr, SYMC)

    def _boom(*a, **k):
        raise RuntimeError("model server down")
    monkeypatch.setattr(manager_mod, "assess_positions", _boom)

    assert asyncio.run(mgr.assess_positions_offcycle()) is False
    _, views_by_cid, qty_by_cid = _publish(mgr, SYMC)
    assert mgr._consume_model_decisions(views_by_cid, qty_by_cid) == ({}, None, None)


def test_a_declared_failed_attempt_is_never_cached_as_all_hold(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path)
    _publish(mgr, SYMC)
    sentinel = {"decisions": {SYMC: {"action": "cut"}}, "ts": 1.0}
    mgr._mgmt_cache = sentinel
    monkeypatch.setattr(
        manager_mod, "assess_positions",
        lambda *a, **k: ({}, {"raw": "truncated", "attempt_ok": False,
                               "failure_reason": "invalid_response_json"}))

    assert asyncio.run(mgr.assess_positions_offcycle()) is False
    assert mgr._mgmt_cache is None, "a newer failure must revoke an older actionable cache"
    _, views_by_cid, qty_by_cid = _publish(mgr, SYMC)
    assert mgr._consume_model_decisions(views_by_cid, qty_by_cid) == ({}, None, None)
    assert mgr._mgmt_attempt_ok is False
    assert mgr._mgmt_failure_reason == "invalid_response_json"


def test_the_assessor_refuses_to_run_on_views_a_stalled_loop_stopped_publishing(tmp_path,
                                                                                monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    mgr = _mgr(tmp_path)
    _publish(mgr, SYMC)
    mgr._mgmt_views_ts = time.time() - (mgr._mgmt_max_age_s() + 1)
    called = _patch_model(monkeypatch, {SYMC: {"action": "cut"}}, capture=[])
    assert asyncio.run(mgr.assess_positions_offcycle()) is False
    assert called == [], "the model was asked about a book the loop had stopped watching"


def test_management_off_or_a_flat_book_never_spends_a_model_call(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path)
    called = _patch_model(monkeypatch, {}, capture=[])
    assert asyncio.run(mgr.assess_positions_offcycle()) is False
    _publish(mgr, SYMC)
    mgr.config.manage_positions = False
    assert asyncio.run(mgr.assess_positions_offcycle()) is False
    assert called == []


def test_consume_is_safe_before_the_assessor_has_ever_run(tmp_path):
    mgr = _mgr(tmp_path)
    _, views_by_cid, qty_by_cid = _publish(mgr, SYMC)
    assert mgr._consume_model_decisions(views_by_cid, qty_by_cid) == ({}, None, None)




def _source(path):
    return Path(path).read_text()


def _repo_file(name):
    return Path(manager_mod.__file__).resolve().parent.parent / name


def _function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def _calls(node):
    return {n.func.id for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}


def test_run_cycle_never_calls_the_model_on_the_deferred_path():
    """Public API contract; production-derived narrative omitted."""
    tree = ast.parse(_source(_repo_file("exitmgr/manager.py")))
    run_cycle = _function(tree, "run_cycle")

    deferred = [n for n in ast.walk(run_cycle)
                if isinstance(n, ast.If)
                and any(isinstance(x, ast.Name) and x.id == "defer_model"
                        for x in ast.walk(n.test))]
    assert deferred, "run_cycle no longer branches on defer_model"
    for branch in deferred:
        for stmt in branch.body:
            assert "assess_positions" not in _calls(stmt), (
                "run_cycle calls the model on the DEFERRED path -- the protective loop would now "
                "wait up to a full model timeout before evaluating stops")


def test_the_offcycle_assessor_touches_no_broker_and_no_lock():
    """Public API contract; production-derived narrative omitted."""
    tree = ast.parse(_source(_repo_file("exitmgr/manager.py")))
    body = ast.get_source_segment(_source(_repo_file("exitmgr/manager.py")),
                                  _function(tree, "assess_positions_offcycle"))
    for forbidden in ("ib_conn", "order_mutation_lock", "place_close_order", "state_manager.save",
                      "Lock", "flock"):
        assert forbidden not in body, (
            f"assess_positions_offcycle references {forbidden!r}; it must only read published "
            f"views and call the model")


def test_run_trader_schedules_the_assessor_beside_the_protective_loop():
    code = "".join(
        tok.string for tok in tokenize.generate_tokens(
            io.StringIO(_source(_repo_file("run_trader.py"))).readline)
        if tok.type != tokenize.COMMENT)
    flat = "".join(code.split())
    assert "_model_assessment_loop()" in flat, "the off-cycle assessor is no longer scheduled"
    assert "loops.append(_model_assessment_loop())" in flat

    tree = ast.parse(_source(_repo_file("run_trader.py")))
    main = _function(tree, "main")
    appends = [n for n in ast.walk(main)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr == "append"
               and any(isinstance(a, ast.Call) and isinstance(a.func, ast.Name)
                       and a.func.id in ("_protective_loop", "_model_assessment_loop")
                       for a in n.args)]
    assert len(appends) == 2, "the assessor and the protective loop must be scheduled together"


def test_the_protective_loop_still_defers_the_model():
    """Public API contract; production-derived narrative omitted."""
    flat = "".join(_source(_repo_file("run_trader.py")).split())
    assert "defer_model=True" in flat
    assert "defer_model=False" not in flat
