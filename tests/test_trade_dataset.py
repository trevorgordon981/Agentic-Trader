"""Tests for per-trade data capture (2026-07-02): MFE/MAE excursion accumulation, the
mark-to-market path, save/reload durability, and the rich fine-tuning-ready closed-trade
record appended to data/trade_dataset.jsonl.

Motivation: the training corpus's exit MFE was synthetic and MAE was absent EVERYWHERE, so
exits could not be empirically validated. This capture records real mark-to-market excursions
on live trades. RECORD-ONLY -- these tests place no orders and touch no broker.
"""
import json
import types

import pytest

from exitmgr.config import Config
from exitmgr.state import State, StateManager
from exitmgr.manager import ExitManager


# --------------------------------------------------------------------------- helpers
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
    import os
    # honor EXITMGR_DATASET_DIR (autouse conftest) -- manager writes now respect it (2026-07-03
    # fold-in); fall back to data/ next to the journal when unset (production layout).
    ddir = os.environ.get("EXITMGR_DATASET_DIR") or os.path.join(
        os.path.dirname(cfg.journal.path) or ".", "data")
    path = os.path.join(ddir, "trade_dataset.jsonl")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]


# --------------------------------------------------------------------------- excursions
def test_mfe_mae_accumulate_over_mark_sequence():
    """A round-trip: runs to +40% then closes -15% -> MFE~40, MAE~-15, both timestamped."""
    st = State()
    # entry basis: debit $800, qty 1 -> exc% = (price*100 - 800)/800*100
    seq = [
        ("2026-07-02T14:00:00-04:00", 8.00),   # exc   0.0
        ("2026-07-02T14:30:00-04:00", 9.60),   # exc +20.0
        ("2026-07-02T15:00:00-04:00", 11.20),  # exc +40.0  <- peak (MFE)
        ("2026-07-02T15:30:00-04:00", 7.60),   # exc  -5.0
        ("2026-07-02T16:00:00-04:00", 6.80),   # exc -15.0  <- trough (MAE)
    ]
    for ts, px in seq:
        st.record_mark(111, px, 800.0, 1, ts=ts)
    assert st.mfe_pct["111"] == pytest.approx(40.0)
    assert st.mae_pct["111"] == pytest.approx(-15.0)
    assert st.mfe_ts["111"] == "2026-07-02T15:00:00-04:00"   # when the peak occurred
    assert st.mae_ts["111"] == "2026-07-02T16:00:00-04:00"   # when the trough occurred
    assert st.peak_prices["111"] == pytest.approx(11.20)
    # full mark path retained as a time series
    assert len(st.mark_path["111"]) == 5
    assert st.mark_path["111"][2]["pnl_pct"] == pytest.approx(40.0)
    assert st.mark_path["111"][-1]["price"] == pytest.approx(6.80)


def test_mfe_mae_monotonic_and_ignore_bad_debit():
    st = State()
    st.record_mark(7, 10.0, 1000.0, 1)   # exc 0
    st.record_mark(7, 12.0, 1000.0, 1)   # exc +20 -> MFE
    st.record_mark(7, 8.0, 1000.0, 1)    # exc -20 -> MAE
    st.record_mark(7, 11.0, 1000.0, 1)   # exc +10 -> does NOT lower MFE nor raise MAE
    assert st.mfe_pct["7"] == pytest.approx(20.0)
    assert st.mae_pct["7"] == pytest.approx(-20.0)
    # bad/zero debit -> no excursion recorded, but peak price still tracked
    st.record_mark(9, 5.0, 0.0, 1)
    assert "9" not in st.mfe_pct and "9" not in st.mae_pct
    assert st.peak_prices["9"] == pytest.approx(5.0)


def test_path_cap_bounds_growth():
    st = State()
    for i in range(20):
        st.record_mark(1, 10.0 + i * 0.01, 1000.0, 1, ts=f"t{i}", path_cap=5)
    assert len(st.mark_path["1"]) == 5           # capped
    assert st.mfe_pct["1"] == pytest.approx((10.19 * 100 - 1000) / 1000 * 100, abs=0.01)  # MFE still updates past cap


# --------------------------------------------------------------------------- durability
def test_excursions_survive_save_reload(tmp_path):
    p = str(tmp_path / "state.json")
    sm = StateManager(p)
    for ts, px in [("t0", 8.0), ("t1", 11.2), ("t2", 6.8)]:
        sm.state.record_mark(222, px, 800.0, 1, ts=ts)
    sm.save()
    # fresh manager reads the same file -> running MFE/MAE/path must be intact
    sm2 = StateManager(p)
    assert sm2.state.mfe_pct["222"] == pytest.approx(40.0)
    assert sm2.state.mae_pct["222"] == pytest.approx(-15.0)
    assert sm2.state.mfe_ts["222"] == "t1"
    assert sm2.state.mae_ts["222"] == "t2"
    assert len(sm2.state.mark_path["222"]) == 3
    assert sm2.state.peak_prices["222"] == pytest.approx(11.2)


def test_prune_tracking_drops_closed_keeps_active():
    st = State()
    st.record_mark(1, 10.0, 1000.0, 1)
    st.record_mark(2, 10.0, 1000.0, 1)
    st.record_mark(3, 10.0, 1000.0, 1)
    st.prune_tracking({2, 3})   # con_id 1 no longer active -> pruned
    for d in (st.mfe_pct, st.mae_pct, st.mfe_ts, st.mae_ts, st.mark_path, st.peak_prices):
        assert "1" not in d
        assert "2" in d and "3" in d


# --------------------------------------------------------------------------- full record
def test_full_closed_trade_record_written_with_every_field(tmp_path):
    """End-to-end: entry snapshot + accumulated MFE/MAE path + close + labels are all written
    as ONE dataset object on close. Round-trip case: ran +40% then closed -15%."""
    je = {"ts": "2026-07-01T14:00:00+00:00", "contract_id": 555, "symbol": "SPY",
          "right": "C", "strike": 610.0, "expiry": "20260731", "quantity": 1, "debit": 800.0,
          "profit_target_pct": 30.0, "stop_pct": 30.0, "conviction": 7,
          "thesis": "bull continuation above 20d", "dte_at_entry": 30, "dte_adjusted": False,
          "entry_delta": 0.60, "entry_iv": 0.22, "underlying_price_at_entry": 611.0,
          "order_status": "Filled", "avg_fill_price": 8.00,
          "fill_ts": "2026-07-01T14:00:05+00:00"}
    mgr, cfg = _mgr(tmp_path, [je])
    # accumulate the mark path in state exactly as run_cycle would each cycle
    for ts, px in [("2026-07-01T15:00:00+00:00", 8.0),
                   ("2026-07-01T16:00:00+00:00", 11.2),   # +40% peak
                   ("2026-07-01T17:00:00+00:00", 6.8)]:    # -15% trough
        mgr.state_manager.state.record_mark(555, px, 800.0, 1, ts=ts)

    trig = types.SimpleNamespace(trigger_type="stop", pnl_pct=-15.0, message="stop")
    mgr._log_exit(555, "SPY", trig, exit_price_per_share=6.80, quantity=1,
                  reason=mgr._exit_reason(trig),
                  extra={"underlying_price": 605.0, "iv": 0.25, "delta": 0.40,
                         "fill_status": "Filled", "avg_fill_price": 6.80})

    rows = _read_dataset(cfg)
    assert len(rows) == 1
    r = rows[0]
    assert r["schema"] == "trade_dataset.v2"
    assert r["kind"] == "trade"
    assert r["con_id"] == 555 and r["symbol"] == "SPY"

    # entry snapshot
    e = r["entry"]
    assert e["ts"] == "2026-07-01T14:00:00+00:00"
    assert e["structure"] == "single" and e["spread"] is None
    assert e["strike"] == 610.0 and e["expiry"] == "20260731"
    assert e["debit"] == 800.0 and e["quantity"] == 1
    assert e["conviction"] == 7.0 and e["thesis"].startswith("bull")
    assert e["profit_target_pct"] == 30.0 and e["stop_pct"] == 30.0
    assert e["entry_delta"] == 0.60 and e["entry_iv"] == 0.22
    assert e["dte_at_entry"] == 30 and e["underlying_price_at_entry"] == 611.0

    # lifecycle: MFE/MAE + full mark path
    lc = r["lifecycle"]
    assert lc["mfe_pct"] == pytest.approx(40.0)
    assert lc["mae_pct"] == pytest.approx(-15.0)
    assert lc["mfe_ts"] == "2026-07-01T16:00:00+00:00"
    assert lc["mae_ts"] == "2026-07-01T17:00:00+00:00"
    assert lc["marks"] == 3 and len(lc["mark_path"]) == 3
    assert lc["peak_price"] == pytest.approx(11.2)
    # drawdown from peak = MFE - realized = 40 - (-15) = 55
    assert lc["drawdown_from_peak_pct"] == pytest.approx(55.0)

    # close
    c = r["close"]
    assert c["reason"] == "stop"
    assert c["exit_price_per_share"] == 6.80
    assert c["proceeds"] == 680.0
    assert c["realized_pnl"] == -120.0
    assert c["realized_pnl_pct"] == pytest.approx(-15.0)
    assert c["holding_days"] is not None
    assert c["underlying_price_at_exit"] == 605.0 and c["exit_iv"] == 0.25
    # closed at -15%, which did NOT reach the +30% TP nor the -30% SL level
    assert c["tp_hit"] is False and c["sl_hit"] is False

    # labels: round-trip (ran to +40 then closed -15)
    lab = r["labels"]
    assert lab["outcome"] == "loss"
    assert lab["win"] is False
    assert lab["round_trip"] is True


def test_sl_hit_true_and_win_label(tmp_path):
    je = {"ts": "2026-07-01T14:00:00+00:00", "contract_id": 556, "symbol": "MU",
          "right": "C", "strike": 100.0, "quantity": 1, "debit": 1000.0,
          "profit_target_pct": 30.0, "stop_pct": 30.0, "conviction": 6}
    mgr, cfg = _mgr(tmp_path, [je])
    mgr.state_manager.state.record_mark(556, 10.0, 1000.0, 1, ts="t0")
    mgr.state_manager.state.record_mark(556, 13.5, 1000.0, 1, ts="t1")  # +35%
    trig = types.SimpleNamespace(trigger_type="profit_target", pnl_pct=35.0, message="tp")
    mgr._log_exit(556, "MU", trig, exit_price_per_share=13.5, quantity=1,
                  reason=mgr._exit_reason(trig))
    r = _read_dataset(cfg)[0]
    assert r["close"]["realized_pnl_pct"] == pytest.approx(35.0)
    assert r["close"]["tp_hit"] is True and r["close"]["sl_hit"] is False
    assert r["labels"]["outcome"] == "win" and r["labels"]["win"] is True
    assert r["labels"]["round_trip"] is False


def test_spread_record_carries_legs(tmp_path):
    je = {"ts": "2026-07-01T14:00:00+00:00", "contract_id": 557, "symbol": "AA",
          "right": "P", "strike": 57.0, "quantity": 1, "debit": 160.0, "conviction": 5,
          "spread": {"short_con_id": 999, "short_strike": 49.5, "width": 7.5}}
    mgr, cfg = _mgr(tmp_path, [je])
    mgr.state_manager.state.record_mark(557, 1.60, 160.0, 1, ts="t0")
    trig = types.SimpleNamespace(trigger_type="time_stop", pnl_pct=0.0, message="t")
    mgr._log_exit(557, "AA", trig, exit_price_per_share=1.60, quantity=1,
                  reason=mgr._exit_reason(trig))
    r = _read_dataset(cfg)[0]
    assert r["entry"]["structure"] == "spread"
    assert r["entry"]["spread"]["short_con_id"] == 999
    assert r["entry"]["spread"]["width"] == 7.5
    assert r["close"]["reason"] == "time_stop"


# ===========================================================================================
# v2 (2026-07-02): full-arc capture -- DECISION context, NO-TRADE/REJECTED rows, enriched
# marks, spread net-mark correctness, fill-quality slippage, record-only safety, and the
# circuit-breaker day-start (P3.9) hardening. All record-only; no orders, no broker.
# ===========================================================================================
import types as _types
from exitmgr import trade_capture as tc
from exitmgr.trader import day_start_pot, _trading_day


def _ddir(tmp_path):
    import os
    d = os.path.join(str(tmp_path), "data")
    os.makedirs(d, exist_ok=True)
    return d


# --------------------------------------------------------------------------- trade_capture unit
def test_capture_decision_joins_only_by_immutable_decision_id(tmp_path):
    d = _ddir(tmp_path)
    idea = _types.SimpleNamespace(underlying="SPY", direction="bullish", structure="single",
                                  conviction=8, thesis="bull above 20d", target_dte=30)
    other = _types.SimpleNamespace(underlying="MU", direction="bullish", structure="single",
                                   conviction=4, thesis="weak", target_dte=20)
    gate = _types.SimpleNamespace(approved=True, reasons=[], per_trade_cap=1234.5)
    tc.capture_decision(d, source="trader", symbol="SPY", right="C", strike=610.0,
                        expiry="20260731", structure="single", con_id=555,
                        decision_id="decision-555",
                        chosen_idea=idea, candidates=[idea, other],
                        raw_strategist="MODEL SAID: buy SPY calls, conviction 8",
                        gate=gate, regime={"regime": "bull", "trend_score": 42, "vix": 14.0},
                        market_context="RAG+news+journal brief text",
                        construction={"tp_pct": 30.0, "sl_pct": 30.0, "dte": 30})
    j = tc.load_decision_context(d, decision_id="decision-555", con_id=555, symbol="SPY", strike=610.0,
                                 expiry="20260731", right="C")
    assert j is not None and j["kind"] == "decision"
    assert j["symbol"] == "SPY" and j["con_id"] == 555
    assert j["chosen"]["conviction"] == 8 and j["chosen"]["thesis"].startswith("bull")
    assert len(j["candidates"]) == 2                       # EVERY candidate considered
    assert "buy SPY calls" in j["raw_strategist"]          # verbatim reasoning
    assert j["gate"]["approved"] is True and j["gate"]["per_trade_cap"] == 1234.5
    assert j["regime"]["regime"] == "bull"
    assert j["construction"]["tp_pct"] == 30.0
    # Causal fallback is forbidden even when every contract field matches.
    assert tc.load_decision_context(
        d, con_id=555, symbol="SPY", strike=610.0, expiry="20260731", right="C") is None
    assert tc.load_decision_context(d, decision_id="wrong-id", con_id=555) is None


def test_capture_no_trade_and_rejected_rows(tmp_path):
    d = _ddir(tmp_path)
    tc.capture_no_trade(d, source="trader", reason="empty_slate",
                        raw_strategist="no trades today", regime={"regime": "risk_off"},
                        market_context="brief")
    idea = _types.SimpleNamespace(underlying="NOK", direction="bullish", structure="spread",
                                  conviction=6, thesis="lotto")
    gate = _types.SimpleNamespace(approved=False, reasons=["daily circuit breaker"], per_trade_cap=0.0)
    tc.capture_rejected(d, source="trader", symbol="NOK", reason=["daily circuit breaker"],
                        stage="risk_gate", idea=idea, gate=gate, structure="spread")
    rows = [json.loads(l) for l in open(tc.dataset_path(d)) if l.strip()]
    kinds = {r["kind"] for r in rows}
    assert kinds == {"no_trade", "rejected"}
    nt = next(r for r in rows if r["kind"] == "no_trade")
    assert nt["schema"] == "trade_dataset.v2" and nt["reason"] == "empty_slate"
    assert nt["raw_strategist"] == "no trades today"
    rj = next(r for r in rows if r["kind"] == "rejected")
    assert rj["schema"] == "trade_dataset.v2" and rj["stage"] == "risk_gate"
    assert rj["symbol"] == "NOK" and rj["gate"]["approved"] is False
    assert "daily circuit breaker" in rj["gate"]["bound_caps"]   # which cap bound it


def test_capture_never_raises_on_garbage(tmp_path):
    """A capture bug must be inert -- unserializable objects / bad dirs must not raise."""
    class Boom:
        def __repr__(self):
            raise RuntimeError("nope")
    d = _ddir(tmp_path)
    # unserializable chosen_idea / candidates must not raise
    assert tc.capture_decision(d, source="trader", symbol="X",
                               chosen_idea=Boom(), candidates=[Boom()]) is not None
    # a bad directory must not raise
    assert tc.capture_no_trade("/root/nonexistent-\0-dir", source="trader", reason="x") is None \
        or True
    # loaders on a missing file -> None, no raise
    assert tc.load_decision_context(str(tmp_path / "nope"), con_id=1) is None
    assert tc.load_review(str(tmp_path / "nope"), symbol="X") is None


# --------------------------------------------------------------------------- enriched marks (PATH)
def test_record_mark_enrichment_persisted_on_path():
    st = State()
    enrich = {"iv": 0.31, "delta": 0.55, "gamma": 0.02, "theta": -0.4, "vega": 0.9,
              "dte": 21, "days_held": 3.2, "dist_to_tp_pct": 25.0, "dist_to_sl_pct": 45.0,
              "mgmt_action": "arm_trail", "mgmt_reason": "up and trending", "underlying": 611.0,
              "none_field": None}
    st.record_mark(42, 9.0, 800.0, 1, ts="t0", enrich=enrich)
    m = st.mark_path["42"][-1]
    # base v1 fields intact
    assert m["price"] == pytest.approx(9.0) and m["pnl_pct"] == pytest.approx(12.5)
    # v2 enrichment merged
    assert m["iv"] == 0.31 and m["delta"] == 0.55 and m["theta"] == -0.4
    assert m["dte"] == 21 and m["days_held"] == 3.2
    assert m["dist_to_tp_pct"] == 25.0 and m["dist_to_sl_pct"] == 45.0
    assert m["mgmt_action"] == "arm_trail" and m["mgmt_reason"] == "up and trending"
    assert m["underlying"] == 611.0
    assert "none_field" not in m          # None keys are NOT merged (never clobber)


def test_record_mark_backward_compatible_without_enrich():
    """Omitting enrich yields exactly the v1 mark shape (backward compatibility)."""
    st = State()
    st.record_mark(1, 10.0, 1000.0, 1, ts="t0")
    m = st.mark_path["1"][-1]
    assert set(m.keys()) == {"ts", "price", "value", "pnl_pct", "underlying"}


# --------------------------------------------------------------------------- spread NET-mark (P1.2)
def test_spread_net_mark_is_long_minus_short_and_labeled(tmp_path):
    """P1.2 audit: a debit spread must be valued/recorded on the NET combo mark (long - short),
    never a single leg. Replicates the manager's net computation and asserts record_mark stores
    that net value (so excursions/rules see the net), tagged is_net_spread."""
    st = State()
    long_mark, short_mark = 2.00, 0.60
    net = long_mark - short_mark                 # 1.40 -- exactly what run_cycle feeds record_mark
    st.record_mark(700, net, 100.0, 1, ts="t0",
                   enrich={"is_net_spread": True})
    m = st.mark_path["700"][-1]
    assert m["price"] == pytest.approx(1.40)     # NET, not the 2.00 long leg
    assert m["value"] == pytest.approx(140.0)
    assert m["pnl_pct"] == pytest.approx(40.0)   # (1.40*100 - 100)/100 = +40%
    assert m["is_net_spread"] is True
    assert st.peak_prices["700"] == pytest.approx(1.40)   # peak (feeds trailing stop) is net too


def test_spread_v2_record_structure_and_legs(tmp_path):
    je = {"ts": "2026-07-01T14:00:00+00:00", "contract_id": 557, "symbol": "AA",
          "right": "P", "strike": 57.0, "quantity": 1, "debit": 160.0, "conviction": 5,
          "spread": {"short_con_id": 999, "short_strike": 49.5, "width": 7.5}}
    mgr, cfg = _mgr(tmp_path, [je])
    mgr.state_manager.state.record_mark(557, 1.60, 160.0, 1, ts="t0",
                                        enrich={"is_net_spread": True})
    trig = _types.SimpleNamespace(trigger_type="time_stop", pnl_pct=0.0, message="t")
    mgr._log_exit(557, "AA", trig, exit_price_per_share=1.60, quantity=1,
                  reason=mgr._exit_reason(trig))
    r = _read_dataset(cfg)[0]
    assert r["schema"] == "trade_dataset.v2"
    assert r["entry"]["structure"] == "spread"
    assert r["entry"]["spread"]["short_con_id"] == 999
    assert r["lifecycle"]["mark_path"][-1]["is_net_spread"] is True


# --------------------------------------------------------------------------- DECISION join E2E
def test_closed_trade_v2_joins_decision_context(tmp_path):
    """End-to-end: a decision captured at entry is joined into the closed-trade v2 record."""
    je = {"ts": "2026-07-01T14:00:00+00:00", "contract_id": 8001, "symbol": "SPY",
          "decision_id": "decision-8001",
          "right": "C", "strike": 610.0, "expiry": "20260731", "quantity": 1, "debit": 800.0,
          "profit_target_pct": 30.0, "stop_pct": 30.0, "conviction": 8,
          "entry_delta": 0.60, "entry_gamma": 0.03, "entry_theta": -0.5, "entry_vega": 1.1,
          "entry_iv": 0.22, "entry_bid": 7.9, "entry_ask": 8.1, "entry_spread_pct": 2.5}
    mgr, cfg = _mgr(tmp_path, [je])
    d = mgr._dataset_dir()
    idea = _types.SimpleNamespace(underlying="SPY", conviction=8, thesis="bull above 20d",
                                  direction="bullish", structure="single")
    tc.capture_decision(d, source="trader", symbol="SPY", right="C", strike=610.0,
                        expiry="20260731", structure="single", con_id=8001,
                        decision_id="decision-8001",
                        chosen_idea=idea, candidates=[idea],
                        raw_strategist="buy SPY 610C, conviction 8",
                        gate=_types.SimpleNamespace(approved=True, reasons=[], per_trade_cap=999.0),
                        regime={"regime": "bull"}, market_context="brief text",
                        construction={"tp_pct": 30.0, "sl_pct": 30.0, "dte": 30})
    mgr.state_manager.state.record_mark(8001, 8.0, 800.0, 1, ts="t0")
    trig = _types.SimpleNamespace(trigger_type="profit_target", pnl_pct=30.0, message="tp hit")
    mgr._log_exit(8001, "SPY", trig, exit_price_per_share=10.4, quantity=1,
                  reason=mgr._exit_reason(trig),
                  extra={"avg_fill_price": 10.4, "trigger_mark": 10.5,
                         "rule_fired": "profit_target", "exit_reasoning": "tp hit",
                         "iv": 0.25, "delta": 0.7, "gamma": 0.02, "theta": -0.3, "vega": 0.8})
    r = _read_dataset(cfg)[0]
    # decision joined
    assert r["decision"] is not None
    assert r["decision"]["chosen"]["conviction"] == 8
    assert "buy SPY 610C" in r["decision"]["raw_strategist"]
    assert r["decision"]["gate"]["approved"] is True
    # entry greeks/liquidity surfaced
    assert r["entry"]["entry_gamma"] == 0.03 and r["entry"]["entry_theta"] == -0.5
    assert r["entry"]["entry_bid"] == 7.9 and r["entry"]["entry_spread_pct"] == 2.5
    # close: rule + reasoning + exit greeks + slippage (fill 10.4 vs trigger mark 10.5 = -0.10)
    assert r["close"]["rule_fired"] == "profit_target"
    assert r["close"]["exit_reasoning"] == "tp hit"
    assert r["close"]["exit_gamma"] == 0.02 and r["close"]["exit_theta"] == -0.3
    assert r["close"]["trigger_mark"] == 10.5
    assert r["close"]["slippage_per_share"] == pytest.approx(-0.10)
    assert r["close"]["slippage_pct"] == pytest.approx(-0.95, abs=0.05)


# --------------------------------------------------------------------------- record-only safety
def test_dataset_join_failure_cannot_break_exit_logging(tmp_path, monkeypatch):
    """A bug in the decision-context join must NOT prevent the exit record from being written
    (record-only guarantee). Force load_decision_context to raise and confirm exits.log + the
    dataset row are still produced."""
    je = {"ts": "2026-07-01T14:00:00+00:00", "contract_id": 8100, "symbol": "MU",
          "right": "C", "strike": 100.0, "quantity": 1, "debit": 1000.0, "conviction": 6}
    mgr, cfg = _mgr(tmp_path, [je])
    def _boom(*a, **k):
        raise RuntimeError("join exploded")
    monkeypatch.setattr(tc, "load_decision_context", _boom)
    monkeypatch.setattr(tc, "load_review", _boom)
    mgr.state_manager.state.record_mark(8100, 10.0, 1000.0, 1, ts="t0")
    trig = _types.SimpleNamespace(trigger_type="stop", pnl_pct=-30.0, message="stop")
    # must not raise
    mgr._log_exit(8100, "MU", trig, exit_price_per_share=7.0, quantity=1,
                  reason=mgr._exit_reason(trig))
    r = _read_dataset(cfg)[0]
    assert r["schema"] == "trade_dataset.v2" and r["con_id"] == 8100
    assert r["decision"] is None and r["review"] is None       # join failed -> None, not crash
    assert r["close"]["realized_pnl"] == pytest.approx(-300.0)  # close still fully recorded


# --------------------------------------------------------------------------- day-start (P3.9)
def test_day_start_pot_hardening():
    # 1. bad net_liq never SETS a baseline (a $0/None read can't poison the breaker)
    b0, s0 = day_start_pot({}, "2026-07-02", 0.0)
    assert b0 == 0.0 and "2026-07-02" not in s0
    b_none, _ = day_start_pot({}, "2026-07-02", None)
    assert b_none == 0.0
    # 2. a good read SETS it, and it is STICKY within the day (a later different read cannot move it)
    base, store = day_start_pot({}, "2026-07-02", 100000.0)
    assert base == 100000.0 and store["2026-07-02"] == 100000.0
    base2, store2 = day_start_pot(store, "2026-07-02", 91000.0)   # -9% intraday
    assert base2 == 100000.0                                       # unchanged -> breaker fires correctly
    # 3. a fresh day rolls over and drops stale prior days
    base3, store3 = day_start_pot(store2, "2026-07-03", 90000.0)
    assert base3 == 90000.0 and list(store3.keys()) == ["2026-07-03"]
    # 4. a bad read on a day with no baseline yet keeps the prior store intact
    _, store4 = day_start_pot(store3, "2026-07-04", float("nan"))
    assert "2026-07-04" not in store4 and store4["2026-07-03"] == 90000.0


def test_trading_day_is_eastern():
    """P3.9: baseline is keyed to the US/Eastern trading day, not UTC -- so the 20:00-00:00 UTC
    window (evening ET) is NOT mislabeled as the next day."""
    from datetime import datetime as _dt, timezone as _tz
    # 2026-07-02 23:30 UTC = 19:30 ET on 2026-07-02 (still the 2nd in ET)
    assert _trading_day(_dt(2026, 7, 2, 23, 30, tzinfo=_tz.utc)) == "2026-07-02"
    # 2026-07-03 01:00 UTC = 21:00 ET on 2026-07-02 -> ET date is still the 2nd (UTC said the 3rd)
    assert _trading_day(_dt(2026, 7, 3, 1, 0, tzinfo=_tz.utc)) == "2026-07-02"
