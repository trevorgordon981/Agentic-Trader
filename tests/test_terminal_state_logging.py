"""Public API contract; production-derived narrative omitted."""
import json
import os
import types

import pytest
from unittest.mock import AsyncMock

from exitmgr.config import Config
from exitmgr.connection import PositionData
from exitmgr.manager import ExitManager



def _mgr(tmp_path, journal_lines):
    cfg = Config()
    cfg.dry_run = True
    cfg.loop_mode = False
    cfg.journal.path = str(tmp_path / "trades.log")
    cfg.state.path = str(tmp_path / "state.json")
    cfg.kill_switch.path = str(tmp_path / "KILL")
    cfg.audit_path = str(tmp_path / "audit.jsonl")
    (tmp_path / "trades.log").write_text(
        "".join(json.dumps(x) + "\n" for x in journal_lines))
    return ExitManager(cfg), cfg


def _read_dataset(cfg):
    ddir = os.environ.get("EXITMGR_DATASET_DIR") or os.path.join(
        os.path.dirname(cfg.journal.path) or ".", "data")
    path = os.path.join(ddir, "trade_dataset.jsonl")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]


def _append_marker(cfg, marker):
    with open(cfg.journal.path, "a") as f:
        f.write(json.dumps(marker) + "\n")





def test_tool_close_single_emits_full_row_with_realized(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    entry = {"ts": "2026-07-01T14:00:00+00:00", "contract_id": 700, "symbol": "RKLB",
             "right": "C", "strike": 20.0, "expiry": "20260918", "quantity": 2, "debit": 800.0,
             "conviction": 6, "thesis": "post-earnings breakout"}
    mgr, cfg = _mgr(tmp_path, [entry])
    assert _read_dataset(cfg) == []

    for ts, px in [("t0", 4.00), ("t1", 5.50), ("t2", 5.00)]:
        mgr.state_manager.state.record_mark(700, px, 800.0, 2, ts=ts)

    _append_marker(cfg, {"contract_id": 700, "symbol": "RKLB", "event": "closed_by_tool",
                         "status": "Filled", "avg_fill_price": 5.00, "tool": "close_symbol",
                         "client_id": 91, "broker_flat_confirmed": True})
    mgr._load_journal()

    rows = _read_dataset(cfg)
    assert len(rows) == 1
    r = rows[0]
    assert r["kind"] == "trade" and r["con_id"] == 700 and r["symbol"] == "RKLB"

    assert r["entry"]["debit"] == 800.0 and r["entry"]["thesis"].startswith("post-earnings")

    assert r["lifecycle"]["marks"] == 3
    assert r["lifecycle"]["mfe_pct"] == pytest.approx(37.5)

    c = r["close"]
    assert c["reason"] == "closed_by_tool" and c["exit_event"] == "closed_by_tool"
    assert c["fill_status"] == "Filled"
    assert c["realized_pnl"] == pytest.approx(200.0)
    assert c["realized_pnl_pct"] == pytest.approx(25.0)
    assert c["close_client_id"] == 91
    assert r["labels"]["outcome"] == "win"

    assert 700 not in mgr._journal_entries


def test_tool_close_dedupes_across_reload(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    entry = {"ts": "2026-07-01T14:00:00+00:00", "contract_id": 700, "symbol": "RKLB",
             "right": "C", "strike": 20.0, "expiry": "20260918", "quantity": 1, "debit": 400.0}
    mgr, cfg = _mgr(tmp_path, [entry])
    _append_marker(cfg, {"contract_id": 700, "symbol": "RKLB", "event": "closed_by_tool",
                         "status": "Filled", "avg_fill_price": 5.00, "tool": "close_symbol",
                         "client_id": 91, "broker_flat_confirmed": True})
    mgr._load_journal()
    mgr._load_journal()
    mgr._load_journal()
    assert len(_read_dataset(cfg)) == 1


def test_tool_close_null_realized_without_price(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    entry = {"ts": "2026-07-01T14:00:00+00:00", "contract_id": 701, "symbol": "MUTX",
             "right": "C", "strike": 100.0, "expiry": "20260918", "quantity": 1, "debit": 500.0}
    mgr, cfg = _mgr(tmp_path, [entry])
    _append_marker(cfg, {"contract_id": 701, "symbol": "MUTX", "event": "closed_by_tool",
                         "status": "Filled", "avg_fill_price": None, "tool": "close_symbol",
                         "client_id": 91, "broker_flat_confirmed": True})
    mgr._load_journal()
    r = _read_dataset(cfg)[0]
    assert r["close"]["reason"] == "closed_by_tool"
    assert r["close"]["realized_pnl"] is None
    assert r["close"]["realized_pnl_pct"] is None
    assert r["close"]["realized_unknown_reason"] == "tool_close_fill_unknown"
    assert r["labels"]["outcome"] is None


def test_liquidate_tool_sets_liquidated_reason(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    entry = {"ts": "2026-07-01T14:00:00+00:00", "contract_id": 702, "symbol": "AA",
             "right": "C", "strike": 40.0, "expiry": "20260918", "quantity": 1, "debit": 300.0}
    mgr, cfg = _mgr(tmp_path, [entry])
    _append_marker(cfg, {"contract_id": 702, "symbol": "AA", "event": "closed_by_tool",
                         "status": "Filled", "avg_fill_price": 4.00, "tool": "liquidate",
                         "client_id": 91, "broker_flat_confirmed": True})
    mgr._load_journal()
    r = _read_dataset(cfg)[0]
    assert r["close"]["reason"] == "liquidated" and r["close"]["exit_event"] == "liquidated"
    assert r["close"]["realized_pnl"] == pytest.approx(100.0)


def test_tool_close_spread_nets_both_legs(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    entry = {"ts": "2026-07-01T14:00:00+00:00", "contract_id": 800, "symbol": "SYMF",
             "right": "C", "strike": 14.0, "expiry": "20260918", "quantity": 1, "debit": 150.0,
             "spread": {"short_con_id": 801, "short_strike": 25.0, "width": 11.0}}
    mgr, cfg = _mgr(tmp_path, [entry])
    mgr.state_manager.state.record_mark(800, 1.50, 150.0, 1, ts="t0",
                                        enrich={"is_net_spread": True})

    _append_marker(cfg, {"contract_id": 800, "symbol": "SYMF", "event": "closed_by_tool",
                         "status": "Filled", "avg_fill_price": 2.00, "tool": "liquidate",
                         "client_id": 91, "broker_flat_confirmed": True})
    _append_marker(cfg, {"contract_id": 801, "symbol": "SYMF", "event": "closed_by_tool",
                         "status": "Filled", "avg_fill_price": 0.40, "tool": "liquidate",
                         "client_id": 91, "broker_flat_confirmed": True})
    mgr._load_journal()
    rows = _read_dataset(cfg)
    assert len(rows) == 1
    r = rows[0]
    assert r["entry"]["structure"] == "spread"
    assert r["entry"]["spread"]["short_con_id"] == 801

    assert r["close"]["realized_pnl"] == pytest.approx(10.0)
    assert r["close"]["reason"] == "liquidated"


def test_tool_close_spread_null_realized_when_short_fill_unknown(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    entry = {"ts": "2026-07-01T14:00:00+00:00", "contract_id": 810, "symbol": "SYMF",
             "right": "C", "strike": 14.0, "expiry": "20260918", "quantity": 1, "debit": 150.0,
             "spread": {"short_con_id": 811, "short_strike": 25.0, "width": 11.0}}
    mgr, cfg = _mgr(tmp_path, [entry])
    _append_marker(cfg, {"contract_id": 810, "symbol": "SYMF", "event": "closed_by_tool",
                         "status": "Filled", "avg_fill_price": 2.00, "tool": "liquidate",
                         "client_id": 91, "broker_flat_confirmed": True})
    _append_marker(cfg, {"contract_id": 811, "symbol": "SYMF", "event": "closed_by_tool",
                         "status": "Filled", "avg_fill_price": None, "tool": "liquidate",
                         "client_id": 91, "broker_flat_confirmed": True})
    mgr._load_journal()
    r = _read_dataset(cfg)[0]
    assert r["close"]["realized_pnl"] is None
    assert r["close"]["realized_unknown_reason"] == "spread_net_fill_unknown"





def test_expiry_otm_worthless_is_minus_100pct(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    entry = {"ts": "2026-06-01T14:00:00+00:00", "contract_id": 900, "symbol": "SPY",
             "right": "C", "strike": 650.0, "expiry": "20260601", "quantity": 1, "debit": 500.0}
    mgr, cfg = _mgr(tmp_path, [entry])
    mgr.state_manager.state.record_mark(900, 5.0, 500.0, 1, ts="t0")

    mgr._emit_expiry_close(900, mgr._journal_entries[900], spot=600.0)
    r = _read_dataset(cfg)[0]
    assert r["close"]["reason"] == "expired" and r["close"]["exit_event"] == "expired"
    assert r["close"]["realized_pnl"] == pytest.approx(-500.0)
    assert r["close"]["realized_pnl_pct"] == pytest.approx(-100.0)
    assert r["close"]["dte_at_close"] == 0
    assert r["labels"]["outcome"] == "loss" and r["labels"]["win"] is False
    assert r["lifecycle"]["marks"] == 1


def test_expiry_itm_uses_intrinsic_value(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    entry = {"ts": "2026-06-01T14:00:00+00:00", "contract_id": 910, "symbol": "MUTX",
             "right": "C", "strike": 100.0, "expiry": "20260601", "quantity": 1, "debit": 200.0}
    mgr, cfg = _mgr(tmp_path, [entry])

    mgr._emit_expiry_close(910, mgr._journal_entries[910], spot=105.0)
    r = _read_dataset(cfg)[0]
    assert r["close"]["realized_pnl"] == pytest.approx(300.0)
    assert r["close"]["realized_pnl_pct"] == pytest.approx(150.0)
    assert r["labels"]["outcome"] == "win"


def test_expiry_value_unknown_without_spot_is_flagged_not_assumed(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    entry = {"ts": "2026-06-01T14:00:00+00:00", "contract_id": 920, "symbol": "AA",
             "right": "C", "strike": 40.0, "expiry": "20260601", "quantity": 1, "debit": 300.0}
    mgr, cfg = _mgr(tmp_path, [entry])
    mgr._emit_expiry_close(920, mgr._journal_entries[920], spot=None)
    r = _read_dataset(cfg)[0]
    assert r["close"]["reason"] == "expired"
    assert r["close"]["realized_pnl"] is None
    assert r["close"]["realized_pnl_pct"] is None
    assert r["close"]["expiry_value_unknown"] is True
    assert r["close"]["realized_unknown_reason"] == "expiry_value_unknown"


@pytest.mark.asyncio
async def test_process_expiries_detects_and_dedupes_across_cycles(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    entry = {"ts": "2026-06-01T14:00:00+00:00", "contract_id": 950, "symbol": "SPY",
             "right": "C", "strike": 650.0, "expiry": "20260601", "quantity": 1, "debit": 500.0}
    mgr, cfg = _mgr(tmp_path, [entry])
    mgr._spot_price = AsyncMock(return_value=600.0)
    await mgr._process_expiries({})
    await mgr._process_expiries({})
    rows = [r for r in _read_dataset(cfg) if r["close"]["reason"] == "expired"]
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_process_expiries_skips_still_live_and_not_yet_expired(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    live = {"ts": "2026-06-01T14:00:00+00:00", "contract_id": 960, "symbol": "SPY",
            "right": "C", "strike": 650.0, "expiry": "20260601", "quantity": 1, "debit": 500.0}
    future = {"ts": "2026-07-01T14:00:00+00:00", "contract_id": 961, "symbol": "MUTX",
              "right": "C", "strike": 100.0, "expiry": "20261231", "quantity": 1, "debit": 500.0}
    mgr, cfg = _mgr(tmp_path, [live, future])
    mgr._spot_price = AsyncMock(return_value=600.0)

    await mgr._process_expiries({960: PositionData(con_id=960, symbol="SPY", right="C",
                                                   quantity=1, avg_cost=5.0, expiry="20260601")})
    assert [r for r in _read_dataset(cfg) if r["close"]["reason"] == "expired"] == []





def test_manager_dataset_dir_env_override_routes_exit_writes_to_tmp(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    override = tmp_path / "override_ds"
    override.mkdir()
    monkeypatch.setenv("EXITMGR_DATASET_DIR", str(override))
    entry = {"ts": "2026-07-01T14:00:00+00:00", "contract_id": 970, "symbol": "SPY",
             "right": "C", "strike": 610.0, "expiry": "20260918", "quantity": 1, "debit": 800.0}
    mgr, cfg = _mgr(tmp_path, [entry])
    assert mgr._dataset_dir() == str(override)
    mgr.state_manager.state.record_mark(970, 8.0, 800.0, 1, ts="t0")
    trig = types.SimpleNamespace(trigger_type="stop", pnl_pct=-10.0, message="stop")
    mgr._log_exit(970, "SPY", trig, exit_price_per_share=7.2, quantity=1, reason="stop")

    assert os.path.exists(str(override / "trade_dataset.jsonl"))

    assert not os.path.exists(
        os.path.join(os.path.dirname(cfg.journal.path), "data", "trade_dataset.jsonl"))
