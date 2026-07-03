"""Tests for the SCALE-OUT / partial-trim HOOK wired into manager.run_cycle (2026-07-02).

rules.py already EMITS scale_out triggers (ExitTrigger.quantity_fraction < 1.0); these tests
prove the MANAGER acts on them correctly on REAL-money-shaped paths:

  * a first-target hit places a PARTIAL close (close_qty ~= half), sets & persists `scaled_out`,
    and does NOT close the whole position;
  * an already-trimmed position never re-trims;
  * the runner's basis is PRO-RATED to the contracts held, so its realized P&L / rule
    evaluation fire on the correct basis (the money bug: without pro-rating a +30% runner
    exit would log as a loss against the full 2-contract debit);
  * a full/risk exit still closes the FULL quantity and never sets scaled_out;
  * scaled_out survives a state save/reload.

run_cycle is driven with the broker + order layer mocked, so NO orders are placed and NO
broker is touched -- we capture the `quantity` handed to place_close_order and inspect state.
"""
import json
import os

import pytest
from unittest.mock import AsyncMock, MagicMock

from exitmgr.config import Config, RulesConfig, TrailingConfig, ScaleOutConfig
from exitmgr.connection import PositionData
from exitmgr.order import OrderResult
from exitmgr.state import State, StateManager
from exitmgr.manager import ExitManager


CON = 1000
# Entry: 5.00/share x 4 contracts = $2000 debit. Far expiry so the DTE<=10 time-stop never fires.
JOURNAL = {"ts": "2026-07-01T14:00:00+00:00", "contract_id": CON, "symbol": "AAPL",
           "right": "C", "strike": 200.0, "expiry": "20261231", "quantity": 4, "debit": 2000.0,
           "conviction": 6}


def _rules():
    return RulesConfig(
        profit_target_pct=30.0,
        stop_pct=30.0,
        time_stop_days=10,
        trailing=TrailingConfig(enabled=False),
        scale_out=ScaleOutConfig(enabled=True, first_target_pct=20.0, trim_fraction=0.5),
    )


def _mgr(tmp_path, journal=JOURNAL):
    cfg = Config()
    cfg.dry_run = False
    cfg.loop_mode = False
    cfg.journal.path = str(tmp_path / "trades.log")
    cfg.state.path = str(tmp_path / "state.json")
    cfg.kill_switch.path = str(tmp_path / "KILL")   # absent -> kill switch inactive
    cfg.audit_path = str(tmp_path / "audit.jsonl")
    cfg.manage_positions = False                    # skip the LLM assessment call
    cfg.alerts_channel = ""                          # no Slack in tests
    cfg.error_channel = ""
    cfg.rules = _rules()
    (tmp_path / "trades.log").write_text(json.dumps(journal) + "\n")
    return ExitManager(cfg), cfg


def _wire(mgr, *, qty, price, expiry="20261231"):
    """Mock the broker + order layer for one or more run_cycle calls.
    Returns the AsyncMock standing in for place_close_order (inspect .call_args)."""
    pos = {CON: PositionData(con_id=CON, symbol="AAPL", right="C",
                             quantity=qty, avg_cost=5.00, expiry=expiry)}
    mgr.ib_conn.get_positions = AsyncMock(return_value=pos)
    mgr.ib_conn.get_open_orders = AsyncMock(return_value={})
    mgr.ib_conn.fetch_quotes = AsyncMock(return_value={CON: {"price": price}})
    mgr.ib_conn.ib = MagicMock()                    # ib_conn.ib is None until connect()
    mgr.ib_conn.ib.portfolio = lambda: []           # no server marking -> use the quote
    mgr._spot_price = AsyncMock(return_value=None)   # enrichment lookup on exit

    place = AsyncMock(return_value=OrderResult(success=True, order_id=555, con_id=CON, trade=None))
    mgr.order_manager.place_close_order = place
    return place


def _read_exits(cfg):
    path = os.path.join(os.path.dirname(cfg.journal.path) or ".", "exits.log")
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]


# ---------------------------------------------------------------- close_qty formula (isolated)
def test_close_qty_formula_matches_spec():
    """close_qty = full when fraction>=1 else max(1, min(qty-1, round(qty*fraction)))."""
    def close_qty(quantity, qf):
        if qf >= 1.0:
            return quantity
        return max(1, min(quantity - 1, round(quantity * qf)))
    assert close_qty(4, 0.5) == 2          # trim half of 4
    assert close_qty(2, 0.5) == 1          # trim half of 2 -> 1, leaves 1 runner
    assert close_qty(3, 0.5) == 2          # round(1.5)=2, still leaves a runner
    assert close_qty(5, 0.5) == 2          # round(2.5)=2 (bankers'), leaves 3
    assert close_qty(2, 0.9) == 1          # clamp: must always leave >=1 runner
    assert close_qty(4, 1.0) == 4          # full exit
    assert close_qty(1, 0.5) == 1          # degenerate (rule won't emit for qty<2, but safe)


# ---------------------------------------------------------------- state save/reload
def test_scaled_out_survives_save_reload(tmp_path):
    p = str(tmp_path / "state.json")
    sm = StateManager(p)
    sm.state.scaled_out[str(CON)] = True
    sm.save()
    sm2 = StateManager(p)
    assert sm2.state.scaled_out.get(str(CON)) is True


def test_old_state_file_without_scaled_out_loads_empty(tmp_path):
    """Backward compatibility: a pre-scale-out state file has no `scaled_out` key."""
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"in_flight": {}, "daily_stats": {}, "last_cycle": None,
                             "peak_prices": {}}))
    sm = StateManager(str(p))
    assert sm.state.scaled_out == {}


# ---------------------------------------------------------------- run_cycle: first-target trim
@pytest.mark.asyncio
async def test_first_target_trims_to_runner(tmp_path):
    mgr, cfg = _mgr(tmp_path)
    place = _wire(mgr, qty=4, price=6.00)            # +20% -> scale_out (not the +30% full target)
    await mgr.run_cycle(dry_run=False)

    place.assert_called_once()
    kw = place.call_args.kwargs
    assert kw["con_id"] == CON
    assert kw["quantity"] == 2                        # HALF closed, not the full 4
    # scaled_out set AND persisted to disk
    assert mgr.state_manager.state.scaled_out.get(str(CON)) is True
    reloaded = StateManager(cfg.state.path).state
    assert reloaded.scaled_out.get(str(CON)) is True
    # exit record marks it a PARTIAL with reason scale_out
    r = _read_exits(cfg)[0]
    assert r["reason"] == "scale_out"
    assert r["quantity"] == 2 and r["partial"] is True and r["remaining_qty"] == 2


@pytest.mark.asyncio
async def test_qty2_trim_leaves_one_runner(tmp_path):
    je = dict(JOURNAL, quantity=2, debit=1000.0)      # 5.00/share x 2
    mgr, cfg = _mgr(tmp_path, journal=je)
    place = _wire(mgr, qty=2, price=6.00)
    await mgr.run_cycle(dry_run=False)
    assert place.call_args.kwargs["quantity"] == 1    # trims 1, leaves 1 runner
    assert mgr.state_manager.state.scaled_out.get(str(CON)) is True


# ---------------------------------------------------------------- run_cycle: no re-trim
@pytest.mark.asyncio
async def test_already_trimmed_does_not_retrim(tmp_path):
    mgr, cfg = _mgr(tmp_path)
    # pre-seed: already trimmed; broker now shows the 2-contract runner
    mgr.state_manager.state.scaled_out[str(CON)] = True
    mgr.state_manager.save()
    place = _wire(mgr, qty=2, price=6.00)             # still at +20%, but scale_out must NOT re-fire
    await mgr.run_cycle(dry_run=False)
    place.assert_not_called()                         # no partial, no full -> nothing to do


# ---------------------------------------------------------------- run_cycle: runner basis pro-rated
@pytest.mark.asyncio
async def test_runner_full_close_uses_prorated_basis(tmp_path):
    """The MONEY test: a trimmed runner (2 of an original 4, journal debit still $2000) that hits
    the +30% target must FULL-close its 2 contracts AND log +30% realized -- proving the basis was
    pro-rated to $1000. Without pro-rating it would value the runner against the full $2000 and
    mislabel a winner as a -35% loss, mis-firing exits on real money."""
    mgr, cfg = _mgr(tmp_path)
    mgr.state_manager.state.scaled_out[str(CON)] = True
    mgr.state_manager.save()
    place = _wire(mgr, qty=2, price=6.50)             # +30% on a 5.00 basis
    await mgr.run_cycle(dry_run=False)

    place.assert_called_once()
    assert place.call_args.kwargs["quantity"] == 2    # FULL close of the runner (not a re-trim)
    r = _read_exits(cfg)[0]
    assert r["reason"] == "profit_target"
    assert r["quantity"] == 2
    assert r["partial"] is False
    assert r["entry_debit"] == pytest.approx(1000.0)  # PRO-RATED to the 2-contract runner
    assert r["proceeds"] == pytest.approx(1300.0)     # 6.50 * 100 * 2
    assert r["realized_pnl"] == pytest.approx(300.0)  # +$300, NOT -$700
    assert r["realized_pnl_pct"] == pytest.approx(30.0)


# ---------------------------------------------------------------- run_cycle: full/risk exit intact
@pytest.mark.asyncio
async def test_full_risk_exit_closes_full_qty(tmp_path):
    mgr, cfg = _mgr(tmp_path)
    place = _wire(mgr, qty=4, price=3.50)             # -30% -> stop, full exit
    await mgr.run_cycle(dry_run=False)

    place.assert_called_once()
    assert place.call_args.kwargs["quantity"] == 4    # WHOLE position closed
    assert str(CON) not in mgr.state_manager.state.scaled_out   # a stop is not a trim
    r = _read_exits(cfg)[0]
    assert r["reason"] == "stop"
    assert r["quantity"] == 4 and r["partial"] is False
    assert r["entry_debit"] == pytest.approx(2000.0)  # full basis, not pro-rated
    assert r["realized_pnl"] == pytest.approx(3.50 * 100 * 4 - 2000.0)   # -600


@pytest.mark.asyncio
async def test_full_profit_target_outranks_scale_out(tmp_path):
    """If price gaps straight to the +30% full target, take the full profit (nothing to let run)."""
    mgr, cfg = _mgr(tmp_path)
    place = _wire(mgr, qty=4, price=6.50)             # +30% full target, never trimmed
    await mgr.run_cycle(dry_run=False)
    assert place.call_args.kwargs["quantity"] == 4    # full close
    assert str(CON) not in mgr.state_manager.state.scaled_out
    assert _read_exits(cfg)[0]["reason"] == "profit_target"
