import json
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from exitmgr import construction
from exitmgr import position_manager
from exitmgr.config import Config, ConstructionConfig, JournalConfig, StateConfig
from exitmgr.manager import ExitManager, _calendar_days_elapsed, _enforce_airtight_stop
from exitmgr.strategist import parse_ideas
from exitmgr.trader import ResolvedOrder, Trader


def _trade(*, ref="decision-1", status="Submitted", cid=101, total=1, price=2.5):
    return SimpleNamespace(
        order=SimpleNamespace(action="BUY", orderRef=ref, permId=999, orderId=7,
                              totalQuantity=total, lmtPrice=price),
        contract=SimpleNamespace(conId=cid, lastTradeDateOrContractMonth="20260821"),
        orderStatus=SimpleNamespace(status=status, remaining=total),
    )


def test_deployed_default_restored_to_40_percent():
    assert ConstructionConfig().max_deployed_pct == 0.40


def test_decay_coupling_40_at_10_passes_but_75_at_18_fails():
    base = ConstructionConfig(max_deployed_pct=0.40)
    ok, reasons = construction.check_budget(0, 10, 1000, [(400, 10)], base)
    assert ok and reasons == []
    uncoupled = replace(base, max_deployed_pct=0.75)
    ok, reasons = construction.check_budget(0, 18, 1000, [(750, 18)], uncoupled)
    assert not ok and any("portfolio theta-decay" in r for r in reasons)


def test_working_buy_is_in_budget_and_not_double_counted_after_partial_fill(tmp_path):
    journal = tmp_path / "trades.log"
    journal.write_text(json.dumps({
        "contract_id": 101, "debit": 250, "expiry": "20260821",
        "decision_id": "decision-1", "order_ref": "decision-1",
    }) + "\n")
    working = _trade()
    pending = construction.open_book_items({}, journal, [working])
    assert pending == {"decision:decision-1": (250.0, pending["decision:decision-1"][1])}
    assert pending["decision:decision-1"][1] > 0

    pos = SimpleNamespace(avg_cost=2.5, quantity=1, expiry="20260821")
    partial = construction.open_book_items({101: pos}, journal, [working])
    assert len(partial) == 1
    assert partial["decision:decision-1"][0] == 250.0


def test_terminal_buy_is_not_counted(tmp_path):
    assert construction.open_book_items({}, tmp_path / "missing", [_trade(status="Filled")]) == {}


def test_correlated_index_positions_still_bind_aggregate_budget(tmp_path):
    journal = tmp_path / "trades.log"
    rows = []
    positions = {}
    for cid, symbol in ((1, "SPY"), (2, "QQQ"), (3, "IWM")):
        rows.append(json.dumps({"contract_id": cid, "debit": 125, "expiry": "20260821",
                                "decision_id": f"d{cid}"}))
        positions[cid] = SimpleNamespace(avg_cost=1.25, quantity=1, expiry="20260821",
                                          symbol=symbol)
    journal.write_text("\n".join(rows) + "\n")
    book = list(construction.open_book_items(positions, journal).values())
    ok, reasons = construction.check_budget(50, 30, 1000, book, ConstructionConfig())
    assert not ok and any("total deployed premium" in r for r in reasons)


def test_intended_hold_parses_and_missing_is_explicit_none():
    base = ('{"trades":[{"underlying":"SPY","direction":"bullish",'
            '"structure":"long call","target_dte":80,"target_delta":0.6,'
            '"est_debit_usd":200,"conviction":7,"thesis":"x"')
    with_hold = parse_ideas(base + ',"intended_hold_days":14}]}')[0]
    legacy = parse_ideas(base + '}]}')[0]
    assert with_hold.intended_hold_days == 14
    assert legacy.intended_hold_days is None


def test_position_prompt_is_conditional_not_false_universal():
    assert "Every position was entered with an intended hold" not in position_manager.SYSTEM
    assert "When intended_hold_days is non-null" in position_manager.SYSTEM
    assert "the underwriting-window state is UNKNOWN" in position_manager.SYSTEM


def test_calendar_elapsed_is_source_bound_and_stop_is_hard_clamped():
    now = datetime(2026, 7, 22, 12, tzinfo=timezone.utc)
    assert _calendar_days_elapsed("2026-07-20T00:00:00+00:00", now) == 2.5
    assert _calendar_days_elapsed(None, now) is None
    cfg = Config().rules
    assert _enforce_airtight_stop(replace(cfg, stop_pct=50)).stop_pct == 30
    assert _enforce_airtight_stop(replace(cfg, stop_pct=20)).stop_pct == 20
    assert _enforce_airtight_stop(replace(cfg, stop_pct=None)).stop_pct == 30


def _manager(tmp_path):
    cfg = Config()
    cfg.state = StateConfig(path=str(tmp_path / "state.json"))
    cfg.journal = JournalConfig(path=str(tmp_path / "trades.log"))
    return ExitManager(cfg)


def test_position_view_exposes_window_and_legacy_nulls(tmp_path):
    mgr = _manager(tmp_path)
    now = datetime.now(timezone.utc)
    mgr._journal_entries[1] = {
        "ts": now.isoformat(), "intended_hold_days": 10, "conviction": 8,
        "quantity": 1, "debit": 200, "symbol": "SPY",
    }
    pos = SimpleNamespace(con_id=1, quantity=1, avg_cost=2, symbol="SPY", expiry="20260821")
    view = mgr._build_position_views([pos], {1: {"price": 2}}, {})[0]
    assert view["intended_hold_days"] == 10
    assert view["entry_conviction"] == 8
    assert view["calendar_days_elapsed"] is not None
    assert view["window_fraction"] is not None

    mgr._journal_entries[1] = {"quantity": 1, "debit": 200, "symbol": "SPY"}
    legacy = mgr._build_position_views([pos], {1: {"price": 2}}, {})[0]
    assert legacy["intended_hold_days"] is None
    assert legacy["calendar_days_elapsed"] is None
    assert legacy["window_fraction"] is None
    assert legacy["entry_conviction"] is None


def test_journal_persists_source_bound_hold(tmp_path):
    trader = Trader.__new__(Trader)
    trader.journal_path = str(tmp_path / "trades.log")
    order = ResolvedOrder("SPY", "C", "20260821", 600, 1, 2.0,
                          SimpleNamespace(conId=101), decision_id="decision-" + "1" * 32,
                          conviction=8, intended_hold_days=10)
    trader._journal_entry(order)
    rec = json.loads((tmp_path / "trades.log").read_text())
    assert rec["intended_hold_days"] == 10
    assert rec["conviction"] == 8
