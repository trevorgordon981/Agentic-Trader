from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import time

import pytest

from exitmgr import entry_safety, risk
from exitmgr import order_lock, reload_queue
from exitmgr.trader import ResolvedOrder, Trader


def _resolved(*, ask=1.10, bid=1.00, observed=100.0, qty=1, con_id=11):
    return ResolvedOrder(
        "SPY", "C", "20270115", 600.0, qty, 1.05,
        SimpleNamespace(conId=con_id), entry_bid=bid, entry_ask=ask,
        quote_observed_at=observed, decision_id="decision-" + "a" * 32)


def test_missing_kill_switch_configuration_fails_closed(tmp_path):
    result = entry_safety.entry_markers_clear(
        config_path=str(tmp_path / "config.yaml"), kill_switch_path=None)
    assert not result.allowed
    assert any("KILL_SWITCH path is missing" in reason for reason in result.reasons)


def test_active_halt_markers_block_and_clear_only_when_absent(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("kill_switch:\n  path: ./KILL_SWITCH\n")
    clear = entry_safety.entry_markers_clear(
        config_path=str(cfg), kill_switch_path="./KILL_SWITCH")
    assert clear.allowed
    (tmp_path / "TRADING_DOWN").touch()
    down = entry_safety.entry_markers_clear(
        config_path=str(cfg), kill_switch_path="./KILL_SWITCH")
    assert not down.allowed and "TRADING_DOWN active" in down.reasons[0]
    (tmp_path / "TRADING_DOWN").unlink()
    (tmp_path / "KILL_SWITCH").touch()
    killed = entry_safety.entry_markers_clear(
        config_path=str(cfg), kill_switch_path="./KILL_SWITCH")
    assert not killed.allowed and any("KILL_SWITCH active" in x for x in killed.reasons)


def test_marker_stat_error_fails_closed(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    real_stat = Path.lstat

    def denied(self, *args, **kwargs):
        if self.name == "KILL_SWITCH":
            raise PermissionError("denied")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", denied)
    result = entry_safety.entry_markers_clear(
        config_path=str(cfg), kill_switch_path="./KILL_SWITCH")
    assert not result.allowed
    assert any("cannot verify KILL_SWITCH" in x for x in result.reasons)


def test_dangling_marker_symlink_still_blocks(tmp_path):
    (tmp_path / "KILL_SWITCH").symlink_to(tmp_path / "missing-target")
    result = entry_safety.entry_markers_clear(
        config_path=str(tmp_path / "config.yaml"), kill_switch_path="./KILL_SWITCH")
    assert not result.allowed
    assert any("KILL_SWITCH active" in x for x in result.reasons)


@pytest.mark.parametrize(
    "snapshot",
    [
        SimpleNamespace(net_liq=0, available_funds=1, cash=1),
        SimpleNamespace(net_liq=100, available_funds=-1, cash=1),
        SimpleNamespace(net_liq=float("nan"), available_funds=1, cash=1),
    ],
)
def test_bad_account_snapshots_block(snapshot):
    assert not entry_safety.account_snapshot_valid(snapshot).allowed


def test_nbbo_requires_two_sides_and_tight_age():
    assert entry_safety.nbbo_valid(_resolved(), now_monotonic=109.9).allowed
    stale = entry_safety.nbbo_valid(_resolved(), now_monotonic=110.1)
    assert not stale.allowed
    assert any("stale" in x for x in stale.reasons)
    assert not entry_safety.nbbo_valid(
        _resolved(bid=0.0), now_monotonic=101.0).allowed


def test_material_contract_quantity_and_price_changes_require_reapproval():
    original = _resolved()
    assert not entry_safety.material_changes(
        original, _resolved(ask=1.12), max_price_change_pct=0.03)
    assert any("price changed" in x for x in entry_safety.material_changes(
        original, _resolved(ask=1.20), max_price_change_pct=0.03))
    assert any("quantity changed" in x for x in entry_safety.material_changes(
        original, _resolved(qty=2)))
    assert any("contract/structure" in x for x in entry_safety.material_changes(
        original, _resolved(con_id=99)))


def test_decision_id_binds_order_ref():
    decision_id = entry_safety.new_decision_id()
    assert decision_id.startswith("decision-") and len(decision_id) == 41
    assert entry_safety.decision_order_ref(decision_id).startswith("alfred-entry:")


def test_confidence_never_waives_hard_concentration():
    limits = risk.RiskLimits(
        max_trade_pct=0.25, max_trade_pct_hard=0.25,
        confident_full_size=True, cap_bypass_min_conviction=6,
        max_single_name_agg_pct=0.36)
    decision = risk.evaluate_trade(
        risk.ProposedTrade("NVDA", 2_000, False, conviction=10),
        net_liq=10_000, available_funds=10_000,
        # SAME underlying as the proposed trade. Before 2026-08-13 `name_exposure` summed EVERY
        # non-index position, so an AMD holding blocked an NVDA entry -- a "single-name" cap
        # behaving portfolio-wide, which rejected every proposal outright and was fixed at
        # risk.py:372. Correlated DIFFERENT names are #6b's job; the whole book is max_deployed_pct's.
        # $2,000 existing + $2,000 proposed = $4,000 against the 36%-of-$10,000 = $3,600 cap, while
        # the per-trade cap ($2,500) leaves the trade alone -- so concentration is the ONLY gate
        # that can reject here, which is exactly the invariant under test.
        open_positions=[risk.OpenPosition("NVDA", 2_000, False)],
        pot_day_start=10_000, approved_names={"NVDA", "AMD"}, limits=limits)
    assert not decision.approved
    assert any("single-name exposure" in x for x in decision.reasons)


@pytest.mark.asyncio
async def test_trader_submit_refuses_stale_nbbo_before_place_order(tmp_path):
    ib = SimpleNamespace(placeOrder=Mock())
    conn = SimpleNamespace(ib=ib, create_combo_contract=Mock())
    trader = Trader(
        ib_conn=conn, exit_manager=SimpleNamespace(), limits=risk.RiskLimits(),
        approved_names={"SPY"}, endpoint="", model="", slack_token="",
        slack_channel="", approver_ids=set(), baseline_path=str(tmp_path / "base"),
        audit_path=str(tmp_path / "audit"), config_path=str(tmp_path / "config.yaml"),
        kill_switch_path="./KILL_SWITCH", trading_down_path=tmp_path / "TRADING_DOWN")
    # Stale RELATIVE TO NOW, not a magic constant. time.monotonic() starts near 0.0 per
    # process here, so observed=1.0 was only 'stale' depending on WHEN in the process this
    # ran: at ~0.8s in, age was -0.2 and it refused as 'clock moved backwards' (passing for
    # the wrong reason); anywhere from ~1s to ~11s in, age landed INSIDE the 10s window and
    # the order was ALLOWED THROUGH, placed, and then sat 22s waiting for a fill. One extra
    # upstream test file was enough to move it into that band. The gate itself is correct.
    stale = _resolved(
        observed=time.monotonic() - (entry_safety.DEFAULT_NBBO_MAX_AGE_SECONDS + 60.0))
    with pytest.raises(RuntimeError, match="NBBO"):
        await trader._submit_order(stale)
    ib.placeOrder.assert_not_called()


def test_service_has_independent_static_protective_loop():
    root = Path(__file__).resolve().parents[1]
    runner = (root / "run_trader.py").read_text()
    wrapper = (root / "run_trader_service.sh").read_text()
    assert "async def _protective_loop" in runner
    assert "defer_model=True" in runner
    assert "skip_exit_cycle=True" in runner
    assert "--protective-interval 30" in wrapper
    assert "exit 1" not in "\n".join(
        line for line in wrapper.splitlines() if "TRADING_DOWN" in line)


def test_retired_lift_can_never_remove_kill_switch():
    root = Path(__file__).resolve().parents[1]
    retired = (root / "ops" / "lift_killswitch_retired.sh").read_text()
    assert "rm " not in retired
    assert "exit 78" in retired


def test_cross_process_order_lock_refuses_overlap(tmp_path, monkeypatch):
    monkeypatch.setenv("EXITMGR_ORDER_LOCK", str(tmp_path / "orders.lock"))
    with order_lock.order_mutation_lock(timeout_seconds=0):
        with pytest.raises(order_lock.OrderMutationBusy):
            with order_lock.order_mutation_lock(timeout_seconds=0):
                pass


def test_reload_fill_identity_survives_drain_and_restart(tmp_path):
    path = str(tmp_path / "reload.json")
    ticket = reload_queue.make_ticket(
        symbol="AAPL", thesis="continue", right="C", width=None, dte_target=30,
        structure="single", is_index=False, reload_conviction=8,
        realized_pnl=100, original_debit=500, source_fill_key="fill-123")
    queue = reload_queue.ReloadQueue(path)
    assert queue.add_once(ticket)
    assert len(queue.drain(today="2026-07-10", max_per_name=2)[0]) == 1
    assert not reload_queue.ReloadQueue(path).add_once(ticket)


def test_entry_and_protective_services_are_separate():
    root = Path(__file__).resolve().parents[1]
    entry = (root / "run_trader_service.sh").read_text()
    protective = (root / "run_protective_service.sh").read_text()
    assert "--mode entry" in entry
    assert "--mode protective" in protective
    assert "TRADER_LLM_PRIORITY" not in protective
    assert "CPU-only" in protective


def test_every_executable_placeorder_site_uses_shared_mutation_lock():
    root = Path(__file__).resolve().parents[1]
    for rel in ("daily_recommend.py", "place_trade.py", "exitmgr/trader.py", "exitmgr/connection.py"):
        lines = (root / rel).read_text().splitlines()
        for idx, line in enumerate(lines):
            if ".placeOrder(" not in line:
                continue
            context = "\n".join(lines[max(0, idx - 4):idx + 1])
            assert "order_mutation_lock" in context, f"unlocked placeOrder in {rel}:{idx + 1}"


@pytest.mark.parametrize("rel", ["daily_recommend.py", "place_trade.py"])
def test_slate_and_manual_paths_wire_the_full_final_gate(rel):
    source = (Path(__file__).resolve().parents[1] / rel).read_text()
    for seam in (
        "entry_markers_clear", "account_snapshot_valid", "nbbo_valid",
        "risk.evaluate_trade", "days_to_earnings", "material_changes",
        "decision_order_ref", "order_mutation_lock",
    ):
        assert seam in source, f"{rel} missing final safety seam {seam}"
