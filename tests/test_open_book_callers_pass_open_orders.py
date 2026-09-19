"""Public API contract; production-derived narrative omitted."""

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

import daily_recommend as dr
import place_trade as pt
from exitmgr.account import PotSnapshot
from exitmgr.strategist import TradeIdea
from exitmgr.trader import ResolvedOrder
from exitmgr import construction
from exitmgr.config import ConstructionConfig
from exitmgr.connection import IBConnection

_APP = Path(__file__).resolve().parent.parent
_FUTURE_EXPIRY = "20271217"
_NET_LIQ = 1100.0
_DEBIT = 250.0



def _journal(tmp_path, debit=_DEBIT):
    p = tmp_path / "trades.log"
    p.write_text(json.dumps({"contract_id": 101, "debit": debit, "expiry": _FUTURE_EXPIRY,
                             "decision_id": "decision-1", "order_ref": "decision-1"}) + "\n")
    return str(p)


def _working_buy(status="PreSubmitted"):
    """Public API contract; production-derived narrative omitted."""
    return SimpleNamespace(
        order=SimpleNamespace(action="BUY", orderRef="decision-1", lmtPrice=2.5,
                              totalQuantity=1, permId=9001, orderId=9001, clientId=93),
        contract=SimpleNamespace(lastTradeDateOrContractMonth=_FUTURE_EXPIRY, conId=101,
                                 symbol="SPY", secType="OPT"),
        orderStatus=SimpleNamespace(status=status, remaining=1, filled=0))


class _FakeIB:
    """Public API contract; production-derived narrative omitted."""

    def __init__(self, answer):
        self.answer = answer
        self.calls = 0

    def isConnected(self):
        return True

    async def reqAllOpenOrdersAsync(self):
        self.calls += 1
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


class _FakeConn:
    def __init__(self, ib, positions=None):
        self.ib = ib
        self._positions = positions or {}

    async def get_positions(self):
        return dict(self._positions)


@pytest.fixture
def slate(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    def _wire(answer, positions=None):
        ib = _FakeIB(answer)
        monkeypatch.setattr(dr, "CONN", _FakeConn(ib, positions))
        monkeypatch.setattr(dr, "JOURNAL_PATH", _journal(tmp_path))
        return ib
    return _wire



def test_no_production_caller_omits_open_orders():
    """Public API contract; production-derived narrative omitted."""
    calls = []
    for name in ("daily_recommend.py", "place_trade.py"):
        text = (_APP / name).read_text()
        for m in re.finditer(r"construction\.open_book\(", text):
            line = text[:m.start()].count("\n") + 1
            tail, depth, taken = text[m.end():], 1, ""
            for ch in tail:
                depth += (ch == "(") - (ch == ")")
                if depth == 0:
                    break
                taken += ch
            if taken.count(",") < 2:
                calls.append("%s:%d" % (name, line))
    assert calls == [], "two-argument open_book() call sites remain: %s" % calls



@pytest.mark.asyncio
async def test_slate_open_book_folds_the_working_buy_and_the_budget_gate_refuses(slate):
    """Public API contract; production-derived narrative omitted."""
    slate([_working_buy()])
    book = await dr._open_book()
    assert len(book) == 1 and book[0][0] == _DEBIT, book
    dte = book[0][1]
    assert dte > 0

    ok_blind, _ = construction.check_budget(_DEBIT, dte, _NET_LIQ, [], ConstructionConfig())
    ok_seen, why = construction.check_budget(_DEBIT, dte, _NET_LIQ, book, ConstructionConfig())
    assert ok_blind is True, "the blind book waved this candidate through"
    assert ok_seen is False, "the complete book refuses it"
    assert any("deployed premium" in r for r in why), why


@pytest.mark.asyncio
async def test_slate_open_book_reports_a_genuinely_empty_book_as_empty(slate):
    """Public API contract; production-derived narrative omitted."""
    slate([])
    book = await dr._open_book()
    assert book == []
    ok, _ = construction.check_budget(_DEBIT, 480, _NET_LIQ, book, ConstructionConfig())
    assert ok is True



@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [
    TimeoutError(),
    RuntimeError("Not connected to IB"),
    None,
])
async def test_slate_open_book_refuses_an_unreadable_read(slate, answer):
    ib = slate(answer)
    with pytest.raises(Exception) as e:
        await dr._open_book()
    assert isinstance(e.value, dr.OpenBookUnreadable), type(e.value)
    assert "under-count" in str(e.value), "the refusal must say what the omission costs"
    assert ib.calls == 1


@pytest.mark.asyncio
async def test_slate_open_book_does_not_swallow_the_read_into_an_empty_book(slate):
    """Public API contract; production-derived narrative omitted."""
    slate(TimeoutError())
    try:
        book = await dr._open_book()
    except dr.OpenBookUnreadable:
        return
    pytest.fail("a failed open-order read returned a book (%r) instead of refusing" % (book,))


@pytest.mark.asyncio
async def test_slate_open_book_uses_the_module_connection_when_no_ib_is_passed(slate):
    ib = slate([_working_buy()])
    assert await dr._open_book() == await dr._open_book(ib)
    assert ib.calls == 2



@pytest.mark.asyncio
async def test_place_trade_returns_the_raw_trades_for_the_fold(tmp_path):
    ib = _FakeIB([_working_buy()])
    orders = await pt._verified_open_orders(ib)
    book = construction.open_book({}, _journal(tmp_path), orders)
    assert len(book) == 1 and book[0][0] == _DEBIT, book
    ok, why = construction.check_budget(_DEBIT, book[0][1], _NET_LIQ, book, ConstructionConfig())
    assert ok is False and any("deployed premium" in r for r in why), why


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [TimeoutError(), None])
async def test_place_trade_refuses_an_unreadable_read(answer):
    with pytest.raises(pt.OpenBookUnreadable) as e:
        await pt._verified_open_orders(_FakeIB(answer))
    assert "under-count" in str(e.value)


@pytest.mark.asyncio
async def test_place_trade_reports_a_genuinely_empty_book_as_empty():
    assert await pt._verified_open_orders(_FakeIB([])) == []


def test_place_trade_reads_the_orders_before_the_first_budget_gate():
    """Public API contract; production-derived narrative omitted."""
    text = (_APP / "place_trade.py").read_text()
    read_at = text.index("open_orders = await _verified_open_orders(")
    gates = [m.start() for m in re.finditer(r"construction\.open_book\(raw_positions", text)]
    assert len(gates) == 2, gates
    assert all(g > read_at for g in gates), "a budget gate is evaluated before the read is proved"



@pytest.mark.asyncio
async def test_get_open_orders_view_would_have_rebuilt_the_undercount(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    from exitmgr import trader

    entry = _working_buy()
    ib = _FakeIB([entry])
    conn = IBConnection(host="127.0.0.1", port=4001, client_id=1)
    conn.ib = ib
    conn._connected = True

    view = await conn.get_open_orders_view()
    assert view.readable is True, "the read itself succeeded -- this is not a failure case"
    assert view.orders == {}, "the long ENTRY is filtered out of the close-oriented view"

    entry_view = await trader.broker_entry_order_view(ib)
    assert entry_view.readable is True
    assert len(entry_view.trades) == 1

    journal = _journal(tmp_path)
    blind = construction.open_book({}, journal, list(view.orders.values()))
    seen = construction.open_book({}, journal, list(entry_view.trades))
    assert blind == [], "the close-oriented view reproduces the $0-deployed under-count"
    assert seen and seen[0][0] == _DEBIT, seen

    cons = ConstructionConfig()
    assert construction.check_budget(_DEBIT, seen[0][1], _NET_LIQ, blind, cons)[0] is True
    assert construction.check_budget(_DEBIT, seen[0][1], _NET_LIQ, seen, cons)[0] is False



@pytest.mark.asyncio
async def test_the_slate_posts_no_proposal_when_the_open_book_cannot_be_valued(
        tmp_path, monkeypatch, slate):
    """Public API contract; production-derived narrative omitted."""
    slate(TimeoutError())
    posted = []

    def _post(*a, **k):
        posted.append(a[-1])
        return "1724300000.%03d" % len(posted)
    monkeypatch.setattr(dr.approval, "post_proposal", _post)
    resolved = ResolvedOrder("SPY", "C", _FUTURE_EXPIRY, 500.0, 1, 2.5, dte=482)

    async def _fake_resolve(ib, idea, available, net_liq=None):
        return resolved, ""
    monkeypatch.setattr(dr, "_resolve", _fake_resolve)

    audit_path = str(tmp_path / "audit.jsonl")
    idea = TradeIdea("SPY", True, "bullish", "long call", 30, 0.35, 250.0, 6, "trend")
    pot = PotSnapshot(net_liq=_NET_LIQ, available_funds=_NET_LIQ, cash=_NET_LIQ)
    pending = []

    ts = await dr._post_idea(object(), idea, pot, 0.25, "tok", "chan", audit_path, pending)

    assert ts is None, "a proposal was posted against a book nobody could value"
    assert pending == [], "and it was queued for the one-tap watch loop"
    assert posted and "could not value the open book" in posted[-1], posted
    events = [json.loads(l)["event"] for l in Path(audit_path).read_text().splitlines() if l.strip()]
    assert "budget_book_unreadable" in events, events
    assert "daily_rec_posted" not in events, events


def test_the_swallowing_handler_is_gone():
    """Public API contract; production-derived narrative omitted."""
    text = (_APP / "daily_recommend.py").read_text()
    assert "[WARN] open-book fetch failed" not in text, \
        "the handler that turned a refused read into an empty book has returned"
