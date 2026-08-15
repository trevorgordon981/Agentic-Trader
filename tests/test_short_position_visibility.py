"""SHORT POSITION VISIBILITY (2026-07-26).

THE DEFECT
    IBConnection.get_positions() filtered on `position_qty > 0`, so a FILLED cash-secured put --
    which has a NEGATIVE quantity -- never entered the position book at all. It existed at the
    broker and nowhere in this process: no stop, no take-profit, no assignment handling, no line
    in any report, until the day it was assigned and 100 shares appeared out of nowhere.

THE FIX, AND ITS DELIBERATE LIMIT
    get_positions() gained OPT-IN keywords. The default is byte-identical (long options only) --
    not out of caution but because a consumer audit showed that handing negatives to the existing
    consumers trades invisibility for CORRUPTION: construction.open_book_items() would add a short
    put's MAX LOSS to the long-premium deployment book (blowing the 40% deployed and 4%/day theta
    caps and locking out every entry, credit and debit alike), risk.evaluate_trade()'s single-name
    aggregate would do the same, rules.py returns None for every stop/target on `quantity <= 0`,
    and order.py's close path hardcodes action='SELL' and would size an order off a negative
    quantity. So the exit manager now ASKS for shorts, keeps them in a separate book, reports them
    with correctly-signed P&L, and handles assignment -- and refuses to route them into the
    long-only exit path until that path is made short-aware.

WHAT THESE TESTS PROVE
    1. a short put appears, with its sign preserved (never abs()'d)
    2. long behaviour is byte-identical -- differential, over a realistic mixed book
    3. every audited consumer behaves correctly given a short
    4. short P&L has the RIGHT SIGN: a CSP whose price FELL is a GAIN
    5. a CSP stays visible across cycles, not only on the fill cycle
    6. the assignment transition is clean -- no crash, no orphan, no mystery position

IBKR is fully mocked throughout. No order is placed and no broker is contacted.
"""
import asyncio
import json
import os
import types

import pytest
from unittest.mock import AsyncMock, MagicMock

from exitmgr.config import Config
from exitmgr.connection import IBConnection, PositionData
from exitmgr.manager import ExitManager


# ============================================================================== broker doubles
def _raw(con_id, right, qty, *, symbol="SPY", avg_cost=1.75, expiry="20260918",
         sec_type="OPT", strike=50.0):
    """A reqPositionsAsync() row. Deliberately NOT a MagicMock for the fields under test: the
    whole bug class here is a field being read as something it is not."""
    p = types.SimpleNamespace()
    p.contract = types.SimpleNamespace(
        conId=con_id, symbol=symbol, right=right, secType=sec_type, strike=strike,
        lastTradeDateOrContractMonth=expiry)
    p.position = qty
    p.avgCost = avg_cost
    return p


def _conn(rows):
    c = IBConnection(host="h", port=1, client_id=2)
    c._connected = True
    c.ib = MagicMock()
    c.ib.reqPositionsAsync = AsyncMock(return_value=rows)
    return c


# A realistic book: two long calls, a long put, the SHORT leg of a debit call spread, a
# standalone SHORT put (the CSP), and 100 shares of assigned stock.
def _realistic_book():
    return [
        _raw(101, "C", 2, symbol="RKLB", avg_cost=4.10, expiry="20260918", strike=20.0),
        _raw(102, "C", 1, symbol="NVDA", avg_cost=12.30, expiry="20261218", strike=180.0),
        _raw(103, "P", 3, symbol="QQQ", avg_cost=2.05, expiry="20260821", strike=560.0),
        _raw(104, "C", -2, symbol="RKLB", avg_cost=1.05, expiry="20260918", strike=25.0),
        _raw(105, "P", -1, symbol="SPY", avg_cost=1.75, expiry="20260918", strike=500.0),
        _raw(106, "", 100, symbol="SPY", avg_cost=498.25, expiry="", sec_type="STK", strike=0.0),
    ]


# The six fields get_positions() has always populated. "Byte-identical for longs" is asserted
# over exactly these -- the two new fields (sec_type/strike) are additive and defaulted.
_LEGACY_FIELDS = ("con_id", "symbol", "right", "quantity", "avg_cost", "expiry")


def _legacy_view(book):
    return {cid: tuple(getattr(pd, f) for f in _LEGACY_FIELDS) for cid, pd in book.items()}


# ================================================================= 1. the short becomes visible
def test_short_put_appears_with_negative_quantity():
    """The headline: opt in, and the cash-secured put is THERE -- and it is negative."""
    out = asyncio.run(_conn(_realistic_book()).get_positions(include_short=True))
    assert 105 in out, "the filled cash-secured put is still invisible"
    csp = out[105]
    assert csp.quantity == -1, "the sign was stripped -- a short must never look like a long"
    assert csp.is_short is True
    assert csp.right == "P" and csp.symbol == "SPY" and csp.strike == 500.0


def test_short_quantity_is_never_abs_ed():
    """A short reported with a positive quantity would be read as a LONG by every consumer that
    does not check -- a worse bug than invisibility. Guard the sign explicitly."""
    out = asyncio.run(_conn(_realistic_book()).get_positions(include_short=True))
    assert all(out[c].quantity < 0 for c in (104, 105))
    assert all(out[c].quantity > 0 for c in (101, 102, 103))


def test_default_call_still_excludes_every_short():
    """The default is the pre-existing contract, unchanged, and every legacy caller uses it."""
    out = asyncio.run(_conn(_realistic_book()).get_positions())
    assert set(out) == {101, 102, 103}
    assert all(pd.quantity > 0 for pd in out.values())


# ============================================================ 2. longs are byte-identical (diff)
def test_long_positions_are_byte_identical_differential():
    """DIFFERENTIAL: over a realistic mixed book, the long rows produced with shorts/stock
    switched ON are field-for-field identical to the rows produced with them OFF, which in turn
    match the frozen pre-change expectation."""
    conn = _conn(_realistic_book())
    default = asyncio.run(conn.get_positions())
    with_short = asyncio.run(conn.get_positions(include_short=True))
    with_both = asyncio.run(conn.get_positions(include_short=True, include_stock=True))

    frozen = {
        101: (101, "RKLB", "C", 2, 4.10, "20260918"),
        102: (102, "NVDA", "C", 1, 12.30, "20261218"),
        103: (103, "QQQ", "P", 3, 2.05, "20260821"),
    }
    assert _legacy_view(default) == frozen

    longs_with_short = {k: v for k, v in with_short.items() if v.quantity > 0}
    longs_with_both = {k: v for k, v in with_both.items()
                       if v.quantity > 0 and v.sec_type != "STK"}
    assert _legacy_view(longs_with_short) == frozen
    assert _legacy_view(longs_with_both) == frozen
    # ...and the objects themselves compare equal, new fields included.
    assert {k: with_short[k] for k in frozen} == {k: default[k] for k in frozen}


def test_long_only_book_is_completely_unaffected_by_the_new_flags():
    """A book with no shorts at all yields the identical result under every flag combination."""
    rows = [_raw(201, "C", 1, symbol="AAPL"), _raw(202, "P", 5, symbol="TSLA")]
    conn = _conn(rows)
    a = asyncio.run(conn.get_positions())
    b = asyncio.run(conn.get_positions(include_short=True))
    c = asyncio.run(conn.get_positions(include_short=True, include_stock=True))
    assert a == b == c and set(a) == {201, 202}


def test_zero_quantity_row_is_excluded_everywhere():
    """A flat row is neither long nor short and must not appear under any flag."""
    rows = [_raw(301, "P", 0, symbol="SPY")]
    conn = _conn(rows)
    assert asyncio.run(conn.get_positions()) == {}
    assert asyncio.run(conn.get_positions(include_short=True, include_stock=True)) == {}


# ============================================================== 3. instrument-type discrimination
def test_assigned_stock_with_residual_option_fields_is_not_treated_as_a_short():
    """HOSTILE CASE: IBKR can echo a right/strike on a STOCK row left by an assignment. secType
    has to be what excludes it -- the option-shaped fields do not. Closing that as an option
    would be a wrong-instrument order."""
    rows = [_raw(401, "P", -100, symbol="SPY", sec_type="STK", strike=500.0)]
    out = asyncio.run(_conn(rows).get_positions(include_short=True))
    assert out == {}, "a STOCK row was mistaken for a short option"


def test_stock_is_excluded_unless_explicitly_requested():
    conn = _conn(_realistic_book())
    assert 106 not in asyncio.run(conn.get_positions())
    assert 106 not in asyncio.run(conn.get_positions(include_short=True))
    both = asyncio.run(conn.get_positions(include_short=True, include_stock=True))
    assert both[106].sec_type == "STK" and both[106].right == "" and both[106].quantity == 100


def test_short_with_unreadable_sectype_is_still_reported():
    """An unreadable secType must not silently DROP a real short -- that is the original bug in
    a new costume. Unknown -> treated as an option -> visible."""
    p = types.SimpleNamespace()
    p.contract = types.SimpleNamespace(conId=501, symbol="SPY", right="P", strike=500.0,
                                       lastTradeDateOrContractMonth="20260918")  # no secType
    p.position = -1
    p.avgCost = 1.75
    out = asyncio.run(_conn([p]).get_positions(include_short=True))
    assert 501 in out and out[501].quantity == -1


# =========================================================================== manager scaffolding
def _mgr(tmp_path, journal_lines=(), *, rows=()):
    cfg = Config()
    cfg.dry_run = True
    cfg.loop_mode = False
    cfg.journal.path = str(tmp_path / "trades.log")
    cfg.state.path = str(tmp_path / "state.json")
    cfg.kill_switch.path = str(tmp_path / "KILL")
    cfg.audit_path = str(tmp_path / "audit.jsonl")
    (tmp_path / "trades.log").write_text(
        "".join(json.dumps(x) + "\n" for x in journal_lines))
    mgr = ExitManager(cfg)
    mgr.ib_conn = _conn(list(rows))
    mgr.ib_conn.ib.portfolio = MagicMock(return_value=[])
    return mgr, cfg


# A journaled cash-secured put exactly as trader._journal_entry writes one.
CSP_JOURNAL = {
    "ts": "2026-07-20T14:00:00+00:00", "contract_id": 105, "symbol": "SPY", "right": "P",
    "strike": 500.0, "expiry": "20260918", "side": "credit", "structure": "cash secured put",
    "action": "SELL", "quantity": -1, "contracts": 1, "collateral_usd": 50000.0,
    "net_credit_usd": 175.0, "max_loss_usd": 49825.0, "debit": 49825.0,
    "assignment_possible": True, "conviction": 7, "profit_target_pct": 50.0, "stop_pct": 100.0,
}
LONG_JOURNAL = {
    "ts": "2026-07-20T14:00:00+00:00", "contract_id": 101, "symbol": "RKLB", "right": "C",
    "strike": 20.0, "expiry": "20260918", "quantity": 2, "debit": 820.0, "conviction": 6,
}


def _exits(cfg):
    p = os.path.join(os.path.dirname(cfg.journal.path) or ".", "exits.log")
    if not os.path.exists(p):
        return []
    with open(p) as f:
        return [json.loads(l) for l in f if l.strip()]


# ================================================== 3. consumer audit -- behaviour with a short
def test_fetch_position_book_splits_and_the_long_book_is_unchanged(tmp_path):
    """AUDITED CONSUMER: every long-only consumer in manager.py is fed `longs`. Prove the split
    hands them exactly the legacy view and nothing else."""
    mgr, _ = _mgr(tmp_path, [CSP_JOURNAL], rows=_realistic_book())
    longs, shorts, stocks = asyncio.run(mgr._fetch_position_book())
    assert set(longs) == {101, 102, 103}
    assert set(shorts) == {104, 105}
    assert set(stocks) == {106}
    assert _legacy_view(longs) == _legacy_view(
        asyncio.run(_conn(_realistic_book()).get_positions()))
    assert mgr._short_positions is not None and set(mgr._short_positions) == {104, 105}


def test_fetch_position_book_falls_back_when_the_connection_rejects_the_keywords(tmp_path):
    """A connection/mock with the OLD signature must degrade to long-only, never take the cycle
    down. Short visibility is an improvement, not a new single point of failure."""
    mgr, _ = _mgr(tmp_path)
    calls = []

    async def _old_signature(*args, **kwargs):
        # the pre-2026-07-26 signature took no arguments at all
        if args or kwargs:
            raise TypeError("get_positions() takes 1 positional argument")
        calls.append("plain")
        return {101: PositionData(101, "RKLB", "C", 2, 4.10, "20260918")}

    mgr.ib_conn.get_positions = _old_signature
    longs, shorts, stocks = asyncio.run(mgr._fetch_position_book())
    assert calls == ["plain"]
    assert set(longs) == {101} and shorts == {} and stocks == {}


def test_unparseable_quantity_defaults_to_the_long_book(tmp_path):
    """Only a PROVABLY short row is diverted. Anything unreadable lands where get_positions() has
    always put it, so no consumer silently loses a position to the new split."""
    mgr, _ = _mgr(tmp_path)
    weird = MagicMock()
    weird.quantity = object()
    weird.sec_type = "OPT"
    mgr.ib_conn.get_positions = AsyncMock(return_value={999: weird})
    longs, shorts, stocks = asyncio.run(mgr._fetch_position_book())
    assert set(longs) == {999} and shorts == {} and stocks == {}


def test_reconcile_is_handed_only_longs_and_stays_safe_with_a_csp_open(tmp_path):
    """AUDITED CONSUMER: state.reconcile_state compares an in-flight close's remaining_qty to the
    live quantity. A negative live quantity makes that comparison inconsistent -> safe=False ->
    exits withheld AND all entries halted account-wide. Shorts are excluded from reconcile for
    that stated reason; with a CSP open, reconcile must still come back SAFE."""
    mgr, _ = _mgr(tmp_path, [CSP_JOURNAL], rows=_realistic_book())
    mgr.ib_conn.get_open_orders = AsyncMock(return_value={})
    assert asyncio.run(mgr._reconcile_on_startup()) is True
    assert mgr._reconcile_bad_con_ids == set()


def test_a_short_never_reaches_the_long_only_management_path(tmp_path):
    """AUDITED CONSUMER: rules.py/order.py/state.py. rules.evaluate_* returns None for every
    stop/target when quantity <= 0 and order.py hardcodes SELL, so a short reaching that path is
    a wrong-side order, not a visible error. Run a real cycle with ONLY a CSP open and assert
    nothing was evaluated and no order was built."""
    rows = [_raw(105, "P", -1, symbol="SPY", strike=500.0, avg_cost=1.75)]
    mgr, _ = _mgr(tmp_path, [CSP_JOURNAL], rows=rows)
    mgr.ib_conn.get_open_orders = AsyncMock(return_value={})
    mgr.ib_conn.fetch_quotes = AsyncMock(return_value={})
    mgr.order_manager.place_close_order = AsyncMock(
        side_effect=AssertionError("an exit order was built for a SHORT position"))
    asyncio.run(mgr.run_cycle(dry_run=True))
    assert mgr.order_manager.place_close_order.await_count == 0


def test_scope_selection_never_includes_a_short(tmp_path):
    """AUDITED CONSUMER: _get_scope_con_ids. In scope='journal' mode it intersects the journal
    with the live book -- and the CSP IS journaled, so only the long/short split keeps it out."""
    mgr, _ = _mgr(tmp_path, [CSP_JOURNAL, LONG_JOURNAL], rows=_realistic_book())
    longs, _s, _k = asyncio.run(mgr._fetch_position_book())
    assert 105 not in mgr._get_scope_con_ids(longs)
    assert 101 in mgr._get_scope_con_ids(longs)


def test_backstop_refuses_a_short_that_reaches_managed_positions(tmp_path, capsys):
    """Defence in depth: if a future refactor ever feeds the wrong book in, the manager must
    REFUSE loudly rather than build a SELL order off a negative quantity."""
    poisoned = [_raw(105, "P", -1, symbol="SPY", strike=500.0)]
    mgr, _ = _mgr(tmp_path, [CSP_JOURNAL], rows=poisoned)
    # deliberately hand the long-only pipeline a short, as a broken refactor would
    mgr._fetch_position_book = AsyncMock(return_value=(
        {105: PositionData(105, "SPY", "P", -1, 1.75, "20260918")}, {}, {}))
    mgr.ib_conn.get_open_orders = AsyncMock(return_value={})
    mgr.order_manager.place_close_order = AsyncMock(
        side_effect=AssertionError("a SHORT was routed into the long-only close path"))
    asyncio.run(mgr.run_cycle(dry_run=True))
    assert "is SHORT" in capsys.readouterr().out
    assert mgr.order_manager.place_close_order.await_count == 0


def test_trader_open_positions_counts_a_csp_at_collateral(tmp_path):
    """AUDITED CONSUMER: trader._open_positions() -> risk.evaluate_trade caps + max_concurrent.

    SUPERSEDED 2026-07-26 (credit exit wiring). This test previously asserted the OPPOSITE -- that
    a CSP never reaches this list -- and called it "the documented, accepted boundary of this
    change". That boundary was the max_concurrent gap: the put counted as ZERO open positions on
    every cycle after the fill. It is now folded in DELIBERATELY, valued at COLLATERAL
    ($500 strike x 100 = $50,000), by a path that reads get_positions(include_short=True)
    directly. What must still never happen is the thing the next test measures: the credit row
    reaching construction.open_book_items via the long-only get_positions() default. That default
    is untouched, which is why both properties can hold at once."""
    from exitmgr.trader import Trader
    (tmp_path / "trades.log").write_text(json.dumps(CSP_JOURNAL) + "\n")
    t = Trader.__new__(Trader)
    t.ib_conn = _conn(_realistic_book())
    t.ib_conn.ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[])
    t.journal_path = str(tmp_path / "trades.log")
    t.audit_path = str(tmp_path / "audit.jsonl")
    out = asyncio.run(Trader._open_positions(t))
    spy = [p for p in out if p.underlying == "SPY"]
    assert len(spy) == 1, "the CSP must occupy exactly one slot in the concurrent book"
    assert spy[0].is_credit is True
    assert spy[0].notional == 50_000.0, "a CSP is valued at collateral, not premium/max-loss"
    # the SHORT CALL (con 104, a spread's short leg) is still excluded -- it is counted through
    # its long leg's journaled net debit, and a naked call must not exist in this account.
    assert all(not (p.underlying == "RKLB" and p.notional == 2_500.0) for p in out)


def test_construction_open_book_would_be_poisoned_by_a_short(tmp_path):
    """THE MEASURED REASON the default was not flipped. construction.open_book_items() keys off
    the journal by con_id and reads `debit` -- which a credit row deliberately sets to MAX LOSS.
    Feed it a short and the long-premium deployment book inflates by ~$49,825, which blows the
    40%-deployed and 4%/day theta caps and rejects EVERY entry, credit and debit alike."""
    from exitmgr import construction
    (tmp_path / "trades.log").write_text(json.dumps(CSP_JOURNAL) + "\n")
    short = {105: PositionData(105, "SPY", "P", -1, 1.75, "20260918")}
    poisoned = construction.open_book_items(short, str(tmp_path / "trades.log"), [])
    assert poisoned, "expected the short to enter the deployment book"
    assert max(float(b) for b, _dte in poisoned.values()) > 40_000
    # ...whereas the long-only book this change actually ships is empty, as it always was.
    assert construction.open_book_items({}, str(tmp_path / "trades.log"), []) == {}


# ================================================================ 4. P&L SIGN -- the sharp edge
def test_short_pnl_a_csp_whose_price_fell_is_a_GAIN():
    """THE core sign test. Sold for $1.75 (credit $175), now marked $0.50 -> +$125.
    Under the LONG formula this same position books as roughly -100%, which would trip a stop on
    a winning trade."""
    r = ExitManager.short_pnl(CSP_JOURNAL, 0.50, 1)
    assert r["credit_usd"] == 175.0
    assert r["cost_to_close_usd"] == 50.0
    assert r["pnl_usd"] == 125.0 and r["pnl_usd"] > 0
    assert r["pct_of_credit"] == pytest.approx(71.43, abs=0.01)


def test_short_pnl_a_csp_whose_price_rose_is_a_LOSS():
    r = ExitManager.short_pnl(CSP_JOURNAL, 3.00, 1)
    assert r["pnl_usd"] == -125.0 and r["pnl_usd"] < 0
    assert r["pct_of_credit"] < 0


def test_short_pnl_is_monotonically_decreasing_in_price():
    """The whole relationship, not one point: as the option gets more expensive the short gets
    worse. A sign error anywhere breaks this ordering."""
    pnls = [ExitManager.short_pnl(CSP_JOURNAL, px, 1)["pnl_usd"]
            for px in (0.0, 0.5, 1.75, 3.0, 10.0)]
    assert pnls == sorted(pnls, reverse=True)
    assert pnls[0] == 175.0      # worthless -> the entire credit is kept
    assert pnls[2] == 0.0        # back to the entry credit -> flat


def test_short_pnl_scales_with_contracts():
    r = ExitManager.short_pnl(dict(CSP_JOURNAL, quantity=-3, net_credit_usd=525.0), 0.50, 3)
    assert r["cost_to_close_usd"] == 150.0 and r["pnl_usd"] == 375.0


def test_short_pnl_refuses_to_guess():
    """An unknown mark or an unknown credit yields None, never 0.0. A fabricated zero reads as
    'flat' and is indistinguishable from a real flat position."""
    assert ExitManager.short_pnl(CSP_JOURNAL, None, 1)["pnl_usd"] is None
    assert ExitManager.short_pnl(CSP_JOURNAL, float("nan"), 1)["pnl_usd"] is None
    assert ExitManager.short_pnl({"quantity": -1}, 0.50, 1)["pnl_usd"] is None


def test_is_credit_row_detects_a_short_by_either_signal():
    assert ExitManager._is_credit_row(CSP_JOURNAL) is True
    assert ExitManager._is_credit_row({"quantity": -2}) is True
    assert ExitManager._is_credit_row({"side": "credit"}) is True
    assert ExitManager._is_credit_row(LONG_JOURNAL) is False
    assert ExitManager._is_credit_row(None) is False
    assert ExitManager._is_credit_row({"quantity": "junk"}) is False


def test_log_exit_books_a_cheap_buyback_as_a_profit(tmp_path):
    """END TO END through the real exit-record path: a CSP bought back at $0.10 is +$165, not the
    ~-$4,860 the long formula produced. The record is self-describing (side=credit, is_short) and
    keeps the negative quantity so no downstream consumer can mistake it for a long."""
    mgr, cfg = _mgr(tmp_path, [CSP_JOURNAL])
    trig = types.SimpleNamespace(trigger_type="profit_target", pnl_pct=0.0, message="")
    mgr._log_exit(105, "SPY", trig, exit_price_per_share=0.10, quantity=-1,
                  reason="profit_target", extra={"exit_event": "manual"})
    rows = _exits(cfg)
    assert len(rows) == 1
    r = rows[0]
    assert r["realized_pnl"] == 165.0 and r["realized_pnl"] > 0
    assert r["side"] == "credit" and r["is_short"] is True
    assert r["quantity"] == -1 and r["contracts"] == 1
    assert r["entry_credit_usd"] == 175.0 and r["close_cost_usd"] == 10.0
    assert r["realized_pnl_pct"] > 0


def test_log_exit_books_an_expensive_buyback_as_a_loss(tmp_path):
    mgr, cfg = _mgr(tmp_path, [CSP_JOURNAL])
    trig = types.SimpleNamespace(trigger_type="stop", pnl_pct=0.0, message="")
    mgr._log_exit(105, "SPY", trig, exit_price_per_share=5.00, quantity=-1, reason="stop")
    r = _exits(cfg)[0]
    assert r["realized_pnl"] == -325.0 and r["realized_pnl_pct"] < 0


def test_log_exit_long_path_is_byte_identical(tmp_path):
    """DIFFERENTIAL on the P&L path: a LONG close must produce exactly the numbers it always did,
    with none of the credit fields present."""
    mgr, cfg = _mgr(tmp_path, [LONG_JOURNAL])
    trig = types.SimpleNamespace(trigger_type="profit_target", pnl_pct=0.0, message="")
    mgr._log_exit(101, "RKLB", trig, exit_price_per_share=6.15, quantity=2,
                  reason="profit_target")
    r = _exits(cfg)[0]
    assert r["proceeds"] == 1230.0                       # 6.15 * 100 * 2
    assert r["entry_debit"] == 820.0
    assert r["realized_pnl"] == 410.0
    assert r["realized_pnl_pct"] == 50.0
    assert r["structure"] == "single" and r["quantity"] == 2
    for k in ("side", "is_short", "entry_credit_usd", "close_cost_usd", "pnl_basis"):
        assert k not in r, f"a credit-only field ({k}) leaked onto a long exit record"


def test_expired_worthless_short_keeps_the_whole_credit(tmp_path):
    """A short put that expires OTM is the MAXIMUM WIN. The long path books exactly the opposite
    (-100% of debit) from the same inputs."""
    mgr, cfg = _mgr(tmp_path, [CSP_JOURNAL])
    mgr._emit_expiry_close(105, CSP_JOURNAL, spot=520.0)     # spot > strike -> intrinsic 0
    r = _exits(cfg)[0]
    assert r["realized_pnl"] == 175.0
    assert r["realized_pnl_pct"] == 100.0
    assert "full credit kept" in r["exit_reasoning"]
    assert r["assigned"] is False


def test_expired_itm_short_is_flagged_as_an_assignment(tmp_path):
    """ITM at expiry is not an expiry, it is an assignment: intrinsic $5 costs $500 against $175
    collected -> -$325, and the resulting shares are named rather than left a mystery."""
    mgr, cfg = _mgr(tmp_path, [CSP_JOURNAL])
    mgr._emit_expiry_close(105, CSP_JOURNAL, spot=495.0)     # 500 - 495 = 5.00 intrinsic
    r = _exits(cfg)[0]
    assert r["realized_pnl"] == -325.0
    assert r["assigned"] is True and r["assigned_shares"] == 100
    assert r["side"] == "credit"


def test_expired_long_is_unchanged(tmp_path):
    """DIFFERENTIAL: the same expiry path on a LONG still books -100% of debit when worthless."""
    mgr, cfg = _mgr(tmp_path, [LONG_JOURNAL])
    mgr._emit_expiry_close(101, LONG_JOURNAL, spot=15.0)     # 20C, spot 15 -> worthless
    r = _exits(cfg)[0]
    assert r["realized_pnl"] == -820.0 and r["realized_pnl_pct"] == -100.0
    assert "assigned" not in r


# ============================================== 5. visible on EVERY cycle, not just the fill one
def test_csp_is_reported_on_every_cycle_not_only_the_fill_cycle(tmp_path):
    """The specific gap that made a CSP unmanaged: it was counted once, on the cycle it filled,
    then vanished. Run three cycles and assert it is present, priced and correctly flagged every
    single time -- including on a book that holds NOTHING else, which used to print
    'No positions to evaluate' and go silent.

    UPDATED 2026-07-26 (credit exit wiring): the flag was `managed is False` with reason
    "short_exit_path_not_implemented", because there was no short exit path. There is one now, so
    a journaled CSP with a usable credit basis and a live mark reports MANAGED -- the unconditional
    UNMANAGED would now be the more dangerous of the two lies. The unmanaged branch is still
    asserted, by the tests that remove the basis (test_an_unjournaled_short_is_still_reported here,
    and the refusal tests in tests/test_credit_wiring.py)."""
    rows = [_raw(105, "P", -1, symbol="SPY", strike=500.0, avg_cost=1.75)]
    mgr, _ = _mgr(tmp_path, [CSP_JOURNAL], rows=rows)
    mgr.ib_conn.get_open_orders = AsyncMock(return_value={})
    mgr.ib_conn.fetch_quotes = AsyncMock(return_value={})
    mark = types.SimpleNamespace(contract=types.SimpleNamespace(conId=105), marketPrice=0.50)
    mgr.ib_conn.ib.portfolio = MagicMock(return_value=[mark])

    for cycle in range(3):
        asyncio.run(mgr.run_cycle(dry_run=True))
        assert len(mgr._short_report) == 1, f"the CSP went invisible on cycle {cycle}"
        row = mgr._short_report[0]
        assert row["con_id"] == 105
        assert row["quantity"] == -1            # sign preserved in the report too
        assert row["contracts"] == 1
        assert row["collateral_usd"] == 50000.0
        assert row["unrealized_pnl_usd"] == 125.0     # 175 credit - 50 to close -> a GAIN
        assert row["managed"] is True and row["unmanaged_reason"] is None


def test_short_report_counts_a_csp_toward_the_concurrent_book(tmp_path):
    """max_concurrent equivalence at the layer this change owns: two open CSPs are two positions
    on every cycle, not zero."""
    rows = [_raw(105, "P", -1, symbol="SPY", strike=500.0),
            _raw(107, "P", -2, symbol="QQQ", strike=540.0)]
    j2 = dict(CSP_JOURNAL, contract_id=107, symbol="QQQ", strike=540.0, quantity=-2,
              net_credit_usd=400.0, collateral_usd=108000.0, max_loss_usd=107600.0,
              debit=107600.0)
    mgr, _ = _mgr(tmp_path, [CSP_JOURNAL, j2], rows=rows)
    mgr.ib_conn.get_open_orders = AsyncMock(return_value={})
    mgr.ib_conn.fetch_quotes = AsyncMock(return_value={})
    asyncio.run(mgr.run_cycle(dry_run=True))
    asyncio.run(mgr.run_cycle(dry_run=True))
    assert len(mgr._short_report) == 2
    assert {r["con_id"] for r in mgr._short_report} == {105, 107}
    assert sum(r["contracts"] for r in mgr._short_report) == 3


def test_an_unjournaled_short_is_still_reported(tmp_path):
    """A short we have no journal row for (a manual TWS sale) must still be VISIBLE -- flagged
    as unjournaled with a null P&L, never silently dropped and never given a made-up number."""
    rows = [_raw(900, "P", -1, symbol="AMD", strike=140.0)]
    mgr, _ = _mgr(tmp_path, [], rows=rows)
    mgr.ib_conn.get_open_orders = AsyncMock(return_value={})
    mgr.ib_conn.fetch_quotes = AsyncMock(return_value={})
    asyncio.run(mgr.run_cycle(dry_run=True))
    assert len(mgr._short_report) == 1
    row = mgr._short_report[0]
    assert row["journaled"] is False
    assert row["unrealized_pnl_usd"] is None
    assert row["collateral_usd"] == 14000.0     # strike*100 off the broker row


# ==================================================================== 6. assignment transition
def test_assignment_emits_a_terminal_row_and_does_not_orphan_the_journal(tmp_path):
    """THE TRANSITION: the option row disappears and 100 shares appear. Before this change the
    journal row simply sat there forever (dte >= 0 skipped it) while nobody managed the stock."""
    mgr, cfg = _mgr(tmp_path, [CSP_JOURNAL])
    shorts = {}                                                    # option gone
    stocks = {106: PositionData(106, "SPY", "", 100, 498.25, "", sec_type="STK")}
    mgr._spot_price = AsyncMock(return_value=495.0)
    asyncio.run(mgr._process_short_assignments(shorts, stocks))

    rows = _exits(cfg)
    assert len(rows) == 1
    r = rows[0]
    assert r["reason"] == "assigned" and r["assigned"] is True
    assert r["assigned_shares"] == 100 and r["side"] == "credit"
    assert r["realized_pnl"] == -325.0        # 175 credit - 500 intrinsic
    assert r["contract_id"] == 105
    # the journal row itself stays readable and self-describing
    mgr._load_journal()
    assert mgr._journal_entries[105]["side"] == "credit"
    # ...and the dead option's record-only tracking is released
    assert "105" not in mgr.state_manager.state.peak_prices


def test_assignment_is_recorded_exactly_once(tmp_path):
    mgr, cfg = _mgr(tmp_path, [CSP_JOURNAL])
    stocks = {106: PositionData(106, "SPY", "", 100, 498.25, "", sec_type="STK")}
    mgr._spot_price = AsyncMock(return_value=495.0)
    for _ in range(3):
        asyncio.run(mgr._process_short_assignments({}, stocks))
    assert len(_exits(cfg)) == 1


def test_disappearance_without_stock_is_not_declared_an_assignment(tmp_path, capsys):
    """A CSP bought back manually also leaves the option book. With no matching shares there is
    no EVIDENCE of assignment, so nothing is fabricated -- it is reported and left for the
    execution capture to reconcile."""
    mgr, cfg = _mgr(tmp_path, [CSP_JOURNAL])
    mgr._spot_price = AsyncMock(return_value=495.0)
    asyncio.run(mgr._process_short_assignments({}, {}))
    assert _exits(cfg) == []
    assert "NOT assignment" in capsys.readouterr().out


def test_a_live_short_is_never_mistaken_for_assigned(tmp_path):
    """The short book has to be consulted, not just the long one -- a live CSP is absent from the
    long book BY CONSTRUCTION, and treating that as gone would close out an open position."""
    mgr, cfg = _mgr(tmp_path, [CSP_JOURNAL])
    live = {105: PositionData(105, "SPY", "P", -1, 1.75, "20260918", strike=500.0)}
    stocks = {106: PositionData(106, "SPY", "", 100, 498.25, "", sec_type="STK")}
    asyncio.run(mgr._process_short_assignments(live, stocks))
    assert _exits(cfg) == []


def test_a_live_short_is_never_treated_as_expired(tmp_path):
    """Same rule on the expiry path: _process_expiries is handed the LONG book, so without the
    short book a live CSP past a stale expiry string would be booked as expired."""
    mgr, cfg = _mgr(tmp_path, [dict(CSP_JOURNAL, expiry="20200101")])
    live_short = {105: PositionData(105, "SPY", "P", -1, 1.75, "20200101", strike=500.0)}
    mgr._spot_price = AsyncMock(return_value=495.0)
    asyncio.run(mgr._process_expiries({}, live_shorts=live_short))
    assert _exits(cfg) == []
    # ...and with the short genuinely gone, it IS booked.
    asyncio.run(mgr._process_expiries({}, live_shorts={}))
    assert len(_exits(cfg)) == 1


def test_assignment_transition_runs_clean_through_a_whole_cycle(tmp_path):
    """No crash, no orphan, no mystery position: cycle 1 sees the CSP, cycle 2 sees the shares."""
    rows_before = [_raw(105, "P", -1, symbol="SPY", strike=500.0)]
    mgr, cfg = _mgr(tmp_path, [CSP_JOURNAL], rows=rows_before)
    mgr.ib_conn.get_open_orders = AsyncMock(return_value={})
    mgr.ib_conn.fetch_quotes = AsyncMock(return_value={})
    mgr._spot_price = AsyncMock(return_value=495.0)
    asyncio.run(mgr.run_cycle(dry_run=True))
    assert len(mgr._short_report) == 1 and _exits(cfg) == []

    # assignment: the option row is replaced by shares
    rows_after = [_raw(106, "", 100, symbol="SPY", sec_type="STK", strike=0.0, expiry="")]
    mgr.ib_conn.ib.reqPositionsAsync = AsyncMock(return_value=rows_after)
    asyncio.run(mgr.run_cycle(dry_run=True))

    assert mgr._short_report == []                  # the option is gone, correctly
    assert set(mgr._stock_positions) == {106}       # the shares are SEEN, not a mystery
    r = _exits(cfg)[0]
    assert r["reason"] == "assigned" and r["assigned_shares"] == 100


def test_assigned_stock_is_never_managed_as_an_option(tmp_path):
    """The shares must not enter the option management path: order.py would submit a stock close
    as an OPT contract."""
    rows = [_raw(106, "", 100, symbol="SPY", sec_type="STK", strike=0.0, expiry="")]
    mgr, _ = _mgr(tmp_path, [CSP_JOURNAL], rows=rows)
    mgr.ib_conn.get_open_orders = AsyncMock(return_value={})
    mgr.ib_conn.fetch_quotes = AsyncMock(return_value={})
    mgr._spot_price = AsyncMock(return_value=495.0)
    mgr.order_manager.place_close_order = AsyncMock(
        side_effect=AssertionError("an option close was built for a STOCK position"))
    asyncio.run(mgr.run_cycle(dry_run=True))
    longs, _s, stocks = asyncio.run(mgr._fetch_position_book())
    assert longs == {} and set(stocks) == {106}
    assert mgr.order_manager.place_close_order.await_count == 0


def test_credit_con_ids_tracks_the_journal(tmp_path):
    """The assignment detector keys off the journal, so the journal read has to be right --
    including dropping a con_id once a close tool has flattened it."""
    mgr, cfg = _mgr(tmp_path, [CSP_JOURNAL, LONG_JOURNAL])
    assert mgr._credit_con_ids == {105}
    with open(cfg.journal.path, "a") as f:
        f.write(json.dumps({"contract_id": 105, "symbol": "SPY", "event": "closed_by_tool",
                            "status": "Filled", "avg_fill_price": 0.10,
                            "tool": "close_symbol", "client_id": 91}) + "\n")
    mgr._load_journal()
    assert mgr._credit_con_ids == set()
